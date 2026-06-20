# Architecture & Code Structure

> AI Transaction Processing Pipeline — a small FastAPI + Celery service that
> ingests a transaction CSV, cleans and de-duplicates it, flags anomalies,
> classifies missing categories with an LLM (Google Gemini, with a deterministic
> local fallback), produces a narrative summary, and persists everything to
> PostgreSQL.

This document explains **what the project does, how it is structured, and how the
pieces fit together**. For setup/run instructions see [README.md](README.md).

---

## 1. What it does (in one paragraph)

A client uploads a CSV of financial transactions. The HTTP API validates the
file, registers a **Job**, and hands the heavy work to a background **Celery
worker**. The worker runs a multi-stage pipeline: parse & normalise rows,
remove duplicates, flag suspicious transactions, ask an LLM to fill in missing
spend categories, compute spend aggregates, and ask the LLM for a JSON narrative
summary. The cleaned transactions, anomalies, per-category totals, and summary
are stored in PostgreSQL and exposed through status/results endpoints. The job
runs asynchronously, so uploads return immediately with a `job_id` the client
polls for completion.

---

## 2. Technology stack

| Concern | Choice |
|---|---|
| HTTP API | **FastAPI** (`uvicorn` ASGI server) |
| Async work queue | **Celery 5** |
| Message broker + result backend | **Redis** |
| Database | **PostgreSQL 16** via **SQLAlchemy 2.0** ORM (`psycopg2`) |
| Validation / serialization | **Pydantic v2** (`from_attributes`) |
| LLM | **Google Gemini** REST API (`generativelanguage.googleapis.com`) via `requests`, with a local heuristic fallback |
| Packaging / orchestration | **Docker** + **Docker Compose** |
| Tests | **pytest** |
| Runtime | **Python 3.12** |

There is **no AI/Gemini SDK dependency** — the Gemini call is a plain HTTPS POST
made with `requests`, which keeps dependencies minimal and the fallback path
fully offline.

---

## 3. High-level architecture

```
                       ┌──────────────────────────────────────────────┐
                       │                Docker Compose                 │
                       │                                               │
   CSV upload          │   ┌───────────┐   send_task    ┌──────────┐   │
  ───────────────────► │   │   api     │ ─────────────► │  redis   │   │
   GET status/results  │   │ (FastAPI) │   (broker)     │ (broker/ │   │
  ◄─────────────────── │   │           │ ◄───────────── │  backend)│   │
                       │   └─────┬─────┘                 └────┬─────┘   │
                       │         │ read/write                 │ consume │
                       │         ▼                            ▼         │
                       │   ┌───────────┐                ┌──────────┐    │
                       │   │ postgres  │ ◄───write────  │  worker  │    │
                       │   │           │   results      │ (Celery) │    │
                       │   └───────────┘                └────┬─────┘    │
                       │                                     │ HTTPS    │
                       └─────────────────────────────────────┼─────────┘
                                                             ▼
                                                   ┌──────────────────┐
                                                   │  Google Gemini   │ (optional;
                                                   │  REST API        │  falls back
                                                   └──────────────────┘  to local heuristics)
```

Four containers (see [docker-compose.yml](docker-compose.yml)):

- **api** — FastAPI app (`uvicorn ... --reload`). Accepts uploads, dispatches
  Celery tasks, serves status/results. Stateless.
- **worker** — Celery worker running the processing pipeline. Talks to Gemini
  and writes results to Postgres.
- **postgres** — PostgreSQL 16 (durable storage; `postgres_data` named volume).
- **redis** — Celery broker **and** result backend.

The `api` and `worker` containers are built from the **same image** and share the
same code (`./app` is bind-mounted into both), so the only difference is the
launch command.

---

## 4. End-to-end request lifecycle

```
Client                 api (FastAPI)              redis        worker (Celery)            postgres
  │                        │                        │               │                       │
  │ POST /jobs/upload      │                        │               │                       │
  │ (multipart CSV)        │                        │               │                       │
  ├───────────────────────►│                        │               │                       │
  │                        │ validate .csv          │               │                       │
  │                        │ save file to UPLOAD_DIR │               │                       │
  │                        │ read_csv() -> validate  │               │                       │
  │                        │   columns + count rows  │               │                       │
  │                        │ INSERT Job(status=      │               │                       │
  │                        │   "pending")  ──────────┼───────────────┼──────────────────────►│
  │                        │ send_task(process_job)  │               │                       │
  │                        ├────────────────────────►│               │                       │
  │ 202 {job_id,"pending"} │                        │  deliver task │                       │
  │◄───────────────────────┤                        ├──────────────►│                       │
  │                        │                        │               │ init_db()             │
  │                        │                        │               │ Job.status=processing─►│
  │                        │                        │               │ run_pipeline(file)    │
  │                        │                        │               │   clean/dedupe        │
  │                        │                        │               │   anomalies           │
  │                        │                        │  (Gemini call) │   classify (LLM)      │
  │                        │                        │               │   summary  (LLM)      │
  │                        │                        │               │ save_result() ───────►│
  │                        │                        │               │ Job.status=completed ─►│
  │ GET /jobs/{id}/status  │                        │               │                       │
  ├───────────────────────►│ SELECT Job ────────────┼───────────────┼──────────────────────►│
  │ {status, summary?}     │                        │               │                       │
  │◄───────────────────────┤                        │               │                       │
  │ GET /jobs/{id}/results │                        │               │                       │
  ├───────────────────────►│ (409 unless completed) │               │                       │
  │ {transactions, ...}    │                        │               │                       │
  │◄───────────────────────┤                        │               │                       │
```

Key property: **the upload endpoint never runs the pipeline**. It only validates
and enqueues, so it returns in milliseconds with HTTP `202 Accepted`. All heavy
lifting (and any Gemini latency) happens in the worker.

---

## 5. Directory & file map

```
Backend_DevOps_Assignment/
├── app/                      # application package
│   ├── __init__.py           # empty package marker
│   ├── main.py               # FastAPI app + all HTTP endpoints
│   ├── config.py             # env-driven settings (Settings singleton)
│   ├── database.py           # SQLAlchemy engine/session/Base + init_db()
│   ├── models.py             # ORM models: Job, Transaction, JobSummary
│   ├── schemas.py            # Pydantic response models
│   ├── celery_app.py         # Celery application instance
│   ├── tasks.py              # process_job task + save_result()
│   ├── pipeline.py           # the data-cleaning / aggregation pipeline
│   └── llm.py                # Gemini client + local fallback + helpers
├── tests/
│   └── test_pipeline.py      # unit tests for pure pipeline functions
├── uploads/                  # runtime upload dir (bind-mounted; gitignored content)
├── transactions.csv          # sample dataset shipped with the assignment
├── Dockerfile                # image build (python:3.12-slim)
├── docker-compose.yml        # api + worker + postgres + redis
├── requirements.txt          # pinned Python dependencies
├── .env.example              # GEMINI_API_KEY / GEMINI_MODEL template
├── .dockerignore
├── README.md                 # quick-start & endpoint cheatsheet
├── ARCHITECTURE.md           # this document
└── Backend_DevOps_Assignment.pdf   # original assignment brief
```

### Module dependency graph

```
main.py ──► celery_app, config, database, models, pipeline, schemas
tasks.py ─► celery_app, database, models, pipeline
pipeline.py ─► llm
llm.py ───► config
models.py ─► database (Base)
database.py ─► config
celery_app.py ─► config
```

`config.py` and `database.py` sit at the bottom of the stack; `pipeline.py` and
`llm.py` contain the domain logic and have **no web/Celery imports**, which is
what makes them unit-testable in isolation.

---

## 6. Module-by-module walkthrough

### `app/config.py` — configuration
A plain `Settings` class reads everything from environment variables with
sensible Docker defaults, exported as a module-level `settings` singleton:

| Setting | Env var | Default |
|---|---|---|
| `database_url` | `DATABASE_URL` | `postgresql+psycopg2://postgres:postgres@postgres:5432/transactions` |
| `redis_url` | `REDIS_URL` | `redis://redis:6379/0` |
| `upload_dir` | `UPLOAD_DIR` | `/app/uploads` |
| `gemini_api_key` | `GEMINI_API_KEY` | `""` (empty → fallback mode) |
| `gemini_model` | `GEMINI_MODEL` | `gemini-1.5-flash` |

There is no `.env` loader in code; environment variables are supplied by Docker
Compose (which itself interpolates `${GEMINI_API_KEY}` from a host `.env`).

### `app/database.py` — persistence plumbing
- Creates the SQLAlchemy `engine` with `pool_pre_ping=True` (so stale pooled
  connections are detected and recycled).
- `SessionLocal` — session factory (`autocommit=False`, `autoflush=False`).
- `Base` — declarative base shared by all models.
- `get_db()` — FastAPI dependency that yields a session and always closes it.
- `init_db(retries=10)` — imports models then calls `Base.metadata.create_all`.
  It **retries up to 10 times with a 2-second sleep**, which tolerates Postgres
  still booting when the app/worker start. This is the project's lightweight
  substitute for migrations: tables are created idempotently on startup.

### `app/models.py` — database schema (ORM)
Three tables. See [§7](#7-data-model) for full column detail.
- `Job` — one row per upload; tracks lifecycle/status and raw/clean row counts.
- `Transaction` — one row per cleaned transaction, with anomaly + LLM fields.
- `JobSummary` — one row per job (1:1) holding aggregates + the LLM narrative.

Relationships use `cascade="all, delete"` and DB-level `ondelete="CASCADE"`, so
deleting a `Job` removes its transactions and summary.

### `app/schemas.py` — API response models
Pydantic v2 models with `Config.from_attributes = True` so they can be built
directly from ORM objects:
- `SummaryMini` — compact summary embedded in status responses.
- `JobStatusResponse` / `JobListItem` — status + listing payloads.
- `TransactionOut` — per-transaction projection (note: `llm_raw_response` and
  internal-only fields are intentionally **not** exposed).
- `JobSummaryOut` — full summary projection.
- `JobResultsResponse` — the combined results envelope.

### `app/celery_app.py` — task queue app
Creates the `celery_app` with Redis as both broker and backend. Config:
- `task_track_started=True` — exposes a `STARTED` state.
- `worker_prefetch_multiplier=1` — each worker grabs one task at a time (fair
  dispatch for long-running jobs).
- `autodiscover_tasks(["app"])` — finds `app/tasks.py`.

### `app/tasks.py` — the background job
`process_job(job_id)` is the Celery entry point. It:
1. Calls `init_db()` (defensive — ensures tables exist in the worker too).
2. Loads the `Job`; returns early if it vanished.
3. Sets status → `processing` and commits (so polling clients see progress).
4. Runs `run_pipeline(job.file_path)` and `save_result(...)`.
5. Sets status → `completed`, records `row_count_raw`/`row_count_clean` and
   `completed_at`.
6. **On any exception**: rolls back, reloads the job, sets status → `failed`
   with the exception text in `error_message`, commits, then **re-raises** (so
   Celery also records the failure).

`save_result(db, job, result)` is **idempotent**: it first deletes any existing
transactions/summary for the job, then bulk-inserts the new cleaned transactions
and the single `JobSummary`. This makes re-processing a job safe.

### `app/main.py` — HTTP API
Defines the FastAPI app and all endpoints (see [§8](#8-api-reference)). On
startup it ensures `UPLOAD_DIR` exists and calls `init_db()`. Helper functions
`get_job_or_404` and `serialize_job_status` keep the handlers thin.

### `app/pipeline.py` — the processing pipeline
Pure, framework-free transformation logic (the heart of the project). Detailed
in [§9](#9-the-processing-pipeline).

### `app/llm.py` — LLM integration + fallback
The `LLMClient`, the local heuristic fallbacks, and the money/aggregation
helpers (`money_to_float`, `group_amounts`). Detailed in [§10](#10-llm-integration--fallback-strategy).

---

## 7. Data model

### `jobs`
| Column | Type | Notes |
|---|---|---|
| `id` | `String(36)` PK | UUID4 string (generated by the API) |
| `filename` | `String(255)` | original (sanitised) filename |
| `file_path` | `String(500)` | absolute path of the saved upload |
| `status` | `String(30)`, indexed | `pending` → `processing` → `completed`/`failed` |
| `row_count_raw` | `Integer` | rows read from the CSV (pre-clean) |
| `row_count_clean` | `Integer` | rows after de-duplication |
| `created_at` | `DateTime` | UTC, set on insert |
| `completed_at` | `DateTime?` | UTC, set when job ends (success or failure) |
| `error_message` | `Text?` | populated only on failure |

### `transactions`
| Column | Type | Notes |
|---|---|---|
| `id` | `Integer` PK | auto |
| `job_id` | `String(36)` FK→jobs, indexed | `ondelete=CASCADE` |
| `txn_id` | `String(100)?` | source transaction id (nullable — some rows lack it) |
| `date` | `Date?` | parsed from multiple formats |
| `merchant` | `String(255)` | defaults to `"Unknown Merchant"` if blank |
| `amount` | `Numeric(14,2)` | exact decimal money |
| `currency` | `String(10)` | upper-cased; defaults `INR` |
| `status` | `String(30)` | upper-cased; defaults `UNKNOWN` |
| `category` | `String(80)` | defaults `Uncategorised` |
| `account_id` | `String(100)` | defaults `UNKNOWN` |
| `notes` | `Text?` | |
| `is_anomaly` | `Boolean` | set by anomaly rules |
| `anomaly_reason` | `Text?` | human-readable reason(s), `;`-joined |
| `llm_category` | `String(80)?` | category produced by the LLM/fallback |
| `llm_raw_response` | `Text?` | raw model output (audit trail) |
| `llm_failed` | `Boolean` | true if the LLM call failed for this row |

### `job_summaries` (1:1 with `jobs`, unique `job_id`)
| Column | Type | Notes |
|---|---|---|
| `total_spend_inr` / `total_spend_usd` | `Numeric(14,2)` | computed locally (always trustworthy) |
| `total_spend_by_currency` | `JSONB` | per-currency totals |
| `top_merchants` | `JSONB` | top 3 merchants by spend |
| `category_breakdown` | `JSONB` | spend per (effective) category |
| `anomaly_count` | `Integer` | number of flagged transactions |
| `narrative` | `Text?` | 2–3 sentence LLM narrative |
| `risk_level` | `String(20)` | `low` / `medium` / `high` |
| `llm_raw_response` | `Text?` | raw summary-model output |
| `llm_failed` | `Boolean` | true if the summary LLM call failed |

---

## 8. API reference

Base URL (local): `http://localhost:8000` — interactive docs at `/docs`.

| Method & path | Purpose | Success | Errors |
|---|---|---|---|
| `GET /health` | liveness probe | `200 {"status":"ok"}` | — |
| `POST /jobs/upload` | upload a CSV, enqueue a job | `202 {job_id, status:"pending"}` | `400` non-CSV or bad columns |
| `GET /jobs/{job_id}/status` | poll job status | `200` `JobStatusResponse` | `404` unknown job |
| `GET /jobs/{job_id}/results` | fetch full results | `200` `JobResultsResponse` | `404` unknown; `409` not yet completed |
| `GET /jobs?status=` | list jobs (newest first) | `200` `list[JobListItem]` | `422` invalid status filter |

Notes:
- **`POST /jobs/upload`** rejects anything whose filename doesn't end in `.csv`,
  saves the file as `{job_id}_{filename}`, and runs `read_csv()` **synchronously**
  to validate the header columns and count rows. If columns are missing it
  deletes the saved file and returns `400`. Only after validation does it insert
  the `Job` and dispatch the Celery task.
- **`GET /jobs/{job_id}/results`** returns `409 Conflict` if the job isn't
  `completed`. `flagged_anomalies` is the subset of transactions with
  `is_anomaly == true`; `per_category_spend` mirrors the summary's
  `category_breakdown`.
- **`GET /jobs`** accepts an optional `status` query param validated by regex
  (`pending|processing|completed|failed`); anything else yields `422`.

---

## 9. The processing pipeline

`run_pipeline(csv_path)` in [app/pipeline.py](app/pipeline.py) chains the stages
below and returns a dict consumed by `save_result`:

```
read_csv ──► remove_duplicate_rows ──► clean_row (×N) ──► mark_anomalies
                                                              │
        ┌─────────────────────────────────────────────────────┘
        ▼
classify_missing_categories ──► build_computed_summary ──► build_llm_summary
```

**1. `read_csv`** — opens with `utf-8-sig` (strips a BOM if present), uses
`csv.DictReader`, and validates that all **9 required columns** are present
(`txn_id, date, merchant, amount, currency, status, category, account_id,
notes`). Missing columns raise `ValueError` (surfaced as a `400` at upload). All
cell values are whitespace-stripped.

**2. `remove_duplicate_rows`** — drops exact duplicate rows. The dedup key is the
tuple of **all required column values** (in sorted column order), so a row is a
duplicate only if every field matches; the first occurrence is kept. Row numbers
are assigned *after* dedup.

**3. `clean_row`** — normalises each surviving row (1-indexed `row_number`) and
applies defaults:

| Field | Cleaning rule |
|---|---|
| `txn_id` | stripped, or `None` if blank |
| `date` | `parse_date`: tries `%d-%m-%Y`, `%Y/%m/%d`, `%Y-%m-%d`; blank → `None`; otherwise unparseable → `ValueError` |
| `merchant` | stripped, or `"Unknown Merchant"` |
| `amount` | `parse_amount`: strips `$` and `,`, blank → `0`, → `Decimal` quantised to 2dp; non-numeric → `ValueError` |
| `currency` | upper-cased, or `"INR"` |
| `status` | upper-cased, or `"UNKNOWN"` |
| `category` | stripped, or `"Uncategorised"` (the sentinel that triggers LLM classification) |
| `account_id` | stripped, or `"UNKNOWN"` |
| `notes` | stripped, or `None` |

Money is handled with `Decimal` end-to-end (never floats) to avoid rounding
errors; conversion to `float` happens only at the JSON-aggregation boundary.

**4. `mark_anomalies`** — two rule families (a row can match both; reasons are
joined with `"; "`):
- **Account outlier** — compute the **median** amount per `account_id`; flag any
  transaction whose amount is **more than 3× the account median** (only when the
  median is > 0). Reason: `"Amount is more than 3x account median (<median>)"`.
- **Currency/brand mismatch** — flag a transaction in **USD** whose merchant name
  contains a known **domestic-only brand** (`swiggy`, `ola`, `irctc`). Reason:
  `"Domestic-only merchant charged in USD"`.

**5. `classify_missing_categories`** — selects rows still equal to
`"Uncategorised"` and sends them in **one batch** to `LLMClient.classify_transactions`.
On `LLMError` the rows are marked `llm_failed=True` and left uncategorised;
otherwise each row's `llm_category` is set from the model response (defaulting to
`"Other"` for any row the model omitted).

**6. `build_computed_summary`** — pure local aggregation (no LLM):
- `total_spend_by_currency` — sum of amounts grouped by currency.
- `category_breakdown` — sum grouped by **effective category** (`llm_category` if
  present, else the original `category`).
- `top_3_merchants` — three highest-spend merchants.
- `anomaly_count` — count of flagged rows.

**7. `build_llm_summary`** — asks the LLM (or fallback) for a narrative JSON
summary, then assembles the final summary dict. Importantly, the **core money
totals that get stored — `total_spend_inr`, `total_spend_usd`, and
`category_breakdown` — always come from the locally computed values, never the
LLM.** The remaining fields (`total_spend_by_currency`, `top_merchants`,
`anomaly_count`, `narrative`, `risk_level`) are taken from the model's JSON
response when present, falling back to the computed values (or sensible defaults)
if the model omits them. If the summary call raises `LLMError`, it falls back
entirely to `LLMClient._fallback_summary` and records `llm_failed=True`.

> **Design note:** the authoritative money figures (INR/USD spend and the
> category breakdown) are always the locally computed aggregates — even where the
> model is allowed to echo numeric fields, those guaranteed-correct totals don't
> depend on it. The LLM's real contribution is the fuzzy work (classification +
> prose + risk label). This keeps the financial numbers correct even when the
> model misbehaves or is offline.

---

## 10. LLM integration & fallback strategy

`LLMClient` ([app/llm.py](app/llm.py)) has two public operations —
`classify_transactions` and `build_summary` — and a strict **"works without a
key"** contract:

```
                 GEMINI_API_KEY set?
                  /             \
                yes              no
                 │                │
         call Gemini REST    local heuristic
         (_call_json)        ( _guess_category /
                 │             _fallback_summary )
          success? ──no──► retry up to 3×
                 │          (1s, 2s, 4s backoff)
                yes              │
                 │            still failing
                 ▼                │
           parse JSON             ▼
                            raise LLMError ──► pipeline records
                                               llm_failed=True and
                                               uses fallback values
```

- **No API key (default)** — fully offline:
  - `_guess_category` maps merchants to categories by keyword
    (e.g. *swiggy/zomato/starbucks* → Food, *amazon/flipkart/myntra* → Shopping,
    *ola/uber* → Transport, *atm* → Cash Withdrawal, …, else *Other*).
  - `_fallback_summary` builds a templated narrative and derives `risk_level`
    from the anomaly count (`>=5` → high, `>=2` → medium, else low).
  - Both report `llm_failed=False` — the fallback is a first-class result, not an
    error.

- **API key present** — calls Gemini's `:generateContent` endpoint with a prompt
  that pins the allowed categories / exact JSON shape, via `_call_json`:
  - **3 attempts** with exponential backoff (`2**attempt` → 1s, 2s, 4s).
  - `_parse_json_text` is defensive about model output: it strips ```` ```json ````
    fences and, if a clean `json.loads` fails, extracts the first `{...}` block
    with a regex.
  - Returned categories are validated against `CATEGORY_OPTIONS` (unknown →
    `Other`); `risk_level` is validated against `{low, medium, high}`.
  - After 3 failures it raises `LLMError`, which the pipeline catches and turns
    into a graceful fallback (classification) or `_fallback_summary` (summary).

Every model response (or fallback payload, or error string) is stored in
`llm_raw_response`, giving a complete audit trail of what the model returned.

The allowed categories are: **Food, Shopping, Travel, Transport, Utilities, Cash
Withdrawal, Entertainment, Other**.

---

## 11. Configuration & environment variables

| Variable | Used by | Default | Purpose |
|---|---|---|---|
| `DATABASE_URL` | api, worker | local Postgres DSN | SQLAlchemy connection |
| `REDIS_URL` | api, worker | `redis://redis:6379/0` | Celery broker + backend |
| `UPLOAD_DIR` | api, worker | `/app/uploads` | where uploads are written |
| `GEMINI_API_KEY` | worker | `""` | enables Gemini; empty → local fallback |
| `GEMINI_MODEL` | worker | `gemini-1.5-flash` | Gemini model name |

`GEMINI_API_KEY`/`GEMINI_MODEL` are passed through from a host `.env`
(`docker-compose.yml` uses `${GEMINI_API_KEY:-}`). Copy `.env.example` → `.env`
and fill in a key to enable live Gemini calls; otherwise everything runs offline.

---

## 12. Running, building & developing

```bash
# start everything (api, worker, postgres, redis)
docker compose up --build
```

- API: `http://localhost:8000` — Swagger UI at `/docs`.
- Tables are auto-created on startup (`init_db`); **no manual migrations**.
- The `./app` directory is bind-mounted and the API runs with `--reload`, so
  code edits hot-reload the API. (The Celery worker is **not** auto-reloading —
  restart the `worker` container to pick up worker-side changes.)
- Postgres data persists in the `postgres_data` named volume across restarts.

Example flow:
```bash
curl -F "file=@transactions.csv" http://localhost:8000/jobs/upload   # → {job_id,...}
curl http://localhost:8000/jobs/<job_id>/status
curl http://localhost:8000/jobs/<job_id>/results
curl "http://localhost:8000/jobs?status=completed"
```

**Build details** ([Dockerfile](Dockerfile)): `python:3.12-slim`, installs pinned
`requirements.txt`, copies `app/` and the sample `transactions.csv`, creates
`/app/uploads`, and defaults to launching `uvicorn`. The compose file overrides
the command per service (uvicorn `--reload` for api, `celery ... worker` for the
worker). Both Postgres and Redis declare **health checks**, and the app/worker
`depends_on` them with `condition: service_healthy`, so they wait for their
dependencies before starting.

---

## 13. Testing

[tests/test_pipeline.py](tests/test_pipeline.py) unit-tests the pure pipeline
functions (no DB, no network, no Docker required):

```bash
pytest
```

Covered:
- `parse_date` accepts the assignment's date formats (`DD-MM-YYYY`, `YYYY/MM/DD`).
- `parse_amount` strips a leading `$`.
- `clean_row` upper-cases status/currency and applies the `Uncategorised` default.
- `mark_anomalies` flags a domestic brand (Swiggy) charged in USD.

These functions were deliberately kept side-effect-free so they're trivially
testable — the web/Celery/DB layers are thin wrappers around them.

---

## 14. Sample dataset

[transactions.csv](transactions.csv) (~95 rows) is crafted to exercise every
pipeline branch:
- **Mixed date formats** — `04-09-2024`, `2024/02/05`, `2024-07-15`.
- **Mixed-case** currencies/statuses — `inr`, `success`, etc. (normalised).
- **Currency symbols & commas** in amounts — `$11325.79`.
- **Missing values** — blank `txn_id`, blank `category` (triggers LLM/fallback
  classification).
- **Exact duplicate rows** — e.g. `TXN1009`, `TXN1035`, `TXN1033`, `TXN1016`
  appear twice (removed by `remove_duplicate_rows`).
- **Account outliers** — the `TXN2000`–`TXN2004` rows (~91k–193k INR) are far
  above their accounts' medians and get flagged.
- **Currency inconsistencies** — `Zomato` and `MakeMyTrip` rows appear in `USD`.
  Note: these particular brands do **not** trigger the domestic-brand-in-USD
  anomaly, which only fires for `swiggy`/`ola`/`irctc` charged in USD — a
  combination the sample CSV happens not to contain (that rule is exercised by
  the unit test instead).

---

## 15. Design notes, edge cases & caveats

- **Async-first.** Uploads enqueue and return `202` immediately; all heavy work
  (and Gemini latency) is isolated in the worker. Clients poll `/status`.
- **Money is `Decimal`, not float.** Parsing, storage (`Numeric(14,2)`), and
  aggregation use `Decimal`; floats appear only at the JSON serialization edge
  (`money_to_float`).
- **LLM is advisory, never authoritative for numbers.** The authoritative money
  figures (INR/USD spend and the category breakdown) are computed locally; the
  model fills missing categories, writes the prose + risk label, and may echo
  some numeric fields, but the guaranteed-correct totals never depend on it. A
  model outage degrades quality, not correctness.
- **Graceful degradation everywhere.** No key → deterministic local heuristics;
  transient Gemini errors → retried then absorbed into `llm_failed=True` +
  fallback values. The full pipeline always completes (or fails loudly with the
  reason recorded on the `Job`).
- **Idempotent processing.** `save_result` clears prior transactions/summary for
  the job before inserting, so re-running a job can't create duplicates.
- **Robust startup ordering.** `init_db` retries while Postgres boots, and
  compose health checks gate `depends_on`.
- **Auditability.** Raw model output is persisted (`llm_raw_response`) on both
  transactions and the summary.
- **`row_count_raw` is counted twice.** Once at upload (stored on the `Job`) and
  again inside the pipeline; the pipeline's value (set in `process_job`)
  overwrites the upload-time value on completion. Both reflect pre-dedup row
  counts, so they agree.
- **Dedup is exact-match only.** Near-duplicates (differing whitespace already
  normalised, but differing notes/dates) are kept — this is intentional.

### Possible extensions
- Replace `init_db` with Alembic migrations for schema evolution.
- Add authentication and per-user job scoping.
- Stream/paginate `/results` for very large files.
- Swap the bespoke `requests` Gemini client for the official SDK and add
  structured-output enforcement.
- Add pagination + filtering to `/jobs` and integration tests around the worker.
```
