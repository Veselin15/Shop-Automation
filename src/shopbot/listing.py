"""Съставяне на текста на обявата от продукта."""

from __future__ import annotations

import re

from .config import ListingConfig
from .models import Listing, Product
from .pricing import PriceBreakdown, format_money

# Всяко споменаване на източника се маха от текста — и защото издава откъде
# идва стоката, и защото звучи като copy-paste от чужд сайт.
SOURCE_MENTIONS = re.compile(
    r"\b(bestsecret|best\s?secret|schustermann|borenstein)\b", re.IGNORECASE
)
WHITESPACE = re.compile(r"[ \t]+")
BLANK_LINES = re.compile(r"\n{3,}")

# Пътят до категорията се пази като един стринг, за да остане четим и в базата,
# и в `shopbot listings`.
CATEGORY_SEP = " > "


def path_to_label(path: list[str] | str) -> str:
    if isinstance(path, str):
        return path
    return CATEGORY_SEP.join(p.strip() for p in path if p and p.strip())


def label_to_path(label: str) -> list[str]:
    return [p.strip() for p in (label or "").split(CATEGORY_SEP) if p.strip()]


def clean_source_text(text: str) -> str:
    text = SOURCE_MENTIONS.sub("", text or "")
    text = WHITESPACE.sub(" ", text)
    text = BLANK_LINES.sub("\n\n", text)
    return text.strip()


def size_hint(product: Product, limit: int = 4) -> str:
    sizes = product.available_sizes
    if not sizes:
        return ""
    if len(sizes) <= limit:
        return "размер " + ", ".join(sizes)
    return "размери " + ", ".join(sizes[:limit]) + " и др."


def build_title(product: Product, cfg: ListingConfig) -> str:
    raw = cfg.title_template.format(
        brand=product.brand,
        name=product.name,
        size_hint=size_hint(product, limit=2),
        color=product.color,
    )
    title = WHITESPACE.sub(" ", clean_source_text(raw)).strip(" -–—,")
    if len(title) <= cfg.title_max_len:
        return title
    # Реже на дума, не по средата на дума.
    cut = title[: cfg.title_max_len].rsplit(" ", 1)[0]
    return cut.rstrip(" -–—,")


def build_description(
    product: Product, price: PriceBreakdown, cfg: ListingConfig
) -> str:
    color_line = f"Цвят: {product.color}\n" if product.color else ""
    sizes = product.available_sizes
    size_line = f"Налични размери: {', '.join(sizes)}\n" if sizes else ""
    material_line = f"Материя: {product.material}\n" if product.material else ""

    body = cfg.description_template.format(
        brand=product.brand,
        name=product.name,
        color_line=color_line,
        size_line=size_line,
        material_line=material_line,
        orig_price=format_money(price.orig_price, price.currency),
        price=format_money(price.final, price.currency),
        discount=f"{product.discount_pct}%",
        delivery_note=cfg.delivery_note,
        extra_note=cfg.extra_note,
        description=clean_source_text(product.description),
        location=cfg.location,
        condition=cfg.condition,
    )
    return BLANK_LINES.sub("\n\n", clean_source_text(body))


def build_listing(
    product: Product,
    price: PriceBreakdown,
    cfg: ListingConfig,
    category: list[str] | str,
) -> Listing:
    return Listing(
        product_id=product.id,
        title=build_title(product, cfg),
        description=build_description(product, price, cfg),
        price=price.final,
        currency=price.currency,
        category_label=path_to_label(category),
        content_hash=product.content_hash(),
    )
