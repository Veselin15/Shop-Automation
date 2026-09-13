"""Оркестрация: откриване -> публикуване -> следене -> сваляне."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from .appraise import AppraisalUnavailable, appraise, prompt_version
from .browser import AuthWallError, BrowserSession, SelectorMissing
from .config import Config
from .db import MAX_PUBLISH_ATTEMPTS, Database
from .humanize import DailyLimiter, Pacer
from .listing import build_listing
from .media import download_images
from .models import Product, ProductStatus
from .notify import Notifier
from .pricing import compute_price, format_money
from .selection import Swap, evaluate, fair_order, plan_swaps
from .sinks.bazar import (
    BazarSink,
    CaptchaWall,
    ProfileSnapshot,
    PublishError,
    QuotaExhausted,
)
from .sources.bestsecret import BestSecretSource, CardHit

log = logging.getLogger(__name__)


@dataclass
class CycleReport:
    scanned: int = 0
    opened: int = 0
    new_candidates: int = 0
    appraised: int = 0
    appraisal_rejects: int = 0
    published: int = 0
    removed: int = 0
    swapped: int = 0
    purged: int = 0
    rechecked: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"прегледани {self.scanned} (отворени {self.opened}), "
            f"нови кандидати {self.new_candidates} "
            f"(оценени {self.appraised}, отпаднали {self.appraisal_rejects}), "
            f"публикувани {self.published}, проверени {self.rechecked}, "
            f"свалени {self.removed}, разменени {self.swapped}, "
            f"изтрити неактивни {self.purged}, грешки {len(self.errors)}"
        )


def publish_queue(db: Database, cfg: Config, limit: int) -> list:
    """Кандидатите в реда, в който ще излизат. Същият ред вижда и `shopbot candidates`.

    Паметта е колкото един ден обяви. По-дълга кара категория, която е
    липсвала седмица, да изземе всички места наведнъж, щом се появи.
    """
    return fair_order(
        db.pending_candidates(),
        cfg.source.weights,
        db.recent_publish_counts(cfg.limits.max_publish_per_day),
        limit,
    )


def swap_plan(db: Database, cfg: Config, limit: int | None = None) -> list[Swap]:
    """Коя обява би отстъпила място и на кого. Същото вижда и `shopbot rotation`.

    Кандидатите се редят спрямо активните обяви, не спрямо излезлите за деня:
    при пълен профил въпросът е кой дял е недопълнен сега.
    """
    active = db.rotation_pool()
    weights = cfg.source.weights
    queue = fair_order(
        db.pending_candidates(), weights, Counter(r["category_key"] for r in active)
    )
    return plan_swaps(active, queue, weights, cfg.limits.profile_slots, cfg.rotation,
                      limit=limit)


class Orchestrator:
    def __init__(self, cfg: Config, db: Database, notifier: Notifier) -> None:
        self.cfg = cfg
        self.db = db
        self.notifier = notifier
        self.pacer = Pacer(cfg.runtime)
        # Презарежда се в началото на всяко обхождане.
        self._appraisal_budget = cfg.appraisal.max_calls_per_run
        # Сайтове, за които вече е пратено известие "иска ръчен вход".
        self._walled: set[str] = set()
        # Свободните места при последното влизане в Bazar.bg; None — не знаем.
        self._free_slots: int | None = None

    # ------------------------------------------------------------- лимити

    def _publish_limiter(self) -> DailyLimiter:
        return DailyLimiter(self.db, "publish", self.cfg.limits.max_publish_per_day)

    def _remove_limiter(self) -> DailyLimiter:
        return DailyLimiter(self.db, "remove", self.cfg.limits.max_remove_per_day)

    def _swap_limiter(self) -> DailyLimiter:
        return DailyLimiter(self.db, "swap", self.cfg.rotation.max_per_day)

    def _quota_blocked(self) -> bool:
        """Свършили ли са безплатните обяви до ден, който още не е дошъл."""
        until = self.db.get_state("free_ads_resume_on")
        return bool(until) and self.pacer.now().date() < date.fromisoformat(until)

    def _plan_swaps(self, limit: int) -> list[Swap]:
        if not self.cfg.rotation.enabled:
            return []
        return swap_plan(self.db, self.cfg, min(limit, self._swap_limiter().remaining))

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
                self._walled.discard("bestsecret")
            except AuthWallError as exc:
                await self._auth_wall("bestsecret", exc, report)

        if publish or reconcile:
            try:
                if await self._sink_phase(report, publish, reconcile, dry_run):
                    self._walled.discard("bazar")
            except AuthWallError as exc:
                await self._auth_wall("bazar", exc, report)

        self.db.log_event("cycle", report.summary())
        log.info("Цикълът приключи: %s", report.summary())
        return report

    async def _auth_wall(self, site: str, exc: AuthWallError, report: CycleReport) -> None:
        """Едно известие на прекъсване, не по едно на всеки цикъл.

        Докато човекът не влезе, стената се удря на всеки 90 минути; четири
        еднакви съобщения подред заравят това, което наистина трябва да се види.
        """
        report.errors.append(str(exc))
        self.db.log_event("auth_wall", str(exc))
        if site in self._walled:
            return
        self._walled.add(site)
        if self.cfg.notifications.on_auth_wall:
            await self.notifier.auth_wall(site)

    # --------------------------------------------------------- фаза източник

    async def _source_phase(self, report: CycleReport, discover: bool, reconcile: bool) -> None:
        session = BrowserSession(
            "bestsecret", self.cfg.profiles_dir / "bestsecret", self.cfg.runtime.headless,
            seed_state=self.cfg.session_file("bestsecret"),
        )
        await session.start()
        signed_in = False
        try:
            source = BestSecretSource(self.cfg, session)
            page = await session.new_page()
            await source.ensure_session(page)
            signed_in = True

            if reconcile:
                await self._recheck_published(source, page, session, report)
                await self._recheck_queue(source, page, report)
            if discover:
                await self._discover(source, page, session, report)
        except AuthWallError:
            signed_in = False    # мъртва сесия не бива да затрие файла
            raise
        finally:
            if signed_in:
                await session.save_seed()
            await session.stop()

    async def _discover(
        self,
        source: BestSecretSource,
        page,
        session: BrowserSession,
        report: CycleReport,
    ) -> None:
        # Два отделни бюджета. Оценката на плочка е безплатна (вече е в
        # паметта); скъпото е отварянето на продуктова страница.
        tile_budget = self.cfg.limits.max_tiles_per_run
        fetch_budget = self.cfg.limits.max_product_pages_per_run
        self._appraisal_budget = self.cfg.appraisal.max_calls_per_run

        configured = [c for c in self.cfg.source.categories if c.is_configured]
        if not configured:
            msg = (
                "нито една категория в source.categories няма попълнен `url` — "
                "отвори филтрираните листинги в BestSecret и копирай адресите"
            )
            log.error(msg)
            report.errors.append(msg)
            return

        # Отварянията се делят между категориите по тегло. Без делене първата
        # категория (дамските очила са 700+ след филтъра) изяжда целия бюджет
        # и часовниците, чантите и шапките не се стигат нито един цикъл.
        total_weight = sum(max(c.weight, 0.0) for c in configured) or float(len(configured))
        quota = {
            c.key: max(1, int(fetch_budget * max(c.weight, 0.0) / total_weight))
            for c in configured
        }

        for index, category in enumerate(configured):
            if fetch_budget <= 0 or tile_budget <= 0:
                # Мълчаливото спиране тук значи, че цели категории никога не
                # се обхождат — затова се вижда в лога.
                log.warning(
                    "бюджетът свърши на %d-та от %d категории (плочки %d, "
                    "отваряния %d); необходените остават за следващия цикъл",
                    index + 1, len(configured), tile_budget, fetch_budget,
                )
                break
            try:
                hits = await source.discover(category, page)
            except SelectorMissing as exc:
                report.errors.append(f"{category.key}: {exc}")
                continue

            category_fetch = quota[category.key]
            for hit in hits:
                if fetch_budget <= 0 or tile_budget <= 0 or category_fetch <= 0:
                    break
                tile_budget -= 1
                report.scanned += 1
                opened = False
                try:
                    opened = await self._consider(
                        source, page, session, hit, category.key, report
                    )
                except AuthWallError:
                    raise
                except Exception as exc:
                    log.warning("продукт %s се провали: %s", hit.url, exc)
                    report.errors.append(f"{hit.url}: {exc}")
                    opened = True  # заявката е тръгнала, дължим пауза
                # Пауза само след реална заявка. Изчакване между две
                # сравнения в паметта не пази никого и изяжда цикъла.
                if opened:
                    fetch_budget -= 1
                    category_fetch -= 1
                    report.opened += 1
                    await self.pacer.pause(factor=0.25)

    async def _consider(
        self,
        source: BestSecretSource,
        page,
        session: BrowserSession,
        hit: CardHit,
        category_key: str,
        report: CycleReport,
    ) -> bool:
        """Един кандидат: оценява от плочката, чете само оцелелите.

        Връща True, ако е отворена продуктова страница — по това се мерят
        и бюджетът, и паузите.
        """
        # Плочката вече носи марка, цена, каталожна цена и намаление. Ако
        # продуктът отпада по тях, продуктовата страница изобщо не се отваря —
        # това спестява стотици заявки на цикъл.
        draft = source.product_from_card(hit, category_key)
        draft_verdict = evaluate(draft, self.cfg.selection)
        if not draft_verdict.accepted:
            log.debug("отпада от листинга %s (%s): %s",
                      draft.id, draft.brand, draft_verdict.reason)
            return False

        # Свалената обява не се вдига наново (save_candidate не пипа removed),
        # а изчерпалата опитите си не се публикува пак — отварянето на
        # страницата им само яде квотата и брои фалшив "нов кандидат".
        existing = self.db.get_listing(draft.id)
        if existing is not None and (
            existing["state"] in ("published", "candidate", "removed")
            or (existing["state"] == "failed" and existing["attempts"] >= MAX_PUBLISH_ATTEMPTS)
        ):
            return False

        # Моделът вече е казал "не" по същите правила — страницата не се
        # отваря пак. Без това отхвърлените часовници в началото на листинга
        # изяждаха квотата на категорията всеки цикъл и ботът не стигаше до
        # нито един нов часовник по-надолу.
        if self._judged_unsellable(draft):
            return False

        product = await source.fetch_product(hit.url, category_key, page, hit)
        if product is None:
            return True

        # Продуктовата страница е по-точна от плочката, затова се преоценява.
        verdict = evaluate(product, self.cfg.selection)
        self.db.upsert_product(product, verdict.score)

        if not verdict.accepted:
            log.debug("отпада от страницата %s (%s): %s",
                      product.id, product.brand, verdict.reason)
            return True

        price = compute_price(product, self.cfg.pricing)
        if price.rejected:
            log.info("отпада по цена %s: %s", product.id, price.rejected)
            self.db.log_event("price_reject", price.rejected, product.id)
            return True

        category_id = self.cfg.listing.category_map.get(category_key, 0)
        if not category_id:
            log.warning("няма Bazar.bg рубрика за '%s' — пропускам", category_key)
            return True

        listing = build_listing(product, price, self.cfg.listing, category_id)

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
            return True

        if not await self._appraisal_passes(product, price, images, report):
            return True

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
        return True

    def _appraisal_threshold(self, category_key: str) -> float:
        cfg = self.cfg.appraisal
        return cfg.min_score_by_category.get(category_key, cfg.min_score)

    def _judged_unsellable(self, product: Product) -> bool:
        """Има ли оценка под прага — по текущите правила и текущия праг."""
        if not self.cfg.appraisal.enabled:
            return False
        cached = self.db.get_appraisal(product.id, prompt_version())
        return (cached is not None
                and cached["score"] < self._appraisal_threshold(product.category_key))

    async def _appraisal_passes(self, product, price, images, report: CycleReport) -> bool:
        """Пуска ли моделът този артикул. Изключената оценка пуска всичко.

        Снимката е решаващата част, затова оценката идва след свалянето им.
        """
        cfg = self.cfg.appraisal
        if not cfg.enabled:
            return True

        threshold = self._appraisal_threshold(product.category_key)

        cached = self.db.get_appraisal(product.id, prompt_version())
        if cached is not None:
            verdict_score, reason = cached["score"], cached["reason"]
        else:
            if self._appraisal_budget <= 0:
                log.info("оценките за цикъла свършиха, %s остава за следващия", product.id)
                return False
            try:
                verdict = await appraise(
                    product, price.final, price.currency, images,
                    cfg, self.cfg.appraisal_key,
                )
            except AppraisalUnavailable as exc:
                # Мълчаливото пускане тук значи да се плаща за обяви, които
                # моделът е трябвало да отсее — затова се вижда в лога.
                log.warning("оценката на %s се провали: %s", product.id, exc)
                report.errors.append(f"оценка {product.id}: {exc}")
                return cfg.on_error == "accept"
            self._appraisal_budget -= 1
            report.appraised += 1
            verdict_score, reason = verdict.score, verdict.reason
            self.db.save_appraisal(product.id, verdict.score, verdict.reason,
                                   verdict.model, prompt_version())

        if verdict_score < threshold:
            report.appraisal_rejects += 1
            log.info("моделът отказва %s (%s): %.2f < %.2f — %s",
                     product.id, product.brand, verdict_score, threshold, reason)
            self.db.log_event("appraisal_reject", f"{verdict_score:.2f} {reason}", product.id)
            return False

        log.debug("моделът пуска %s: %.2f — %s", product.id, verdict_score, reason)
        return True

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

            if product is not None and product.price <= 0:
                # Името се е прочело, цената — не. Това е неуспешно четене, а
                # не присъда: нула тук сваля обявата с "намалението падна на 0%".
                log.warning("проверката на %s не прочете цена, оставям я за после",
                            product_id)
                await self.pacer.pause(factor=0.3)
                continue

            report.rechecked += 1
            if product is None:
                self.db.mark_checked(product_id, ProductStatus.GONE, miss=True)
            else:
                self.db.mark_checked(product_id, product.status)
                self.db.update_prices(product_id, product.price, product.orig_price)
            await self.pacer.pause(factor=0.3)

    async def _recheck_queue(self, source: BestSecretSource, page, report: CycleReport) -> None:
        """Проверява в BestSecret кандидатите, които излизат следващи.

        Безплатните обяви са сто. Кандидат, чакал два дни в опашката, може
        вече да е изчерпан — обява за него изгаря една от стоте и носи само
        купувач, на когото няма какво да се продаде.
        """
        if self._quota_blocked():
            return
        cutoff = (
            datetime.now(UTC) - timedelta(hours=self.cfg.schedule.recheck_min_age_h)
        ).isoformat(timespec="seconds")

        for row in publish_queue(self.db, self.cfg, self.cfg.limits.max_publish_per_run):
            product_id = row["product_id"]
            checked = self.db.last_checked(product_id)
            if checked and checked >= cutoff:
                continue
            product = self.db.get_product(product_id)
            if product is None:
                continue
            try:
                fresh = await source.fetch_product(product.url, product.category_key, page)
            except AuthWallError:
                raise
            except Exception as exc:
                log.warning("проверката на кандидат %s се провали: %s", product_id, exc)
                continue
            await self.pacer.pause(factor=0.3)

            report.rechecked += 1
            if fresh is None:
                self.db.mark_checked(product_id, ProductStatus.GONE, miss=True)
                if self.db.miss_count(product_id) >= self.cfg.removal.misses_before_removal:
                    self._drop_candidate(product_id, "продуктът вече го няма в BestSecret")
                continue
            if fresh.price <= 0:
                continue    # непрочетена цена не е присъда

            self.db.mark_checked(product_id, fresh.status)
            self.db.update_prices(product_id, fresh.price, fresh.orig_price)
            current = self.db.get_product(product_id)
            if fresh.status == ProductStatus.SOLD_OUT:
                self._drop_candidate(product_id, "изчерпан преди публикуване")
            elif current.discount_pct < self.cfg.selection.min_discount_pct:
                self._drop_candidate(
                    product_id, f"намалението падна на {current.discount_pct}% преди публикуване"
                )

    def _drop_candidate(self, product_id: str, reason: str) -> None:
        log.info("кандидатът %s отпада: %s", product_id, reason)
        self.db.mark_removed(product_id, reason)
        self.db.log_event("dropped", reason, product_id)

    # ------------------------------------------------------------ фаза Bazar

    async def _sink_phase(
        self, report: CycleReport, publish: bool, reconcile: bool, dry_run: bool
    ) -> bool:
        """Връща дали е влязъл в Bazar.bg."""
        removals = self._pending_removals() if reconcile else []
        # При изчерпан дневен лимит няма какво да се публикува. Без тази
        # проверка ботът пак влизаше в профила на всеки цикъл само за да
        # установи това — вход без никакво действие изглежда като бот.
        # Свършилите безплатни обяви също: до деня, който Bazar.bg е казал,
        # всяка изпратена обява отива в „Чакащи плащане“.
        may_publish = (
            publish
            and not self._quota_blocked()
            and self._publish_limiter().allow()
            and bool(self.db.pending_candidates())
        )
        # Същото при пълен профил: при последното влизане нямаше място, а по
        # броячите от него няма и обява, която да отстъпи своето.
        if may_publish and self._free_slots == 0 and not self._plan_swaps(limit=1):
            may_publish = False

        if not removals and not may_publish:
            log.info("Bazar.bg: няма работа този цикъл")
            return False

        session = BrowserSession(
            "bazar", self.cfg.profiles_dir / "bazar", self.cfg.runtime.headless,
            seed_state=self.cfg.session_file("bazar"),
        )
        await session.start()
        signed_in = False
        try:
            sink = BazarSink(self.cfg, session, self.pacer)
            page = await session.new_page()
            await sink.ensure_logged_in(page)
            signed_in = True

            snapshot = await sink.read_profile(page)
            free = self._take_stock(snapshot)
            free += await self._purge_inactive(sink, page, snapshot, report, dry_run)
            if removals:
                removed = await self._remove_listings(sink, page, removals, report, dry_run)
                if self.cfg.removal.mode == "delete":
                    free += removed
            if may_publish:
                budget = self.cfg.limits.max_publish_per_run
                candidates = publish_queue(self.db, self.cfg, min(budget, free))
                published, stopped = await self._publish_candidates(
                    sink, page, candidates, report, dry_run, slots=free
                )
                free -= published
                if free <= 0 and not stopped:
                    free += await self._rotate(sink, page, report, dry_run, budget - published)
            self._free_slots = free
            return True
        except AuthWallError:
            signed_in = False    # мъртва сесия не бива да затрие файла
            raise
        finally:
            if signed_in:
                await session.save_seed()
            await session.stop()

    def _pending_removals(self) -> list[tuple[str, str, str, str, str]]:
        """(product_id, bazar_id, title, причина, адрес) за всичко за сваляне."""
        if not self.cfg.removal.auto_remove:
            return []

        pending: list[tuple[str, str, str, str, str]] = []
        for row in self.db.active_listings():
            product = self.db.get_product(row["product_id"])
            if product is None:
                continue
            reason = self._removal_reason(product, row)
            if reason:
                pending.append((
                    row["product_id"], row["bazar_id"] or "", row["title"],
                    reason, row["bazar_url"] or "",
                ))
        return pending

    def _removal_reason(self, product: Product, row) -> str:
        product_id = row["product_id"]
        if product.status == ProductStatus.GONE:
            misses = self.db.miss_count(product_id)
            if misses >= self.cfg.removal.misses_before_removal:
                return "продуктът вече го няма в BestSecret"
            return ""
        if product.status == ProductStatus.SOLD_OUT:
            return "изчерпан е"
        if product.discount_pct < self.cfg.removal.remove_below_discount_pct:
            return f"намалението падна на {product.discount_pct}%"

        # Офертата, заради която обявата изобщо е пусната. Дребно свиване
        # (75% -> 72%) не променя сметката; голямото значи, че вече продаваме
        # нещо друго на цената на старата оферта.
        was = row["publish_discount_pct"] if "publish_discount_pct" in row.keys() else 0
        drop = was - product.discount_pct
        if was and drop > self.cfg.removal.max_discount_drop_pct:
            return f"намалението падна от {was}% на {product.discount_pct}%"
        return ""

    async def _remove_listings(
        self, sink: BazarSink, page, removals, report: CycleReport, dry_run: bool
    ) -> int:
        limiter = self._remove_limiter()
        removed = 0
        for product_id, bazar_id, title, reason, bazar_url in removals:
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
                if self.cfg.removal.mode == "deactivate":
                    ok = await sink.deactivate_ad(bazar_id, page)
                else:
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
                removed += 1
                if self.cfg.notifications.on_remove:
                    await self.notifier.removed(title, reason, bazar_url)
            await self.pacer.pause()
        return removed

    # ------------------------------------------------------ места в профила

    def _take_stock(self, snapshot: ProfileSnapshot) -> int:
        """Свободните места по истинския брой в профила.

        Базата не знае за обявите, пуснати на ръка, нито кои са изтекли, а
        Bazar.bg брои и деактивираните. Затова решава прочетеното. Базата е
        само долна граница: изпусната страница не бива да отвори места, които
        ги няма — следващата обява удря тавана.
        """
        for bazar_id in snapshot.inactive:
            row = self.db.listing_by_bazar_id(bazar_id)
            if row is not None and row["state"] == "published":
                self.db.mark_removed(row["product_id"], "неактивна в Bazar.bg (изтекла?)")
                self.db.log_event("expired", bazar_id, row["product_id"])
        self._reclaim_unseen(snapshot)

        stats ={ad_id: s for ad_id, s in snapshot.active.items() if s is not None}
        self.db.save_ad_stats(stats)
        if snapshot.active and not stats:
            log.warning("броячите под обявите не се прочетоха — размени няма да има; "
                        "провери „Прегледи / Телефон / Любими“ в „Моите обяви“")

        slots = self.cfg.limits.profile_slots
        used = max(snapshot.used, self.db.active_listing_count())
        free = max(slots - used, 0)
        log.info("Bazar.bg: %d активни + %d неактивни от %d места, свободни %d",
                 len(snapshot.active), len(snapshot.inactive), slots, free)
        return free

    def _reclaim_unseen(self, snapshot: ProfileSnapshot) -> None:
        """Обява от базата, която я няма в нито един от двата списъка.

        Скорошната най-често е изпратена след края на безплатните обяви: има
        номер, но чака плащане и никой не я вижда — връща се в опашката. По-
        старата е изтрита на ръка. Липсват ли много наведнъж, четенето се е
        провалило и базата не се пипа.
        """
        if not snapshot.active:
            return
        seen = set(snapshot.active) | set(snapshot.inactive)
        unseen = [r for r in self.db.active_listings()
                  if r["bazar_id"] and r["bazar_id"] not in seen]
        if len(unseen) > 10:
            log.warning("Bazar.bg: %d обяви от базата ги няма в профила — "
                        "по-скоро четенето е непълно, не пипам нищо", len(unseen))
            return

        recent = datetime.now(UTC) - timedelta(days=2)
        for row in unseen:
            published = row["published_at"]
            if published and datetime.fromisoformat(published) >= recent:
                reason = "не се вижда в Bazar.bg (чака плащане?)"
                self.db.requeue(row["product_id"], reason)
            else:
                reason = "изчезнала от Bazar.bg (ръчно изтрита?)"
                self.db.mark_removed(row["product_id"], reason)
            log.info("Bazar.bg: %s — %s", row["title"], reason)
            self.db.log_event("unseen", f"{row['bazar_id']} {reason}", row["product_id"])

    async def _purge_inactive(
        self, sink: BazarSink, page, snapshot: ProfileSnapshot, report: CycleReport,
        dry_run: bool,
    ) -> int:
        """Трие деактивираните обяви на бота. Връща освободените места.

        Деактивираната обява заема място от стоте, а никой не я вижда. Номер,
        който базата не познава, е пуснат на ръка — него ботът не пипа.
        """
        if self.cfg.removal.mode != "delete":
            return 0
        limiter = self._remove_limiter()
        purged = 0
        for bazar_id in snapshot.inactive:
            row = self.db.listing_by_bazar_id(bazar_id)
            if row is None:
                continue
            if not limiter.allow():
                log.info("дневният лимит за сваляне е изчерпан")
                break
            if dry_run:
                log.info("DRY RUN: бих изтрил неактивната %s", row["title"])
                continue
            try:
                ok = await sink.delete_ad(bazar_id, page)
            except AuthWallError:
                raise
            except Exception as exc:
                log.warning("изтриването на неактивната %s се провали: %s", bazar_id, exc)
                report.errors.append(f"delete {bazar_id}: {exc}")
                continue
            if ok:
                self.db.log_event("purged", f"неактивна {bazar_id}", row["product_id"])
                limiter.consume()
                report.purged += 1
                purged += 1
            await self.pacer.pause()
        return purged

    async def _rotate(
        self, sink: BazarSink, page, report: CycleReport, dry_run: bool, budget: int
    ) -> int:
        """Пълен профил: сменя слаби обяви с по-добри кандидати.

        Връща освободените места: паднала обява, чийто заместник не е
        излязъл, оставя мястото си за следващия цикъл.
        """
        swaps = self._plan_swaps(min(budget, self.cfg.rotation.max_per_run))
        remove_limiter = self._remove_limiter()
        swap_limiter = self._swap_limiter()
        publish_limiter = self._publish_limiter()
        freed = 0

        for swap in swaps:
            if not (remove_limiter.allow() and swap_limiter.allow()
                    and publish_limiter.allow()):
                log.info("дневният лимит за размени е изчерпан")
                break
            victim, candidate = swap.victim, swap.candidate
            reason = (
                f"място за по-добра обява: {candidate['title'][:40]} "
                f"({max(candidate['appraisal'], 0):.2f} срещу {swap.victim_value:.2f})"
            )
            if dry_run:
                log.info("DRY RUN: бих сменил %s — %s", victim["title"], reason)
                continue

            try:
                ok = await sink.delete_ad(victim["bazar_id"], page)
            except AuthWallError:
                raise
            except Exception as exc:
                log.warning("размяната на %s се провали: %s", victim["bazar_id"], exc)
                report.errors.append(f"delete {victim['bazar_id']}: {exc}")
                continue
            if not ok:
                continue

            log.info("размяна: %s пада — %s", victim["title"], reason)
            self.db.mark_removed(victim["product_id"], reason)
            self.db.log_event("rotated", reason, victim["product_id"])
            remove_limiter.consume()
            swap_limiter.consume()
            report.swapped += 1
            freed += 1
            if self.cfg.notifications.on_remove:
                await self.notifier.removed(victim["title"], reason, victim["bazar_url"] or "")
            await self.pacer.pause()

            outcome = await self._publish_one(sink, page, candidate, report, dry_run)
            if outcome == "published":
                freed -= 1
            elif outcome == "stop":
                break
        return freed

    # ------------------------------------------------------------ публикуване

    async def _publish_candidates(
        self, sink: BazarSink, page, candidates, report: CycleReport, dry_run: bool,
        slots: int | None = None,
    ) -> tuple[int, bool]:
        """Връща (публикувани, спряно ли е) — спряната серия не минава към размени."""
        limiter = self._publish_limiter()
        published = 0

        for row in candidates:
            if not limiter.allow():
                log.info("дневният лимит за публикуване е изчерпан (%d)", limiter.cap)
                return published, True
            if slots is not None and published >= slots:
                log.info("профилът е пълен (%d места)", self.cfg.limits.profile_slots)
                break
            outcome = await self._publish_one(sink, page, row, report, dry_run)
            if outcome == "stop":
                return published, True
            if outcome == "published":
                published += 1
        return published, False

    async def _publish_one(
        self, sink: BazarSink, page, row, report: CycleReport, dry_run: bool
    ) -> str:
        """Една обява: "published", "skipped" или "stop" — последното спира серията."""
        product = self.db.get_product(row["product_id"])
        if product is None:
            return "skipped"

        price = compute_price(product, self.cfg.pricing)
        if price.rejected:
            self.db.mark_failed(row["product_id"], f"цена: {price.rejected}")
            return "skipped"

        # Рубриката се извежда от конфига ВСЕКИ ПЪТ, а не се чете от
        # реда в базата: иначе промяна в category_map никога не стига до
        # вече записаните кандидати, а стари редове носят 0 и се провалят
        # безкрайно.
        category_id = self.cfg.listing.category_map.get(product.category_key, 0)
        if not category_id:
            self.db.mark_failed(
                row["product_id"],
                f"няма рубрика за категория '{product.category_key}'",
            )
            return "skipped"

        listing = build_listing(product, price, self.cfg.listing, category_id)
        images = sorted((self.cfg.images_dir / product.id).glob("*.jpg"))
        if not images:
            self.db.mark_failed(row["product_id"], "снимките липсват на диска")
            return "skipped"

        try:
            bazar_id, bazar_url = await sink.publish(listing, list(images), page, dry_run)
        except AuthWallError:
            raise
        except CaptchaWall as exc:
            # Проверката е пред профила, не пред обявата: всеки следващ
            # кандидат удря същата стена и изгаря по опит, докато след три
            # цикъла добрите обяви не умрат завинаги. Спираме без опити.
            log.warning("Bazar.bg иска проверка „не сте робот“, спирам: %s", exc)
            report.errors.append(str(exc))
            self.db.log_event("captcha", str(exc), product.id)
            if self.cfg.notifications.on_error:
                await self.notifier.error(
                    "публикуване",
                    "Bazar.bg поиска проверка „не сте робот“ — публикуването "
                    "спира до следващия цикъл, кандидатите не губят опит.",
                )
            return "stop"
        except QuotaExhausted as exc:
            # Лимитът е на профила, не на обявата — опит не се губи, а
            # публикуването спира до деня, в който Bazar.bg дава нова.
            resume = exc.quota.resume_on or self.pacer.now().date() + timedelta(days=1)
            self.db.set_state("free_ads_resume_on", resume.isoformat())
            log.warning("свършиха безплатните обяви, публикуването спира до %s: %s",
                        resume, exc)
            report.errors.append(str(exc))
            self.db.log_event("quota", f"до {resume}; чака плащане: {exc.draft_id or '-'}",
                              product.id)
            if self.cfg.notifications.on_error:
                draft = (f" Обява {exc.draft_id} остана в „Чакащи плащане“."
                         if exc.draft_id else "")
                await self.notifier.error(
                    "публикуване",
                    f"Свършиха безплатните обяви в Bazar.bg. Публикуването спира до "
                    f"{resume:%d.%m}, свалянето продължава.{draft}",
                )
            return "stop"
        except (PublishError, SelectorMissing) as exc:
            log.warning("публикуването на %s се провали: %s", product.id, exc)
            self.db.mark_failed(product.id, str(exc))
            report.errors.append(str(exc))
            if self.cfg.notifications.on_error:
                await self.notifier.error("публикуване", str(exc))
            await self.pacer.pause()
            return "skipped"

        if dry_run:
            log.info("DRY RUN: обявата е попълнена, но не е изпратена")
            return "stop"

        self.db.mark_published(product.id, bazar_id, bazar_url, product.discount_pct)
        self.db.log_event("published", bazar_url, product.id)
        self._publish_limiter().consume()
        report.published += 1
        if self.cfg.notifications.on_publish:
            await self.notifier.published(
                listing.title,
                format_money(listing.price, listing.currency),
                bazar_url,
                source_url=product.url,
                cost=format_money(product.price, product.currency),
            )
        await self.pacer.publish_pause()
        return "published"

    # ------------------------------------------------------- непрекъснат режим

    async def run_forever(self) -> None:
        """Дълготраен режим за домашния сървър."""
        # loop.time() брои от пускането на машината, не от 1970. С 0.0 за
        # начало първото обхождане след рестарт на сървъра чакаше 4 часа.
        last_discover: float | None = None
        loop = asyncio.get_running_loop()

        while True:
            if not self.pacer.within_active_hours():
                wait = self.pacer.seconds_until_active()
                log.info("извън работния прозорец, спя %.0f минути", wait / 60)
                await asyncio.sleep(min(wait, 1800))
                continue

            now = loop.time()
            do_discover = (
                last_discover is None
                or (now - last_discover) >= self.cfg.schedule.discover_every_min * 60
            )
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
