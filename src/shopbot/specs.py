"""Спецификациите от продуктовата страница, преведени на български.

BestSecret дава истински данни за артикула — механизъм, водоустойчивост,
материя, размери. Те правят разликата между обява от два реда и обява, на
която купувачът вярва. Тук само се превеждат; нищо не се съчинява, защото
измислена характеристика в обява е обещание, което после ти плащаш.

Редовете идват като "Ключ: стойност" на английски. Ключ, който не познаваме,
отпада — по-добре по-късо описание, отколкото английщина по средата.
"""

from __future__ import annotations

import re

# Заглавия на секции и редове, които не бива да влизат в обявата:
# вътрешни номера на източника и неговите правила за връщане.
SKIP_LINES = re.compile(
    r"^(product information|fit & measurements|materials & care|"
    r"information on product safety|item number|please note)",
    re.IGNORECASE,
)

KEYS = {
    "manufacturer's item number": "Модел",
    "bracelet colour": "Цвят на каишката",
    "case colour": "Цвят на корпуса",
    "fastening": "Закопчаване",
    "water resistance": "Водоустойчивост",
    "clockwork": "Механизъм",
    "movement": "Механизъм",
    "material": "Материя",
    "materials": "Материя",
    "case and clasp": "Корпус и закопчалка",
    "lining": "Подплата",
    "outer material": "Външен материал",
    "inner material": "Вътрешен материал",
    "lens": "Стъкла",
    "lenses": "Стъкла",
    "frame": "Рамка",
    "frame colour": "Цвят на рамката",
    "lens colour": "Цвят на стъклата",
    "uv protection": "UV защита",
    "colour": "Цвят",
    "pattern": "Десен",
    "closure": "Закопчаване",
    "compartments": "Отделения",
    "measurements": "Размери",
    "measurements for one size": "Размери",
    "details": "Детайли",
    "care instructions": "Поддръжка",
}

VALUES = {
    "japanese clockwork": "японски механизъм",
    "quartz": "кварцов",
    "automatic": "автоматичен",
    "stainless steel": "неръждаема стомана",
    "real leather": "естествена кожа",
    "leather": "естествена кожа",
    "genuine leather": "естествена кожа",
    "textile": "текстил",
    "cotton": "памук",
    "wool": "вълна",
    "cashmere": "кашмир",
    "silk": "коприна",
    "polyester": "полиестер",
    "polyurethane": "полиуретан",
    "clip closure": "щипкова закопчалка",
    "buckle": "класическа катарама",
    "zip": "цип",
    "zip fastening": "закопчаване с цип",
    "magnetic": "магнитно",
    "silver": "сребрист",
    "gold": "златист",
    "black": "черен",
    "white": "бял",
    "brown": "кафяв",
    "blue": "син",
    "green": "зелен",
    "red": "червен",
    "beige": "бежов",
    "grey": "сив",
    "gray": "сив",
    "rose gold": "розово злато",
    "case diameter": "диаметър на корпуса",
    "case height": "дебелина на корпуса",
    "bracelet width": "ширина на каишката",
    "bracelet length": "дължина на каишката",
    "width": "ширина",
    "height": "височина",
    "length": "дължина",
    "depth": "дълбочина",
}


UNITS = {"cm": "см", "mm": "мм", "kg": "кг"}


def _translate_value(value: str) -> str:
    """Превежда каквото знае и не пипа останалото.

    Регистърът се пази: "MC2-B252S" е номерът на модела, а "3 ATM" се пише с
    главни — превод, който ги смачква в малки букви, изглежда като грешка.
    """
    text = value.strip()
    if text.lower() in VALUES:
        return VALUES[text.lower()]

    for en, bg in sorted(VALUES.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"\b{re.escape(en)}\b", bg, text, flags=re.IGNORECASE)
    for en, bg in UNITS.items():
        text = re.sub(rf"(?<=\d)\s*{en}\b", f" {bg}", text, flags=re.IGNORECASE)
    return text.strip()


def parse_specs(raw: str, limit: int = 8) -> list[tuple[str, str]]:
    """Двойки (българско име, стойност) от текста на продуктовата страница."""
    specs: list[tuple[str, str]] = []
    seen: set[str] = set()

    for line in (raw or "").splitlines():
        line = " ".join(line.split())
        if not line or SKIP_LINES.match(line):
            continue
        key, sep, value = line.partition(":")
        if not sep or not value.strip():
            continue
        bg = KEYS.get(key.strip().lower())
        if not bg or bg in seen:
            continue
        seen.add(bg)
        specs.append((bg, _translate_value(value)))
        if len(specs) >= limit:
            break
    return specs


def format_specs(specs: list[tuple[str, str]]) -> str:
    return "\n".join(f"• {k}: {v}" for k, v in specs)
