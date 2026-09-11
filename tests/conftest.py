import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    db_url = f"sqlite:///{tmp_path}/test.db"
    app = create_app(db_url)
    with TestClient(app) as c:
        yield c


def make_config(**overrides):
    """A small but valid config: 10 cells x 5 lines per page."""
    config = {
        "paper": {"width_mm": 100.0, "height_mm": 80.0},
        "margins": {"top_mm": 5.0, "bottom_mm": 5.0, "left_mm": 5.0, "right_mm": 5.0, "binding_mm": 0.0},
        "binding_edge": "left",
        "flip_mode": "page_flip",
        "dot": {"diameter_mm": 1.5, "pitch_mm": 2.5},
        "cell": {"pitch_mm": 6.0, "max_cells_per_line": None},
        "line": {"pitch_mm": 10.0, "max_lines_per_page": None},
        "interpoint": {"enabled": True, "offset_x_mm": 2.5, "offset_y_mm": 0.0, "min_separation_mm": 2.0},
        "allow_8dot": False,
        "back_side_premirrored": False,
    }
    config.update(overrides)
    return config


@pytest.fixture()
def job_factory(client):
    def create(content, config=None, structure=None, source_format="unicode", name="test"):
        payload = {
            "name": name,
            "source_format": source_format,
            "content": content,
            "structure": structure or {"blocks": []},
            "config": config or make_config(),
        }
        resp = client.post("/jobs", json=payload)
        assert resp.status_code == 201, resp.text
        return resp.json()

    return create
