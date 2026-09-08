"""Подбор: кои продукти изобщо си струват обява."""

from __future__ import annotations

from dataclasses import dataclass

from .config import SelectionConfig
from .models import Product


@dataclass(slots=True)
class Verdict:
    accepted: bool
    score: float
    reason: str = ""


def _normalize_discount(pct: int, floor_pct: int) -> float:
    """Намаление floor..90% -> 0..1. Под прага дава 0."""
    if pct <= floor_pct:
        return 0.0
    span = max(90 - floor_pct, 1)
    return min((pct - floor_pct) / span, 1.0)


def _rank_signal(rank: int) -> float:
    """Позиция в листинга: първите ~40 носят стойност, после затихва."""
    if rank >= 200:
        return 0.0
    return max(0.0, 1.0 - rank / 200.0)


def brand_tier(brand: str, tiers: dict[str, float], default: float = 0.35) -> float:
    """Мачва без разлика в главни/малки букви, за да не зависи от изписването.

    Прави и частично съвпадение: BestSecret изписва марките ту 'Guess',
    ту 'GUESS Accessories', ту 'Emporio Armani' срещу 'Armani' в списъка.
    """
    needle = brand.casefold().strip()
    if not needle:
        return default

    lowered = {k.casefold().strip(): v for k, v in tiers.items()}
    if needle in lowered:
        return lowered[needle]

    matches = [v for k, v in lowered.items() if k and (k in needle or needle in k)]
    return max(matches) if matches else default


def popularity_score(product: Product, cfg: SelectionConfig) -> float:
    w = cfg.popularity.weights
    parts = {
        "discount": _normalize_discount(product.discount_pct, cfg.min_discount_pct),
        "bestseller_badge": 1.0 if product.bestseller_badge else 0.0,
        "low_stock": 1.0 if product.low_stock else 0.0,
        "listing_rank": _rank_signal(product.listing_rank),
        "brand_tier": brand_tier(
            product.brand, cfg.popularity.brand_tiers, cfg.popularity.unknown_brand_tier
        ),
    }
    total_weight = sum(w.get(k, 0.0) for k in parts)
    if total_weight <= 0:
        return 0.0
    return sum(parts[k] * w.get(k, 0.0) for k in parts) / total_weight


def evaluate(product: Product, cfg: SelectionConfig) -> Verdict:
    """Твърдите филтри първо — по-евтино е и обяснява по-ясно защо е отпаднал."""
    title = f"{product.brand} {product.name}".casefold()

    for kw in cfg.title_deny_keywords:
        if kw.casefold() in title:
            return Verdict(False, 0.0, f"забранена дума в заглавието: {kw}")

    if cfg.brands_deny and any(b.casefold() == product.brand.casefold() for b in cfg.brands_deny):
        return Verdict(False, 0.0, f"марка в черен списък: {product.brand}")

    if cfg.brands_allow and not any(
        b.casefold() == product.brand.casefold() for b in cfg.brands_allow
    ):
        return Verdict(False, 0.0, f"марка извън белия списък: {product.brand}")

    tier = brand_tier(
        product.brand, cfg.popularity.brand_tiers, cfg.popularity.unknown_brand_tier
    )
    if cfg.min_brand_tier > 0 and tier < cfg.min_brand_tier:
        return Verdict(False, 0.0, f"марка '{product.brand}' е под прага ({tier:.2f})")

    if product.discount_pct < cfg.min_discount_pct:
        return Verdict(False, 0.0, f"намаление {product.discount_pct}% < {cfg.min_discount_pct}%")

    if not (cfg.min_source_price <= product.price <= cfg.max_source_price):
        return Verdict(False, 0.0, f"цена {product.price} извън диапазона")

    # Артикул без размерна таблица (очила, часовник, портфейл) е "един размер"
    # и не бива да пада заради проверка, писана за дрехи.
    if product.sizes:
        available = len(product.available_sizes)
        if available < cfg.min_sizes_available:
            return Verdict(False, 0.0, f"само {available} налични размера")
    elif not cfg.allow_one_size:
        return Verdict(False, 0.0, "няма размери на страницата")

    if not product.images:
        return Verdict(False, 0.0, "няма снимки")

    score = popularity_score(product, cfg)
    if score < cfg.popularity.min_score:
        return Verdict(False, score, f"скор {score:.2f} < {cfg.popularity.min_score}")

    return Verdict(True, score)
