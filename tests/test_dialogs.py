from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from factories import add_chats
from tgarchive.archiver.client import DEVICE_MODEL, create_client
from tgarchive.archiver.dialogs import DialogInfo, fetch_dialogs, format_plan, plan_dialogs
from tgarchive.config import ArchiverSettings
from tgarchive.db.engine import Database
from tgarchive.db.enums import ArchiveMode
from tgarchive.db.models import AuditLog, Chat
from tl_objects import ALI_ID, CHANNEL_ID, channel, group, me, message, user

VALID_HASH = "0123456789abcdef0123456789abcdef"


def settings(tmp_path: Path, **overrides: Any) -> ArchiverSettings:
    return ArchiverSettings(_env_file=None, tg_api_id=1, tg_api_hash=VALID_HASH, data_dir=tmp_path, **overrides)


@dataclass
class FakeDialog:
    entity: Any
    message: Any
    archived: bool = False
    unread_count: int = 0


class FakeClient:
    def __init__(self, dialogs: list[FakeDialog]) -> None:
        self.dialogs = dialogs

    async def iter_dialogs(self, limit: int | None = None):
        for dialog in self.dialogs[:limit]:
            yield dialog


def _dialogs() -> list[FakeDialog]:
    return [
        FakeDialog(user(first_name="Ali"), message(40)),
        FakeDialog(me(), message(41)),
        FakeDialog(user(9, bot=True, first_name="Helper"), message(42)),
        FakeDialog(group(), None, archived=True, unread_count=3),
        FakeDialog(channel(), message(500)),
        FakeDialog(object(), None),  # noma'lum entity — o'tkazib yuboriladi
    ]


async def test_fetch_dialogs_includes_archive_folder() -> None:
    result = await fetch_dialogs(FakeClient(_dialogs()))
    assert [d.chat.chat_type.value for d in result] == ["private", "saved", "bot", "group", "channel"]
    assert result[0].top_message_id == 40
    archived = result[3]
    assert archived.in_archive_folder and archived.top_message_id is None and archived.unread_count == 3


async def test_fetch_dialogs_limit() -> None:
    assert len(await fetch_dialogs(FakeClient(_dialogs()), limit=2)) == 2


async def test_plan_without_save_does_not_write(db: Database, tmp_path: Path) -> None:
    dialogs = await fetch_dialogs(FakeClient(_dialogs()))
    planned = await plan_dialogs(db, settings(tmp_path), dialogs)
    assert [p.policy.mode for p in planned] == [
        ArchiveMode.FULL,
        ArchiveMode.FULL,
        ArchiveMode.SKIP,
        ArchiveMode.FULL,
        ArchiveMode.WINDOW,
    ]
    async with db.read() as session:
        assert list(await session.scalars(select(Chat))) == []


async def test_plan_with_save_registers_chats_and_respects_overrides(db: Database, tmp_path: Path) -> None:
    dialogs: list[DialogInfo] = await fetch_dialogs(FakeClient(_dialogs()))
    await add_chats(db, dialogs[0].chat)
    async with db.write() as session:
        (await session.get(Chat, ALI_ID)).archive_override = False  # egasi bot orqali o'chirgan

    planned = await plan_dialogs(db, settings(tmp_path), dialogs, save=True)
    assert planned[0].policy.mode is ArchiveMode.SKIP
    async with db.read() as session:
        rows = {c.chat_id: c for c in await session.scalars(select(Chat))}
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "dialogs_saved"))
    assert len(rows) == 5
    assert rows[ALI_ID].archive_mode is ArchiveMode.SKIP
    assert rows[CHANNEL_ID].archive_mode is ArchiveMode.WINDOW
    assert audit.details["total"] == 5 and audit.details["window"] == 1


async def test_format_plan(db: Database, tmp_path: Path) -> None:
    planned = await plan_dialogs(db, settings(tmp_path), await fetch_dialogs(FakeClient(_dialogs())))
    table = format_plan(planned)
    assert "window20" in table and "[Archive]" in table
    assert "kanal: oxirgi 20 ta post" in table
    assert table.splitlines()[-1] == "Jami: 5 | to'liq: 3 | oxirgi postlar: 1 | o'tkazib yuboriladi: 1"


async def test_create_client_uses_session_path(tmp_path: Path) -> None:
    cfg = settings(tmp_path, flood_sleep_threshold_s=33)
    client = create_client(cfg)
    try:
        assert client.session.filename == str(cfg.session_path)
        assert cfg.session_path.exists()
        assert client.flood_sleep_threshold == 33
        assert client._init_request.device_model == DEVICE_MODEL
    finally:
        client.session.close()


@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX ruxsatlari faqat Linux'da")
async def test_session_file_is_private(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    client = create_client(cfg)
    try:
        assert cfg.session_path.stat().st_mode & 0o077 == 0
        assert cfg.paths.sessions.stat().st_mode & 0o077 == 0
    finally:
        client.session.close()
