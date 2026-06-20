# AI Transaction Processing Pipeline

Small FastAPI + Celery project for the backend internship assignment.

## Run

```bash
docker compose up --build
```

API docs: http://localhost:8000/docs

The app creates database tables on startup, so there are no manual migrations.

## Optional Gemini setup

The code uses Gemini when `GEMINI_API_KEY` is present:

```bash
cp .env.example .env
# add your key in .env
docker compose up --build
```

Without a key, it uses a deterministic local fallback so the project still runs with one command.

## Endpoints

Upload the provided CSV:

```bash
curl -F "file=@transactions.csv" http://localhost:8000/jobs/upload
```

Check status:

```bash
curl http://localhost:8000/jobs/<job_id>/status
```

Fetch results:

```bash
curl http://localhost:8000/jobs/<job_id>/results
```

List jobs:

```bash
curl http://localhost:8000/jobs
curl http://localhost:8000/jobs?status=completed
```

## What the worker does

1. Cleans dates, amounts, statuses, blank categories, and duplicate rows.
2. Flags account-level outliers and domestic-only brands charged in USD.
3. Batches missing-category classification through Gemini or the local fallback.
4. Asks Gemini for a JSON narrative summary, with retry and fallback behavior.
5. Stores cleaned transactions, anomalies, category totals, and summary JSON in PostgreSQL.

## Tests

```bash
pytest
```
