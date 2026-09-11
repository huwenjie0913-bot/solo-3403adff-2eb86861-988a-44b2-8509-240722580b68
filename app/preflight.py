"""Preflight engine: validates a duplex braille layout against the geometry.

Checks performed:

* ``illegal_character``     — characters outside Unicode Braille / BRF set
* ``eight_dot_not_allowed`` — dots 7/8 used while 8-dot braille is disabled
* ``line_too_long``         — more cells on a line than the grid allows
* ``page_overflow``         — more lines on a page than the grid allows
* ``out_of_bounds``         — a raised dot falls outside the printable area
* ``dot_collision``         — front/back dots closer than the minimum spacing
* ``mirror_mismatch``       — explicitly supplied back side does not match the
                              expected mirrored reading order
* ``page_sequence_error``   — explicitly supplied front side out of sequence
* ``sheet_count_mismatch``  — number of supplied sheets differs from content

The engine never rewrites the braille content: original cells and explicit
page breaks (form feeds) are preserved exactly as supplied.
"""

from __future__ import annotations

from typing import Any

from .braille import BLANK_CELL, dots_of, is_braille, uses_8dot
from .content import parse_content
from .geometry import Grid, compute_grid, dot_position_back, dot_position_front
from .schemas import JobConfig, Structure

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


def _issue(itype: str, severity: str, detail: str, **extra: Any) -> dict:
    issue = {"type": itype, "severity": severity, "detail": detail}
    issue.update(extra)
    return issue


def _page_dots(
    lines: list[str], side: str, cfg: JobConfig, grid: Grid
) -> tuple[list[dict], list[list[int]]]:
    """Raised dots (physical coordinates) and cell matrix of one page side."""
    dots: list[dict] = []
    matrix = [[0] * grid.cols for _ in range(grid.rows)]
    for r, line in enumerate(lines[: grid.rows]):
        for c, ch in enumerate(line[: grid.cols]):
            if not is_braille(ch):
                continue
            matrix[r][c] = ord(ch) - 0x2800
            for dot in dots_of(ch):
                if side == "front":
                    x, y = dot_position_front(cfg, grid, r, c, dot)
                else:
                    x, y = dot_position_back(cfg, grid, r, c, dot)
                dots.append({"row": r, "col": c, "dot": dot, "x_mm": round(x, 4), "y_mm": round(y, 4)})
    return dots, matrix


def _find_collisions(front_dots: list[dict], back_dots: list[dict], min_sep: float) -> list[dict]:
    """All front/back dot pairs closer than ``min_sep`` (spatial hash)."""
    if min_sep <= 0 or not front_dots or not back_dots:
        return []
    cell = min_sep
    index: dict[tuple[int, int], list[dict]] = {}
    for d in front_dots:
        key = (int(d["x_mm"] // cell), int(d["y_mm"] // cell))
        index.setdefault(key, []).append(d)
    collisions: list[dict] = []
    min_sep_sq = min_sep * min_sep
    for b in back_dots:
        bx, by = b["x_mm"], b["y_mm"]
        kx, ky = int(bx // cell), int(by // cell)
        for ix in range(kx - 1, kx + 2):
            for iy in range(ky - 1, ky + 2):
                for f in index.get((ix, iy), ()):
                    dx = f["x_mm"] - bx
                    dy = f["y_mm"] - by
                    dist_sq = dx * dx + dy * dy
                    if dist_sq < min_sep_sq - 1e-12:
                        collisions.append(
                            {
                                "front": f,
                                "back": b,
                                "distance_mm": round(dist_sq**0.5, 4),
                            }
                        )
    return collisions


def _normalize_for_compare(lines: list[str]) -> list[str]:
    """Trim trailing blank cells/lines so layout padding does not compare."""
    out = [ln.rstrip(BLANK_CELL + " ") for ln in lines]
    while out and out[-1] == "":
        out.pop()
    return out


def _unmirror(lines: list[str], cfg: JobConfig) -> list[str]:
    """Undo the physical mirror of a premirrored back side."""
    if cfg.flip_mode == "page_flip":
        return [ln[::-1] for ln in lines]
    return list(reversed(lines))


def run_preflight(content: str, structure: Structure, cfg: JobConfig) -> dict:
    """Run all preflight checks and return the full report."""
    parsed = parse_content(content)
    grid = compute_grid(cfg)
    issues: list[dict] = []

    # ---- per-line checks -------------------------------------------------
    pages: list[list[str]] = []
    for line in parsed.lines:
        while len(pages) < line.page:
            pages.append([])
        pages[line.page - 1].append(line.text)
        for col, ch in enumerate(line.text, start=1):
            if not is_braille(ch):
                issues.append(
                    _issue(
                        "illegal_character",
                        SEVERITY_ERROR,
                        f"character {ch!r} (U+{ord(ch):04X}) is not a braille pattern",
                        page=line.page,
                        row=line.row,
                        col=col,
                        char=ch,
                        codepoint=f"U+{ord(ch):04X}",
                    )
                )
            elif uses_8dot(ch) and not cfg.allow_8dot:
                issues.append(
                    _issue(
                        "eight_dot_not_allowed",
                        SEVERITY_ERROR,
                        "cell uses dot 7/8 but 8-dot braille is disabled",
                        page=line.page,
                        row=line.row,
                        col=col,
                        char=ch,
                        codepoint=f"U+{ord(ch):04X}",
                    )
                )
        if grid.cols and len(line.text) > grid.cols:
            issues.append(
                _issue(
                    "line_too_long",
                    SEVERITY_ERROR,
                    f"line has {len(line.text)} cells, grid allows {grid.cols}",
                    page=line.page,
                    row=line.row,
                    cells=len(line.text),
                    max_cells=grid.cols,
                )
            )

    for page_no, page_lines in enumerate(pages, start=1):
        if grid.rows and len(page_lines) > grid.rows:
            issues.append(
                _issue(
                    "page_overflow",
                    SEVERITY_ERROR,
                    f"page has {len(page_lines)} lines, grid allows {grid.rows}",
                    page=page_no,
                    lines=len(page_lines),
                    max_lines=grid.rows,
                )
            )

    # ---- geometry: bounds and collisions ---------------------------------
    sheets: list[dict] = []
    for page_no, page_lines in enumerate(pages, start=1):
        side = "front" if page_no % 2 == 1 else "back"
        sheet_no = (page_no + 1) // 2
        while len(sheets) < sheet_no:
            sheets.append({"sheet": len(sheets) + 1, "front": None, "back": None, "collisions": []})
        dots, matrix = _page_dots(page_lines, side, cfg, grid)
        area = grid.printable_front if side == "front" else grid.printable_back
        for d in dots:
            if not area.contains(d["x_mm"], d["y_mm"]):
                issues.append(
                    _issue(
                        "out_of_bounds",
                        SEVERITY_ERROR,
                        f"dot at ({d['x_mm']}, {d['y_mm']}) mm is outside the printable area",
                        page=page_no,
                        side=side,
                        row=d["row"] + 1,
                        col=d["col"] + 1,
                        dot=d["dot"],
                        x_mm=d["x_mm"],
                        y_mm=d["y_mm"],
                        printable=area.as_dict(),
                    )
                )
        sheets[sheet_no - 1][side] = {
            "page": page_no,
            "matrix": matrix,
            "dot_count": len(dots),
            "_dots": dots,
        }

    if cfg.interpoint.enabled:
        for sheet in sheets:
            front = sheet.get("front")
            back = sheet.get("back")
            if not front or not back:
                continue
            collisions = _find_collisions(
                front["_dots"], back["_dots"], cfg.interpoint.min_separation_mm
            )
            for col in collisions:
                sheet["collisions"].append(col)
                issues.append(
                    _issue(
                        "dot_collision",
                        SEVERITY_ERROR,
                        f"front dot (row {col['front']['row'] + 1}, col {col['front']['col'] + 1}, "
                        f"dot {col['front']['dot']}) and back dot (row {col['back']['row'] + 1}, "
                        f"col {col['back']['col'] + 1}, dot {col['back']['dot']}) are "
                        f"{col['distance_mm']} mm apart, minimum is "
                        f"{cfg.interpoint.min_separation_mm} mm",
                        page=front["page"],
                        side="front",
                        sheet=sheet["sheet"],
                        front=col["front"],
                        back=col["back"],
                        distance_mm=col["distance_mm"],
                    )
                )

    # ---- mirror / sequence verification ----------------------------------
    if structure.sheets is not None:
        issues.extend(_verify_sheets(structure, pages, cfg))

    # ---- assemble report --------------------------------------------------
    for sheet in sheets:
        sheet["binding"] = {
            "edge": cfg.binding_edge,
            "flip_mode": cfg.flip_mode,
            "interpoint_offset_mm": [cfg.interpoint.offset_x_mm, cfg.interpoint.offset_y_mm],
        }
        for side in ("front", "back"):
            if sheet.get(side):
                sheet[side].pop("_dots", None)

    errors = sum(1 for i in issues if i["severity"] == SEVERITY_ERROR)
    warnings = sum(1 for i in issues if i["severity"] == SEVERITY_WARNING)
    by_type: dict[str, int] = {}
    for i in issues:
        by_type[i["type"]] = by_type.get(i["type"], 0) + 1

    return {
        "ok": errors == 0,
        "summary": {"errors": errors, "warnings": warnings, "by_type": by_type},
        "issues": issues,
        "geometry": grid.as_dict(),
        "page_count": len(pages),
        "sheet_count": len(sheets),
        "sheets": sheets,
    }


def _verify_sheets(structure: Structure, pages: list[list[str]], cfg: JobConfig) -> list[dict]:
    """Compare explicitly supplied sheet sides against the expected sequence."""
    issues: list[dict] = []
    expected_sheets = (len(pages) + 1) // 2
    if len(structure.sheets) != expected_sheets:
        issues.append(
            _issue(
                "sheet_count_mismatch",
                SEVERITY_WARNING,
                f"{len(structure.sheets)} sheets supplied, content produces {expected_sheets}",
                supplied=len(structure.sheets),
                expected=expected_sheets,
            )
        )
    for idx, spec in enumerate(structure.sheets):
        sheet_no = idx + 1
        expected_front = pages[2 * idx] if 2 * idx < len(pages) else []
        expected_back = pages[2 * idx + 1] if 2 * idx + 1 < len(pages) else []

        got_front = _normalize_for_compare(spec.front)
        want_front = _normalize_for_compare(expected_front)
        if got_front != want_front:
            row = _first_diff(got_front, want_front)
            issues.append(
                _issue(
                    "page_sequence_error",
                    SEVERITY_ERROR,
                    f"sheet {sheet_no} front does not match expected page {2 * idx + 1}",
                    sheet=sheet_no,
                    side="front",
                    row=row,
                    expected=want_front[row - 1] if row and row <= len(want_front) else None,
                    actual=got_front[row - 1] if row and row <= len(got_front) else None,
                )
            )

        got_back = _normalize_for_compare(spec.back)
        if cfg.back_side_premirrored:
            got_back = _normalize_for_compare(_unmirror(got_back, cfg))
        want_back = _normalize_for_compare(expected_back)
        if got_back != want_back:
            row = _first_diff(got_back, want_back)
            issues.append(
                _issue(
                    "mirror_mismatch",
                    SEVERITY_ERROR,
                    f"sheet {sheet_no} back does not match the expected mirrored page "
                    f"{2 * idx + 2} (flip mode {cfg.flip_mode})",
                    sheet=sheet_no,
                    side="back",
                    row=row,
                    expected=want_back[row - 1] if row and row <= len(want_back) else None,
                    actual=got_back[row - 1] if row and row <= len(got_back) else None,
                )
            )
    return issues


def _first_diff(a: list[str], b: list[str]) -> int | None:
    """1-based index of the first differing line, or None."""
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else None
        y = b[i] if i < len(b) else None
        if x != y:
            return i + 1
    return None
