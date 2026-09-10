"""Оценката от модела е портата пред публикуването — заслужава истински тест."""

import asyncio

import pytest

from shopbot.appraise import AppraisalUnavailable, _parse, appraise, prompt_version
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
    db.save_appraisal(product.id, score, reason, "test", prompt_version())
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


# ------------------------------------------------------- доставчици


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload


def capture(monkeypatch, payload, status=200):
    """Подменя мрежата и връща какво е било изпратено."""
    sent = {}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            sent["url"] = url
            sent["headers"] = headers or {}
            sent["body"] = json
            return FakeResponse(payload, status)

    monkeypatch.setattr("shopbot.appraise.httpx.AsyncClient", FakeClient)
    return sent


def call(cfg, tmp_path, key=""):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"fake jpeg bytes")
    return asyncio.run(appraise(make_product(), 99.9, "EUR", [photo], cfg, key))


def test_gemini_is_the_free_default(tmp_path, monkeypatch):
    """По подразбиране не бива да се вика платен доставчик."""
    cfg = AppraisalConfig(enabled=True)
    assert cfg.provider == "gemini"
    sent = capture(monkeypatch, {
        "candidates": [{"content": {"parts": [{"text": '{"score": 80, "reason": "ок"}'}]}}]
    })
    verdict = call(cfg, tmp_path, key="free-key")
    assert verdict.score == pytest.approx(0.80)
    assert "generativelanguage" in sent["url"]
    assert sent["headers"]["x-goog-api-key"] == "free-key"


def test_ollama_needs_no_key_and_stays_local(tmp_path, monkeypatch):
    cfg = AppraisalConfig(enabled=True, provider="ollama", model="llava")
    sent = capture(monkeypatch, {
        "message": {"content": '{"score": 40, "reason": "странна форма"}'}
    })
    verdict = call(cfg, tmp_path)
    assert verdict.score == pytest.approx(0.40)
    assert sent["url"].startswith("http://127.0.0.1:11434")
    assert sent["body"]["messages"][1]["images"]


def test_missing_key_says_which_one(tmp_path):
    cfg = AppraisalConfig(enabled=True)
    with pytest.raises(AppraisalUnavailable, match="GEMINI_API_KEY"):
        call(cfg, tmp_path)


def test_blocked_answer_is_an_error_not_a_zero(tmp_path, monkeypatch):
    """Отрязан или блокиран отговор не бива да мине за 'никой няма да го купи'."""
    cfg = AppraisalConfig(enabled=True)
    capture(monkeypatch, {"candidates": []})
    with pytest.raises(AppraisalUnavailable):
        call(cfg, tmp_path, key="free-key")


def test_new_rules_invalidate_old_verdicts(tmp_path):
    """Смениш ли правилата, старата оценка отговаря на друг въпрос."""
    db = Database(tmp_path / "t.db")
    db.save_appraisal("x1", 0.9, "по старите правила", "test", "старa-версия")
    assert db.get_appraisal("x1", "старa-версия") is not None
    assert db.get_appraisal("x1", prompt_version()) is None
    db.close()
