> Documento di onboarding per agenti AI / sviluppatori. Spiega architettura, flussi, file e convenzioni della dashboard ENRI **senza dover leggere tutto il codice**.

---

## 1. Cos'è questo progetto

Dashboard di project management per **ENRI** — un progetto infrastrutturale di posa fibra/cavidotti (12 lotti, 7 cluster, province BG/MI/MB/PV/CR). Mostra l'avanzamento di:
- **Fase 1 – Progettazione**: iter autorizzativo (richieste, scadenze, pratiche per cluster/lotto/ente).
- **Fase 2 – Avanzamento Lavori (Scavi)**: stato cantieri, % completamento, metri scavati.
- **Sopralluoghi, Mappa georeferenziata, Executive Summary, Milestone, AI Alerts**.
- **Area Impresa**: portale per le imprese appaltatrici (vista pratiche dei propri lotti + workflow di approvazione admin).

È una **dashboard solo-frontend statica** (HTML + JS vanilla, niente React/Vue) servita da **GitHub Pages**, con un **backend FastAPI** (Render) che permette di aggiornare i dataset senza re-pushare su GitHub e che — dopo ogni approvazione — committa anche la versione aggiornata direttamente nel repo.

---

## 2. Architettura a colpo d'occhio

```
┌──────────────────────────────┐    ┌────────────────────────────┐    ┌──────────────────────┐
│  GitHub Pages                │    │  Render (FastAPI)          │    │  MongoDB Atlas       │
│  enri-rds.github.io/dashboard│───▶│  enri-dashboard-api.       │───▶│  DB: enri_dashboard  │
│  HTML + JS vanilla           │    │  onrender.com              │    │   - uploads (GridFS) │
│  Pagine: hub, index, mappa,… │    │  /api/* endpoints          │    │   - assignments      │
└──────────────────────────────┘    └────────────────────────────┘    │   - pending_updates  │
        │                                       ▲                      └──────────────────────┘
        │ js/api-config.js                      │ multipart upload + workflow imprese
        │ intercetta fetch('Master.csv') e la   │
        │ riscrive in fetch(API/api/data/…)     │
        └───────────────────────────────────────┘
                                                │
                                                ▼ commit automatico (GitHub API, fine-grained PAT)
                                       Master.csv / QGIS.geojson / Riepilogo_progettazione.csv

Auth login: hub.html → POST /api/auth/login → backend chiama Google Apps Script server-to-server →
            restituisce token HMAC firmato (nome|ruolo|exp) salvato in localStorage._enri_token.
            Ogni chiamata /api/imprese/* o /api/auth/* richiede l'header `x-session-token`.
```

**Tre modi di girare:**
1. **Production**: GitHub Pages + Render + Atlas.
2. **Preview Emergent**: static HTML servito da `frontend/server.js` su :3000, FastAPI su :8001, MongoDB locale.
3. **Solo statico**: GitHub Pages senza backend (le pagine fanno `fetch('Master.csv')` direttamente sui file committati nel repo — sempre presenti perché il backend ri-pusha dopo ogni approvazione).

---

## 3. Struttura del repository

```
/app/
├── hub.html                    # PORTALE — entrypoint con login (via /api/auth/login) + griglia di card
├── index.html                  # FASE 1 — Progettazione (KPI, GANTT, tabella pratiche) — SWR su Master.csv
├── scavi.html                  # FASE 2 — Avanzamento scavi (lotti, cluster, donut)
├── mappa.html                  # Mappa Leaflet con SWR su QGIS / QTS / SED / Master
├── mappa_impresa.html          # NEW — variante della mappa filtrata sui lotti dell'impresa loggata
├── executive_summary.html      # Riepilogo direzionale + GANTT
├── sopralluoghi.html           # Redazione verbali di sopralluogo cantiere
├── milestone.html              # Milestone di progetto
├── ai_alerts.html              # Beta — alert predittivi
├── admin.html                  # PANNELLO ADMIN — upload + coda imprese + assegnazioni + storico versioni
├── imprese.html                # PORTALE IMPRESE — aggiorna pratiche / nuova tratta / mie submission
│
├── M/                          # Sotto-progetto "M" (varianti di mappa.html e geojson) — non trattato qui
│
├── pm/                         # Sezione Project Management
│   └── *.html
│
├── js/
│   └── api-config.js           # **CRUCIALE** — intercetta fetch() e li reindirizza al backend
│
├── Master.csv                  # Dataset principale pratiche — separatore auto-rilevato (TAB o ;)
├── Riepilogo_progettazione.csv # Riepilogo cluster — RIGENERATO automaticamente da Master.csv
├── dati.csv                    # Dati ausiliari (in disuso — index.html calcola la stessa aggregazione lato client)
├── QGIS.geojson                # Tracciato lotti — RIGENERATO automaticamente (properties di stato) da Master.csv
├── QTS.geojson                 # Tracciato secondario
├── SED_classificato.geojson    # Attraversamenti (Stradali / Ferroviari / Idrici)
│
├── backend/
│   ├── server.py               # FastAPI app v2.0.0 — vedi §5
│   ├── requirements.txt        # include httpx (chiamate GitHub API + Apps Script)
│   ├── .env                    # MONGO_URL, DB_NAME, UPLOAD_TOKEN, SESSION_SECRET, APPS_SCRIPT_*, GITHUB_*
│   └── .env.example
│
├── frontend/                   # Server statico per preview Emergent (NON in produzione)
│   ├── server.js
│   └── package.json
│
├── DEPLOY.md                   # Guida deploy Render + Atlas + GitHub Pages
├── Procfile                    # `web: uvicorn server:app --host 0.0.0.0 --port $PORT`
└── memory/
    ├── PRD.md                  # Stato avanzamento del progetto
    ├── test_credentials.md     # Credenziali backend per preview
    └── AGENT_BRIEF.md          # ← QUESTO FILE
```

---

## 4. Pagine — cosa fa ognuna

| Pagina | Ruolo necessario | Cosa mostra / fa |
|---|---|---|
| **hub.html** | nessuno (login obbligatorio) | Login server-side (POST `/api/auth/login`) + griglia card. Mostra card "Area Impresa" solo se backend conferma assegnazione. Anti-FOUC: card nascoste finché non torna il ruolo. Cold-start Render: timeout 10s e pannello "Riprova" |
| **index.html** | tutti | KPI, GANTT, tabella filtrabile, modal pratiche, grafici Chart.js. **SWR**: legge GitHub statico + Render in parallelo, ridisegna live se il backend ha dati più freschi. XLSX caricato lazy al primo "Esporta Excel" |
| **scavi.html** | `admin`/`admin2` | KPI scavi (non avviati/in corso/sospeso/completati), barre per lotto e per cluster, donut chart, modal lotto/cluster |
| **mappa.html** | tutti | Leaflet — SWR su `QGIS.geojson`, `QTS.geojson`, `SED_classificato.geojson`, `Master.csv`; ricostruzione live dei layer se cambiano. Basemaps Google-style, ricerca, misurazione distanze, esportazione PDF |
| **mappa_impresa.html** | impresa loggata | Variante di mappa.html filtrata sui lotti assegnati all'impresa (usa `/api/imprese/pratiche` e session token) |
| **executive_summary.html** | tutti | Riepilogo per direzione (GANTT, milestone critiche, scostamenti) |
| **sopralluoghi.html** | tutti | Form di redazione verbale + download PDF |
| **milestone.html** | tutti | Milestone contrattuali e di impresa |
| **ai_alerts.html** | tutti (Beta) | Pagina placeholder per future analisi AI |
| **admin.html** | richiede `UPLOAD_TOKEN` | 4 tabs (File correnti, Storico versioni, **Coda imprese**, **Assegnazioni imprese**). Badge live conteggio pending, modali HTML custom (no `alert/confirm/prompt` nativi), date in formato `gg/mm/aa, hh:mm`, righe coda colorate per tipo |
| **imprese.html** | impresa assegnata + session token | 3 tabs (Aggiorna pratiche / Nuova pratica / Le mie submission). Campo **Ente** = select dinamica + opzione "Altro"; **Pratica** in nuova tratta = solo numero progressivo editabile (prefisso AUT/NO + suffisso lotto calcolati). Selezionando una tratta, se altre tratte condividono ENTE+TIPO+PRATICA il sistema propone di aggiornarle tutte insieme |

Tutte le pagine fanno guardia: `if (!localStorage.getItem('_enri_user')) → mostra overlay/redirect a hub.html`. Le pagine impresa controllano anche `localStorage._enri_token`.

---

## 5. Backend FastAPI (`backend/server.py` v2.0.0)

**Modello di storage**: MongoDB GridFS è AUTORITATIVO per i file caricati. I CSV/GeoJSON
nel repo (`/app/Master.csv`, ecc.) sono usati come SEED FALLBACK quando non esiste
ancora un upload per quel nome. Dopo ogni approvazione/eliminazione/ripristino il
backend pusha anche su GitHub via API (token fine-grained, scope `Contents: Read and write`)
così la copia statica del repo non va più in deriva da quella servita live.

**Tutte le rotte iniziano con `/api/`**.

### 5.1 File / dataset

| Metodo | Endpoint | Auth | Descrizione |
|---|---|---|---|
| GET | `/api/` | — | Health JSON di servizio |
| GET | `/api/health` | — | Ping a MongoDB |
| GET | `/api/files` | — | Lista unificata: Mongo + seed disco. Ogni file: `source: 'mongo'\|'disk'`, `versions`, `size`, `modified` |
| GET | `/api/data/{path}` | — | Scarica file (Mongo se presente, altrimenti disco). Usato da `js/api-config.js` |
| GET | `/api/data-text/{path}` | — | Idem in `text/plain` |
| GET | `/api/preview/{path}?max_bytes=N` | — | Anteprima primi N byte (256–65536, default 8192) |
| GET | `/api/uploads?limit=&project=&filename=&include_deleted=` | — | Storico upload (filtri + audit cancellati) |
| POST | `/api/upload` | `UPLOAD_TOKEN` (form `token` o header `x-upload-token`) | Multipart: `file`, `target` opz., `project`, `convert_to_csv`. Salva in GridFS + record `uploads`. Excel → CSV auto. **Se il target è `Master.csv` pusha su GitHub e rigenera QGIS/Riepilogo** |
| DELETE | `/api/uploads/{id}` | `UPLOAD_TOKEN` | Soft-delete singola versione + purge GridFS. Se era Master.csv ri-pusha lo stato corrente |
| DELETE | `/api/files/{path}` | `UPLOAD_TOKEN` | Soft-delete di TUTTE le versioni — se esiste un seed disco, torna a essere servito |
| PATCH | `/api/files/{old}` | `UPLOAD_TOKEN` | Body JSON `{"new_name": "..."}` (409 se collide) |
| POST | `/api/uploads/{id}/restore` | `UPLOAD_TOKEN` | Ripristina versione cancellata (410 se i byte GridFS sono già stati purgati) |

### 5.2 Auth firmata (HMAC)

Risolve la falla per cui chiunque poteva fare `localStorage.setItem('_enri_user', 'Nome Impresa')`
e impersonare quel nome senza conoscere il codice. Ora il codice è verificato dal backend e ogni
chiamata successiva richiede un token firmato non falsificabile.

| Metodo | Endpoint | Auth | Descrizione |
|---|---|---|---|
| POST | `/api/auth/login` | — | Body `{nome, codice}` → backend chiama Apps Script server-to-server (segreto MAI esposto al browser) → restituisce `{ok, token, nome, ruolo}` |
| GET | `/api/auth/verify` | session token | Rivalidazione silenziosa (usata da index.html) |
| POST | `/api/logs/get` / `/api/logs/put` | session token | Proxy JSONBin via Apps Script (segreto server-side) |

**Schema token** (base64 urlsafe): `nome | ruolo | exp_unix | HMAC-SHA256(payload, SESSION_SECRET)`
con TTL configurabile via `SESSION_TTL_SECONDS` (default 12h).

### 5.3 Imprese (richiedono `x-session-token` — il `nome` è SEMPRE preso dal token, MAI da un parametro client)

| Metodo | Endpoint | Descrizione |
|---|---|---|
| GET | `/api/imprese/me` | Profilo + lotti assegnati (404 se non assegnata) |
| GET | `/api/imprese/pratiche` | Master.csv filtrato per i lotti dell'impresa. Confronto per **codice lotto esatto** (non substring — "Lotto 2" non aggancia "Lotto 2A") |
| POST | `/api/imprese/submit` | Body `{type:'update'\|'new', changes:[...]}` → record in `pending_updates`, status `pending` |
| GET | `/api/imprese/my-submissions` | Storico delle proprie submission |
| DELETE | `/api/imprese/submissions/{id}` | L'impresa cancella SOLO le proprie submission ancora `pending` |

### 5.4 Admin (richiedono `UPLOAD_TOKEN`)

| Metodo | Endpoint | Descrizione |
|---|---|---|
| GET / PUT / DELETE | `/api/admin/assignments[/{nome}]` | CRUD nome impresa ↔ lotti (lookup case-insensitive) |
| GET | `/api/admin/pending-updates?status=pending\|approved\|rejected\|all` | Lista coda |
| POST | `/api/admin/pending-updates/{id}/approve` | Applica le modifiche a Master.csv (nuova versione GridFS), **rigenera QGIS.geojson + Riepilogo_progettazione.csv** e pusha tutti e tre su GitHub |
| POST | `/api/admin/pending-updates/{id}/reject` | Body `{note}` |

### 5.5 Auto-push GitHub + rigenerazione derivati

`_push_to_github(file_bytes, path, label)` — commit via API GitHub (GET sha → PUT contents), fire-and-forget, retry su conflitto sha (409) fino a 3 volte. Skip silenzioso se `GITHUB_TOKEN` mancante: MongoDB resta comunque autoritativo.

`_regenerate_derived_files(master_df, note)` — Scoperta chiave: `Riepilogo_progettazione.csv` è esattamente la tabella attributi di `QGIS.geojson` esportata in CSV (stesse 21 colonne, stesso ordine, stessi valori, verificato riga per riga). Per questo:

```
Master.csv (dinamico)
    │
    ▼
_compute_tratta_summary() — per ogni TRATTA_ID:
   • STATO_AUTORIZZAZIONE ← ultima riga AUTORIZZAZIONE attiva (le pratiche
     con STATO_PERMESSO=NO COMPETENZA vengono saltate se esistono alternative)
   • STATO_NULLAOSTA / STATO_ORDINANZA ← peggiore tra l'ultimo stato di ciascun ente
   • LAVORABILE ← SI solo se AUTORIZZAZIONE=OTTENUTO e, se richiesti,
     anche NULLA OSTA/ORDINANZA=OTTENUTO
   • PRATICA ← "AUT/24/1A | NO/22/1A | …" (AUT, poi NO, poi ORD)
    │
    ▼
PATCH UNICA sulle properties di QGIS.geojson (geometria invariata)
    │
    ▼
Riepilogo_progettazione.csv = derivato direttamente da quelle properties
```

Campi **mai derivati automaticamente** (provenienti da sistemi esterni o senza regola certa): `CAMPO AWS`, `PROTOCOLLO_AUT`, `ENTE 2`, `fid`, `TIPOLOGIA`, `PROVINCIA`, `ROUTE`, `SPAN`. Restano quelli già presenti in QGIS.geojson.

### 5.6 Approvazione: come viene applicata una `update`

`_apply_changes_to_df`:
1. Match per `TRATTA_ID + ENTE + TIPO_PERMESSO` (+ `PRATICA` se fornita come discriminante: utile quando sulla stessa tratta+ente+tipo esistono pratiche diverse).
2. **Copia l'ultima riga esistente** e la inserisce **subito DOPO** la stessa (non in fondo al file) — così le righe dello stesso iter restano vicine.
3. Auto-set `DATA_ULTIMA_MODIFICA = oggi` se non fornito e c'è un cambio di `STATO_PERMESSO`.
4. Per le submission `type:'new'` aggiunge una riga vuota popolata con i campi inviati.

### 5.7 Variabili d'ambiente (`backend/.env`)

| Key | Significato |
|---|---|
| `MONGO_URL` | URI Mongo (locale o Atlas) |
| `DB_NAME` | `enri_dashboard` |
| `DATA_DIR` | Cartella seed (default: parent di `/backend`, in preview `/app`) |
| `ALLOWED_ORIGINS` | CSV di origini CORS |
| `UPLOAD_TOKEN` | Token admin (upload/delete/assignments/approve). Generare con `openssl rand -hex 32` |
| `MAX_UPLOAD_MB` | Default `25` |
| `SESSION_SECRET` | **OBBLIGATORIO** — chiave HMAC per i session token (`openssl rand -hex 32`) |
| `SESSION_TTL_SECONDS` | Default `43200` (12h) |
| `APPS_SCRIPT_URL` | URL del Google Apps Script di login |
| `APPS_SCRIPT_SECRET` | Segreto condiviso con Apps Script (MAI esposto al client) |
| `GITHUB_TOKEN` | Fine-grained PAT, repo `ENRI-RDS/dashboard`, `Contents: Read and write`. Se assente il push viene skippato |
| `GITHUB_REPO` | Default `ENRI-RDS/dashboard` |
| `GITHUB_BRANCH` | Default `main` |
| `GITHUB_CSV_PATH` / `GITHUB_QGIS_PATH` / `GITHUB_RIEPILOGO_PATH` | Override path nel repo se servono sottocartelle |

### 5.8 Vincoli di sicurezza
- `_safe_relpath()` impedisce path-traversal (`..`, `\`, caratteri fuori `[A-Za-z0-9_\-./]`).
- Estensioni accettate: `.csv`, `.xlsx`, `.xls`, `.geojson`, `.json`.
- Limite dimensione = `MAX_UPLOAD_MB`.
- HMAC compare con `hmac.compare_digest` (timing-safe).
- Le rotte impresa **ignorano qualunque parametro `nome`** passato dal client e usano sempre quello firmato nel token.

---

## 6. Come i file caricati arrivano sul frontend — `js/api-config.js`

Ogni pagina HTML include `<script src="js/api-config.js"></script>` in `<head>`. Lo script:

1. Legge `window.ENRI_API_BASE` **oppure** `localStorage.getItem('enri_api_base')`.
2. Se vuoto → non fa nulla, le pagine leggono i CSV/GeoJSON statici committati nel repo.
3. Se valorizzato → **monkey-patcha `window.fetch`**: ogni `fetch('Master.csv')` viene riscritta in `fetch(`${API_BASE}/api/data/Master.csv`)`.

**Pattern SWR (stale-while-revalidate)** in `index.html`, `mappa.html`, `mappa_impresa.html`:
- fetch parallelo del file statico GitHub (istantaneo, sempre disponibile) E del backend Render (può essere lento al cold-start);
- la UI mostra subito il dato statico;
- se Render risponde con un contenuto diverso, **la UI si aggiorna live senza reload** (callback ridisegna tabella, ricostruisce layer Leaflet, ecc.).

**Flusso completo upload/approvazione:**
1. Impresa apre `imprese.html` (auth: token in `_enri_token`), compila modifiche, invia.
2. Backend salva in `pending_updates` (status `pending`).
3. Admin apre `admin.html` → tab "Coda imprese" (badge live) → Approva.
4. Backend: legge Master.csv corrente → applica `changes` con pandas (riga inserita dopo l'ultima della stessa pratica) → crea nuova versione GridFS → **rigenera QGIS.geojson + Riepilogo_progettazione.csv** → pusha tutti e tre su GitHub.
5. Tutte le pagine, alla prossima `fetch('Master.csv')`, ricevono la nuova versione dal backend; le SWR aggiornano la UI live senza reload.

---

## 7. Auth & ruoli

### 7.1 Login utente (hub.html → /api/auth/login)
Implementato come **proxy server-to-server** verso Google Apps Script. Il browser invia `{nome, codice}`; il backend verifica con Apps Script (segreto MAI esposto) e firma un token HMAC che viene salvato in localStorage:

```js
localStorage._enri_user   // nome canonico
localStorage._enri_role   // 'admin' | 'admin2' | 'user' | 'impresa'
localStorage._enri_token  // session token firmato (HMAC, scade dopo SESSION_TTL_SECONDS)
```

L'admin gestisce gli accessi modificando lo sheet Google collegato all'Apps Script (requisito esplicito del committente).

In `hub.html` due liste filtrano le card:
- `SCAVI_ALLOWED_ROLES = ['admin', 'admin2']` → mostra/blocca card "Avanzamento Lavori".
- `ADMIN_ALLOWED_ROLES = ['admin', 'admin2']` → mostra/nasconde card "Pannello Admin".
- Card "Area Impresa" mostrata solo se `/api/imprese/me` conferma assegnazione.

### 7.2 Upload backend e azioni admin
Tutte le scritture admin (`POST /api/upload`, `DELETE /api/uploads/*`, `PUT /api/admin/assignments/*`, `POST /api/admin/pending-updates/*/approve|reject`) richiedono `UPLOAD_TOKEN`. In `admin.html` l'admin lo incolla manualmente nel form.

### 7.3 Azioni impresa
Tutti gli endpoint `/api/imprese/*` richiedono header `x-session-token`. Il `nome` viene SEMPRE letto dal token firmato, mai da un parametro `?nome=` (che è stato rimosso completamente).

---

## 8. Convenzioni & gotchas

1. **Separatore CSV auto-rilevato**: `Master.csv` può essere tab o `;` (Excel italiano vs export). `_detect_sep()` lo determina sulla prima riga; lettura/scrittura lo preservano. Il push su GitHub usa SEMPRE tab (formato originale del file nel repo).
2. **Encoding-robust**: Master.csv viene letto provando `utf-8`, `cp1252`, `latin-1` prima di fallback a `errors='replace'`. Le righe malformate (es. virgola non quotata in NOTE) usano `on_bad_lines='warn'` invece di fallire.
3. **CORS**: il backend espone solo le origini in `ALLOWED_ORIGINS`. Aggiungere il dominio preview/Render quando cambia.
4. **Path con sotto-cartelle**: `target=M/QGIS_3.geojson` salva in `DATA_DIR/M/QGIS_3.geojson`. La regex `_SAFE_NAME_RE` accetta `/` ma rifiuta `..`.
5. **Render free tier**: il servizio si spegne dopo 15min di inattività, prima chiamata ~30s di cold-start → frontend usa timeout 10s + pannello "Riprova".
6. **AbortController → Promise.race**: l'anteprima Claude non supporta la clonazione di `AbortSignal` via `postMessage`; i timeout sui fetch usano `Promise.race` ovunque (compatibile sia in anteprima che in produzione).
7. **NO COMPETENZA**: le pratiche AUTORIZZAZIONE chiuse con STATO_PERMESSO=NO COMPETENZA sono "superate" — se esiste una pratica attiva successiva sulla stessa tratta, è quella a contare; se NO COMPETENZA è l'unica presente, la tratta è IN ATTESA della prossima pratica.
8. **Conflitti GitHub (409)**: `_push_to_github` rilegge lo sha aggiornato e riprova fino a 3 volte se qualcuno scrive sullo stesso file in parallelo.
9. **Lookup impresa case-insensitive**: `_find_assignment` cerca con regex `^nome$` case-insensitive — `sertori`, `Sertori`, `SERTORI` trovano tutti lo stesso record.
10. **`dati.csv` è in disuso**: `index.html` calcola la stessa aggregazione LOTTO×STATO×Metri/Percentuale lato client da `Riepilogo_progettazione.csv` (variabili `aggMap`/`totMap` in `loadData()`).

---

## 9. Modello dati MongoDB

```jsonc
// collection: uploads (storico versioni file)
// source: "impresa" (Master.csv da approvazione) | "derived" (QGIS.geojson/Riepilogo
// rigenerati automaticamente) | assente per upload manuali da admin.html
{
  "_id": ObjectId,
  "filename": "Master.csv",            // path relativo a DATA_DIR
  "original_name": "Master_v2.xlsx",
  "size": 192167,
  "content_type": "text/csv",
  "project": "main",                   // "main" | "M" | "pm"
  "rows": 3120,
  "uploaded_at": "2026-06-23T11:30:00+00:00",
  "gridfs_id": ObjectId,               // → fs.files (None se cancellato)
  "deleted_at": null | "ISO",
  "source": "impresa" | "derived" | null,
  "note": "Submission <id> from <nome> (update)" | "restore/delete Master.csv" | null
}

// collection: assignments (impresa → lotti)
{
  "_id": ObjectId,
  "nome": "Costruzioni Alfa Srl",
  "lotti": ["Lotto 1", "Lotto 1A"],
  "active": true,
  "created_at": "ISO",
  "updated_at": "ISO"
}

// collection: pending_updates (workflow approvazione)
{
  "_id": ObjectId,
  "nome": "Costruzioni Alfa Srl",
  "type": "update" | "new",
  "changes": [
    // type=update
    {"tratta_id": "TR_0103", "ente": "...", "tipo_permesso": "AUTORIZZAZIONE",
     "fields": {"STATO_PERMESSO": "OTTENUTO", ...}},
    // type=new
    {"TRATTA_ID": "...", "ENTE": "...", ...}
  ],
  "status": "pending" | "approved" | "rejected",
  "submitted_at": "ISO",
  "reviewed_at": "ISO" | null,
  "reviewed_by": null,
  "applied_upload_id": "<id Master.csv versione generata>" | null,
  "summary": {"updated": 3, "added": 0, "not_found": 0} | null,
  "reviewed_note": "motivo rifiuto" | null,
  "note": "nota libera dell'impresa"
}
```

File content in `fs.files` / `fs.chunks` (motor `AsyncIOMotorGridFSBucket`, bucket `files`).

---

## 10. Flusso impresa end-to-end (riepilogo)

1. Admin → `admin.html` tab "Assegnazioni imprese" → aggiunge `Costruzioni Alfa Srl` con lotti `Lotto 1, Lotto 1A`.
2. L'impresa accede su `hub.html` con nome esattamente `Costruzioni Alfa Srl` + codice → backend verifica con Apps Script → restituisce session token.
3. Hub mostra card "Area Impresa" (check `/api/imprese/me` con il token).
4. Click → `imprese.html` → tab "Aggiorna pratiche" / "Nuova pratica" → coda modifiche → "Invia per approvazione".
5. Admin → tab "Coda imprese" (badge live) → Approva.
6. Backend applica `changes` a Master.csv, crea nuova versione GridFS, rigenera QGIS+Riepilogo, **pusha tutti e tre su GitHub**.
7. Dashboard (`/api/data/Master.csv`) serve immediatamente la nuova versione; SWR sulle pagine aggiorna la UI live senza reload.

---

_Ultimo aggiornamento: 2026-06-23_
