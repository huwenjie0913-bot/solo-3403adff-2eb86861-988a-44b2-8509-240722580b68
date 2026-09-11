"""Embosser compile module tests: profiles, compilation, tickets, readback."""

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import CompileFile
from tests.conftest import make_config


def make_embosser(**overrides):
    """A small device profile: 10 cells x 5 lines, 6-dot, BRF, native duplex."""
    config = {
        "cells_per_line": 10,
        "lines_per_page": 5,
        "supports_8dot": False,
        "input_encoding": "brf",
        "page_break": "form_feed",
        "line_ending": "lf",
        "duplex_mode": "native_duplex",
        "pad_missing_back": False,
        "extra_control_bytes": [],
    }
    config.update(overrides)
    return config


def make_version(client, job_factory, content="⠁\n⠃\f⠅\n⠉", config=None):
    """Create a job and confirm its original pagination as a version."""
    job = job_factory(content, config=config or make_config())
    resp = client.post(f"/jobs/{job['id']}/versions", json={"source": "original"})
    assert resp.status_code == 201, resp.text
    return job, resp.json()["id"]


def compile_ok(client, version_id, **embosser_overrides):
    resp = client.post(
        f"/versions/{version_id}/compile", json={"embosser": make_embosser(**embosser_overrides)}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Embosser profiles
# ---------------------------------------------------------------------------


def test_embosser_crud(client):
    resp = client.post("/embossers", json={"name": "Index Basic-D", "config": make_embosser()})
    assert resp.status_code == 201, resp.text
    emb = resp.json()
    assert emb["config"]["cells_per_line"] == 10

    resp = client.get("/embossers")
    assert [e["id"] for e in resp.json()] == [emb["id"]]

    resp = client.get(f"/embossers/{emb['id']}")
    assert resp.json()["name"] == "Index Basic-D"

    resp = client.delete(f"/embossers/{emb['id']}")
    assert resp.status_code == 204
    assert client.get(f"/embossers/{emb['id']}").status_code == 404


def test_embosser_config_validation(client):
    bad = make_embosser(cells_per_line=0)
    assert client.post("/embossers", json={"name": "x", "config": bad}).status_code == 422
    bad = make_embosser(extra_control_bytes=[300])
    assert client.post("/embossers", json={"name": "x", "config": bad}).status_code == 422
    bad = make_embosser(duplex_mode="duplexinator")
    assert client.post("/embossers", json={"name": "x", "config": bad}).status_code == 422


# ---------------------------------------------------------------------------
# Native duplex compilation
# ---------------------------------------------------------------------------


def test_compile_native_duplex_brf(client, job_factory):
    job, vid = make_version(client, job_factory)  # page1 ⠁/⠃, page2 ⠅/⠉
    emb_id = client.post("/embossers", json={"name": "d", "config": make_embosser()}).json()["id"]

    resp = client.post(f"/versions/{vid}/compile", json={"embosser_id": emb_id, "note": "run 1"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    ticket = body["ticket"]
    assert ticket["version_id"] == vid
    assert ticket["embosser"]["embosser_id"] == emb_id
    assert ticket["grid_check"]["version_grid"] == {"cols": 15, "rows": 7}
    assert ticket["grid_check"]["device"] == {"cells_per_line": 10, "lines_per_page": 5}

    # one duplex pass, pages paired per sheet
    assert len(ticket["passes"]) == 1
    p = ticket["passes"][0]
    assert p["pass"] == "duplex"
    assert p["reload"] is None
    assert [(o["sheet"], o["side"], o["version_page"]) for o in p["paper_order"]] == [
        (1, "front", 1),
        (1, "back", 2),
    ]
    # cell mapping traces back to source lines
    assert p["cell_mapping"][0]["lines"][0]["text"] == "⠁"
    assert p["cell_mapping"][0]["lines"][0]["source_lines"] == [0]

    # the byte stream: BRF letters, LF line endings, FF after each page
    files = client.get(f"/compile-batches/{body['batch_id']}/files").json()
    assert len(files) == 1
    assert files[0]["filename"].endswith(".brf")
    resp = client.get(f"/compile-files/{files[0]['file_id']}/download")
    assert resp.status_code == 200
    assert resp.content == b"A\nB\n\x0cK\nC\n\x0c"
    assert hashlib.sha256(resp.content).hexdigest() == p["sha256"] == files[0]["sha256"]


def test_compile_crlf_and_no_page_break(client, job_factory):
    _, vid = make_version(client, job_factory)
    body = compile_ok(client, vid, line_ending="crlf", page_break="none")
    p = body["ticket"]["passes"][0]
    resp = client.get(f"/compile-files/{p['file_id']}/download")
    assert resp.content == b"A\r\nB\r\nK\r\nC\r\n"


def test_compile_line_advance_pads_pages(client, job_factory):
    _, vid = make_version(client, job_factory, content="⠁\n⠃\f⠅")
    body = compile_ok(client, vid, page_break="line_advance")
    p = body["ticket"]["passes"][0]
    resp = client.get(f"/compile-files/{p['file_id']}/download")
    # page 1: 2 lines + 3 blank padding lines; page 2: 1 line + 4 padding
    assert resp.content == b"A\nB\n" + b"\n" * 3 + b"K\n" + b"\n" * 4


def test_compile_unicode_braille_8dot(client, job_factory):
    cfg = make_config(allow_8dot=True)
    _, vid = make_version(client, job_factory, content="⣿\f⠃", config=cfg)
    body = compile_ok(client, vid, input_encoding="unicode_braille", supports_8dot=True)
    p = body["ticket"]["passes"][0]
    assert p["encoding"] == "unicode_braille"
    files = client.get(f"/compile-batches/{body['batch_id']}/files").json()
    assert files[0]["filename"].endswith(".brl")
    resp = client.get(f"/compile-files/{p['file_id']}/download")
    assert resp.content == "⣿\n\f⠃\n\f".encode("utf-8")


# ---------------------------------------------------------------------------
# Simplex pass planning
# ---------------------------------------------------------------------------


def test_compile_simplex_page_flip(client, job_factory):
    _, vid = make_version(client, job_factory, content="⠁\f⠃\f⠅\f⠉")
    body = compile_ok(client, vid, duplex_mode="simplex_manual")
    passes = {p["pass"]: p for p in body["ticket"]["passes"]}
    assert set(passes) == {"front", "back"}

    front = passes["front"]
    assert [(o["sheet"], o["version_page"]) for o in front["paper_order"]] == [(1, 1), (2, 3)]
    assert front["reload"] is None

    back = passes["back"]
    # page_flip: flip the whole stack left-right -> backs in forward order
    assert back["reload"]["direction"] == "left_right"
    assert back["reload"]["reversed"] is False
    assert back["reload"]["sheet_order"] == "forward"
    assert [(o["sheet"], o["version_page"]) for o in back["paper_order"]] == [(1, 2), (2, 4)]

    front_bytes = client.get(f"/compile-files/{front['file_id']}/download").content
    back_bytes = client.get(f"/compile-files/{back['file_id']}/download").content
    assert front_bytes == b"A\n\x0cK\n\x0c"
    assert back_bytes == b"B\n\x0cC\n\x0c"


def test_compile_simplex_top_flip_reverses_back_pass(client, job_factory):
    cfg = make_config(flip_mode="top_flip")
    _, vid = make_version(client, job_factory, content="⠁\f⠃\f⠅\f⠉", config=cfg)
    body = compile_ok(client, vid, duplex_mode="simplex_manual")
    passes = {p["pass"]: p for p in body["ticket"]["passes"]}

    back = passes["back"]
    # top_flip: re-feed one by one from the stack top -> backs in reverse order
    assert back["reload"]["direction"] == "top_bottom"
    assert back["reload"]["reversed"] is True
    assert back["reload"]["sheet_order"] == "reverse"
    assert [(o["sheet"], o["version_page"]) for o in back["paper_order"]] == [(2, 4), (1, 2)]

    back_bytes = client.get(f"/compile-files/{back['file_id']}/download").content
    assert back_bytes == b"C\n\x0cB\n\x0c"


# ---------------------------------------------------------------------------
# Compile errors (page / row / col / source)
# ---------------------------------------------------------------------------


def _compile_errors(client, version_id, **embosser_overrides):
    resp = client.post(
        f"/versions/{version_id}/compile", json={"embosser": make_embosser(**embosser_overrides)}
    )
    assert resp.status_code == 422, resp.text
    return resp.json()["detail"]["errors"]


def test_compile_eight_dot_not_supported(client, job_factory):
    cfg = make_config(allow_8dot=True)
    _, vid = make_version(client, job_factory, content="⣿\f⠃", config=cfg)
    errors = _compile_errors(client, vid)  # 6-dot device
    assert len(errors) == 1
    e = errors[0]
    assert e["type"] == "eight_dot_not_supported"
    assert (e["page"], e["row"], e["col"], e["source"]) == (1, 1, 1, "duplex")


def test_compile_8dot_unencodable_in_brf(client, job_factory):
    cfg = make_config(allow_8dot=True)
    _, vid = make_version(client, job_factory, content="⣿\f⠃", config=cfg)
    errors = _compile_errors(client, vid, supports_8dot=True)  # BRF table is 6-dot only
    assert [e["type"] for e in errors] == ["unencodable_character"]
    assert (errors[0]["page"], errors[0]["row"], errors[0]["col"]) == (1, 1, 1)


def test_compile_unencodable_character(client, job_factory):
    _, vid = make_version(client, job_factory, content="⠁x\f⠃")
    errors = _compile_errors(client, vid)
    assert [e["type"] for e in errors] == ["unencodable_character"]
    e = errors[0]
    assert (e["page"], e["row"], e["col"], e["source"]) == (1, 1, 2, "duplex")
    assert e["char"] == "x"


def test_compile_line_too_long(client, job_factory):
    _, vid = make_version(client, job_factory, content="⠁" * 12 + "\f⠃")
    errors = _compile_errors(client, vid)
    assert [e["type"] for e in errors] == ["line_too_long"]
    e = errors[0]
    assert (e["page"], e["row"], e["source"]) == (1, 1, "duplex")
    assert e["col"] == 11  # first column beyond the 10-cell device width
    assert (e["cells"], e["max_cells"]) == (12, 10)


def test_compile_page_overflow(client, job_factory):
    _, vid = make_version(client, job_factory, content="⠁\n⠁\n⠁\n⠁\n⠁\n⠁\f⠃")
    errors = _compile_errors(client, vid)
    assert [e["type"] for e in errors] == ["page_overflow"]
    e = errors[0]
    assert (e["page"], e["source"]) == (1, "duplex")
    assert e["row"] == 6  # first row beyond the 5-line device page
    assert e["col"] is None
    assert (e["lines"], e["max_lines"]) == (6, 5)


def test_compile_missing_back_side(client, job_factory):
    _, vid = make_version(client, job_factory, content="⠁")  # odd page count
    errors = _compile_errors(client, vid)
    assert [e["type"] for e in errors] == ["missing_back_side"]
    e = errors[0]
    assert (e["page"], e["sheet"], e["side"], e["source"]) == (1, 1, "back", "duplex")
    assert e["row"] is None and e["col"] is None  # a whole page is missing


def test_compile_pad_missing_back(client, job_factory):
    _, vid = make_version(client, job_factory, content="⠁")
    body = compile_ok(client, vid, pad_missing_back=True)
    p = body["ticket"]["passes"][0]
    assert [(o["side"], o["padded"]) for o in p["paper_order"]] == [
        ("front", False),
        ("back", True),
    ]
    assert p["paper_order"][1]["version_page"] is None
    content = client.get(f"/compile-files/{p['file_id']}/download").content
    assert content == b"A\n\x0c\x0c"  # blank back page: just the form feed


def test_compile_control_byte_conflict(client, job_factory):
    _, vid = make_version(client, job_factory)  # ⠁ encodes to 0x41 ('A') in BRF
    errors = _compile_errors(client, vid, extra_control_bytes=[0x41])
    assert [e["type"] for e in errors] == ["control_byte_conflict"]
    e = errors[0]
    assert (e["page"], e["row"], e["col"], e["source"]) == (1, 1, 1, "duplex")
    assert e["bytes"] == ["0x41"]


def test_compile_error_source_simplex(client, job_factory):
    cfg = make_config(allow_8dot=True)
    _, vid = make_version(client, job_factory, content="⠁\f⣿", config=cfg)
    errors = _compile_errors(client, vid, duplex_mode="simplex_manual")
    assert [e["type"] for e in errors] == ["eight_dot_not_supported"]
    assert (errors[0]["page"], errors[0]["source"]) == (2, "back")


def test_compile_request_validation(client, job_factory):
    _, vid = make_version(client, job_factory)
    assert client.post(f"/versions/{vid}/compile", json={}).status_code == 422
    both = {"embosser_id": 1, "embosser": make_embosser()}
    assert client.post(f"/versions/{vid}/compile", json=both).status_code == 422
    missing = {"embosser": make_embosser()}
    assert client.post("/versions/999/compile", json=missing).status_code == 404
    assert client.post(f"/versions/{vid}/compile", json={"embosser_id": 999}).status_code == 404


# ---------------------------------------------------------------------------
# Batch binding, ticket download, cascade
# ---------------------------------------------------------------------------


def test_batch_binds_version_and_config_snapshot(client, job_factory):
    _, vid = make_version(client, job_factory)
    emb_id = client.post("/embossers", json={"name": "d", "config": make_embosser()}).json()["id"]
    body = client.post(f"/versions/{vid}/compile", json={"embosser_id": emb_id}).json()
    batch_id = body["batch_id"]

    # deleting the embosser must not break the batch (config snapshot)
    client.delete(f"/embossers/{emb_id}")
    detail = client.get(f"/compile-batches/{batch_id}").json()
    assert detail["embosser_config"]["cells_per_line"] == 10
    assert detail["ticket"]["embosser"]["embosser_id"] == emb_id

    batches = client.get("/compile-batches", params={"version_id": vid}).json()
    assert [b["batch_id"] for b in batches] == [batch_id]
    assert batches[0]["passes"] == ["duplex"]

    resp = client.get(f"/compile-batches/{batch_id}/ticket")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    ticket = resp.json()
    assert ticket["passes"][0]["cell_mapping"][0]["lines"][0]["text"] == "⠁"


def test_delete_job_cascades_compile_batches(client, job_factory):
    job, vid = make_version(client, job_factory)
    body = compile_ok(client, vid)
    assert client.delete(f"/jobs/{job['id']}").status_code == 204
    assert client.get("/compile-batches").json() == []
    assert client.get(f"/compile-batches/{body['batch_id']}").status_code == 404


# ---------------------------------------------------------------------------
# Readback
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path}/embosser_test.db")
    with TestClient(app) as c:
        yield app, c


def _make_batch(client, content="⠁\n⠃\f⠅\n⠉", config=None, **embosser_overrides):
    job = client.post(
        "/jobs",
        json={
            "name": "rb",
            "source_format": "unicode",
            "content": content,
            "structure": {"blocks": []},
            "config": config or make_config(),
        },
    ).json()
    vid = client.post(f"/jobs/{job['id']}/versions", json={"source": "original"}).json()["id"]
    return compile_ok(client, vid, **embosser_overrides)


def test_readback_ok(client, job_factory):
    body = _make_batch(client)
    resp = client.post(f"/compile-batches/{body['batch_id']}/readback")
    assert resp.status_code == 200
    report = resp.json()
    assert report["ok"] is True
    f = report["files"][0]
    assert (f["pages_checked"], f["lines_checked"], f["cells_checked"]) == (2, 4, 4)
    assert f["mismatches"] == []


def test_readback_simplex_and_line_advance(client, job_factory):
    body = _make_batch(
        client, content="⠁\n⠃\f⠅", page_break="line_advance", duplex_mode="simplex_manual",
        pad_missing_back=True,
    )
    report = client.post(f"/compile-batches/{body['batch_id']}/readback").json()
    assert report["ok"] is True
    by_pass = {f["pass"]: f for f in report["files"]}
    # front pass: 2 lines + 3 padding; back pass: 1 line + 4 padding (padded sheet)
    assert by_pass["front"]["lines_checked"] == 5
    assert by_pass["back"]["lines_checked"] == 5


def test_readback_detects_tampered_cells(app_client):
    app, client = app_client
    body = _make_batch(client)
    file_id = body["ticket"]["passes"][0]["file_id"]

    session = app.state.SessionLocal()
    row = session.get(CompileFile, file_id)
    data = bytearray(bytes(row.content))
    data[0] = ord("B")  # ⠁ (mask 1) -> ⠃ (mask 3)
    row.content = bytes(data)
    row.sha256 = hashlib.sha256(bytes(data)).hexdigest()
    session.commit()
    session.close()

    report = client.post(f"/compile-batches/{body['batch_id']}/readback").json()
    assert report["ok"] is False
    mm = report["mismatches"]
    assert len(mm) == 1
    assert mm[0]["type"] == "cell_mismatch"
    assert (mm[0]["file_page"], mm[0]["row"], mm[0]["col"]) == (1, 1, 1)
    assert (mm[0]["expected_mask"], mm[0]["actual_mask"]) == (1, 3)
    assert (mm[0]["version_page"], mm[0]["sheet"], mm[0]["side"]) == (1, 1, "front")


def test_readback_detects_checksum_and_page_break_damage(app_client):
    app, client = app_client
    body = _make_batch(client)
    file_id = body["ticket"]["passes"][0]["file_id"]

    session = app.state.SessionLocal()
    row = session.get(CompileFile, file_id)
    data = bytes(row.content).replace(b"\x0c", b"")  # strip the form feeds
    row.content = data  # keep the stale sha256 on purpose
    session.commit()
    session.close()

    report = client.post(f"/compile-batches/{body['batch_id']}/readback").json()
    assert report["ok"] is False
    types = {m["type"] for m in report["mismatches"]}
    assert "checksum_mismatch" in types
    assert "missing_page_break" in types


def test_readback_missing_final_form_feed_fails(app_client):
    app, client = app_client
    body = _make_batch(client)  # duplex, form_feed page control
    file_id = body["ticket"]["passes"][0]["file_id"]

    session = app.state.SessionLocal()
    row = session.get(CompileFile, file_id)
    data = bytes(row.content)
    assert data.endswith(b"\x0c")
    data = data[:-1]  # strip only the final form feed
    row.content = data
    row.sha256 = hashlib.sha256(data).hexdigest()  # keep the checksum clean
    session.commit()
    session.close()

    report = client.post(f"/compile-batches/{body['batch_id']}/readback").json()
    assert report["ok"] is False
    mm = report["mismatches"]
    assert [m["type"] for m in mm] == ["missing_page_break"]
    assert mm[0]["file_page"] == 2  # the last page lost its form feed
    # page order and the cell mapping were still verified before the break
    f = report["files"][0]
    assert (f["pages_checked"], f["cells_checked"]) == (2, 4)


def test_readback_missing_back_file_fails(app_client):
    app, client = app_client
    body = _make_batch(client, content="⠁\f⠃", duplex_mode="simplex_manual")
    files = client.get(f"/compile-batches/{body['batch_id']}/files").json()
    back_id = {f["pass"]: f["file_id"] for f in files}["back"]

    session = app.state.SessionLocal()
    session.delete(session.get(CompileFile, back_id))  # lose the back-pass file
    session.commit()
    session.close()

    report = client.post(f"/compile-batches/{body['batch_id']}/readback").json()
    assert report["ok"] is False
    missing = [m for m in report["mismatches"] if m["type"] == "missing_file"]
    assert len(missing) == 1
    assert missing[0]["source"] == "back"
    # the remaining front file is still verified against the version
    by_pass = {f["pass"]: f for f in report["files"]}
    assert by_pass["front"]["ok"] is True
    assert (by_pass["front"]["pages_checked"], by_pass["front"]["cells_checked"]) == (1, 1)
    assert by_pass["back"]["ok"] is False
    assert by_pass["back"]["filename"] is None
