"""Playwright обвивка: постоянни профили, устойчиви селектори, разпознаване на login wall."""

from __future__ import annotations

import json
import logging
import random
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

log = logging.getLogger(__name__)

# User-Agent НЕ се подменя умишлено.
#
# Playwright сменя само заглавието User-Agent, но не и client hints
# (Sec-CH-UA), които продължават да съобщават истинската версия на Chromium.
# Подменен UA значи сървърът да види "Chrome/128" в едното и "151" в другото
# — несъответствие, което е по-силен сигнал за автоматизация, отколкото
# каквото и да е друго. По-честният вариант е браузърът да се представя
# такъв, какъвто е.

VIEWPORTS = [(1440, 900), (1536, 864), (1366, 768)]


class AuthWallError(RuntimeError):
    """Сесията е изтекла или сайтът иска човешка проверка (CAPTCHA)."""

    def __init__(self, site: str, detail: str = "") -> None:
        super().__init__(f"{site}: нужен е ръчен вход ({detail})" if detail else site)
        self.site = site
        self.detail = detail


class SelectorMissing(RuntimeError):
    """Нито един кандидат-селектор не е намерен — най-вероятно сайтът е сменил дизайна."""


class BrowserMissing(RuntimeError):
    """Playwright е инсталиран, но самият Chromium не е свален."""


class BrowserSession:
    """Един постоянен профил (бисквитки + localStorage) на диска."""

    def __init__(
        self,
        name: str,
        profile_dir: Path,
        headless: bool = True,
        seed_state: Path | None = None,
    ) -> None:
        self.name = name
        self.profile_dir = profile_dir
        self.headless = headless
        # JSON с изнесена сесия, който се влива при всяко пускане. Нужен е,
        # защото сесийните бисквитки не преживяват затварянето на браузъра.
        self.seed_state = seed_state
        self._pw: Any = None
        self.context: BrowserContext | None = None

    async def start(self) -> BrowserContext:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        width, height = random.choice(VIEWPORTS)
        try:
            self.context = await self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=self.headless,
                viewport={"width": width, "height": height},
                locale="bg-BG",
                timezone_id="Europe/Sofia",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
        except PlaywrightError as exc:
            # Инсталирането на пакета не сваля браузъра — това са две стъпки
            # и втората често се пропуска.
            if "Executable doesn't exist" in str(exc):
                await self._pw.stop()
                self._pw = None
                raise BrowserMissing(
                    "Chromium за Playwright не е свален. Пусни:\n"
                    "  Windows:  .\\.venv\\Scripts\\playwright install chromium\n"
                    "  Linux:    ./.venv/bin/playwright install chromium"
                ) from None
            raise
        self.context.set_default_timeout(30_000)

        if self.seed_state and self.seed_state.exists():
            try:
                await self.seed_cookies(self.seed_state)
            except Exception as exc:
                log.warning("сесията от %s не се вля: %s", self.seed_state, exc)

        return self.context

    async def seed_cookies(self, source: Path) -> int:
        """Влива само бисквитките от изнесена сесия. Без навигация, евтино е."""
        assert self.context, "сесията не е стартирана"
        state = json.loads(source.read_text(encoding="utf-8"))
        cookies = state.get("cookies", [])
        if cookies:
            await self.context.add_cookies(cookies)
            log.info("вляти %d бисквитки от %s", len(cookies), source.name)
        return len(cookies)

    async def stop(self) -> None:
        if self.context:
            await self.context.close()
            self.context = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def new_page(self) -> Page:
        assert self.context, "сесията не е стартирана"
        return await self.context.new_page()

    # ------------------------------------------------------- пренос на сесия

    async def export_state(self, dest: Path) -> Path:
        """Записва бисквитките и localStorage като преносим JSON.

        Профилът на Chromium НЕ може просто да се копира между машини:
        бисквитките са криптирани с ключ на операционната система (DPAPI под
        Windows), затова профил, направен на Windows, не се чете от Linux.
        Този JSON е в чист вид и се пренася без проблем.
        """
        assert self.context, "сесията не е стартирана"
        dest.parent.mkdir(parents=True, exist_ok=True)
        state = await self.context.storage_state()
        dest.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return dest

    async def import_state(self, source: Path) -> tuple[int, int]:
        """Влива изнесена сесия в текущия профил. Връща (бисквитки, origins)."""
        assert self.context, "сесията не е стартирана"
        state = json.loads(source.read_text(encoding="utf-8"))

        cookies = state.get("cookies", [])
        if cookies:
            await self.context.add_cookies(cookies)

        origins = state.get("origins", [])
        if origins:
            page = await self.context.new_page()
            for origin in origins:
                items = origin.get("localStorage", [])
                if not items:
                    continue
                try:
                    await page.goto(origin["origin"], wait_until="domcontentloaded")
                    await page.evaluate(
                        """
                        (items) => {
                          for (const { name, value } of items) {
                            try { localStorage.setItem(name, value); } catch (e) {}
                          }
                        }
                        """,
                        items,
                    )
                except Exception as exc:
                    log.warning("localStorage за %s не се пренесе: %s", origin["origin"], exc)
            await page.close()

        return len(cookies), len(origins)


@asynccontextmanager
async def session(name: str, profile_dir: Path, headless: bool = True) -> AsyncIterator[
    BrowserSession
]:
    s = BrowserSession(name, profile_dir, headless)
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


# --------------------------------------------------------------------------
#  Устойчиви селектори
# --------------------------------------------------------------------------


def as_list(value: str | Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


async def first_locator(
    page: Page, candidates: str | Sequence[str] | None, timeout_ms: int = 2500
) -> Locator | None:
    """Пробва кандидатите отгоре надолу и връща първия, който съществува."""
    for sel in as_list(candidates):
        loc = page.locator(sel).first
        try:
            await loc.wait_for(state="attached", timeout=timeout_ms)
            return loc
        except Exception:
            continue
    return None


async def require_locator(
    page: Page, candidates: str | Sequence[str] | None, what: str, timeout_ms: int = 5000
) -> Locator:
    loc = await first_locator(page, candidates, timeout_ms)
    if loc is None:
        raise SelectorMissing(
            f"Не намирам '{what}' на {page.url}. "
            f"Пробвани селектори: {as_list(candidates)}. "
            f"Обнови config/selectors.yaml (виж `shopbot calibrate`)."
        )
    return loc


async def any_present(page: Page, candidates: str | Sequence[str] | None) -> bool:
    for sel in as_list(candidates):
        try:
            if await page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


async def text_of(page: Page, candidates: str | Sequence[str] | None) -> str:
    loc = await first_locator(page, candidates)
    if loc is None:
        return ""
    try:
        return (await loc.inner_text()).strip()
    except Exception:
        return ""


async def type_like_human(loc: Locator, text: str) -> None:
    """Пише със забавяне вместо да залепя стойността директно.

    Част от полетата на Bazar.bg следят input събития (брояч на символи,
    автодовършване на категория); фокус + натискане на клавиши ги задейства
    така, както го прави човек.
    """
    await loc.click()
    await loc.fill("")
    await loc.type(text, delay=random.uniform(18, 55))


async def dismiss_cookie_banner(
    page: Page,
    reject: Sequence[str],
    dismiss: Sequence[str] = (),
    container: str | None = None,
) -> bool:
    """Маха банера за бисквитки, като отказва незадължителните.

    Банерът се инжектира със закъснение и прихваща кликовете по цялата
    страница — без това всяко попълване на форма чака 30 секунди и гърми.

    Чака се самият бутон, а не обвиващият контейнер: обвивката е с нулев
    размер и Playwright не я смята за видима, така че изчакване по нея
    излиза веднага и банерът остава.

    "Приемете всички" не се натиска никога; резервният вариант е затваряне.
    """
    found_any = False
    for sel in list(reject) + list(dismiss):
        button = page.locator(sel).first
        try:
            await button.wait_for(state="visible", timeout=4000)
        except Exception:
            continue
        found_any = True

        try:
            await button.click(timeout=5000)
        except Exception as exc:
            log.debug("кликът по %s не мина: %s", sel, exc)
            continue

        if await _banner_gone(page, button, container):
            log.info("банерът за бисквитки е затворен (%s)", sel)
            return True

    if not found_any:
        # Най-често просто вече е отказан и бисквитката за избора е запазена.
        log.debug("няма банер за бисквитки на %s", page.url)
        return False

    log.warning(
        "банерът за бисквитки е на екрана, но не се маха — кликовете ще се "
        "прихващат. Обнови cookie_* в config/selectors.yaml."
    )
    return False


async def _banner_gone(page: Page, button: Locator, container: str | None) -> bool:
    try:
        await button.wait_for(state="hidden", timeout=5000)
    except Exception:
        return False
    if container:
        try:
            await page.locator(container).first.wait_for(state="detached", timeout=2000)
        except Exception:
            pass  # може да остане в DOM-а, стига да не прихваща кликове
    return True
