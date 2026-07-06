# MasjidOS — Agentic Timetable Comprehension & Publishing System

An MCP-based agentic system that reads masjid prayer timetables from arbitrary,
never-before-seen sources (HTML, PDF, image, graphic-laden PDF, JS-rendered
pages), understands them with a vision-language model, self-corrects, decides on
its own when it is too uncertain to auto-publish, and generates the Salah/Iqamah
CSVs the Masjidal portal expects.

## What is AI here (and what is not)

**AI / agentic:** the comprehension of unbounded, unstandardized timetable
layouts, the tool-selection reasoning loop, the self-correction pass, and the
confidence-based decision of *when to escalate to a human*.

**Not AI (deterministic automation, and labelled as such):** portal login,
navigation, CSV generation, CSV upload.

## Architecture

A LangGraph ReAct agent acts as an **MCP client** against three MCP servers:

- **Acquisition** — fetch the source given a masjid URL: static HTML, a headless-
  browser render for JS-driven pages, PDF text/image extraction, or a raw image.
- **Comprehension** — the AI core: a VLM reads a full calendar (typically a
  month) of Salah/Iqamah times, infers the labeling scheme, maps it to the
  canonical schema, and returns PER-COLUMN confidence (not per-cell — a month's
  Fajr column is either well-mapped or it isn't). A deterministic grounding check
  then verifies every claimed value literally appears in the source text, so the
  VLM's own stated confidence can't paper over a hallucinated value.
- **Publishing** — generate Salah/Iqamah CSVs (one row per date, Jumuah repeated
  since it's a fixed weekly time) and upload via Playwright.

Low-confidence or validation-failing cases are routed to a **Streamlit review gate**;
high-confidence ones are published immediately, no human click required.

## The only inputs

1. A spreadsheet (`.xlsx`/`.csv`) of masjid **names + website URLs**.
2. Portal **login credentials**.

Both are supplied via `.env` / the directory file — see `.env.example`.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env         # then edit .env with your keys + portal creds + API_KEY
# put your masjid directory at data/masjids.xlsx (columns: name, url)

# Run the whole thing:
uvicorn app.api:app --reload --port 8000     # terminal 1: backend + agent
streamlit run app/review_app.py              # terminal 2: review gate UI
```

Or with Docker:

```bash
docker compose up --build
```

## Deploy to Render (real live app link)

`render.yaml` at the repo root is a Render Blueprint that provisions everything
needed: the backend API, the Streamlit review UI, and a shared Postgres
database (both services need to see the same review queue, which a plain
SQLite file can't do across two separate containers).

1. Push this repo to GitHub (already done if you're reading this from there).
2. In the [Render dashboard](https://dashboard.render.com), **New +** ->
   **Blueprint**, and point it at the repo. Render reads `render.yaml` and
   provisions the database + both services automatically.
3. Render will prompt for the secrets marked `sync: false` in `render.yaml`
   (`OPENAI_API_KEY`, `API_KEY`, `PORTAL_EMAIL`, `PORTAL_PASSWORD`, and
   `LANGCHAIN_API_KEY` if you want tracing) — fill these in via its dashboard,
   never in the repo.
4. Once deployed, you get real `https://masjidos-backend-xxxx.onrender.com`
   and `https://masjidos-review-xxxx.onrender.com` links.

Notes specific to this app:
- `PORTAL_HEADLESS` must stay `true` in `render.yaml` — there's no display on
  a server. Leave `PORTAL_UPLOAD_ENABLED=false` until you've watched it
  succeed locally first.
- Playwright launches a real headless Chromium during portal uploads — if the
  smallest paid instance type OOMs, size the `masjidos-backend` service up.
- `data/outputs/` (generated CSVs) and `data/logs/` are NOT persisted across
  restarts/redeploys in this config (no disk attached) — the review queue
  itself is safe (it's in Postgres), but generated CSV files are ephemeral.
  Add a Render disk to `masjidos-backend` if you need those to survive.
- This Blueprint hasn't been verified against a live Render deploy from this
  environment (no Render access here) — if the dashboard flags a schema field
  during sync, the same setting can usually be entered manually for that
  service (e.g. its "Start Command").

### Security

Every `app/api.py` route except `/health` requires an `X-API-Key` header matching
`API_KEY` in `.env`. Generate one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8000/review/queue
```

CORS origins are controlled by `CORS_ALLOW_ORIGINS` (comma-separated; `*` for
local/demo use — restrict this in a real deployment).

### Observability

- Structured logs (`loguru`) go to console **and** a rotating file at
  `data/logs/masjidos.log` (10 MB rotation, 14-day retention).
- Every run's step-by-step tool-call trace is stored per-item in the database and
  viewable in the Streamlit review gate ("Agent reasoning trace").
- Optional free tracing of the LangGraph agent's full reasoning loop via
  [LangSmith](https://smith.langchain.com): set `LANGCHAIN_TRACING_V2=true` and
  `LANGCHAIN_API_KEY` in `.env`.

## Repo layout

```
masjidos/
├── config/settings.py          # env + paths + logging/tracing bootstrap (pydantic-settings)
├── mcp_servers/
│   ├── acquisition_server.py    # MCP: fetch_html / fetch_rendered_html / find_pdf / render_image / read_directory
│   ├── comprehension_server.py  # MCP: vlm_read_timetable / validate_extraction  (AI core)
│   └── publishing_server.py     # MCP: generate_*_csv / portal_upload
├── agent/
│   ├── schema.py                # canonical multi-date Salah/Iqamah data models
│   ├── graph.py                 # LangGraph ReAct loop + self-correction + escalation
│   └── run.py                    # process one masjid or the whole directory
├── app/
│   ├── api.py                    # FastAPI: kick off runs, serve review queue (API-key protected)
│   └── review_app.py             # Streamlit human review gate + agent trace
├── data/masjids.xlsx            # INPUT: masjid names + urls
└── tests/test_normalization.py
```

## Known limitations

- **Portal upload is implemented against the real DOM but not yet live-tested
  end-to-end from an automated run.** `portal_upload()` in
  `mcp_servers/publishing_server.py` logs into the backend admin, finds the
  masjid in Mosque Manager, follows "New UI Login" into the mosque-facing
  portal, and uploads both CSVs via their upload modals — all URLs, form field
  names, and button labels were confirmed against the live site. Two details
  are inference rather than confirmed-from-HTML (documented in the function's
  docstring): which of two same-labeled buttons is the modal's actual submit
  action, and exact-match assumptions when searching Mosque Manager by name.
  Test with `PORTAL_HEADLESS=false` first, ideally against a non-critical
  masjid, before trusting it broadly. `PORTAL_UPLOAD_ENABLED=false` keeps this
  off by default; CSVs are still generated correctly either way.
- CSV column headers/format were matched exactly against Masjidal's own
  downloadable sample templates (`Masjidal_Salah_CSV_Template`,
  `Masjidal_Iqama_CSV_Template`), not guessed.
- Batch/spreadsheet mode (`process_directory`) and the Google Sheets integration
  are implemented but lightly tested compared to the single-masjid path.
- The grounding check is substring-based: it catches outright fabricated values,
  but not a fabricated value that happens to coincide with a real value for a
  *different* field elsewhere in the source.
