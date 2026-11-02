"""Akkauntning faol sessiyalari (Telegram "Qurilmalar") suratini saqlash/o'qish.

Archiver MTProto orqali ``account.getAuthorizations`` ni davriy o'qiydi va bu yerga
yozadi; bot (MTProto'siz) shu suratni o'qib ``/sessions`` da ko'rsatadi. Surat oddiy
JSON sifatida ``app_meta`` da saqlanadi — alohida jadval/migratsiya kerak emas.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from tgarchive.db.meta import get_json_meta, set_json_meta

SESSIONS_KEY = "sessions_snapshot"


@dataclass(frozen=True, slots=True)
class SessionInfo:
    hash: str
    current: bool  # shu archiver sessiyasi
    official_app: bool
    device_model: str
    platform: str
    system_version: str
    app_name: str
    app_version: str
    ip: str
    country: str
    region: str
    date_created: datetime | None
    date_active: datetime | None


@dataclass(frozen=True, slots=True)
class SessionsSnapshot:
    taken_at: datetime | None  # surat qachon olingan
    sessions: list[SessionInfo]


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _to_info(data: dict[str, Any]) -> SessionInfo:
    return SessionInfo(
        hash=str(data.get("hash", "")),
        current=bool(data.get("current", False)),
        official_app=bool(data.get("official_app", False)),
        device_model=str(data.get("device_model", "")),
        platform=str(data.get("platform", "")),
        system_version=str(data.get("system_version", "")),
        app_name=str(data.get("app_name", "")),
        app_version=str(data.get("app_version", "")),
        ip=str(data.get("ip", "")),
        country=str(data.get("country", "")),
        region=str(data.get("region", "")),
        date_created=_parse_dt(data.get("date_created")),
        date_active=_parse_dt(data.get("date_active")),
    )


async def save_sessions_snapshot(session: AsyncSession, sessions: list[dict[str, Any]], now: datetime) -> None:
    """Sessiyalar ro'yxatini (dict'lar) va surat vaqtini saqlaydi (oldingisini almashtiradi)."""
    await set_json_meta(session, SESSIONS_KEY, {"taken_at": now.isoformat(), "sessions": sessions})


async def load_sessions_snapshot(session: AsyncSession) -> SessionsSnapshot:
    """Oxirgi suratni o'qiydi. Surat yo'q bo'lsa — bo'sh ro'yxat."""
    data = await get_json_meta(session, SESSIONS_KEY)
    if not data:
        return SessionsSnapshot(None, [])
    raw = data.get("sessions")
    sessions = [_to_info(s) for s in raw] if isinstance(raw, list) else []
    return SessionsSnapshot(_parse_dt(data.get("taken_at")), sessions)
