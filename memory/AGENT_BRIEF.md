> Documento di onboarding per agenti AI / sviluppatori. Spiega architettura, flussi, file e convenzioni della dashboard ENRI **senza dover leggere tutto il codice**.

---

## 1. Cos'è questo progetto

Dashboard di project management per **ENRI** — un progetto infrastrutturale di posa fibra/cavidotti (12 lotti, 7 cluster, province BG/MI/MB/PV/CR). Mostra l'avanzamento di:
- **Fase 1 – Progettazione**: iter autorizzativo (richieste, scadenze, pratiche per cluster/lotto/ente).
- **Fase 2 – Avanzamento Lavori (Scavi)**: stato cantieri, % completamento, metri scavati.
- **Sopralluoghi, Mappa georeferenziata, Executive Summary, Milestone, AI Alerts**.

È una **dashboard solo-frontend statica** (HTML + JS vanilla, niente React/Vue) servita da **GitHub Pages**, con un **backend opzionale FastAPI** che permette di aggiornare i dataset senza re-pushare su GitHub.

---

## 2. Architettura a colpo d'occhio

```
┌──────────────────────────────┐        ┌────────────────────────────┐         ┌──────────────────────┐
│ GitHub Pages                 │        │ Render (FastAPI)           │         │ MongoDB Atlas        │
│ enri-rds.github.io/dashboard │ ─────▶ │ enri-dashboard-api.        │ ──────▶ │ DB: enri_dashboard   │
│ HTML + JS vanilla            │ fetch  │   onrender.com             │  motor  │ - uploads            │
│ Pagine: hub, index, mappa,…  │        │ /api/* endpoints           │         │ - datasets           │
└──────────────────────────────┘        └────────────────────────────┘         └──────────────────────┘
        │                                            ▲
        │ js/api-config.js                           │ multipart upload (Excel→CSV / GeoJSON)
        │ intercetta fetch('Master.csv') e la        │
        │ riscrive in fetch(API/api/data/Master.csv) │
        └────────────────────────────────────────────┘

Auth login → Google Apps Script (URL hardcoded in hub.html) che restituisce ruolo: admin / admin2 / user
```

**Tre modi di girare:**
1. **Production**: GitHub Pages + Render + Atlas.
2. **Preview Emergent**: static HTML servito da `frontend/server.js` su :3000, FastAPI su :8001, MongoDB locale.
3. **Solo statico**: GitHub Pages senza backend (le pagine fanno `fetch('Master.csv')` direttamente sui file committati nel repo).

---

## 3. Struttura del repository

```
/app/
├── hub.html                      # PORTALE — entrypoint con login + griglia di card
├── index.html                    # FASE 1 — Progettazione (KPI, GANTT, tabella pratiche)
├── scavi.html                    # FASE 2 — Avanzamento scavi (lotti, cluster, donut)
├── mappa.html                    # Mappa Leaflet con lotti + tratte + attraversamenti SED
├── executive_summary.html        # Riepilogo direzionale + GANTT
├── sopralluoghi.html             # Redazione verbali di sopralluogo cantiere
├── milestone.html                # Milestone di progetto
├── ai_alerts.html                # Beta — alert predittivi
├── admin.html                    # PANNELLO ADMIN — upload Excel/CSV/GeoJSON al backend
│
├── M/                            # Sotto-progetto \"M\" (varianti di mappa.html e geojson)
│   ├── Hub.html
│   ├── mappa.html
│   └── QGIS_3.geojson
├── pm/                           # Sezione Project Management (5 pagine)
│   └── *.html
│
├── js/
│   └── api-config.js             # **CRUCIALE** — intercetta fetch() e li reindirizza al backend
│
├── Master.csv                    # Dataset principale pratiche (delimitatore `;`)
├── Riepilogo_progettazione.csv   # Riepilogo cluster
├── dati.csv                      # Dati ausiliari
├── QGIS.geojson                  # Tracciato lotti (geometrie principali)
├── QTS.geojson                   # Tracciato secondario
├── SED_classificato.geojson      # Attraversamenti (Stradali / Ferroviari / Idrici)
│
├── backend/
│   ├── server.py                 # FastAPI app — vedi §5
│   ├── requirements.txt
│   ├── .env                      # MONGO_URL, DB_NAME, UPLOAD_TOKEN, ALLOWED_ORIGINS, ...
│   └── .env.example
│
├── frontend/                     # Server statico per preview Emergent (NON in produzione)
│   ├── server.js                 # Node http.server che serve /app su :3000
│   └── package.json
│
├── DEPLOY.md                     # Guida deploy Render + Atlas + GitHub Pages
├── Procfile                      # `web: uvicorn server:app --host 0.0.0.0 --port $PORT`
└── memory/
    ├── PRD.md                    # Stato avanzamento del progetto
    ├── test_credentials.md       # Credenziali backend per preview
    └── AGENT_BRIEF.md            # ← QUESTO FILE
```

---

## 4. Pagine — cosa fa ognuna

| Pagina | Ruolo necessario | Cosa mostra / fa |
|---|---|---|
| **hub.html** | nessuno (login obbligatorio) | Login via Google Apps Script + griglia 7 card (Progettazione, Lavori, Mappa, Executive, Sopralluoghi, Milestone, AI Alerts, **Admin** se admin) |
| **index.html** | tutti | KPI fase Submission/Approval, GANTT pratiche, tabella filtrabile, modal pratiche, grafici Chart.js |
| **scavi.html** | `admin`/`admin2` | KPI scavi (non avviati/in corso/sospeso/completati), barre per lotto e per cluster, donut chart, modal lotto/cluster |
| **mappa.html** | tutti | Leaflet — disegna lotti/tratte da `QGIS.geojson`, punti SED da `SED_classificato.geojson`, basemaps Google-style, ricerca, misurazione distanze, esportazione PDF |
| **executive_summary.html** | tutti | Riepilogo per direzione (GANTT, milestone critiche, scostamenti) |
| **sopralluoghi.html** | tutti | Form di redazione verbale + download PDF |
| **milestone.html** | tutti | Milestone contrattuali e di impresa |
| **ai_alerts.html** | tutti (Beta) | Pagina placeholder per future analisi AI |
| **admin.html** | richiede `UPLOAD_TOKEN` | Form upload (Excel/CSV/GeoJSON) → POST `/api/upload`, storico ultimi caricamenti |

Tutte le pagine fanno guardia: `if (!localStorage.getItem('_enri_user')) → mostra overlay/redirect a hub.html`.

---

## 5. Backend FastAPI (`backend/server.py` v2)

**Modello di storage**: MongoDB GridFS è AUTORITATIVO per i file caricati. I CSV/GeoJSON
nel repo (`/app/Master.csv`, ecc.) sono usati come SEED FALLBACK quando non esiste
ancora un upload per quel nome. Questo elimina la perdita dati su Render free tier
(disco effimero) e mantiene piena retro-compatibilità con GitHub Pages.

**Tutte le rotte iniziano con `/api/`**.

| Metodo | Endpoint | Auth | Descrizione |
|---|---|---|---|
| GET  | `/api/`                            | — | Health JSON di servizio |
| GET  | `/api/health`                      | — | Ping a MongoDB |
| GET  | `/api/files`                       | — | Lista unificata: file con upload Mongo attivi + seed su disco. Ogni file ha `source: 'mongo'|'disk'`, `versions`, `size`, `modified` |
| GET  | `/api/data/{path}`                 | — | Scarica file: Mongo se presente, altrimenti disco. Usato da `js/api-config.js` |
| GET  | `/api/data-text/{path}`            | — | Idem in `text/plain` |
| GET  | `/api/preview/{path}?max_bytes=N`  | — | Anteprima primi N byte (256–65536, default 8192). Restituisce `{source, size, truncated, content}` |
| GET  | `/api/uploads?limit=&project=&filename=&include_deleted=` | — | Storico upload (con filtri + audit cancellati) |
| POST | `/api/upload`                      | `UPLOAD_TOKEN` (form `token` o header `x-upload-token`) | Multipart: `file`, `target` (opz), `project`, `convert_to_csv`. Salva contenuto in **GridFS** e crea record in `uploads`. Excel → CSV `;` se richiesto |
| DELETE | `/api/uploads/{id}`              | `UPLOAD_TOKEN` | Soft-delete di una singola versione + purge GridFS (libera spazio Atlas) |
| DELETE | `/api/files/{path}`              | `UPLOAD_TOKEN` | Soft-delete di TUTTE le versioni del file. Se esiste un seed su disco, ritorna a essere servito; altrimenti il file ritorna 404 |
| PATCH  | `/api/files/{old}`               | `UPLOAD_TOKEN` | Body JSON `{"new_name": "..."}` — rinomina tutte le versioni attive (errore 409 se il nuovo nome è già in uso) |
| POST   | `/api/uploads/{id}/restore`      | `UPLOAD_TOKEN` | Ripristina una versione cancellata (errore 410 se i byte GridFS sono già stati purgati) |

### Schema `uploads`
```jsonc
{
  "_id": ObjectId,
  "filename": "Master.csv",
  "original_name": "Master_v2.xlsx",
  "size": 192167,
  "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "project": "main",
  "rows": 3120,
  "uploaded_at": "2026-06-14T11:30:00+00:00",
  "gridfs_id": ObjectId,       // → fs.files (None se cancellato)
  "deleted_at": null | "ISO"
}
```
File content in `fs.files` / `fs.chunks` (motor `AsyncIOMotorGridFSBucket`).

### Variabili d'ambiente (`backend/.env`)

| Key | Significato |
|---|---|
| `MONGO_URL`        | URI Mongo (locale o Atlas) |
| `DB_NAME`          | `enri_dashboard` |
| `DATA_DIR`         | Cartella dove salvare i file (default: parent di `/backend`, in preview `/app`) |
| `ALLOWED_ORIGINS`  | CSV di origini CORS (deve includere `https://enri-rds.github.io` + il dominio preview/Render) |
| `UPLOAD_TOKEN`     | Token condiviso che protegge upload/delete. Generare con `openssl rand -hex 32` |
| `MAX_UPLOAD_MB`    | Default `25` |

### Modello dati MongoDB

```jsonc
// collection: uploads (storico di ogni upload)
{
  \"_id\": ObjectId,
  \"filename\": \"M/QGIS_3.geojson\",      // path relativo a DATA_DIR
  \"original_name\": \"QGIS_3.geojson\",
  \"size\": 119744,
  \"content_type\": \"application/geo+json\",
  \"project\": \"M\",                       // \"main\" | \"M\" | \"pm\"
  \"rows\": 1234 | null,                  // numero righe se CSV
  \"uploaded_at\": \"2026-06-14T09:48:49+00:00\"
}

// collection: datasets (snapshot \"ultima versione\" per ciascun file)
{
  \"_id\": ObjectId,
  \"name\": \"Master.csv\",
  \"size\": 192167,
  \"rows\": 3120,
  \"project\": \"main\",
  \"updated_at\": \"2026-06-14T09:48:49+00:00\"
}
```

### Vincoli di sicurezza (già in `server.py`)
- `_safe_relpath()` impedisce path-traversal (`..`, `\`, caratteri fuori `[A-Za-z0-9_\-./]`).
- Estensioni accettate: `.csv`, `.xlsx`, `.xls`, `.geojson`, `.json` (ALLOWED_EXT).
- Limite dimensione = `MAX_UPLOAD_MB`.

---

## 6. Come i file caricati arrivano sul frontend — `js/api-config.js`

Ogni pagina HTML include `<script src=\"js/api-config.js\"></script>` in `<head>`. Questo script:

1. Legge `window.ENRI_API_BASE` **oppure** `localStorage.getItem('enri_api_base')`.
2. Se vuoto → non fa nulla, le pagine leggono i CSV/GeoJSON statici committati nel repo (comportamento storico).
3. Se valorizzato → **monkey-patcha `window.fetch`**: ogni `fetch('Master.csv')`, `fetch('QGIS.geojson')`, ecc. viene **riscritta in** `fetch(`${API_BASE}/api/data/Master.csv`)`. Quindi tutto il codice esistente delle pagine continua a funzionare senza modifiche — i dati arrivano dal backend live invece che dai file statici del repo.

Per attivarlo da una qualunque pagina:
```js
localStorage.setItem('enri_api_base', 'https://enri-dashboard-api.onrender.com');
location.reload();
// per disattivare:
localStorage.removeItem('enri_api_base');
```

**Flusso completo di un upload:**
1. Admin apre `admin.html`, incolla API endpoint + UPLOAD_TOKEN, trascina `Master_v2.xlsx`, opzionalmente specifica `target=Master.csv` e `project=main`.
2. Frontend → `POST /api/upload` multipart.
3. Backend: legge XLSX con `pandas.read_excel` → converte in CSV `;` → scrive su `DATA_DIR/Master.csv` → inserisce record in `uploads` → upsert in `datasets`.
4. Tutte le altre pagine, alla prossima `fetch('Master.csv')`, ricevono il nuovo file (perché `api-config.js` le instrada al backend).

---

## 7. Auth & ruoli

Esistono **due livelli** di protezione:

### 7.1 Login utente (hub.html)
Implementato via **Google Apps Script** (URL hardcoded `APPS_SCRIPT_URL` in `hub.html`). Il body `{ secret, action:'login', nome, codice }` viene inviato in POST. La risposta restituisce `{ ok, nome, ruolo }` dove `ruolo ∈ {admin, admin2, user}`. Il dato è salvato in `localStorage`:

```js
localStorage._enri_user   // nome canonico
localStorage._enri_role   // 'admin' | 'admin2' | 'user'
```

L'admin gestisce gli accessi modificando lo sheet Google collegato all'Apps Script (è la richiesta esplicita del committente).

In `hub.html` due liste filtrano le card:
- `SCAVI_ALLOWED_ROLES = ['admin', 'admin2']` → mostra/blocca card \"Avanzamento Lavori\".
- `ADMIN_ALLOWED_ROLES = ['admin', 'admin2']` → mostra/nasconde card \"Pannello Admin\".

### 7.2 Upload backend
Tutte le scritture (`POST /api/upload`, `DELETE /api/uploads/{id}`) richiedono `UPLOAD_TOKEN`. In `admin.html` l'admin lo incolla manualmente nel form (oppure lo salva in `localStorage.enri_api_base` per riempire l'endpoint).

---

## 8. Convenzioni & gotchas

1. **CSV delimitatore `;`** (formato italiano). Tutti i `read_excel` lato backend producono `;`.
2. **CORS**: il backend espone solo le origini in `ALLOWED_ORIGINS`. Aggiungere il dominio preview/Render quando cambia.
3. **Path con sotto-cartelle**: `target=M/QGIS_3.geojson` salva in `DATA_DIR/M/QGIS_3.geojson`. La regex `_SAFE_NAME_RE` accetta `/` ma rifiuta `..`.
4. **Render free tier**: il servizio si spegne dopo 15min di inattività, prima chiamata ~30s di cold-start.
5. **Anti-FOUC su `mappa.html`**: la sidebar viene nascosta via `<style>` inline _prima_ del DOMContentLoaded per evitare flash su mobile.
6. **`view-transition`** è abilitato in tutte le pagine (`@view-transition { navigation: auto }`): transizioni native tra hub→pagine.
7. **Tracking accessi su `scavi.html`**: invia ping a una bin JSONBin tramite proxy Apps Script (vedi snippet in fondo a `scavi.html`). Non parte se `_enri_user` è vuoto.
8. **Font**: Bricolage Grotesque (titoli), Plus Jakarta Sans (testo), Fira Code (mono), DM Sans/DM Mono solo su mappa.html.
9. **Niente React/build step.** Si modifica HTML→push→GitHub Pages aggiorna. Nessun `npm run build`.
10. **Cartella `frontend/` esiste solo nella preview** per rispettare lo slot `supervisor` di Emergent. NON va pushata su GitHub (o serve solo come placeholder).

---

## 9. Setup locale rapido

```bash
# Backend
cd /app/backend
cp .env.example .env          # poi modifica MONGO_URL, UPLOAD_TOKEN, ALLOWED_ORIGINS
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn server:app --reload --port 8001

# Frontend statico (qualsiasi server statico va bene)
cd /app
python -m http.server 5500
# → http://localhost:5500/hub.html
# In console del browser:
localStorage.setItem('enri_api_base', 'http://localhost:8001');
```

In ambiente **Emergent preview**:
- Backend gestito da supervisor (`/etc/supervisor/conf.d/supervisord.conf`, slot `backend`, porta 8001).
- Frontend statico gestito da supervisor slot `frontend` (`node /app/frontend/server.js`, porta 3000).
- MongoDB locale via supervisor slot `mongodb`.
- URL pubblico: `https://data-manager-portal.preview.emergentagent.com` (ingress mappa `/api/*`→8001, resto→3000).

---

## 10. Estensioni più probabili (backlog)

| P | Idea | Note |
|---|---|---|
| P1 | Generazione automatica del GeoJSON dai CSV uploadati | Oggi vanno caricati entrambi separatamente |
| P1 | Auto-commit dei file uploadati su GitHub Pages | Per non avere \"deriva\" tra copia live (backend) e copia statica (repo) |
| P2 | Multi-impresa con utenti separati | Oggi c'è 1 solo `UPLOAD_TOKEN` condiviso |
| P2 | Integrazione AI per `ai_alerts.html` (GPT-5.2 / Claude Sonnet 4.5 / Gemini 3) | Analisi storico avanzamento, anomalie, report narrativo |
| P2 | Badge \"Ultimo upload: …\" sulla card Admin in `hub.html` | Fetch su `/api/uploads?limit=1` |
| P3 | Webhook Telegram/Slack su nuovo upload | Notifica al PM |

---

## 11. Quick reference per agenti

**Per aggiungere una nuova pagina al portale:**
1. Crea `nuova_pagina.html` in `/app/` con header/topbar coerente (vedi `scavi.html` come template).
2. Aggiungi `<script src=\"js/api-config.js\"></script>` in `<head>` se carica dati.
3. Aggiungi guardia auth: `if (!localStorage._enri_user) → redirect a hub.html`.
4. In `hub.html`, duplica una `<a class=\"nav-card …\">…</a>` e aggiungi al cluster di card. Se ruolo-protetta, replicare il pattern di `#adminCard` o `#scaviCard`.

**Per esporre una nuova rotta backend:**
1. Aggiungi `@app.get(\"/api/...\")` in `backend/server.py`.
2. Se modifica i dati, proteggi con `_check_token(...)`.
3. Restituisci JSON (no `ObjectId` raw → casta a `str`).
4. Aggiungi descrizione in `DEPLOY.md` §5.

**Per debuggare \"non vedo i dati nuovi\":**
1. Apri DevTools del browser, controlla console: deve apparire `[ENRI] API base active → https://...`.
2. Verifica `localStorage.enri_api_base` valorizzato.
3. Verifica CORS sul backend: `curl -I -X OPTIONS https://api/.../api/files -H \"Origin: https://enri-rds.github.io\"`.
4. Verifica `/api/files` restituisca il file aggiornato.

---

_Ultima revisione: 2026-06-14 — quando aggiorni significativamente il progetto, aggiorna anche questo file._
"
Observation: Create successful: /app/memory/AGENT_BRIEF.md
