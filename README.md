# Cashback Agent

Web agent quản lý hoàn tiền thẻ tín dụng cho Claw-a-thon.

Agent hiện có:

- Rule engine tính hoàn tiền theo kỳ sao kê hoặc tháng dương lịch.
- Rule theo MCC, lĩnh vực, merchant và kênh thanh toán online/POS.
- Điều kiện tổng chi tiêu tối thiểu.
- Cap hoàn tiền theo kỳ và theo từng lĩnh vực.
- Rule làm tròn eligible spend theo bội số, ví dụ SeaBank Shopee bội số 50.000đ.
- Web UI dạng hỏi đáp.
- Endpoint `POST /invocations` để tương thích convention AgentBase SDK.

## Chạy local

```powershell
cd D:\Demo\claw-a-thon-making-money
python .\server.py
```

Mở:

- `http://127.0.0.1:8080`
- `http://127.0.0.1:8080/health`

Nếu port 8080 đang bận:

```powershell
$env:PORT=8000
python .\server.py
```

## Bật MiniMax cho chatbot

Chatbot dùng hybrid flow: MiniMax hiểu câu hỏi và trả intent JSON, còn Python/SQLite thực hiện truy vấn và tính cashback.

Tạo file `.env` từ `.env.example`:

```powershell
copy .env.example .env
```

Điền các biến cần thiết trong `.env`:

```text
LLM_BASE_URL=
LLM_MODEL=
LLM_API_KEY=
```

Nếu `.env` chưa có đủ `LLM_API_KEY` và `LLM_MODEL`, agent vẫn chạy bằng parser rule-based.

## Câu hỏi mẫu

```text
tôi sắp tiêu 2 triệu tại shopee online, nên dùng thẻ nào?
thẻ Cake tháng này đã hoàn bao nhiêu tiền, còn có thể hoàn thêm bao nhiêu?
thẻ Sacombank có hoàn tiền cho MCC 5812 không?
```

## Database

Agent dùng SQLite tại `data/cashback_agent.db`. Database được tự tạo và seed từ `data/cards.json` + `data/transactions.json` ở lần chạy đầu tiên.

Schema chính:

### `cards`

Lưu thông tin thẻ:

- `id`
- `name`
- `statement_type`: `calendar_month` hoặc `statement_cycle`
- `statement_close_day`: ngày chốt sao kê
- `cashback_close_day`: ngày chốt/ghi nhận hoàn tiền
- `credit_limit`: hạn mức thẻ
- `min_total_spend`: tổng chi tiêu tối thiểu để được hoàn
- `period_cap`: cap hoàn tiền toàn kỳ
- `cashback_rules_json`: quy định hoàn tiền dạng JSON
- `aliases_json`: tên gọi tắt để agent nhận diện

### `transactions`

Lưu giao dịch được input:

- `id`
- `card_id`
- `transaction_date`: ngày giao dịch
- `amount`: số tiền giao dịch
- `merchant_name`: tên merchant
- `mcc`: mã MCC
- `payment_method`: `online` hoặc `pos`
- `category`: lĩnh vực
- `note`

## API

### `POST /api/ask`

```json
{ "question": "tôi sắp tiêu 2 triệu tại shopee online, nên dùng thẻ nào?" }
```

### `POST /api/recommend`

```json
{
  "amount": 2000000,
  "category": "shopee",
  "merchant": "Shopee",
  "channel": "online",
  "mcc": "5311"
}
```

### `GET /api/progress`

Trả tiến độ hoàn tiền của toàn bộ thẻ trong kỳ hiện tại.

### `GET /api/schema`

Trả thông tin database và danh sách cột chính.

### `POST /api/cards`

Tạo hoặc cập nhật thẻ:

```json
{
  "id": "my-card",
  "name": "My Cashback Card",
  "statement": { "type": "statement_cycle", "close_day": 25 },
  "cashback_close_day": 5,
  "credit_limit": 50000000,
  "min_total_spend": 5000000,
  "period_cap": 600000,
  "aliases": ["mycard"],
  "cashback_rules": [
    {
      "name": "5% online",
      "rate": 0.05,
      "channels": ["online"],
      "cap_per_period": 600000,
      "cap_key": "online"
    }
  ]
}
```

### `POST /api/transactions`

Ghi nhận giao dịch mới vào `data/transactions.json`.

```json
{
  "card_id": "cake-cashback",
  "amount": 1200000,
  "category": "dining",
  "merchant": "Pizza 4P",
  "mcc": "5812",
  "channel": "pos",
  "date": "2026-06-10"
}
```

## Thêm thẻ thật

Sửa `data/cards.json`. Mỗi thẻ có thể có:

- `statement.type`: `calendar_month` hoặc `statement_cycle`
- `statement.close_day`: ngày chốt sao kê nếu dùng `statement_cycle`
- `min_total_spend`: tổng chi tiêu tối thiểu để được hoàn
- `period_cap`: cap hoàn tiền toàn kỳ
- `cashback_rules`: danh sách rule theo `rate`, `categories`, `mcc`, `channels`, `merchants`, `cap_per_period`, `round_eligible_spend_to`

## Deploy AgentBase

Container phải listen port `8080` và có `GET /health` trả 200. Dockerfile hiện đã đáp ứng contract này.
