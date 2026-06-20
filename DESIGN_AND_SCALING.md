# System Design, Data Flow & Scaling

> Talking-points companion to [ARCHITECTURE.md](ARCHITECTURE.md). Structured to
> support a short verbal walkthrough: **§1 System Design & Data Flow (~1 min)**
> and **§2 Bottlenecks & Scale (~2 min)**. Every bottleneck below cites the exact
> line of code where it lives.

---

## 1. System Design & Data Flow

### 1a. The Blueprint

Four cooperating services; the API never does heavy work, the worker does.

```
                      ┌────────────────────── Docker Compose ──────────────────────┐
                      │                                                             │
  ①  POST /jobs/upload│   ┌──────────┐   ③ send_task      ┌─────────┐  ④ consume    │
 ────────────────────►│   │   api    │ ────────────────►  │  redis  │ ────────────┐ │
  ⑧ GET status/results│   │ FastAPI  │                    │ broker+ │             │ │
 ◄────────────────────│   │ (uvicorn)│ ◄──────────────    │ backend │             ▼ │
                      │   └────┬─────┘   task state         └─────────┘     ┌──────────┐
                      │        │ ② validate+save+enqueue                    │  worker  │
                      │        │ ⑦ read results                             │ (Celery) │
                      │        ▼                                            └────┬─────┘
                      │   ┌──────────┐  ⑥ persist cleaned txns + summary         │ ⑤ pipeline
                      │   │ postgres │ ◄──────────────────────────────────────────┤   + LLM
                      │   │   (16)   │                                            │
                      │   └──────────┘                                  ┌─────────▼────────┐
                      │                                                 │  Gemini REST API │ (optional;
                      └─────────────────────────────────────────────────│  else local      │  offline
                                                                        │  fallback)       │  fallback)
                                                                        └──────────────────┘
```

*(In the live walkthrough, draw this on Miro/draw.io and narrate the numbered
arrows ①–⑧ — see the lifecycle in §1c.)*

- **api** — stateless FastAPI app. Validates uploads, enqueues, serves reads.
- **worker** — Celery consumer running the cleaning/anomaly/LLM/aggregation pipeline.
- **postgres** — durable store (`jobs`, `transactions`, `job_summaries`).
- **redis** — Celery broker **and** result backend.
- **Gemini** — called only by the worker; degrades to deterministic local logic.

### 1b. The "Why" — reasoning behind the choices

**Why split API and worker (the central decision).** CSV parsing, anomaly
math, and LLM calls are slow and bursty; HTTP requests must stay fast. Putting
the pipeline behind a queue lets the upload return `202` in milliseconds
([main.py:30-59](app/main.py#L30-L59)) while the worker absorbs latency and
retries. It also lets the two scale **independently** — many cheap API replicas,
fewer heavy workers.

**Why this folder structure.** The package is organized by *responsibility*, and
deliberately keeps the domain logic free of framework imports:

| Layer | Files | Rule it follows |
|---|---|---|
| Edge / transport | `main.py`, `schemas.py` | HTTP only; thin handlers |
| Orchestration | `tasks.py`, `celery_app.py` | wiring, no business rules |
| **Domain (pure)** | `pipeline.py`, `llm.py` | **no FastAPI/Celery/DB imports** |
| Infra | `database.py`, `config.py`, `models.py` | engine, settings, schema |

The payoff: `pipeline.py` and `llm.py` are unit-testable with plain function
calls — [tests/test_pipeline.py](tests/test_pipeline.py) needs no DB, broker, or
network. Web/Celery/DB layers are thin wrappers around that pure core.

**Why the database schema looks like this.**
- A **`jobs`** table models the async lifecycle explicitly (`pending → processing
  → completed/failed`) with `error_message`/`completed_at`, so a polling client
  always has a truthful status — the queue state is projected into SQL, not
  hidden in Redis.
- **`transactions`** stores both the cleaned values *and* the provenance
  (`is_anomaly`, `anomaly_reason`, `llm_category`, `llm_raw_response`,
  `llm_failed`) — every automated decision is auditable after the fact.
- **`job_summaries`** is 1:1 with a job (`unique` FK) and uses **`JSONB`** for
  the flexible aggregates (`category_breakdown`, `top_merchants`,
  `total_spend_by_currency`) while keeping the money totals as exact
  `Numeric(14,2)`. Structured where it must be exact, schemaless where it's a
  bag of rollups.
- **`ondelete=CASCADE`** + ORM `cascade="all, delete"` make a job the unit of
  ownership: delete the job, its rows and summary go with it.

**Why these libraries.**
- **FastAPI + Pydantic** — typed request/response models for free, `/docs`
  out of the box, `from_attributes` to serialize ORM objects directly.
- **Celery + Redis** — the standard, batteries-included Python task queue; Redis
  doubles as broker and backend so the stack stays at four containers.
- **SQLAlchemy 2.0 + psycopg2** — mature ORM; `Decimal`/`Numeric` end-to-end so
  money never touches floating point.
- **Plain `requests` for Gemini, no AI SDK** ([llm.py:95-115](app/llm.py#L95-L115))
  — one fewer heavy dependency, and it makes the **offline fallback** trivial:
  no key → deterministic heuristics, so the whole project runs with one
  `docker compose up` and zero secrets.

**Why a local LLM fallback at all.** The financial numbers are computed locally
and treated as the source of truth; the model only does fuzzy work
(classification + prose + risk label). That means a missing key or a Gemini
outage degrades *quality*, not *correctness* — and the assignment runs
deterministically for a reviewer with no API key.

### 1c. Request lifecycle — one upload, end to end

Tracing `POST /jobs/upload` → persistence → `GET …/results` (arrows ①–⑧ above):

1. **① Upload hits the API.** `upload_job` rejects anything not ending in `.csv`
   (`400`) ([main.py:32-33](app/main.py#L32-L33)).
2. **② Save + validate synchronously.** The file is streamed to
   `UPLOAD_DIR/{job_id}_{name}` ([main.py:37-40](app/main.py#L37-L40)), then
   `read_csv()` runs **inline** to validate the 9 required columns and count rows
   ([main.py:43](app/main.py#L43)). Bad columns → delete file + `400`
   ([main.py:44-46](app/main.py#L44-L46)).
3. **Persist the job.** Insert `Job(status="pending", row_count_raw=…)` and commit
   ([main.py:48-56](app/main.py#L48-L56)).
4. **③ Enqueue + return.** `send_task("app.tasks.process_job", [job_id])` pushes
   to Redis; the handler returns `202 {job_id, "pending"}`
   ([main.py:58-59](app/main.py#L58-L59)). **The request ends here** — nothing
   above blocked on the pipeline.
5. **④ Worker picks up the task.** `process_job` calls `init_db()`, sets status
   `processing` and commits so pollers see progress
   ([tasks.py:13-22](app/tasks.py#L13-L22)).
6. **⑤ Pipeline runs.** `run_pipeline(file_path)`
   ([pipeline.py:27-44](app/pipeline.py#L27-L44)): read → de-dupe → clean →
   `mark_anomalies` → `classify_missing_categories` (LLM/fallback) →
   `build_computed_summary` → `build_llm_summary`.
7. **⑥ Persist results (idempotently).** `save_result` deletes any prior rows for
   the job, then inserts cleaned transactions + one `JobSummary`
   ([tasks.py:44-85](app/tasks.py#L44-L85)); status → `completed` with row counts
   and `completed_at` ([tasks.py:26-30](app/tasks.py#L26-L30)). On any exception:
   rollback → status `failed` + `error_message`, then re-raise
   ([tasks.py:31-39](app/tasks.py#L31-L39)).
8. **⑦⑧ Client reads back.** `GET …/status` polls the lifecycle; `GET …/results`
   returns `409` until `completed`, then loads the transactions, splits out
   anomalies, and returns the summary
   ([main.py:68-90](app/main.py#L68-L90)).

---

## 2. Bottlenecks & Scale

### 2a. The Breaking Point — where 100× traffic breaks *this* codebase

Concrete failure points, each tied to a line of code. Roughly ordered by how soon
they bite.

| # | Breaks at scale | Where | Why it fails at 100× |
|---|---|---|---|
| 1 | **Synchronous parse in the request thread** | [main.py:43](app/main.py#L43) | `read_csv()` reads + validates the whole file *inside* the upload handler. Sync `def` endpoints run in FastAPI's bounded threadpool; 100× concurrent uploads exhaust threads and hold a DB connection each → upload latency and `503`s, before the worker is even involved. |
| 2 | **DB connection pool** | [database.py:9](app/database.py#L9) | `create_engine` sets no pool sizing, so SQLAlchemy's default `QueuePool` (≈5 + 10 overflow per process) caps concurrent DB work. Add Postgres's default `max_connections≈100` shared across the API pool + every worker pool, and 100× concurrency means `QueuePool limit … connection timed out`. |
| 3 | **Single worker, serial-ish processing** | [docker-compose.yml:23-25](docker-compose.yml#L23-L25), [celery_app.py:12-15](app/celery_app.py#L12-L15) | One `worker` replica with `prefetch_multiplier=1` and default concurrency (= CPU cores). Ingest scales but processing doesn't → the Redis queue grows without bound and job latency goes to minutes/hours. No autoscaling. |
| 4 | **LLM call: one giant blocking batch per job** | [llm.py:34-67](app/llm.py#L34-L67), [llm.py:95-115](app/llm.py#L95-L115) | All missing-category rows go in **one** prompt; `_call_json` is a synchronous `requests.post(timeout=30)` with up to 3 retries and `time.sleep` backoff. Big files blow the model's token limit, and at fleet scale Gemini **RPM/TPM rate limits** become the hard ceiling — there's no cross-job batching, rate limiting, or async. The worker process is parked on a blocking socket the whole time. |
| 5 | **`/results` loads the entire job into memory** | [main.py:74-90](app/main.py#L74-L90) | `.all()` with no pagination pulls every transaction into the API process, filters anomalies in Python, and serializes all of it through Pydantic. A million-row job OOMs the API and produces a multi-hundred-MB response. |
| 6 | **Whole file held in RAM in the pipeline** | [pipeline.py:27-44](app/pipeline.py#L27-L44) | `read_csv` returns a `list`, and the pipeline builds several parallel lists plus per-account median tables. Memory is O(rows) — a multi-GB CSV kills the worker. No streaming/chunking. |
| 7 | **Per-row ORM inserts** | [tasks.py:48-67](app/tasks.py#L48-L67) | `save_result` does `db.add(...)` per transaction inside one transaction. N individual INSERTs → slow commits and a long-held write lock; throughput collapses on large files. |
| 8 | **Local-disk uploads** | [main.py:37-40](app/main.py#L37-L40) | Files land on a container-local/bind-mounted `UPLOAD_DIR`. A worker on another host can't read what the API wrote, so this **blocks horizontal scaling outright**. Files are also never deleted → unbounded disk growth. |
| 9 | **Redis is broker + backend + single instance** | [celery_app.py:6-10](app/celery_app.py#L6-L10) | One Redis is a single point of failure for queue *and* results; `task_track_started` state accumulates there. No HA, no separation of concerns. |
| 10 | **Reliability gaps under retry** | [celery_app.py:12-15](app/celery_app.py#L12-L15) | Default early-ack (no `acks_late`): a worker crash mid-job silently drops the task — the job is stuck in `processing` forever. `init_db()` also runs on **every** task ([tasks.py:13](app/tasks.py#L13)), adding needless DDL chatter at high task rates. No upload idempotency, so retries/double-clicks double-process. |

**One-line summary for the interview:** *it breaks at the edges first* — the
synchronous upload thread and the connection pool saturate before anything else,
then the single worker + blocking all-in-one LLM call become the throughput
ceiling, and local-disk file storage blocks you from scaling out to fix any of
it.

### 2b. The Next Iteration — re-engineering for enterprise scale

Each change below maps to a bottleneck above, with its trade-off stated plainly.

| Area | Change | Fixes | Trade-off |
|---|---|---|---|
| **File ingest** | Upload straight to **object storage (S3/GCS) via presigned URLs**; pass the object key through the queue. | #1, #8 | New infra; presigned-URL + lifecycle complexity; the API no longer "sees" the file (validate in the worker). |
| **Upload path** | Make upload do *only* metadata + enqueue; move column validation into the worker (or a fast head-only check). | #1 | Invalid files are detected later (async), so surface validation errors via job status, not the HTTP 400. |
| **Connection pooling** | Put **PgBouncer** in front of Postgres; tune `pool_size`/`max_overflow`; consider async driver (`asyncpg`). | #2 | PgBouncer transaction-pooling disables some session features; async is a sizable rewrite. |
| **Worker scaling** | Many stateless workers behind **KEDA/HPA autoscaling on queue depth**; split big files into chunks with Celery **`group`/`chord`** (map-classify → reduce-summary). | #3, #6 | Orchestration + partial-failure handling; cross-row aggregates (per-account median) need a two-pass or a SQL-side computation. |
| **Streaming pipeline** | Process the CSV in **bounded chunks** (or push raw rows to Postgres and aggregate with SQL: `COPY` + `GROUP BY`, percentile funcs for medians). | #6, #7 | More complex code; some logic moves from Python into SQL. |
| **Bulk writes** | Replace per-row `add` with `bulk_insert_mappings` / `COPY` / `execute_values`. | #7 | Bypasses ORM events/validation; need explicit conflict handling. |
| **LLM tier** | **Chunk** classification into token-bounded requests; dedicated **rate-limited LLM worker pool** (token bucket on RPM/TPM); **cache by merchant** (classification is near-deterministic per merchant); make calls async/concurrent. | #4 | Cache staleness; added Redis cache; chunking + reassembly logic; cost/latency tuning. |
| **Read path** | **Paginate/cursor** `/results`; stream large payloads; serve heavy reads from a **read replica**. | #5 | API contract change; replica lag (eventually-consistent reads). |
| **Schema/DB** | **Alembic** migrations instead of `create_all`; **partition** `transactions` by `job_id`/time; managed Postgres + read replicas; drop `init_db()` from the task path. | #2, #5, #10 | Migration discipline; partition maintenance. |
| **Reliability** | `acks_late=True` + `task_reject_on_worker_lost`; **idempotency key** = file hash on upload; retries with jittered backoff; **dead-letter queue**. | #10 | Tasks must be fully idempotent (mostly true already via `save_result`'s delete-then-insert). |
| **Infra/HA** | Separate **broker vs. result backend**; managed/clustered Redis (or move durable results to Postgres); API behind an LB with HPA. | #9 | More moving parts and cost. |
| **Observability** | Structured logs, **OpenTelemetry tracing**, metrics (queue depth, job + LLM latency/error rate), alerting. | all | Instrumentation overhead. |
| **Enterprise concerns** | AuthN/AuthZ, per-tenant quotas/rate limits, encryption at rest, PII handling + audit for financial data. | — | Real but unavoidable for production fintech data. |

**The headline trade-off.** Today's design optimizes for *clarity and
single-command reproducibility* — perfect for an assignment, deliberately
un-tuned for scale. The enterprise version trades that simplicity for
**horizontal scalability and resilience**: object storage + chunked map/reduce
workers + a rate-limited, cached LLM tier + bulk SQL writes, all behind
autoscaling and observability. More components and operational burden, but each
addition removes a specific, named ceiling above — and the existing clean split
between the pure domain core (`pipeline.py`/`llm.py`) and the infra wrappers is
exactly what makes that migration incremental rather than a rewrite.
