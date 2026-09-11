"""Preflight engine tests (direct function calls)."""

from app.geometry import compute_grid
from app.preflight import run_preflight
from app.schemas import JobConfig, Structure

from .conftest import make_config


def cfg(**kw):
    return JobConfig(**make_config(**kw))


def test_clean_job_passes():
    report = run_preflight("⠁⠃⠉\n⠙⠑", Structure(), cfg())
    assert report["ok"]
    assert report["summary"]["errors"] == 0
    assert report["geometry"]["cols"] == 15  # (90 - 2.5) / 6 + 1
    assert report["geometry"]["rows"] == 7  # (70 - 5) / 10 + 1


def test_illegal_character_reported_with_position():
    report = run_preflight("⠁A⠉", Structure(), cfg())
    assert not report["ok"]
    issues = [i for i in report["issues"] if i["type"] == "illegal_character"]
    assert len(issues) == 1
    assert issues[0]["row"] == 1 and issues[0]["col"] == 2
    assert issues[0]["codepoint"] == "U+0041"


def test_eight_dot_rejected_unless_allowed():
    report = run_preflight("⡀", Structure(), cfg())
    assert any(i["type"] == "eight_dot_not_allowed" for i in report["issues"])
    report8 = run_preflight("⡀", Structure(), cfg(allow_8dot=True))
    assert report8["ok"]


def test_line_too_long():
    report = run_preflight("⠁" * 16, Structure(), cfg())  # grid allows 15
    assert any(i["type"] == "line_too_long" for i in report["issues"])


def test_page_overflow():
    content = "\n".join(["⠁"] * 8)  # grid allows 7 lines
    report = run_preflight(content, Structure(), cfg())
    assert any(i["type"] == "page_overflow" for i in report["issues"])


def test_explicit_page_breaks_create_sheets():
    content = "⠁\f⠃\f⠉"  # 3 pages -> sheet 1 front/back, sheet 2 front
    report = run_preflight(content, Structure(), cfg())
    assert report["page_count"] == 3
    assert report["sheet_count"] == 2
    assert report["sheets"][0]["front"]["page"] == 1
    assert report["sheets"][0]["back"]["page"] == 2
    assert report["sheets"][1]["front"]["page"] == 3
    assert report["sheets"][1]["back"] is None


def test_out_of_bounds_when_offset_pushes_dots_out():
    # a large negative interpoint offset pushes back-side dots off the sheet
    config = make_config()
    config["interpoint"]["offset_x_mm"] = -50.0
    report = run_preflight("⠁\f⠁", Structure(), JobConfig(**config))
    oob = [i for i in report["issues"] if i["type"] == "out_of_bounds"]
    assert oob
    assert oob[0]["side"] == "back"


def test_dot_collision_with_zero_offset():
    config = make_config()
    config["interpoint"]["offset_x_mm"] = 0.0
    config["interpoint"]["offset_y_mm"] = 0.0
    report = run_preflight("⠁\f⠁", Structure(), JobConfig(**config))
    collisions = [i for i in report["issues"] if i["type"] == "dot_collision"]
    assert collisions, "coincident front/back dots must collide"
    assert collisions[0]["distance_mm"] == 0.0
    assert report["sheets"][0]["collisions"]


def test_no_collision_with_staggered_offset():
    report = run_preflight("⠁\f⠁", Structure(), cfg())  # offset 2.5 mm
    assert not any(i["type"] == "dot_collision" for i in report["issues"])


def test_mirror_mismatch_detected():
    structure = Structure(
        sheets=[{"front": ["⠁"], "back": ["⠉"]}]  # content expects back = ⠃
    )
    report = run_preflight("⠁\f⠃", structure, cfg())
    mirror = [i for i in report["issues"] if i["type"] == "mirror_mismatch"]
    assert mirror
    assert mirror[0]["expected"] == "⠃"
    assert mirror[0]["actual"] == "⠉"


def test_premirrored_back_side_accepted():
    # operator mirrored the back line for page_flip: reading order "⠃⠁"
    # becomes "⠁⠃" physically
    structure = Structure(sheets=[{"front": ["⠁"], "back": ["⠁⠃"]}])
    config = make_config()
    config["back_side_premirrored"] = True
    report = run_preflight("⠁\f⠃⠁", structure, JobConfig(**config))
    assert not any(i["type"] == "mirror_mismatch" for i in report["issues"])


def test_sheet_count_mismatch_warns():
    structure = Structure(sheets=[])
    report = run_preflight("⠁\f⠃", structure, cfg())
    assert any(i["type"] == "sheet_count_mismatch" for i in report["issues"])


def test_sheets_output_contains_matrix_and_binding():
    report = run_preflight("⠁\f⠃", Structure(), cfg())
    sheet = report["sheets"][0]
    assert sheet["binding"]["edge"] == "left"
    assert sheet["binding"]["flip_mode"] == "page_flip"
    assert sheet["front"]["matrix"][0][0] == 1  # dot 1
    assert sheet["back"]["matrix"][0][0] == 2  # dot 2
    assert sheet["front"]["dot_count"] == 1


def test_grid_geometry():
    grid = compute_grid(cfg())
    assert grid.cols == 15
    assert grid.rows == 7
    assert grid.printable_front.left == 5.0
    assert grid.printable_front.right == 95.0
