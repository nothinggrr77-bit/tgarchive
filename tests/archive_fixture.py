"""Qidiruv va bot testlari uchun haqiqiy oqim orqali to'ldirilgan arxiv.

Ali (id 7) bilan shaxsiy chat:
  1 rasm (yuklangan)            2 kino.zip 2 MB (katta fayl, 1 MB chegara)
  3 "<b>xavfli</b> & matn salom" 4 "salom javob" (3 ga reply)
  5 "eski" -> tahrir: "yangi matn salom"
  6 "o'chirilgan xabar" (o'chirilgan, tasdiqlangan)
  7 juda uzun matn (6000 belgi)  8 chiquvchi: "men yozdim"
Kanal "Yangiliklar": 2 ta post.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from telethon.tl import types

from factories import at
from fake_telegram import FakeClock, FakeTelegram, RecordingSleep
from tgarchive.archiver.convert import message_to_record
from tgarchive.archiver.dialogs import sync_dialogs
from tgarchive.archiver.history import HistoryImporter
from tgarchive.archiver.media import MediaWorker
from tgarchive.config import ArchiverSettings
from tgarchive.db.engine import Database
from tgarchive.db.enums import MessageSource
from tgarchive.db.models import Media, Message
from tgarchive.db.store import apply_deletions, get_message, save_message
from tl_objects import ALI_ID, CHANNEL_ID, CHANNEL_RAW_ID, ME_ID, channel, document_media, message, photo_media, user

VALID_HASH = "0123456789abcdef0123456789abcdef"
LONG_TEXT = "uzun " * 1200
PHOTO_BYTES = b"P" * 5000


@dataclass
class Archive:
    pk: dict[int, int]  # Ali chatidagi message_id -> messages.id
    media: dict[int, int]  # message_id -> media.id
    channel_id: int = CHANNEL_ID


def _pm(i: int, **fields) -> types.Message:
    return message(i, peer=types.PeerUser(ALI_ID), **fields)


async def build_archive(tmp_path: Path, db: Database) -> Archive:
    cfg = ArchiverSettings(
        _env_file=None,
        tg_api_id=1,
        tg_api_hash=VALID_HASH,
        data_dir=tmp_path,
        history_page_size=10,
        history_page_delay_s=0,
        media_max_file_mb=1,
        media_min_free_gb=0,
    )
    tg = FakeTelegram()
    zip_doc = document_media(
        [types.DocumentAttributeFilename(file_name="kino.zip")], mime="application/zip", size=2 * 1024 * 1024, doc_id=302
    )
    tg.add_chat(
        user(),
        _pm(1, media=photo_media(), message=""),
        _pm(2, media=zip_doc, message=""),
        _pm(3, message="<b>xavfli</b> & matn salom"),
        _pm(4, message="salom javob", reply_to=types.MessageReplyHeader(reply_to_msg_id=3)),
        _pm(5, message="eski"),
        _pm(6, message="o'chirilgan xabar"),
        _pm(7, message=LONG_TEXT),
        _pm(8, message="men yozdim", out=True),
    )
    tg.add_chat(
        channel(),
        *[message(i, peer=types.PeerChannel(CHANNEL_RAW_ID), post=True, message="kanal posti") for i in (1, 2)],
    )
    tg.files[(ALI_ID, 1)] = PHOTO_BYTES

    await sync_dialogs(tg, db, cfg)
    await HistoryImporter(tg, db, cfg, self_id=ME_ID, sleep=RecordingSleep(), clock=FakeClock()).run_until_idle()
    disk = lambda _p: SimpleNamespace(free=100 * 1024**3, total=200 * 1024**3)  # noqa: E731
    await MediaWorker(tg, db, cfg, sleep=RecordingSleep(), disk_usage=disk).run_once()

    async with db.write() as session:
        edited = message_to_record(_pm(5, message="yangi matn salom", edit_date=at(50)), self_id=ME_ID)
        await save_message(session, edited, MessageSource.REALTIME)
        await apply_deletions(session, [6], chat_id=ALI_ID)

    async with db.read() as session:
        pk = {i: (await get_message(session, ALI_ID, i)).id for i in range(1, 9)}
        media = {}
        for i in (1, 2):
            row = await session.get(Message, pk[i])
            media[i] = (
                await session.execute(Media.__table__.select().where(Media.message_pk == row.id))
            ).first().id
    return Archive(pk=pk, media=media)
