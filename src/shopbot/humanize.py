"""Темпо и лимити.

Целта не е да заблуждаваме нечия защита, а обратното: ботът да не залива
двата сайта със заявки и да не публикува 40 обяви за 5 минути, което е
най-бързият начин да ти замразят профила в Bazar.bg.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import RuntimeConfig

log = logging.getLogger(__name__)


def _zone(name: str) -> tzinfo:
    """Windows няма системна tz база; пакетът `tzdata` я носи, но не разчитаме на него."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError):
        log.warning("Няма часова зона '%s' — минавам на UTC. Инсталирай пакета tzdata.", name)
        return UTC


class Pacer:
    def __init__(self, cfg: RuntimeConfig) -> None:
        self.cfg = cfg
        self.tz = _zone(cfg.timezone)

    async def pause(self, factor: float = 1.0) -> None:
        """Пауза между две действия в един и същ сайт."""
        delay = random.uniform(self.cfg.min_action_delay_s, self.cfg.max_action_delay_s) * factor
        log.debug("пауза %.1fs", delay)
        await asyncio.sleep(delay)

    async def publish_pause(self) -> None:
        """Паузата между две обяви — отделна и много по-дълга.

        Bazar.bg показа проверка „не сте робот" след деветнайсет обяви за
        четири часа. Няколко минути между обявите излизат по-евтино от един
        замразен профил.
        """
        delay = random.uniform(self.cfg.min_publish_delay_s, self.cfg.max_publish_delay_s)
        log.info("пауза преди следващата обява: %.0f сек", delay)
        await asyncio.sleep(delay)

    async def micro_pause(self) -> None:
        """Кратка пауза между полета в една форма."""
        await asyncio.sleep(random.uniform(0.4, 1.8))

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def within_active_hours(self, at: datetime | None = None) -> bool:
        at = at or self.now()
        start, end = self.cfg.active_window
        current = at.time()
        if start <= end:
            return start <= current <= end
        # Прозорец през полунощ, напр. 22:00-02:00.
        return current >= start or current <= end

    def seconds_until_active(self, at: datetime | None = None) -> float:
        at = at or self.now()
        if self.within_active_hours(at):
            return 0.0
        start, _ = self.cfg.active_window
        target = at.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        if target <= at:
            target += timedelta(days=1)
        return max((target - at).total_seconds(), 0.0)


class DailyLimiter:
    """Тънка обвивка над брояча в базата, за да не се дублира логиката."""

    def __init__(self, db, kind: str, cap: int) -> None:
        self.db = db
        self.kind = kind
        self.cap = cap

    @property
    def used(self) -> int:
        return self.db.counter(self.kind)

    @property
    def remaining(self) -> int:
        return max(self.cap - self.used, 0)

    def allow(self) -> bool:
        return self.remaining > 0

    def consume(self, amount: int = 1) -> None:
        self.db.bump_counter(self.kind, amount)
