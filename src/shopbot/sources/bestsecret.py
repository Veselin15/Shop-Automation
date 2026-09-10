"""Четене от BestSecret.

Сайтът е изцяло зад вход ("iron door" на /acquisition/entrance), затова тук
няма логика за автоматично логване с парола — първият вход се прави ръчно
(`shopbot login bestsecret`) и сесията се пази като JSON. Ако сесията падне,
модулът вдига AuthWallError и ботът спира да пипа този сайт, вместо да блъска
формата за вход.

Две неща, проверени на живо в логнат профил, определят целия модул:

1. **Плочката в листинга носи всичко за подбора** — марка, име, каталожна
   цена, процент намаление и цена. Затова се филтрира още там и продуктовата
   страница се отваря само за оцелелите. Разликата е между 20 и 400 отваряния
   на цикъл.

2. **Страницата показва едновременно EUR и BGN**, а извън основния продукт
   стои мини-кошница със свои `.rrp` / `.sold-price` / `.discount-tag`. Всяко
   четене е ограничено до `.main-prices` вътре в корена на продукта; иначе се
   вадят чужди числа. (JSON-LD няма — проверено.)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Page

from ..browser import AuthWallError, BrowserSession, any_present
from ..config import Config, SourceCategory
from ..models import Product, ProductStatus, Size
from ..parsing import clean, parse_money, parse_percent, product_id_from_url

log = logging.getLogger(__name__)

SITE = "bestsecret"

# Галерията сервира един и същ кадър в няколко размера: _68X84_, _352X429_,
# _970X1182_. Размерът НЕ може да се пренапише в адреса — хешът в пътя е
# специфичен за размера и подменен размер връща 404. Затова се взима най-
# големият вариант, който страницата реално предлага.
IMAGE_SIZE_TOKEN = re.compile(r"_(\d+)[xX](\d+)_")
# Кой кадър от галерията е това (…_970X1182_2.jpg -> 2), за да не смятаме
# два размера на една и съща снимка за две снимки.
IMAGE_INDEX = re.compile(r"_(\d+)\.(?:jpg|jpeg|png|webp)$", re.IGNORECASE)
# Дозареждането при скрол зависи от скоростта на машината и мрежата. На
# по-бавен сървър три проверки по 900 ms хващаха 12 продукта вместо 200,
# затова има минимален брой скролове и повече поредни стабилни четения.
MAX_SCROLLS = 25
MIN_SCROLLS = 6
STABLE_READINGS = 3
SCROLL_SETTLE_MS = 1200
# Под толкова плочки листингът почти сигурно не е дозаредил докрай.
SUSPICIOUSLY_FEW = 20

BESTSELLER_WORDS = ("bestseller", "best seller", "top", "beliebt", "popular", "хит")
LOW_STOCK_WORDS = ("only", "last", "few", "nur noch")


@dataclass(slots=True)
class CardHit:
    """Каквото плочката в листинга дава, преди да отворим продукта."""

    url: str
    rank: int
    brand: str = ""
    name: str = ""
    price: float = 0.0
    orig_price: float = 0.0
    currency: str = "EUR"
    discount_pct: int = 0
    image: str = ""
    bestseller: bool = False
    badge_text: str = ""
    tags: list[str] = field(default_factory=list)


class BestSecretSource:
    def __init__(self, cfg: Config, session: BrowserSession) -> None:
        self.cfg = cfg
        self.session = session
        self.sel = cfg.selectors.get("bestsecret", {})

    # ------------------------------------------------------------------ вход

    async def _guard(self, page: Page) -> None:
        """Вдига AuthWallError, ако сме изхвърлени пред вратата."""
        if not await looks_logged_in(page, self.sel):
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
            loaded = await self._scroll_through(page)

            cards = await self._collect_cards(page, start_rank=rank)
            log.info(
                "BestSecret: страница %d -> %d плочки, %d уникални",
                page_no, loaded, len(cards),
            )
            if not cards:
                log.info("BestSecret: няма продукти на страница %d, спирам", page_no)
                break

            hits.extend(cards)
            rank += len(cards)

        log.info("BestSecret: %d продукта в %s", len(hits), category.key)
        return hits

    async def _scroll_through(self, page: Page) -> int:
        """Дозарежда листинга, като бута последната плочка във видимото поле.

        `mouse.wheel` не върши работа: ако към момента са рендирани само
        няколко плочки, страницата не е по-висока от прозореца, колелцето
        няма какво да превърти и се получава задънена улица — съдържание се
        зарежда при скрол, а скрол няма откъде да стане. На по-бавна машина
        това спираше на 8 продукта вместо 300.

        Бутането на последния елемент задейства наблюдателя директно и
        работи независимо от височината на документа.
        """
        selector = self.sel["product_card"][0]
        tiles = page.locator(selector)

        previous = -1
        stable = 0
        for step in range(MAX_SCROLLS):
            count = await tiles.count()
            if count:
                try:
                    await tiles.nth(count - 1).scroll_into_view_if_needed(timeout=5000)
                except Exception:
                    await page.mouse.wheel(0, 2400)
            else:
                await page.mouse.wheel(0, 2400)

            await page.wait_for_timeout(SCROLL_SETTLE_MS)
            current = await tiles.count()
            stable = stable + 1 if current == previous else 0
            previous = current
            if step + 1 >= MIN_SCROLLS and stable >= STABLE_READINGS:
                break

        if previous <= SUSPICIOUSLY_FEW:
            log.warning(
                "само %d плочки след %d скрола на %s — листингът вероятно не "
                "дозарежда; провери product_card в selectors.yaml",
                previous, step + 1, page.url,
            )
        return previous

    async def _collect_cards(self, page: Page, start_rank: int) -> list[CardHit]:
        raw = await page.evaluate(
            """
            (sel) => {
              const pick = (root, sels) => {
                for (const s of sels || []) {
                  const el = root.querySelector(s);
                  if (el && (el.textContent || '').trim()) return el.textContent.trim();
                }
                return '';
              };
              let tiles = [];
              for (const s of sel.product_card) {
                const found = document.querySelectorAll(s);
                if (found.length) { tiles = [...found]; break; }
              }
              return tiles.map(t => {
                const a = t.querySelector(sel.card_link[0]) || t.querySelector('a[href]');
                let img = '';
                for (const s of sel.card_image || []) {
                  const el = t.querySelector(s);
                  if (el) img = el.currentSrc || el.src || el.getAttribute('data-src') || '';
                  if (img) break;
                }
                return {
                  href: a ? a.href : null,
                  brand: pick(t, sel.card_brand),
                  name: pick(t, sel.card_name),
                  rrp: pick(t, sel.card_rrp),
                  price: pick(t, sel.card_price),
                  discount: pick(t, sel.card_discount),
                  badge: pick(t, sel.card_badge),
                  image: img,
                };
              }).filter(x => x.href);
            }
            """,
            self.sel,
        )

        hits: list[CardHit] = []
        seen: set[str] = set()
        for i, item in enumerate(raw):
            href = item["href"].split("#")[0]
            key = _product_key(href)
            if key in seen:
                continue
            seen.add(key)

            price, currency = parse_money(item["price"])
            orig_price, _ = parse_money(item["rrp"])
            badge = clean(item.get("badge", ""))

            hits.append(
                CardHit(
                    url=href,
                    rank=start_rank + i,
                    brand=clean(item["brand"]),
                    name=clean(item["name"]),
                    price=price,
                    orig_price=orig_price,
                    currency=currency or "EUR",
                    discount_pct=parse_percent(item["discount"]),
                    image=item["image"] or "",
                    bestseller=_has_word(badge, BESTSELLER_WORDS),
                    badge_text=badge,
                )
            )
        return hits

    def product_from_card(self, hit: CardHit, category_key: str) -> Product:
        """Черновата от плочката — стига за подбора, без да отваряме продукта."""
        return Product(
            id=_product_key(hit.url),
            url=hit.url,
            brand=hit.brand,
            name=hit.name,
            category_key=category_key,
            price=hit.price,
            orig_price=hit.orig_price,
            currency=hit.currency,
            images=[hit.image] if hit.image else [],
            sizes=[],
            bestseller_badge=hit.bestseller,
            low_stock=_has_word(hit.badge_text, LOW_STOCK_WORDS),
            listing_rank=hit.rank,
        )

    # ------------------------------------------------------------- продукт

    async def fetch_product(
        self, url: str, category_key: str, page: Page, hit: CardHit | None = None
    ) -> Product | None:
        """Отваря продуктовата страница и сглобява Product. None = вече го няма."""
        resp = await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

        if resp is not None and resp.status in (404, 410):
            log.info("BestSecret: %s връща %s — продуктът е свален", url, resp.status)
            return None

        await self._guard(page)

        data = await page.evaluate(
            """
            (sel) => {
              let root = null;
              for (const s of sel.pdp_root) { root = document.querySelector(s); if (root) break; }
              if (!root) return null;
              const doc = root.ownerDocument;
              const pick = sels => {
                for (const s of sels || []) {
                  const el = root.querySelector(s);
                  if (el && (el.textContent || '').trim()) return el.textContent.trim();
                }
                return '';
              };
              const sizes = [];
              for (const s of sel.pdp_sizes || []) {
                const nodes = doc.querySelectorAll(s);
                if (!nodes.length) continue;
                for (const n of nodes) {
                  const label = (n.textContent || n.value || '').trim();
                  if (!label) continue;
                  const cls = (n.className || '') + ' ' + (n.getAttribute('aria-label') || '');
                  sizes.push({
                    label,
                    available: !(n.hasAttribute('disabled')
                      || n.getAttribute('aria-disabled') === 'true'
                      || /disabled|sold[-_ ]?out|unavailable/i.test(cls)),
                  });
                }
                break;
              }
              const images = [];
              for (const s of sel.pdp_images || []) {
                for (const img of doc.querySelectorAll(s)) {
                  const src = img.currentSrc || img.src || img.getAttribute('data-src') || '';
                  if (src && !src.startsWith('data:')) images.push(src);
                }
                if (images.length) break;
              }
              return {
                brand: pick(sel.pdp_brand),
                name: pick(sel.pdp_title),
                price: pick(sel.pdp_price),
                rrp: pick(sel.pdp_orig_price),
                discount: pick(sel.pdp_discount),
                description: pick(sel.pdp_description).slice(0, 1200),
                sizes, images,
              };
            }
            """,
            self.sel,
        )

        if not data or not data["name"]:
            log.warning("BestSecret: не мога да прочета %s — провери pdp_* селекторите", url)
            return None

        price, currency = parse_money(data["price"])
        orig_price, _ = parse_money(data["rrp"])

        product = Product(
            id=_product_key(url),
            url=url,
            brand=clean(data["brand"]),
            name=clean(data["name"]),
            category_key=category_key,
            price=price,
            orig_price=orig_price,
            currency=currency or "EUR",
            description=clean(data["description"]),
            images=_dedupe_images(data["images"]),
            sizes=[
                Size(label=clean(s["label"])[:12], available=bool(s["available"]))
                for s in data["sizes"]
            ],
        )

        product.low_stock = await any_present(page, self.sel.get("pdp_low_stock_markers"))
        sold_out = await any_present(page, self.sel.get("pdp_sold_out_markers"))

        if hit is not None:
            product.bestseller_badge = hit.bestseller
            product.listing_rank = hit.rank
            if product.orig_price <= 0:
                product.orig_price = hit.orig_price

        if sold_out or (product.sizes and not product.available_sizes):
            product.status = ProductStatus.SOLD_OUT
        elif product.discount_pct == 0:
            product.status = ProductStatus.UNDISCOUNTED
        else:
            product.status = ProductStatus.AVAILABLE

        return product


# ---------------------------------------------------------------- помощни


async def looks_logged_in(page: Page, sel: dict) -> bool:
    """Логнати ли сме в BestSecret.

    Проверява се първо по URL: излезлите се препращат към
    /acquisition/entrance. Това е поведение на сайта, а не CSS клас, затова
    не се чупи при редизайн. DOM маркерите са допълнителна мрежа.
    """
    pattern = sel.get("auth_wall_url_pattern", "acquisition/entrance")
    if pattern and pattern in page.url:
        return False
    return not await any_present(page, sel.get("auth_wall_markers"))


def _product_key(url: str) -> str:
    """Стабилен идентификатор.

    Адресът е /product.htm?code=41021024&colorCode=001579106 — един артикул
    в различни цветове са различни обяви, затова и двете влизат в ключа.
    """
    query = parse_qs(urlsplit(url).query)
    code = (query.get("code") or [""])[0]
    color = (query.get("colorCode") or [""])[0]
    if code:
        return f"{code}_{color}" if color else code
    return product_id_from_url(url)


def _image_area(url: str) -> int:
    """Площта в пиксели, прочетена от името на файла. 0 = неизвестен размер."""
    match = IMAGE_SIZE_TOKEN.search(url)
    return int(match.group(1)) * int(match.group(2)) if match else 0


def _image_slot(url: str) -> str:
    """Кой кадър е това. Всички размери на един кадър делят един слот.

    Взима се само името на файла: пътят съдържа хеш, който е различен за
    всеки размер, така че адресът като цяло не става за идентичност.
    """
    filename = url.split("?")[0].rsplit("/", 1)[-1]
    return IMAGE_SIZE_TOKEN.sub("_", filename)


def _dedupe_images(urls: list[str]) -> list[str]:
    """По един адрес на кадър — този с най-голям размер измежду предложените.

    Подмяна на размера в адреса не работи (хешът е за конкретния размер),
    затова се избира от това, което страницата вече дава.
    """
    best: dict[str, tuple[int, str]] = {}
    order: list[str] = []
    for raw in urls:
        if not raw or raw.startswith("data:"):
            continue
        slot = _image_slot(raw)
        area = _image_area(raw)
        if slot not in best:
            order.append(slot)
            best[slot] = (area, raw)
        elif area > best[slot][0]:
            best[slot] = (area, raw)
    return [best[s][1] for s in order]


def _has_word(text: str, words: tuple[str, ...]) -> bool:
    lowered = (text or "").casefold()
    return any(w in lowered for w in words)


def _paged_url(base: str, page_no: int, sort_query: str = "") -> str:
    """Добавя страниране към URL, който вече може да носи филтри.

    Филтрираните адреси от BestSecret идват с готов query string, затова
    параметрите се сливат, а не се залепват след нов '?'.

    Звездичката в "70.0-*" остава незакодирана — BestSecret връща празен
    листинг, ако тя дойде като %2A.
    """
    parts = urlsplit(base)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))

    if sort_query:
        params.update(dict(parse_qsl(sort_query.lstrip("?&"), keep_blank_values=True)))
    if page_no > 1:
        params["page"] = str(page_no)

    return urlunsplit(parts._replace(query=urlencode(params, safe="*")))
