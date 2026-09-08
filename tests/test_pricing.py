from shopbot.config import PricingConfig
from shopbot.models import Product, Size
from shopbot.pricing import apply_charm, compute_price, convert


def make_product(price=100.0, orig=300.0, category="women_clothing"):
    return Product(
        id="1",
        url="http://x/1",
        brand="Hugo Boss",
        name="Риза",
        category_key=category,
        price=price,
        orig_price=orig,
        currency="EUR",
        images=["a.jpg"],
        sizes=[Size("M"), Size("L")],
    )


def test_charm_rounds_up_never_down():
    assert apply_charm(34.17, 0.90) == 34.90
    assert apply_charm(34.95, 0.90) == 35.90
    assert apply_charm(35.00, 0.90) == 35.90
    assert apply_charm(34.90, 0.90) == 34.90


def test_charm_disabled_keeps_two_decimals():
    assert apply_charm(34.176, None) == 34.18


def test_convert_eur_bgn_roundtrip():
    assert convert(10.0, "EUR", "EUR", 1.95583) == 10.0
    bgn = convert(10.0, "EUR", "BGN", 1.95583)
    assert round(convert(bgn, "BGN", "EUR", 1.95583), 6) == 10.0


def test_markup_applied_per_category():
    cfg = PricingConfig(
        default_markup=1.45,
        markup_by_category={"women_shoes": 1.20},
        shipping_buffer=0.0,
        min_absolute_margin=0.0,
        charm_ending=None,
    )
    clothing = compute_price(make_product(category="women_clothing"), cfg)
    shoes = compute_price(make_product(category="women_shoes"), cfg)
    assert clothing.final == 145.0
    assert shoes.final == 120.0


def test_minimum_absolute_margin_wins_on_cheap_items():
    cfg = PricingConfig(
        default_markup=1.10,
        shipping_buffer=0.0,
        min_absolute_margin=8.0,
        charm_ending=None,
        min_listing_price=0.0,
    )
    price = compute_price(make_product(price=20.0, orig=100.0), cfg)
    # 10% от 20 е само 2 лв марж, затова се вдига до +8.
    assert price.final == 28.0
    assert price.margin == 8.0


def test_shipping_buffer_enters_the_cost_base():
    cfg = PricingConfig(
        default_markup=2.0,
        shipping_buffer=5.0,
        min_absolute_margin=0.0,
        charm_ending=None,
    )
    price = compute_price(make_product(price=10.0, orig=100.0), cfg)
    assert price.cost == 15.0
    assert price.final == 30.0


def test_rejects_price_above_catalogue():
    cfg = PricingConfig(
        default_markup=5.0, shipping_buffer=0.0, min_absolute_margin=0.0, charm_ending=None
    )
    price = compute_price(make_product(price=100.0, orig=300.0), cfg)
    assert price.rejected
    assert "каталожната" in price.rejected


def test_rejects_below_minimum_listing_price():
    cfg = PricingConfig(
        default_markup=1.1,
        shipping_buffer=0.0,
        min_absolute_margin=0.0,
        charm_ending=None,
        min_listing_price=50.0,
    )
    price = compute_price(make_product(price=10.0, orig=100.0), cfg)
    assert "под минимума" in price.rejected


def test_bgn_output_converts_both_prices():
    cfg = PricingConfig(
        output_currency="BGN",
        default_markup=1.0,
        shipping_buffer=0.0,
        min_absolute_margin=0.0,
        charm_ending=None,
        max_listing_price=10_000,
    )
    price = compute_price(make_product(price=100.0, orig=300.0), cfg)
    assert price.currency == "BGN"
    assert round(price.final, 2) == 195.58
    assert round(price.orig_price, 2) == 586.75
