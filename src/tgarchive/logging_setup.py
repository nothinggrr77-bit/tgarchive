"""Structured logging (structlog + stdlib) va maxfiy qiymatlarni yashirish.

Telethon, aiogram va SQLAlchemy stdlib ``logging`` dan foydalanadi; ularning yozuvlari
ham shu formatter orqali o'tadi. Yashirish ikki bosqichda ishlaydi:

1. Kalit bo'yicha: ``token``, ``api_hash``, ``password`` kabi maydonlar ``***`` bo'ladi.
2. Qiymat bo'yicha: ro'yxatdan o'tgan maxfiy qiymatlar (API_HASH, BOT_TOKEN) va bot
   token shakliga mos har qanday qator tayyor log qatoridan (traceback ham) olib tashlanadi.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable
from typing import Any

import structlog

MASK = "***"
_SENSITIVE_KEY_RE = re.compile(
    r"token|api_hash|secret|passw|session_string|auth_key|phone|login_code|api_key", re.I
)
# \b ishlatilmaydi: URL ichida token "bot123456:..." ko'rinishida, "t" va "1" orasida chegara yo'q.
_BOT_TOKEN_RE = re.compile(r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{30,}")
_AGE_SECRET_RE = re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]{20,}")
_MIN_SECRET_LEN = 6


class SecretRedactor:
    def __init__(self) -> None:
        self._values: set[str] = set()

    def register(self, *values: str) -> None:
        for value in values:
            if value and len(value) >= _MIN_SECRET_LEN:
                self._values.add(value)

    def redact_text(self, text: str) -> str:
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, MASK)
        return _AGE_SECRET_RE.sub(MASK, _BOT_TOKEN_RE.sub(MASK, text))

    def __call__(self, _logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        """structlog processor: render qilishdan oldin kalitlarni tekshiradi."""
        for key in list(event_dict):
            if key.startswith("_"):
                continue
            if _SENSITIVE_KEY_RE.search(key) and key != "event":
                event_dict[key] = MASK
            elif isinstance(event_dict[key], str):
                event_dict[key] = self.redact_text(event_dict[key])
        return event_dict

    def redact_rendered(self, _logger: Any, _method: str, rendered: str) -> str:
        """Oxirgi processor: tayyor qatordan (traceback bilan) qiymatlarni olib tashlaydi."""
        return self.redact_text(rendered)


redactor = SecretRedactor()

_NOISY_LOGGERS = {
    "telethon": logging.WARNING,
    "aiogram.event": logging.INFO,
    "sqlalchemy.engine": logging.WARNING,
    "alembic": logging.WARNING,
    "aiosqlite": logging.WARNING,
    "asyncio": logging.WARNING,
}


def setup_logging(
    level: str = "INFO",
    fmt: str = "json",
    secrets: Iterable[str] = (),
    stream: Any = None,
) -> None:
    redactor.register(*secrets)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redactor,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    if fmt == "json":
        final: list[Any] = [structlog.processors.format_exc_info, structlog.processors.JSONRenderer(ensure_ascii=False)]
    else:
        final = [structlog.dev.ConsoleRenderer(colors=False)]

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *final,
            redactor.redact_rendered,
        ],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for name, lib_level in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(max(lib_level, root.level))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
