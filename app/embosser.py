"""Embosser job compiler: confirmed version -> embosser byte streams.

Reads a confirmed version, checks its layout grid against the embosser's
capacity and produces sendable BRF or UTF-8 Braille byte streams, plus a
JSON work ticket (工单) recording paper order, reload orientation, byte
checksums and the cell mapping.

Pass planning
-------------
* ``native_duplex`` — one file; front and back pages are paired per sheet:
  sheet 1 front, sheet 1 back, sheet 2 front, ...
* ``simplex_manual`` — two files.  The *front* pass prints all front pages
  in sheet order.  For the *back* pass the printed stack must be reloaded;
  the plan depends on the job's flip mode (assuming the embosser stacks its
  output printed-side-up, last printed sheet on top):

  * ``page_flip`` — the operator flips the whole output stack around the
    vertical axis (left-to-right, like a book page).  Flipping the stack as
    one block reverses the sheet order, so the back pass is emitted in
    **forward** sheet order; reload direction ``left_right``.
  * ``top_flip`` — the tumbled stack would present its trailing edge to the
    feeder, so sheets are re-fed one at a time from the top of the output
    stack, each tumbled top-to-bottom.  The output stack yields the last
    printed sheet first, so the back pass is emitted in **reverse** sheet
    order; reload direction ``top_bottom``.

The compiler never rewrites braille cells or explicit page breaks: page
boundaries come from the version, and every emitted byte maps one-to-one to
a version cell (``line_advance`` page control only appends blank lines).

Every compile error carries the page, row, column and the source pass so
the operator can locate the offending cell.  The readback helpers re-parse
a byte stream and verify page order, page breaks and the cell mapping
against the original version.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

from .braille import BRF_ASCII, UNICODE_TO_BRF, is_braille, mask_of, uses_8dot
from .geometry import compute_grid
from .schemas import EmbosserConfig, JobConfig

#: BRF character -> 6-dot mask (inverse of the BRF table).
BRF_CHAR_TO_MASK = {ch: i for i, ch in enumerate(BRF_ASCII)}

LINE_ENDING_BYTES = {"lf": b"\n", "crlf": b"\r\n", "cr": b"\r"}
FORM_FEED_BYTE = 0x0C

PASS_DUPLEX = "duplex"
PASS_FRONT = "front"
PASS_BACK = "back"

#: Reload plans for the manual (simplex) back pass, per flip mode.
RELOAD_PLANS: dict[str, dict] = {
    "page_flip": {
        "direction": "left_right",
        "sheet_order": "forward",
        "reversed": False,
        "instruction": (
            "flip the whole printed stack around the vertical axis "
            "(left-to-right, like a book page) and reload it; flipping the "
            "stack as one block reverses the sheet order, so the back pass "
            "prints in forward sheet order"
        ),
    },
    "top_flip": {
        "direction": "top_bottom",
        "sheet_order": "reverse",
        "reversed": True,
        "instruction": (
            "re-feed the printed sheets one at a time from the top of the "
            "output stack, tumbling each sheet top-to-bottom; the output "
            "stack yields the last printed sheet first, so the back pass "
            "prints in reverse sheet order"
        ),
    },
}


# ---------------------------------------------------------------------------
# Plan pages and passes
# ---------------------------------------------------------------------------


@dataclass
class PlanPage:
    sheet: int
    side: str  # "front" | "back"
    version_page: Optional[int]  # None for a padded blank page
    lines: list[dict]  # version line dicts: row / text / source_lines
    padded: bool = False


@dataclass
class PassPlan:
    name: str  # "duplex" | "front" | "back"
    pages: list[PlanPage] = field(default_factory=list)
    reload: Optional[dict] = None


def _err(itype: str, detail: str, source: Optional[str], **extra: Any) -> dict:
    err = {"type": itype, "detail": detail, "source": source}
    err.update(extra)
    return err


def _collect_sheets(snapshot: dict) -> list[dict]:
    """Pair version pages into sheets: [{front: PlanPage, back: PlanPage}]."""
    sheets: dict[int, dict[str, PlanPage]] = {}
    for p in snapshot["solution"]["pages"]:
        entry = sheets.setdefault(p["sheet"], {})
        entry[p["side"]] = PlanPage(
            sheet=p["sheet"], side=p["side"], version_page=p["page"], lines=p["lines"]
        )
    return [sheets[k] for k in sorted(sheets)]


def _reserved_bytes(emb: EmbosserConfig) -> set[int]:
    """Bytes the device reserves for line/page control."""
    reserved = set(LINE_ENDING_BYTES[emb.line_ending])
    if emb.page_break == "form_feed":
        reserved.add(FORM_FEED_BYTE)
    reserved.update(emb.extra_control_bytes)
    return reserved


def _encode_char(ch: str, emb: EmbosserConfig) -> Optional[bytes]:
    """Encoded bytes of one cell, or None if the encoding cannot represent it."""
    if emb.input_encoding == "brf":
        b = UNICODE_TO_BRF.get(ch)  # the BRF table covers 6-dot cells only
        return b.encode("ascii") if b is not None else None
    return ch.encode("utf-8")


def _validate_page(page: PlanPage, emb: EmbosserConfig, source: str, errors: list[dict]) -> None:
    """Check one version page against the device capacity and encoding."""
    vp = page.version_page
    loc = {"page": vp, "sheet": page.sheet, "side": page.side}
    if len(page.lines) > emb.lines_per_page:
        errors.append(
            _err(
                "page_overflow",
                f"page has {len(page.lines)} lines, device allows {emb.lines_per_page}",
                source,
                **loc,
                row=emb.lines_per_page + 1,  # first row beyond the device page
                col=None,
                lines=len(page.lines),
                max_lines=emb.lines_per_page,
            )
        )
    reserved = _reserved_bytes(emb)
    for line in page.lines:
        text = line["text"]
        row = line["row"]
        if len(text) > emb.cells_per_line:
            errors.append(
                _err(
                    "line_too_long",
                    f"line has {len(text)} cells, device allows {emb.cells_per_line}",
                    source,
                    **loc,
                    row=row,
                    col=emb.cells_per_line + 1,  # first column beyond the device width
                    cells=len(text),
                    max_cells=emb.cells_per_line,
                )
            )
        for col, ch in enumerate(text, start=1):
            if not is_braille(ch):
                errors.append(
                    _err(
                        "unencodable_character",
                        f"character {ch!r} (U+{ord(ch):04X}) is not a braille pattern",
                        source,
                        **loc,
                        row=row,
                        col=col,
                        char=ch,
                        codepoint=f"U+{ord(ch):04X}",
                    )
                )
                continue
            if uses_8dot(ch) and not emb.supports_8dot:
                errors.append(
                    _err(
                        "eight_dot_not_supported",
                        "cell uses dot 7/8 but the device is 6-dot only",
                        source,
                        **loc,
                        row=row,
                        col=col,
                        char=ch,
                        codepoint=f"U+{ord(ch):04X}",
                    )
                )
                continue
            encoded = _encode_char(ch, emb)
            if encoded is None:
                errors.append(
                    _err(
                        "unencodable_character",
                        "8-dot cell cannot be encoded in BRF (Braille ASCII)",
                        source,
                        **loc,
                        row=row,
                        col=col,
                        char=ch,
                        codepoint=f"U+{ord(ch):04X}",
                    )
                )
                continue
            conflict = sorted(set(encoded) & reserved)
            if conflict:
                errors.append(
                    _err(
                        "control_byte_conflict",
                        "cell encodes to reserved control byte(s) "
                        + ", ".join(f"0x{b:02X}" for b in conflict),
                        source,
                        **loc,
                        row=row,
                        col=col,
                        char=ch,
                        codepoint=f"U+{ord(ch):04X}",
                        bytes=[f"0x{b:02X}" for b in conflict],
                    )
                )


def build_plan(snapshot: dict, emb: EmbosserConfig) -> tuple[Optional[list[PassPlan]], list[dict], dict]:
    """Pair sheets, validate against the device and plan the passes.

    Returns ``(passes, errors, context)``; ``passes`` is None when errors
    were found.
    """
    cfg = JobConfig(**snapshot["config"])
    grid = compute_grid(cfg)
    context = {
        "version_id": snapshot.get("version_id"),
        "job_id": snapshot.get("job_id"),
        "job_name": snapshot.get("job_name"),
        "content_sha256": snapshot.get("content_sha256"),
        "flip_mode": cfg.flip_mode,
        "binding_edge": cfg.binding_edge,
        "duplex_mode": emb.duplex_mode,
        "grid": {"cols": grid.cols, "rows": grid.rows, "cell_dot_rows": grid.cell_dot_rows},
        "grid_check": {
            "version_grid": {"cols": grid.cols, "rows": grid.rows},
            "device": {"cells_per_line": emb.cells_per_line, "lines_per_page": emb.lines_per_page},
            "fits": grid.cols <= emb.cells_per_line and grid.rows <= emb.lines_per_page,
        },
    }

    sheets = _collect_sheets(snapshot)

    def source_of(side: str) -> str:
        return PASS_DUPLEX if emb.duplex_mode == "native_duplex" else side

    # missing back sides
    errors: list[dict] = []
    for sheet in sheets:
        front = sheet.get("front")
        if front is not None and sheet.get("back") is None:
            if emb.pad_missing_back:
                sheet["back"] = PlanPage(
                    sheet=front.sheet, side="back", version_page=None, lines=[], padded=True
                )
            else:
                errors.append(
                    _err(
                        "missing_back_side",
                        f"sheet {front.sheet} has a front page ({front.version_page}) "
                        "but no back page",
                        source_of("back"),
                        page=front.version_page,
                        sheet=front.sheet,
                        side="back",
                        row=None,
                        col=None,
                    )
                )

    # capacity / encoding checks
    for sheet in sheets:
        for side in ("front", "back"):
            page = sheet.get(side)
            if page is not None:
                _validate_page(page, emb, source_of(side), errors)
    if errors:
        return None, errors, context

    # pass planning
    if emb.duplex_mode == "native_duplex":
        pages: list[PlanPage] = []
        for sheet in sheets:
            for side in ("front", "back"):
                page = sheet.get(side)
                if page is not None:
                    pages.append(page)
        passes = [PassPlan(PASS_DUPLEX, pages)]
    else:
        front_pages = [s["front"] for s in sheets if s.get("front") is not None]
        back_pages = [s["back"] for s in sheets if s.get("back") is not None]
        reload = dict(RELOAD_PLANS[cfg.flip_mode])
        if reload["reversed"]:
            back_pages = list(reversed(back_pages))
        passes = [
            PassPlan(PASS_FRONT, front_pages),
            PassPlan(PASS_BACK, back_pages, reload=reload),
        ]
    return passes, [], context


# ---------------------------------------------------------------------------
# Byte stream generation
# ---------------------------------------------------------------------------


def _encode_pass(plan: PassPlan, emb: EmbosserConfig) -> bytes:
    """Encode one pass; never alters cells or page boundaries of the version."""
    le = LINE_ENDING_BYTES[emb.line_ending]
    out = bytearray()
    for page in plan.pages:
        for line in page.lines:
            for ch in line["text"]:
                encoded = _encode_char(ch, emb)
                if encoded is not None:  # validated beforehand
                    out += encoded
            out += le
        if emb.page_break == "line_advance":
            out += le * (emb.lines_per_page - len(page.lines))
        elif emb.page_break == "form_feed":
            out += bytes([FORM_FEED_BYTE])
    return bytes(out)


def _paper_order(plan: PassPlan) -> list[dict]:
    return [
        {
            "file_page": i,
            "sheet": page.sheet,
            "side": page.side,
            "version_page": page.version_page,
            "padded": page.padded,
        }
        for i, page in enumerate(plan.pages, start=1)
    ]


def _cell_mapping(plan: PassPlan) -> list[dict]:
    return [
        {
            "file_page": i,
            "sheet": page.sheet,
            "side": page.side,
            "version_page": page.version_page,
            "lines": [
                {"row": line["row"], "text": line["text"], "source_lines": line["source_lines"]}
                for line in page.lines
            ],
        }
        for i, page in enumerate(plan.pages, start=1)
    ]


def compile_version(snapshot: dict, emb: EmbosserConfig) -> dict:
    """Compile a version snapshot for an embosser profile.

    Returns ``{"ok": True, "context": ..., "passes": [...]}`` where each pass
    carries its byte stream, checksum, paper order and cell mapping, or
    ``{"ok": False, "errors": [...]}`` when the version cannot be embossed
    on this device.
    """
    passes, errors, context = build_plan(snapshot, emb)
    if passes is None:
        return {"ok": False, "errors": errors, "context": context}

    out_passes = []
    for plan in passes:
        content = _encode_pass(plan, emb)
        out_passes.append(
            {
                "pass": plan.name,
                "encoding": emb.input_encoding,
                "page_break": emb.page_break,
                "line_ending": emb.line_ending,
                "paper_order": _paper_order(plan),
                "reload": plan.reload,
                "cell_mapping": _cell_mapping(plan),
                "content": content,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    return {"ok": True, "context": context, "passes": out_passes}


# ---------------------------------------------------------------------------
# Readback: re-parse a byte stream and verify it against the version
# ---------------------------------------------------------------------------


def _tokenize(content: bytes, emb: EmbosserConfig) -> tuple[list[tuple], list[dict]]:
    """Split a byte stream into ("line", [masks]) and ("ff", None) events."""
    events: list[tuple] = []
    errors: list[dict] = []
    if emb.input_encoding == "unicode_braille":
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            return [], [
                {"type": "undecodable_byte", "detail": f"invalid UTF-8: {exc}", "offset": exc.start}
            ]
        cur: list[int] = []
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\r":
                if i + 1 < len(text) and text[i + 1] == "\n":
                    i += 1
                events.append(("line", cur))
                cur = []
            elif ch == "\n":
                events.append(("line", cur))
                cur = []
            elif ch == "\f":
                if cur:
                    events.append(("line", cur))
                    cur = []
                events.append(("ff", None))
            elif is_braille(ch):
                cur.append(mask_of(ch))
            else:
                errors.append(
                    {
                        "type": "undecodable_byte",
                        "detail": f"character {ch!r} (U+{ord(ch):04X}) at character offset {i} "
                        "is not a braille pattern",
                        "offset": i,
                        "char": ch,
                    }
                )
            i += 1
        if cur:
            events.append(("line", cur))
        return events, errors

    cur = []
    i = 0
    n = len(content)
    while i < n:
        b = content[i]
        if b == 0x0D:  # CR or CRLF
            if i + 1 < n and content[i + 1] == 0x0A:
                i += 1
            events.append(("line", cur))
            cur = []
        elif b == 0x0A:
            events.append(("line", cur))
            cur = []
        elif b == FORM_FEED_BYTE:
            if cur:
                events.append(("line", cur))
                cur = []
            events.append(("ff", None))
        else:
            mask = BRF_CHAR_TO_MASK.get(chr(b))
            if mask is None:
                errors.append(
                    {
                        "type": "undecodable_byte",
                        "detail": f"byte 0x{b:02X} at offset {i} is not in the BRF table",
                        "offset": i,
                        "byte": f"0x{b:02X}",
                    }
                )
            else:
                cur.append(mask)
        i += 1
    if cur:
        events.append(("line", cur))
    return events, errors


def _verify_events(
    events: list[tuple], cell_mapping: list[dict], emb: EmbosserConfig, pass_name: str
) -> dict:
    """Walk the planned pages and compare them with the parsed events."""
    mismatches: list[dict] = []
    pos = 0
    pages_checked = 0
    lines_checked = 0
    cells_checked = 0
    n_pages = len(cell_mapping)

    def mismatch(itype: str, detail: str, **extra: Any) -> None:
        mismatches.append({"type": itype, "detail": detail, "source": pass_name, **extra})

    for entry in cell_mapping:
        fi = entry["file_page"]
        loc = {
            "file_page": fi,
            "version_page": entry["version_page"],
            "sheet": entry["sheet"],
            "side": entry["side"],
        }
        for line in entry["lines"]:
            row = line["row"]
            if pos >= len(events) or events[pos][0] != "line":
                mismatch(
                    "missing_line",
                    f"expected line {row} of file page {fi} is missing",
                    **loc,
                    row=row,
                )
                return {
                    "mismatches": mismatches,
                    "pages_checked": pages_checked,
                    "lines_checked": lines_checked,
                    "cells_checked": cells_checked,
                    "completed": False,
                }
            actual = events[pos][1]
            pos += 1
            expected = [mask_of(c) for c in line["text"]]
            lines_checked += 1
            for c in range(max(len(expected), len(actual))):
                exp = expected[c] if c < len(expected) else None
                act = actual[c] if c < len(actual) else None
                cells_checked += 1
                if exp != act:
                    mismatch(
                        "cell_mismatch",
                        f"cell at file page {fi}, row {row}, col {c + 1} differs from the version",
                        **loc,
                        row=row,
                        col=c + 1,
                        expected_mask=exp,
                        actual_mask=act,
                    )
        if emb.page_break == "form_feed":
            if pos < len(events) and events[pos][0] == "ff":
                pos += 1
            else:
                # every page, including the last, must be terminated by a
                # form feed; a missing one means the stream is damaged
                mismatch(
                    "missing_page_break",
                    f"no form feed after file page {fi}",
                    **loc,
                )
        elif emb.page_break == "line_advance":
            for _ in range(emb.lines_per_page - len(entry["lines"])):
                if pos < len(events) and events[pos][0] == "line" and all(
                    m == 0 for m in events[pos][1]
                ):
                    pos += 1
                    lines_checked += 1
                else:
                    mismatch(
                        "padding_mismatch",
                        f"page-advance padding of file page {fi} is missing or not blank",
                        **loc,
                    )
                    break
        pages_checked += 1

    if pos < len(events):
        mismatch(
            "trailing_data",
            f"{len(events) - pos} unexpected event(s) after the last planned page",
            file_page=n_pages,
        )
    return {
        "mismatches": mismatches,
        "pages_checked": pages_checked,
        "lines_checked": lines_checked,
        "cells_checked": cells_checked,
        "completed": True,
    }


def readback_batch(snapshot: dict, emb: EmbosserConfig, files: list[dict]) -> dict:
    """Re-parse stored byte streams and verify them against the version.

    ``files`` are dicts with ``pass_name`` / ``filename`` / ``sha256`` /
    ``content``.  The plan is rebuilt from the version snapshot and the
    batch's embosser config snapshot, so page order, page breaks and the
    cell mapping are all checked against the original version.
    """
    planned = compile_version(snapshot, emb)
    if not planned["ok"]:
        return {
            "ok": False,
            "detail": "the version no longer compiles with the batch's embosser config",
            "errors": planned["errors"],
            "files": [],
            "mismatches": [],
        }
    by_pass = {p["pass"]: p for p in planned["passes"]}
    out_files: list[dict] = []
    all_mismatches: list[dict] = []
    for f in files:
        plan = by_pass.get(f["pass_name"])
        entry: dict = {
            "pass": f["pass_name"],
            "filename": f["filename"],
            "ok": True,
            "pages_checked": 0,
            "lines_checked": 0,
            "cells_checked": 0,
            "mismatches": [],
        }
        if plan is None:
            entry["ok"] = False
            entry["mismatches"].append(
                {
                    "type": "unexpected_file",
                    "detail": f"pass '{f['pass_name']}' is not part of the planned passes",
                    "source": f["pass_name"],
                }
            )
        else:
            mismatches: list[dict] = []
            actual_sha = hashlib.sha256(f["content"]).hexdigest()
            if actual_sha != f["sha256"]:
                mismatches.append(
                    {
                        "type": "checksum_mismatch",
                        "detail": "stored bytes do not match the recorded sha256",
                        "source": f["pass_name"],
                        "expected_sha256": f["sha256"],
                        "actual_sha256": actual_sha,
                    }
                )
            events, errors = _tokenize(f["content"], emb)
            for e in errors:
                e["source"] = f["pass_name"]
            mismatches.extend(errors)
            result = _verify_events(events, plan["cell_mapping"], emb, f["pass_name"])
            mismatches.extend(result["mismatches"])
            entry.update(
                {
                    "ok": not mismatches,
                    "pages_checked": result["pages_checked"],
                    "lines_checked": result["lines_checked"],
                    "cells_checked": result["cells_checked"],
                    "mismatches": mismatches,
                }
            )
        all_mismatches.extend(entry["mismatches"])
        out_files.append(entry)

    # every planned pass must have a file; a missing one fails the readback
    seen = {f["pass_name"] for f in files}
    for p in planned["passes"]:
        if p["pass"] in seen:
            continue
        entry = {
            "pass": p["pass"],
            "filename": None,
            "ok": False,
            "pages_checked": 0,
            "lines_checked": 0,
            "cells_checked": 0,
            "mismatches": [
                {
                    "type": "missing_file",
                    "detail": f"pass '{p['pass']}' is planned but has no file in this batch",
                    "source": p["pass"],
                }
            ],
        }
        all_mismatches.extend(entry["mismatches"])
        out_files.append(entry)

    return {
        "ok": not all_mismatches,
        "files": out_files,
        "mismatches": all_mismatches,
    }
