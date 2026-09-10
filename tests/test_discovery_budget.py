"""Бюджетът от отваряния трябва да стигне до всяка категория.

Първата категория (дамските очила са 700+ след филтъра) изяждаше целия
бюджет и часовниците, чантите и шапките не се обхождаха нито един цикъл.
"""

import asyncio

import pytest

from shopbot.config import Config, SourceCategory
from shopbot.db import Database
from shopbot.notify import Notifier
from shopbot.orchestrator import CycleReport, Orchestrator
from shopbot.sources.bestsecret import CardHit


class GreedySource:
    """Източник, при който всяка плочка води до отваряне на страница."""

    def __init__(self):
        self.visited: list[str] = []

    async def discover(self, category, page):
        self.visited.append(category.key)
        return [CardHit(url=f"http://x/{category.key}/{i}", rank=i) for i in range(100)]


@pytest.fixture()
def orch(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.source.categories = [
        SourceCategory(key=f"cat{i}", label=f"Категория {i}", url=f"http://x/{i}")
        for i in range(4)
    ]
    cfg.limits.max_product_pages_per_run = 40
    cfg.limits.max_tiles_per_run = 6000
    db = Database(tmp_path / "test.db")
    yield Orchestrator(cfg, db, Notifier()), cfg
    db.close()


def test_every_category_gets_a_share_of_the_openings(orch, monkeypatch):
    o, cfg = orch
    opened: list[str] = []

    async def fake_consider(source, page, session, hit, category_key, report):
        opened.append(category_key)
        return True

    monkeypatch.setattr(o, "_consider", fake_consider)
    monkeypatch.setattr(o.pacer, "pause", lambda **kw: asyncio.sleep(0))

    source = GreedySource()
    report = CycleReport()
    asyncio.run(o._discover(source, None, None, report))

    assert source.visited == [c.key for c in cfg.source.categories]
    per_category = {c.key: opened.count(c.key) for c in cfg.source.categories}
    assert all(count > 0 for count in per_category.values()), per_category
    # Поравно: 40 отваряния, четири категории.
    assert per_category == {"cat0": 10, "cat1": 10, "cat2": 10, "cat3": 10}
    assert len(opened) <= cfg.limits.max_product_pages_per_run


def test_weight_decides_who_gets_the_openings(tmp_path, monkeypatch):
    """Тежестта е начинът да се наблегне на една категория, без да се махат другите."""
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.source.categories = [
        SourceCategory(key="watches", label="Часовници", url="http://x/1", weight=3.0),
        SourceCategory(key="sunglasses", label="Очила", url="http://x/2", weight=1.0),
    ]
    cfg.limits.max_product_pages_per_run = 40
    db = Database(tmp_path / "w.db")
    o = Orchestrator(db=db, cfg=cfg, notifier=Notifier())

    opened: list[str] = []

    async def fake_consider(source, page, session, hit, category_key, report):
        opened.append(category_key)
        return True

    monkeypatch.setattr(o, "_consider", fake_consider)
    monkeypatch.setattr(o.pacer, "pause", lambda **kw: asyncio.sleep(0))
    asyncio.run(o._discover(GreedySource(), None, None, CycleReport()))
    db.close()

    assert opened.count("watches") == 30
    assert opened.count("sunglasses") == 10
