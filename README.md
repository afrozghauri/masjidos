# MasjidOS — Agentic Timetable Comprehension & Publishing System

An MCP-based agentic system that reads masjid prayer timetables from arbitrary,
never-before-seen sources (HTML, PDF, image, graphic-laden PDF), understands them
with a vision-language model, self-corrects, decides on its own when it is too
uncertain to auto-publish, and uploads the result to the Masjidal portal.

## What is AI here (and what is not)

**AI / agentic:** the comprehension of unbounded, unstandardized timetable
layouts, the tool-selection reasoning loop, the self-correction pass, and the
confidence-based decision of *when to escalate to a human*.

**Not AI (deterministic automation, and labelled as such):** portal login,
navigation, CSV generation, CSV upload.

## Architecture

A LangGraph ReAct agent acts as an **MCP client** against three MCP servers:

- **Acquisition** — fetch the source (HTML / PDF / image) given a masjid URL.
- **Comprehension** — the AI core: a VLM reads the timetable, infers the labeling
  scheme, maps to the canonical schema, returns per-field confidence; plus a
  deterministic consistency validator.
- **Publishing** — generate Salah/Iqamah CSVs and upload via Playwright.

Low-confidence or validation-failing cases are routed to a **Streamlit review gate**.

## The only inputs

1. A spreadsheet (`.xlsx`/`.csv`) of masjid **names + website URLs**.
2. Portal **login credentials**.

Both are supplied via `.env` / the directory file — see `.env.example`.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env         # then edit .env with your keys + portal creds
# put your masjid directory at data/masjids.xlsx (columns: name, url)

# Run the whole thing:
uvicorn app.api:app --reload --port 8000     # terminal 1: backend + agent
streamlit run app/review_app.py              # terminal 2: review gate UI
```

Or with Docker:

```bash
docker compose up --build
```

## Repo layout

```
masjidos/
├── config/settings.py          # env + paths (pydantic-settings)
├── mcp_servers/
│   ├── acquisition_server.py    # MCP: fetch_html / find_pdf / render_image / read_directory
│   ├── comprehension_server.py  # MCP: vlm_read_timetable / validate_extraction  (AI core)
│   └── publishing_server.py     # MCP: generate_*_csv / portal_upload
├── agent/
│   ├── schema.py                # canonical Salah/Iqamah data models
│   ├── graph.py                 # LangGraph ReAct loop + self-correction + escalation
│   └── run.py                    # process one masjid or the whole directory
├── app/
│   ├── api.py                    # FastAPI: kick off runs, serve review queue
│   └── review_app.py             # Streamlit human review gate + agent trace
├── data/masjids.xlsx            # INPUT: masjid names + urls
└── tests/test_normalization.py
```
