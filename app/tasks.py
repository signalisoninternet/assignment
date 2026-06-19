from datetime import datetime

from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.database import SessionLocal, init_db
from app.models import Job, JobSummary, Transaction
from app.pipeline import run_pipeline


@celery_app.task(name="app.tasks.process_job")
def process_job(job_id: str):
    init_db()
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if not job:
            return

        job.status = "processing"
        db.commit()

        result = run_pipeline(job.file_path)
        save_result(db, job, result)

        job.status = "completed"
        job.row_count_raw = result["row_count_raw"]
        job.row_count_clean = result["row_count_clean"]
        job.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.get(Job, job_id)
        if job:
            job.status = "failed"
            job.error_message = str(exc)
            job.completed_at = datetime.utcnow()
            db.commit()
        raise
    finally:
        db.close()


def save_result(db: Session, job: Job, result: dict):
    db.query(Transaction).filter(Transaction.job_id == job.id).delete()
    db.query(JobSummary).filter(JobSummary.job_id == job.id).delete()

    for item in result["transactions"]:
        db.add(
            Transaction(
                job_id=job.id,
                txn_id=item["txn_id"],
                date=item["date"],
                merchant=item["merchant"],
                amount=item["amount"],
                currency=item["currency"],
                status=item["status"],
                category=item["category"],
                account_id=item["account_id"],
                notes=item["notes"],
                is_anomaly=item["is_anomaly"],
                anomaly_reason=item["anomaly_reason"],
                llm_category=item["llm_category"],
                llm_raw_response=item["llm_raw_response"],
                llm_failed=item["llm_failed"],
            )
        )

    summary = result["summary"]
    db.add(
        JobSummary(
            job_id=job.id,
            total_spend_inr=summary["total_spend_inr"],
            total_spend_usd=summary["total_spend_usd"],
            total_spend_by_currency=summary["total_spend_by_currency"],
            top_merchants=summary["top_merchants"],
            category_breakdown=summary["category_breakdown"],
            anomaly_count=summary["anomaly_count"],
            narrative=summary["narrative"],
            risk_level=summary["risk_level"],
            llm_raw_response=summary["llm_raw_response"],
            llm_failed=summary["llm_failed"],
        )
    )
