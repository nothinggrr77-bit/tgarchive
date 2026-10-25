"""Archiver xizmati va CLI'dagi status/health buyruqlari."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from telethon import errors, events
from telethon.tl import types

from factories import sqlite_url
from fake_telegram import FakeTelegram, RecordingSleep
from tgarchive.archiver.service import ArchiverService
from tgarchive.cli import main
from tgarchive.config import ArchiverSettings
from tgarchive.db.engine import Database
from tgarchive.db.enums import SyncStatus
from tgarchive.db.meta import bind_account, bound_account_id
from tgarchive.db.migrate import upgrade
from tgarchive.db.models import ChatSyncState
from tgarchive.db.status import ARCHIVER, beat, get_heartbeat, import_summary
from tgarchive.db.store import get_message
from tgarchive.db.types import utcnow
from tgarchive.exitcodes import EXIT_AUTH, EXIT_OK, EXIT_RUNTIME
from tl_objects import ME_ID, channel, message, user

VALID_HASH = "0123456789abcdef0123456789abcdef"


def settings(tmp_path: Path, **overrides: Any) -> ArchiverSettings:
    values: dict[str, Any] = {"history_page_size": 10, "history_page_delay_s": 0, "channel_keep_posts": 5} | overrides
    cfg = ArchiverSettings(_env_file=None, tg_api_id=1, tg_api_hash=VALID_HASH, data_dir=tmp_path, **values)
    cfg.paths.db.parent.mkdir(parents=True, exist_ok=True)
    upgrade(sqlite_url(cfg.paths.db))
    return cfg


def with_session_file(cfg: ArchiverSettings) -> ArchiverSettings:
    cfg.session_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.session_path.touch()
    return cfg


class NoMedia:
    """Media worker o'rniga: to'xtash testlarida uning kutishlari aralashmasin."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def run(self) -> None:
        return None

    def details(self) -> None:
        return None


def telegram() -> FakeTelegram:
    tg = FakeTelegram()
    tg.add_chat(user(), *[message(i, peer=types.PeerUser(7)) for i in range(1, 26)])
    tg.add_chat(channel(), *[message(i, peer=types.PeerChannel(1234567890), post=True) for i in range(1, 13)])
    return tg


async def open_db(cfg: ArchiverSettings) -> Database:
    return Database(cfg.database_url)


async def test_import_once_runs_everything_and_reports(tmp_path: Path) -> None:
    cfg = with_session_file(settings(tmp_path))
    tg = telegram()
    service = ArchiverService(cfg, client_factory=lambda _: tg, sleep=RecordingSleep())

    assert await service.run(once=True) == EXIT_OK

    db = await open_db(cfg)
    async with db.read() as session:
        assert await bound_account_id(session) == ME_ID
        heartbeat = await get_heartbeat(session, ARCHIVER)
        summary = await import_summary(session)
    await db.dispose()
    assert heartbeat.state == "stopped" and heartbeat.details["account_id"] == ME_ID
    assert summary.backfill_by_status == {"done": 2}
    assert summary.total_messages == 25 + 5
    assert summary.chats_by_mode == {"full": 1, "window": 1}
    assert not tg.connected


async def test_missing_session_file_is_auth_error_without_connecting(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    factory_calls = []
    service = ArchiverService(cfg, client_factory=lambda s: factory_calls.append(s), sleep=RecordingSleep())
    assert await service.run(once=True) == EXIT_AUTH
    assert factory_calls == []
    db = await open_db(cfg)
    async with db.read() as session:
        heartbeat = await get_heartbeat(session, ARCHIVER)
    await db.dispose()
    assert heartbeat.state == "auth_error" and "tgarchive login" in heartbeat.details["error"]


async def test_revoked_session_is_auth_error(tmp_path: Path) -> None:
    cfg = with_session_file(settings(tmp_path))
    tg = telegram()
    tg.authorized = False
    assert await ArchiverService(cfg, client_factory=lambda _: tg, sleep=RecordingSleep()).run(once=True) == EXIT_AUTH


async def test_other_account_database_is_refused(tmp_path: Path) -> None:
    cfg = with_session_file(settings(tmp_path))
    db = await open_db(cfg)
    async with db.write() as session:
        await bind_account(session, 999)
    await db.dispose()
    tg = telegram()
    assert await ArchiverService(cfg, client_factory=lambda _: tg, sleep=RecordingSleep()).run(once=True) == EXIT_AUTH
    assert tg.count("get_messages") == 0


async def test_session_revoked_mid_run(tmp_path: Path) -> None:
    cfg = with_session_file(settings(tmp_path))
    tg = telegram()
    tg.fail("get_messages", errors.AuthKeyUnregisteredError(request=None), on_call=2)
    assert await ArchiverService(cfg, client_factory=lambda _: tg, sleep=RecordingSleep()).run(once=True) == EXIT_AUTH


async def test_network_loss_backs_off_and_reconnects(tmp_path: Path) -> None:
    cfg = with_session_file(settings(tmp_path))
    tg = telegram()
    tg.fail("get_messages", ConnectionError("uzildi"), on_call=1)
    sleep = RecordingSleep()
    assert await ArchiverService(cfg, client_factory=lambda _: tg, sleep=sleep).run(once=True) == EXIT_OK
    assert 10 in sleep.calls


async def test_service_mode_stops_cleanly_on_signal(tmp_path: Path) -> None:
    cfg = with_session_file(settings(tmp_path))
    tg = telegram()
    stop = asyncio.Event()

    async def stop_when_idle(seconds: float) -> None:
        if seconds > 0:  # sahifalar orasidagi 0 s pauza emas, keyingi sinxrongacha kutish
            stop.set()  # SIGTERM kelgandek

    service = ArchiverService(
        cfg, client_factory=lambda _: tg, sleep=stop_when_idle, stop=stop, media_factory=NoMedia
    )
    assert await service.run() == EXIT_OK
    db = await open_db(cfg)
    async with db.read() as session:
        states = list(await session.scalars(ChatSyncState.__table__.select()))
        statuses = {row.backfill_status for row in await session.scalars(select(ChatSyncState))}
        heartbeat = await get_heartbeat(session, ARCHIVER)
    await db.dispose()
    assert len(states) == 2 and statuses == {SyncStatus.DONE}
    assert heartbeat.state == "stopped"


async def test_stop_during_import_keeps_checkpoint_and_resumes(tmp_path: Path) -> None:
    cfg = with_session_file(settings(tmp_path))
    tg = telegram()
    stop = asyncio.Event()

    async def stop_now(seconds: float) -> None:
        stop.set()  # birinchi sahifadan keyin SIGTERM

    service = ArchiverService(cfg, client_factory=lambda _: tg, sleep=stop_now, stop=stop, media_factory=NoMedia)
    assert await service.run() == EXIT_OK
    db = await open_db(cfg)
    async with db.read() as session:
        private_state = await session.get(ChatSyncState, 7)
    assert private_state.backfill_status is SyncStatus.RUNNING  # tugamagan — "done" emas
    assert private_state.backfill_cursor_id == 16

    assert await ArchiverService(cfg, client_factory=lambda _: tg, sleep=RecordingSleep()).run(once=True) == EXIT_OK
    async with db.read() as session:
        private_state = await session.get(ChatSyncState, 7)
    await db.dispose()
    assert (private_state.backfill_status, private_state.backfill_imported) == (SyncStatus.DONE, 25)


# ----------------------------------------------------------------- CLI


def _env_file(tmp_path: Path) -> Path:
    env = tmp_path / "test.env"
    env.write_text(f"DATA_DIR={tmp_path.as_posix()}/data\n", encoding="utf-8")
    return env


async def _write_heartbeat(tmp_path: Path, state: str, age_s: int) -> None:
    db = Database(sqlite_url(tmp_path / "data" / "db" / "archive.sqlite3"))
    async with db.write() as session:
        await beat(session, ARCHIVER, state, started_at=utcnow())
        (await get_heartbeat(session, ARCHIVER)).last_beat_at = utcnow() - timedelta(seconds=age_s)
    await db.dispose()


def test_health_and_status_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = str(_env_file(tmp_path))
    assert main(["--env-file", env, "health"]) == EXIT_RUNTIME  # baza yo'q
    assert main(["--env-file", env, "db-upgrade"]) == EXIT_OK
    assert main(["--env-file", env, "health"]) == EXIT_RUNTIME  # heartbeat yo'q

    asyncio.run(_write_heartbeat(tmp_path, "importing", age_s=10))
    assert main(["--env-file", env, "health"]) == EXIT_OK
    asyncio.run(_write_heartbeat(tmp_path, "importing", age_s=600))
    assert main(["--env-file", env, "health", "--max-age", "180"]) == EXIT_RUNTIME
    asyncio.run(_write_heartbeat(tmp_path, "auth_error", age_s=5))
    assert main(["--env-file", env, "health"]) == EXIT_RUNTIME

    capsys.readouterr()
    assert main(["--env-file", env, "status"]) == EXIT_OK
    output = capsys.readouterr().out
    assert "Archiver: auth_error" in output and "Xabarlar: jami 0" in output


async def test_status_shows_auth_error_reason(tmp_path: Path) -> None:
    from tgarchive.db.status import format_summary

    cfg = settings(tmp_path)
    assert await ArchiverService(cfg, sleep=RecordingSleep()).run(once=True) == EXIT_AUTH
    db = await open_db(cfg)
    async with db.read() as session:
        text = format_summary(await import_summary(session), await get_heartbeat(session, ARCHIVER), utcnow())
    await db.dispose()
    assert "Sabab: session yo'q: avval tgarchive login" in text


# ------------------------------------------------------------ real vaqt


async def test_handlers_registered_before_connect_and_catch_up_is_not_lost(tmp_path: Path) -> None:
    cfg = with_session_file(settings(tmp_path))
    tg = telegram()
    # Ulanish paytida catch_up olib keladigan hodisa: tarixda yo'q, faqat real vaqtdan keladi
    tg.on_connect = [(events.NewMessage, events.NewMessage.Event(message(30, peer=types.PeerUser(7))))]

    assert await ArchiverService(cfg, client_factory=lambda _: tg, sleep=RecordingSleep()).run(once=True) == EXIT_OK

    order = [name for name, _chat, _kw in tg.calls if name in ("add_event_handler", "connect")]
    assert order[:4] == ["add_event_handler"] * 3 + ["connect"]
    assert tg.handlers == []  # to'xtashda olib tashlangan
    db = await open_db(cfg)
    async with db.read() as session:
        caught = await get_message(session, 7, 30)
        heartbeat = await get_heartbeat(session, ARCHIVER)
    await db.dispose()
    assert caught is not None and caught.source.value == "realtime"
    assert heartbeat.details["realtime"]["events"]["created"] == 1


async def test_realtime_can_be_disabled(tmp_path: Path) -> None:
    cfg = with_session_file(settings(tmp_path, realtime_enabled=False))
    tg = telegram()
    assert await ArchiverService(cfg, client_factory=lambda _: tg, sleep=RecordingSleep()).run(once=True) == EXIT_OK
    assert tg.count("add_event_handler") == 0


async def test_auth_failure_unregisters_handlers_and_disconnects(tmp_path: Path) -> None:
    cfg = with_session_file(settings(tmp_path))
    tg = telegram()
    tg.authorized = False
    assert await ArchiverService(cfg, client_factory=lambda _: tg, sleep=RecordingSleep()).run(once=True) == EXIT_AUTH
    assert tg.handlers == [] and not tg.connected
