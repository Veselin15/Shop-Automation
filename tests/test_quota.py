"""Безплатните обяви в Bazar.bg са сто. След тях публикуването чака, а не гори."""

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest

from shopbot.config import Config
from shopbot.db import Database
from shopbot.models import Listing, Product, ProductStatus
from shopbot.notify import Notifier
from shopbot.orchestrator import CycleReport, Orchestrator
from shopbot.sinks.bazar import (
    FreeAdQuota,
    ProfileSnapshot,
    QuotaExhausted,
    read_free_ad_quota,
)

# Дословно от профила, 13 септември.
WALL = """Чисто нова мъжка чанта Cerruti 1881 Backpack
редакция
Публикувано/обновено: днес в 14:50 ч.
Лимит за безплатни обяви
Виждаш това, защото си достигнал лимита за безплатни обяви.

За да продължиш да добавяш обяви, трябва да закупиш пакет обяви, или да активираш Premium абонамент.

Брой оставащи безплатни обяви: 0 / 100

*Дата на следваща безплатна обява - 17 септември"""


def test_the_limit_page_is_read():
    assert read_free_ad_quota(WALL, date(2026, 9, 13)) == FreeAdQuota(
        remaining=0, total=100, resume_on=date(2026, 9, 17), exhausted=True
    )


def test_free_ads_left_are_not_a_wall():
    quota = read_free_ad_quota("Брой оставащи безплатни обяви: 37 / 100", date(2026, 9, 13))
    assert quota.remaining == 37 and not quota.exhausted
    assert read_free_ad_quota("Добави обява", date(2026, 9, 13)) is None


def test_january_seen_in_december_is_next_year():
    quota = read_free_ad_quota("Дата на следваща безплатна обява - 3 януари", date(2026, 12, 29))
    assert quota.resume_on == date(2027, 1, 3)


# --------------------------------------------- в цикъла


class Silent(Notifier):
    async def send(self, text):
        pass


@pytest.fixture()
def orch(tmp_path, monkeypatch):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.listing.category_map = {"bags_women": 338}
    db = Database(tmp_path / "quota.db")
    o = Orchestrator(cfg, db, Silent())

    async def no_pause(*args, **kwargs):
        pass

    monkeypatch.setattr(o.pacer, "pause", no_pause)
    monkeypatch.setattr(o.pacer, "publish_pause", no_pause)
    yield o, db, cfg
    db.close()


def product(pid):
    return Product(id=pid, url=f"http://x/{pid}", brand="Guess", name="Bag",
                   category_key="bags_women", price=49.99, orig_price=199.0, images=["a.jpg"])


def waiting(db, cfg, pid):
    db.upsert_product(product(pid))
    db.save_candidate(Listing(product_id=pid, title=pid))
    folder = cfg.images_dir / pid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "00_x.jpg").write_bytes(b"jpeg")


def live(db, pid, age_days=10):
    db.upsert_product(product(pid))
    db.save_candidate(Listing(product_id=pid, title=pid))
    db.mark_published(pid, f"ad{pid}", f"https://bazar.bg/obiava-{pid}", 75)
    published = (datetime.now(UTC) - timedelta(days=age_days)).isoformat(timespec="seconds")
    db.conn.execute("UPDATE listings SET published_at=? WHERE product_id=?", (published, pid))
    db.conn.commit()


def test_the_wall_stops_publishing_without_burning_attempts(orch):
    """Стотата обява и трите след нея отидоха в „Чакащи плащане“ като публикувани."""
    o, db, cfg = orch
    for pid in ("a", "b"):
        waiting(db, cfg, pid)

    class WalledSink:
        tries = 0

        async def publish(self, listing, images, page, dry_run=False):
            WalledSink.tries += 1
            raise QuotaExhausted("край", FreeAdQuota(0, 100, date(2026, 9, 17), True),
                                 draft_id="56057102")

    published, stopped = asyncio.run(o._publish_candidates(
        WalledSink(), None, db.pending_candidates(), CycleReport(), False))

    assert (published, stopped, WalledSink.tries) == (0, True, 1)
    assert [db.get_listing(p)["state"] for p in ("a", "b")] == ["candidate", "candidate"]
    assert db.get_listing("a")["attempts"] == 0
    assert db.get_state("free_ads_resume_on") == "2026-09-17"


def test_no_trip_to_publish_while_the_free_ads_are_out(orch, monkeypatch):
    o, db, cfg = orch
    waiting(db, cfg, "a")
    db.set_state("free_ads_resume_on", (o.pacer.now().date() + timedelta(days=2)).isoformat())

    class NoBrowser:
        def __init__(self, *args, **kwargs):
            raise AssertionError("не бива да се отваря браузър")

    monkeypatch.setattr("shopbot.orchestrator.BrowserSession", NoBrowser)
    assert asyncio.run(o._sink_phase(CycleReport(), True, True, False)) is False


def test_publishing_resumes_on_the_day_bazar_named(orch):
    o, db, _ = orch
    db.set_state("free_ads_resume_on", o.pacer.now().date().isoformat())
    assert o._quota_blocked() is False


def test_an_ad_waiting_for_payment_goes_back_to_the_queue(orch):
    o, db, _ = orch
    live(db, "seen")
    live(db, "paywalled", age_days=0)
    live(db, "old", age_days=5)

    o._take_stock(ProfileSnapshot(active={"adseen": None}))

    assert db.get_listing("seen")["state"] == "published"
    assert db.get_listing("paywalled")["state"] == "candidate"
    assert db.get_listing("paywalled")["bazar_id"] is None
    assert db.get_listing("old")["state"] == "removed"


def test_a_broken_read_does_not_unpublish_everything(orch):
    o, db, _ = orch
    for i in range(12):
        live(db, f"p{i}")
    o._take_stock(ProfileSnapshot(active={"adp0": None}))
    assert db.active_listing_count() == 12


def test_a_sold_out_candidate_is_dropped_before_it_costs_a_free_ad(orch):
    o, db, cfg = orch
    waiting(db, cfg, "gone")
    waiting(db, cfg, "fine")
    stale = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    db.conn.execute("UPDATE products SET last_checked=?", (stale,))
    db.conn.commit()

    class Source:
        async def fetch_product(self, url, category_key, page, hit=None):
            fresh = product(url.rsplit("/", 1)[1])
            if fresh.id == "gone":
                fresh.status = ProductStatus.SOLD_OUT
            return fresh

    asyncio.run(o._recheck_queue(Source(), None, CycleReport()))
    assert db.get_listing("gone")["state"] == "removed"
    assert db.get_listing("fine")["state"] == "candidate"
