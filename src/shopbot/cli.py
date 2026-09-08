"""Команден интерфейс."""

from __future__ import annotations

import asyncio
import json
import logging

import typer
from rich.console import Console
from rich.table import Table

from .browser import BrowserSession, any_present
from .config import load_config
from .db import Database
from .listing import build_listing
from .notify import Notifier
from .orchestrator import Orchestrator
from .pricing import compute_price, format_money
from .selection import evaluate
from .sinks.bazar import BazarSink
from .sources.bestsecret import BestSecretSource

app = typer.Typer(add_completion=False, help="BestSecret -> Bazar.bg автоматизация")
console = Console()


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


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

            markers = (
                cfg.selectors["bestsecret"].get("logged_in_markers")
                if site == "bestsecret"
                else cfg.selectors["bazar"].get("logged_in_markers")
            )
            ok = await any_present(page, markers)
            if ok:
                console.print("[green]Сесията е записана.[/green]")
                db.log_event("login", f"{site}: ръчен вход")
            else:
                console.print(
                    "[yellow]Не разпознавам маркер за логнат профил.[/yellow] "
                    "Сесията все пак е запазена; ако не тръгне, оправи "
                    "logged_in_markers в config/selectors.yaml."
                )
        finally:
            await session.stop()

    asyncio.run(_run())


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

    report = asyncio.run(
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
        asyncio.run(orch.run_forever())
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
        session = BrowserSession(site, cfg.profiles_dir / site, headless=cfg.runtime.headless)
        await session.start()
        try:
            page = await session.new_page()
            await page.goto(target, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)

            dump = await page.evaluate(
                """
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

    asyncio.run(_run())


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
            "bestsecret", cfg.profiles_dir / "bestsecret", cfg.runtime.headless
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

            path = cfg.listing.category_map.get(category, ["Мода", "Аксесоари"])
            listing = build_listing(product, price, cfg.listing, path)
            console.print("\n[bold]Заглавие:[/bold] " + listing.title)
            console.print("[bold]Описание:[/bold]\n" + listing.description)
        finally:
            await session.stop()

    asyncio.run(_run())


@app.command()
def sync(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Сверява базата с реално активните обяви в Bazar.bg.

    Ако си изтрил обява ръчно, това я маркира като свалена, за да не остане
    продуктът блокиран като 'публикуван'.
    """
    setup_logging(verbose)
    cfg, db, _ = _ctx()

    async def _run() -> None:
        session = BrowserSession("bazar", cfg.profiles_dir / "bazar", cfg.runtime.headless)
        await session.start()
        try:
            from .humanize import Pacer

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

    asyncio.run(_run())


if __name__ == "__main__":
    app()
