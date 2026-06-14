"""
ENRI Dashboard — Backend API
============================
FastAPI service for the ENRI-RDS/dashboard project.

Storage model
-------------
MongoDB is the AUTHORITATIVE storage for uploaded files (via GridFS).
Files committed in the git repo are used as the INITIAL SEED ONLY:
when no upload exists for a given filename, the API falls back to
the file on disk (the version pushed on GitHub Pages).

This avoids data loss on Render's ephemeral filesystem AND keeps the
static GitHub Pages fallback fully functional.
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
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response, FileResponse
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorGridFSBucket

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

# DATA_DIR is the directory containing the git-committed seed files
# (Master.csv, QGIS.geojson, etc.). It's READ-ONLY in the new model.
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT_DIR.parent)).resolve()

_default_origins = (
    "https://enri-rds.github.io,"
    "http://localhost:3000,"
    "http://localhost:5500,"
    "http://127.0.0.1:5500"
)
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("ALLOWED_ORIGINS", _default_origins).split(",") if o.strip()
]

UPLOAD_TOKEN = os.environ.get("UPLOAD_TOKEN", "").strip()
ALLOWED_EXT = {".csv", ".xlsx", ".xls", ".geojson", ".json"}
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "25"))

# ─────────────────────────────────────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────────────────────────────────────
client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]
uploads_col = db["uploads"]
gridfs = AsyncIOMotorGridFSBucket(db, bucket_name="files")

# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="ENRI Dashboard API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-./]+$")
_EXCLUDE_DIRS = {"backend", "frontend", "node_modules", "__pycache__", ".git", "memory", "js"}


def _safe_relpath(name: str) -> str:
    name = name.replace("\\", "/").lstrip("/")
    if ".." in name.split("/") or not _SAFE_NAME_RE.match(name):
        raise HTTPException(400, "Invalid filename")
    return name


def _check_token(token: str | None) -> None:
    if UPLOAD_TOKEN and token != UPLOAD_TOKEN:
        raise HTTPException(401, "Invalid or missing upload token")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _media_type(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower()
    if ext == "geojson":
        return "application/geo+json"
    if ext == "json":
        return "application/json"
    if ext == "csv":
        return "text/csv; charset=utf-8"
    return "application/octet-stream"


def _serialize(doc: dict) -> dict:
    d = dict(doc)
    if "_id" in d:
        d["_id"] = str(d["_id"])
    if "gridfs_id" in d and isinstance(d["gridfs_id"], ObjectId):
        d["gridfs_id"] = str(d["gridfs_id"])
    return d


async def _current_upload(filename: str) -> dict | None:
    """Most recent non-deleted upload for a given filename."""
    return await uploads_col.find_one(
        {"filename": filename, "deleted_at": None},
        sort=[("uploaded_at", -1)],
    )


async def _read_gridfs(gridfs_id: ObjectId) -> bytes:
    stream = await gridfs.open_download_stream(gridfs_id)
    try:
        return await stream.read()
    finally:
        # motor's GridOut.close is synchronous
        close = getattr(stream, "close", None)
        if callable(close):
            res = close()
            if hasattr(res, "__await__"):
                await res


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/")
async def root():
    return {
        "service": "enri-dashboard-api",
        "status": "ok",
        "time": _now_iso(),
        "version": "2.0.0",
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
    """Union of:
    - files with current (non-deleted) GridFS uploads
    - seed files on disk (excluding system dirs) that aren't shadowed by a
      Mongo upload
    """
    out: dict[str, dict] = {}

    # 1) Disk seed
    if DATA_DIR.exists():
        for p in sorted(DATA_DIR.rglob("*")):
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext not in {".csv", ".geojson", ".json"}:
                continue
            try:
                rel = p.relative_to(DATA_DIR).as_posix()
            except ValueError:
                continue
            top = rel.split("/", 1)[0]
            if top in _EXCLUDE_DIRS or top.startswith("."):
                continue
            out[rel] = {
                "name": rel,
                "size": p.stat().st_size,
                "type": ext[1:],
                "source": "disk",
                "modified": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
                "versions": 0,
            }

    # 2) Mongo current versions (override disk)
    pipeline = [
        {"$match": {"deleted_at": None}},
        {"$sort": {"uploaded_at": -1}},
        {"$group": {
            "_id": "$filename",
            "size": {"$first": "$size"},
            "uploaded_at": {"$first": "$uploaded_at"},
            "project": {"$first": "$project"},
            "rows": {"$first": "$rows"},
        }},
    ]
    versions_pipeline = [
        {"$match": {"deleted_at": None}},
        {"$group": {"_id": "$filename", "count": {"$sum": 1}}},
    ]
    version_counts: dict[str, int] = {}
    async for d in uploads_col.aggregate(versions_pipeline):
        version_counts[d["_id"]] = d["count"]

    async for d in uploads_col.aggregate(pipeline):
        name = d["_id"]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        out[name] = {
            "name": name,
            "size": d["size"],
            "type": ext,
            "source": "mongo",
            "modified": d["uploaded_at"],
            "project": d.get("project", "main"),
            "rows": d.get("rows"),
            "versions": version_counts.get(name, 1),
        }

    files = sorted(out.values(), key=lambda x: x["name"])
    return {"files": files, "count": len(files)}


@app.get("/api/data/{filename:path}")
async def get_data_file(filename: str):
    rel = _safe_relpath(filename)
    cur = await _current_upload(rel)
    if cur:
        data = await _read_gridfs(cur["gridfs_id"])
        return Response(content=data, media_type=_media_type(rel))
    # Fallback to disk seed (git-committed file)
    path = DATA_DIR / rel
    if path.exists() and path.is_file():
        return FileResponse(path, media_type=_media_type(rel), filename=path.name)
    raise HTTPException(404, f"File not found: {rel}")


@app.get("/api/data-text/{filename:path}", response_class=PlainTextResponse)
async def get_data_text(filename: str):
    rel = _safe_relpath(filename)
    cur = await _current_upload(rel)
    if cur:
        data = await _read_gridfs(cur["gridfs_id"])
        return data.decode("utf-8", errors="replace")
    path = DATA_DIR / rel
    if path.exists() and path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")
    raise HTTPException(404, f"File not found: {rel}")


@app.get("/api/preview/{filename:path}")
async def preview_file(filename: str, max_bytes: int = 8192):
    """Returns a short text preview of a file (first max_bytes)."""
    rel = _safe_relpath(filename)
    max_bytes = max(256, min(max_bytes, 65536))
    cur = await _current_upload(rel)
    if cur:
        data = (await _read_gridfs(cur["gridfs_id"]))[:max_bytes]
        source = "mongo"
        size = cur["size"]
    else:
        path = DATA_DIR / rel
        if not (path.exists() and path.is_file()):
            raise HTTPException(404, f"File not found: {rel}")
        size = path.stat().st_size
        with path.open("rb") as f:
            data = f.read(max_bytes)
        source = "disk"
    return {
        "filename": rel,
        "source": source,
        "size": size,
        "truncated": size > max_bytes,
        "content": data.decode("utf-8", errors="replace"),
    }


@app.get("/api/uploads")
async def list_uploads(
    limit: int = 50,
    project: str | None = None,
    filename: str | None = None,
    include_deleted: bool = False,
):
    q: dict = {}
    if project:
        q["project"] = project
    if filename:
        q["filename"] = filename
    if not include_deleted:
        q["deleted_at"] = None
    cur = uploads_col.find(q).sort("uploaded_at", -1).limit(min(limit, 500))
    items = [_serialize(d) async for d in cur]
    return {"uploads": items, "count": len(items)}


@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    target: str = Form(""),
    project: str = Form("main"),
    convert_to_csv: bool = Form(True),
    x_upload_token: Annotated[str | None, Form(alias="token")] = None,
    header_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
):
    _check_token(x_upload_token or header_token)

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

    if out_name.lower().endswith(".csv") and rows is None:
        try:
            rows = max(0, out_bytes.decode("utf-8", errors="replace").count("\n") - 1)
        except Exception:
            rows = None

    rel = _safe_relpath(out_name)

    # Store content in GridFS
    gridfs_id = await gridfs.upload_from_stream(
        rel,
        io.BytesIO(out_bytes),
        metadata={"project": project or "main", "uploaded_at": _now_iso()},
    )

    record = {
        "filename": rel,
        "original_name": file.filename,
        "size": len(out_bytes),
        "content_type": file.content_type or "",
        "project": project or "main",
        "rows": rows,
        "uploaded_at": _now_iso(),
        "gridfs_id": gridfs_id,
        "deleted_at": None,
    }
    res = await uploads_col.insert_one(record)

    return JSONResponse({
        "ok": True,
        "id": str(res.inserted_id),
        "filename": rel,
        "size": len(out_bytes),
        "rows": rows,
        "converted_from_excel": ext in {".xlsx", ".xls"} and convert_to_csv,
    })


@app.delete("/api/uploads/{upload_id}")
async def delete_upload(
    upload_id: str,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
):
    """Soft-delete a single upload version + remove its GridFS blob."""
    _check_token(x_upload_token)
    try:
        oid = ObjectId(upload_id)
    except Exception:
        raise HTTPException(400, "Invalid id")
    doc = await uploads_col.find_one({"_id": oid})
    if not doc:
        raise HTTPException(404, "Upload not found")
    if doc.get("gridfs_id"):
        try:
            await gridfs.delete(doc["gridfs_id"])
        except Exception:
            pass
    await uploads_col.update_one(
        {"_id": oid},
        {"$set": {"deleted_at": _now_iso(), "gridfs_id": None}},
    )
    return {"deleted": str(oid), "filename": doc["filename"]}


@app.delete("/api/files/{filename:path}")
async def delete_file(
    filename: str,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
):
    """Soft-delete ALL upload versions of `filename`. After this call, if
    a disk seed exists, it becomes the served version again; otherwise the
    file returns 404."""
    _check_token(x_upload_token)
    rel = _safe_relpath(filename)
    # Collect gridfs ids to remove
    ids: list[ObjectId] = []
    async for d in uploads_col.find({"filename": rel, "deleted_at": None}):
        if d.get("gridfs_id"):
            ids.append(d["gridfs_id"])
    for gid in ids:
        try:
            await gridfs.delete(gid)
        except Exception:
            pass
    res = await uploads_col.update_many(
        {"filename": rel, "deleted_at": None},
        {"$set": {"deleted_at": _now_iso(), "gridfs_id": None}},
    )
    return {"filename": rel, "deleted_versions": res.modified_count}


@app.patch("/api/files/{old_name:path}")
async def rename_file(
    old_name: str,
    payload: dict,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
):
    """Rename a file in MongoDB. Disk seed (if any) keeps its original name
    but it's shadowed by the renamed Mongo entry."""
    _check_token(x_upload_token)
    new_name = (payload or {}).get("new_name", "")
    if not new_name:
        raise HTTPException(400, "Missing 'new_name'")
    old_rel = _safe_relpath(old_name)
    new_rel = _safe_relpath(new_name)
    if old_rel == new_rel:
        return {"ok": True, "filename": new_rel, "updated": 0}
    # Make sure target name isn't already taken by an active upload
    clash = await uploads_col.find_one({"filename": new_rel, "deleted_at": None})
    if clash:
        raise HTTPException(409, f"Target name already in use: {new_rel}")
    res = await uploads_col.update_many(
        {"filename": old_rel, "deleted_at": None},
        {"$set": {"filename": new_rel}},
    )
    return {"ok": True, "from": old_rel, "to": new_rel, "updated": res.modified_count}


@app.post("/api/uploads/{upload_id}/restore")
async def restore_upload(
    upload_id: str,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
):
    """Make a past (soft-deleted) version current again, by clearing
    `deleted_at` on it. NB: doesn't recover GridFS bytes if they were
    already purged. If gridfs_id is null, this fails."""
    _check_token(x_upload_token)
    try:
        oid = ObjectId(upload_id)
    except Exception:
        raise HTTPException(400, "Invalid id")
    doc = await uploads_col.find_one({"_id": oid})
    if not doc:
        raise HTTPException(404, "Upload not found")
    if not doc.get("gridfs_id"):
        raise HTTPException(410, "Underlying content was purged; cannot restore")
    await uploads_col.update_one({"_id": oid}, {"$set": {"deleted_at": None}})
    return {"ok": True, "restored": str(oid), "filename": doc["filename"]}


@app.on_event("startup")
async def _on_startup():
    print(f"[enri-dashboard] DATA_DIR (seed) = {DATA_DIR}")
    print(f"[enri-dashboard] DB_NAME         = {DB_NAME}")
    print(f"[enri-dashboard] CORS            = {ALLOWED_ORIGINS}")
    print(f"[enri-dashboard] UPLOAD_TOKEN    = {'set' if UPLOAD_TOKEN else 'OFF (open)'}")
    # Backfill: ensure pre-existing upload records have a deleted_at field
    await uploads_col.update_many(
        {"deleted_at": {"$exists": False}}, {"$set": {"deleted_at": None}}
    )
