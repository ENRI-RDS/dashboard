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
import hashlib
import hmac
import io
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import httpx
import pandas as pd
from bson import ObjectId
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, Header, Query
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
solleciti_col = db["solleciti"]                # registro solleciti per tratta/pratica
cantieri_col  = db["cantieri"]                  # stato cantiere per pratica di autorizzazione
sopralluoghi_col = db["sopralluoghi"]          # verbali di sopralluogo
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
_EXCLUDE_DIRS  = {"backend", "frontend", "node_modules", "__pycache__", ".git", "memory", "js", "M"}
_EXCLUDE_FILES = {"dati.csv"}          # file su disco da NON esporre nella lista admin
# "M" esclusa: cartella di file legacy (es. M/QGIS_3.geojson) non collegati alla
# dashboard — restano nel repo per storico ma non devono comparire in admin.html


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
            if p.name in _EXCLUDE_FILES:
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
            "upload_source": {"$first": "$source"},
            "note": {"$first": "$note"},
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
            "upload_source": d.get("upload_source") or "admin",  # impresa | derived | admin (upload manuale)
            "note": d.get("note") or "",
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

    # Se è Master.csv, rigenera anche i file derivati (Riepilogo_progettazione.csv,
    # QGIS.geojson) e sincronizza GitHub — stesso comportamento di approve/delete/restore,
    # altrimenti un upload manuale lascia mappa e barre ferme alla versione precedente.
    if rel == MASTER_FILENAME:
        asyncio.create_task(_push_current_master_to_github())

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
    # Se è Master.csv, sincronizza GitHub e rigenera i file derivati con la
    # versione ora corrente (il seed da disco, se non resta nessun'altra versione)
    if rel == MASTER_FILENAME:
        asyncio.create_task(_push_current_master_to_github())
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
    # Sync cantieri: crea automaticamente cantieri non_avviato per tratte LAVORABILE=SI
    try:
        created = await _sync_cantieri()
        if created:
            asyncio.create_task(_push_cantieri_to_github())
    except Exception as e:
        print(f"[startup] _sync_cantieri: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# IMPRESE (contractor) — assignments + pending updates workflow
# ─────────────────────────────────────────────────────────────────────────────
# An "impresa" user logs in via the existing Google Apps Script flow (hub.html).
# We keep a server-side mapping nome → lotti[] in `assignments`. When the user
# submits updates / new rows, they go in `pending_updates`; the admin approves
# and the change is applied to Master.csv (a new GridFS version is created).
# ═════════════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
# Sessione firmata — risolve il problema per cui chiunque potesse fare
# `localStorage.setItem('_enri_user', 'Nome Impresa')` e impersonare quel
# nome senza conoscere il codice di accesso (il codice era verificato SOLO
# da Google Apps Script al login, mai dal backend sulle chiamate successive).
#
# Flusso corretto:
#   1. hub.html invia nome+codice a /api/auth/login (non più direttamente ad
#      Apps Script: il segreto APPS_SCRIPT_SECRET resta lato server)
#   2. Il backend verifica nome+codice chiamando Apps Script server-to-server
#   3. Se ok, firma un token (HMAC, non falsificabile senza SESSION_SECRET)
#      con nome+ruolo+scadenza, e lo restituisce al browser
#   4. Ogni chiamata /api/imprese/* richiede questo token nell'header
#      x-session-token; il `nome` viene SEMPRE preso dal token firmato,
#      MAI dal parametro `nome` passato dal client (che viene ignorato)
# ─────────────────────────────────────────────────────────────────────────────

SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", str(12 * 3600)))  # 12h default
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "")
APPS_SCRIPT_SECRET = os.environ.get("APPS_SCRIPT_SECRET", "")


def _sign_session(nome: str, ruolo: str) -> str:
    if not SESSION_SECRET:
        raise HTTPException(500, "SESSION_SECRET non configurato sul server")
    exp = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{nome}|{ruolo}|{exp}"
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}|{sig}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _verify_session(token: str) -> dict | None:
    if not token or not SESSION_SECRET:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        nome, ruolo, exp, sig = raw.split("|", 3)
        payload = f"{nome}|{ruolo}|{exp}"
        expected = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if int(exp) < int(time.time()):
            return None
        return {"nome": nome, "ruolo": ruolo}
    except Exception:
        return None


async def _require_session(
    x_session_token: Annotated[str | None, Header(alias="x-session-token")] = None,
) -> dict:
    """Dependency per gli endpoint /api/imprese/*: il `nome` autenticato viene
    SEMPRE letto dal token firmato, mai da un parametro passato dal client."""
    sess = _verify_session(x_session_token or "")
    if not sess:
        raise HTTPException(401, "Sessione non valida o scaduta — effettua di nuovo il login")
    return sess


@app.post("/api/auth/login")
async def auth_login(payload: dict):
    """Proxy server-to-server verso Google Apps Script: il browser non vede
    più APPS_SCRIPT_SECRET, e il codice viene verificato per davvero (non solo
    al primo login, ma e' la base per ogni chiamata successiva tramite il token)."""
    nome = (payload or {}).get("nome", "").strip()
    codice = (payload or {}).get("codice", "").strip()
    if not nome or not codice:
        raise HTTPException(400, "Nome e codice sono richiesti")
    if not APPS_SCRIPT_URL or not APPS_SCRIPT_SECRET:
        raise HTTPException(500, "Login non configurato sul server (APPS_SCRIPT_URL/SECRET mancanti)")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.post(
                APPS_SCRIPT_URL,
                content=json.dumps({"secret": APPS_SCRIPT_SECRET, "action": "login", "nome": nome, "codice": codice}),
                headers={"Content-Type": "text/plain"},
            )
    except Exception as e:
        raise HTTPException(502, f"Servizio di login non raggiungibile: {e}")

    try:
        data = r.json()
    except Exception:
        snippet = r.text[:200].replace("\n", " ")
        raise HTTPException(502, f"Risposta non valida da Apps Script (HTTP {r.status_code}): {snippet!r}")

    if not data.get("ok"):
        raise HTTPException(401, data.get("msg") or "Nome o codice non riconosciuti")

    nome_canonical = data.get("nome") or nome
    ruolo = str(data.get("ruolo") or "user").lower()
    token = _sign_session(nome_canonical, ruolo)
    return {"ok": True, "token": token, "nome": nome_canonical, "ruolo": ruolo}


@app.get("/api/auth/verify")
async def auth_verify(sess: dict = Depends(_require_session)):
    """Verifica token di sessione e restituisce nome+ruolo — usato da index.html per la rivalidazione silenziosa."""
    return {"ok": True, "nome": sess["nome"], "ruolo": sess.get("ruolo", "user")}


@app.post("/api/logs/get")
async def logs_get(payload: dict, sess: dict = Depends(_require_session)):
    """Proxy JSONBin get via Apps Script (il segreto resta server-side)."""
    bin_id = (payload or {}).get("binId", "")
    if not APPS_SCRIPT_URL or not APPS_SCRIPT_SECRET:
        raise HTTPException(500, "Log service non configurato")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.post(
                APPS_SCRIPT_URL,
                content=json.dumps({"secret": APPS_SCRIPT_SECRET, "action": "jsonbin_get", "binId": bin_id}),
                headers={"Content-Type": "text/plain"},
            )
        return r.json()
    except Exception as e:
        raise HTTPException(502, f"Log service non raggiungibile: {e}")


@app.post("/api/logs/put")
async def logs_put(payload: dict, sess: dict = Depends(_require_session)):
    """Proxy JSONBin put via Apps Script (il segreto resta server-side)."""
    bin_id = (payload or {}).get("binId", "")
    data   = (payload or {}).get("data")
    if not APPS_SCRIPT_URL or not APPS_SCRIPT_SECRET:
        raise HTTPException(500, "Log service non configurato")
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.post(
                APPS_SCRIPT_URL,
                content=json.dumps({"secret": APPS_SCRIPT_SECRET, "action": "jsonbin_put", "binId": bin_id, "data": data}),
                headers={"Content-Type": "text/plain"},
            )
        return r.json()
    except Exception as e:
        raise HTTPException(502, f"Log service non raggiungibile: {e}")


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
    # Master.csv may be UTF-8 or Latin-1/CP1252 depending on the Excel export.
    # on_bad_lines='warn' evita che UNA riga malformata (es. virgola non quotata
    # in un campo di testo libero come NOTE) faccia fallire la lettura di tutto
    # il file: la riga incriminata viene segnalata in log e scartata, il resto
    # del file resta leggibile.
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return pd.read_csv(
                io.BytesIO(raw), sep=_detected_sep, dtype=str, keep_default_na=False,
                encoding=enc, on_bad_lines="warn",
            )
        except (UnicodeDecodeError, UnicodeError):
            continue
    # Last resort: replace bad bytes
    return pd.read_csv(io.BytesIO(raw), sep=_detected_sep, dtype=str, keep_default_na=False, encoding="utf-8", encoding_errors="replace")


QGIS_FILENAME      = "QGIS.geojson"
RIEPILOGO_FILENAME = "Riepilogo_progettazione.csv"

GITHUB_REPO     = os.environ.get("GITHUB_REPO", "ENRI-RDS/dashboard")
GITHUB_BRANCH   = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_CSV_PATH = os.environ.get("GITHUB_CSV_PATH", "Master.csv")

# Mappa file dashboard -> path nel repo GitHub (override via env se servono sottocartelle)
GITHUB_PATHS: dict = {
    "Master.csv":                  GITHUB_CSV_PATH,
    "Riepilogo_progettazione.csv": os.environ.get("GITHUB_RIEPILOGO_PATH", "Riepilogo_progettazione.csv"),
    "QGIS.geojson":                os.environ.get("GITHUB_QGIS_PATH", "QGIS.geojson"),
    "solleciti.csv":               os.environ.get("GITHUB_SOLLECITI_PATH", "solleciti.csv"),
    "sopralluoghi.csv":             os.environ.get("GITHUB_SOPRALLUOGHI_PATH", "sopralluoghi.csv"),
}


async def _push_to_github(file_bytes: bytes, path: str = None, label: str = None) -> None:
    """Aggiorna un file su GitHub via API (Master.csv, QGIS.geojson, Riepilogo_progettazione.csv, ...).
    In caso di conflitto sha (409 — qualcun altro ha scritto sullo stesso file nel frattempo,
    es. una modifica manuale in parallelo) rilegge lo sha aggiornato e riprova fino a 3 volte."""
    path  = path or GITHUB_CSV_PATH
    label = label or path
    print(f"[GitHub] push avviato — {len(file_bytes)} bytes, repo={GITHUB_REPO}, path={path}, branch={GITHUB_BRANCH}")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("[GitHub] GITHUB_TOKEN non impostato — skip push")
        return
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    max_tentativi = 3
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            for tentativo in range(1, max_tentativi + 1):
                r = await client.get(url, headers=headers, params={"ref": GITHUB_BRANCH})
                if r.status_code not in (200, 404):
                    print(f"[GitHub] GET {url} → {r.status_code}: {r.text[:200]}")
                    return
                sha = r.json().get("sha") if r.status_code == 200 else None
                payload: dict = {
                    "message": f"Auto-update {label} via approvazione admin [skip ci]",
                    "content": base64.b64encode(file_bytes).decode(),
                    "branch": GITHUB_BRANCH,
                }
                if sha:
                    payload["sha"] = sha
                resp = await client.put(url, headers=headers, json=payload)
                if resp.status_code in (200, 201):
                    extra = f" (tentativo {tentativo}/{max_tentativi})" if tentativo > 1 else ""
                    print(f"[GitHub] {label} aggiornato sul branch {GITHUB_BRANCH}{extra}")
                    return
                if resp.status_code == 409 and tentativo < max_tentativi:
                    print(f"[GitHub] Conflitto sha su {label} (tentativo {tentativo}/{max_tentativi}) — rileggo e riprovo")
                    await asyncio.sleep(1)
                    continue
                print(f"[GitHub] Errore push {label}: {resp.status_code} {resp.text[:300]}")
                return
    except Exception as e:
        print(f"[GitHub] Eccezione {label}: {type(e).__name__}: {e}")



async def _push_current_master_to_github() -> None:
    """Legge la versione corrente di Master.csv da MongoDB, la pusha su GitHub
    e rigenera QGIS.geojson + Riepilogo_progettazione.csv (usata da delete/restore)."""
    try:
        df = await _read_master_csv()
        github_buf = io.StringIO()
        df.to_csv(github_buf, index=False, sep="\t")
        github_data = github_buf.getvalue().encode("utf-8")
        await _push_to_github(github_data, path=GITHUB_PATHS["Master.csv"], label="Master.csv")
        await _regenerate_derived_files(df, note="restore/delete Master.csv")
    except Exception as e:
        print(f"[GitHub] _push_current_master: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# Derivazione QGIS.geojson + Riepilogo_progettazione.csv da Master.csv
# ─────────────────────────────────────────────────────────────────────────────
# Regole verificate contro un export reale di Riepilogo_progettazione.csv:
#   - STATO_LEGENDA è sempre identico a STATO_AUTORIZZAZIONE (0 eccezioni su 692 righe)
#   - LAVORABILE = SI solo se STATO_AUTORIZZAZIONE = OTTENUTO E (se richiesto)
#     anche il/i NULLA OSTA sono OTTENUTI (idem ORDINANZA se richiesta)
#   - Quando una tratta ha PIU' nulla osta (enti diversi), si usa lo stato PIU'
#     INDIETRO (peggiore) tra l'ultimo stato registrato per ciascun ente
#   - CAMPO AWS, PROTOCOLLO_AUT, ENTE 2: provengono da un sistema esterno o non
#     hanno una regola derivabile con certezza dai dati disponibili — NON vengono
#     toccati dalla rigenerazione, il valore esistente viene preservato.
# ═════════════════════════════════════════════════════════════════════════════

_STATUS_RANK = {
    "NECESSARIA INTEGRAZIONE": 0,
    "IN REDAZIONE INTEGRAZIONE": 1,
    "IN REDAZIONE": 2,
    "IN ATTESA": 2,
    "COORDINAMENTO": 3,
    "IN FIRMA RDS": 4,
    "INVIATO": 5,
    "PROTOCOLLATO INTEGRAZIONE": 6,
    "PROTOCOLLATO": 7,
    "OTTENUTO": 8,
}

_TIPO_PREFIX = {"AUTORIZZAZIONE": "AUT", "NULLA OSTA": "NO", "ORDINANZA": "ORD"}


def _norm(s) -> str:
    return str(s or "").replace("\xa0", " ").strip().upper()


def _worst_status(statuses: list) -> str:
    clean = [str(s).strip() for s in statuses
             if s and str(s).strip() and str(s).strip().upper() != "NO COMPETENZA"]
    if not clean:
        return ""
    return min(clean, key=lambda s: _STATUS_RANK.get(s.upper(), 99))


def _latest_per_ente(rows: list, tipo: str) -> list:
    """Tra le righe di un TIPO_PERMESSO, prende l'ultima riga (cronologicamente
    piu' recente, assumendo l'ordine di inserimento) per ciascun ente distinto."""
    by_ente, order = {}, []
    for r in rows:
        if _norm(r.get("TIPO_PERMESSO")) != tipo:
            continue
        ente = _norm(r.get("ENTE"))
        if ente not in by_ente:
            order.append(ente)
        by_ente[ente] = r  # l'ultima occorrenza trovata vince
    return [by_ente[e] for e in order]


def _lotto_from_source(source_name: str) -> str:
    """'Lotto 1A.xlsx' -> '1A'."""
    s = str(source_name or "")
    s = re.sub(r"(?i)^lotto\s*", "", s).strip()
    s = re.sub(r"(?i)\.xlsx?$", "", s).strip()
    return s.upper()


def _build_pratica(rows: list) -> str:
    """'AUT/24/1A | NO/22/1A | NO/26/1A' — AUT prima, poi NO, poi ORD."""
    parts, seen = [], set()
    for tipo, pref in (("AUTORIZZAZIONE", "AUT"), ("NULLA OSTA", "NO"), ("ORDINANZA", "ORD")):
        for r in rows:
            if _norm(r.get("TIPO_PERMESSO")) != tipo:
                continue
            num = str(r.get("PRATICA") or "").strip()
            if not num:
                continue
            lotto = _lotto_from_source(r.get("Source.Name", ""))
            tok = f"{pref}/{num}/{lotto}"
            if tok not in seen:
                seen.add(tok)
                parts.append(tok)
    return " | ".join(parts)


def _compute_tratta_summary(master_df: "pd.DataFrame") -> dict:
    """Per ogni TRATTA_ID calcola: STATO_AUTORIZZAZIONE, STATO_NULLAOSTA,
    STATO_ORDINANZA, LAVORABILE, MOTIVO_NO, PRATICA, STATO_LEGENDA, ENTE."""
    if master_df is None or "TRATTA_ID" not in master_df.columns:
        return {}
    df = master_df.fillna("")
    result = {}

    for tratta_id, group in df.groupby(df["TRATTA_ID"].astype(str).str.strip()):
        tratta_id = tratta_id.strip()
        if not tratta_id:
            continue
        rows = group.to_dict(orient="records")

        # AUTORIZZAZIONE: un solo permesso per tratta -> ultima riga inserita.
        # Le pratiche chiuse con STATO_PERMESSO=NO COMPETENZA sono superate:
        # l'ente ha dichiarato di non essere competente, quindi prima o poi
        # arriva una NUOVA pratica con un ente diverso sulla stessa tratta.
        # Finché esiste un'alternativa attiva va sempre preferita a NO COMPETENZA;
        # se invece NO COMPETENZA è l'unica pratica presente, la tratta è
        # semplicemente in attesa che la nuova pratica venga aperta.
        aut_rows = [r for r in rows if _norm(r.get("TIPO_PERMESSO")) == "AUTORIZZAZIONE"]
        aut_rows_attive = [r for r in aut_rows if _norm(r.get("STATO_PERMESSO")) != "NO COMPETENZA"]
        if aut_rows_attive:
            aut_row_corrente = aut_rows_attive[-1]
            stato_aut = _norm(aut_row_corrente.get("STATO_PERMESSO"))
        elif aut_rows:
            aut_row_corrente = aut_rows[-1]
            stato_aut = "IN ATTESA"
        else:
            aut_row_corrente = None
            stato_aut = "IN ATTESA"
        ente_aut  = str(aut_row_corrente.get("ENTE", "")).strip() if aut_row_corrente else ""
        need_no   = _norm(aut_row_corrente.get("NULLA OSTA NECESSARIO")) if aut_row_corrente else "NO"
        need_ord  = _norm(aut_row_corrente.get("ORDINANZA NECESSARIA")) if aut_row_corrente else "NO"

        # NULLA OSTA / ORDINANZA: possono essercene piu' di uno (enti diversi) ->
        # prendi l'ultimo stato di ciascun ente, poi il PEGGIORE tra questi
        no_latest  = _latest_per_ente(rows, "NULLA OSTA")
        stato_no   = _worst_status([r.get("STATO_PERMESSO") for r in no_latest]) if no_latest else (
            "IN ATTESA" if need_no == "SI" else "NON NECESSARIO"
        )
        ord_latest = _latest_per_ente(rows, "ORDINANZA")
        stato_ord  = _worst_status([r.get("STATO_PERMESSO") for r in ord_latest]) if ord_latest else (
            "IN ATTESA" if need_ord == "SI" else "NON NECESSARIO"
        )

        aut_ok = stato_aut == "OTTENUTO"
        no_ok  = stato_no == "OTTENUTO"
        ord_ok = stato_ord == "OTTENUTO"

        lavorabile = aut_ok
        if need_no == "SI":
            lavorabile = lavorabile and no_ok
        if need_ord == "SI":
            lavorabile = lavorabile and ord_ok

        motivi = []
        if not aut_ok:
            motivi.append("Manca autorizz")
        if need_no == "SI" and not no_ok:
            motivi.append("Manca nulla osta")
        if need_ord == "SI" and not ord_ok:
            motivi.append("Manca ordinanza")

        result[tratta_id] = {
            "STATO_AUTORIZZAZIONE": stato_aut,
            "STATO_LEGENDA":        stato_aut,  # sempre identico, confermato sui dati reali
            "STATO_NULLAOSTA":      stato_no,
            "STATO_ORDINANZA":      stato_ord,
            "LAVORABILE":           "SI" if lavorabile else "NO",
            "MOTIVO_NO":            " | ".join(motivi),
            "PRATICA":              _build_pratica(rows),
            "PRATICA_AUT":          str(aut_row_corrente.get("PRATICA", "")).strip() if aut_row_corrente else "",
            "ENTE":                 ente_aut,
            "LUNGHEZZA":            rows[0].get("LUNGHEZZA", 0) if rows else 0,
        }
    return result


async def _read_current_geojson(filename: str):
    """Legge un GeoJSON (MongoDB se presente, altrimenti seed su disco)."""
    cur = await _current_upload(filename)
    if cur and cur.get("gridfs_id"):
        raw = await _read_gridfs(cur["gridfs_id"])
    else:
        path = DATA_DIR / filename
        if not path.exists():
            return None
        raw = path.read_bytes()
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return None


async def _store_derived_file(filename: str, data: bytes, content_type: str, note: str) -> str:
    """Salva un file derivato (rigenerato) come nuova versione in GridFS."""
    gid = await gridfs.upload_from_stream(
        filename, io.BytesIO(data),
        metadata={"project": "main", "uploaded_at": _now_iso(), "source": "derived", "note": note},
    )
    record = {
        "filename": filename, "original_name": filename, "size": len(data),
        "content_type": content_type, "project": "main", "rows": None,
        "uploaded_at": _now_iso(), "gridfs_id": gid, "deleted_at": None,
        "source": "derived", "note": note,
    }
    res = await uploads_col.insert_one(record)
    return str(res.inserted_id)


async def _regenerate_derived_files(master_df: "pd.DataFrame", note: str = "") -> dict:
    """Rigenera QGIS.geojson e Riepilogo_progettazione.csv a partire da Master.csv.

    Verificato sui file reali: Riepilogo_progettazione.csv e' ESATTAMENTE la
    tabella attributi di QGIS.geojson esportata in CSV (stesse 21 colonne,
    stesso ordine, stessi valori, stesso numero di righe — confrontato riga
    per riga su TR_0103 e sull'intero file). Per questo la patch avviene UNA
    SOLA VOLTA sulle properties di QGIS.geojson, e Riepilogo viene poi
    derivato direttamente da quello: i due file non possono piu' disallinearsi.

    Vengono aggiornati SOLO i campi calcolati da Master.csv:
    STATO_AUTORIZZAZIONE, STATO_LEGENDA, STATO_NULLAOSTA, STATO_ORDINANZA,
    LAVORABILE, MOTIVO_NO, PRATICA, ENTE.
    Tutto il resto (fid, TIPOLOGIA, PROVINCIA, COMUNE, CLUSTER, ROUTE, ENTE 2,
    LUNGHEZZA, SPAN, LOTTO, PROTOCOLLO_AUT, CAMPO AWS, geometria) resta
    esattamente come nel QGIS.geojson esistente.

    Fire-and-forget: eventuali errori vengono solo loggati.
    """
    # Ordine colonne confermato sul file reale (json.load preserva l'ordine delle key)
    RIEPILOGO_COLUMNS = [
        "fid", "TIPOLOGIA", "PROVINCIA", "COMUNE", "CLUSTER", "ROUTE", "ENTE", "ENTE 2",
        "LUNGHEZZA", "SPAN", "LOTTO", "TRATTA_ID", "MOTIVO_NO", "STATO_AUTORIZZAZIONE",
        "STATO_NULLAOSTA", "STATO_ORDINANZA", "PROTOCOLLO_AUT", "PRATICA", "LAVORABILE",
        "STATO_LEGENDA", "CAMPO AWS",
    ]

    out_ids: dict = {}
    try:
        summary = _compute_tratta_summary(master_df)
        if not summary:
            print("[Sync] Master.csv senza TRATTA_ID utilizzabili — skip rigenerazione")
            return out_ids

        geo = await _read_current_geojson(QGIS_FILENAME)
        if not geo or not isinstance(geo.get("features"), list):
            print(f"[Sync] {QGIS_FILENAME} non trovato — skip rigenerazione")
            return out_ids

        # ── 1. Patch in place delle proprieta' di stato su QGIS.geojson ─────────
        riepilogo_rows = []
        for feat in geo["features"]:
            props = feat.get("properties") or {}
            tid = str(props.get("TRATTA_ID") or "").strip()
            s = summary.get(tid)
            if s:
                props["STATO_AUTORIZZAZIONE"] = s["STATO_AUTORIZZAZIONE"]
                props["STATO_LEGENDA"]        = s["STATO_LEGENDA"]
                props["STATO_NULLAOSTA"]      = s["STATO_NULLAOSTA"]
                props["STATO_ORDINANZA"]      = s["STATO_ORDINANZA"]
                props["LAVORABILE"]           = s["LAVORABILE"]
                props["MOTIVO_NO"]            = s["MOTIVO_NO"]
                if s["PRATICA"]:
                    props["PRATICA"] = s["PRATICA"]
                if s["ENTE"]:
                    props["ENTE"] = s["ENTE"]
            feat["properties"] = props
            # Riga corrispondente per Riepilogo_progettazione.csv — stesse colonne,
            # stessi valori, derivati dalla stessa feature appena patchata.
            riepilogo_rows.append({col: props.get(col, "") for col in RIEPILOGO_COLUMNS})

        geo_bytes = json.dumps(geo, ensure_ascii=False).encode("utf-8")
        out_ids["qgis"] = await _store_derived_file(QGIS_FILENAME, geo_bytes, "application/geo+json", note)
        asyncio.create_task(_push_to_github(geo_bytes, path=GITHUB_PATHS["QGIS.geojson"], label=QGIS_FILENAME))

        # ── 2. Riepilogo_progettazione.csv: derivato 1:1 da QGIS.geojson ────────
        riep_df = pd.DataFrame(riepilogo_rows, columns=RIEPILOGO_COLUMNS)
        rbuf = io.StringIO()
        riep_df.to_csv(rbuf, index=False)  # virgola, come il file originale
        rdata = rbuf.getvalue().encode("utf-8")
        out_ids["riepilogo"] = await _store_derived_file(RIEPILOGO_FILENAME, rdata, "text/csv", note)
        asyncio.create_task(_push_to_github(rdata, path=GITHUB_PATHS["Riepilogo_progettazione.csv"], label=RIEPILOGO_FILENAME))

        print(f"[Sync] Rigenerati: {list(out_ids.keys())} ({len(summary)} tratte, note={note!r})")
    except Exception as e:
        print(f"[Sync] Errore rigenerazione QGIS/Riepilogo: {type(e).__name__}: {e}")
    return out_ids





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
    # Aggiorna Master.csv su GitHub e rigenera i file derivati (fire-and-forget)
    asyncio.create_task(_push_to_github(github_data, path=GITHUB_PATHS["Master.csv"], label="Master.csv"))
    asyncio.create_task(_regenerate_derived_files(df, note=note))
    return str(res.inserted_id)


def _serialize_assignment(d: dict) -> dict:
    out = dict(d)
    out["_id"] = str(out["_id"])
    return out


# ───────── Imprese (no admin token required, identified by their `nome`) ────

async def _find_assignment(nome: str) -> dict | None:
    """Cerca un assignment per nome in modo case-insensitive,
    così 'sertori', 'Sertori' e 'SERTORI' trovano tutti lo stesso record."""
    return await assignments_col.find_one(
        {"nome": {"$regex": f"^{re.escape(nome.strip())}$", "$options": "i"}}
    )


@app.get("/api/enti")
async def get_enti(sess: dict = Depends(_require_session)):
    """Restituisce tutti gli enti unici presenti in Master.csv, ordinati alfabeticamente."""
    df = await _read_master_csv()
    if df is None or "ENTE" not in df.columns:
        return {"enti": []}
    enti = sorted(
        {str(v).strip() for v in df["ENTE"].dropna() if str(v).strip()},
        key=lambda x: x.lower()
    )
    return {"enti": enti}


@app.get("/api/imprese/me")
async def impresa_me(sess: dict = Depends(_require_session)):
    """Returns the impresa's profile if they are assigned, else 404.
    Il nome viene dal token di sessione firmato, non da un parametro client."""
    doc = await _find_assignment(sess["nome"])
    if not doc:
        raise HTTPException(404, "Impresa non assegnata")
    return {"nome": doc["nome"], "lotti": doc.get("lotti", []), "active": bool(doc.get("active", True))}


@app.get("/api/imprese/pratiche")
async def impresa_pratiche(sess: dict = Depends(_require_session)):
    """Returns Master.csv rows whose Source.Name matches one of the user's lotti
    (confronto per codice lotto esatto, non substring — così 'Lotto 2' non
    aggancia per errore 'Lotto 2A.xlsx' o eventuali lotti a doppia cifra)."""
    doc = await _find_assignment(sess["nome"])
    if not doc or not doc.get("active", True):
        raise HTTPException(404, "Impresa non autorizzata")
    lotti = {_lotto_from_source(l) for l in doc.get("lotti", []) if str(l).strip()}
    df = await _read_master_csv()
    if "Source.Name" not in df.columns or not lotti:
        return {"pratiche": [], "lotti": sorted(lotti), "total": 0}
    mask = df["Source.Name"].apply(lambda x: _lotto_from_source(x) in lotti)
    sub = df[mask]
    pratiche = sub.fillna("").to_dict(orient="records")
    return {"pratiche": pratiche, "lotti": sorted(lotti), "total": len(pratiche)}


@app.post("/api/imprese/submit")
async def impresa_submit(payload: dict, sess: dict = Depends(_require_session)):
    """Body: {type: 'update'|'new', changes: [...]}. Il `nome` arriva dalla
    sessione firmata: anche se il client invia un 'nome' diverso nel body,
    viene ignorato — non e' piu' possibile inviare submission per conto di
    un'altra impresa semplicemente cambiando un parametro.
    For 'update': each change has {tratta_id, ente, tipo_permesso, fields:{col:val}}
    For 'new': each change is a full row dict.
    Goes into pending_updates with status='pending'."""
    nome = sess["nome"]
    typ = (payload or {}).get("type", "").strip()
    changes = (payload or {}).get("changes") or []
    if typ not in {"update", "new"}:
        raise HTTPException(400, "type must be 'update' or 'new'")
    if not isinstance(changes, list) or not changes:
        raise HTTPException(400, "Empty 'changes' array")
    doc = await _find_assignment(nome)
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
async def my_submissions(limit: int = 50, sess: dict = Depends(_require_session)):
    cur = pending_col.find({"nome": sess["nome"]}).sort("submitted_at", -1).limit(min(limit, 200))
    items = []
    async for d in cur:
        d["_id"] = str(d["_id"])
        items.append(d)
    return {"submissions": items, "count": len(items)}


@app.delete("/api/imprese/submissions/{sub_id}")
async def delete_my_submission(sub_id: str, sess: dict = Depends(_require_session)):
    """L'impresa può cancellare solo le proprie submission ancora in stato pending.
    Il confronto usa il nome dalla sessione, non un parametro client."""
    try:
        oid = ObjectId(sub_id)
    except Exception:
        raise HTTPException(400, "ID submission non valido")
    doc = await pending_col.find_one({"_id": oid})
    if not doc:
        raise HTTPException(404, "Submission non trovata")
    if doc.get("nome") != sess["nome"]:
        raise HTTPException(403, "Non autorizzato")
    if doc.get("status") != "pending":
        raise HTTPException(409, "Solo le richieste in attesa possono essere eliminate")
    await pending_col.delete_one({"_id": oid})
    return {"deleted": sub_id}


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
    out = await _find_assignment(nome)
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
    # Arricchisce ogni change con lo stato attuale dal Master.csv
    try:
        df = await _read_master_csv()
        for sub in items:
            if sub.get("type") != "update":
                continue
            for ch in sub.get("changes", []):
                tratta  = str(ch.get("tratta_id") or "").strip()
                ente    = str(ch.get("ente") or "").strip()
                tipo    = str(ch.get("tipo_permesso") or "").strip()
                pratica = str(ch.get("original_pratica") or "").strip()
                if not tratta:
                    continue
                mask = df["TRATTA_ID"].astype(str).str.strip() == tratta
                if ente:
                    mask = mask & (df["ENTE"].astype(str).str.strip() == ente)
                if tipo:
                    mask = mask & (df["TIPO_PERMESSO"].astype(str).str.strip() == tipo)
                if pratica and "PRATICA" in df.columns:
                    mp = mask & (df["PRATICA"].astype(str).str.strip() == pratica)
                    if mp.any():
                        mask = mp
                rows = df[mask]
                if rows.empty:
                    continue
                row = rows.sort_values("DATA_ULTIMA_MODIFICA", ascending=False).iloc[0]
                ch["_stato_attuale"]     = str(row.get("STATO_PERMESSO", "") or "")
                ch["_data_richiesta"]    = str(row.get("DATA_RICHIESTA", "") or "")
                ch["_data_ult_mod"]      = str(row.get("DATA_ULTIMA_MODIFICA", "") or "")
                ch["_data_approvazione"] = str(row.get("DATA_APPROVAZIONE", "") or "")
                # Ricava lotto da Source.Name (es. "Lotto1.xlsx" → "1A", "Lotto3.xlsx" → "3A")
                src = str(row.get("Source.Name", "") or "")
                import re as _re
                lm = _re.search(r'[Ll]otto\s*(\w+)', src)
                ch["_lotto"] = lm.group(1) if lm else ""
    except Exception as e:
        print(f"[pending-updates] enrich error: {e}")

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
    # Sync cantieri: crea automaticamente cantieri non_avviato per le nuove tratte lavorabili
    asyncio.create_task(_sync_cantieri())
    asyncio.create_task(_push_cantieri_to_github())
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


# ═════════════════════════════════════════════════════════════════════════════
# SOPRALLUOGHI — verbali di sopralluogo
# ─────────────────────────────────────────────────────────────────────────────
SOPRALLUOGHI_COLS = [
    "codice_verbale", "data_sopralluogo", "lotto", "tratta_id", "impresa",
    "referente_impresa", "referente_retelit", "comune", "localita",
    "tipo_intervento", "esito", "note", "segnalazioni", "azioni_richieste",
    "firma_impresa", "firma_retelit", "created_at",
]


async def _build_sopralluoghi_csv() -> bytes:
    rows = []
    async for d in sopralluoghi_col.find({}).sort("codice_verbale", 1):
        row = {col: str(d.get(col, "")) for col in SOPRALLUOGHI_COLS}
        rows.append(row)
    import io, csv as _csv
    buf = io.StringIO()
    w = _csv.DictWriter(buf, fieldnames=SOPRALLUOGHI_COLS, delimiter=";",
                        extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


async def _push_sopralluoghi_to_github() -> None:
    try:
        data = await _build_sopralluoghi_csv()
        await _push_to_github(data, path=GITHUB_PATHS["sopralluoghi.csv"], label="sopralluoghi.csv")
    except Exception as e:
        print(f"[GitHub] _push_sopralluoghi: {e}")


@app.get("/api/sopralluoghi/next-codice")
async def sopralluogo_next_codice(sess: dict = Depends(_require_session)):
    """Restituisce il prossimo codice verbale progressivo."""
    last = await sopralluoghi_col.find_one({}, sort=[("codice_verbale", -1)])
    next_n = 1
    if last and last.get("codice_verbale"):
        try:
            next_n = int(str(last["codice_verbale"]).split("-")[-1]) + 1
        except (ValueError, IndexError):
            count = await sopralluoghi_col.count_documents({})
            next_n = count + 1
    year = _now_iso()[:4]
    return {"codice": f"VBS-{year}-{next_n:04d}", "numero": next_n}


@app.post("/api/sopralluoghi")
async def save_sopralluogo(payload: dict, sess: dict = Depends(_require_session)):
    """Salva un verbale di sopralluogo su MongoDB e aggiorna sopralluoghi.csv su GitHub."""
    last = await sopralluoghi_col.find_one({}, sort=[("codice_verbale", -1)])
    next_n = 1
    if last and last.get("codice_verbale"):
        try:
            next_n = int(str(last["codice_verbale"]).split("-")[-1]) + 1
        except (ValueError, IndexError):
            count = await sopralluoghi_col.count_documents({})
            next_n = count + 1
    year = _now_iso()[:4]
    codice = f"VBS-{year}-{next_n:04d}"

    record = {
        "codice_verbale":      codice,
        "data_sopralluogo":    str((payload or {}).get("data_sopralluogo", "")).strip(),
        "lotto":               str((payload or {}).get("lotto", "")).strip(),
        "tratta_id":           str((payload or {}).get("tratta_id", "")).strip(),
        "impresa":             str((payload or {}).get("impresa", sess["nome"])).strip(),
        "referente_impresa":   str((payload or {}).get("referente_impresa", "")).strip(),
        "referente_retelit":   str((payload or {}).get("referente_retelit", "")).strip(),
        "comune":              str((payload or {}).get("comune", "")).strip(),
        "localita":            str((payload or {}).get("localita", "")).strip(),
        "tipo_intervento":     str((payload or {}).get("tipo_intervento", "")).strip(),
        "esito":               str((payload or {}).get("esito", "")).strip(),
        "note":                str((payload or {}).get("note", "")).strip(),
        "segnalazioni":        str((payload or {}).get("segnalazioni", "")).strip(),
        "azioni_richieste":    str((payload or {}).get("azioni_richieste", "")).strip(),
        "firma_impresa":       str((payload or {}).get("firma_impresa", "")).strip(),
        "firma_retelit":       str((payload or {}).get("firma_retelit", "")).strip(),
        "created_at":          _now_iso(),
    }
    await sopralluoghi_col.insert_one(record)
    asyncio.create_task(_push_sopralluoghi_to_github())
    return {"ok": True, "codice_verbale": codice}


# SOLLECITI — registro solleciti per tratta/pratica
# ─────────────────────────────────────────────────────────────────────────────
# Ogni sollecito viene scritto direttamente su MongoDB (senza approvazione admin)
# e il CSV solleciti.csv viene sincronizzato su GitHub dopo ogni inserimento.
# ═════════════════════════════════════════════════════════════════════════════

SOLLECITI_COLS = ["_id", "tratta_id", "pratica", "ente", "tipo_permesso", "stato_permesso",
                  "lunghezza", "data_richiesta", "data_ultima_modifica",
                  "numero_sollecito", "tipo_sollecito", "data_sollecito", "note", "impresa", "created_at"]


async def _build_solleciti_csv() -> bytes:
    """Legge tutti i solleciti da MongoDB e genera un CSV con separatore ;"""
    cols = ["tratta_id", "pratica", "tipo_sollecito", "data_sollecito", "note", "impresa", "created_at"]
    rows = []
    async for d in solleciti_col.find({}).sort("created_at", -1):
        rows.append({c: str(d.get(c, "") or "") for c in cols})
    df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    buf = io.StringIO()
    df.to_csv(buf, index=False, sep=";")
    return buf.getvalue().encode("utf-8")


async def _push_solleciti_to_github() -> None:
    """Rigenera solleciti.csv e lo pusha su GitHub."""
    try:
        data = await _build_solleciti_csv()
        await _push_to_github(data, path=GITHUB_PATHS["solleciti.csv"], label="solleciti.csv")
    except Exception as e:
        print(f"[GitHub] _push_solleciti: {e}")


@app.get("/api/imprese/solleciti")
async def get_solleciti(sess: dict = Depends(_require_session)):
    """Restituisce i solleciti dell'impresa autenticata, filtrati per le tratte dei suoi lotti."""
    nome = sess["nome"]
    # Recupera i lotti assegnati
    assignment = await _find_assignment(nome)
    if not assignment:
        return {"solleciti": [], "count": 0}
    lotti = [str(l) for l in (assignment.get("lotti") or [])]

    # Legge le tratte dei lotti dell'impresa dal Master.csv
    try:
        df = await _read_master_csv()
        tratte_impresa: set[str] = set()
        for lotto in lotti:
            mask = df["Source.Name"].astype(str).str.contains(lotto, case=False, na=False)
            tratte_impresa.update(df.loc[mask, "TRATTA_ID"].astype(str).str.strip().tolist())
    except Exception:
        tratte_impresa = set()

    cur = solleciti_col.find(
        {"tratta_id": {"$in": list(tratte_impresa)}} if tratte_impresa else {"impresa": nome}
    ).sort("created_at", -1).limit(500)
    items = []
    async for d in cur:
        d["_id"] = str(d["_id"])
        items.append(d)
    return {"solleciti": items, "count": len(items)}


@app.post("/api/imprese/solleciti")
async def add_sollecito(payload: dict, sess: dict = Depends(_require_session)):
    """Inserisce un nuovo sollecito. Scrittura diretta senza approvazione admin."""
    nome = sess["nome"]
    tratta_id    = str((payload or {}).get("tratta_id", "")).strip()
    pratica      = str((payload or {}).get("pratica", "")).strip()
    tipo         = str((payload or {}).get("tipo_sollecito", "")).strip()
    data_sol     = str((payload or {}).get("data_sollecito", "")).strip()
    note         = str((payload or {}).get("note", "")).strip()
    ente         = str((payload or {}).get("ente", "")).strip()
    tipo_perm    = str((payload or {}).get("tipo_permesso", "")).strip()
    stato_perm   = str((payload or {}).get("stato_permesso", "")).strip()
    lunghezza    = str((payload or {}).get("lunghezza", "")).strip()
    data_rich    = str((payload or {}).get("data_richiesta", "")).strip()
    data_ult_mod = str((payload or {}).get("data_ultima_modifica", "")).strip()
    try:
        numero_sol = int((payload or {}).get("numero_sollecito") or 1)
    except (TypeError, ValueError):
        numero_sol = 1

    if not tratta_id:
        raise HTTPException(400, "tratta_id obbligatorio")
    if tipo not in ("PEC", "MAIL", "TELEFONICO"):
        raise HTTPException(400, "tipo_sollecito deve essere PEC, MAIL o TELEFONICO")
    if not data_sol:
        raise HTTPException(400, "data_sollecito obbligatoria")

    record = {
        "tratta_id":           tratta_id,
        "pratica":             pratica,
        "tipo_sollecito":      tipo,
        "data_sollecito":      data_sol,
        "note":                note,
        "impresa":             nome,
        "ente":                ente,
        "tipo_permesso":       tipo_perm,
        "stato_permesso":      stato_perm,
        "lunghezza":           lunghezza,
        "data_richiesta":      data_rich,
        "data_ultima_modifica": data_ult_mod,
        "numero_sollecito":    numero_sol,
        "created_at":          _now_iso(),
    }
    res = await solleciti_col.insert_one(record)
    asyncio.create_task(_push_solleciti_to_github())
    return {"ok": True, "id": str(res.inserted_id)}


@app.post("/api/imprese/solleciti/bulk-insert")
async def bulk_insert_solleciti(payload: dict, sess: dict = Depends(_require_session)):
    """Inserisce più solleciti in una sola chiamata e fa un unico push GitHub."""
    nome  = sess["nome"]
    items = (payload or {}).get("items", [])
    if not items or not isinstance(items, list):
        raise HTTPException(400, "items obbligatorio")

    inserted = []
    for item in items:
        tratta_id = str(item.get("tratta_id", "")).strip()
        tipo      = str(item.get("tipo_sollecito", "")).strip()
        data_sol  = str(item.get("data_sollecito", "")).strip()
        if not tratta_id or tipo not in ("PEC", "MAIL", "TELEFONICO") or not data_sol:
            continue
        record = {
            "tratta_id":           tratta_id,
            "pratica":             str(item.get("pratica", "")).strip(),
            "tipo_sollecito":      tipo,
            "data_sollecito":      data_sol,
            "note":                str(item.get("note", "")).strip(),
            "impresa":             nome,
            "ente":                str(item.get("ente", "")).strip(),
            "tipo_permesso":       str(item.get("tipo_permesso", "")).strip(),
            "stato_permesso":      str(item.get("stato_permesso", "")).strip(),
            "lunghezza":           str(item.get("lunghezza", "")).strip(),
            "data_richiesta":      str(item.get("data_richiesta", "")).strip(),
            "data_ultima_modifica": str(item.get("data_ultima_modifica", "")).strip(),
            "numero_sollecito":    int(item.get("numero_sollecito") or 1),
            "created_at":          _now_iso(),
        }
        res = await solleciti_col.insert_one(record)
        inserted.append(str(res.inserted_id))

    if inserted:
        asyncio.create_task(_push_solleciti_to_github())
    return {"ok": True, "inserted": inserted, "count": len(inserted)}


@app.delete("/api/imprese/solleciti/{sol_id}")
async def delete_sollecito(sol_id: str, sess: dict = Depends(_require_session)):
    """L'impresa può eliminare solo i propri solleciti."""
    try:
        oid = ObjectId(sol_id)
    except Exception:
        raise HTTPException(400, "ID non valido")
    doc = await solleciti_col.find_one({"_id": oid})
    if not doc:
        raise HTTPException(404, "Sollecito non trovato")
    if doc.get("impresa") != sess["nome"]:
        raise HTTPException(403, "Non autorizzato")
    await solleciti_col.delete_one({"_id": oid})
    asyncio.create_task(_push_solleciti_to_github())
    return {"deleted": sol_id}




@app.get("/api/admin/solleciti")
async def admin_get_solleciti(
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q:        Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    """Restituisce tutti i solleciti (vista admin, senza filtro impresa)."""
    _check_token(x_upload_token or token_q)
    items = []
    async for d in solleciti_col.find({}).sort("data_sollecito", -1):
        d["_id"] = str(d["_id"])
        items.append(d)
    return {"solleciti": items, "count": len(items)}

@app.post("/api/imprese/solleciti/bulk-delete")
async def bulk_delete_solleciti(payload: dict, sess: dict = Depends(_require_session)):
    """Elimina più solleciti in una sola chiamata e fa un unico push GitHub."""
    ids = (payload or {}).get("ids", [])
    if not ids or not isinstance(ids, list):
        raise HTTPException(400, "ids obbligatorio")
    deleted = []
    for sol_id in ids:
        try:
            oid = ObjectId(str(sol_id))
        except Exception:
            continue
        doc = await solleciti_col.find_one({"_id": oid})
        if not doc or doc.get("impresa") != sess["nome"]:
            continue
        await solleciti_col.delete_one({"_id": oid})
        deleted.append(str(sol_id))
    if deleted:
        asyncio.create_task(_push_solleciti_to_github())
    return {"deleted": deleted, "count": len(deleted)}


# ═════════════════════════════════════════════════════════════════════════════
# CANTIERI — stato avanzamento scavi per PRATICA di AUTORIZZAZIONE
# ─────────────────────────────────────────────────────────────────────────────
# Un cantiere = una pratica di AUTORIZZAZIONE ottenuta (non una singola tratta:
# una stessa autorizzazione può coprire più tratte). Il NULLA OSTA non è un
# cantiere a sé: è un permesso accessorio che può mancare su alcune tratte
# della stessa autorizzazione. Quelle tratte restano elencate nel cantiere
# (per visibilità) ma i loro metri sono esclusi da metri_totali finché il
# nulla osta non viene ottenuto — a quel punto rientrano automaticamente al
# sync successivo.
#
# Flusso:
#   1. Ogni volta che il Master.csv viene aggiornato, _sync_cantieri() raggruppa
#      le tratte con AUTORIZZAZIONE OTTENUTA per (ente, numero pratica, lotto)
#      e crea/aggiorna un documento cantiere per pratica.
#   2. L'impresa aggiorna giornalmente i metri realizzati e lo stato cantiere
#      a livello di pratica (un solo stato/contatore per tutte le tratte).
#   3. scavi.html legge GET /api/cantieri (pubblico) per popolare i grafici.
# ═════════════════════════════════════════════════════════════════════════════

STATO_CANTIERE_VALUES = ["non_avviato", "allestimento", "in_corso", "sospeso", "completato"]
TECNICA_SCAVO_VALUES  = ["trincea", "no_dig", ""]

CANTIERI_CSV_PATH = os.environ.get("GITHUB_CANTIERI_PATH", "cantieri.csv")
CANTIERI_COLS = [
    "pratica_id", "ente", "lotto", "cluster",
    "tratte_lavorabili", "tratte_bloccate",
    "metri_totali", "metri_totali_potenziali",
    "stato_cantiere", "tecnica_scavo",
    "data_inizio_prevista", "data_inizio_effettiva",
    "data_fine_prevista", "data_fine_effettiva",
    "metri_scavati", "n_fronti_attivi", "note",
    "impresa", "updated_at",
]


def _cantieri_csv_row(d: dict) -> dict:
    """Appiattisce un documento cantiere (con array 'tratte') in una riga CSV."""
    tratte = d.get("tratte", []) or []
    lav  = [t["tratta_id"] for t in tratte if t.get("lavorabile")]
    bloc = [f"{t['tratta_id']} ({t.get('motivo_no','').strip() or 'bloccata'})" for t in tratte if not t.get("lavorabile")]
    row = {c: str(d.get(c, "") or "") for c in CANTIERI_COLS}
    row["tratte_lavorabili"] = ", ".join(lav)
    row["tratte_bloccate"]   = ", ".join(bloc)
    return row


async def _sync_cantieri() -> int:
    """Raggruppa le tratte con AUTORIZZAZIONE OTTENUTA per pratica (ente, numero,
    lotto) e crea/aggiorna un documento cantiere per pratica. metri_totali conta
    solo le tratte attualmente LAVORABILE=SI; le tratte bloccate da un nulla
    osta mancante restano elencate in 'tratte' ma escluse dal totale.
    Ritorna il numero di nuovi cantieri (nuove pratiche) creati."""
    try:
        df = await _read_master_csv()
        summary = _compute_tratta_summary(df)
    except Exception as e:
        print(f"[sync_cantieri] errore lettura master: {e}")
        return 0

    groups: dict[tuple, dict] = {}
    for tratta_id, info in summary.items():
        if info.get("STATO_AUTORIZZAZIONE") != "OTTENUTO":
            continue  # niente cantiere finché l'autorizzazione non è ottenuta
        pratica_num = (info.get("PRATICA_AUT") or "").strip()
        if not pratica_num:
            continue
        rows = df[df["TRATTA_ID"].astype(str).str.strip() == tratta_id]
        lotto   = _lotto_from_source(rows.iloc[0].get("Source.Name", "")) if not rows.empty else ""
        cluster = str(rows.iloc[0].get("CLUSTER", "")) if not rows.empty else ""
        try:
            lunghezza = float(str(info.get("LUNGHEZZA", 0) or 0).replace(",", "."))
        except Exception:
            lunghezza = 0.0

        ente = info.get("ENTE", "")
        key = (ente, pratica_num, lotto)
        g = groups.setdefault(key, {
            "pratica_id": f"AUT/{pratica_num}/{lotto}",
            "ente": ente, "lotto": lotto, "cluster": cluster,
            "tratte": [],
        })
        g["tratte"].append({
            "tratta_id":  tratta_id,
            "lunghezza":  lunghezza,
            "lavorabile": info.get("LAVORABILE") == "SI",
            "motivo_no":  info.get("MOTIVO_NO", ""),
        })

    created = 0
    stato_rank = {s: i for i, s in enumerate(STATO_CANTIERE_VALUES)}
    for key, g in groups.items():
        metri_totali     = sum(t["lunghezza"] for t in g["tratte"] if t["lavorabile"])
        metri_totali_pot = sum(t["lunghezza"] for t in g["tratte"])

        existing = await cantieri_col.find_one({"pratica_id": g["pratica_id"], "ente": g["ente"]})
        if existing:
            await cantieri_col.update_one(
                {"_id": existing["_id"]},
                {"$set": {
                    "tratte": g["tratte"], "lotto": g["lotto"], "cluster": g["cluster"],
                    "metri_totali": metri_totali, "metri_totali_potenziali": metri_totali_pot,
                }},
            )
            continue

        # Migrazione best-effort: se esistevano già documenti del VECCHIO schema
        # (1 per tratta_id, pre-raggruppamento per pratica), recupera l'avanzamento
        # già inserito dall'impresa prima di accorparli nel nuovo cantiere.
        tratta_ids = [t["tratta_id"] for t in g["tratte"]]
        old_docs = await cantieri_col.find(
            {"tratta_id": {"$in": tratta_ids}, "pratica_id": {"$exists": False}}
        ).to_list(length=None)

        metri_scavati  = sum(float(d.get("metri_scavati", 0) or 0) for d in old_docs)
        old_log = [
            {**entry, "tratta_id": d.get("tratta_id")}
            for d in old_docs for entry in d.get("log", [])
        ]
        stato_cantiere = max(
            (d.get("stato_cantiere", "non_avviato") for d in old_docs),
            key=lambda s: stato_rank.get(s, 0), default="non_avviato",
        )
        tecnica_scavo = next((d.get("tecnica_scavo") for d in old_docs if d.get("tecnica_scavo")), "")
        n_fronti      = max((int(d.get("n_fronti_attivi", 0) or 0) for d in old_docs), default=0)
        impresa       = next((d.get("impresa") for d in old_docs if d.get("impresa")), "")

        doc = {
            "pratica_id": g["pratica_id"], "ente": g["ente"], "lotto": g["lotto"], "cluster": g["cluster"],
            "tratte": g["tratte"],
            "metri_totali": metri_totali, "metri_totali_potenziali": metri_totali_pot,
            "stato_cantiere": stato_cantiere, "tecnica_scavo": tecnica_scavo,
            "data_inizio_prevista": "", "data_inizio_effettiva": "",
            "data_fine_prevista": "", "data_fine_effettiva": "",
            "metri_scavati": metri_scavati, "n_fronti_attivi": n_fronti, "note": "",
            "impresa": impresa, "updated_at": _now_iso(),
            "log": old_log,
        }
        await cantieri_col.insert_one(doc)
        if old_docs:
            await cantieri_col.delete_many({"_id": {"$in": [d["_id"] for d in old_docs]}})
        created += 1

    if created:
        print(f"[sync_cantieri] creati {created} nuovi cantieri (per pratica)")
    return created


async def _push_cantieri_to_github() -> None:
    """Rigenera cantieri.csv e lo pusha su GitHub."""
    try:
        rows = []
        async for d in cantieri_col.find({}).sort("pratica_id", 1):
            rows.append(_cantieri_csv_row(d))
        import pandas as _pd, io as _io
        _df = _pd.DataFrame(rows, columns=CANTIERI_COLS) if rows else _pd.DataFrame(columns=CANTIERI_COLS)
        buf = _io.StringIO()
        _df.to_csv(buf, index=False, sep=";")
        data = buf.getvalue().encode("utf-8")
        await _push_to_github(data, path=CANTIERI_CSV_PATH, label="cantieri.csv")
    except Exception as e:
        print(f"[GitHub] _push_cantieri: {e}")


# ── Endpoint pubblico: lista cantieri ────────────────────────────────────────

@app.get("/api/cantieri")
async def get_cantieri(lotto: str = "", cluster: str = "", stato: str = ""):
    """Lista cantieri (pubblica), uno per pratica di autorizzazione. Filtrabile
    per lotto, cluster, stato."""
    q: dict = {}
    if lotto:   q["lotto"]          = lotto
    if cluster: q["cluster"]        = cluster
    if stato:   q["stato_cantiere"] = stato
    items = []
    async for d in cantieri_col.find(q).sort("pratica_id", 1):
        d["_id"] = str(d["_id"])
        d.pop("log", None)   # non esporre lo storico nel listing
        items.append(d)
    return {"cantieri": items, "count": len(items)}


# ── Endpoint impresa: aggiornamento giornaliero ───────────────────────────────

@app.get("/api/admin/sync-cantieri")
async def admin_sync_cantieri(
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    """Forza la sincronizzazione dei cantieri (solo admin)."""
    _check_token(x_upload_token or token_q)
    created = await _sync_cantieri()
    total = await cantieri_col.count_documents({})
    asyncio.create_task(_push_cantieri_to_github())
    return {"created": created, "total_cantieri": total}


@app.get("/api/imprese/cantieri")
async def get_cantieri_impresa(sess: dict = Depends(_require_session)):
    """Cantieri (per pratica) nei lotti dell'impresa autenticata."""
    nome = sess["nome"]
    assignment = await _find_assignment(nome)
    if not assignment:
        return {"cantieri": [], "count": 0, "debug": "no assignment found"}
    # Normalizza i lotti dell'impresa con _lotto_from_source
    raw_lotti = [str(l) for l in (assignment.get("lotti") or [])]
    lotti = [_lotto_from_source(l) for l in raw_lotti]
    items = []
    async for d in cantieri_col.find({}).sort("pratica_id", 1):
        d["_id"] = str(d["_id"])
        d.pop("log", None)
        cant_lotto = _lotto_from_source(str(d.get("lotto") or ""))
        if cant_lotto in lotti:
            items.append(d)
    return {"cantieri": items, "count": len(items)}


@app.post("/api/imprese/cantieri/{pratica_id:path}")
async def update_cantiere(pratica_id: str, payload: dict, sess: dict = Depends(_require_session)):
    """L'impresa aggiorna lo stato cantiere e i metri realizzati oggi (a livello
    di pratica: un solo stato/contatore per tutte le tratte della pratica)."""
    nome = sess["nome"]
    doc = await cantieri_col.find_one({"pratica_id": pratica_id})
    if not doc:
        raise HTTPException(404, "Cantiere non trovato")

    # Verifica che la pratica appartenga ai lotti dell'impresa
    assignment = await _find_assignment(nome)
    lotti = [str(l) for l in ((assignment or {}).get("lotti") or [])]
    if doc.get("lotto") not in lotti:
        raise HTTPException(403, "Pratica non assegnata a questa impresa")

    # Campi aggiornabili dall'impresa
    allowed = {
        "stato_cantiere", "tecnica_scavo",
        "data_inizio_prevista", "data_inizio_effettiva",
        "data_fine_prevista", "data_fine_effettiva",
        "metri_realizzati_oggi",   # campo speciale: viene accumulato
        "n_fronti_attivi", "note",
    }
    update: dict = {}
    for k, v in (payload or {}).items():
        if k not in allowed:
            continue
        if k == "stato_cantiere" and v not in STATO_CANTIERE_VALUES:
            raise HTTPException(400, f"stato_cantiere non valido: {v}")
        if k == "tecnica_scavo" and v not in TECNICA_SCAVO_VALUES:
            raise HTTPException(400, f"tecnica_scavo non valida: {v}")
        if k != "metri_realizzati_oggi":
            update[k] = v

    # Accumula metri giornalieri
    metri_oggi = 0.0
    if "metri_realizzati_oggi" in payload:
        try:
            metri_oggi = max(0.0, float(payload["metri_realizzati_oggi"]))
        except Exception:
            raise HTTPException(400, "metri_realizzati_oggi deve essere un numero")
        update["$inc"] = {"metri_scavati": metri_oggi}

    update["impresa"]    = nome
    update["updated_at"] = _now_iso()

    # Log entry giornaliero
    log_entry = {
        "data":               _now_iso()[:10],
        "impresa":            nome,
        "stato_cantiere":     payload.get("stato_cantiere", doc.get("stato_cantiere")),
        "metri_realizzati":   metri_oggi,
        "n_fronti_attivi":    payload.get("n_fronti_attivi", doc.get("n_fronti_attivi", 0)),
        "note":               payload.get("note", ""),
    }

    # Costruisci update MongoDB
    mongo_set = {k: v for k, v in update.items() if k != "$inc"}
    mongo_update: dict = {"$set": mongo_set, "$push": {"log": log_entry}}
    if "$inc" in update:
        mongo_update["$inc"] = update["$inc"]

    await cantieri_col.update_one({"pratica_id": pratica_id}, mongo_update)
    asyncio.create_task(_push_cantieri_to_github())
    return {"ok": True, "pratica_id": pratica_id}


@app.get("/api/imprese/cantieri/{pratica_id:path}/log")
async def get_cantiere_log(pratica_id: str, sess: dict = Depends(_require_session)):
    """Storico aggiornamenti giornalieri di un cantiere (pratica)."""
    doc = await cantieri_col.find_one({"pratica_id": pratica_id})
    if not doc:
        raise HTTPException(404, "Cantiere non trovato")
    return {"log": doc.get("log", []), "pratica_id": pratica_id}


# ── Trigger sync cantieri dopo approvazione Master.csv ───────────────────────
# _sync_cantieri() è già chiamata alla fine di approve_pending_update
# (vedi hook sotto) — aggiungiamo il trigger se non esiste
