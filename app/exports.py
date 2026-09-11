"""Exporters: PEF (Portable Embosser Format), SVG proofs, JSON trace.

Everything is computed locally from a version snapshot — no external
services are involved.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from typing import Any, Optional
from xml.sax.saxutils import escape

from .geometry import compute_grid, dot_position_back, dot_position_front
from .braille import dots_of, is_braille
from .preflight import _find_collisions
from .schemas import JobConfig

PEF_NS = "http://www.daisy.org/ns/2008/pef"
DC_NS = "http://purl.org/dc/elements/1.1/"

ET.register_namespace("", PEF_NS)
ET.register_namespace("dc", DC_NS)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _sheet_pages(snapshot: dict) -> dict[int, dict[str, Optional[dict]]]:
    """Group solution pages into sheets: {sheet_no: {'front': page, 'back': page}}."""
    sheets: dict[int, dict[str, Optional[dict]]] = {}
    for page in snapshot["solution"]["pages"]:
        entry = sheets.setdefault(page["sheet"], {"front": None, "back": None})
        entry[page["side"]] = page
    return sheets


def _page_dots(lines: list[dict], side: str, cfg: JobConfig, grid) -> list[dict]:
    dots: list[dict] = []
    for line in lines:
        r = line["row"] - 1
        for c, ch in enumerate(line["text"]):
            if not is_braille(ch):
                continue
            for dot in dots_of(ch):
                if side == "front":
                    x, y = dot_position_front(cfg, grid, r, c, dot)
                else:
                    x, y = dot_position_back(cfg, grid, r, c, dot)
                dots.append(
                    {"row": r, "col": c, "dot": dot, "x_mm": round(x, 4), "y_mm": round(y, 4)}
                )
    return dots


# ---------------------------------------------------------------------------
# PEF
# ---------------------------------------------------------------------------


def version_to_pef(snapshot: dict) -> str:
    """Render the version as a PEF document.

    Volume 1 holds the front (recto) pages, volume 2 the back (verso)
    pages, both marked duplex so interpoint embossers pair them up.
    """
    cfg = JobConfig(**snapshot["config"])
    grid = compute_grid(cfg)
    pages = snapshot["solution"]["pages"]
    front_pages = [p for p in pages if p["side"] == "front"]
    back_pages = [p for p in pages if p["side"] == "back"]

    pef = ET.Element(f"{{{PEF_NS}}}pef", {"version": "2008-1"})
    head = ET.SubElement(pef, f"{{{PEF_NS}}}head")
    meta = ET.SubElement(head, f"{{{PEF_NS}}}meta")
    meta.set(f"{{{DC_NS}}}format", "application/x-pef+xml")
    meta.set(f"{{{DC_NS}}}title", str(snapshot.get("job_name") or "braille job"))
    meta.set(f"{{{DC_NS}}}date", str(snapshot.get("created_at", ""))[:10])
    body = ET.SubElement(pef, f"{{{PEF_NS}}}body")

    for volume_pages, volume_no in ((front_pages, 1), (back_pages, 2)):
        if not volume_pages:
            continue
        volume = ET.SubElement(
            body,
            f"{{{PEF_NS}}}volume",
            {
                "cols": str(grid.cols),
                "rows": str(grid.rows),
                "duplex": "true" if cfg.interpoint.enabled else "false",
            },
        )
        section = ET.SubElement(volume, f"{{{PEF_NS}}}section")
        for page in volume_pages:
            page_el = ET.SubElement(section, f"{{{PEF_NS}}}page")
            for line in page["lines"]:
                row_el = ET.SubElement(page_el, f"{{{PEF_NS}}}row")
                row_el.text = line["text"]

    ET.indent(pef, space="  ")
    return ET.tostring(pef, encoding="unicode", xml_declaration=True)


# ---------------------------------------------------------------------------
# SVG proofs
# ---------------------------------------------------------------------------

_EMBOSS_FILL = "#111111"
_DEBOSS_STROKE = "#1a5fb4"
_COLLISION = "#cc0000"


def _circle(x: float, y: float, r: float, attrs: str) -> str:
    return f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{r:.3f}" {attrs}/>'


def version_to_svg(snapshot: dict, sheet_no: int, layer: str = "overlay", style: str = "emboss") -> str:
    """SVG proof of one sheet.

    Coordinates are physical millimetres in the front view of the sheet.
    ``layer`` selects front dots, back dots or an overlay of both (front
    filled, back hollow, collisions highlighted).  ``style`` renders dots
    as embossed (filled) or debossed (hollow) marks.
    """
    cfg = JobConfig(**snapshot["config"])
    grid = compute_grid(cfg)
    sheets = _sheet_pages(snapshot)
    if sheet_no not in sheets:
        raise ValueError(f"sheet {sheet_no} does not exist")
    sheet = sheets[sheet_no]

    front_dots: list[dict] = []
    back_dots: list[dict] = []
    if sheet.get("front"):
        front_dots = _page_dots(sheet["front"]["lines"], "front", cfg, grid)
    if sheet.get("back"):
        back_dots = _page_dots(sheet["back"]["lines"], "back", cfg, grid)

    collisions = _find_collisions(front_dots, back_dots, cfg.interpoint.min_separation_mm)
    coll_front = {(c["front"]["row"], c["front"]["col"], c["front"]["dot"]) for c in collisions}
    coll_back = {(c["back"]["row"], c["back"]["col"], c["back"]["dot"]) for c in collisions}

    w = cfg.paper.width_mm
    h = cfg.paper.height_mm
    r = cfg.dot.diameter_mm / 2.0
    pf = grid.printable_front
    pb = grid.printable_back

    def dot_svg(d: dict, colliding: set, filled_style: str, hollow_style: str, filled: bool) -> str:
        key = (d["row"], d["col"], d["dot"])
        if key in colliding:
            return _circle(d["x_mm"], d["y_mm"], r, f'fill="{_COLLISION}"')
        return _circle(d["x_mm"], d["y_mm"], r, filled_style if filled else hollow_style)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}mm" height="{h}mm" '
        f'viewBox="0 0 {w} {h}" font-family="sans-serif" font-size="3">',
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="white" stroke="black" stroke-width="0.3"/>',
        f'<rect x="{pf.left}" y="{pf.top}" width="{pf.right - pf.left}" '
        f'height="{pf.bottom - pf.top}" fill="none" stroke="#888" '
        f'stroke-width="0.2" stroke-dasharray="1.5 1"/>',
    ]
    if cfg.interpoint.enabled:
        parts.append(
            f'<rect x="{pb.left}" y="{pb.top}" width="{pb.right - pb.left}" '
            f'height="{pb.bottom - pb.top}" fill="none" stroke="{_DEBOSS_STROKE}" '
            f'stroke-width="0.2" stroke-dasharray="0.6 0.8"/>'
        )

    # binding edge marker
    edge = cfg.binding_edge
    if edge == "left":
        parts.append(f'<line x1="1" y1="0" x2="1" y2="{h}" stroke="#e66100" stroke-width="1.2"/>')
        parts.append(f'<text x="2.5" y="6" fill="#e66100" transform="rotate(90 2.5 6)">binding</text>')
    elif edge == "right":
        parts.append(f'<line x1="{w - 1}" y1="0" x2="{w - 1}" y2="{h}" stroke="#e66100" stroke-width="1.2"/>')
        parts.append(f'<text x="{w - 4}" y="6" fill="#e66100" transform="rotate(90 {w - 4} 6)">binding</text>')
    elif edge == "top":
        parts.append(f'<line x1="0" y1="1" x2="{w}" y2="1" stroke="#e66100" stroke-width="1.2"/>')
        parts.append('<text x="4" y="4" fill="#e66100">binding</text>')
    else:
        parts.append(f'<line x1="0" y1="{h - 1}" x2="{w}" y2="{h - 1}" stroke="#e66100" stroke-width="1.2"/>')
        parts.append(f'<text x="4" y="{h - 2.5}" fill="#e66100">binding</text>')

    emboss_attrs = f'fill="{_EMBOSS_FILL}"'
    deboss_attrs = f'fill="none" stroke="{_DEBOSS_STROKE}" stroke-width="0.25"'

    show_front = layer in ("front", "overlay")
    show_back = layer in ("back", "overlay")
    if layer == "overlay":
        for d in front_dots:
            parts.append(dot_svg(d, coll_front, emboss_attrs, deboss_attrs, filled=True))
        for d in back_dots:
            parts.append(
                _circle(
                    d["x_mm"],
                    d["y_mm"],
                    r,
                    f'fill="{_COLLISION}"'
                    if (d["row"], d["col"], d["dot"]) in coll_back
                    else deboss_attrs,
                )
            )
    else:
        filled = style == "emboss"
        dots = front_dots if show_front else back_dots
        coll = coll_front if show_front else coll_back
        for d in dots:
            parts.append(dot_svg(d, coll, emboss_attrs, deboss_attrs, filled))

    label = (
        f"sheet {sheet_no} | layer {escape(layer)} | style {escape(style)} | "
        f"flip {cfg.flip_mode} | binding {edge} | collisions {len(collisions)}"
    )
    parts.append(f'<text x="4" y="{h - 2.5}" fill="#333" font-size="3.5">{escape(label)}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# JSON trace
# ---------------------------------------------------------------------------


def version_to_trace(snapshot: dict) -> dict[str, Any]:
    """Full trace record: configuration, mapping, coordinates, collisions."""
    cfg = JobConfig(**snapshot["config"])
    grid = compute_grid(cfg)
    sheets_out: list[dict] = []
    for sheet_no in sorted(_sheet_pages(snapshot)):
        sheet = _sheet_pages(snapshot)[sheet_no]
        front_dots: list[dict] = []
        back_dots: list[dict] = []
        front_page = sheet.get("front")
        back_page = sheet.get("back")
        if front_page:
            front_dots = _page_dots(front_page["lines"], "front", cfg, grid)
        if back_page:
            back_dots = _page_dots(back_page["lines"], "back", cfg, grid)
        collisions = _find_collisions(front_dots, back_dots, cfg.interpoint.min_separation_mm)
        sheets_out.append(
            {
                "sheet": sheet_no,
                "binding": {
                    "edge": cfg.binding_edge,
                    "flip_mode": cfg.flip_mode,
                    "interpoint_offset_mm": [
                        cfg.interpoint.offset_x_mm,
                        cfg.interpoint.offset_y_mm,
                    ],
                },
                "front": {"page": front_page["page"], "dots": front_dots} if front_page else None,
                "back": {"page": back_page["page"], "dots": back_dots} if back_page else None,
                "collisions": collisions,
            }
        )

    return {
        "version_id": snapshot.get("version_id"),
        "job_id": snapshot["job_id"],
        "job_name": snapshot.get("job_name"),
        "created_at": snapshot.get("created_at"),
        "note": snapshot.get("note", ""),
        "content_sha256": snapshot.get("content_sha256"),
        "config": snapshot["config"],
        "layout_params": snapshot.get("layout_params"),
        "source": snapshot.get("source"),
        "grid": grid.as_dict(),
        "solution": {
            "profile": snapshot["solution"]["profile"],
            "rank": snapshot["solution"].get("rank"),
            "stats": snapshot["solution"]["stats"],
            "mapping": snapshot["solution"]["mapping"],
            "violations": snapshot["solution"]["violations"],
            "unsatisfied_rules": snapshot["solution"]["unsatisfied_rules"],
        },
        "sheets": sheets_out,
    }


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
