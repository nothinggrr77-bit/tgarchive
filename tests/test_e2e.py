"""Uchdan-uchgacha: .env -> archiver (import, real vaqt, media, zaxira) -> bot -> eksport -> tiklash.

Barcha komponentlar bitta bazada, haqiqiy kod bilan ishlaydi; faqat Telegram tarmog'i soxta
(FakeTelegram va Bot API so'rovlarini yozib oluvchi session).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.methods import SendDocument, SendMessage
from sqlalchemy import select
from telethon import events
from telethon.tl import types

from fake_botapi import OWNER, TOKEN, RecordingSession, text_update
from fake_telegram import FakeTelegram
from tgarchive.archiver.service import ArchiverService
from tgarchive.backup import list_backups
from tgarchive.bot.app import build_dispatcher
from tgarchive.cli import main
from tgarchive.config import ArchiverSettings, BotSettings, load_settings
from tgarchive.crypto import generate_keypair
from tgarchive.db.engine import Database
from tgarchive.db.enums import DeletionStatus, MediaSkipReason, MediaStatus
from tgarchive.db.models import Media, Message, MessageVersion
from tgarchive.exitcodes import EXIT_OK
from tl_objects import ALI_ID, CHANNEL_RAW_ID, channel, document_media, message, photo_media, user

VALID_HASH = "0123456789abcdef0123456789abcdef"


def pm(i: int, **fields) -> types.Message:
    return message(i, peer=types.PeerUser(ALI_ID), **fields)


def telegram() -> FakeTelegram:
    tg = FakeTelegram()
    big = document_media(
        [types.DocumentAttributeFilename(file_name="kino.zip")], mime="application/zip", size=2 * 1024 * 1024, doc_id=77
    )
    tg.add_chat(
        user(),
        pm(1, media=photo_media(), message=""),
        pm(2, media=big, message=""),
        *[pm(i, message=f"salom {i}") for i in range(3, 8)],
    )
    tg.add_chat(channel(), *[message(i, peer=types.PeerChannel(CHANNEL_RAW_ID), post=True) for i in range(1, 9)])
    tg.files[(ALI_ID, 1)] = b"J" * 3000
    return tg


def test_full_pipeline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    public, secret = generate_keypair()
    key = tmp_path / "key.txt"
    key.write_text(secret + "\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                f"DATA_DIR={tmp_path.as_posix()}/data",
                "TG_API_ID=1",
                f"TG_API_HASH={VALID_HASH}",
                f"BOT_TOKEN={TOKEN}",
                f"OWNER_ID={OWNER}",
                f"BACKUP_AGE_RECIPIENTS={public}",
                "MEDIA_MAX_FILE_MB=1",
                "MEDIA_MIN_FREE_GB=0",
                "HISTORY_PAGE_DELAY_S=0",
                "CHANNEL_KEEP_POSTS=5",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cli = ["--env-file", str(env)]
    assert main([*cli, "db-upgrade"]) == EXIT_OK
    cfg = load_settings(ArchiverSettings, env)
    cfg.session_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(cfg.session_path).close()  # login qilingan deb hisoblaymiz

    # ------------------------------------------------ 1. archiver xizmati
    tg = telegram()
    stop = asyncio.Event()
    phase = {"events_sent": False}

    async def sleep(_seconds: float) -> None:
        await asyncio.sleep(0.01)
        media, rt = service.media, service.realtime
        if service.state != "idle" or media is None or media.state != "idle":
            return
        if not phase["events_sent"]:
            phase["events_sent"] = True
            del tg.history[ALI_ID][6]  # Telegram'da o'chirildi
            new = pm(8, message="real vaqt salom")
            tg.add(ALI_ID, new)
            await tg.emit(events.NewMessage, events.NewMessage.Event(new))
            edited = pm(5, message="tahrirlangan salom", edit_date=new.date)
            await tg.emit(events.MessageEdited, events.MessageEdited.Event(edited))
            await tg.emit(events.MessageDeleted, events.MessageDeleted.Event(deleted_ids=[6], peer=None))
        elif rt.queue.empty() and service.maintenance.state == "idle" and list_backups(cfg.paths.backups):
            stop.set()

    service = ArchiverService(cfg, client_factory=lambda _: tg, stop=stop, sleep=sleep)
    assert asyncio.run(asyncio.wait_for(service.run(), timeout=60)) == EXIT_OK

    async def check_archive() -> None:
        db = Database(cfg.database_url)
        async with db.read() as session:
            rows = {
                m.message_id: m for m in await session.scalars(select(Message).where(Message.chat_id == ALI_ID))
            }
            versions = list(await session.scalars(select(MessageVersion)))
            media = {m.media_type.value: m for m in await session.scalars(select(Media))}
        await db.dispose()
        assert sorted(rows) == list(range(1, 9))
        assert rows[8].text == "real vaqt salom" and rows[8].source.value == "realtime"
        assert rows[5].text == "tahrirlangan salom" and [v.text for v in versions] == ["salom 5"]
        assert rows[6].deletion_status is DeletionStatus.CONFIRMED and rows[6].text == "salom 6"
        assert media["photo"].status is MediaStatus.DONE
        assert media["document"].skip_reason is MediaSkipReason.TOO_LARGE

    asyncio.run(check_archive())
    [backup] = list_backups(cfg.paths.backups)
    assert backup.encrypted

    # ------------------------------------------------------ 2. admin bot
    bot_cfg = load_settings(BotSettings, env)

    async def use_bot() -> RecordingSession:
        db = Database(bot_cfg.database_url)
        session = RecordingSession()
        bot = Bot(TOKEN, session=session)
        dp = build_dispatcher(db, bot_cfg)
        for text in ("real vaqt", "/deleted", "/media", "/status", "/export chat:7 json"):
            await dp.feed_update(bot, text_update(text))
        await db.dispose()
        return session

    session = asyncio.run(use_bot())
    texts = [m.text for m in session.of(SendMessage)]
    assert "real vaqt salom" in texts[0]
    assert "salom 6" in texts[1]
    assert "katta fayllar 1 (2.0 MB)" in texts[2]
    assert "Zaxira: oxirgi" in texts[3] and "shifrlangan" in texts[3]
    [document] = session.of(SendDocument)
    assert document.caption.startswith("📦 8 xabar")
    [export_zip] = list(bot_cfg.paths.exports.glob("export-*.zip"))
    with zipfile.ZipFile(export_zip) as zf:
        data = json.loads(zf.read("export.json"))
    by_id = {m["message_id"]: m for m in data["messages"]}
    assert by_id[5]["versions"][0]["text"] == "salom 5" and by_id[6]["deletion"]["status"] == "confirmed"

    # --------------------------------------------- 3. status, doctor, zaxira
    capsys.readouterr()
    assert main([*cli, "status"]) == EXIT_OK
    status = capsys.readouterr().out
    assert "Real vaqt" not in status or "yangi 1" in status
    assert "Media: yuklangan 1" in status and "shifrlangan" in status
    assert main([*cli, "doctor", "--offline"]) == EXIT_OK
    assert "Natija: 0 xato" in capsys.readouterr().out

    # Avtomatik zaxira xizmat ishga tushishi bilan (import tugamasdan) olingan — import'dan keyin qo'lda yana bittasi
    assert main([*cli, "backup", "create"]) == EXIT_OK
    latest = list_backups(cfg.paths.backups)[0]
    assert latest.path != backup.path
    assert main([*cli, "backup", "verify", latest.path.name, "--identity", str(key)]) == EXIT_OK

    with closing(sqlite3.connect(cfg.paths.db)) as conn:  # avariya: xabarlar yo'qoldi
        conn.execute("DELETE FROM messages")
        conn.commit()
    assert main([*cli, "backup", "restore", latest.path.name, "--identity", str(key), "--yes"]) == EXIT_OK
    with closing(sqlite3.connect(cfg.paths.db)) as conn:
        assert conn.execute("SELECT count(*) FROM messages WHERE chat_id = ?", (ALI_ID,)).fetchone()[0] == 8
