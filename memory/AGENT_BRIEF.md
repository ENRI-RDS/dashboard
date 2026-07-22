> Documento di onboarding per agenti AI / sviluppatori. Spiega architettura, flussi, file e convenzioni della dashboard ENRI **senza dover leggere tutto il codice**.

---

## 1. Cos'è questo progetto

Dashboard di project management per **ENRI** — un progetto infrastrutturale di posa fibra/cavidotti (12 lotti, 7 cluster, province BG/MI/MB/PV/CR). Mostra l'avanzamento di:
- **Fase 1 – Progettazione**: iter autorizzativo (richieste, scadenze, pratiche per cluster/lotto/ente).
- **Fase 2 – Avanzamento Lavori (Scavi)**: stato cantieri, % completamento, metri scavati.
- **Sopralluoghi, Mappa georeferenziata, Milestone, AI Alerts**.
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
├── ~~mappa_impresa.html~~         # RIMOSSA (rev.126) — sola-vista, superset da mappa_impresa_caricamento.html. File non più nel repo.
├── ~~executive_summary.html~~     # RIMOSSA — non più nel progetto. Il pulsante "Executive" è stato rimosso dalla topbar di index.html. Nessun CSS residuo né link attivi.
├── sopralluoghi.html           # Redazione verbali di sopralluogo cantiere
├── milestone.html              # Milestone di progetto
├── ai_alerts.html              # Beta — alert predittivi
├── admin.html                  # PANNELLO ADMIN — upload + coda imprese + assegnazioni + storico versioni
├── imprese.html                # PORTALE IMPRESE — aggiorna pratiche / nuova tratta / mie submission
├── imprese_scavi.html          # Area Impresa: avanzamento scavi giornaliero (stato cantiere, metri, log)
├── mappa_impresa_caricamento.html  # Area Impresa: mappa Leaflet filtrata sui lotti assegnati, con possibilità di aggiornare/inserire pratiche direttamente dalla tratta selezionata
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
| **index.html** | `admin`/`admin2`/`user` (tutti tranne `impresa`) | KPI, GANTT, tabella filtrabile, modal pratiche, grafici Chart.js. **SWR**: legge GitHub statico + Render in parallelo, ridisegna live se il backend ha dati più freschi. XLSX caricato lazy al primo "Esporta Excel" |
| **scavi.html** | `admin`/`admin2`/`user` (tutti tranne `impresa`) | KPI scavi (non avviati/in corso/sospeso/completati) con metri+% sul totale (rev.8), barre per lotto e per cluster a segmenti multi-stato reali (rev.8, non più blob binario), donut chart, modal lotto/cluster, tabella "Tutti i Cantieri" con `codice_cantiere`/`impresa` (rev.10: rimossa colonna `tecnica_scavo`, resta solo nella card modal). ⚠️ Ruolo cambiato: in precedenza era ristretto a `admin`/`admin2`, ora `SCAVI_ALLOWED_ROLES` include anche `user`. ✅ Topbar uniformata al brand kit Retelit (§8.11 pattern: navy, logo base64, `.topbar-back`) — sostituita la vecchia topbar chiara con `.logo`/`.btn-nav`. Rimosso il badge "Struttura reale · avanzamento di esempio". **(sessione 2026-07-03)**: exec-strip con 3 KPI grandi (avanzamento fisico/permessi/sospesi) in header; titolo/eyebrow/sottotitolo pagina rimossi su richiesta utente, `#pageSubDyn` di nuovo assente dal markup |
| **mappa.html** | `admin`/`admin2`/`user` (tutti tranne `impresa`) | Leaflet — SWR su `QGIS.geojson`, `QTS.geojson`, `SED_classificato.geojson`, `Master.csv`; ricostruzione live dei layer se cambiano. Basemaps Google-style, ricerca, misurazione distanze, esportazione PDF |
| **mappa_impresa_caricamento.html** | impresa loggata | Mappa filtrata sui lotti assegnati (usa `/api/imprese/pratiche` e session token), con possibilità di aggiornare lo stato di una pratica o inserirne una nuova direttamente cliccando sulla tratta (`/api/imprese/submit`, `/api/imprese/my-submissions`, `/api/imprese/cantieri*`). Include il blocco "tracking accessi" (vedi §5.9). Unica pagina mappa lato impresa (sostituisce l'ex `mappa_impresa.html`, rimossa rev.126) |
| **milestone.html** | `admin`/`admin2`/`user` (tutti tranne `impresa`) | Milestone contrattuali e di impresa |
| **sopralluoghi.html** | `admin`/`admin2`/`user` (tutti tranne `impresa`) | Form di redazione verbale. ⚠️ Non è più solo client-side: i verbali sono ora persistiti su MongoDB (`POST/GET/DELETE /api/sopralluoghi`) con codice progressivo `VBS-AAAA-NNNN` e foto caricate su GitHub (`sopralluoghi/foto/{codice}/`) invece che generare solo un PDF locale |
| **ai_alerts.html** | tutti (Beta) | Pagina placeholder per future analisi AI |
| **polizze_convenzioni.html** | `admin`/`admin2`/`user` (tutti tranne `impresa`) | Pratiche con CONVENZIONE e/o POLIZZA richiesta: filtro per lotto/impresa/stato, KPI aggregati. Legge `/api/admin/polizze-convenzioni/data-richiesta`, con fallback diretto a Master.csv su GitHub Pages se l'API non risponde. ✅ **RISOLTO**: guardia di login (`_enri_user`) aggiunta, stesso pattern overlay usato in scavi.html/sopralluoghi.html. La sola scrittura (`POST /api/admin/polizze-convenzioni/update`) richiede un `UPLOAD_TOKEN` chiesto al volo via modale, salvato in `localStorage['enri_upload_token']` — stessa chiave di admin.html (§8.15) |
| **admin.html** | richiede `UPLOAD_TOKEN` | 4 tabs (File correnti, Storico versioni, **Coda imprese**, **Assegnazioni imprese**). Badge live conteggio pending, modali HTML custom (no `alert/confirm/prompt` nativi), date in formato `gg/mm/aa, hh:mm`, righe coda colorate per tipo. Upload: select `target` a scelta fissa (no testo libero), JS blocca l'invio se il nome del file selezionato dal disco non combacia (case-insensitive) col `target` scelto — messaggio "Nome file non corrispondente" (riga ~842) |
| **imprese.html** | impresa assegnata + session token | 3 tabs (Aggiorna pratiche / Nuova pratica / Le mie submission). Campo **Ente** = select dinamica via `/api/enti` + opzione "Altro"; **Pratica** in nuova tratta = solo numero progressivo editabile (prefisso AUT/NO + suffisso lotto calcolati). Selezionando una tratta, se altre tratte condividono ENTE+TIPO+PRATICA il sistema propone di aggiornarle tutte insieme |
| **imprese_scavi.html** | impresa assegnata + session token | "Avanzamento Scavi" lato impresa: aggiornamento giornaliero per pratica/cantiere (stato cantiere, tecnica di scavo, date inizio/fine, metri realizzati **oggi** — accumulati su `metri_scavati`), storico log consultabile. Scrittura diretta, **senza** workflow di approvazione admin (a differenza di `imprese.html`) |

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
| GET | `/api/files` | session token, **solo staff** | Lista unificata: Mongo + seed disco. Ogni file: `source: 'mongo'\|'disk'`, `versions`, `size`, `modified` |
| GET | `/api/data/{path}` | session token | Scarica file (Mongo se presente, altrimenti disco). Usato da `js/api-config.js` |
| GET | `/api/data-text/{path}` | session token | Idem in `text/plain` |
| GET | `/api/preview/{path}?max_bytes=N` | session token, **solo staff** | Anteprima primi N byte (256–65536, default 8192) |
| GET | `/api/uploads?limit=&project=&filename=&include_deleted=` | session token, **solo staff** | Storico upload (filtri + audit cancellati) |
| POST | `/api/upload` | `UPLOAD_TOKEN` (form `token` o header `x-upload-token`) | Multipart: `file`, `target` opz., `project`, `convert_to_csv`. Salva in GridFS + record `uploads`. Excel → CSV auto. **Se il target è `Master.csv` pusha su GitHub e rigenera QGIS/Riepilogo**. ⚠️ Se `target` è vuoto, `out_name` ricade sul filename originale del file caricato (`server.py` riga ~359) — da `admin.html` non capita mai (guardia lato client, v. riga 110), ma qualunque altro chiamante di questo endpoint senza passare `target` esplicito rischia di salvare sotto nome sbagliato senza errore. `GET /api/data/{filename}` e `GET /api/files` selezionano sempre l'upload con `uploaded_at` più recente per quel filename (nessun lock/versioning ottimistico tra scritture concorrenti, v. §8.41) |
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

- **`codice_cantiere`** (`CA/{progressivo}/{lotto}`, rev.5): identificativo "ufficiale" mostrato all'impresa e in `scavi.html`, distinto da `cantiere_key` (chiave tecnica) e `pratica_id` (riferimento pratica). Assegnato **una sola volta** alla creazione in `_sync_cantieri()`, mai riassegnato; `_max_codice_per_lotto()`+`_backfill_codici_cantiere()` garantiscono continuità anche sui cantieri storici. Esposto su `/api/cantieri`, `/api/imprese/cantieri`, `cantieri.csv`.
- ✅ **RISOLTO (rev.10)**: `impresa` sui cantieri era popolato **solo** da migrazione best-effort dal vecchio schema pre-raggruppamento (`old_docs`) — per qualsiasi cantiere creato dopo, restava sempre `""`. `_sync_cantieri()` ora costruisce all'inizio una mappa `lotto_impresa` dalla collection `assignments` (stessa fonte/logica di `GET /api/lotti-cantieri`) e la usa per popolare `impresa` sia in creazione sia in update (backfill automatico sui cantieri esistenti privi del campo, senza sovrascrivere se un lotto non ha assegnazione).
- **Transizioni stato** (`STATO_TRANSITIONS`, JS, rev.5): la select stato in `mappa_impresa_caricamento.html`/`imprese_scavi.html` mostra solo stato corrente + transizioni valide (`non_avviato→allestimento→in_corso→{sospeso,completato}`, `sospeso→in_corso`). Su `allestimento`: `tecnica_scavo`/`metri_realizzati_oggi` disabilitati e non richiesti (né in UI né in payload). `data_inizio_effettiva` lockata permanentemente una volta valorizzata.
- `GET /api/cantieri?lotto=&cluster=&stato=` — lista pubblica, usata da `scavi.html` (lo storico `log` viene tolto dal listing per non appesantire la risposta).
- L'impresa aggiorna via `POST /api/imprese/cantieri/{cantiere_key}` (vedi §5.3): **scrittura diretta senza approvazione**, a differenza del flusso `imprese.html`/`pending_updates`. Ogni update accumula `metri_realizzati_oggi` su `metri_scavati` (`$inc`) e appende una riga a `log[]` (data, impresa, stato, **tecnica_scavo** [rev.6], metri, note, motivo blocco). ⚠️ Bug corretto (rev.5): il check assegnazione confrontava `lotto` non normalizzato (`"1A"` vs `"Lotto 1A"`) → 403 spurio; ora entrambi i lati passano da `_lotto_from_source()`.
- `_sync_cantieri()` crea i cantieri mancanti a partire da Master.csv; viene rilanciata sia da `/api/admin/sync-cantieri` sia automaticamente dopo ogni approvazione di `pending_updates`. **`metri_totali` = somma di TUTTE le tratte della pratica (autorizzazione ottenuta), indipendentemente da `lavorabile`** (rev.9, §8.33): quel flag per-tratta è solo indicativo per la mappa (NULLA OSTA/ORDINANZA ottenuti), non deve e non limita cosa l'impresa può rendicontare come scavato.
- Dopo ogni scrittura, push fire-and-forget su GitHub (`_push_cantieri_to_github`).

### 5.11 Solleciti (collection `solleciti`)

Sistema di "promemoria/follow-up" per pratiche in attesa, associato alle tratte dei lotti dell'impresa. Scrittura diretta (no workflow di approvazione), CRUD completo lato impresa (`/api/imprese/solleciti*`) e vista aggregata lato admin (`/api/admin/solleciti`).

### 5.12 ✅ Tracking accessi — RISOLTO + migrato da JSONBin a MongoDB

Tutte le 4 pagine con blocco `<!-- TRACKING ACCESSI -->` (`scavi.html`, `mappa.html`, `mappa_impresa_caricamento.html`, `sopralluoghi.html`) chiamano `POST /api/logs/get` e `POST /api/logs/put` (header `x-session-token`). **Il backend non chiama più Apps Script/JSONBin per questo**: legge/scrive dalla collection Mongo `access_logs` (un documento per `binId`, campi `utenti[]`/`accessi[]`). Contratto richiesta/risposta lasciato identico apposta (`{binId}` → `{record:{utenti,accessi}}`), quindi **le pagine non sono state toccate di nuovo** — solo il backend è cambiato.

`APPS_SCRIPT_URL`/`APPS_SCRIPT_SECRET` restano in uso **solo per il login** (`action: "login"` verso il Google Sheet), non più per i log.

⚠️ **Dati storici non migrati**: gli accessi già registrati sul vecchio bin JSONBin non sono stati copiati automaticamente su MongoDB (nessun accesso di rete disponibile per farlo in questa sessione). Se serve conservare lo storico, va fatto un import una tantum leggendo il bin esistente e scrivendolo in `access_logs`. Da questo deploy in poi, il log riparte vuoto su Mongo.

`index.html` conteneva anche codice/commenti morti che nominavano JSONBin per una funzione "Note per pratica" già disattivata (stub `noteLoad`/`noteSave` no-op) e per due commenti descrittivi non più accurati — ripuliti (rinominati, nessuna funzionalità toccata).

---

## 6. Come i file caricati arrivano sul frontend — `js/api-config.js`

Ogni pagina HTML include `<script src="js/api-config.js"></script>` in `<head>`. Lo script:

1. Legge `window.ENRI_API_BASE` **oppure** `localStorage.getItem('enri_api_base')`.
2. Se vuoto → non fa nulla, le pagine leggono i CSV/GeoJSON statici committati nel repo.
3. Se valorizzato → **monkey-patcha `window.fetch`**: ogni `fetch('Master.csv')` viene riscritta in `fetch(`${API_BASE}/api/data/Master.csv`)`.

**Pattern SWR (stale-while-revalidate)** in `index.html`, `mappa.html`, `mappa_impresa_caricamento.html`:
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
- `IMPRESA_ROLES = ['impresa']` → attiva la modalità "Area Impresa": nasconde tutte le card normali e mostra fino a 3 card dedicate (`impresaCardsWrap`), ciascuna condizionata a `display:none` finché non si conferma l'assegnazione via `/api/imprese/me`:
  - `impresaCard` → `imprese.html` (aggiorna pratiche)
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
5. **Render free tier**: il servizio si spegne dopo 15min di inattività, prima chiamata ~30s di cold-start → frontend usa timeout 10s + pannello "Riprova". ⚠️ **`.github/workflows/keepalive.yml` abbandonato (rev.28)**: gli scheduled workflow di GitHub Actions non sono affidabili al minuto (ritardi 10-15+min documentati, causavano comunque sleep intermittenti) — sostituito con **cron-job.org** (ping `/api/health` ogni 13min, precisione al minuto, alert email su fallimento). Il file su GitHub resta solo con `workflow_dispatch` per test manuali, `schedule` rimosso. ✅ Confermato attivo in orario lavorativo (2026-07-06) — zero cold-start diurno per le imprese.
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
22. **Card modal cantieri in `scavi.html` (rev.7)**: `openModalStato()` mostrava solo `pratica_id`/lotto/ente/comune/progresso. Ora mostra tutti i dati disponibili: `codice_cantiere` (primario), cluster, tecnica scavo, impresa, conteggio tratte lavorabili/bloccate (derivato da `r.tratte[]`, **non** da `tratte_lavorabili`/`tratte_bloccate` — quei campi esistono solo nella riga CSV, non nel doc Mongo), le 4 date se valorizzate, motivo blocco+ripresa stimata se `sospeso`, note.
23. ✅ **RISOLTO** — **Palette colori stato fuori brandkit Retelit**: `STATO_COLORS`/`COLORS`/`STATI_COLORI_MAP` usavano hex arbitrari non a palette (viola `#9b59b6`/`#7A3DAA`, rosa `#B5657A`, verde/rosso/blu/arancio non-token). Mappa canonica ora identica su `index.html`, `mappa.html`, `mappa_impresa_caricamento.html`, `scavi.html` (v. §8bis; includeva anche `mappa_impresa.html`, poi rimossa rev.126). **Esclusi deliberatamente**: palette per-lotto/cluster (`LOTTO_COLORS`, rainbow array) e marker misura/ricerca — servono a distinguere entità diverse, non rappresentano uno "stato", restano arbitrari.
24. ✅ **RISOLTO** — **Nesting HTML rotto in `mappa_impresa_caricamento.html`**: `</div>` di troppo chiudeva `#sidebarEl` prima che `#scaviBodyPanel` venisse aperto → il pannello scavi diventava fratello della sidebar invece che figlio, causava "doppia finestra" (mappa schiacciata, legenda sovrapposta al form). Verificare sempre il nesting con `html.parser` prima di assumere che un problema visivo sia CSS quando coinvolge un intero pannello.
25. ✅ **RISOLTO** — **`.pr-form-actions` senza `flex-wrap`**: su sidebar 300px i 3 pulsanti (Annulla/Storico/Salva aggiornamento) andavano in overflow orizzontale e venivano tagliati da `.sidebar{overflow:hidden}` (es. "Annulla" → "nnulla"). Aggiunto `flex-wrap:wrap` in `mappa_impresa_caricamento.html`.
26. ✅ **RISOLTO** — **"Aggiorna Scavi" apriva cantiere sbagliato**: il fallback quando non c'era match esatto sulla tratta prendeva "il primo cantiere dello stesso lotto" alla cieca. Ora: match esatto → apri; nessun match + lotto con 1 solo cantiere → apri; nessun match + lotto con più cantieri → messaggio, nessuna apertura automatica; nessun match + nessun cantiere sul lotto → messaggio "autorizzazione non ancora ottenuta". File: `mappa_impresa_caricamento.html`.
27. ✅ **RISOLTO** — **Bug `LAVORABILE` in `_compute_tratta_summary` (`server.py`)**: `need_no`/`need_ord` venivano letti SOLO dal flag `NULLA OSTA NECESSARIO`/`ORDINANZA NECESSARIA` sulla riga AUT. Se il flag era vuoto/NO ma esistevano comunque righe reali NULLA OSTA/ORDINANZA non ottenute per la tratta, `LAVORABILE` risultava `SI` per errore. Fix: `no_effettivo = (need_no=="SI") or bool(no_latest)` (idem `ord_effettivo`) — vincolante se il flag lo dichiara O se la pratica esiste davvero nei dati.
28. ✅ **RISOLTO** — Integrata la tabella "Tutti i Cantieri" di `scavi.html`: aggiunte colonne `codice_cantiere`, `impresa`, `tecnica_scavo` (label via `TECNICA_LABEL`) — dati già esposti da `GET /api/cantieri` ma non renderizzati. Aggiunti anche a `haystack` di ricerca.
29. ✅ **RISOLTO** — Nuovo endpoint `POST /api/admin/regenerate-derived` (auth `x-upload-token`): rilegge il Master.csv corrente e richiama `_regenerate_derived_files` senza upload — serve a riprocessare i dati esistenti dopo un fix a `_compute_tratta_summary` senza toccare Master.csv.
30. ✅ **RISOLTO** — **Popup mappa: info cantiere integrate** (prima mostravano solo dati pratica/autorizzazione).
    - `mappa_impresa_caricamento.html`: sezione "Cantiere" completa (stato+colore, tecnica, metri scavati/totali+%, impedimento), dati da `SC_CANTIERI` precaricato all'avvio (`scEnsureInit()` fire-and-forget in testa alla IIFE di init mappa).
    - `mappa.html` (già allora anche `mappa_impresa.html`, rimossa rev.126): stessa sezione ma **sola lettura** (nessun bottone azione), dati da `GET /api/cantieri` (pubblico, no auth) in `RO_CANTIERI` (costanti locali `RO_SC_STATO_LABEL/COLOR/TECNICA_LABEL`, funzione `_roLoadCantieri()`).
    - Lookup in tutti e 3 i casi: `cantieri.find(c => (c.tratte||[]).some(t => t.tratta_id === p.TRATTA_ID))`.
31. ✅ **RISOLTO** — `.btn.secondary{color:var(--muted)}` faceva sembrare disabilitati i pulsanti secondari (es. "Nuova pratica"/"Aggiorna Scavi" nel popup mappa apparivano "spenti" accanto al primario blu). Cambiato a `color:var(--text)` + hover `border-color/color:var(--accent)` in tutti e 3 i file mappa.
32. ✅ **RISOLTO** — **`scavi.html`: 3 bug segnalati dall'utente su KPI/barre/tabella (rev.8)**:
    - **KPI cards**: contavano solo i lotti aggregati sul "peggiore" stato, senza metri né %. Ora `_renderKpi()` conta i cantieri reali da `CANTIERI_RAW` e popola `#kpi{Nav,All,Cor,Com,Sos}Sub` con `metri m · pct% sul totale`.
    - **Barre "Avanzamento per Cluster" e "per Lotto"**: la riga scavi era binaria (un unico segmento colorato `in-corso`/stato-peggiore + grigio "non scavato"), non rifletteva i singoli stati dei cantieri che compongono cluster/lotto. Ora `_loadCantieri()` traccia `statoMetri{stato:metri}` per lotto e cluster (propagato in `LOTTI[].statoMetri` e `window._CLUSTER_SCAVI[cl].statoMetri`); `_renderClusters()`/`_renderBars()` disegnano un segmento per ogni stato reale (ordine/colori da `STATO_ORDER`/`STATO_COLOR`) + eventuale segmento grigio "Non tracciato" per i metri senza cantiere associato.
    - **Tabella "Tutti i Cantieri"**: vedi voce 28.
33. ✅ **RISOLTO (rev.9)** — **Bug `metri_totali` incoerente in `_sync_cantieri()` (`server.py`)**: `metri_totali` veniva ricalcolato ad ogni sync sommando solo le tratte con `lavorabile==True` (riga ~2229) e sovrascritto con `$set` senza mai controllare `metri_scavati` già accumulato. Siccome `lavorabile` dipende anche da NULLA OSTA/ORDINANZA (non solo dall'AUTORIZZAZIONE), un cantiere già "in corso" con metri regolarmente rendicontati poteva vedersi azzerare `metri_totali` a un sync successivo (visto in produzione: `CA/3/2A`, 0 totali / 600 scavati). **Chiarito dall'utente**: `lavorabile` è un flag SOLO per la visualizzazione in mappa, non deve limitare cosa un'impresa può rendicontare. Fix: `metri_totali` ora somma **tutte** le tratte della pratica (autorizzazione ottenuta), indipendentemente da `lavorabile` — di fatto uguale a `metri_totali_potenziali` (campo lasciato per compatibilità con pagine che lo leggono separatamente). ⚠️ **Da fare manualmente una tantum**: i documenti `cantieri` già in Mongo restano con il vecchio `metri_totali` finché non gira un sync — chiamare `GET /api/admin/sync-cantieri` (auth `x-upload-token`) per riallinearli subito, altrimenti si autocorreggono al prossimo upload/approvazione di Master.csv.
34. **RISOLTO (rev.21)** — **Tabella "Tutti i Cantieri" in `scavi.html`: header non andava a capo**: `white-space:nowrap` globale su `.cant-table thead th` + `tight:true` che lo riforzava inline → 13 colonne in overflow orizzontale anche con label corte, nonostante gli aggiustamenti di rev.18/rev.20. Fix: `white-space:normal` sugli header (con `line-height:1.35`), `tight` ora `max-width:70px` invece di `nowrap`, `.th-cell` allineato `flex-start` (serve per header su 2 righe), label accorciate (Prov., Stato, Avanz., M. Tot., M. Scavati, Registro). Bottone "Registro" per riga: rimossa emoji 📋 (vietata da brand kit §8.11) e stile inline generico, sostituiti con classe `.btn-registro` (bordo/hover accent Retelit, icona SVG inline al posto dell'emoji).
35. **RISOLTO (rev.22)** — **Card "Registro Lotti · Stato Cantiere" rimossa da `scavi.html`** (ridondante con "Avanzamento per Lotto" + tabella "Tutti i Cantieri"): eliminato il markup della card, la funzione `_renderTabella()` e la sua chiamata in `_renderAll()`. I pill province (`.prov-pill`, stesso stile già usato nella tabella rimossa) sono stati spostati sotto al numero lotto in `_renderBars()` (card "Avanzamento per Lotto"), dentro `.bar-lotto-wrap`.
36. **RISOLTO (rev.23)** — **Colore distintivo per provincia in tutta `scavi.html`**: nuovo `provColor(p)`/`provPill(p)` (hash deterministico su palette di 10 colori brand-coerenti — stessa provincia = stesso colore sempre, non serve enumerare le province a mano). Applicato a: pill "Avanzamento per Lotto", card modal cantiere, colonna "Prov." tabella "Tutti i Cantieri", modal Lotto (era hardcoded su `--accent`), modal Cluster (era testo semplice `MI / PV`), card "Metri scavati per provincia". `.prov-pill` CSS ora senza colori fissi (solo forma), il colore è sempre inline via `provColor()`.
37. **RISOLTO (rev.24)** — **Redesign card cantiere nel modal stato (`openModalStato`)**: erano righe separate da bordo inferiore con testo semplice "Label: valore" — ora card vere (`.msc-*`, bordo+radius+padding), chip invece di label:valore, provincia con `provPill()`, icone SVG al posto delle emoji 📍/⏸/📝 (vietate da brand kit §8.11). ⚠️ Bug corretto nello stesso giro: `background:var(--danger)0d` non è CSS valido (non si concatena testo dopo `var()`) e `--danger` non è nemmeno definita in `scavi.html` — sostituito con hex diretto `#C0392B` (stesso token brand kit). **(rev.25)**: rimossi chip "Tecnica" e "Tratte lavorabili/bloccate" dalla card (giudicati privi di senso in questo contesto dall'utente) — resta solo "Impresa".
38. **Redesign vista dirigenti `scavi.html` (sessione 2026-07-03, no rev.-tag per evitare collisione col rev.38 changelog sotto)**: (a) `.phase-header` ("Stato Cantieri") portato da 9px a 13px, allineato a `.page-title`; (b) **exec-strip** in header: 3 numeri grandi (Avanzamento Fisico %, Permessi Ottenuti %, Cantieri in Sospeso — rosso se >0) — alimentati da `pctGlob`/`pctPermessi`, calcolati in `_renderRiepilogo()` ma prima mai scritti a video (dead code); titolo/eyebrow/sottotitolo pagina rimossi su richiesta utente, resta solo l'exec-strip allineata a destra (`#pageSubDyn` di nuovo assente dal markup, `_renderPageSubtitle()` esce per guard `if(!el)return`, nessun errore); (c) nuova card **"Performance Imprese"** subito sotto i KPI: aggregazione per `impresa` con colonne Lotto/Cluster (multi-valore, Set ordinato con `_lottoCompare`), Cantieri, Completati/In Corso/Sospesi, Metri Tot., % Avanzamento (bar+colore), Ritmo m/gg (da `data_inizio_effettiva`→oggi o `data_fine_effettiva`) — ordinabile per colonna (`_sortImprese()`, funzione `_renderImprese()` in pipeline `_renderAll()`); (d) rimossa legenda duplicata identica tra "Avanzamento per Cluster" e "Avanzamento per Lotto" (resta una sola, con rimando testuale nella seconda). ⚠️ Ritmo m/gg è vuoto per i cantieri senza `data_inizio_effettiva` valorizzata — verificare copertura dato in produzione.
39. **`scavi.html` — bug/rifiniture "Performance Imprese" + modal "Stato Cantieri" (rev.77)**: (a) ✅ **RISOLTO — bug % avanzamento non coerente con la vista "per Lotto"**: `_renderImprese()` calcolava il denominatore sommando `metri_totali` dai soli cantieri già sincronizzati in Mongo per quell'impresa (es. Sielte/lotto 2A = 7852m) invece del totale da PERMESSI (Riepilogo_progettazione.csv, 11.589m per lo stesso lotto — più completo, copre tutte le tratte autorizzate anche se non ancora un cantiere Mongo), come fa correttamente `byLotto`/`LOTTI`. Risultato: stessa `metriScav`, denominatori diversi → 8% mostrato invece di 5,5%. Fix: nuovo `g.metriTotByLotto{}` per-impresa-per-lotto, `metriTotReale` = somma di `PERMESSI[lotto].totale` (fallback sul dato cantieri se il lotto non è in PERMESSI) — usato sia per il denominatore di `pct` sia per la colonna "Metri Tot." visualizzata (prima disallineata rispetto al nuovo pct). (b) ✅ **RISOLTO** — colonne Lotto/Cluster della tabella non erano ordinabili: mancavano `class="imp-th-sort"`+`onclick="_sortImprese(...)"` sui `<th>`, e il comparator confrontava direttamente due `Set` JS (`a.lotti < b.lotti` è sempre `false` → nessun ordinamento) — ora le due chiavi vengono convertite alla stessa stringa ordinata mostrata a video (`[...set].sort(...).join(', ')`) prima del confronto. Ordinamento di default per "miglior avanzamento" (`{key:'pct', dir:'desc'}`) era già corretto, il sort rotto sulle altre colonne dava probabilmente l'impressione che l'intera tabella non si riordinasse. (c) Rinominata label header "Avanzamento Fisico" → "Avanzamento globale" (solo testo, chiave dato `execPctScavi` invariata). (d) Card "Avanzamento per Cluster": rimosso il nome descrittivo del cluster (es. "Milano urbano", da `CLUSTER_LABEL`) — resta solo il badge "Cluster N"; `CLUSTER_LABEL` lasciata invariata altrove (modal cluster, dove il nome resta utile). (e) `openModalStato()` (modal aperta cliccando le card KPI "Stato Cantieri"): sostituita la lista a card `.msc-*` con la stessa tabella/colonne di "Tutti i Cantieri" (Cod. Cantiere, Pratica, Ente, Lotto, Cluster, Prov., Comune, **Impresa**, Stato, Avanz., M. Tot., M. Scavati, bottone Registro) — chiude anche TODO §11 12.1/12.2 (impresa ora visibile sia in "Performance Imprese" sia nelle card modal cantiere). ⚠️ **Trade-off non richiesto esplicitamente**: la vecchia card mostrava anche `motivo_blocco`/`data_ripresa_stimata` (solo per stato Sospeso) e `note` libere, assenti nella tabella "Tutti i Cantieri" — questi due dati non sono più visibili da questa modal. Segnalato all'utente, non reintegrato salvo richiesta. (f) ✅ **RISOLTO (rev.78, follow-up immediato)** — dopo il punto (e) il div `.modal` di `#modalStatoOverlay` era rimasto a `width:560px` inline (dimensionato per la vecchia lista card), causando scroll orizzontale forzato con la tabella a 13 colonne (screenshot utente: solo le prime 7 colonne visibili). Portato a `width:1180px;max-width:96vw` — stesso pattern già usato per il modal Cluster di `index.html` in rev.75 quando gli fu aggiunta una colonna.
40. **Formato reale di `Master.csv`**: il file di produzione è **tab-separated** (non comma, non semicolon) — `server.py` lo scrive sempre con `sep="\t"` verso GitHub/derivati. Qualsiasi `Master.csv` ricevuto per editing va verificato con `head -1 file | cat -A` prima di assumerne la struttura: se ogni riga appare come un unico blob tra virgolette, è corrotto (visto in rev.50, archiviato) e va riconvertito prima di qualunque modifica. Occhio anche a varianti `;`-separated/latin-1/CRLF (viste in rev.63-64, archiviato) e a nomi colonna quasi-identici (`DATA_PREVISTO_RILASCIO` vs `DATA_PREVISTA_RILASCIO`, femminile è quello corretto usato dal codice).

41. **Split tratte esistenti (nuovi TRATTA_ID) — sessione 2026-07-06**: `_regenerate_derived_files` patcha SOLO le property delle feature già presenti in `QGIS.geojson` (match per TRATTA_ID, geometria invariata) — non crea geometrie nuove. Procedura corretta: 1) split linea in QGIS, nuovi TRATTA_ID univoci, export geojson (verificato CRS export = EPSG:4326/CRS84, non UTM, altrimenti coordinate fuori scala e mappa mostra vista mondo); 2) upload con `target=QGIS.geojson` esatto — `admin.html` valida che il nome file scelto combaci col target selezionato (riga ~842), quindi niente più rischio di salvataggio sotto nome sbagliato; 3) SOLO DOPO caricare/approvare Master.csv coi nuovi TRATTA_ID. Ordine invertito = righe orfane senza geometria. ⚠️ **Rischio residuo non risolto**: nessun lock/versioning ottimistico tra upload manuale admin e `_regenerate_derived_files` triggerato da approvazione `pending_updates` impresa — se un'approvazione impresa scatta a ridosso dell'upload manuale del geojson, vince chi scrive per ultimo su `uploaded_at`. Dopo un upload critico, verificare sempre `GET /api/files` (campo `size`/`modified` dell'entry) per confermare che sia la versione servita, prima di considerare chiuso il lavoro.

42. **RISOLTO (2026-07-09)** — **`imprese.html`, tab "Le mie submission": aggiunto codice pratica + fix bug "Nessun campo" sulle submission "Nuova pratica"**. Contesto: ogni `change` di una submission `update` ha forma `{tratta_id, ente, tipo_permesso, original_pratica, lunghezza, fields:{...}}`, mentre una submission `new` ha `change` = record flat completo (`Source.Name`, `TRATTA_ID`, `TIPO_PERMESSO`, `PRATICA`, ecc., **senza** wrapper `fields`). `showSubDetail()` leggeva sempre `c.fields||{}` → per le submission "Nuova pratica" risultava sempre `{}`, quindi il popup mostrava "Nessun campo" nonostante i dati fossero presenti nel `change` stesso. Fix:
    - Nuovo helper `_codiceForChange(c, type)`: per `type==='new'` usa `buildCodice(c)` diretto (il change ha già tutti i campi); per `type==='update'` cerca in `PRATICHE` (già caricato) la riga con `TRATTA_ID`+`ENTE`+`TIPO_PERMESSO` (+ `original_pratica` se disponibile) per recuperare `_codice` (serve il lotto/`Source.Name`, assente nel change di update).
    - Tabella "Le mie submission": nuova colonna "Codice pratica" (primo codice + `+N` se la submission raggruppa più pratiche in un solo invio).
    - Popup dettaglio: ogni blocco `.sub-change` mostra ora il codice pratica accanto a tratta/ente/tipo; header del popup mostra la lista di tutti i codici coinvolti se >1; per `type==='new'` i campi mostrati ora sono l'intero record (esclusi i campi già in header: `Source.Name`/`TRATTA_ID`/`ENTE`/`TIPO_PERMESSO`) invece di un oggetto vuoto.
    - Sequenziato `loadPratiche()` prima di `loadMine()` nel boot (`await`) — prima partivano in parallelo, rischio di race condition sul lookup `_codiceForChange` per le submission `update` se `PRATICHE` non era ancora popolato al primo render.

⚠️ **Segnalato, non risolto**: font-size sotto i 12px in decine di punti di `scavi.html` (9px/10px/10.5px/11px su badge, chip, celle tabella, eyebrow) — viola la regola brand kit §4.6 "mai sotto 12px in interfaccia". Font-family/pesi (Raleway + JetBrains Mono) invece corretti e caricati bene. In attesa di conferma utente prima di un pass esteso (60+ punti, rischio di rompere il layout di badge/tabelle compatte).

43. **RISOLTO (2026-07-09)** — **`imprese.html`, tab Solleciti: `PROTOCOLLATO INTEGRAZIONE` escluso per errore dalla lista pratiche sollecitabili**. `SOL_STATI_ESCLUSI` lo trattava come uno stato "chiuso" (insieme a `NECESSARIA INTEGRAZIONE`/`IN REDAZIONE INTEGRAZIONE`), ma in `STATO_TRANSITIONS` ha lo stesso ruolo di `PROTOCOLLATO`: pratica (di integrazione) inviata all'ente, in attesa di risposta (→ `NECESSARIA INTEGRAZIONE` o `OTTENUTO`) — quindi va sollecitato esattamente come `PROTOCOLLATO`. Rimosso dall'esclusione. Effetto visibile: prima la lista "Nuovo sollecito" mostrava quasi solo pratiche `INVIATO`/`PROTOCOLLATO` (segnalato dall'utente via screenshot), ora include anche le integrazioni protocollate.
⚠️ Non verificato in questo giro: logica del numero sollecito automatico (`sol-numero`).


---

## 8bis. Palette colori stato — mapping canonico (tutte le pagine)

Usata da `STATO_COLORS` (mappa/scavi) e `COLORS`/`STATI_COLORI_MAP` (index). Se aggiungi/tocchi uno di questi dizionari in una pagina, allinealo a questi valori:

| Stato | Hex | Token brandkit |
|---|---|---|
| IN ATTESA / IN REDAZIONE | `#6B7685` | `--gray-500` |
| IN FIRMA RDS | `#D08A1A` | `--warn` |
| INVIO PRELIMINARE | `#043F75` | `--retelit-blue` |
| INVIATO / PROTOCOLLATO | `#41BBD9` | `--retelit-sky` |
| NECESSARIA INTEGRAZIONE | `#C0392B` | `--err` |
| IN REDAZIONE INTEGRAZIONE / PROTOCOLLATO INTEGRAZIONE | `#436A93` | `--retelit-blue-75` |
| OTTENUTO | `#1E9E6A` | `--ok` |

File allineati: `index.html`, `mappa.html`, `mappa_impresa_caricamento.html`, `scavi.html`, `imprese.html`.

**2026-07-09** — fix colori stato (bug, non allineamento di routine):
- `mappa_impresa_caricamento.html`: `STATO_COLORS['IN REDAZIONE INTEGRAZIONE']` era `#C0392B` (rosso, sbagliato) invece di `#436A93` (blu) → corretto.
- `imprese.html`: non aveva alcuna `STATO_COLORS`. Le celle stato usavano `class="stato-${stato.replace(/\s+/g,'.')}"` con regole CSS tipo `.stato-IN.REDAZIONE` — selettore composto che richiede DUE classi separate (`stato-IN` AND `REDAZIONE`), non matcha mai un'unica classe col punto letterale generata dal replace. Risultato: tutti gli stati multi-parola (`NECESSARIA INTEGRAZIONE`, `PROTOCOLLATO INTEGRAZIONE`, `IN REDAZIONE INTEGRAZIONE`) non prendevano MAI il colore dal CSS. Fix: aggiunta `STATO_COLORS` locale (identica alla tabella sopra) + `color` inline via helper `statoColor()`, rimosse le classi `.stato-X` rotte. Stesso fix di pattern applicato alla card pratiche sidebar in `mappa_impresa_caricamento.html` (usava lo stesso selettore rotto nonostante avesse già `STATO_COLORS`/`getColor()` disponibili — ora usa `getColor()` inline).
- Verificato: `scavi.html`, `mappa.html`, `hub.html` non usano questo pattern — bug isolato a queste due pagine.

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

## 11. TODO aperti (da Checklist Bug e Migliorie, importata 2026-07-02)

**Regola permanente**: dopo ogni modifica (in questa sessione o future), verificare se corrisponde a una voce di questa tabella e aggiornarne subito lo Stato (es. "Chiuso (rev.NN)" + breve nota), anche se non era quella la richiesta esplicita dell'utente.

Fonte: checklist Excel utente. Solo voci non "Completato" (34/59 già completate, non riportate qui — vedi file originale per lo storico completo).

| ID | Pagina | Descrizione | Stato | Priorità | Note |
|---|---|---|---|---|---|
| 6.1 | imprese_scavi | Implementare modello strutturato caricamento dati scavi | In corso | Alta | |
| 6.3 | imprese_scavi | Identificare informazioni da visualizzare nella pagina dedicata | In corso | Alta | |
| 6.4 | imprese_scavi | Migliorare la visibilità delle card dei cantieri | In corso | Alta | |
| 10 | NEW | Pagina Gantt progetto + tabella associazione impresa/lotti/cluster (%design, %perm, %delivery, metri totali) | Da fare | Alta | ENTRO MERCOLEDÌ |
| 11.6 | Master | Pratica NO/27/1A: correggere data invio (da quando è partito ENRI) | Da fare | Alta | |
| — | server.py | `SESSION_SECRET` a 8 cifre numeriche | Aperto (voluto) | Bassa | Bruteforce offline HMAC in tempi brevi se un token viene intercettato — rischio accettato dall'utente per la minaccia target ("smanettone", non attaccante dedicato) |
| 11.1 | Index | Eliminare previsione "mia" e richiederla all'impresa | Chiuso (rev.48) | — | Campo compilato dall'impresa, non più calcolato |
| 11.2 | Index | Creare nuova colonna "update" | Già presente (rev.44) | — | |
| — | All | Rename `COORDINAMENTO`→`INVIO PRELIMINARE` | Risolto (rev.43) | — | |
| 11.4 | Index | Portare i solleciti nei popup insieme alle note | Chiuso (rev.167) | — | Badge dedicati (`_solBadge`/`_solSedBadge`), popup separato da Note |
| 11.9 | Master | % ottenuto Valtellina non compare (lotto piccolo?) | Risolto (rev.49) | — | Soglia `pct>=5` nascondeva la % su segmenti piccoli |
| 12.1/12.2 | Scavi | Associazione/visibilità lotto-impresa nelle card cantiere | Chiuso (rev.77) | — | Card "Performance Imprese" + colonna Impresa nei modal |
| 12.3 | Scavi | Stato cantieri in topbar più grande | Chiuso (rev.76) | — | |
| 12.4/13 | Scavi | Tabella imprese + vista dirigenziale | Chiuso (rev.76) | — | |
| — | Scavi | Font-size sotto i 12px fuori brand kit | Chiuso (rev.101) | — | |
| — | Mappa_imprese_caricamento | Export geojson lotti impresa | Chiuso (rev.78-80) | — | `exportGeoJSONLotti()` |
| — | Imprese | Stato ex-coordinamento (Invio Preliminare) | Chiuso (rev.112) | — | Già in `STATO_TRANSITIONS` |
| — | Master/server | Colonna `DATA_UPDATE` mancante | Chiuso (rev.114-115) | — | Colonna + `_touch_data_update*` per i solleciti |
| — | milestone.html/admin.html | Guardia auth mancante | Chiuso (rev.131-132) | Sicurezza | Guardia client-side; per le pagine dati la vera barriera è `_require_staff_session` server-side (rev.129) |
| — | server.py | Endpoint staff senza controllo ruolo | Chiuso (rev.129-130) | Sicurezza | `_require_staff_session` su 6 endpoint (v. changelog) |
| — | server.py | GridFS senza limite versioni | Chiuso (rev.130-131bis) | — | `KEEP_VERSIONS=4` + prune automatico |
| — | server.py | Login senza rate limit | Chiuso (2026-07-06) | — | Lockout 429/300s dopo 5 tentativi falliti (in-memory) |

---

_Ultimo aggiornamento: 2026-07-22 (rev. 204)_

- **rev.204** — `polizze_convenzioni.html`: segnalato dall'utente da screenshot — card KPI (rev.201) troppo grandi, spazio sprecato (numero e label su una riga con `justify-content:space-between` che li spingeva agli estremi, chip breakdown su riga separata sotto). Card ora a riga singola: numero+etichetta compatti a sinistra (`.kpi-num`, non più `.kpi-top`), chip breakdown affiancati sulla stessa riga (wrap se lo spazio non basta) invece che sotto. Padding 10px 14px→8px 12px, valore 20px→16px, chip 10px→9.5px con padding ridotto. `node --check` OK.


- **rev.203** — `server.py`, `GET /api/admin/polizze-convenzioni/data-richiesta`: segnalato dall'utente — Data Richiesta e Data Emissione risultavano **tutte non popolate**. Trovato e corretto un bug reale di robustezza: `delete_many({"_id": {"$nin": list(seen_keys)}})` girava anche quando `seen_keys` era vuoto (es. `CONVENZIONE`/`POLIZZA` non trovate nel Master in quel momento) — con `seen_keys` vuoto, `$nin: []` fa match su **tutti** i documenti e azzera l'intera collection `pol_conv_dates_col`, comprese le date già raccolte. Ora la funzione esce prima (senza toccare la collection) se nessuna delle 2 colonne è nel Master, e il `delete_many` viene saltato se `seen_keys` è vuoto per qualunque altro motivo. **Nota per l'utente**, causa più probabile del problema riportato: 1) Data Emissione era assente perché il `server.py` in uso non aveva ancora il fix rev.202 (mancava `"EMESSA": "data_emissione"` in `STATO_DATE_FIELD` e il campo nella response) — risolto qui. 2) Queste date si popolano solo **da ora in poi**, al momento in cui una pratica passa di stato tramite il pulsante "Salva" in questa pagina — non possono essere ricostruite retroattivamente per pratiche già `EMESSA`/`INVIATA` prima che la feature esistesse (nessuno storico da cui recuperarle). 3) Data Richiesta dipende dalla colonna `DATA_ULTIMA_MODIFICA` di Master.csv: se è vuota per quelle righe (es. righe caricate/importate senza passare da un update tracciato), resta vuota — nessun fix di codice può inventare quella data, va verificato/valorizzato a monte nel CSV se serve.
- **rev.202** — `polizze_convenzioni.html` + `server.py`: aggiunta 4ª colonna data "Data Emissione", stesso pattern di rev.197/198 — si fissa (una sola volta) al primo salvataggio con stato `EMESSA`. Backend: `STATO_DATE_FIELD` esteso con `"EMESSA": "data_emissione"`; `GET .../data-richiesta` risponde ora `{date, date_invio, date_richiesta_rds, date_emissione}`. Frontend: entrambe le tabelle (Convenzioni e Polizze) a 9 colonne (Pratica/Lotto/Ente/Stato permesso/Convenzione o Polizza/Data Richiesta/Data Richiesta RDS/Data Invio/Data Emissione), colspan aggiornati ovunque.
- **rev.201** — `polizze_convenzioni.html`: revisione layout su richiesta utente (screenshot) — non voleva le due tabelle affiancate. `.split` da grid 2 colonne a flex verticale (Convenzioni sopra, Polizze sotto, entrambe piena larghezza) per lasciare spazio a più colonne in futuro. Rimosso il sub-panel `.dates-panel` introdotto in rev.197/198: le 3 colonne data (Data Richiesta / Data Richiesta RDS / Data Invio) tornano in riga nella tabella principale, ora su **entrambe** le tabelle (Convenzioni prima ne aveva solo 1, Polizze le aveva nel sub-panel). Nessuna modifica backend necessaria per questa parte: `STATO_DATE_FIELD`/`get_pol_conv_date_richiesta` erano già generici per campo, bastava leggere `DATE_INVIO_MAP`/`DATE_RDS_MAP` anche lato Convenzioni in `parseAndRender()`. KPI in alto ridisegnate: card più piccole (val 28px→20px) con breakdown per stato sotto il numero (`countByStato()`+`renderKpiBreakdown()`, chip colorati riusando `STATI_COLORS`). Confermato con mockup (Visualizer) prima di implementare. ⚠️ Nota di processo: numerati 201-203 anziché 199-200 (già usati in questa sessione dall'utente per `scavi.html`) per evitare collisione — nessun lavoro duplicato, confermato via diff. `py_compile`/`node --check` OK.

_Ultimo aggiornamento: 2026-07-21 (rev. 200)_

- **rev.200** — fix rev.199: la colonna "Date" di `scavi.html` mostrava "in ritardo"/date anche per cantieri **mai toccati dall'impresa**, perché `data_inizio_prevista` può già essere valorizzata dal seed/import (`cantieri.csv`) prima di qualunque caricamento reale. `server.py`: `GET /api/cantieri` e `GET /api/imprese/cantieri` ora restituiscono `log_count` (lunghezza dell'array `log`, senza esporne il contenuto) invece di limitarsi a fare `pop("log")`. `scavi.html`: `_scavoDateCell()`/`_scavoDatePlain()` mostrano "nessuna data" se `log_count` è 0/assente, ignorando eventuali date di seed non confermate da un aggiornamento impresa; il pannello riepilogo date nel modal Registro (rev.199) ora appare solo se `log.length > 0`.


_Ultimo aggiornamento: 2026-07-21 (rev. 199)_

- **rev.199** — `scavi.html`: le 5 date cantiere (`data_inizio_prevista/effettiva`, `data_fine_prevista/effettiva`, `data_ripresa_stimata`) erano salvate da `imprese_scavi.html`/`admin.html` ma **mai mostrate** nella vista staff — nuova colonna "Date" in tabella "Tutti i Cantieri" (`CANTIERI_COLS`+render riga, colspan 13→14) e nei modal Stato/Cluster (`_cantieriTableHtml`): mostra la data più rilevante per lo stato corrente (inizio previsto/effettivo, fine effettiva, ripresa se sospeso) con scostamento gg vs previsto colorato (`--ok`/`--err`) e tooltip con tutte le date. Helper `_scavoDateCell()`/`_scavoDatePlain()`. Modal "Registro" (`openModalRegistro`): aggiunto pannello riepilogo date pianificate/effettive sopra il log eventi (prima mostrava solo la data dell'ultimo aggiornamento, non le date di pianificazione). Nessuna modifica backend: i dati esistevano già in Mongo, mancava solo la visualizzazione. TODO aperto (prossimo giro, da decidere con l'utente): rendere le date più obbligatorie/visibili lato `imprese_scavi.html` e calcolare scostamenti/ritardi aggregati a livello lotto/cluster.

- **rev.198** — `polizze_convenzioni.html` + `server.py`: tabella Polizze, tracciata anche "Data Richiesta RDS" (stato `RICHIESTA RDS`), oltre a Data Richiesta/Data Invio (rev.197). Le 3 date spostate fuori dalla tabella principale in un sub-panel sotto (`.dates-panel`, `buildDatesRows()`), per fare spazio — tabella Polizze torna a 5 colonne. Backend: `STATO_DATE_FIELD = {"INVIATA": "data_invio", "RICHIESTA RDS": "data_richiesta_rds"}` in `update_polizza_convenzione`, fissate una sola volta (non sovrascritte da transizioni successive). `GET .../data-richiesta` → `{date, date_invio, date_richiesta_rds}`. Tabella Convenzioni non toccata.
- **rev.197** — stessi file: aggiunta "Data Invio", popolata al primo salvataggio con stato `INVIATA`. Fix bug collaterale: il vecchio `$setOnInsert` su `data_richiesta` non valorizzava documenti Mongo già esistenti — sostituito con `find_one`+insert/update esplicito.
- **rev.196/195/194/193** (`admin.html` tab Cantieri, `imprese_scavi.html`) — allineamento visivo cantieri a `scavi.html`: chip stato colorati + barra avanzamento metri nella tabella elenco, select stato colorato nel pannello edit; bottone "Storico" spostato direttamente in ogni card; fix nowrap colonne Codice/Pratica; aggiunto campo mancante "Data inizio prevista" e **fix bug**: le 3 date cantiere sono salvate in Mongo in ISO (`aaaa-mm-gg`), non `gg/mm/aaaa` come Master.csv — il pannello applicava comunque la conversione dd/mm/yyyy e le mostrava sempre vuote; ora lette/scritte native.
- **rev.192** — audit di conferma (nessun nuovo lavoro): confermato che 4 fix di sicurezza (proxy Google Apps Script → backend Mongo, auth su `data-richiesta`, header sessione, sync cantieri non bloccante) erano già presenti nel repo corrente rispetto a uno snapshot precedente ricaricato per errore dall'utente.
- **rev.191** — `mappa.html`+`mappa_impresa_caricamento.html`: fix arrotondamento "Metri scavati" (decimali float infiniti). `hub.html`+`ai_alerts.html`: pagina Alert Predittivi nascosta per ruolo `dl` (solo redirect client-side, non gated server-side — gap noto).
- **rev.190** — **fix strutturale** `js/api-config.js`: il wrapper `window.fetch` inietta ora automaticamente `x-session-token` su ogni chiamata `API_BASE`-prefixed priva di header esplicito, per chiudere la classe di bug "fetch senza token → 401 silenzioso" ricorsa più volte (rev.129/130/135/136/149). Corretto anche 1 caso reale residuo in `scavi.html`.
- **rev.189** — nuovo ruolo `dl` (Direzione Lavori): vede tutto come `user` tranne Milestone. Gating reso reale **lato server** (non solo redirect client-side): dati Milestone spostati in `server.py` dietro `GET /api/milestone` + nuova dependency `_require_milestone_session` (403 se `ruolo=='dl'`), ruolo letto dal token HMAC firmato.
- **rev.188** — `server.py`+`index.html`+`admin.html`: le note su una pratica ora taggate `[RETELIT]`/`[IMPRESA]` in Master.csv (`_tag_note()`), mostrate come chip colorato invece di testo indistinguibile.

_Ultimo aggiornamento: 2026-07-13 (rev. 187)_

- **rev.187/186** — `gantt.html`: **bug reale in `mergeMasterCsv()`** (regressione rev.180) — quando l'ultimo stato Master.csv di una pratica è `NO COMPETENZA`, la funzione usciva prima di valutare la riga Progettazione anche se la pratica era realmente avanzata prima di chiudersi così. Fix: nuova mappa `everAdvanced[pratica|ente]` costruita su **tutto** lo storico, non solo l'ultima riga. Nota collegata: una stessa pratica può avere più segmenti fisici distinti nel Gantt (stesso `_pratica`+`_ente`) — il fix di aggiornamento va applicato con `filter().forEach()`, non `find()` (si fermava al primo segmento). Aggiunta anche barra di riepilogo (rollup stile MS Project) sui gruppi collassati.
- **rev.185** — `gantt.html`: **bug reale** — da rev.182 Opere Civili è ricalcolata a metri/giorno ma Test/As Built restavano ancorate alle vecchie date fisse del `.mpp`, aprendo gap di settimane per pratiche piccole. Fix: `cascadeOpereCiviliDurations()` trasla i successori dello scostamento reale rispetto alla baseline `.mpp` (`_mppBaseEnd`). Refactor collegato: le cascate da spostamento-permesso e da ricalcolo-durata si sovrascrivevano a vicenda invece di sommarsi — ora accumulano in `_deltaPermesso`+`_deltaKm` separati e sommati prima di applicarli.
- **rev.184** — `server.py`+`gantt.html`: tasso di scavo (100 m/g) reso configurabile da UI, con priorità PERMESSO > IMPRESA > LOTTO > DEFAULT. Nuova collection `gantt_rates_col` (scope libero `"lotto:X"`/`"impresa:X"`/`"pratica:X"`/`"global"`), endpoint `GET/PUT/DELETE /api/gantt/rates/{scope}`. Race condition trovata e corretta: la risoluzione tassi deve completare **prima** di `mergeCantieriProgress()` (dati scavi reali devono sempre vincere sulla stima).
- **rev.183/182/181/180** — `gantt.html`, evoluzione calcolo Opere Civili/Progettazione: durata Opere Civili da km reali invece di baseline uniforme `.mpp` (`ceil(km*1000/100m)`, minimo 1gg), poi corretta a soli giorni lavorativi lun-ven (`addWorkDaysDmy`); % Progettazione dedotta da `STATO_PERMESSO` (IN ATTESA/IN REDAZIONE→0%, oltre→100%) e aggregata **in km** (non a conteggio pratiche), coerente con le altre metriche km-weighted della dashboard.
- **rev.179/178/177** — `gantt.html`: **riscrittura strutturale** da gerarchia piatta a Lotto→Comune→Pratica→5 Fasi (`GANTT_ROWS` 137→192 righe, metadati `_ente`/`_pratica`/`_comune` diretti invece di regex sulla label); nuova `propagateGanttDatesFromPermits()` (propaga in cascata via `GANTT_DEPS`/BFS lo scostamento quando un permesso slitta, senza toccare righe con dato scavi reale o override admin); fix bug KPI "Permessi ottenuti" che contava anche le milestone Materiali.

_Ultimo aggiornamento: 2026-07-07 (rev. 154–176)_ — `gantt.html`: dipendenze reali tra fasi estratte dal `.mpp` (`GANTT_DEPS`, connettori SVG, popover sola-lettura); modalità "modifica" (admin) con popover edit + persistenza override su Mongo (`gantt_overrides_col`, `_require_admin_session`); collassa/espandi per fase/pratica; KPI e tooltip milestone raffinati. `hub.html`: riposizionamento card Gantt/Milestone/Alert Predittivi/Pannello Gestione (3 iterazioni di layout). `imprese.html`: tabella Solleciti/Aggiorna pratiche — colonna "Data ottenimento" morta sostituita da "Data ultimo sollecito"; submit di sola nota permesso senza obbligo di cambiare stato. `server.py`: fix `DATA_UPDATE` mai aggiornato sui solleciti (mask troppo stretta, matchava anche su `pratica`/`tipo_permesso` invece che solo `TRATTA_ID`) + endpoint one-off di backfill.

_Ultimo aggiornamento: 2026-07-06/07 (rev. 129–148) — hardening sicurezza + isolamento dati impresa_

Sessione dedicata, in vista del go-live delle credenziali impresa. Falle trovate e chiuse in sequenza:
- **rev.129/130**: molti endpoint staff erano protetti solo da token valido, non da ruolo — un account `impresa` poteva chiamarli direttamente bypassando l'hub. Nuova dependency `_require_staff_session` (403 su ruolo impresa) su 6 endpoint (`lotti-cantieri`, `sopralluoghi`×3, `cantieri`, `cantieri/{key}/log`).
- **rev.135**: `/api/data*`, `/api/files`, `/api/preview`, `/api/uploads` erano **senza alcuna autenticazione** — chiunque poteva scaricare Master.csv/QGIS/Riepilogo dal backend. Gated con `_require_session`/`_require_staff_session`.
- **rev.139**: gap residuo di rev.135 — `_require_session` validava solo il token, non il ruolo: un token impresa poteva comunque scaricare i file interi (Master/QGIS/SED/Riepilogo di **tutti** i lotti). Fix: `SENSITIVE_FILES` + `_guard_sensitive_read()` (403 su ruolo impresa) + nuovo endpoint scoped `GET /api/imprese/master-sed` che filtra server-side sui lotti assegnati; questi 4 file non vengono più pubblicati sul repo pubblico GitHub (restano solo su GridFS). TODO operativo aperto: la storia git pubblica resta scaricabile finché non si fa un purge (git filter-repo/BFG) o si rende il repo privato.
- **rev.138/140**: 2 bug di isolamento dati cross-impresa — `get_solleciti` filtrava il lotto per substring invece di match esatto (un'impresa vedeva solleciti di lotti simili ma non suoi); `GET /api/imprese/cantieri/{key}/log` non verificava ownership sul lotto (leggibile lo storico di cantieri di altre imprese).
- **rev.131/132**: guardia auth client-side aggiunta a `admin.html`/`milestone.html` (redirect `hub.html` se ruolo non consentito) — resta bypassabile forzando `localStorage`, la vera barriera per le scritture resta `UPLOAD_TOKEN`/i controlli server sopra.
- **rev.133/134**: audit log azioni admin (`admin_actions_col`, `_log_admin_action`); fix `delete_upload` che rigenerava/pushava file ridondantemente anche cancellando versioni non correnti.
- **rev.136/137**: verifica TODO + audit pre-produzione sulle 3 pagine impresa (nessun bug bloccante residuo).

Nello stesso arco: rimozione pagina ridondante `mappa_impresa.html` (rev.126-128, superata da `mappa_impresa_caricamento.html`); `imprese.html`/`mappa_impresa_caricamento.html` — stato `NO COMPETENZA`/`OTTENUTO` nasconde campi Nulla Osta/Polizza/Convenzione non pertinenti, select→checkbox per quei 3 campi, rimosso campo "Ordinanza necessaria", fix bug di scope (`_openHelp`/`_retryBoot` non esposte su `window`, `onclick` falliva) (rev.113-125).

---

_Storico rev.65–rev.113 compattato_ (dettagli completi nei backup di sessione, non riportati qui):
- **`scavi.html`** (rev.65,77-91,96,101-110): iterazioni stile/larghezze colonne tabella cantieri, raggruppamento imprese per cluster/lotto, modal Cluster/Lotto, pulizia CSS morto.
- **`index.html`** (rev.65-75,97-100): fix header/popup Note (bug dati mancanti, note multiple, rimozione solleciti dal popup — riapre TODO 11.4), colonna Impresa nei modal, rimozione colonna "Prev. Rilascio", icone sort SVG.
- **`imprese.html`** (rev.111-123): tasto guida "?" (bug scope IIFE), NO COMPETENZA nasconde campi non pertinenti, select→checkbox, rimozione campo Ordinanza.
- **`mappa_impresa_caricamento.html`** (rev.78-80,122): export GeoJSON con esclusione campi gestionali, allineamento form a `imprese.html`.

_Storico rev.10–rev.64 archiviato in `AGENT_BRIEF_ARCHIVE.md`_:
- Rename `COORDINAMENTO`→`INVIO PRELIMINARE` (rev.34-43); eliminata previsione calcolata, sostituita da campo compilato dall'impresa (rev.44-48); corruzione ricorrente `Master.csv` diagnosticata/riparata più volte (rev.50,63-64 — v. §8.40); iterazioni barra % nei modal lotto/cluster (rev.49,51-59); colonna `DATA_UPDATE` (rev.60-62); fix storici `scavi.html` — sort/filtro, donut KPI, rebranding, keepalive Render, guardia bfcache (rev.10-33, ha scoperto il gap auth chiuso poi in rev.131/132).

_Note di compattazione: rev.65-113 compattato 2026-07-06 (rev.10-64 già archiviato in precedenza). rev.114-187 compattato 2026-07-21 con lo stesso criterio (dettagli originali recuperabili dai backup di sessione se serve un audit puntuale). Restano inline solo rev.188-198 (più recenti/actionable)._
