"""Content handling: pagination of source text and structural blocks."""

from __future__ import annotations

from dataclasses import dataclass, field

from .braille import is_blank_line
from .schemas import Block, BlockType, Structure


@dataclass
class SourceLine:
    """One line of source content with its original position."""

    no: int  # 0-based global line index
    page: int  # 1-based original page (split on form feed)
    row: int  # 1-based row within the original page
    text: str


@dataclass
class ParsedContent:
    lines: list[SourceLine]
    page_count: int  # number of explicit pages (form feeds + 1)
    # global line index after which an explicit page break sits (the last
    # line of each explicit page except the final one)
    explicit_break_after: set[int] = field(default_factory=set)


def parse_content(content: str) -> ParsedContent:
    """Split content into lines, honouring explicit page breaks (form feed).

    A single trailing newline at the very end of the content is ignored so
    that files ending with a newline do not gain a phantom blank line.
    """
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    raw_pages = text.split("\f")
    lines: list[SourceLine] = []
    explicit_break_after: set[int] = set()
    for page_idx, raw in enumerate(raw_pages):
        page_lines = raw.split("\n")
        # drop one trailing empty element produced by a trailing newline
        if page_lines and page_lines[-1] == "" and page_idx == len(raw_pages) - 1:
            page_lines = page_lines[:-1]
        for row, text in enumerate(page_lines, start=1):
            lines.append(SourceLine(no=len(lines), page=page_idx + 1, row=row, text=text))
        if page_idx < len(raw_pages) - 1 and lines:
            explicit_break_after.add(len(lines) - 1)
    return ParsedContent(lines=lines, page_count=len(raw_pages), explicit_break_after=explicit_break_after)


def build_blocks(parsed: ParsedContent, structure: Structure) -> list[Block]:
    """Resolve the effective block list.

    Blocks come from the supplied structure; gaps between them (and the
    whole content when no structure is given) are auto-structured into
    paragraphs separated by blank lines.
    """
    total = len(parsed.lines)
    explicit: list[Block] = sorted(structure.blocks, key=lambda b: b.line_start)
    for b in explicit:
        if b.line_end > total:
            raise ValueError(
                f"block {b.type} [{b.line_start}, {b.line_end}) exceeds content length {total}"
            )
    for prev, nxt in zip(explicit, explicit[1:]):
        if nxt.line_start < prev.line_end:
            raise ValueError(
                f"blocks overlap: [{prev.line_start}, {prev.line_end}) and [{nxt.line_start}, {nxt.line_end})"
            )

    blocks: list[Block] = []
    cursor = 0

    def auto_block(start: int, end: int) -> None:
        """Split an unstructured range into paragraphs on blank lines."""
        run_start: int | None = None
        for i in range(start, end):
            if is_blank_line(parsed.lines[i].text):
                if run_start is not None:
                    blocks.append(Block(type="paragraph", line_start=run_start, line_end=i))
                    run_start = None
            elif run_start is None:
                run_start = i
        if run_start is not None:
            blocks.append(Block(type="paragraph", line_start=run_start, line_end=end))

    for b in explicit:
        if b.line_start > cursor:
            auto_block(cursor, b.line_start)
        blocks.append(b)
        cursor = b.line_end
    if cursor < total:
        auto_block(cursor, total)
    return blocks


def block_type_at(blocks: list[Block], line_no: int) -> BlockType | None:
    for b in blocks:
        if b.line_start <= line_no < b.line_end:
            return b.type
    return None
