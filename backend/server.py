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

import asyncio
import base64
import io
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import httpx
import pandas as pd
from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Header, Query
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
assignments_col = db["assignments"]            # impresa nome -> {lotti: [...]}
pending_col = db["pending_updates"]            # submissions from imprese pending admin review
gridfs = AsyncIOMotorGridFSBucket(db, bucket_name="files")

# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="ENRI Dashboard API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
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
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    """Soft-delete a single upload version + remove its GridFS blob."""
    _check_token(x_upload_token or token_q)
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
    # Se è Master.csv, sincronizza GitHub con la versione ora corrente
    if doc.get("filename") == MASTER_FILENAME:
        asyncio.create_task(_push_current_master_to_github())
    return {"deleted": str(oid), "filename": doc["filename"]}


@app.delete("/api/files/{filename:path}")
async def delete_file(
    filename: str,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    """Soft-delete ALL upload versions of `filename`. After this call, if
    a disk seed exists, it becomes the served version again; otherwise the
    file returns 404."""
    _check_token(x_upload_token or token_q)
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
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    """Rename a file in MongoDB. Disk seed (if any) keeps its original name
    but it's shadowed by the renamed Mongo entry."""
    _check_token(x_upload_token or token_q)
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
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    """Make a past (soft-deleted) version current again, by clearing
    `deleted_at` on it. NB: doesn't recover GridFS bytes if they were
    already purged. If gridfs_id is null, this fails."""
    _check_token(x_upload_token or token_q)
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
    # Se è Master.csv, sincronizza GitHub con la versione ripristinata
    if doc.get("filename") == MASTER_FILENAME:
        asyncio.create_task(_push_current_master_to_github())
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


# ═════════════════════════════════════════════════════════════════════════════
# IMPRESE (contractor) — assignments + pending updates workflow
# ─────────────────────────────────────────────────────────────────────────────
# An "impresa" user logs in via the existing Google Apps Script flow (hub.html).
# We keep a server-side mapping nome → lotti[] in `assignments`. When the user
# submits updates / new rows, they go in `pending_updates`; the admin approves
# and the change is applied to Master.csv (a new GridFS version is created).
# ═════════════════════════════════════════════════════════════════════════════

MASTER_FILENAME = "Master.csv"
ROW_KEY_COLS = ("TRATTA_ID", "ENTE", "TIPO_PERMESSO")  # natural key for an update

# Separatore rilevato dal file effettivo — viene impostato in _read_master_csv()
_detected_sep: str = ";"

def _detect_sep(raw: bytes, encoding: str = "utf-8") -> str:
    """Auto-detect CSV separator: tab, semicolon or comma."""
    try:
        sample = raw[:4096].decode(encoding, errors="replace")
        first_line = sample.split("\n")[0]
        counts = {"\t": first_line.count("\t"), ";": first_line.count(";"), ",": first_line.count(",")}
        return max(counts, key=counts.get)
    except Exception:
        return ";"

async def _read_master_csv() -> "pd.DataFrame":
    """Read the current authoritative Master.csv (Mongo first, then disk seed)."""
    global _detected_sep
    cur = await _current_upload(MASTER_FILENAME)
    if cur:
        raw = await _read_gridfs(cur["gridfs_id"])
    else:
        path = DATA_DIR / MASTER_FILENAME
        if not path.exists():
            raise HTTPException(404, "Master.csv not found")
        raw = path.read_bytes()
    # Auto-rileva separatore dal contenuto reale del file
    _detected_sep = _detect_sep(raw)
    # Master.csv may be UTF-8 or Latin-1/CP1252 depending on the Excel export
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(raw), sep=_detected_sep, dtype=str, keep_default_na=False, encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    # Last resort: replace bad bytes
    return pd.read_csv(io.BytesIO(raw), sep=_detected_sep, dtype=str, keep_default_na=False, encoding="utf-8", encoding_errors="replace")


GITHUB_REPO   = os.environ.get("GITHUB_REPO", "ENRI-RDS/dashboard")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_CSV_PATH = os.environ.get("GITHUB_CSV_PATH", "Master.csv")

async def _push_current_master_to_github() -> None:
    """Legge la versione corrente di Master.csv da MongoDB e la pusha su GitHub."""
    try:
        df = await _read_master_csv()
        github_buf = io.StringIO()
        df.to_csv(github_buf, index=False, sep="\t")
        github_data = github_buf.getvalue().encode("utf-8")
        await _push_to_github(github_data)
    except Exception as e:
        print(f"[GitHub] _push_current_master: {e}")


async def _push_to_github(csv_bytes: bytes) -> None:
    """Aggiorna Master.csv su GitHub via API dopo ogni approvazione."""
    print(f"[GitHub] push avviato — {len(csv_bytes)} bytes, repo={GITHUB_REPO}, path={GITHUB_CSV_PATH}, branch={GITHUB_BRANCH}")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("[GitHub] GITHUB_TOKEN non impostato — skip push")
        return
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_CSV_PATH}"
    try:
      async with httpx.AsyncClient(timeout=20) as client:
        # 1. Recupera il SHA del file attuale (obbligatorio per aggiornarlo)
        r = await client.get(url, headers=headers, params={"ref": GITHUB_BRANCH})
        if r.status_code not in (200, 404):
            print(f"[GitHub] GET {url} → {r.status_code}: {r.text[:200]}")
            return
        sha = r.json().get("sha") if r.status_code == 200 else None
        # 2. Commit del file aggiornato
        payload: dict = {
            "message": "Auto-update Master.csv via approvazione admin [skip ci]",
            "content": base64.b64encode(csv_bytes).decode(),
            "branch": GITHUB_BRANCH,
        }
        if sha:
            payload["sha"] = sha
        resp = await client.put(url, headers=headers, json=payload)
        if resp.status_code in (200, 201):
            print(f"[GitHub] Master.csv aggiornato sul branch {GITHUB_BRANCH}")
        else:
            print(f"[GitHub] Errore push: {resp.status_code} {resp.text[:300]}")
    except Exception as e:
        print(f"[GitHub] Eccezione: {type(e).__name__}: {e}")


async def _write_master_csv(df: "pd.DataFrame", note: str) -> str:
    """Persist a new Master.csv version in GridFS and return the new upload id."""
    buf = io.StringIO()
    df.to_csv(buf, index=False, sep=_detected_sep)
    data = buf.getvalue().encode("utf-8")
    # Per GitHub usiamo sempre tab (formato originale del file nel repo)
    github_buf = io.StringIO()
    df.to_csv(github_buf, index=False, sep="\t")
    github_data = github_buf.getvalue().encode("utf-8")
    gid = await gridfs.upload_from_stream(
        MASTER_FILENAME,
        io.BytesIO(data),
        metadata={"project": "main", "uploaded_at": _now_iso(), "source": "impresa", "note": note},
    )
    record = {
        "filename": MASTER_FILENAME,
        "original_name": MASTER_FILENAME,
        "size": len(data),
        "content_type": "text/csv",
        "project": "main",
        "rows": max(0, len(df)),
        "uploaded_at": _now_iso(),
        "gridfs_id": gid,
        "deleted_at": None,
        "source": "impresa",
        "note": note,
    }
    res = await uploads_col.insert_one(record)
    # Aggiorna Master.csv anche su GitHub (fire-and-forget, non blocca la risposta)
    asyncio.create_task(_push_to_github(github_data))
    return str(res.inserted_id)


def _serialize_assignment(d: dict) -> dict:
    out = dict(d)
    out["_id"] = str(out["_id"])
    return out


# ───────── Imprese (no admin token required, identified by their `nome`) ────

@app.get("/api/imprese/me")
async def impresa_me(nome: str):
    """Returns the impresa's profile if they are assigned, else 404."""
    doc = await assignments_col.find_one({"nome": nome})
    if not doc:
        raise HTTPException(404, "Impresa non assegnata")
    return {"nome": doc["nome"], "lotti": doc.get("lotti", []), "active": bool(doc.get("active", True))}


@app.get("/api/imprese/pratiche")
async def impresa_pratiche(nome: str):
    """Returns Master.csv rows whose Source.Name matches one of the user's lotti."""
    doc = await assignments_col.find_one({"nome": nome})
    if not doc or not doc.get("active", True):
        raise HTTPException(404, "Impresa non autorizzata")
    lotti = set(doc.get("lotti", []))
    df = await _read_master_csv()
    if "Source.Name" not in df.columns:
        return {"pratiche": [], "lotti": list(lotti), "total": 0}
    mask = df["Source.Name"].apply(lambda x: any(lot and lot in str(x) for lot in lotti)) if lotti else False
    sub = df[mask] if hasattr(mask, "__iter__") else df.iloc[0:0]
    pratiche = sub.fillna("").to_dict(orient="records")
    return {"pratiche": pratiche, "lotti": sorted(lotti), "total": len(pratiche)}


@app.post("/api/imprese/submit")
async def impresa_submit(payload: dict):
    """Body: {nome, type: 'update'|'new', changes: [...]}.
    For 'update': each change has {tratta_id, ente, tipo_permesso, fields:{col:val}}
    For 'new': each change is a full row dict.
    Goes into pending_updates with status='pending'."""
    nome = (payload or {}).get("nome", "").strip()
    typ = (payload or {}).get("type", "").strip()
    changes = (payload or {}).get("changes") or []
    if not nome:
        raise HTTPException(400, "Missing 'nome'")
    if typ not in {"update", "new"}:
        raise HTTPException(400, "type must be 'update' or 'new'")
    if not isinstance(changes, list) or not changes:
        raise HTTPException(400, "Empty 'changes' array")
    doc = await assignments_col.find_one({"nome": nome})
    if not doc or not doc.get("active", True):
        raise HTTPException(403, "Impresa non autorizzata")

    record = {
        "nome": nome,
        "type": typ,
        "changes": changes,
        "status": "pending",
        "submitted_at": _now_iso(),
        "reviewed_at": None,
        "reviewed_by": None,
        "applied_upload_id": None,
        "note": (payload or {}).get("note", ""),
    }
    res = await pending_col.insert_one(record)
    return {"ok": True, "id": str(res.inserted_id), "count": len(changes)}


@app.get("/api/imprese/my-submissions")
async def my_submissions(nome: str, limit: int = 50):
    cur = pending_col.find({"nome": nome}).sort("submitted_at", -1).limit(min(limit, 200))
    items = []
    async for d in cur:
        d["_id"] = str(d["_id"])
        items.append(d)
    return {"submissions": items, "count": len(items)}


# ───────── Admin: assignments management ────────────────────────────────────

@app.get("/api/admin/assignments")
async def list_assignments(
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    _check_token(x_upload_token or token_q)
    cur = assignments_col.find({}).sort("nome", 1)
    items = [_serialize_assignment(d) async for d in cur]
    return {"assignments": items, "count": len(items)}


@app.put("/api/admin/assignments/{nome}")
async def upsert_assignment(
    nome: str,
    payload: dict,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    _check_token(x_upload_token or token_q)
    nome = nome.strip()
    lotti = (payload or {}).get("lotti", [])
    active = bool((payload or {}).get("active", True))
    if not nome:
        raise HTTPException(400, "nome required")
    if not isinstance(lotti, list):
        raise HTTPException(400, "lotti must be a list")
    doc = {"nome": nome, "lotti": [str(x) for x in lotti], "active": active, "updated_at": _now_iso()}
    await assignments_col.update_one({"nome": nome}, {"$set": doc, "$setOnInsert": {"created_at": _now_iso()}}, upsert=True)
    out = await assignments_col.find_one({"nome": nome})
    return _serialize_assignment(out)


@app.delete("/api/admin/assignments/{nome}")
async def delete_assignment(
    nome: str,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    _check_token(x_upload_token or token_q)
    res = await assignments_col.delete_one({"nome": nome})
    return {"deleted": res.deleted_count}


# ───────── Admin: pending updates approval queue ────────────────────────────

@app.get("/api/admin/pending-updates")
async def list_pending(
    status: str = "pending",
    limit: int = 100,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    _check_token(x_upload_token or token_q)
    q: dict = {}
    if status and status != "all":
        q["status"] = status
    cur = pending_col.find(q).sort("submitted_at", -1).limit(min(limit, 500))
    items = []
    async for d in cur:
        d["_id"] = str(d["_id"])
        items.append(d)
    return {"submissions": items, "count": len(items)}


def _apply_changes_to_df(df, submission: dict) -> tuple:
    """Returns (new_df, summary). Raises HTTPException on errors."""
    typ = submission["type"]
    changes = submission["changes"]
    summary = {"updated": 0, "added": 0, "not_found": 0}
    if typ == "update":
        for ch in changes:
            tratta = (ch.get("tratta_id") or "").strip()
            ente   = (ch.get("ente") or "").strip()
            tipo   = (ch.get("tipo_permesso") or "").strip()
            fields = ch.get("fields") or {}
            if not tratta:
                continue
            mask = df["TRATTA_ID"].astype(str).str.strip() == tratta
            if ente:
                mask = mask & (df["ENTE"].astype(str).str.strip() == ente)
            if tipo:
                mask = mask & (df["TIPO_PERMESSO"].astype(str).str.strip() == tipo)
            # Se la submission include anche PRATICA, usa come discriminante
            # per distinguere pratiche diverse sulla stessa tratta+ente+tipo
            pratica_key = str(fields.get("PRATICA") or ch.get("pratica") or "").strip()
            if not pratica_key and "PRATICA" in df.columns:
                # Prova a ricavarlo dalla submission (campo originale della riga)
                pratica_key = str(ch.get("original_pratica") or "").strip()
            if pratica_key and "PRATICA" in df.columns:
                mask_p = mask & (df["PRATICA"].astype(str).str.strip() == pratica_key)
                idx_p  = df.index[mask_p].tolist()
                if idx_p:   # usa il filtro per pratica solo se trova qualcosa
                    idx = idx_p
                else:
                    idx = df.index[mask].tolist()
            else:
                idx = df.index[mask].tolist()
            if not idx:
                summary["not_found"] += 1
                continue
            # Auto-set DATA_ULTIMA_MODIFICA se non fornita
            if "DATA_ULTIMA_MODIFICA" not in fields and "STATO_PERMESSO" in fields:
                fields["DATA_ULTIMA_MODIFICA"] = datetime.now(timezone.utc).strftime("%d/%m/%Y")
            # Copia l'ultima riga esistente e inserisce la nuova SUBITO DOPO
            # in modo da mantenere le righe dello stesso iter vicine
            last_idx = idx[-1]
            last_row = df.loc[last_idx].copy()
            for col, val in fields.items():
                if col in df.columns:
                    last_row[col] = str(val)
            new_row_df = pd.DataFrame([last_row])
            # Dividi il DataFrame prima e dopo il punto di inserimento
            top    = df.iloc[:last_idx + 1]
            bottom = df.iloc[last_idx + 1:]
            df = pd.concat([top, new_row_df, bottom], ignore_index=True)
            summary["updated"] += 1
    elif typ == "new":
        for row in changes:
            if not isinstance(row, dict):
                continue
            new_row = {c: str(row.get(c, "")) for c in df.columns}
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            summary["added"] += 1
    return df, summary


@app.post("/api/admin/pending-updates/{sub_id}/approve")
async def approve_pending(
    sub_id: str,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    _check_token(x_upload_token or token_q)
    try:
        oid = ObjectId(sub_id)
    except Exception:
        raise HTTPException(400, "Invalid id")
    sub = await pending_col.find_one({"_id": oid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    if sub.get("status") != "pending":
        raise HTTPException(409, f"Already {sub.get('status')}")
    df = await _read_master_csv()
    new_df, summary = _apply_changes_to_df(df, sub)
    note = f"Submission {sub_id} from {sub['nome']} ({sub['type']})"
    upload_id = await _write_master_csv(new_df, note=note)
    await pending_col.update_one(
        {"_id": oid},
        {"$set": {"status": "approved", "reviewed_at": _now_iso(), "applied_upload_id": upload_id, "summary": summary}},
    )
    return {"ok": True, "summary": summary, "new_upload_id": upload_id}


@app.post("/api/admin/pending-updates/{sub_id}/reject")
async def reject_pending(
    sub_id: str,
    payload: dict | None = None,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    _check_token(x_upload_token or token_q)
    try:
        oid = ObjectId(sub_id)
    except Exception:
        raise HTTPException(400, "Invalid id")
    note = ((payload or {}).get("note") or "").strip()
    res = await pending_col.update_one(
        {"_id": oid, "status": "pending"},
        {"$set": {"status": "rejected", "reviewed_at": _now_iso(), "reviewed_note": note}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Submission not found or not pending")
    return {"ok": True}
