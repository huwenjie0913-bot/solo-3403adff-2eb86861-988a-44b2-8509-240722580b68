"""Pydantic schemas for the API (requests, responses, configuration)."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class PaperConfig(BaseModel):
    """Physical paper size in millimetres."""

    width_mm: float = Field(297.0, gt=0)
    height_mm: float = Field(210.0, gt=0)


class MarginConfig(BaseModel):
    """Outer margins plus the extra binding (gutter) margin, in millimetres."""

    top_mm: float = Field(12.0, ge=0)
    bottom_mm: float = Field(12.0, ge=0)
    left_mm: float = Field(12.0, ge=0)
    right_mm: float = Field(12.0, ge=0)
    binding_mm: float = Field(6.0, ge=0)


class DotConfig(BaseModel):
    """Embossed dot geometry."""

    diameter_mm: float = Field(1.5, gt=0)
    pitch_mm: float = Field(2.5, gt=0, description="centre-to-centre dot spacing inside a cell")


class CellConfig(BaseModel):
    """Braille cell (方) geometry."""

    pitch_mm: float = Field(6.0, gt=0, description="horizontal pitch of adjacent cells")
    max_cells_per_line: Optional[int] = Field(
        None, gt=0, description="hard cap on cells per line; defaults to what the printable width allows"
    )


class LineConfig(BaseModel):
    """Line (row of cells) geometry."""

    pitch_mm: float = Field(10.0, gt=0, description="vertical pitch of adjacent lines")
    max_lines_per_page: Optional[int] = Field(
        None, gt=0, description="hard cap on lines per page; defaults to what the printable height allows"
    )


class InterpointConfig(BaseModel):
    """Duplex (interpoint) embossing parameters.

    The back side is shifted by ``offset_*`` relative to the front grid so
    that raised dots from both sides do not coincide.  Any pair of front/back
    dots closer than ``min_separation_mm`` is reported as a collision.
    """

    enabled: bool = True
    offset_x_mm: float = 2.5
    offset_y_mm: float = 0.0
    min_separation_mm: float = Field(2.0, ge=0)


BindingEdge = Literal["left", "right", "top", "bottom"]
FlipMode = Literal["page_flip", "top_flip"]


class JobConfig(BaseModel):
    """Full layout/embossing configuration for a job."""

    paper: PaperConfig = PaperConfig()
    margins: MarginConfig = MarginConfig()
    binding_edge: BindingEdge = "left"
    flip_mode: FlipMode = Field(
        "page_flip",
        description="page_flip: sheet turns around the vertical axis (book); "
        "top_flip: sheet turns around the horizontal axis (tumble)",
    )
    dot: DotConfig = DotConfig()
    cell: CellConfig = CellConfig()
    line: LineConfig = LineConfig()
    interpoint: InterpointConfig = InterpointConfig()
    allow_8dot: bool = Field(False, description="allow dots 7/8 (8-dot braille)")
    back_side_premirrored: bool = Field(
        False,
        description="set when explicitly supplied back-side lines are already "
        "physically mirrored (as some embosser software outputs them)",
    )

    @model_validator(mode="after")
    def _check_flip_binding_consistency(self) -> "JobConfig":
        # Not an error, but the geometry module relies on these being sane.
        return self


# ---------------------------------------------------------------------------
# Document structure
# ---------------------------------------------------------------------------

BlockType = Literal["heading", "paragraph", "table"]


class Block(BaseModel):
    """A structural block over the flattened source lines.

    ``line_start``/``line_end`` are 0-based indices into the flattened
    content lines (pages split on form feed, then lines on newline; line
    numbering continues across pages).  ``line_end`` is exclusive.
    """

    type: BlockType
    line_start: int = Field(..., ge=0)
    line_end: int = Field(..., gt=0)
    level: Optional[int] = Field(None, ge=1, le=6, description="heading level")

    @model_validator(mode="after")
    def _check_range(self) -> "Block":
        if self.line_end <= self.line_start:
            raise ValueError("line_end must be greater than line_start")
        return self


class SheetSpec(BaseModel):
    """Explicitly supplied front/back lines of one physical sheet.

    Lines are given in *reading order* (Unicode braille or BRF matching the
    job's source format).  Used to verify that an operator prepared the
    back side with the correct mirror transform.
    """

    front: list[str] = []
    back: list[str] = []


class Structure(BaseModel):
    """Logical structure of the source content."""

    blocks: list[Block] = []
    sheets: Optional[list[SheetSpec]] = Field(
        None, description="explicit per-sheet front/back lines for mirror verification"
    )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

SourceFormat = Literal["unicode", "brf"]


class JobCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    source_format: SourceFormat = "unicode"
    content: str = Field(..., description="braille text; '\\n' separates lines, '\\f' is an explicit page break")
    structure: Structure = Structure()
    config: JobConfig = JobConfig()


class JobSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    source_format: str
    created_at: str
    line_count: int
    page_count: int


class JobDetail(JobSummary):
    structure: dict
    config: dict


# ---------------------------------------------------------------------------
# Layout (reflow)
# ---------------------------------------------------------------------------


class LayoutParams(BaseModel):
    """Constraints for the reflow/pagination engine."""

    cells_per_line: Optional[int] = Field(
        None, gt=0, description="wrap width; defaults to the grid capacity"
    )
    lines_per_page: Optional[int] = Field(
        None, gt=0, description="page capacity; defaults to the grid capacity"
    )
    break_at_blank_only: bool = Field(
        True, description="only break lines at blank cells; if a word exceeds the "
        "line width it is hard-broken and a violation is recorded"
    )
    blank_lines_between_paragraphs: int = Field(1, ge=0, le=5)
    blank_line_before_heading: int = Field(1, ge=0, le=5)
    blank_line_after_heading: int = Field(1, ge=0, le=5)
    blank_lines_around_table: int = Field(1, ge=0, le=5)
    heading_keep_with_next_lines: int = Field(
        2, ge=0, description="a heading must share its page with at least this many lines of the next block"
    )
    paragraph_min_lines_at_page_end: int = Field(
        2, ge=0, description="min paragraph lines left at the bottom of a page (widow control)"
    )
    paragraph_min_lines_at_page_start: int = Field(
        2, ge=0, description="min paragraph lines carried to the top of the next page (orphan control)"
    )
    paragraph_may_span_pages: bool = True
    table_keep_together: bool = Field(True, description="never split a table across pages")
    respect_explicit_page_breaks: bool = Field(
        True, description="locked page boundaries (form feeds) are never moved"
    )
    max_pages: Optional[int] = Field(None, gt=0, description="page budget; exceeding it is a violation")
    max_solutions: int = Field(3, ge=1, le=10)


class LayoutRequest(BaseModel):
    params: LayoutParams = LayoutParams()


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


class VersionCreate(BaseModel):
    """Confirm a layout solution (or the original pagination) as a version."""

    layout_run_id: Optional[int] = Field(None, description="run to confirm; omit with source='original'")
    solution_index: Optional[int] = Field(None, ge=0, description="index into the run's ranked solutions")
    source: Literal["layout", "original"] = "layout"
    note: str = ""


class VersionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    layout_run_id: Optional[int]
    solution_index: Optional[int]
    note: str
    created_at: str
