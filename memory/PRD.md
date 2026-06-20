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
            ▲                              │      │
            │                              │      └── DATA_DIR (file del repo) come SEED FALLBACK
            │                              │
            └──────── push automatico ─────┘  (Master.csv, QGIS.geojson, Riepilogo_progettazione.csv)
            │
            └── js/api-config.js intercetta fetch('Master.csv') → fetch(API/api/data/Master.csv)
```

**Modello di storage**: MongoDB GridFS è autoritativo. I file nel repo restano come seed iniziale; servono il fallback se nessun upload esiste. Dopo ogni approvazione/eliminazione/ripristino, il backend pusha anche su GitHub via API (token fine-grained, scope `Contents: Read and write`) così la copia statica del repo non va più in deriva da quella servita live.

**Pattern di lettura frontend (stale-while-revalidate)**: `index.html` e `mappa.html` fanno fetch in parallelo del file statico GitHub (istantaneo, sempre disponibile) e dell'endpoint Render (`/api/data/...`, può essere lento al cold-start). Il dato statico viene mostrato subito; se Render risponde con un contenuto diverso, la UI si aggiorna live senza reload.

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

## Implementato (20/06/2026)

### Push automatico su GitHub (chiude il backlog P2 "auto-commit")
- `_push_to_github(file_bytes, path, label)`: commit via API GitHub (GET sha → PUT contents), fire-and-forget, non blocca la risposta HTTP
- `GITHUB_PATHS`: mappa filename → path nel repo (override via env `GITHUB_RIEPILOGO_PATH`, `GITHUB_QGIS_PATH`, `GITHUB_CSV_PATH` se servono sottocartelle)
- Trigger: approvazione submission, eliminazione versione, ripristino versione (tutti e tre chiamano `_push_current_master_to_github` o passano dal flusso `_write_master_csv`)
- Richiede `GITHUB_TOKEN` su Render (fine-grained PAT, repo `ENRI-RDS/dashboard`, permesso `Contents: Read and write`); se assente il push viene saltato e loggato, MongoDB resta comunque autoritativo

### Rigenerazione automatica dei file derivati
Scoperta chiave: **`Riepilogo_progettazione.csv` è esattamente la tabella attributi di `QGIS.geojson` esportata in CSV** — stesse 21 colonne, stesso ordine, stessi valori (verificato riga per riga sui file reali). Architettura semplificata a singola fonte:

```
Master.csv (dinamico, aggiornato dalle approvazioni)
       │
       ▼
_compute_tratta_summary()  — per ogni TRATTA_ID:
   • STATO_AUTORIZZAZIONE ← ultima riga AUTORIZZAZIONE
   • STATO_NULLAOSTA / STATO_ORDINANZA ← peggiore tra l'ultimo stato di ciascun ente
     (se la tratta ha più nulla osta di enti diversi)
   • LAVORABILE ← SI solo se AUTORIZZAZIONE=OTTENUTO e, se richiesti,
     anche NULLA OSTA/ORDINANZA=OTTENUTO (flag "NULLA OSTA NECESSARIO"/"ORDINANZA NECESSARIA")
   • PRATICA ← codici completi "AUT/24/1A | NO/22/1A | ..." (AUT prima, poi NO, poi ORD)
       │
       ▼
PATCH UNICA sulle properties di QGIS.geojson (geometria invariata)
       │
       ▼
Riepilogo_progettazione.csv = derivato direttamente dalle stesse properties patchate
```

Campi **mai derivati automaticamente** (nessuna regola certa dai dati disponibili, o provenienti da sistemi esterni): `CAMPO AWS`, `PROTOCOLLO_AUT`, `ENTE 2`, `fid`, `TIPOLOGIA`, `PROVINCIA`, `ROUTE`, `SPAN` — restano quelli già presenti in QGIS.geojson.

`dati.csv` (estratto Power Query LOTTO×STATO×Metri/Percentuale): confermato **non necessario** — `index.html` calcola la stessa aggregazione lato client da `Riepilogo_progettazione.csv` già caricato (`loadData()`, variabili `aggMap`/`totMap`).

### Fix bug — caricamento dati
- **Separatore CSV auto-rilevato**: `Master.csv` può essere tab o `;`; lettura/scrittura ora rilevano e preservano il separatore reale invece di assumere `;` fisso (causava colonne unite o file illeggibile su GitHub)
- **Inserimento riga in posizione corretta**: l'approvazione di un aggiornamento ora inserisce la nuova riga subito dopo l'ultima riga della stessa tratta+ente+tipo+pratica (non più in fondo al file) — mantiene vicine le righe dello stesso iter di approvazione
- **Live-refresh senza reload**: il callback SWR di `Master.csv` in `index.html` ridisegna subito tabella "Tutte le Pratiche" e pannello SED quando arrivano dati più freschi da Render (prima aggiornava solo la cache silenziosamente)
- **AbortController → Promise.race**: l'anteprima Claude non supporta la clonazione di `AbortSignal` via `postMessage`; i timeout sui fetch usano ora `Promise.race` ovunque (compatibile sia in anteprima che in produzione)

### `imprese.html`
- Campo **Ente**: select popolato dinamicamente dagli enti già presenti nelle pratiche del lotto (+ opzione "Altro" con campo libero) invece di testo libero
- Campo **Pratica** (nuova tratta): solo il numero progressivo è editabile — prefisso (AUT/NO) e suffisso (lotto) calcolati automaticamente con anteprima live del codice completo; tipo permesso "ORDINANZA" rimosso dalle opzioni (gestito come flag separato)
- Campo **Pratica** (aggiorna pratiche): reso **solo lettura** — la modifica del codice pratica richiede contatto diretto con l'amministratore
- **Tab "Le mie submission"**: righe cliccabili → modale di dettaglio con tipo, stato (badge colorato), modifiche per tratta, note di revisione in evidenza se rifiutata
- **Tratte collegate alla stessa pratica**: alla selezione di una tratta, se altre tratte condividono ENTE+TIPO_PERMESSO+PRATICA, il sistema propone di aggiornarle tutte insieme (default); se si procede con una sola, richiede conferma esplicita con avviso che lo stato complessivo della pratica resterà invariato

### `admin.html`
- Tutti i dialog nativi del browser (`alert`/`confirm`/`prompt`) sostituiti con modali HTML coerenti con il resto della UI: modale dettagli submission (JSON leggibile invece di `alert(JSON.stringify(...))`), modale conferma generico, modale rifiuto con campo nota
- Date in tabella: formato `gg/mm/aa, hh:mm` (anno a 2 cifre, senza secondi)
- Righe "Coda imprese" colorate per tipo: bordo verde + badge "＋ Nuova tratta" / bordo blu + badge "↻ Aggiornamento"

### `hub.html`
- Fix flash-of-unauthorized-content: tutte le card restavano nascoste finché il backend non conferma il ruolo/assegnazione, invece di mostrare tutto per una frazione di secondo prima del check
- Gestione cold-start Render: timeout 10s sul check `/api/imprese/me`, poi pannello "Server non raggiungibile" con bottone **Riprova** invece di spinner infinito

### `mappa.html`
- Pattern SWR per tutti i file caricati (`QGIS.geojson`, `QTS.geojson`, `SED_classificato.geojson`, `Master.csv`): GitHub statico per il caricamento immediato, Render in background per i dati aggiornati, con ricostruzione live dei layer se i dati cambiano
- Fix bug: `zoomToFeature` non si fermava dopo aver trovato il layer (O(n) inutile su tutti i layer); race condition in `loadSED` se chiamata due volte in rapida successione
- `requirements.txt`: aggiunto `httpx` (necessario per le chiamate all'API GitHub)

## Modello dati Mongo (oggi)

```jsonc
// uploads (storico versioni file)
// source: "impresa" (Master.csv da approvazione) | "derived" (QGIS.geojson/Riepilogo
//          rigenerati automaticamente) | assente per upload manuali da admin.html
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
7. Backend legge Master.csv corrente, applica `changes` con pandas, crea nuova versione GridFS, **rigenera QGIS.geojson + Riepilogo_progettazione.csv** (stessa logica di calcolo stato), pusha tutti e tre su GitHub
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
- P3 — Webhook su nuovo upload
- P3 — Verificare se i campi `MOTIVO_NO` (caso ordinanza) e `CAMPO AWS` hanno una regola derivabile, o se restano manuali per sempre

_Ultimo aggiornamento: 2026-06-20_
