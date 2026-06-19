import shutil
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.database import get_db, init_db
from app.models import Job, JobSummary, Transaction
from app.pipeline import read_csv
from app.schemas import JobListItem, JobResultsResponse, JobStatusResponse


app = FastAPI(title="AI Transaction Processing Pipeline")


@app.on_event("startup")
def on_startup():
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/jobs/upload", status_code=202)
def upload_job(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a CSV file")

    job_id = str(uuid.uuid4())
    safe_name = Path(file.filename).name
    file_path = Path(settings.upload_dir) / f"{job_id}_{safe_name}"

    with file_path.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)

    try:
        raw_rows = read_csv(str(file_path))
    except ValueError as exc:
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = Job(
        id=job_id,
        filename=safe_name,
        file_path=str(file_path),
        status="pending",
        row_count_raw=len(raw_rows),
    )
    db.add(job)
    db.commit()

    celery_app.send_task("app.tasks.process_job", args=[job_id])
    return {"job_id": job_id, "status": "pending"}


@app.get("/jobs/{job_id}/status", response_model=JobStatusResponse)
def get_job_status(job_id: str, db: Session = Depends(get_db)):
    job = get_job_or_404(db, job_id)
    return serialize_job_status(job)


@app.get("/jobs/{job_id}/results", response_model=JobResultsResponse)
def get_job_results(job_id: str, db: Session = Depends(get_db)):
    job = get_job_or_404(db, job_id)
    if job.status != "completed":
        raise HTTPException(status_code=409, detail=f"Job is {job.status}, results are not ready")

    transactions = (
        db.query(Transaction)
        .filter(Transaction.job_id == job.id)
        .order_by(Transaction.id.asc())
        .all()
    )
    anomalies = [txn for txn in transactions if txn.is_anomaly]
    summary = db.query(JobSummary).filter(JobSummary.job_id == job.id).first()

    return {
        "job_id": job.id,
        "status": job.status,
        "cleaned_transactions": transactions,
        "flagged_anomalies": anomalies,
        "per_category_spend": summary.category_breakdown if summary else {},
        "llm_summary": summary,
    }


@app.get("/jobs", response_model=list[JobListItem])
def list_jobs(
    status: str | None = Query(default=None, pattern="^(pending|processing|completed|failed)$"),
    db: Session = Depends(get_db),
):
    query = db.query(Job)
    if status:
        query = query.filter(Job.status == status)
    jobs = query.order_by(Job.created_at.desc()).all()
    return [
        {
            "job_id": job.id,
            "status": job.status,
            "filename": job.filename,
            "row_count_raw": job.row_count_raw,
            "row_count_clean": job.row_count_clean,
            "created_at": job.created_at,
        }
        for job in jobs
    ]


def get_job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def serialize_job_status(job: Job) -> dict:
    response = {
        "job_id": job.id,
        "status": job.status,
        "filename": job.filename,
        "row_count_raw": job.row_count_raw,
        "row_count_clean": job.row_count_clean,
        "created_at": job.created_at,
        "completed_at": job.completed_at,
        "error_message": job.error_message,
        "summary": None,
    }
    if job.status == "completed" and job.summary:
        response["summary"] = {
            "total_spend_by_currency": job.summary.total_spend_by_currency,
            "anomaly_count": job.summary.anomaly_count,
            "risk_level": job.summary.risk_level,
        }
    return response
