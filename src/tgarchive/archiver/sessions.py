"""Akkauntning faol sessiyalarini (Telegram "Qurilmalar") davriy suratga olish.

Archiver ``account.getAuthorizations`` ni o'qiydi va bazaga yozadi; bot shu suratni
o'qib ``/sessions`` da ko'rsatadi. Hech qachon tashqariga xato chiqarmaydi — chaqiruvchi
asosiy aylanishni buzmasin (sessiyalar ro'yxati yangilanmasa, bot eski suratni ko'rsatadi).
"""

from __future__ import annotations

from typing import Any

from telethon.tl import functions

from tgarchive.db.engine import Database
from tgarchive.db.sessions import save_sessions_snapshot
from tgarchive.db.types import utcnow
from tgarchive.logging_setup import get_logger

log = get_logger(__name__)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _to_dict(auth: Any) -> dict[str, Any]:
    return {
        "hash": str(getattr(auth, "hash", "")),
        "current": bool(getattr(auth, "current", False)),
        "official_app": bool(getattr(auth, "official_app", False)),
        "device_model": getattr(auth, "device_model", "") or "",
        "platform": getattr(auth, "platform", "") or "",
        "system_version": getattr(auth, "system_version", "") or "",
        "app_name": getattr(auth, "app_name", "") or "",
        "app_version": getattr(auth, "app_version", "") or "",
        "ip": getattr(auth, "ip", "") or "",
        "country": getattr(auth, "country", "") or "",
        "region": getattr(auth, "region", "") or "",
        "date_created": _iso(getattr(auth, "date_created", None)),
        "date_active": _iso(getattr(auth, "date_active", None)),
    }


async def snapshot_sessions(client: Any, db: Database) -> int:
    """Faol sessiyalarni o'qib bazaga yozadi. Qaytaradi: sessiyalar soni (xatoda 0)."""
    try:
        result = await client(functions.account.GetAuthorizationsRequest())
        sessions = [_to_dict(auth) for auth in result.authorizations]
    except Exception as exc:  # tarmoq/FloodWait/API — aylanishni buzmaymiz
        log.warning("sessions_snapshot_failed", error=type(exc).__name__)
        return 0
    try:
        async with db.write() as session:
            await save_sessions_snapshot(session, sessions, utcnow())
    except Exception:
        log.exception("sessions_snapshot_save_failed")
        return 0
    log.info("sessions_snapshot", count=len(sessions))
    return len(sessions)
