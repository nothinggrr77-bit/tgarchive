"""Bot ekranlari: bazadan matn va inline tugmalar yasaydi (Telegram'ga so'rov yubormaydi).

Handler'lar faqat shu funksiyalarni chaqirib, natijani yuboradi — shuning uchun ekranlar
alohida testlanadi. Har bir matn ``SAFE_LIMIT`` (≈3900 belgi) dan oshmaydi.
"""

from __future__ import annotations

import secrets
import shutil
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from tgarchive.bot.callbacks import (
    ChatMessages,
    ChatsPage,
    Conversation,
    FetchFile,
    FullText,
    LargePage,
    MediaKind,
    Menu,
    OpenChat,
    OpenMessage,
    SearchPage,
    SendFile,
    Versions,
)
from tgarchive.bot.format import (
    CHAT_TYPES,
    DELETION,
    MEDIA_NAMES,
    MODES,
    SAFE_LIMIT,
    chat_label,
    esc,
    escape_within,
    sender_label,
    fit,
    local_day,
    local_hm,
    local_time,
    media_label,
    shorten,
    split_text,
)
from tgarchive.config import BotSettings
from tgarchive.db.engine import Database
from tgarchive.db.enums import REVIEWABLE_SKIPS, MediaSkipReason, MediaStatus, MediaType
from tgarchive.db.media import (
    FetchRefused,
    format_media_summary,
    format_size,
    list_large_files,
    media_summary,
    request_fetch,
)
from tgarchive.db.meta import LAST_BACKUP_KEY, get_json_meta
from tgarchive.db.models import AuditLog, Chat, DeletionEvent, Media, Message
from tgarchive.db.search import (
    MessageRow,
    SearchQuery,
    archive_stats,
    chat_detail,
    list_chats,
    message_detail,
    search_messages,
)
from tgarchive.db.status import ARCHIVER, format_backup_meta, format_summary, get_heartbeat, import_summary
from tgarchive.db.store import add_audit, get_versions
from tgarchive.db.types import utcnow
from tgarchive.mediafiles import resolve_media_path

BOT_UPLOAD_LIMIT = 50 * 1024 * 1024  # oddiy Bot API orqali yuboriladigan fayl chegarasi
DELETION_RESOLUTIONS = {
    "confirmed": "tasdiqlangan",
    "probable": "ehtimoliy",
    "not_archived": "arxivda nusxa yo'q",
    "ambiguous": "aniqlanmagan (bir nechta nomzod)",
    "revoked": "bekor qilindi (xabar joyida)",
}
EXPORT_HELP = """<b>Eksport</b> — natija zip fayl (HTML yoki JSON)
<code>/export chat:-1001234 after:2026-01-01 before:2026-02-01</code>
<code>/export chat:7 json +media</code>
<code>/export is:deleted</code> — barcha o'chirilganlar
<code>/export all</code> — butun arxiv

Filtrlar /search bilan bir xil. <code>json</code> — JSON (sukut: HTML), <code>+media</code> — yuklangan fayllar ham.
50 MB gacha bo'lsa shu yerga yuboriladi, kattasi serverda qoladi. Eksportlar bir necha kundan keyin serverdan avtomatik o'chiriladi."""
BUTTONS_PER_ROW = 5
QUERY_STORE_SIZE = 500
Order = Literal["date", "deleted"]

# Suhbatni o'qish ("telegramdek"): bitta oynada ko'pi bilan shuncha xabar, har biriga raqam-tugma.
CONV_MAX = 10
CONV_BODY = 260  # bitta xabar matni shu belgidan uzun bo'lsa qisqartiriladi (to'lig'i — tugma orqali)
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"

HELP = """<b>Buyruqlar</b>
/search <i>matn</i> — qidirish (yoki shunchaki matn yuboring)
/recent — so'nggi xabarlar
/chats — arxivdagi chatlar (chatni bosib, suhbatni Telegram'dagidek o'qiysiz)
/deleted — o'chirilganligi qayd etilgan xabarlar
/media — media: turlar bo'yicha, katta fayllar; /media <i>nom</i> — fayl nomi bo'yicha
/stats — arxiv statistikasi
/status — archiver, zaxira va server holati
/export <i>filtrlar</i> — eksport (zip: HTML yoki JSON), /export bilan batafsil
/audit — audit jurnali va o'chirish hodisalari

<b>Qidiruv filtrlari</b> (matn bilan birga yoki alohida)
<code>chat:-1001234</code> — chat ID (/chats dan)
<code>from:7</code> — yuboruvchi ID, <code>from:me</code> — mening xabarlarim
<code>after:2026-01-01</code> <code>before:2026-02-01</code> <code>on:2026-01-15</code>
<code>type:photo</code> (video, voice, audio, document, animation, sticker, video_note)
<code>has:media</code> <code>has:reply</code> <code>is:edited</code> <code>is:deleted</code>
<code>name:hisobot</code> — fayl nomi

Misol: <code>/search shartnoma from:7 after:2026-09-01 has:media</code>
Qidiruv so'z boshi bo'yicha: <code>kitob</code> → «kitoblarimiz»."""


@dataclass(frozen=True, slots=True)
class View:
    text: str
    keyboard: InlineKeyboardMarkup | None = None


@dataclass(frozen=True, slots=True)
class FileToSend:
    path: Path
    media_type: MediaType
    caption: str
    file_name: str | None


class QueryStore:
    """Sahifalash uchun qidiruv so'rovlari (callback_data ga sig'maydi). Xotirada, oxirgi 500 ta."""

    def __init__(self, capacity: int = QUERY_STORE_SIZE) -> None:
        self.capacity = capacity
        self._items: OrderedDict[str, tuple[SearchQuery, Order, str | None]] = OrderedDict()

    def put(self, query: SearchQuery, order: Order = "date", title: str | None = None) -> str:
        token = secrets.token_urlsafe(6)
        self._items[token] = (query, order, title)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)
        return token

    def get(self, token: str) -> tuple[SearchQuery, Order, str | None] | None:
        item = self._items.get(token)
        if item is not None:
            self._items.move_to_end(token)
        return item


def _number_buttons(builder: InlineKeyboardBuilder, items: list[tuple[int, str]]) -> None:
    for number, data in items:
        builder.button(text=str(number), callback_data=data)


def _nav(offset: int, size: int, has_next: bool, make) -> list[tuple[str, str]]:
    buttons = []
    if offset > 0:
        buttons.append(("◀️", make(max(0, offset - size))))
    if has_next:
        buttons.append(("▶️", make(offset + size)))
    return buttons


def _keyboard(numbered: list[tuple[int, str]], nav: list[tuple[str, str]], extra: list[tuple[str, str]] = ()) -> InlineKeyboardMarkup | None:
    builder = InlineKeyboardBuilder()
    _number_buttons(builder, numbered)
    rows = [BUTTONS_PER_ROW] * (len(numbered) // BUTTONS_PER_ROW)
    if len(numbered) % BUTTONS_PER_ROW:
        rows.append(len(numbered) % BUTTONS_PER_ROW)
    for text, data in nav:
        builder.button(text=text, callback_data=data)
    if nav:
        rows.append(len(nav))
    for text, data in extra:
        builder.button(text=text, callback_data=data)
        rows.append(1)
    if not rows:
        return None
    builder.adjust(*rows)
    return builder.as_markup()


class Views:
    def __init__(self, db: Database, settings: BotSettings, *, started_at: datetime | None = None) -> None:
        self.db = db
        self.settings = settings
        self.tz = ZoneInfo(settings.display_timezone)
        self.page_size = settings.bot_page_size
        self.queries = QueryStore()
        self.started_at = started_at or utcnow()

    # ------------------------------------------------------------ menyu

    def menu(self) -> View:
        builder = InlineKeyboardBuilder()
        for text, action in (
            ("🕘 So'nggi", "recent"),
            ("💬 Chatlar", "chats"),
            ("🗑 O'chirilgan", "deleted"),
            ("📎 Media", "media"),
            ("📊 Statistika", "stats"),
            ("🩺 Holat", "status"),
            ("❓ Yordam", "help"),
        ):
            builder.button(text=text, callback_data=Menu(action=action).pack())
        builder.adjust(2, 2, 2, 1)
        return View(
            "<b>Telegram arxivi</b>\nQidirish uchun shunchaki matn yuboring yoki /search ishlating.",
            builder.as_markup(),
        )

    def help(self) -> View:
        return View(HELP)

    # ---------------------------------------------------------- qidiruv

    async def search(
        self,
        query: SearchQuery,
        *,
        order: Order = "date",
        offset: int = 0,
        token: str | None = None,
        title: str | None = None,
    ) -> View:
        token = token or self.queries.put(query, order, title)
        async with self.db.read() as session:
            page = await search_messages(session, query, limit=self.page_size, offset=offset, order=order)
        header = title or f"🔎 {esc(query.describe())}"
        if not page.rows:
            return View(f"{header}\n\nHech narsa topilmadi." if offset == 0 else f"{header}\n\nBoshqa natija yo'q.")
        lines = [f"{header} — {offset + 1}–{offset + len(page.rows)}", ""]
        numbered = []
        for index, row in enumerate(page.rows, start=offset + 1):
            lines.append(self._row_line(index, row, order))
            numbered.append((index, OpenMessage(pk=row.pk).pack()))
        nav = _nav(offset, self.page_size, page.has_next, lambda o: SearchPage(token=token, offset=o).pack())
        return View(fit(lines), _keyboard(numbered, nav))

    def _sender(self, row: MessageRow) -> str:
        return sender_label(
            is_outgoing=row.is_outgoing,
            sender_name=row.sender_name,
            sender_id=row.sender_id,
            chat_id=row.chat_id,
            chat_title=row.chat_title,
        )

    def _row_line(self, index: int, row: MessageRow, order: Order) -> str:
        when = local_time(row.deleted_at if order == "deleted" else row.date, self.tz)
        body = shorten(row.text) or media_label(row.media_type) or (f"[{row.action}]" if row.action else "(matnsiz)")
        marks = []
        if row.text and row.media_type:
            marks.append(media_label(row.media_type).split(" ")[0])
        if row.edit_count:
            marks.append("✏️")
        if row.deletion_status:
            marks.append("🗑")
        if row.reply_to_msg_id:
            marks.append("↩️")
        suffix = (" " + "".join(marks)) if marks else ""
        return (
            f"<b>{index}.</b> {when} · <i>{esc(shorten(chat_label(row.chat_title, row.chat_id), 30))}</i> · "
            f"{esc(shorten(self._sender(row), 25))}{suffix}\n    {esc(body)}"
        )

    async def search_page(self, token: str, offset: int) -> View | None:
        stored = self.queries.get(token)
        if stored is None:
            return None
        query, order, title = stored
        return await self.search(query, order=order, offset=offset, token=token, title=title)

    # ------------------------------------------------------------ xabar

    async def message(self, pk: int) -> View:
        async with self.db.read() as session:
            detail = await message_detail(session, pk)
        if detail is None:
            return View("Xabar arxivda topilmadi (o'chirib tashlangan bo'lishi mumkin).")
        m, chat = detail.message, detail.chat
        head = [
            f"💬 <b>{esc(chat_label(chat.title, chat.chat_id))}</b> ({CHAT_TYPES.get(chat.chat_type, chat.chat_type)})"
            f" · xabar #{m.message_id}",
            f"👤 {esc(self._who(m, chat))}"
            + (f" (id {m.sender_id})" if m.sender_id and not m.is_outgoing else "")
            + f" · {local_time(m.date, self.tz)}",
        ]
        if m.reply_to_msg_id:
            if detail.parent is not None:
                p = detail.parent
                head.append(f"↩️ Javob: {esc(self._who(p, chat if p.chat_id == chat.chat_id else None))}: {esc(shorten(p.text, 60) or media_label(p.media_type) or '(matnsiz)')}")
            else:
                head.append(f"↩️ Javob: xabar #{m.reply_to_msg_id} (arxivda yo'q)")
        if m.fwd_from_id or m.fwd_from_name:
            head.append(f"↪️ Forward: {esc(m.fwd_from_name or m.fwd_from_id)} · {local_time(m.fwd_date, self.tz)}")
        if m.edit_count:
            head.append(f"✏️ Tahrirlangan: {m.edit_count} marta, oxirgi {local_time(m.edit_date, self.tz)}")
        elif m.edit_date:
            head.append(f"✏️ Arxivga tushishidan oldin tahrirlangan ({local_time(m.edit_date, self.tz)}), asl matn noma'lum")
        if m.deletion_status:
            head.append(f"🗑 {DELETION[m.deletion_status]}: {local_time(m.deleted_at, self.tz)}")
        if detail.media is not None:
            head.append("📎 " + self._media_line(detail.media))
        elif m.media_type:
            head.append(media_label(m.media_type))
        if detail.reply_count:
            head.append(f"💬 Shu xabarga javoblar: {detail.reply_count}")

        header = "\n".join(head)
        body = m.text or (f"[servis: {m.action}]" if m.action else "")
        shown, truncated = escape_within(body, SAFE_LIMIT - len(header) - 20)
        text = header + (f"\n\n{shown}" if shown else "")

        builder = InlineKeyboardBuilder()
        if detail.parent is not None:
            builder.button(text="↩️ Asl xabar", callback_data=OpenMessage(pk=detail.parent.id).pack())
        if detail.version_count:
            builder.button(text=f"✏️ Versiyalar ({detail.version_count})", callback_data=Versions(pk=pk).pack())
        if truncated:
            builder.button(text="📃 To'liq matn", callback_data=FullText(pk=pk).pack())
        media = detail.media
        if media is not None and media.status is MediaStatus.DONE:
            builder.button(text="📎 Faylni olish", callback_data=SendFile(media_id=media.id).pack())
        elif media is not None and not media.force_download and (
            media.skip_reason in REVIEWABLE_SKIPS or media.status is MediaStatus.FAILED
        ):
            builder.button(text="⬇️ Yuklab olish", callback_data=FetchFile(media_id=media.id).pack())
        builder.button(text="📖 Chatni o'qish", callback_data=Conversation(chat_id=chat.chat_id).pack())
        builder.adjust(2)
        return View(text, builder.as_markup())

    @staticmethod
    def _who(message: Message, chat) -> str:
        return sender_label(
            is_outgoing=message.is_outgoing,
            sender_name=message.sender_name,
            sender_id=message.sender_id,
            chat_id=message.chat_id,
            chat_title=chat.title if chat is not None else None,
        )

    @staticmethod
    def _media_line(media: Media) -> str:
        name = esc(media.file_name) + ", " if media.file_name else ""
        size = format_size(media.stored_size or media.expected_size)
        label = media_label(media.media_type)
        if media.status is MediaStatus.DONE:
            state = "yuklangan"
        elif media.skip_reason is MediaSkipReason.TOO_LARGE:
            state = "<b>katta fayl</b>, yuklanmagan"
        elif media.status is MediaStatus.SKIPPED:
            state = f"yuklanmaydi ({media.skip_reason.value if media.skip_reason else '-'})"
        elif media.status is MediaStatus.FAILED:
            state = "yuklab bo'lmadi"
        elif media.status is MediaStatus.UNAVAILABLE:
            state = "Telegram'da yo'q"
        else:
            state = "navbatda"
        return f"{label}: {name}{size} — {state}"

    async def full_text(self, pk: int) -> list[str]:
        """To'liq matn bo'laklari — ODDIY matn (parse_mode'siz) yuborilishi kerak, escape qilinmagan."""
        async with self.db.read() as session:
            message = await session.get(Message, pk)
        if message is None or not message.text:
            return ["Matn yo'q."]
        return split_text(message.text, SAFE_LIMIT)

    async def versions(self, pk: int) -> View:
        async with self.db.read() as session:
            message = await session.get(Message, pk)
            versions = await get_versions(session, pk) if message else []
        if message is None:
            return View("Xabar topilmadi.")
        lines = [f"✏️ <b>Xabar #{message.message_id} versiyalari</b>", ""]
        for v in versions:
            lines.append(
                f"<b>{v.version_no}.</b> {local_time(v.observed_at, self.tz)} → {local_time(v.replaced_at, self.tz)}\n"
                f"{esc(shorten(v.text, 600) or media_label(v.media_type) or '(matnsiz)')}"
            )
        lines.append(f"<b>Joriy</b> ({local_time(message.edit_date, self.tz)}):\n{esc(shorten(message.text, 600) or '(matnsiz)')}")
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Xabar", callback_data=OpenMessage(pk=pk).pack())
        return View(fit(lines), builder.as_markup())

    # ----------------------------------------------------------- chatlar

    async def chats(self, offset: int = 0) -> View:
        async with self.db.read() as session:
            page = await list_chats(session, limit=self.page_size, offset=offset)
        if not page.rows:
            return View("Arxivda hali chat yo'q.")
        lines = [f"💬 <b>Chatlar</b> — {offset + 1}–{offset + len(page.rows)}", ""]
        numbered = []
        for index, row in enumerate(page.rows, start=offset + 1):
            lines.append(
                f"<b>{index}.</b> {esc(shorten(chat_label(row.title, row.chat_id), 40))} "
                f"({CHAT_TYPES.get(row.chat_type, row.chat_type)}, {MODES.get(row.archive_mode, '-')})\n"
                f"    {row.message_count} xabar · oxirgi {local_time(row.last_date, self.tz)} · <code>{row.chat_id}</code>"
            )
            numbered.append((index, Conversation(chat_id=row.chat_id).pack()))
        nav = _nav(offset, self.page_size, page.has_next, lambda o: ChatsPage(offset=o).pack())
        return View(fit(lines), _keyboard(numbered, nav))

    async def chat(self, chat_id: int) -> View:
        async with self.db.read() as session:
            detail = await chat_detail(session, chat_id)
        if detail is None:
            return View("Chat topilmadi.")
        c, st = detail.chat, detail.state
        lines = [
            f"💬 <b>{esc(chat_label(c.title, c.chat_id))}</b>",
            f"Tur: {CHAT_TYPES.get(c.chat_type, c.chat_type)} · rejim: {MODES.get(c.archive_mode, '-')}"
            + (f" · @{esc(c.username)}" if c.username else ""),
            f"ID: <code>{c.chat_id}</code>",
            f"Xabarlar: {detail.message_count} ({local_time(detail.first_date, self.tz)} — {local_time(detail.last_date, self.tz)})",
            f"Tahrirlangan: {detail.edited_count} · o'chirilgan: {detail.deleted_count} · yuklangan fayllar: {detail.media_done}",
        ]
        if c.is_protected:
            lines.append("🔒 Himoyalangan kontent: fayllar yuklanmaydi")
        if st is not None:
            lines.append(f"Tarix importi: {st.backfill_status.value}, import qilingan {st.backfill_imported}")
            if st.last_error:
                lines.append(f"Oxirgi xato: {esc(shorten(st.last_error, 150))}")
        lines.append(f"\nShu chatda qidirish: <code>/search chat:{c.chat_id} matn</code>")
        builder = InlineKeyboardBuilder()
        builder.button(text="📖 O'qish", callback_data=Conversation(chat_id=chat_id).pack())
        builder.button(text="🗑 O'chirilganlar", callback_data=ChatMessages(chat_id=chat_id, deleted=True).pack())
        builder.adjust(2)
        return View("\n".join(lines), builder.as_markup())

    async def chat_messages(self, chat_id: int, *, deleted: bool) -> View:
        async with self.db.read() as session:
            detail = await chat_detail(session, chat_id)
        name = esc(chat_label(detail.chat.title if detail else None, chat_id))
        if deleted:
            return await self.search(
                SearchQuery(chat_id=chat_id, deleted=True), order="deleted", title=f"🗑 {name}: o'chirilganlar"
            )
        return await self.search(SearchQuery(chat_id=chat_id), title=f"🕘 {name}: so'nggi xabarlar")

    # ------------------------------------------------------ suhbat (o'qish)

    async def conversation(self, chat_id: int, offset: int = 0) -> View:
        """Chatni Telegram'dagidek o'qish: eng yangi oynadan boshlab, eskisi tepada, yangisi pastda.

        ``⬆️ Eskiroq`` oynani tarixga suradi (offset oshadi), ``⬇️ Yangiroq`` qaytaradi.
        Har bir xabarga ① ② … tugmasi — to'liq matn, media va javob zanjirini ochadi.
        """
        size = min(self.page_size, CONV_MAX)
        async with self.db.read() as session:
            chat = await session.get(Chat, chat_id)
            page = await search_messages(session, SearchQuery(chat_id=chat_id), limit=size, offset=offset)
        if chat is None:
            return View("Chat topilmadi.")
        name = esc(chat_label(chat.title, chat.chat_id))
        header = f"💬 <b>{name}</b> · {CHAT_TYPES.get(chat.chat_type, chat.chat_type)}"
        rows = list(reversed(page.rows))  # vaqt bo'yicha: eskisi tepada

        builder = InlineKeyboardBuilder()
        row_sizes: list[int] = []
        if not rows:
            empty = "Bu chatda xabar yo'q." if offset == 0 else "Boshqa eski xabar yo'q."
            text = f"{header}\n\n{empty}"
        else:
            lines = [header]
            last_day = None
            markers = 0
            for index, row in enumerate(rows):
                marker = CIRCLED[index] if index < len(CIRCLED) else f"{index + 1}."
                day = local_day(row.date, self.tz)
                if day != last_day:
                    lines.append(f"\n──────  {day}  ──────")
                    last_day = day
                lines.append("")
                lines.append(self._bubble(marker, row))
                builder.button(text=marker, callback_data=OpenMessage(pk=row.pk).pack())
                markers += 1
            for start in range(0, markers, BUTTONS_PER_ROW):
                row_sizes.append(min(BUTTONS_PER_ROW, markers - start))
            text = fit(lines)

        nav: list[tuple[str, str]] = []
        if page.has_next:
            nav.append(("⬆️ Eskiroq", Conversation(chat_id=chat_id, offset=offset + size).pack()))
        if offset > 0:
            nav.append(("⬇️ Yangiroq", Conversation(chat_id=chat_id, offset=max(0, offset - size)).pack()))
        for label, data in nav:
            builder.button(text=label, callback_data=data)
        if nav:
            row_sizes.append(len(nav))

        footer = [
            ("ℹ️ Chat haqida", OpenChat(chat_id=chat_id).pack()),
            ("🗑 O'chirilgan", ChatMessages(chat_id=chat_id, deleted=True).pack()),
            ("💬 Chatlar", Menu(action="chats").pack()),
        ]
        for label, data in footer:
            builder.button(text=label, callback_data=data)
        row_sizes.append(len(footer))
        builder.adjust(*row_sizes)
        return View(text, builder.as_markup())

    def _bubble(self, marker: str, row: MessageRow) -> str:
        who = esc(shorten(self._sender(row), 28))
        head = f"{marker} <b>{who}</b> · {local_hm(row.date, self.tz)}"
        if row.text:
            body = esc(shorten(row.text, CONV_BODY))
        else:
            body = media_label(row.media_type) or (f"[{esc(row.action)}]" if row.action else "<i>(matnsiz)</i>")
        marks = []
        if row.reply_to_msg_id:
            marks.append(f"↳ #{row.reply_to_msg_id}")
        if row.text and row.media_type:
            marks.append(media_label(row.media_type))
        if row.edit_count:
            marks.append("✏️")
        if row.deletion_status:
            marks.append("🗑 o'chirilgan")
        sub = f"\n   <i>{' · '.join(marks)}</i>" if marks else ""
        return f"{head}\n{body}{sub}"

    async def recent(self) -> View:
        return await self.search(SearchQuery(), title="🕘 So'nggi xabarlar")

    async def deleted(self) -> View:
        return await self.search(SearchQuery(deleted=True), order="deleted", title="🗑 O'chirilganligi qayd etilganlar")

    # ------------------------------------------------------------- media

    async def media_menu(self) -> View:
        async with self.db.read() as session:
            summary = await media_summary(session)
        builder = InlineKeyboardBuilder()
        builder.button(text="📦 Katta fayllar", callback_data=LargePage(offset=0).pack())
        for kind in (MediaType.PHOTO, MediaType.VIDEO, MediaType.DOCUMENT, MediaType.VOICE, MediaType.AUDIO, MediaType.VIDEO_NOTE):
            builder.button(text=media_label(kind), callback_data=MediaKind(kind=kind.value).pack())
        builder.adjust(1, 3, 3)
        text = (
            f"📎 <b>Media</b>\n{esc(format_media_summary(summary))}\n\n"
            "Fayl nomi bo'yicha: <code>/media nom</code>"
        )
        return View(text, builder.as_markup())

    async def media_kind(self, kind: str) -> View:
        try:
            media_type = MediaType(kind)
        except ValueError:
            return View("Noma'lum media turi.")
        return await self.search(
            SearchQuery(media_type=media_type), title=f"{media_label(media_type)} — so'nggilari"
        )

    async def media_by_name(self, name: str) -> View:
        return await self.search(SearchQuery(file_name=name[:100]), title=f"📎 Fayl nomi: «{esc(name[:100])}»")

    async def large_files(self, offset: int = 0) -> View:
        async with self.db.read() as session:
            files = await list_large_files(session, limit=self.page_size + 1, offset=offset)
        has_next = len(files) > self.page_size
        files = files[: self.page_size]
        if not files:
            return View("📦 Katta fayllar yo'q." if offset == 0 else "Boshqa katta fayl yo'q.")
        lines = [f"📦 <b>Katta fayllar</b> (yuklanmagan) — {offset + 1}–{offset + len(files)}", ""]
        builder = InlineKeyboardBuilder()
        rows: list[int] = []
        for index, f in enumerate(files, start=offset + 1):
            name = f.file_name or MEDIA_NAMES.get(f.media_type, f.media_type.value)
            lines.append(
                f"<b>{index}.</b> {local_time(f.date, self.tz)} · <b>{format_size(f.size)}</b> · {media_label(f.media_type)}\n"
                f"    {esc(shorten(name, 60))} — <i>{esc(shorten(chat_label(f.chat_title, f.chat_id), 30))}</i>"
            )
            builder.button(text=f"{index}. Xabar", callback_data=OpenMessage(pk=f.message_pk).pack())
            builder.button(text=f"⬇️ {index}. Yuklab olish", callback_data=FetchFile(media_id=f.media_id).pack())
            rows.append(2)
        for text, data in _nav(offset, self.page_size, has_next, lambda o: LargePage(offset=o).pack()):
            builder.button(text=text, callback_data=data)
        builder.adjust(*rows, 2)
        lines.append("\n«Yuklab olish» — fayl hajm chegarasiga qaramay archiver navbatiga qo'yiladi.")
        return View(fit(lines), builder.as_markup())

    async def fetch(self, media_id: int) -> str:
        try:
            async with self.db.write() as session:
                media = await request_fetch(session, media_id)
                await add_audit(
                    session, "media_fetch_requested", actor="owner", actor_id=self.settings.owner_id, target=f"media:{media.id}"
                )
        except FetchRefused as exc:
            return str(exc)
        return "Navbatga qo'yildi. Archiver yuklab bo'lgach, xabardagi «📎 Faylni olish» tugmasi chiqadi."

    async def file_to_send(self, media_id: int) -> FileToSend | str:
        async with self.db.read() as session:
            media = await session.get(Media, media_id)
            message = await session.get(Message, media.message_pk) if media else None
        if media is None or message is None or media.status is not MediaStatus.DONE or not media.local_path:
            return "Fayl hali yuklanmagan."
        try:
            path = resolve_media_path(self.settings.paths.media, media.local_path)
        except ValueError:
            return "Fayl yo'li noto'g'ri."
        if not path.is_file():
            return "Fayl diskda topilmadi — archiver uni qayta yuklaydi (tgarchive media verify)."
        size = path.stat().st_size
        if size > BOT_UPLOAD_LIMIT:
            return (
                f"Fayl {format_size(size)}: Bot API 50 MB dan katta faylni yubora olmaydi.\n"
                f"Serverda: <code>{esc(media.local_path)}</code>"
            )
        async with self.db.write() as session:
            await add_audit(
                session, "bot_send_file", actor="owner", actor_id=self.settings.owner_id, target=f"media:{media_id}"
            )
        caption = esc(shorten(message.text, 900)) if message.text else ""
        return FileToSend(path, media.media_type, caption, media.file_name)

    # ------------------------------------------------------- statistika

    async def stats(self) -> View:
        async with self.db.read() as session:
            stats = await archive_stats(session)
            media = await media_summary(session)
        modes = ", ".join(f"{MODES.get(k, k)}: {v}" for k, v in sorted(stats.chats_by_mode.items(), key=str)) or "-"
        lines = [
            "📊 <b>Arxiv statistikasi</b>",
            f"Xabarlar: <b>{stats.total_messages}</b> ({local_time(stats.first_date, self.tz)} — {local_time(stats.last_date, self.tz)})",
            f"Tahrirlangan: {stats.edited_messages} (eski versiyalar: {stats.versions})",
            f"O'chirilgan: tasdiqlangan {stats.deleted_confirmed}, ehtimoliy {stats.deleted_probable}",
            f"Chatlar: {esc(modes)}",
            "Tarix importi: " + (", ".join(f"{k}: {v}" for k, v in sorted(stats.backfill_by_status.items())) or "-"),
            esc(format_media_summary(media)),
            f"Baza hajmi: {format_size(self._db_size())}",
        ]
        if stats.top_chats:
            lines.append("\n<b>Eng ko'p xabarli chatlar</b>")
            lines += [
                f"{i}. {esc(shorten(chat_label(c.title, c.chat_id), 40))} — {c.message_count}"
                for i, c in enumerate(stats.top_chats, start=1)
            ]
        return View(fit(lines))

    async def status(self) -> View:
        async with self.db.read() as session:
            summary = await import_summary(session)
            heartbeat = await get_heartbeat(session, ARCHIVER)
            media = await media_summary(session)
            backup = await get_json_meta(session, LAST_BACKUP_KEY)
        now = utcnow()
        lines = [
            "🩺 <b>Holat</b>",
            esc(format_summary(summary, heartbeat, now)),
            esc(format_media_summary(media)),
            esc(format_backup_meta(backup, now)),
        ]
        if heartbeat is not None and (now - heartbeat.last_beat_at).total_seconds() > 180:
            lines.append("⚠️ Archiver 3 daqiqadan beri signal bermayapti — u to'xtagan bo'lishi mumkin.")
        media_dir = self.settings.paths.media
        if media_dir.exists():
            usage = shutil.disk_usage(media_dir)
            lines.append(f"Disk: bo'sh {format_size(usage.free)} / {format_size(usage.total)}")
        lines.append(f"Baza: {format_size(self._db_size())}")
        uptime = int((now - self.started_at).total_seconds())
        lines.append(f"Bot: ishlayapti {uptime // 3600} soat {uptime % 3600 // 60} daqiqa")
        return View(fit(lines))

    async def audit(self) -> View:
        async with self.db.read() as session:
            entries = list(await session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(15)))
            deletions = list(await session.scalars(select(DeletionEvent).order_by(DeletionEvent.id.desc()).limit(10)))
        lines = ["🧾 <b>Audit jurnali</b> (oxirgi 15)"]
        for e in entries:
            who = e.actor + (f" {e.actor_id}" if e.actor_id else "")
            target = f" → {esc(shorten(e.target, 40))}" if e.target else ""
            lines.append(f"{local_time(e.created_at, self.tz)} · {esc(who)} · <b>{esc(e.action)}</b>{target}{'' if e.ok else ' ❌'}")
        if not entries:
            lines.append("bo'sh")
        lines.append("\n🗑 <b>O'chirish hodisalari</b> (oxirgi 10)")
        for d in deletions:
            chat = d.resolved_chat_id or d.chat_id or "?"
            lines.append(
                f"{local_time(d.detected_at, self.tz)} · #{d.message_id} (chat {chat}) · "
                f"{esc(DELETION_RESOLUTIONS.get(d.resolution.value, d.resolution.value))} · {esc(d.source.value)}"
            )
        if not deletions:
            lines.append("bo'sh")
        return View(fit(lines))

    def _db_size(self) -> int:
        path = self.settings.paths.db
        return sum(p.stat().st_size for p in (path, Path(f"{path}-wal")) if p.exists())

