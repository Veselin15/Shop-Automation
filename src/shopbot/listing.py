"""Съставяне на текста на обявата от продукта."""

from __future__ import annotations

import re

from .config import ListingConfig
from .models import Listing, Product
from .pricing import PriceBreakdown, format_money
from .specs import format_specs, parse_specs

# Всяко споменаване на източника се маха от текста — и защото издава откъде
# идва стоката, и защото звучи като copy-paste от чужд сайт.
SOURCE_MENTIONS = re.compile(
    r"\b(bestsecret|best\s?secret|schustermann|borenstein)\b", re.IGNORECASE
)
WHITESPACE = re.compile(r"[ \t]+")
BLANK_LINES = re.compile(r"\n{3,}")
PUNCTUATION = re.compile(r"[^\w\s]")


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


def detect_color(text: str, cfg: ListingConfig) -> str:
    """Цветът, само когато наистина пише в текста. Иначе празно.

    Търси се най-дългата съвпаднала дума, за да бие "navy blue" над "blue",
    а "dark green" над "red" в "dark green with red trim".
    """
    # Препинанието става интервал: "Colour: brown." и "(brown)" също са кафяво,
    # а не резервното черно.
    haystack = f" {PUNCTUATION.sub(' ', text.lower())} "
    hits = [
        (len(word), option)
        for word, option in cfg.color_map.items()
        if word and f" {PUNCTUATION.sub(' ', word.lower())} " in haystack
    ]
    return max(hits)[1] if hits else ""


def pick_color(text: str, cfg: ListingConfig) -> str:
    """Цветът за формата на Bazar.bg, където полето е задължително.

    Тук резервната стойност е оправдана — иначе обявата изобщо не тръгва.
    В заглавието обаче се ползва detect_color: пред купувача не се твърди
    цвят, който никъде не пише.
    """
    return detect_color(text, cfg) or cfg.color_fallback


def build_title(product: Product, cfg: ListingConfig) -> str:
    """Заглавието започва с българска дума.

    Bazar.bg се търси на кирилица, а имената в BestSecret са на английски —
    "Sunglasses Roxie" не се намира от никого. Освен това сайтът иска поне
    15 символа в заглавието.
    """
    prefix = cfg.title_prefix.get(product.category_key, "")
    raw = cfg.title_template.format(
        condition_word=cfg.condition_word.get(product.category_key, "Чисто нов"),
        prefix=prefix,
        prefix_lower=prefix[:1].lower() + prefix[1:] if prefix else "",
        brand=product.brand,
        name=product.name,
        size_hint=size_hint(product, limit=2),
        color=product.color,
    )
    title = WHITESPACE.sub(" ", clean_source_text(raw)).strip(" -–—,")

    # Цветът се долепя само ако остава място — по-добре без него, отколкото
    # отрязан по средата на думата.
    color_word = cfg.color_title_map.get(detect_color(
        " ".join((product.name, product.color, product.description)), cfg
    ), "")
    if color_word:
        # "Chronograph black, черен цвят" казва едно и също два пъти на два езика.
        for english in cfg.color_map:
            title = re.sub(
                rf"[\s,-]*\b{re.escape(english)}\b\s*$", "", title, flags=re.IGNORECASE
            )
        with_color = f"{title}, {color_word} цвят"
        if len(with_color) <= cfg.title_max_len:
            return with_color

    if len(title) <= cfg.title_max_len:
        return title
    # Реже на дума, не по средата на дума.
    cut = title[: cfg.title_max_len].rsplit(" ", 1)[0]
    return cut.rstrip(" -–—,")


def build_description(
    product: Product, price: PriceBreakdown, cfg: ListingConfig
) -> str:
    color_line = f"Цвят: {product.color}\n" if product.color else ""
    # "Налични размери: One Size" при аксесоар е празен ред, който само
    # разсейва — размерът има смисъл само когато има от какво да се избира.
    sizes = [s for s in product.available_sizes if s.strip().casefold() != "one size"]
    size_line = f"Налични размери: {', '.join(sizes)}\n" if sizes else ""
    material_line = f"Материя: {product.material}\n" if product.material else ""

    specs = parse_specs(product.description, cfg.max_specs)
    specs_block = "Характеристики:\n" + format_specs(specs) + "\n" if specs else ""

    body = cfg.description_template.format(
        specs_block=specs_block,
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
    category_id: int,
) -> Listing:
    return Listing(
        product_id=product.id,
        title=build_title(product, cfg),
        description=build_description(product, price, cfg),
        price=price.final,
        currency=price.currency,
        category_id=category_id,
        category_key=product.category_key,
        color=pick_color(
            " ".join((product.name, product.color, product.description)), cfg
        ),
        content_hash=product.content_hash(),
    )
