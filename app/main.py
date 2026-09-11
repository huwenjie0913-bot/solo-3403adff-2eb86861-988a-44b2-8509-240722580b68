"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from . import __version__, routers
from .db import init_db, make_engine, make_session_factory


def create_app(db_url: str | None = None) -> FastAPI:
    engine = make_engine(db_url)
    init_db(engine)

    app = FastAPI(
        title="Braille Duplex Preflight & Layout Service",
        description=(
            "Preflight and reflow of interpoint (duplex) braille layouts: "
            "dot-coordinate geometry, illegal-character/bounds/capacity/"
            "mirror/collision checks, constraint-aware pagination, ranked "
            "solutions, versioned confirmation, PEF/SVG/JSON export and "
            "embosser job compilation (BRF/UTF-8 byte streams, duplex pass "
            "planning, JSON work tickets, readback verification). "
            "Runs fully locally."
        ),
        version=__version__,
    )
    app.state.engine = engine
    app.state.SessionLocal = make_session_factory(engine)

    app.include_router(routers.jobs.router)
    app.include_router(routers.layout.router)
    app.include_router(routers.versions.router)
    app.include_router(routers.embosser.router)

    @app.get("/")
    def root() -> dict:
        return {
            "service": "braille-duplex-preflight",
            "version": __version__,
            "endpoints": [
                "POST /jobs",
                "GET /jobs",
                "GET /jobs/{id}",
                "POST /jobs/{id}/preflight",
                "GET /jobs/{id}/preflight",
                "GET /jobs/{id}/sheets/{n}/dots",
                "POST /jobs/{id}/layout",
                "GET /jobs/{id}/layout",
                "GET /layout-runs/{id}",
                "POST /jobs/{id}/versions",
                "GET /jobs/{id}/versions",
                "GET /versions/{id}",
                "GET /versions/{id}/export/pef",
                "GET /versions/{id}/export/svg",
                "GET /versions/{id}/export/trace",
                "POST /embossers",
                "GET /embossers",
                "GET /embossers/{id}",
                "DELETE /embossers/{id}",
                "POST /versions/{id}/compile",
                "GET /compile-batches",
                "GET /compile-batches/{id}",
                "GET /compile-batches/{id}/ticket",
                "GET /compile-batches/{id}/files",
                "GET /compile-files/{id}/download",
                "POST /compile-batches/{id}/readback",
            ],
        }

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
