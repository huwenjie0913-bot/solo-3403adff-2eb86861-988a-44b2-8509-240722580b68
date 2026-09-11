"""Routers: versions (confirmed solutions) and exports."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..exports import content_hash, version_to_pef, version_to_svg, version_to_trace
from ..layout import solution_from_original
from ..models import Job, LayoutRun, Version
from ..schemas import JobConfig, LayoutParams, VersionCreate
from .jobs import get_job_or_404

router = APIRouter(tags=["versions"])


def _version_summary(v: Version) -> dict:
    return {
        "id": v.id,
        "job_id": v.job_id,
        "layout_run_id": v.layout_run_id,
        "solution_index": v.solution_index,
        "note": v.note,
        "created_at": v.created_at.isoformat(),
    }


@router.post("/jobs/{job_id}/versions", status_code=201)
def confirm_version(job_id: int, payload: VersionCreate, db: Session = Depends(get_db)) -> dict:
    """Confirm a layout solution (or the original pagination) as a new version."""
    job = get_job_or_404(db, job_id)
    cfg = JobConfig(**job.config)

    if payload.source == "original":
        params = LayoutParams()
        solution = solution_from_original(job.content, cfg, params)
        layout_run_id = None
        solution_index = None
        source = {"source": "original"}
        params_dict = params.model_dump()
    else:
        if payload.layout_run_id is None or payload.solution_index is None:
            raise HTTPException(
                status_code=422,
                detail="layout_run_id and solution_index are required when source='layout'",
            )
        run = db.get(LayoutRun, payload.layout_run_id)
        if run is None or run.job_id != job_id:
            raise HTTPException(status_code=404, detail=f"layout run {payload.layout_run_id} not found")
        solutions = run.result.get("solutions", [])
        if not 0 <= payload.solution_index < len(solutions):
            raise HTTPException(
                status_code=422,
                detail=f"solution_index {payload.solution_index} out of range "
                f"(run has {len(solutions)} solutions)",
            )
        solution = solutions[payload.solution_index]
        layout_run_id = run.id
        solution_index = payload.solution_index
        source = {"layout_run_id": run.id, "solution_index": payload.solution_index}
        params_dict = run.params

    snapshot = {
        "job_id": job.id,
        "job_name": job.name,
        "config": job.config,
        "layout_params": params_dict,
        "solution": solution,
        "content_sha256": content_hash(job.content),
        "source": source,
        "note": payload.note,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    version = Version(
        job_id=job.id,
        layout_run_id=layout_run_id,
        solution_index=solution_index,
        note=payload.note,
        snapshot=snapshot,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    version.snapshot["version_id"] = version.id
    return _version_summary(version)


@router.get("/jobs/{job_id}/versions")
def list_versions(job_id: int, db: Session = Depends(get_db)) -> list[dict]:
    get_job_or_404(db, job_id)
    return [
        _version_summary(v)
        for v in db.query(Version).filter(Version.job_id == job_id).order_by(Version.id).all()
    ]


def _get_version_or_404(db: Session, version_id: int) -> Version:
    v = db.get(Version, version_id)
    if v is None:
        raise HTTPException(status_code=404, detail=f"version {version_id} not found")
    return v


@router.get("/versions/{version_id}")
def get_version(version_id: int, db: Session = Depends(get_db)) -> dict:
    v = _get_version_or_404(db, version_id)
    return {**_version_summary(v), "snapshot": v.snapshot}


@router.get("/versions/{version_id}/export/pef")
def export_pef(version_id: int, db: Session = Depends(get_db)) -> Response:
    v = _get_version_or_404(db, version_id)
    pef = version_to_pef(v.snapshot)
    return Response(
        content=pef,
        media_type="application/x-pef+xml",
        headers={"Content-Disposition": f'attachment; filename="version_{version_id}.pef"'},
    )


@router.get("/versions/{version_id}/export/svg")
def export_svg(
    version_id: int,
    sheet: int = Query(1, ge=1),
    layer: str = Query("overlay", pattern="^(front|back|overlay)$"),
    style: str = Query("emboss", pattern="^(emboss|deboss)$"),
    db: Session = Depends(get_db),
) -> Response:
    """SVG proof of one sheet (embossed/debossed dots, or an overlay)."""
    v = _get_version_or_404(db, version_id)
    try:
        svg = version_to_svg(v.snapshot, sheet_no=sheet, layer=layer, style=style)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(content=svg, media_type="image/svg+xml")


@router.get("/versions/{version_id}/export/trace")
def export_trace(version_id: int, db: Session = Depends(get_db)) -> dict:
    """JSON trace record: config, mapping, dot coordinates, collisions."""
    v = _get_version_or_404(db, version_id)
    return version_to_trace(v.snapshot)
