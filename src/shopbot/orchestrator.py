"""Оркестрация: откриване -> публикуване -> следене -> сваляне."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .browser import AuthWallError, BrowserSession, SelectorMissing
from .config import Config
from .db import Database
from .humanize import DailyLimiter, Pacer
from .listing import build_listing
from .media import download_images
from .models import Product, ProductStatus
from .notify import Notifier
from .pricing import compute_price, format_money
from .selection import evaluate
from .sinks.bazar import BazarSink, PublishError
from .sources.bestsecret import BestSecretSource, CardHit

log = logging.getLogger(__name__)


@dataclass
class CycleReport:
    scanned: int = 0
    new_candidates: int = 0
    published: int = 0
    removed: int = 0
    rechecked: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"прегледани {self.scanned}, нови кандидати {self.new_candidates}, "
            f"публикувани {self.published}, проверени {self.rechecked}, "
            f"свалени {self.removed}, грешки {len(self.errors)}"
        )


class Orchestrator:
    def __init__(self, cfg: Config, db: Database, notifier: Notifier) -> None:
        self.cfg = cfg
        self.db = db
        self.notifier = notifier
        self.pacer = Pacer(cfg.runtime)

    # ------------------------------------------------------------- лимити

    def _publish_limiter(self) -> DailyLimiter:
        return DailyLimiter(self.db, "publish", self.cfg.limits.max_publish_per_day)

    def _remove_limiter(self) -> DailyLimiter:
        return DailyLimiter(self.db, "remove", self.cfg.limits.max_remove_per_day)

    # ------------------------------------------------------------- цикъл

    async def run_once(
        self,
        discover: bool = True,
        publish: bool = True,
        reconcile: bool = True,
        dry_run: bool = False,
    ) -> CycleReport:
        report = CycleReport()

        if discover or reconcile:
            try:
                await self._source_phase(report, discover, reconcile)
            except AuthWallError as exc:
                report.errors.append(str(exc))
                if self.cfg.notifications.on_auth_wall:
                    await self.notifier.auth_wall("bestsecret")
                self.db.log_event("auth_wall", str(exc))

        if publish or reconcile:
            try:
                await self._sink_phase(report, publish, reconcile, dry_run)
            except AuthWallError as exc:
                report.errors.append(str(exc))
                if self.cfg.notifications.on_auth_wall:
                    await self.notifier.auth_wall("bazar")
                self.db.log_event("auth_wall", str(exc))

        self.db.log_event("cycle", report.summary())
        log.info("Цикълът приключи: %s", report.summary())
        return report

    # --------------------------------------------------------- фаза източник

    async def _source_phase(self, report: CycleReport, discover: bool, reconcile: bool) -> None:
        session = BrowserSession(
            "bestsecret", self.cfg.profiles_dir / "bestsecret", self.cfg.runtime.headless,
            seed_state=self.cfg.session_file("bestsecret"),
        )
        await session.start()
        try:
            source = BestSecretSource(self.cfg, session)
            page = await session.new_page()
            await source.ensure_session(page)

            if reconcile:
                await self._recheck_published(source, page, session, report)
            if discover:
                await self._discover(source, page, session, report)
        finally:
            await session.stop()

    async def _discover(
        self,
        source: BestSecretSource,
        page,
        session: BrowserSession,
        report: CycleReport,
    ) -> None:
        budget = self.cfg.limits.max_products_scanned_per_run

        configured = [c for c in self.cfg.source.categories if c.is_configured]
        if not configured:
            msg = (
                "нито една категория в source.categories няма попълнен `url` — "
                "отвори филтрираните листинги в BestSecret и копирай адресите"
            )
            log.error(msg)
            report.errors.append(msg)
            return

        for category in configured:
            if budget <= 0:
                break
            try:
                hits = await source.discover(category, page)
            except SelectorMissing as exc:
                report.errors.append(f"{category.key}: {exc}")
                continue

            for hit in hits:
                if budget <= 0:
                    break
                budget -= 1
                report.scanned += 1
                try:
                    await self._consider(source, page, session, hit, category.key, report)
                except AuthWallError:
                    raise
                except Exception as exc:
                    log.warning("продукт %s се провали: %s", hit.url, exc)
                    report.errors.append(f"{hit.url}: {exc}")
                await self.pacer.pause(factor=0.25)

    async def _consider(
        self,
        source: BestSecretSource,
        page,
        session: BrowserSession,
        hit: CardHit,
        category_key: str,
        report: CycleReport,
    ) -> None:
        """Един кандидат: оценява от плочката, чете само оцелелите, ценообразува."""
        # Плочката вече носи марка, цена, каталожна цена и намаление. Ако
        # продуктът отпада по тях, продуктовата страница изобщо не се отваря —
        # това спестява стотици заявки на цикъл.
        draft = source.product_from_card(hit, category_key)
        draft_verdict = evaluate(draft, self.cfg.selection)
        if not draft_verdict.accepted:
            log.debug("отпада от листинга %s (%s): %s",
                      draft.id, draft.brand, draft_verdict.reason)
            return

        existing = self.db.get_listing(draft.id)
        if existing is not None and existing["state"] in ("published", "candidate"):
            return

        product = await source.fetch_product(hit.url, category_key, page, hit)
        if product is None:
            return

        # Продуктовата страница е по-точна от плочката, затова се преоценява.
        verdict = evaluate(product, self.cfg.selection)
        self.db.upsert_product(product, verdict.score)

        if not verdict.accepted:
            log.debug("отпада от страницата %s (%s): %s",
                      product.id, product.brand, verdict.reason)
            return

        price = compute_price(product, self.cfg.pricing)
        if price.rejected:
            log.info("отпада по цена %s: %s", product.id, price.rejected)
            self.db.log_event("price_reject", price.rejected, product.id)
            return

        category_path = self.cfg.listing.category_map.get(category_key, [])
        if not category_path:
            log.warning("няма Bazar.bg категория за '%s' — пропускам", category_key)
            return

        listing = build_listing(product, price, self.cfg.listing, category_path)

        # Снимките се свалят сега, докато сесията към източника е жива.
        images = await download_images(
            session.context,
            product.images,
            product.id,
            self.cfg.images_dir,
            self.cfg.listing.max_images,
        )
        if not images:
            log.info("без валидни снимки, отпада: %s", product.id)
            self.db.log_event("no_images", product.url, product.id)
            return

        self.db.save_candidate(listing)
        report.new_candidates += 1
        log.info(
            "нов кандидат: %s %s | %s (себестойност %s, марж %s)",
            product.brand,
            product.name[:40],
            format_money(price.final, price.currency),
            format_money(price.cost, price.currency),
            format_money(price.margin, price.currency),
        )

    async def _recheck_published(
        self,
        source: BestSecretSource,
        page,
        session: BrowserSession,
        report: CycleReport,
    ) -> None:
        due = self.db.listings_due_for_recheck(
            self.cfg.schedule.recheck_min_age_h, limit=40
        )
        for row in due:
            product_id = row["product_id"]
            url = row["product_url"]
            try:
                product = await source.fetch_product(url, "", page)
            except AuthWallError:
                raise
            except Exception as exc:
                log.warning("проверката на %s се провали: %s", product_id, exc)
                continue

            report.rechecked += 1
            if product is None:
                self.db.mark_checked(product_id, ProductStatus.GONE, miss=True)
            else:
                self.db.mark_checked(product_id, product.status)
                self.db.update_prices(product_id, product.price, product.orig_price)
            await self.pacer.pause(factor=0.3)

    # ------------------------------------------------------------ фаза Bazar

    async def _sink_phase(
        self, report: CycleReport, publish: bool, reconcile: bool, dry_run: bool
    ) -> None:
        removals = self._pending_removals() if reconcile else []
        candidates = self.db.pending_candidates(self.cfg.limits.max_publish_per_run) if publish else []

        if not removals and not candidates:
            log.info("Bazar.bg: няма работа този цикъл")
            return

        session = BrowserSession(
            "bazar", self.cfg.profiles_dir / "bazar", self.cfg.runtime.headless,
            seed_state=self.cfg.session_file("bazar"),
        )
        await session.start()
        try:
            sink = BazarSink(self.cfg, session, self.pacer)
            page = await session.new_page()
            await sink.ensure_logged_in(page)

            if removals:
                await self._remove_listings(sink, page, removals, report, dry_run)
            if candidates:
                await self._publish_candidates(sink, page, candidates, report, dry_run)
        finally:
            await session.stop()

    def _pending_removals(self) -> list[tuple[str, str, str, str]]:
        """(product_id, bazar_id, title, причина) за всичко, което трябва да падне."""
        if not self.cfg.removal.auto_remove:
            return []

        pending: list[tuple[str, str, str, str]] = []
        for row in self.db.active_listings():
            product = self.db.get_product(row["product_id"])
            if product is None:
                continue
            reason = self._removal_reason(product, row["product_id"])
            if reason:
                pending.append((row["product_id"], row["bazar_id"] or "", row["title"], reason))
        return pending

    def _removal_reason(self, product: Product, product_id: str) -> str:
        if product.status == ProductStatus.GONE:
            misses = self.db.miss_count(product_id)
            if misses >= self.cfg.removal.misses_before_removal:
                return "продуктът вече го няма в BestSecret"
            return ""
        if product.status == ProductStatus.SOLD_OUT:
            return "изчерпан е"
        if product.discount_pct < self.cfg.removal.remove_below_discount_pct:
            return f"намалението падна на {product.discount_pct}%"
        return ""

    async def _remove_listings(
        self, sink: BazarSink, page, removals, report: CycleReport, dry_run: bool
    ) -> None:
        limiter = self._remove_limiter()
        for product_id, bazar_id, title, reason in removals:
            if not limiter.allow():
                log.info("дневният лимит за сваляне е изчерпан")
                break
            if not bazar_id:
                self.db.mark_removed(product_id, "няма записан bazar_id")
                continue
            if dry_run:
                log.info("DRY RUN: бих свалил %s (%s)", title, reason)
                continue

            try:
                ok = await sink.delete_ad(bazar_id, page)
            except AuthWallError:
                raise
            except Exception as exc:
                log.warning("свалянето на %s се провали: %s", bazar_id, exc)
                report.errors.append(f"delete {bazar_id}: {exc}")
                continue

            if ok:
                self.db.mark_removed(product_id, reason)
                self.db.log_event("removed", reason, product_id)
                limiter.consume()
                report.removed += 1
                if self.cfg.notifications.on_remove:
                    await self.notifier.removed(title, reason)
            await self.pacer.pause()

    async def _publish_candidates(
        self, sink: BazarSink, page, candidates, report: CycleReport, dry_run: bool
    ) -> None:
        limiter = self._publish_limiter()
        active = self.db.active_listing_count()

        for row in candidates:
            if not limiter.allow():
                log.info("дневният лимит за публикуване е изчерпан (%d)", limiter.cap)
                break
            if active >= self.cfg.limits.max_active_listings:
                log.info("достигнат е таванът от %d активни обяви", active)
                break

            product = self.db.get_product(row["product_id"])
            if product is None:
                continue

            price = compute_price(product, self.cfg.pricing)
            if price.rejected:
                self.db.mark_failed(row["product_id"], f"цена: {price.rejected}")
                continue

            listing = build_listing(
                product, price, self.cfg.listing, row["category_label"]
            )
            images = sorted((self.cfg.images_dir / product.id).glob("*.jpg"))
            if not images:
                self.db.mark_failed(row["product_id"], "снимките липсват на диска")
                continue

            try:
                bazar_id, bazar_url = await sink.publish(listing, list(images), page, dry_run)
            except AuthWallError:
                raise
            except (PublishError, SelectorMissing) as exc:
                log.warning("публикуването на %s се провали: %s", product.id, exc)
                self.db.mark_failed(product.id, str(exc))
                report.errors.append(str(exc))
                if self.cfg.notifications.on_error:
                    await self.notifier.error("публикуване", str(exc))
                await self.pacer.pause()
                continue

            if dry_run:
                log.info("DRY RUN: обявата е попълнена, но не е изпратена")
                break

            self.db.mark_published(product.id, bazar_id, bazar_url)
            self.db.log_event("published", bazar_url, product.id)
            limiter.consume()
            active += 1
            report.published += 1
            if self.cfg.notifications.on_publish:
                await self.notifier.published(
                    listing.title, format_money(listing.price, listing.currency), bazar_url
                )
            await self.pacer.pause()

    # ------------------------------------------------------- непрекъснат режим

    async def run_forever(self) -> None:
        """Дълготраен режим за домашния сървър."""
        last_discover = 0.0
        loop = asyncio.get_running_loop()

        while True:
            if not self.pacer.within_active_hours():
                wait = self.pacer.seconds_until_active()
                log.info("извън работния прозорец, спя %.0f минути", wait / 60)
                await asyncio.sleep(min(wait, 1800))
                continue

            now = loop.time()
            do_discover = (now - last_discover) >= self.cfg.schedule.discover_every_min * 60
            if do_discover:
                last_discover = now

            try:
                await self.run_once(discover=do_discover, publish=True, reconcile=True)
            except Exception as exc:
                log.exception("цикълът гръмна")
                self.db.log_event("crash", str(exc))
                if self.cfg.notifications.on_error:
                    await self.notifier.error("основния цикъл", str(exc))

            await asyncio.sleep(self.cfg.schedule.recheck_every_min * 60)
