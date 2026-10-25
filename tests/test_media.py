"""8-bosqich: media arxivi — tasnif, yuklash, katta fayllar, takror, disk, tekshiruv, axlat."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from telethon import errors
from telethon.tl import types

from factories import at, sqlite_url
from fake_telegram import FakeClock, FakeTelegram, RecordingSleep
from tgarchive.archiver.dialogs import sync_dialogs
from tgarchive.archiver.history import HistoryImporter
from tgarchive.archiver.media import RETRY_S, MediaWorker, classify
from tgarchive.archiver.service import ArchiverService
from tgarchive.cli import main
from tgarchive.config import ArchiverSettings
from tgarchive.db.engine import Database
from tgarchive.db.enums import ChatType, MediaSkipReason, MediaStatus, MediaType
from tgarchive.db.media import FetchRefused, list_large_files, media_summary, request_fetch
from tgarchive.db.migrate import upgrade
from tgarchive.db.models import Chat, Media, MediaTrash, Message
from tgarchive.exitcodes import EXIT_OK, EXIT_RUNTIME
from tgarchive.mediafiles import media_extension, media_relpath, resolve_media_path
from tl_objects import ALI_ID, CHANNEL_RAW_ID, ME_ID, channel, document_media, message, photo_media, user

VALID_HASH = "0123456789abcdef0123456789abcdef"
MB = 1024 * 1024
FILENAME = types.DocumentAttributeFilename


def settings(tmp_path: Path, **overrides: Any) -> ArchiverSettings:
    values: dict[str, Any] = {
        "history_page_size": 10,
        "history_page_delay_s": 0,
        "channel_keep_posts": 5,
        "media_max_file_mb": 1,
        "media_min_free_gb": 1,
    } | overrides
    return ArchiverSettings(_env_file=None, tg_api_id=1, tg_api_hash=VALID_HASH, data_dir=tmp_path, **values)


def disk(free_gb: float = 100.0) -> Any:
    return lambda _path: SimpleNamespace(free=int(free_gb * 1024**3), total=int(200 * 1024**3))


def doc(name: str, size: int, doc_id: int, mime: str = "application/octet-stream", **flags: Any) -> Any:
    return document_media([FILENAME(file_name=name)], mime=mime, size=size, doc_id=doc_id, **flags)


def pm(i: int, media: Any, **fields: Any) -> Any:
    return message(i, peer=types.PeerUser(ALI_ID), media=media, **fields)


async def archive(tmp_path: Path, db: Database, tg: FakeTelegram, cfg: ArchiverSettings) -> None:
    await sync_dialogs(tg, db, cfg)
    importer = HistoryImporter(tg, db, cfg, self_id=ME_ID, sleep=RecordingSleep(), clock=FakeClock())
    await importer.run_until_idle()


def worker(tg: FakeTelegram, db: Database, cfg: ArchiverSettings, **kwargs: Any) -> MediaWorker:
    kwargs.setdefault("sleep", RecordingSleep())
    kwargs.setdefault("disk_usage", disk())
    return MediaWorker(tg, db, cfg, **kwargs)


async def rows(db: Database, chat_id: int = ALI_ID) -> dict[int, Media]:
    """message_id -> joriy media qatori."""
    async with db.read() as session:
        result = await session.execute(
            select(Message.message_id, Media)
            .join(Media, Media.message_pk == Message.id)
            .where(Message.chat_id == chat_id, Media.superseded_at.is_(None))
        )
        return {mid: media for mid, media in result}


def standard_chat(tg: FakeTelegram) -> dict[int, bytes]:
    """1 — rasm, 2 — kichik hujjat, 3 — katta hujjat (2 MB > 1 MB chegara)."""
    tg.add_chat(
        user(),
        pm(1, photo_media()),
        pm(2, doc("hisobot.pdf", 3000, 301, mime="application/pdf")),
        pm(3, doc("kino.zip", 2 * MB, 302, mime="application/zip")),
    )
    files = {1: b"P" * 5000, 2: b"D" * 3000, 3: b"Z" * (2 * MB)}
    for mid, content in files.items():
        tg.files[(ALI_ID, mid)] = content
    return files


# ------------------------------------------------------------------- tasnif


def _objects(**media: Any) -> tuple[Media, Message, Chat]:
    m = Media(media_type=media.pop("media_type", MediaType.PHOTO), expected_size=media.pop("size", 1000), **media)
    msg = Message(chat_id=7, message_id=1, date=at(), noforwards=False)
    chat = Chat(chat_id=7, chat_type=ChatType.PRIVATE, is_protected=False, media_override=None)
    return m, msg, chat


def test_classify_rules(tmp_path: Path) -> None:
    cfg = settings(tmp_path, media_max_file_mb=50)

    def reason(m: Media, msg: Message, chat: Chat, s: ArchiverSettings = cfg) -> MediaSkipReason | None:
        decision = classify(m, msg, chat, s)
        return None if decision.download else decision.reason

    assert reason(*_objects()) is None
    assert reason(*_objects(size=50 * MB)) is None  # chegaraning o'zi — mumkin
    m, msg, chat = _objects(media_type=MediaType.VIDEO, size=60 * MB)
    decision = classify(m, msg, chat, cfg)
    assert decision.reason is MediaSkipReason.TOO_LARGE and decision.note == "katta fayl: 60.0 MB > 50 MB"
    assert reason(*_objects(ttl_seconds=10)) is MediaSkipReason.VIEW_ONCE
    m, msg, chat = _objects()
    msg.noforwards = True
    assert reason(m, msg, chat) is MediaSkipReason.PROTECTED
    m, msg, chat = _objects(force_download=True)
    chat.is_protected = True
    assert reason(m, msg, chat) is MediaSkipReason.PROTECTED  # majburiy yuklash ham himoyani yengmaydi
    assert reason(*_objects(media_type=MediaType.STICKER)) is MediaSkipReason.TYPE
    assert reason(*_objects(), s=settings(tmp_path, media_enabled=False)) is MediaSkipReason.POLICY
    m, msg, chat = _objects()
    chat.chat_type = ChatType.CHANNEL
    assert reason(m, msg, chat) is MediaSkipReason.POLICY
    chat.media_override = True
    assert reason(m, msg, chat) is None
    assert reason(*_objects(size=60 * MB, media_type=MediaType.STICKER, force_download=True)) is None


def test_extensions_and_paths_cannot_escape(tmp_path: Path) -> None:
    def ext(media_type: MediaType, name: str | None = None, mime: str | None = None) -> str:
        return media_extension(Media(media_type=media_type, file_name=name, mime_type=mime))

    assert ext(MediaType.PHOTO) == ".jpg" and ext(MediaType.VOICE, mime="audio/ogg") == ".ogg"
    assert ext(MediaType.DOCUMENT, "../../etc/passwd.PDF") == ".pdf"
    assert ext(MediaType.DOCUMENT, "..\\..\\evil.Exe") == ".exe"
    assert ext(MediaType.DOCUMENT, "x.ex e", mime="application/zip") == ".zip"
    assert ext(MediaType.DOCUMENT, "README") == ".bin"
    assert ext(MediaType.STICKER, mime="application/x-tgsticker") == ".tgs"

    media = Media(id=42, media_type=MediaType.DOCUMENT, file_name="../a.pdf")
    msg = Message(chat_id=-1001234, message_id=7, date=at())
    assert media_relpath(media, msg) == f"-1001234/{at():%Y-%m}/7-42.pdf"

    root = tmp_path / "media"
    root.mkdir()
    assert resolve_media_path(root, "7/2026-10/1-1.jpg").is_relative_to(root.resolve())
    for bad in ("../outside.jpg", "7/../../outside.jpg", "/etc/passwd", ""):
        with pytest.raises(ValueError):
            resolve_media_path(root, bad)


# ---------------------------------------------------------------- yuklash


async def test_small_files_downloaded_large_file_listed(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    files = standard_chat(tg)
    cfg = settings(tmp_path)
    await archive(tmp_path, db, tg, cfg)
    w = worker(tg, db, cfg)
    await w.prepare()
    await w.run_once()

    media = await rows(db)
    for mid in (1, 2):
        assert media[mid].status is MediaStatus.DONE
        path = resolve_media_path(cfg.paths.media, media[mid].local_path)
        assert path.read_bytes() == files[mid]
        assert media[mid].sha256 == hashlib.sha256(files[mid]).hexdigest()
        assert media[mid].stored_size == len(files[mid])
        assert media[mid].local_path.startswith(f"{ALI_ID}/")
        if os.name == "posix":
            assert path.stat().st_mode & 0o077 == 0
    assert media[2].local_path.endswith(".pdf")

    big = media[3]
    assert (big.status, big.skip_reason) == (MediaStatus.SKIPPED, MediaSkipReason.TOO_LARGE)
    assert big.status_note == "katta fayl: 2.0 MB > 1 MB" and big.local_path is None
    assert tg.count("download_media") == 2  # katta fayl umuman so'ralmadi

    async with db.read() as session:
        large = await list_large_files(session)
        summary = await media_summary(session)
    assert [(f.file_name, f.size, f.chat_title, f.message_id) for f in large] == [("kino.zip", 2 * MB, "Ali", 3)]
    assert summary.by_status == {"done": 2, "skipped": 1}
    assert (summary.large_count, summary.large_bytes, summary.stored_bytes) == (1, 2 * MB, 8000)
    assert list((cfg.paths.media / ".tmp").iterdir()) == []


async def test_size_discovered_during_download(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    tg.add_chat(user(), pm(1, doc("yolgon.bin", 1000, 305)))  # Telegram 1000 bayt degan
    tg.files[(ALI_ID, 1)] = b"X" * (2 * MB)
    cfg = settings(tmp_path)
    await archive(tmp_path, db, tg, cfg)
    w = worker(tg, db, cfg)
    await w.run_once()

    media = (await rows(db))[1]
    assert (media.status, media.skip_reason) == (MediaStatus.SKIPPED, MediaSkipReason.TOO_LARGE)
    assert media.expected_size > MB and media.local_path is None
    assert list((cfg.paths.media / ".tmp").iterdir()) == []


async def test_owner_can_fetch_a_large_file(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    files = standard_chat(tg)
    tg.add(ALI_ID, pm(4, doc("maxfiy.pdf", 100, 310), noforwards=True))
    cfg = settings(tmp_path)
    await archive(tmp_path, db, tg, cfg)
    w = worker(tg, db, cfg)
    await w.run_once()
    media = await rows(db)

    async with db.write() as session:
        await request_fetch(session, media[3].id)
        for bad in (media[4].id, media[1].id, 999_999):  # himoyalangan, allaqachon yuklangan, yo'q
            with pytest.raises(FetchRefused):
                await request_fetch(session, bad)
    await w.run_once()

    big = (await rows(db))[3]
    assert big.status is MediaStatus.DONE and big.force_download is True
    assert resolve_media_path(cfg.paths.media, big.local_path).read_bytes() == files[3]
    assert (await rows(db))[4].skip_reason is MediaSkipReason.PROTECTED


async def test_same_file_is_downloaded_once(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    tg.add_chat(user(), pm(1, doc("rasm.pdf", 500, 400)), pm(2, doc("rasm.pdf", 500, 400)))
    tg.add_chat(user(8, first_name="Vali"), message(5, peer=types.PeerUser(8), media=doc("rasm.pdf", 500, 400)))
    for key in ((ALI_ID, 1), (ALI_ID, 2), (8, 5)):
        tg.files[key] = b"S" * 500
    cfg = settings(tmp_path)
    await archive(tmp_path, db, tg, cfg)
    w = worker(tg, db, cfg)
    await w.run_once()
    await w.run_once()

    all_media = list((await rows(db)).values()) + list((await rows(db, 8)).values())
    assert {m.status for m in all_media} == {MediaStatus.DONE}
    assert len({m.local_path for m in all_media}) == 1
    assert tg.count("download_media") == 1 and w.stats["reused"] == 2
    async with db.read() as session:
        assert (await media_summary(session)).stored_bytes == 500  # disk hajmi takrorsiz


async def test_message_gone_on_telegram_is_unavailable(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    standard_chat(tg)
    cfg = settings(tmp_path)
    await archive(tmp_path, db, tg, cfg)
    del tg.history[ALI_ID][2]
    await worker(tg, db, cfg).run_once()
    media = (await rows(db))[2]
    assert media.status is MediaStatus.UNAVAILABLE and "yo'q" in media.status_note


# ------------------------------------------------------------------ xatolar


async def test_errors_retry_with_backoff_then_fail(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    tg.add_chat(user(), pm(1, photo_media()))
    tg.files[(ALI_ID, 1)] = b"P" * 100
    cfg = settings(tmp_path, media_max_attempts=2)
    await archive(tmp_path, db, tg, cfg)
    clock = FakeClock()
    w = worker(tg, db, cfg, clock=clock)

    tg.fail("download_media", RuntimeError("buzildi"), on_call=1)
    await w.run_once()
    media = (await rows(db))[1]
    assert (media.status, media.attempts) == (MediaStatus.PENDING, 1)
    assert media.status_note == "RuntimeError: buzildi"
    assert (media.next_attempt_at - clock.now).total_seconds() == RETRY_S[0]
    assert await w.run_once() == 0  # vaqti kelmagan

    clock.advance(RETRY_S[0] + 1)
    tg.fail("download_media", RuntimeError("yana"), on_call=1)
    await w.run_once()
    media = (await rows(db))[1]
    assert (media.status, media.attempts) == (MediaStatus.FAILED, 2)


async def test_flood_wait_is_not_an_attempt(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    tg.add_chat(user(), pm(1, photo_media()))
    cfg = settings(tmp_path)
    await archive(tmp_path, db, tg, cfg)
    clock, sleep = FakeClock(), RecordingSleep()
    tg.fail("download_media", errors.FloodWaitError(request=None, capture=30), on_call=1)
    await worker(tg, db, cfg, clock=clock, sleep=sleep).run_once()

    media = (await rows(db))[1]
    assert (media.status, media.attempts, media.status_note) == (MediaStatus.PENDING, 0, "FloodWait 30 s")
    assert (media.next_attempt_at - clock.now).total_seconds() == 30 and 30 in sleep.calls


async def test_low_disk_pauses_and_keeps_queue(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    standard_chat(tg)
    cfg = settings(tmp_path, media_min_free_gb=5)
    await archive(tmp_path, db, tg, cfg)
    w = worker(tg, db, cfg, disk_usage=disk(free_gb=4))
    await w.run_once()

    media = await rows(db)
    assert {media[1].status, media[2].status} == {MediaStatus.PENDING}
    assert media[3].skip_reason is MediaSkipReason.TOO_LARGE  # tasnif to'xtamaydi
    assert w.paused.startswith("diskda joy kam") and tg.count("download_media") == 0

    w._disk_usage = disk(free_gb=50)
    await w.run_once()
    assert w.paused is None and (await rows(db))[1].status is MediaStatus.DONE


async def test_total_media_cap(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    standard_chat(tg)
    cfg = settings(tmp_path, media_max_total_gb=0.00001)  # ~10 KB
    await archive(tmp_path, db, tg, cfg)
    w = worker(tg, db, cfg)
    await w.run_once()
    # Eng yangisidan: hujjat (3 KB) sig'adi, rasm (kutilgan 120 KB) chegaradan oshadi — kutadi
    assert w.paused == "MEDIA_MAX_TOTAL_GB to'ldi: 2.9 KB / 10.5 KB"
    media = await rows(db)
    assert (media[2].status, media[1].status) == (MediaStatus.DONE, MediaStatus.PENDING)
    assert tg.count("download_media") == 1


# --------------------------------------------------- tiklanish va tekshiruv


async def test_prepare_recovers_interrupted_download(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    standard_chat(tg)
    cfg = settings(tmp_path)
    await archive(tmp_path, db, tg, cfg)
    media = await rows(db)
    async with db.write() as session:
        (await session.get(Media, media[1].id)).status = MediaStatus.DOWNLOADING
    tmp = cfg.paths.media / ".tmp"
    tmp.mkdir(parents=True)
    (tmp / f"{media[1].id}.part").write_bytes(b"yarim")

    w = worker(tg, db, cfg)
    await w.prepare()
    assert (await rows(db))[1].status is MediaStatus.PENDING
    assert list(tmp.iterdir()) == []
    await w.run_once()
    assert (await rows(db))[1].status is MediaStatus.DONE


async def test_verify_finds_missing_and_corrupt_files_and_redownloads(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    files = standard_chat(tg)
    cfg = settings(tmp_path)
    await archive(tmp_path, db, tg, cfg)
    w = worker(tg, db, cfg)
    await w.run_once()
    media = await rows(db)
    photo = resolve_media_path(cfg.paths.media, media[1].local_path)
    pdf = resolve_media_path(cfg.paths.media, media[2].local_path)
    photo.unlink()
    pdf.write_bytes(b"?" * len(files[2]))  # hajm bir xil, mazmun boshqa

    quick = await w.verify()
    assert (quick.checked, quick.missing, quick.mismatched) == (2, 1, 0)
    full = await w.verify(checksum=True)
    assert (full.checked, full.mismatched) == (1, 1)
    assert {m.status for mid, m in (await rows(db)).items() if mid != 3} == {MediaStatus.MISSING}

    await w.run_once()
    assert photo.read_bytes() == files[1] and pdf.read_bytes() == files[2]
    assert {m.status for mid, m in (await rows(db)).items() if mid != 3} == {MediaStatus.DONE}


async def test_policy_change_reviews_skipped_but_never_protected(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    standard_chat(tg)
    tg.add(ALI_ID, pm(4, doc("maxfiy.pdf", 100, 311), noforwards=True))
    cfg = settings(tmp_path, media_types="photo")
    await archive(tmp_path, db, tg, cfg)
    w = worker(tg, db, cfg)
    await w.prepare()
    await w.run_once()
    media = await rows(db)
    assert media[2].skip_reason is MediaSkipReason.TYPE and media[3].skip_reason is MediaSkipReason.TYPE

    bigger = settings(tmp_path, media_types="photo,document", media_max_file_mb=5)
    w2 = worker(tg, db, bigger)
    await w2.prepare()
    await w2.run_once()
    media = await rows(db)
    assert media[2].status is MediaStatus.DONE and media[3].status is MediaStatus.DONE
    assert media[4].skip_reason is MediaSkipReason.PROTECTED


# --------------------------------------------------------------------- axlat


async def test_window_pruned_post_file_is_deleted_unless_shared(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()

    def post(i: int, doc_id: int) -> Any:
        return message(i, peer=types.PeerChannel(CHANNEL_RAW_ID), post=True, media=doc(f"{i}.pdf", 100, doc_id))

    ch = tg.add_chat(channel(), *[post(i, 9000 + i) for i in range(1, 6)], post(6, 9002))  # 2 va 6 — bir xil fayl
    for i in range(1, 9):
        tg.files[(ch, i)] = f"fayl-{i}".encode()
    tg.files[(ch, 6)] = tg.files[(ch, 2)]
    cfg = settings(tmp_path, media_in_channels=True)
    await archive(tmp_path, db, tg, cfg)
    w = worker(tg, db, cfg)
    await w.run_once()
    await w.run_once()
    before = await rows(db, ch)
    assert sorted(before) == [2, 3, 4, 5, 6] and {m.status for m in before.values()} == {MediaStatus.DONE}
    path3 = resolve_media_path(cfg.paths.media, before[3].local_path)
    shared = resolve_media_path(cfg.paths.media, before[2].local_path)
    assert before[6].local_path == before[2].local_path

    tg.add(ch, post(7, 9007), post(8, 9008))
    await archive(tmp_path, db, tg, cfg)  # oyna: 4..8, postlar 2 va 3 chiqadi
    async with db.read() as session:
        trash = list(await session.scalars(select(MediaTrash.local_path)))
    assert trash == [before[3].local_path]  # umumiy fayl axlatga tushmaydi

    assert await w.empty_trash() == 1
    assert not path3.exists() and shared.exists()


# ----------------------------------------------------------- xizmat va CLI


async def test_service_downloads_media_in_run_mode(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    cfg.paths.db.parent.mkdir(parents=True)
    upgrade(sqlite_url(cfg.paths.db))
    cfg.session_path.parent.mkdir(parents=True)
    cfg.session_path.touch()
    tg = FakeTelegram()
    standard_chat(tg)
    stop = asyncio.Event()

    async def sleep(_seconds: float) -> None:
        await asyncio.sleep(0)
        media = service.media
        if service.state == "idle" and media is not None and media.state == "idle":
            stop.set()

    service = ArchiverService(
        cfg, client_factory=lambda _: tg, stop=stop, sleep=sleep, media_factory=lambda *a, **k: worker(*a, **k)
    )
    assert await asyncio.wait_for(service.run(), timeout=20) == EXIT_OK

    db = Database(cfg.database_url)
    statuses = {mid: m.status for mid, m in (await rows(db)).items()}
    await db.dispose()
    assert statuses == {1: MediaStatus.DONE, 2: MediaStatus.DONE, 3: MediaStatus.SKIPPED}


async def test_import_once_does_not_download(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    cfg.paths.db.parent.mkdir(parents=True)
    upgrade(sqlite_url(cfg.paths.db))
    cfg.session_path.parent.mkdir(parents=True)
    cfg.session_path.touch()
    tg = FakeTelegram()
    standard_chat(tg)
    assert await ArchiverService(cfg, client_factory=lambda _: tg, sleep=RecordingSleep()).run(once=True) == EXIT_OK
    assert tg.count("download_media") == 0


def test_media_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = tmp_path / "test.env"
    env.write_text(
        f"DATA_DIR={tmp_path.as_posix()}\nTG_API_ID=1\nTG_API_HASH={VALID_HASH}\nMEDIA_MAX_FILE_MB=1\n"
        "MEDIA_MIN_FREE_GB=0\n",
        encoding="utf-8",
    )
    assert main(["--env-file", str(env), "db-upgrade"]) == EXIT_OK
    cfg = settings(tmp_path, media_min_free_gb=0)
    tg = FakeTelegram()
    standard_chat(tg)

    async def prepare() -> int:
        db = Database(cfg.database_url)
        try:
            await archive(tmp_path, db, tg, cfg)
            await worker(tg, db, cfg).run_once()
            return (await rows(db))[3].id
        finally:
            await db.dispose()

    big_id = asyncio.run(prepare())
    capsys.readouterr()

    assert main(["--env-file", str(env), "media", "large"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "kino.zip" in out and "2.0 MB" in out and "Ali" in out and str(big_id) in out

    assert main(["--env-file", str(env), "status"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "katta fayllar (> 1 MB) 1 (2.0 MB)" in out and "yuklangan 2" in out

    assert main(["--env-file", str(env), "media", "fetch", str(big_id)]) == EXIT_OK
    assert "navbatga qo'yildi" in capsys.readouterr().out
    assert main(["--env-file", str(env), "media", "fetch", "999999"]) == EXIT_RUNTIME

    assert main(["--env-file", str(env), "media", "verify"]) == EXIT_OK
    assert "Tekshirildi: 2, diskda yo'q: 0" in capsys.readouterr().out
