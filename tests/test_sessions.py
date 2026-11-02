from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fake_botapi import OWNER, TOKEN
from fake_telegram import FakeAuth, FakeTelegram

from tgarchive.archiver.sessions import snapshot_sessions
from tgarchive.config import BotSettings
from tgarchive.db.engine import Database
from tgarchive.db.sessions import load_sessions_snapshot, save_sessions_snapshot


def _iso(dt: datetime) -> str:
    return dt.isoformat()


async def test_save_and_load_snapshot(db: Database) -> None:
    now = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    created = datetime(2025, 8, 1, 12, 0, tzinfo=UTC)
    async with db.write() as session:
        await save_sessions_snapshot(
            session,
            [
                {
                    "hash": "0", "current": True, "device_model": "iPhone 15",
                    "platform": "ios", "app_name": "Telegram iOS", "app_version": "10.9",
                    "ip": "213.230.1.1", "country": "Uzbekistan", "region": "Tashkent",
                    "date_created": _iso(created), "date_active": _iso(now),
                }
            ],
            now,
        )
    async with db.read() as session:
        snap = await load_sessions_snapshot(session)
    assert snap.taken_at == now
    [s] = snap.sessions
    assert s.current and s.device_model == "iPhone 15" and s.country == "Uzbekistan"
    assert s.date_created == created and s.date_active == now


async def test_load_empty_when_no_snapshot(db: Database) -> None:
    async with db.read() as session:
        snap = await load_sessions_snapshot(session)
    assert snap.taken_at is None and snap.sessions == []


async def test_snapshot_sessions_reads_from_client(db: Database) -> None:
    tg = FakeTelegram()
    now = datetime.now(UTC)
    tg.authorizations = [
        FakeAuth(hash=1, current=True, device_model="Server", platform="linux",
                 app_name="tgarchive", date_created=now - timedelta(days=30), date_active=now),
        FakeAuth(hash=2, device_model="Pixel 8", platform="android",
                 app_name="Telegram Android", date_created=now - timedelta(days=5),
                 date_active=now - timedelta(hours=3)),
    ]
    count = await snapshot_sessions(tg, db)
    assert count == 2
    async with db.read() as session:
        snap = await load_sessions_snapshot(session)
    assert {s.device_model for s in snap.sessions} == {"Server", "Pixel 8"}
    assert any(s.current for s in snap.sessions)


async def test_snapshot_sessions_swallows_client_error(db: Database) -> None:
    class Broken:
        async def __call__(self, _request: object) -> object:
            raise RuntimeError("tarmoq yo'q")

    assert await snapshot_sessions(Broken(), db) == 0  # xato aylanishni buzmaydi


def _bot_settings(tmp_path: Path) -> BotSettings:
    return BotSettings(_env_file=None, bot_token=TOKEN, owner_id=OWNER, data_dir=tmp_path)


async def test_sessions_view_formats_online_and_new(db: Database, tmp_path: Path) -> None:
    from tgarchive.bot.views import Views

    now = datetime.now(UTC)
    async with db.write() as session:
        await save_sessions_snapshot(
            session,
            [
                {"hash": "1", "current": True, "device_model": "Server", "platform": "linux",
                 "app_name": "tgarchive", "date_created": _iso(now - timedelta(days=40)),
                 "date_active": _iso(now)},
                {"hash": "2", "device_model": "Pixel 8", "platform": "android",
                 "app_name": "Telegram Android", "country": "Uzbekistan", "region": "Tashkent",
                 "date_created": _iso(now - timedelta(days=5)),
                 "date_active": _iso(now - timedelta(days=2))},
                {"hash": "3", "device_model": "iPad", "platform": "ios",
                 "date_created": _iso(now - timedelta(minutes=10)),
                 "date_active": _iso(now - timedelta(minutes=1))},
            ],
            now,
        )
    views = Views(db, _bot_settings(tmp_path))
    view = await views.sessions()
    text = view.text
    assert "3 ta qurilma" in text
    assert "🤖 shu arxiv sessiyasi" in text  # joriy (archiver) sessiya belgilangan
    assert "🟢" in text and "online" in text  # Server hozir faol
    assert "2 kun oldin" in text  # Pixel 8 oxirgi faolligi
    assert "🆕" in text  # iPad so'nggi 24 soatda kirilgan
    assert "Pixel 8" in text and "Server" in text


async def test_sessions_view_empty(db: Database, tmp_path: Path) -> None:
    from tgarchive.bot.views import Views

    views = Views(db, _bot_settings(tmp_path))
    view = await views.sessions()
    assert "Hali o'qilmagan" in view.text
