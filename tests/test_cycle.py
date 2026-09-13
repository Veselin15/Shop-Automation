"""Цикълът: кога тръгва обхождането, кога спира публикуването, колко известия."""

import asyncio

import pytest

from shopbot.browser import AuthWallError
from shopbot.config import Config
from shopbot.db import Database
from shopbot.models import Listing, Product
from shopbot.notify import Notifier
from shopbot.orchestrator import CycleReport, Orchestrator
from shopbot.sinks.bazar import CaptchaWall


class CountingNotifier(Notifier):
    def __init__(self):
        super().__init__()
        self.sent: list[str] = []

    async def send(self, text):
        self.sent.append(text)


@pytest.fixture()
def orch(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    db = Database(tmp_path / "cycle.db")
    notifier = CountingNotifier()
    yield Orchestrator(cfg, db, notifier), db, cfg, notifier
    db.close()


def make_product(pid, category="watches_men"):
    return Product(id=pid, url=f"http://x/{pid}", brand="Guess", name="Watch",
                   category_key=category, price=49.99, orig_price=199.0, images=["a.jpg"])


def queue(db, cfg, pid):
    db.upsert_product(make_product(pid), score=0.5)
    db.save_candidate(Listing(product_id=pid, title=pid))
    folder = cfg.images_dir / pid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "00_x.jpg").write_bytes(b"jpeg")


class Stop(RuntimeError):
    pass


def test_first_cycle_after_a_reboot_discovers(orch, monkeypatch):
    """loop.time() брои от пускането на машината: 20 минути след рестарт е 1200."""
    o, _, _, _ = orch
    seen = []

    async def fake_run_once(discover, publish, reconcile, dry_run=False):
        seen.append(discover)

    async def stop_sleeping(seconds):
        raise Stop

    monkeypatch.setattr(o, "run_once", fake_run_once)
    monkeypatch.setattr(o.pacer, "within_active_hours", lambda at=None: True)
    monkeypatch.setattr(asyncio, "sleep", stop_sleeping)

    async def main():
        asyncio.get_running_loop().time = lambda: 1200.0
        await o.run_forever()

    with pytest.raises(Stop):
        asyncio.run(main())
    assert seen == [True]


def test_one_notification_per_outage_not_per_cycle(orch, monkeypatch):
    o, _, _, notifier = orch
    walled = True

    async def source_phase(report, discover, reconcile):
        if walled:
            raise AuthWallError("bestsecret", "login wall")

    async def sink_phase(report, publish, reconcile, dry_run):
        return False

    monkeypatch.setattr(o, "_source_phase", source_phase)
    monkeypatch.setattr(o, "_sink_phase", sink_phase)

    for _ in range(3):
        asyncio.run(o.run_once())
    assert len(notifier.sent) == 1

    walled = False
    asyncio.run(o.run_once())
    walled = True
    asyncio.run(o.run_once())
    assert len(notifier.sent) == 2, "след успешен вход новото прекъсване пак се съобщава"


def test_a_robot_check_stops_the_run_without_burning_attempts(orch, monkeypatch):
    """Два неуспеха подред с „не сте робот“ — точно това изгаряше опитите."""
    o, db, cfg, _ = orch
    cfg.listing.category_map = {"watches_men": 336}
    for pid in ("w1", "w2", "w3"):
        queue(db, cfg, pid)

    class WalledSink:
        tries = 0

        async def publish(self, listing, images, page, dry_run=False):
            WalledSink.tries += 1
            raise CaptchaWall("обявата не се публикува (Моля, повърдете че не сте робот.)")

    async def no_pause(*args, **kwargs):
        pass

    monkeypatch.setattr(o.pacer, "pause", no_pause)
    monkeypatch.setattr(o.pacer, "publish_pause", no_pause)

    rows = db.pending_candidates(10)
    asyncio.run(o._publish_candidates(WalledSink(), None, rows, CycleReport(), False))

    assert WalledSink.tries == 1
    assert [db.get_listing(p)["attempts"] for p in ("w1", "w2", "w3")] == [0, 0, 0]


def test_no_trip_to_bazar_when_nothing_may_be_published(orch, monkeypatch):
    o, db, cfg, _ = orch
    cfg.limits.max_publish_per_day = 1
    queue(db, cfg, "w1")
    db.bump_counter("publish")

    class NoBrowser:
        def __init__(self, *args, **kwargs):
            raise AssertionError("не бива да се отваря браузър")

    monkeypatch.setattr("shopbot.orchestrator.BrowserSession", NoBrowser)
    assert asyncio.run(o._sink_phase(CycleReport(), True, True, False)) is False


def test_a_live_session_is_written_back_and_a_dead_one_is_not(orch, monkeypatch):
    """Bazar.bg сменя `pl` в движение; файлът от входа остаряваше за един цикъл."""
    o, db, cfg, _ = orch
    cfg.listing.category_map = {"watches_men": 336}
    queue(db, cfg, "w1")
    saved = []
    walled = False

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            pass

        async def new_page(self):
            return None

        async def save_seed(self):
            saved.append(True)

        async def stop(self):
            pass

    class FakeSink:
        def __init__(self, *args):
            pass

        async def ensure_logged_in(self, page):
            if walled:
                raise AuthWallError("bazar", "login wall")

    async def publish(*args):
        pass

    monkeypatch.setattr("shopbot.orchestrator.BrowserSession", FakeSession)
    monkeypatch.setattr("shopbot.orchestrator.BazarSink", FakeSink)
    monkeypatch.setattr(o, "_publish_candidates", publish)

    assert asyncio.run(o._sink_phase(CycleReport(), True, False, False)) is True
    assert saved == [True]

    walled = True
    with pytest.raises(AuthWallError):
        asyncio.run(o._sink_phase(CycleReport(), True, False, False))
    assert saved == [True], "стената не бива да презапише добрия файл"


def test_an_unread_catalogue_price_does_not_erase_the_discount(orch):
    _, db, _, _ = orch
    db.upsert_product(make_product("p1"))
    db.update_prices("p1", 45.0, 0.0)
    product = db.get_product("p1")
    assert product.price == 45.0
    assert product.orig_price == 199.0
