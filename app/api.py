"""FastAPI backend: kick off agent runs and serve the review queue.

All routes except /health require an X-API-Key header matching settings.api_key
(see config/settings.py / .env). Unauthenticated access would let anyone trigger
OpenAI spend, a real portal upload, or read extracted timetable data.
"""
import json
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from loguru import logger
from pydantic import BaseModel

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config.settings import settings  # noqa: E402
from agent.run import process_masjid, process_directory, Session, ReviewItem  # noqa: E402
from mcp_servers.publishing_server import generate_salah_csv, generate_iqamah_csv, portal_upload  # noqa: E402

app = FastAPI(title="MasjidOS")

_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(provided: str | None = Depends(_api_key_header)) -> None:
    if not settings.api_key:
        # Fail closed: an unset API_KEY must not silently mean "open to everyone".
        raise HTTPException(500, "Server misconfigured: API_KEY is not set in .env.")
    if not provided or not secrets.compare_digest(provided, settings.api_key):
        raise HTTPException(401, "Missing or invalid X-API-Key header.")


@app.get("/health")
def health():
    """Unauthenticated liveness check for container/orchestrator health probes."""
    return {"ok": True}


class MasjidIn(BaseModel):
    name: str
    url: str


@app.post("/run/masjid", dependencies=[Depends(require_api_key)])
async def run_one(m: MasjidIn):
    logger.info(f"API: run/masjid requested for '{m.name}'")
    return await process_masjid(m.name, m.url)


class DirectoryIn(BaseModel):
    source: str = ""   # Google Sheets link OR local file path; empty = use .env default


@app.post("/run/all", dependencies=[Depends(require_api_key)])
async def run_all(d: DirectoryIn = DirectoryIn()):
    logger.info(f"API: run/all requested (source={d.source or 'default'})")
    return await process_directory(d.source)


@app.get("/review/queue", dependencies=[Depends(require_api_key)])
def queue():
    s = Session()
    items = s.query(ReviewItem).order_by(ReviewItem.id.desc()).all()
    out = [{"id": i.id, "masjid": i.masjid_name, "status": i.status,
            "min_confidence": i.min_confidence, "rationale": i.rationale,
            "extraction": json.loads(i.extraction_json)} for i in items]
    s.close()
    return out


@app.post("/review/{item_id}/approve", dependencies=[Depends(require_api_key)])
async def approve(item_id: int):
    """Human approves an item -> generate CSVs, upload, and ONLY THEN mark the
    source row as Done (Google Sheet or local file)."""
    from mcp_servers.acquisition_server import mark_row_done

    s = Session()
    item = s.query(ReviewItem).get(item_id)
    if not item:
        s.close(); return {"ok": False, "error": "not found"}
    logger.info(f"API: approve requested for item {item_id} ({item.masjid_name})")
    ej = item.extraction_json
    salah = generate_salah_csv(item.masjid_name, ej)
    iqamah = generate_iqamah_csv(item.masjid_name, ej)
    up = await portal_upload(item.masjid_name, salah["path"], iqamah["path"])

    mark = {"ok": False, "error": "no source/row recorded for this item"}
    if item.source and item.row_number:
        mark = mark_row_done(item.source, item.row_number)

    item.status = "APPROVED"
    s.commit(); s.close()
    return {"ok": True, "salah": salah, "iqamah": iqamah, "upload": up, "marked_done": mark}
