"""10-bosqich: zaxira — yaratish, age shifri, tekshirish, tiklash, rejalashtirish, CLI."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from archive_fixture import build_archive
from fake_telegram import FakeClock, RecordingSleep
from tgarchive.archiver.maintenance import Maintenance
from tgarchive.backup import (
    AGE_SUFFIX,
    PLAIN_SUFFIX,
    BackupError,
    clean_stale_tmp,
    create_backup,
    list_backups,
    open_backup,
    restore_backup,
    verify_backup,
)
from tgarchive.cli import main
from tgarchive.config import ArchiverSettings, ConfigError, load_settings
from tgarchive.crypto import generate_keypair, load_identities
from tgarchive.db.engine import Database
from tgarchive.db.meta import LAST_BACKUP_KEY, get_json_meta
from tgarchive.db.models import AuditLog, Message
from tgarchive.db.status import ARCHIVER, beat, format_backup_meta
from tgarchive.db.types import utcnow
from tgarchive.exitcodes import EXIT_OK, EXIT_RUNTIME
from tl_objects import ALI_ID

VALID_HASH = "0123456789abcdef0123456789abcdef"
T0 = datetime(2026, 10, 25, 3, 30, tzinfo=UTC)


def settings(tmp_path: Path, **overrides: Any) -> ArchiverSettings:
    return ArchiverSettings(
        _env_file=None, tg_api_id=1, tg_api_hash=VALID_HASH, data_dir=tmp_path, media_max_file_mb=1, **overrides
    )


def make_session(cfg: ArchiverSettings) -> None:
    cfg.session_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cfg.session_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS sessions (dc_id INTEGER, auth_key BLOB)")
        conn.execute("INSERT INTO sessions VALUES (2, x'00112233')")


def keyfile(tmp_path: Path, secret: str, name: str = "key.txt") -> Path:
    path = tmp_path / name
    path.write_text(f"# test kaliti\n{secret}\n", encoding="utf-8")
    return path


def members(path: Path, identities: list | None = None) -> dict[str, bytes]:
    with open_backup(path, identities) as stream, tarfile.open(fileobj=stream, mode="r|gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}


def with_(cfg: ArchiverSettings, **changes: Any) -> ArchiverSettings:
    return cfg.model_copy(update=changes)


@pytest.fixture
async def archived(db: Database, db_path: Path, tmp_path: Path) -> ArchiverSettings:
    await build_archive(tmp_path, db)
    cfg = settings(tmp_path, db_path=db_path)
    make_session(cfg)
    return cfg


# ---------------------------------------------------------------- yaratish


async def test_plain_backup_contains_consistent_db_and_session(archived: ArchiverSettings) -> None:
    info = create_backup(archived, now=T0)
    assert info.path.name == f"tgarchive-20261025-033000{PLAIN_SUFFIX}" and not info.encrypted
    files = members(info.path)
    assert set(files) == {"manifest.json", "db/archive.sqlite3", "session/archiver.session"}
    manifest = json.loads(files["manifest.json"])
    assert manifest["counts"]["messages"] == 10 and manifest["revision"] == "0004"
    assert manifest["encrypted"] is False and manifest["media"] == {"included": False, "files": 0}
    if os.name == "posix":
        assert info.path.stat().st_mode & 0o077 == 0
    assert not list((archived.paths.backups / ".tmp").iterdir())  # vaqtinchalik fayllar qolmaydi


async def test_encrypted_backup_needs_the_right_key(archived: ArchiverSettings, tmp_path: Path) -> None:
    public, secret = generate_keypair()
    cfg = with_(archived, backup_age_recipients=[public])
    info = create_backup(cfg, now=T0)
    assert info.path.name.endswith(AGE_SUFFIX) and info.encrypted
    raw = info.path.read_bytes()
    assert raw.startswith(b"age-encryption.org/v1\n") and b"archive.sqlite3" not in raw

    identities = load_identities(keyfile(tmp_path, secret))
    manifest = verify_backup(info.path, identities, work_root=cfg.paths.backups)
    assert manifest["encrypted"] is True and manifest["recipients"] == [public]

    with pytest.raises(BackupError, match="maxfiy kalit"):
        verify_backup(info.path, None, work_root=cfg.paths.backups)
    _, other = generate_keypair()
    with pytest.raises(BackupError, match="kalit mos emas"):
        verify_backup(info.path, load_identities(keyfile(tmp_path, other, "other.txt")), work_root=cfg.paths.backups)


@pytest.mark.parametrize("encrypted", [False, True])
async def test_tampered_backup_is_rejected(archived: ArchiverSettings, tmp_path: Path, encrypted: bool) -> None:
    public, secret = generate_keypair()
    cfg = with_(archived, backup_age_recipients=[public] if encrypted else [])
    info = create_backup(cfg, now=T0)
    data = bytearray(info.path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    info.path.write_bytes(bytes(data))
    identities = load_identities(keyfile(tmp_path, secret)) if encrypted else None
    with pytest.raises(BackupError):
        verify_backup(info.path, identities, work_root=cfg.paths.backups)


async def test_path_traversal_in_archive_is_rejected(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    evil = cfg.paths.backups / "tgarchive-20260101-000000.tar.gz"
    evil.parent.mkdir(parents=True)
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo("../../hujum.txt")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"xxx"))
    with pytest.raises(BackupError):
        verify_backup(evil, None, work_root=cfg.paths.backups)
    assert not (tmp_path / "hujum.txt").exists() and not (cfg.paths.backups / "hujum.txt").exists()


async def test_media_can_be_included(archived: ArchiverSettings, tmp_path: Path) -> None:
    cfg = with_(archived, backup_include_media=True)
    info = create_backup(cfg, now=T0)
    media = [name for name in members(info.path) if name.startswith("media/")]
    assert len(media) == 1 and media[0].endswith(".jpg") and ".tmp" not in media[0]


async def test_old_backups_are_pruned(archived: ArchiverSettings, tmp_path: Path) -> None:
    cfg = with_(archived, backup_keep=2)
    for hours in range(3):
        create_backup(cfg, now=T0 + timedelta(hours=hours))
    names = [b.path.name for b in list_backups(cfg.paths.backups)]
    assert names == ["tgarchive-20261025-053000.tar.gz", "tgarchive-20261025-043000.tar.gz"]


def test_stale_tmp_is_cleaned(tmp_path: Path) -> None:
    tmp = tmp_path / ".tmp"
    old, fresh = tmp / "eski", tmp / "yangi"
    old.mkdir(parents=True), fresh.mkdir()
    os.utime(old, (T0.timestamp() - 7 * 3600,) * 2)
    os.utime(fresh, (T0.timestamp(),) * 2)
    assert clean_stale_tmp(tmp_path, T0) == 1
    assert not old.exists() and fresh.exists()


# ------------------------------------------------------------------ tiklash


async def test_restore_brings_back_data_and_keeps_current_db(archived: ArchiverSettings, db: Database, tmp_path: Path) -> None:
    public, secret = generate_keypair()
    cfg = with_(archived, backup_age_recipients=[public])
    info = create_backup(cfg, now=T0)
    async with db.write() as session:  # zaxiradan keyin ma'lumot yo'qoldi
        for message in await session.scalars(select(Message).where(Message.chat_id == ALI_ID)):
            await session.delete(message)
    cfg.session_path.write_bytes(b"buzilgan")
    await db.dispose()

    report = restore_backup(info.path, cfg, load_identities(keyfile(tmp_path, secret)), now=T0 + timedelta(days=1))

    assert cfg.paths.db in report.restored and cfg.session_path in report.restored
    asides = {p.name for p in report.moved_aside}
    assert "archive.sqlite3.before-restore-20261026-033000" in asides
    assert "archiver.session.before-restore-20261026-033000" in asides
    restored = Database(cfg.database_url)
    async with restored.read() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 10
    await restored.dispose()
    with sqlite3.connect(cfg.session_path) as conn:
        assert conn.execute("SELECT dc_id FROM sessions").fetchone() == (2,)
    with sqlite3.connect(cfg.paths.db.with_name("archive.sqlite3.before-restore-20261026-033000")) as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone() == (2,)  # eski holat yo'qolmagan


# ------------------------------------------------------------- rejalashtirish


async def test_scheduled_backup_and_failure_handling(archived: ArchiverSettings, db: Database) -> None:
    clock = FakeClock(T0)
    calls: list[datetime] = []

    def backup_fn(cfg: ArchiverSettings, *, now: datetime):
        calls.append(now)
        if len(calls) == 2:
            raise OSError("disk to'ldi")
        return create_backup(cfg, now=now)

    m = Maintenance(db, archived, clock=clock, sleep=RecordingSleep(), backup_fn=backup_fn)
    await m.tick()
    await m.tick()  # hali vaqti emas
    assert len(calls) == 1
    clock.advance(24 * 3600)
    await m.tick()  # xato
    clock.advance(1800)
    await m.tick()  # xatodan keyin 1 soat kutadi
    assert len(calls) == 2
    clock.advance(1900)
    await m.tick()
    assert len(calls) == 3

    async with db.read() as session:
        meta = await get_json_meta(session, LAST_BACKUP_KEY)
        actions = [a.action for a in await session.scalars(select(AuditLog).order_by(AuditLog.id))]
    assert meta["file"] == "tgarchive-20261026-043140.tar.gz" and "error" not in meta  # 24 soat + 1800 + 1900 s
    assert actions.count("backup_created") == 2 and actions.count("backup_failed") == 1


def test_backup_status_line() -> None:
    now = T0 + timedelta(hours=5)
    assert format_backup_meta(None, now) == "Zaxira: hali yaratilmagan"
    ok = {"at": T0.isoformat(), "size": 2048, "encrypted": True}
    assert format_backup_meta(ok, now) == "Zaxira: oxirgi 2026-10-25 03:30 UTC (5 soat oldin), 2.0 KB, shifrlangan"
    plain = format_backup_meta(ok | {"encrypted": False}, now)
    assert "SHIFRLANMAGAN" in plain
    failed = format_backup_meta(ok | {"error": "OSError: disk", "error_at": (T0 + timedelta(hours=1)).isoformat()}, now)
    assert "muvaffaqiyatsiz" in failed and "OSError: disk" in failed


def test_invalid_recipient_is_a_config_error(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f"TG_API_ID=1\nTG_API_HASH={VALID_HASH}\nBACKUP_AGE_RECIPIENTS=age1notakey\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="BACKUP_AGE_RECIPIENTS"):
        load_settings(ArchiverSettings, env)


# ----------------------------------------------------------------------- CLI


def test_backup_cli_create_list_verify_restore(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    public, secret = generate_keypair()
    key = keyfile(tmp_path, secret)
    env = tmp_path / "test.env"
    env.write_text(
        f"DATA_DIR={tmp_path.as_posix()}/data\nTG_API_ID=1\nTG_API_HASH={VALID_HASH}\nBACKUP_AGE_RECIPIENTS={public}\n",
        encoding="utf-8",
    )
    base = ["--env-file", str(env)]
    assert main([*base, "db-upgrade"]) == EXIT_OK
    assert main([*base, "backup", "create"]) == EXIT_OK
    assert "shifrlangan (age)" in capsys.readouterr().out
    assert main([*base, "backup", "list"]) == EXIT_OK
    name = capsys.readouterr().out.split()[-1]
    assert name.endswith(AGE_SUFFIX)

    assert main([*base, "backup", "verify", name, "--identity", str(key)]) == EXIT_OK
    assert "Zaxira butun" in capsys.readouterr().out
    assert main([*base, "backup", "verify", name]) == EXIT_RUNTIME  # kalitsiz

    async def archiver_running() -> None:
        cfg = settings(tmp_path / "data")
        db = Database(cfg.database_url)
        async with db.write() as session:
            await beat(session, ARCHIVER, "idle", started_at=utcnow())
        await db.dispose()

    asyncio.run(archiver_running())
    assert main([*base, "backup", "restore", name, "--identity", str(key), "--yes"]) == EXIT_RUNTIME
    assert "docker compose stop archiver bot" in capsys.readouterr().err
    assert main([*base, "backup", "restore", name, "--identity", str(key), "--yes", "--force"]) == EXIT_OK
    assert "Tiklandi" in capsys.readouterr().out


def test_identity_from_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    public, secret = generate_keypair()
    env = tmp_path / "test.env"
    env.write_text(
        f"DATA_DIR={tmp_path.as_posix()}/data\nTG_API_ID=1\nTG_API_HASH={VALID_HASH}\nBACKUP_AGE_RECIPIENTS={public}\n",
        encoding="utf-8",
    )
    base = ["--env-file", str(env)]
    assert main([*base, "db-upgrade"]) == EXIT_OK
    assert main([*base, "backup", "create"]) == EXIT_OK
    name = list_backups(tmp_path / "data" / "backups")[0].path.name
    monkeypatch.setattr("sys.stdin", io.StringIO(f"# kalit\n{secret}\n"))
    capsys.readouterr()
    assert main([*base, "backup", "verify", name, "--identity", "-"]) == EXIT_OK
    assert "Zaxira butun" in capsys.readouterr().out
