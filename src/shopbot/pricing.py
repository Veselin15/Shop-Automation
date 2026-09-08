"""Ценообразуване: BestSecret цена -> цена в обявата."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import PricingConfig
from .models import Product


@dataclass(slots=True)
class PriceBreakdown:
    """Разбивка, за да е ясно откъде идва числото (влиза и в лога)."""

    cost: float           # себестойност в изходна валута (цена + доставка)
    markup: float         # приложен коефициент
    raw: float            # преди закръгляне
    final: float          # цената в обявата
    margin: float         # final - cost
    currency: str
    orig_price: float     # каталожната цена, конвертирана
    rejected: str = ""    # непразно = не публикувай, ето защо


def convert(amount: float, from_currency: str, to_currency: str, eur_bgn: float) -> float:
    """BestSecret работи в EUR; обявата може да е в EUR или BGN."""
    if from_currency == to_currency:
        return amount
    if from_currency == "EUR" and to_currency == "BGN":
        return amount * eur_bgn
    if from_currency == "BGN" and to_currency == "EUR":
        return amount / eur_bgn
    raise ValueError(f"Няма курс {from_currency}->{to_currency}")


def apply_charm(value: float, ending: float | None) -> float:
    """34.17 -> 34.90. Винаги нагоре, за да не изядем марж."""
    if ending is None:
        return round(value, 2)
    base = math.floor(value)
    candidate = base + ending
    if candidate < value - 1e-9:
        candidate += 1.0
    return round(candidate, 2)


def compute_price(product: Product, cfg: PricingConfig) -> PriceBreakdown:
    rate = cfg.fx.eur_bgn
    out = cfg.output_currency

    src_price = convert(product.price, product.currency, out, rate)
    orig_price = convert(product.orig_price, product.currency, out, rate)
    shipping = cfg.shipping_buffer

    cost = src_price + shipping
    markup = cfg.markup_by_category.get(product.category_key, cfg.default_markup)

    by_pct = cost * markup
    by_abs = cost + cfg.min_absolute_margin
    raw = max(by_pct, by_abs)
    final = apply_charm(raw, cfg.charm_ending)

    breakdown = PriceBreakdown(
        cost=round(cost, 2),
        markup=markup,
        raw=round(raw, 2),
        final=final,
        margin=round(final - cost, 2),
        currency=out,
        orig_price=round(orig_price, 2),
    )

    if final < cfg.min_listing_price:
        breakdown.rejected = f"цена {final} под минимума {cfg.min_listing_price}"
    elif final > cfg.max_listing_price:
        breakdown.rejected = f"цена {final} над максимума {cfg.max_listing_price}"
    elif orig_price > 0 and final >= orig_price:
        # Никой няма да купи препродажба над каталожната цена.
        breakdown.rejected = f"цена {final} не е под каталожната {orig_price}"

    return breakdown


def format_money(value: float, currency: str) -> str:
    symbol = "€" if currency == "EUR" else "лв."
    return f"{value:.2f} {symbol}"
