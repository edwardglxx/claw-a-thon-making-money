from __future__ import annotations

import datetime as dt
import html
import io
import json
import os
import re
import unicodedata
import zipfile
from pathlib import Path
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse
from llm_router import answer_with_context, convert_card_rules_text, extract_transactions_from_image, is_enabled as llm_text_enabled, is_vision_enabled

from cashback_agent import (
    CashbackError,
    TransactionDraft,
    card_progress,
    check_card_coverage,
    load_cards,
    load_transactions,
    parse_vietnamese_query,
    period_for,
    record_transaction,
    simulate_recommendation,
)
from database import (
    delete_card,
    delete_transaction,
    init_database,
    insert_merchant_mcc,
    list_transactions,
    lookup_merchant_mcc,
    schema_summary,
    search_merchant_mcc,
    update_transaction,
    update_transaction_mcc,
    upsert_card,
    upsert_merchant_mcc,
)


class TextExtractingHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        if tag in {"p", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1
        if tag in {"p", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            value = html.unescape(data).strip()
            if value:
                self.parts.append(value)

    def text(self) -> str:
        raw = " ".join(self.parts)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r"\n\s+", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
PENDING_STATE_PATH = Path(__file__).resolve().parent / "data" / "pending_chat.json"
FACTS_PATH = Path(__file__).resolve().parent / "data" / "facts.json"
PENDING_CHAT_QUESTION: str | None = None
PENDING_AMOUNT_QUESTION: str | None = None
LAST_CHAT_RESULT: dict | None = None
FOLLOWUP_OFFSET = 0
PENDING_DB_ACTION: dict | None = None


def money(value: int | float | None) -> str:
    return f"{int(round(value or 0)):,}".replace(",", ".") + "đ"


def strip_accents(value: str | None) -> str:
    text = str(value or "")
    text = text.replace("đ", "d").replace("Đ", "D")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def normalize_memory_text(value: str | None) -> str:
    return strip_accents(value).lower().strip()


def load_facts() -> list[dict]:
    try:
        if not FACTS_PATH.exists():
            return []
        payload = json.loads(FACTS_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_facts(facts: list[dict]) -> None:
    FACTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FACTS_PATH.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_fact(text: str) -> dict:
    raw = str(text or "").strip()
    if not raw:
        raise CashbackError("Bạn nhập nội dung cần ghi nhớ sau /fact nhé. Ví dụ: /fact 30shine không có thanh toán online")
    normalized = normalize_memory_text(raw)
    fact = {
        "id": "fact-" + dt.datetime.now().strftime("%Y%m%d%H%M%S%f"),
        "text": raw,
        "normalized_text": normalized,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    if any(token in normalized for token in [
        "khong co thanh toan online",
        "khong ho tro online",
        "khong thanh toan online",
        "khong co online",
        "khong co thanh toan truc tuyen",
        "khong ho tro truc tuyen",
        "khong thanh toan truc tuyen",
        "khong co truc tuyen",
    ]):
        before = re.split(r"\bkhong\b", normalized, maxsplit=1)[0].strip(" ,.;:-")
        original_before = re.split(r"\bkhông\b|\bkhong\b", raw, maxsplit=1, flags=re.I)[0].strip(" ,.;:-")
        merchant = original_before or before
        if merchant:
            fact.update({
                "type": "payment_channel_unavailable",
                "merchant": merchant,
                "merchant_norm": normalize_memory_text(merchant),
                "channel": "online",
            })
    return fact


def add_fact(text: str) -> dict:
    fact = parse_fact(text)
    facts = load_facts()
    facts.append(fact)
    save_facts(facts)
    return fact


def related_facts(text: str | None, limit: int = 12) -> list[dict]:
    facts = load_facts()
    norm = normalize_memory_text(text)
    if not norm:
        return facts[-limit:]
    matched = []
    for fact in facts:
        merchant_norm = fact.get("merchant_norm")
        fact_norm = fact.get("normalized_text") or normalize_memory_text(fact.get("text"))
        if merchant_norm and merchant_norm in norm:
            matched.append(fact)
        elif fact_norm and any(token and token in norm for token in fact_norm.split()[:3]):
            matched.append(fact)
    return (matched or facts[-limit:])[-limit:]


def today_iso() -> str:
    return dt.date.today().isoformat()


def display_card_name(card_id: str | None) -> str:
    if not card_id:
        return "chưa có"
    for card in load_cards():
        if card.get("id") == card_id:
            return card.get("name") or card_id
    return card_id


def display_channel(channel: str | None) -> str:
    if not channel:
        return "chưa có"
    return "Online" if channel == "online" else "POS" if channel == "pos" else str(channel)


def display_date(value: str | None) -> str:
    date_value = value or dt.date.today().isoformat()
    try:
        parsed = dt.date.fromisoformat(date_value)
        text = f"{parsed.day}/{parsed.month}/{parsed.year}"
        if parsed == dt.date.today():
            text += " (hôm nay)"
        return text
    except ValueError:
        return str(value or "hôm nay")


def parse_transaction_date_text(text: str | None) -> str | None:
    raw = str(text or "")
    lower = raw.lower()
    if any(token in lower for token in ["hôm qua", "hom qua", "yesterday"]):
        return (dt.date.today() - dt.timedelta(days=1)).isoformat()
    if any(token in lower for token in ["hôm nay", "hom nay", "today"]):
        return dt.date.today().isoformat()
    iso = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", raw)
    if iso:
        return iso.group(1)
    dmy = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b", raw)
    if dmy:
        day, month, year = map(int, dmy.groups())
        try:
            return dt.date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def canonical_merchant_alias(text: str | None) -> str | None:
    folded = strip_accents(str(text or "")).lower()
    alias_patterns = [
        (r"\b(?:di\s+)?(?:be|becar|bebike)\b", "BE GROUP"),
        (r"\b(?:di\s+)?(?:grab|grabcar|grabbike)\b", "Grab"),
        (r"\bbhx\b", "Bách hoá xanh"),
        (r"\bdmx\b", "Điện máy xanh"),
        (r"\btgdd\b", "Thế giới di động"),
        (r"\b(?:711|7\s*eleven)\b", "Seven Eleven"),
        (r"\btch\b", "The Coffee House"),
        (r"\bshopee\s*food\b", "Shopee Food"),
        (r"\bshopee\b", "Shopee"),
    ]
    for pattern, merchant in alias_patterns:
        if re.search(pattern, folded):
            return merchant
    return None


def preferred_mcc_for_merchant(merchant: str | None) -> str | None:
    key = strip_accents(str(merchant or "")).lower().strip()
    if key in {"grab", "be group", "be"}:
        return "4121"
    if key in {"seven eleven", "7 eleven", "711"}:
        return "5499"
    if key in {"shopee", "shopee food", "shopeefood"}:
        return "5262"
    return None


def title_rule_name(value: str | None) -> str:
    text = str(value or "Rule cashback").strip()
    if not text:
        return "Rule cashback"
    match = re.match(r"^(\d+(?:[.,]\d+)?%)\s+(.+)$", text)
    if match:
        rest = match.group(2).strip()
        return f"{match.group(1)} {rest[:1].upper()}{rest[1:]}"
    return text[:1].upper() + text[1:]


def display_cashback_rules(rules: list[dict] | None) -> str:
    if not rules:
        return "- Quy định hoàn tiền: chưa có"
    lines = ["- Quy định hoàn tiền:"]
    for rule in rules:
        details = []
        if rule.get("rate") is not None:
            details.append(f"Tỉ lệ {float(rule['rate']) * 100:g}%")
        if rule.get("mcc"):
            details.append("MCC " + ", ".join(str(x) for x in rule.get("mcc", [])))
        if rule.get("channels"):
            details.append("Kênh " + ", ".join(str(x).title() for x in rule.get("channels", [])))
        if rule.get("merchants"):
            details.append("Merchant " + ", ".join(str(x).title() for x in rule.get("merchants", [])))
        if rule.get("categories"):
            details.append("Nhóm " + ", ".join(display_category(x) for x in rule.get("categories", [])))
        if rule.get("cap_per_period"):
            details.append(f"Hoàn tối đa {money(rule.get('cap_per_period'))}")
        if rule.get("excluded_mcc"):
            details.append("Loại trừ MCC " + ", ".join(str(x) for x in rule.get("excluded_mcc", [])))
        name = title_rule_name(rule.get("name"))
        lines.append(f"  + {name}: " + (" | ".join(details) if details else "chưa có chi tiết"))
    return "\n".join(lines)


def display_category(value: str) -> str:
    labels = {
        "shopee": "Shopee",
        "dining": "ăn uống",
        "grocery": "siêu thị",
        "transport": "gọi xe",
        "shopping": "mua sắm",
    }
    return labels.get(str(value), str(value))


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-") or "card"


def split_command(text: str) -> tuple[str | None, str]:
    match = re.match(r"^/(input_trans|input_mcc|input_rule|delete_trans|ask|clear|fact)\b[:\s-]*(.*)$", str(text or "").strip(), flags=re.I | re.S)
    if not match:
        return None, str(text or "").strip()
    return match.group(1).lower(), match.group(2).strip()


def save_pending_amount_question(question: str | None) -> None:
    try:
        PENDING_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if question:
            PENDING_STATE_PATH.write_text(json.dumps({"amount_question": question}, ensure_ascii=False), encoding="utf-8")
        elif PENDING_STATE_PATH.exists():
            PENDING_STATE_PATH.unlink()
    except OSError:
        pass


def load_pending_amount_question() -> str | None:
    try:
        if not PENDING_STATE_PATH.exists():
            return None
        payload = json.loads(PENDING_STATE_PATH.read_text(encoding="utf-8"))
        return payload.get("amount_question") or None
    except (OSError, json.JSONDecodeError):
        return None


def is_confirm(text: str) -> bool:
    lower = text.lower().strip()
    return lower in {"đúng", "dung", "chính xác", "chinh xac", "xác nhận", "xac nhan", "confirm", "ok", "yes"}


def is_cancel(text: str) -> bool:
    return text.lower().strip() in {"hủy", "huy", "huỷ", "huỷ lệnh", "hủy lệnh", "huy lenh", "cancel", "bỏ qua", "bo qua"}


def says_unknown(text: str) -> bool:
    lower = text.lower()
    return any(token in lower for token in ["không biết", "khong biet", "không rõ", "khong ro", "chưa rõ", "chua ro"])


def parse_number_value(value: str) -> float:
    raw = str(value or "").strip()
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        return float(re.sub(r"[.,]", "", raw))
    return float(raw.replace(",", "."))


def parse_amount(text: str) -> int | None:
    lower = text.lower()
    match = re.search(r"((?:\d{1,3}(?:[.,]\d{3})+)|\d+(?:[.,]\d+)?)\s*(tr|triệu|trieu|m|k|nghìn|nghin|ngàn|ngan|đ|d|vnd|vnđ)", lower)
    if not match:
        context_match = re.search(r"(?:số tiền|so tien|amount|giá|gia|giá trị|gia tri)\s*(?:là|la|:)?\s*((?:\d{1,3}(?:[.,]\d{3})+)|\d{5,})", lower)
        if context_match:
            return int(re.sub(r"[.,]", "", context_match.group(1)))
        plain_match = re.search(r"\b((?:\d{1,3}(?:[.,]\d{3})+)|\d{5,})\b", lower)
        if plain_match:
            return int(re.sub(r"[.,]", "", plain_match.group(1)))
    if not match:
        return None
    number = parse_number_value(match.group(1))
    unit = match.group(2) if len(match.groups()) > 1 and match.group(2) else ""
    if unit in {"tr", "triệu", "trieu", "m"}:
        return int(number * 1_000_000)
    if unit in {"k", "nghìn", "nghin", "ngàn", "ngan"}:
        return int(number * 1_000)
    return int(number)


def parse_amount_after(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text, flags=re.I)
    if not match:
        return None
    return parse_amount(match.group(1))


def parse_close_days(text: str) -> tuple[int | None, int | None]:
    both = re.search(
        r"(?:chốt sao kê|chot sao ke|sao kê|sao ke)\s+(?:và|va|,)\s+(?:ngày\s+|ngay\s+)?(?:chốt hoàn tiền|chot hoan tien|chốt hoàn|chot hoan|hoàn tiền|hoan tien)\s+(?:là|la)?\s*(?:ngày|ngay)?\s*(\d{1,2})",
        text,
        flags=re.I,
    )
    if both:
        day = int(both.group(1))
        return day, day
    close = re.search(r"(?:chốt sao kê|chot sao ke|sao kê|sao ke)\s*(?:là|la)?\s*(?:ngày|ngay)?\s*(\d{1,2})", text, flags=re.I)
    cashback_close = re.search(r"(?:chốt hoàn tiền|chot hoan tien|chốt hoàn|chot hoan|hoàn tiền|hoan tien)\s*(?:là|la)?\s*(?:ngày|ngay)?\s*(\d{1,2})", text, flags=re.I)
    return int(close.group(1)) if close else None, int(cashback_close.group(1)) if cashback_close else None


def parse_cashback_rule(text: str) -> dict | None:
    lower = text.lower()
    rate_match = re.search(r"(?:tỉ lệ|ti le|tỷ lệ|ty le|hoàn|hoan)?\s*(\d+(?:[.,]\d+)?)\s*%", text, flags=re.I)
    if not rate_match:
        return None
    mcc_values = re.findall(r"\bmcc\s*([0-9\s,;/]+)|\((?:\s*mcc)?\s*([0-9\s,;/]+)\)", text, flags=re.I)
    mcc_list: list[str] = []
    for groups in mcc_values:
        for value in groups:
            for code in re.findall(r"\b\d{4}\b", value or ""):
                if code not in mcc_list:
                    mcc_list.append(code)
    excluded = re.findall(r"(?:trừ|tru|ngoại trừ|ngoai tru)\s*(?:mcc)?\s*(\d{4})", text, flags=re.I)
    cap = parse_amount_after(r"(?:hoàn\s+)?(?:tối đa|max|cap)\s*(\d+(?:[.,]\d+)?\s*(?:tr|triệu|trieu|m|k|nghìn|nghin|ngàn|ngan|đ|d|vnd|vnđ)?)", text)
    category_match = re.search(r"(?:danh mục|danh muc|lĩnh vực|linh vuc)\s+(.+?)(?:\s*\(|,|;|\.|$)", text, flags=re.I)
    category = category_match.group(1).strip(" ?.,:") if category_match else None
    rate_percent = float(rate_match.group(1).replace(",", "."))
    name_parts = [f"{rate_percent:g}%"]
    if category:
        name_parts.append(category[:60])
    elif mcc_list:
        name_parts.append("MCC " + ", ".join(mcc_list))
    rule = {
        "name": " ".join(name_parts),
        "rate": rate_percent / 100,
    }
    if mcc_list:
        rule["mcc"] = [x for x in mcc_list if x not in excluded]
    if excluded:
        rule["excluded_mcc"] = excluded
    if cap:
        rule["cap_per_period"] = cap
        rule["cap_key"] = slugify(category or "manual")
    if "online" in lower or "trực tuyến" in lower or "truc tuyen" in lower:
        rule["channels"] = ["online"]
    if "pos" in lower or "offline" in lower or "quẹt" in lower or "quet" in lower:
        rule["channels"] = ["pos"]
    return rule


def category_aliases_from_text(text: str) -> list[str]:
    lower = text.lower().replace("shopee food", "shopeefood")
    mapping = [
        ("shopee", ["shopee", "shopee food", "shopeefood"]),
        ("dining", ["ăn uống", "an uong", "nhà hàng", "nha hang", "restaurant"]),
        ("grocery", ["siêu thị", "sieu thi", "supermarket"]),
        ("transport", ["gọi xe", "goi xe", "di chuyển", "di chuyen", "taxi"]),
        ("shopping", ["mua sắm", "mua sam", "shopping"]),
    ]
    categories: list[str] = []
    for category, aliases in mapping:
        if any(alias in lower for alias in aliases) and category not in categories:
            categories.append(category)
    return categories


def merchant_aliases_from_text(text: str) -> list[str]:
    lower = text.lower()
    merchants: list[str] = []
    if "tiktokshop" in lower or "tiktok shop" in lower:
        merchants.append("tiktokshop")
    if "shopee" in lower:
        merchants.append("shopee")
    if "shopee food" in lower or "shopeefood" in lower:
        merchants.append("shopee food")
    return merchants


def rule_label_for(merchants: list[str], categories: list[str]) -> str:
    labels: list[str] = []
    labels.extend(str(m).title() for m in merchants)
    labels.extend(display_category(category) for category in categories)
    return ", ".join(dict.fromkeys(labels))


def parse_cashback_rules(text: str) -> list[dict]:
    matches = list(re.finditer(r"(?:hoàn|hoan)\s*(\d+(?:[.,]\d+)?)\s*%", text, flags=re.I))
    if len(matches) <= 1:
        rule = parse_cashback_rule(text)
        if not rule:
            return []
        categories = category_aliases_from_text(text)
        merchants = merchant_aliases_from_text(text)
        if categories and not rule.get("categories"):
            rule["categories"] = categories
        if merchants:
            rule["merchants"] = merchants
        if categories and not rule.get("cap_key"):
            rule["cap_key"] = categories[0]
        label = rule_label_for(merchants, categories)
        if label:
            rule["name"] = f"{float(rule['rate']) * 100:g}% {label}"
        return [rule]

    rules: list[dict] = []
    shared_cap = parse_amount_after(
        r"(?:hoàn\s+)?(?:tối đa|max|cap)\s*(\d+(?:[.,]\d+)?\s*(?:tr|triệu|trieu|m|k|nghìn|nghin|ngàn|ngan|đ|d|vnd|vnđ)?)",
        text,
    )
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segment = text[match.start():end].strip(" .;,")
        rate_percent = float(match.group(1).replace(",", "."))
        categories = category_aliases_from_text(segment)
        merchants = merchant_aliases_from_text(segment)
        cap = parse_amount_after(
            r"(?:hoàn\s+)?(?:tối đa|max|cap)\s*(\d+(?:[.,]\d+)?\s*(?:tr|triệu|trieu|m|k|nghìn|nghin|ngàn|ngan|đ|d|vnd|vnđ)?)",
            segment,
        )
        if cap is None and index == 0:
            cap = shared_cap
        label = rule_label_for(merchants, categories)
        rule = {
            "name": f"{rate_percent:g}% " + (label if label else "cashback"),
            "rate": rate_percent / 100,
        }
        if cap:
            rule["cap_per_period"] = cap
            rule["cap_key"] = categories[0] if categories else slugify(rule["name"])
        if categories:
            rule["categories"] = categories
        if merchants:
            rule["merchants"] = merchants
        mcc_values = re.findall(r"\bmcc\s*([0-9\s,;/]+)|\((?:\s*mcc)?\s*([0-9\s,;/]+)\)", segment, flags=re.I)
        mcc_list: list[str] = []
        for groups in mcc_values:
            for value in groups:
                for code in re.findall(r"\b\d{4}\b", value or ""):
                    if code not in mcc_list:
                        mcc_list.append(code)
        if mcc_list:
            rule["mcc"] = mcc_list
        rules.append(rule)
    return rules


def find_card_id(text: str) -> str | None:
    lower = text.lower()
    for card in load_cards():
        aliases = [card["id"], card["name"], *card.get("aliases", [])]
        if any(str(alias).lower() in lower for alias in aliases):
            return card["id"]
    fallback_aliases = {
        "sacombank": "sacombank-platinum-cashback",
        "sea bank": "seabank-seaeasy",
        "seabank": "seabank-seaeasy",
        "cake": "cake-cashback",
        "mdigi": "msb-mdigi",
        "m digi": "msb-mdigi",
    }
    for alias, card_id in fallback_aliases.items():
        if alias in lower:
            return card_id
    return None


def extract_card_name_from_text(text: str) -> str | None:
    match = re.search(r"(?:thẻ|the|card)\s+(.+?)(?:\s+(?:có|co|hoàn|hoan|loại trừ|loai tru|mỗi|moi)|,|;|$)", text, flags=re.I)
    if not match:
        return None
    return match.group(1).strip(" ?.,:")


def parse_input_transaction(text: str) -> dict:
    lower = text.lower()
    merchant_text = None
    explicit_merchant = re.search(
        r"(?:merchant|cửa hàng|cua hang)\s*:?\s*([^,.;?\n]+)",
        text,
        flags=re.I,
    )
    if explicit_merchant:
        merchant_text = explicit_merchant.group(1).strip(" ?.,:")
    location_match = re.search(r"(?:tại|tai|ở|o)\s+(.+?)(?:,|\.|\?|$)", text, flags=re.I)
    if not merchant_text and location_match:
        merchant_text = location_match.group(1).strip(" ?.,:")
    purchase_match = re.search(r"(?:mua|chi|tiêu|tieu)\s+(.+?)(?:,|\.|\?|$|\s+\d+(?:[.,]\d+)?\s*(?:tr|triệu|trieu|m|k|nghìn|nghin|ngàn|ngan|đ|d|vnd|vnđ)\b)", text, flags=re.I)
    if not merchant_text and purchase_match:
        merchant_text = purchase_match.group(1).strip(" ?.,:")
    if not merchant_text and any(token in lower for token in ["giao dịch", "giao dich", "merchant", "cửa hàng", "cua hang", "tại ", "tai ", "ở ", "o ", "quẹt", "quet", "mua "]):
        merchant_source = re.sub(r"^(thêm|them|nhập|nhap|ghi)\s+giao\s+dịch\s*:?", "", text, flags=re.I).strip()
        merchant_source = re.sub(r"^(vừa|vua|mới|moi|đã|da)?\s*(quẹt|quet|tiêu|tieu|mua)\s*", "", merchant_source, flags=re.I).strip()
        merchant_source = re.sub(r"^\d+(?:[.,]\d+)?\s*(?:tr|triệu|trieu|m|k|nghìn|nghin|ngàn|ngan|đ|d|vnd|vnđ)\s*", "", merchant_source, flags=re.I).strip()
        merchant_source = re.sub(r"^(merchant|cửa hàng|cua hang|tại|tai)\s*:?", "", merchant_source, flags=re.I).strip()
        merchant_text = re.split(
            r"\s*,\s*|\s+mcc\s*\d{4}|\s+(?:online|pos|offline|trực tuyến|truc tuyen|quẹt|quet)\b|\s+\d+(?:[.,]\d+)?\s*(?:tr|triệu|trieu|m|k|nghìn|nghin|ngàn|ngan|đ|d|vnd|vnđ)\b|\s+(?:vào|vao)\s+(?:thẻ|the)\b",
            merchant_source,
            maxsplit=1,
        )[0].strip(" ?.,:")
    if merchant_text:
        merchant_text = canonical_merchant_alias(merchant_text) or merchant_text
    mcc = re.search(r"\bmcc\s*(\d{4})\b", lower)
    date = parse_transaction_date_text(text)
    channel = "online" if any(x in lower for x in ["online", "trực tuyến", "truc tuyen"]) else "pos" if any(x in lower for x in ["pos", "offline", "quẹt", "quet"]) else None
    if not channel and strip_accents(str(merchant_text or "")).lower().strip() in {"shopee", "shopee food", "shopeefood"}:
        channel = "online"
    mcc_value = mcc.group(1) if mcc else None
    category = None
    if not mcc_value and merchant_text:
        mcc_match = lookup_merchant_mcc(merchant_text, channel)
        if not mcc_match:
            search_terms = [merchant_text, strip_accents(merchant_text)]
            for term in search_terms:
                rows = [
                    row for row in search_merchant_mcc(term, limit=20)
                    if re.fullmatch(r"\d{4}", str(row.get("mcc") or ""))
                    and (not channel or row.get("payment_method") in {channel, "any"})
                ]
                if rows:
                    mcc_match = rows[0]
                    break
        preferred_mcc = preferred_mcc_for_merchant(merchant_text)
        if preferred_mcc and (not mcc_match or str(mcc_match.get("mcc") or "") != preferred_mcc):
            rows = [
                row for row in search_merchant_mcc(merchant_text, limit=100)
                if str(row.get("mcc") or "") == preferred_mcc
            ]
            if rows:
                mcc_match = rows[0]
            else:
                mcc_match = {"mcc": preferred_mcc, "category": None}
        if mcc_match:
            mcc_value = mcc_match.get("mcc")
            category = mcc_match.get("category")
    payload = {
        "card_id": find_card_id(text),
        "amount": parse_amount(text),
        "merchant": merchant_text or None,
        "mcc": mcc_value,
        "mcc_source": "database_suggestion" if mcc_value and not mcc else None,
        "channel": channel,
        "category": category,
        "date": date,
        "note": "created by chatbot",
    }
    return {"type": "transaction", "payload": payload, "required": ["card_id", "amount", "merchant", "channel"]}


def transaction_action_from_image_payload(extracted: dict, fallback_text: str = "") -> dict:
    transactions = extracted.get("transactions") if isinstance(extracted, dict) else None
    if not transactions:
        raise CashbackError("Mình chưa đọc được giao dịch từ ảnh. Bạn thử paste ảnh rõ hơn hoặc nhập giao dịch bằng /input_trans nhé.")
    txn = next((row for row in transactions if isinstance(row, dict)), None)
    if not txn:
        raise CashbackError("Mình chưa đọc được giao dịch từ ảnh. Bạn thử paste ảnh rõ hơn hoặc nhập giao dịch bằng /input_trans nhé.")
    card_name = txn.get("card_name") or ""
    merchant = txn.get("merchant") or None
    mcc = str(txn.get("mcc") or "").strip() or None
    channel = txn.get("payment_method") if txn.get("payment_method") in {"online", "pos"} else None
    amount = txn.get("amount")
    try:
        amount = int(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount = parse_amount(str(amount))
    if merchant and not mcc:
        match = lookup_merchant_mcc(str(merchant), channel)
        if match:
            mcc = match.get("mcc")
    payload = {
        "card_id": find_card_id(str(card_name) or fallback_text),
        "amount": amount,
        "merchant": merchant,
        "mcc": mcc,
        "mcc_source": "database_suggestion" if mcc and not txn.get("mcc") else None,
        "channel": channel,
        "date": txn.get("date") or today_iso(),
        "category": txn.get("category"),
        "note": "import từ ảnh" + (f"; {txn.get('note')}" if txn.get("note") else ""),
    }
    return {
        "type": "transaction",
        "payload": payload,
        "required": ["card_id", "amount", "merchant", "channel"],
        "image_extract": {
            "confidence": extracted.get("confidence"),
            "raw_text": extracted.get("raw_text"),
            "count": len(transactions),
        },
    }


def llm_context_summary() -> dict:
    cards = []
    for card in load_cards():
        cards.append({
            "id": card.get("id"),
            "name": card.get("name"),
            "min_total_spend": card.get("min_total_spend"),
            "period_cap": card.get("period_cap"),
            "cashback_round_down_to": card.get("cashback_round_down_to"),
            "cashback_rules": [
                {
                    "name": rule.get("name"),
                    "rate": rule.get("rate"),
                    "mcc": rule.get("mcc"),
                    "categories": rule.get("categories"),
                    "merchants": rule.get("merchants"),
                    "channels": rule.get("channels"),
                    "cap_per_period": rule.get("cap_per_period"),
                    "max_cashback_per_transaction": rule.get("max_cashback_per_transaction"),
                    "excluded_mcc": rule.get("excluded_mcc"),
                }
                for rule in card.get("cashback_rules", [])
            ],
        })
    return {
        "cards": cards,
        "facts": related_facts(None),
        "supported_commands": ["/ask", "/input_trans", "/fact", "/clear"],
        "principle": "LLM chỉ giải thích/đọc hiểu; tính cashback chính xác do rule engine xử lý.",
    }


def is_natural_transaction_input(text: str) -> bool:
    lower = str(text or "").lower()
    return any(token in lower for token in [
        "vừa quẹt", "vua quet", "mới quẹt", "moi quet", "đã quẹt", "da quet",
        "vừa tiêu", "vua tieu", "đã tiêu", "da tieu", "ghi nhận giúp", "ghi nhan giup",
    ]) and parse_amount(lower) is not None


def parse_input_mcc(text: str) -> dict:
    lower = text.lower()
    mcc = re.search(r"\bmcc\s*(?:là|la|thành|thanh)?\s*(\d{4})\b|(?:là|la|thành|thanh)\s*(\d{4})", lower)
    if not mcc:
        mcc = re.search(r"^\s*(\d{4})\s*$", lower)
    merchant_source = re.sub(r"^(thêm|them|nhập|nhap|cập nhật|cap nhat|update)\s+mcc\s*", "", text, flags=re.I).strip()
    natural_match = re.search(
        r"^(.+?)\s+(?:có|co)?\s*(?:mã|ma)?\s*mcc\s*(?:là|la)?\s*\d{4}\b",
        merchant_source,
        flags=re.I,
    )
    if natural_match:
        merchant = natural_match.group(1).strip(" ?.,:")
    else:
        merchant = re.split(
            r"\s+(?:có|co)?\s*(?:mã|ma)?\s*(?:mcc|là|la|thành|thanh)\s*\d{4}|\s+(?:online|pos|any|offline)\b",
            merchant_source,
            maxsplit=1,
            flags=re.I,
        )[0].strip(" ?.,:")
    if re.fullmatch(r"\d{4}", merchant or ""):
        merchant = None
    address = None
    if merchant:
        location_match = re.search(r"^(.+?)\s+(?:ở|o|tại|tai)\s+(.+)$", merchant, flags=re.I)
        if location_match:
            merchant = location_match.group(1).strip(" ?.,:")
            address = location_match.group(2).strip(" ?.,:")
        if merchant.lower() == "grab" and not address:
            address = "Việt Nam"
    method = "online" if "online" in lower else "pos" if any(x in lower for x in ["pos", "offline"]) else "any"
    payload = {
        "merchant_name": merchant or None,
        "mcc": next((x for x in (mcc.groups() if mcc else []) if x), None),
        "payment_method": method,
        "address": address,
        "note": "updated by chatbot",
        "upsert": True,
    }
    return {"type": "mcc", "payload": payload, "required": ["merchant_name", "mcc"]}


def parse_input_card(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("{"):
        payload = json.loads(stripped)
    else:
        name_match = re.search(r"(?:thẻ|the|card)\s+(.+?)(?:,|;|$)", text, flags=re.I)
        name = name_match.group(1).strip() if name_match else None
        statement_close_day, cashback_close_day = parse_close_days(text)
        credit_limit = parse_amount_after(r"(?:hạn mức|han muc|limit)\s*(?:là|la|:)?\s*(\d+(?:[.,]\d+)?\s*(?:tr|triệu|trieu|m|k|nghìn|nghin|ngàn|ngan|đ|d|vnd|vnđ)?)", text)
        min_total_spend = parse_amount_after(r"(?:chi tiêu tối thiểu|chi tieu toi thieu|tổng chi tiêu tối thiểu|tong chi tieu toi thieu)\s*(?:là|la|:|mới hoàn tiền|moi hoan tien)?\s*(\d+(?:[.,]\d+)?\s*(?:tr|triệu|trieu|m|k|nghìn|nghin|ngàn|ngan|đ|d|vnd|vnđ)?)", text)
        if re.search(r"(?:không|khong)\s+(?:quy định|quy dinh|có|co)\s+(?:chi tiêu tối thiểu|chi tieu toi thieu)", text, flags=re.I):
            min_total_spend = 0
        rule_text = text
        rule_match = re.search(r"(?:rule|quy định hoàn tiền|quy dinh hoan tien|cơ chế hoàn tiền|co che hoan tien|điều kiện hoàn tiền|dieu kien hoan tien)\s*:?\s*(.+)$", text, flags=re.I)
        if rule_match:
            rule_text = rule_match.group(1).strip()
        rules = parse_cashback_rules(rule_text)
        period_cap_values = [int(rule.get("cap_per_period") or 0) for rule in rules if rule.get("cap_per_period")]
        period_cap = max(period_cap_values) if period_cap_values else None
        payload = {
            "id": slugify(name or "") if name else None,
            "name": name,
            "statement_type": "statement_cycle",
            "statement_close_day": statement_close_day,
            "cashback_close_day": cashback_close_day,
            "credit_limit": credit_limit,
            "min_total_spend": min_total_spend if min_total_spend is not None else 0,
            "period_cap": period_cap,
            "cashback_round_down_to": 0,
            "cashback_rules": rules,
            "aliases": [],
        }
    if not payload.get("id") and payload.get("name"):
        payload["id"] = slugify(payload["name"])
    return {"type": "card", "payload": payload, "required": ["id", "name", "statement_close_day", "cashback_close_day", "credit_limit", "cashback_rules"]}


def parse_input_rule(text: str) -> dict:
    lower = text.lower()
    card_id = find_card_id(text)
    card_name = display_card_name(card_id) if card_id else extract_card_name_from_text(text)
    mcc_values = re.findall(r"\bmcc\s*(\d{4})\b|\b(\d{4})\b", lower)
    mcc_list = [a or b for a, b in mcc_values]
    is_exclusion = any(token in lower for token in ["loại trừ", "loai tru", "ngoại trừ", "ngoai tru", "trừ ", "tru "])
    per_txn_cap = None
    if any(token in lower for token in ["mỗi giao dịch", "moi giao dich", "giao dịch chỉ hoàn", "giao dich chi hoan"]):
        per_txn_cap = parse_amount_after(
            r"(?:mỗi giao dịch|moi giao dich|giao dịch|giao dich).*?(?:hoàn tối đa|hoan toi da|tối đa|toi da|max|cap)\s*(\d+(?:[.,]\d+)?\s*(?:tr|triệu|trieu|m|k|nghìn|nghin|ngàn|ngan|đ|d|vnd|vnđ)?)",
            text,
        )
    operation = "exclude_mcc" if is_exclusion else "update_per_transaction_cap" if per_txn_cap else "add_rule"
    excluded_categories = []
    if any(token in lower for token in ["vé máy bay", "ve may bay", "đại lý du lịch", "dai ly du lich"]):
        excluded_categories.append("travel-agency")
    description = re.sub(r"^(thêm|them|cập nhật|cap nhat)?\s*(điều khoản|dieu khoan|quy định|quy dinh|rule)?\s*", "", text, flags=re.I).strip()
    cashback_rules = parse_cashback_rules(text)
    payload = {
        "card_id": card_id,
        "card_name": card_name,
        "operation": operation,
        "mcc": mcc_list,
        "excluded_categories": excluded_categories,
        "max_cashback_per_transaction": per_txn_cap,
        "cashback_rules": cashback_rules,
        "description": description,
    }
    required = ["card_id", "operation"]
    if operation == "update_per_transaction_cap":
        required.append("max_cashback_per_transaction")
    return {"type": "rule", "payload": payload, "required": required}


def validate_card_payload(payload: dict) -> dict:
    payload = dict(payload or {})
    if not payload.get("name"):
        raise CashbackError("Tên thẻ là thông tin bắt buộc.")
    if not payload.get("id"):
        payload["id"] = slugify(payload["name"])
    statement = payload.get("statement") or {}
    statement_day = payload.get("statement_close_day") or statement.get("close_day")
    cashback_day = payload.get("cashback_close_day")
    for label, value in [("Ngày chốt sao kê", statement_day), ("Ngày chốt hoàn tiền", cashback_day)]:
        try:
            day = int(value)
        except (TypeError, ValueError):
            raise CashbackError(f"{label} phải là số từ 1 đến 31.")
        if day < 1 or day > 31:
            raise CashbackError(f"{label} phải là số từ 1 đến 31.")
    payload["statement"] = {
        "type": statement.get("type") or payload.get("statement_type") or "statement_cycle",
        "close_day": int(statement_day),
    }
    payload["cashback_close_day"] = int(cashback_day)
    rules = payload.get("cashback_rules")
    if not isinstance(rules, list) or not rules:
        raise CashbackError("Quy định hoàn tiền phải có ít nhất 1 rule.")
    for idx, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            raise CashbackError(f"Rule #{idx} phải là object JSON.")
        if not rule.get("name"):
            raise CashbackError(f"Rule #{idx} thiếu name.")
        try:
            rate = float(rule.get("rate"))
        except (TypeError, ValueError):
            raise CashbackError(f"Rule #{idx} thiếu rate hợp lệ.")
        if rate <= 0:
            raise CashbackError(f"Rule #{idx} thiếu rate hợp lệ.")
        rule["rate"] = rate
    payload["credit_limit"] = int(payload.get("credit_limit") or 0)
    payload["min_total_spend"] = int(payload.get("min_total_spend") or 0)
    payload["cashback_round_down_to"] = int(payload.get("cashback_round_down_to") or 0)
    if payload.get("period_cap") in ("", None):
        payload["period_cap"] = None
    else:
        payload["period_cap"] = int(payload.get("period_cap") or 0)
    for rule in payload["cashback_rules"]:
        rule["name"] = title_rule_name(rule.get("name"))
        if payload["period_cap"] and not rule.get("cap_per_period"):
            rule["cap_per_period"] = int(payload["period_cap"])
            rule["cap_key"] = rule.get("cap_key") or slugify(rule.get("name") or "rule")
    payload["aliases"] = payload.get("aliases") or []
    return payload


def normalize_llm_card_rules(payload: dict | None, fallback_text: str) -> dict:
    source = "llm"
    parsed = payload or {}
    rules = parsed.get("cashback_rules")
    if not isinstance(rules, list) or not rules:
        vib_rules = parse_vib_super_card_rules(fallback_text)
        source = "fallback_parser"
        min_total_spend = parse_amount_after(
            r"(?:chi tiêu tối thiểu|chi tieu toi thieu|tổng chi tiêu tối thiểu|tong chi tieu toi thieu)\s*(?:là|la|:)?\s*(\d+(?:[.,]\d+)?\s*(?:tr|triệu|trieu|m|k|nghìn|nghin|ngàn|ngan|đ|d|vnd|vnđ)?)",
            fallback_text,
        )
        round_down_to = parse_amount_after(
            r"(?:làm tròn|lam tron).*?(?:bội số|boi so)\s*(\d+(?:[.,]\d+)?\s*(?:tr|triệu|trieu|m|k|nghìn|nghin|ngàn|ngan|đ|d|vnd|vnđ)?)",
            fallback_text,
        )
        rules = vib_rules or parse_cashback_rules(fallback_text)
        parsed = {
            "cashback_rules": rules,
            "period_cap": 1_000_000 if vib_rules else None,
            "min_total_spend": min_total_spend,
            "cashback_round_down_to": round_down_to,
        }
    clean_rules = []
    for index, rule in enumerate(rules or [], start=1):
        if not isinstance(rule, dict):
            continue
        clean = dict(rule)
        try:
            clean["rate"] = float(clean.get("rate"))
        except (TypeError, ValueError):
            continue
        if clean["rate"] <= 0:
            continue
        clean["name"] = title_rule_name(str(clean.get("name") or f"{clean['rate'] * 100:g}% rule {index}"))
        for key in ["mcc", "excluded_mcc", "merchants", "categories", "channels"]:
            if clean.get(key) is None:
                clean.pop(key, None)
            elif not isinstance(clean.get(key), list):
                clean[key] = [clean[key]]
        for key in ["cap_per_period", "max_cashback_per_transaction", "low_amount_threshold", "low_amount_cashback_cap"]:
            if clean.get(key) in ("", None):
                clean.pop(key, None)
            else:
                clean[key] = int(clean[key])
        clean_rules.append(clean)
    period_cap = parsed.get("period_cap")
    if period_cap not in ("", None):
        period_cap = int(period_cap)
        for clean in clean_rules:
            if not clean.get("cap_per_period"):
                clean["cap_per_period"] = period_cap
                clean["cap_key"] = clean.get("cap_key") or slugify(clean.get("name") or "rule")
    else:
        period_cap = None
    return {
        "cashback_rules": clean_rules,
        "period_cap": period_cap,
        "min_total_spend": parsed.get("min_total_spend"),
        "cashback_round_down_to": parsed.get("cashback_round_down_to"),
        "source": source,
    }


def parse_vib_super_card_rules(text: str) -> list[dict] | None:
    normalized = strip_accents(text).lower()
    if "vib super card" not in normalized or "diem thuong" not in normalized:
        return None

    def mccs_between(start_label: str, end_labels: list[str]) -> list[str]:
        start_key = strip_accents(start_label).lower()
        starts = [match.start() for match in re.finditer(re.escape(start_key), normalized)]
        best: list[str] = []
        for start in starts:
            end = len(text)
            for label in end_labels:
                pos = normalized.find(strip_accents(label).lower(), start + len(start_key))
                if pos > start:
                    end = min(end, pos)
            segment = text[start:end]
            values = re.findall(r"\b\d{4}\b", segment)
            result = []
            for value in values:
                if value not in result:
                    result.append(value)
            if len(result) > len(best):
                best = result
        return best

    dining_mcc = mccs_between("Ẩm thực", ["Du lịch", "Mua sắm", "Giao dịch trực tuyến", "Giao dịch nước ngoài"])
    travel_mcc = mccs_between("Du lịch", ["Mua sắm", "Giao dịch trực tuyến", "Giao dịch nước ngoài"])
    shopping_mcc = mccs_between("Mua sắm", ["Giao dịch trực tuyến", "Giao dịch nước ngoài"])
    per_category_cap = 500_000
    rules = [
        {
            "name": "15% Giao dịch nước ngoài",
            "rate": 0.15,
            "categories": ["foreign"],
            "channels": ["pos"],
            "cap_per_period": per_category_cap,
            "cap_key": "foreign",
        },
        {
            "name": "10% Ẩm thực",
            "rate": 0.10,
            "mcc": dining_mcc or ["5814", "5813", "5812", "5811"],
            "cap_per_period": per_category_cap,
            "cap_key": "dining",
        },
        {
            "name": "10% Du lịch",
            "rate": 0.10,
            "mcc": travel_mcc,
            "cap_per_period": per_category_cap,
            "cap_key": "travel",
        },
        {
            "name": "10% Mua sắm",
            "rate": 0.10,
            "mcc": shopping_mcc,
            "cap_per_period": per_category_cap,
            "cap_key": "shopping",
        },
        {
            "name": "5% Giao dịch trực tuyến",
            "rate": 0.05,
            "channels": ["online"],
            "excluded_mcc": ["6300", "7399"],
            "cap_per_period": per_category_cap,
            "cap_key": "online",
        },
        {
            "name": "0.1% Giao dịch còn lại",
            "rate": 0.001,
            "cap_per_period": 1_000_000,
            "cap_key": "other",
        },
    ]
    return [rule for rule in rules if rule.get("rate") and (rule.get("mcc") != [])]


def extract_card_rule_text_from_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CashbackError("Link quy định ngân hàng phải là URL http/https hợp lệ.")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 CashbackAgent/1.0",
            "Accept": "text/html,application/xhtml+xml,text/plain,*/*;q=0.8",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            raw = response.read(1_500_000)
    except Exception as exc:
        raise CashbackError("Không đọc được link ngân hàng. Bạn thử copy nội dung quy định vào ô text nhé.") from exc
    if "pdf" in content_type or raw.startswith(b"%PDF"):
        raise CashbackError("Link này là PDF nên app chưa bóc text trực tiếp được. Bạn copy phần quy định hoàn tiền trong PDF vào ô text rồi chuyển bằng LLM nhé.")
    encoding = "utf-8"
    match = re.search(r"charset=([\w.-]+)", content_type)
    if match:
        encoding = match.group(1)
    text = raw.decode(encoding, errors="ignore")
    challenge_signals = [
        "challenge validation",
        "tạm thời không xử lý được yêu cầu",
        "tam thoi khong xu ly duoc yeu cau",
        "sec-container",
        "challenge content",
    ]
    normalized_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    if any(signal in text.lower() or signal in normalized_text for signal in challenge_signals):
        raise CashbackError("Website ngân hàng đang chặn request tự động nên app chưa đọc được nội dung. Bạn copy riêng phần quy định hoàn tiền vào ô text rồi chuyển bằng LLM nhé.")
    if "<html" in text[:1000].lower() or "<body" in text[:5000].lower():
        parser = TextExtractingHTMLParser()
        parser.feed(text)
        text = parser.text()
    else:
        text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 120:
        raise CashbackError("Không lấy được đủ nội dung từ link. Bạn paste phần quy định hoàn tiền vào ô text nhé.")
    return text[:20000]


def extract_card_rule_text_from_upload(filename: str, content: bytes) -> str:
    name = str(filename or "").lower()
    if not content:
        return ""
    if len(content) > 8_000_000:
        raise CashbackError("File quy định quá lớn. Bạn dùng file dưới 8MB nhé.")
    if name.endswith((".txt", ".csv", ".md", ".html", ".htm")):
        text = content.decode("utf-8", errors="ignore")
        if name.endswith((".html", ".htm")):
            parser = TextExtractingHTMLParser()
            parser.feed(text)
            text = parser.text()
    elif name.endswith(".docx"):
        text = extract_docx_text(content)
    elif name.endswith(".pdf"):
        text = extract_pdf_text(content)
    else:
        raise CashbackError("File quy định chỉ hỗ trợ .txt, .pdf hoặc .docx.")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    if len(text) < 80:
        raise CashbackError("Không bóc được đủ nội dung từ file. Bạn thử copy phần quy định vào ô text nhé.")
    return text[:30000]


def extract_docx_text(content: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as docx:
            xml_data = docx.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise CashbackError("Không đọc được file Word. Bạn kiểm tra file .docx hoặc paste nội dung vào ô text nhé.") from exc
    root = ET.fromstring(xml_data)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = []
    for paragraph in root.findall(".//w:p", namespace):
        parts = [node.text or "" for node in paragraph.findall(".//w:t", namespace)]
        value = "".join(parts).strip()
        if value:
            paragraphs.append(value)
    for table_index, table in enumerate(root.findall(".//w:tbl", namespace), start=1):
        rows = []
        for row in table.findall(".//w:tr", namespace):
            cells = []
            for cell in row.findall("./w:tc", namespace):
                cell_parts = [node.text or "" for node in cell.findall(".//w:t", namespace)]
                value = " ".join(part.strip() for part in cell_parts if part and part.strip()).strip()
                if value:
                    cells.append(value)
            if cells:
                rows.append(" || ".join(cells))
        if rows:
            paragraphs.append(f"[Bảng {table_index}]")
            paragraphs.extend(rows)
    return "\n".join(paragraphs)


def extract_pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise CashbackError("Server chưa cài thư viện đọc PDF. Bạn dùng file Word/TXT hoặc paste nội dung vào ô text nhé.") from exc
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = [(page.extract_text() or "") for page in reader.pages[:30]]
    except Exception as exc:
        raise CashbackError("Không đọc được file PDF. Nếu PDF là ảnh scan, bạn chuyển sang Word/TXT hoặc paste nội dung nhé.") from exc
    return "\n".join(pages)


DELETE_NOT_FOUND = "Không tìm thấy giao dịch bạn yêu cầu. Vui lòng xoá thủ công trong mục Lịch sử giao dịch."
TRANSACTION_MANUAL_FALLBACK = "Xin lỗi, tôi chưa thể hiểu đúng ý bạn. Bạn hãy tìm, xoá và thêm lại giao dịch trong mục Lịch sử giao dịch nhé."
TRANSACTION_TARGET_ACTIONS = {"delete_transaction", "update_transaction_mcc"}


def is_update_transaction_mcc_request(text: str) -> bool:
    lower = str(text or "").lower()
    return any(token in lower for token in ["cập nhật", "cap nhat", "update", "sửa", "sua"]) and "mcc" in lower and any(
        token in lower for token in ["giao dịch", "giao dich", "transaction", "txn-"]
    )


def is_delete_request_text(text: str) -> bool:
    lower = str(text or "").lower()
    return any(token in lower for token in ["xóa", "xoá", "xoa", "delete"]) and any(token in lower for token in ["giao dịch", "giao dich", "transaction", "txn-"])


def extract_transaction_id(text: str) -> str | None:
    match = re.search(r"\btxn-\d+\b", str(text or ""), flags=re.I)
    return match.group(0) if match else None


def parse_delete_merchant(text: str) -> str | None:
    cleaned = re.sub(r"\b(vui lòng|vui long|hãy|hay|giúp|giup|mình|minh|please)\b", " ", str(text or ""), flags=re.I)
    cleaned = re.sub(r"\b(xóa|xoá|xoa|delete|giao dịch|giao dich|transaction|vừa thêm vào|vua them vao|vừa thêm|vua them|mới nhập|moi nhap|đã nhập sai|da nhap sai)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\btxn-\d+\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\bmcc\s*\d{4}\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\d+(?:[.,]\d+)?\s*(?:tr|triệu|trieu|m|k|nghìn|nghin|ngàn|ngan|đ|d|vnd|vnđ)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(online|pos|offline|trực tuyến|truc tuyen|quẹt|quet|hôm nay|hom nay)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?.,:")
    return cleaned or None


def transaction_matches(txn: dict, txn_id: str | None, merchant: str | None, amount: int | None, text: str) -> bool:
    lower = text.lower()
    if txn_id and txn.get("id", "").lower() != txn_id.lower():
        return False
    if amount is not None and int(txn.get("amount") or 0) != int(amount):
        return False
    if merchant:
        merchant_value = str(txn.get("merchant_name") or txn.get("merchant") or "").lower()
        if merchant.lower() not in merchant_value and merchant_value not in merchant.lower():
            return False
    if "vừa thêm" in lower or "vua them" in lower or "mới nhập" in lower or "moi nhap" in lower:
        return True
    return bool(txn_id or amount is not None or merchant)


def find_transaction_for_delete(text: str) -> dict | None:
    txn_id = extract_transaction_id(text)
    amount = parse_amount(text)
    merchant = parse_delete_merchant(text)
    candidates = [txn for txn in list_transactions() if transaction_matches(txn, txn_id, merchant, amount, text)]
    if not candidates:
        return None
    return candidates[-1]


def parse_delete_transaction(text: str) -> dict:
    txn = find_transaction_for_delete(text)
    if not txn:
        raise CashbackError(DELETE_NOT_FOUND)
    payload = {
        "id": txn.get("id"),
        "card_id": txn.get("card_id"),
        "merchant": txn.get("merchant_name") or txn.get("merchant"),
        "amount": txn.get("amount"),
        "mcc": txn.get("mcc"),
        "channel": txn.get("payment_method") or txn.get("channel"),
        "date": txn.get("date") or txn.get("transaction_date"),
    }
    return {"type": "delete_transaction", "payload": payload, "required": ["id"]}


def parse_update_transaction_mcc_merchant(text: str) -> str | None:
    cleaned = re.sub(r"\b(vui lòng|vui long|hãy|hay|giúp|giup|mình|minh|please|của tôi|cua toi|về đúng mã|ve dung ma|đúng mã|dung ma)\b", " ", str(text or ""), flags=re.I)
    cleaned = re.sub(r"\b(cập nhật|cap nhat|update|sửa|sua|lại|lai|giao dịch|giao dich|transaction|mcc|mã|ma|mới|moi)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\btxn-\d+\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\d+(?:[.,]\d+)?\s*(?:tr|triệu|trieu|m|k|nghìn|nghin|ngàn|ngan|đ|d|vnd|vnđ)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\bmcc\s*\d{4}\b|\b\d{4}\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(hôm nay|hom nay|today|ngày|ngay|sang|thành|thanh|là|la)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?.,:")
    return cleaned or None


def find_transaction_for_mcc_update(text: str) -> dict | None:
    txn_id = extract_transaction_id(text)
    amount = parse_amount(text)
    merchant = parse_update_transaction_mcc_merchant(text)
    candidates = [txn for txn in list_transactions() if transaction_matches(txn, txn_id, merchant, amount, text)]
    if not candidates:
        return None
    return candidates[-1]


def parse_update_transaction_mcc(text: str) -> dict:
    txn = find_transaction_for_mcc_update(text)
    parsed_merchant = parse_update_transaction_mcc_merchant(text)
    payload = {
        "id": txn.get("id") if txn else None,
        "card_id": txn.get("card_id") if txn else None,
        "merchant": parsed_merchant or ((txn.get("merchant_name") or txn.get("merchant")) if txn else None),
        "amount": txn.get("amount") if txn else parse_amount(text),
        "old_mcc": txn.get("mcc") if txn else None,
        "mcc": None,
        "channel": (txn.get("payment_method") or txn.get("channel")) if txn else None,
        "date": (txn.get("date") or txn.get("transaction_date")) if txn else None,
        "category": None,
    }
    mcc = re.search(
        r"\bmcc\s*(?:mới|moi|là|la|thành|thanh|sang|:)?\s*(?:là|la|:)?\s*(\d{4})\b|(?:mã|ma)?\s*(?:mới|moi|là|la|thành|thanh|sang|:)\s*(\d{4})\b",
        str(text or ""),
        flags=re.I,
    )
    if mcc:
        payload["mcc"] = next((x for x in mcc.groups() if x), None)
    return {"type": "update_transaction_mcc", "payload": payload, "required": ["id", "mcc"]}


def month_anchor(base: dt.date, months_delta: int) -> dt.date:
    month_index = base.year * 12 + (base.month - 1) + months_delta
    year = month_index // 12
    month = month_index % 12 + 1
    if month == 12:
        last_day = 31
    else:
        last_day = (dt.date(year, month + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(year, month, min(base.day, last_day))


def available_statement_periods(card_id: str, selected_date: str | None = None) -> list[dict]:
    card = next((c for c in load_cards() if c.get("id") == card_id), None)
    if not card:
        return []
    today = dt.date.today()
    dates = {
        str(txn["date"])
        for txn in list_transactions()
        if txn.get("card_id") == card_id and txn.get("date")
    }
    if not dates:
        dates = {month_anchor(today, offset).isoformat() for offset in range(-2, 1)}
    if selected_date:
        dates.add(selected_date)
    periods: dict[str, dict] = {}
    for date_text in dates:
        try:
            _, start, end = period_for(card, date_text)
        except Exception:
            continue
        key = f"{start.isoformat()}..{end.isoformat()}"
        periods[key] = {
            "key": key,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "date": end.isoformat(),
            "label": f"{start.day:02d}/{start.month:02d}/{start.year} - {end.day:02d}/{end.month:02d}/{end.year}",
        }
    return sorted(periods.values(), key=lambda row: row["end"], reverse=True)


def missing_fields(action: dict) -> list[str]:
    payload = action["payload"]
    optional_nulls = set(action.get("optional_nulls") or [])
    missing = []
    for field in action["required"]:
        if field in optional_nulls:
            continue
        value = payload.get(field)
        if value is None or value == "" or (field == "cashback_rules" and not value):
            missing.append(field)
    return missing


def describe_action(action: dict) -> str:
    payload = action["payload"]
    if action["type"] == "transaction":
        mcc_text = payload.get("mcc") or "null"
        if payload.get("mcc_source") == "database_suggestion":
            mcc_text = f"{mcc_text} (gợi ý từ database)"
        return (
            "Xác nhận lưu giao dịch:\n"
            f"- Thẻ: {display_card_name(payload.get('card_id'))}\n"
            f"- Merchant: {str(payload.get('merchant') or 'chưa có').title()}\n"
            f"- Số tiền: {money(payload.get('amount')) if payload.get('amount') else 'chưa có'}\n"
            f"- MCC: {mcc_text}\n"
            f"- Hình thức: {display_channel(payload.get('channel'))}\n"
            f"- Ngày: {display_date(payload.get('date'))}"
        )
    if action["type"] == "delete_transaction":
        return (
            "Xác nhận xoá giao dịch:\n"
            f"- Thẻ: {display_card_name(payload.get('card_id'))}\n"
            f"- Merchant: {str(payload.get('merchant') or 'chưa có').title()}\n"
            f"- Số tiền: {money(payload.get('amount')) if payload.get('amount') else 'chưa có'}\n"
            f"- MCC: {payload.get('mcc') or 'null'}\n"
            f"- Hình thức: {display_channel(payload.get('channel'))}\n"
            f"- Ngày: {display_date(payload.get('date'))}"
        )
    if action["type"] == "update_transaction_mcc":
        return (
            "Xác nhận cập nhật MCC giao dịch:\n"
            f"- Giao dịch: {payload.get('id') or 'chưa tìm thấy'}\n"
            f"- Thẻ: {display_card_name(payload.get('card_id'))}\n"
            f"- Merchant: {str(payload.get('merchant') or 'chưa có').title()}\n"
            f"- Số tiền: {money(payload.get('amount')) if payload.get('amount') else 'chưa có'}\n"
            f"- MCC hiện tại: {payload.get('old_mcc') or 'null'}\n"
            f"- MCC mới: {payload.get('mcc') or 'chưa có'}\n"
            f"- Ngày: {display_date(payload.get('date'))}"
        )
    if action["type"] == "mcc":
        return (
            "Mình sẽ cập nhật MCC:\n"
            f"- Merchant: {payload.get('merchant_name') or 'chưa có'}\n"
            f"- MCC: {payload.get('mcc') or 'chưa có'}\n"
            f"- Hình thức: {payload.get('payment_method') or 'any'}\n"
            f"- Địa chỉ/phạm vi: {payload.get('address') or 'không có'}"
        )
    if action["type"] == "rule":
        return describe_rule_action(payload)
    return (
        "Mình sẽ thêm/cập nhật thẻ:\n"
        f"- ID: {payload.get('id') or 'chưa có'}\n"
        f"- Tên thẻ: {payload.get('name') or 'chưa có'}\n"
        f"- Chốt sao kê: {payload.get('statement_close_day') or 'null'}\n"
        f"- Chốt hoàn tiền: {payload.get('cashback_close_day') or 'null'}\n"
        f"- Hạn mức: {money(payload.get('credit_limit'))}\n"
        f"- Điều kiện chi tiêu tối thiểu: {money(payload.get('min_total_spend')) if payload.get('min_total_spend') else 'không có'}\n"
        f"{display_cashback_rules(payload.get('cashback_rules'))}"
    )


def describe_rule_action(payload: dict) -> str:
    operation_labels = {
        "exclude_mcc": "thêm MCC loại trừ",
        "update_per_transaction_cap": "cập nhật mức hoàn tối đa mỗi giao dịch",
    }
    operation_text = operation_labels.get(payload.get("operation"), "thêm/cập nhật rule")
    return (
        "Mình sẽ cập nhật quy định hoàn tiền:\n"
        f"- Thẻ: {display_card_name(payload.get('card_id')) if payload.get('card_id') else payload.get('card_name') or 'chưa có'}\n"
        f"- Thao tác: {operation_text}\n"
        f"- MCC: {', '.join(payload.get('mcc') or []) or 'không có'}\n"
        f"- Nhóm loại trừ: {', '.join(payload.get('excluded_categories') or []) or 'không có'}\n"
        f"- Hoàn tối đa mỗi giao dịch: {money(payload.get('max_cashback_per_transaction')) if payload.get('max_cashback_per_transaction') else 'không có'}\n"
        f"{display_cashback_rules(payload.get('cashback_rules'))}\n"
        f"- Mô tả: {payload.get('description') or 'không có'}"
    )


def missing_field_label(field: str, action: dict) -> str:
    labels = {
        "card_id": "tên thẻ đã có trong database",
        "amount": "số tiền",
        "merchant": "merchant",
        "channel": "hình thức thanh toán",
        "mcc": "MCC",
        "name": "tên thẻ",
        "statement_close_day": "ngày chốt sao kê",
        "cashback_close_day": "ngày chốt hoàn tiền",
        "credit_limit": "hạn mức",
        "cashback_rules": "quy định hoàn tiền",
        "max_cashback_per_transaction": "mức hoàn tối đa mỗi giao dịch",
    }
    if field == "card_id" and action.get("type") == "rule" and action.get("payload", {}).get("card_name"):
        return f"thẻ {action['payload']['card_name']} chưa có trong database"
    return labels.get(field, field)


def prompt_for_action(action: dict) -> dict:
    missing = missing_fields(action)
    if missing:
        if action["type"] in TRANSACTION_TARGET_ACTIONS:
            action["clarification_count"] = int(action.get("clarification_count") or 0) + 1
            if action["clarification_count"] > 3:
                return {
                    "intent": "action_exhausted",
                    "result": action,
                    "answer": TRANSACTION_MANUAL_FALLBACK,
                }
        return {
            "intent": f"pending_{action['type']}",
            "result": action,
            "answer": describe_action(action) + "\n\nCòn thiếu: " + ", ".join(missing_field_label(field, action) for field in missing) + ". Bạn bổ sung giúp mình; nếu không biết thông tin nào thì nói không biết/không rõ.",
        }
    return {
        "intent": f"confirm_{action['type']}",
        "result": action,
        "answer": describe_action(action),
    }


def merge_action(action: dict, text: str) -> dict:
    parser = {
        "transaction": parse_input_transaction,
        "mcc": parse_input_mcc,
        "card": parse_input_card,
        "rule": parse_input_rule,
        "delete_transaction": parse_delete_transaction,
        "update_transaction_mcc": parse_update_transaction_mcc,
    }[action["type"]]
    update = parser(text)["payload"]
    for key, value in update.items():
        if action["type"] == "update_transaction_mcc" and key == "merchant" and re.search(r"\bmcc\b|\bmã\b|\bma\b", text, flags=re.I):
            continue
        if value is not None and value != "" and value != [] and value != {}:
            action["payload"][key] = value
    if says_unknown(text):
        for field in missing_fields(action):
            if action["type"] in TRANSACTION_TARGET_ACTIONS:
                continue
            if action["type"] == "mcc" and field == "mcc":
                continue
            if action["type"] == "transaction" and field == "channel":
                continue
            action.setdefault("optional_nulls", []).append(field)
            action["payload"][field] = None
    return action


def execute_action(action: dict) -> dict:
    payload = action["payload"]
    if action["type"] == "transaction":
        txn = record_transaction(payload)
        return {"intent": "record_transaction", "result": {"transaction": txn}, "answer": "Đã lưu thành công."}
    if action["type"] == "delete_transaction":
        try:
            txn = delete_transaction(payload.get("id"))
        except ValueError:
            return {"intent": "delete_transaction", "result": {}, "answer": DELETE_NOT_FOUND}
        return {"intent": "delete_transaction", "result": {"transaction": txn}, "answer": "Đã xoá thành công"}
    if action["type"] == "update_transaction_mcc":
        txn = update_transaction_mcc(payload["id"], payload["mcc"], payload.get("category"), payload.get("merchant"))
        return {"intent": "update_transaction_mcc", "result": {"transaction": txn}, "answer": "Đã cập nhật MCC giao dịch thành công."}
    if action["type"] == "mcc":
        row = upsert_merchant_mcc(payload)
        return {"intent": "update_mcc", "result": {"merchant_mcc": row}, "answer": f"Đã cập nhật database MCC: {row['merchant_name']} -> MCC {row['mcc']} ({row['payment_method']})."}
    if action["type"] == "rule":
        cards = load_cards()
        card = next((c for c in cards if c["id"] == payload["card_id"]), None)
        if not card:
            card_name = payload.get("card_name") or payload.get("card_id") or "này"
            raise CashbackError(f"Thẻ {card_name} chưa có trong database. Bạn hãy vào tab Quản lí thẻ, bấm Thêm thẻ mới để thêm thẻ trước, rồi cập nhật rule sau.")
        if payload.get("operation") == "exclude_mcc":
            for rule in card.get("cashback_rules", []):
                excluded = [str(x) for x in rule.get("excluded_mcc", [])]
                for mcc in payload.get("mcc") or []:
                    if str(mcc) not in excluded:
                        excluded.append(str(mcc))
                rule["excluded_mcc"] = excluded
                excluded_categories = [str(x) for x in rule.get("excluded_categories", [])]
                for category in payload.get("excluded_categories") or []:
                    if str(category) not in excluded_categories:
                        excluded_categories.append(str(category))
                if excluded_categories:
                    rule["excluded_categories"] = excluded_categories
            action_text = "thêm MCC loại trừ " + (", ".join(payload.get("mcc") or []) or "không có")
        elif payload.get("operation") == "update_per_transaction_cap":
            cap = int(payload.get("max_cashback_per_transaction") or 0)
            if cap <= 0:
                raise CashbackError("Thiếu mức hoàn tối đa mỗi giao dịch.")
            target_mcc = {str(x) for x in payload.get("mcc") or []}
            updated_count = 0
            for rule in card.get("cashback_rules", []):
                rule_mcc = {str(x) for x in rule.get("mcc") or []}
                if target_mcc and not (target_mcc & rule_mcc):
                    continue
                rule["max_cashback_per_transaction"] = cap
                updated_count += 1
            if updated_count == 0:
                raise CashbackError("Không tìm thấy rule cashback phù hợp để cập nhật.")
            action_text = f"cập nhật hoàn tối đa mỗi giao dịch thành {money(cap)} cho {updated_count} rule"
        elif payload.get("operation") == "add_rule":
            new_rules = payload.get("cashback_rules") or []
            if not new_rules:
                raise CashbackError("Mình chưa đọc được rule hoàn tiền cần thêm. Bạn bổ sung tỉ lệ hoàn tiền và điều kiện áp dụng giúp mình.")
            existing = card.setdefault("cashback_rules", [])
            existing.extend(new_rules)
            action_text = f"thêm {len(new_rules)} rule hoàn tiền mới"
        else:
            raise CashbackError("Hiện tại /input_rule chưa hiểu loại cập nhật này. Bạn mô tả rõ thẻ, điều kiện và giới hạn cần đổi giúp mình.")
        updated = upsert_card(card)
        return {
            "intent": "input_rule",
            "result": {"card": updated, "rule_update": payload},
            "answer": f"Đã cập nhật quy định hoàn tiền cho {updated['name']}: {action_text}.",
        }
    payload["credit_limit"] = int(payload.get("credit_limit") or 0)
    payload["min_total_spend"] = int(payload.get("min_total_spend") or 0)
    payload["cashback_round_down_to"] = int(payload.get("cashback_round_down_to") or 0)
    payload["cashback_rules"] = payload.get("cashback_rules") or []
    payload["aliases"] = payload.get("aliases") or []
    card = upsert_card(payload)
    return {"intent": "input_card", "result": {"card": card}, "answer": f"Đã thêm/cập nhật thẻ {card['name']} vào database."}


def looks_like_amount_only(text: str) -> bool:
    return bool(re.fullmatch(r"\s*\d+(?:[.,]\d+)?\s*(k|nghìn|nghin|ngàn|ngan|tr|triệu|trieu|m)?\s*", text.lower()))


def missing_amount_error(message: str) -> bool:
    return "số tiền" in message or "so tien" in message


def missing_required_info_error(message: str) -> bool:
    return missing_amount_error(message) or "thẻ nào" in message or "the nao" in message or "merchant" in message


def is_recommendation_text(text: str) -> bool:
    lower = str(text or "").lower()
    return any(token in lower for token in [
        "nên quẹt thẻ", "nen quet the", "nên dùng thẻ", "nen dung the",
        "thẻ nào", "the nao", "thẻ gì", "the gi", "sắp đi", "sap di",
        "sắp ăn", "sap an", "sắp mua", "sap mua", "sắp tiêu", "sap tieu",
    ])


def should_extend_pending(pending: str | None, text: str) -> bool:
    if not pending:
        return False
    lower = text.lower().strip()
    if not lower or len(lower) > 80:
        return False
    if is_more_followup(lower):
        return False
    new_intent_tokens = [
        "mcc của", "mcc cua", "ở đâu", "o dau", "nên dùng", "nen dung",
        "sắp mua", "sap mua", "sắp tiêu", "sap tieu", "xóa", "xoa",
        "cập nhật", "cap nhat", "thêm giao dịch", "them giao dich",
    ]
    return not any(token in lower for token in new_intent_tokens)


def is_more_followup(text: str) -> bool:
    lower = text.lower()
    more_signal = any(token in lower for token in ["còn", "ngoài ra", "ngoai ra", "khác", "khac", "thêm nữa", "them nua"])
    more_signal = more_signal or bool(re.search(r"\bcon\s+(nua|gi|nhung|dia|mcc|cua)\b", lower))
    return more_signal and any(
        token in lower for token in ["địa chỉ", "dia chi", "mcc", "cửa hàng", "cua hang", "nào", "nao"]
    )


def format_followup(result: dict, offset: int, size: int = 10) -> dict | None:
    rows = (result.get("result") or {}).get("rows") or []
    if not rows:
        return None
    chunk = rows[offset: offset + size]
    if not chunk:
        return {
            "intent": "followup_more",
            "answer": "Mình đã hiển thị hết các kết quả đang có trong database cho câu hỏi trước.",
            "result": {"rows": []},
        }
    parts = []
    for row in chunk:
        merchant = row.get("merchant_name") or row.get("merchant_full_name") or "Merchant"
        address = row.get("address") or row.get("merchant_full_name") or "không có địa chỉ"
        mcc = row.get("mcc") or "-"
        method = row.get("payment_method") or "-"
        parts.append(f"{merchant} - {address} - MCC {mcc} ({method})")
    remaining = max(0, len(rows) - offset - len(chunk))
    tail = f" Còn {remaining} kết quả khác." if remaining else " Đã hết kết quả."
    return {
        "intent": "followup_more",
        "answer": "Các kết quả tiếp theo: " + "; ".join(parts) + tail,
        "result": {"rows": chunk, "remaining": remaining},
    }


INDEX_HTML = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Cashback Agent</title>
  <style>
    :root { color-scheme: light; font-family: Arial, sans-serif; }
    body { margin: 0; background: #f7f7f4; color: #202124; }
    header { padding: 18px 24px; background: #154734; color: #fff; }
    h1 { margin: 0; font-size: 22px; letter-spacing: 0; }
    main { display: grid; grid-template-columns: 1.4fr .9fr; gap: 18px; padding: 18px; max-width: 1180px; margin: 0 auto; }
    section { background: #fff; border: 1px solid #ddd9cf; border-radius: 8px; padding: 16px; }
    textarea, input, select { width: 100%; box-sizing: border-box; border: 1px solid #c9c5bc; border-radius: 6px; padding: 10px; font: inherit; }
    textarea { min-height: 88px; resize: vertical; }
    button { border: 0; border-radius: 6px; background: #1b6b4a; color: #fff; padding: 10px 14px; font-weight: 700; cursor: pointer; }
    button.secondary { background: #3949ab; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 10px 0; }
    .answer { margin-top: 12px; padding: 12px; border-radius: 6px; background: #eef7f1; white-space: pre-wrap; line-height: 1.45; }
    .muted { color: #666; font-size: 13px; }
    .card { border-top: 1px solid #e6e2d8; padding: 10px 0; }
    .meter { height: 9px; background: #ece8df; border-radius: 999px; overflow: hidden; margin-top: 6px; }
    .fill { height: 100%; background: #d99a28; width: 0%; }
    @media (max-width: 820px) { main { grid-template-columns: 1fr; padding: 12px; } .row { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header><h1>Cashback Agent</h1><div class="muted" style="color:#dbe8df">Quản lý hoàn tiền thẻ tín dụng theo rule, MCC, kênh thanh toán và kỳ sao kê.</div></header>
  <main>
    <section>
      <h2>Hỏi agent</h2>
      <textarea id="question">tôi sắp tiêu 2 triệu tại shopee online, nên dùng thẻ nào?</textarea>
      <div style="margin-top:10px"><button onclick="ask()">Gửi câu hỏi</button></div>
      <div id="answer" class="answer">Nhập câu hỏi để bắt đầu.</div>
    </section>
    <section>
      <h2>Ghi nhận giao dịch</h2>
      <div class="row"><select id="card"></select><input id="amount" placeholder="Số tiền, ví dụ 2000000" /></div>
      <div class="row"><input id="category" placeholder="Lĩnh vực: shopee, online, dining..." /><input id="mcc" placeholder="MCC, ví dụ 5812" /></div>
      <div class="row"><input id="merchant" placeholder="Merchant, ví dụ Shopee" /><select id="channel"><option value="online">online</option><option value="pos">pos</option></select></div>
      <button class="secondary" onclick="recordTxn()">Lưu giao dịch</button>
      <h2>Tiến độ</h2>
      <div id="progress"></div>
    </section>
  </main>
<script>
async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Request failed');
  return data;
}
async function init() {
  const cards = await api('/api/cards');
  const select = document.getElementById('card');
  select.innerHTML = cards.cards.map(c => `<option value="${c.id}">${c.name}</option>`).join('');
  await loadProgress();
}
async function ask() {
  const question = document.getElementById('question').value;
  try {
    const data = await api('/api/ask', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({question})});
    document.getElementById('answer').textContent = data.answer;
    await loadProgress();
  } catch (err) { document.getElementById('answer').textContent = err.message; }
}
async function recordTxn() {
  const body = {
    card_id: document.getElementById('card').value,
    amount: Number(document.getElementById('amount').value),
    category: document.getElementById('category').value,
    mcc: document.getElementById('mcc').value,
    merchant: document.getElementById('merchant').value,
    channel: document.getElementById('channel').value
  };
  try {
    await api('/api/transactions', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    document.getElementById('answer').textContent = 'Đã lưu giao dịch.';
    await loadProgress();
  } catch (err) { document.getElementById('answer').textContent = err.message; }
}
async function loadProgress() {
  const data = await api('/api/progress');
  document.getElementById('progress').innerHTML = data.cards.map(c => {
    const pct = c.min_total_spend ? Math.min(100, Math.round(c.total_spend / c.min_total_spend * 100)) : 100;
    return `<div class="card"><strong>${c.card_name}</strong><br>
      <span class="muted">Chi tiêu: ${fmt(c.total_spend)} | Hoàn: ${fmt(c.earned_cashback)} | Tiềm năng: ${fmt(c.potential_cashback)}</span>
      <div class="meter"><div class="fill" style="width:${pct}%"></div></div>
      <span class="muted">${c.qualified ? 'Đã đạt điều kiện' : 'Cần thêm ' + fmt(c.min_spend_gap)}</span></div>`;
  }).join('');
}
function fmt(v) { return Math.round(v).toLocaleString('vi-VN') + 'đ'; }
init();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _multipart_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        content_type = self.headers.get("Content-Type", "")
        raw = self.rfile.read(length)
        message = BytesParser(policy=email_policy).parsebytes(
            b"Content-Type: " + content_type.encode("utf-8") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
        )
        fields: dict[str, object] = {}
        for part in message.iter_parts():
            disposition = part.get("Content-Disposition", "")
            if "form-data" not in disposition:
                continue
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                fields[name] = {"filename": filename, "content": payload}
            else:
                fields[name] = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
        return fields

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                index_path = STATIC_DIR / "index.html"
                self._html(index_path.read_text(encoding="utf-8") if index_path.exists() else INDEX_HTML)
            elif parsed.path == "/health":
                self._json(200, {"status": "ok", "agent": "cashback-agent"})
            elif parsed.path == "/api/cards":
                self._json(200, {"cards": load_cards()})
            elif parsed.path == "/api/progress":
                self._json(200, {"cards": [card_progress(c["id"], qs.get("date", [None])[0]) for c in load_cards()]})
            elif parsed.path == "/api/card-detail":
                card_id = qs.get("card_id", [None])[0]
                if not card_id:
                    raise CashbackError("Missing card_id.")
                selected_date = qs.get("date", [None])[0]
                progress = card_progress(card_id, selected_date)
                cashback_by_txn: dict[str, int] = {}
                rate_by_txn: dict[str, float] = {}
                for match in progress.get("matches", []):
                    txn_id = match.get("transaction_id")
                    if txn_id:
                        cashback_by_txn[txn_id] = cashback_by_txn.get(txn_id, 0) + int(round(match.get("cashback") or 0))
                        rate_by_txn[txn_id] = max(rate_by_txn.get(txn_id, 0.0), float(match.get("rate") or 0) * 100)
                txns = []
                for txn in list_transactions():
                    if txn.get("card_id") != progress["card_id"]:
                        continue
                    row = dict(txn)
                    row["cashback_amount"] = cashback_by_txn.get(txn.get("id"), 0)
                    row["cashback_rate"] = rate_by_txn.get(txn.get("id"), 0.0)
                    txns.append(row)
                self._json(200, {"progress": progress, "transactions": txns, "periods": available_statement_periods(card_id, selected_date)})
            elif parsed.path == "/api/schema":
                self._json(200, schema_summary())
            elif parsed.path == "/api/merchant-mcc/lookup":
                merchant = qs.get("merchant", [None])[0]
                method = qs.get("payment_method", qs.get("channel", [None]))[0]
                self._json(200, {"match": lookup_merchant_mcc(merchant, method)})
            elif parsed.path == "/api/merchant-mcc/search":
                self._json(200, {"rows": search_merchant_mcc(qs.get("q", [None])[0])})
            else:
                self._json(404, {"error": "Not found"})
        except CashbackError as exc:
            self._json(400, {"error": str(exc)})

    def do_POST(self) -> None:
        global PENDING_CHAT_QUESTION, PENDING_AMOUNT_QUESTION, LAST_CHAT_RESULT, FOLLOWUP_OFFSET, PENDING_DB_ACTION
        try:
            if self.path == "/api/ask" or self.path == "/invocations":
                payload = self._body()
                question = payload.get("question") or payload.get("input") or payload.get("prompt")
                image_data_url = payload.get("image_data_url") or payload.get("image")
                if image_data_url:
                    if not is_vision_enabled():
                        self._json(200, {
                            "intent": "image_import_unavailable",
                            "answer": "Mình đã nhận ảnh, nhưng LLM vision chưa được cấu hình. Hãy cấu hình LLM_API_KEY và LLM_VISION_MODEL trong .env để đọc ảnh giao dịch.",
                            "result": {},
                        })
                        return
                    extracted = extract_transactions_from_image(str(image_data_url), str(question or ""))
                    if not extracted:
                        self._json(200, {
                            "intent": "image_import_failed",
                            "answer": "Mình chưa đọc được dữ liệu giao dịch từ ảnh này. Bạn thử paste ảnh rõ hơn hoặc nhập bằng /input_trans nhé.",
                            "result": {},
                        })
                        return
                    PENDING_DB_ACTION = transaction_action_from_image_payload(extracted, str(question or ""))
                    response = prompt_for_action(PENDING_DB_ACTION)
                    self._json(200, response)
                    return
                if not question:
                    raise CashbackError("Thiếu question/input.")
                question_text = str(question)
                command, clean_text = split_command(question_text)
                if question_text.strip().lower().startswith("/input_card"):
                    self._json(200, {
                        "intent": "input_card_disabled",
                        "answer": "Mình đã tắt lệnh /input_card trong chatbot. Bạn vào tab Quản lí thẻ và bấm Thêm thẻ mới để nhập thẻ bằng form nhé.",
                        "result": {},
                    })
                    return
                if command == "clear":
                    PENDING_CHAT_QUESTION = None
                    PENDING_AMOUNT_QUESTION = None
                    save_pending_amount_question(None)
                    LAST_CHAT_RESULT = None
                    FOLLOWUP_OFFSET = 0
                    PENDING_DB_ACTION = None
                    self._json(200, {"intent": "clear_chat", "answer": "Đã xoá lịch sử chat và các lệnh đang chờ.", "result": {}})
                    return
                if command == "fact":
                    PENDING_DB_ACTION = None
                    fact = add_fact(clean_text)
                    self._json(200, {
                        "intent": "record_fact",
                        "answer": f"Đã ghi nhớ fact: {fact.get('text')}.",
                        "result": {"fact": fact},
                    })
                    return
                if PENDING_DB_ACTION and command not in {"input_trans", "input_mcc", "input_rule", "delete_trans", "ask", "clear", "fact"}:
                    if is_cancel(question_text):
                        PENDING_DB_ACTION = None
                        self._json(200, {"intent": "cancel_pending_action", "answer": "Đã hủy bản nháp, mình chưa ghi gì vào database.", "result": {}})
                        return
                    if is_confirm(question_text):
                        action = PENDING_DB_ACTION
                        missing = missing_fields(action)
                        if missing:
                            self._json(200, prompt_for_action(action))
                            return
                        PENDING_DB_ACTION = None
                        self._json(200, execute_action(action))
                        return
                    PENDING_DB_ACTION = merge_action(PENDING_DB_ACTION, question_text)
                    response = prompt_for_action(PENDING_DB_ACTION)
                    if response.get("intent") == "action_exhausted":
                        PENDING_DB_ACTION = None
                    self._json(200, response)
                    return
                if command == "ask":
                    PENDING_DB_ACTION = None
                if not PENDING_DB_ACTION and is_confirm(question_text):
                    self._json(200, {"intent": "noop", "answer": "", "result": {}})
                    return
                if command in {"input_trans", "input_mcc", "input_rule", "delete_trans"}:
                    parser = {
                        "input_trans": parse_input_transaction,
                        "input_mcc": parse_input_mcc,
                        "input_rule": parse_input_rule,
                        "delete_trans": parse_delete_transaction,
                    }[command]
                    try:
                        PENDING_DB_ACTION = parser(clean_text)
                    except CashbackError as exc:
                        if str(exc) == DELETE_NOT_FOUND:
                            self._json(200, {"intent": "delete_transaction_not_found", "answer": DELETE_NOT_FOUND, "result": {}})
                            return
                        raise
                    response = prompt_for_action(PENDING_DB_ACTION)
                    if response.get("intent") == "action_exhausted":
                        PENDING_DB_ACTION = None
                    self._json(200, response)
                    return
                if command == "ask":
                    question_text = clean_text
                if is_delete_request_text(question_text):
                    try:
                        PENDING_DB_ACTION = parse_delete_transaction(question_text)
                    except CashbackError as exc:
                        if str(exc) == DELETE_NOT_FOUND:
                            self._json(200, {"intent": "delete_transaction_not_found", "answer": DELETE_NOT_FOUND, "result": {}})
                            return
                        raise
                    response = prompt_for_action(PENDING_DB_ACTION)
                    if response.get("intent") == "action_exhausted":
                        PENDING_DB_ACTION = None
                    self._json(200, response)
                    return
                if is_update_transaction_mcc_request(question_text):
                    PENDING_DB_ACTION = parse_update_transaction_mcc(question_text)
                    response = prompt_for_action(PENDING_DB_ACTION)
                    if response.get("intent") == "action_exhausted":
                        PENDING_DB_ACTION = None
                    self._json(200, response)
                    return
                if is_natural_transaction_input(question_text):
                    PENDING_DB_ACTION = parse_input_transaction(question_text)
                    response = prompt_for_action(PENDING_DB_ACTION)
                    if response.get("intent") == "action_exhausted":
                        PENDING_DB_ACTION = None
                    self._json(200, response)
                    return
                stored_amount_question = PENDING_AMOUNT_QUESTION or load_pending_amount_question()
                if (PENDING_CHAT_QUESTION or stored_amount_question) and looks_like_amount_only(question_text):
                    question_text = f"{PENDING_CHAT_QUESTION or stored_amount_question} {question_text}"
                    PENDING_CHAT_QUESTION = None
                    PENDING_AMOUNT_QUESTION = None
                    save_pending_amount_question(None)
                elif should_extend_pending(PENDING_CHAT_QUESTION, question_text):
                    question_text = f"{PENDING_CHAT_QUESTION} {question_text}"
                    PENDING_CHAT_QUESTION = None
                    PENDING_AMOUNT_QUESTION = None
                if LAST_CHAT_RESULT and is_more_followup(question_text):
                    followup = format_followup(LAST_CHAT_RESULT, FOLLOWUP_OFFSET)
                    if followup:
                        FOLLOWUP_OFFSET += len(followup.get("result", {}).get("rows", []))
                        self._json(200, followup)
                        return
                if is_recommendation_text(question_text) and parse_amount(question_text) is None:
                    PENDING_CHAT_QUESTION = question_text
                    PENDING_AMOUNT_QUESTION = question_text
                    save_pending_amount_question(question_text)
                    self._json(200, {"intent": "pending_amount", "answer": "Bạn cho mình số tiền dự kiến chi tiêu nhé.", "result": {}})
                    return
                try:
                    result = parse_vietnamese_query(question_text)
                    PENDING_CHAT_QUESTION = None
                    if result.get("intent") == "pending_amount":
                        PENDING_CHAT_QUESTION = question_text
                        PENDING_AMOUNT_QUESTION = question_text
                        save_pending_amount_question(question_text)
                    result_body = result.get("result") or {}
                    LAST_CHAT_RESULT = result if result_body.get("rows") else None
                    FOLLOWUP_OFFSET = int(result_body.get("displayed_rows", 10)) if LAST_CHAT_RESULT else 0
                    self._json(200, result)
                except CashbackError as exc:
                    if missing_required_info_error(str(exc)):
                        PENDING_CHAT_QUESTION = question_text
                        if missing_amount_error(str(exc)):
                            PENDING_AMOUNT_QUESTION = question_text
                            save_pending_amount_question(question_text)
                    elif "Mình chưa hiểu câu hỏi" in str(exc) and llm_text_enabled():
                        answer = answer_with_context(question_text, llm_context_summary())
                        if answer:
                            self._json(200, {"intent": "llm_answer", "answer": answer, "result": {"source": "llm"}})
                            return
                    raise
            elif self.path == "/api/recommend":
                payload = self._body()
                draft = TransactionDraft(
                    amount=int(payload["amount"]),
                    category=payload.get("category"),
                    merchant=payload.get("merchant"),
                    mcc=payload.get("mcc"),
                    channel=payload.get("channel"),
                    date=payload.get("date"),
                )
                self._json(200, simulate_recommendation(draft))
            elif self.path == "/api/coverage":
                payload = self._body()
                draft = TransactionDraft(
                    amount=int(payload.get("amount", 1)),
                    category=payload.get("category"),
                    merchant=payload.get("merchant"),
                    mcc=payload.get("mcc"),
                    channel=payload.get("channel"),
                    date=payload.get("date"),
                )
                self._json(200, check_card_coverage(payload["card"], draft))
            elif self.path == "/api/transactions":
                self._json(201, {"transaction": record_transaction(self._body())})
            elif self.path == "/api/transactions/delete":
                self._json(200, {"transaction": delete_transaction(self._body().get("id"))})
            elif self.path == "/api/transactions/update":
                self._json(200, {"transaction": update_transaction(self._body())})
            elif self.path == "/api/transactions/mcc":
                payload = self._body()
                self._json(200, {"transaction": update_transaction_mcc(payload["id"], payload["mcc"], payload.get("category"))})
            elif self.path == "/api/cards":
                self._json(200, {"card": upsert_card(validate_card_payload(self._body()))})
            elif self.path == "/api/cards/parse-rules":
                content_type = self.headers.get("Content-Type", "")
                payload = self._multipart_body() if content_type.startswith("multipart/form-data") else self._body()
                text = str(payload.get("text") or "").strip()
                url = str(payload.get("url") or "").strip()
                upload = payload.get("ruleFile") if isinstance(payload.get("ruleFile"), dict) else None
                fetched_text = extract_card_rule_text_from_url(url) if url else ""
                file_text = extract_card_rule_text_from_upload(upload.get("filename", ""), upload.get("content", b"")) if upload else ""
                if not text and not fetched_text and not file_text:
                    raise CashbackError("Bạn cần nhập mô tả quy định hoàn tiền hoặc upload file quy định.")
                combined_text = "\n\n".join(
                    part for part in [
                        f"Thông tin người dùng nhập:\n{text}" if text else "",
                        f"Nội dung lấy từ website ngân hàng ({url}):\n{fetched_text}" if fetched_text else "",
                        f"Nội dung lấy từ file {upload.get('filename')}:\n{file_text}" if upload and file_text else "",
                    ]
                    if part
                )
                parsed = convert_card_rules_text(combined_text) if llm_text_enabled() else None
                result = normalize_llm_card_rules(parsed, combined_text)
                if fetched_text:
                    result["source_url"] = url
                    result["website_text_length"] = len(fetched_text)
                if file_text:
                    result["source_file"] = upload.get("filename")
                    result["file_text_length"] = len(file_text)
                self._json(200, result)
            elif self.path == "/api/cards/delete":
                self._json(200, {"card": delete_card(self._body().get("card_id"))})
            elif self.path == "/api/merchant-mcc":
                payload = self._body()
                if payload.get("upsert"):
                    self._json(200, {"row": upsert_merchant_mcc(payload)})
                else:
                    self._json(201, {"row": insert_merchant_mcc(payload)})
            else:
                self._json(404, {"error": "Not found"})
        except (CashbackError, KeyError, ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        print(f"{self.address_string()} - {format % args}")


def main() -> int:
    init_database()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Cashback agent running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
