"""SQLAlchemy models."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    source_format: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)  # normalized Unicode braille
    raw_content: Mapped[str] = mapped_column(Text)  # as submitted
    structure: Mapped[dict] = mapped_column(JSON)
    config: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class PreflightReport(Base):
    __tablename__ = "preflight_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    ok: Mapped[bool] = mapped_column(Boolean)
    report: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class LayoutRun(Base):
    __tablename__ = "layout_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    params: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Version(Base):
    __tablename__ = "versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    layout_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    solution_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Embosser(Base):
    __tablename__ = "embossers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict] = mapped_column(JSON)  # EmbosserConfig
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class CompileBatch(Base):
    """One compile run: a version bound to an embosser config snapshot."""

    __tablename__ = "compile_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("versions.id"), index=True)
    embosser_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embosser_name: Mapped[str] = mapped_column(String(200), default="")
    embosser_config: Mapped[dict] = mapped_column(JSON)  # snapshot at compile time
    note: Mapped[str] = mapped_column(Text, default="")
    ticket: Mapped[dict] = mapped_column(JSON)  # JSON work ticket (工单)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class CompileFile(Base):
    """One sendable byte stream of a compile batch (one per pass)."""

    __tablename__ = "compile_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("compile_batches.id"), index=True)
    pass_name: Mapped[str] = mapped_column(String(20))  # duplex | front | back
    filename: Mapped[str] = mapped_column(String(200))
    encoding: Mapped[str] = mapped_column(String(20))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
