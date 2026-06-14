# ENRI Dashboard — PRD

## Problema originale
Dashboard project management ENRI servita da GitHub Pages, con backend FastAPI opzionale per aggiornare i dataset senza re-push su git. Esistenza di **due copie** dei file (statici nel repo + caricati dal backend) creava deriva dei dati su Render free tier (disco effimero).

Richiesta utente:
- Risolvere conflitto file git vs backend (Render gratis + Mongo Atlas gratis)
- Permettere accesso/cancellazione dei dati caricati
- NON modificare l'autenticazione attuale (Google Apps Script)
- Massima efficienza token

## Architettura attuale

```
GitHub Pages (HTML statico) ──fetch──▶ Render FastAPI ──GridFS──▶ MongoDB Atlas
                                            │
                                            └── DATA_DIR (file del repo) come SEED FALLBACK
```

**Modello di storage (nuovo)**: MongoDB GridFS è autoritativo per i file caricati.
I file presenti nel repo (`Master.csv`, `QGIS.geojson`, ecc.) sono usati solo come
**seed iniziale** quando un nome file non ha ancora upload. Questo elimina la
perdita dati causata dal disco effimero di Render.

## Implementato (14/06/2026)

### Backend (`backend/server.py` v2.0)
- Storage GridFS (`bucket = files`) per ogni upload
- `uploads` collection con `gridfs_id`, `deleted_at` per soft-delete + audit
- Endpoint nuovi/aggiornati:
  - `GET /api/files` — lista unificata mongo+disk con `source` flag e conteggio versioni
  - `GET /api/data/{path}` — serve da Mongo se presente, altrimenti fallback al disco
  - `GET /api/data-text/{path}` — versione plain text
  - `GET /api/preview/{path}?max_bytes=N` — anteprima primi N byte (default 8KB)
  - `POST /api/upload` — salva il contenuto in GridFS (non più su disco)
  - `GET /api/uploads?filename=&include_deleted=` — storico con filtri
  - `DELETE /api/uploads/{id}` — elimina singola versione (purge GridFS)
  - `DELETE /api/files/{path}` — elimina tutte le versioni di un file
  - `PATCH /api/files/{old}` — rinomina file (body JSON `{new_name}`)
  - `POST /api/uploads/{id}/restore` — ripristino (solo se gridfs intatto)
- Backfill automatico di `deleted_at=None` su upload preesistenti
- CORS, token upload, validazione path, dimensione max immutati

### Frontend (`admin.html` redesign)
- Endpoint API + token salvati in `localStorage` (chiavi `enri_api_base`, `enri_upload_token`)
- Tab "File correnti": preview, scarica, rinomina, elimina tutte le versioni
- Tab "Storico versioni": elimina singola versione, filtro `include_deleted`
- Modal di anteprima (16KB) + modal di rinomina
- Indicatori `source: disk|mongo` per ogni file
- Statistiche live: stato API, n. file, n. versioni Mongo
- Data-testid presenti su tutti gli elementi interattivi

### Fix collaterali
- Riparato `js/api-config.js` (era avvolto in virgolette JSON, JS non parsabile)
- Creato `backend/.env` con default per ambiente preview
- Creato `frontend/server.js` minimo per servire il sito statico da `/app/` (slot supervisor Emergent)

## Auth (NON toccato per scelta utente)
- Login via Google Apps Script in `hub.html` rimane invariato
- `localStorage._enri_user` / `_enri_role` rimangono il meccanismo di autorizzazione
- Token upload backend resta `UPLOAD_TOKEN` env var

## Personas
- **Admin (RDS)**: carica nuovi Master.csv/GeoJSON, cancella vecchie versioni, rinomina file
- **PM / staff**: consultano la dashboard sulle pagine fase1/fase2/mappa
- **Direzione**: vista executive summary

## Backlog (P0/P1/P2)
- P1 — Indicatore "Ultimo upload: …" sulla card Admin in hub.html (richiede 1 fetch `/api/uploads?limit=1`)
- P1 — Auto-commit dei file uploadati sul repo GitHub (così la copia statica resta in sync con MongoDB)
- P2 — Notifica Telegram/Slack su nuovo upload
- P2 — Quota / dashboard storage Atlas free (warn quando ci si avvicina ai 512MB)
- P2 — Multi-impresa con utenti separati + tokens distinti

## Prossimi passi possibili
- Aggiungere card "Ultimo upload" in hub.html (vedi P1)
- Implementare badge "ultimo update" nelle pagine index.html / scavi.html
- Migrazione dati esistenti: opzionale `POST /api/import-seed` per copiare i CSV/GeoJSON dal disco a GridFS in un colpo solo

_Ultimo aggiornamento: 2026-06-14_
