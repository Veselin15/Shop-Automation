"""Публикуване и сваляне на обяви в Bazar.bg.

Реалните адреси (проверени):
    вход        POST https://bazar.bg/user/login   (полета: email, password)
    нова обява  https://bazar.bg/ads/save
    моите обяви https://bazar.bg/ads/my
    изтриване   https://bazar.bg/ads/delete/<id>

Формата за вход съдържа скрито поле `contact_website`. То е honeypot —
човек не го вижда и не го попълва. Ботът също не бива да го докосва.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from playwright.async_api import Page

from ..browser import (
    AuthWallError,
    BrowserSession,
    any_present,
    dismiss_cookie_banner,
    first_locator,
    require_locator,
    type_like_human,
)
from ..config import Config
from ..humanize import Pacer
from ..listing import label_to_path
from ..models import Listing

log = logging.getLogger(__name__)

SITE = "bazar"


class PublishError(RuntimeError):
    pass


class BazarSink:
    def __init__(self, cfg: Config, session: BrowserSession, pacer: Pacer) -> None:
        self.cfg = cfg
        self.session = session
        self.pacer = pacer
        self.sel = cfg.selectors.get("bazar", {})

    # ------------------------------------------------------------------ вход

    async def _handle_cookies(self, page: Page) -> None:
        await dismiss_cookie_banner(
            page,
            self.sel.get("cookie_reject", []),
            self.sel.get("cookie_dismiss", []),
            self.sel.get("cookie_container"),
        )

    async def is_logged_in(self, page: Page) -> bool:
        return await any_present(page, self.sel.get("logged_in_markers"))

    async def ensure_logged_in(self, page: Page) -> None:
        await page.goto(self.sel["my_ads_url"], wait_until="domcontentloaded")
        await self._handle_cookies(page)

        if await self.is_logged_in(page):
            log.info("Bazar.bg: вече сме логнати")
            return

        email = self.cfg.secrets.bazar_email
        password = self.cfg.secrets.bazar_password
        if not (email and password):
            raise AuthWallError(SITE, "липсват BAZAR_EMAIL/BAZAR_PASSWORD в .env")

        log.info("Bazar.bg: правя вход")
        await page.goto(self.sel["login_url"], wait_until="domcontentloaded")
        await self._handle_cookies(page)

        email_field = await require_locator(page, self.sel["login_email"], "поле за e-mail")
        await type_like_human(email_field, email)
        await self.pacer.micro_pause()

        pwd_field = await require_locator(page, self.sel["login_password"], "поле за парола")
        await type_like_human(pwd_field, password)
        await self.pacer.micro_pause()

        # Honeypot-ът остава празен — умишлено не го пипаме.
        submit = await require_locator(page, self.sel["login_submit"], "бутон за вход")
        await submit.click()
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2500)

        if not await self.is_logged_in(page):
            raise AuthWallError(
                SITE,
                "входът не мина — грешни данни, CAPTCHA или потвърждение по имейл",
            )
        log.info("Bazar.bg: входът мина")

    # ------------------------------------------------------------ публикуване

    async def publish(
        self, listing: Listing, images: list[Path], page: Page, dry_run: bool = False
    ) -> tuple[str, str]:
        """Връща (bazar_id, bazar_url). При dry_run попълва формата и спира."""
        await page.goto(self.sel["publish_url"], wait_until="domcontentloaded")
        await self._handle_cookies(page)

        if not await self.is_logged_in(page):
            raise AuthWallError(SITE, "изхвърлени сме от сесията на формата за обява")

        await self._fill_category(page, listing.category_label)
        await self.pacer.micro_pause()

        title = await require_locator(page, self.sel["form_title"], "заглавие")
        await type_like_human(title, listing.title)
        await self.pacer.micro_pause()

        description = await require_locator(page, self.sel["form_description"], "описание")
        await type_like_human(description, listing.description)
        await self.pacer.micro_pause()

        price = await require_locator(page, self.sel["form_price"], "цена")
        await type_like_human(price, f"{listing.price:.2f}")
        await self.pacer.micro_pause()

        await self._select_if_present(page, self.sel.get("form_currency"), listing.currency)
        await self._fill_location(page, self.cfg.listing.location)
        await self._select_if_present(
            page, self.sel.get("form_condition"), self.cfg.listing.condition
        )

        if images:
            await self._upload_images(page, images)

        if dry_run:
            log.info("DRY RUN: формата е попълнена, но не се изпраща")
            await page.screenshot(path=str(self.cfg.data_dir / "dry_run_form.png"), full_page=True)
            return ("", "")

        submit = await require_locator(page, self.sel["form_submit"], "бутон за публикуване")
        await submit.click()
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(4000)

        ad_id, ad_url = await self._detect_published(page)
        if not ad_id:
            error_text = await self._read_form_error(page)
            shot = self.cfg.data_dir / f"publish_fail_{listing.product_id}.png"
            await page.screenshot(path=str(shot), full_page=True)
            raise PublishError(
                f"обявата не се публикува ({error_text or 'няма ясна грешка'}); "
                f"снимка на екрана: {shot}"
            )

        log.info("Bazar.bg: публикувана обява %s -> %s", ad_id, ad_url)
        return ad_id, ad_url

    async def _detect_published(self, page: Page) -> tuple[str, str]:
        pattern = self.sel.get("published_url_pattern", r"bazar\.bg/obiava-(\d+)")

        match = re.search(pattern, page.url)
        if match:
            return match.group(1), page.url

        # Понякога след запис се показва междинна страница с линк към обявата.
        href = await page.evaluate(
            """
            (pat) => {
              const re = new RegExp(pat);
              for (const a of document.querySelectorAll('a[href]')) {
                if (re.test(a.href)) return a.href;
              }
              return null;
            }
            """,
            pattern,
        )
        if href:
            match = re.search(pattern, href)
            if match:
                return match.group(1), href
        return "", ""

    async def _read_form_error(self, page: Page) -> str:
        for sel in self.sel.get("form_error", []):
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    text = (await loc.inner_text()).strip()
                    if text:
                        return text[:300]
            except Exception:
                continue
        return ""

    # ------------------------------------------------------------ полета

    async def _select_if_present(self, page: Page, candidates, value: str) -> None:
        """Избира стойност в <select>, ако такъв изобщо съществува."""
        if not candidates or not value:
            return
        loc = await first_locator(page, candidates, timeout_ms=1500)
        if loc is None:
            return
        for attempt in (
            lambda: loc.select_option(label=value),
            lambda: loc.select_option(value=value),
        ):
            try:
                await attempt()
                return
            except Exception:
                continue
        log.debug("не мога да избера '%s' — оставям стойността по подразбиране", value)

    async def _fill_category(self, page: Page, category_label: str) -> None:
        """Избира категорията ниво по ниво: 'Мода > Аксесоари > Портфейли'.

        Пътят е нужен, защото етикети като 'Мъжки' се срещат под Часовници,
        под Дрехи и под Обувки — само последният етикет е двусмислен.
        """
        path = label_to_path(category_label)
        if not path:
            raise PublishError("няма категория за тази обява (виж listing.category_map)")

        # Ако е обикновен <select>, там обикновено стои само листото.
        select = await first_locator(page, self.sel.get("form_category_opener"), 1500)
        if select is not None:
            tag = await select.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                await self._select_if_present(
                    page, self.sel.get("form_category_opener"), path[-1]
                )
                return
            await select.click()
            await page.wait_for_timeout(600)

        # Търсачка на категории: пишем листото и избираме от предложенията.
        search = await first_locator(page, self.sel.get("form_category_search"), 2000)
        if search is not None:
            await type_like_human(search, path[-1])
            await page.wait_for_timeout(1200)
            if await self._click_option(page, path[-1]):
                return

        # Дърво: минаваме през нивата в ред.
        clicked_any = False
        for level in path:
            if await self._click_option(page, level):
                clicked_any = True
                await page.wait_for_timeout(700)
        if clicked_any:
            return

        raise PublishError(
            f"не мога да избера категория '{category_label}'. "
            f"Пусни `shopbot calibrate bazar` и оправи form_category_* в selectors.yaml"
        )

    async def _click_option(self, page: Page, label: str) -> bool:
        """Кликва видим елемент с точно този текст. Точното съвпадение е важно:
        'Чанти' иначе би уцелило 'Дамски чанти за рамо' и подобни."""
        for locator in (
            page.get_by_text(label, exact=True),
            page.locator(f"text={label}"),
        ):
            try:
                count = await locator.count()
            except Exception:
                continue
            for i in range(min(count, 5)):
                candidate = locator.nth(i)
                try:
                    if await candidate.is_visible():
                        await candidate.click(timeout=3000)
                        return True
                except Exception:
                    continue
        return False

    async def _fill_location(self, page: Page, city: str) -> None:
        if not city:
            return
        loc = await first_locator(page, self.sel.get("form_location"), 2000)
        if loc is None:
            log.debug("няма поле за град във формата")
            return
        tag = await loc.evaluate("el => el.tagName.toLowerCase()")
        if tag == "select":
            await self._select_if_present(page, self.sel.get("form_location"), city)
            return
        await type_like_human(loc, city)
        await page.wait_for_timeout(1000)
        suggestion = page.locator(f"text={city}").first
        try:
            if await suggestion.count() > 0 and await suggestion.is_visible():
                await suggestion.click()
        except Exception:
            pass

    async def _upload_images(self, page: Page, images: list[Path]) -> None:
        file_input = await first_locator(page, self.sel.get("form_images_input"), 3000)
        if file_input is None:
            log.warning("няма поле за качване на снимки — обявата ще е без снимки")
            return
        paths = [str(p) for p in images[: self.cfg.listing.max_images]]
        await file_input.set_input_files(paths)
        # Качването е асинхронно; изпращане преди да е готово реже снимките.
        await page.wait_for_timeout(2000 + 1200 * len(paths))
        log.info("качени %d снимки", len(paths))

    # --------------------------------------------------------------- сваляне

    async def delete_ad(self, bazar_id: str, page: Page) -> bool:
        url = self.sel["delete_url_template"].format(id=bazar_id)
        await page.goto(url, wait_until="domcontentloaded")
        await self._handle_cookies(page)

        if not await self.is_logged_in(page):
            raise AuthWallError(SITE, "сесията падна при изтриване")

        confirm = await first_locator(page, self.sel.get("delete_confirm"), 3000)
        if confirm is not None:
            await confirm.click()
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(2000)

        still_live = await self.ad_exists(bazar_id, page)
        if still_live:
            log.warning("Bazar.bg: обява %s още е активна след изтриване", bazar_id)
            return False
        log.info("Bazar.bg: обява %s е свалена", bazar_id)
        return True

    async def ad_exists(self, bazar_id: str, page: Page) -> bool:
        """Проверява публично дали обявата още се вижда."""
        resp = await page.goto(
            f"https://bazar.bg/obiava-{bazar_id}", wait_until="domcontentloaded"
        )
        if resp is None:
            return False
        if resp.status in (404, 410):
            return False
        title = (await page.title()).casefold()
        return "не е намерена" not in title and "not found" not in title

    async def list_my_ads(self, page: Page) -> list[str]:
        """ID-тата на активните обяви — за сверяване с базата."""
        await page.goto(self.sel["my_ads_url"], wait_until="domcontentloaded")
        await self._handle_cookies(page)
        if not await self.is_logged_in(page):
            raise AuthWallError(SITE, "сесията падна при четене на моите обяви")

        ids = await page.evaluate(
            r"""
            () => {
              const found = new Set();
              for (const a of document.querySelectorAll('a[href]')) {
                const m = a.href.match(/obiava-(\d+)/);
                if (m) found.add(m[1]);
              }
              for (const el of document.querySelectorAll('[data-ad-id]')) {
                found.add(el.getAttribute('data-ad-id'));
              }
              return Array.from(found);
            }
            """
        )
        return [str(i) for i in ids if i]
