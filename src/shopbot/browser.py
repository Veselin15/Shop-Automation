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

# Реалистичен, стабилен UA. Не се върти на всеки старт — сменящ се UA при
# същите бисквитки изглежда по-подозрително, отколкото постоянен.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

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

    def __init__(self, name: str, profile_dir: Path, headless: bool = True) -> None:
        self.name = name
        self.profile_dir = profile_dir
        self.headless = headless
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
                user_agent=USER_AGENT,
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
        return self.context

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


async def dismiss_cookie_banner(page: Page, reject: Sequence[str], accept: Sequence[str]) -> None:
    """Отказва незадължителните бисквитки, ако има такъв бутон."""
    for sel in list(reject) + list(accept):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=3000)
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue
