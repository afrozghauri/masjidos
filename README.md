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
