import json
import re
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any

import requests

from app.config import settings


CATEGORY_OPTIONS = [
    "Food",
    "Shopping",
    "Travel",
    "Transport",
    "Utilities",
    "Cash Withdrawal",
    "Entertainment",
    "Other",
]


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self):
        self.api_key = settings.openrouter_api_key
        self.model = settings.openrouter_model
        self.base_url = settings.openrouter_base_url.rstrip("/")

    def classify_transactions(self, rows: list[dict[str, Any]]) -> tuple[dict[int, str], str, bool]:
        if not rows:
            return {}, "[]", False

        if not self.api_key:
            result = {row["row_number"]: self._guess_category(row) for row in rows}
            return result, json.dumps({"source": "local_fallback", "items": result}), False

        payload = [
            {
                "row_number": row["row_number"],
                "merchant": row["merchant"],
                "amount": str(row["amount"]),
                "currency": row["currency"],
                "notes": row.get("notes") or "",
            }
            for row in rows
        ]
        prompt = (
            "Classify each transaction into exactly one of these categories: "
            f"{', '.join(CATEGORY_OPTIONS)}.\n"
            "Return only JSON in this shape: "
            '{"items":[{"row_number":1,"category":"Food"}]}.\n'
            f"Transactions: {json.dumps(payload)}"
        )
        data, raw = self._call_json(prompt)
        items = data.get("items", [])
        result: dict[int, str] = {}
        for item in items:
            category = item.get("category")
            if category not in CATEGORY_OPTIONS:
                category = "Other"
            result[int(item["row_number"])] = category
        return result, raw, False

    def build_summary(self, context: dict[str, Any]) -> tuple[dict[str, Any], str, bool]:
        if not self.api_key:
            summary = self._fallback_summary(context)
            return summary, json.dumps({"source": "local_fallback", **summary}), False

        prompt = (
            "Create a transaction summary as valid JSON only. Use this exact shape: "
            '{"total_spend_by_currency":{"INR":0,"USD":0},'
            '"top_3_merchants":[{"merchant":"Name","amount":0}],'
            '"anomaly_count":0,"narrative":"3-4 short sentences","risk_level":"low"}.\n'
            'risk_level must be one of "low", "medium", "high".\n'
            "Write the narrative in simple language. Explain what was found, why "
            "the flagged transactions need attention, and the safest next action. "
            "Use only the supplied facts, do not invent a cause, and do not claim "
            "that a transaction is fraud.\n"
            f"Transaction facts: {json.dumps(context, default=str)}"
        )
        data, raw = self._call_json(prompt)
        risk = data.get("risk_level", "low")
        if risk not in {"low", "medium", "high"}:
            risk = "low"
        summary = {
            "total_spend_by_currency": data.get("total_spend_by_currency", {}),
            "top_3_merchants": data.get("top_3_merchants", []),
            "anomaly_count": int(data.get("anomaly_count", 0)),
            "narrative": data.get("narrative", ""),
            "risk_level": risk,
        }
        return summary, raw, False

    def _call_json(self, prompt: str) -> tuple[dict[str, Any], str]:
        last_error = None
        for attempt in range(3):
            try:
                url = f"{self.base_url}/chat/completions"
                response = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=30,
                )
                response.raise_for_status()
                body = response.json()
                text = body["choices"][0]["message"]["content"]
                return self._parse_json_text(text), text
            except Exception as exc:  # requests, malformed JSON, model format drift
                last_error = exc
                time.sleep(2**attempt)
        raise LLMError(str(last_error))

    def _parse_json_text(self, text: str) -> dict[str, Any]:
        cleaned = text.strip()
        cleaned = re.sub(r"^```json\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if not match:
                raise
            return json.loads(match.group(0))

    def _guess_category(self, row: dict[str, Any]) -> str:
        merchant = row["merchant"].lower()
        if any(word in merchant for word in ["swiggy", "zomato", "starbucks"]):
            return "Food"
        if any(word in merchant for word in ["amazon", "flipkart", "myntra"]):
            return "Shopping"
        if any(word in merchant for word in ["irctc", "makemytrip"]):
            return "Travel"
        if any(word in merchant for word in ["ola", "uber"]):
            return "Transport"
        if any(word in merchant for word in ["jio", "electric", "recharge"]):
            return "Utilities"
        if "atm" in merchant:
            return "Cash Withdrawal"
        if any(word in merchant for word in ["netflix", "bookmyshow"]):
            return "Entertainment"
        return "Other"

    def _fallback_summary(self, context: dict[str, Any]) -> dict[str, Any]:
        anomaly_count = int(context["anomaly_count"])
        risk = "high" if anomaly_count >= 5 else "medium" if anomaly_count >= 2 else "low"
        totals = context["total_spend_by_currency"]
        top = context["top_3_merchants"]
        top_name = top[0]["merchant"] if top else "the leading merchant"
        if anomaly_count:
            next_step = (
                "Review each flagged merchant, amount and currency against the source "
                "statement, and contact the bank if a transaction is not recognised."
            )
        else:
            next_step = "No immediate action is required, but normal account monitoring should continue."
        narrative = (
            f"Total spend was INR {totals.get('INR', 0)} and USD {totals.get('USD', 0)}. "
            f"{top_name} had the highest merchant spend. "
            f"The rule-based checks flagged {anomaly_count} transaction(s), giving a {risk} risk level. "
            f"{next_step}"
        )
        return {
            "total_spend_by_currency": totals,
            "top_3_merchants": top,
            "anomaly_count": anomaly_count,
            "narrative": narrative,
            "risk_level": risk,
        }


def money_to_float(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def group_amounts(rows: list[dict[str, Any]], key: str) -> dict[str, float]:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        label = row[key] or "Uncategorised"
        totals[label] += row["amount"]
    return {label: money_to_float(amount) for label, amount in totals.items()}
