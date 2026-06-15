from __future__ import annotations

import json
import os
import re
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from cashback_agent import (
    CashbackError,
    TransactionDraft,
    card_progress,
    check_card_coverage,
    load_cards,
    load_transactions,
    parse_vietnamese_query,
    record_transaction,
    simulate_recommendation,
)
from database import (
    delete_transaction,
    init_database,
    insert_merchant_mcc,
    list_transactions,
    lookup_merchant_mcc,
    schema_summary,
    search_merchant_mcc,
    update_transaction_mcc,
    upsert_card,
    upsert_merchant_mcc,
)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
PENDING_CHAT_QUESTION: str | None = None
LAST_CHAT_RESULT: dict | None = None
FOLLOWUP_OFFSET = 0


def looks_like_amount_only(text: str) -> bool:
    return bool(re.fullmatch(r"\s*\d+(?:[.,]\d+)?\s*(k|nghìn|nghin|ngàn|ngan|tr|triệu|trieu|m)?\s*", text.lower()))


def missing_amount_error(message: str) -> bool:
    return "số tiền" in message or "so tien" in message


def missing_required_info_error(message: str) -> bool:
    return missing_amount_error(message) or "thẻ nào" in message or "the nao" in message or "merchant" in message


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
                progress = card_progress(card_id, qs.get("date", [None])[0])
                txns = [txn for txn in list_transactions() if txn.get("card_id") == progress["card_id"]]
                self._json(200, {"progress": progress, "transactions": txns})
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
        global PENDING_CHAT_QUESTION, LAST_CHAT_RESULT, FOLLOWUP_OFFSET
        try:
            if self.path == "/api/ask" or self.path == "/invocations":
                payload = self._body()
                question = payload.get("question") or payload.get("input") or payload.get("prompt")
                if not question:
                    raise CashbackError("Thiếu question/input.")
                question_text = str(question)
                if PENDING_CHAT_QUESTION and looks_like_amount_only(question_text):
                    question_text = f"{PENDING_CHAT_QUESTION} {question_text}"
                    PENDING_CHAT_QUESTION = None
                elif should_extend_pending(PENDING_CHAT_QUESTION, question_text):
                    question_text = f"{PENDING_CHAT_QUESTION} {question_text}"
                    PENDING_CHAT_QUESTION = None
                if LAST_CHAT_RESULT and is_more_followup(question_text):
                    followup = format_followup(LAST_CHAT_RESULT, FOLLOWUP_OFFSET)
                    if followup:
                        FOLLOWUP_OFFSET += len(followup.get("result", {}).get("rows", []))
                        self._json(200, followup)
                        return
                try:
                    result = parse_vietnamese_query(question_text)
                    PENDING_CHAT_QUESTION = None
                    result_body = result.get("result") or {}
                    LAST_CHAT_RESULT = result if result_body.get("rows") else None
                    FOLLOWUP_OFFSET = int(result_body.get("displayed_rows", 10)) if LAST_CHAT_RESULT else 0
                    self._json(200, result)
                except CashbackError as exc:
                    if missing_required_info_error(str(exc)):
                        PENDING_CHAT_QUESTION = question_text
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
            elif self.path == "/api/transactions/mcc":
                payload = self._body()
                self._json(200, {"transaction": update_transaction_mcc(payload["id"], payload["mcc"], payload.get("category"))})
            elif self.path == "/api/cards":
                self._json(200, {"card": upsert_card(self._body())})
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
