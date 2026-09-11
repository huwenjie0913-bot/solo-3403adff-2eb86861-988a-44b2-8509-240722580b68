"""Layout engine tests (direct function calls)."""

from app.layout import run_layout, solution_from_original
from app.schemas import JobConfig, LayoutParams, Structure, Block

from .conftest import make_config


def cfg():
    return JobConfig(**make_config())


# grid of make_config: 15 cells x 7 lines
PARAMS = LayoutParams(cells_per_line=10, lines_per_page=5)


def run(content, structure=None, params=None):
    return run_layout(content, structure or Structure(), cfg(), params or PARAMS)


def test_basic_pagination_and_mapping():
    # 7 one-line paragraphs (blank lines keep them separate), capacity 5
    content = "\n\n".join(["⠁" * 5] * 7)
    params = LayoutParams(cells_per_line=10, lines_per_page=5, blank_lines_between_paragraphs=0)
    result = run(content, params=params)
    assert result["solutions"]
    best = result["solutions"][0]
    # 13 output lines (7 paragraphs + 6 source blanks) -> 3 pages
    assert best["stats"]["pages"] == 3
    # every source line is mapped
    for line_no in range(13):
        assert str(line_no) in best["mapping"]
    assert best["mapping"]["0"][0] == {"page": 1, "row": 1}
    assert best["mapping"]["4"][0] == {"page": 1, "row": 5}
    assert best["mapping"]["6"][0] == {"page": 2, "row": 2}
    assert best["mapping"]["12"][0] == {"page": 3, "row": 3}


def test_paragraph_rewrap_preserves_cells():
    # one paragraph of 25 cells + blanks; wrap width 10
    text = "⠁⠁⠁⠁⠀⠁⠁⠁⠁⠀⠁⠁⠁⠁⠀⠁⠁⠁⠁⠀⠁⠁⠁⠁"
    structure = Structure(blocks=[Block(type="paragraph", line_start=0, line_end=1)])
    result = run(text, structure)
    best = result["solutions"][0]
    out_text = "".join(
        line["text"] for page in best["pages"] for line in page["lines"] if line["kind"] == "paragraph"
    )
    assert out_text == text  # no cell added, removed or altered


def test_explicit_page_break_is_locked():
    # blank lines keep the paragraphs separate; the form feed must not move
    content = "⠁\n\n⠁\f⠃\n\n⠃"
    params = LayoutParams(cells_per_line=10, lines_per_page=5)
    result = run(content, params=params)
    best = result["solutions"][0]
    assert best["stats"]["pages"] == 2
    page1_text = [l["text"] for l in best["pages"][0]["lines"]]
    page2_text = [l["text"] for l in best["pages"][1]["lines"]]
    assert page1_text == ["⠁", "", "⠁"]
    assert page2_text == ["⠃", "", "⠃"]


def test_table_not_split():
    lines = ["⠁" * 4] * 4 + ["⠛" * 4] * 4  # paragraph 4 lines + table 4 lines
    structure = Structure(
        blocks=[
            Block(type="paragraph", line_start=0, line_end=4),
            Block(type="table", line_start=4, line_end=8),
        ]
    )
    params = LayoutParams(cells_per_line=10, lines_per_page=5, blank_lines_around_table=0)
    result = run("\n".join(lines), structure, params)
    best = result["solutions"][0]
    # table must sit entirely on page 2
    table_pages = {
        page["page"]
        for page in best["pages"]
        for line in page["lines"]
        if line["kind"] == "table"
    }
    assert len(table_pages) == 1
    assert not any(v["rule"] == "table_keep_together" for v in best["violations"])


def test_oversize_table_reports_unsatisfiable():
    lines = ["⠛" * 4] * 6  # table taller than the 5-line page
    structure = Structure(blocks=[Block(type="table", line_start=0, line_end=6)])
    result = run("\n".join(lines), structure)
    assert any(r["rule"] == "table_keep_together" for r in result["unsatisfiable_rules"])
    best = result["solutions"][0]
    assert any(v["rule"] == "table_keep_together" for v in best["violations"])


def test_heading_keeps_with_next():
    # para(4) + heading(1) + para(4), capacity 5: without keep-with-next the
    # heading would sit alone at the bottom of page 1
    lines = ["⠁" * 3] * 4 + ["⠓"] + ["⠃" * 3] * 4
    structure = Structure(
        blocks=[
            Block(type="paragraph", line_start=0, line_end=4),
            Block(type="heading", line_start=4, line_end=5),
            Block(type="paragraph", line_start=5, line_end=9),
        ]
    )
    params = LayoutParams(
        cells_per_line=10,
        lines_per_page=5,
        blank_line_before_heading=0,
        blank_line_after_heading=0,
        blank_lines_between_paragraphs=0,
        heading_keep_with_next_lines=2,
    )
    result = run("\n".join(lines), structure, params)
    best = result["solutions"][0]
    heading_page = next(
        page["page"] for page in best["pages"] for line in page["lines"] if line["kind"] == "heading"
    )
    para_lines_after = [
        line
        for page in best["pages"]
        if page["page"] == heading_page
        for line in page["lines"]
        if line["kind"] == "paragraph"
    ]
    assert len(para_lines_after) >= 2


def test_widow_orphan_avoided():
    # 6 para lines, capacity 5, min 2/2: split must be 3+3 or 4+2 etc.
    lines = ["⠁" * 3] * 6
    structure = Structure(blocks=[Block(type="paragraph", line_start=0, line_end=6)])
    params = LayoutParams(
        cells_per_line=10,
        lines_per_page=5,
        paragraph_min_lines_at_page_end=2,
        paragraph_min_lines_at_page_start=2,
    )
    result = run("\n".join(lines), structure, params)
    best = result["solutions"][0]
    assert not any("paragraph_min_lines" in v["rule"] for v in best["violations"])
    counts = [
        sum(1 for l in page["lines"] if l["kind"] == "paragraph") for page in best["pages"]
    ]
    assert min(counts) >= 2


def test_max_pages_violation():
    # 12 one-line paragraphs + 11 source blanks = 23 lines -> 5 pages
    content = "\n\n".join(["⠁"] * 12)
    params = LayoutParams(
        cells_per_line=10, lines_per_page=5, blank_lines_between_paragraphs=0, max_pages=2
    )
    result = run(content, params=params)
    best = result["solutions"][0]
    assert best["stats"]["pages"] == 5
    assert any(v["rule"] == "max_pages" for v in best["violations"])


def test_solutions_ranked_by_pages_then_violations_then_changes():
    content = "\n".join(["⠁" * 3] * 12)
    result = run(content)
    keys = [
        (s["stats"]["pages"], s["stats"]["violations"], s["stats"]["changes"])
        for s in result["solutions"]
    ]
    assert keys == sorted(keys)
    assert [s["rank"] for s in result["solutions"]] == list(range(1, len(keys) + 1))


def test_original_solution_keeps_pagination():
    content = "⠁\n⠁\f⠃"
    sol = solution_from_original(content, cfg(), PARAMS)
    assert sol["stats"]["pages"] == 2
    assert sol["pages"][0]["lines"][0]["text"] == "⠁"
    assert sol["pages"][1]["lines"][0]["text"] == "⠃"
    assert sol["stats"]["changes"] == 0


def test_forced_mid_word_break_recorded():
    text = "⠁" * 25  # one long word, width 10, break_at_blank_only
    structure = Structure(blocks=[Block(type="paragraph", line_start=0, line_end=1)])
    result = run(text, structure)
    best = result["solutions"][0]
    assert any(v["rule"] == "break_at_blank_only" for v in best["violations"])
    out_text = "".join(
        l["text"] for p in best["pages"] for l in p["lines"] if l["kind"] == "paragraph"
    )
    assert out_text == text


def test_sheet_and_side_assignment():
    # 12 one-line paragraphs + 11 source blanks = 23 lines -> 5 pages
    content = "\n\n".join(["⠁"] * 12)
    params = LayoutParams(cells_per_line=10, lines_per_page=5, blank_lines_between_paragraphs=0)
    result = run(content, params=params)
    best = result["solutions"][0]
    assert best["pages"][0]["side"] == "front" and best["pages"][0]["sheet"] == 1
    assert best["pages"][1]["side"] == "back" and best["pages"][1]["sheet"] == 1
    assert best["pages"][2]["side"] == "front" and best["pages"][2]["sheet"] == 2
