
Action: file_editor create /app/DEPLOY.md --file-text "# Deploy guide — ENRI Dashboard

Frontend statico → **GitHub Pages** (gratis, già attivo su `https://enri-rds.github.io/dashboard/`).
Backend FastAPI → **Render** (free tier).
Database → **MongoDB Atlas** (free M0 cluster, 512 MB).

---

## 1) MongoDB Atlas — 5 minuti

1. Vai su https://www.mongodb.com/cloud/atlas/register e crea un account.
2. Crea un cluster **M0 (Free)** nella region più vicina (es. AWS / Frankfurt).
3. Quando ti chiede *“Where would you like to connect from?”* clicca **Allow access from anywhere** (`0.0.0.0/0`). Su Render gli IP cambiano, è la scelta giusta.
4. Crea un **Database User** (Database Access → Add new user). Salva username e password.
5. Vai su **Connect → Drivers → Python**, copia la connection string. Sarà tipo:

   ```
   mongodb+srv://USER:PASSWORD@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
   ```

   Sostituisci `USER` e `PASSWORD` con quelli del passo 4. Tienila pronta per il prossimo passo.

---

## 2) Render — Deploy del backend

### Modalità A — con `render.yaml` (consigliato)

Il file `render.yaml` è già in repo (root del progetto).

1. Pusha il codice nel repo `ENRI-RDS/dashboard` su GitHub (se non lo è già).
2. Vai su https://dashboard.render.com → **New +** → **Blueprint**.
3. Connetti il tuo account GitHub e seleziona il repo `dashboard`.
4. Render legge `render.yaml` e propone il servizio `enri-dashboard-api`. Clicca **Apply**.
5. Render ti chiederà di settare i **valori dei secret** (perché in `render.yaml` sono marcati `sync: false`):
   - `MONGO_URL` → la connection string di Atlas dal passo 1.
   - `UPLOAD_TOKEN` → genera una stringa lunga e casuale (es. `openssl rand -hex 32`).
6. Conferma. Render fa il build (~3–5 min) e ti dà un URL del tipo:

   ```
   https://enri-dashboard-api.onrender.com
   ```

### Modalità B — manuale (se non vuoi usare il blueprint)

1. Render → **New +** → **Web Service** → collega il repo.
2. Settings:
   - **Root Directory**: `backend`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn server:app --host 0.0.0.0 --port $PORT`
   - **Runtime**: Python 3
3. Environment variables (in **Settings → Environment**):

   | Key | Value |
   |---|---|
   | `MONGO_URL` | la tua connection string Atlas |
   | `DB_NAME` | `enri_dashboard` |
   | `ALLOWED_ORIGINS` | `https://enri-rds.github.io` |
   | `UPLOAD_TOKEN` | stringa casuale (consigliato) |
   | `MAX_UPLOAD_MB` | `25` |
   | `PYTHON_VERSION` | `3.11.9` |

4. Deploy → aspetta il green check → copia l'URL pubblico.

### Sanity check (dal terminale)

```bash
curl https://enri-dashboard-api.onrender.com/api/
# → {\"service\":\"enri-dashboard-api\",\"status\":\"ok\",...}

curl https://enri-dashboard-api.onrender.com/api/health
# → {\"ok\":true,\"mongo\":true,...}
```

⚠️ Il free tier di Render **spegne il servizio dopo 15 minuti di inattività**. La prima chiamata dopo lo spegnimento può metterci ~30 secondi. Per un uso più serio passa al piano **Starter ($7/mese)**.

---

## 3) Collegare il frontend (GitHub Pages) al backend

Il frontend resta dov'è. Devi solo dirgli dove sta il backend.

### Opzione 1 — Configurazione runtime (più semplice, nessun re-deploy)

Apri il sito una volta nel browser e in console (`F12`):

```js
localStorage.setItem('enri_api_base', 'https://enri-dashboard-api.onrender.com');
location.reload();
```

Da ora in poi tutte le `fetch('Master.csv')`, `fetch('QGIS.geojson')` ecc. delle pagine HTML verranno automaticamente reindirizzate sul backend (vedi `js/api-config.js`).

Per tornare al comportamento statico:

```js
localStorage.removeItem('enri_api_base');
```

### Opzione 2 — Hardcoded nel codice

Edita le pagine HTML che caricano dati e prima di tutto includi:

```html
<script>window.ENRI_API_BASE = 'https://enri-dashboard-api.onrender.com';</script>
<script src=\"js/api-config.js\"></script>
```

### Pagina admin

Apri `https://enri-rds.github.io/dashboard/admin.html`:

1. Incolla l'URL del backend Render nel campo **API endpoint**
2. Incolla l'**Upload token** (se configurato)
3. Trascina il tuo Excel/CSV/GeoJSON, scegli progetto e nome di destinazione, **Carica**
4. Excel viene convertito in CSV automaticamente, dati salvati su MongoDB + disco.

---

## 4) Sviluppo locale

```bash
cd backend
cp .env.example .env       # poi modifica .env con MONGO_URL locale o Atlas
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn server:app --reload --port 8001
```

Per servire il frontend statico in locale:

```bash
cd ..                       # nella root del repo
python -m http.server 5500
# apri http://localhost:5500/hub.html
```

In console del browser:
```js
localStorage.setItem('enri_api_base', 'http://localhost:8001');
```

---

## 5) Endpoint API disponibili

| Metodo | Endpoint | Descrizione |
|---|---|---|
| GET | `/api/` | health-check base |
| GET | `/api/health` | check MongoDB |
| GET | `/api/files` | elenca tutti i CSV/GeoJSON disponibili |
| GET | `/api/data/{filename}` | scarica un file (es. `Master.csv`, `M/QGIS_3.geojson`) |
| GET | `/api/data-text/{filename}` | stessa cosa ma come testo plain |
| GET | `/api/uploads?limit=50&project=M` | storico upload |
| POST | `/api/upload` | upload Excel/CSV/GeoJSON (multipart form-data) |
| GET | `/api/datasets` | ultima versione di ogni dataset |
| DELETE | `/api/uploads/{id}` | elimina record (richiede token) |

Il campo `target` in upload ti permette di sovrascrivere file specifici, es. `Master.csv` o `M/QGIS_3.geojson`.

---

## 6) Cosa NON è incluso (per ora)

- ❌ Auth utenti per impresa (solo `UPLOAD_TOKEN` condiviso). Se vuoi più imprese, dimmelo.
- ❌ Generazione automatica dei GeoJSON dai CSV uploadati (oggi devi caricarli entrambi).
- ❌ Commit automatico su GitHub dal backend (il backend tiene la versione \"live\", GitHub Pages resta la copia statica). Posso aggiungerlo con GitHub API se serve.
- ❌ AI alerts (`ai_alerts.html` resta statico). Posso integrarci Claude/GPT-5.2 se decidi di farlo.
"
Observation: Create successful: /app/DEPLOY.md
