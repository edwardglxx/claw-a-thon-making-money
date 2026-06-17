from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import replace_mcc_categories  # noqa: E402

DATA_DIR = ROOT / "data"
VIB_PDF = DATA_DIR / "vib_mcc_super_card.pdf"
SACOMBANK_PDF = DATA_DIR / "sacombank_mcc_dac_biet.pdf"
UOB_PDF = DATA_DIR / "uob_x2_benefits_mcc.pdf"
OUTPUT_JSON = DATA_DIR / "mcc_categories.json"


def clean_text(value: str | None) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    return text or None


def read_vib_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for raw in table:
                    if len(raw) < 3:
                        continue
                    mcc = clean_text(raw[1])
                    description = clean_text(raw[2])
                    if not mcc or not re.fullmatch(r"\d{4}", mcc):
                        continue
                    rows.append(
                        {
                            "mcc": mcc,
                            "description_en": description,
                            "description_vi": None,
                            "source": "VIB Super Card MCC VN_EN PDF 16.10.2023",
                        }
                    )
    return rows


def read_sacombank_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    for match in re.finditer(r"\bMCC\s+(\d{4})\s+(.+?)(?=\nMCC\s+\d{4}\s+|\Z)", text, flags=re.S):
        rows.append(
            {
                "mcc": match.group(1),
                "description_en": None,
                "description_vi": clean_text(match.group(2)),
                "source": "Sacombank MCC đặc biệt PDF",
            }
        )
    return rows


def looks_english(value: str | None) -> bool:
    text = value or ""
    return bool(text) and not re.search(r"[àáảãạăằắẳẵặâầấẩẫậđèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵ]", text.lower())


def append_uob_row(rows: list[dict], mcc: str | None, description: str | None, source: str) -> None:
    code = clean_text(mcc)
    text = clean_text(description)
    if not code or not text or not re.fullmatch(r"\d{4}", code):
        return
    row = {
        "mcc": code,
        "description_en": None,
        "description_vi": text,
        "source": source,
    }
    if looks_english(text):
        row["description_en"] = text
        if text.isupper() or "AIR" in text.upper() or "AIRLINES" in text.upper():
            row["description_vi"] = f"Hãng hàng không - {text}"
    rows.append(row)


def read_uob_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    source = "UOB x2 benefits credit cards PDF"
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for raw in table:
                    cells = [clean_text(cell) for cell in raw]
                    if not any(cells):
                        continue
                    lower = " ".join(cell.lower() for cell in cells if cell)
                    has_code = any(bool(cell and re.fullmatch(r"\d{4}", cell)) for cell in cells)
                    if not has_code and ("mcc" in lower or "mã ngành" in lower or "mã danh mục" in lower):
                        continue
                    if len(cells) >= 4 and re.fullmatch(r"\d{4}", cells[0] or "") and re.fullmatch(r"\d{4}", cells[2] or ""):
                        append_uob_row(rows, cells[0], cells[1], source)
                        append_uob_row(rows, cells[2], cells[3], source)
                        continue
                    if len(cells) >= 4 and re.fullmatch(r"\d{4}", cells[0] or ""):
                        append_uob_row(rows, cells[0], " - ".join(cell for cell in cells[1:3] if cell), source)
                        continue
                    if len(cells) >= 3 and re.fullmatch(r"\d{4}", cells[0] or ""):
                        append_uob_row(rows, cells[0], " - ".join(cell for cell in cells[1:3] if cell), source)
                        continue
                    for idx, cell in enumerate(cells):
                        if not cell or not re.fullmatch(r"\d{4}", cell):
                            continue
                        description = None
                        for candidate in cells[idx + 1:]:
                            if candidate and not re.fullmatch(r"\d{4}", candidate):
                                description = candidate
                                break
                        if description:
                            append_uob_row(rows, cell, description, source)
    return rows


def merge_rows(rows: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for row in rows:
        mcc = str(row.get("mcc") or "").strip()
        if not mcc:
            continue
        current = merged.setdefault(
            mcc,
            {
                "mcc": mcc,
                "description_en": None,
                "description_vi": None,
                "sources": [],
            },
        )
        if row.get("description_en") and not current["description_en"]:
            current["description_en"] = row["description_en"]
        if row.get("description_vi") and not current["description_vi"]:
            current["description_vi"] = row["description_vi"]
        if row.get("source") and row["source"] not in current["sources"]:
            current["sources"].append(row["source"])
    return [merged[key] for key in sorted(merged)]


def main() -> int:
    rows = merge_rows(read_vib_rows(VIB_PDF) + read_sacombank_rows(SACOMBANK_PDF) + read_uob_rows(UOB_PDF))
    OUTPUT_JSON.write_text(json.dumps({"mcc_categories": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    count = replace_mcc_categories(rows)
    print(f"Imported {count} MCC categories.")
    print(f"Wrote {OUTPUT_JSON}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
