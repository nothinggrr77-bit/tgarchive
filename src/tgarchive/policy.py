"""Chat bo'yicha arxivlash siyosati: arxivlanadimi, qanday rejimda, media yuklanadimi.

Ustuvorlik tartibi (yuqoridagisi kuchliroq):

1. ``CHAT_DENYLIST`` — mutlaq: hech qachon arxivlanmaydi.
2. Egasining per-chat qarori (``chats.archive_override``, bot orqali o'rnatiladi).
3. ``CHAT_ALLOWLIST`` (bo'sh bo'lmasa) va chat turi bo'yicha sozlamalar (``ARCHIVE_*``).

Rejim: kanallar ``CHANNEL_KEEP_POSTS > 0`` bo'lsa ``WINDOW`` (oxirgi N ta post),
qolgan hammasi ``FULL`` (doimiy). Himoyalangan (noforwards) chatlardan media hech
qachon yuklanmaydi — bu qoidani sozlama yoki override o'zgartira olmaydi.
"""

from __future__ import annotations

from dataclasses import dataclass

from tgarchive.config import ArchiverSettings
from tgarchive.db.enums import ArchiveMode, ChatType
from tgarchive.db.store import ChatInfo

_TYPE_SETTING = {
    ChatType.PRIVATE: "archive_private",
    ChatType.BOT: "archive_bots",
    ChatType.SAVED: "archive_saved",
    ChatType.GROUP: "archive_groups",
    ChatType.SUPERGROUP: "archive_groups",
    ChatType.CHANNEL: "archive_channels",
}


@dataclass(frozen=True, slots=True)
class ChatPolicy:
    mode: ArchiveMode
    reason: str
    keep_last: int | None = None  # WINDOW rejimida saqlanadigan postlar soni
    media: bool = False
    media_reason: str = ""
    comments: bool = False  # kanal postlariga yozilgan izohlarni saqlash

    @property
    def archived(self) -> bool:
        return self.mode is not ArchiveMode.SKIP


def _skip(reason: str) -> ChatPolicy:
    return ChatPolicy(mode=ArchiveMode.SKIP, reason=reason, media_reason="arxivlanmaydi")


def decide_policy(
    chat: ChatInfo,
    settings: ArchiverSettings,
    *,
    archive_override: bool | None = None,
    media_override: bool | None = None,
) -> ChatPolicy:
    if chat.chat_id in settings.chat_denylist:
        return _skip("CHAT_DENYLIST da")
    if archive_override is False:
        return _skip("egasi o'chirgan")
    if archive_override is not True:
        if settings.chat_allowlist and chat.chat_id not in settings.chat_allowlist:
            return _skip("CHAT_ALLOWLIST da yo'q")
        setting = _TYPE_SETTING[chat.chat_type]
        if not getattr(settings, setting):
            return _skip(f"{setting.upper()}=false")

    if chat.chat_type is ChatType.CHANNEL and settings.channel_keep_posts > 0:
        mode, keep_last = ArchiveMode.WINDOW, settings.channel_keep_posts
        reason = f"kanal: oxirgi {keep_last} ta post"
    else:
        mode, keep_last = ArchiveMode.FULL, None
        reason = "egasi yoqqan" if archive_override else "to'liq, doimiy"

    media, media_reason = media_decision(chat, settings, media_override)
    return ChatPolicy(
        mode=mode,
        reason=reason,
        keep_last=keep_last,
        media=media,
        media_reason=media_reason,
        comments=chat.chat_type is ChatType.CHANNEL and settings.channel_save_comments,
    )


def media_decision(chat: ChatInfo, settings: ArchiverSettings, override: bool | None) -> tuple[bool, str]:
    if chat.is_protected:
        return False, "himoyalangan kontent"
    if override is not None:
        return override, "egasi qarori"
    if not settings.media_enabled:
        return False, "MEDIA_ENABLED=false"
    if chat.chat_type is ChatType.CHANNEL and not settings.media_in_channels:
        return False, "MEDIA_IN_CHANNELS=false"
    return True, ",".join(sorted(settings.media_types))
