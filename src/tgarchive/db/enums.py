"""Bazada matn sifatida saqlanadigan holat va tur qiymatlari."""

from __future__ import annotations

from enum import StrEnum


class ChatType(StrEnum):
    PRIVATE = "private"  # oddiy foydalanuvchi bilan shaxsiy chat
    BOT = "bot"  # bot bilan shaxsiy chat
    SAVED = "saved"  # Saved Messages (o'zi bilan chat)
    GROUP = "group"  # oddiy (basic) guruh
    SUPERGROUP = "supergroup"  # superguruh, forum ham shu tur
    CHANNEL = "channel"  # broadcast kanal

    @property
    def uses_common_box(self) -> bool:
        """Xabar ID'lari akkaunt bo'yicha umumiy ketma-ketlikdami (common message box).

        Shaxsiy chatlar va oddiy guruhlar bitta umumiy ID ketma-ketligini ishlatadi;
        har bir kanal/superguruhning o'z ketma-ketligi bor.
        """
        return self in COMMON_BOX_TYPES


COMMON_BOX_TYPES = frozenset({ChatType.PRIVATE, ChatType.BOT, ChatType.SAVED, ChatType.GROUP})


class ArchiveMode(StrEnum):
    """Chat qanday arxivlanadi (archiver siyosatidan; bot ko'rsatish uchun o'qiydi)."""

    FULL = "full"  # butun tarix + yangi xabarlar, doimiy
    WINDOW = "window"  # faqat oxirgi N ta post (kanallar)
    SKIP = "skip"  # arxivlanmaydi


class MessageSource(StrEnum):
    HISTORY = "history"  # eski tarix importi (backfill)
    REALTIME = "realtime"  # real vaqt hodisasi
    GAPFILL = "gapfill"  # uzilishdan keyingi bo'shliqni to'ldirish yoki solishtirish


class MediaType(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    VOICE = "voice"
    AUDIO = "audio"
    DOCUMENT = "document"
    ANIMATION = "animation"
    STICKER = "sticker"
    VIDEO_NOTE = "video_note"
    # Fayl bo'lmagan turlar: faqat messages.media_type da qayd etiladi
    WEBPAGE = "webpage"
    CONTACT = "contact"
    LOCATION = "location"
    POLL = "poll"
    DICE = "dice"
    GAME = "game"
    INVOICE = "invoice"
    STORY = "story"
    OTHER = "other"

    @property
    def is_file(self) -> bool:
        return self in FILE_MEDIA_TYPES


FILE_MEDIA_TYPES = frozenset(
    {
        MediaType.PHOTO,
        MediaType.VIDEO,
        MediaType.VOICE,
        MediaType.AUDIO,
        MediaType.DOCUMENT,
        MediaType.ANIMATION,
        MediaType.STICKER,
        MediaType.VIDEO_NOTE,
    }
)


class MediaStatus(StrEnum):
    PENDING = "pending"  # navbatda: siyosat tekshiriladi va yuklanadi
    DOWNLOADING = "downloading"
    DONE = "done"
    FAILED = "failed"  # urinishlar soni tugadi
    SKIPPED = "skipped"  # yuklanmaydi: sabab skip_reason da, metama'lumot saqlanadi
    MISSING = "missing"  # bazada bor, lekin diskda yo'q yoki checksum mos emas — qayta yuklanadi
    UNAVAILABLE = "unavailable"  # Telegram'da endi mavjud emas


class MediaSkipReason(StrEnum):
    TOO_LARGE = "too_large"  # MEDIA_MAX_FILE_MB dan katta — "katta fayl" sifatida ko'rsatiladi
    TYPE = "type"  # turi MEDIA_TYPES da yo'q
    POLICY = "policy"  # MEDIA_ENABLED / MEDIA_IN_CHANNELS / egasining chat bo'yicha qarori
    PROTECTED = "protected"  # himoyalangan kontent (noforwards) — hech qachon yuklanmaydi
    VIEW_ONCE = "view_once"  # bir marta ko'riladigan media (ttl) — hech qachon yuklanmaydi


# Sozlama o'zgarsa qayta ko'rib chiqiladigan sabablar (himoyalangan va view-once — hech qachon)
REVIEWABLE_SKIPS = frozenset({MediaSkipReason.TOO_LARGE, MediaSkipReason.TYPE, MediaSkipReason.POLICY})


class SyncStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"  # faqat tarix oxirigacha xatosiz o'qilganda
    FAILED = "failed"  # keyinroq qayta uriniladi (next_retry_at)
    DISABLED = "disabled"  # siyosat bo'yicha arxivlanmaydi


class DeletionStatus(StrEnum):
    """messages.deletion_status: NULL — o'chirilganlik qayd etilmagan."""

    CONFIRMED = "confirmed"  # chat aniq ko'rsatilgan hodisa yoki tekshiruv bilan tasdiqlangan
    PROBABLE = "probable"  # chat ko'rsatilmagan hodisa, common box'da yagona moslik


class DeletionResolution(StrEnum):
    """deletion_events.resolution: har bir o'chirish hodisasi qanday hal qilingani."""

    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    NOT_ARCHIVED = "not_archived"  # aniqlanmagan: arxivda nusxa yo'q
    AMBIGUOUS = "ambiguous"  # aniqlanmagan: bir nechta nomzod, hech biri belgilanmadi
    REVOKED = "revoked"  # avvalgi belgi bekor qilindi: xabar yana mavjudligi ko'rindi


class DeletionSource(StrEnum):
    EVENT = "event"  # Telegram update (real vaqt yoki catch-up)
    VERIFICATION = "verification"  # get_messages orqali tekshiruv
    RESYNC = "resync"  # xabar qayta olinganda (REVOKED uchun)
