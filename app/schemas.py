from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class SummaryMini(BaseModel):
    total_spend_by_currency: dict[str, Any]
    anomaly_count: int
    risk_level: str

    class Config:
        from_attributes = True


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    filename: str
    row_count_raw: int
    row_count_clean: int
    created_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None
    summary: SummaryMini | None = None


class JobListItem(BaseModel):
    job_id: str
    status: str
    filename: str
    row_count_raw: int
    row_count_clean: int
    created_at: datetime


class TransactionOut(BaseModel):
    txn_id: str | None
    date: date | None
    merchant: str
    amount: Decimal
    currency: str
    status: str
    category: str
    account_id: str
    notes: str | None
    is_anomaly: bool
    anomaly_reason: str | None
    llm_category: str | None
    llm_failed: bool

    class Config:
        from_attributes = True


class JobSummaryOut(BaseModel):
    total_spend_inr: Decimal
    total_spend_usd: Decimal
    total_spend_by_currency: dict[str, Any]
    top_merchants: list[dict[str, Any]]
    category_breakdown: dict[str, Any]
    anomaly_count: int
    narrative: str | None
    risk_level: str
    llm_failed: bool

    class Config:
        from_attributes = True


class JobResultsResponse(BaseModel):
    job_id: str
    status: str
    cleaned_transactions: list[TransactionOut]
    flagged_anomalies: list[TransactionOut]
    per_category_spend: dict[str, Any]
    llm_summary: JobSummaryOut | None
