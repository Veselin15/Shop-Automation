"""Публикуване и сваляне на обяви в Bazar.bg.

Реалните адреси (проверени на живо):
    страница за вход  https://bazar.bg/user/login
    нова обява        https://bazar.bg/ads/save
    моите обяви       https://bazar.bg/ads/my
    изтриване         https://bazar.bg/ads/delete/<id>

Две неща, които не се виждат от HTML-а и струваха време:

1. **Входът не е form POST.** Формата сочи към /user/login, но кликът върху
   бутона задейства JavaScript, който вика поредица от API-та и накрая
   `POST /api/v3_1_0/authentication/login`. Само неговият отговор казва дали
   входът е приет — страницата изглежда еднакво и при успех, и при отказ.
   Затова се чака точно тази заявка, а JSON-ът ѝ носи причината на български.

2. **`contact_website` е honeypot** — родителят му е
   `position:absolute; left:-9999px; aria-hidden="true"`. Човек не го вижда и
   не го попълва; ботът също не го докосва.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

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
from ..models import Listing

log = logging.getLogger(__name__)

SITE = "bazar"
LOGIN_PATH = "/user/login"

CYRILLIC = re.compile(r"[\u0400-\u04FF]")

# Bazar.bg не е последователен къде държи етикета на едно поле, затова
# и инспекторът, и попълването ползват едни и същи помощни функции.
JS_FIELD_HELPERS = r"""
              const labelFor = el => {
                let n = el;
                for (let i = 0; i < 6 && n; i++, n = n.parentElement) {
                  const t = n.querySelector && n.querySelector('.ab_text');
                  if (t) return t.textContent.trim().replace(/\s+/g, ' ');
                }
                return '';
              };
              // Текстът до радио бутона: <label for>, обгръщащ <label>, или
              // текстът на съседа. Bazar.bg не е последователен.
              const choiceText = el => {
                if (el.id) {
                  const l = document.querySelector('label[for="' + el.id + '"]');
                  if (l) return l.textContent.trim().replace(/\s+/g, ' ');
                }
                const wrap = el.closest('label');
                if (wrap) return wrap.textContent.trim().replace(/\s+/g, ' ');
                let n = el.nextSibling;
                while (n) {
                  const t = (n.textContent || '').trim().replace(/\s+/g, ' ');
                  if (t) return t;
                  n = n.nextSibling;
                }
                const parent = el.parentElement;
                return parent ? parent.textContent.trim().replace(/\s+/g, ' ').slice(0, 40) : '';
              };
"""

MANUAL_LOGIN_HINT = (
    "Направи сесията ръчно и я пренеси: на компютъра си пусни "
    "`shopbot login bazar` (там въвеждаш и кода), после "
    "`scp data/sessions/bazar.json <сървър>:~/Shop-Automation/` и на сървъра "
    "`shopbot import-session bazar --file ~/Shop-Automation/bazar.json`"
)


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
        """Решава се по URL-а, не по CSS.

        Bazar.bg препраща излезлите от /ads/my към /user/login?back=... —
        поведение на сайта, което не се чупи при редизайн. DOM маркерите
        остават като допълнителна мрежа.
        """
        if LOGIN_PATH in page.url:
            return False
        if await any_present(page, self.sel.get("logged_in_markers")):
            return True
        # Стигнали сме до "моите обяви" без да ни изхвърлят — значи сме вътре.
        return self.sel["my_ads_url"].rstrip("/") in page.url

    async def _visit_my_ads(self, page: Page) -> bool:
        """Отваря "моите обяви" и връща дали сме допуснати."""
        await page.goto(self.sel["my_ads_url"], wait_until="domcontentloaded")
        await self._handle_cookies(page)
        await page.wait_for_timeout(800)
        return await self.is_logged_in(page)

    async def ensure_logged_in(self, page: Page) -> None:
        if await self._visit_my_ads(page):
            log.info("Bazar.bg: вече сме логнати")
            return

        if not self.cfg.bazar.password_login:
            raise AuthWallError(SITE, MANUAL_LOGIN_HINT)

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

        # Honeypot-ът (contact_website, скрит на left:-9999px) остава празен —
        # умишлено не го пипаме.
        submit = await require_locator(page, self.sel["login_submit"], "бутон за вход")

        # Входът минава през API, не през самата форма: кликът задейства JS,
        # който вика authentication/login. Само неговият отговор казва дали
        # входът е приет — страницата остава същата и в двата случая.
        api_pattern = self.sel.get("login_api_pattern", "authentication/login")
        responses: list[str] = []

        try:
            async with page.expect_response(
                lambda r: api_pattern in r.url and r.request.method == "POST",
                timeout=25_000,
            ) as info:
                await submit.click()
            api = await info.value
            payload = await _api_payload(api)
            responses.append(f"HTTP {api.status}: {_describe(payload)}")

            # 2FA не се заобикаля. Спираме веднага и казваме какво да се
            # направи — иначе следващият цикъл поръчва още един код.
            if payload.get("requires_2fa"):
                raise AuthWallError(
                    SITE,
                    "профилът иска двуфакторна аутентикация, затова вход с "
                    "парола не може да мине. " + MANUAL_LOGIN_HINT,
                )
        except AuthWallError:
            raise
        except PlaywrightTimeout:
            responses.append(
                f"кликът не задейства заявка към {api_pattern} за 25 секунди"
            )
        except Exception as exc:
            responses.append(f"отговорът от входа не се прочете: {exc}")

        # JS-ът пренасочва след успешен вход; даваме му време.
        await page.wait_for_timeout(3000)

        if not await self._visit_my_ads(page):
            detail = await self._login_failure_detail(page, responses)
            raise AuthWallError(SITE, detail)
        log.info("Bazar.bg: входът мина")

    async def _login_failure_detail(
        self, page: Page, responses: list[str] | None = None
    ) -> str:
        """Събира каквото сайтът казва, за да не гадаем защо входът пада."""
        shot = self.cfg.data_dir / "login_fail_bazar.png"
        try:
            await page.screenshot(path=str(shot), full_page=True)
        except Exception:
            pass

        message = await self._read_form_error(page) or await self._form_text(page)

        parts = [f"входът не мина, останахме на {page.url}"]
        if responses:
            parts.append("отговор на входа: " + "; ".join(responses))
        if message:
            parts.append(f"съобщение от сайта: {message}")
        else:
            # Няма съобщение на страницата — най-честите причини по ред на
            # вероятност, за да не се гадае.
            parts.append(
                "страницата не казва защо. Провери: 1) паролата в .env; "
                "2) дали профилът не е само през Google (тогава директна "
                "парола няма — направи си такава от Bazar.bg); "
                "3) дали не се иска потвърждение по имейл"
            )
        parts.append(f"снимка на екрана: {shot}")
        return " | ".join(parts)

    async def _form_text(self, page: Page) -> str:
        """Текстът на формата — вътре попада и съобщение без познат клас."""
        try:
            form = page.locator(self.sel["login_form"]).first
            if await form.count() == 0:
                return ""
            text = (await form.inner_text()).strip()
            return " ".join(text.split())[:200]
        except Exception:
            return ""

    # ------------------------------------------------------------ публикуване

    async def publish(
        self, listing: Listing, images: list[Path], page: Page, dry_run: bool = False
    ) -> tuple[str, str]:
        """Връща (bazar_id, bazar_url). При dry_run попълва формата и спира."""
        self._check_content(listing)

        await page.goto(self.sel["publish_url"], wait_until="domcontentloaded")
        await self._handle_cookies(page)
        await page.wait_for_timeout(2000)

        if not await self.is_logged_in(page):
            raise AuthWallError(SITE, "изхвърлени сме от сесията на формата за обява")

        title = await require_locator(page, self.sel["form_title"], "заглавие")
        await type_like_human(title, listing.title)
        # Заглавието задейства AI подсказка за рубрика; изчакваме я, за да не
        # презапише нашата категория след това.
        await page.wait_for_timeout(3000)

        await self._fill_category(page, listing.category_id)
        await self._fill_form_fields(page, listing.category_key)
        await self._fill_description(page, listing.description)
        await self._fill_price(page, listing.price)
        await self._fill_location(page, self.cfg.listing.location)

        if self.cfg.listing.phone:
            phone = await first_locator(page, self.sel.get("form_phone"), 2000)
            if phone is not None:
                await type_like_human(phone, self.cfg.listing.phone)

        if images:
            await self._upload_images(page, images)

        if dry_run:
            shot = self.cfg.data_dir / "dry_run_form.png"
            await page.screenshot(path=str(shot), full_page=True)
            log.info("DRY RUN: формата е попълнена, не се изпраща. Снимка: %s", shot)
            return ("", "")

        submit = await require_locator(page, self.sel["form_submit"], "бутон за публикуване")
        await submit.click()
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(5000)

        ad_id, ad_url = await self._detect_published(page)
        if not ad_id:
            error_text = await self._read_form_error(page)
            empty = await self._unfilled_fields(page)
            shot = self.cfg.data_dir / f"publish_fail_{listing.product_id}.png"
            await page.screenshot(path=str(shot), full_page=True)
            parts = [f"обявата не се публикува ({error_text or 'няма ясна грешка'})"]
            if empty:
                parts.append(f"незапълнени полета: {empty}")
            parts.append(f"снимка: {shot}")
            raise PublishError("; ".join(parts))

        log.info("Bazar.bg: публикувана обява %s -> %s", ad_id, ad_url)
        return ad_id, ad_url

    def _check_content(self, listing: Listing) -> None:
        """Правилата на Bazar.bg, проверени преди изобщо да отворим формата.

        По-добре е обявата да отпадне тук, отколкото да изяде опит за
        публикуване и да остане "failed" заради нещо напълно предвидимо.
        Праговете са от съобщенията в самата форма.
        """
        min_title = int(self.sel.get("min_title_len", 15))
        min_descr = int(self.sel.get("min_description_len", 40))

        if len(listing.title) < min_title:
            raise PublishError(
                f"заглавието е {len(listing.title)} знака, сайтът иска поне {min_title}"
            )
        if len(listing.description) < min_descr:
            raise PublishError(
                f"описанието е {len(listing.description)} знака, "
                f"сайтът иска поне {min_descr}"
            )
        if not CYRILLIC.search(listing.description):
            raise PublishError("Bazar.bg изисква кирилица в описанието")
        if not listing.category_id:
            raise PublishError("няма Bazar.bg рубрика (виж listing.category_map)")

    async def _fill_category(self, page: Page, category_id: int) -> None:
        """Задава рубриката като число.

        Видимият <select id=rubChooser> се пълни от JS и стои празен, докато
        не се мине през джаджата — но формата праща скритото поле category_id.
        Затова се пише то, със същите числа, които сайтът си използва (взети
        от window.categoriesTree на самата форма).
        """
        ok = await page.evaluate(
            r"""
            ([sel, id]) => {
              const hidden = document.querySelector(sel.form_category_id);
              if (!hidden) return false;
              hidden.value = String(id);
              hidden.dispatchEvent(new Event('change', { bubbles: true }));

              // Ако списъкът вече е попълнен, вдигаме и него — иначе формата
              // изглежда наполовина празна и скрипт може да я презапише.
              const select = document.querySelector(sel.form_category_select);
              if (select && select.options.length) {
                select.value = String(id);
                select.dispatchEvent(new Event('change', { bubbles: true }));
              }
              return hidden.value === String(id);
            }
            """,
            [self.sel, category_id],
        )
        if not ok:
            raise PublishError(f"не мога да задам рубрика {category_id}")
        await self.pacer.micro_pause()

    async def _fill_form_fields(self, page: Page, category_key: str) -> None:
        """Попълва полетата, които се появяват след избор на рубрика.

        Bazar.bg добавя задължителни полета според рубриката: "Изберете вид"
        (Мъжки/Дамски), "Състояние", "Доставка за сметка на". Стойностите се
        задават в конфига, а не се избират автоматично — грешен избор при
        доставката значи ти да плащаш куриера, а грешен "вид" вкарва обявата
        в чужда ниша.

        Полето "Вид" зависи от категорията в BestSecret, не от рубриката:
        мъжките и дамските очила са в една и съща рубрика 339.
        """
        wanted = dict(self.cfg.listing.form_defaults)
        wanted.update(self.cfg.listing.category_attributes.get(category_key, {}))
        if not wanted:
            return

        result = await page.evaluate(
            r"""
            ([form, wanted]) => {
              HELPERS
              const matchKey = labels => {
                for (const key of Object.keys(wanted)) {
                  const needle = key.toLowerCase();
                  if (labels.some(l => l && l.toLowerCase().includes(needle))) return key;
                }
                return null;
              };

              const done = [];
              const missed = [];

              for (const el of document.querySelectorAll(form + ' select')) {
                if (el.offsetParent === null) continue;
                const key = matchKey([el.name, el.id, labelFor(el)]);
                if (!key) continue;
                const target = wanted[key].toLowerCase();
                let hit = null;
                for (const option of el.options) {
                  if (option.text.trim().toLowerCase() === target) { hit = option; break; }
                }
                if (hit) {
                  el.value = hit.value;
                  el.dispatchEvent(new Event('change', { bubbles: true }));
                  done.push(key + ' = ' + hit.text.trim());
                } else {
                  missed.push(key + ' -> няма опция "' + wanted[key] + '"; има: ' +
                    [...el.options].map(o => o.text.trim()).filter(Boolean).join(' / '));
                }
              }

              const groups = {};
              for (const el of document.querySelectorAll(
                     form + ' input[type=radio], ' + form + ' input[type=checkbox]')) {
                if (el.offsetParent === null) continue;
                (groups[el.name] = groups[el.name] || []).push(el);
              }
              for (const [name, items] of Object.entries(groups)) {
                const key = matchKey([name, labelFor(items[0])]);
                if (!key) continue;
                const target = wanted[key].toLowerCase();
                let hit = null;
                for (const el of items) {
                  if (choiceText(el).toLowerCase().startsWith(target)) { hit = el; break; }
                }
                if (hit) {
                  hit.checked = true;
                  hit.dispatchEvent(new Event('change', { bubbles: true }));
                  hit.dispatchEvent(new Event('click', { bubbles: true }));
                  done.push(key + ' = ' + choiceText(hit));
                } else {
                  missed.push(key + ' -> няма избор "' + wanted[key] + '"; има: ' +
                    items.map(choiceText).filter(Boolean).join(' / '));
                }
              }
              return { done, missed };
            }
            """.replace("HELPERS", JS_FIELD_HELPERS),
            [self.sel["form"], wanted],
        )

        for entry in result["done"]:
            log.info("поле от рубриката: %s", entry)
        if result["missed"]:
            raise PublishError(
                "стойност от конфига не съвпада с формата: "
                + "; ".join(result["missed"])
            )
        await self.pacer.micro_pause()

    async def _unfilled_fields(self, page: Page) -> str:
        """Кои задължителни полета са останали празни — за смислен доклад при провал."""
        try:
            empty = await page.evaluate(
                r"""
                (form) => {
                  HELPERS
                  const out = [];
                  for (const el of document.querySelectorAll(form + ' select')) {
                    if (el.offsetParent === null) continue;
                    if (el.value && el.value !== '0') continue;
                    out.push((labelFor(el) || el.name || el.id) + ' -> възможни: ' +
                      [...el.options].filter(o => o.value && o.value !== '0')
                        .map(o => o.text.trim()).slice(0, 8).join(' / '));
                  }
                  const groups = {};
                  for (const el of document.querySelectorAll(form + ' input[type=radio]')) {
                    if (el.offsetParent === null) continue;
                    (groups[el.name] = groups[el.name] || []).push(el);
                  }
                  for (const [name, items] of Object.entries(groups)) {
                    if (items.some(el => el.checked)) continue;
                    out.push((labelFor(items[0]) || name) + ' -> възможни: ' +
                      items.map(choiceText).filter(Boolean).join(' / '));
                  }
                  return out;
                }
                """.replace("HELPERS", JS_FIELD_HELPERS),
                self.sel["form"],
            )
        except Exception:
            return ""
        return "; ".join(empty)

    async def _fill_description(self, page: Page, text: str) -> None:
        """Пише в Redactor редактора вътре в iframe-а.

        Видимото поле е contenteditable body в #redactorIframe; скритият
        textarea#descr се пълни от редактора при изпращане. Пишем и в двете:
        редактора, за да е коректно състоянието му, и textarea-та като
        подсигуровка, ако скриптът не се задейства.
        """
        frame = page.frame_locator(self.sel["form_description_frame"])
        body = frame.locator(self.sel["form_description_body"])
        await body.click()
        await body.fill(text)
        await self.pacer.micro_pause()

        await page.evaluate(
            r"""
            ([sel, text]) => {
              const ta = document.querySelector(sel.form_description_textarea);
              if (ta) {
                ta.value = text;
                ta.dispatchEvent(new Event('change', { bubbles: true }));
              }
            }
            """,
            [self.sel, text],
        )

    async def _fill_price(self, page: Page, price: float) -> None:
        """Избира "фиксирана цена" и вписва сумата.

        Без радиото формата остава на "По договаряне" и цената се игнорира.
        """
        radio = await first_locator(page, self.sel.get("form_price_fixed_radio"), 2000)
        if radio is not None:
            await radio.check()
            await self.pacer.micro_pause()

        field = await require_locator(page, self.sel["form_price"], "цена")
        await type_like_human(field, f"{price:.2f}")
        await self.pacer.micro_pause()

    async def _fill_location(self, page: Page, city: str) -> None:
        """Градът е <select> с опции от вида "гр. София"."""
        if not city:
            return
        loc = await first_locator(page, self.sel.get("form_location_city"), 3000)
        if loc is None:
            log.warning("няма поле за град във формата")
            return

        for label in (f"гр. {city}", city):
            try:
                await loc.select_option(label=label)
                await self.pacer.micro_pause()
                return
            except Exception:
                continue
        log.warning("не намирам '%s' в списъка с градове", city)

    async def _upload_images(self, page: Page, images: list[Path]) -> None:
        file_input = await first_locator(page, self.sel.get("form_images_input"), 4000)
        if file_input is None:
            log.warning("няма поле за качване на снимки — обявата ще е без снимки")
            return
        paths = [str(p) for p in images[: self.cfg.listing.max_images]]
        await file_input.set_input_files(paths)
        # Качването е асинхронно; изпращане преди да е готово реже снимките.
        await page.wait_for_timeout(3000 + 1500 * len(paths))
        log.info("качени %d снимки", len(paths))

    async def _detect_published(self, page: Page) -> tuple[str, str]:
        pattern = self.sel.get("published_url_pattern", r"bazar\.bg/obiava-(\d+)")

        match = re.search(pattern, page.url)
        if match:
            return match.group(1), page.url

        # Понякога след запис се показва междинна страница с линк към обявата.
        href = await page.evaluate(
            r"""
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
                if await loc.count() > 0 and await loc.is_visible():
                    text = (await loc.inner_text()).strip()
                    if text:
                        return " ".join(text.split())[:300]
            except Exception:
                continue
        return ""

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


async def _api_payload(response) -> dict:
    """JSON-ът от API-то за вход, или празно при нечетим отговор."""
    try:
        payload = await response.json()
    except Exception:
        try:
            return {"message": " ".join((await response.text())[:300].split())}
        except Exception:
            return {}
    return payload if isinstance(payload, dict) else {"message": str(payload)[:300]}


def _describe(payload: dict) -> str:
    """Човешкото съобщение от API-то, плюс вдигнатите флагове.

    Отговорът идва с екранирана кирилица; без декодиране причината е
    нечетима точно когато най-много трябва да се чете.
    """
    if not payload:
        return "(отговорът не се прочете)"
    message = payload.get("message") or payload.get("error") or ""
    flags = [k for k, v in payload.items() if v is True and k != "message"]
    if message:
        return message + (f" [{', '.join(flags)}]" if flags else "")
    return " ".join(str(payload)[:300].split())
