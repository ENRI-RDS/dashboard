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
from collections import Counter
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
admin_actions_col = db["admin_actions"]  # audit: azioni protette da UPLOAD_TOKEN (chi/cosa/quando)
assignments_col = db["assignments"]            # impresa nome -> {lotti: [...]}
pending_col = db["pending_updates"]            # submissions from imprese pending admin review
solleciti_col = db["solleciti"]                # registro solleciti per tratta/pratica

# Lock per serializzare i cicli read-modify-write su Master.csv (GridFS).
# Senza questo lock, due richieste concorrenti (es. solleciti di imprese diverse)
# possono leggere la stessa versione e la seconda scrittura sovrascrive/perde la prima.
_master_csv_lock = asyncio.Lock()
cantieri_col  = db["cantieri"]                  # stato cantiere per pratica di autorizzazione
sopralluoghi_col = db["sopralluoghi"]          # verbali di sopralluogo
pol_conv_dates_col = db["pol_conv_dates"]      # prima data in cui CONVENZIONE/POLIZZA è comparsa per una pratica
access_logs_col = db["access_logs"]            # log accessi (ex-JSONBin) — un documento per binId, {utenti:[...], accessi:[...]}
gantt_overrides_col = db["gantt_overrides"]    # override manuali riga Gantt (pct/date/label) per lotto, indip. da invii impresa
gantt_rates_col = db["gantt_rates"]            # regole tasso scavo (m/giorno) per scope: "global" | "lotto:<ID>" | "impresa:<NOME>" | "pratica:<CODICE>"
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


async def _log_admin_action(azione: str, target: str, actor_nome: str | None) -> None:
    """Audit trail per le azioni protette da UPLOAD_TOKEN (non da sessione, quindi
    `actor_nome` è auto-dichiarato dal client — utile per tracciare, non per provare)."""
    try:
        await admin_actions_col.insert_one({
            "azione": azione, "target": target,
            "actor": (actor_nome or "").strip() or "sconosciuto",
            "timestamp": _now_iso(),
        })
    except Exception:
        pass  # l'audit log non deve mai far fallire l'azione principale


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


KEEP_VERSIONS = int(os.environ.get("KEEP_VERSIONS", "4"))


async def _prune_old_versions(filename: str, keep: int = KEEP_VERSIONS) -> int:
    """Mantiene solo le ultime `keep` versioni non cancellate di un file:
    elimina il blob GridFS e soft-delete (deleted_at) delle versioni più vecchie."""
    cur = uploads_col.find({"filename": filename, "deleted_at": None}).sort("uploaded_at", -1).skip(keep)
    n = 0
    async for doc in cur:
        gid = doc.get("gridfs_id")
        if gid:
            try:
                await gridfs.delete(gid)
            except Exception:
                pass
        await uploads_col.update_one(
            {"_id": doc["_id"]},
            {"$set": {"deleted_at": _now_iso(), "gridfs_id": None}},
        )
        n += 1
    return n


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
# Sessioni firmate (usate anche da /api/data* più sotto — definite qui,
# prima delle routes, perché Depends() valuta il default arg a import-time)
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


async def _require_staff_session(
    x_session_token: Annotated[str | None, Header(alias="x-session-token")] = None,
) -> dict:
    """Come _require_session ma esclude il ruolo 'impresa' — per endpoint non
    pensati per le pagine Area Impresa (che usano gli /api/imprese/* scoped)."""
    sess = await _require_session(x_session_token)
    if sess.get("ruolo") == "impresa":
        raise HTTPException(403, "Accesso non consentito per questo ruolo")
    return sess


async def _require_admin_session(
    x_session_token: Annotated[str | None, Header(alias="x-session-token")] = None,
) -> dict:
    """Solo ruolo 'admin' o 'admin2' — per scritture riservate (es. override Gantt)."""
    sess = await _require_staff_session(x_session_token)
    if sess.get("ruolo") not in ("admin", "admin2"):
        raise HTTPException(403, "Riservato ad admin")
    return sess


async def _require_milestone_session(
    x_session_token: Annotated[str | None, Header(alias="x-session-token")] = None,
) -> dict:
    """Come _require_staff_session ma esclude anche il ruolo 'dl' (Direzione
    Lavori): vede tutto come 'user' tranne la pagina Milestone di Progetto,
    su richiesta esplicita utente. Il ruolo è letto dal token firmato
    (HMAC/SESSION_SECRET), non falsificabile lato client."""
    sess = await _require_staff_session(x_session_token)
    if sess.get("ruolo") == "dl":
        raise HTTPException(403, "Milestone di progetto non disponibili per questo ruolo")
    return sess


# File "core" con dati di TUTTI i lotti/imprese. In lettura (/api/data*,
# /api/preview, /api/files) sono riservati ai ruoli interni: le Aree Impresa
# usano gli endpoint /api/imprese/* già scoped sui propri lotti. Inoltre NON
# vengono più pubblicati sul repo GitHub pubblico (vedi _push_to_github).
SENSITIVE_FILES = {
    "Master.csv",
    "QGIS.geojson",
    "Riepilogo_progettazione.csv",
    "SED_classificato.geojson",
}


def _guard_sensitive_read(rel: str, sess: dict) -> None:
    """Blocca la lettura dei file core da parte del ruolo 'impresa': un token
    impresa legittimo non deve poter scaricare il dataset intero di tutti i
    concorrenti via /api/data. I ruoli interni (admin/admin2/user) restano ok."""
    if Path(rel).name in SENSITIVE_FILES and sess.get("ruolo") == "impresa":
        raise HTTPException(
            403,
            "Accesso non consentito per questo ruolo — le imprese usano gli endpoint /api/imprese/*",
        )


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
async def list_files(sess: dict = Depends(_require_staff_session)):
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
                "modified": _GITHUB_PUSH_TIMES.get(p.name) or datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
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
async def get_data_file(filename: str, sess: dict = Depends(_require_session)):
    rel = _safe_relpath(filename)
    _guard_sensitive_read(rel, sess)
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
async def get_data_text(filename: str, sess: dict = Depends(_require_session)):
    rel = _safe_relpath(filename)
    _guard_sensitive_read(rel, sess)
    cur = await _current_upload(rel)
    if cur:
        data = await _read_gridfs(cur["gridfs_id"])
        return data.decode("utf-8", errors="replace")
    path = DATA_DIR / rel
    if path.exists() and path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")
    raise HTTPException(404, f"File not found: {rel}")


@app.get("/api/preview/{filename:path}")
async def preview_file(filename: str, max_bytes: int = 8192, sess: dict = Depends(_require_staff_session)):
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
    sess: dict = Depends(_require_staff_session),
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
    x_actor_nome: Annotated[str | None, Header(alias="x-actor-nome")] = None,
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
    df = None
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

    # Se stiamo sostituendo Master.csv, preserva eventuale stato CONVENZIONE/POLIZZA
    # già avanzato (RICHIESTA RDS/INVIATA/EMESSA) rispetto a quanto porta il nuovo
    # file — fix bug: un nuovo export esterno (QGIS/Excel) porta solo SI/NO e
    # retrocederebbe silenziosamente il workflow tracciato da polizze_convenzioni.html.
    pol_conv_preserved = 0
    if rel == MASTER_FILENAME:
        try:
            sep_new = _detect_sep(out_bytes)
            if df is None:
                new_df = None
                for enc in ("utf-8", "cp1252", "latin-1"):
                    try:
                        new_df = pd.read_csv(io.BytesIO(out_bytes), sep=sep_new, dtype=str, keep_default_na=False, encoding=enc, on_bad_lines="warn")
                        break
                    except (UnicodeDecodeError, UnicodeError):
                        continue
            else:
                new_df = df.astype(str)
            if new_df is not None:
                old_df = await _read_master_csv()
                pol_conv_preserved = _preserve_pol_conv_state(old_df, new_df)
                if pol_conv_preserved:
                    buf2 = io.StringIO()
                    new_df.to_csv(buf2, index=False, sep=sep_new)
                    out_bytes = buf2.getvalue().encode("utf-8")
        except Exception as e:
            print(f"[_preserve_pol_conv_state] fallita, procedo senza (upload non bloccato): {e}")

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
    asyncio.create_task(_prune_old_versions(rel))
    await _log_admin_action(
        "upload" + (f" (+{pol_conv_preserved} CONVENZIONE/POLIZZA preservate)" if pol_conv_preserved else ""),
        rel, x_actor_nome,
    )

    # Se è Master.csv, rigenera anche i file derivati (Riepilogo_progettazione.csv,
    # QGIS.geojson) e sincronizza GitHub — stesso comportamento di approve/delete/restore,
    # altrimenti un upload manuale lascia mappa e barre ferme alla versione precedente.
    if rel == MASTER_FILENAME:
        asyncio.create_task(_push_current_master_to_github())
    elif rel in GITHUB_PATHS:
        asyncio.create_task(_push_to_github(out_bytes, path=GITHUB_PATHS[rel], label=rel))

    return JSONResponse({
        "ok": True,
        "id": str(res.inserted_id),
        "filename": rel,
        "size": len(out_bytes),
        "rows": rows,
        "converted_from_excel": ext in {".xlsx", ".xls"} and convert_to_csv,
        "pol_conv_preserved": pol_conv_preserved,
    })


@app.delete("/api/uploads/{upload_id}")
async def delete_upload(
    upload_id: str,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
    x_actor_nome: Annotated[str | None, Header(alias="x-actor-nome")] = None,
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
    # Determina se questa era la versione corrente PRIMA di cancellarla:
    # se era una versione vecchia già superata, il contenuto "corrente" non
    # cambia e non serve pushare/rigenerare nulla su GitHub.
    was_current = False
    if doc.get("filename") == MASTER_FILENAME:
        cur = await _current_upload(MASTER_FILENAME)
        was_current = bool(cur and cur["_id"] == oid)
    if doc.get("gridfs_id"):
        try:
            await gridfs.delete(doc["gridfs_id"])
        except Exception:
            pass
    await uploads_col.update_one(
        {"_id": oid},
        {"$set": {"deleted_at": _now_iso(), "gridfs_id": None}},
    )
    await _log_admin_action("delete_version", doc["filename"], x_actor_nome)
    # Sincronizza GitHub solo se era davvero la versione corrente di Master.csv
    if was_current:
        asyncio.create_task(_push_current_master_to_github())
    return {"deleted": str(oid), "filename": doc["filename"]}


@app.delete("/api/files/{filename:path}")
async def delete_file(
    filename: str,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
    x_actor_nome: Annotated[str | None, Header(alias="x-actor-nome")] = None,
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
    await _log_admin_action("delete_all_versions", rel, x_actor_nome)
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
    x_actor_nome: Annotated[str | None, Header(alias="x-actor-nome")] = None,
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
    await _log_admin_action("restore", doc["filename"], x_actor_nome)
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
    # Indici MongoDB — evitano full collection scan sulle query più frequenti
    try:
        await uploads_col.create_index([("filename", 1), ("deleted_at", 1), ("uploaded_at", -1)])
        await assignments_col.create_index("nome")
        await pending_col.create_index([("status", 1), ("submitted_at", -1)])
        await solleciti_col.create_index([("pratica", 1), ("data_sollecito", 1)])
        await solleciti_col.create_index("tratta_id")
        await cantieri_col.create_index([("pratica_id", 1), ("ente", 1)])
        await cantieri_col.create_index("lotto")
        await sopralluoghi_col.create_index("codice_verbale")
        await gantt_rates_col.create_index("scope", unique=True)
        print("[enri-dashboard] Indici MongoDB verificati/creati")
    except Exception as e:
        print(f"[startup] creazione indici: {e}")
    # Backfill: ensure pre-existing upload records have a deleted_at field
    await uploads_col.update_many(
        {"deleted_at": {"$exists": False}}, {"$set": {"deleted_at": None}}
    )
    # Sync cantieri: crea automaticamente cantieri non_avviato per tratte LAVORABILE=SI
    # Eseguita in background (non awaitata) per non ritardare l'accettazione
    # delle richieste HTTP (es. /api/health) durante il boot dopo un cold-start.
    async def _startup_sync_cantieri():
        try:
            created = await _sync_cantieri()
            if created:
                asyncio.create_task(_push_cantieri_to_github())
        except Exception as e:
            print(f"[startup] _sync_cantieri: {e}")
    asyncio.create_task(_startup_sync_cantieri())


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

LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_SECONDS = int(os.environ.get("LOGIN_LOCKOUT_SECONDS", "300"))
_login_failures: dict[str, list[float]] = {}


def _check_login_rate_limit(nome_key: str) -> None:
    now = time.time()
    attempts = [t for t in _login_failures.get(nome_key, []) if now - t < LOGIN_LOCKOUT_SECONDS]
    _login_failures[nome_key] = attempts
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        wait = int(LOGIN_LOCKOUT_SECONDS - (now - attempts[0]))
        raise HTTPException(429, f"Troppi tentativi falliti. Riprova tra {max(wait, 1)}s")


def _record_login_failure(nome_key: str) -> None:
    _login_failures.setdefault(nome_key, []).append(time.time())


@app.post("/api/auth/login")
async def auth_login(payload: dict):
    """Proxy server-to-server verso Google Apps Script: il browser non vede
    più APPS_SCRIPT_SECRET, e il codice viene verificato per davvero (non solo
    al primo login, ma e' la base per ogni chiamata successiva tramite il token)."""
    nome = (payload or {}).get("nome", "").strip()
    codice = (payload or {}).get("codice", "").strip()
    if not nome or not codice:
        raise HTTPException(400, "Nome e codice sono richiesti")
    nome_key = nome.lower()
    _check_login_rate_limit(nome_key)
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
        _record_login_failure(nome_key)
        raise HTTPException(401, data.get("msg") or "Nome o codice non riconosciuti")

    _login_failures.pop(nome_key, None)
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
    """Log accessi — letto da MongoDB (access_logs). JSONBin/Apps Script non più usati per questo."""
    bin_id = (payload or {}).get("binId", "")
    doc = await access_logs_col.find_one({"_id": bin_id}) or {}
    return {"record": {
        "utenti":  doc.get("utenti", []),
        "accessi": doc.get("accessi", []),
    }}


@app.post("/api/logs/put")
async def logs_put(payload: dict, sess: dict = Depends(_require_session)):
    """Log accessi — scritto su MongoDB (access_logs). JSONBin/Apps Script non più usati per questo."""
    bin_id = (payload or {}).get("binId", "")
    data   = (payload or {}).get("data") or {}
    if not bin_id:
        raise HTTPException(400, "binId mancante")
    await access_logs_col.update_one(
        {"_id": bin_id},
        {"$set": {
            "utenti":  data.get("utenti", []),
            "accessi": data.get("accessi", []),
        }},
        upsert=True,
    )
    return {"ok": True}


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

# Cache in-process per _read_master_csv() — evita di riparsare lo stesso CSV
# più volte nella stessa request (e tra request ravvicinate finché non cambia versione).
_master_csv_cache: dict = {"key": None, "df": None}

async def _read_master_csv() -> "pd.DataFrame":
    """Read the current authoritative Master.csv (Mongo first, then disk seed).
    Cache in-process: la cache è valida finché la versione corrente (upload _id)
    non cambia, così le ~11 chiamate per request evitano di rileggere/riparsare
    lo stesso file da GridFS più volte."""
    global _detected_sep
    cur = await _current_upload(MASTER_FILENAME)
    cache_key = str(cur["_id"]) if cur else "disk-seed"

    cached = _master_csv_cache.get("key")
    if cached == cache_key and _master_csv_cache.get("df") is not None:
        return _master_csv_cache["df"]

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
    df = None
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            df = pd.read_csv(
                io.BytesIO(raw), sep=_detected_sep, dtype=str, keep_default_na=False,
                encoding=enc, on_bad_lines="warn",
            )
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if df is None:
        # Last resort: replace bad bytes
        df = pd.read_csv(io.BytesIO(raw), sep=_detected_sep, dtype=str, keep_default_na=False, encoding="utf-8", encoding_errors="replace")

    _master_csv_cache["key"] = cache_key
    _master_csv_cache["df"]  = df
    return df


QGIS_FILENAME      = "QGIS.geojson"
RIEPILOGO_FILENAME = "Riepilogo_progettazione.csv"


async def _read_riepilogo_csv() -> "pd.DataFrame | None":
    """Legge Riepilogo_progettazione.csv (Mongo se presente, altrimenti seed su
    disco). Usato per recuperare CLUSTER/PROVINCIA/COMUNE per TRATTA_ID: questi
    campi non esistono in Master.csv, solo in Riepilogo (ereditati da QGIS.geojson).
    Ritorna None se il file non e' ancora disponibile (fail-soft: i cantieri
    vengono comunque creati, solo senza questi campi)."""
    try:
        cur = await _current_upload(RIEPILOGO_FILENAME)
        if cur:
            raw = await _read_gridfs(cur["gridfs_id"])
        else:
            path = DATA_DIR / RIEPILOGO_FILENAME
            if not path.exists():
                return None
            raw = path.read_bytes()
        sep = _detect_sep(raw)
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                return pd.read_csv(
                    io.BytesIO(raw), sep=sep, dtype=str, keep_default_na=False,
                    encoding=enc, on_bad_lines="warn",
                )
            except (UnicodeDecodeError, UnicodeError):
                continue
        return pd.read_csv(io.BytesIO(raw), sep=sep, dtype=str, keep_default_na=False, encoding="utf-8", encoding_errors="replace")
    except Exception as e:
        print(f"[_read_riepilogo_csv] errore: {e}")
        return None


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
    "QTS.geojson":                 os.environ.get("GITHUB_QTS_PATH", "QTS.geojson"),
    "SED_classificato.geojson":    os.environ.get("GITHUB_SED_PATH", "SED_classificato.geojson"),
}


_GITHUB_PUSH_TIMES: dict[str, str] = {}   # label/basename -> ISO timestamp ultimo push riuscito

async def _push_to_github(file_bytes: bytes, path: str = None, label: str = None) -> None:
    """Aggiorna un file su GitHub via API (Master.csv, QGIS.geojson, Riepilogo_progettazione.csv, ...).
    In caso di conflitto sha (409 — qualcun altro ha scritto sullo stesso file nel frattempo,
    es. una modifica manuale in parallelo) rilegge lo sha aggiornato e riprova fino a 3 volte."""
    path  = path or GITHUB_CSV_PATH
    label = label or path
    if os.path.basename(path) in SENSITIVE_FILES:
        print(f"[GitHub] push disabilitato per file sensibile {path} — NON pubblicato sul repo pubblico (servito solo dal backend gated)")
        return
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
                    _GITHUB_PUSH_TIMES[os.path.basename(path)] = _now_iso()
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
    "INVIO PRELIMINARE": 3,
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


def _it_date_to_iso(s: str) -> str:
    """'17/03/2026' -> '2026-03-17'. Stringa vuota/non parsabile -> ''."""
    s = str(s or "").strip()
    if not s:
        return ""
    try:
        return datetime.strptime(s, "%d/%m/%Y").strftime("%Y-%m-%d")
    except Exception:
        return ""


def _iso_date_to_it(s: str) -> str:
    """'2026-03-17' -> '17/03/2026'. Stringa vuota/non parsabile -> ''."""
    s = str(s or "").strip()
    if not s:
        return ""
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return ""


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

        # Vincolante se il flag sull'AUT lo dichiara necessario OPPURE se
        # esistono comunque righe reali NULLA OSTA/ORDINANZA per la tratta:
        # il flag può essere disallineato nei dati sorgente, la pratica no.
        no_effettivo  = (need_no == "SI") or bool(no_latest)
        ord_effettivo = (need_ord == "SI") or bool(ord_latest)

        lavorabile = aut_ok
        if no_effettivo:
            lavorabile = lavorabile and no_ok
        if ord_effettivo:
            lavorabile = lavorabile and ord_ok

        motivi = []
        if not aut_ok:
            motivi.append("Manca autorizz")
        if no_effettivo and not no_ok:
            motivi.append("Manca nulla osta")
        if ord_effettivo and not ord_ok:
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
    asyncio.create_task(_prune_old_versions(filename))
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
    asyncio.create_task(_prune_old_versions(MASTER_FILENAME))
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


# ── Milestone di Progetto — dati serviti da qui (non più embedded in
# milestone.html) così il controllo di accesso per ruolo è reale lato server,
# non solo un redirect client-side aggirabile forzando localStorage.
MILESTONE_IMPRESE_ROWS = [
    {"lotto": "1A", "cluster": "1–3", "invio": "-", "ottenim": "-", "avvio": "31/05/2026", "p50": "-", "p90": "31/10/2026", "p100": "31/12/2026"},
    {"lotto": "1B", "cluster": "1", "invio": "-", "ottenim": "-", "avvio": "31/05/2026", "p50": "-", "p90": "31/10/2026", "p100": "31/12/2026"},
    {"lotto": "2A", "cluster": "2", "invio": "-", "ottenim": "-", "avvio": "31/05/2026", "p50": "-", "p90": "31/10/2026", "p100": "31/12/2026"},
    {"lotto": "2B", "cluster": "2", "invio": "-", "ottenim": "-", "avvio": "31/05/2026", "p50": "-", "p90": "31/10/2026", "p100": "31/12/2026"},
    {"lotto": "1", "cluster": "3", "invio": "30/09/2026", "ottenim": "30/09/2027", "avvio": "31/08/2026", "p50": "30/04/2027", "p90": "-", "p100": "15/11/2027"},
    {"lotto": "2", "cluster": "3", "invio": "30/09/2026", "ottenim": "30/09/2027", "avvio": "31/08/2026", "p50": "30/04/2027", "p90": "-", "p100": "15/11/2027"},
    {"lotto": "3", "cluster": "3", "invio": "30/09/2026", "ottenim": "30/09/2027", "avvio": "31/08/2026", "p50": "30/04/2027", "p90": "-", "p100": "15/11/2027"},
    {"lotto": "4", "cluster": "3", "invio": "30/09/2026", "ottenim": "30/09/2027", "avvio": "31/08/2026", "p50": "30/04/2027", "p90": "-", "p100": "15/11/2027"},
    {"lotto": "5", "cluster": "3", "invio": "30/09/2026", "ottenim": "30/09/2027", "avvio": "31/08/2026", "p50": "30/04/2027", "p90": "-", "p100": "15/11/2027"},
    {"lotto": "6", "cluster": "3–4", "invio": "30/09/2026", "ottenim": "30/09/2027", "avvio": "31/10/2026", "p50": "31/05/2027", "p90": "-", "p100": "15/11/2027"},
    {"lotto": "7", "cluster": "6–7", "invio": "30/09/2026", "ottenim": "30/09/2027", "avvio": "31/01/2027", "p50": "30/09/2027", "p90": "-", "p100": "31/03/2028"},
    {"lotto": "8", "cluster": "5", "invio": "30/09/2026", "ottenim": "30/09/2027", "avvio": "31/01/2027", "p50": "30/09/2027", "p90": "-", "p100": "31/07/2028"},
]

MILESTONE_CONTRACT_ROWS = [
    {"milestone": "Permits submission", "p50": "-", "p70": "-", "p100": "31/12/2026"},
    {"milestone": "Authorizations received", "p50": "-", "p70": "-", "p100": "31/12/2027"},
    {"milestone": "Cluster 1", "p50": "-", "p70": "31/12/2026", "p100": "30/04/2027"},
    {"milestone": "Cluster 2", "p50": "31/12/2026", "p70": "-", "p100": "31/05/2027"},
    {"milestone": "Cluster 3", "p50": "31/05/2027", "p70": "-", "p100": "31/03/2028"},
    {"milestone": "Cluster 4", "p50": "-", "p70": "-", "p100": "31/03/2028"},
    {"milestone": "Cluster 5", "p50": "-", "p70": "-", "p100": "30/06/2028"},
    {"milestone": "Cluster 6", "p50": "-", "p70": "-", "p100": "31/12/2028"},
    {"milestone": "Cluster 7", "p50": "-", "p70": "-", "p100": "31/12/2028"},
]


@app.get("/api/milestone")
async def get_milestone(sess: dict = Depends(_require_milestone_session)):
    """Dati di milestone.html. Accesso negato (403) al ruolo 'dl', in aggiunta
    al redirect client-side già presente sulla pagina — qui il controllo è
    reale perché il ruolo viene dal token firmato server-side."""
    return {"imprese": MILESTONE_IMPRESE_ROWS, "contract": MILESTONE_CONTRACT_ROWS}


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


@app.get("/api/imprese/master-sed")
async def impresa_master_sed(sess: dict = Depends(_require_session)):
    """GeoJSON (QGIS.geojson + SED_classificato.geojson) filtrati ai SOLI lotti
    assegnati all'impresa (nome dal token firmato). Le pagine Area Impresa usano
    questo endpoint invece di scaricare i file interi con i lotti di tutti i
    concorrenti. Stesso pattern di scoping di /api/imprese/pratiche."""
    doc = await _find_assignment(sess["nome"])
    if not doc or not doc.get("active", True):
        raise HTTPException(404, "Impresa non autorizzata")
    lotti = {_lotto_from_source(l) for l in doc.get("lotti", []) if str(l).strip()}

    def _scope(geo: "dict | None") -> dict:
        if not geo or not isinstance(geo.get("features"), list):
            return {"type": "FeatureCollection", "features": []}
        feats = [
            f for f in geo["features"]
            if str((f.get("properties") or {}).get("LOTTO", "")).strip().upper() in lotti
        ]
        out = {k: v for k, v in geo.items() if k != "features"}
        out.setdefault("type", "FeatureCollection")
        out["features"] = feats
        return out

    qgis = _scope(await _read_current_geojson("QGIS.geojson"))
    sed = _scope(await _read_current_geojson("SED_classificato.geojson"))
    return {"qgis": qgis, "sed": sed, "lotti": sorted(lotti)}


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

    # Tagga le eventuali note con [IMPRESA] cosi' index.html/admin.html possono
    # distinguerle dalle note admin (v. _tag_note / add_admin_note)
    if typ == "update":
        for ch in changes:
            fields = ch.get("fields") or {}
            if str(fields.get("NOTE") or "").strip():
                fields["NOTE"] = _tag_note(fields["NOTE"], "IMPRESA")
    elif typ == "new":
        for row in changes:
            if isinstance(row, dict) and str(row.get("NOTE") or "").strip():
                row["NOTE"] = _tag_note(row["NOTE"], "IMPRESA")

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
                # NB: DATA_ULTIMA_MODIFICA è testo DD/MM/YYYY — ordinare come stringa
                # è sbagliato (es. "26/03/2026" > "02/07/2026" lessicograficamente pur
                # essendo antecedente). Serve un parsing esplicito a datetime.
                _dum_dt = pd.to_datetime(rows["DATA_ULTIMA_MODIFICA"], format="%d/%m/%Y", errors="coerce")
                row = rows.loc[_dum_dt.sort_values(ascending=False, na_position="last").index[0]]
                ch["_stato_attuale"]     = str(row.get("STATO_PERMESSO", "") or "")
                ch["_data_richiesta"]    = str(row.get("DATA_RICHIESTA", "") or "")
                ch["_data_ult_mod"]      = str(row.get("DATA_ULTIMA_MODIFICA", "") or "")
                ch["_data_approvazione"] = str(row.get("DATA_APPROVAZIONE", "") or "")
                ch["_nulla_osta"] = str(row.get("NULLA OSTA NECESSARIO", "") or "").strip()
                # Ricava lotto da Source.Name (es. "Lotto1.xlsx" → "1A", "Lotto3.xlsx" → "3A")
                src = str(row.get("Source.Name", "") or "")
                import re as _re
                lm = _re.search(r'[Ll]otto\s*(\w+)', src)
                ch["_lotto"] = lm.group(1) if lm else ""
    except Exception as e:
        print(f"[pending-updates] enrich error: {e}")

    return {"submissions": items, "count": len(items)}


def _apply_changes_to_df(df, submission: dict) -> tuple:
    """Returns (new_df, summary). Raises HTTPException on errors.

    Comportamento di default (in_place=False, usato da imprese.html e dalle
    NOTE admin): copia l'ultima riga e inserisce una nuova riga subito dopo,
    per mantenere lo storico di chi ha scritto cosa e quando.

    in_place=True (usato dalla correzione dati admin — stato/date/N_SED):
    sovrascrive i campi direttamente sulla riga esistente, senza duplicarla.
    Una correzione di un dato inesatto non è un evento di business da
    storicizzare come le note o gli aggiornamenti di stato dell'impresa: se
    duplicassimo la riga, il dato sbagliato originale resterebbe comunque nel
    CSV (anche se ignorato dalle pagine che leggono solo l'ultima riga)."""
    typ = submission["type"]
    changes = submission["changes"]
    in_place = bool(submission.get("in_place"))
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
            # Auto-set DATA_ULTIMA_MODIFICA se non fornita (solo su cambio stato)
            if "DATA_ULTIMA_MODIFICA" not in fields and "STATO_PERMESSO" in fields:
                fields["DATA_ULTIMA_MODIFICA"] = datetime.now(timezone.utc).strftime("%d/%m/%Y")
            # DATA_UPDATE: qualsiasi tocco dell'impresa sulla pratica (stato, nota, o altro campo), non solo cambio stato
            if "DATA_UPDATE" in df.columns and "DATA_UPDATE" not in fields:
                fields["DATA_UPDATE"] = datetime.now(timezone.utc).strftime("%d/%m/%Y")
            last_idx = idx[-1]
            if in_place:
                # Correzione: sovrascrive i campi sulla riga esistente, nessuna nuova riga
                if ch.get("preserve_note_tag") and "NOTE" in fields and fields["NOTE"]:
                    # Non cambiare l'autore mostrato (RETELIT/IMPRESA): si sta
                    # solo correggendo il testo di una nota già esistente.
                    existing = str(df.loc[last_idx, "NOTE"]) if "NOTE" in df.columns else ""
                    m = _NOTE_TAG_RE.match(existing or "")
                    tag_prefix = m.group(0) if m else ""
                    fields["NOTE"] = f"{tag_prefix}{fields['NOTE']}"
                for col, val in fields.items():
                    if col in df.columns:
                        df.loc[last_idx, col] = str(val)
                summary["updated"] += 1
                continue
            # Copia l'ultima riga esistente e inserisce la nuova SUBITO DOPO
            # in modo da mantenere le righe dello stesso iter vicine
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
            if "DATA_UPDATE" in df.columns and not new_row.get("DATA_UPDATE"):
                new_row["DATA_UPDATE"] = datetime.now(timezone.utc).strftime("%d/%m/%Y")
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            summary["added"] += 1
    return df, summary


@app.post("/api/admin/regenerate-derived")
async def regenerate_derived(
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    """Rigenera QGIS.geojson + Riepilogo_progettazione.csv dal Master.csv corrente
    senza modificarlo — utile dopo un fix a _compute_tratta_summary per applicare
    la nuova logica ai dati già presenti, senza dover re-uploadare Master.csv."""
    _check_token(x_upload_token or token_q)
    df = await _read_master_csv()
    result = await _regenerate_derived_files(df, note="force regenerate (manual)")
    return {"ok": True, "result": result}


@app.post("/api/admin/backfill-data-update-solleciti")
async def backfill_data_update_solleciti(
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    """One-off (rev.147): valorizza DATA_UPDATE per le tratte con solleciti registrati
    PRIMA del fix del bug di matching (mask rotta su 'pratica' → touch mai avvenuto).
    Per ogni tratta_id in 'solleciti', prende la data_sollecito più recente e la usa
    come DATA_UPDATE se è più recente di quella già presente (o se assente).
    Non tocca le tratte senza solleciti. Idempotente: rieseguibile senza effetti collaterali
    (non peggiora mai una DATA_UPDATE già più recente di quella dei solleciti)."""
    _check_token(x_upload_token or token_q)

    def _parse_it(s: str):
        try:
            return datetime.strptime(str(s).strip(), "%d/%m/%Y")
        except Exception:
            return None

    # Max data_sollecito per tratta_id
    max_per_tratta: dict[str, datetime] = {}
    async for d in solleciti_col.find({}, {"tratta_id": 1, "data_sollecito": 1}):
        tid = str(d.get("tratta_id", "")).strip()
        dt = _parse_it(d.get("data_sollecito", ""))
        if not tid or not dt:
            continue
        if tid not in max_per_tratta or dt > max_per_tratta[tid]:
            max_per_tratta[tid] = dt

    async with _master_csv_lock:
        df = await _read_master_csv()
        if "TRATTA_ID" not in df.columns:
            return {
                "ok": False,
                "error": "colonna TRATTA_ID assente da Master.csv",
                "colonne_trovate": df.columns.tolist(),
            }
        if "DATA_UPDATE" not in df.columns:
            df["DATA_UPDATE"] = ""
            column_created = True
        else:
            column_created = False

        touched = []
        col_tratta = df["TRATTA_ID"].astype(str).str.strip()
        for tid, sol_dt in max_per_tratta.items():
            mask = col_tratta == tid
            if not mask.any():
                continue
            existing_raw = df.loc[mask, "DATA_UPDATE"].iloc[0]
            existing_dt = _parse_it(existing_raw)
            if existing_dt and existing_dt >= sol_dt:
                continue  # già più recente (o uguale), non sovrascrivere
            new_val = sol_dt.strftime("%d/%m/%Y")
            df.loc[mask, "DATA_UPDATE"] = new_val
            touched.append({"tratta_id": tid, "data_update": new_val})

        if touched or column_created:
            await _write_master_csv(
                df,
                note=f"Backfill one-off rev.147: DATA_UPDATE da storico solleciti ({len(touched)} tratte)"
                     + (" + colonna creata" if column_created else ""),
            )

    return {"ok": True, "touched": len(touched), "column_created": column_created, "detail": touched}


@app.put("/api/admin/pending-updates/{sub_id}")
async def edit_pending(
    sub_id: str,
    payload: dict,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
    x_actor_nome: Annotated[str | None, Header(alias="x-actor-nome")] = None,
):
    """Corregge i dati (`changes`) di una submission impresa ancora in stato 'pending',
    prima di approvarla — es. errori di digitazione o dati di test inviati per sbaglio.
    Non è modificabile una submission già approvata/rifiutata (usare direttamente
    la tabella Cantieri/Master per correzioni post-approvazione)."""
    _check_token(x_upload_token or token_q)
    try:
        oid = ObjectId(sub_id)
    except Exception:
        raise HTTPException(400, "Invalid id")
    sub = await pending_col.find_one({"_id": oid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    if sub.get("status") != "pending":
        raise HTTPException(409, f"Non modificabile: già {sub.get('status')}")
    changes = payload.get("changes")
    if not isinstance(changes, list):
        raise HTTPException(400, "'changes' deve essere una lista")
    await pending_col.update_one(
        {"_id": oid},
        {"$set": {"changes": changes, "edited_at": _now_iso(), "edited_by": x_actor_nome or "admin"}},
    )
    await _log_admin_action("edit_pending_update", sub_id, x_actor_nome)
    return {"ok": True}


@app.post("/api/admin/pending-updates/{sub_id}/approve")
async def approve_pending(
    sub_id: str,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
    x_actor_nome: Annotated[str | None, Header(alias="x-actor-nome")] = None,
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
    async with _master_csv_lock:
        df = await _read_master_csv()
        new_df, summary = _apply_changes_to_df(df, sub)
        note = f"Submission {sub_id} from {sub['nome']} ({sub['type']})"
        upload_id = await _write_master_csv(new_df, note=note)
    reviewer = x_actor_nome or "admin"
    await pending_col.update_one(
        {"_id": oid},
        {"$set": {"status": "approved", "reviewed_at": _now_iso(), "reviewed_by": reviewer, "applied_upload_id": upload_id, "summary": summary}},
    )
    await _log_admin_action("approve_pending_update", sub_id, x_actor_nome)
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
    x_actor_nome: Annotated[str | None, Header(alias="x-actor-nome")] = None,
):
    _check_token(x_upload_token or token_q)
    try:
        oid = ObjectId(sub_id)
    except Exception:
        raise HTTPException(400, "Invalid id")
    note = ((payload or {}).get("note") or "").strip()
    reviewer = x_actor_nome or "admin"
    res = await pending_col.update_one(
        {"_id": oid, "status": "pending"},
        {"$set": {"status": "rejected", "reviewed_at": _now_iso(), "reviewed_by": reviewer, "reviewed_note": note}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Submission not found or not pending")
    await _log_admin_action("reject_pending_update", sub_id, x_actor_nome)
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# NOTE ADMIN — l'admin annota una pratica con lo stesso meccanismo delle imprese
# (stessa pipeline _apply_changes_to_df: nuova riga copiata dall'ultima esistente,
# NOTE valorizzata, DATA_UPDATE stampata). Distinguibile da una nota impresa via
# prefisso [RETELIT]/[IMPRESA] (_tag_note) — index.html/admin.html lo parsano
# per mostrare l'etichetta "Retelit"/"Impresa" e nascondono il prefisso grezzo.
# ─────────────────────────────────────────────────────────────────────────────
_NOTE_TAG_RE = re.compile(r"^\[(RETELIT|IMPRESA)\]\s*")


def _tag_note(note: str, tag: str) -> str:
    """Prefissa una NOTE con [RETELIT]/[IMPRESA] per distinguerne l'autore in
    index.html/admin.html. Rimuove un eventuale tag preesistente prima di
    riapplicarlo, cosi' un admin che modifica una submission impresa (PUT
    /api/admin/pending-updates/{id}) non produce prefissi impilati."""
    note = (note or "").strip()
    if not note:
        return note
    note = _NOTE_TAG_RE.sub("", note)
    return f"[{tag}] {note}"
@app.get("/api/admin/pratiche-search")
async def search_pratiche_admin(
    q: str = "",
    limit: int = 800,
    sess: dict = Depends(_require_staff_session),
):
    """Cerca pratiche in Master.csv per TRATTA_ID / codice pratica / ente / lotto,
    deduplicate all'ultima riga (Master.csv è append-only, storico incluso).
    Usata da admin.html per trovare la pratica a cui aggiungere una nota."""
    df = await _read_master_csv()
    if df is None or df.empty:
        return {"results": []}
    needed = {"TRATTA_ID", "ENTE", "TIPO_PERMESSO", "PRATICA", "Source.Name", "STATO_PERMESSO", "NOTE"}
    missing = needed - set(df.columns)
    if missing:
        return {"results": [], "error": f"Colonne mancanti in Master.csv: {sorted(missing)}"}
    work = df.fillna("")
    q_norm = q.strip().lower()
    if q_norm:
        hay = (
            work["TRATTA_ID"].astype(str) + " " + work["PRATICA"].astype(str) + " " +
            work["ENTE"].astype(str) + " " + work["Source.Name"].astype(str)
        ).str.lower()
        work = work[hay.str.contains(re.escape(q_norm), na=False)]
    if work.empty:
        return {"results": []}
    work = work.assign(_key=(
        work["TRATTA_ID"].astype(str).str.strip() + "|" + work["ENTE"].astype(str).str.strip() + "|" +
        work["TIPO_PERMESSO"].astype(str).str.strip() + "|" + work["PRATICA"].astype(str).str.strip()
    ))
    latest = work.drop_duplicates(subset="_key", keep="last")
    # Righe senza numero PRATICA non sono rappresentabili come "pratica" e non
    # possono ricevere note (add_admin_note richiede una pratica valida) →
    # escluse dalla tabella, come richiesto dall'utente.
    latest = latest[latest["PRATICA"].astype(str).str.strip() != ""]
    # Le pratiche già OTTENUTO non necessitano più di note (emesse, chiuse) →
    # escluse dalla tabella, come richiesto dall'utente.
    latest = latest[latest["STATO_PERMESSO"].astype(str).str.strip().str.upper() != "OTTENUTO"]
    if latest.empty:
        return {"results": []}
    # Raggruppa per pratica (ENTE+TIPO_PERMESSO+PRATICA): una pratica AUTORIZZAZIONE
    # copre piu' tratte, e la nota va condivisa su tutte come fa imprese.html
    # (PR_SIBLINGS).
    latest = latest.assign(_lotto=latest["Source.Name"].apply(_lotto_from_source))
    latest = latest.assign(_gkey=(
        latest["ENTE"].astype(str).str.strip() + "|" + latest["TIPO_PERMESSO"].astype(str).str.strip() + "|" +
        latest["PRATICA"].astype(str).str.strip() + "|" + latest["_lotto"]
    ))
    PREFIX = {"AUTORIZZAZIONE": "AUT", "NULLA OSTA": "NO", "ORDINANZA": "ORD"}
    out = []
    for _, grp in latest.groupby("_gkey", sort=False):
        with_note = grp[grp["NOTE"].astype(str).str.strip() != ""]
        rep = with_note.iloc[-1] if not with_note.empty else grp.iloc[0]
        tipo = str(rep.get("TIPO_PERMESSO", "")).strip()
        pratica_num = str(rep.get("PRATICA", "")).strip()
        lotto = rep.get("_lotto", "") or _lotto_from_source(rep.get("Source.Name", ""))
        pref = PREFIX.get(tipo, (tipo[:3] or "").upper())
        tratta_ids = sorted({t for t in grp["TRATTA_ID"].astype(str).str.strip() if t})
        out.append({
            "tratta_ids": tratta_ids,
            "tratta_id": tratta_ids[0] if tratta_ids else "",
            "n_tratte": len(tratta_ids),
            "ente": str(rep.get("ENTE", "")).strip(),
            "tipo_permesso": tipo,
            "pratica": pratica_num,
            "lotto": lotto,
            "codice": f"{pref}/{pratica_num}/{lotto}" if pratica_num else "",
            "stato_permesso": str(rep.get("STATO_PERMESSO", "")).strip(),
            "note_attuale": str(rep.get("NOTE", "")).strip(),
            "data_richiesta": _it_date_to_iso(rep.get("DATA_RICHIESTA", "")),
            "data_prevista_rilascio": _it_date_to_iso(rep.get("DATA_PREVISTA_RILASCIO", "")),
            "data_approvazione": _it_date_to_iso(rep.get("DATA_APPROVAZIONE", "")),
            "n_sed": str(rep.get("N_SED", "")).strip(),
        })
    out.sort(key=lambda x: x["codice"])
    return {"results": out[:max(1, min(limit, 800))]}


PRATICA_STATO_VALUES = [
    "IN ATTESA", "IN REDAZIONE", "IN FIRMA RDS", "INVIO PRELIMINARE",
    "INVIATO", "PROTOCOLLATO", "NECESSARIA INTEGRAZIONE",
    "IN REDAZIONE INTEGRAZIONE", "PROTOCOLLATO INTEGRAZIONE",
    "OTTENUTO", "NO COMPETENZA",
]


@app.post("/api/admin/pratiche/update")
async def update_admin_pratica(
    payload: dict,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
    x_actor_nome: Annotated[str | None, Header(alias="x-actor-nome")] = None,
):
    """Modifica diretta dei dati di una pratica (stato, date, nota, n. SED)
    esattamente come farebbe un'impresa da imprese.html, attraverso
    _apply_changes_to_df: nessuna approvazione richiesta (l'admin è già
    l'autorità), scrittura diretta su Master.csv, applicata a TUTTE le tratte
    della pratica (stessa pipeline già usata da add_admin_note, generalizzata
    a più campi)."""
    _check_token(x_upload_token or token_q)
    p = payload or {}
    ente = str(p.get("ente") or "").strip()
    tipo = str(p.get("tipo_permesso") or "").strip()
    pratica = str(p.get("pratica") or "").strip()
    tratte = [str(t).strip() for t in (p.get("tratta_ids") or []) if str(t).strip()]
    single = str(p.get("tratta_id") or "").strip()
    if single and single not in tratte:
        tratte.append(single)
    if not tratte:
        raise HTTPException(400, "tratta_ids (o tratta_id) è obbligatorio")

    raw = p.get("fields") or {}
    mode = str(p.get("mode") or "").strip().lower()
    if mode not in ("data", "note"):
        # Fallback per eventuali chiamate legacy senza mode esplicito
        mode = "note" if set(raw.keys()) <= {"note"} else "data"

    fields: dict = {}
    if mode == "data":
        if "stato_permesso" in raw:
            v = str(raw["stato_permesso"] or "").strip()
            if v and v not in PRATICA_STATO_VALUES:
                raise HTTPException(400, f"stato_permesso non valido: {v}")
            if v:
                fields["STATO_PERMESSO"] = v
        if "data_richiesta" in raw:
            v = _iso_date_to_it(raw["data_richiesta"])
            if raw["data_richiesta"] and not v:
                raise HTTPException(400, "data_richiesta non valida")
            fields["DATA_RICHIESTA"] = v
        if "data_prevista_rilascio" in raw:
            v = _iso_date_to_it(raw["data_prevista_rilascio"])
            if raw["data_prevista_rilascio"] and not v:
                raise HTTPException(400, "data_prevista_rilascio non valida")
            fields["DATA_PREVISTA_RILASCIO"] = v
        if "data_approvazione" in raw:
            v = _iso_date_to_it(raw["data_approvazione"])
            if raw["data_approvazione"] and not v:
                raise HTTPException(400, "data_approvazione non valida")
            fields["DATA_APPROVAZIONE"] = v
        if "n_sed" in raw:
            fields["N_SED"] = str(raw["n_sed"] or "").strip()
        if "note" in raw:
            # Correzione diretta della nota esistente: NON si ritagga come
            # RETELIT — chi l'ha scritta in origine (Impresa o Retelit) resta
            # l'autore mostrato, si corregge solo il testo.
            # _apply_changes_to_df preserva il tag esistente sulla riga.
            fields["NOTE"] = str(raw["note"] or "").strip()
        if not fields:
            raise HTTPException(400, "Nessun campo valido da aggiornare")
        in_place = True
        preserve_note_tag = True
    else:
        text = str(raw.get("note") or "").strip()
        if not text:
            raise HTTPException(400, "note è obbligatoria")
        # Nuova nota: sempre taggata RETELIT, sempre una nuova riga di storico
        fields["NOTE"] = _tag_note(text, "RETELIT")
        in_place = False
        preserve_note_tag = False

    submission = {
        "type": "update",
        "in_place": in_place,
        "changes": [{
            "tratta_id": t, "ente": ente, "tipo_permesso": tipo,
            "original_pratica": pratica,
            "preserve_note_tag": preserve_note_tag,
            "fields": dict(fields),
        } for t in tratte],
    }
    async with _master_csv_lock:
        df = await _read_master_csv()
        new_df, summary = _apply_changes_to_df(df, submission)
        if summary.get("updated", 0) == 0:
            raise HTTPException(404, "Nessuna riga trovata per TRATTA_ID/ENTE/TIPO_PERMESSO/PRATICA indicati")
        reviewer = x_actor_nome or "admin"
        upload_id = await _write_master_csv(new_df, note=f"Admin update by {reviewer} on {len(tratte)} tratta/e ({tratte[0]}{'…' if len(tratte)>1 else ''})")
    await _log_admin_action("update_pratica", f"{'+'.join(tratte)}|{ente}|{tipo}|{pratica}", x_actor_nome)
    return {"ok": True, "summary": summary, "new_upload_id": upload_id}


@app.get("/api/admin/pratiche/note-history")
async def pratica_note_history(
    ente: str, tipo_permesso: str, pratica: str, lotto: str,
    sess: dict = Depends(_require_staff_session),
):
    """Storico di TUTTE le note inserite nel tempo su una pratica, raccolte da OGNI
    riga grezza di Master.csv (non solo l'ultima per tratta) e deduplicate per
    (testo, data) — stessa logica di praticaNotesRaw in index.html. Serve a mostrare
    anche le note vecchie di più caricamenti fa, non solo quella corrente, così da
    poterle correggere con /pratiche/note/correct."""
    df = await _read_master_csv()
    if df is None or df.empty:
        return {"notes": []}
    needed = {"ENTE", "TIPO_PERMESSO", "PRATICA", "Source.Name", "NOTE"}
    if needed - set(df.columns):
        return {"notes": []}
    work = df.fillna("")
    mask = (
        (work["ENTE"].astype(str).str.strip() == ente) &
        (work["TIPO_PERMESSO"].astype(str).str.strip() == tipo_permesso) &
        (work["PRATICA"].astype(str).str.strip() == pratica)
    )
    work = work[mask]
    work = work[work["Source.Name"].apply(_lotto_from_source) == lotto]
    has_stato = "STATO_PERMESSO" in work.columns
    has_tratta = "TRATTA_ID" in work.columns
    # Replica esatta di effectiveDateRaw in index.html (praticaNotesRaw): se una riga è un
    # aggiornamento SOLO-NOTA (stessa tratta, stesso STATO_PERMESSO, stessa DATA_ULTIMA_MODIFICA
    # già vista prima), la vera data dell'evento è DATA_UPDATE, non DATA_ULTIMA_MODIFICA.
    # Senza questo fallback due note distinte con stessa dum collassano sulla stessa data.
    stato_seen = []  # lista di {stato, date, tratta_ids: set()}
    seen = set()
    notes = []
    for _, row in work.iterrows():
        stato = str(row.get("STATO_PERMESSO", "")).strip() if has_stato else ""
        tratta = str(row.get("TRATTA_ID", "")).strip() if has_tratta else ""
        dum = str(row.get("DATA_ULTIMA_MODIFICA", "")).strip()
        data_update = str(row.get("DATA_UPDATE", "")).strip()
        note_date_raw = dum or data_update
        is_continuation = any(
            s["stato"] == stato and s["date"] == note_date_raw and tratta in s["tratta_ids"]
            for s in stato_seen
        )
        effective_date = data_update if (is_continuation and data_update) else note_date_raw
        merge_target = next((s for s in stato_seen if s["stato"] == stato and s["date"] == effective_date), None)
        if merge_target is not None:
            merge_target["tratta_ids"].add(tratta)
        else:
            stato_seen.append({"stato": stato, "date": effective_date, "tratta_ids": {tratta}})

        note = str(row.get("NOTE", "")).strip()
        if not note:
            continue
        key = (note, effective_date)
        if key in seen:
            continue
        seen.add(key)
        notes.append({"note": note, "date": effective_date})
    notes.sort(key=lambda n: _it_date_to_iso(n["date"]), reverse=True)
    return {"notes": notes}


@app.post("/api/admin/pratiche/note/correct")
async def correct_pratica_note(
    payload: dict,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
    x_actor_nome: Annotated[str | None, Header(alias="x-actor-nome")] = None,
):
    """Corregge il testo di una nota STORICA (anche non l'ultima) su una pratica.
    A differenza di /pratiche/update (mode=data), che tocca solo l'ultima riga per
    tratta, qui si cerca per testo+data della nota su TUTTE le righe grezze di
    Master.csv del gruppo pratica (ente+tipo+pratica+lotto) — la stessa nota storica
    è duplicata su ogni tratta attraversata dalla pratica in quell'evento, quindi la
    correzione va propagata su tutte, non solo sull'ultima riga di ciascuna tratta."""
    _check_token(x_upload_token or token_q)
    p = payload or {}
    ente = str(p.get("ente") or "").strip()
    tipo = str(p.get("tipo_permesso") or "").strip()
    pratica = str(p.get("pratica") or "").strip()
    lotto = str(p.get("lotto") or "").strip()
    old_note = str(p.get("old_note") or "").strip()
    old_date = str(p.get("old_date") or "").strip()
    new_text = str(p.get("new_note") or "").strip()
    if not (ente and tipo and pratica and lotto and old_note):
        raise HTTPException(400, "ente, tipo_permesso, pratica, lotto e old_note sono obbligatori")
    if not new_text:
        raise HTTPException(400, "new_note è obbligatoria")

    async with _master_csv_lock:
        df = await _read_master_csv()
        if df is None or df.empty:
            raise HTTPException(404, "Master.csv non disponibile")
        needed = {"ENTE", "TIPO_PERMESSO", "PRATICA", "Source.Name", "NOTE"}
        if needed - set(df.columns):
            raise HTTPException(400, "Colonne mancanti in Master.csv")
        mask = (
            (df["ENTE"].astype(str).str.strip() == ente) &
            (df["TIPO_PERMESSO"].astype(str).str.strip() == tipo) &
            (df["PRATICA"].astype(str).str.strip() == pratica) &
            (df["Source.Name"].apply(_lotto_from_source) == lotto) &
            (df["NOTE"].astype(str).str.strip() == old_note)
        )
        if old_date:
            cols_present = [c for c in ("DATA_ULTIMA_MODIFICA", "DATA_UPDATE") if c in df.columns]
            if cols_present:
                date_mask = df[cols_present[0]].astype(str).str.strip() == old_date
                for c in cols_present[1:]:
                    date_mask = date_mask | (df[c].astype(str).str.strip() == old_date)
                mask = mask & date_mask
        idx = df.index[mask].tolist()
        if not idx:
            raise HTTPException(404, "Nessuna riga trovata per quella nota/data — verifica che il testo combaci esattamente")
        # Preserva il tag [RETELIT]/[IMPRESA] esistente su ciascuna riga: si corregge
        # solo il testo, non l'autore mostrato.
        for i in idx:
            existing = str(df.loc[i, "NOTE"]) if "NOTE" in df.columns else ""
            m = _NOTE_TAG_RE.match(existing or "")
            tag_prefix = m.group(0) if m else ""
            df.loc[i, "NOTE"] = f"{tag_prefix}{new_text}"
        reviewer = x_actor_nome or "admin"
        upload_id = await _write_master_csv(
            df, note=f"Admin note-correct by {reviewer} on {len(idx)} riga/e ({ente}|{tipo}|{pratica}|{lotto})"
        )
    await _log_admin_action("correct_pratica_note", f"{ente}|{tipo}|{pratica}|{lotto}", x_actor_nome)
    return {"ok": True, "updated": len(idx), "new_upload_id": upload_id}


@app.post("/api/admin/pratiche/note/delete")
async def delete_pratica_note(
    payload: dict,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
    x_actor_nome: Annotated[str | None, Header(alias="x-actor-nome")] = None,
):
    """Elimina (svuota) una nota STORICA su tutte le righe grezze di Master.csv che
    la condividono — stesso matching di /pratiche/note/correct (ente+tipo+pratica+
    lotto+testo[+data]), ma il campo NOTE viene azzerato invece che riscritto. Non
    rimuove la riga fisica del CSV (Master.csv resta append-only), solo il testo
    della nota su quell'evento storico."""
    _check_token(x_upload_token or token_q)
    p = payload or {}
    ente = str(p.get("ente") or "").strip()
    tipo = str(p.get("tipo_permesso") or "").strip()
    pratica = str(p.get("pratica") or "").strip()
    lotto = str(p.get("lotto") or "").strip()
    old_note = str(p.get("old_note") or "").strip()
    old_date = str(p.get("old_date") or "").strip()
    if not (ente and tipo and pratica and lotto and old_note):
        raise HTTPException(400, "ente, tipo_permesso, pratica, lotto e old_note sono obbligatori")

    async with _master_csv_lock:
        df = await _read_master_csv()
        if df is None or df.empty:
            raise HTTPException(404, "Master.csv non disponibile")
        needed = {"ENTE", "TIPO_PERMESSO", "PRATICA", "Source.Name", "NOTE"}
        if needed - set(df.columns):
            raise HTTPException(400, "Colonne mancanti in Master.csv")
        mask = (
            (df["ENTE"].astype(str).str.strip() == ente) &
            (df["TIPO_PERMESSO"].astype(str).str.strip() == tipo) &
            (df["PRATICA"].astype(str).str.strip() == pratica) &
            (df["Source.Name"].apply(_lotto_from_source) == lotto) &
            (df["NOTE"].astype(str).str.strip() == old_note)
        )
        if old_date:
            cols_present = [c for c in ("DATA_ULTIMA_MODIFICA", "DATA_UPDATE") if c in df.columns]
            if cols_present:
                date_mask = df[cols_present[0]].astype(str).str.strip() == old_date
                for c in cols_present[1:]:
                    date_mask = date_mask | (df[c].astype(str).str.strip() == old_date)
                mask = mask & date_mask
        idx = df.index[mask].tolist()
        if not idx:
            raise HTTPException(404, "Nessuna riga trovata per quella nota/data — verifica che il testo combaci esattamente")
        for i in idx:
            df.loc[i, "NOTE"] = ""
        reviewer = x_actor_nome or "admin"
        upload_id = await _write_master_csv(
            df, note=f"Admin note-delete by {reviewer} on {len(idx)} riga/e ({ente}|{tipo}|{pratica}|{lotto})"
        )
    await _log_admin_action("delete_pratica_note", f"{ente}|{tipo}|{pratica}|{lotto}", x_actor_nome)
    return {"ok": True, "updated": len(idx), "new_upload_id": upload_id}



_POL_CONV_ALLOWED_FIELDS = {"CONVENZIONE", "POLIZZA"}
_POL_CONV_ALLOWED_VALUES = {"NECESSARIA", "RICHIESTA RDS", "INVIATA", "EMESSA", ""}
# SI/NO sono i soli valori che l'impresa può scrivere (checkbox "è necessaria?"),
# usati anche come default nelle esportazioni esterne (QGIS/Excel) che alimentano
# nuovi upload di Master.csv. NECESSARIA/RICHIESTA RDS/INVIATA/EMESSA sono invece
# lo stato di workflow avanzato manualmente da polizze_convenzioni.html: un nuovo
# upload che porta solo SI/NO per una pratica già oltre NECESSARIA non deve mai
# retrocederla (bug segnalato dall'utente: polizze/convenzioni tornate a
# "NECESSARIA" dopo un caricamento di Master.csv aggiornato).
_POL_CONV_RANK = {"": 0, "NO": 0, "SI": 1, "NECESSARIA": 1, "RICHIESTA RDS": 2, "INVIATA": 3, "EMESSA": 4}


def _preserve_pol_conv_state(old_df: "pd.DataFrame", new_df: "pd.DataFrame") -> int:
    """Prima di sostituire Master.csv con un nuovo upload, riporta nel nuovo file
    lo stato CONVENZIONE/POLIZZA più avanzato già presente nel file corrente, per
    ogni lotto+pratica, se il nuovo file porterebbe una retrocessione (es. SI/NO
    grezzo dalla fonte esterna sopra un INVIATA/EMESSA impostato a mano). Non tocca
    nulla se il nuovo valore è uguale o più avanzato. Ritorna il numero di celle
    corrette, per il log di audit."""
    def _extract_lotto(src) -> str:
        return str(src).replace(".xlsx", "").replace(".xls", "").replace("Lotto ", "").strip().upper()

    src_col_old = next((c for c in old_df.columns if c.strip().upper().replace(".", "").replace(" ", "") in {"SOURCENAME", "SOURCE_NAME"}), None)
    prat_col_old = next((c for c in old_df.columns if c.strip().upper() == "PRATICA"), None)
    if src_col_old is None or prat_col_old is None:
        return 0

    # stato più avanzato già visto nel file corrente, per (lotto, pratica, campo)
    best: dict[tuple, str] = {}
    for col_name, field in (("CONVENZIONE", "CONVENZIONE"), ("POLIZZA", "POLIZZA")):
        real_old = next((c for c in old_df.columns if c.strip().upper() == col_name), None)
        if real_old is None:
            continue
        for _, row in old_df.iterrows():
            val = str(row.get(real_old, "")).strip().upper()
            if _POL_CONV_RANK.get(val, 0) < 2:  # sotto RICHIESTA RDS: niente da preservare
                continue
            lotto = _extract_lotto(row.get(src_col_old, ""))
            pratica = str(row.get(prat_col_old, "")).strip()
            if not lotto or not pratica:
                continue
            key = (lotto, pratica, field)
            if key not in best or _POL_CONV_RANK.get(val, 0) > _POL_CONV_RANK.get(best[key], 0):
                best[key] = val

    if not best:
        return 0

    src_col_new = next((c for c in new_df.columns if c.strip().upper().replace(".", "").replace(" ", "") in {"SOURCENAME", "SOURCE_NAME"}), None)
    prat_col_new = next((c for c in new_df.columns if c.strip().upper() == "PRATICA"), None)
    if src_col_new is None or prat_col_new is None:
        return 0

    touched = 0
    for col_name, field in (("CONVENZIONE", "CONVENZIONE"), ("POLIZZA", "POLIZZA")):
        real_new = next((c for c in new_df.columns if c.strip().upper() == col_name), None)
        if real_new is None:
            continue
        lotti_new = new_df[src_col_new].astype(str).apply(_extract_lotto)
        pratiche_new = new_df[prat_col_new].astype(str).str.strip()
        for i in new_df.index:
            key = (lotti_new.loc[i], pratiche_new.loc[i], field)
            preserved = best.get(key)
            if preserved is None:
                continue
            cur = str(new_df.at[i, real_new]).strip().upper()
            if _POL_CONV_RANK.get(cur, 0) < _POL_CONV_RANK.get(preserved, 0):
                new_df.at[i, real_new] = preserved
                touched += 1
    return touched


@app.get("/api/admin/polizze-convenzioni/data-richiesta")
async def get_pol_conv_date_richiesta(sess: dict = Depends(_require_staff_session)):
    """Per ogni pratica con CONVENZIONE/POLIZZA valorizzata, fissa la data
    DATA_ULTIMA_MODIFICA dal Master.csv come data di prima richiesta.
    La data viene salvata una sola volta — i giri successivi non la toccano."""
    df = await _read_master_csv()
    src_col    = next((c for c in df.columns if c.strip().upper().replace(".", "").replace(" ", "") in {"SOURCENAME", "SOURCE_NAME"}), None)
    pratica_col = next((c for c in df.columns if c.strip().upper() == "PRATICA"), None)
    conv_col   = next((c for c in df.columns if c.strip().upper() == "CONVENZIONE"), None)
    pol_col    = next((c for c in df.columns if c.strip().upper() == "POLIZZA"), None)
    data_col   = next((c for c in df.columns if c.strip().upper() == "DATA_ULTIMA_MODIFICA"), None)
    if src_col is None or pratica_col is None:
        return {"date": {}}
    if conv_col is None and pol_col is None:
        # Nessuna delle 2 colonne è nel Master: NON toccare la collection esistente
        # (altrimenti il delete_many sotto, con seen_keys vuoto, cancellerebbe
        # tutte le date già raccolte in precedenza).
        dates, dates_invio, dates_rds, dates_emissione = {}, {}, {}, {}
        async for d in pol_conv_dates_col.find({}):
            dates[d["_id"]] = d.get("data_richiesta", "")
            dates_invio[d["_id"]] = d.get("data_invio", "")
            dates_rds[d["_id"]] = d.get("data_richiesta_rds", "")
            dates_emissione[d["_id"]] = d.get("data_emissione", "")
        return {"date": dates, "date_invio": dates_invio, "date_richiesta_rds": dates_rds, "date_emissione": dates_emissione}

    def _extract_lotto(src: str) -> str:
        return str(src).replace(".xlsx", "").replace(".xls", "").replace("Lotto ", "").strip().upper()

    seen_keys = set()
    for col, field in ((conv_col, "CONVENZIONE"), (pol_col, "POLIZZA")):
        if col is None:
            continue
        for _, row in df.iterrows():
            val = str(row.get(col, "")).strip()
            if not val:
                continue
            lotto   = _extract_lotto(row.get(src_col, ""))
            pratica = str(row.get(pratica_col, "")).strip()
            if not lotto or not pratica:
                continue
            key = f"{lotto}|{pratica}|{field}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            data_mod = str(row.get(data_col, "")).strip() if data_col else ""
            # Fissa solo alla prima rilevazione, anche se il documento esiste
            # già (es. creato prima da un data_invio su INVIATA).
            existing = await pol_conv_dates_col.find_one({"_id": key})
            if existing is None:
                await pol_conv_dates_col.insert_one({"_id": key, "data_richiesta": data_mod})
            elif not existing.get("data_richiesta"):
                await pol_conv_dates_col.update_one({"_id": key}, {"$set": {"data_richiesta": data_mod}})

    # Rimuove chiavi non più presenti nel Master — solo se ne abbiamo trovate
    # di nuove: seen_keys vuoto per un problema transitorio (CSV non ancora
    # sincronizzato, colonne rinominate) non deve azzerare la collection.
    if seen_keys:
        await pol_conv_dates_col.delete_many({"_id": {"$nin": list(seen_keys)}})

    dates, dates_invio, dates_rds, dates_emissione = {}, {}, {}, {}
    async for d in pol_conv_dates_col.find({}):
        dates[d["_id"]] = d.get("data_richiesta", "")
        dates_invio[d["_id"]] = d.get("data_invio", "")
        dates_rds[d["_id"]] = d.get("data_richiesta_rds", "")
        dates_emissione[d["_id"]] = d.get("data_emissione", "")
    return {"date": dates, "date_invio": dates_invio, "date_richiesta_rds": dates_rds, "date_emissione": dates_emissione}


@app.post("/api/admin/polizze-convenzioni/update")
async def update_polizza_convenzione(
    payload: dict,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    """Aggiorna CONVENZIONE e/o POLIZZA per tutte le righe lotto+pratica nel Master CSV.
    Body: {lotto: "2B", pratica: "11", fields: {CONVENZIONE?: val, POLIZZA?: val}}
    Valori ammessi: NECESSARIA | RICHIESTA RDS | INVIATA | EMESSA | "" (vuoto = cancella)
    Richiede x-upload-token. Scrive su MongoDB e pusha su GitHub."""
    _check_token(x_upload_token or token_q)

    lotto   = str((payload or {}).get("lotto",   "")).strip().upper()
    pratica = str((payload or {}).get("pratica", "")).strip()
    fields  = (payload or {}).get("fields") or {}

    if not lotto or not pratica:
        raise HTTPException(400, "lotto e pratica sono obbligatori")
    if not fields:
        raise HTTPException(400, "fields non puo essere vuoto")

    bad_fields = set(fields.keys()) - _POL_CONV_ALLOWED_FIELDS
    if bad_fields:
        raise HTTPException(400, f"Campi non consentiti: {bad_fields}")
    for k, v in fields.items():
        if str(v).strip().upper() not in {s.upper() for s in _POL_CONV_ALLOWED_VALUES}:
            raise HTTPException(400, f"Valore non ammesso per {k}: '{v}'")

    async with _master_csv_lock:
        df = await _read_master_csv()

        def _extract_lotto(src: str) -> str:
            return src.replace(".xlsx", "").replace(".xls", "").replace("Lotto ", "").strip().upper()

        src_col = next((c for c in df.columns if c.strip().upper().replace(".", "").replace(" ", "") in {"SOURCENAME", "SOURCE_NAME"}), None)
        if src_col is None:
            raise HTTPException(500, "Colonna SOURCE.NAME non trovata nel Master CSV")

        pratica_col = next((c for c in df.columns if c.strip().upper() == "PRATICA"), None)
        if pratica_col is None:
            raise HTTPException(500, "Colonna PRATICA non trovata nel Master CSV")

        mask = (
            df[src_col].astype(str).apply(_extract_lotto) == lotto
        ) & (
            df[pratica_col].astype(str).str.strip() == pratica
        )

        matched = int(mask.sum())
        if matched == 0:
            raise HTTPException(404, f"Nessuna riga trovata per lotto={lotto} pratica={pratica}")

        for col, val in fields.items():
            real_col = next((c for c in df.columns if c.strip().upper() == col.upper()), None)
            if real_col is None:
                raise HTTPException(500, f"Colonna {col} non trovata nel Master CSV")
            df.loc[mask, real_col] = str(val).strip()

        note = f"Admin update polizze/convenzioni: lotto={lotto} pratica={pratica} fields={fields}"
        upload_id = await _write_master_csv(df, note=note)

    today_str = datetime.now().strftime("%d/%m/%Y")
    STATO_DATE_FIELD = {"INVIATA": "data_invio", "RICHIESTA RDS": "data_richiesta_rds", "EMESSA": "data_emissione"}
    for col, val in fields.items():
        date_field = STATO_DATE_FIELD.get(str(val).strip().upper())
        if not date_field:
            continue
        key = f"{lotto}|{pratica}|{col.upper()}"
        existing = await pol_conv_dates_col.find_one({"_id": key})
        if existing is None:
            await pol_conv_dates_col.insert_one({"_id": key, date_field: today_str})
        elif not existing.get(date_field):
            await pol_conv_dates_col.update_one({"_id": key}, {"$set": {date_field: today_str}})

    return {"ok": True, "rows_updated": matched, "new_upload_id": upload_id}


_POL_CONV_DATE_KEYS = {
    "richiesta": "data_richiesta",
    "richiesta_rds": "data_richiesta_rds",
    "invio": "data_invio",
    "emissione": "data_emissione",
}


@app.post("/api/admin/polizze-convenzioni/set-date")
async def set_pol_conv_date(
    payload: dict,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    """Modifica manuale di una delle 4 date CONVENZIONE/POLIZZA (Richiesta,
    Richiesta RDS, Invio, Emissione) — a differenza di STATO_DATE_FIELD (rev.197-
    202), che le fissa automaticamente e una sola volta al primo cambio di stato,
    questo endpoint permette una correzione manuale in qualunque momento, per
    sistemare casi passati (es. persi da un bug come rev.213) o errori futuri.
    Body: {lotto, pratica, field: "CONVENZIONE"|"POLIZZA",
           date_key: "richiesta"|"richiesta_rds"|"invio"|"emissione",
           value: "GG/MM/AAAA" oppure "" per cancellare}
    Richiede x-upload-token. Scrive solo su pol_conv_dates_col, non su Master.csv."""
    _check_token(x_upload_token or token_q)

    lotto    = str((payload or {}).get("lotto", "")).strip().upper()
    pratica  = str((payload or {}).get("pratica", "")).strip()
    field    = str((payload or {}).get("field", "")).strip().upper()
    date_key = str((payload or {}).get("date_key", "")).strip().lower()
    value    = str((payload or {}).get("value", "")).strip()

    if not lotto or not pratica:
        raise HTTPException(400, "lotto e pratica sono obbligatori")
    if field not in _POL_CONV_ALLOWED_FIELDS:
        raise HTTPException(400, f"field deve essere uno tra {sorted(_POL_CONV_ALLOWED_FIELDS)}")
    mongo_field = _POL_CONV_DATE_KEYS.get(date_key)
    if mongo_field is None:
        raise HTTPException(400, f"date_key deve essere uno tra {sorted(_POL_CONV_DATE_KEYS)}")
    if value:
        try:
            datetime.strptime(value, "%d/%m/%Y")
        except ValueError:
            raise HTTPException(400, "value deve essere in formato GG/MM/AAAA (o vuoto per cancellare)")

    key = f"{lotto}|{pratica}|{field}"
    if value:
        await pol_conv_dates_col.update_one({"_id": key}, {"$set": {mongo_field: value}}, upsert=True)
    else:
        await pol_conv_dates_col.update_one({"_id": key}, {"$unset": {mongo_field: ""}}, upsert=True)

    return {"ok": True, "key": key, "date_key": date_key, "value": value}



# ═════════════════════════════════════════════════════════════════════════════
# SOPRALLUOGHI — verbali di sopralluogo
# ─────────────────────────────────────────────────────────────────────────────
SOPRALLUOGHI_COLS = [
    "codice_verbale", "data_sopralluogo", "lotto", "tratta_id", "impresa",
    "referente_impresa", "referente_retelit", "comune", "localita",
    "tipo_intervento", "esito", "note", "segnalazioni", "azioni_richieste",
    "firma_impresa", "firma_retelit", "foto_urls", "created_at",
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


@app.get("/api/lotti-cantieri")
async def get_lotti_cantieri(sess: dict = Depends(_require_staff_session)):
    """Restituisce lotti distinti (da Master.csv) e i loro cantieri (da MongoDB)."""
    lotti_master = []

    # Leggi lotti da Master.csv — prova più nomi colonna
    try:
        df = await _read_master_csv()
        if df is not None:
            col = next((c for c in df.columns if c.strip().lower() in
                        ("source.name", "source name", "lotto", "lotti", "nome_lotto")), None)
            print(f"[lotti-cantieri] colonne disponibili: {list(df.columns[:10])}, colonna lotto: {col}")
            if col:
                raw = df[col].dropna().unique().tolist()
                lotti_master = sorted({_lotto_from_source(r) for r in raw if str(r).strip()})
                print(f"[lotti-cantieri] lotti da Master.csv: {lotti_master}")
    except Exception as e:
        print(f"[lotti-cantieri] errore lettura Master.csv: {e}")

    # Cantieri da MongoDB — raggruppati per lotto
    cantieri_map: dict = {}
    async for doc in cantieri_col.find({}, {"cantiere_key": 1, "lotto": 1, "ente": 1}):
        lotto = _lotto_from_source(doc.get("lotto", ""))
        key   = str(doc.get("cantiere_key", "")).strip()
        if not lotto or not key:
            continue
        num   = key.split("|")[0].strip() if "|" in key else key
        ente  = str(doc.get("ente", "")).strip()
        codice = f"CA/{num}/{lotto}"
        label  = f"{codice} — {ente}" if ente else codice
        if lotto not in cantieri_map:
            cantieri_map[lotto] = []
        if not any(c["value"] == key for c in cantieri_map[lotto]):
            cantieri_map[lotto].append({"value": key, "label": label, "num": num})

    # Unisce: lotti dal Master + lotti dai cantieri (fallback se Master fallisce)
    tutti_lotti = sorted(set(lotti_master) | set(cantieri_map.keys()))
    result = {l: sorted(cantieri_map.get(l, []), key=lambda x: x["num"]) for l in tutti_lotti}

    # Mappa lotto → impresa assegnata (per auto-popolare il campo impresa)
    lotto_impresa = {}
    async for a in assignments_col.find({}, {"nome": 1, "lotti": 1}):
        nome_impresa = a.get("nome", "")
        for l in (a.get("lotti") or []):
            lotto_norm = _lotto_from_source(l)
            if lotto_norm:
                lotto_impresa[lotto_norm] = nome_impresa

    print(f"[lotti-cantieri] lotti finali: {list(result.keys())}")
    print(f"[lotti-cantieri] lotto_impresa: {lotto_impresa}")
    return {"lotti": result, "lotto_impresa": lotto_impresa}


@app.delete("/api/sopralluoghi/{sop_id}")
async def delete_sopralluogo(sop_id: str, sess: dict = Depends(_require_session)):
    """Elimina un verbale di sopralluogo — solo admin."""
    if sess.get("ruolo", "user") != "admin":
        raise HTTPException(403, "Solo gli admin possono eliminare verbali")
    try:
        oid = ObjectId(sop_id)
    except Exception:
        raise HTTPException(400, "ID non valido")
    res = await sopralluoghi_col.delete_one({"_id": oid})
    if res.deleted_count == 0:
        raise HTTPException(404, "Verbale non trovato")
    asyncio.create_task(_push_sopralluoghi_to_github())
    return {"ok": True, "deleted": sop_id}


@app.get("/api/sopralluoghi")
async def list_sopralluoghi(sess: dict = Depends(_require_staff_session)):
    """Restituisce tutti i verbali di sopralluogo, ordinati per codice decrescente."""
    verbali = []
    async for d in sopralluoghi_col.find({}).sort("codice_verbale", -1):
        d["_id"] = str(d["_id"])
        verbali.append(d)
    return {"verbali": verbali}


@app.get("/api/sopralluoghi/next-codice")
async def sopralluogo_next_codice(sess: dict = Depends(_require_staff_session)):
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
async def save_sopralluogo(payload: dict, sess: dict = Depends(_require_staff_session)):
    """Salva un verbale di sopralluogo su MongoDB e aggiorna sopralluoghi.csv su GitHub.
    Le foto (se presenti, come data URL base64) vengono caricate su GitHub in
    sopralluoghi/foto/{codice_verbale}/ per non saturare lo storage MongoDB gratuito."""
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

    # Upload foto su GitHub (max 4, ciascuna come data URL base64 dal frontend)
    foto_urls = []
    foto_in = (payload or {}).get("foto") or []
    for i, foto in enumerate(foto_in[:4]):
        try:
            data_url = foto.get("dataUrl", "") if isinstance(foto, dict) else str(foto)
            if "," not in data_url:
                continue
            header, b64data = data_url.split(",", 1)
            ext = "jpg"
            if "png" in header:
                ext = "png"
            elif "webp" in header:
                ext = "webp"
            img_bytes = base64.b64decode(b64data)
            github_path = f"sopralluoghi/foto/{codice}/foto_{i+1}.{ext}"
            await _push_to_github(img_bytes, path=github_path, label=f"foto {i+1} — {codice}")
            raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{github_path}"
            foto_urls.append(raw_url)
        except Exception as e:
            print(f"[sopralluoghi] errore upload foto {i+1}: {e}")

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
        "foto_urls":           ", ".join(foto_urls),
        "created_at":          _now_iso(),
    }
    await sopralluoghi_col.insert_one(record)
    asyncio.create_task(_push_sopralluoghi_to_github())
    return {"ok": True, "codice_verbale": codice, "foto_urls": foto_urls}


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
    lotti = {_lotto_from_source(l) for l in (assignment.get("lotti") or []) if str(l).strip()}

    # Legge le tratte dei lotti dell'impresa dal Master.csv (match esatto, non substring:
    # "Lotto 2" non deve agganciare "Lotto 2A" — stesso criterio di impresa_pratiche/get_cantieri_impresa)
    try:
        df = await _read_master_csv()
        tratte_impresa: set[str] = set()
        if "Source.Name" in df.columns and lotti:
            mask = df["Source.Name"].apply(lambda x: _lotto_from_source(x) in lotti)
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


async def _touch_data_update(tratta_id: str, pratica: str, tipo_permesso: str = "") -> None:
    """Aggiorna DATA_UPDATE=oggi sulle righe Master.csv della pratica toccata da un sollecito.
    Scrittura diretta (fire-and-forget), coerente col modello 'solleciti senza approvazione admin'."""
    try:
        async with _master_csv_lock:
            df = await _read_master_csv()
            if "DATA_UPDATE" not in df.columns or "TRATTA_ID" not in df.columns:
                return
            mask = df["TRATTA_ID"].astype(str).str.strip() == str(tratta_id).strip()
            if not mask.any():
                return
            df.loc[mask, "DATA_UPDATE"] = datetime.now(timezone.utc).strftime("%d/%m/%Y")
            await _write_master_csv(df, note=f"Sollecito tratta {tratta_id} pratica {pratica}: touch DATA_UPDATE")
    except Exception as e:
        print(f"[_touch_data_update] {e}")


async def _touch_data_update_multi(keys: list) -> None:
    """Come _touch_data_update ma per più pratiche in un solo read+write di Master.csv."""
    try:
        async with _master_csv_lock:
            df = await _read_master_csv()
            if "DATA_UPDATE" not in df.columns or "TRATTA_ID" not in df.columns:
                return
            oggi = datetime.now(timezone.utc).strftime("%d/%m/%Y")
            any_hit = False
            for tratta_id, pratica, tipo_permesso in keys:
                mask = df["TRATTA_ID"].astype(str).str.strip() == str(tratta_id).strip()
                if mask.any():
                    df.loc[mask, "DATA_UPDATE"] = oggi
                    any_hit = True
            if any_hit:
                await _write_master_csv(df, note=f"Solleciti bulk ({len(keys)}): touch DATA_UPDATE")
    except Exception as e:
        print(f"[_touch_data_update_multi] {e}")


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
    asyncio.create_task(_touch_data_update(tratta_id, pratica, tipo_perm))
    return {"ok": True, "id": str(res.inserted_id)}


@app.post("/api/imprese/solleciti/bulk-insert")
async def bulk_insert_solleciti(payload: dict, sess: dict = Depends(_require_session)):
    """Inserisce più solleciti in una sola chiamata e fa un unico push GitHub."""
    nome  = sess["nome"]
    items = (payload or {}).get("items", [])
    if not items or not isinstance(items, list):
        raise HTTPException(400, "items obbligatorio")

    inserted = []
    touch_keys = []
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
        touch_keys.append((tratta_id, record["pratica"], record["tipo_permesso"]))

    if inserted:
        asyncio.create_task(_push_solleciti_to_github())
        asyncio.create_task(_touch_data_update_multi(touch_keys))
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


@app.get("/api/staff/solleciti")
async def staff_get_solleciti(sess: dict = Depends(_require_staff_session)):
    """Restituisce tutti i solleciti per le pagine staff (index.html ecc.), letti
    direttamente da MongoDB — non dipende dal push/deploy su GitHub Pages, a
    differenza del vecchio solleciti.csv statico (vedi AGENT_BRIEF rev.215)."""
    items = []
    async for d in solleciti_col.find({}).sort("created_at", -1):
        items.append({
            "pratica":  str(d.get("pratica", "") or ""),
            "tratta":   str(d.get("tratta_id", "") or ""),
            "tipo":     str(d.get("tipo_sollecito", "") or ""),
            "data":     str(d.get("data_sollecito", "") or ""),
            "note":     str(d.get("note", "") or ""),
            "impresa":  str(d.get("impresa", "") or ""),
        })
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
# una stessa autorizzazione può coprire più tratte). Il NULLA OSTA/ORDINANZA
# non sono un cantiere a sé: sono permessi accessori che possono mancare su
# alcune tratte della stessa autorizzazione. Quelle tratte restano elencate
# nel cantiere con 'lavorabile'=false, ma è un flag SOLO indicativo (usato
# in mappa per colorare le tratte non ancora cantierabili): NON esclude i
# metri dal totale rendicontabile dall'impresa. metri_totali conta sempre
# tutta la lunghezza della pratica autorizzata.
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
TECNICA_SCAVO_VALUES  = ["trincea", "no_dig", "canaletta", ""]

CANTIERI_CSV_PATH = os.environ.get("GITHUB_CANTIERI_PATH", "cantieri.csv")
CANTIERI_COLS = [
    "cantiere_key", "codice_cantiere", "pratica_id", "ente", "lotto", "cluster",
    "tratte_lavorabili", "tratte_bloccate",
    "metri_totali", "metri_totali_potenziali",
    "stato_cantiere", "tecnica_scavo",
    "data_inizio_prevista", "data_inizio_effettiva",
    "data_fine_prevista", "data_fine_effettiva",
    "metri_scavati", "note", "motivo_blocco", "data_ripresa_stimata",
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


async def _max_codice_per_lotto() -> dict:
    """Numero massimo di codice_cantiere (CA/N/lotto) già assegnato per ciascun
    lotto, per continuare la sequenza senza mai riassegnare un numero usato."""
    cache: dict[str, int] = {}
    async for d in cantieri_col.find(
        {"codice_cantiere": {"$regex": "^CA/"}}, {"lotto": 1, "codice_cantiere": 1}
    ):
        m = re.match(r"^CA/(\d+)/", d.get("codice_cantiere", ""))
        if not m:
            continue
        lotto = d.get("lotto", "")
        cache[lotto] = max(cache.get(lotto, 0), int(m.group(1)))
    return cache


async def _backfill_codici_cantiere(cache: dict) -> int:
    """Assegna codice_cantiere ai cantieri creati prima dell'introduzione del
    campo. Progressivo stabile per lotto — una volta scritto su un documento
    non viene mai più toccato, nemmeno da sync successivi. Ordine pratica_id
    (unico riferimento disponibile per i cantieri storici, non essendoci un
    timestamp di creazione)."""
    n = 0
    async for d in cantieri_col.find(
        {"$or": [{"codice_cantiere": {"$exists": False}}, {"codice_cantiere": ""}]}
    ).sort([("lotto", 1), ("pratica_id", 1)]):
        lotto = d.get("lotto", "")
        cache[lotto] = cache.get(lotto, 0) + 1
        codice = f"CA/{cache[lotto]}/{lotto}"
        await cantieri_col.update_one({"_id": d["_id"]}, {"$set": {"codice_cantiere": codice}})
        n += 1
    if n:
        print(f"[sync_cantieri] assegnato codice_cantiere a {n} cantieri storici")
    return n


async def _sync_cantieri() -> int:
    """Raggruppa le tratte con AUTORIZZAZIONE OTTENUTA per pratica (ente, numero,
    lotto) e crea/aggiorna un documento cantiere per pratica. metri_totali conta
    TUTTE le tratte della pratica (autorizzazione ottenuta), indipendentemente
    da 'lavorabile': quel flag è solo indicativo per la visualizzazione in
    mappa (NULLA OSTA/ORDINANZA ottenuti) e non deve limitare i metri che
    un'impresa può rendicontare come scavati sul cantiere.
    Ritorna il numero di nuovi cantieri (nuove pratiche) creati."""
    try:
        df = await _read_master_csv()
        summary = _compute_tratta_summary(df)
    except Exception as e:
        print(f"[sync_cantieri] errore lettura master: {e}")
        return 0

    # CLUSTER/PROVINCIA/COMUNE non esistono in Master.csv: vengono recuperati da
    # Riepilogo_progettazione.csv (che li eredita da QGIS.geojson), indicizzati
    # per TRATTA_ID. Fail-soft: se il file non è disponibile i cantieri vengono
    # comunque creati, semplicemente senza questi tre campi.
    geo_by_tratta: dict[str, dict] = {}
    try:
        riep_df = await _read_riepilogo_csv()
        if riep_df is not None and "TRATTA_ID" in riep_df.columns:
            for _, row in riep_df.iterrows():
                tid = str(row.get("TRATTA_ID", "")).strip()
                if not tid:
                    continue
                geo_by_tratta[tid] = {
                    "cluster":   str(row.get("CLUSTER", "")).strip(),
                    "provincia": str(row.get("PROVINCIA", "")).strip(),
                    "comune":    str(row.get("COMUNE", "")).strip(),
                }
    except Exception as e:
        print(f"[sync_cantieri] errore lettura riepilogo (cluster/provincia/comune): {e}")

    codice_cache = await _max_codice_per_lotto()
    await _backfill_codici_cantiere(codice_cache)

    # Mappa lotto → impresa assegnata (stessa fonte di /api/lotti-cantieri) per
    # popolare/backfillare 'impresa' sia sui cantieri esistenti che su quelli nuovi.
    lotto_impresa: dict[str, str] = {}
    async for a in assignments_col.find({}, {"nome": 1, "lotti": 1}):
        nome_impresa = a.get("nome", "")
        for l in (a.get("lotti") or []):
            lotto_norm = _lotto_from_source(l)
            if lotto_norm:
                lotto_impresa[lotto_norm] = nome_impresa

    groups: dict[tuple, dict] = {}
    for tratta_id, info in summary.items():
        if info.get("STATO_AUTORIZZAZIONE") != "OTTENUTO":
            continue  # niente cantiere finché l'autorizzazione non è ottenuta
        pratica_num = (info.get("PRATICA_AUT") or "").strip()
        if not pratica_num:
            continue
        rows = df[df["TRATTA_ID"].astype(str).str.strip() == tratta_id]
        lotto = _lotto_from_source(rows.iloc[0].get("Source.Name", "")) if not rows.empty else ""
        geo   = geo_by_tratta.get(tratta_id, {})
        cluster   = geo.get("cluster", "")
        provincia = geo.get("provincia", "")
        comune    = geo.get("comune", "")
        try:
            raw_lung = info.get("LUNGHEZZA", 0)
            lunghezza = 0.0 if pd.isna(raw_lung) else float(str(raw_lung).replace(",", "."))
        except Exception:
            lunghezza = 0.0

        ente_display = re.sub(r"\s+", " ", str(info.get("ENTE", ""))).strip()
        ente_key = ente_display.upper()
        key = (ente_key, pratica_num, lotto)
        g = groups.setdefault(key, {
            "cantiere_key": f"{pratica_num}|{lotto}|{ente_key}",
            "pratica_id": f"AUT/{pratica_num}/{lotto}",
            "ente": ente_display, "lotto": lotto, "cluster": cluster,
            "tratte": [],
        })
        g["tratte"].append({
            "tratta_id":  tratta_id,
            "lunghezza":  lunghezza,
            "lavorabile": info.get("LAVORABILE") == "SI",
            "motivo_no":  info.get("MOTIVO_NO", ""),
            "provincia":  provincia,
            "comune":     comune,
        })

    created = 0
    stato_rank = {s: i for i, s in enumerate(STATO_CANTIERE_VALUES)}
    for key, g in groups.items():
        # metri_totali = tutte le tratte della pratica (autorizzazione ottenuta),
        # a prescindere da 'lavorabile' — quel flag è solo per la mappa e non
        # deve ridurre il totale su cui l'impresa rendiconta i metri scavati.
        metri_totali     = sum(t["lunghezza"] for t in g["tratte"])
        metri_totali_pot = sum(t["lunghezza"] for t in g["tratte"])
        # provincia/comune del cantiere: il valore più frequente tra le sue tratte
        provincia_count = Counter(t["provincia"] for t in g["tratte"] if t.get("provincia"))
        comune_count     = Counter(t["comune"]    for t in g["tratte"] if t.get("comune"))
        provincia_cantiere = provincia_count.most_common(1)[0][0] if provincia_count else ""
        comune_cantiere     = comune_count.most_common(1)[0][0] if comune_count else ""

        existing = await cantieri_col.find_one({"cantiere_key": g["cantiere_key"]})
        if existing:
            await cantieri_col.update_one(
                {"_id": existing["_id"]},
                {"$set": {
                    "tratte": g["tratte"], "lotto": g["lotto"], "cluster": g["cluster"],
                    "metri_totali": metri_totali, "metri_totali_potenziali": metri_totali_pot,
                    "provincia": provincia_cantiere, "comune": comune_cantiere,
                    "impresa": lotto_impresa.get(g["lotto"], existing.get("impresa", "")),
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
        impresa       = lotto_impresa.get(g["lotto"]) or next((d.get("impresa") for d in old_docs if d.get("impresa")), "")

        codice_cache[g["lotto"]] = codice_cache.get(g["lotto"], 0) + 1
        codice_cantiere = f"CA/{codice_cache[g['lotto']]}/{g['lotto']}"

        doc = {
            "cantiere_key": g["cantiere_key"],
            "codice_cantiere": codice_cantiere,
            "pratica_id": g["pratica_id"], "ente": g["ente"], "lotto": g["lotto"], "cluster": g["cluster"],
            "provincia": provincia_cantiere, "comune": comune_cantiere,
            "tratte": g["tratte"],
            "metri_totali": metri_totali, "metri_totali_potenziali": metri_totali_pot,
            "stato_cantiere": stato_cantiere, "tecnica_scavo": tecnica_scavo,
            "data_inizio_prevista": "", "data_inizio_effettiva": "",
            "data_fine_prevista": "", "data_fine_effettiva": "",
            "metri_scavati": metri_scavati, "note": "",
            "motivo_blocco": "", "data_ripresa_stimata": "",
            "impresa": impresa, "updated_at": _now_iso(),
            "log": old_log,
        }
        await cantieri_col.insert_one(doc)
        if old_docs:
            await cantieri_col.delete_many({"_id": {"$in": [d["_id"] for d in old_docs]}})
        created += 1

    # Pulizia doppioni: cantieri creati prima dell'introduzione di 'cantiere_key'
    # (schema intermedio: solo pratica_id+ente, niente cantiere_key) oppure prima
    # della normalizzazione di 'ente' (spazi multipli/maiuscole diverse → stessa
    # pratica vista come due chiavi diverse). $nin su un campo assente include
    # anche i documenti dove il campo non esiste affatto (schema intermedio).
    touched_keys = {g["cantiere_key"] for g in groups.values()}
    merged = 0
    async for orphan in cantieri_col.find({
        "pratica_id": {"$exists": True},
        "cantiere_key": {"$nin": list(touched_keys)},
    }):
        m = re.match(r"^AUT/(.+)/([^/]+)$", orphan.get("pratica_id") or "")
        if not m:
            continue
        o_num, o_lotto = m.group(1), m.group(2)
        o_ente_key = re.sub(r"\s+", " ", str(orphan.get("ente", ""))).strip().upper()
        target_key = f"{o_num}|{o_lotto}|{o_ente_key}"
        if target_key == orphan.get("cantiere_key") or target_key not in touched_keys:
            continue  # non è un doppione da normalizzazione: lascialo (es. autorizzazione non più OTTENUTA)
        target = await cantieri_col.find_one({"cantiere_key": target_key})
        if not target:
            continue
        await cantieri_col.update_one(
            {"_id": target["_id"]},
            {"$inc": {"metri_scavati": float(orphan.get("metri_scavati", 0) or 0)},
             "$push": {"log": {"$each": orphan.get("log", [])}}},
        )
        await cantieri_col.delete_one({"_id": orphan["_id"]})
        merged += 1
    if merged:
        print(f"[sync_cantieri] uniti {merged} cantieri duplicati (variazioni di formattazione ente)")

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
async def get_cantieri(lotto: str = "", cluster: str = "", stato: str = "", sess: dict = Depends(_require_staff_session)):
    """Lista cantieri (pubblica), uno per pratica di autorizzazione. Filtrabile
    per lotto, cluster, stato."""
    q: dict = {}
    if lotto:   q["lotto"]          = lotto
    if cluster: q["cluster"]        = cluster
    if stato:   q["stato_cantiere"] = stato
    items = []
    async for d in cantieri_col.find(q).sort("pratica_id", 1):
        d["_id"] = str(d["_id"])
        d["log_count"] = len(d.get("log") or [])   # segnala se l'impresa ha mai fatto un aggiornamento
        d.pop("log", None)   # non esporre lo storico nel listing
        items.append(d)
    return {"cantieri": items, "count": len(items)}


@app.get("/api/cantieri/{cantiere_key:path}/log")
async def get_cantiere_log_public(cantiere_key: str, sess: dict = Depends(_require_staff_session)):
    """Storico aggiornamenti di un cantiere (pubblico, sola lettura — no session).
    Usato dal 'Registro Cantiere' in scavi.html."""
    doc = await cantieri_col.find_one({"cantiere_key": cantiere_key})
    if not doc:
        raise HTTPException(404, "Cantiere non trovato")
    return {"log": doc.get("log", []), "cantiere_key": cantiere_key, "pratica_id": doc.get("pratica_id")}


# ── Endpoint impresa: aggiornamento giornaliero ───────────────────────────────

@app.get("/api/admin/actions")
async def list_admin_actions(
    limit: int = 100,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    """Log azioni admin (upload/delete/restore/prune) — vedi _log_admin_action."""
    _check_token(x_upload_token or token_q)
    cur = admin_actions_col.find({}).sort("timestamp", -1).limit(min(limit, 500))
    items = [_serialize(d) async for d in cur]
    return {"actions": items, "count": len(items)}


@app.post("/api/admin/prune-versions")
async def admin_prune_versions(
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
    x_actor_nome: Annotated[str | None, Header(alias="x-actor-nome")] = None,
):
    """One-shot: applica KEEP_VERSIONS all'arretrato già esistente (il prune
    automatico dopo ogni upload agisce solo sui filename toccati da quel momento in poi)."""
    _check_token(x_upload_token or token_q)
    filenames = await uploads_col.distinct("filename", {"deleted_at": None})
    result = {}
    for fn in filenames:
        result[fn] = await _prune_old_versions(fn)
    await _log_admin_action("prune_versions", ",".join(filenames) or "-", x_actor_nome)
    return {"pruned": result, "keep_versions": KEEP_VERSIONS}


@app.delete("/api/admin/sopralluoghi/reset")
async def admin_reset_sopralluoghi(
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    """Svuota la collection sopralluoghi (solo test/dev)."""
    _check_token(x_upload_token or token_q)
    deleted = (await sopralluoghi_col.delete_many({})).deleted_count
    asyncio.create_task(_push_sopralluoghi_to_github())
    return {"deleted": deleted}


@app.delete("/api/admin/cantieri/reset")
async def admin_reset_cantieri(
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
):
    """Svuota la collection cantieri (solo test/dev) e la ricrea da Master.csv."""
    _check_token(x_upload_token or token_q)
    deleted = (await cantieri_col.delete_many({})).deleted_count
    created = await _sync_cantieri()
    asyncio.create_task(_push_cantieri_to_github())
    return {"deleted": deleted, "recreated": created}


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


@app.put("/api/admin/cantieri/{cantiere_key:path}")
async def admin_update_cantiere(
    cantiere_key: str,
    payload: dict,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
    x_actor_nome: Annotated[str | None, Header(alias="x-actor-nome")] = None,
):
    """Correzione admin di un cantiere: a differenza di POST /api/imprese/cantieri/{key}
    (scrittura impresa, metri_scavati SEMPRE in accumulo via $inc), qui i campi passati
    vengono impostati DIRETTAMENTE — utile per correggere dati di test/errati senza dover
    passare da un valore negativo (non ammesso) o azzerare tutto il cantiere.
    Non tocca il log storico salvo che 'clear_log' sia True."""
    _check_token(x_upload_token or token_q)
    doc = await cantieri_col.find_one({"cantiere_key": cantiere_key})
    if not doc:
        raise HTTPException(404, "Cantiere non trovato")

    allowed = {
        "stato_cantiere", "tecnica_scavo", "metri_scavati",
        "data_inizio_prevista", "data_inizio_effettiva",
        "data_fine_prevista", "data_fine_effettiva",
        "note", "motivo_blocco", "data_ripresa_stimata", "impresa",
    }
    mongo_set: dict = {}
    for k, v in (payload or {}).items():
        if k not in allowed:
            continue
        if k == "stato_cantiere" and v not in STATO_CANTIERE_VALUES:
            raise HTTPException(400, f"stato_cantiere non valido: {v}")
        if k == "tecnica_scavo" and v and v not in TECNICA_SCAVO_VALUES:
            raise HTTPException(400, f"tecnica_scavo non valida: {v}")
        if k == "metri_scavati":
            try:
                v = max(0.0, float(v))
            except Exception:
                raise HTTPException(400, "metri_scavati deve essere un numero")
        mongo_set[k] = v

    mongo_update: dict = {"$set": mongo_set} if mongo_set else {}
    if payload.get("clear_log"):
        mongo_update["$set"] = {**mongo_set, "log": []}
    if not mongo_update:
        raise HTTPException(400, "Nessun campo valido da aggiornare")
    mongo_update["$set"]["updated_at"] = _now_iso()

    await cantieri_col.update_one({"cantiere_key": cantiere_key}, mongo_update)
    await _log_admin_action("update_cantiere", cantiere_key, x_actor_nome)
    asyncio.create_task(_push_cantieri_to_github())
    return {"ok": True, "cantiere_key": cantiere_key}


@app.put("/api/admin/cantieri/{cantiere_key:path}/log/{idx}")
async def admin_update_cantiere_log_entry(
    cantiere_key: str,
    idx: int,
    payload: dict,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
    x_actor_nome: Annotated[str | None, Header(alias="x-actor-nome")] = None,
):
    """Corregge una singola riga di storico (log) di un cantiere — es. un
    caricamento errato inserito da un'impresa. metri_scavati viene
    ricalcolato come somma di tutte le righe di log rimaste, per restare
    coerente con l'accumulo fatto da POST /api/imprese/cantieri/{key}."""
    _check_token(x_upload_token or token_q)
    doc = await cantieri_col.find_one({"cantiere_key": cantiere_key})
    if not doc:
        raise HTTPException(404, "Cantiere non trovato")
    log = doc.get("log") or []
    if idx < 0 or idx >= len(log):
        raise HTTPException(404, "Voce di storico non trovata")

    allowed = {
        "data", "impresa", "stato_cantiere", "tecnica_scavo",
        "metri_realizzati", "note", "motivo_blocco", "data_ripresa_stimata",
    }
    entry = dict(log[idx])
    for k, v in (payload or {}).items():
        if k not in allowed:
            continue
        if k == "stato_cantiere" and v not in STATO_CANTIERE_VALUES:
            raise HTTPException(400, f"stato_cantiere non valido: {v}")
        if k == "tecnica_scavo" and v and v not in TECNICA_SCAVO_VALUES:
            raise HTTPException(400, f"tecnica_scavo non valida: {v}")
        if k == "metri_realizzati":
            try:
                v = max(0.0, float(v))
            except Exception:
                raise HTTPException(400, "metri_realizzati deve essere un numero")
        entry[k] = v
    log[idx] = entry

    metri_scavati = sum(float(e.get("metri_realizzati") or 0) for e in log)
    await cantieri_col.update_one(
        {"cantiere_key": cantiere_key},
        {"$set": {"log": log, "metri_scavati": metri_scavati, "updated_at": _now_iso()}},
    )
    await _log_admin_action("update_cantiere_log", f"{cantiere_key}#{idx}", x_actor_nome)
    asyncio.create_task(_push_cantieri_to_github())
    return {"ok": True, "cantiere_key": cantiere_key, "idx": idx, "metri_scavati": metri_scavati}


@app.delete("/api/admin/cantieri/{cantiere_key:path}/log/{idx}")
async def admin_delete_cantiere_log_entry(
    cantiere_key: str,
    idx: int,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
    x_actor_nome: Annotated[str | None, Header(alias="x-actor-nome")] = None,
):
    """Elimina una singola riga di storico (es. caricamento di test/duplicato)
    senza toccare le altre. metri_scavati ricalcolato sulle righe rimaste."""
    _check_token(x_upload_token or token_q)
    doc = await cantieri_col.find_one({"cantiere_key": cantiere_key})
    if not doc:
        raise HTTPException(404, "Cantiere non trovato")
    log = doc.get("log") or []
    if idx < 0 or idx >= len(log):
        raise HTTPException(404, "Voce di storico non trovata")
    del log[idx]

    metri_scavati = sum(float(e.get("metri_realizzati") or 0) for e in log)
    await cantieri_col.update_one(
        {"cantiere_key": cantiere_key},
        {"$set": {"log": log, "metri_scavati": metri_scavati, "updated_at": _now_iso()}},
    )
    await _log_admin_action("delete_cantiere_log", f"{cantiere_key}#{idx}", x_actor_nome)
    asyncio.create_task(_push_cantieri_to_github())
    return {"ok": True, "cantiere_key": cantiere_key, "idx": idx, "metri_scavati": metri_scavati}


@app.delete("/api/admin/cantieri/{cantiere_key:path}/reset")
async def admin_reset_single_cantiere(
    cantiere_key: str,
    x_upload_token: Annotated[str | None, Header(alias="x-upload-token")] = None,
    token_q: Annotated[str | None, Query(alias="x_upload_token")] = None,
    x_actor_nome: Annotated[str | None, Header(alias="x-actor-nome")] = None,
):
    """Riporta UN SOLO cantiere allo stato 'pristino' (non_avviato, 0 metri, log
    vuoto) senza toccare gli altri e senza ricrearlo — a differenza di
    DELETE /api/admin/cantieri/reset che svuota TUTTA la collection. Utile per
    rimuovere avanzamenti di test inseriti per errore da un'impresa. Metadati
    (cantiere_key, codice_cantiere, pratica_id, ente, lotto, metri_totali, impresa)
    restano invariati."""
    _check_token(x_upload_token or token_q)
    doc = await cantieri_col.find_one({"cantiere_key": cantiere_key})
    if not doc:
        raise HTTPException(404, "Cantiere non trovato")
    await cantieri_col.update_one(
        {"cantiere_key": cantiere_key},
        {"$set": {
            "stato_cantiere": "non_avviato", "tecnica_scavo": "",
            "data_inizio_prevista": "", "data_inizio_effettiva": "",
            "data_fine_prevista": "", "data_fine_effettiva": "",
            "metri_scavati": 0.0, "note": "",
            "motivo_blocco": "", "data_ripresa_stimata": "",
            "log": [], "updated_at": _now_iso(),
        }},
    )
    await _log_admin_action("reset_single_cantiere", cantiere_key, x_actor_nome)
    asyncio.create_task(_push_cantieri_to_github())
    return {"ok": True, "cantiere_key": cantiere_key}


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
        d["log_count"] = len(d.get("log") or [])
        d.pop("log", None)
        cant_lotto = _lotto_from_source(str(d.get("lotto") or ""))
        if cant_lotto in lotti:
            items.append(d)
    return {"cantieri": items, "count": len(items)}


@app.post("/api/imprese/cantieri/{cantiere_key:path}")
async def update_cantiere(cantiere_key: str, payload: dict, sess: dict = Depends(_require_session)):
    """L'impresa aggiorna lo stato cantiere e i metri realizzati oggi (a livello
    di pratica: un solo stato/contatore per tutte le tratte della pratica).
    cantiere_key (non pratica_id) perché pratica_id ('AUT/24/1A') non è garantito
    univoco tra enti diversi sullo stesso lotto/numero."""
    nome = sess["nome"]
    doc = await cantieri_col.find_one({"cantiere_key": cantiere_key})
    if not doc:
        raise HTTPException(404, "Cantiere non trovato")

    # Verifica che la pratica appartenga ai lotti dell'impresa
    assignment = await _find_assignment(nome)
    raw_lotti = [str(l) for l in ((assignment or {}).get("lotti") or [])]
    lotti = [_lotto_from_source(l) for l in raw_lotti]
    if _lotto_from_source(str(doc.get("lotto") or "")) not in lotti:
        raise HTTPException(403, "Pratica non assegnata a questa impresa")

    # Campi aggiornabili dall'impresa
    allowed = {
        "stato_cantiere", "tecnica_scavo",
        "data_inizio_prevista", "data_inizio_effettiva",
        "data_fine_prevista", "data_fine_effettiva",
        "metri_realizzati_oggi",   # campo speciale: viene accumulato
        "note", "motivo_blocco", "data_ripresa_stimata",
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

    nuovo_stato = payload.get("stato_cantiere", doc.get("stato_cantiere"))
    if nuovo_stato == "allestimento" and not payload.get("data_inizio_prevista") and not doc.get("data_inizio_prevista"):
        raise HTTPException(400, "data_inizio_prevista obbligatoria per lo stato Allestimento")
    if nuovo_stato == "in_corso" and not doc.get("data_inizio_effettiva") and not payload.get("data_inizio_effettiva"):
        raise HTTPException(400, "data_inizio_effettiva obbligatoria al primo avvio cantiere")
    if nuovo_stato == "completato" and not payload.get("data_fine_effettiva") and not doc.get("data_fine_effettiva"):
        raise HTTPException(400, "data_fine_effettiva obbligatoria per chiudere il cantiere")

    # Accumula metri giornalieri (con cap su metri_totali)
    metri_oggi = 0.0
    if "metri_realizzati_oggi" in payload:
        try:
            metri_oggi = max(0.0, float(payload["metri_realizzati_oggi"]))
        except Exception:
            raise HTTPException(400, "metri_realizzati_oggi deve essere un numero")
        metri_totali   = float(doc.get("metri_totali", 0) or 0)
        metri_scavati_attuali = float(doc.get("metri_scavati", 0) or 0)
        rimanenti = max(0.0, metri_totali - metri_scavati_attuali)
        if metri_totali > 0 and metri_oggi > rimanenti:
            raise HTTPException(
                400,
                f"Metri inseriti ({metri_oggi:.0f}m) superano i metri rimanenti "
                f"del cantiere ({rimanenti:.0f}m su {metri_totali:.0f}m totali)."
            )
        update["$inc"] = {"metri_scavati": metri_oggi}

    update["impresa"]    = nome
    update["updated_at"] = _now_iso()

    # Log entry giornaliero
    log_entry = {
        "data":                  _now_iso()[:10],
        "impresa":               nome,
        "stato_cantiere":        payload.get("stato_cantiere", doc.get("stato_cantiere")),
        "tecnica_scavo":         payload.get("tecnica_scavo", doc.get("tecnica_scavo")),
        "metri_realizzati":      metri_oggi,
        "note":                  payload.get("note", ""),
        "motivo_blocco":         payload.get("motivo_blocco", ""),
        "data_ripresa_stimata":  payload.get("data_ripresa_stimata", ""),
    }

    # Costruisci update MongoDB
    mongo_set = {k: v for k, v in update.items() if k != "$inc"}
    mongo_update: dict = {"$set": mongo_set, "$push": {"log": log_entry}}
    if "$inc" in update:
        mongo_update["$inc"] = update["$inc"]

    await cantieri_col.update_one({"cantiere_key": cantiere_key}, mongo_update)
    asyncio.create_task(_push_cantieri_to_github())
    return {"ok": True, "cantiere_key": cantiere_key, "pratica_id": doc.get("pratica_id")}


@app.get("/api/imprese/cantieri/{cantiere_key:path}/log")
async def get_cantiere_log(cantiere_key: str, sess: dict = Depends(_require_session)):
    """Storico aggiornamenti giornalieri di un cantiere (pratica)."""
    doc = await cantieri_col.find_one({"cantiere_key": cantiere_key})
    if not doc:
        raise HTTPException(404, "Cantiere non trovato")

    # Verifica che la pratica appartenga ai lotti dell'impresa (stesso controllo di update_cantiere)
    assignment = await _find_assignment(sess["nome"])
    raw_lotti = [str(l) for l in ((assignment or {}).get("lotti") or [])]
    lotti = [_lotto_from_source(l) for l in raw_lotti]
    if _lotto_from_source(str(doc.get("lotto") or "")) not in lotti:
        raise HTTPException(403, "Pratica non assegnata a questa impresa")

    return {"log": doc.get("log", []), "cantiere_key": cantiere_key, "pratica_id": doc.get("pratica_id")}


# ── Gantt: override manuali per riga (pct/date/label), indipendenti dagli ───
# invii impresa — non tutte le fasi (es. materiali) derivano da un invio.
# Chiave riga: {lotto}|{row_id}, row_id = indice della riga nell'array
# GANTT_ROWS lato frontend (statico, quindi stabile).

@app.get("/api/gantt/overrides")
async def get_gantt_overrides(lotto: str = "", sess: dict = Depends(_require_staff_session)):
    if not lotto:
        raise HTTPException(400, "lotto required")
    cur = gantt_overrides_col.find({"lotto": lotto})
    out = {}
    async for d in cur:
        out[str(d["row_id"])] = {
            "pct": d.get("pct"),
            "start": d.get("start"),
            "end": d.get("end"),
            "date": d.get("date"),
            "label": d.get("label"),
            "sub": d.get("sub"),
            "dep_pred": d.get("dep_pred"),
            "dep_type": d.get("dep_type"),
            "dep_lag": d.get("dep_lag"),
            "updated_at": d.get("updated_at"),
            "updated_by": d.get("updated_by"),
        }
    return {"lotto": lotto, "overrides": out}


@app.put("/api/gantt/overrides/{lotto}/{row_id}")
async def upsert_gantt_override(lotto: str, row_id: str, payload: dict, sess: dict = Depends(_require_admin_session)):
    fields = {}
    for k in ("pct", "start", "end", "date", "label", "sub", "dep_pred", "dep_type", "dep_lag"):
        if k in (payload or {}):
            fields[k] = payload[k]
    if not fields:
        raise HTTPException(400, "Nessun campo da aggiornare")
    if "pct" in fields:
        try:
            fields["pct"] = max(0, min(100, int(fields["pct"])))
        except (TypeError, ValueError):
            raise HTTPException(400, "pct deve essere un intero 0-100")
    if "dep_pred" in fields:
        dep_pred = fields["dep_pred"]
        if dep_pred in (None, ""):
            fields["dep_pred"] = None
        else:
            try:
                dep_pred_int = int(dep_pred)
            except (TypeError, ValueError):
                raise HTTPException(400, "dep_pred deve essere un id riga intero")
            if str(dep_pred_int) == str(row_id):
                raise HTTPException(400, "Un task non può dipendere da se stesso")
            fields["dep_pred"] = dep_pred_int
    if "dep_type" in fields and fields["dep_type"] not in ("FS", "SS"):
        raise HTTPException(400, "dep_type deve essere 'FS' o 'SS'")
    if "dep_lag" in fields:
        try:
            fields["dep_lag"] = max(0, int(fields["dep_lag"]))
        except (TypeError, ValueError):
            raise HTTPException(400, "dep_lag deve essere un intero >= 0")
    doc = {"lotto": lotto, "row_id": row_id, **fields,
           "updated_at": _now_iso(), "updated_by": sess["nome"]}
    await gantt_overrides_col.update_one(
        {"lotto": lotto, "row_id": row_id},
        {"$set": doc, "$setOnInsert": {"created_at": _now_iso()}},
        upsert=True,
    )
    return {"ok": True, "lotto": lotto, "row_id": row_id, **fields}


@app.delete("/api/gantt/overrides/{lotto}/{row_id}")
async def delete_gantt_override(lotto: str, row_id: str, sess: dict = Depends(_require_admin_session)):
    """Ripristina il valore automatico/baseline per la riga (rimuove l'override manuale)."""
    res = await gantt_overrides_col.delete_one({"lotto": lotto, "row_id": row_id})
    return {"deleted": res.deleted_count}


# ── Tasso di produzione scavo (m/giorno), configurabile per scope ────────────
# scope è una stringa libera con 4 forme valide:
#   "global"              → default di fallback, usato se nessuna regola più specifica esiste
#   "lotto:<ID>"           es. "lotto:1A"
#   "impresa:<NOME>"       es. "impresa:ROSSI SPA" (nome esatto come in assignments_col)
#   "pratica:<CODICE>"     es. "pratica:AUT/1/1A" (codice pratica completo, univoco anche tra lotti)
# Il frontend risolve la priorità (pratica > impresa > lotto > global) leggendo tutte le
# regole in un colpo solo via GET e applicando la più specifica per ciascuna pratica.
_GANTT_RATE_SCOPE_PREFIXES = ("lotto:", "impresa:", "pratica:")


def _validate_gantt_rate_scope(scope: str) -> None:
    if scope == "global" or scope.startswith(_GANTT_RATE_SCOPE_PREFIXES):
        return
    raise HTTPException(400, "scope deve essere 'global', 'lotto:<ID>', 'impresa:<NOME>' o 'pratica:<CODICE>'")


@app.get("/api/gantt/rates")
async def get_gantt_rates(sess: dict = Depends(_require_staff_session)):
    cur = gantt_rates_col.find({})
    out = {}
    async for d in cur:
        out[d["scope"]] = {
            "m_giorno": d.get("m_giorno"),
            "updated_at": d.get("updated_at"),
            "updated_by": d.get("updated_by"),
        }
    return {"rates": out}


@app.put("/api/gantt/rates/{scope:path}")
async def upsert_gantt_rate(scope: str, payload: dict, sess: dict = Depends(_require_admin_session)):
    _validate_gantt_rate_scope(scope)
    try:
        m_giorno = float((payload or {}).get("m_giorno"))
    except (TypeError, ValueError):
        raise HTTPException(400, "m_giorno deve essere un numero")
    if m_giorno <= 0 or m_giorno > 10000:
        raise HTTPException(400, "m_giorno deve essere un valore positivo plausibile (0-10000)")
    doc = {"scope": scope, "m_giorno": m_giorno, "updated_at": _now_iso(), "updated_by": sess["nome"]}
    await gantt_rates_col.update_one(
        {"scope": scope},
        {"$set": doc, "$setOnInsert": {"created_at": _now_iso()}},
        upsert=True,
    )
    return {"ok": True, "scope": scope, "m_giorno": m_giorno}


@app.delete("/api/gantt/rates/{scope:path}")
async def delete_gantt_rate(scope: str, sess: dict = Depends(_require_admin_session)):
    """Rimuove la regola per questo scope: la pratica ricade sul livello meno specifico successivo."""
    res = await gantt_rates_col.delete_one({"scope": scope})
    return {"deleted": res.deleted_count}


# ── Trigger sync cantieri dopo approvazione Master.csv ───────────────────────
# _sync_cantieri() è già chiamata alla fine di approve_pending_update
# (vedi hook sotto) — aggiungiamo il trigger se non esiste
