from decimal import Decimal

from app.pipeline import clean_row, mark_anomalies, parse_amount, parse_date


def test_parse_date_accepts_assignment_formats():
    assert parse_date("04-09-2024").isoformat() == "2024-09-04"
    assert parse_date("2024/02/05").isoformat() == "2024-02-05"


def test_parse_amount_strips_currency_symbol():
    assert parse_amount("$11325.79") == Decimal("11325.79")


def test_clean_row_normalises_status_and_missing_category():
    row = {
        "txn_id": "TXN1",
        "date": "2024/02/05",
        "merchant": "Swiggy",
        "amount": "$10",
        "currency": "inr",
        "status": "success",
        "category": "",
        "account_id": "ACC1",
        "notes": "",
    }
    cleaned = clean_row(row, 1)
    assert cleaned["status"] == "SUCCESS"
    assert cleaned["currency"] == "INR"
    assert cleaned["category"] == "Uncategorised"


def test_anomaly_flags_domestic_brand_in_usd():
    rows = [
        {
            "merchant": "Swiggy",
            "amount": Decimal("100.00"),
            "currency": "USD",
            "account_id": "ACC1",
            "is_anomaly": False,
            "anomaly_reason": None,
        }
    ]
    mark_anomalies(rows)
    assert rows[0]["is_anomaly"] is True
    assert "Domestic-only merchant" in rows[0]["anomaly_reason"]
