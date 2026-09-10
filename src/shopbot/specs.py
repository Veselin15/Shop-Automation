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

# Заглавията на секциите стоят между полетата и се лепят за стойността
# отпред ("plastic Fit & Measurements"), затова стойността се реже на тях.
SECTIONS = re.compile(
    r"\s*(product information|fit & measurements|materials & care|"
    r"information on product safety|please note|item number).*$",
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
    "pockets": "Джобове",
    "extras": "Детайли",
    "measurements": "Размери",
    "measurements for one size": "Размери",
    "details": "Детайли",
    "model name": "Модел",
    "shape": "Форма",
    "size": "Размер",
    "care instructions": "Поддръжка",
    "wristband": "Каишка",
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
    "plastic": "пластмаса",
    "acetate": "ацетат",
    "metal": "метал",
    "titanium": "титан",
    "canvas": "плат",
    "suede": "велур",
    # Стойностите долу са изчетени от самите продукти, а не измислени —
    # непреведена английска дума по средата на българска обява личи веднага.
    "clip closure": "щипкова закопчалка",
    "pin buckle": "катарама с щифт",
    "butterfly clasp": "закопчалка тип пеперуда",
    "buckle": "класическа катарама",
    "continuous zip": "цип по цялата дължина",
    "zip fastening": "закопчаване с цип",
    "zipper": "цип",
    "zip": "цип",
    "overlapping flap with press stud(s)": "капак с тик-так копче",
    "press stud(s)": "тик-так копче",
    "magnetic": "магнитно",
    "uni-colour": "едноцветен",
    "movement": "механизъм",
    "swiss": "швейцарски",
    "bovine": "телешка",
    "various pockets and card compartments": "джобове и отделения за карти",
    "inside zipper pocket": "вътрешен джоб с цип",
    "card slot": "отделение за карти",
    "pocket(s)": "джоб(ове)",
    "watch case": "кутия за часовник",
    "case included": "с включена кутия",
    "gift box": "подаръчна кутия",
    "logo appliqué(s)": "апликация с лого",
    "quilted": "капитониран",
    "logo": "лого",
    "hand-wash": "ръчно пране",
    "on both temples": "от двете страни",
    "wristband": "каишка",
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

    # Границата е "не-буква", а не \b: изразът \bpocket\(s\)\b не съвпада с
    # нищо, защото след затварящата скоба няма буква, до която да има граница.
    for en, bg in sorted(VALUES.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"(?<!\w){re.escape(en)}(?!\w)", bg, text, flags=re.IGNORECASE)
    for en, bg in UNITS.items():
        text = re.sub(rf"(?<=\d)\s*{en}\b", f" {bg}", text, flags=re.IGNORECASE)
    return text.strip()


def parse_specs(raw: str, limit: int = 8) -> list[tuple[str, str]]:
    """Двойки (българско име, стойност) от текста на продуктовата страница.

    Текстът идва ту на редове, ту слят в едно изречение — акордеонът на
    BestSecret е свит и тогава `innerText` не дава нови редове. Затова не се
    разчита на редове: търсят се самите познати ключове, а стойността е
    всичко до следващия ключ.
    """
    text = " ".join((raw or "").split())
    if not text:
        return []

    pattern = "|".join(
        re.escape(k) for k in sorted(KEYS, key=len, reverse=True)
    )
    matches = list(re.finditer(rf"({pattern})\s*:", text, re.IGNORECASE))

    specs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for i, m in enumerate(matches):
        bg = KEYS[m.group(1).lower()]
        if bg in seen:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[m.end():end].strip(" .,;")

        # Между две полета стои и свободен текст — заглавие на секция или
        # "Sunglasses by Carrera". Нито едното не е част от стойността.
        value = SECTIONS.sub("", value)
        value = re.split(r"(?<=[a-zа-я])\s+(?=[A-ZА-Я][a-zа-я]+\s+by)", value)[0]
        # Непознато поле също стои залепено за предната стойност ("цип
        # Extras: logo appliqué"). Не го превеждаме, но и не го влачим.
        value = re.split(r"\s+(?=[A-Z][A-Za-z0-9'&\-/ ]{1,28}:)", value)[0]
        value = value.strip(" .,;")
        if not value or len(value) > 160:
            continue
        seen.add(bg)
        specs.append((bg, _translate_value(value)))
        if len(specs) >= limit:
            break
    return specs


def format_specs(specs: list[tuple[str, str]]) -> str:
    return "\n".join(f"• {k}: {v}" for k, v in specs)
