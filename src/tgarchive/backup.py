"""Zaxira nusxa: baza va session (ixtiyoriy media), age bilan shifrlash, tekshirish, tiklash.

Fayl: ``BACKUP_DIR/tgarchive-YYYYmmdd-HHMMSS.tar.gz.age`` (shifrlangan) yoki ``.tar.gz``.
Ichida: ``manifest.json`` (versiya, migratsiya, sonlar, sha256), ``db/archive.sqlite3``,
``session/<nom>.session`` va ``BACKUP_INCLUDE_MEDIA=true`` bo'lsa ``media/...``.

* Baza va session SQLite **online backup API** bilan olinadi — xizmat to'xtamaydi, nusxa izchil.
* tar → gzip → age **oqim bilan**: shifrlanmagan vaqtinchalik nusxa diskka yozilmaydi,
  katta fayl ham xotiraga to'liq yuklanmaydi.
* Shifrlash ochiq kalit bilan; maxfiy kalit serverda saqlanmaydi — faqat tiklashda beriladi.
* Tiklashda arxiv ``filter="data"`` bilan ochiladi (``../`` va havolalar rad etiladi), sha256 va
  ``quick_check`` tekshiriladi; joriy baza o'chirilmaydi — ``.before-restore-<vaqt>`` nomi bilan qoladi.

Bu modul sinxron; asinxron kodda ``asyncio.to_thread`` orqali chaqiriladi.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import tarfile
import threading
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import pyrage

from tgarchive import __version__
from tgarchive.config import ArchiverSettings, BaseAppSettings
from tgarchive.crypto import parse_recipients
from tgarchive.db.types import utcnow
from tgarchive.fs import IS_POSIX, ensure_private_dir
from tgarchive.mediafiles import hash_file

PREFIX = "tgarchive-"
PLAIN_SUFFIX = ".tar.gz"
AGE_SUFFIX = ".tar.gz.age"
TMP_DIR = ".tmp"
MANIFEST = "manifest.json"
DB_MEMBER = "db/archive.sqlite3"
SESSION_DIR = "session"
MEDIA_DIR = "media"
FORMAT_VERSION = 1
COMPRESS_LEVEL = 6
STALE_TMP = timedelta(hours=6)
_STAMP = "%Y%m%d-%H%M%S"


class BackupError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BackupInfo:
    path: Path
    created_at: datetime
    size: int
    encrypted: bool


@dataclass(frozen=True, slots=True)
class RestoreReport:
    manifest: dict[str, Any]
    restored: list[Path] = field(default_factory=list)
    moved_aside: list[Path] = field(default_factory=list)
    media_files: int = 0


# ------------------------------------------------------------------- nomlar


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime(_STAMP)


def parse_backup_name(path: Path) -> BackupInfo | None:
    name = path.name
    if not name.startswith(PREFIX):
        return None
    for suffix, encrypted in ((AGE_SUFFIX, True), (PLAIN_SUFFIX, False)):
        if name.endswith(suffix):
            try:
                created = datetime.strptime(name[len(PREFIX) : -len(suffix)], _STAMP).replace(tzinfo=UTC)
            except ValueError:
                return None
            return BackupInfo(path, created, path.stat().st_size if path.exists() else 0, encrypted)
    return None


def list_backups(root: Path) -> list[BackupInfo]:
    if not root.exists():
        return []
    items = [info for p in root.iterdir() if p.is_file() and (info := parse_backup_name(p))]
    return sorted(items, key=lambda i: i.created_at, reverse=True)


def prune_backups(root: Path, keep: int) -> list[Path]:
    removed = []
    for info in list_backups(root)[keep:]:
        info.path.unlink(missing_ok=True)
        removed.append(info.path)
    return removed


def clean_stale_tmp(root: Path, now: datetime | None = None) -> int:
    """Uzilib qolgan zaxira jarayonlarining vaqtinchalik kataloglarini o'chiradi."""
    tmp = root / TMP_DIR
    if not tmp.exists():
        return 0
    limit = (now or utcnow()).timestamp() - STALE_TMP.total_seconds()
    removed = 0
    for item in tmp.iterdir():
        if item.stat().st_mtime < limit:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
            removed += 1
    return removed


# ------------------------------------------------------------------- yaratish


def _sqlite_copy(source: Path, target: Path, busy_timeout_ms: int) -> None:
    """Ishlab turgan SQLite bazasidan izchil nusxa (online backup API)."""
    if not source.exists():
        raise BackupError(f"baza topilmadi: {source}")
    with closing(sqlite3.connect(source, timeout=busy_timeout_ms / 1000)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")  # bitta mustaqil fayl (-wal'siz)
        result = dst.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise BackupError(f"nusxa tekshiruvdan o'tmadi ({source.name}): {result}")


def _db_facts(path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(path)) as conn:
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        counts = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("chats", "messages", "message_versions", "media", "deletion_events")
        }
    return {"revision": revision[0] if revision else None, "counts": counts}


def _iter_media(root: Path) -> Iterator[tuple[str, Path]]:
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_file() and not path.is_symlink() and rel.parts[0] != TMP_DIR:
            yield rel.as_posix(), path


def _write_tar(out: IO[bytes], manifest: dict[str, Any], files: list[tuple[str, Path]], media: list[tuple[str, Path]]) -> None:
    with tarfile.open(fileobj=out, mode="w|gz", compresslevel=COMPRESS_LEVEL) as tar:
        data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST)
        info.size, info.mode, info.mtime = len(data), 0o600, int(utcnow().timestamp())
        tar.addfile(info, io.BytesIO(data))
        for arcname, path in files:
            tar.add(path, arcname=arcname, recursive=False)
        for rel, path in media:
            tar.add(path, arcname=f"{MEDIA_DIR}/{rel}", recursive=False)


def _encrypt_to(out: IO[bytes], recipients: list[Any], produce: Callable[[IO[bytes]], None]) -> None:
    """``produce`` yozgan oqimni (alohida thread'da) age bilan shifrlab ``out`` ga yozadi."""
    read_fd, write_fd = os.pipe()
    errors: list[BaseException] = []

    def producer() -> None:
        try:
            with os.fdopen(write_fd, "wb") as writer:
                produce(writer)
        except BaseException as exc:  # noqa: BLE001 — asosiy thread'ga uzatiladi
            errors.append(exc)

    thread = threading.Thread(target=producer, name="backup-tar", daemon=True)
    thread.start()
    try:
        with os.fdopen(read_fd, "rb") as reader:
            pyrage.encrypt_io(reader, out, recipients)
    finally:
        thread.join()
    if errors:
        raise BackupError(f"arxivlashda xato: {type(errors[0]).__name__}: {errors[0]}") from errors[0]


def create_backup(settings: ArchiverSettings, *, now: datetime | None = None) -> BackupInfo:
    now = now or utcnow()
    root = ensure_private_dir(settings.paths.backups)
    work = ensure_private_dir(ensure_private_dir(root / TMP_DIR) / _stamp(now))
    try:
        files: list[tuple[str, Path]] = []
        db_copy = work / "archive.sqlite3"
        _sqlite_copy(settings.paths.db, db_copy, settings.db_busy_timeout_ms)
        files.append((DB_MEMBER, db_copy))
        if settings.session_path.exists():
            session_copy = ensure_private_dir(work / SESSION_DIR) / settings.session_path.name
            _sqlite_copy(settings.session_path, session_copy, settings.db_busy_timeout_ms)
            files.append((f"{SESSION_DIR}/{session_copy.name}", session_copy))
        media = list(_iter_media(settings.paths.media)) if settings.backup_include_media else []
        recipients = parse_recipients(settings.backup_age_recipients)
        manifest = {
            "format": FORMAT_VERSION,
            "created_at": now.isoformat(),
            "tgarchive": __version__,
            **_db_facts(db_copy),
            "files": [
                {"name": arc, "size": path.stat().st_size, "sha256": hash_file(path)[1]} for arc, path in files
            ],
            "media": {"included": settings.backup_include_media, "files": len(media)},
            "encrypted": bool(recipients),
            "recipients": [r.strip() for r in settings.backup_age_recipients],
        }
        name = f"{PREFIX}{_stamp(now)}{AGE_SUFFIX if recipients else PLAIN_SUFFIX}"
        part = work / f"{name}.part"
        with open(part, "wb") as out:
            if recipients:
                _encrypt_to(out, recipients, lambda w: _write_tar(w, manifest, files, media))
            else:
                _write_tar(out, manifest, files, media)
        final = root / name
        os.replace(part, final)
        if IS_POSIX:
            os.chmod(final, 0o600)
        prune_backups(root, settings.backup_keep)
        info = parse_backup_name(final)
        assert info is not None
        return info
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------- o'qish va tiklash


@contextmanager
def open_backup(path: Path, identities: list[Any] | None) -> Iterator[IO[bytes]]:
    """Zaxiraning tar.gz oqimi (kerak bo'lsa age shifri ochilgan holda)."""
    info = parse_backup_name(path)
    if info is None:
        raise BackupError(f"zaxira fayli nomi tanilmadi: {path.name}")
    if not info.encrypted:
        with open(path, "rb") as plain:
            yield plain
        return
    if not identities:
        raise BackupError("zaxira shifrlangan: maxfiy kalit faylini bering (--identity)")
    read_fd, write_fd = os.pipe()
    errors: list[BaseException] = []

    def producer() -> None:
        try:
            with open(path, "rb") as source, os.fdopen(write_fd, "wb") as writer:
                pyrage.decrypt_io(source, writer, identities)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=producer, name="backup-decrypt", daemon=True)
    thread.start()
    reader = os.fdopen(read_fd, "rb")
    try:
        yield reader
    finally:
        reader.close()
        thread.join()
        real = [e for e in errors if not isinstance(e, BrokenPipeError)]
        if real:
            raise BackupError("shifrni ochib bo'lmadi: kalit mos emas yoki fayl buzilgan") from real[0]


def extract_backup(path: Path, identities: list[Any] | None, dest: Path) -> dict[str, Any]:
    """Zaxirani ``dest`` ga ochadi va to'liq tekshiradi. Qaytaradi: manifest."""
    ensure_private_dir(dest)
    try:
        with open_backup(path, identities) as stream, tarfile.open(fileobj=stream, mode="r|gz") as tar:
            tar.extractall(dest, filter="data")
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise BackupError(f"zaxira arxivi buzilgan: {type(exc).__name__}: {exc}") from exc
    try:
        manifest = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BackupError("zaxirada manifest.json yo'q yoki buzilgan") from None
    if manifest.get("format") != FORMAT_VERSION:
        raise BackupError(f"zaxira formati tanilmadi: {manifest.get('format')}")
    for entry in manifest.get("files", []):
        member = dest / entry["name"]
        if not member.is_file():
            raise BackupError(f"zaxirada {entry['name']} yo'q")
        size, digest = hash_file(member)
        if size != entry["size"] or digest != entry["sha256"]:
            raise BackupError(f"{entry['name']}: sha256 mos emas — zaxira buzilgan")
    with closing(sqlite3.connect(dest / DB_MEMBER)) as conn:
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise BackupError(f"zaxiradagi baza tekshiruvdan o'tmadi: {result}")
    return manifest


def verify_backup(path: Path, identities: list[Any] | None, *, work_root: Path) -> dict[str, Any]:
    work = work_root / TMP_DIR / f"verify-{_stamp(utcnow())}"
    try:
        return extract_backup(path, identities, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _move_aside(path: Path, stamp: str, moved: list[Path]) -> None:
    if path.exists():
        aside = path.with_name(f"{path.name}.before-restore-{stamp}")
        os.replace(path, aside)
        moved.append(aside)


def restore_backup(
    path: Path, settings: BaseAppSettings, identities: list[Any] | None, *, now: datetime | None = None
) -> RestoreReport:
    """Zaxirani tiklaydi. Archiver va bot TO'XTATILGAN bo'lishi shart (CLI buni tekshiradi)."""
    now = now or utcnow()
    stamp = _stamp(now)
    paths = settings.paths
    work = ensure_private_dir(ensure_private_dir(paths.backups / TMP_DIR) / f"restore-{stamp}")
    try:
        manifest = extract_backup(path, identities, work)
        moved: list[Path] = []
        restored: list[Path] = []

        ensure_private_dir(paths.db.parent)
        for suffix in ("", "-wal", "-shm"):
            _move_aside(Path(f"{paths.db}{suffix}"), stamp, moved)
        shutil.move(work / DB_MEMBER, paths.db)  # boshqa volume bo'lishi mumkin — copy+delete
        restored.append(paths.db)

        session_src = work / SESSION_DIR
        if session_src.is_dir():
            ensure_private_dir(paths.sessions)
            for item in sorted(session_src.iterdir()):
                target = paths.sessions / item.name
                for suffix in ("", "-journal"):
                    _move_aside(Path(f"{target}{suffix}"), stamp, moved)
                shutil.move(item, target)
                restored.append(target)

        media_count = 0
        media_src = work / MEDIA_DIR
        if media_src.is_dir():
            ensure_private_dir(paths.media)
            for rel, file in _iter_media(media_src):
                target = paths.media / rel
                ensure_private_dir(target.parent)
                shutil.move(file, target)
                media_count += 1
        return RestoreReport(manifest, restored, moved, media_count)
    finally:
        shutil.rmtree(work, ignore_errors=True)
