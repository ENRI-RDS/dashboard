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
2. **Preview**: static HTML servito da `frontend/server.js` su :3000, FastAPI su :8001, MongoDB locale.
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
├── ~~executive_summary.html~~     # RIMOSSA — non più nel progetto. Il pulsante "Executive" è stato rimosso dalla topbar di index.html. Nessun CSS residuo né link attivi.
├── sopralluoghi.html           # Redazione verbali di sopralluogo cantiere
├── milestone.html              # Milestone di progetto
├── ai_alerts.html              # Beta — alert predittivi
├── admin.html                  # PANNELLO ADMIN — upload + coda imprese + assegnazioni + storico versioni
├── imprese.html                # PORTALE IMPRESE — aggiorna pratiche / nuova tratta / mie submission
├── imprese_scavi.html          # NEW — Area Impresa: avanzamento scavi giornaliero (stato cantiere, metri, log)
├── mappa_impresa_caricamento.html  # NEW — variante di mappa_impresa.html con possibilità di aggiornare/inserire pratiche direttamente dalla tratta selezionata sulla mappa
├── polizze_convenzioni.html    # NEW — pratiche con CONVENZIONE/POLIZZA richiesta: filtro lotto/impresa/stato + KPI aggregati
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
| **scavi.html** | `admin`/`admin2`/`user` (tutti tranne `impresa`) | KPI scavi (non avviati/in corso/sospeso/completati), barre per lotto e per cluster, donut chart, modal lotto/cluster. ⚠️ Ruolo cambiato: in precedenza era ristretto a `admin`/`admin2`, ora `SCAVI_ALLOWED_ROLES` include anche `user`. ✅ Topbar uniformata al brand kit Retelit (§8.11 pattern: navy, logo base64, `.topbar-back`) — sostituita la vecchia topbar chiara con `.logo`/`.btn-nav`. Rimosso il badge "Struttura reale · avanzamento di esempio" e il sottotitolo dinamico generato da `_renderPageSubtitle()` (elemento `#pageSubDyn` non più nel markup; la funzione JS resta ma esce subito per guard `if(!el) return`) |
| **mappa.html** | tutti | Leaflet — SWR su `QGIS.geojson`, `QTS.geojson`, `SED_classificato.geojson`, `Master.csv`; ricostruzione live dei layer se cambiano. Basemaps Google-style, ricerca, misurazione distanze, esportazione PDF |
| **mappa_impresa.html** | impresa loggata | Variante di mappa.html filtrata sui lotti assegnati all'impresa (usa `/api/imprese/pratiche` e session token) — sola visualizzazione |
| **mappa_impresa_caricamento.html** | impresa loggata | NEW — stessa mappa filtrata, ma con possibilità di aggiornare lo stato di una pratica o inserirne una nuova direttamente cliccando sulla tratta (usa `/api/imprese/pratiche`, `/api/imprese/submit`, `/api/imprese/my-submissions`, `/api/imprese/cantieri*`). Include il blocco "tracking accessi" (vedi §5.9) |
| **milestone.html** | tutti | Milestone contrattuali e di impresa |
| **sopralluoghi.html** | tutti | Form di redazione verbale. ⚠️ Non è più solo client-side: i verbali sono ora persistiti su MongoDB (`POST/GET/DELETE /api/sopralluoghi`) con codice progressivo `VBS-AAAA-NNNN` e foto caricate su GitHub (`sopralluoghi/foto/{codice}/`) invece che generare solo un PDF locale |
| **ai_alerts.html** | tutti (Beta) | Pagina placeholder per future analisi AI |
| **polizze_convenzioni.html** | ✅ **RISOLTO** — guardia di login (`_enri_user`) aggiunta, stesso pattern overlay usato in scavi.html/sopralluoghi.html (vedi file corretto). La sola scrittura (`POST /api/admin/polizze-convenzioni/update`) richiede un `UPLOAD_TOKEN` chiesto al volo via modale, ora salvato in `localStorage['enri_upload_token']` — stessa chiave di admin.html (vedi §8.15, RISOLTO) | Pratiche con CONVENZIONE e/o POLIZZA richiesta: filtro per lotto/impresa/stato, KPI aggregati. Legge `/api/admin/polizze-convenzioni/data-richiesta`, con fallback diretto a Master.csv su GitHub Pages se l'API non risponde |
| **admin.html** | richiede `UPLOAD_TOKEN` | 4 tabs (File correnti, Storico versioni, **Coda imprese**, **Assegnazioni imprese**). Badge live conteggio pending, modali HTML custom (no `alert/confirm/prompt` nativi), date in formato `gg/mm/aa, hh:mm`, righe coda colorate per tipo |
| **imprese.html** | impresa assegnata + session token | 3 tabs (Aggiorna pratiche / Nuova pratica / Le mie submission). Campo **Ente** = select dinamica via `/api/enti` + opzione "Altro"; **Pratica** in nuova tratta = solo numero progressivo editabile (prefisso AUT/NO + suffisso lotto calcolati). Selezionando una tratta, se altre tratte condividono ENTE+TIPO+PRATICA il sistema propone di aggiornarle tutte insieme |
| **imprese_scavi.html** | impresa assegnata + session token | NEW — "Avanzamento Scavi" lato impresa: aggiornamento giornaliero per pratica/cantiere (stato cantiere, tecnica di scavo, date inizio/fine, metri realizzati **oggi** — accumulati su `metri_scavati`), storico log consultabile. Scrittura diretta, **senza** workflow di approvazione admin (a differenza di `imprese.html`) |

Tutte le pagine fanno guardia: `if (!localStorage.getItem('_enri_user')) → mostra overlay/redirect a hub.html`. Le pagine impresa controllano anche `localStorage._enri_session`.

⚠️ **Rinominata la chiave di sessione**: il token firmato in localStorage non si chiama più `_enri_token` ma **`_enri_session`** (verificato in hub.html, mappa_impresa_caricamento.html). Se trovi ancora `_enri_token` in qualche file non aggiornato, è codice vecchio.

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
| GET | `/api/enti` | session token | NEW — elenco enti unici presenti in Master.csv, ordinati alfabeticamente (popola la select "Ente" in imprese.html) |

### 5.2 Auth firmata (HMAC)

Risolve la falla per cui chiunque poteva fare `localStorage.setItem('_enri_user', 'Nome Impresa')`
e impersonare quel nome senza conoscere il codice. Ora il codice è verificato dal backend e ogni
chiamata successiva richiede un token firmato non falsificabile.

| Metodo | Endpoint | Auth | Descrizione |
|---|---|---|---|
| POST | `/api/auth/login` | — | Body `{nome, codice}` → backend chiama Apps Script server-to-server (segreto MAI esposto al browser) → restituisce `{ok, token, nome, ruolo}` |
| GET | `/api/auth/verify` | session token | Rivalidazione silenziosa (usata da index.html) |
| POST | `/api/logs/get` / `/api/logs/put` | session token | Log accessi — MongoDB (`access_logs`), non più JSONBin/Apps Script (vedi §5.12) |

**Schema token** (base64 urlsafe): `nome | ruolo | exp_unix | HMAC-SHA256(payload, SESSION_SECRET)`
con TTL configurabile via `SESSION_TTL_SECONDS` (default 12h).

### 5.3 Imprese (richiedono `x-session-token` — il `nome` è SEMPRE preso dal token, MAI da un parametro client)

> Nota: l'header resta `x-session-token`; è solo la chiave **localStorage** lato client che è stata rinominata da `_enri_token` a `_enri_session` (vedi §4 e §7).

| Metodo | Endpoint | Descrizione |
|---|---|---|
| GET | `/api/imprese/me` | Profilo + lotti assegnati (404 se non assegnata) |
| GET | `/api/imprese/pratiche` | Master.csv filtrato per i lotti dell'impresa. Confronto per **codice lotto esatto** (non substring — "Lotto 2" non aggancia "Lotto 2A") |
| POST | `/api/imprese/submit` | Body `{type:'update'\|'new', changes:[...]}` → record in `pending_updates`, status `pending` |
| GET | `/api/imprese/my-submissions` | Storico delle proprie submission |
| DELETE | `/api/imprese/submissions/{id}` | L'impresa cancella SOLO le proprie submission ancora `pending` |
| GET | `/api/lotti-cantieri` | NEW — lotti distinti (da Master.csv) + cantieri associati (da MongoDB) + mappa lotto→impresa assegnata. Usato per popolare select a cascata |
| GET | `/api/imprese/cantieri` | NEW — cantieri (uno per pratica di autorizzazione) nei lotti dell'impresa autenticata |
| POST | `/api/imprese/cantieri/{cantiere_key}` | NEW — aggiorna stato cantiere, tecnica scavo, date, **metri_realizzati_oggi** (si accumula su `metri_scavati`, non sovrascrive), note. Push automatico su GitHub. **Scrittura diretta, nessuna approvazione admin** (a differenza di `/api/imprese/submit`) |
| GET | `/api/imprese/cantieri/{cantiere_key}/log` | NEW — storico aggiornamenti giornalieri di un cantiere |
| GET | `/api/imprese/solleciti` | NEW — solleciti dell'impresa, filtrati per le tratte dei suoi lotti |
| POST | `/api/imprese/solleciti` | NEW — inserisce un sollecito. Scrittura diretta, nessuna approvazione |
| POST | `/api/imprese/solleciti/bulk-insert` / `bulk-delete` | NEW — inserimento/cancellazione massiva |
| DELETE | `/api/imprese/solleciti/{id}` | NEW — elimina un sollecito |

### 5.4 Admin (richiedono `UPLOAD_TOKEN`)

| Metodo | Endpoint | Descrizione |
|---|---|---|
| GET / PUT / DELETE | `/api/admin/assignments[/{nome}]` | CRUD nome impresa ↔ lotti (lookup case-insensitive) |
| GET | `/api/admin/pending-updates?status=pending\|approved\|rejected\|all` | Lista coda |
| POST | `/api/admin/pending-updates/{id}/approve` | Applica le modifiche a Master.csv (nuova versione GridFS), **rigenera QGIS.geojson + Riepilogo_progettazione.csv** e pusha tutti e tre su GitHub. Da qui parte anche `_sync_cantieri()` (vedi §5.12) |
| POST | `/api/admin/pending-updates/{id}/reject` | Body `{note}` |
| GET | `/api/admin/polizze-convenzioni/data-richiesta` | NEW — per ogni pratica con CONVENZIONE/POLIZZA valorizzata, fissa (una sola volta, `$setOnInsert`) la data di prima richiesta nella collection `pol_conv_dates` |
| POST | `/api/admin/polizze-convenzioni/update` | NEW — Body `{lotto, pratica, fields:{CONVENZIONE?, POLIZZA?}}`. Valori ammessi: `NECESSARIA\|RICHIESTA RDS\|INVIATA\|OTTENUTA\|""`. Scrive su Master.csv (tutte le righe lotto+pratica) e pusha su GitHub |
| GET | `/api/admin/sync-cantieri` | NEW — forza la sincronizzazione cantieri↔Master.csv (crea cantieri mancanti, uno per pratica AUTORIZZAZIONE) |
| GET | `/api/admin/solleciti` | NEW — vista admin di tutti i solleciti |

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

### 5.9 Sopralluoghi (collection `sopralluoghi`)

| Metodo | Endpoint | Auth | Descrizione |
|---|---|---|---|
| GET | `/api/sopralluoghi` | session token | Tutti i verbali, ordinati per `codice_verbale` decrescente |
| GET | `/api/sopralluoghi/next-codice` | session token | Prossimo codice progressivo `VBS-{anno}-{NNNN}` |
| POST | `/api/sopralluoghi` | session token | Salva verbale su MongoDB + aggiorna `sopralluoghi.csv` su GitHub. Le foto (data URL base64) vengono caricate su GitHub in `sopralluoghi/foto/{codice}/` per non saturare lo storage Mongo gratuito |
| DELETE | `/api/sopralluoghi/{id}` | session token, **solo ruolo `admin`** | Elimina un verbale |

### 5.10 Cantieri / Avanzamento Scavi impresa (collection `cantieri`)

Un cantiere = una pratica AUTORIZZAZIONE (chiave `cantiere_key`, **non** `pratica_id`, perché quest'ultimo non è garantito univoco tra enti diversi sullo stesso lotto/numero).

- `GET /api/cantieri?lotto=&cluster=&stato=` — lista pubblica, usata da `scavi.html` (lo storico `log` viene tolto dal listing per non appesantire la risposta).
- L'impresa aggiorna via `POST /api/imprese/cantieri/{cantiere_key}` (vedi §5.3): **scrittura diretta senza approvazione**, a differenza del flusso `imprese.html`/`pending_updates`. Ogni update accumula `metri_realizzati_oggi` su `metri_scavati` (`$inc`) e appende una riga a `log[]` (data, impresa, stato, metri, note, motivo blocco).
- `_sync_cantieri()` crea i cantieri mancanti a partire da Master.csv; viene rilanciata sia da `/api/admin/sync-cantieri` sia automaticamente dopo ogni approvazione di `pending_updates`.
- Dopo ogni scrittura, push fire-and-forget su GitHub (`_push_cantieri_to_github`).

### 5.11 Solleciti (collection `solleciti`)

Sistema di "promemoria/follow-up" per pratiche in attesa, associato alle tratte dei lotti dell'impresa. Scrittura diretta (no workflow di approvazione), CRUD completo lato impresa (`/api/imprese/solleciti*`) e vista aggregata lato admin (`/api/admin/solleciti`).

### 5.12 ✅ Tracking accessi — RISOLTO + migrato da JSONBin a MongoDB

Tutte le 5 pagine con blocco `<!-- TRACKING ACCESSI -->` (`scavi.html`, `mappa.html`, `mappa_impresa.html`, `mappa_impresa_caricamento.html`, `sopralluoghi.html`) chiamano `POST /api/logs/get` e `POST /api/logs/put` (header `x-session-token`). **Il backend non chiama più Apps Script/JSONBin per questo**: legge/scrive dalla collection Mongo `access_logs` (un documento per `binId`, campi `utenti[]`/`accessi[]`). Contratto richiesta/risposta lasciato identico apposta (`{binId}` → `{record:{utenti,accessi}}`), quindi **le 5 pagine non sono state toccate di nuovo** — solo il backend è cambiato.

`APPS_SCRIPT_URL`/`APPS_SCRIPT_SECRET` restano in uso **solo per il login** (`action: "login"` verso il Google Sheet), non più per i log.

⚠️ **Dati storici non migrati**: gli accessi già registrati sul vecchio bin JSONBin non sono stati copiati automaticamente su MongoDB (nessun accesso di rete disponibile per farlo in questa sessione). Se serve conservare lo storico, va fatto un import una tantum leggendo il bin esistente e scrivendolo in `access_logs`. Da questo deploy in poi, il log riparte vuoto su Mongo.

`index.html` conteneva anche codice/commenti morti che nominavano JSONBin per una funzione "Note per pratica" già disattivata (stub `noteLoad`/`noteSave` no-op) e per due commenti descrittivi non più accurati — ripuliti (rinominati, nessuna funzionalità toccata).

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
1. Impresa apre `imprese.html` (auth: token in `_enri_session`), compila modifiche, invia.
2. Backend salva in `pending_updates` (status `pending`).
3. Admin apre `admin.html` → tab "Coda imprese" (badge live) → Approva.
4. Backend: legge Master.csv corrente → applica `changes` con pandas (riga inserita dopo l'ultima della stessa pratica) → crea nuova versione GridFS → **rigenera QGIS.geojson + Riepilogo_progettazione.csv** → pusha tutti e tre su GitHub.
5. Tutte le pagine, alla prossima `fetch('Master.csv')`, ricevono la nuova versione dal backend; le SWR aggiornano la UI live senza reload.

---

## 7. Auth & ruoli

### 7.1 Login utente (hub.html → /api/auth/login)
Implementato come **proxy server-to-server** verso Google Apps Script. Il browser invia `{nome, codice}`; il backend verifica con Apps Script (segreto MAI esposto) e firma un token HMAC che viene salvato in localStorage:

```js
localStorage._enri_user    // nome canonico
localStorage._enri_role    // 'admin' | 'admin2' | 'user' | 'impresa'
localStorage._enri_session // session token firmato (HMAC, scade dopo SESSION_TTL_SECONDS) — ⚠️ rinominata da _enri_token
```

L'admin gestisce gli accessi modificando lo sheet Google collegato all'Apps Script (requisito esplicito del committente).

In `hub.html` tre liste filtrano le card:
- `SCAVI_ALLOWED_ROLES = ['admin', 'admin2', 'user']` → mostra/blocca card "Avanzamento Lavori". ⚠️ Ora include anche `user`, non più solo admin
- `ADMIN_ALLOWED_ROLES = ['admin', 'admin2']` → mostra/nasconde card "Pannello Admin".
- `IMPRESA_ROLES = ['impresa']` → attiva la modalità "Area Impresa": nasconde tutte le card normali e mostra fino a 4 card dedicate (`impresaCardsWrap`), ciascuna condizionata a `display:none` finché non si conferma l'assegnazione via `/api/imprese/me`:
  - `impresaCard` → `imprese.html` (aggiorna pratiche)
  - `impresaMapCard` → `mappa_impresa.html` (sola visualizzazione mappa)
  - `impresaMapUpdCard` → `mappa_impresa_caricamento.html` (mappa con aggiornamento pratiche)
  - `impresaScaviCard` → `imprese_scavi.html` (avanzamento scavi)

Nota minor: la chiamata `fetch(apiBase + '/api/imprese/me?nome=' + ...)` in `hub.html` passa ancora un parametro `?nome=` in query string, ma il backend (`impresa_me`) lo ignora completamente e legge sempre il nome dal token firmato — il parametro è innocuo ma andrebbe rimosso dal client per coerenza con quanto dichiarato in §7.3.

### 7.2 Upload backend e azioni admin
Tutte le scritture admin (`POST /api/upload`, `DELETE /api/uploads/*`, `PUT /api/admin/assignments/*`, `POST /api/admin/pending-updates/*/approve|reject`, `POST /api/admin/polizze-convenzioni/update`, `GET /api/admin/sync-cantieri`) richiedono `UPLOAD_TOKEN`. In `admin.html` l'admin lo incolla manualmente nel form.

### 7.3 Azioni impresa
Tutti gli endpoint `/api/imprese/*` richiedono header `x-session-token`. Il `nome` viene SEMPRE letto dal token firmato, mai da un parametro `?nome=` lato server (vedi nota sopra: il client a volte lo invia ancora, ma viene ignorato).

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
11. **Rebranding "Retelit" — COMPLETATO su TUTTE le pagine (rev.3)**: topbar navy (`--retelit-blue`, 58px, padding 28px) con logo Retelit embeddato come base64 bianco (50px) su tutte le pagine: `hub`, `index`, `mappa`, `sopralluoghi`, `polizze_convenzioni`, `milestone`. Bottone Hub unificato come `.topbar-back` (bordo `--retelit-sky`, hover trasparente). `<title>` aggiornati a `Retelit — …` su tutte le pagine. Tutti i token brand CSS (`--retelit-blue-50`, `--retelit-ice`, `--retelit-sky`, `--accent2`, `--border2`, `--muted2`, `--text2`, `--radius-*`, `--shadow-*`, `--dur-*`, `--ease-*`) definiti nel `:root` di ogni file. Emoji vietate dal brand kit rimosse da topbar, placeholder, export buttons e JS (restano solo simboli funzionali in pannelli tecnici Leaflet).
12. **Token rinominato**: `_enri_token` → `_enri_session` in localStorage (vedi §7.1). Se incontri ancora `_enri_token` in una pagina, è codice non aggiornato.
13. **Scrittura diretta vs workflow di approvazione**: occhio a non confondere i due modelli — `imprese.html`/`mappa_impresa_caricamento.html` (pratiche) passano sempre da `pending_updates` + approvazione admin; `imprese_scavi.html` (cantieri) e i solleciti scrivono **direttamente** su MongoDB/GitHub senza step di revisione.
14. ✅ **RISOLTO** — Tracking accessi con segreto esposto: vedi §5.12. Tutte le 5 pagine ora usano il proxy `/api/logs/*`, nessun secret in chiaro nel client.
15. ✅ **RISOLTO** — `polizze_convenzioni.html` ora legge/scrive il token nella stessa chiave `localStorage['enri_upload_token']` usata da `admin.html` (prima usava `sessionStorage['_enri_upload_token']`). Se il token è già stato inserito una volta in una delle due pagine, l'altra non lo richiede più. Scelta deliberata: si è mantenuto il modello a doppio secret (login + UPLOAD_TOKEN), non si è passati a un controllo basato solo sul ruolo — nessuna modifica al backend.
16. **Login duplicato in `index.html`**: oltre al login "ufficiale" su `hub.html`, `index.html` ha un proprio overlay di login che chiama anch'esso `POST /api/auth/login` e poi reindirizza a `hub.html` dopo aver salvato `_enri_user/_enri_role/_enri_session`. Serve da fallback per chi atterra direttamente su `index.html?direct=1` (link diretto dalla card "Fase 1" in hub.html) senza essere già loggato.
17. **`executive_summary.html` rimosso dal progetto** (confermato dall'utente): non c'è più una card collegata in `hub.html`. Se trovi ancora riferimenti al file in vecchie versioni di documentazione o in altri repo collegati, sono obsoleti.
18. **"Lotti in Progettazione" (ex "Lotti in Avvio")**: i lotti 1–8 non ancora avviati sono ora etichettati "Lotti in Progettazione" ovunque in `index.html` (label visuale, riga totale, modale riepilogo, export). La colonna milestone "Invio Perm./Scadenza" è stata rimossa da quella sezione (ora c'è `milestone.html` dedicata).
19. **`IMPRESE_PER_LOTTO` estesa a tutti i 12 lotti**: in `index.html` l'oggetto include ora anche i lotti 1–8 (`1→Valtellina`, `2→Sertori`, `3→Valtellina`, `4→Sielte`, `5→Circet`, `6→Sertori`, `7→Valtellina`, `8→Sielte`). Il nome impresa compare sotto il numero lotto nelle barre di avanzamento.
20. **SED — bottoni Excel unificati**: la sezione "Attraversamenti SED" in `index.html` aveva due pulsanti separati ("Scarica Vista" / "Scarica Tutti"). Ora è un unico dropdown `sedXlsDropdown` identico a `xlsDropdown` di "Tutte le Pratiche" (con opzioni "Tutti gli attraversamenti" e "Vista corrente").
21. **`debug_dashboard.py`**: script Python di audit automatico per i file HTML della dashboard. Controlla: CSS vars mancanti/undefined, topbar navy, logo, hub button style, div balance, emoji UI vietate vs funzionali Leaflet, gradienti decorativi, backdrop-filter, colori fuori palette, funzioni JS duplicate e dead, variabili JS inutilizzate, DOM ID mancanti, console.log in produzione, segreti hardcoded. Output con score /100 per file. Uso: `python3 debug_dashboard.py file.html [...]`.

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

```jsonc
// collection: cantieri (NEW — un documento per pratica di autorizzazione)
{
  "_id": ObjectId,
  "cantiere_key": "24|1A",          // num pratica | lotto — NON pratica_id (non univoco tra enti)
  "codice_cantiere": "CA/3/1A",     // NEW — identificativo cantiere mostrato in UI, distinto da pratica_id
  "codice_progressivo": 3,          // NEW — contatore progressivo per lotto, assegnato una sola volta alla creazione
  "pratica_id": "AUT/24/1A",        // resta come riferimento pratica, non più usato come nome del cantiere in UI
  "lotto": "1A",
  "cluster": "...",
  "ente": "...",
  "stato_cantiere": "...",          // valori in STATO_CANTIERE_VALUES
  "tecnica_scavo": "...",           // valori in TECNICA_SCAVO_VALUES
  "data_inizio_prevista": "ISO" | null,
  "data_inizio_effettiva": "ISO" | null,
  "data_fine_prevista": "ISO" | null,
  "data_fine_effettiva": "ISO" | null,
  "metri_scavati": 0,                // accumulato via $inc da metri_realizzati_oggi
  "note": "...",
  "motivo_blocco": "...",
  "data_ripresa_stimata": "ISO" | null,
  "impresa": "Costruzioni Alfa Srl",
  "updated_at": "ISO",
  "log": [ {"data": "AAAA-MM-GG", "impresa": "...", "stato_cantiere": "...",
            "metri_realizzati": 0, "note": "...", "motivo_blocco": "...",
            "data_ripresa_stimata": "..."} ]
}

// collection: sopralluoghi (NEW)
{
  "_id": ObjectId,
  "codice_verbale": "VBS-2026-0042",
  // + campi del form (cantiere, segnalazioni, azioni richieste, firme, riferimenti foto su GitHub)
}

// collection: solleciti (NEW)
{
  "_id": ObjectId,
  "tratta_id": "...",
  "pratica": "...",
  "impresa": "Costruzioni Alfa Srl",
  "tipo_sollecito": "...",
  "created_at": "ISO"
}

// collection: pol_conv_dates (NEW — chiave: "{lotto}|{pratica}|CONVENZIONE|POLIZZA")
{
  "_id": "1A|11|CONVENZIONE",
  "data_richiesta": "AAAA-MM-GG"   // fissata una sola volta con $setOnInsert
}

// collection: access_logs (NEW — ex JSONBin, un documento per binId)
{
  "_id": "69c8fbdced015c742bc8e978",   // il vecchio LOG_BIN_ID, riusato come chiave Mongo
  "utenti":  ["Mario Rossi", "Costruzioni Alfa Srl", "..."],
  "accessi": [ {"ts":"ISO","utente":"...","ruolo":"...","ip":"...","ua":"...",
                "durata":0,"lotti":[],"nAperture":0,"pagina":"scavi"} ]
}
```

---

## 10. Flusso impresa end-to-end (riepilogo)

1. Admin → `admin.html` tab "Assegnazioni imprese" → aggiunge `Costruzioni Alfa Srl` con lotti `Lotto 1, Lotto 1A`.
2. L'impresa accede su `hub.html` con nome esattamente `Costruzioni Alfa Srl` + codice → backend verifica con Apps Script → restituisce session token (`_enri_session`).
3. Hub entra in "modalità impresa" (check `/api/imprese/me`) e mostra fino a 4 card: Aggiorna Pratiche, Mappa (sola vista), Mappa con aggiornamento, Avanzamento Scavi.
4a. **Flusso pratiche** (con approvazione): Click → `imprese.html` o `mappa_impresa_caricamento.html` → coda modifiche → "Invia per approvazione" → Admin tab "Coda imprese" → Approva → Backend applica `changes` a Master.csv, crea nuova versione GridFS, rigenera QGIS+Riepilogo, **pusha tutti e tre su GitHub** → `_sync_cantieri()` riallinea i cantieri.
4b. **Flusso scavi** (scrittura diretta, NEW): Click → `imprese_scavi.html` → aggiorna stato cantiere/metri giornalieri → `POST /api/imprese/cantieri/{key}` scrive subito su MongoDB e pusha su GitHub, **senza passare da `pending_updates`**.
5. Dashboard (`/api/data/Master.csv`, `/api/cantieri`) serve immediatamente la nuova versione; SWR sulle pagine aggiorna la UI live senza reload.

---

_Ultimo aggiornamento: 2026-07-01 (rev. 4) — rev.4 ha: uniformata la topbar di `scavi.html` al brand kit Retelit (navy, logo base64, `.topbar-back` — era rimasta sulla vecchia topbar chiara con `.btn-nav`, §4); rimossi da `scavi.html` il badge "Struttura reale · avanzamento di esempio" e il sottotitolo dinamico `#pageSubDyn` (§4). rev.3 ha: completato il rebranding Retelit su tutte le pagine (§8.11, topbar navy, logo base64 bianco 50px, title tag, token CSS completi su tutti i file); rimosso definitivamente `executive_summary.html` e ogni riferimento residuo (§3, §8.17); rinominato "Lotti in Avvio" → "Lotti in Progettazione" e rimossa colonna milestone ridondante da `index.html` (§8.18); esteso `IMPRESE_PER_LOTTO` a tutti i 12 lotti (§8.19); unificato i bottoni Excel SED in un dropdown (§8.20); documentato `debug_dashboard.py` (§8.21)._
