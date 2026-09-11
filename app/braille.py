"""Braille cell primitives: Unicode Braille Patterns and BRF (Braille ASCII).

A braille cell has up to 8 dots arranged in 2 columns x 4 rows::

    1 4
    2 5
    3 6
    7 8

Unicode Braille Patterns (U+2800..U+28FF) encode the dot pattern in the low
byte: bit 0 -> dot 1, bit 1 -> dot 2, ..., bit 7 -> dot 8.

BRF (Braille Ready Format) uses the North American Braille ASCII table: the
ASCII character at table index ``i`` encodes the same cell as U+2800 + ``i``.
"""

from __future__ import annotations

BRAILLE_BASE = 0x2800
BRAILLE_END = 0x28FF
BLANK_CELL = "⠀"  # U+2800, empty braille pattern

#: North American Braille ASCII, index == 6-dot pattern value (dots 1..6).
BRF_ASCII = " A1B'K2L@CIF/MSP\"E3H9O6R^DJG>NTQ,*5<-U8V.%[$+X!&;:4\\0Z7(_?W]#Y)="

BRF_TO_UNICODE = {ch: chr(BRAILLE_BASE + i) for i, ch in enumerate(BRF_ASCII)}
UNICODE_TO_BRF = {chr(BRAILLE_BASE + i): ch for i, ch in enumerate(BRF_ASCII)}

#: Characters that are allowed next to braille cells in source text.
CONTROL_CHARS = {"\n", "\f", "\r"}

#: Dot -> (column, row) offset inside a cell, in units of dot pitch.
DOT_OFFSETS: dict[int, tuple[int, int]] = {
    1: (0, 0),
    2: (0, 1),
    3: (0, 2),
    4: (1, 0),
    5: (1, 1),
    6: (1, 2),
    7: (0, 3),
    8: (1, 3),
}

#: Number of dot rows a cell occupies (3 for 6-dot, 4 for 8-dot braille).
CELL_DOT_ROWS_6 = 3
CELL_DOT_ROWS_8 = 4


def is_braille(ch: str) -> bool:
    """True if ``ch`` is a Unicode Braille Pattern."""
    return BRAILLE_BASE <= ord(ch) <= BRAILLE_END


def mask_of(ch: str) -> int:
    """Dot bitmask of a braille character (bit 0 = dot 1 ... bit 7 = dot 8)."""
    if not is_braille(ch):
        return 0
    return ord(ch) - BRAILLE_BASE


def char_of_mask(mask: int) -> str:
    """Braille character for a dot bitmask."""
    return chr(BRAILLE_BASE + (mask & 0xFF))


def dots_of(ch: str) -> frozenset[int]:
    """Set of raised dot numbers (1..8) for a braille character."""
    mask = mask_of(ch)
    return frozenset(d for d in range(1, 9) if mask & (1 << (d - 1)))


def uses_8dot(ch: str) -> bool:
    """True if the character raises dot 7 or dot 8."""
    return is_braille(ch) and bool(mask_of(ch) & 0b1100_0000)


def brf_to_unicode(text: str) -> str:
    """Convert BRF (Braille ASCII) text to Unicode Braille Patterns.

    Known BRF characters are mapped to their cell; newlines and form feeds
    pass through; anything else is left untouched so the preflight can flag
    it as an illegal character at its original position.
    """
    out = []
    for ch in text:
        mapped = BRF_TO_UNICODE.get(ch)
        if mapped is not None:
            out.append(mapped)
        elif ch in CONTROL_CHARS:
            out.append(ch)
        elif ch == "\t":
            out.append(ch)
        else:
            out.append(ch)  # keep for illegal-character reporting
    return "".join(out)


def unicode_to_brf(text: str) -> str:
    """Convert Unicode Braille Patterns to BRF; unknown chars pass through."""
    return "".join(UNICODE_TO_BRF.get(ch, ch) for ch in text)


def is_blank(ch: str) -> bool:
    """True if ``ch`` is an empty braille cell (a legal line-break point)."""
    return ch == BLANK_CELL


def is_blank_line(text: str) -> bool:
    """True if the line contains only blank cells (or is empty)."""
    return all(is_blank(c) for c in text)
