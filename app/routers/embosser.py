"""Routers: embosser profiles, compile batches, downloads, readback."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..embosser import compile_version, readback_batch
from ..models import CompileBatch, CompileFile, Embosser, Version
from ..schemas import CompileRequest, EmbosserConfig, EmbosserCreate
from .versions import _get_version_or_404, _snapshot_of

router = APIRouter(tags=["embosser"])


# ---------------------------------------------------------------------------
# Embosser profiles
# ---------------------------------------------------------------------------


def _embosser_summary(e: Embosser) -> dict:
    return {
        "id": e.id,
        "name": e.name,
        "config": e.config,
        "created_at": e.created_at.isoformat(),
    }


@router.post("/embossers", status_code=201)
def create_embosser(payload: EmbosserCreate, db: Session = Depends(get_db)) -> dict:
    """Register an embosser profile (grid, 6/8-dot, encoding, page control, duplex)."""
    row = Embosser(name=payload.name, config=payload.config.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return _embosser_summary(row)


@router.get("/embossers")
def list_embossers(db: Session = Depends(get_db)) -> list[dict]:
    return [_embosser_summary(e) for e in db.query(Embosser).order_by(Embosser.id).all()]


def _get_embosser_or_404(db: Session, embosser_id: int) -> Embosser:
    row = db.get(Embosser, embosser_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"embosser {embosser_id} not found")
    return row


@router.get("/embossers/{embosser_id}")
def get_embosser(embosser_id: int, db: Session = Depends(get_db)) -> dict:
    return _embosser_summary(_get_embosser_or_404(db, embosser_id))


@router.delete("/embossers/{embosser_id}", status_code=204)
def delete_embosser(embosser_id: int, db: Session = Depends(get_db)) -> Response:
    """Delete a profile; existing batches keep their config snapshot."""
    db.delete(_get_embosser_or_404(db, embosser_id))
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Compile batches
# ---------------------------------------------------------------------------


def _batch_summary(b: CompileBatch, db: Session) -> dict:
    files = db.query(CompileFile).filter(CompileFile.batch_id == b.id).order_by(CompileFile.id).all()
    return {
        "batch_id": b.id,
        "version_id": b.version_id,
        "embosser_id": b.embosser_id,
        "embosser_name": b.embosser_name,
        "note": b.note,
        "created_at": b.created_at.isoformat(),
        "passes": [f.pass_name for f in files],
        "file_count": len(files),
    }


def _get_batch_or_404(db: Session, batch_id: int) -> CompileBatch:
    b = db.get(CompileBatch, batch_id)
    if b is None:
        raise HTTPException(status_code=404, detail=f"compile batch {batch_id} not found")
    return b


@router.post("/versions/{version_id}/compile", status_code=201)
def compile_version_endpoint(
    version_id: int, payload: CompileRequest, db: Session = Depends(get_db)
) -> dict:
    """Compile a confirmed version into embosser byte streams.

    Binds the version to an embosser config snapshot; on success returns the
    batch with its JSON work ticket.  When the version cannot be embossed on
    the device, responds 422 with the errors (page/row/col/source).
    """
    v = _get_version_or_404(db, version_id)
    if payload.embosser_id is not None:
        emb_row = _get_embosser_or_404(db, payload.embosser_id)
        emb = EmbosserConfig(**emb_row.config)
        embosser_id: Optional[int] = emb_row.id
        embosser_name = emb_row.name
    else:
        emb = payload.embosser
        embosser_id = None
        embosser_name = "inline"

    result = compile_version(_snapshot_of(v), emb)
    if not result["ok"]:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "version cannot be compiled for this embosser",
                "errors": result["errors"],
            },
        )

    batch = CompileBatch(
        version_id=v.id,
        embosser_id=embosser_id,
        embosser_name=embosser_name,
        embosser_config=emb.model_dump(),
        note=payload.note,
        ticket={},
    )
    db.add(batch)
    db.flush()

    passes_out = []
    for p in result["passes"]:
        ext = "brf" if p["encoding"] == "brf" else "brl"
        filename = f"batch_{batch.id}_{p['pass']}.{ext}"
        f = CompileFile(
            batch_id=batch.id,
            pass_name=p["pass"],
            filename=filename,
            encoding=p["encoding"],
            size_bytes=p["size_bytes"],
            sha256=p["sha256"],
            content=p["content"],
        )
        db.add(f)
        db.flush()
        passes_out.append(
            {
                "pass": p["pass"],
                "file_id": f.id,
                "filename": filename,
                "encoding": p["encoding"],
                "page_break": p["page_break"],
                "line_ending": p["line_ending"],
                "paper_order": p["paper_order"],
                "reload": p["reload"],
                "sha256": p["sha256"],
                "size_bytes": p["size_bytes"],
                "cell_mapping": p["cell_mapping"],
            }
        )

    ticket = {
        "batch_id": batch.id,
        **result["context"],
        "embosser": {
            "embosser_id": embosser_id,
            "name": embosser_name,
            "config": emb.model_dump(),
        },
        "note": payload.note,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "passes": passes_out,
    }
    batch.ticket = ticket
    db.commit()
    db.refresh(batch)
    return {"batch_id": batch.id, "version_id": v.id, "ok": True, "ticket": ticket}


@router.get("/compile-batches")
def list_compile_batches(
    version_id: Optional[int] = Query(None), db: Session = Depends(get_db)
) -> list[dict]:
    q = db.query(CompileBatch).order_by(CompileBatch.id)
    if version_id is not None:
        q = q.filter(CompileBatch.version_id == version_id)
    return [_batch_summary(b, db) for b in q.all()]


@router.get("/compile-batches/{batch_id}")
def get_compile_batch(batch_id: int, db: Session = Depends(get_db)) -> dict:
    b = _get_batch_or_404(db, batch_id)
    return {**_batch_summary(b, db), "embosser_config": b.embosser_config, "ticket": b.ticket}


@router.get("/compile-batches/{batch_id}/ticket")
def download_ticket(batch_id: int, db: Session = Depends(get_db)) -> Response:
    """JSON work ticket: paper order, reload orientation, checksums, cell mapping."""
    b = _get_batch_or_404(db, batch_id)
    return Response(
        content=json.dumps(b.ticket, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="compile_ticket_{batch_id}.json"'
        },
    )


@router.get("/compile-batches/{batch_id}/files")
def list_batch_files(batch_id: int, db: Session = Depends(get_db)) -> list[dict]:
    _get_batch_or_404(db, batch_id)
    files = db.query(CompileFile).filter(CompileFile.batch_id == batch_id).order_by(CompileFile.id).all()
    return [
        {
            "file_id": f.id,
            "batch_id": f.batch_id,
            "pass": f.pass_name,
            "filename": f.filename,
            "encoding": f.encoding,
            "size_bytes": f.size_bytes,
            "sha256": f.sha256,
            "created_at": f.created_at.isoformat(),
        }
        for f in files
    ]


@router.get("/compile-files/{file_id}/download")
def download_file(file_id: int, db: Session = Depends(get_db)) -> Response:
    """The sendable byte stream of one pass (BRF or UTF-8 Braille)."""
    f = db.get(CompileFile, file_id)
    if f is None:
        raise HTTPException(status_code=404, detail=f"compile file {file_id} not found")
    return Response(
        content=bytes(f.content),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{f.filename}"'},
    )


@router.post("/compile-batches/{batch_id}/readback")
def readback(batch_id: int, db: Session = Depends(get_db)) -> dict:
    """Re-parse the stored byte streams and verify them against the version.

    Confirms page order, page breaks and the cell mapping still match the
    original version, using the batch's embosser config snapshot.
    """
    b = _get_batch_or_404(db, batch_id)
    version = db.get(Version, b.version_id)
    if version is None:
        raise HTTPException(
            status_code=404,
            detail=f"version {b.version_id} bound to this batch no longer exists",
        )
    emb = EmbosserConfig(**b.embosser_config)
    files = db.query(CompileFile).filter(CompileFile.batch_id == batch_id).order_by(CompileFile.id).all()
    report = readback_batch(
        {**version.snapshot, "version_id": version.id},
        emb,
        [
            {
                "pass_name": f.pass_name,
                "filename": f.filename,
                "sha256": f.sha256,
                "content": bytes(f.content),
            }
            for f in files
        ],
    )
    return {"batch_id": b.id, "version_id": version.id, **report}
