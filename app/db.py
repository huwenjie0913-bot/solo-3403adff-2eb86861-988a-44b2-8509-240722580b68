"""Database setup: engine, session factory, FastAPI dependency."""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from .models import Base

DEFAULT_DB_URL = "sqlite:///./braille_preflight.db"


def make_engine(db_url: str | None = None):
    url = db_url or os.environ.get("BRAILLE_DB_URL", DEFAULT_DB_URL)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args)


def make_session_factory(engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(engine) -> None:
    Base.metadata.create_all(engine)


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI dependency: a session from the app's session factory."""
    session_local = request.app.state.SessionLocal
    db = session_local()
    try:
        yield db
    finally:
        db.close()
