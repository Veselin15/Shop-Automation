"""Свалянето е функцията, която пази профила чист — заслужава истински тест."""

import pytest

from shopbot.config import Config, RemovalConfig
from shopbot.db import Database
from shopbot.models import Listing, Product, ProductStatus, Size
from shopbot.notify import Notifier
from shopbot.orchestrator import Orchestrator


@pytest.fixture()
def orch(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.removal = RemovalConfig(
        auto_remove=True, remove_below_discount_pct=45, misses_before_removal=2
    )
    db = Database(tmp_path / "test.db")
    yield Orchestrator(cfg, db, Notifier()), db, cfg
    db.close()


def publish(db, product):
    db.upsert_product(product, score=0.9)
    db.save_candidate(Listing(product_id=product.id, title=f"{product.brand} {product.name}"))
    db.mark_published(product.id, f"ad{product.id}",
                      f"https://bazar.bg/obiava-{product.id}", product.discount_pct)


def make(pid="111", price=50.0, orig=200.0, status=ProductStatus.AVAILABLE):
    return Product(
        id=pid,
        url=f"http://x/{pid}",
        brand="Nike",
        name="Яке",
        price=price,
        orig_price=orig,
        images=["a.jpg"],
        sizes=[Size("M"), Size("L")],
        status=status,
    )


def test_available_and_discounted_stays_up(orch):
    o, db, _ = orch
    publish(db, make())
    assert o._pending_removals() == []


def test_sold_out_is_removed_immediately(orch):
    o, db, _ = orch
    publish(db, make())
    db.mark_checked("111", ProductStatus.SOLD_OUT)
    removals = o._pending_removals()
    assert len(removals) == 1
    assert removals[0][3] == "изчерпан е"


def test_price_back_up_triggers_removal(orch):
    o, db, _ = orch
    publish(db, make(price=50.0, orig=200.0))
    # 200 -> 130 е само 35% намаление, под прага от 45%.
    db.update_prices("111", 130.0, 200.0)
    removals = o._pending_removals()
    assert len(removals) == 1
    assert "намалението падна" in removals[0][3]


def test_gone_product_needs_two_consecutive_misses(orch):
    o, db, _ = orch
    publish(db, make())

    db.mark_checked("111", ProductStatus.GONE, miss=True)
    assert o._pending_removals() == [], "една липса може да е временна грешка"

    db.mark_checked("111", ProductStatus.GONE, miss=True)
    assert len(o._pending_removals()) == 1


def test_a_successful_recheck_resets_the_miss_counter(orch):
    o, db, _ = orch
    publish(db, make())
    db.mark_checked("111", ProductStatus.GONE, miss=True)
    db.mark_checked("111", ProductStatus.AVAILABLE)
    assert db.miss_count("111") == 0
    db.mark_checked("111", ProductStatus.GONE, miss=True)
    assert o._pending_removals() == []


def test_auto_remove_off_disables_everything(orch):
    o, db, cfg = orch
    publish(db, make())
    db.mark_checked("111", ProductStatus.SOLD_OUT)
    cfg.removal.auto_remove = False
    assert o._pending_removals() == []


def test_removed_listing_is_no_longer_active(orch):
    o, db, _ = orch
    publish(db, make())
    assert db.active_listing_count() == 1
    db.mark_removed("111", "изчерпан е")
    assert db.active_listing_count() == 0
    assert o._pending_removals() == []


def test_publishing_a_candidate_does_not_overwrite_a_published_row(orch):
    """Повторно откриване на същия продукт не бива да го връща в опашката."""
    _, db, _ = orch
    product = make()
    publish(db, product)
    db.save_candidate(Listing(product_id=product.id, title="друго заглавие"))
    row = db.get_listing(product.id)
    assert row["state"] == "published"
    assert row["title"] == "Nike Яке"


def test_daily_counters_are_per_kind(orch):
    _, db, _ = orch
    db.bump_counter("publish", 3)
    db.bump_counter("remove", 1)
    assert db.counter("publish") == 3
    assert db.counter("remove") == 1


# --------------------------------------------- офертата още ли е същата


def test_small_shrink_in_the_discount_keeps_the_listing(orch):
    """75% -> 72% не променя сметката и не бива да сваля обявата."""
    o, db, _ = orch
    publish(db, make(price=50.0, orig=200.0))          # 75%
    db.upsert_product(make(price=56.0, orig=200.0))    # 72%
    assert o._pending_removals() == []


def test_a_real_collapse_takes_it_down(orch):
    """75% -> 60% значи, че продаваме друго на цената на старата оферта."""
    o, db, cfg = orch
    publish(db, make(price=50.0, orig=200.0))          # 75%
    db.upsert_product(make(price=80.0, orig=200.0))    # 60%
    pending = o._pending_removals()
    assert len(pending) == 1
    assert "75%" in pending[0][3] and "60%" in pending[0][3]


def test_the_tolerance_is_configurable(orch):
    o, db, cfg = orch
    publish(db, make(price=50.0, orig=200.0))          # 75%
    db.upsert_product(make(price=64.0, orig=200.0))    # 68%, спад 7 пункта
    cfg.removal.max_discount_drop_pct = 10
    assert o._pending_removals() == []
    cfg.removal.max_discount_drop_pct = 5
    assert len(o._pending_removals()) == 1


# --------------------------------------------- известието


def test_the_notification_carries_both_links():
    """Поръчката се прави ръчно в BestSecret — адресът трябва да е в известието."""
    import asyncio

    sent: list[str] = []
    n = Notifier()
    n.send = lambda text: sent.append(text) or asyncio.sleep(0)

    asyncio.run(n.published(
        "Мъжки часовник Guess", "76.90 €", "https://bazar.bg/obiava-1",
        source_url="https://www.bestsecret.com/product.htm?code=1", cost="49.99 €",
    ))
    assert "bazar.bg/obiava-1" in sent[0]
    assert "bestsecret.com/product.htm?code=1" in sent[0]
    assert "49.99" in sent[0]


def test_ads_go_out_minutes_apart_not_seconds():
    """Серия обяви една след друга е това, което вика проверката „не сте робот"."""
    from shopbot.config import Config
    from shopbot.humanize import Pacer

    cfg = Config()
    pacer = Pacer(cfg.runtime)
    assert cfg.runtime.min_publish_delay_s >= 120
    assert cfg.runtime.min_publish_delay_s > cfg.runtime.max_action_delay_s
    assert hasattr(pacer, "publish_pause")


def test_the_listing_promises_inspection_before_payment():
    """Обещанието за преглед и тест е част от доверието — пази се в конфига."""
    from shopbot.config import load_config

    cfg = load_config()
    assert "преглед и тест" in cfg.listing.extra_note


def test_the_removal_notice_names_which_ad_fell():
    """Без адрес известието не казва коя от двайсет обяви е паднала."""
    import asyncio

    sent: list[str] = []
    n = Notifier()
    n.send = lambda text: sent.append(text) or asyncio.sleep(0)

    asyncio.run(n.removed("Слънчеви очила Carrera", "изчерпан е",
                          "https://bazar.bg/obiava-56010352/slanchevi-ochila"))
    assert "obiava-56010352" in sent[0]
    assert "изчерпан" in sent[0]


def test_pending_removals_carry_the_ad_url(orch):
    """Адресът идва от базата заедно с причината, иначе известието е сляпо."""
    o, db, _ = orch
    publish(db, make(price=50.0, orig=200.0))
    db.upsert_product(make(price=120.0, orig=200.0))   # 40%, под твърдия под
    pending = o._pending_removals()
    assert len(pending) == 1
    assert pending[0][4].startswith("https://bazar.bg/obiava-")


def test_removal_defaults_to_the_reversible_option():
    """Изтриването е необратимо; продуктът може пак да поевтинее след седмица."""
    from shopbot.config import Config

    assert Config().removal.mode == "deactivate"


def test_a_second_browser_refuses_the_same_profile(tmp_path):
    """Два браузъра върху един профил събарят сесията — вторият трябва да откаже."""
    import os

    from shopbot.browser import BrowserSession, ProfileBusy

    profile = tmp_path / "bazar"
    profile.mkdir()
    (profile / ".shopbot.lock").write_text(str(os.getppid()), encoding="utf-8")

    other = BrowserSession("bazar", profile, headless=True)
    try:
        other._claim_profile()
    except ProfileBusy as exc:
        assert "bazar" in str(exc)
    else:
        raise AssertionError("вторият процес не биваше да получи профила")


def test_a_stale_lock_does_not_block_forever(tmp_path):
    """Убит процес не бива да заключи профила завинаги."""
    from shopbot.browser import BrowserSession

    profile = tmp_path / "bestsecret"
    profile.mkdir()
    (profile / ".shopbot.lock").write_text("999999", encoding="utf-8")  # мъртъв PID

    s = BrowserSession("bestsecret", profile, headless=True)
    s._claim_profile()          # не хвърля
    assert (profile / ".shopbot.lock").read_text().strip() == str(__import__("os").getpid())


def test_the_lock_check_tells_live_from_dead():
    import os

    from shopbot.browser import _pid_alive

    assert _pid_alive(os.getppid()) is True
    assert _pid_alive(999999) is False


def test_the_lock_check_never_signals_on_windows(monkeypatch):
    """Под Windows os.kill(pid, 0) праща Ctrl+C, а всичко друго убива процеса."""
    import os

    import pytest

    from shopbot.browser import _pid_alive

    if os.name != "nt":
        pytest.skip("само под Windows")

    def forbidden(*args):
        raise AssertionError("os.kill не е проверка под Windows")

    monkeypatch.setattr(os, "kill", forbidden)
    assert _pid_alive(os.getppid()) is True


def test_a_title_with_an_ampersand_still_reaches_telegram():
    """„Dolce & Gabbana“ в HTML режим кара Telegram да откаже цялото съобщение."""
    import asyncio

    sent: list[str] = []
    n = Notifier()
    n.send = lambda text: sent.append(text) or asyncio.sleep(0)

    asyncio.run(n.published(
        "Шал Dolce & Gabbana <Logo>", "59.90 €", "https://bazar.bg/obiava-1",
        source_url="https://www.bestsecret.com/product.htm?code=1&colorCode=2",
    ))
    assert "Dolce &amp; Gabbana &lt;Logo&gt;" in sent[0]
    assert 'href="https://www.bestsecret.com/product.htm?code=1&amp;colorCode=2"' in sent[0]
