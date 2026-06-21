import json
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.database import Base


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    filename = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    status = Column(String(30), nullable=False, default="pending", index=True)
    row_count_raw = Column(Integer, default=0)
    row_count_clean = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)

    transactions = relationship("Transaction", back_populates="job", cascade="all, delete")
    summary = relationship("JobSummary", back_populates="job", uselist=False, cascade="all, delete")


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True)
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    txn_id = Column(String(100), nullable=True)
    date = Column(Date, nullable=True)
    merchant = Column(String(255), nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    currency = Column(String(10), nullable=False)
    status = Column(String(30), nullable=False)
    category = Column(String(80), nullable=False, default="Uncategorised")
    account_id = Column(String(100), nullable=False)
    notes = Column(Text, nullable=True)
    is_anomaly = Column(Boolean, default=False)
    anomaly_reason = Column(Text, nullable=True)
    llm_category = Column(String(80), nullable=True)
    llm_raw_response = Column(Text, nullable=True)
    llm_failed = Column(Boolean, default=False)

    job = relationship("Job", back_populates="transactions")


class JobSummary(Base):
    __tablename__ = "job_summaries"

    id = Column(Integer, primary_key=True)
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, nullable=False)
    total_spend_inr = Column(Numeric(14, 2), default=0)
    total_spend_usd = Column(Numeric(14, 2), default=0)
    total_spend_by_currency = Column(JSONB, default=dict)
    top_merchants = Column(JSONB, default=list)
    category_breakdown = Column(JSONB, default=dict)
    anomaly_count = Column(Integer, default=0)
    narrative = Column(Text, nullable=True)
    risk_level = Column(String(20), default="low")
    llm_raw_response = Column(Text, nullable=True)
    llm_failed = Column(Boolean, default=False)

    job = relationship("Job", back_populates="summary")

    @property
    def analysis_source(self) -> str:
        """Identify the summary source without adding another database column."""
        if self.llm_failed:
            return "local_fallback"
        try:
            raw = json.loads(self.llm_raw_response or "{}")
            if raw.get("source") == "local_fallback":
                return "local_fallback"
        except (json.JSONDecodeError, TypeError):
            pass
        return "openrouter"
