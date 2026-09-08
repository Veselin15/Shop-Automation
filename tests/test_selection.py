from shopbot.config import PopularityConfig, SelectionConfig
from shopbot.models import Product, Size
from shopbot.selection import brand_tier, evaluate, popularity_score

WEIGHTS = {
    "discount": 0.40,
    "bestseller_badge": 0.25,
    "low_stock": 0.15,
    "listing_rank": 0.10,
    "brand_tier": 0.10,
}


def cfg(**overrides):
    base = dict(
        min_discount_pct=55,
        min_source_price=15.0,
        max_source_price=400.0,
        min_sizes_available=2,
        popularity=PopularityConfig(
            min_score=0.45, weights=WEIGHTS, brand_tiers={"Hugo Boss": 1.0, "Puma": 0.8}
        ),
    )
    base.update(overrides)
    return SelectionConfig(**base)


def product(**overrides):
    base = dict(
        id="1",
        url="http://x/1",
        brand="Hugo Boss",
        name="Риза",
        price=60.0,
        orig_price=200.0,
        images=["a.jpg", "b.jpg"],
        sizes=[Size("M"), Size("L")],
        bestseller_badge=True,
        listing_rank=3,
    )
    base.update(overrides)
    return Product(**base)


def test_accepts_a_strong_candidate():
    verdict = evaluate(product(), cfg())
    assert verdict.accepted, verdict.reason


def test_rejects_weak_discount():
    verdict = evaluate(product(price=150.0, orig_price=200.0), cfg())
    assert not verdict.accepted
    assert "намаление" in verdict.reason


def test_rejects_when_too_few_sizes_left():
    p = product(sizes=[Size("M"), Size("L", available=False)])
    verdict = evaluate(p, cfg())
    assert not verdict.accepted
    assert "налични размера" in verdict.reason


def test_rejects_product_without_images():
    verdict = evaluate(product(images=[]), cfg())
    assert not verdict.accepted
    assert verdict.reason == "няма снимки"


def test_brand_denylist_is_case_insensitive():
    verdict = evaluate(product(brand="hugo boss"), cfg(brands_deny=["Hugo Boss"]))
    assert not verdict.accepted
    assert "черен списък" in verdict.reason


def test_brand_allowlist_blocks_everything_else():
    verdict = evaluate(product(brand="Puma"), cfg(brands_allow=["Hugo Boss"]))
    assert not verdict.accepted
    assert "белия списък" in verdict.reason


def test_denied_keyword_in_title():
    verdict = evaluate(product(name="Gift Card 50"), cfg(title_deny_keywords=["gift card"]))
    assert not verdict.accepted


def test_price_outside_range_is_rejected():
    assert not evaluate(product(price=5.0, orig_price=100.0), cfg()).accepted
    assert not evaluate(product(price=900.0, orig_price=5000.0), cfg()).accepted


def test_unknown_brand_gets_the_configured_default():
    assert brand_tier("Няма такава марка", {"Hugo Boss": 1.0}) == 0.35
    assert brand_tier("Няма такава марка", {"Hugo Boss": 1.0}, default=0.1) == 0.1
    assert brand_tier("hugo boss", {"Hugo Boss": 1.0}) == 1.0


def test_brand_matching_handles_how_bestsecret_writes_names():
    """BestSecret пише 'Emporio Armani', 'GUESS Accessories', 'BOSS Green'."""
    tiers = {"Armani": 1.0, "Guess": 1.0, "Boss": 1.0, "Ray-Ban": 0.95}
    assert brand_tier("Emporio Armani", tiers) == 1.0
    assert brand_tier("Giorgio Armani", tiers) == 1.0
    assert brand_tier("GUESS Accessories", tiers) == 1.0
    assert brand_tier("BOSS Green", tiers) == 1.0
    assert brand_tier("Ray-Ban", tiers) == 0.95
    assert brand_tier("Съвсем друга марка", tiers) == 0.35


def test_exact_match_wins_over_partial():
    tiers = {"Armani": 0.5, "Emporio Armani": 1.0}
    assert brand_tier("Emporio Armani", tiers) == 1.0


def test_popularity_signals_move_the_score():
    quiet = product(bestseller_badge=False, low_stock=False, listing_rank=500, brand="Zzz")
    loud = product(bestseller_badge=True, low_stock=True, listing_rank=1, brand="Hugo Boss")
    assert popularity_score(loud, cfg()) > popularity_score(quiet, cfg())


def test_score_below_threshold_is_rejected_even_if_filters_pass():
    weak = product(
        brand="Непозната", bestseller_badge=False, low_stock=False, listing_rank=900,
        price=60.0, orig_price=140.0,   # 57% — минава прага, но носи малко скор
    )
    verdict = evaluate(weak, cfg())
    assert not verdict.accepted
    assert "скор" in verdict.reason
