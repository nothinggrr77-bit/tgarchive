"""10-bosqich: eksport (HTML/JSON zip) va saqlash muddati (retention)."""

from __future__ import annotations

import json
import os
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from telethon.tl import types

from archive_fixture import Archive, build_archive
from fake_telegram import FakeClock, FakeTelegram, RecordingSleep
from tgarchive import export as export_module
from tgarchive.archiver.dialogs import sync_dialogs
from tgarchive.archiver.history import HistoryImporter
from tgarchive.cli import main
from tgarchive.config import ArchiverSettings, BaseAppSettings
from tgarchive.db.engine import Database
from tgarchive.db.models import AuditLog, Media, MediaTrash, Message, MessageVersion
from tgarchive.db.retention import apply_retention, set_chat_retention
from tgarchive.db.search import SearchQuery
from tgarchive.export import ExportRequest, cleanup_exports, export_messages, parse_export_args
from tgarchive.exitcodes import EXIT_CONFIG, EXIT_OK
from tl_objects import ALI_ID, CHANNEL_ID, ME_ID, message, user

TASHKENT = ZoneInfo("Asia/Tashkent")
VALID_HASH = "0123456789abcdef0123456789abcdef"


def settings(tmp_path: Path, db_path: Path, **overrides: Any) -> BaseAppSettings:
    return BaseAppSettings(_env_file=None, data_dir=tmp_path, db_path=db_path, **overrides)


@pytest.fixture
async def archive(db: Database, tmp_path: Path) -> Archive:
    return await build_archive(tmp_path, db)


def read_zip(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


# ----------------------------------------------------------------- argumentlar


def test_parse_export_args() -> None:
    request, errors = parse_export_args("chat:7 after:2026-01-01 json +media", TASHKENT)
    assert errors == [] and request.fmt == "json" and request.with_media and request.query.chat_id == 7
    request, errors = parse_export_args("all", TASHKENT)
    assert errors == [] and request.query.is_empty and request.fmt == "html"
    assert parse_export_args("json", TASHKENT)[1][0].startswith("chat:, sana yoki boshqa filtr kerak")
    assert "son bo'lishi kerak" in parse_export_args("chat:x", TASHKENT)[1][0]


# ---------------------------------------------------------------------- HTML


async def test_html_export_is_escaped_and_complete(db: Database, db_path: Path, tmp_path: Path, archive: Archive) -> None:
    cfg = settings(tmp_path, db_path)
    result = await export_messages(db, cfg, ExportRequest(SearchQuery()))
    assert result.path.name.startswith("export-") and result.path.suffix == ".zip"
    assert (result.messages, result.chats) == (10, 2)
    files = read_zip(result.path)
    assert set(files) == {"export.html", "README.txt"}
    page = files["export.html"].decode("utf-8")
    assert "Content-Security-Policy" in page and "default-src 'none'" in page
    assert "&lt;b&gt;xavfli&lt;/b&gt; &amp; matn salom" in page and "<b>xavfli" not in page
    assert '<a href="#m7_3">javob: #3</a>' in page and 'id="m7_3"' in page
    assert "Oldingi versiyalar (1)" in page and "eski" in page
    assert "o'chirilgan (tasdiqlangan)" in page
    assert "kino.zip (2.0 MB) — yuklanmagan (too_large)" in page
    assert page.count("<section>") == 2  # har chat alohida bo'lim
    if os.name == "posix":
        assert result.path.stat().st_mode & 0o077 == 0
    async with db.read() as session:
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "export_created"))
    assert audit.details["messages"] == 10 and audit.target == result.path.name


async def test_script_in_text_cannot_run(db: Database, db_path: Path, tmp_path: Path, archive: Archive) -> None:
    async with db.write() as session:
        msg = await session.get(Message, archive.pk[8])
        msg.text = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    result = await export_messages(db, settings(tmp_path, db_path), ExportRequest(SearchQuery(outgoing=True)))
    page = read_zip(result.path)["export.html"].decode()
    assert "<script>" not in page and "<img src=x" not in page and "&lt;script&gt;" in page


# ---------------------------------------------------------------------- JSON


async def test_json_export_with_filters_and_media(db: Database, db_path: Path, tmp_path: Path, archive: Archive) -> None:
    cfg = settings(tmp_path, db_path)
    request = ExportRequest(SearchQuery(chat_id=ALI_ID), fmt="json", with_media=True)
    result = await export_messages(db, cfg, request)
    files = read_zip(result.path)
    data = json.loads(files["export.json"])
    messages = data["messages"]
    assert [m["message_id"] for m in messages] == list(range(1, 9))  # chat ichida vaqt tartibida
    assert data["chats"] == {str(ALI_ID): {"title": "Ali", "type": "private", "messages": 8}}
    by_id = {m["message_id"]: m for m in messages}
    assert by_id[5]["text"] == "yangi matn salom" and by_id[5]["versions"][0]["text"] == "eski"
    assert by_id[6]["deletion"]["status"] == "confirmed"
    assert by_id[4]["reply_to"]["msg_id"] == 3 and by_id[8]["outgoing"] is True
    photo = by_id[1]["media"]
    assert photo["file"].startswith("media/") and files[photo["file"]].startswith(b"P")
    assert by_id[2]["media"]["file"] is None and by_id[2]["media"]["skip_reason"] == "too_large"
    assert data["summary"] == {"messages": 8, "chats": 1, "media_included": 1, "media_skipped": 0}


async def test_media_budget_and_batches(db: Database, db_path: Path, tmp_path: Path, archive: Archive, monkeypatch) -> None:
    monkeypatch.setattr(export_module, "BATCH", 3)  # keyset sahifalash ham sinaladi
    cfg = settings(tmp_path, db_path, export_max_media_mb=0)
    result = await export_messages(db, cfg, ExportRequest(SearchQuery(), fmt="json", with_media=True))
    data = json.loads(read_zip(result.path)["export.json"])
    assert len(data["messages"]) == 10 and len({(m["chat_id"], m["message_id"]) for m in data["messages"]}) == 10
    assert data["summary"]["media_skipped"] == 1 and result.media_included == 0


async def test_empty_result(db: Database, db_path: Path, tmp_path: Path, archive: Archive) -> None:
    result = await export_messages(db, settings(tmp_path, db_path), ExportRequest(SearchQuery(text="mavjudemas")))
    assert result.messages == 0
    assert "Xabarlar: 0" in read_zip(result.path)["export.html"].decode()


def test_cleanup_exports(tmp_path: Path) -> None:
    now = datetime(2026, 10, 25, tzinfo=UTC)
    files = {
        "export-old.zip": now - timedelta(days=8),
        "export-new.zip": now - timedelta(days=1),
        "export-x.zip.part": now - timedelta(minutes=5),  # hozir yozilayotgan bo'lishi mumkin
        "export-y.zip.part": now - timedelta(hours=2),
        "boshqa.txt": now - timedelta(days=30),
    }
    for name, when in files.items():
        (tmp_path / name).write_text("x")
        os.utime(tmp_path / name, (when.timestamp(), when.timestamp()))
    removed = {p.name for p in cleanup_exports(tmp_path, 7, now)}
    assert removed == {"export-old.zip", "export-y.zip.part"}


def test_export_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = tmp_path / "test.env"
    env.write_text(f"DATA_DIR={tmp_path.as_posix()}\n", encoding="utf-8")
    assert main(["--env-file", str(env), "db-upgrade"]) == EXIT_OK
    assert main(["--env-file", str(env), "export", "json"]) == EXIT_CONFIG  # filtrsiz — rad etiladi
    assert main(["--env-file", str(env), "export", "all", "json"]) == EXIT_OK
    assert "Eksport tayyor" in capsys.readouterr().out


# ------------------------------------------------------------------ retention


async def test_retention_removes_old_messages_and_files(db: Database, tmp_path: Path, archive: Archive) -> None:
    async with db.read() as session:
        newest = await session.scalar(select(func.max(Message.date)).where(Message.chat_id == ALI_ID))
    now = newest + timedelta(days=10)
    assert await apply_retention(db, now=now, global_days=None) == {}  # sukut: doimiy

    assert await set_chat_retention(db, ALI_ID, 5)
    removed = await apply_retention(db, now=now, global_days=None)
    assert removed == {ALI_ID: 8}
    async with db.read() as session:
        assert await session.scalar(select(func.count()).where(Message.chat_id == ALI_ID)) == 0
        assert await session.scalar(select(func.count()).where(Message.chat_id == CHANNEL_ID)) == 2
        assert await session.scalar(select(func.count()).select_from(MessageVersion)) == 0
        assert await session.scalar(select(func.count()).select_from(Media)) == 0
        trash = list(await session.scalars(select(MediaTrash.local_path)))
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "retention_applied"))
    assert len(trash) == 1 and trash[0].endswith(".jpg")  # yuklangan rasm fayli o'chiriladi
    assert audit.details == {"total": 8, "chats": {str(ALI_ID): 8}}

    removed = await apply_retention(db, now=now + timedelta(days=100), global_days=30)
    assert removed == {CHANNEL_ID: 2}
    assert not await set_chat_retention(db, 123456, 5)


async def test_backfill_respects_retention(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *[message(i, peer=types.PeerUser(ALI_ID)) for i in range(1, 26)])
    cfg = ArchiverSettings(
        _env_file=None, tg_api_id=1, tg_api_hash=VALID_HASH, data_dir=tmp_path,
        history_page_size=10, history_page_delay_s=0, retention_days=1,
    )
    await sync_dialogs(tg, db, cfg)
    newest = tg.history[chat][25].date
    clock = FakeClock(newest + timedelta(days=1) - timedelta(minutes=15))  # chegara: 10-xabar
    await HistoryImporter(tg, db, cfg, self_id=ME_ID, sleep=RecordingSleep(), clock=clock).run_until_idle()
    async with db.read() as session:
        ids = sorted(await session.scalars(select(Message.message_id).where(Message.chat_id == chat)))
    assert ids == list(range(10, 26))  # muddati o'tganlar qayta import qilinmaydi


def test_retention_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = tmp_path / "test.env"
    env.write_text(f"DATA_DIR={tmp_path.as_posix()}\n", encoding="utf-8")
    base = ["--env-file", str(env)]
    assert main([*base, "db-upgrade"]) == EXIT_OK

    async def add_chat() -> None:
        from tgarchive.db.enums import ChatType
        from tgarchive.db.store import ChatInfo, upsert_chat

        db = Database(BaseAppSettings(_env_file=None, data_dir=tmp_path).database_url)
        async with db.write() as session:
            await upsert_chat(session, ChatInfo(chat_id=7, chat_type=ChatType.PRIVATE, title="Ali"))
        await db.dispose()

    import asyncio

    asyncio.run(add_chat())
    capsys.readouterr()
    assert main([*base, "retention", "set", "7", "30"]) == EXIT_OK
    assert main([*base, "retention", "show"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "7  Ali: 30 kun" in out and "doimiy saqlash" in out
    assert main([*base, "retention", "set", "7", "off"]) == EXIT_OK
    assert main([*base, "retention", "set", "7", "abc"]) == EXIT_CONFIG
