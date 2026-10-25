"""Eksport: chat, sana oralig'i va qidiruv filtrlari bo'yicha HTML yoki JSON, zip ichida.

``/export chat:7 after:2026-01-01 before:2026-02-01 json +media`` — filtrlar ``/search`` bilan bir xil.

* Xabarlar partiyalab o'qiladi (keyset, 500 tadan) va zip ichiga oqim bilan yoziladi —
  katta eksport ham xotirani to'ldirmaydi.
* HTML'da barcha matn escape qilinadi, qo'shimcha ravishda qat'iy Content-Security-Policy:
  eksport ochilganda hech qanday skript ishlamaydi.
* ``+media`` bilan yuklangan fayllar ``media/`` ga qo'shiladi (``EXPORT_MAX_MEDIA_MB`` gacha).
* Eksport — shaxsiy ma'lumot nusxasi: ``EXPORT_DIR`` 0700, fayl 0600, ``EXPORT_KEEP_DAYS``
  dan keyin avtomatik o'chiriladi; har bir eksport ``audit_log`` ga yoziladi.
"""

from __future__ import annotations

import asyncio
import html
import io
import json
import os
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select, tuple_

from tgarchive import __version__
from tgarchive.config import BaseAppSettings
from tgarchive.db.engine import Database
from tgarchive.db.enums import MediaStatus, MediaType
from tgarchive.db.media import format_size
from tgarchive.db.models import Chat, Media, Message, MessageVersion
from tgarchive.db.search import SearchQuery, apply_filters, parse_search
from tgarchive.db.store import add_audit
from tgarchive.db.types import utcnow
from tgarchive.fs import IS_POSIX, ensure_private_dir
from tgarchive.mediafiles import resolve_media_path

Format = Literal["html", "json"]
PREFIX = "export-"
BATCH = 500
_STAMP = "%Y%m%d-%H%M%S"
STALE_PART = timedelta(hours=1)
CSP = "default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'"


class ExportError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ExportRequest:
    query: SearchQuery
    fmt: Format = "html"
    with_media: bool = False


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    size: int
    messages: int
    chats: int
    media_included: int
    media_skipped: int


def parse_export_args(raw: str, tz: ZoneInfo) -> tuple[ExportRequest | None, list[str]]:
    """``json``/``html``, ``+media`` va ``all`` kalit so'zlari; qolgani — qidiruv filtrlari."""
    fmt: Format = "html"
    with_media = False
    everything = False
    rest = []
    for token in raw.split():
        low = token.lower()
        if low in ("html", "json", "format:html", "format:json"):
            fmt = "json" if low.endswith("json") else "html"
        elif low in ("+media", "media:yes"):
            with_media = True
        elif low == "all":
            everything = True
        else:
            rest.append(token)
    query, errors = parse_search(" ".join(rest), tz)
    if not errors and query.is_empty and not everything:
        errors.append("chat:, sana yoki boshqa filtr kerak. Butun arxivni eksport qilish uchun: all")
    return (None if errors else ExportRequest(query, fmt, with_media)), errors


def cleanup_exports(root: Path, keep_days: int, now: datetime | None = None) -> list[Path]:
    """``EXPORT_KEEP_DAYS`` dan eski eksportlar va uzilib qolgan ``.part`` fayllarni o'chiradi."""
    if not root.exists():
        return []
    now = now or utcnow()
    removed = []
    for path in root.iterdir():
        if not (path.is_file() and path.name.startswith(PREFIX)):
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, now.tzinfo)
        # .part — boshqa eksport hozir yozayotgan bo'lishi mumkin, faqat eskisi o'chiriladi
        limit = now - (STALE_PART if path.suffix == ".part" else timedelta(days=keep_days))
        if modified < limit:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


# ------------------------------------------------------------------ yozuvchilar


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class _JsonWriter:
    def __init__(self, out: io.TextIOBase, meta: dict[str, Any]) -> None:
        self.out = out
        self.first = True
        out.write('{"export": ' + json.dumps(meta, ensure_ascii=False) + ',\n"messages": [\n')

    def chat(self, chat: Chat) -> None:
        pass

    def message(self, item: dict[str, Any]) -> None:
        self.out.write(("" if self.first else ",\n") + json.dumps(item, ensure_ascii=False, default=str))
        self.first = False

    def finish(self, chats: dict[int, dict[str, Any]], summary: dict[str, Any]) -> None:
        chat_map = {str(k): v for k, v in chats.items()}
        self.out.write("\n],\n\"chats\": " + json.dumps(chat_map, ensure_ascii=False))
        self.out.write(',\n"summary": ' + json.dumps(summary, ensure_ascii=False) + "}\n")


_HTML_HEAD = """<!doctype html>
<html lang="uz"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Telegram arxivi — eksport</title>
<style>
body{{font:15px/1.45 system-ui,sans-serif;margin:0;background:#f4f5f7;color:#1d1f23}}
header,footer{{background:#fff;padding:16px 20px;border-bottom:1px solid #e3e5e8}}
footer{{border-top:1px solid #e3e5e8;color:#555}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:17px;margin:24px 20px 8px}}
.msg{{background:#fff;margin:6px 20px;padding:8px 12px;border-radius:8px;max-width:820px}}
.out{{background:#e8f4ff;margin-left:auto;margin-right:20px}}
.meta{{color:#6b7078;font-size:13px}} .text{{white-space:pre-wrap;word-wrap:break-word;margin-top:4px}}
.badge{{display:inline-block;font-size:12px;padding:1px 6px;border-radius:4px;background:#eceef1;margin-left:4px}}
.del{{background:#ffe5e5}} .media{{margin-top:6px;color:#444}} img{{max-width:100%;border-radius:6px}}
details{{margin-top:6px;font-size:13px;color:#444}} a{{color:#2266cc}}
@media (prefers-color-scheme:dark){{body{{background:#17191c;color:#e6e6e6}}header,footer,.msg{{background:#22252a;border-color:#333}}
.out{{background:#1d3550}}.meta{{color:#9aa0a8}}.badge{{background:#33373d}}.del{{background:#5a2a2a}}}}
</style></head><body>
<header><h1>Telegram arxivi — eksport</h1><div class="meta">{meta}</div></header>
"""


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


class _HtmlWriter:
    def __init__(self, out: io.TextIOBase, meta: dict[str, Any], tz: ZoneInfo) -> None:
        self.out = out
        self.tz = tz
        self.open_section = False
        line = f"Yaratilgan: {_e(meta['created_local'])} · Filtr: {_e(meta['filter'])}"
        out.write(_HTML_HEAD.format(csp=CSP, meta=line))

    def _time(self, value: str | None) -> str:
        if not value:
            return "-"
        return datetime.fromisoformat(value).astimezone(self.tz).strftime("%Y-%m-%d %H:%M")

    def chat(self, chat: Chat) -> None:
        if self.open_section:
            self.out.write("</section>\n")
        self.out.write(
            f'<section><h2>{_e(chat.title or chat.chat_id)} <span class="badge">{_e(chat.chat_type.value)}</span> '
            f'<span class="badge">ID {chat.chat_id}</span></h2>\n'
        )
        self.open_section = True

    def message(self, m: dict[str, Any]) -> None:
        classes = "msg" + (" out" if m["outgoing"] else "")
        anchor = f"m{m['chat_id']}_{m['message_id']}"
        badges = []
        if m["edit_count"]:
            badges.append(f'<span class="badge">✏️ tahrirlangan {m["edit_count"]}</span>')
        if m["deletion"]:
            status = "tasdiqlangan" if m["deletion"]["status"] == "confirmed" else "ehtimoliy"
            badges.append(f'<span class="badge del">🗑 o\'chirilgan ({status}) {self._time(m["deletion"]["at"])}</span>')
        parts = [
            f'<article class="{classes}" id="{anchor}"><div class="meta">{self._time(m["date"])} · '
            f'<b>{_e(m["sender_name"] or m["sender_id"] or "-")}</b> · #{m["message_id"]}{"".join(badges)}</div>'
        ]
        reply = m["reply_to"]
        if reply:
            target = f"m{reply['peer_id'] or m['chat_id']}_{reply['msg_id']}"
            parts.append(f'<div class="meta">↩️ <a href="#{target}">javob: #{reply["msg_id"]}</a></div>')
        fwd = m["forward"]
        if fwd:
            parts.append(f'<div class="meta">↪️ forward: {_e(fwd["from_name"] or fwd["from_id"] or "-")}</div>')
        if m["text"]:
            parts.append(f'<div class="text">{_e(m["text"])}</div>')
        elif m["action"]:
            parts.append(f'<div class="meta">[{_e(m["action"])}]</div>')
        media = m["media"]
        if media:
            label = _e(media["file_name"] or media["type"])
            if media.get("file") and media["type"] == MediaType.PHOTO.value:
                parts.append(f'<div class="media"><img src="{_e(media["file"])}" alt="{label}" loading="lazy"></div>')
            elif media.get("file"):
                parts.append(f'<div class="media">📎 <a href="{_e(media["file"])}">{label}</a> ({_e(media["size_text"])})</div>')
            else:
                parts.append(f'<div class="media">📎 {label} ({_e(media["size_text"])}) — {_e(media["state"])}</div>')
        if m["versions"]:
            items = "".join(
                f"<li>{self._time(v['observed_at'])}: {_e(v['text'] or '(matnsiz)')}</li>" for v in m["versions"]
            )
            parts.append(f"<details><summary>✏️ Oldingi versiyalar ({len(m['versions'])})</summary><ol>{items}</ol></details>")
        parts.append("</article>\n")
        self.out.write("".join(parts))

    def finish(self, chats: dict[int, dict[str, Any]], summary: dict[str, Any]) -> None:
        if self.open_section:
            self.out.write("</section>\n")
        self.out.write(
            f"<footer>Xabarlar: {summary['messages']} · chatlar: {summary['chats']} · "
            f"fayllar: {summary['media_included']} (qo'shilmagan: {summary['media_skipped']}) · "
            f"tgarchive {_e(__version__)}</footer></body></html>\n"
        )


# ---------------------------------------------------------------------- eksport


def _media_state(media: Media) -> str:
    if media.status is MediaStatus.DONE:
        return "yuklangan, eksportga qo'shilmagan"
    if media.skip_reason is not None:
        return f"yuklanmagan ({media.skip_reason.value})"
    return media.status.value


def _message_dict(
    m: Message, versions: list[MessageVersion], media: Media | None, media_file: str | None
) -> dict[str, Any]:
    media_info = None
    if media is not None:
        size = media.stored_size or media.expected_size
        media_info = {
            "type": media.media_type.value,
            "file_name": media.file_name,
            "mime_type": media.mime_type,
            "size": size,
            "size_text": format_size(size),
            "status": media.status.value,
            "skip_reason": media.skip_reason.value if media.skip_reason else None,
            "sha256": media.sha256,
            "file": media_file,
            "state": _media_state(media),
        }
    return {
        "chat_id": m.chat_id,
        "message_id": m.message_id,
        "date": _iso(m.date),
        "sender_id": m.sender_id,
        "sender_name": "Men" if m.is_outgoing else m.sender_name,
        "outgoing": m.is_outgoing,
        "text": m.text,
        "entities": m.entities,
        "action": m.action,
        "reply_to": {"msg_id": m.reply_to_msg_id, "peer_id": m.reply_to_peer_id, "top_id": m.reply_to_top_id}
        if m.reply_to_msg_id
        else None,
        "forward": {
            "from_id": m.fwd_from_id,
            "from_name": m.fwd_from_name,
            "date": _iso(m.fwd_date),
            "channel_post": m.fwd_channel_post_id,
        }
        if (m.fwd_from_id or m.fwd_from_name)
        else None,
        "grouped_id": m.grouped_id,
        "edit_date": _iso(m.edit_date),
        "edit_count": m.edit_count,
        "versions": [
            {
                "no": v.version_no,
                "text": v.text,
                "edit_date": _iso(v.edit_date),
                "observed_at": _iso(v.observed_at),
                "replaced_at": _iso(v.replaced_at),
            }
            for v in versions
        ],
        "deletion": {"status": m.deletion_status.value, "at": _iso(m.deleted_at)} if m.deletion_status else None,
        "media": media_info,
        "source": m.source.value,
        "archived_at": _iso(m.archived_at),
    }


async def export_messages(
    db: Database,
    settings: BaseAppSettings,
    request: ExportRequest,
    *,
    actor: str = "owner",
    actor_id: int | None = None,
    now: datetime | None = None,
) -> ExportResult:
    now = now or utcnow()
    tz = ZoneInfo(settings.display_timezone)
    root = ensure_private_dir(settings.paths.exports)
    cleanup_exports(root, settings.export_keep_days, now)
    name = f"{PREFIX}{now.strftime(_STAMP)}.zip"
    final = root / name
    part = root / f"{name}.part"
    media_budget = settings.export_max_media_mb * 1024 * 1024 if request.with_media else 0
    media_to_add: list[str] = []
    media_used = 0
    media_skipped = 0
    chats: dict[int, dict[str, Any]] = {}
    count = 0
    meta = {
        "created_at": now.isoformat(),
        "created_local": now.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        "filter": request.query.describe(),
        "format": request.fmt,
        "with_media": request.with_media,
        "tgarchive": __version__,
    }
    try:
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            with io.TextIOWrapper(archive.open(f"export.{request.fmt}", "w", force_zip64=True), encoding="utf-8") as out:
                writer = _HtmlWriter(out, meta, tz) if request.fmt == "html" else _JsonWriter(out, meta)
                last: tuple[int, datetime, int] | None = None
                current_chat: int | None = None
                while True:
                    async with db.read() as session:
                        stmt = apply_filters(select(Message, Chat).join(Chat, Chat.chat_id == Message.chat_id), request.query)
                        if stmt is None:
                            break
                        if last is not None:
                            stmt = stmt.where(tuple_(Message.chat_id, Message.date, Message.id) > tuple_(*last))
                        rows = (
                            await session.execute(stmt.order_by(Message.chat_id, Message.date, Message.id).limit(BATCH))
                        ).all()
                        if not rows:
                            break
                        pks = [m.id for m, _ in rows]
                        versions: dict[int, list[MessageVersion]] = defaultdict(list)
                        edited = [m.id for m, _ in rows if m.edit_count]
                        if edited:
                            for v in await session.scalars(
                                select(MessageVersion)
                                .where(MessageVersion.message_pk.in_(edited))
                                .order_by(MessageVersion.message_pk, MessageVersion.version_no)
                            ):
                                versions[v.message_pk].append(v)
                        media_rows = {
                            md.message_pk: md
                            for md in await session.scalars(
                                select(Media).where(Media.message_pk.in_(pks), Media.superseded_at.is_(None))
                            )
                        }
                    for message, chat in rows:
                        if chat.chat_id != current_chat:
                            writer.chat(chat)
                            current_chat = chat.chat_id
                            chats[chat.chat_id] = {"title": chat.title, "type": chat.chat_type.value, "messages": 0}
                        chats[chat.chat_id]["messages"] += 1
                        media = media_rows.get(message.id)
                        media_file = None
                        if media is not None and media.status is MediaStatus.DONE and media.local_path:
                            size = media.stored_size or 0
                            if request.with_media and media_used + size <= media_budget:
                                media_file = f"media/{media.local_path}"
                                media_to_add.append(media.local_path)
                                media_used += size
                            elif request.with_media:
                                media_skipped += 1
                        writer.message(_message_dict(message, versions.get(message.id, []), media, media_file))
                        count += 1
                    last_msg = rows[-1][0]
                    last = (last_msg.chat_id, last_msg.date, last_msg.id)
                summary = {
                    "messages": count,
                    "chats": len(chats),
                    "media_included": len(media_to_add),
                    "media_skipped": media_skipped,
                }
                writer.finish(chats, summary)
            added = 0
            for relpath in dict.fromkeys(media_to_add):
                try:
                    source = resolve_media_path(settings.paths.media, relpath)
                except ValueError:
                    continue
                if source.is_file():
                    await asyncio.to_thread(archive.write, source, f"media/{relpath}")
                    added += 1
            archive.writestr("README.txt", _readme(meta, summary))
        os.replace(part, final)
        if IS_POSIX:
            os.chmod(final, 0o600)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    async with db.write() as session:
        await add_audit(
            session,
            "export_created",
            actor=actor,
            actor_id=actor_id,
            target=name,
            details={"filter": meta["filter"], "format": request.fmt, **summary},
        )
    return ExportResult(final, final.stat().st_size, count, len(chats), added, media_skipped)


def _readme(meta: dict[str, Any], summary: dict[str, Any]) -> str:
    return (
        "Telegram arxivi — eksport\n"
        f"Yaratilgan: {meta['created_local']} ({meta['created_at']})\n"
        f"Filtr: {meta['filter']}\n"
        f"Xabarlar: {summary['messages']}, chatlar: {summary['chats']}, "
        f"fayllar: {summary['media_included']} (qo'shilmagan: {summary['media_skipped']})\n\n"
        "Bu faylda shaxsiy yozishmalar bor. Uni ishonchsiz joyga yuklamang va kerak bo'lmay qolganda o'chiring.\n"
    )
