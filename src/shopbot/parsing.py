"""Дребни парсери за текст, който идва от уеб страници."""

from __future__ import annotations

import re

CURRENCY_SYMBOLS = {"€": "EUR", "EUR": "EUR", "лв": "BGN", "лв.": "BGN", "BGN": "BGN"}

NBSP = " "
NARROW_NBSP = " "

# Хваща 1.234,56 / 1,234.56 / 49,95 / 49.95 / 8
_NUMBER = re.compile(r"\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?")


def _normalize_number(raw: str) -> str:
    """Решава кой знак е десетичен и кой е за хиляди.

    Правилото: при два различни разделителя десетичният е по-десният.
    При един разделител три цифри след него значат хиляди ('1.299'),
    една или две значат стотинки ('49,95').
    """
    has_comma, has_dot = "," in raw, "." in raw

    if has_comma and has_dot:
        if raw.rfind(",") > raw.rfind("."):
            return raw.replace(".", "").replace(",", ".")
        return raw.replace(",", "")

    for sep in (",", "."):
        if sep in raw:
            if raw.count(sep) > 1 or len(raw.rsplit(sep, 1)[-1]) == 3:
                return raw.replace(sep, "")
            return raw.replace(sep, ".")

    return raw


def parse_money(text: str) -> tuple[float, str]:
    """'€ 1.299,00' -> (1299.0, 'EUR'). Връща (0.0, '') при неуспех."""
    if not text:
        return 0.0, ""

    currency = ""
    for token, code in CURRENCY_SYMBOLS.items():
        if token in text:
            currency = code
            break

    cleaned = text.replace(NBSP, " ").replace(NARROW_NBSP, " ")
    match = _NUMBER.search(cleaned)
    if not match:
        return 0.0, currency

    raw = re.sub(r"\s", "", match.group(0))
    try:
        return float(_normalize_number(raw)), currency
    except ValueError:
        return 0.0, currency


def parse_percent(text: str) -> int:
    match = re.search(r"(\d{1,3})\s*%", text or "")
    return int(match.group(1)) if match else 0


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def product_id_from_url(url: str) -> str:
    """Стабилен идентификатор от URL-а на продукта.

    Предпочита числов артикулен номер; ако няма, взима последния сегмент.
    """
    path = url.split("?", 1)[0].rstrip("/")
    numbers = re.findall(r"(\d{5,})", path)
    if numbers:
        return numbers[-1]
    tail = path.rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9_.-]", "", tail)[:80] or path[-80:]
