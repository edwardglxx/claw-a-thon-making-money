from __future__ import annotations

import json
import os
import re
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
TRAINING_EXAMPLES_PATH = BASE_DIR / "data" / "chat_training_examples.json"
_TRAINING_EXAMPLES: list[dict[str, Any]] | None = None


def load_env() -> None:
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def is_enabled() -> bool:
    load_env()
    return bool(os.getenv("LLM_API_KEY") and os.getenv("LLM_MODEL"))


def is_vision_enabled() -> bool:
    load_env()
    return bool(os.getenv("LLM_API_KEY") and (os.getenv("LLM_VISION_MODEL") or os.getenv("LLM_MODEL")))


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
- unsupported_topic: ask about data this app does not currently have, such as FX fees, annual fees, installment interest, credit scoring, credit limit increase policy, or opening a new card.
- unknown.

Fields:
intent, merchant, amount, card, mcc, exclude_mcc, payment_method, category, date, transaction_id, area, topic.
Use null for unknown. payment_method is online, pos, or null.
Amounts must be integer VND.
Training examples, if provided below, are behavior/style guidance only. Sample answers may contain placeholders and are not factual data. Never copy sample answers as facts.
"""


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text.lower()).strip()


def load_training_examples() -> list[dict[str, Any]]:
    global _TRAINING_EXAMPLES
    if _TRAINING_EXAMPLES is not None:
        return _TRAINING_EXAMPLES
    if not TRAINING_EXAMPLES_PATH.exists():
        _TRAINING_EXAMPLES = []
        return _TRAINING_EXAMPLES
    try:
        payload = json.loads(TRAINING_EXAMPLES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _TRAINING_EXAMPLES = []
        return _TRAINING_EXAMPLES
    _TRAINING_EXAMPLES = list(payload.get("examples") or [])
    return _TRAINING_EXAMPLES


def select_training_examples(question: str, limit: int = 8) -> list[dict[str, Any]]:
    query_tokens = {
        token for token in re.findall(r"[a-z0-9]+", normalize_text(question))
        if len(token) >= 3
    }
    if not query_tokens:
        return []
    scored: list[tuple[int, dict[str, Any]]] = []
    for example in load_training_examples():
        haystack = " ".join([
            str(example.get("question") or ""),
            str(example.get("sample_answer") or ""),
            str(example.get("note") or ""),
        ])
        tokens = {
            token for token in re.findall(r"[a-z0-9]+", normalize_text(haystack))
            if len(token) >= 3
        }
        score = len(query_tokens & tokens)
        if score:
            scored.append((score, example))
    scored.sort(key=lambda item: (item[0], int(item[1].get("no") or 0)), reverse=True)
    return [example for _, example in scored[:limit]]


def build_system_prompt(question: str) -> str:
    examples = select_training_examples(question)
    if not examples:
        return SYSTEM_PROMPT
    lines = [
        SYSTEM_PROMPT,
        "",
        "Relevant behavior examples:",
    ]
    for example in examples:
        lines.append(
            json.dumps(
                {
                    "question": example.get("question"),
                    "sample_answer_style": example.get("sample_answer"),
                    "note": example.get("note"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    return "\n".join(lines)


def interpret(question: str) -> dict[str, Any] | None:
    if not is_enabled():
        return None

    payload = {
        "model": os.environ["LLM_MODEL"],
        "messages": [
            {"role": "system", "content": build_system_prompt(question)},
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


IMAGE_TRANSACTION_PROMPT = """Bạn là bộ trích xuất dữ liệu giao dịch từ ảnh/screenshot/hóa đơn/sao kê ngân hàng.
Trả về JSON hợp lệ, ngắn gọn, không giải thích.

Schema:
{
  "transactions": [
    {
      "date": "YYYY-MM-DD hoặc null",
      "merchant": "tên merchant hoặc null",
      "amount": số nguyên VND hoặc null,
      "mcc": "4 chữ số hoặc null",
      "payment_method": "online|pos|null",
      "card_name": "tên thẻ nếu có hoặc null",
      "note": "ghi chú ngắn nếu cần"
    }
  ],
  "confidence": "high|medium|low",
  "raw_text": "text quan trọng đọc được"
}

Quy tắc:
- Chỉ lấy giao dịch chi tiêu/thanh toán, không lấy số dư, hạn mức, OTP, mã tham chiếu nếu không phải số tiền giao dịch.
- Amount là VND integer. Ví dụ 120,000 VNĐ -> 120000.
- Nếu không chắc field nào, để null.
- Nếu ảnh có nhiều giao dịch, trả nhiều dòng.
"""


def extract_transactions_from_image(image_data_url: str, user_note: str = "") -> dict[str, Any] | None:
    if not image_data_url.startswith("data:image/"):
        return None
    if not is_vision_enabled():
        return None
    model = os.getenv("LLM_VISION_MODEL") or os.getenv("LLM_MODEL")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": IMAGE_TRANSACTION_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_note or "Hãy đọc ảnh này và trích xuất giao dịch."},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            },
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
        with urllib.request.urlopen(req, timeout=30) as response:
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


GENERAL_ANSWER_PROMPT = """Bạn là trợ lý tiếng Việt cho app quản lý cashback thẻ tín dụng.
Trả lời ngắn gọn, rõ ràng, dựa trên context JSON do hệ thống cung cấp.

Ràng buộc:
- Không tự bịa số tiền hoàn/cap/rule ngoài context.
- Nếu câu hỏi cần tính cashback cụ thể nhưng thiếu số tiền, merchant, MCC hoặc hình thức thanh toán, hãy hỏi bổ sung đúng field thiếu.
- Nếu câu hỏi thuộc nghiệp vụ đã có trong app, hướng dẫn user dùng /ask, /input_trans, /input_mcc, /input_card, /input_rule khi phù hợp.
- Nếu context không đủ, nói rõ chưa đủ dữ liệu.
"""


def answer_with_context(question: str, context: dict[str, Any]) -> str | None:
    if not is_enabled():
        return None
    payload = {
        "model": os.environ["LLM_MODEL"],
        "messages": [
            {"role": "system", "content": GENERAL_ANSWER_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"question": question, "context": context},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        "temperature": 0.2,
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
    return content or None


CARD_RULE_JSON_PROMPT = """Bạn chuyển mô tả quy định hoàn tiền thẻ tín dụng tiếng Việt thành JSON.
Chỉ trả JSON object hợp lệ, không giải thích.

Schema:
{
  "cashback_rules": [
    {
      "name": "tên rule ngắn",
      "rate": số decimal, ví dụ 0.1 cho 10%,
      "mcc": ["4 chữ số"] hoặc bỏ nếu không có,
      "excluded_mcc": ["4 chữ số"] hoặc bỏ nếu không có,
      "merchants": ["merchant key lowercase"] hoặc bỏ nếu không có,
      "categories": ["shopping|dining|grocery|mobility|transport|travel-agency|fashion|shopee|manual"] hoặc bỏ nếu không chắc,
      "channels": ["online" hoặc "pos"] hoặc bỏ nếu mọi kênh,
      "cap_per_period": số nguyên VND hoặc bỏ nếu không có,
      "max_cashback_per_transaction": số nguyên VND hoặc bỏ nếu không có,
      "low_amount_threshold": số nguyên VND hoặc bỏ nếu không có,
      "low_amount_cashback_cap": số nguyên VND hoặc bỏ nếu không có,
      "cap_key": "khóa nhóm ngắn lowercase" hoặc bỏ nếu không có cap nhóm
    }
  ],
  "period_cap": số nguyên VND hoặc null,
  "min_total_spend": số nguyên VND hoặc null,
  "cashback_round_down_to": số nguyên VND hoặc null
}

Quy tắc:
- Không tự bịa MCC nếu text không nói rõ.
- Nếu có nhiều nhóm/rate khác nhau, tách thành nhiều rule.
- Nếu text nói "tối đa X/tháng" cho toàn thẻ, đặt period_cap.
- Nếu text nói "tối đa X/lĩnh vực" hoặc "mỗi nhóm tối đa X", đặt cap_per_period từng rule.
- Nếu thẻ có period_cap và một nhóm/rule không nêu cap riêng, đặt cap_per_period của nhóm/rule đó bằng period_cap.
- Nếu text nói "mỗi giao dịch tối đa X", đặt max_cashback_per_transaction.
- Merchant key dùng lowercase, không dấu, ví dụ "shopee", "tiktokshop".
"""


def convert_card_rules_text(text: str) -> dict[str, Any] | None:
    if not is_enabled() or not text.strip():
        return None
    payload = {
        "model": os.environ["LLM_MODEL"],
        "messages": [
            {"role": "system", "content": CARD_RULE_JSON_PROMPT},
            {"role": "user", "content": text},
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
        with urllib.request.urlopen(req, timeout=25) as response:
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
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.S)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None
