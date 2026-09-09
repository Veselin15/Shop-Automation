"""Стратегията за аксесоари: без размерен риск, 70%+, предимно известни марки.

Част от тестовете зареждат истинския config/config.yaml — те са там, за да
не може настройка да се промени случайно и мълчаливо да развали подбора.
"""

import pytest

from shopbot.config import PopularityConfig, SelectionConfig, load_config
from shopbot.listing import build_description, build_title
from shopbot.models import Product, Size
from shopbot.pricing import compute_price
from shopbot.selection import evaluate
from shopbot.sources.bestsecret import _dedupe_images, _image_area, _paged_url


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


def test_title_starts_with_a_bulgarian_word(real):
    """Bazar.bg се търси на кирилица; чисто английско заглавие не се намира."""
    product = accessory(brand="Carrera", name="Sunglasses Hyperfit 23/S")
    title = build_title(product, real.listing)
    assert title.startswith("Слънчеви очила"), title
    assert len(title) >= 15, "сайтът иска поне 15 знака"


def test_every_category_has_a_bulgarian_title_prefix(real):
    missing = [
        c.key for c in real.source.categories if not real.listing.title_prefix.get(c.key)
    ]
    assert not missing, f"без български префикс: {missing}"


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


# ------------------------------------------------------- снимки

IMG = "https://image.bestsecret.com/40564711__000889193/{h}/image_40564711__000889193_{size}_{i}.jpg"


def test_gallery_keeps_the_largest_variant_of_each_frame():
    """Всеки кадър идва в няколко размера, всеки със собствен хеш в пътя."""
    urls = [
        IMG.format(h="aaa", size="68X84", i=1),
        IMG.format(h="bbb", size="970X1182", i=1),
        IMG.format(h="ccc", size="352X429", i=2),
        IMG.format(h="ddd", size="970X1182", i=2),
    ]
    result = _dedupe_images(urls)
    assert len(result) == 2, "два кадъра, не четири снимки"
    assert all("970X1182" in u for u in result)
    assert "bbb" in result[0] and "ddd" in result[1], "хешът трябва да е този на големия"


def test_size_token_is_never_rewritten():
    """Подмяна на размера в адреса дава 404 — хешът е за конкретния размер."""
    only_small = [IMG.format(h="aaa", size="68X84", i=1)]
    assert _dedupe_images(only_small) == only_small


def test_lowercase_size_token_is_understood():
    """Вторият CDN на BestSecret пише размера с малки букви."""
    small = "https://picture.bestsecret.com/static/images/3357/image_x_68x84_0.jpg"
    big = "https://picture.bestsecret.com/static/images/3357/image_x_970x1182_0.jpg"
    assert _image_area(small) == 68 * 84
    assert _dedupe_images([small, big]) == [big]


def test_data_uris_are_dropped():
    assert _dedupe_images(["data:image/png;base64,iVBOR", ""]) == []
