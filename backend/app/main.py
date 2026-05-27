"""
Scrub — local PII detection & redaction service.

Two endpoints drive the UI:

  POST /analyze    multipart file upload -> detect PII, return findings,
                   clusters, and an audit report. Stores the parsed document
                   in an in-memory session keyed by a token so /transform can
                   reuse it without re-uploading.

  POST /transform  { session_id, global_policy, overrides } -> applies the
                   chosen policies and returns the cleaned file for download.

Everything runs locally and in-memory. Nothing is written to disk or sent over
the network. Sessions expire from memory on a simple TTL sweep.

Run:  uvicorn app.main:app --reload
"""
from __future__ import annotations

import datetime
import time
import uuid
from pathlib import Path
from typing import Dict, List

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core.detect import detect, ENTITY_META, ENTITY_TAG
from .core.transform import cluster, replacement_for, fake_value
from .adapters import txt, docx_adapter, pdf_adapter

app = FastAPI(title="Scrub", version="1.0")

# Frontend runs same-origin in production; allow localhost dev server too.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "X-Scrub-Saved-Path"],
)

# ---- In-memory session store (no disk, no DB) ------------------------------
_SESSIONS: Dict[str, dict] = {}
_TTL_SECONDS = 60 * 30  # 30 minutes

# Cleaned outputs are written here on every /transform — convenience archive
# for the user. Only the *scrubbed* file lands on disk; the original document
# stays in memory and still expires with the session.
_OUTPUTS_DIR = Path(__file__).parent.parent / "outputs"


def _sweep():
    now = time.time()
    for sid in [s for s, v in _SESSIONS.items() if now - v["created"] > _TTL_SECONDS]:
        _SESSIONS.pop(sid, None)


def _kind(filename: str) -> str:
    name = filename.lower()
    if name.endswith(".docx"):
        return "docx"
    if name.endswith(".pdf"):
        return "pdf"
    if name.endswith(".txt") or name.endswith(".text"):
        return "txt"
    raise HTTPException(400, "Unsupported file type. Use .txt, .docx, or .pdf.")


# ---- /analyze --------------------------------------------------------------
@app.post("/analyze")
async def analyze(file: UploadFile = File(...)):
    _sweep()
    raw = await file.read()
    kind = _kind(file.filename)

    # Extract normalized text (+ format state for round-tripping).
    if kind == "txt":
        text = txt.extract(raw)
        state = None
    elif kind == "docx":
        text, state = docx_adapter.extract(raw)
    else:
        text, state = pdf_adapter.extract(raw)
        if state.is_probably_scanned():
            raise HTTPException(
                422,
                "This PDF looks scanned (image-only). Text-based PDFs are "
                "supported; scanned documents need OCR, which isn't enabled.",
            )

    findings = cluster(detect(text))

    # Build cluster summary for the UI (one row per unique entity).
    clusters: Dict[int, dict] = {}
    for f in findings:
        c = clusters.setdefault(f.cluster_id, {
            "cluster_id": f.cluster_id,
            "type": f.type,
            "label": f.label,
            "color": f.color,
            "text": f.text,
            "count": 0,
            "max_score": 0.0,
            "fake_preview": fake_value(f.type, f.text, f.cluster_id),
            "tag": ENTITY_TAG.get(f.type, "(redacted)"),
        })
        c["count"] += 1
        c["max_score"] = max(c["max_score"], f.score)

    sid = uuid.uuid4().hex
    _SESSIONS[sid] = {
        "created": time.time(),
        "kind": kind,
        "text": text,
        "state": state,
        "findings": findings,
        "filename": file.filename,
    }

    # Audit report — internal tool, so cleartext is fine here.
    report = {
        "filename": file.filename,
        "total_findings": len(findings),
        "unique_entities": len(clusters),
        "by_type": _counts_by_type(findings),
    }

    return {
        "session_id": sid,
        "text": text,
        "findings": [f.to_dict() for f in findings],
        "clusters": sorted(clusters.values(), key=lambda c: c["type"]),
        "report": report,
    }


def _counts_by_type(findings) -> List[dict]:
    agg: Dict[str, int] = {}
    for f in findings:
        agg[f.label] = agg.get(f.label, 0) + 1
    return [{"label": k, "count": v} for k, v in sorted(agg.items())]


# ---- /transform ------------------------------------------------------------
class TransformRequest(BaseModel):
    session_id: str
    global_policy: str = "fake"          # fake | redact | keep
    overrides: Dict[int, str] = {}       # cluster_id -> policy


@app.post("/transform")
async def transform(req: TransformRequest):
    sess = _SESSIONS.get(req.session_id)
    if not sess:
        raise HTTPException(404, "Session expired. Please re-upload the document.")

    findings = sess["findings"]
    text = sess["text"]
    kind = sess["kind"]

    def policy_for(cid: int) -> str:
        return req.overrides.get(cid, req.global_policy)

    # Build edits = (start, end, replacement) for everything not 'keep'.
    edits = []
    policies = []
    for f in findings:
        pol = policy_for(f.cluster_id)
        if pol == "keep":
            continue
        edits.append((f.start, f.end, replacement_for(f, pol)))
        policies.append(pol)

    # Reassemble per format.
    if kind == "txt":
        out_bytes = txt.reassemble(text, edits)
        media = "text/plain"
        ext = ".txt"
    elif kind == "docx":
        out_bytes = docx_adapter.reassemble(sess["state"], edits)
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ext = ".docx"
    else:
        out_bytes = pdf_adapter.reassemble(sess["state"], edits, policies)
        media = "application/pdf"
        ext = ".pdf"

    base = Path(sess["filename"]).stem

    # Mirror to local outputs/ with a timestamp so each transform is preserved.
    _OUTPUTS_DIR.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    saved_path = _OUTPUTS_DIR / f"{base}_scrubbed_{ts}{ext}"
    saved_path.write_bytes(out_bytes)

    headers = {
        "Content-Disposition": f'attachment; filename="{base}_scrubbed{ext}"',
        "X-Scrub-Saved-Path": str(saved_path),
    }
    return Response(content=out_bytes, media_type=media, headers=headers)


# ---- saved outputs ---------------------------------------------------------
# Browseable list of cleaned files that /transform has written to disk so the
# UI can show a "saved sessions" panel and re-download earlier results.
MIME_BY_EXT = {
    ".txt": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
}


@app.get("/api/outputs")
def list_outputs() -> List[dict]:
    if not _OUTPUTS_DIR.exists():
        return []
    rows = []
    for p in _OUTPUTS_DIR.iterdir():
        if not p.is_file():
            continue
        st = p.stat()
        rows.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    rows.sort(key=lambda r: -r["mtime"])  # newest first
    return rows


@app.get("/api/outputs/{filename}")
def download_output(filename: str, disposition: str = "inline"):
    # Strip any directory parts to block path traversal.
    safe = Path(filename).name
    p = _OUTPUTS_DIR / safe
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "Not found.")
    media = MIME_BY_EXT.get(p.suffix.lower(), "application/octet-stream")
    # Default inline so clicking the filename opens text/PDF in a new tab.
    # Pass ?disposition=attachment to force a download (the "download ↓" link).
    # .docx has no inline browser renderer and triggers a save dialog either way.
    disp = "attachment" if disposition == "attachment" else "inline"
    return FileResponse(
        p,
        media_type=media,
        headers={"Content-Disposition": f'{disp}; filename="{safe}"'},
    )


# ---- health + frontend -----------------------------------------------------
@app.get("/api/health")
def health():
    return {"status": "ok"}


# Serve the built frontend if present (single-origin deploy).
_static = Path(__file__).parent.parent / "static"
if _static.exists():
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")
