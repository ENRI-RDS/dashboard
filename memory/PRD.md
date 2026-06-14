# ENRI Dashboard — PRD

## Problema originale
Dashboard project management ENRI servita da GitHub Pages, con backend FastAPI opzionale per aggiornare i dataset senza re-push su git. Esistenza di **due copie** dei file (statici nel repo + caricati dal backend) creava deriva dei dati su Render free tier (disco effimero).

## Richieste utente (progressive)
1. Risolvere conflitto file git vs backend (Render+Atlas gratis)
2. Accesso/cancellazione dei dati caricati
3. NON modificare l'autenticazione attuale (Google Apps Script)
4. ❌ OneDrive integration (annullata: utente non è admin del tenant M365)
5. **Ottimizzazioni `index.html`**
6. **Pagina per le imprese**: aggiornare/inserire pratiche direttamente, login via Google Sheet, vista filtrata per propri lotti, approvazione admin prima della pubblicazione

## Architettura attuale

```
GitHub Pages (HTML statico) ──fetch──▶ Render FastAPI ──GridFS──▶ MongoDB Atlas
            │                              │
            │                              └── DATA_DIR (file del repo) come SEED FALLBACK
            │
            └── js/api-config.js intercetta fetch('Master.csv') → fetch(API/api/data/Master.csv)
```

**Modello di storage**: MongoDB GridFS è autoritativo. I file nel repo restano come seed iniziale; servono il fallback se nessun upload esiste.

## Implementato (14/06/2026)

### Backend (`backend/server.py` v2.1)
- Storage GridFS autoritativo, fallback su disco seed
- Token accettato via header `x-upload-token` E query `?x_upload_token=...`
- Encoding-robust: legge Master.csv anche se è CP1252 / Latin-1
- Endpoint file: `/api/files`, `/api/data/{path}`, `/api/preview/{path}`, `/api/uploads`, `POST /api/upload`, `DELETE /api/uploads/{id}`, `DELETE /api/files/{path}`, `PATCH /api/files/{old}`, `POST /api/uploads/{id}/restore`
- **Endpoint imprese** (no token):
  - `GET /api/imprese/me?nome=X` — profilo + lotti assegnati
  - `GET /api/imprese/pratiche?nome=X` — Master.csv filtrato per i lotti
  - `POST /api/imprese/submit` — salva submission in `pending_updates`
  - `GET /api/imprese/my-submissions?nome=X` — storico
- **Endpoint admin** (richiede `UPLOAD_TOKEN`):
  - `GET/PUT/DELETE /api/admin/assignments[/{nome}]` — CRUD nome impresa ↔ lotti
  - `GET /api/admin/pending-updates?status=pending|approved|rejected|all`
  - `POST /api/admin/pending-updates/{id}/approve` — applica a Master.csv (genera nuova versione GridFS)
  - `POST /api/admin/pending-updates/{id}/reject` — body `{note}`

### Frontend — pagine
- **`admin.html`**: 4 tabs (File correnti, Storico versioni, **Coda imprese**, **Assegnazioni imprese**). Badge live conteggio pending. Modal anteprima/rinomina. Connection card con localStorage.
- **`imprese.html`** *(nuova)*: 3 tabs (Aggiorna pratiche, Nuova pratica, Le mie submission). Login implicito via `_enri_user` di localStorage; backend verifica `assignments`. Coda modifiche prima dell'invio. Reload "Le mie" mostra stato approvato/rifiutato.
- **`hub.html`**: nuova card "Area Impresa" mostrata se backend conferma assegnazione.
- **Tutte le pagine**: ora include `<script src="js/api-config.js"></script>` → fetch CSV/GeoJSON ridiretti al backend automaticamente.

### Ottimizzazioni `index.html`
- ✅ Include `js/api-config.js` → finalmente i dati arrivano dal backend live
- ✅ **Lazy-load `xlsx.full.min.js`** (~900 KB): caricato solo al primo click "Esporta Excel" tramite `window.loadXLSX()`
- 🟡 Da fare in futuro: estrarre CSS comune in `css/dashboard.css` (cache cross-page)

### Fix collaterali
- `js/api-config.js`: era avvolto in virgolette JSON (corrotto), ricostruito
- `backend/.env`: creato per ambiente preview
- `frontend/server.js`: static server minimo per slot supervisor Emergent

## Modello dati Mongo (oggi)

```jsonc
// uploads (storico versioni file)
{ _id, filename, gridfs_id, deleted_at, uploaded_at, size, rows, source?, note? }

// assignments (mapping impresa → lotti)
{ _id, nome, lotti: ["Lotto 1", "Lotto 1A"], active: true, created_at, updated_at }

// pending_updates (workflow approvazione)
{ _id, nome, type: "update"|"new",
  changes: [{tratta_id, ente, tipo_permesso, fields:{...}} | {row dict}],
  status: "pending"|"approved"|"rejected",
  submitted_at, reviewed_at, applied_upload_id, reviewed_note, summary
}
```

## Flusso impresa end-to-end

1. Admin → `admin.html` tab "Assegnazioni imprese" → aggiunge `Costruzioni Alfa Srl` con lotti `Lotto 1, Lotto 1A`
2. L'impresa accede su `hub.html` (Apps Script login) con nome esattamente `Costruzioni Alfa Srl`
3. Hub mostra card "Area Impresa" (verifica backend `/api/imprese/me`)
4. Click → `imprese.html` → tab "Aggiorna pratiche" o "Nuova pratica"
5. Coda modifiche → "Invia per approvazione"
6. Admin → tab "Coda imprese" (badge mostra conteggio) → Approva
7. Backend legge Master.csv corrente, applica `changes` con pandas, crea nuova versione GridFS
8. Dashboard `/api/data/Master.csv` serve immediatamente la nuova versione (no redeploy)

## Personas
- **Admin RDS**: carica file, gestisce assegnazioni imprese, approva coda
- **Impresa appaltatrice**: aggiorna pratiche dei propri lotti via `imprese.html`
- **PM / direzione**: visualizza dashboard (index.html, scavi.html, mappa.html, executive_summary.html)

## Backlog (P0/P1/P2)
- P1 — Email/Telegram notification quando arriva nuova submission impresa
- P1 — Estrarre CSS in `css/dashboard.css` condiviso (cache cross-page)
- P1 — Badge "Ultimo upload: …" su hub.html card Admin
- P2 — Filtro avanzato impresa: nascondere pratiche già "OTTENUTE" di default
- P2 — Storico Excel-export per audit modifiche (chi ha cambiato cosa quando)
- P2 — Auto-commit dei file uploadati sul repo GitHub Pages
- P3 — Webhook su nuovo upload

_Ultimo aggiornamento: 2026-06-14_
