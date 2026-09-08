"""Стратегията за аксесоари: без размерен риск, 70%+, предимно известни марки.

Част от тестовете зареждат истинския config/config.yaml — те са там, за да
не може настройка да се промени случайно и мълчаливо да развали подбора.
"""

import pytest

from shopbot.config import PopularityConfig, SelectionConfig, load_config
from shopbot.listing import build_description, label_to_path, path_to_label
from shopbot.models import Product, Size
from shopbot.pricing import compute_price
from shopbot.selection import evaluate
from shopbot.sources.bestsecret import _paged_url


def accessory(**overrides):
    """Слънчеви очила: без размери, каквито са реалните аксесоари."""
    base = {
        "id": "1",
        "url": "http://x/1",
        "brand": "Guess",
        "name": "Слънчеви очила",
        "category_key": "sunglasses_women",
        "price": 60.0,
        "orig_price": 240.0,
        "images": ["a.jpg", "b.jpg"],
        "sizes": [],
        "listing_rank": 20,
    }
    base.update(overrides)
    return Product(**base)


# ------------------------------------------------------- размери (или липсата им)


def test_item_without_sizes_is_accepted_as_one_size():
    cfg = SelectionConfig(
        min_discount_pct=70,
        min_source_price=30,
        max_source_price=200,
        allow_one_size=True,
        min_sizes_available=1,
        popularity=PopularityConfig(min_score=0.0),
    )
    verdict = evaluate(accessory(), cfg)
    assert verdict.accepted, verdict.reason


def test_one_size_can_be_turned_off():
    cfg = SelectionConfig(
        min_discount_pct=70, min_source_price=30, max_source_price=200,
        allow_one_size=False, popularity=PopularityConfig(min_score=0.0),
    )
    verdict = evaluate(accessory(), cfg)
    assert not verdict.accepted
    assert "няма размери" in verdict.reason


def test_items_that_do_have_sizes_are_still_checked():
    """Шапка с размери S/M/L, всичките изчерпани, трябва да отпадне."""
    cfg = SelectionConfig(
        min_discount_pct=70, min_source_price=30, max_source_price=200,
        allow_one_size=True, min_sizes_available=1,
        popularity=PopularityConfig(min_score=0.0),
    )
    hat = accessory(
        sizes=[Size("S", available=False), Size("M", available=False)],
    )
    verdict = evaluate(hat, cfg)
    assert not verdict.accepted
    assert "налични размера" in verdict.reason


def test_min_brand_tier_hard_filter():
    cfg = SelectionConfig(
        min_discount_pct=70, min_source_price=30, max_source_price=200,
        min_brand_tier=0.5,
        popularity=PopularityConfig(
            min_score=0.0, brand_tiers={"Guess": 1.0}, unknown_brand_tier=0.35
        ),
    )
    assert evaluate(accessory(brand="Guess"), cfg).accepted
    rejected = evaluate(accessory(brand="Някаква"), cfg)
    assert not rejected.accepted
    assert "под прага" in rejected.reason


# ------------------------------------------------------- истинската конфигурация


@pytest.fixture(scope="module")
def real():
    return load_config()


def test_config_enforces_the_agreed_thresholds(real):
    assert real.selection.min_discount_pct == 70
    assert real.selection.min_source_price == 30.0
    assert real.selection.max_source_price == 200.0
    assert real.selection.allow_one_size is True


def test_top_brand_at_seventy_percent_passes(real):
    verdict = evaluate(accessory(brand="Guess", price=60.0, orig_price=200.0), real.selection)
    assert verdict.accepted, verdict.reason


@pytest.mark.parametrize(
    "brand", ["Michael Kors", "Emporio Armani", "Furla", "Carrera", "Jimmy Choo", "Diesel"]
)
def test_every_brand_from_the_top_list_passes(real, brand):
    verdict = evaluate(accessory(brand=brand, price=60.0, orig_price=210.0), real.selection)
    assert verdict.accepted, f"{brand}: {verdict.reason}"


def test_unknown_brand_needs_an_extreme_discount(real):
    weak = accessory(brand="Някаква Марка", price=60.0, orig_price=210.0)  # 71%
    assert not evaluate(weak, real.selection).accepted

    strong = accessory(
        brand="Някаква Марка", price=40.0, orig_price=400.0,  # 90%
        bestseller_badge=True, low_stock=True, listing_rank=5,
    )
    assert evaluate(strong, real.selection).accepted


@pytest.mark.parametrize(
    "price,orig,why",
    [
        (25.0, 200.0, "твърде евтино — не си струва заявката"),
        (250.0, 1200.0, "твърде скъпо за предварително плащане"),
    ],
)
def test_price_band_is_respected(real, price, orig, why):
    verdict = evaluate(accessory(price=price, orig_price=orig), real.selection)
    assert not verdict.accepted, why
    assert "извън диапазона" in verdict.reason


def test_sixty_five_percent_discount_is_not_enough(real):
    verdict = evaluate(accessory(price=70.0, orig_price=200.0), real.selection)
    assert not verdict.accepted
    assert "намаление" in verdict.reason


def test_pricing_keeps_a_worthwhile_margin_on_cheap_accessories(real):
    """Ключодържател за 30 € трябва да носи поне минималния марж."""
    product = accessory(category_key="small_accessories_women", price=30.0, orig_price=150.0)
    price = compute_price(product, real.pricing)
    assert not price.rejected, price.rejected
    assert price.margin >= real.pricing.min_absolute_margin
    assert price.final < price.orig_price


def test_pricing_rejects_when_resale_would_exceed_catalogue(real):
    product = accessory(price=190.0, orig_price=210.0)
    assert compute_price(product, real.pricing).rejected


# ------------------------------------------------------- обява


def test_description_carries_the_preorder_notice(real):
    product = accessory()
    price = compute_price(product, real.pricing)
    text = build_description(product, price, real.listing)
    assert "предварителна заявка" in text
    assert "10 работни дни" in text
    assert "BestSecret" not in text


def test_description_has_no_leftover_placeholders(real):
    product = accessory()
    price = compute_price(product, real.pricing)
    text = build_description(product, price, real.listing)
    assert "{" not in text and "}" not in text


def test_markup_keys_match_real_category_keys(real):
    """Ключ с печатна грешка мълчаливо пада към default_markup — оттам идват
    сгрешени цени, които никой не забелязва."""
    known = {c.key for c in real.source.categories}
    unknown = set(real.pricing.markup_by_category) - known
    assert not unknown, f"markup_by_category сочи несъществуващи категории: {unknown}"


def test_every_source_category_has_a_bazar_category(real):
    missing = [
        c.key for c in real.source.categories if not real.listing.category_map.get(c.key)
    ]
    assert not missing, f"без Bazar.bg категория: {missing}"


# ------------------------------------------------------- дребни помощни


def test_category_path_roundtrip():
    path = ["Мода", "Аксесоари", "Портфейли, портмонета"]
    label = path_to_label(path)
    assert label == "Мода > Аксесоари > Портфейли, портмонета"
    assert label_to_path(label) == path
    assert path_to_label(label) == label  # вече е стринг, не се пипа


def test_paged_url_keeps_existing_filters():
    """Филтрираните адреси на BestSecret идват с готов query string."""
    base = "https://www.bestsecret.com/women/accessories.htm?discount=70&brand=guess"
    first = _paged_url(base, 1)
    assert "discount=70" in first and "brand=guess" in first and "page=" not in first

    second = _paged_url(base, 2)
    assert "discount=70" in second and "brand=guess" in second and "page=2" in second
    assert second.count("?") == 1


def test_paged_url_without_query():
    assert _paged_url("https://x.com/a.htm", 1) == "https://x.com/a.htm"
    assert _paged_url("https://x.com/a.htm", 3).endswith("?page=3")
