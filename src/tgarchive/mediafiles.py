"""Media fayllarining diskdagi yo'llari — archiver ham, bot ham ishlatadi (Telethon'siz).

Yo'l faqat raqamlardan va tozalangan kengaytmadan yasaladi:
``MEDIA_DIR/<chat_id>/<YYYY-MM>/<message_id>-<media_id>.<ext>``. O'qishda yo'l har doim
``MEDIA_DIR`` ichida ekanligi tekshiriladi.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path, PurePosixPath

from tgarchive.db.enums import MediaType
from tgarchive.db.models import Media, Message

HASH_CHUNK = 1 << 20
_EXT_BY_TYPE = {MediaType.PHOTO: ".jpg", MediaType.VOICE: ".ogg", MediaType.VIDEO_NOTE: ".mp4"}
_EXT_BY_MIME = {"audio/ogg": ".ogg", "application/x-tgsticker": ".tgs"}
_EXT_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
_FALLBACK_EXT = ".bin"


def media_extension(media: Media) -> str:
    if media.media_type in _EXT_BY_TYPE:
        return _EXT_BY_TYPE[media.media_type]
    if media.file_name:
        # Faqat kengaytma olinadi — nomdagi "../" kabi qismlar yo'lga hech qachon tushmaydi
        suffix = PurePosixPath(media.file_name.replace("\\", "/")).suffix.lower()
        if _EXT_RE.match(suffix):
            return suffix
    if media.mime_type:
        ext = _EXT_BY_MIME.get(media.mime_type) or mimetypes.guess_extension(media.mime_type) or ""
        if _EXT_RE.match(ext):
            return ext
    return _FALLBACK_EXT


def media_relpath(media: Media, message: Message) -> str:
    return f"{message.chat_id}/{message.date:%Y-%m}/{message.message_id}-{media.id}{media_extension(media)}"


def resolve_media_path(root: Path, relpath: str) -> Path:
    """``MEDIA_DIR`` ichidagi mutlaq yo'l. Tashqariga chiqadigan yo'l — xato."""
    base = root.resolve()
    path = (base / relpath).resolve()
    if not path.is_relative_to(base) or path == base:
        raise ValueError(f"media yo'li MEDIA_DIR dan tashqarida: {relpath!r}")
    return path


def hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()
