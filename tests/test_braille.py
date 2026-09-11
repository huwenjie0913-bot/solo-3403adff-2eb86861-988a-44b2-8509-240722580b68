"""Unit tests for braille primitives."""

from app.braille import (
    BRF_ASCII,
    brf_to_unicode,
    dots_of,
    is_braille,
    mask_of,
    unicode_to_brf,
    uses_8dot,
)


def test_brf_table_has_64_entries():
    assert len(BRF_ASCII) == 64


def test_brf_to_unicode_basic():
    # "A" = dot 1, "1" = dot 2, space = blank cell
    assert brf_to_unicode("A") == "⠁"
    assert brf_to_unicode("1") == "⠂"
    assert brf_to_unicode(" ") == "⠀"
    assert brf_to_unicode("=") == "⠿"  # all six dots


def test_brf_roundtrip():
    text = "HELLO, WORLD 123"
    assert unicode_to_brf(brf_to_unicode(text)) == text


def test_brf_unknown_char_passthrough():
    # characters outside the BRF table are kept for the preflight to flag
    assert brf_to_unicode("é") == "é"


def test_dots_and_mask():
    assert mask_of("⠁") == 0b00000001
    assert dots_of("⠇") == frozenset({1, 2, 3})
    assert dots_of("⠀") == frozenset()


def test_eight_dot():
    assert uses_8dot("⡀")  # dot 7
    assert uses_8dot("⢀")  # dot 8
    assert not uses_8dot("⠁")


def test_is_braille():
    assert is_braille("⠿")
    assert not is_braille("A")
