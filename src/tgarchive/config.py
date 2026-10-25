"""Konfiguratsiya: muhit o'zgaruvchilari va .env fayldan o'qiladi.

Uch sinf bor:

* ``BaseAppSettings`` — har ikkala komponentga umumiy (yo'llar, baza, loglar).
* ``ArchiverSettings`` — asosiy akkaunt (MTProto) uchun: API_ID/API_HASH, siyosatlar.
* ``BotSettings`` — admin bot uchun: BOT_TOKEN, OWNER_ID.

Bot jarayoni ``ArchiverSettings`` ni yuklamaydi, shuning uchun unga TG_API_HASH va
session fayli umuman kerak emas (minimal huquq tamoyili).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import URL

from tgarchive.db.enums import FILE_MEDIA_TYPES, MediaType

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

_API_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
_BOT_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


class ConfigError(Exception):
    """Konfiguratsiya xatosi. Xabarda maxfiy qiymatlar bo'lmaydi."""


@dataclass(frozen=True, slots=True)
class AppPaths:
    data: Path
    db: Path
    media: Path
    exports: Path
    backups: Path
    sessions: Path


def _split_csv(value: Any) -> Any:
    """``"1, -100200"`` ko'rinishidagi qatorni ro'yxatga aylantiradi."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class BaseAppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # "HISTORY_MAX_AGE_DAYS=" kabi bo'sh qator — berilmagan deb hisoblanadi (sukut qiymati)
        env_ignore_empty=True,
    )

    data_dir: Path = Path("data")
    db_path: Path | None = None
    media_dir: Path | None = None
    export_dir: Path | None = None
    backup_dir: Path | None = None

    log_level: LogLevel = "INFO"
    log_format: Literal["json", "console"] = "json"
    db_busy_timeout_ms: int = Field(10_000, ge=100, le=600_000)
    display_timezone: str = "Asia/Tashkent"
    retention_days: int | None = Field(None, ge=1)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("display_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("noma'lum vaqt mintaqasi (masalan: Asia/Tashkent)") from None
        return value

    @property
    def paths(self) -> AppPaths:
        data = self.data_dir.expanduser().resolve()

        def pick(explicit: Path | None, default: Path) -> Path:
            return (explicit or default).expanduser().resolve()

        return AppPaths(
            data=data,
            db=pick(self.db_path, data / "db" / "archive.sqlite3"),
            media=pick(self.media_dir, data / "media"),
            exports=pick(self.export_dir, data / "exports"),
            backups=pick(self.backup_dir, data / "backups"),
            sessions=data / "session",
        )

    @property
    def database_url(self) -> URL:
        return URL.create("sqlite+aiosqlite", database=str(self.paths.db))

    def secret_values(self) -> list[str]:
        """Loglarda yashirilishi kerak bo'lgan qiymatlar."""
        return []


class ArchiverSettings(BaseAppSettings):
    tg_api_id: int = Field(gt=0)
    tg_api_hash: SecretStr
    tg_session_name: str = Field("archiver", pattern=r"^[A-Za-z0-9_-]{1,64}$")

    archive_private: bool = True
    archive_bots: bool = False
    archive_saved: bool = True
    archive_groups: bool = True
    archive_channels: bool = True
    chat_allowlist: Annotated[list[int], NoDecode] = []
    chat_denylist: Annotated[list[int], NoDecode] = []

    history_enabled: bool = True
    history_page_size: int = Field(100, ge=1, le=100)
    history_page_delay_s: float = Field(1.0, ge=0, le=60)
    history_max_age_days: int | None = Field(None, ge=1)
    flood_sleep_threshold_s: int = Field(60, ge=0, le=86_400)
    max_flood_wait_s: int = Field(3600, ge=60)
    # Dialoglar ro'yxati va yangi xabarlarni tekshirish oralig'i (real vaqt bilan birga — solishtirish)
    sync_interval_s: int = Field(600, ge=60, le=86_400)
    # Real vaqt hodisalari: yangi xabar, tahrir, o'chirish
    realtime_enabled: bool = True

    # Kanallar: faqat ma'lumoti va oxirgi N ta post (0 — to'liq tarix).
    # Izohlar (postlarga reply'lar) har biri saqlanadi.
    channel_keep_posts: int = Field(20, ge=0, le=10_000)
    channel_save_comments: bool = True
    # Yangi post bo'lmasa ham kanal oynasi (tahrirlar, o'chirishlar, yangi izohlar) shu oraliqda tekshiriladi
    channel_refresh_interval_s: int = Field(3600, ge=300, le=86_400)

    media_enabled: bool = True
    # Stickerlardan tashqari barcha fayl turlari
    media_types: Annotated[frozenset[MediaType], NoDecode] = frozenset(
        {
            MediaType.PHOTO,
            MediaType.VIDEO,
            MediaType.VOICE,
            MediaType.AUDIO,
            MediaType.DOCUMENT,
            MediaType.ANIMATION,
            MediaType.VIDEO_NOTE,
        }
    )
    media_in_channels: bool = False
    # Bundan katta fayl yuklanmaydi, lekin "katta fayl" sifatida metama'lumoti bilan ko'rsatiladi
    media_max_file_mb: int = Field(50, ge=1, le=4000)
    media_max_total_gb: float | None = Field(None, gt=0)
    media_min_free_gb: float = Field(5.0, ge=0)
    media_concurrency: int = Field(2, ge=1, le=8)
    media_max_attempts: int = Field(5, ge=1, le=50)

    heartbeat_interval_s: int = Field(30, ge=5, le=3600)

    @field_validator("chat_allowlist", "chat_denylist", mode="before")
    @classmethod
    def _parse_id_lists(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("tg_api_hash")
    @classmethod
    def _check_api_hash(cls, value: SecretStr) -> SecretStr:
        if not _API_HASH_RE.match(value.get_secret_value()):
            raise ValueError("32 ta kichik hex belgidan iborat bo'lishi kerak (my.telegram.org)")
        return value

    @field_validator("media_types", mode="before")
    @classmethod
    def _parse_media_types(cls, value: Any) -> Any:
        value = _split_csv(value)
        if isinstance(value, list):
            return [v.lower() if isinstance(v, str) else v for v in value]
        return value

    @field_validator("media_types")
    @classmethod
    def _only_file_types(cls, value: frozenset[MediaType]) -> frozenset[MediaType]:
        wrong = sorted(t.value for t in value if t not in FILE_MEDIA_TYPES)
        if wrong:
            raise ValueError(f"fayl bo'lmagan turlarni yuklab bo'lmaydi: {', '.join(wrong)}")
        return value

    @property
    def session_path(self) -> Path:
        return self.paths.sessions / f"{self.tg_session_name}.session"

    def secret_values(self) -> list[str]:
        return [self.tg_api_hash.get_secret_value()]


class BotSettings(BaseAppSettings):
    bot_token: SecretStr
    owner_id: int = Field(gt=0)
    bot_page_size: int = Field(10, ge=1, le=50)

    @field_validator("bot_token")
    @classmethod
    def _check_token(cls, value: SecretStr) -> SecretStr:
        if not _BOT_TOKEN_RE.match(value.get_secret_value()):
            raise ValueError("@BotFather bergan formatda emas (123456:ABC...)")
        return value

    def secret_values(self) -> list[str]:
        return [self.bot_token.get_secret_value()]


S = TypeVar("S", bound=BaseAppSettings)

_ERROR_TEXT = {
    "missing": "majburiy, lekin berilmagan",
    "int_parsing": "butun son bo'lishi kerak",
    "float_parsing": "son bo'lishi kerak",
    "bool_parsing": "true yoki false bo'lishi kerak",
    "literal_error": "ruxsat etilmagan qiymat, mumkin: {expected}",
    "enum": "ruxsat etilmagan qiymat, mumkin: {expected}",
    "greater_than": "{gt} dan katta bo'lishi kerak",
    "greater_than_equal": "kamida {ge} bo'lishi kerak",
    "less_than": "{lt} dan kichik bo'lishi kerak",
    "less_than_equal": "ko'pi bilan {le} bo'lishi kerak",
    "string_pattern_mismatch": "formati noto'g'ri",
}


def format_validation_error(exc: ValidationError) -> str:
    """Pydantic xatosini kiritilgan qiymatlarsiz (maxfiylik uchun) matnga aylantiradi.

    ``ctx`` da faqat chegaralar (masalan ``gt=0``) bo'ladi, foydalanuvchi qiymati emas.
    """
    lines = []
    for err in exc.errors(include_input=False, include_url=False):
        name = ".".join(str(p) for p in err["loc"]).upper() or "?"
        ctx = err.get("ctx", {})
        if err["type"] == "value_error":
            text = str(ctx.get("error", err["msg"]))
        elif err["type"] in _ERROR_TEXT:
            try:
                text = _ERROR_TEXT[err["type"]].format(**ctx)
            except (KeyError, IndexError):
                text = err["msg"]
        else:
            text = err["msg"]
        lines.append(f"  - {name}: {text}")
    return "Konfiguratsiyada xato:\n" + "\n".join(lines)


def load_settings(cls: type[S], env_file: str | Path | None = ".env") -> S:
    """Sozlamalarni yuklaydi; xato bo'lsa maxfiy qiymatlarsiz ``ConfigError`` beradi."""
    try:
        return cls(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc)) from None
