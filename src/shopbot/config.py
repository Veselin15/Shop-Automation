"""Зареждане и валидация на конфигурацията."""

from __future__ import annotations

import os
from datetime import time
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "config.yaml"
DEFAULT_SELECTORS = REPO_ROOT / "config" / "selectors.yaml"


class RuntimeConfig(BaseModel):
    headless: bool = True
    timezone: str = "Europe/Sofia"
    active_hours: str = "09:00-22:30"
    min_action_delay_s: float = 20.0
    max_action_delay_s: float = 75.0

    @property
    def active_window(self) -> tuple[time, time]:
        start, end = self.active_hours.split("-")
        return time.fromisoformat(start.strip()), time.fromisoformat(end.strip())

    @field_validator("active_hours")
    @classmethod
    def _check_window(cls, v: str) -> str:
        start, _, end = v.partition("-")
        time.fromisoformat(start.strip())
        time.fromisoformat(end.strip())
        return v


class ScheduleConfig(BaseModel):
    discover_every_min: int = 240
    recheck_every_min: int = 90
    recheck_min_age_h: int = 6


class LimitsConfig(BaseModel):
    max_publish_per_day: int = 8
    max_publish_per_run: int = 3
    max_remove_per_day: int = 25
    # Плочки за оценка — евтино, само сравнения в паметта.
    max_tiles_per_run: int = 1500
    # Отваряния на продуктова страница — това е скъпото и то се лимитира.
    max_product_pages_per_run: int = 40
    max_active_listings: int = 60


class SourceCategory(BaseModel):
    key: str
    label: str
    # `url` е пълен адрес от BestSecret, вече с приложени филтри (категория,
    # намаление, марки). Копира се направо от адресната лента на браузъра и е
    # предпочитаният начин — филтрирането от страната на сайта спестява
    # стотици отваряния на продуктови страници.
    url: str = ""
    # Резервен вариант: само пътят, без филтри.
    path: str = ""

    def resolve(self, base_url: str) -> str:
        if self.url:
            return self.url
        return base_url.rstrip("/") + "/" + self.path.lstrip("/")

    @property
    def is_configured(self) -> bool:
        return bool(self.url or self.path)


class SourceConfig(BaseModel):
    name: str = "bestsecret"
    base_url: str = "https://www.bestsecret.com"
    categories: list[SourceCategory] = Field(default_factory=list)
    sort_query: str = ""
    max_pages_per_category: int = 3


class PopularityConfig(BaseModel):
    min_score: float = 0.45
    weights: dict[str, float] = Field(default_factory=dict)
    brand_tiers: dict[str, float] = Field(default_factory=dict)
    # Колко тежи марка, която липсва в списъка. Ниска стойност значи, че
    # непозната марка трябва да компенсира с много голямо намаление.
    unknown_brand_tier: float = 0.35


class SelectionConfig(BaseModel):
    min_discount_pct: int = 55
    min_source_price: float = 15.0
    max_source_price: float = 400.0
    min_sizes_available: int = 2
    # Аксесоарите (очила, часовници, портфейли) нямат размер. Без това
    # проверката за размери ги отхвърля всичките.
    allow_one_size: bool = True
    # Твърд под за нивото на марката. 0 = изключено.
    min_brand_tier: float = 0.0
    brands_allow: list[str] = Field(default_factory=list)
    brands_deny: list[str] = Field(default_factory=list)
    title_deny_keywords: list[str] = Field(default_factory=list)
    popularity: PopularityConfig = Field(default_factory=PopularityConfig)


class FxConfig(BaseModel):
    eur_bgn: float = 1.95583


class PricingConfig(BaseModel):
    output_currency: Literal["EUR", "BGN"] = "EUR"
    fx: FxConfig = Field(default_factory=FxConfig)
    default_markup: float = 1.45
    markup_by_category: dict[str, float] = Field(default_factory=dict)
    shipping_buffer: float = 0.0
    min_absolute_margin: float = 8.0
    charm_ending: float | None = 0.90
    min_listing_price: float = 15.0
    max_listing_price: float = 900.0


class ListingConfig(BaseModel):
    location: str = "София"
    # Ключ от source.categories -> числово id на рубриката в Bazar.bg.
    # Числото е това, което формата праща; имената се сменят, id-тата не.
    category_map: dict[str, int] = Field(default_factory=dict)
    # Българска дума пред заглавието, за да се намира обявата при търсене.
    title_prefix: dict[str, str] = Field(default_factory=dict)
    # Полета, които Bazar.bg показва чак след избор на рубрика.
    # Еднакви за всички обяви (състояние, кой плаща доставката).
    form_defaults: dict[str, str] = Field(default_factory=dict)
    # Различни по категория — ключът е от source.categories, защото
    # "Вид" е Мъжки/Дамски, а мъжките и дамските очила са в една рубрика.
    category_attributes: dict[str, dict[str, str]] = Field(default_factory=dict)
    # Цветът е задължителен при чанти и портфейли. BestSecret не го дава в
    # отделно поле, затова се търси по дума в заглавието и описанието:
    # дума в текста -> опция в Bazar.bg.
    color_map: dict[str, str] = Field(default_factory=dict)
    # Какво да пише, когато в текста няма цвят. Празно = обявата се проваля
    # шумно, вместо да получи грешен цвят.
    color_fallback: str = ""
    phone: str = ""
    condition: str = "Ново"
    max_images: int = 6
    title_template: str = "{brand} {name} - {size_hint}"
    title_max_len: int = 70
    description_template: str = ""
    # Условията за предварителна заявка. Купувачът трябва да ги види в обявата,
    # преди да поръча — иначе следват спорове за срока.
    delivery_note: str = ""
    extra_note: str = ""


class BazarConfig(BaseModel):
    """Как ботът влиза в Bazar.bg."""

    # Автоматичен вход с парола. Изключва се, когато профилът има включена
    # двуфакторна аутентикация: API-то връща requires_2fa и всеки опит само
    # поръчва нов код, без изобщо да може да мине.
    password_login: bool = True


class RemovalConfig(BaseModel):
    auto_remove: bool = True
    remove_below_discount_pct: int = 45
    misses_before_removal: int = 2


class NotificationsConfig(BaseModel):
    on_publish: bool = True
    on_remove: bool = True
    on_error: bool = True
    on_auth_wall: bool = True


class Secrets(BaseModel):
    bestsecret_email: str = ""
    bestsecret_password: str = ""
    bazar_email: str = ""
    bazar_password: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


class Config(BaseModel):
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    source: SourceConfig = Field(default_factory=SourceConfig)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    listing: ListingConfig = Field(default_factory=ListingConfig)
    bazar: BazarConfig = Field(default_factory=BazarConfig)
    removal: RemovalConfig = Field(default_factory=RemovalConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)

    # Попълва се извън YAML-а.
    secrets: Secrets = Field(default_factory=Secrets)
    selectors: dict[str, Any] = Field(default_factory=dict)
    data_dir: Path = REPO_ROOT / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "shopbot.db"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    def session_file(self, site: str) -> Path:
        """Изнесената сесия за сайта.

        Тя, а не профилът на Chromium, е източникът на истината: входът в
        BestSecret е session cookie, която живее само в паметта и изчезва при
        затваряне на браузъра. Профилът я губи, JSON-ът я пази.
        """
        return self.sessions_dir / f"{site}.json"


def load_config(
    path: Path | str | None = None, selectors_path: Path | str | None = None
) -> Config:
    load_dotenv(REPO_ROOT / ".env")

    cfg_path = Path(path or DEFAULT_CONFIG)
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    sel_path = Path(selectors_path or DEFAULT_SELECTORS)
    selectors = yaml.safe_load(sel_path.read_text(encoding="utf-8")) or {}

    cfg = Config(**raw)
    cfg.selectors = selectors
    cfg.secrets = Secrets(
        bestsecret_email=os.getenv("BESTSECRET_EMAIL", ""),
        bestsecret_password=os.getenv("BESTSECRET_PASSWORD", ""),
        bazar_email=os.getenv("BAZAR_EMAIL", ""),
        bazar_password=os.getenv("BAZAR_PASSWORD", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )
    cfg.data_dir = Path(os.getenv("SHOPBOT_DATA_DIR", str(REPO_ROOT / "data"))).resolve()

    if os.getenv("SHOPBOT_HEADLESS") is not None:
        cfg.runtime.headless = os.getenv("SHOPBOT_HEADLESS", "1") not in ("0", "false", "False")

    for d in (cfg.data_dir, cfg.images_dir, cfg.profiles_dir):
        d.mkdir(parents=True, exist_ok=True)

    return cfg
