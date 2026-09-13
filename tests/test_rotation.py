"""Пълен профил: слабата обява отстъпва мястото си, но само на ясно по-добър кандидат."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from shopbot.config import Config, RotationConfig
from shopbot.db import Database
from shopbot.models import AdStats, Listing, Product
from shopbot.notify import Notifier
from shopbot.orchestrator import CycleReport, Orchestrator, swap_plan
from shopbot.selection import listing_value, plan_swaps
from shopbot.sinks.bazar import ProfileSnapshot, parse_ad_stats

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)
CFG = RotationConfig(min_age_days=5, margin=0.05)


def ad(pid, category="bags", appraisal=0.8, age_days=10, views=30, phones=0, favorites=0,
       stats=True):
    return {
        "product_id": pid, "bazar_id": f"ad{pid}", "title": pid, "category_key": category,
        "appraisal": appraisal,
        "published_at": (NOW - timedelta(days=age_days)).isoformat(timespec="seconds"),
        "views": views, "phones": phones, "favorites": favorites,
        "stats_at": NOW.isoformat() if stats else None,
    }


def cand(pid, category="bags", appraisal=0.8):
    return {"product_id": pid, "category_key": category, "appraisal": appraisal, "title": pid}


def pairs(swaps):
    return [(s.victim["product_id"], s.candidate["product_id"]) for s in swaps]


def test_an_ad_nobody_looks_at_gives_way_to_an_equal_candidate():
    """Същата оценка, но купувачите вече казаха своето — нулата губи половината."""
    active = [ad("quiet", views=0), ad("b"), ad("c")]
    swaps = plan_swaps(active, [cand("new")], {"bags": 1.0}, 3, CFG, NOW)
    assert pairs(swaps) == [("quiet", "new")]


def test_the_candidate_must_be_clearly_better():
    active = [ad("a"), ad("b")]
    assert plan_swaps(active, [cand("n", appraisal=0.82)], {"bags": 1.0}, 2, CFG, NOW) == []
    assert pairs(plan_swaps(active, [cand("n", appraisal=0.9)], {"bags": 1.0}, 2, CFG, NOW))


def test_young_ads_and_ads_someone_wants_are_kept():
    kept = [
        ad("young", views=0, age_days=2),
        ad("called", views=0, phones=1),
        ad("liked", views=0, favorites=1),
        ad("unread", views=0, stats=False),
    ]
    for row in kept:
        assert listing_value(row, NOW, 3.0, CFG.min_age_days) is None, row["product_id"]
    assert plan_swaps(kept, [cand("n", appraisal=0.95)], {"bags": 1.0}, 4, CFG, NOW) == []


def test_an_ad_from_before_the_appraisal_counts_as_zero():
    active = [ad("old_hat", category="hats", appraisal=-1), ad("b", category="hats")]
    swaps = plan_swaps(active, [cand("n", category="hats", appraisal=0.5)],
                       {"hats": 1.0}, 2, CFG, NOW)
    assert pairs(swaps) == [("old_hat", "n")]


def test_a_category_under_its_share_keeps_its_ads_against_the_others():
    """Два скучни часовника при таван 10 — делът им е 6, чантите не ги изместват."""
    weights = {"watches": 3.0, "bags": 1.5, "sunglasses": 0.5}
    active = ([ad(f"w{i}", category="watches", appraisal=0.5, views=0) for i in range(2)]
              + [ad(f"b{i}", category="bags", appraisal=0.8) for i in range(8)])

    swaps = plan_swaps(active, [cand("sun", category="sunglasses", appraisal=0.9)],
                       weights, 10, CFG, NOW)
    assert len(swaps) == 1 and swaps[0].victim["category_key"] == "bags"

    swaps = plan_swaps(active, [cand("watch", category="watches", appraisal=0.5)],
                       weights, 10, CFG, NOW)
    assert swaps[0].victim["category_key"] == "watches", "в своята категория смяната е честна"


def test_a_category_over_its_share_does_not_grow_at_the_expense_of_another():
    """Живата база: дамските чанти 26 при дял 9, мъжките 13 при дял 6 — и двете над."""
    weights = {"bags": 1.0, "bags_men": 1.0}
    active = ([ad(f"b{i}", category="bags", appraisal=0.8) for i in range(6)]
              + [ad(f"m{i}", category="bags_men", appraisal=0.6) for i in range(5)])
    swaps = plan_swaps(active, [cand("new", category="bags", appraisal=0.9)],
                       weights, 8, CFG, NOW)
    assert [s.victim["category_key"] for s in swaps] == ["bags"]


def test_each_ad_goes_once_and_the_limit_holds():
    active = [ad("q1", views=0), ad("q2", views=0), ad("b1"), ad("b2"), ad("b3")]
    many = [cand(f"n{i}", appraisal=0.9) for i in range(5)]
    assert len(plan_swaps(active, many, {"bags": 1.0}, 5, CFG, NOW, limit=1)) == 1

    swaps = plan_swaps(active, many, {"bags": 1.0}, 5, CFG, NOW)
    victims = [s.victim["product_id"] for s in swaps]
    assert len(victims) == len(set(victims))
    assert set(victims) >= {"q1", "q2"}


def test_the_counters_under_an_ad_are_read():
    text = "Портфейл Diesel Leather wallet 65,90 € Прегледи: 8 · Телефон: 1 · Добавено в Любими: 2"
    assert parse_ad_stats(text) == AdStats(views=8, phones=1, favorites=2)
    assert parse_ad_stats("Портфейл Diesel Leather wallet 65,90 €") is None
    assert parse_ad_stats("Прегледи: 8 · Добавено в Любими: 0") is None, "без телефона не е нула"


def test_an_ad_in_both_lists_is_not_taken_for_inactive():
    """?state=4 без ефект връща активните — те не бива да се изтрият като неактивни."""
    snapshot = ProfileSnapshot(active={"1": None}, inactive=["1", "2"])
    assert snapshot.inactive == ["2"]
    assert snapshot.used == 2


# --------------------------------------------- в цикъла


class Silent(Notifier):
    async def send(self, text):
        pass


@pytest.fixture()
def orch(tmp_path, monkeypatch):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.listing.category_map = {"bags_women": 338}
    db = Database(tmp_path / "rotation.db")
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


def live(db, pid, appraisal=0.8, age_days=10):
    db.upsert_product(product(pid))
    db.save_appraisal(pid, appraisal, "", "test")
    db.save_candidate(Listing(product_id=pid, title=pid))
    db.mark_published(pid, f"ad{pid}", f"https://bazar.bg/obiava-{pid}", 75)
    published = (datetime.now(UTC) - timedelta(days=age_days)).isoformat(timespec="seconds")
    db.conn.execute("UPDATE listings SET published_at=? WHERE product_id=?", (published, pid))
    db.conn.commit()


def waiting(db, cfg, pid, appraisal=0.9):
    db.upsert_product(product(pid))
    db.save_appraisal(pid, appraisal, "", "test")
    db.save_candidate(Listing(product_id=pid, title=pid))
    folder = cfg.images_dir / pid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "00_x.jpg").write_bytes(b"jpeg")


class FakeSession:
    def __init__(self, *args, **kwargs):
        pass

    async def start(self):
        pass

    async def new_page(self):
        return None

    async def save_seed(self):
        pass

    async def stop(self):
        pass


class FakeSink:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.deleted: list[str] = []
        self.published: list[str] = []

    async def ensure_logged_in(self, page):
        pass

    async def read_profile(self, page):
        return self.snapshot

    async def delete_ad(self, bazar_id, page):
        self.deleted.append(bazar_id)
        return True

    async def publish(self, listing, images, page, dry_run=False):
        self.published.append(listing.product_id)
        return f"new{listing.product_id}", f"https://bazar.bg/obiava-new{listing.product_id}"


def test_a_full_profile_swaps_the_ad_nobody_looks_at(orch, monkeypatch):
    o, db, cfg = orch
    cfg.rotation.enabled = True
    cfg.limits.profile_slots = 2
    live(db, "quiet")
    live(db, "busy")
    waiting(db, cfg, "better")
    sink = FakeSink(ProfileSnapshot(active={"adquiet": AdStats(0, 0, 0),
                                            "adbusy": AdStats(60, 0, 0)}))
    monkeypatch.setattr("shopbot.orchestrator.BrowserSession", FakeSession)
    monkeypatch.setattr("shopbot.orchestrator.BazarSink", lambda *args: sink)

    report = CycleReport()
    assert asyncio.run(o._sink_phase(report, True, False, False)) is True

    assert sink.deleted == ["adquiet"]
    assert sink.published == ["better"]
    assert db.get_listing("quiet")["state"] == "removed"
    assert db.get_listing("better")["state"] == "published"
    assert report.swapped == 1
    assert o._free_slots == 0


def test_the_bots_deactivated_ads_are_deleted_and_manual_ones_stay(orch):
    o, db, cfg = orch
    cfg.removal.mode = "delete"
    live(db, "old")
    db.mark_removed("old", "изчерпан е")
    sink = FakeSink(ProfileSnapshot(inactive=["adold", "manual1"]))

    report = CycleReport()
    freed = asyncio.run(o._purge_inactive(sink, None, sink.snapshot, report, False))
    assert sink.deleted == ["adold"]
    assert freed == 1 and report.purged == 1


def test_a_short_read_does_not_open_slots_that_are_not_there(orch):
    o, db, cfg = orch
    cfg.limits.profile_slots = 3
    for pid in ("a", "b", "c"):
        live(db, pid)
    assert o._take_stock(ProfileSnapshot()) == 0


def test_an_ad_that_went_inactive_on_its_own_is_marked_down(orch):
    o, db, _ = orch
    live(db, "a")
    o._take_stock(ProfileSnapshot(inactive=["ada"]))
    assert db.get_listing("a")["state"] == "removed"


def test_an_ad_whose_counters_were_not_read_this_time_is_not_swapped(orch):
    o, db, cfg = orch
    cfg.limits.profile_slots = 1
    live(db, "a", appraisal=0.1)
    waiting(db, cfg, "w")
    db.save_ad_stats({"ada": AdStats(0, 0, 0)})
    assert swap_plan(db, cfg)

    o._take_stock(ProfileSnapshot(active={"ada": None}))
    assert swap_plan(db, cfg) == []


def test_no_trip_when_the_profile_was_full_and_nothing_can_give_way(orch, monkeypatch):
    o, db, cfg = orch
    waiting(db, cfg, "w")
    o._free_slots = 0

    class NoBrowser:
        def __init__(self, *args, **kwargs):
            raise AssertionError("не бива да се отваря браузър")

    monkeypatch.setattr("shopbot.orchestrator.BrowserSession", NoBrowser)
    assert asyncio.run(o._sink_phase(CycleReport(), True, True, False)) is False
