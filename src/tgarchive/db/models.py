"""Ma'lumotlar modeli. Batafsil izoh: docs/03-data-model.md."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from tgarchive.db.base import Base
from tgarchive.db.enums import (
    ArchiveMode,
    ChatType,
    DeletionResolution,
    DeletionSource,
    DeletionStatus,
    MediaSkipReason,
    MediaStatus,
    MediaType,
    MessageSource,
    SyncStatus,
)
from tgarchive.db.types import BigIntPK, UTCDateTime, str_enum, utcnow


class Chat(Base):
    """Arxivdagi chat.

    PK — Telethon "marked" peer ID: foydalanuvchi musbat, oddiy guruh ``-ID``,
    kanal/superguruh ``-100ID``. Shu sababli turli turdagi ID'lar to'qnashmaydi.
    """

    __tablename__ = "chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_type: Mapped[ChatType] = mapped_column(str_enum(ChatType))
    title: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))
    is_protected: Mapped[bool] = mapped_column(default=False)  # noforwards
    migrated_to_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    # None — global siyosat (.env), True/False — shu chat uchun aniq qaror
    archive_override: Mapped[bool | None]
    media_override: Mapped[bool | None]
    retention_days: Mapped[int | None]
    # Archiver oxirgi marta qo'llagan siyosat (policy.py) — bot ko'rsatishi uchun
    archive_mode: Mapped[ArchiveMode | None] = mapped_column(str_enum(ArchiveMode))
    linked_chat_id: Mapped[int | None] = mapped_column(BigInteger)  # kanalning muhokama (izoh) guruhi
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class AppMeta(Base):
    """Kalit-qiymat: bazaning o'ziga oid ma'lumotlar (masalan, qaysi akkauntga tegishli)."""

    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Message(Base):
    """Xabarning joriy (eng so'nggi ko'rilgan) holati.

    ``(chat_id, message_id)`` noyob — bir xil xabar qayta kelsa dublikat yaratilmaydi.
    Reply bog'lanishi ``(reply_to_peer_id yoki chat_id, reply_to_msg_id)`` orqali
    o'z-o'ziga join bilan topiladi; FK qo'yilmagan, chunki reply qilingan xabar
    hali import qilinmagan, o'chirilgan yoki arxiv doirasidan tashqarida bo'lishi mumkin.
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.chat_id", ondelete="CASCADE"))
    message_id: Mapped[int] = mapped_column(BigInteger)

    sender_id: Mapped[int | None] = mapped_column(BigInteger)
    sender_name: Mapped[str | None] = mapped_column(String(255))  # xabar vaqtidagi nom
    is_outgoing: Mapped[bool] = mapped_column(default=False)
    date: Mapped[datetime] = mapped_column(UTCDateTime)
    edit_date: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Telegram'da media caption ham xabar matni hisoblanadi, shuning uchun bitta ustun.
    text: Mapped[str | None] = mapped_column(Text)
    entities: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)

    reply_to_msg_id: Mapped[int | None] = mapped_column(BigInteger)
    reply_to_peer_id: Mapped[int | None] = mapped_column(BigInteger)  # faqat boshqa chatga reply bo'lsa
    reply_to_top_id: Mapped[int | None] = mapped_column(BigInteger)  # forum mavzusi / thread

    fwd_from_id: Mapped[int | None] = mapped_column(BigInteger)
    fwd_from_name: Mapped[str | None] = mapped_column(String(255))  # yashirin forward'da faqat nom bo'ladi
    fwd_date: Mapped[datetime | None] = mapped_column(UTCDateTime)
    fwd_channel_post_id: Mapped[int | None] = mapped_column(BigInteger)

    grouped_id: Mapped[int | None] = mapped_column(BigInteger)  # albom
    media_type: Mapped[MediaType | None] = mapped_column(str_enum(MediaType))
    action: Mapped[str | None] = mapped_column(String(64))  # servis xabar turi
    noforwards: Mapped[bool] = mapped_column(default=False)

    content_hash: Mapped[str] = mapped_column(String(64))
    edit_count: Mapped[int] = mapped_column(default=0)  # message_versions dagi yozuvlar soni
    comments_synced_id: Mapped[int | None] = mapped_column(BigInteger)  # kanal posti: shu ID'gacha izohlar olingan
    deletion_status: Mapped[DeletionStatus | None] = mapped_column(str_enum(DeletionStatus))
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # o'chirish aniqlangan vaqt

    source: Mapped[MessageSource] = mapped_column(str_enum(MessageSource))
    archived_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_messages_chat_id_message_id"),
        Index("ix_messages_chat_date", "chat_id", "date"),
        Index("ix_messages_date", "date"),
        Index("ix_messages_sender_date", "sender_id", "date"),
        # O'chirish hodisasi chat'siz kelganda (common box) faqat message_id bo'yicha qidiriladi
        Index("ix_messages_message_id", "message_id"),
        Index(
            "ix_messages_reply",
            "chat_id",
            "reply_to_msg_id",
            sqlite_where=sql_text("reply_to_msg_id IS NOT NULL"),
            postgresql_where=sql_text("reply_to_msg_id IS NOT NULL"),
        ),
        # Boshqa chatdan qilingan reply'lar: kanal oynasidan chiqarishda reply qilingan postni himoyalash
        Index(
            "ix_messages_reply_peer",
            "reply_to_peer_id",
            "reply_to_msg_id",
            sqlite_where=sql_text("reply_to_peer_id IS NOT NULL"),
            postgresql_where=sql_text("reply_to_peer_id IS NOT NULL"),
        ),
        Index(
            "ix_messages_media_type_date",
            "media_type",
            "date",
            sqlite_where=sql_text("media_type IS NOT NULL"),
            postgresql_where=sql_text("media_type IS NOT NULL"),
        ),
        Index(
            "ix_messages_edit_date",
            "edit_date",
            sqlite_where=sql_text("edit_date IS NOT NULL"),
            postgresql_where=sql_text("edit_date IS NOT NULL"),
        ),
        Index(
            "ix_messages_deleted_at",
            "deleted_at",
            sqlite_where=sql_text("deletion_status IS NOT NULL"),
            postgresql_where=sql_text("deletion_status IS NOT NULL"),
        ),
    )


class MessageVersion(Base):
    """Xabarning almashtirilgan (eski) holati. Joriy holat ``messages`` da qoladi.

    ``version_no=1`` — arxivda birinchi ko'rilgan holat. Yozuvlar faqat qo'shiladi.
    """

    __tablename__ = "message_versions"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    message_pk: Mapped[int] = mapped_column(BigInteger, ForeignKey("messages.id", ondelete="CASCADE"))
    version_no: Mapped[int]
    text: Mapped[str | None] = mapped_column(Text)
    entities: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    media_type: Mapped[MediaType | None] = mapped_column(str_enum(MediaType))
    edit_date: Mapped[datetime | None] = mapped_column(UTCDateTime)  # shu holatning Telegram edit_date'i
    content_hash: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)  # shu holat birinchi ko'rilgan vaqt
    replaced_at: Mapped[datetime] = mapped_column(UTCDateTime)  # yangi holat bilan almashtirilgan vaqt

    __table_args__ = (UniqueConstraint("message_pk", "version_no", name="uq_message_versions_message_pk_version_no"),)


class Media(Base):
    """Fayl ko'rinishidagi media metama'lumoti va yuklab olish navbati.

    Fayl bazaga emas, ``MEDIA_DIR`` ga yoziladi; bu yerda nisbiy yo'l va sha256 saqlanadi.
    Xabar mediasi tahrirlansa, eski yozuv ``superseded_at`` bilan belgilanadi va o'chirilmaydi.
    """

    __tablename__ = "media"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    message_pk: Mapped[int] = mapped_column(BigInteger, ForeignKey("messages.id", ondelete="CASCADE"))
    media_type: Mapped[MediaType] = mapped_column(str_enum(MediaType))

    tg_media_id: Mapped[int | None] = mapped_column(BigInteger)  # photo.id / document.id
    tg_access_hash: Mapped[int | None] = mapped_column(BigInteger)
    tg_dc_id: Mapped[int | None]
    mime_type: Mapped[str | None] = mapped_column(String(127))
    file_name: Mapped[str | None] = mapped_column(String(255))
    expected_size: Mapped[int | None] = mapped_column(BigInteger)
    duration: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None]
    height: Mapped[int | None]
    ttl_seconds: Mapped[int | None]  # view-once media — hech qachon yuklanmaydi

    status: Mapped[MediaStatus] = mapped_column(str_enum(MediaStatus), default=MediaStatus.PENDING)
    status_note: Mapped[str | None] = mapped_column(String(255))
    skip_reason: Mapped[MediaSkipReason | None] = mapped_column(str_enum(MediaSkipReason))
    # Egasi so'ragan: hajm chegarasiga qaramay yuklansin (tgarchive media fetch)
    force_download: Mapped[bool] = mapped_column(default=False, server_default=sql_text("0"))
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    local_path: Mapped[str | None] = mapped_column(String(1024))  # MEDIA_DIR ga nisbatan
    stored_size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    downloaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    superseded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_media_message_pk", "message_pk"),
        # Har bir xabarda ko'pi bilan bitta joriy media
        Index(
            "uq_media_current",
            "message_pk",
            unique=True,
            sqlite_where=sql_text("superseded_at IS NULL"),
            postgresql_where=sql_text("superseded_at IS NULL"),
        ),
        Index("ix_media_queue", "status", "next_attempt_at"),
        Index("ix_media_tg_media_id", "tg_media_id"),
        # Bir xil fayl bir necha xabarda (forward) — fayl faqat oxirgi havola yo'qolganda o'chiriladi
        Index(
            "ix_media_local_path",
            "local_path",
            sqlite_where=sql_text("local_path IS NOT NULL"),
            postgresql_where=sql_text("local_path IS NOT NULL"),
        ),
    )


class ChatSyncState(Base):
    """Har bir chat bo'yicha import checkpoint'i.

    * Orqaga (backfill): ``backfill_boundary_id`` dan pastga, ``backfill_cursor_id`` —
      keyingi sahifa shu ID'dan kichik xabarlarni oladi (Telegram ``offset_id``).
    * Oldinga: ``high_water_id`` gacha (shu ID ham) hamma xabar olingani tasdiqlangan;
      qayta ishga tushganda ``(high_water_id, eng_yangi]`` oralig'i to'ldiriladi.

    Sahifa xabarlari va cursor bitta tranzaksiyada yoziladi.
    """

    __tablename__ = "chat_sync_state"

    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chats.chat_id", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )
    backfill_status: Mapped[SyncStatus] = mapped_column(str_enum(SyncStatus), default=SyncStatus.PENDING)
    backfill_boundary_id: Mapped[int | None] = mapped_column(BigInteger)
    backfill_cursor_id: Mapped[int | None] = mapped_column(BigInteger)
    backfill_imported: Mapped[int] = mapped_column(default=0)
    high_water_id: Mapped[int | None] = mapped_column(BigInteger)
    remote_top_id: Mapped[int | None] = mapped_column(BigInteger)  # dialoglar ro'yxatida ko'rilgan eng yangi ID
    last_refresh_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # oxirgi oldinga sinxronlash / oyna

    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (Index("ix_chat_sync_state_status_retry", "backfill_status", "next_retry_at"),)


class DeletionEvent(Base):
    """O'chirish hodisalarining audit jurnali (har bir xabar ID'si uchun bitta yozuv).

    ``chat_id`` — hodisada Telegram bergan chat (common box hodisalarida NULL).
    ``resolved_chat_id`` / ``message_pk`` — arxivdagi qaysi yozuvga bog'langani.
    """

    __tablename__ = "deletion_events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    resolution: Mapped[DeletionResolution] = mapped_column(str_enum(DeletionResolution))
    source: Mapped[DeletionSource] = mapped_column(str_enum(DeletionSource))
    message_pk: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("messages.id", ondelete="SET NULL"))
    resolved_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        Index("ix_deletion_events_detected_at", "detected_at"),
        Index("ix_deletion_events_message_pk", "message_pk"),
        Index("ix_deletion_events_chat_detected", "resolved_chat_id", "detected_at"),
    )


class MediaTrash(Base):
    """Arxivdan olib tashlangan xabarlarning endi hech kim ishlatmaydigan fayllari.

    Xabar o'chirilishi bilan bitta tranzaksiyada yoziladi; faylni media worker o'chiradi
    (va o'chirishdan oldin yana tekshiradi). Shu sababli bazani eski zaxiradan tiklash
    "ortiqcha" fayllarni o'chirib yubormaydi — faqat aniq ro'yxatdagilar o'chiriladi.
    """

    __tablename__ = "media_trash"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    local_path: Mapped[str] = mapped_column(String(1024), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AuditLog(Base):
    """Umumiy audit: bot buyruqlari, ruxsatsiz urinishlar, eksport, retention va h.k."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(32))  # system | owner | stranger
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(255))
    ok: Mapped[bool] = mapped_column(default=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_audit_log_created_at", "created_at"),
        Index("ix_audit_log_action_created", "action", "created_at"),
    )


class ComponentHeartbeat(Base):
    """Komponentlar holati: /status va healthcheck shu jadvalni o'qiydi."""

    __tablename__ = "component_heartbeats"

    component: Mapped[str] = mapped_column(String(32), primary_key=True)  # archiver | bot
    state: Mapped[str] = mapped_column(String(32))  # starting | running | stopping | error
    pid: Mapped[int | None]
    hostname: Mapped[str | None] = mapped_column(String(255))
    version: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_beat_at: Mapped[datetime] = mapped_column(UTCDateTime)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
