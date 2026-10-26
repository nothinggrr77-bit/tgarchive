"""9-bosqich: admin bot — ekranlar, egasini tekshirish, buyruqlar va tugmalar (tarmoqsiz)."""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    SendDocument,
    SendMessage,
    SendPhoto,
    SetMyCommands,
)
from sqlalchemy import select

from archive_fixture import LONG_TEXT, Archive, build_archive
from fake_botapi import OWNER, STRANGER, TOKEN, RecordingSession, callback_update, text_update
from tgarchive.bot import views as views_module
from tgarchive.bot.app import BOT_COMPONENT, build_dispatcher, wait_for_schema
from tgarchive.bot.callbacks import (
    ChatMessages,
    Conversation,
    FetchFile,
    FullText,
    Menu,
    OpenChat,
    OpenMessage,
    SearchPage,
    SendFile,
)
from tgarchive.bot.format import MESSAGE_LIMIT, escape_within, split_text
from tgarchive.bot.views import Views
from tgarchive.cli import main
from tgarchive.config import BotSettings
from tgarchive.db.engine import Database
from tgarchive.db.models import AuditLog, Media
from tgarchive.db.search import SearchQuery
from tgarchive.db.status import ARCHIVER, beat
from tgarchive.db.types import utcnow
from tgarchive.exitcodes import EXIT_OK

CALLBACK_LIMIT = 64


def settings(tmp_path: Path, **overrides: Any) -> BotSettings:
    values = {"bot_page_size": 2} | overrides
    return BotSettings(_env_file=None, bot_token=TOKEN, owner_id=OWNER, data_dir=tmp_path, **values)


@pytest.fixture
async def archive(db: Database, tmp_path: Path) -> Archive:
    return await build_archive(tmp_path, db)


@pytest.fixture
def views(db: Database, tmp_path: Path) -> Views:
    return Views(db, settings(tmp_path))


def callbacks(view) -> list[str]:
    if view.keyboard is None:
        return []
    return [b.callback_data for row in view.keyboard.inline_keyboard for b in row]


def button_texts(view) -> list[str]:
    return [b.text for row in (view.keyboard.inline_keyboard if view.keyboard else []) for b in row]


# ------------------------------------------------------------------ formatlash


def test_escape_within_never_cuts_an_entity() -> None:
    text, cut = escape_within("&" * 50, 23)
    assert cut and text.endswith("…") and len(text) <= 23
    assert re.fullmatch(r"(&amp;)+…", text)
    assert escape_within("<b>", 100) == ("&lt;b&gt;", False)


def test_split_text_respects_limit() -> None:
    chunks = split_text(LONG_TEXT, 1000)
    assert all(len(c) <= 1000 for c in chunks) and len(chunks) == 7
    assert "".join(chunks).replace(" ", "") == LONG_TEXT.replace(" ", "")


# ---------------------------------------------------------------- ekranlar


async def test_search_view_escapes_html_and_paginates(views: Views, archive: Archive) -> None:
    view = await views.search(SearchQuery(text="salom"))
    assert "&lt;b&gt;xavfli" not in view.text  # 1-sahifada 5 va 4
    assert "yangi matn salom" in view.text and view.text.startswith("🔎 «salom» — 1–2")
    data = callbacks(view)
    assert OpenMessage(pk=archive.pk[5]).pack() in data
    nxt = next(d for d in data if d.startswith("sp:"))
    token, offset = SearchPage.unpack(nxt).token, SearchPage.unpack(nxt).offset
    second = await views.search_page(token, offset)
    assert "&lt;b&gt;xavfli&lt;/b&gt; &amp; matn" in second.text and "<b>xavfli" not in second.text
    assert "◀️" in button_texts(second) and "▶️" not in button_texts(second)
    assert await views.search_page("yo'q-token", 0) is None
    assert all(len(d.encode()) <= CALLBACK_LIMIT for d in data)


async def test_empty_search(views: Views, archive: Archive) -> None:
    view = await views.search(SearchQuery(text="mavjudemas"))
    assert "Hech narsa topilmadi" in view.text and view.keyboard is None


async def test_message_views(views: Views, archive: Archive) -> None:
    reply = await views.message(archive.pk[4])
    assert "↩️ Javob: Ali: &lt;b&gt;xavfli" in reply.text and "↩️ Asl xabar" in button_texts(reply)

    edited = await views.message(archive.pk[5])
    assert "✏️ Tahrirlangan: 1 marta" in edited.text and "✏️ Versiyalar (1)" in button_texts(edited)
    versions = await views.versions(archive.pk[5])
    assert "eski" in versions.text and "yangi matn salom" in versions.text

    deleted = await views.message(archive.pk[6])
    assert "o'chirilgan (tasdiqlangan)" in deleted.text and "o'chirilgan xabar" in deleted.text

    big = await views.message(archive.pk[2])
    assert "kino.zip, 2.0 MB — <b>katta fayl</b>, yuklanmagan" in big.text
    assert FetchFile(media_id=archive.media[2]).pack() in callbacks(big)

    photo = await views.message(archive.pk[1])
    assert SendFile(media_id=archive.media[1]).pack() in callbacks(photo)

    long = await views.message(archive.pk[7])
    assert len(long.text) <= MESSAGE_LIMIT and long.text.endswith("…")
    assert FullText(pk=archive.pk[7]).pack() in callbacks(long)
    chunks = await views.full_text(archive.pk[7])
    assert all(len(c) <= MESSAGE_LIMIT for c in chunks) and len(chunks) == 2

    assert "topilmadi" in (await views.message(999_999)).text


async def test_chats_and_chat_views(views: Views, archive: Archive) -> None:
    chats = await views.chats()
    assert "Ali" in chats.text and "Yangiliklar" in chats.text and "oxirgi postlar" in chats.text
    chat = await views.chat(7)
    assert "Xabarlar: 8" in chat.text and "o'chirilgan: 1" in chat.text and "<code>/search chat:7 matn</code>" in chat.text
    deleted = await views.chat_messages(7, deleted=True)
    assert "o'chirilgan xabar" in deleted.text and "salom" not in deleted.text


async def test_conversation_reads_like_a_chat(views: Views, archive: Archive) -> None:
    view = await views.conversation(7)  # page_size=2 -> eng yangi 2 ta: #7 (uzun), #8 (chiquvchi)
    assert view.text.startswith("💬 <b>Ali</b> · shaxsiy")
    assert view.text.index("①") < view.text.index("②")  # eskisi tepada
    assert "<b>Men</b>" in view.text and "men yozdim" in view.text  # chiquvchi xabar
    assert "uzun…" in view.text  # uzun xabar qisqartirilgan (keyin keyingi xabar keladi)
    assert "──────" in view.text  # sana ajratkichi
    data = callbacks(view)
    assert OpenMessage(pk=archive.pk[7]).pack() in data and OpenMessage(pk=archive.pk[8]).pack() in data
    assert "⬆️ Eskiroq" in button_texts(view) and "⬇️ Yangiroq" not in button_texts(view)
    assert OpenChat(chat_id=7).pack() in data
    assert ChatMessages(chat_id=7, deleted=True).pack() in data and Menu(action="chats").pack() in data
    assert all(len(d.encode()) <= CALLBACK_LIMIT for d in data)


async def test_conversation_paging_shows_edited_and_deleted(views: Views, archive: Archive) -> None:
    view = await views.conversation(7, offset=2)  # #5 (tahrirlangan) va #6 (o'chirilgan)
    assert "yangi matn salom" in view.text and "✏️" in view.text  # joriy versiya + tahrir belgisi
    assert "o'chirilgan xabar" in view.text and "🗑 o'chirilgan" in view.text
    assert "⬆️ Eskiroq" in button_texts(view) and "⬇️ Yangiroq" in button_texts(view)


async def test_conversation_past_the_end(views: Views, archive: Archive) -> None:
    view = await views.conversation(7, offset=100)
    assert "Boshqa eski xabar yo'q." in view.text
    assert "⬇️ Yangiroq" in button_texts(view) and "⬆️ Eskiroq" not in button_texts(view)
    assert not any(d.startswith("m:") for d in callbacks(view))  # xabar-tugmalari yo'q


async def test_open_chat_from_list_enters_reading_mode(harness: BotHarness, archive: Archive) -> None:
    await harness.feed(text_update("/chats"))
    chat_list = harness.session.of(SendMessage)[-1]
    enter = Conversation(chat_id=7).pack()
    assert enter in [b.callback_data for row in chat_list.reply_markup.inline_keyboard for b in row]
    await harness.feed(callback_update(enter))
    [opened] = harness.session.of(EditMessageText)
    assert "💬 <b>" in opened.text and "──────" in opened.text  # suhbat ekrani

    older = next(b.callback_data for row in opened.reply_markup.inline_keyboard for b in row if b.text == "⬆️ Eskiroq")
    await harness.feed(callback_update(older))
    assert len(harness.session.of(EditMessageText)) == 2  # varaqlash o'sha xabarni almashtiradi


async def test_media_views_and_fetch(views: Views, archive: Archive, db: Database) -> None:
    menu = await views.media_menu()
    assert "katta fayllar 1 (2.0 MB)" in menu.text
    large = await views.large_files()
    assert "kino.zip" in large.text and "2.0 MB" in large.text
    assert FetchFile(media_id=archive.media[2]).pack() in callbacks(large)
    assert OpenMessage(pk=archive.pk[2]).pack() in callbacks(large)

    answer = await views.fetch(archive.media[2])
    assert answer.startswith("Navbatga qo'yildi")
    async with db.read() as session:
        media = await session.get(Media, archive.media[2])
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "media_fetch_requested"))
    assert media.force_download is True and audit.actor_id == OWNER
    assert "yuklangan" in await views.fetch(archive.media[1])  # allaqachon bor — rad etiladi

    photo = await views.media_kind("photo")
    assert "1–1" in photo.text


async def test_file_to_send(views: Views, archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    item = await views.file_to_send(archive.media[1])
    assert not isinstance(item, str) and item.path.read_bytes().startswith(b"P")
    assert await views.file_to_send(archive.media[2]) == "Fayl hali yuklanmagan."
    monkeypatch.setattr(views_module, "BOT_UPLOAD_LIMIT", 100)
    too_big = await views.file_to_send(archive.media[1])
    assert isinstance(too_big, str) and "50 MB" in too_big and "Serverda" in too_big


async def test_stats_and_status_views(views: Views, archive: Archive, db: Database) -> None:
    stats = await views.stats()
    assert "Xabarlar: <b>10</b>" in stats.text and "tasdiqlangan 1" in stats.text
    async with db.write() as session:
        await beat(session, ARCHIVER, "idle", started_at=utcnow())
        from tgarchive.db.status import get_heartbeat

        (await get_heartbeat(session, ARCHIVER)).last_beat_at = utcnow() - timedelta(minutes=10)
    status = await views.status()
    assert "Archiver: idle" in status.text and "3 daqiqadan beri signal bermayapti" in status.text
    assert "Bot: ishlayapti" in status.text


# ------------------------------------------------------- dispatcher oqimi


class BotHarness:
    def __init__(self, db: Database, cfg: BotSettings) -> None:
        self.session = RecordingSession()
        self.bot = Bot(TOKEN, session=self.session)
        self.dp = build_dispatcher(db, cfg)

    async def feed(self, update) -> None:
        await self.dp.feed_update(self.bot, update)

    def texts(self) -> list[str]:
        return [m.text for m in self.session.of(SendMessage)]


@pytest.fixture
def harness(db: Database, tmp_path: Path) -> BotHarness:
    return BotHarness(db, settings(tmp_path))


async def test_owner_start_sets_private_commands_and_menu(harness: BotHarness, archive: Archive) -> None:
    await harness.feed(text_update("/start"))
    [commands] = harness.session.of(SetMyCommands)
    assert commands.scope.chat_id == OWNER  # buyruqlar faqat egasining chatida
    [menu] = harness.session.of(SendMessage)
    assert menu.chat_id == OWNER and "Telegram arxivi" in menu.text and menu.reply_markup is not None


async def test_plain_text_is_a_search(harness: BotHarness, archive: Archive) -> None:
    await harness.feed(text_update("salom"))
    assert harness.texts()[0].startswith("🔎 «salom»")
    await harness.feed(text_update("/search salom after:2026-99-01"))
    assert "Qidiruvda xato" in harness.texts()[1]
    await harness.feed(text_update("/search"))
    assert "Foydalanish" in harness.texts()[2]


@pytest.mark.parametrize("command", ["/recent", "/chats", "/deleted", "/media", "/media kino", "/stats", "/status", "/help", "/export"])
async def test_every_command_answers_owner(harness: BotHarness, archive: Archive, command: str) -> None:
    await harness.feed(text_update(command))
    [answer] = harness.session.of(SendMessage)
    assert answer.chat_id == OWNER and answer.text and len(answer.text) <= MESSAGE_LIMIT


async def test_stranger_gets_nothing_and_is_audited_once(harness: BotHarness, archive: Archive, db: Database) -> None:
    for text in ("/start", "/search salom", "salom"):
        await harness.feed(text_update(text, user_id=STRANGER))
    await harness.feed(callback_update(OpenMessage(pk=archive.pk[3]).pack(), user_id=STRANGER))
    assert harness.session.requests == []  # hech qanday javob, hatto "ruxsat yo'q" ham emas
    async with db.read() as session:
        audits = list(await session.scalars(select(AuditLog).where(AuditLog.action == "unauthorized")))
    assert len(audits) == 1  # 10 daqiqada bir marta
    assert (audits[0].actor, audits[0].actor_id, audits[0].ok) == ("stranger", STRANGER, False)
    assert "salom" not in str(audits[0].details)  # xabar matni yozilmaydi


async def test_owner_in_a_group_is_ignored(harness: BotHarness, archive: Archive, db: Database) -> None:
    await harness.feed(text_update("/recent", chat_id=-100555, chat_type="supergroup"))
    assert harness.session.requests == []
    async with db.read() as session:
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "unauthorized"))
    assert audit.actor == "owner" and audit.details["chat_type"] == "supergroup"


async def test_callbacks(harness: BotHarness, archive: Archive, db: Database) -> None:
    await harness.feed(callback_update(OpenMessage(pk=archive.pk[4]).pack()))
    assert "↩️ Javob" in harness.texts()[-1]

    await harness.feed(text_update("salom"))
    first_page = harness.session.of(SendMessage)[-1]
    nxt = next(b.callback_data for row in first_page.reply_markup.inline_keyboard for b in row if b.callback_data.startswith("sp:"))
    await harness.feed(callback_update(nxt))
    [edited] = harness.session.of(EditMessageText)
    assert edited.message_id == 900 and "xavfli" in edited.text

    await harness.feed(callback_update(SearchPage(token="eskirgan", offset=2).pack()))
    expired = harness.session.of(AnswerCallbackQuery)[-1]
    assert expired.show_alert is True and "eskirgan" in expired.text

    await harness.feed(callback_update(FetchFile(media_id=archive.media[2]).pack()))
    fetched = harness.session.of(AnswerCallbackQuery)[-1]
    assert fetched.show_alert is True and fetched.text.startswith("Navbatga qo'yildi")
    async with db.read() as session:
        assert (await session.get(Media, archive.media[2])).force_download is True

    sent_before = len(harness.session.of(SendMessage))
    await harness.feed(callback_update(FullText(pk=archive.pk[7]).pack()))
    chunks = harness.session.of(SendMessage)[sent_before:]
    assert len(chunks) == 2 and all(c.parse_mode is None for c in chunks)


async def test_send_file_and_fallback_to_document(harness: BotHarness, archive: Archive) -> None:
    await harness.feed(callback_update(SendFile(media_id=archive.media[1]).pack()))
    [photo] = harness.session.of(SendPhoto)
    assert photo.chat_id == OWNER

    harness.session.failures[SendPhoto] = lambda m: TelegramBadRequest(method=m, message="Bad Request: PHOTO_INVALID")
    await harness.feed(callback_update(SendFile(media_id=archive.media[1]).pack()))
    assert len(harness.session.of(SendDocument)) == 1


async def test_handler_error_is_reported_to_owner(harness: BotHarness, archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(self) -> None:
        raise RuntimeError("baza buzildi")

    monkeypatch.setattr(Views, "stats", broken)
    await harness.feed(text_update("/stats"))
    assert harness.texts() == ["⚠️ Ichki xato yuz berdi, tafsiloti server log'ida."]


# ---------------------------------------------------------------- ishga tushish


async def test_wait_for_schema(db: Database, tmp_path: Path) -> None:
    assert await wait_for_schema(db, asyncio.Event()) is True  # db fixture — migratsiya qilingan
    empty = Database(f"sqlite+aiosqlite:///{(tmp_path / 'bo`sh.sqlite3').as_posix()}")
    stop = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    asyncio.get_running_loop().create_task(stop_soon())
    assert await wait_for_schema(empty, stop, interval_s=0.01) is False
    await empty.dispose()


def test_bot_health(tmp_path: Path) -> None:
    env = tmp_path / "test.env"
    env.write_text(f"DATA_DIR={tmp_path.as_posix()}/data\n", encoding="utf-8")
    assert main(["--env-file", str(env), "db-upgrade"]) == EXIT_OK
    cfg = settings(tmp_path / "data")

    async def write() -> None:
        db = Database(cfg.database_url)
        async with db.write() as session:
            await beat(session, BOT_COMPONENT, "running", started_at=utcnow())
        await db.dispose()

    asyncio.run(write())
    assert main(["--env-file", str(env), "health", "--component", "bot"]) == EXIT_OK


# ------------------------------------------------------- 10-bosqich: eksport


async def test_export_command(harness: BotHarness, archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    await harness.feed(text_update("/export"))
    assert "<b>Eksport</b>" in harness.texts()[-1]
    await harness.feed(text_update("/export json"))
    assert "Eksportda xato" in harness.texts()[-1]

    await harness.feed(text_update("/export chat:7 json"))
    assert harness.texts()[-1].startswith("⏳ Eksport tayyorlanmoqda")
    [document] = harness.session.of(SendDocument)
    assert document.chat_id == OWNER and document.caption.startswith("📦 8 xabar, 1 chat")

    await harness.feed(text_update("/export mavjudemas"))
    assert harness.texts()[-1] == "Filtrga mos xabar yo'q — eksport bo'sh."

    from tgarchive.bot import handlers as handlers_module

    monkeypatch.setattr(handlers_module, "BOT_UPLOAD_LIMIT", 10)
    await harness.feed(text_update("/export all"))
    assert "50 MB dan katta" in harness.texts()[-1] and "docker compose cp bot:/data/exports/" in harness.texts()[-1]
    assert len(harness.session.of(SendDocument)) == 1


async def test_audit_and_status_show_backup(harness: BotHarness, archive: Archive) -> None:
    await harness.feed(callback_update(FetchFile(media_id=archive.media[2]).pack()))  # audit yozuvi paydo bo'ladi
    await harness.feed(text_update("/audit"))
    audit = harness.texts()[-1]
    assert "Audit jurnali" in audit and "media_fetch_requested" in audit
    assert "O'chirish hodisalari" in audit and "#6" in audit and "tasdiqlangan" in audit
    await harness.feed(text_update("/status"))
    assert "Zaxira: hali yaratilmagan" in harness.texts()[-1]
