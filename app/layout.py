"""Reflow/pagination engine.

Re-breaks paragraph lines to the configured measure, inserts layout blank
lines within the configured ranges and paginates the result, while:

* never altering a single braille cell of the source content (every source
  line, including blank ones, appears in the output stream),
* never moving an explicit (locked) page break,
* keeping headings on one page and together with the next lines,
* keeping tables unsplit (unless a table is taller than a page — reported
  as an unsatisfiable rule),
* honouring paragraph widow/orphan minima where possible.

Candidate solutions are produced with several cost profiles and ranked by
(page count, violations, change amount).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .braille import BLANK_CELL, is_blank, is_blank_line
from .content import Block, ParsedContent, build_blocks, parse_content
from .geometry import compute_grid
from .schemas import JobConfig, LayoutParams, Structure

KIND_HEADING = "heading"
KIND_PARAGRAPH = "paragraph"
KIND_TABLE = "table"
KIND_BLANK_CONTENT = "blank_content"  # blank line present in the source
KIND_BLANK_LAYOUT = "blank_layout"  # blank line inserted by the layout engine


@dataclass
class OutLine:
    text: str
    kind: str
    block_id: Optional[int]
    source_lines: list[int] = field(default_factory=list)
    orig_page: Optional[int] = None  # 1-based original page of the first source line


# ---------------------------------------------------------------------------
# Paragraph wrapping
# ---------------------------------------------------------------------------


def _wrap_chars(
    chars: list[tuple[str, int]], width: int, blank_only: bool
) -> tuple[list[list[tuple[str, int]]], int]:
    """Greedy wrap of a character stream; returns (lines, forced_breaks).

    Breaks happen at blank cells (the blank stays at the end of the line, so
    no cell is lost).  When ``blank_only`` is set and no blank is available,
    the line is hard-broken and a forced mid-word break is counted.
    """
    lines: list[list[tuple[str, int]]] = []
    cur: list[tuple[str, int]] = []
    last_blank = -1
    forced = 0
    for i, (ch, src) in enumerate(chars):
        cur.append((ch, src))
        if is_blank(ch):
            last_blank = len(cur) - 1
        if len(cur) == width and i < len(chars) - 1:
            if blank_only and last_blank < 0:
                lines.append(cur)
                cur = []
                forced += 1
            elif blank_only:
                lines.append(cur[: last_blank + 1])
                cur = cur[last_blank + 1 :]
            else:
                lines.append(cur)
                cur = []
            last_blank = -1
            for k, (c2, _) in enumerate(cur):
                if is_blank(c2):
                    last_blank = k
    if cur:
        lines.append(cur)
    return lines, forced


# ---------------------------------------------------------------------------
# Output-line stream construction
# ---------------------------------------------------------------------------


@dataclass
class _Stream:
    """Intermediate representation fed into the paginator."""

    out: list[OutLine]
    must_break: set[int]  # out indices that must start a new page
    forced_mid_word_breaks: int
    blocks: list[Block]


def _blanks_between(prev_type: Optional[str], next_type: str, params: LayoutParams) -> int:
    if prev_type is None:
        return 0
    if prev_type == KIND_TABLE or next_type == KIND_TABLE:
        return params.blank_lines_around_table
    if prev_type == KIND_HEADING:
        return params.blank_line_after_heading
    if next_type == KIND_HEADING:
        return params.blank_line_before_heading
    return params.blank_lines_between_paragraphs


def _build_stream(
    parsed: ParsedContent, structure: Structure, params: LayoutParams, width: int
) -> _Stream:
    blocks = build_blocks(parsed, structure)
    breaks_after = parsed.explicit_break_after
    out: list[OutLine] = []
    must_break: set[int] = set()
    forced_breaks = 0

    def emit_source_blank(ln_no: int) -> None:
        ln = parsed.lines[ln_no]
        out.append(
            OutLine(
                text="",
                kind=KIND_BLANK_CONTENT,
                block_id=None,
                source_lines=[ln.no],
                orig_page=ln.page,
            )
        )
        if ln.no in breaks_after:
            must_break.add(len(out))

    cursor = 0
    prev_type: Optional[str] = None
    for block_id, block in enumerate(blocks):
        # source lines not covered by any block (blank separators)
        for ln_no in range(cursor, block.line_start):
            emit_source_blank(ln_no)
        # locked page boundary immediately before this block
        if block.line_start > 0 and (block.line_start - 1) in breaks_after:
            must_break.add(len(out))
        # inter-block layout blanks: top up to the configured count, taking
        # already-present source blank lines into account
        want = _blanks_between(prev_type, block.type, params)
        existing = 0
        for ln in reversed(out):
            if ln.kind == KIND_BLANK_CONTENT:
                existing += 1
            else:
                break
        for _ in range(max(0, want - existing)):
            out.append(OutLine(text="", kind=KIND_BLANK_LAYOUT, block_id=None))

        block_lines = parsed.lines[block.line_start : block.line_end]
        if block.type == "paragraph":
            forced_breaks += _emit_paragraph(
                block_id, block_lines, breaks_after, params, width, out, must_break, parsed
            )
        else:
            kind = KIND_HEADING if block.type == "heading" else KIND_TABLE
            for ln in block_lines:
                out.append(
                    OutLine(
                        text=ln.text,
                        kind=kind,
                        block_id=block_id,
                        source_lines=[ln.no],
                        orig_page=ln.page,
                    )
                )
                if ln.no in breaks_after:
                    must_break.add(len(out))
        prev_type = block.type
        cursor = block.line_end

    for ln_no in range(cursor, len(parsed.lines)):
        emit_source_blank(ln_no)

    return _Stream(
        out=out,
        must_break={i for i in must_break if 0 < i < len(out)},
        forced_mid_word_breaks=forced_breaks,
        blocks=blocks,
    )


def _emit_paragraph(
    block_id: int,
    block_lines: list,
    breaks_after: set[int],
    params: LayoutParams,
    width: int,
    out: list[OutLine],
    must_break: set[int],
    parsed: ParsedContent,
) -> int:
    """Emit wrapped lines of one paragraph block; returns forced breaks."""
    forced_breaks = 0
    run: list[tuple[str, int]] = []

    def flush() -> None:
        nonlocal forced_breaks
        if not run:
            return
        wrapped, forced = _wrap_chars(run, width, params.break_at_blank_only)
        forced_breaks += forced
        for wline in wrapped:
            srcs = sorted({s for _, s in wline})
            out.append(
                OutLine(
                    text="".join(c for c, _ in wline),
                    kind=KIND_PARAGRAPH,
                    block_id=block_id,
                    source_lines=srcs,
                    orig_page=parsed.lines[srcs[0]].page,
                )
            )
        run.clear()

    for ln in block_lines:
        if is_blank_line(ln.text):
            flush()
            out.append(
                OutLine(
                    text="",
                    kind=KIND_BLANK_CONTENT,
                    block_id=block_id,
                    source_lines=[ln.no],
                    orig_page=ln.page,
                )
            )
        else:
            if run:
                run.append((BLANK_CELL, ln.no))  # join source lines with one blank cell
            run.extend((ch, ln.no) for ch in ln.text)
        if ln.no in breaks_after:
            flush()
            must_break.add(len(out))
    flush()
    return forced_breaks


# ---------------------------------------------------------------------------
# Constraint computation
# ---------------------------------------------------------------------------


@dataclass
class _Constraints:
    allowed: list[bool]  # allowed[i]: a page may start at out line i
    forced: list[bool]  # forced[i]: a page must start at out line i
    droppable: list[bool]  # droppable[i]: layout blank, dropped at page starts
    lead_drop: list[int]  # count of consecutive droppables starting at i
    para_of: list[Optional[int]]  # paragraph group id per out line
    para_span: list[tuple[int, int]]  # group id -> [start, end)
    soft_viol: list[list[str]]  # soft rule names triggered by breaking before i
    hard_rule: list[Optional[str]]  # rule responsible for allowed[i] == False
    unsatisfiable: list[dict]  # rules no solution can satisfy


def _compute_constraints(
    stream: _Stream, params: LayoutParams, capacity: int, width: int
) -> _Constraints:
    out = stream.out
    n = len(out)
    allowed = [True] * (n + 1)
    forced = [False] * (n + 1)
    for i in stream.must_break:
        forced[i] = True
    droppable = [ln.kind == KIND_BLANK_LAYOUT for ln in out] + [False]
    lead_drop = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        lead_drop[i] = (lead_drop[i + 1] + 1) if droppable[i] else 0
    soft_viol: list[list[str]] = [[] for _ in range(n + 1)]
    hard_rule: list[Optional[str]] = [None] * (n + 1)
    unsatisfiable: list[dict] = []

    # paragraph groups: consecutive out lines of one paragraph block
    para_of: list[Optional[int]] = [None] * n
    para_span: list[list[int]] = []
    gid = -1
    prev_bid: Optional[int] = None
    for i, ln in enumerate(out):
        if ln.kind in (KIND_PARAGRAPH, KIND_BLANK_CONTENT) and ln.block_id is not None:
            if ln.block_id != prev_bid:
                gid += 1
                para_span.append([i, i])
            para_of[i] = gid
            para_span[gid][1] = i
            prev_bid = ln.block_id
        else:
            prev_bid = None

    def forbid(start: int, end: int, rule: str, detail: str) -> None:
        """Forbid breaks inside (start, end]; soften if taller than a page."""
        if end - start + 1 > capacity:
            unsatisfiable.append({"rule": rule, "detail": detail})
            for k in range(start + 1, end + 1):
                soft_viol[k].append(rule)
        else:
            for k in range(start + 1, end + 1):
                allowed[k] = False
                hard_rule[k] = rule

    # block spans in out coordinates
    block_span: dict[int, list[int]] = {}
    for i, ln in enumerate(out):
        if ln.block_id is not None:
            span = block_span.setdefault(ln.block_id, [i, i])
            span[1] = i

    blocks = stream.blocks
    for bid, (s, e) in block_span.items():
        btype = blocks[bid].type
        if btype == "heading":
            forbid(s, e, "heading_keep_together", "heading block is taller than one page")
        elif btype == "table":
            if params.table_keep_together:
                forbid(s, e, "table_keep_together", "table is taller than one page")
        elif btype == "paragraph" and not params.paragraph_may_span_pages:
            forbid(s, e, "paragraph_may_span_pages", "paragraph is taller than one page")

    # heading keep-with-next
    keep = params.heading_keep_with_next_lines
    if keep > 0:
        ordered = sorted(block_span.items())
        for idx, (bid, (s, e)) in enumerate(ordered):
            if blocks[bid].type != "heading":
                continue
            nxt = None
            for bid2, (s2, e2) in ordered[idx + 1 :]:
                nxt = (s2, e2)
                break
            if nxt is None:
                continue
            s2, e2 = nxt
            content_seen = 0
            k_end = e2
            for k in range(s2, e2 + 1):
                if out[k].kind != KIND_BLANK_LAYOUT:
                    content_seen += 1
                if content_seen >= keep:
                    k_end = k
                    break
            forbid(
                s,
                k_end,
                "heading_keep_with_next",
                f"heading plus {keep} following line(s) is taller than one page",
            )

    # widow/orphan impossibility
    min_end = params.paragraph_min_lines_at_page_end
    min_start = params.paragraph_min_lines_at_page_start
    if min_end > capacity or min_start > capacity:
        unsatisfiable.append(
            {
                "rule": "widow_orphan",
                "detail": "paragraph min lines at page start/end exceed the page capacity",
            }
        )
    else:
        for s, e in para_span:
            h = e - s + 1
            if h > capacity and h < min_end + min_start:
                unsatisfiable.append(
                    {
                        "rule": "widow_orphan",
                        "detail": f"paragraph of {h} lines must split but cannot keep "
                        f"{min_end}/{min_start} lines on both sides",
                    }
                )

    # content lines wider than the measure
    for i, ln in enumerate(out):
        if len(ln.text) > width:
            unsatisfiable.append(
                {
                    "rule": "line_too_long",
                    "detail": f"out line {i + 1} has {len(ln.text)} cells, measure is {width}",
                }
            )

    # locked breaks override soft permissions
    for i in stream.must_break:
        allowed[i] = True
        hard_rule[i] = None

    return _Constraints(
        allowed=allowed,
        forced=forced,
        droppable=droppable,
        lead_drop=lead_drop,
        para_of=para_of,
        para_span=[(s, e + 1) for s, e in para_span],
        soft_viol=soft_viol,
        hard_rule=hard_rule,
        unsatisfiable=unsatisfiable,
    )


def _relaxed(cons: _Constraints) -> _Constraints:
    """Copy of the constraints with every hard rule softened.

    Used as a fallback when the strict problem is infeasible (e.g. two
    glued spans overlap into a region taller than one page).
    """
    allowed = list(cons.allowed)
    soft_viol = [list(v) for v in cons.soft_viol]
    for k, rule in enumerate(cons.hard_rule):
        if rule is not None and not allowed[k]:
            allowed[k] = True
            soft_viol[k] = soft_viol[k] + [rule]
    return _Constraints(
        allowed=allowed,
        forced=cons.forced,
        droppable=cons.droppable,
        lead_drop=cons.lead_drop,
        para_of=cons.para_of,
        para_span=cons.para_span,
        soft_viol=soft_viol,
        hard_rule=[None] * len(cons.hard_rule),
        unsatisfiable=cons.unsatisfiable,
    )


# ---------------------------------------------------------------------------
# Paginator (dynamic programming)
# ---------------------------------------------------------------------------

_PROFILES = [
    {"name": "balanced", "page_w": 100.0, "viol_w": 10000.0, "new_break_w": 5.0, "blank_w": 2.0},
    {"name": "compact", "page_w": 100000.0, "viol_w": 10000.0, "new_break_w": 5.0, "blank_w": 2.0},
    {"name": "faithful", "page_w": 100.0, "viol_w": 10000.0, "new_break_w": 2000.0, "blank_w": 2.0},
]


def _paginate(
    stream: _Stream, cons: _Constraints, params: LayoutParams, capacity: int, profile: dict
) -> Optional[list[tuple[int, int]]]:
    """Return page segments [start, end) over out lines, or None if infeasible."""
    n = len(stream.out)
    if n == 0:
        return []
    page_w = profile["page_w"]
    viol_w = profile["viol_w"]
    new_break_w = profile["new_break_w"]
    blank_w = profile["blank_w"]

    forced_ps = [0] * (n + 1)
    for k in range(1, n + 1):
        forced_ps[k] = forced_ps[k - 1] + (1 if cons.forced[k] else 0)

    min_end = params.paragraph_min_lines_at_page_end
    min_start = params.paragraph_min_lines_at_page_start

    def break_cost(j: int, i: int) -> float:
        c = 0.0
        if not cons.forced[j]:
            c += new_break_w
        c += blank_w * min(cons.lead_drop[j], i - j)
        gid = cons.para_of[j]
        if gid is not None and j > 0 and cons.para_of[j - 1] == gid:
            start, end = cons.para_span[gid]
            if j - start < min_end:
                c += viol_w
            if end - j < min_start:
                c += viol_w
        c += viol_w * len(cons.soft_viol[j])
        return c

    max_blank_run = max(cons.lead_drop) if cons.lead_drop else 0
    INF = float("inf")
    dp = [INF] * (n + 1)
    bp = [-1] * (n + 1)
    dp[0] = 0.0
    for i in range(1, n + 1):
        for j in range(i - 1, -1, -1):
            span = i - j
            eff = span - min(cons.lead_drop[j], span)
            if eff > capacity:
                if span > capacity + max_blank_run:
                    break
                continue
            if dp[j] == INF:
                continue
            if forced_ps[i - 1] - forced_ps[j] > 0:
                continue  # a locked break falls inside this page
            if j > 0 and not (cons.allowed[j] or cons.forced[j]):
                continue
            cost = dp[j] + page_w + (break_cost(j, i) if j > 0 else 0.0)
            if cost < dp[i] - 1e-9:
                dp[i] = cost
                bp[i] = j
    if dp[n] == INF:
        return None
    segments: list[tuple[int, int]] = []
    i = n
    while i > 0:
        j = bp[i]
        segments.append((j, i))
        i = j
    segments.reverse()
    return segments


# ---------------------------------------------------------------------------
# Solution assembly
# ---------------------------------------------------------------------------


def _assemble(
    stream: _Stream,
    cons: _Constraints,
    segments: list[tuple[int, int]],
    params: LayoutParams,
    profile_name: str,
) -> dict:
    out = stream.out
    pages: list[dict] = []
    mapping: dict[str, list[dict]] = {}
    violations: list[dict] = []
    dropped_blanks = 0
    min_end = params.paragraph_min_lines_at_page_end
    min_start = params.paragraph_min_lines_at_page_start

    page_no = 0
    for s, e in segments:
        start = s
        while start < e and cons.droppable[start]:
            start += 1
        if start == e:
            dropped_blanks += e - s
            continue  # segment of droppable blanks only: no page emitted
        dropped_blanks += start - s
        page_no += 1
        page_lines: list[dict] = []
        for idx in range(start, e):
            ln = out[idx]
            row = len(page_lines) + 1
            page_lines.append(
                {
                    "row": row,
                    "text": ln.text,
                    "kind": ln.kind,
                    "block_id": ln.block_id,
                    "source_lines": ln.source_lines,
                }
            )
            for src in ln.source_lines:
                mapping.setdefault(str(src), []).append({"page": page_no, "row": row})
        pages.append(
            {
                "page": page_no,
                "sheet": (page_no + 1) // 2,
                "side": "front" if page_no % 2 == 1 else "back",
                "lines": page_lines,
            }
        )
        if page_no > 1:
            gid = cons.para_of[start]
            if gid is not None and start > 0 and cons.para_of[start - 1] == gid:
                gstart, gend = cons.para_span[gid]
                before = start - gstart
                after = gend - start
                if before < min_end:
                    violations.append(
                        {
                            "rule": "paragraph_min_lines_at_page_end",
                            "page": page_no - 1,
                            "detail": f"only {before} paragraph line(s) at the bottom of page "
                            f"{page_no - 1}, minimum is {min_end}",
                        }
                    )
                if after < min_start:
                    violations.append(
                        {
                            "rule": "paragraph_min_lines_at_page_start",
                            "page": page_no,
                            "detail": f"only {after} paragraph line(s) at the top of page "
                            f"{page_no}, minimum is {min_start}",
                        }
                    )
            for rule in cons.soft_viol[start]:
                violations.append(
                    {
                        "rule": rule,
                        "page": page_no,
                        "detail": f"rule '{rule}' could not be honoured at the break "
                        f"before page {page_no}",
                    }
                )

    if stream.forced_mid_word_breaks:
        violations.append(
            {
                "rule": "break_at_blank_only",
                "page": None,
                "detail": f"{stream.forced_mid_word_breaks} forced mid-word line break(s)",
            }
        )
    if params.max_pages is not None and len(pages) > params.max_pages:
        violations.append(
            {
                "rule": "max_pages",
                "page": None,
                "detail": f"{len(pages)} pages exceed the budget of {params.max_pages}",
            }
        )

    # change amount vs. the original pagination
    moved = 0
    page_of_seg: list[int] = []
    pno = 0
    for s, e in segments:
        start = s
        while start < e and cons.droppable[start]:
            start += 1
        if start == e:
            continue
        pno += 1
        for idx in range(start, e):
            ln = out[idx]
            if ln.orig_page is not None and ln.orig_page != pno:
                moved += 1
    rewrap_delta = 0
    block_out_lines: dict[int, int] = {}
    for ln in out:
        if ln.block_id is not None and ln.kind in (KIND_PARAGRAPH, KIND_BLANK_CONTENT):
            block_out_lines[ln.block_id] = block_out_lines.get(ln.block_id, 0) + 1
    for bid, count in block_out_lines.items():
        src_count = stream.blocks[bid].line_end - stream.blocks[bid].line_start
        rewrap_delta += abs(count - src_count)
    added_blanks = sum(1 for ln in out if ln.kind == KIND_BLANK_LAYOUT)
    changes = moved + rewrap_delta + added_blanks + dropped_blanks

    return {
        "profile": profile_name,
        "pages": pages,
        "mapping": mapping,
        "violations": violations,
        "unsatisfied_rules": list(cons.unsatisfiable),
        "stats": {
            "pages": len(pages),
            "violations": len(violations),
            "changes": changes,
            "moved_lines": moved,
            "rewrap_delta": rewrap_delta,
            "added_layout_blanks": added_blanks,
            "dropped_layout_blanks": dropped_blanks,
        },
    }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def run_layout(
    content: str, structure: Structure, cfg: JobConfig, params: LayoutParams
) -> dict:
    """Reflow the content and return ranked candidate solutions."""
    grid = compute_grid(cfg)
    width = params.cells_per_line or grid.cols
    capacity = params.lines_per_page or grid.rows
    if width <= 0 or capacity <= 0:
        raise ValueError("grid capacity is zero: check paper, margins and pitches")

    parsed = parse_content(content)
    stream = _build_stream(parsed, structure, params, width)
    cons = _compute_constraints(stream, params, capacity, width)

    solutions: list[dict] = []
    seen: set[tuple] = set()

    def collect(cons_obj: _Constraints) -> None:
        for profile in _PROFILES:
            segments = _paginate(stream, cons_obj, params, capacity, profile)
            if segments is None:
                continue
            key = tuple(segments)
            if key in seen:
                continue
            seen.add(key)
            solutions.append(_assemble(stream, cons_obj, segments, params, profile["name"]))

    collect(cons)
    if not solutions:
        # strict problem infeasible: soften every hard rule and retry
        collect(_relaxed(cons))

    solutions.sort(
        key=lambda s: (s["stats"]["pages"], s["stats"]["violations"], s["stats"]["changes"])
    )
    for rank, sol in enumerate(solutions, start=1):
        sol["rank"] = rank
    solutions = solutions[: params.max_solutions]

    return {
        "params": params.model_dump(),
        "grid": {
            "cols": grid.cols,
            "rows": grid.rows,
            "measure_cells": width,
            "page_lines": capacity,
        },
        "unsatisfiable_rules": list(cons.unsatisfiable),
        "solutions": solutions,
    }


def solution_from_original(content: str, cfg: JobConfig, params: LayoutParams) -> dict:
    """A 'solution' that keeps the original pagination exactly as supplied."""
    grid = compute_grid(cfg)
    width = params.cells_per_line or grid.cols
    capacity = params.lines_per_page or grid.rows
    parsed = parse_content(content)

    pages: list[list[dict]] = []
    mapping: dict[str, list[dict]] = {}
    violations: list[dict] = []
    for ln in parsed.lines:
        while len(pages) < ln.page:
            pages.append([])
        pages[ln.page - 1].append(
            {
                "row": ln.row,
                "text": ln.text,
                "kind": "content",
                "block_id": None,
                "source_lines": [ln.no],
            }
        )
        mapping.setdefault(str(ln.no), []).append({"page": ln.page, "row": ln.row})
    if not pages:
        pages = [[]]

    out_pages = []
    for pno, lines in enumerate(pages, start=1):
        if capacity and len(lines) > capacity:
            violations.append(
                {
                    "rule": "page_overflow",
                    "page": pno,
                    "detail": f"page {pno} has {len(lines)} lines, capacity is {capacity}",
                }
            )
        for line in lines:
            if width and len(line["text"]) > width:
                violations.append(
                    {
                        "rule": "line_too_long",
                        "page": pno,
                        "detail": f"row {line['row']} has {len(line['text'])} cells, "
                        f"measure is {width}",
                    }
                )
        out_pages.append(
            {
                "page": pno,
                "sheet": (pno + 1) // 2,
                "side": "front" if pno % 2 == 1 else "back",
                "lines": lines,
            }
        )

    return {
        "profile": "original",
        "rank": 0,
        "pages": out_pages,
        "mapping": mapping,
        "violations": violations,
        "unsatisfied_rules": [],
        "stats": {
            "pages": len(out_pages),
            "violations": len(violations),
            "changes": 0,
            "moved_lines": 0,
            "rewrap_delta": 0,
            "added_layout_blanks": 0,
            "dropped_layout_blanks": 0,
        },
    }
