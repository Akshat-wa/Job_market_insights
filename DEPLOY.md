# Deploy Job Market Insights (Portfolio)

This guide gets you from **local code** → **live URL** recruiters can use.

**Stack (all have free tiers):**
- **Neon** — PostgreSQL
- **Render** — Flask API (Docker)
- **Cloudflare Pages** — static UI

---

## What was added in code

| File | Purpose |
|------|---------|
| `ingest_jobs_flexible.py` | Flexible CSV upload ingest (session-scoped jobs) |
| `api_server.py` | `/api/upload`, `/api/load-demo`, `/api/session/new`, `/api/query` |
| `schema_deploy.sql` | Session columns + bootstrap tables |
| `config_loader.py` | Reads `DATABASE_URL`, `GEMINI_API_KEY` from env |
| `session_plan.py` | Auto-filters job SQL by `session_id` |
| `Interface/` | Upload UI + session-aware queries |
| `demo/sample_combined.csv` | One-click demo data |

---

## Part A — Accounts to create (you do this)

### 1. Google AI Studio (Gemini API key)
1. Go to [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Create an API key
3. Save it as `GEMINI_API_KEY` (you'll paste into Render later)

### 2. Neon (PostgreSQL)
1. Sign up at [https://neon.tech](https://neon.tech)
2. Create project: e.g. `job-insights`
3. Create **two databases** in the same project:
   - `job_market` (jobs + skills)
   - `candidates` (demo resumes — optional for federated queries)
4. Copy connection strings (Connection details → **Pooled** URI)
   - `DATABASE_URL` → `job_market` DB
   - `CANDIDATES_DATABASE_URL` → `candidates` DB

> **If you only create one DB for now:** set both env vars to the same `job_market` URL. Job queries work; candidate queries need the candidates DB populated separately.

### 3. Render (API hosting)
1. Sign up at [https://render.com](https://render.com)
2. You'll create a **Web Service** from this repo (Part C)

### 4. Cloudflare Pages (frontend)
1. Sign up at [https://dash.cloudflare.com](https://dash.cloudflare.com)
2. You'll deploy the `Interface/` folder (Part D)

### 5. GitHub (if not already)
Push this project to a GitHub repo Render and Cloudflare can pull from.

```powershell
cd "C:\Users\aksha\Documents\IIA Project\JobMarketsInsight-main\JobMarketsInsight-main"
git init
git add .
git commit -m "Portfolio-ready Job Market Insights demo"
git branch -M main
git remote add origin https://github.com/YOUR_USER/job-market-insights.git
git push -u origin main
```

> Your Neon data is already seeded — production uses the **same** `DATABASE_URL` (no re-seed on Render).

---

## Part B — Local test (do this before deploying)

### Step 1: Python env
```powershell
cd "buisness logic"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Step 2: Environment file
```powershell
copy .env.example .env
```
Edit `.env`:
```
GEMINI_API_KEY=your_key_here
DATABASE_URL=postgresql://...@.../job_market?sslmode=require
CANDIDATES_DATABASE_URL=postgresql://...@.../candidates?sslmode=require
```

### Step 3: Seed portfolio demo (~100 MB, Path A — recommended)

**Storage budget on Neon free tier (512 MB total):**

| Slice | Target | Purpose |
|-------|--------|---------|
| Demo jobs (session `demo`) | ~55–65 MB in DB | ~250k synthetic jobs from 60 MB CSV |
| Demo candidates | ~25–35 MB in DB | All 4,817 resumes from `master_resumes.jsonl` (~16 MB file) |
| Upload headroom | ~150 MB reserved | Recruiter CSV uploads (capped at 5 MB / 5k rows) |
| Buffer | ~180 MB | Indexes, WAL, safety |

**One command seeds everything (no LinkedIn CSVs, no transformer model):**
```powershell
python seed_portfolio.py
```
This will:
1. Generate `data/portfolio_jobs_combined.csv` (~60 MB, cached after first run)
2. Ingest ~250k jobs into session `demo`
3. Light-ingest all resumes (~5–15 min, no BERT download)
4. Print storage stats + remaining headroom

**Options:**
```powershell
python seed_portfolio.py --jobs-mb 50          # smaller jobs slice
python seed_portfolio.py --skip-candidates   # jobs only (~60 MB total)
python seed_portfolio.py --dry-run           # preview plan only
```

**Check storage anytime:**
```powershell
python -c "from config_loader import load_config; from db import DBAdapter; from storage_stats import get_storage_stats; import json; print(json.dumps(get_storage_stats(DBAdapter(load_config())), indent=2))"
```

**Upload limits (auto-applied via `portfolio_budget.yaml`):**
- Max upload file: **5 MB**
- Max upload rows: **5,000**
- Old upload sessions deleted after **24 hours** (demo session kept)

### Step 3 (alternative): Full LinkedIn datasets

**Only if you have the large CSVs locally:**
```powershell
python jobs_db.py --config config.yaml
python candidates_db.py --config config.yaml   # downloads ~500MB transformer model
```
⚠️ This can exceed Neon free tier — use Path A for portfolio.

### Step 4: Run API locally
```powershell
python api_server.py
```
Open [http://127.0.0.1:8000/api/health](http://127.0.0.1:8000/api/health) — should return `"db": "ok"`.

### Step 5: Run UI locally
Open `Interface/index.html` in browser, or:
```powershell
cd ..\Interface
python -m http.server 5500
```
Visit [http://127.0.0.1:5500](http://127.0.0.1:5500)

1. Click **Use portfolio demo (~250k jobs)** (after `seed_portfolio.py`)
2. Or click **Load sample (8 jobs)** for a quick smoke test
3. Ask: `Top 10 skills for data scientist in India`
4. Upload your own CSV (stays in your session; demo data untouched)

---

## Part C — Deploy API to Render

### Step 1: New Web Service
- Render Dashboard → **New +** → **Web Service**
- Connect your GitHub repo
- **Root directory:** leave empty (repo root) — `Dockerfile` at repo root copies `buisness logic/`
- **Runtime:** Docker
- **Instance type:** Free
- **Region:** Singapore (match your Neon region)

### Step 2: Environment variables (Render → Environment)

| Key | Value |
|-----|-------|
| `GEMINI_API_KEY` | your Gemini key |
| `XAI_API_KEY` | (optional) Grok fallback key |
| `DATABASE_URL` | Neon `job_market` pooled URI |
| `CANDIDATES_DATABASE_URL` | Neon `candidates` pooled URI |
| `LLM_LIVE` | `1` to enable LLM; `0` for SQL-only (saves quota) |
| `PORT` | `8000` (Render may set automatically) |
| `CONFIG_PATH` | `config.yaml` |

### Step 3: Deploy
- Click **Deploy**
- Wait for build (~3–5 min first time)
- Note your URL: `https://job-insights-api.onrender.com` (example)

### Step 4: Verify
```text
GET https://YOUR-RENDER-URL.onrender.com/api/health
```
First request after idle may take **30–60s** (free tier cold start).

### Step 5: Load demo data on production
```powershell
curl -X POST https://YOUR-RENDER-URL.onrender.com/api/load-demo `
  -H "Content-Type: application/json" `
  -d "{\"session_id\": \"demo\"}"
```

---

## Part D — Deploy UI to Cloudflare Pages

### Step 1: Create project
- Cloudflare → **Workers & Pages** → **Create** → **Pages** → **Connect to Git**
- Repo: your project
- **Build output directory:** `Interface`
- **Build command:** (leave empty — static site)

### Step 2: Set API URL
Edit `Interface/config.js` before deploy:

```javascript
window.API_BASE = "https://YOUR-RENDER-URL.onrender.com";
```

(`index.html` loads `config.js` before `index.js`.)

### Step 3: Deploy
- Save → Pages builds → URL: `https://job-insights.pages.dev`

### Step 4: CORS
`api_server.py` already enables CORS. If you restrict origins later, add your Pages URL.

---

## Part E — CSV upload formats (for recruiters)

### Combined file (easiest)
```csv
job_link,job_title,company,job_location,job_skills
https://...,Data Scientist,Acme,Bangalore India,"python, sql, ml"
```

### Two files
**postings.csv:** `job_link`, `job_title`, `company`, `job_location`  
**skills.csv:** `job_link`, `job_skills`

### Flexible columns
Aliases auto-detected: `title`, `url`, `skills`, `company_name`, etc.  
Missing `job_link` → synthetic key from title+company+location.

### Limits (free tier)
- Max file size: **10 MB**
- Max rows: **20,000** per upload

---

## Part F — Portfolio integration

On your portfolio subpage, link:
- **Live demo:** `https://job-insights.pages.dev`
- **GitHub:** repo URL
- **One-liner:** *Upload job CSVs, ask hiring questions in plain English, powered by federated SQL + Gemini.*

Screenshot checklist:
1. Upload panel with session stats
2. Query + summary result
3. Table output

---

## Troubleshooting (where I can't automate)

| Problem | Fix |
|---------|-----|
| `db: error` on health | Check `DATABASE_URL` in Render; Neon IP allowlist is open by default |
| `Dockerfile: no such file` | Use repo-root `Dockerfile` (push latest) or set Root Directory to `buisness logic` |
| `GEMINI_API_KEY not set` | Add env var in Render, redeploy |
| UI can't reach API | Set `window.API_BASE` to Render URL; check browser console |
| Cold start timeout | Retry after 60s; upgrade Render plan or add uptime ping (cron-job.org free) |
| Upload fails `session_id column` | API runs `schema_deploy.sql` on startup — redeploy once |
| Candidate queries empty | Run `candidates_db.py` against Neon `candidates` DB |
| `psycopg2` SSL error | Ensure `?sslmode=require` on Neon URI |

---

## Optional: keep API warm (free)

Use [https://cron-job.org](https://cron-job.org) to ping every 10 minutes:
```
GET https://YOUR-RENDER-URL.onrender.com/api/health
```

---

## Next improvements (after v1 live)

1. Pre-seed `demo` session on Render startup
2. Candidate CSV upload (lighter than full NER pipeline)
3. Auth / rate limits per IP
4. Custom domain on Cloudflare

---

## Quick command reference

```powershell
# Local API
cd "buisness logic"
python api_server.py

# Docker local test
docker build -t job-insights-api .
docker run -p 8000:8000 --env-file .env job-insights-api
```

When you're ready for Part C, share your Render URL if health check fails and we can debug together.
