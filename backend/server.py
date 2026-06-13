"""
ENRI Dashboard — Backend API
============================
FastAPI service for the ENRI-RDS/dashboard project.

Purpose
-------
- Accept Excel / CSV / GeoJSON uploads from the contractors (no more manual GitHub commits)
- Parse Excel automatically and convert to CSV
- Store data in MongoDB (versioned) and on disk
- Serve the data back to the static frontend (hub.html, mappa.html, milestone.html, ...)

Deployment target: Render (web service) + MongoDB Atlas.
Frontend stays on GitHub Pages.
"""
from __future__ import annotations

import io
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import pandas as pd
from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

# Where uploaded files are persisted on disk.
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT_DIR.parent)).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# CORS — allow GitHub Pages + local development by default
_default_origins = (
    "https://enri-rds.github.io,"
    "http://localhost:3000,"
    "http://localhost:5500,"
    "http://127.0.0.1:5500"
)
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("ALLOWED_ORIGINS", _default_origins).split(",") if o.strip()
]

# Optional upload protection (set UPLOAD_TOKEN in production)
UPLOAD_TOKEN = os.environ.get("UPLOAD_TOKEN", "").strip()

# Allowed file extensions and max size
ALLOWED_EXT = {".csv", ".xlsx", ".xls", ".geojson", ".json"}
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "25"))

# ─────────────────────────────────────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────────────────────────────────────
client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]
uploads_col = db["uploads"]
datasets_col = db["datasets"]  # latest version of each named dataset


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────
def _objid_to_str(v: Any) -> str:
    return str(v) if isinstance(v, ObjectId) else v


PyObjectId = Annotated[str, BeforeValidator(_objid_to_str)]


class UploadRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)
    id: PyObjectId | None = Field(default=None, alias="_id")
    filename: str
    original_name: str
    size: int
    content_type: str
    project: str = "main"  # "main" | "M" | "pm"
    rows: int | None = None
    uploaded_at: str


# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="ENRI Dashboard API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,  # bearer token in header, not cookies
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-./]+$")


def _safe_relpath(name: str) -> Path:
    """Return a safe relative path under DATA_DIR."""
    name = name.replace("\\", "/").lstrip("/")
    if ".." in name.split("/") or not _SAFE_NAME_RE.match(name):
        raise HTTPException(400, "Invalid filename")
    return Path(name)


def _check_token(token: str | None) -> None:
    if UPLOAD_TOKEN and token != UPLOAD_TOKEN:
        raise HTTPException(401, "Invalid or missing upload token")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/")
async def root():
    return {
        "service": "enri-dashboard-api",
        "status": "ok",
        "time": _now_iso(),
        "version": "1.0.0",
    }


@app.get("/api/health")
async def health():
    try:
        await db.command("ping")
        mongo_ok = True
    except Exception:
        mongo_ok = False
    return {"ok": True, "mongo": mongo_ok, "time": _now_iso()}


@app.get("/api/files")
async def list_files():
    """List all CSV / GeoJSON files currently available on disk."""
    out = []
    for p in sorted(DATA_DIR.rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in {".csv", ".geojson", ".json"}:
            continue
        rel = p.relative_to(DATA_DIR).as_posix()
        out.append({
            "name": rel,
            "size": p.stat().st_size,
            "type": ext[1:],
            "modified": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
        })
    return {"files": out, "count": len(out)}


@app.get("/api/data/{filename:path}")
async def get_data_file(filename: str):
    """Serve a CSV / GeoJSON file. Used by the static frontend as a drop-in
    replacement for direct file URLs on GitHub Pages."""
    rel = _safe_relpath(filename)
    path = DATA_DIR / rel
    if not path.exists() or not path.is_file():
        raise HTTPException(404, f"File not found: {rel}")
    media = "application/geo+json" if path.suffix.lower() == ".geojson" else None
    return FileResponse(path, media_type=media, filename=path.name)


@app.get("/api/data-text/{filename:path}", response_class=PlainTextResponse)
async def get_data_text(filename: str):
    """Serve raw text (CSV). Useful for fetch() consumers that want the body."""
    rel = _safe_relpath(filename)
    path = DATA_DIR / rel
    if not path.exists():
        raise HTTPException(404, f"File not found: {rel}")
    return path.read_text(encoding="utf-8", errors="replace")


@app.get("/api/uploads")
async def list_uploads(limit: int = 50, project: str | None = None):
    q: dict = {}
    if project:
        q["project"] = project
    cur = uploads_col.find(q).sort("uploaded_at", -1).limit(min(limit, 200))
    items = []
    async for d in cur:
        d["_id"] = str(d["_id"])
        items.append(d)
    return {"uploads": items, "count": len(items)}


@app.delete("/api/uploads/{upload_id}")
async def delete_upload(upload_id: str, x_upload_token: str | None = None):
    _check_token(x_upload_token)
    try:
        oid = ObjectId(upload_id)
    except Exception:
        raise HTTPException(400, "Invalid id")
    doc = await uploads_col.find_one_and_delete({"_id": oid})
    if not doc:
        raise HTTPException(404, "Upload not found")
    return {"deleted": str(oid)}


@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    target: str = Form(""),
    project: str = Form("main"),
    convert_to_csv: bool = Form(True),
    x_upload_token: Annotated[str | None, Form(alias="token")] = None,
):
    """
    Upload Excel / CSV / GeoJSON.
    """
    _check_token(x_upload_token)

    raw = await file.read()
    if len(raw) == 0:
        raise HTTPException(400, "Empty file")
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File too large (>{MAX_UPLOAD_MB} MB)")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Unsupported file type {ext!r}. Allowed: {sorted(ALLOWED_EXT)}")

    rows = None
    out_name = target.strip() or (file.filename or "uploaded")
    out_bytes = raw

    # Excel → CSV conversion
    if ext in {".xlsx", ".xls"} and convert_to_csv:
        try:
            df = pd.read_excel(io.BytesIO(raw))
        except Exception as e:
            raise HTTPException(400, f"Cannot parse Excel: {e}")
        rows = int(len(df))
        buf = io.StringIO()
        df.to_csv(buf, index=False, sep=";")
        out_bytes = buf.getvalue().encode("utf-8")
        if not target:
            out_name = Path(file.filename).stem + ".csv"

    # CSV → count rows (best effort)
    if out_name.lower().endswith(".csv") and rows is None:
        try:
            rows = max(0, out_bytes.decode("utf-8", errors["replace"]).count("\n") - 1)
        except Exception:
            rows = None

    # Persist on disk
    rel = _safe_relpath(out_name)
    save_path = DATA_DIR / rel
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(out_bytes)

    # Record in Mongo
    record = {
        "filename": rel.as_posix(),
        "original_name": file.filename,
        "size": len(out_bytes),
        "content_type": file.content_type or "",
        "project": project or "main",
        "rows": rows,
        "uploaded_at": _now_iso(),
    }
    res = await uploads_col.insert_one(record)

    # Upsert the "latest" dataset pointer
    await datasets_col.update_one(
        {"name": rel.as_posix()},
        {
            "$set": {
                "name": rel.
