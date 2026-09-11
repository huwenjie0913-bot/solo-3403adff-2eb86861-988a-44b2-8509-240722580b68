"""Routers: jobs and preflight."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..braille import brf_to_unicode
from ..content import build_blocks, parse_content
from ..db import get_db
from ..geometry import compute_grid
from ..models import Job, LayoutRun, PreflightReport, Version
from ..preflight import _page_dots, run_preflight
from ..schemas import JobConfig, JobCreate, Structure

router = APIRouter(tags=["jobs"])


def get_job_or_404(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return job


def _job_summary(job: Job) -> dict:
    parsed = parse_content(job.content)
    return {
        "id": job.id,
        "name": job.name,
        "source_format": job.source_format,
        "created_at": job.created_at.isoformat(),
        "line_count": len(parsed.lines),
        "page_count": parsed.page_count,
    }


@router.post("/jobs", status_code=201)
def create_job(payload: JobCreate, db: Session = Depends(get_db)) -> dict:
    content = payload.content
    if payload.source_format == "brf":
        content = brf_to_unicode(content)
    # validate the structure eagerly so bad ranges fail at creation time
    parsed = parse_content(content)
    try:
        build_blocks(parsed, payload.structure)
        compute_grid(payload.config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    job = Job(
        name=payload.name,
        source_format=payload.source_format,
        content=content,
        raw_content=payload.content,
        structure=payload.structure.model_dump(),
        config=payload.config.model_dump(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return {**_job_summary(job), "structure": job.structure, "config": job.config}


@router.get("/jobs")
def list_jobs(db: Session = Depends(get_db)) -> list[dict]:
    return [_job_summary(j) for j in db.query(Job).order_by(Job.id).all()]


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)) -> dict:
    job = get_job_or_404(db, job_id)
    return {**_job_summary(job), "structure": job.structure, "config": job.config}


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: int, db: Session = Depends(get_db)) -> Response:
    job = get_job_or_404(db, job_id)
    for model in (PreflightReport, LayoutRun, Version):
        db.query(model).filter(model.job_id == job_id).delete()
    db.delete(job)
    db.commit()
    return Response(status_code=204)


@router.post("/jobs/{job_id}/preflight")
def run_job_preflight(job_id: int, db: Session = Depends(get_db)) -> dict:
    job = get_job_or_404(db, job_id)
    cfg = JobConfig(**job.config)
    structure = Structure(**job.structure)
    report = run_preflight(job.content, structure, cfg)
    row = PreflightReport(job_id=job.id, ok=report["ok"], report=report)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"report_id": row.id, "job_id": job.id, **report}


@router.get("/jobs/{job_id}/preflight")
def latest_preflight(job_id: int, db: Session = Depends(get_db)) -> dict:
    get_job_or_404(db, job_id)
    row = (
        db.query(PreflightReport)
        .filter(PreflightReport.job_id == job_id)
        .order_by(PreflightReport.id.desc())
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no preflight report yet; POST one first")
    return {"report_id": row.id, "job_id": job_id, **row.report}


@router.get("/jobs/{job_id}/sheets/{sheet_no}/dots")
def sheet_dots(job_id: int, sheet_no: int, db: Session = Depends(get_db)) -> dict:
    """Physical coordinates of every raised dot of one sheet (both sides)."""
    job = get_job_or_404(db, job_id)
    cfg = JobConfig(**job.config)
    grid = compute_grid(cfg)
    parsed = parse_content(job.content)
    pages: list[list[str]] = []
    for line in parsed.lines:
        while len(pages) < line.page:
            pages.append([])
        pages[line.page - 1].append(line.text)
    front_page = 2 * sheet_no - 1
    back_page = 2 * sheet_no
    if front_page > len(pages):
        raise HTTPException(status_code=404, detail=f"sheet {sheet_no} does not exist")

    result: dict = {
        "sheet": sheet_no,
        "binding": {
            "edge": cfg.binding_edge,
            "flip_mode": cfg.flip_mode,
            "interpoint_offset_mm": [cfg.interpoint.offset_x_mm, cfg.interpoint.offset_y_mm],
        },
        "front": None,
        "back": None,
    }
    front_dots, _ = _page_dots(pages[front_page - 1], "front", cfg, grid)
    result["front"] = {"page": front_page, "dots": front_dots}
    if back_page <= len(pages):
        back_dots, _ = _page_dots(pages[back_page - 1], "back", cfg, grid)
        result["back"] = {"page": back_page, "dots": back_dots}
    return result
