"""Редът на публикуване: часовниците водят, но никоя категория не гладува."""

from collections import Counter

from shopbot.config import Config
from shopbot.db import Database
from shopbot.models import Listing, Product
from shopbot.orchestrator import publish_queue
from shopbot.selection import fair_order

WEIGHTS = {"watches": 3.0, "bags": 1.5, "sunglasses": 0.5}


def rows(category, n, appraisal=0.6):
    return [
        {"product_id": f"{category}{i}", "category_key": category,
         "appraisal": appraisal - i / 100}
        for i in range(n)
    ]


def categories(order):
    return Counter(r["category_key"] for r in order)


def test_the_heavier_category_gets_more_places_but_everyone_gets_some():
    """Чантите са оценени по-високо от часовниците — дялът пак следва тежестта."""
    queue = rows("bags", 20, 0.85) + rows("watches", 20, 0.50) + rows("sunglasses", 20, 0.75)
    order = fair_order(queue, WEIGHTS, recent={}, limit=20)
    counts = categories(order)
    assert order[0]["category_key"] == "watches"
    assert counts["watches"] > counts["bags"] > counts["sunglasses"] > 0


def test_memory_of_recent_ads_lets_the_light_categories_through():
    """По 7 обяви на цикъл: без памет първите 7 места са винаги едни и същи."""
    queue = rows("bags", 30) + rows("watches", 30) + rows("sunglasses", 30)

    assert "sunglasses" not in categories(fair_order(queue, WEIGHTS, {}, 7))

    recent: Counter = Counter()
    published = []
    for _ in range(3):
        batch = fair_order(queue, WEIGHTS, recent, 7)
        published.extend(batch)
        recent.update(categories(batch))
        queue = [r for r in queue if r not in batch]
    assert categories(published)["sunglasses"] > 0


def test_a_starved_category_goes_first():
    """Последният ден беше само чанти — часовниците са на ход."""
    queue = rows("bags", 5, 0.9) + rows("watches", 5, 0.5)
    order = fair_order(queue, WEIGHTS, recent={"bags": 20}, limit=4)
    assert [r["category_key"] for r in order] == ["watches"] * 4


def test_inside_a_category_the_best_judged_goes_first():
    order = fair_order(rows("watches", 3), WEIGHTS, {}, None)
    assert [r["product_id"] for r in order] == ["watches0", "watches1", "watches2"]


def test_the_queue_remembers_only_the_last_day(tmp_path):
    """Паметта е колкото дневния лимит — стара история не бива да решава днес."""
    db = Database(tmp_path / "order.db")
    for i, category in enumerate(["bags"] * 5 + ["watches"] * 2):
        pid = f"p{i}"
        db.upsert_product(Product(id=pid, url=f"http://x/{pid}", category_key=category))
        db.save_candidate(Listing(product_id=pid, title=pid))
        db.mark_published(pid, pid, f"https://bazar.bg/obiava-{i}")
        db.conn.execute("UPDATE listings SET published_at=? WHERE product_id=?",
                        (f"2026-09-12T10:{i:02d}:00+00:00", pid))
    db.conn.commit()

    assert db.recent_publish_counts(3) == {"bags": 1, "watches": 2}

    cfg = Config()
    cfg.limits.max_publish_per_day = 3
    assert publish_queue(db, cfg, 10) == []
    db.close()
