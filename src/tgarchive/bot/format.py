"""Bot matnlari: HTML-escape, vaqt mintaqasi, qisqartirish va Telegram chegaralari.

Telegram chegaralari: xabar matni 4096 belgi, caption 1024 belgi, callback_data 64 bayt.
Barcha foydalanuvchi va arxiv matnlari ``esc`` orqali o'tadi — parse_mode=HTML.
"""

from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from tgarchive.db.enums import ArchiveMode, ChatType, DeletionStatus, MediaType

MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024
SAFE_LIMIT = 3900  # HTML teglari va qo'shimcha qatorlar uchun zaxira
SNIPPET = 90

CHAT_TYPES = {
    ChatType.PRIVATE: "shaxsiy",
    ChatType.BOT: "bot",
    ChatType.SAVED: "Saqlanganlar",
    ChatType.GROUP: "guruh",
    ChatType.SUPERGROUP: "superguruh",
    ChatType.CHANNEL: "kanal",
}
MODES = {ArchiveMode.FULL: "to'liq", ArchiveMode.WINDOW: "oxirgi postlar", ArchiveMode.SKIP: "arxivlanmaydi"}
DELETION = {DeletionStatus.CONFIRMED: "o'chirilgan (tasdiqlangan)", DeletionStatus.PROBABLE: "o'chirilgan (ehtimoliy)"}
MEDIA_ICONS = {
    MediaType.PHOTO: "🖼",
    MediaType.VIDEO: "🎬",
    MediaType.VOICE: "🎤",
    MediaType.AUDIO: "🎵",
    MediaType.DOCUMENT: "📄",
    MediaType.ANIMATION: "🎞",
    MediaType.STICKER: "🔖",
    MediaType.VIDEO_NOTE: "⏺",
}
MEDIA_NAMES = {
    MediaType.PHOTO: "rasm",
    MediaType.VIDEO: "video",
    MediaType.VOICE: "ovozli xabar",
    MediaType.AUDIO: "audio",
    MediaType.DOCUMENT: "hujjat",
    MediaType.ANIMATION: "GIF",
    MediaType.STICKER: "stiker",
    MediaType.VIDEO_NOTE: "video xabar",
}


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def local_time(value: datetime | None, tz: ZoneInfo) -> str:
    if value is None:
        return "-"
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def local_hm(value: datetime | None, tz: ZoneInfo) -> str:
    """Faqat soat:daqiqa — suhbatni o'qishda sana alohida ajratkichda ko'rsatiladi."""
    if value is None:
        return "--:--"
    return value.astimezone(tz).strftime("%H:%M")


def local_day(value: datetime | None, tz: ZoneInfo) -> str:
    if value is None:
        return "-"
    return value.astimezone(tz).strftime("%Y-%m-%d")


def shorten(value: str | None, limit: int = SNIPPET) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def media_label(media_type: MediaType | None) -> str:
    if media_type is None:
        return ""
    return f"{MEDIA_ICONS.get(media_type, '📎')} {MEDIA_NAMES.get(media_type, media_type.value)}"


def chat_label(title: str | None, chat_id: int) -> str:
    return title or str(chat_id)


def escape_within(value: str, budget: int) -> tuple[str, bool]:
    """HTML-escape qilingan matn ``budget`` belgiga sig'adigan qilib qisqartiriladi.

    Avval xom matn kesiladi, keyin escape qilinadi — ``&amp;`` kabi belgi o'rtasidan kesilmaydi.
    Qaytaradi: (matn, qisqartirildimi).
    """
    escaped = esc(value)
    if len(escaped) <= budget:
        return escaped, False
    # Ikkilik qidiruv: escape qilingandan keyin "…" bilan birga sig'adigan eng uzun xom bo'lak
    low, high = 0, min(len(value), budget)
    while low < high:
        mid = (low + high + 1) // 2
        if len(esc(value[:mid])) <= budget - 1:
            low = mid
        else:
            high = mid - 1
    return esc(value[:low].rstrip()) + "…", True


def sender_label(
    *, is_outgoing: bool, sender_name: str | None, sender_id: int | None, chat_id: int, chat_title: str | None
) -> str:
    """Yuboruvchi nomi. Telegram nom bermagan bo'lsa, shaxsiy chatda suhbatdosh — chatning o'zi."""
    if is_outgoing:
        return "Men"
    if sender_name:
        return sender_name
    if sender_id is not None and sender_id == chat_id and chat_title:
        return chat_title
    return str(sender_id) if sender_id is not None else "-"


def split_text(value: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Uzun matnni qator chegarasida (iloji bo'lsa) ``limit`` dan oshmaydigan bo'laklarga bo'ladi."""
    chunks: list[str] = []
    rest = value
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest or not chunks:
        chunks.append(rest)
    return chunks


def fit(parts: list[str], limit: int = SAFE_LIMIT) -> str:
    """Qatorlarni birlashtiradi; sig'masa, oxirgilari tashlab yuboriladi va bu ko'rsatiladi."""
    out: list[str] = []
    size = 0
    for index, part in enumerate(parts):
        if size + len(part) + 1 > limit:
            out.append(f"… yana {len(parts) - index} qator sig'madi")
            break
        out.append(part)
        size += len(part) + 1
    return "\n".join(out)
