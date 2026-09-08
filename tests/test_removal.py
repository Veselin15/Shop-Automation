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
    db.mark_published(product.id, f"ad{product.id}", f"https://bazar.bg/obiava-{product.id}")


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
