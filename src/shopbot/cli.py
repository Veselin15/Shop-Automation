"""Команден интерфейс."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .browser import (
    BrowserMissing,
    BrowserSession,
    any_present,
    require_locator,
    type_like_human,
)
from .config import load_config
from .db import Database
from .humanize import Pacer
from .listing import build_listing
from .notify import (
    Notifier,
    TelegramRejected,
    TelegramUnreachable,
    check_token,
    discover_chats,
)
from .orchestrator import Orchestrator
from .pricing import compute_price, format_money
from .selection import evaluate
from .sinks.bazar import JS_FIELD_HELPERS, BazarSink
from .sources.bestsecret import BestSecretSource, looks_logged_in

app = typer.Typer(add_completion=False, help="BestSecret -> Bazar.bg автоматизация")
console = Console()


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _run_async(coro):
    """Пуска корутина и превръща предвидимите сривове в четимо съобщение."""
    try:
        return asyncio.run(coro)
    except BrowserMissing as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None


async def _session_valid(site: str, page, cfg) -> bool:
    """Логнати ли сме. За BestSecret решава URL-ът, за Bazar.bg — DOM маркер."""
    if site == "bestsecret":
        return await looks_logged_in(page, cfg.selectors["bestsecret"])
    return await any_present(page, cfg.selectors["bazar"].get("logged_in_markers"))


def _ctx():
    cfg = load_config()
    db = Database(cfg.db_path)
    notifier = Notifier(cfg.secrets.telegram_bot_token, cfg.secrets.telegram_chat_id)
    return cfg, db, notifier


# --------------------------------------------------------------------- login


@app.command()
def login(
    site: str = typer.Argument(..., help="bestsecret | bazar"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Ръчен вход с видим браузър. Сесията остава запазена в data/profiles/."""
    setup_logging(verbose)
    cfg, db, _ = _ctx()

    if site not in ("bestsecret", "bazar"):
        raise typer.BadParameter("site трябва да е bestsecret или bazar")

    async def _run() -> None:
        session = BrowserSession(site, cfg.profiles_dir / site, headless=False)
        await session.start()
        try:
            page = await session.new_page()
            start_url = (
                cfg.source.base_url
                if site == "bestsecret"
                else cfg.selectors["bazar"]["login_url"]
            )
            await page.goto(start_url, wait_until="domcontentloaded")

            console.print(
                f"\n[bold cyan]Влез ръчно в {site} в отворения браузър.[/bold cyan]\n"
                "Профилът се записва автоматично — следващите пускания няма да питат.\n"
            )
            typer.prompt("Натисни Enter, когато си вътре", default="", show_default=False)

            # Изнасяме ВЕДНАГА, още докато браузърът е отворен. Входът в
            # BestSecret е session cookie — живее само в паметта и се губи
            # при затваряне, така че по-късен export-session хваща само
            # трайните бисквитки и сесията изглежда празна.
            state_file = await session.export_state(cfg.session_file(site))

            n_cookies = len(json.loads(state_file.read_text(encoding="utf-8"))["cookies"])
            if await _session_valid(site, page, cfg):
                console.print(
                    f"[green]Сесията е записана[/green] ({n_cookies} бисквитки)."
                )
                db.log_event("login", f"{site}: ръчен вход, {n_cookies} бисквитки")
            else:
                console.print(
                    "[yellow]Изглежда още не си влязъл.[/yellow] Сесията е запазена, "
                    "но сайтът още те смята за гост. Пусни командата пак и изчакай "
                    "входа да завърши, преди да натиснеш Enter."
                )
            console.print(f"Изнесена сесия: {state_file}")
        finally:
            await session.stop()

    _run_async(_run())


@app.command("export-session")
def export_session(
    site: str = typer.Argument(..., help="bestsecret | bazar"),
    out: str = typer.Option("", "--out", help="къде да запише JSON-а"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Изнася влязлата сесия като преносим JSON.

    Профил на Chromium не се копира между Windows и Linux — бисквитките са
    криптирани с ключ на операционната система. Този JSON се пренася.
    """
    setup_logging(verbose)
    cfg, _, _ = _ctx()
    dest = Path(out) if out else cfg.session_file(site)

    async def _run() -> None:
        session = BrowserSession(
            site, cfg.profiles_dir / site, headless=True,
            seed_state=cfg.session_file(site),
        )
        await session.start()
        try:
            path = await session.export_state(dest)
            console.print(f"[green]Записах сесията в[/green] {path}")
            console.print(
                "Прехвърли я на сървъра и я внеси там:\n"
                f"  scp {path} veski4a@192.168.0.101:~/Shop-Automation/\n"
                f"  ./.venv/bin/shopbot import-session {site} --file ~/Shop-Automation/{dest.name}"
            )
        finally:
            await session.stop()

    _run_async(_run())


@app.command("import-session")
def import_session(
    site: str = typer.Argument(..., help="bestsecret | bazar"),
    file: str = typer.Option(..., "--file", help="JSON от export-session"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Внася изнесена сесия в профила на тази машина и проверява дали е валидна."""
    setup_logging(verbose)
    cfg, db, _ = _ctx()
    source = Path(file)
    if not source.exists():
        raise typer.BadParameter(f"няма такъв файл: {source}")

    async def _run() -> None:
        session = BrowserSession(
            site, cfg.profiles_dir / site, headless=True,
            seed_state=cfg.session_file(site),
        )
        await session.start()
        try:
            cookies, origins = await session.import_state(source)
            console.print(f"Внесох {cookies} бисквитки и {origins} origin-а.")

            page = await session.new_page()
            check_url = (
                cfg.source.base_url + "/home.htm"
                if site == "bestsecret"
                else cfg.selectors["bazar"]["my_ads_url"]
            )
            await page.goto(check_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            if await _session_valid(site, page, cfg):
                console.print("[green]Сесията работи — профилът е логнат.[/green]")
                db.log_event("login", f"{site}: внесена сесия")
            else:
                console.print(
                    "[yellow]Не разпознавам логнат профил.[/yellow] Или сесията е "
                    "изтекла, или logged_in_markers в selectors.yaml трябва да се "
                    "обнови. Виж data/import_check.png"
                )
                await page.screenshot(
                    path=str(cfg.data_dir / "import_check.png"), full_page=True
                )
        finally:
            await session.stop()

    _run_async(_run())


@app.command("test-notify")
def test_notify(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Проверява Telegram настройките и праща пробно съобщение.

    Без TELEGRAM_CHAT_ID показва кои разговори вижда ботът, за да си избереш id-то.
    """
    setup_logging(verbose)
    cfg, _, notifier = _ctx()
    token = cfg.secrets.telegram_bot_token
    chat_id = cfg.secrets.telegram_chat_id

    if not token:
        console.print(
            "[red]Липсва TELEGRAM_BOT_TOKEN в .env[/red]\n"
            "Направи бот при @BotFather в Telegram с командата /newbot."
        )
        raise typer.Exit(1)

    async def _run() -> None:
        try:
            username = await check_token(token)
        except TelegramUnreachable as exc:
            console.print(
                f"[red]Не стигам до Telegram:[/red] {exc}\n"
                "Това е мрежов проблем, не проблем с токена — провери интернета, "
                "DNS-а или сертификатите на машината."
            )
            raise typer.Exit(1) from exc
        except TelegramRejected as exc:
            console.print(
                f"[red]Telegram отказа токена:[/red] {exc}\n"
                "Копирай го пак от @BotFather — целия ред, включително числата "
                "пред двоеточието."
            )
            raise typer.Exit(1) from exc
        console.print(f"[green]Токенът работи.[/green] Ботът е @{username}")

        if not chat_id:
            console.print("\n[yellow]Липсва TELEGRAM_CHAT_ID.[/yellow] Търся разговори…")
            chats = await discover_chats(token)
            if not chats:
                console.print(
                    f"Няма нито един. Отвори https://t.me/{username} в Telegram, "
                    "натисни Start (или пиши каквото и да е) и пусни командата пак."
                )
                raise typer.Exit(1)

            table = Table(title="намерени разговори")
            table.add_column("TELEGRAM_CHAT_ID")
            table.add_column("кой е")
            for cid, name in chats:
                table.add_row(cid, name)
            console.print(table)
            console.print("Сложи желаното id в .env и пусни командата пак.")
            raise typer.Exit(1)

        await notifier.send(
            "✅ <b>shopbot</b> е свързан.\n"
            "Оттук ще получаваш публикувани обяви, свалени обяви и грешки."
        )
        console.print(
            f"[green]Пратих пробно съобщение до {chat_id}.[/green] "
            "Провери Telegram — ако не е пристигнало, id-то е грешно."
        )

    _run_async(_run())


# ----------------------------------------------------------------------- run


@app.command()
def once(
    dry_run: bool = typer.Option(False, "--dry-run", help="попълва, но не изпраща нищо"),
    no_discover: bool = typer.Option(False, "--no-discover"),
    no_publish: bool = typer.Option(False, "--no-publish"),
    no_reconcile: bool = typer.Option(False, "--no-reconcile"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Един пълен цикъл и изход."""
    setup_logging(verbose)
    cfg, db, notifier = _ctx()
    orch = Orchestrator(cfg, db, notifier)

    report = _run_async(
        orch.run_once(
            discover=not no_discover,
            publish=not no_publish,
            reconcile=not no_reconcile,
            dry_run=dry_run,
        )
    )
    console.print(f"\n[bold]{report.summary()}[/bold]")
    for err in report.errors[:10]:
        console.print(f"  [red]•[/red] {err}")


@app.command()
def run(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Непрекъснат режим — това пуска systemd на сървъра."""
    setup_logging(verbose)
    cfg, db, notifier = _ctx()
    orch = Orchestrator(cfg, db, notifier)
    console.print("[bold green]shopbot тръгна.[/bold green] Ctrl+C за спиране.")
    try:
        _run_async(orch.run_forever())
    except KeyboardInterrupt:
        console.print("\nспрян.")


# -------------------------------------------------------------------- status


@app.command()
def status() -> None:
    """Какво има в базата в момента."""
    _, db, _ = _ctx()

    counts = dict(
        db.conn.execute("SELECT state, COUNT(*) FROM listings GROUP BY state").fetchall()
    )
    products = db.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    table = Table(title="shopbot")
    table.add_column("показател")
    table.add_column("стойност", justify="right")
    table.add_row("проследявани продукти", str(products))
    for state in ("candidate", "published", "failed", "removed"):
        table.add_row(f"обяви: {state}", str(counts.get(state, 0)))
    table.add_row("публикувани днес", str(db.counter("publish")))
    table.add_row("свалени днес", str(db.counter("remove")))
    console.print(table)

    events = Table(title="последни събития")
    events.add_column("кога")
    events.add_column("какво")
    events.add_column("детайли", overflow="fold")
    for row in db.recent_events(15):
        events.add_row(row["ts"][11:19], row["kind"], (row["message"] or "")[:80])
    console.print(events)


@app.command()
def candidates(limit: int = typer.Option(30, "--limit")) -> None:
    """Какво чака да бъде публикувано, с марж — прегледай преди да пуснеш бота.

    Пълни се от `shopbot once --no-publish`, което обхожда BestSecret, но не
    пипа Bazar.bg.
    """
    cfg, db, _ = _ctx()
    rows = db.pending_candidates(limit)
    if not rows:
        console.print(
            "Опашката е празна. Напълни я с [bold]shopbot once --no-publish[/bold]."
        )
        return

    table = Table(title=f"кандидати ({len(rows)})")
    table.add_column("марка")
    table.add_column("заглавие", overflow="fold")
    table.add_column("нам.", justify="right")
    table.add_column("цена", justify="right")
    table.add_column("марж", justify="right")
    table.add_column("скор", justify="right")

    for row in rows:
        product = db.get_product(row["product_id"])
        if product is None:
            continue
        price = compute_price(product, cfg.pricing)
        table.add_row(
            product.brand[:18],
            row["title"][:44],
            f"{product.discount_pct}%",
            format_money(price.final, price.currency),
            format_money(price.margin, price.currency),
            f"{row['score']:.2f}",
        )
    console.print(table)


@app.command()
def listings() -> None:
    """Активните обяви и връзката им с източника."""
    _, db, _ = _ctx()
    table = Table(title="активни обяви")
    table.add_column("bazar id")
    table.add_column("заглавие", overflow="fold")
    table.add_column("цена", justify="right")
    table.add_column("продукт")
    for row in db.active_listings():
        table.add_row(
            row["bazar_id"] or "-",
            row["title"][:50],
            f"{row['price']:.2f} {row['currency']}",
            row["product_id"],
        )
    console.print(table)


# ----------------------------------------------------------------- calibrate


@app.command()
def calibrate(
    site: str = typer.Argument(..., help="bestsecret | bazar"),
    url: str = typer.Option("", "--url", help="конкретен адрес за оглед"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Изкарва структурата на страницата, за да оправиш selectors.yaml.

    Записва JSON с формите, полетата и повтарящите се блокове плюс екранна
    снимка в data/. Пусни го, когато нещо спре да се намира.
    """
    setup_logging(verbose)
    cfg, _, _ = _ctx()

    target = url or (
        cfg.source.base_url + "/home.htm"
        if site == "bestsecret"
        else cfg.selectors["bazar"]["publish_url"]
    )

    async def _run() -> None:
        session = BrowserSession(
            site, cfg.profiles_dir / site, headless=cfg.runtime.headless,
            seed_state=cfg.session_file(site),
        )
        await session.start()
        try:
            page = await session.new_page()

            # Без вход /ads/save препраща към /user/login и снимаме грешната
            # форма — точно това правеше командата досега.
            if site == "bazar":
                sink = BazarSink(cfg, session, Pacer(cfg.runtime))
                await sink.ensure_logged_in(page)

            await page.goto(target, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)

            if site == "bazar" and "user/login" in page.url:
                console.print(
                    "[red]Пренасочени сме към страницата за вход.[/red] "
                    "Провери BAZAR_EMAIL и BAZAR_PASSWORD в .env — снимката "
                    "нямаше да е на формата за обява."
                )
                raise typer.Exit(1)

            dump = await page.evaluate(
                r"""
                () => {
                  const forms = Array.from(document.querySelectorAll('form')).map(f => ({
                    action: f.action, id: f.id, cls: f.className,
                    fields: Array.from(f.querySelectorAll('input,select,textarea,button'))
                      .map(i => ({
                        tag: i.tagName, type: i.type || '', name: i.name || '',
                        id: i.id || '', cls: (i.className || '').slice(0, 80),
                        placeholder: i.placeholder || '',
                        hidden: i.type === 'hidden' || i.offsetParent === null,
                      })),
                  }));
                  const counts = {};
                  for (const el of document.querySelectorAll('[data-testid],[class]')) {
                    const key = el.getAttribute('data-testid')
                      ? `[data-testid="${el.getAttribute('data-testid')}"]`
                      : '.' + String(el.className).trim().split(/\\s+/)[0];
                    if (!key || key === '.') continue;
                    counts[key] = (counts[key] || 0) + 1;
                  }
                  const repeated = Object.entries(counts)
                    .filter(([, n]) => n >= 4).sort((a, b) => b[1] - a[1]).slice(0, 40);
                  return {
                    url: location.href, title: document.title, forms, repeated,
                    fileInputs: document.querySelectorAll('input[type=file]').length,
                  };
                }
                """
            )

            out = cfg.data_dir / f"calibrate_{site}.json"
            out.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
            shot = cfg.data_dir / f"calibrate_{site}.png"
            await page.screenshot(path=str(shot), full_page=True)

            console.print(f"[green]Записах[/green] {out}")
            console.print(f"[green]Записах[/green] {shot}")
            console.print(f"\nСтраница: [bold]{dump['title']}[/bold] ({dump['url']})")
            console.print(f"Форми: {len(dump['forms'])}, file inputs: {dump['fileInputs']}")
            for key, n in dump["repeated"][:12]:
                console.print(f"  повтарящ се блок  {key}  x{n}")
        finally:
            await session.stop()

    _run_async(_run())


# --------------------------------------------------------------------- debug


@app.command()
def inspect(
    url: str = typer.Argument(..., help="адрес на продукт в BestSecret"),
    category: str = typer.Option("sunglasses", "--category", help="ключ от source.categories"),
    verbose: bool = typer.Option(True, "--verbose", "-v"),
) -> None:
    """Прочита един продукт и показва решението: минава ли, на каква цена."""
    setup_logging(verbose)
    cfg, _, _ = _ctx()

    async def _run() -> None:
        session = BrowserSession(
            "bestsecret", cfg.profiles_dir / "bestsecret", cfg.runtime.headless,
            seed_state=cfg.session_file("bestsecret"),
        )
        await session.start()
        try:
            source = BestSecretSource(cfg, session)
            page = await session.new_page()
            await source.ensure_session(page)
            product = await source.fetch_product(url, category, page)
            if product is None:
                console.print("[red]Продуктът не се чете (изтрит или сменени селектори).[/red]")
                return

            verdict = evaluate(product, cfg.selection)
            price = compute_price(product, cfg.pricing)

            table = Table(title=f"{product.brand} {product.name}"[:70])
            table.add_column("поле")
            table.add_column("стойност", overflow="fold")
            table.add_row("id", product.id)
            table.add_row("цена в източника", f"{product.price:.2f} {product.currency}")
            table.add_row("каталожна", f"{product.orig_price:.2f} {product.currency}")
            table.add_row("намаление", f"{product.discount_pct}%")
            table.add_row("налични размери", ", ".join(product.available_sizes) or "-")
            table.add_row("снимки", str(len(product.images)))
            table.add_row("статус", str(product.status))
            table.add_row("скор", f"{verdict.score:.2f}")
            table.add_row(
                "решение",
                "[green]минава[/green]" if verdict.accepted else f"[red]отпада[/red] ({verdict.reason})",
            )
            table.add_row("себестойност", format_money(price.cost, price.currency))
            table.add_row("цена в обявата", format_money(price.final, price.currency))
            table.add_row("марж", format_money(price.margin, price.currency))
            if price.rejected:
                table.add_row("цена отхвърлена", price.rejected)
            console.print(table)

            category_id = cfg.listing.category_map.get(category, 0)
            listing = build_listing(product, price, cfg.listing, category_id)
            console.print("\n[bold]Заглавие:[/bold] " + listing.title)
            console.print("[bold]Описание:[/bold]\n" + listing.description)
        finally:
            await session.stop()

    _run_async(_run())


@app.command("inspect-form")
def inspect_form(
    category: int = typer.Option(339, "--category", help="числово id на рубриката"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Показва всички полета на формата за обява СЛЕД избор на рубрика.

    Част от полетата на Bazar.bg се появяват чак когато рубриката е избрана
    (например "Вид"). Командата ги изкарва с етикетите и опциите им, за да
    се допълни listing.category_attributes.
    """
    setup_logging(verbose)
    cfg, _, _ = _ctx()
    sel = cfg.selectors["bazar"]

    async def _run() -> None:
        session = BrowserSession(
            "bazar", cfg.profiles_dir / "bazar", cfg.runtime.headless,
            seed_state=cfg.session_file("bazar"),
        )
        await session.start()
        try:
            sink = BazarSink(cfg, session, Pacer(cfg.runtime))
            page = await session.new_page()
            await sink.ensure_logged_in(page)

            await page.goto(sel["publish_url"], wait_until="domcontentloaded")
            await sink._handle_cookies(page)
            await page.wait_for_timeout(2500)

            title = await require_locator(page, sel["form_title"], "заглавие")
            await type_like_human(title, "Слънчеви очила Carrera проба на формата")
            await page.wait_for_timeout(3000)

            await sink._fill_category(page, category)
            await page.wait_for_timeout(4000)

            data = await page.evaluate(
                r"""
                (form) => {
                  HELPERS
                  const out = [];
                  for (const el of document.querySelectorAll(
                         form + ' select, ' + form + ' input, ' + form + ' textarea')) {
                    if (el.type === 'hidden') continue;
                    out.push({
                      tag: el.tagName,
                      type: el.type || '',
                      name: el.name || '',
                      id: el.id || '',
                      label: labelFor(el),
                      choice: (el.type === 'radio' || el.type === 'checkbox')
                        ? choiceText(el) : '',
                      visible: el.offsetParent !== null,
                      value: (el.value || '').slice(0, 30),
                      options: el.tagName === 'SELECT'
                        ? [...el.options].map(o => o.value + ' = ' + o.text.trim().slice(0, 30))
                        : null,
                    });
                  }
                  return out;
                }
                """.replace("HELPERS", JS_FIELD_HELPERS),
                cfg.selectors["bazar"]["form"],
            )

            shot = cfg.data_dir / f"form_category_{category}.png"
            await page.screenshot(path=str(shot), full_page=True)

            table = Table(title=f"видими полета след рубрика {category}")
            table.add_column("етикет")
            table.add_column("поле")
            table.add_column("стойност")
            for f in data:
                if not f["visible"]:
                    continue
                name = f["name"] or f["id"] or f["tag"]
                table.add_row(
                    f["label"][:28],
                    f"{f['tag'].lower()} {name}"[:30],
                    (f["choice"] or f["value"])[:26],
                )
            console.print(table)

            console.print("\n[bold]Радио бутони и точният им текст:[/bold]")
            seen_groups: set[str] = set()
            for f in data:
                if f["type"] not in ("radio", "checkbox") or not f["visible"]:
                    continue
                key = f["name"] or f["id"]
                if key not in seen_groups:
                    seen_groups.add(key)
                    console.print(f"  [cyan]{key}[/cyan]  ({f['label'][:30]})")
                console.print(f"      {f['value']} = {f['choice'][:45]}")

            console.print("\n[bold]Падащи менюта и опциите им:[/bold]")
            for f in data:
                if f["options"] and f["visible"]:
                    name = f["name"] or f["id"]
                    console.print(f"  [cyan]{name}[/cyan]  ({f['label'][:30]})")
                    for option in f["options"][:12]:
                        console.print(f"      {option}")
            console.print(f"\nСнимка на формата: {shot}")
        finally:
            await session.stop()

    _run_async(_run())


@app.command()
def sync(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Сверява базата с реално активните обяви в Bazar.bg.

    Ако си изтрил обява ръчно, това я маркира като свалена, за да не остане
    продуктът блокиран като 'публикуван'.
    """
    setup_logging(verbose)
    cfg, db, _ = _ctx()

    async def _run() -> None:
        session = BrowserSession(
            "bazar", cfg.profiles_dir / "bazar", cfg.runtime.headless,
            seed_state=cfg.session_file("bazar"),
        )
        await session.start()
        try:
            
            sink = BazarSink(cfg, session, Pacer(cfg.runtime))
            page = await session.new_page()
            await sink.ensure_logged_in(page)
            live = set(await sink.list_my_ads(page))
            console.print(f"В Bazar.bg виждам {len(live)} обяви")

            fixed = 0
            for row in db.active_listings():
                if row["bazar_id"] and row["bazar_id"] not in live:
                    db.mark_removed(row["product_id"], "изчезнала от Bazar.bg (ръчно изтрита?)")
                    fixed += 1
            console.print(f"Маркирах {fixed} обяви като свалени.")
        finally:
            await session.stop()

    _run_async(_run())


if __name__ == "__main__":
    app()
