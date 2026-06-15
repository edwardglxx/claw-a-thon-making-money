from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, replace
from typing import Any

from database import (
    delete_transaction,
    get_latest_transaction,
    insert_transaction,
    list_cards,
    list_transactions,
    lookup_merchant_mcc,
    search_merchant_mcc,
    search_merchant_mcc_by_address,
    update_transaction_mcc,
    upsert_merchant_mcc,
)
from llm_router import interpret as llm_interpret


class CashbackError(Exception):
    pass


@dataclass(frozen=True)
class TransactionDraft:
    amount: int
    category: str | None = None
    merchant: str | None = None
    mcc: str | None = None
    channel: str | None = None
    date: str | None = None
    data_match: dict[str, Any] | None = None


def today_iso() -> str:
    return dt.date.today().isoformat()


def load_cards() -> list[dict[str, Any]]:
    return list_cards()


def load_transactions() -> list[dict[str, Any]]:
    return list_transactions()


def enrich_draft(draft: TransactionDraft) -> TransactionDraft:
    if draft.mcc:
        data_match = draft.data_match or {"source": "user_input", "mcc": draft.mcc}
        return TransactionDraft(
            amount=draft.amount,
            category=draft.category,
            merchant=draft.merchant,
            mcc=draft.mcc,
            channel=draft.channel,
            date=draft.date,
            data_match=data_match,
        )
    if not draft.merchant:
        return draft
    match = lookup_merchant_mcc(draft.merchant, draft.channel)
    if not match:
        return draft
    return TransactionDraft(
        amount=draft.amount,
        category=draft.category or match.get("category"),
        merchant=draft.merchant,
        mcc=match.get("mcc"),
        channel=draft.channel or match.get("payment_method"),
        date=draft.date,
        data_match=match,
    )


def data_match_note(draft: TransactionDraft) -> str:
    if draft.data_match:
        source = draft.data_match.get("source") or "database"
        merchant = draft.data_match.get("merchant_name")
        mcc = draft.data_match.get("mcc")
        if source == "user_input":
            return f"MCC {mcc} do bạn cung cấp trực tiếp."
        return f"Khớp kết quả database MCC: {merchant or draft.merchant} -> MCC {mcc}."
    return "Merchant này chưa có trong database."


def money(value: float | int) -> str:
    return f"{int(round(value)):,}".replace(",", ".") + "đ"


def parse_date(value: str | None) -> dt.date:
    if not value:
        return dt.date.today()
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise CashbackError("Ngày cần có định dạng YYYY-MM-DD.") from exc


def period_for(card: dict[str, Any], on_date: str | None = None) -> tuple[str, dt.date, dt.date]:
    current = parse_date(on_date)
    statement = card.get("statement", {"type": "calendar_month"})
    if statement.get("type") == "calendar_month":
        start = current.replace(day=1)
        if current.month == 12:
            end = current.replace(year=current.year + 1, month=1, day=1) - dt.timedelta(days=1)
        else:
            end = current.replace(month=current.month + 1, day=1) - dt.timedelta(days=1)
    else:
        close_day = int(statement.get("close_day", 25))
        close_this_month = current.replace(day=min(close_day, _last_day(current.year, current.month)))
        if current <= close_this_month:
            end = close_this_month
            prev_month = current.month - 1 or 12
            prev_year = current.year - 1 if current.month == 1 else current.year
            prev_close = dt.date(prev_year, prev_month, min(close_day, _last_day(prev_year, prev_month)))
            start = prev_close + dt.timedelta(days=1)
        else:
            start = close_this_month + dt.timedelta(days=1)
            next_month = current.month + 1 if current.month < 12 else 1
            next_year = current.year if current.month < 12 else current.year + 1
            end = dt.date(next_year, next_month, min(close_day, _last_day(next_year, next_month)))
    return f"{start.isoformat()}..{end.isoformat()}", start, end


def _last_day(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (dt.date(year, month + 1, 1) - dt.timedelta(days=1)).day


def in_period(txn: dict[str, Any], start: dt.date, end: dt.date) -> bool:
    day = parse_date(txn.get("date"))
    return start <= day <= end


def normalize_text(value: str | None) -> str:
    return (value or "").strip().lower()


def rule_matches(rule: dict[str, Any], txn: dict[str, Any]) -> bool:
    category = normalize_text(txn.get("category"))
    channel = normalize_text(txn.get("channel"))
    mcc = str(txn.get("mcc") or "")
    matched_conditions = 0
    rule_has_conditions = any(key in rule for key in ["categories", "channels", "mcc", "merchants"])

    if mcc and mcc in {str(x) for x in rule.get("excluded_mcc", [])}:
        return False
    if category and category in {normalize_text(x) for x in rule.get("excluded_categories", [])}:
        return False
    if channel and channel in {normalize_text(x) for x in rule.get("excluded_channels", [])}:
        return False

    if "categories" in rule and category:
        if category not in {normalize_text(x) for x in rule["categories"]}:
            return False
        matched_conditions += 1
    if "channels" in rule and channel:
        if channel not in {normalize_text(x) for x in rule["channels"]}:
            return False
        matched_conditions += 1
    if "mcc" in rule and mcc:
        if mcc not in {str(x) for x in rule["mcc"]}:
            return False
        matched_conditions += 1
    if "merchants" in rule:
        merchant = normalize_text(txn.get("merchant"))
        if merchant:
            aliases = {normalize_text(x) for x in rule["merchants"]}
            if merchant not in aliases and not any(alias in merchant for alias in aliases):
                return False
            matched_conditions += 1
    return matched_conditions > 0 if rule_has_conditions else True


def eligible_amount(amount: int, rule: dict[str, Any]) -> int:
    unit = int(rule.get("round_eligible_spend_to", 1))
    if unit <= 1:
        return amount
    return int(math.floor(amount / unit) * unit)


def raw_cashback(txn: dict[str, Any], rule: dict[str, Any]) -> float:
    amount = int(txn["amount"])
    cashback = eligible_amount(amount, rule) * float(rule["rate"])
    per_txn_cap = rule.get("max_cashback_per_transaction")
    low_amount_threshold = rule.get("low_amount_threshold")
    low_amount_cap = rule.get("low_amount_cashback_cap")
    if low_amount_threshold is not None and low_amount_cap is not None and amount < int(low_amount_threshold):
        cashback = min(cashback, float(low_amount_cap))
    elif per_txn_cap is not None:
        cashback = min(cashback, float(per_txn_cap))
    return cashback


def cycle_transactions(card_id: str, on_date: str | None, txns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    card = get_card(card_id)
    _, start, end = period_for(card, on_date)
    return [t for t in txns if t.get("card_id") == card_id and in_period(t, start, end)]


def get_card(card_id_or_name: str) -> dict[str, Any]:
    needle = normalize_text(card_id_or_name)
    for card in load_cards():
        aliases = [card["id"], card["name"], *card.get("aliases", [])]
        if any(normalize_text(alias) == needle for alias in aliases):
            return card
    raise CashbackError(f"Không tìm thấy thẻ: {card_id_or_name}")


def evaluate_card(card: dict[str, Any], txns: list[dict[str, Any]], on_date: str | None = None) -> dict[str, Any]:
    period_key, start, end = period_for(card, on_date)
    current = [t for t in txns if t.get("card_id") == card["id"] and in_period(t, start, end)]
    total_spend = sum(int(t["amount"]) for t in current)
    category_cashback: dict[str, float] = {}
    matched_cashback = 0.0
    matched_spend = 0
    matches: list[dict[str, Any]] = []

    for txn in current:
        for rule in card.get("cashback_rules", []):
            if not rule_matches(rule, txn):
                continue
            cb = raw_cashback(txn, rule)
            cap_key = rule.get("cap_key") or rule.get("name")
            cap = rule.get("cap_per_period")
            used = category_cashback.get(cap_key, 0.0)
            if cap is not None:
                cb = max(0.0, min(cb, float(cap) - used))
            category_cashback[cap_key] = used + cb
            matched_cashback += cb
            matched_spend += int(txn["amount"])
            matches.append({"transaction_id": txn.get("id"), "rule": rule.get("name"), "cashback": cb})
            break

    period_cap = card.get("period_cap")
    if period_cap is not None:
        matched_cashback = min(matched_cashback, float(period_cap))

    cashback_round = int(card.get("cashback_round_down_to") or 0)
    if cashback_round > 1:
        matched_cashback = math.floor(matched_cashback / cashback_round) * cashback_round

    min_spend = int(card.get("min_total_spend", 0))
    qualified = total_spend >= min_spend
    payable = matched_cashback if qualified else 0.0

    return {
        "card_id": card["id"],
        "card_name": card["name"],
        "period": period_key,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "total_spend": total_spend,
        "matched_spend": matched_spend,
        "qualified": qualified,
        "min_total_spend": min_spend,
        "min_spend_gap": max(0, min_spend - total_spend),
        "earned_cashback": round(payable),
        "potential_cashback": round(matched_cashback),
        "remaining_period_cap": None if period_cap is None else max(0, int(period_cap - matched_cashback)),
        "category_cashback": {k: round(v) for k, v in category_cashback.items()},
        "matches": matches,
    }


def simulate_recommendation(draft: TransactionDraft) -> dict[str, Any]:
    candidates = _recommendation_draft_candidates(draft)
    results = [_simulate_recommendation_single(candidate) for candidate in candidates]
    results.sort(key=_recommendation_result_score, reverse=True)
    return results[0]


def should_split_payment_methods(draft: TransactionDraft) -> bool:
    return bool(draft.amount and not draft.channel and not draft.merchant)


def simulate_payment_method_recommendations(draft: TransactionDraft) -> dict[str, Any]:
    return {
        "online": simulate_recommendation(replace(draft, channel="online")),
        "pos": simulate_recommendation(replace(draft, channel="pos")),
    }


def _recommendation_draft_candidates(draft: TransactionDraft) -> list[TransactionDraft]:
    if draft.mcc or not draft.merchant:
        return [draft]
    rows = search_merchant_mcc(draft.merchant, limit=100)
    candidates: list[TransactionDraft] = []
    seen: set[tuple[str, str | None]] = set()
    for row in rows:
        mcc = str(row.get("mcc") or "")
        method = row.get("payment_method")
        if not mcc:
            continue
        key = (mcc, method)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            TransactionDraft(
                amount=draft.amount,
                category=draft.category or row.get("category"),
                merchant=draft.merchant,
                mcc=mcc,
                channel=draft.channel or (method if method != "any" else None),
                date=draft.date,
                data_match=row,
            )
        )
    return candidates or [draft]


def _recommendation_result_score(result: dict[str, Any]) -> tuple[Any, ...]:
    best = result.get("best") or {}
    return (
        bool(best.get("has_direct_match")),
        int(best.get("cashback_gain") or 0),
        int(best.get("potential_cashback_gain") or 0),
        bool(best.get("threshold_help")),
        int(best.get("threshold_gap_reduction") or 0),
        -int(best.get("min_spend_gap_after") or 0),
    )


def _simulate_recommendation_single(draft: TransactionDraft) -> dict[str, Any]:
    draft = enrich_draft(draft)
    txns = load_transactions()
    date_value = draft.date or today_iso()
    synthetic = {
        "id": "__draft__",
        "date": date_value,
        "amount": draft.amount,
        "category": draft.category,
        "merchant": draft.merchant,
        "mcc": draft.mcc,
        "channel": draft.channel,
    }
    rows = []
    for card in load_cards():
        before = evaluate_card(card, txns, date_value)
        after_txn = dict(synthetic, card_id=card["id"])
        after = evaluate_card(card, txns + [after_txn], date_value)
        gain = after["earned_cashback"] - before["earned_cashback"]
        potential_gain = after["potential_cashback"] - before["potential_cashback"]
        matching_rule_details = [
            rule for rule in card.get("cashback_rules", []) if rule_matches(rule, after_txn)
        ]
        matching_rules = [rule["name"] for rule in matching_rule_details]
        needs_database_note = any(_rule_needs_database_note(rule) for rule in matching_rule_details)
        direct_raw_cashback = raw_cashback(after_txn, matching_rule_details[0]) if matching_rule_details else 0
        primary_rate = float(matching_rule_details[0].get("rate", 0)) if matching_rule_details else 0
        threshold_help = (
            int(card.get("min_total_spend", 0)) > 0
            and not before["qualified"]
            and after["total_spend"] > before["total_spend"]
        )
        threshold_gap_reduction = max(0, before["min_spend_gap"] - after["min_spend_gap"])
        rows.append(
            {
                "card_id": card["id"],
                "card_name": card["name"],
                "cashback_gain": gain,
                "potential_cashback_gain": potential_gain,
                "has_direct_match": bool(matching_rules),
                "threshold_help": threshold_help,
                "threshold_gap_reduction": threshold_gap_reduction,
                "min_spend_gap_after": after["min_spend_gap"],
                "pending_cashback_after": after["potential_cashback"],
                "qualified_after": after["qualified"],
                "matching_rules": matching_rules,
                "needs_database_note": needs_database_note,
                "primary_rate": primary_rate,
                "direct_raw_cashback": round(direct_raw_cashback),
                "remaining_period_cap_before": before["remaining_period_cap"],
                "period_cap_limited": (
                    bool(matching_rule_details)
                    and before["remaining_period_cap"] is not None
                    and direct_raw_cashback > gain
                    and before["remaining_period_cap"] <= direct_raw_cashback
                ),
                "cashback_rounded": (
                    bool(matching_rule_details)
                    and int(card.get("cashback_round_down_to") or 0) > 1
                    and direct_raw_cashback > gain
                    and gain > 0
                ),
                "round_down_to": int(card.get("cashback_round_down_to") or 0),
                "note": _recommendation_note(after, matching_rules, gain, potential_gain),
            }
        )
    if not any(row["has_direct_match"] or row["cashback_gain"] > 0 or row["potential_cashback_gain"] > 0 for row in rows):
        rows.sort(
            key=lambda x: (
                x["threshold_help"],
                x["threshold_gap_reduction"],
                -x["min_spend_gap_after"],
            ),
            reverse=True,
        )
    else:
        rows.sort(key=lambda x: (x["cashback_gain"], x["potential_cashback_gain"], x["has_direct_match"]), reverse=True)
    best = rows[0] if rows else None
    note = data_match_note(draft) if best and best.get("needs_database_note") and _has_merchant_for_database_note(draft) else ""
    return {"draft": synthetic, "best": best, "cards": rows, "data_match_note": note}


def _rule_needs_database_note(rule: dict[str, Any]) -> bool:
    return "mcc" in rule


def _has_merchant_for_database_note(draft: TransactionDraft) -> bool:
    merchant = normalize_text(draft.merchant)
    if not merchant:
        return False
    return not re.fullmatch(r"mcc\s*\d{4}", merchant)


def _recommendation_note(
    after: dict[str, Any], matching_rules: list[str], gain: int, potential_gain: int
) -> str:
    if gain > 0:
        if not matching_rules:
            return f"Giao dịch này không có cashback riêng, nhưng giúp đạt điều kiện tổng chi tiêu và mở khóa {money(gain)} đang chờ."
        return f"Hoàn thêm ngay {money(gain)}."
    if not matching_rules:
        return "Không khớp rule hoàn tiền."
    if potential_gain > 0 and not after["qualified"]:
        return f"Có thể hoàn {money(potential_gain)} nếu đạt thêm {money(after['min_spend_gap'])} tổng chi tiêu."
    return "Có rule phù hợp nhưng đã hết cap hoặc chưa tạo thêm tiền hoàn."


def card_progress(card_query: str, on_date: str | None = None) -> dict[str, Any]:
    card = get_card(card_query)
    return evaluate_card(card, load_transactions(), on_date)


def check_card_coverage(card_query: str, draft: TransactionDraft) -> dict[str, Any]:
    draft = enrich_draft(draft)
    card = get_card(card_query)
    txn = {
        "amount": draft.amount or 1,
        "category": draft.category,
        "merchant": draft.merchant,
        "mcc": draft.mcc,
        "channel": draft.channel,
        "date": draft.date or today_iso(),
    }
    matched = [rule for rule in card.get("cashback_rules", []) if rule_matches(rule, txn)]
    alternatives = []
    if not matched:
        for other in load_cards():
            if other["id"] == card["id"]:
                continue
            rules = [rule["name"] for rule in other.get("cashback_rules", []) if rule_matches(rule, txn)]
            if rules:
                alternatives.append({"card_id": other["id"], "card_name": other["name"], "rules": rules})
    return {
        "card_id": card["id"],
        "card_name": card["name"],
        "covered": bool(matched),
        "matched_rules": [rule["name"] for rule in matched],
        "alternatives": alternatives,
        "data_match_note": data_match_note(draft),
    }


def record_transaction(payload: dict[str, Any]) -> dict[str, Any]:
    card = get_card(payload["card_id"])
    draft = enrich_draft(
        TransactionDraft(
            amount=int(payload["amount"]),
            category=payload.get("category"),
            merchant=payload.get("merchant") or payload.get("merchant_name"),
            mcc=payload.get("mcc"),
            channel=payload.get("channel") or payload.get("payment_method"),
            date=payload.get("date") or payload.get("transaction_date"),
        )
    )
    txn = dict(
        payload,
        card_id=card["id"],
        category=draft.category,
        merchant=draft.merchant,
        mcc=draft.mcc,
        channel=draft.channel,
        date=draft.date,
    )
    return insert_transaction(txn)


def parse_vietnamese_query(text: str) -> dict[str, Any]:
    lower = normalize_text(text)
    llm_result = handle_llm_intent(text)
    if llm_result:
        return llm_result

    amount = _parse_amount(lower)
    card_name = _extract_card(lower)
    merchant = _extract_merchant(lower)
    draft = TransactionDraft(
        amount=amount or 0,
        category=_extract_category(lower),
        merchant=merchant,
        mcc=_extract_mcc(lower),
        channel=_extract_channel(lower),
        date=_extract_date(lower),
    )

    if is_transaction_delete_query(lower):
        result = handle_delete_transaction(lower)
        return {"intent": "delete_transaction", "result": result, "answer": result["answer"]}

    if is_mcc_update_query(lower):
        result = handle_mcc_update(lower, draft)
        return {"intent": "update_mcc", "result": result, "answer": result["answer"]}

    if is_transaction_record_query(lower):
        result = handle_record_transaction(lower, draft, card_name)
        return {"intent": "record_transaction", "result": result, "answer": result["answer"]}

    if is_amount_confirmation_query(lower):
        result = handle_amount_confirmation(draft)
        return {"intent": "amount_confirmation", "result": result, "answer": result["answer"]}

    if is_nearby_store_query(lower):
        result = handle_nearby_store_advice(lower, draft)
        return {"intent": "nearby_store_advice", "result": result, "answer": result["answer"]}

    if is_mcc_lookup_query(lower):
        result = answer_mcc_lookup(merchant or _extract_mcc_lookup_term(lower), draft.channel)
        return {"intent": "mcc_lookup", "result": result, "answer": result["answer"]}

    if is_mcc_advice_query(lower):
        result = handle_mcc_advice(draft)
        return {"intent": "mcc_advice", "result": result, "answer": result["answer"]}

    if any(x in lower for x in [
        "nên chi", "nen chi", "thẻ nào", "the nao", "thẻ gì", "the gi",
        "nên dùng thẻ", "nen dung the", "tốt nhất", "tot nhat",
        "sắp tiêu", "sap tieu", "sắp chi", "sap chi",
        "sắp mua", "sap mua", "mua sắm", "mua sam"
    ]):
        if not amount:
            raise CashbackError("Bạn cho mình số tiền dự kiến chi tiêu nhé.")
        if should_split_payment_methods(draft):
            result = simulate_payment_method_recommendations(draft)
            return {"intent": "recommend_payment_methods", "result": result, "answer": answer_payment_method_recommendations(result)}
        result = simulate_recommendation(draft)
        return {"intent": "recommend", "result": result, "answer": answer_recommendation(result)}

    if any(x in lower for x in ["đã hoàn", "da hoan", "còn có thể", "con co the", "tiến độ", "tien do", "hoàn thêm", "hoan them"]):
        if not card_name:
            raise CashbackError("Bạn muốn xem tiến độ của thẻ nào?")
        result = card_progress(card_name, draft.date)
        return {"intent": "progress", "result": result, "answer": answer_progress(result)}

    if any(x in lower for x in ["có hoàn", "co hoan", "mcc", "lĩnh vực này", "linh vuc nay", "hoàn tiền cho", "hoan tien cho"]):
        if not card_name:
            raise CashbackError("Bạn muốn kiểm tra thẻ nào?")
        result = check_card_coverage(card_name, draft)
        return {"intent": "coverage", "result": result, "answer": answer_coverage(result)}

    raise CashbackError(
        "Mình chưa hiểu câu hỏi. Hãy hỏi theo dạng: sắp tiêu X tại lĩnh vực Y; thẻ Z đã hoàn bao nhiêu; hoặc thẻ A có hoàn cho MCC/lĩnh vực này không."
    )


def handle_llm_intent(text: str) -> dict[str, Any] | None:
    parsed = llm_interpret(text) or deterministic_intent(text)
    if not parsed or parsed.get("intent") in {None, "unknown"}:
        return None

    intent = parsed.get("intent")
    draft = TransactionDraft(
        amount=int(parsed.get("amount") or 0),
        category=parsed.get("category"),
        merchant=parsed.get("merchant"),
        mcc=str(parsed.get("mcc")) if parsed.get("mcc") else None,
        channel=parsed.get("payment_method"),
        date=parsed.get("date"),
    )

    if intent == "mcc_lookup":
        result = answer_mcc_lookup(parsed.get("merchant"), parsed.get("payment_method"))
        return {"intent": intent, "result": result, "answer": result["answer"]}
    if intent == "merchant_mcc_excluding":
        result = answer_merchant_mcc_excluding(parsed.get("merchant"), str(parsed.get("exclude_mcc") or ""))
        return {"intent": intent, "result": result, "answer": result["answer"]}
    if intent == "merchant_addresses":
        result = answer_merchant_addresses(parsed.get("merchant"), parsed.get("area"))
        return {"intent": intent, "result": result, "answer": result["answer"]}
    if intent == "recommend_card":
        if not draft.amount:
            raise CashbackError("Bạn cho mình số tiền dự kiến chi tiêu nhé.")
        if should_split_payment_methods(draft):
            result = simulate_payment_method_recommendations(draft)
            return {"intent": intent, "result": result, "answer": answer_payment_method_recommendations(result)}
        result = simulate_recommendation(draft)
        return {"intent": intent, "result": result, "answer": answer_recommendation(result)}
    if intent == "record_transaction":
        card = parsed.get("card")
        if not card:
            raise CashbackError("Bạn muốn nhập giao dịch vào thẻ nào?")
        result = handle_record_transaction(text, draft, card)
        return {"intent": intent, "result": result, "answer": result["answer"]}
    if intent == "delete_transaction":
        result = handle_delete_transaction(str(parsed.get("transaction_id") or "latest"))
        return {"intent": intent, "result": result, "answer": result["answer"]}
    if intent == "update_mcc":
        result = handle_mcc_update(text, draft)
        return {"intent": intent, "result": result, "answer": result["answer"]}
    if intent == "nearby_store_advice":
        result = handle_nearby_store_advice(text, draft)
        return {"intent": intent, "result": result, "answer": result["answer"]}
    if intent == "progress":
        card = parsed.get("card")
        if not card:
            raise CashbackError("Bạn muốn xem tiến độ của thẻ nào?")
        progress = card_progress(card, parsed.get("date"))
        return {"intent": intent, "result": progress, "answer": answer_progress(progress)}
    if intent == "coverage":
        card = parsed.get("card")
        if not card:
            raise CashbackError("Bạn muốn kiểm tra thẻ nào?")
        result = check_card_coverage(card, draft)
        return {"intent": intent, "result": result, "answer": answer_coverage(result)}
    return None


def deterministic_intent(text: str) -> dict[str, Any] | None:
    lower = normalize_text(text)
    exclude = re.search(r"(?:ngoài|ngoai)\s+(\d{4})", lower)
    if "mcc" in lower and exclude:
        return {
            "intent": "merchant_mcc_excluding",
            "merchant": _extract_merchant_excluding_term(lower) or _extract_merchant(lower) or _extract_mcc_lookup_term(lower),
            "exclude_mcc": exclude.group(1),
        }
    if any(token in lower for token in ["địa chỉ", "dia chi", "address", "ở đâu", "o dau"]) or _extract_merchant_area_query(lower):
        merchant_area = _extract_merchant_area_query(lower)
        return {
            "intent": "merchant_addresses",
            "merchant": (merchant_area or {}).get("merchant") or _extract_merchant(lower) or _extract_address_merchant(lower),
            "area": (merchant_area or {}).get("area") or _extract_area(lower),
        }
    return None


def is_mcc_lookup_query(text: str) -> bool:
    return "mcc" in text and any(token in text for token in [
        "là gì", "la gi", "bao nhiêu", "bao nhieu", "mã", "ma", "của", "cua",
        "mấy", "may", "những", "nhung", "nào", "nao", "có mcc", "co mcc"
    ])


def is_transaction_record_query(text: str) -> bool:
    return any(token in text for token in ["nhập giao dịch", "nhap giao dich", "thêm giao dịch", "them giao dich", "ghi giao dịch", "ghi giao dich", "đã tiêu", "da tieu", "vừa tiêu", "vua tieu"])


def is_transaction_delete_query(text: str) -> bool:
    return any(token in text for token in ["xóa giao dịch", "xoa giao dich", "xoá giao dịch", "delete transaction"])


def is_mcc_update_query(text: str) -> bool:
    return any(token in text for token in ["cập nhật mcc", "cap nhat mcc", "update mcc", "sửa mcc", "sua mcc", "thêm mcc", "them mcc", "nhập mcc", "nhap mcc"])


def is_mcc_advice_query(text: str) -> bool:
    return "mcc" in text and any(token in text for token in ["nên là", "nen la", "hợp lý", "hop ly", "tư vấn", "tu van", "sắp mua", "sap mua"])


def is_nearby_store_query(text: str) -> bool:
    return any(token in text for token in ["gần tôi", "gan toi", "gần", "gan", "khu vực", "khu vuc"]) and any(token in text for token in ["cửa hàng", "cua hang", "mua ở đâu", "mua o dau", "ở đâu", "o dau"])


def is_amount_confirmation_query(text: str) -> bool:
    return any(token in text for token in ["giá trị giao dịch", "gia tri giao dich", "số tiền giao dịch", "so tien giao dich"]) and _parse_amount(text) is not None


def handle_record_transaction(text: str, draft: TransactionDraft, card_name: str | None) -> dict[str, Any]:
    if not card_name:
        raise CashbackError("Mình đã hiểu đây là giao dịch cần lưu. Bạn muốn nhập giao dịch này vào thẻ nào?")
    if not draft.amount:
        raise CashbackError("Mình đã hiểu đây là giao dịch cần lưu. Bạn cho mình số tiền giao dịch để lưu nhé.")
    if not draft.merchant:
        raise CashbackError("Mình đã hiểu đây là giao dịch cần lưu. Bạn cho mình tên merchant/cửa hàng của giao dịch nhé.")
    txn = record_transaction(
        {
            "card_id": card_name,
            "amount": draft.amount,
            "merchant": draft.merchant,
            "mcc": draft.mcc,
            "channel": draft.channel or "pos",
            "category": draft.category,
            "date": draft.date or today_iso(),
            "note": "created by chatbot",
        }
    )
    return {
        "transaction": txn,
        "answer": f"Đã lưu giao dịch {txn['id']}: {txn['merchant_name']} {money(txn['amount'])}, MCC {txn.get('mcc') or 'chưa rõ'}, phương thức {txn['payment_method']}.",
    }


def handle_amount_confirmation(draft: TransactionDraft) -> dict[str, Any]:
    latest = get_latest_transaction()
    if not latest:
        return {"answer": "Mình chưa thấy giao dịch nào gần đây để đối chiếu số tiền.", "transaction": None}
    stated_amount = draft.amount
    current_amount = int(latest.get("amount") or 0)
    if stated_amount == current_amount:
        answer = (
            f"Đúng rồi, giao dịch gần nhất đang lưu là {money(current_amount)} "
            f"tại {latest.get('merchant_name') or latest.get('merchant') or 'merchant chưa rõ'}."
        )
    else:
        answer = (
            f"Mình đang thấy giao dịch gần nhất lưu {money(current_amount)}, còn bạn vừa nhắc {money(stated_amount)}. "
            "Nếu giao dịch vừa nhập sai, bạn có thể bảo mình xóa giao dịch gần nhất rồi nhập lại."
        )
    return {"answer": answer, "transaction": latest}


def handle_delete_transaction(text: str) -> dict[str, Any]:
    txn_id = _extract_transaction_id(text)
    deleted = delete_transaction(txn_id)
    return {
        "transaction": deleted,
        "answer": f"Đã xóa giao dịch {deleted['id']}: {deleted['merchant_name']} {money(deleted['amount'])}.",
    }


def handle_mcc_update(text: str, draft: TransactionDraft) -> dict[str, Any]:
    mcc = draft.mcc or _extract_mcc_value(text)
    if not mcc:
        raise CashbackError("Bạn muốn cập nhật MCC nào? Ví dụ: cập nhật MCC WINMART thành 5411.")
    txn_id = _extract_transaction_id(text)
    if txn_id:
        txn = update_transaction_mcc(txn_id, mcc, draft.category)
        return {
            "transaction": txn,
            "answer": f"Đã cập nhật giao dịch {txn['id']} sang MCC {txn['mcc']}.",
        }
    merchant = draft.merchant or _extract_mcc_update_merchant(text)
    if not merchant:
        raise CashbackError("Bạn muốn thêm/cập nhật MCC cho merchant nào?")
    row = upsert_merchant_mcc(
        {
            "merchant_name": merchant,
            "mcc": mcc,
            "payment_method": draft.channel or "any",
            "category": draft.category,
            "note": "updated by chatbot",
        }
    )
    return {
        "merchant_mcc": row,
        "answer": f"Đã cập nhật database MCC: {row['merchant_name']} -> MCC {row['mcc']} ({row['payment_method']}).",
    }


def handle_mcc_advice(draft: TransactionDraft) -> dict[str, Any]:
    if draft.merchant:
        return answer_mcc_lookup(draft.merchant, draft.channel)
    if draft.category:
        rows = search_merchant_mcc(draft.category, limit=5)
    else:
        rows = []
    if rows:
        options = "; ".join(f"{row['merchant_name']} MCC {row['mcc']}" for row in rows)
        return {"rows": rows, "answer": f"MCC gợi ý dựa trên database: {options}."}
    return {"rows": [], "answer": "Mình chưa đủ dữ liệu để tư vấn MCC. Hãy cho merchant hoặc lĩnh vực cụ thể hơn."}


def handle_nearby_store_advice(text: str, draft: TransactionDraft) -> dict[str, Any]:
    area = _extract_area(text)
    mcc = draft.mcc
    if not mcc and draft.category:
        # Use known card rules to infer useful MCCs for that category if possible.
        for card in load_cards():
            for rule in card.get("cashback_rules", []):
                if draft.category in {normalize_text(x) for x in rule.get("categories", [])} and rule.get("mcc"):
                    mcc = str(rule["mcc"][0])
                    break
            if mcc:
                break
    rows = search_merchant_mcc_by_address(area, mcc=mcc, payment_method=draft.channel, limit=8)
    if not rows:
        return {"rows": [], "answer": "Mình chưa tìm thấy cửa hàng phù hợp trong database địa chỉ/MCC hiện có."}
    options = []
    for row in rows:
        recommendation = simulate_recommendation(
            TransactionDraft(
                amount=draft.amount or 200000,
                merchant=row["merchant_name"],
                mcc=row["mcc"],
                channel=row["payment_method"],
                date=draft.date,
            )
        )
        best = recommendation.get("best") or {}
        options.append(
            f"{row['merchant_name']} ({row.get('address') or 'không có địa chỉ'}, MCC {row['mcc']}) - nên dùng {best.get('card_name', 'chưa rõ')}"
        )
    return {"rows": rows, "answer": "Gợi ý gần khu vực bạn nêu: " + "; ".join(options)}


def answer_merchant_mcc_excluding(merchant: str | None, exclude_mcc: str) -> dict[str, Any]:
    if not merchant:
        raise CashbackError("Bạn muốn kiểm tra MCC khác của merchant nào?")
    rows = search_merchant_mcc(merchant, limit=100)
    filtered = [row for row in rows if str(row["mcc"]) != str(exclude_mcc)]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in filtered:
        grouped.setdefault(str(row["mcc"]), []).append(row)
    if not grouped:
        return {
            "rows": [],
            "answer": f"Mình chưa thấy MCC nào khác {exclude_mcc} cho {merchant} trong database.",
        }
    parts = []
    for mcc, items in sorted(grouped.items()):
        examples = ", ".join((item.get("address") or item.get("merchant_full_name") or item["merchant_name"]) for item in items[:3])
        parts.append(f"MCC {mcc}: {len(items)} dòng, ví dụ {examples}")
    return {
        "rows": filtered,
        "answer": f"Ngoài MCC {exclude_mcc}, {merchant} còn có trong database: " + "; ".join(parts),
    }


def answer_merchant_addresses(merchant: str | None, area: str | None = None) -> dict[str, Any]:
    if not merchant:
        raise CashbackError("Bạn muốn hỏi địa chỉ của cửa hàng/merchant nào?")
    rows = search_merchant_mcc(merchant, limit=100)
    if area:
        area_key = normalize_area(area)
        rows = [
            row for row in rows
            if area_key in normalize_area(row.get("address")) or area_key in normalize_area(row.get("merchant_full_name"))
        ]
    if not rows:
        return {"rows": [], "answer": f"Mình chưa tìm thấy địa chỉ của {merchant} trong database."}
    parts = []
    for row in rows[:10]:
        address = row.get("address") or row.get("merchant_full_name") or "không có địa chỉ"
        parts.append(f"{row['merchant_name']} - {address} - MCC {row['mcc']} ({row['payment_method']})")
    suffix = "" if len(rows) <= 10 else f" Và còn {len(rows) - 10} dòng khác."
    return {
        "rows": rows,
        "displayed_rows": min(10, len(rows)),
        "answer": f"Địa chỉ/kết quả cho {merchant}: " + "; ".join(parts) + suffix,
    }


def normalize_area(value: str | None) -> str:
    text = normalize_text(value)
    text = re.sub(r"\bquận\s*(\d+)\b", r"q\1", text)
    text = re.sub(r"\bquan\s*(\d+)\b", r"q\1", text)
    return text


def answer_mcc_lookup(term: str | None, channel: str | None = None) -> dict[str, Any]:
    if not term:
        raise CashbackError("Bạn muốn tra MCC của cửa hàng/website nào?")
    exact = lookup_merchant_mcc(term, channel)
    if exact:
        related_rows = search_merchant_mcc(term, limit=100)
        answer = _format_merchant_mcc_summary(exact["merchant_name"], related_rows)
        if len(related_rows) > 1:
            answer += " Bạn có thể hỏi 'ngoài ra còn địa chỉ/MCC nào khác không?' để xem thêm."
        return {"match": exact, "alternatives": [], "rows": related_rows, "displayed_rows": 1, "answer": answer}

    alternatives = search_merchant_mcc(term, limit=5)
    if alternatives:
        names = "; ".join(f"{row['merchant_name']} MCC {row['mcc']} ({row['payment_method']})" for row in alternatives)
        return {
            "match": None,
            "alternatives": alternatives,
            "rows": alternatives,
            "displayed_rows": len(alternatives),
            "answer": f"Không có match chính xác cho {term}. Kết quả gần đúng trong database: {names}.",
        }
    return {
        "match": None,
        "alternatives": [],
        "rows": [],
        "displayed_rows": 0,
        "answer": f"Không tìm thấy {term} trong database MCC. Nếu muốn, hãy dùng nút Thêm MCC để bổ sung.",
    }


def _format_merchant_mcc_summary(merchant_name: str, rows: list[dict[str, Any]]) -> str:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("mcc") or "-"), []).append(row)
    ordered = sorted(grouped.items(), key=lambda item: len(item[1]), reverse=True)
    total = sum(len(items) for _, items in ordered)
    if not ordered:
        return f"Không tìm thấy MCC của {merchant_name} trong database."
    parts = []
    for index, (mcc, items) in enumerate(ordered):
        share = len(items) / total if total else 0
        if index == 0 and len(ordered) > 1 and share >= 0.5:
            label = "phần lớn cửa hàng"
        elif len(items) == 1:
            label = "một số ít cửa hàng"
        else:
            label = "một số cửa hàng"
        examples = ", ".join(
            (item.get("address") or item.get("merchant_full_name") or item.get("merchant_name") or "").strip()
            for item in items[:3]
        )
        example_text = f", ví dụ {examples}" if examples else ""
        parts.append(f"{label} có MCC {mcc} ({len(items)} dòng{example_text})")
    return f"{merchant_name} có {len(ordered)} MCC trong database: " + "; ".join(parts) + "."


def answer_recommendation(result: dict[str, Any]) -> str:
    best = result["best"]
    if not best:
        return "Chưa có thẻ nào để gợi ý."
    if best["cashback_gain"] == 0 and best["potential_cashback_gain"] == 0 and not best.get("has_direct_match"):
        lines = [f"Không có thẻ nào hoàn tiền trực tiếp cho giao dịch này."]
        if best.get("threshold_help"):
            lines.append(
                f"Nên dùng {best['card_name']} vì thẻ này có điều kiện tổng chi tiêu; giao dịch sẽ giúp giảm phần còn thiếu {money(best['threshold_gap_reduction'])}, còn cần {money(best['min_spend_gap_after'])} để đủ điều kiện hoàn tiền."
            )
        else:
            lines.append(
                f"Nên dùng {best['card_name']} chỉ như lựa chọn theo dõi chi tiêu, vì hiện chưa có rule hoàn tiền phù hợp trong dữ liệu."
            )
        return _with_data_note(" ".join(lines), result["data_match_note"])
    lines = [f"Nên dùng {best['card_name']}: {_cashback_sentence(best)}"]
    if best["cashback_gain"] == 0 and best["potential_cashback_gain"] > 0:
        lines.append(f"Tiền hoàn này đang chờ điều kiện tổng chi tiêu, hiện còn thiếu {money(best['min_spend_gap_after'])}.")
    alternatives = [x for x in result["cards"][1:4] if x["potential_cashback_gain"] > 0 or x["cashback_gain"] > 0]
    if alternatives:
        lines.append("Các lựa chọn khác: " + "; ".join(f"{x['card_name']} ({_inline_sentence(_cashback_sentence(x))})" for x in alternatives))
    return _with_data_note(" ".join(lines), result["data_match_note"])


def answer_payment_method_recommendations(result: dict[str, Any]) -> str:
    online = _inline_sentence(answer_recommendation(result["online"]))
    pos = _inline_sentence(answer_recommendation(result["pos"]))
    return (
        "Nếu thanh toán online: "
        f"{online}\n"
        "Nếu quẹt POS/offline: "
        f"{pos}"
    )


def _cashback_sentence(row: dict[str, Any]) -> str:
    gain = int(row.get("cashback_gain") or 0)
    potential_gain = int(row.get("potential_cashback_gain") or 0)
    amount = gain or potential_gain
    rate = float(row.get("primary_rate") or 0)
    raw = int(row.get("direct_raw_cashback") or 0)
    if row.get("period_cap_limited") and amount > 0:
        return f"có thể hoàn {money(amount)} vì đã chạm hạn mức hoàn tối đa của tháng."
    if rate and amount > 0:
        rate_text = f"{rate * 100:g}%"
        if row.get("cashback_rounded") and row.get("round_down_to"):
            return (
                f"hoàn {rate_text}, tương ứng {money(raw)} trước làm tròn. "
                f"Với tổng cashback hiện tại của tháng, số được ghi nhận thêm là {money(amount)} "
                f"vì cashback tháng được làm tròn xuống bội số {money(row['round_down_to'])}."
            )
        if raw and raw != amount:
            return f"hoàn {rate_text}; sau giới hạn còn {money(amount)}."
        return f"hoàn {rate_text}, tương ứng {money(amount)}."
    if amount > 0:
        return f"có thể hoàn {money(amount)}."
    return row.get("note") or "chưa tạo thêm tiền hoàn."


def _with_data_note(answer: str, note: str) -> str:
    if not note:
        return answer
    return f"{answer}\n\n_Note: {note}_"


def _inline_sentence(text: str) -> str:
    return text.strip().rstrip(".")


def answer_progress(result: dict[str, Any]) -> str:
    lines = [
        f"{result['card_name']} trong kỳ {result['period']}:",
        f"tổng chi tiêu {money(result['total_spend'])},",
        f"tiền hoàn đã đủ điều kiện {money(result['earned_cashback'])}.",
    ]
    if not result["qualified"]:
        lines.append(f"Cần chi thêm {money(result['min_spend_gap'])} để đạt điều kiện hoàn tiền.")
        if result["potential_cashback"]:
            lines.append(f"Tiền hoàn đang chờ điều kiện: {money(result['potential_cashback'])}.")
    if result["remaining_period_cap"] is not None:
        lines.append(f"Cap còn lại ước tính: {money(result['remaining_period_cap'])}.")
    return " ".join(lines)


def answer_coverage(result: dict[str, Any]) -> str:
    if result["covered"]:
        return f"Có. {result['card_name']} khớp rule: {', '.join(result['matched_rules'])}. {result['data_match_note']}"
    if result["alternatives"]:
        names = "; ".join(f"{x['card_name']} ({', '.join(x['rules'])})" for x in result["alternatives"])
        return f"Không. {result['card_name']} không hoàn cho giao dịch này. Gợi ý thẻ khác: {names}. {result['data_match_note']}"
    return f"Không. {result['card_name']} không hoàn cho giao dịch này và chưa có thẻ thay thế phù hợp trong dữ liệu. {result['data_match_note']}"


def _parse_amount(text: str) -> int | None:
    money_pattern = r"(\d+(?:[.,]\d+)?)\s*(tr|triệu|trieu|m|k|nghìn|nghin|ngàn|ngan|đ|d|vnd|vnđ)"
    candidates = list(re.finditer(money_pattern, text))
    for match in candidates:
        prefix = text[max(0, match.start() - 8):match.start()]
        if re.search(r"mcc\s*$", prefix):
            continue
        number = float(match.group(1).replace(",", "."))
        unit = match.group(2) or ""
        if unit in {"tr", "triệu", "trieu", "m"}:
            return int(number * 1_000_000)
        if unit in {"k", "nghìn", "nghin", "ngàn", "ngan"}:
            return int(number * 1_000)
        return int(number)
    match = re.search(r"(?:số tiền|so tien|giá trị|gia tri|amount)\s*(?:là|la|:)?\s*(\d+(?:[.,]\d+)?)", text)
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    unit = ""
    if unit in {"tr", "triệu", "trieu", "m"}:
        return int(number * 1_000_000)
    if unit in {"k", "nghìn", "nghin", "ngàn", "ngan"}:
        return int(number * 1_000)
    return int(number)


def _extract_card(text: str) -> str | None:
    for card in load_cards():
        aliases = [card["id"], card["name"], *card.get("aliases", [])]
        if any(normalize_text(alias) in text for alias in aliases):
            return card["id"]
    fallback_aliases = {
        "sacombank": "sacombank-platinum-cashback",
        "sea bank": "seabank-seaeasy",
        "seabank": "seabank-seaeasy",
        "cake": "cake-cashback",
    }
    for alias, card_id in fallback_aliases.items():
        if alias in text:
            return card_id
    return None


def _extract_category(text: str) -> str | None:
    mapping = {
        "shopee": ["shopee"],
        "online": ["online", "trực tuyến", "truc tuyen", "internet"],
        "shopping": ["mua sắm", "mua sam", "shopping"],
        "dining": ["ăn uống", "an uong", "nhà hàng", "nha hang", "cafe", "restaurant"],
        "grocery": ["siêu thị", "sieu thi", "grocery", "tạp hóa", "tap hoa"],
        "fuel": ["xăng", "xang", "fuel"],
        "travel-agency": ["vé máy bay", "ve may bay", "đại lý du lịch", "dai ly du lich", "travel agency"],
        "travel": ["du lịch", "du lich", "khách sạn", "khach san", "travel"],
        "entertainment": ["giải trí", "giai tri", "cinema", "phim"],
    }
    for category, aliases in mapping.items():
        if any(alias in text for alias in aliases):
            return category
    return None


def _extract_merchant(text: str) -> str | None:
    record_match = re.search(
        r"(?:thêm giao dịch|them giao dich|nhập giao dịch|nhap giao dich|ghi giao dịch|ghi giao dich)\s*:?\s*(.+)",
        text,
    )
    if record_match:
        tail = record_match.group(1)
        merchant_part = re.split(
            r"\s*,\s*|\s+mcc\s*\d{4}|\s+(?:online|pos|trực tuyến|truc tuyen|quẹt|quet)\b|\s+\d+(?:[.,]\d+)?\s*(?:tr|triệu|trieu|m|k|nghìn|nghin|ngàn|ngan|đ|d|vnd|vnđ)\b|\s+(?:vào|vao)\s+(?:hôm nay|hom nay|ngày|ngay|thẻ|the)\b",
            tail,
            maxsplit=1,
        )[0].strip(" ?.,:")
        if merchant_part:
            return merchant_part
    known_merchants = ["shopee", "lazada", "tiki", "grab", "be group", "gojek", "con cung"]
    for merchant in known_merchants:
        if re.search(rf"\b{re.escape(merchant)}\b", text):
            return merchant
    if re.search(r"\bbe\b", text):
        return "be"
    match = re.search(
        r"(?:tại|tai|ở|o)\s+([a-z0-9][a-z0-9\s&.-]{1,40}?)(?:\s+(?:online|pos|trực tuyến|truc tuyen|quẹt|quet|thì|thi|nên|nen|dùng|dung|,|\?|$)|,|\?|$)",
        text,
    )
    if match:
        return match.group(1).strip()
    return None


def _extract_mcc_lookup_term(text: str) -> str | None:
    patterns = [
        r"mcc\s+(?:của|cua)\s+(.+?)(?:\s+(?:là|la|bao|mã|ma)|\?|$)",
        r"(.+?)\s+(?:có|co)\s+(?:mấy|may|những|nhung|các|cac)?\s*mcc(?:\s+(?:nào|nao))?",
        r"(.+?)\s+(?:có|co)?\s*mcc\s+(?:là|la|bao|mã|ma|gì|gi)",
        r"mã\s+mcc\s+(?:của|cua)\s+(.+?)(?:\?|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            term = match.group(1).strip(" ?.,")
            term = re.sub(r"^(cửa hàng|cua hang|website|merchant)\s+", "", term).strip()
            if term:
                return term
    return None


def _extract_address_merchant(text: str) -> str | None:
    patterns = [
        r"(?:địa chỉ|dia chi|address)\s+(?:của|cua)?\s*(.+?)(?:\?|$)",
        r"(.+?)\s+(?:ở đâu|o dau|địa chỉ|dia chi)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            term = match.group(1).strip(" ?.,")
            term = re.sub(r"^(cửa hàng|cua hang|website|merchant)\s+", "", term).strip()
            if term:
                return term
    return None


def _extract_merchant_excluding_term(text: str) -> str | None:
    patterns = [
        r"(?:ngoài|ngoai)\s+\d{4}\s+(?:ra\s+)?(?:thì|thi)?\s*(.+?)\s+(?:còn|con)\s+(?:có|co)?\s*mcc",
        r"(.+?)\s+(?:còn|con)\s+(?:có|co)?\s*mcc\s+(?:nào|nao)\s+(?:khác|khac)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            term = match.group(1).strip(" ?.,")
            if term:
                return term
    return None


def _extract_transaction_id(text: str) -> str | None:
    match = re.search(r"(txn-\d+|sample-\d+)", text)
    if match:
        return match.group(1)
    if any(token in text for token in ["cuối", "cuoi", "gần nhất", "gan nhat", "last", "latest"]):
        return "latest"
    return None


def _extract_mcc_update_merchant(text: str) -> str | None:
    patterns = [
        r"(?:mcc\s+)?(.+?)\s+(?:thành|thanh|là|la)\s+\d{4}",
        r"(?:cho|của|cua)\s+(.+?)\s+(?:thành|thanh|là|la|mcc)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            merchant = match.group(1).strip(" ?.,")
            merchant = re.sub(r"^(cập nhật|cap nhat|update|sửa|sua|thêm|them|nhập|nhap)\s+mcc\s+", "", merchant).strip()
            if merchant:
                return merchant
    return None


def _extract_area(text: str) -> str | None:
    patterns = [
        r"(?:gần|gan|khu vực|khu vuc|ở|o)\s+([a-z0-9\s.-]{2,40}?)(?:\s+(?:có|co|mcc|để|de|nên|nen|,|\?|$))",
        r"(q\d+|quận\s*\d+|quan\s*\d+|crescent mall|vincom|lâm văn bền|lam van ben)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip(" ?.,")
    return None


def _extract_merchant_area_query(text: str) -> dict[str, str] | None:
    patterns = [
        r"(?:nhà tôi|nha toi)\s+(?:ở|o)\s+(.+?),\s*(.+?)\s+(?:nào|nao)",
        r"(.+?)\s+(?:nào|nao)\s+(?:ở|o)\s+(.+?)(?:\?|$)",
        r"(.+?)\s+(?:ở|o)\s+(q\d+|quận\s*\d+|quan\s*\d+)(?:\?|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        if pattern.startswith("(?:nh"):
            area = match.group(1).strip(" ?.,")
            merchant = match.group(2).strip(" ?.,")
        else:
            merchant = match.group(1).strip(" ?.,")
            area = match.group(2).strip(" ?.,")
        merchant = re.sub(r"^(cửa hàng|cua hang)\s+", "", merchant).strip()
        if merchant and area:
            return {"merchant": merchant, "area": area}
    return None


def _extract_mcc(text: str) -> str | None:
    match = re.search(r"mcc\s*(\d{4})", text)
    return match.group(1) if match else None


def _extract_mcc_value(text: str) -> str | None:
    match = re.search(r"(?:mcc|thành|thanh|là|la)\s*(\d{4})", text)
    return match.group(1) if match else None


def _extract_channel(text: str) -> str | None:
    if any(x in text for x in ["online", "trực tuyến", "truc tuyen"]):
        return "online"
    if any(x in text for x in ["pos", "quẹt", "quet", "offline"]):
        return "pos"
    return None


def _extract_date(text: str) -> str | None:
    if any(token in text for token in ["hôm nay", "hom nay", "today"]):
        return today_iso()
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    return match.group(1) if match else None
