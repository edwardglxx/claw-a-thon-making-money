from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import replace_merchant_mcc  # noqa: E402

DATA_DIR = ROOT / "data"
DEFAULT_PDF = DATA_DIR / "vib_danh_sach_2_merchants.pdf"
OUTPUT_CSV = DATA_DIR / "vib_danh_sach_2_merchants.csv"
SOURCE_NAME = "vib_danh_sach_2_merchants.pdf"


def clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def parse_pdf(path: Path) -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    pattern = re.compile(r"^\s*(\d{1,3},\d{3})\s+(.+?)\s+(POS)\s+(\d{4})\s*$", flags=re.I)
    reader = PdfReader(path)
    for page in reader.pages:
        for line in (page.extract_text() or "").splitlines():
            match = pattern.match(clean(line))
            if not match:
                continue
            stt, merchant, method, mcc = match.groups()
            payment_method = "pos" if method.lower() == "pos" else "any"
            key = (merchant.upper(), payment_method, mcc)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "merchant_full_name": merchant,
                    "merchant_name": merchant,
                    "payment_method": payment_method,
                    "mcc": mcc,
                    "note": f"STT {stt}" if stt else None,
                    "source": SOURCE_NAME,
                }
            )
    return rows


def write_csv(rows: list[dict]) -> None:
    with OUTPUT_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "merchant_full_name",
                "merchant_name",
                "payment_method",
                "mcc",
                "note",
                "source",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    rows = parse_pdf(path)
    write_csv(rows)
    count = replace_merchant_mcc(rows, source=SOURCE_NAME)
    print(f"Parsed {len(rows)} rows from {path}.")
    print(f"Imported {count} merchant MCC rows with source={SOURCE_NAME}.")
    print(f"Wrote {OUTPUT_CSV}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
