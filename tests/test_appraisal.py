"""Оценката от модела е портата пред публикуването — заслужава истински тест."""

import asyncio

import pytest

from shopbot.appraise import AppraisalUnavailable, _parse
from shopbot.config import AppraisalConfig, Config, load_config
from shopbot.db import Database
from shopbot.models import Product
from shopbot.notify import Notifier
from shopbot.orchestrator import CycleReport, Orchestrator
from shopbot.pricing import compute_price


def make_product(pid="777", category="watches_men", brand="Michael Kors"):
    return Product(
        id=pid,
        url=f"http://x/{pid}",
        brand=brand,
        name="Chronograph",
        category_key=category,
        price=89.49,
        orig_price=349.0,
        images=["a.jpg"],
    )


@pytest.fixture()
def orch(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.appraisal = AppraisalConfig(
        enabled=True, min_score=0.55, min_score_by_category={"watches_men": 0.45}
    )
    db = Database(tmp_path / "test.db")
    yield Orchestrator(cfg, db, Notifier()), db, cfg
    db.close()


def run(orch, product, score, reason="ок", monkeypatch=None):
    o, db, cfg = orch
    price = compute_price(product, cfg.pricing)
    report = CycleReport()
    db.save_appraisal(product.id, score, reason, "test")
    return asyncio.run(o._appraisal_passes(product, price, [], report)), report


def test_low_score_stops_the_listing(orch):
    passed, report = run(orch, make_product(category="sunglasses_women"), 0.30)
    assert passed is False
    assert report.appraisal_rejects == 1


def test_watches_pass_where_sunglasses_would_not(orch):
    """Един и същ скор, различна категория — това е смисълът на праговете."""
    assert run(orch, make_product(pid="1", category="watches_men"), 0.50)[0] is True
    assert run(orch, make_product(pid="2", category="sunglasses_women"), 0.50)[0] is False


def test_disabled_appraisal_lets_everything_through(orch):
    _, _, cfg = orch
    cfg.appraisal.enabled = False
    assert run(orch, make_product(pid="3"), 0.01)[0] is True


def test_cached_score_costs_nothing(orch):
    """Оценката се плаща веднъж: кешираният артикул не пипа бюджета."""
    o, _, _ = orch
    before = o._appraisal_budget
    run(orch, make_product(pid="4"), 0.90)
    assert o._appraisal_budget == before


def test_unreadable_answer_is_an_error_not_a_zero(orch):
    """Нечетим отговор не бива да мине за 'лош продукт'."""
    with pytest.raises(AppraisalUnavailable):
        _parse("моля извинете, не мога да преценя")


def test_answer_wrapped_in_prose_is_still_read():
    verdict = _parse('Ето оценката: {"score": 72, "reason": "класически часовник"}')
    assert verdict.score == pytest.approx(0.72)
    assert verdict.reason == "класически часовник"


def test_every_configured_threshold_belongs_to_a_real_category():
    """Праг за несъществуваща категория е тих no-op — по-добре да го хванем."""
    cfg = load_config()
    known = {c.key for c in cfg.source.categories}
    unknown = set(cfg.appraisal.min_score_by_category) - known
    assert not unknown, f"прагове за непознати категории: {unknown}"
