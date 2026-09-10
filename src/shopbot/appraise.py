"""Оценка от модел: има ли този артикул реален шанс да се продаде.

Скорът от `selection.py` мери сигнали около продукта — намаление, ниво на
марката, позиция в листинга. Той не вижда самия артикул, а точно там е
разликата между часовник, който върви, и очила с екстравагантна форма,
намалени с 80% именно защото никой не ги е поискал.

Затова снимката и името отиват при модел, който казва едно число: колко е
вероятно българин да купи точно този артикул на точно тази цена.

Три доставчика, за да има и безплатен път:
  gemini    — безплатният таван на Google AI Studio (иска само ключ);
  ollama    — модел, който върви на самия сървър, без сметка и без интернет;
  anthropic — платено, когато качеството на оценката си струва.
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

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

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


def _facts(product: Product, price: float, currency: str) -> str:
    return (
        f"Марка: {product.brand}\n"
        f"Име: {product.name}\n"
        f"Категория: {product.category_key}\n"
        f"Наша цена в обявата: {price:.2f} {currency}\n"
        f"Каталожна цена: {product.orig_price:.2f} {product.currency} "
        f"(намаление {product.discount_pct}%)\n"
        f"Състояние: ново, с етикет"
    )


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


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


async def _post(url: str, *, headers: dict, payload: dict, timeout: float) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise AppraisalUnavailable(str(exc)) from exc
    if resp.status_code != 200:
        # Дълъг откъс: Google слага заместващия модел чак в края на текста.
        raise AppraisalUnavailable(f"HTTP {resp.status_code}: {resp.text[:600]}")
    return resp.json()


# --------------------------------------------------------------- доставчици


async def _call_anthropic(facts: str, images: list[Path], cfg: AppraisalConfig,
                          api_key: str) -> Appraisal:
    if not api_key:
        raise AppraisalUnavailable(
            "липсва ANTHROPIC_API_KEY — сложи го в .env или смени appraisal.provider"
        )
    content: list[dict] = [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(p)}}
        for p in images
    ]
    content.append({"type": "text", "text": facts})
    body = await _post(
        ANTHROPIC_URL,
        headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION,
                 "content-type": "application/json"},
        payload={"model": cfg.model, "max_tokens": 300, "system": SYSTEM_PROMPT,
                 "messages": [{"role": "user", "content": content}]},
        timeout=cfg.timeout_s,
    )
    verdict = _parse("".join(part.get("text", "") for part in body.get("content", [])))
    verdict.model = body.get("model", cfg.model)
    return verdict


async def _call_gemini(facts: str, images: list[Path], cfg: AppraisalConfig,
                       api_key: str) -> Appraisal:
    """Безплатният таван на Google AI Studio. Ключът се взима без карта."""
    if not api_key:
        raise AppraisalUnavailable(
            "липсва GEMINI_API_KEY — вземи безплатен ключ от aistudio.google.com "
            "или смени appraisal.provider на 'ollama'"
        )
    parts: list[dict] = [
        {"inline_data": {"mime_type": "image/jpeg", "data": _b64(p)}} for p in images
    ]
    parts.append({"text": facts})
    body = await _post(
        GEMINI_URL.format(model=cfg.model),
        headers={"x-goog-api-key": api_key, "content-type": "application/json"},
        payload={
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                # Разсъждаващите модели ядат от този таван, преди да
                # стигнат до отговора — оттам празни parts.
                "maxOutputTokens": 2048,
                "temperature": 0,
                "responseMimeType": "application/json",
            },
        },
        timeout=cfg.timeout_s,
    )
    candidates = body.get("candidates") or []
    if not candidates:
        # Празен списък значи блокиран или отрязан отговор, не лош продукт.
        raise AppraisalUnavailable(f"празен отговор: {json.dumps(body)[:300]}")
    text = "".join(
        part.get("text", "") for part in candidates[0].get("content", {}).get("parts", [])
    )
    if not text.strip():
        reason = candidates[0].get("finishReason", "?")
        raise AppraisalUnavailable(f"отговор без текст (finishReason={reason})")
    verdict = _parse(text)
    verdict.model = cfg.model
    return verdict


async def _call_ollama(facts: str, images: list[Path], cfg: AppraisalConfig) -> Appraisal:
    """Модел на самия сървър: без сметка, без ключ, без трафик навън."""
    body = await _post(
        cfg.ollama_url.rstrip("/") + "/api/chat",
        headers={"content-type": "application/json"},
        payload={
            "model": cfg.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": facts, "images": [_b64(p) for p in images]},
            ],
        },
        timeout=cfg.timeout_s,
    )
    verdict = _parse(body.get("message", {}).get("content", ""))
    verdict.model = body.get("model", cfg.model)
    return verdict


async def appraise(
    product: Product,
    price: float,
    currency: str,
    images: list[Path],
    cfg: AppraisalConfig,
    api_key: str = "",
) -> Appraisal:
    """Едно число за един артикул. Вдига AppraisalUnavailable при всяка беда."""
    if not images:
        raise AppraisalUnavailable("няма снимка за оценка")

    facts = _facts(product, price, currency)
    picked = images[: cfg.max_images]

    if cfg.provider == "gemini":
        return await _call_gemini(facts, picked, cfg, api_key)
    if cfg.provider == "ollama":
        return await _call_ollama(facts, picked, cfg)
    if cfg.provider == "anthropic":
        return await _call_anthropic(facts, picked, cfg, api_key)
    raise AppraisalUnavailable(f"непознат доставчик: {cfg.provider}")
