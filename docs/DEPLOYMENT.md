# Deployment

## Local, everything in one process

```bash
pip install -r requirements.txt && pip install --no-deps -e .
python scripts/seed_demo.py --n 12        # optional fictional data
streamlit run streamlit_app.py
```

SQLite at `./data/packetpilot.db`, processing in-process, deterministic extractor unless an API key is set.

## Local, full stack (Docker)

```bash
docker compose up --build
# UI http://localhost:8501   API http://localhost:8000/docs
```

PostgreSQL 16, Redis 7, the API, an RQ worker and the UI (talking to the API over HTTP). Put `OPENAI_API_KEY` or
`GEMINI_API_KEY` in your shell or a `.env` file next to `docker-compose.yml` to enable hybrid extraction.

## Render (API + worker + UI + PostgreSQL + Key Value)

`render.yaml` is a Blueprint:

1. Push the repository to GitHub.
2. In Render: **New → Blueprint**, pick the repo. It creates `packetpilot-api`, `packetpilot-worker`,
   `packetpilot-ui`, a PostgreSQL database and a Key Value (Redis-compatible) instance.
3. Set `OPENAI_API_KEY` on the API and worker if you want LLM extraction (marked `sync: false`, so Render asks).
4. `PACKETPILOT_API_KEY` is generated for the API and passed to the UI automatically.

Uploads are stored in PostgreSQL (`PACKETPILOT_STORAGE=db`), so the worker doesn't need a shared disk. Free plans
sleep when idle and free databases expire; check Render's current plan limits before relying on them. Review the
blueprint against Render's documentation before applying — field names occasionally change.

Load demo data into a deployed database from your machine:

```bash
DATABASE_URL=<external connection string> python scripts/seed_demo.py --n 12
```

## Streamlit Community Cloud (UI only)

Point the app at `streamlit_app.py`. Without `PACKETPILOT_API_URL` it runs in embedded mode with SQLite on the
app's ephemeral disk (data resets on restart) — fine for a public demo with fictional data. Add `packages.txt`
containing `tesseract-ocr` for OCR. To persist data, set `PACKETPILOT_API_URL` and `PACKETPILOT_API_KEY` in the
app's secrets to use a deployed API, or `DATABASE_URL` to use a hosted PostgreSQL directly.

## Configuration

All settings are environment variables; see [`.env.example`](../.env.example). The ones that matter in production:

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | SQLite file | `postgres://` and `postgresql://` URLs are accepted |
| `REDIS_URL` | unset | set to process uploads on RQ workers |
| `PACKETPILOT_API_KEY` | unset | require `X-API-Key` on `/api/*` |
| `PACKETPILOT_EXTRACTOR` | `auto` | `rules`, `llm`, `hybrid`, or `auto` (hybrid if a key is set) |
| `OPENAI_API_KEY` / `GEMINI_API_KEY` | unset | provider chosen by `PACKETPILOT_LLM_PROVIDER` |
| `PACKETPILOT_AUTO_VERIFY_THRESHOLD` | `0.90` | minimum confidence for bulk approval eligibility |
| `PACKETPILOT_SLA_HOURS` | `24` | dashboard SLA breach threshold |

## Before real client data

This is a prototype. Before any real submission goes through it: SSO in front of API and UI, TLS, encrypted
backups, a retention policy, access logging, a security review, and a data processing agreement (or zero-retention
endpoint) with the LLM provider. Rules-only mode keeps all document text inside your infrastructure.
