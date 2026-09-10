"""Оценка от модел: има ли този артикул реален шанс да се продаде.

Скорът от `selection.py` мери сигнали около продукта — намаление, ниво на
марката, позиция в листинга. Той не вижда самия артикул, а точно там е
разликата между часовник, който върви, и очила с екстравагантна форма,
намалени с 80% именно защото никой не ги е поискал.

Затова снимката и името отиват при модел, който казва едно число: колко е
вероятно българин да купи точно този артикул на точно тази цена.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import AppraisalConfig
from .models import Product

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

SYSTEM_PROMPT = """Ти оценяваш стока за препродажба в български сайт за обяви (Bazar.bg).

Продавачът купува от европейски аутлет и препродава в България. Питането е само
едно: ако тази обява стои в Bazar.bg на посочената цена, колко е вероятно
истински български купувач да я поръча в рамките на месец-два?

Тежи по снимката и името:
- Разпознаваема ли е марката за българския пазар, или е неизвестна.
- Класическа ли е формата, или е екстравагантна, странна, силно модна за
  един сезон. Много артикули са с огромно намаление именно защото не се
  търсят и в оригиналния магазин.
- Цената спрямо това, което купувачът вижда за същите пари другаде.
- Универсален ли е артикулът (мъжки/дамски, всекидневен), или е за тесен вкус.

Отговаряй само с JSON: {"score": <цяло число 0-100>, "reason": "<до 12 думи на български>"}
0 = никой няма да го купи, 100 = продава се веднага."""


class AppraisalUnavailable(RuntimeError):
    """Моделът не отговори — мрежа, ключ или лимит. Не е присъда за продукта."""


@dataclass(slots=True)
class Appraisal:
    score: float  # 0..1, за да се сравнява с останалите прагове
    reason: str
    model: str = ""


def _payload(product: Product, price: float, currency: str, images: list[Path],
             cfg: AppraisalConfig) -> dict:
    content: list[dict] = []
    for path in images[: cfg.max_images]:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            },
        })
    content.append({
        "type": "text",
        "text": (
            f"Марка: {product.brand}\n"
            f"Име: {product.name}\n"
            f"Категория: {product.category_key}\n"
            f"Наша цена в обявата: {price:.2f} {currency}\n"
            f"Каталожна цена: {product.orig_price:.2f} {product.currency} "
            f"(намаление {product.discount_pct}%)\n"
            f"Състояние: ново, с етикет"
        ),
    })
    return {
        "model": cfg.model,
        "max_tokens": 200,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": content}],
    }


def _parse(text: str) -> Appraisal:
    """Моделът понякога слага JSON-а в изречение — вади се първата скоба."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise AppraisalUnavailable(f"отговор без JSON: {text[:120]}")
    try:
        data = json.loads(match.group(0))
        raw = float(data["score"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise AppraisalUnavailable(f"нечетим отговор: {text[:120]}") from exc
    return Appraisal(score=max(0.0, min(raw, 100.0)) / 100.0,
                     reason=str(data.get("reason", "")).strip())


async def appraise(
    product: Product,
    price: float,
    currency: str,
    images: list[Path],
    cfg: AppraisalConfig,
    api_key: str,
) -> Appraisal:
    """Едно число за един артикул. Вдига AppraisalUnavailable при всяка беда."""
    if not api_key:
        raise AppraisalUnavailable(
            "липсва ANTHROPIC_API_KEY — сложи го в .env или изключи appraisal.enabled"
        )
    if not images:
        raise AppraisalUnavailable("няма снимка за оценка")

    try:
        async with httpx.AsyncClient(timeout=cfg.timeout_s) as client:
            resp = await client.post(
                API_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": API_VERSION,
                    "content-type": "application/json",
                },
                json=_payload(product, price, currency, images, cfg),
            )
    except httpx.HTTPError as exc:
        raise AppraisalUnavailable(str(exc)) from exc

    if resp.status_code != 200:
        raise AppraisalUnavailable(f"HTTP {resp.status_code}: {resp.text[:160]}")

    body = resp.json()
    text = "".join(part.get("text", "") for part in body.get("content", []))
    verdict = _parse(text)
    verdict.model = body.get("model", cfg.model)
    return verdict
