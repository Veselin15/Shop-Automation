import pytest

from shopbot.config import ListingConfig, load_config
from shopbot.listing import build_title, clean_source_text, size_hint
from shopbot.models import Product, Size
from shopbot.parsing import parse_money, parse_percent, product_id_from_url

# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize(
    "text,expected,currency",
    [
        ("€ 49,95", 49.95, "EUR"),
        ("49.95 €", 49.95, "EUR"),
        ("€ 1.299,00", 1299.0, "EUR"),
        ("1,299.00 EUR", 1299.0, "EUR"),
        ("129,90 лв.", 129.90, "BGN"),
        ("EUR 8", 8.0, "EUR"),
        ("няма цена", 0.0, ""),
    ],
)
def test_parse_money(text, expected, currency):
    value, cur = parse_money(text)
    assert value == pytest.approx(expected)
    assert cur == currency


def test_thousand_separator_without_decimals():
    assert parse_money("1.299 €")[0] == pytest.approx(1299.0)


def test_parse_percent():
    assert parse_percent("-70%") == 70
    assert parse_percent("без намаление") == 0


def test_product_id_prefers_article_number():
    assert product_id_from_url("https://x.com/p/nike-tee-12345678.htm") == "12345678"
    assert product_id_from_url("https://x.com/p/nike-tee?color=red") == "nike-tee"


# ------------------------------------------------------------------ listing


def listing_cfg(**overrides):
    base = dict(
        title_template="{brand} {name} - {size_hint}",
        title_max_len=40,
        description_template=(
            "{brand} — {name}\n{color_line}{size_line}"
            "Каталожна цена: {orig_price} — при мен: {price}\n{extra_note}"
        ),
        extra_note="Пиши на лично.",
    )
    base.update(overrides)
    return ListingConfig(**base)


def product(**overrides):
    base = dict(
        id="1",
        url="http://x/1",
        brand="Hugo Boss",
        name="Мъжка риза с дълъг ръкав",
        sizes=[Size("M"), Size("L"), Size("XL", available=False)],
    )
    base.update(overrides)
    return Product(**base)


def test_source_name_is_stripped_from_text():
    assert "BestSecret" not in clean_source_text("Купено от BestSecret на добра цена")
    assert "Best Secret" not in clean_source_text("Best Secret оферта")


def test_title_is_cut_on_a_word_boundary():
    title = build_title(product(), listing_cfg(title_max_len=25))
    assert len(title) <= 25
    assert not title.endswith(" ")
    assert " " in title


def test_title_keeps_short_titles_intact():
    title = build_title(product(name="Риза"), listing_cfg())
    assert title.startswith("Hugo Boss Риза")


def test_size_hint_only_lists_available_sizes():
    hint = size_hint(product())
    assert "XL" not in hint
    assert "M" in hint and "L" in hint


def test_size_hint_collapses_long_lists():
    p = product(sizes=[Size(s) for s in ["XS", "S", "M", "L", "XL", "XXL"]])
    assert size_hint(p, limit=4).endswith("и др.")


def test_content_hash_reacts_to_price_and_sizes():
    a = product()
    b = product()
    assert a.content_hash() == b.content_hash()
    b.price = 99.0
    assert a.content_hash() != b.content_hash()

    c = product()
    c.sizes = [Size("M")]
    assert a.content_hash() != c.content_hash()


def test_discount_pct_is_zero_when_not_discounted():
    assert product(price=100.0, orig_price=100.0).discount_pct == 0
    assert product(price=50.0, orig_price=200.0).discount_pct == 75


def test_every_category_url_carries_the_discount_filter():
    """Филтърът на BestSecret е част от стратегията, не украса.

    Без него ботът обхожда 456 плочки на категория, за да намери три
    подходящи; с него листингът връща само намаленото 70%+.
    """
    cfg = load_config()
    for category in cfg.source.categories:
        url = category.resolve(cfg.source.base_url)
        assert "filterParam_relativeSavingRanges=" in url, category.key
        floor = url.split("filterParam_relativeSavingRanges=")[1].split("-")[0]
        assert float(floor) >= cfg.selection.min_discount_pct, category.key


def test_paging_keeps_the_filter_and_its_star():
    """BestSecret връща празен листинг, ако звездичката дойде като %2A."""
    from shopbot.sources.bestsecret import _paged_url

    base = (
        "https://www.bestsecret.com/category.htm?area=WOMEN_ACCESSORIES"
        "&category=women_accessoires_uhren&filterParam_relativeSavingRanges=70.0-*"
    )
    paged = _paged_url(base, 3)
    assert "filterParam_relativeSavingRanges=70.0-*" in paged
    assert "page=3" in paged
