# AI Transaction Processing Pipeline

A FastAPI + Celery service that ingests a transactions CSV, cleans and
de-duplicates it, flags anomalies, classifies missing categories with an LLM
(OpenRouter, with an offline fallback), and produces a narrative summary — all
stored in PostgreSQL. Comes with a small web UI to upload a file and view the
results.

## Run

Backend and frontend start together with one command:

```bash
docker compose up --build
```

- **Frontend (web UI):** http://localhost:8000/
- **Backend (API docs):** http://localhost:8000/docs

Tables are created automatically on startup — no migrations needed.

### Optional LLM (OpenRouter)

Works offline by default. To enable live LLM calls, add a free OpenRouter key:

```bash
cp .env.example .env.local   # add OPENROUTER_API_KEY in .env.local
docker compose up --build
```

Both `.env` and `.env.local` are supported and ignored by Git. If both exist,
`.env.local` takes precedence.
