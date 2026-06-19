import csv
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any

from app.llm import LLMClient, LLMError, group_amounts, money_to_float


REQUIRED_COLUMNS = {
    "txn_id",
    "date",
    "merchant",
    "amount",
    "currency",
    "status",
    "category",
    "account_id",
    "notes",
}

DOMESTIC_ONLY_BRANDS = {"swiggy", "ola", "irctc"}


def run_pipeline(csv_path: str) -> dict[str, Any]:
    raw_rows = read_csv(csv_path)
    unique_rows = remove_duplicate_rows(raw_rows)
    cleaned_rows = [clean_row(row, index + 1) for index, row in enumerate(unique_rows)]
    mark_anomalies(cleaned_rows)

    llm = LLMClient()
    classify_missing_categories(cleaned_rows, llm)

    computed_summary = build_computed_summary(cleaned_rows)
    summary = build_llm_summary(computed_summary, llm)

    return {
        "row_count_raw": len(raw_rows),
        "row_count_clean": len(cleaned_rows),
        "transactions": cleaned_rows,
        "summary": summary,
    }


def read_csv(csv_path: str) -> list[dict[str, str]]:
    path = Path(csv_path)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
        return [{key: (value or "").strip() for key, value in row.items()} for row in reader]


def remove_duplicate_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = set()
    unique_rows = []
    for row in rows:
        row_key = tuple(row.get(column, "") for column in sorted(REQUIRED_COLUMNS))
        if row_key in seen:
            continue
        seen.add(row_key)
        unique_rows.append(row)
    return unique_rows


def clean_row(row: dict[str, str], row_number: int) -> dict[str, Any]:
    category = clean_text(row.get("category"))
    return {
        "row_number": row_number,
        "txn_id": clean_text(row.get("txn_id")) or None,
        "date": parse_date(row.get("date", "")),
        "merchant": clean_text(row.get("merchant")) or "Unknown Merchant",
        "amount": parse_amount(row.get("amount", "")),
        "currency": clean_text(row.get("currency")).upper() or "INR",
        "status": clean_text(row.get("status")).upper() or "UNKNOWN",
        "category": category or "Uncategorised",
        "account_id": clean_text(row.get("account_id")) or "UNKNOWN",
        "notes": clean_text(row.get("notes")) or None,
        "is_anomaly": False,
        "anomaly_reason": None,
        "llm_category": None,
        "llm_raw_response": None,
        "llm_failed": False,
    }


def clean_text(value: str | None) -> str:
    return (value or "").strip()


def parse_date(value: str):
    value = clean_text(value)
    if not value:
        return None
    for pattern in ("%d-%m-%Y", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date: {value}")


def parse_amount(value: str) -> Decimal:
    cleaned = clean_text(value).replace("$", "").replace(",", "")
    if not cleaned:
        cleaned = "0"
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount: {value}") from exc


def mark_anomalies(rows: list[dict[str, Any]]) -> None:
    amounts_by_account: dict[str, list[Decimal]] = defaultdict(list)
    for row in rows:
        amounts_by_account[row["account_id"]].append(row["amount"])

    medians = {
        account_id: Decimal(str(median(amounts)))
        for account_id, amounts in amounts_by_account.items()
        if amounts
    }

    for row in rows:
        reasons = []
        account_median = medians.get(row["account_id"], Decimal("0"))
        if account_median > 0 and row["amount"] > account_median * 3:
            reasons.append(f"Amount is more than 3x account median ({account_median})")

        merchant_key = row["merchant"].lower()
        is_domestic_brand = any(brand in merchant_key for brand in DOMESTIC_ONLY_BRANDS)
        if row["currency"] == "USD" and is_domestic_brand:
            reasons.append("Domestic-only merchant charged in USD")

        if reasons:
            row["is_anomaly"] = True
            row["anomaly_reason"] = "; ".join(reasons)


def classify_missing_categories(rows: list[dict[str, Any]], llm: LLMClient) -> None:
    missing = [row for row in rows if row["category"] == "Uncategorised"]
    if not missing:
        return

    try:
        categories, raw_response, failed = llm.classify_transactions(missing)
    except LLMError as exc:
        raw_response = str(exc)
        categories = {}
        failed = True

    for row in missing:
        row["llm_raw_response"] = raw_response
        row["llm_failed"] = failed
        if not failed:
            row["llm_category"] = categories.get(row["row_number"], "Other")


def build_computed_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_spend_by_currency = group_amounts(rows, "currency")
    category_rows = [
        {**row, "effective_category": row["llm_category"] or row["category"]}
        for row in rows
    ]
    category_breakdown = group_amounts(category_rows, "effective_category")

    merchant_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        merchant_totals[row["merchant"]] += row["amount"]
    top_merchants = [
        {"merchant": merchant, "amount": money_to_float(amount)}
        for merchant, amount in sorted(
            merchant_totals.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:3]
    ]

    return {
        "total_spend_by_currency": total_spend_by_currency,
        "category_breakdown": category_breakdown,
        "top_3_merchants": top_merchants,
        "anomaly_count": sum(1 for row in rows if row["is_anomaly"]),
    }


def build_llm_summary(computed_summary: dict[str, Any], llm: LLMClient) -> dict[str, Any]:
    try:
        summary, raw_response, failed = llm.build_summary(computed_summary)
    except LLMError as exc:
        summary = llm._fallback_summary(computed_summary)
        raw_response = str(exc)
        failed = True

    totals = computed_summary["total_spend_by_currency"]
    return {
        "total_spend_inr": Decimal(str(totals.get("INR", 0))).quantize(Decimal("0.01")),
        "total_spend_usd": Decimal(str(totals.get("USD", 0))).quantize(Decimal("0.01")),
        "total_spend_by_currency": summary.get(
            "total_spend_by_currency",
            computed_summary["total_spend_by_currency"],
        ),
        "top_merchants": summary.get("top_3_merchants", computed_summary["top_3_merchants"]),
        "category_breakdown": computed_summary["category_breakdown"],
        "anomaly_count": int(summary.get("anomaly_count", computed_summary["anomaly_count"])),
        "narrative": summary.get("narrative", ""),
        "risk_level": summary.get("risk_level", "low"),
        "llm_raw_response": raw_response,
        "llm_failed": failed,
    }
