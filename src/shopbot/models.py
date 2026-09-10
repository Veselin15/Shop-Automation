"""Домейн модели."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ProductStatus(StrEnum):
    AVAILABLE = "available"
    SOLD_OUT = "sold_out"
    GONE = "gone"            # страницата вече не съществува
    UNDISCOUNTED = "undiscounted"


class ListingState(StrEnum):
    CANDIDATE = "candidate"
    PUBLISHED = "published"
    FAILED = "failed"
    REMOVED = "removed"


@dataclass(slots=True)
class Size:
    label: str
    available: bool = True


@dataclass(slots=True)
class Product:
    """Един артикул, както е видян в BestSecret."""

    id: str
    url: str
    brand: str = ""
    name: str = ""
    category_key: str = ""
    price: float = 0.0
    orig_price: float = 0.0
    currency: str = "EUR"
    description: str = ""
    color: str = ""
    material: str = ""
    images: list[str] = field(default_factory=list)
    sizes: list[Size] = field(default_factory=list)
    status: ProductStatus = ProductStatus.AVAILABLE

    # Сигнали за популярност
    bestseller_badge: bool = False
    low_stock: bool = False
    listing_rank: int = 9999

    @property
    def discount_pct(self) -> int:
        if self.orig_price <= 0 or self.price <= 0 or self.price >= self.orig_price:
            return 0
        return round((1 - self.price / self.orig_price) * 100)

    @property
    def available_sizes(self) -> list[str]:
        return [s.label for s in self.sizes if s.available]

    def content_hash(self) -> str:
        """Хеш на всичко, чиято промяна налага редакция на обявата."""
        payload = "|".join(
            [
                self.brand,
                self.name,
                f"{self.price:.2f}",
                f"{self.orig_price:.2f}",
                ",".join(sorted(self.available_sizes)),
                ",".join(self.images[:6]),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class Listing:
    """Обява в Bazar.bg, обвързана с продукт."""

    product_id: str
    title: str = ""
    description: str = ""
    price: float = 0.0
    currency: str = "EUR"
    category_id: int = 0
    # Категорията в източника — от нея зависи "Вид" (Мъжки/Дамски).
    category_key: str = ""
    # Цвят с думите на Bazar.bg ("Черни"), не с тези на BestSecret.
    color: str = ""
    image_paths: list[str] = field(default_factory=list)
    bazar_id: str | None = None
    bazar_url: str | None = None
    state: ListingState = ListingState.CANDIDATE
    content_hash: str = ""
