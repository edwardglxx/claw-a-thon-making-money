from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def load_env() -> None:
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def is_enabled() -> bool:
    load_env()
    return bool(os.getenv("LLM_API_KEY") and os.getenv("LLM_MODEL"))


SYSTEM_PROMPT = """You route Vietnamese cashback/card-management chat into JSON.
Return only valid compact JSON. Do not answer the user directly.

Allowed intents:
- mcc_lookup: ask MCC of a merchant.
- merchant_mcc_excluding: ask other MCCs of a merchant excluding one MCC.
- merchant_addresses: ask address/list stores of a merchant.
- recommend_card: ask which card to use for a planned transaction.
- record_transaction: save a past transaction.
- delete_transaction: delete a transaction.
- update_mcc: add/update merchant MCC or transaction MCC.
- nearby_store_advice: ask stores near an area with suitable MCC/card.
- progress: ask cashback progress of a card.
- coverage: ask whether a card covers a merchant/MCC/category.
- unknown.

Fields:
intent, merchant, amount, card, mcc, exclude_mcc, payment_method, category, date, transaction_id, area.
Use null for unknown. payment_method is online, pos, or null.
Amounts must be integer VND.
"""


def interpret(question: str) -> dict[str, Any] | None:
    if not is_enabled():
        return None

    payload = {
        "model": os.environ["LLM_MODEL"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    base_url = os.getenv("LLM_BASE_URL", "https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1").rstrip("/")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=18) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None

    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
