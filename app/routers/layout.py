"""Routers: layout (reflow) runs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..layout import run_layout
from ..models import LayoutRun
from ..schemas import JobConfig, LayoutRequest, Structure
from .jobs import get_job_or_404

router = APIRouter(tags=["layout"])


@router.post("/jobs/{job_id}/layout", status_code=201)
def create_layout_run(job_id: int, payload: LayoutRequest, db: Session = Depends(get_db)) -> dict:
    """Reflow the job content and return ranked candidate solutions."""
    job = get_job_or_404(db, job_id)
    cfg = JobConfig(**job.config)
    structure = Structure(**job.structure)
    try:
        result = run_layout(job.content, structure, cfg, payload.params)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run = LayoutRun(job_id=job.id, params=payload.params.model_dump(), result=result)
    db.add(run)
    db.commit()
    db.refresh(run)
    return {"run_id": run.id, "job_id": job.id, **result}


@router.get("/jobs/{job_id}/layout")
def list_layout_runs(job_id: int, db: Session = Depends(get_db)) -> list[dict]:
    get_job_or_404(db, job_id)
    runs = (
        db.query(LayoutRun)
        .filter(LayoutRun.job_id == job_id)
        .order_by(LayoutRun.id)
        .all()
    )
    return [
        {
            "run_id": r.id,
            "job_id": r.job_id,
            "created_at": r.created_at.isoformat(),
            "solution_count": len(r.result.get("solutions", [])),
        }
        for r in runs
    ]


@router.get("/layout-runs/{run_id}")
def get_layout_run(run_id: int, db: Session = Depends(get_db)) -> dict:
    run = db.get(LayoutRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"layout run {run_id} not found")
    return {"run_id": run.id, "job_id": run.job_id, **run.result}
