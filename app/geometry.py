"""Paper geometry: grid computation and dot coordinates.

Coordinate system: origin at the top-left corner of the paper, x to the
right, y downwards, all values in millimetres.

The *front* side is laid out in logical coordinates.  The *back* side of an
interpoint sheet is computed by applying the flip transform of the
configured flip mode (the physical rotation of the sheet) plus the
interpoint offset that staggers the back dots between the front dots:

* ``page_flip`` — the sheet turns around the vertical axis (like a book
  page): ``x' = W - x``, ``y' = y``.
* ``top_flip`` — the sheet turns around the horizontal axis (tumble):
  ``x' = x``, ``y' = H - y``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .braille import CELL_DOT_ROWS_6, CELL_DOT_ROWS_8, DOT_OFFSETS
from .schemas import JobConfig

TOL = 1e-6


@dataclass(frozen=True)
class Rect:
    left: float
    top: float
    right: float
    bottom: float

    def contains(self, x: float, y: float, tol: float = TOL) -> bool:
        return (
            self.left - tol <= x <= self.right + tol
            and self.top - tol <= y <= self.bottom + tol
        )

    def as_dict(self) -> dict:
        return {
            "left_mm": round(self.left, 4),
            "top_mm": round(self.top, 4),
            "right_mm": round(self.right, 4),
            "bottom_mm": round(self.bottom, 4),
        }


@dataclass(frozen=True)
class Grid:
    """Derived grid parameters for a job configuration."""

    cols: int  # cells per line
    rows: int  # lines per page
    cell_dot_rows: int  # 3 or 4 (8-dot braille)
    printable_front: Rect
    printable_back: Rect  # in physical (front-view) coordinates

    def as_dict(self) -> dict:
        return {
            "cols": self.cols,
            "rows": self.rows,
            "cell_dot_rows": self.cell_dot_rows,
            "printable_front": self.printable_front.as_dict(),
            "printable_back": self.printable_back.as_dict(),
        }


def flip_point(cfg: JobConfig, x: float, y: float) -> tuple[float, float]:
    """Physical coordinates of a logical front-side point after sheet flip."""
    if cfg.flip_mode == "page_flip":
        return cfg.paper.width_mm - x, y
    return x, cfg.paper.height_mm - y


def printable_front(cfg: JobConfig) -> Rect:
    m = cfg.margins
    left = m.left_mm + (m.binding_mm if cfg.binding_edge == "left" else 0.0)
    right = cfg.paper.width_mm - m.right_mm - (m.binding_mm if cfg.binding_edge == "right" else 0.0)
    top = m.top_mm + (m.binding_mm if cfg.binding_edge == "top" else 0.0)
    bottom = cfg.paper.height_mm - m.bottom_mm - (m.binding_mm if cfg.binding_edge == "bottom" else 0.0)
    return Rect(left, top, right, bottom)


def printable_back(cfg: JobConfig) -> Rect:
    """Printable area of the back side in physical (front-view) coordinates."""
    f = printable_front(cfg)
    if cfg.flip_mode == "page_flip":
        w = cfg.paper.width_mm
        return Rect(w - f.right, f.top, w - f.left, f.bottom)
    h = cfg.paper.height_mm
    return Rect(f.left, h - f.bottom, f.right, h - f.top)


def compute_grid(cfg: JobConfig) -> Grid:
    """Compute cells-per-line and lines-per-page from the configuration."""
    area = printable_front(cfg)
    if area.right <= area.left or area.bottom <= area.top:
        raise ValueError("printable area is empty: margins exceed the paper size")

    cell_dot_rows = CELL_DOT_ROWS_8 if cfg.allow_8dot else CELL_DOT_ROWS_6
    dot_pitch = cfg.dot.pitch_mm

    # A cell spans one dot pitch horizontally (two dot columns).
    width = area.right - area.left
    cols = int(math.floor((width - dot_pitch) / cfg.cell.pitch_mm + TOL)) + 1
    if cfg.cell.max_cells_per_line is not None:
        cols = min(cols, cfg.cell.max_cells_per_line)
    cols = max(cols, 0)

    # A line of cells spans (cell_dot_rows - 1) dot pitches vertically.
    height = area.bottom - area.top
    line_span = (cell_dot_rows - 1) * dot_pitch
    rows = int(math.floor((height - line_span) / cfg.line.pitch_mm + TOL)) + 1
    if cfg.line.max_lines_per_page is not None:
        rows = min(rows, cfg.line.max_lines_per_page)
    rows = max(rows, 0)

    return Grid(
        cols=cols,
        rows=rows,
        cell_dot_rows=cell_dot_rows,
        printable_front=area,
        printable_back=printable_back(cfg),
    )


def cell_origin_front(cfg: JobConfig, grid: Grid, row: int, col: int) -> tuple[float, float]:
    """Logical (front-side) origin of the cell at (row, col)."""
    x = grid.printable_front.left + col * cfg.cell.pitch_mm
    y = grid.printable_front.top + row * cfg.line.pitch_mm
    return x, y


def dot_position_front(cfg: JobConfig, grid: Grid, row: int, col: int, dot: int) -> tuple[float, float]:
    """Physical front-side coordinates of one raised dot."""
    cx, cy = cell_origin_front(cfg, grid, row, col)
    dx, dy = DOT_OFFSETS[dot]
    return cx + dx * cfg.dot.pitch_mm, cy + dy * cfg.dot.pitch_mm


def dot_position_back(cfg: JobConfig, grid: Grid, row: int, col: int, dot: int) -> tuple[float, float]:
    """Physical coordinates of a back-side dot, seen from the front.

    The back cell uses the same logical grid; its position is mirrored by
    the flip transform and shifted by the interpoint offset.
    """
    x, y = dot_position_front(cfg, grid, row, col, dot)
    x, y = flip_point(cfg, x, y)
    return x + cfg.interpoint.offset_x_mm, y + cfg.interpoint.offset_y_mm
