"""Известия по Telegram. Ако не е конфигуриран, всичко отива само в лога."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_UPDATES = "https://api.telegram.org/bot{token}/getUpdates"
TELEGRAM_ME = "https://api.telegram.org/bot{token}/getMe"
MAX_LEN = 3800


class TelegramUnreachable(RuntimeError):
    """Не стигнахме до Telegram — мрежа, DNS или сертификати, не токенът."""


class TelegramRejected(RuntimeError):
    """Telegram отговори, но отказа заявката (най-често грешен токен)."""


async def _get(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
        return resp.json()
    except httpx.HTTPError as exc:
        raise TelegramUnreachable(str(exc)) from exc


async def check_token(token: str) -> str:
    """Връща потребителското име на бота или вдига грешка с обяснение."""
    data = await _get(TELEGRAM_ME.format(token=token))
    if not data.get("ok"):
        raise TelegramRejected(data.get("description", "Telegram отказа токена"))
    return data["result"].get("username", "?")


async def discover_chats(token: str) -> list[tuple[str, str]]:
    """Намира chat id-тата, писали на бота. Връща [(id, описание)].

    Telegram показва разговор в getUpdates само след като някой пише на бота
    пръв — затова стъпката „пусни /start на бота" не е по избор.
    """
    data = await _get(TELEGRAM_UPDATES.format(token=token))
    if not data.get("ok"):
        raise TelegramRejected(data.get("description", "Telegram отказа заявката"))

    found: dict[str, str] = {}
    for update in data.get("result", []):
        message = (
            update.get("message")
            or update.get("channel_post")
            or update.get("my_chat_member")
            or {}
        )
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        name = (
            chat.get("title")
            or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            or chat.get("username")
            or "без име"
        )
        found[str(chat_id)] = f"{name} ({chat.get('type', '?')})"
    return sorted(found.items())


class Notifier:
    def __init__(self, token: str = "", chat_id: str = "") -> None:
        self.token = token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, text: str) -> None:
        log.info("notify: %s", text.replace("\n", " | ")[:200])
        if not self.enabled:
            return
        payload = {
            "chat_id": self.chat_id,
            "text": text[:MAX_LEN],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(TELEGRAM_API.format(token=self.token), json=payload)
                if resp.status_code != 200:
                    log.warning("Telegram отказа (%s): %s", resp.status_code, resp.text[:200])
        except Exception as exc:  # известието никога не бива да събаря бота
            log.warning("Известието не тръгна: %s", exc)

    async def published(self, title: str, price: str, url: str,
                        source_url: str = "", cost: str = "") -> None:
        """Двата адреса вървят заедно: обявата и продуктът, от който идва.

        Поръчката се прави ръчно в BestSecret, затова адресът там трябва да е
        под ръка още в известието, а не да се търси после по име.
        """
        lines = ["🟢 Публикувано", f"<b>{title}</b>", price]
        if cost:
            lines.append(f"себестойност {cost}")
        lines.append(url)
        if source_url:
            lines.append(f'<a href="{source_url}">➡️ Отвори в BestSecret</a>')
        await self.send("\n".join(lines))

    async def removed(self, title: str, reason: str, url: str = "") -> None:
        """Адресът върви и тук: показва коя точно обява е паднала.

        Самата страница вече не се отваря — Bazar.bg пренасочва изтритите —
        но номерът в адреса е достатъчен, за да се намери обявата в профила.
        """
        lines = ["🔴 Свалено", f"<b>{title}</b>", f"Причина: {reason}"]
        if url:
            lines.append(url)
        await self.send("\n".join(lines))

    async def auth_wall(self, site: str) -> None:
        await self.send(
            f"🔐 <b>{site}</b> иска ръчен вход.\n"
            f"Ботът е на пауза за този сайт.\n"
            f"Пусни: <code>shopbot login {site}</code>"
        )

    async def error(self, context: str, message: str) -> None:
        await self.send(f"⚠️ Грешка в {context}\n<code>{message[:600]}</code>")
