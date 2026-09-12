"""Съвпадения по цяла дума, не по парче от дума."""

from shopbot.config import ListingConfig
from shopbot.listing import detect_color
from shopbot.selection import brand_tier


def test_a_short_brand_does_not_match_inside_another_word():
    tiers = {"Etro": 0.78, "Boss": 1.0}
    assert brand_tier("Retrosuperfuture", tiers) == 0.35
    assert brand_tier("Bossi", tiers) == 0.35
    assert brand_tier("BOSS Orange", tiers) == 1.0


def test_colour_next_to_punctuation_is_still_found():
    cfg = ListingConfig(color_map={"brown": "Кафеви", "black": "Черни"}, color_fallback="Черни")
    assert detect_color("Colour: brown. Pattern: uni-colour", cfg) == "Кафеви"
    assert detect_color("Leather wallet (brown)", cfg) == "Кафеви"
