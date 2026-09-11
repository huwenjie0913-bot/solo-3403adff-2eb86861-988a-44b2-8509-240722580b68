"""End-to-end API tests."""


def test_full_workflow(client, job_factory):
    # BRF input: "A1B," per line, 6 lines, one form feed
    job = job_factory("A1B,\nA1B,\nA1B,\fA1B,", source_format="brf")
    job_id = job["id"]
    assert job["line_count"] == 4
    assert job["page_count"] == 2

    # preflight
    resp = client.post(f"/jobs/{job_id}/preflight")
    assert resp.status_code == 200
    report = resp.json()
    assert report["ok"]
    assert report["sheet_count"] == 1
    assert report["sheets"][0]["binding"]["flip_mode"] == "page_flip"
    # stored report retrievable
    resp = client.get(f"/jobs/{job_id}/preflight")
    assert resp.status_code == 200
    assert resp.json()["report_id"] == report["report_id"]

    # dots endpoint
    resp = client.get(f"/jobs/{job_id}/sheets/1/dots")
    assert resp.status_code == 200
    dots = resp.json()
    assert dots["front"]["page"] == 1
    assert dots["back"]["page"] == 2
    assert dots["front"]["dots"], "front side should have raised dots"
    assert dots["binding"]["edge"] == "left"

    # layout
    resp = client.post(f"/jobs/{job_id}/layout", json={"params": {"cells_per_line": 10, "lines_per_page": 5}})
    assert resp.status_code == 201
    run = resp.json()
    assert run["solutions"]
    assert run["solutions"][0]["rank"] == 1

    # confirm version
    resp = client.post(
        f"/jobs/{job_id}/versions",
        json={"layout_run_id": run["run_id"], "solution_index": 0, "note": "approved"},
    )
    assert resp.status_code == 201
    version_id = resp.json()["id"]

    # exports
    resp = client.get(f"/versions/{version_id}/export/pef")
    assert resp.status_code == 200
    assert "<pef" in resp.text and "<row>" in resp.text
    assert "⠁" in resp.text  # BRF 'A' converted to dot 1

    resp = client.get(f"/versions/{version_id}/export/svg", params={"sheet": 1, "layer": "overlay"})
    assert resp.status_code == 200
    assert resp.text.startswith("<svg") or "<svg" in resp.text
    assert "<circle" in resp.text

    resp = client.get(f"/versions/{version_id}/export/svg", params={"sheet": 1, "layer": "back", "style": "deboss"})
    assert resp.status_code == 200

    resp = client.get(f"/versions/{version_id}/export/trace")
    assert resp.status_code == 200
    trace = resp.json()
    assert trace["version_id"] == version_id
    assert trace["content_sha256"]
    assert trace["sheets"][0]["binding"]["flip_mode"] == "page_flip"
    assert trace["solution"]["mapping"]

    # version listing
    resp = client.get(f"/jobs/{job_id}/versions")
    assert [v["id"] for v in resp.json()] == [version_id]


def test_version_from_original(client, job_factory):
    job = job_factory("⠁\n⠁\f⠃")
    resp = client.post(f"/jobs/{job['id']}/versions", json={"source": "original"})
    assert resp.status_code == 201
    version_id = resp.json()["id"]
    resp = client.get(f"/versions/{version_id}/export/pef")
    assert resp.status_code == 200
    assert resp.text.count("<page>") == 2


def test_preflight_flags_illegal_via_api(client, job_factory):
    job = job_factory("⠁hello")
    resp = client.post(f"/jobs/{job['id']}/preflight")
    report = resp.json()
    assert not report["ok"]
    assert report["summary"]["by_type"]["illegal_character"] == 5


def test_layout_unsatisfiable_rules_via_api(client, job_factory):
    # table taller than one page (grid allows 7 lines)
    lines = ["⠛⠛"] * 9
    structure = {"blocks": [{"type": "table", "line_start": 0, "line_end": 9}]}
    job = job_factory("\n".join(lines), structure=structure)
    resp = client.post(f"/jobs/{job['id']}/layout", json={"params": {}})
    assert resp.status_code == 201
    result = resp.json()
    assert any(r["rule"] == "table_keep_together" for r in result["unsatisfiable_rules"])
    assert result["solutions"], "relaxed solution must still be returned"


def test_not_found_and_validation(client):
    assert client.get("/jobs/999").status_code == 404
    assert client.post("/jobs/999/preflight").status_code == 404
    assert client.get("/versions/999").status_code == 404
    # overlapping blocks rejected
    resp = client.post(
        "/jobs",
        json={
            "name": "bad",
            "source_format": "unicode",
            "content": "⠁\n⠁\n⠁",
            "structure": {
                "blocks": [
                    {"type": "paragraph", "line_start": 0, "line_end": 2},
                    {"type": "table", "line_start": 1, "line_end": 3},
                ]
            },
            "config": {},
        },
    )
    assert resp.status_code == 422


def test_solution_index_out_of_range(client, job_factory):
    job = job_factory("⠁")
    run = client.post(f"/jobs/{job['id']}/layout", json={"params": {}}).json()
    resp = client.post(
        f"/jobs/{job['id']}/versions",
        json={"layout_run_id": run["run_id"], "solution_index": 99},
    )
    assert resp.status_code == 422


def test_delete_job_cascades(client, job_factory):
    job = job_factory("⠁")
    job_id = job["id"]
    client.post(f"/jobs/{job_id}/preflight")
    client.post(f"/jobs/{job_id}/versions", json={"source": "original"})
    assert client.delete(f"/jobs/{job_id}").status_code == 204
    assert client.get(f"/jobs/{job_id}").status_code == 404
