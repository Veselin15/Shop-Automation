"""Четене от BestSecret.

Сайтът е изцяло зад вход ("iron door" на /acquisition/entrance), затова тук
няма логика за автоматично логване с парола — първият вход се прави ръчно
(`shopbot login bestsecret`) и профилът се пази на диска. Ако сесията падне,
модулът вдига AuthWallError и ботът спира да пипа този сайт, вместо да блъска
формата за вход.

Извличането на данни минава през три нива, в този ред:
  1. JSON-LD (schema.org Product) — стабилно, не зависи от CSS класове
  2. вграден JSON в страницата (__NEXT_DATA__ / __INITIAL_STATE__)
  3. CSS селектори от config/selectors.yaml
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Page

from ..browser import (
    AuthWallError,
    BrowserSession,
    any_present,
    text_of,
)
from ..config import Config, SourceCategory
from ..models import Product, ProductStatus, Size
from ..parsing import clean, parse_money, product_id_from_url

log = logging.getLogger(__name__)

SITE = "bestsecret"

JSONLD_SCRIPT = """
() => Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
    .map(s => s.textContent).filter(Boolean)
"""

EMBEDDED_SCRIPT = """
() => {
  const keys = ['__NEXT_DATA__', '__INITIAL_STATE__', '__PRELOADED_STATE__', 'dataLayer'];
  const out = {};
  for (const k of keys) {
    try { if (window[k]) out[k] = JSON.parse(JSON.stringify(window[k])); } catch (e) {}
  }
  return out;
}
"""


@dataclass(slots=True)
class CardHit:
    """Каквото се вижда още от листинга, преди да отворим продукта."""

    url: str
    rank: int
    bestseller: bool = False
    badge_text: str = ""


class BestSecretSource:
    def __init__(self, cfg: Config, session: BrowserSession) -> None:
        self.cfg = cfg
        self.session = session
        self.sel = cfg.selectors.get("bestsecret", {})

    # ------------------------------------------------------------------ вход

    async def _guard(self, page: Page) -> None:
        """Вдига AuthWallError, ако сме изхвърлени пред вратата."""
        if await any_present(page, self.sel.get("auth_wall_markers")):
            raise AuthWallError(SITE, f"login wall на {page.url}")

    async def ensure_session(self, page: Page) -> None:
        await page.goto(self.cfg.source.base_url + "/home.htm", wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        await self._guard(page)
        log.info("BestSecret: сесията е активна")

    # --------------------------------------------------------------- листинг

    async def discover(self, category: SourceCategory, page: Page) -> list[CardHit]:
        """Обхожда категория и връща намерените продукти в реда на сайта."""
        hits: list[CardHit] = []
        rank = 0

        if not category.is_configured:
            log.warning("категория '%s' няма url/path — пропускам", category.key)
            return []

        for page_no in range(1, self.cfg.source.max_pages_per_category + 1):
            url = _paged_url(
                category.resolve(self.cfg.source.base_url),
                page_no,
                self.cfg.source.sort_query,
            )
            log.info("BestSecret: чета %s", url)
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            await self._guard(page)
            await self._scroll_through(page)

            cards = await self._collect_cards(page, start_rank=rank)
            if not cards:
                log.info("BestSecret: няма продукти на страница %d, спирам", page_no)
                break

            hits.extend(cards)
            rank += len(cards)

        log.info("BestSecret: %d продукта в %s", len(hits), category.key)
        return hits

    async def _scroll_through(self, page: Page) -> None:
        """Много листинги дозареждат при скрол; без това виждаме само първите."""
        for _ in range(6):
            await page.mouse.wheel(0, 1800)
            await page.wait_for_timeout(700)

    async def _collect_cards(self, page: Page, start_rank: int) -> list[CardHit]:
        selectors = self.sel.get("product_card", [])
        badge_selectors = self.sel.get("card_badge", [])

        result = await page.evaluate(
            """
            ([cardSels, badgeSels]) => {
              let cards = [];
              for (const sel of cardSels) {
                const found = Array.from(document.querySelectorAll(sel));
                if (found.length > cards.length) cards = found;
              }
              return cards.map(c => {
                const a = c.tagName === 'A' ? c : c.querySelector('a[href]');
                let badge = '';
                for (const bs of badgeSels) {
                  const b = c.querySelector(bs);
                  if (b && b.textContent.trim()) { badge = b.textContent.trim(); break; }
                }
                return { href: a ? a.href : null, badge };
              }).filter(x => x.href);
            }
            """,
            [selectors, badge_selectors],
        )

        hits: list[CardHit] = []
        seen: set[str] = set()
        for i, item in enumerate(result):
            href = item["href"].split("?")[0]
            if href in seen:
                continue
            seen.add(href)
            badge = clean(item.get("badge", ""))
            hits.append(
                CardHit(
                    url=href,
                    rank=start_rank + i,
                    bestseller=_is_bestseller_badge(badge),
                    badge_text=badge,
                )
            )
        return hits

    # ------------------------------------------------------------- продукт

    async def fetch_product(
        self, url: str, category_key: str, page: Page, hit: CardHit | None = None
    ) -> Product | None:
        """Отваря продуктовата страница и сглобява Product. None = вече го няма."""
        resp = await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1200)

        if resp is not None and resp.status in (404, 410):
            log.info("BestSecret: %s връща %s — продуктът е свален", url, resp.status)
            return None

        await self._guard(page)

        product = Product(id=product_id_from_url(url), url=url, category_key=category_key)

        ld = await self._read_jsonld(page)
        if ld:
            _apply_jsonld(product, ld)

        if not product.name or product.price <= 0:
            await self._apply_dom(page, product)

        if not product.images:
            product.images = await self._collect_images(page)

        if not product.sizes:
            product.sizes = await self._collect_sizes(page)

        product.low_stock = await any_present(page, self.sel.get("pdp_low_stock_markers"))
        sold_out = await any_present(page, self.sel.get("pdp_sold_out_markers"))

        if hit is not None:
            product.bestseller_badge = hit.bestseller
            product.listing_rank = hit.rank

        if sold_out or (product.sizes and not product.available_sizes):
            product.status = ProductStatus.SOLD_OUT
        elif product.discount_pct == 0:
            product.status = ProductStatus.UNDISCOUNTED
        else:
            product.status = ProductStatus.AVAILABLE

        if not product.name:
            log.warning("BestSecret: не мога да прочета %s — провери селекторите", url)
            return None

        return product

    async def _read_jsonld(self, page: Page) -> dict[str, Any] | None:
        try:
            blobs = await page.evaluate(JSONLD_SCRIPT)
        except Exception:
            return None
        for blob in blobs:
            try:
                data = json.loads(blob)
            except json.JSONDecodeError:
                continue
            found = _find_product_node(data)
            if found:
                return found
        return None

    async def _apply_dom(self, page: Page, product: Product) -> None:
        product.name = product.name or clean(await text_of(page, self.sel.get("pdp_title")))
        product.brand = product.brand or clean(await text_of(page, self.sel.get("pdp_brand")))

        if product.price <= 0:
            price, currency = parse_money(await text_of(page, self.sel.get("pdp_price")))
            product.price = price
            if currency:
                product.currency = currency

        if product.orig_price <= 0:
            orig, _ = parse_money(await text_of(page, self.sel.get("pdp_orig_price")))
            product.orig_price = orig

        if not product.description:
            product.description = clean(
                await text_of(page, self.sel.get("pdp_description"))
            )[:1200]

    async def _collect_images(self, page: Page) -> list[str]:
        selectors = self.sel.get("pdp_images", [])
        urls = await page.evaluate(
            """
            (sels) => {
              const out = [];
              for (const sel of sels) {
                for (const img of document.querySelectorAll(sel)) {
                  const src = img.currentSrc || img.src ||
                              img.getAttribute('data-src') || '';
                  if (src && !src.startsWith('data:')) out.push(src);
                }
                if (out.length) break;
              }
              return out;
            }
            """,
            selectors,
        )
        seen: set[str] = set()
        ordered: list[str] = []
        for u in urls:
            base = u.split("?")[0]
            if base in seen:
                continue
            seen.add(base)
            ordered.append(u)
        return ordered

    async def _collect_sizes(self, page: Page) -> list[Size]:
        selectors = self.sel.get("pdp_sizes", [])
        disabled_attr = self.sel.get("pdp_size_disabled_attr", "disabled")
        raw = await page.evaluate(
            """
            ([sels, disabledAttr]) => {
              for (const sel of sels) {
                const nodes = Array.from(document.querySelectorAll(sel));
                if (!nodes.length) continue;
                return nodes.map(n => {
                  const label = (n.textContent || n.value || '').trim();
                  const cls = (n.className || '') + ' ' + (n.getAttribute('aria-label') || '');
                  const disabled = n.hasAttribute(disabledAttr) ||
                        n.getAttribute('aria-disabled') === 'true' ||
                        /disabled|sold[-_ ]?out|unavailable|nicht/i.test(cls);
                  return { label, available: !disabled };
                }).filter(s => s.label);
              }
              return [];
            }
            """,
            [selectors, disabled_attr],
        )
        return [Size(label=clean(s["label"])[:12], available=bool(s["available"])) for s in raw]


# ---------------------------------------------------------------- помощни


def _paged_url(base: str, page_no: int, sort_query: str = "") -> str:
    """Добавя страниране към URL, който вече може да носи филтри.

    Филтрираните адреси от BestSecret идват с готов query string, затова
    параметрите се сливат, а не се залепват след нов '?'.
    """
    parts = urlsplit(base)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))

    if sort_query:
        params.update(dict(parse_qsl(sort_query.lstrip("?&"), keep_blank_values=True)))
    if page_no > 1:
        params["page"] = str(page_no)

    return urlunsplit(parts._replace(query=urlencode(params)))


def _is_bestseller_badge(text: str) -> bool:
    lowered = text.casefold()
    return any(
        marker in lowered
        for marker in ("bestseller", "best seller", "top", "beliebt", "popular", "хит")
    )


def _find_product_node(data: Any) -> dict[str, Any] | None:
    """JSON-LD често е @graph или списък; търсим възела с @type Product."""
    if isinstance(data, dict):
        types = data.get("@type")
        types = [types] if isinstance(types, str) else (types or [])
        if any(str(t).casefold() == "product" for t in types):
            return data
        for key in ("@graph", "itemListElement", "mainEntity"):
            if key in data:
                found = _find_product_node(data[key])
                if found:
                    return found
    elif isinstance(data, list):
        for item in data:
            found = _find_product_node(item)
            if found:
                return found
    return None


def _apply_jsonld(product: Product, node: dict[str, Any]) -> None:
    product.name = clean(str(node.get("name", "")))[:160]

    brand = node.get("brand")
    if isinstance(brand, dict):
        product.brand = clean(str(brand.get("name", "")))
    elif isinstance(brand, str):
        product.brand = clean(brand)

    product.description = clean(str(node.get("description", "")))[:1200]
    product.color = clean(str(node.get("color", "")))
    product.material = clean(str(node.get("material", "")))

    images = node.get("image")
    if isinstance(images, str):
        product.images = [images]
    elif isinstance(images, list):
        product.images = [i for i in images if isinstance(i, str)]

    offers = node.get("offers")
    if isinstance(offers, dict):
        offers_list = [offers]
    elif isinstance(offers, list):
        offers_list = [o for o in offers if isinstance(o, dict)]
    else:
        offers_list = []

    prices: list[float] = []
    for offer in offers_list:
        raw_price = offer.get("price") or offer.get("lowPrice")
        if raw_price is not None:
            try:
                prices.append(float(str(raw_price).replace(",", ".")))
            except ValueError:
                pass
        currency = offer.get("priceCurrency")
        if currency:
            product.currency = str(currency)

        availability = str(offer.get("availability", "")).casefold()
        if "outofstock" in availability or "soldout" in availability:
            product.status = ProductStatus.SOLD_OUT

    if prices:
        product.price = min(prices)

    # Каталожната цена рядко е в offers; идва от отделно поле.
    for key in ("listPrice", "highPrice", "msrp"):
        value = node.get(key)
        if value:
            try:
                product.orig_price = float(str(value).replace(",", "."))
                break
            except ValueError:
                continue
