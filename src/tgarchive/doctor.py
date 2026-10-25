"""``tgarchive doctor`` — serverdagi barcha ulanish nuqtalari va sozlamalarni tekshirish.

Ma'lumotlarni o'zgartirmaydi: session'ning vaqtinchalik nusxasi bilan ishlaydi, bazani faqat o'qiydi
(yo'q kataloglarni yaratishi mumkin). Natija:
✅ ishlaydi, ⚠️ ogohlantirish (ishlaydi, lekin e'tibor bering), ❌ xato (tuzatish shart),
➖ tekshirilmadi. Chiqish kodi: xato bo'lsa 1, aks holda 0.

Tarmoq tekshiruvlari:
* API_ID/API_HASH — ``auth.exportLoginToken`` (QR-login tokeni): ikkalasini ham tekshiradi,
  telefonga kod yubormaydi, hech kimga xabar bermaydi. ``help.getConfig`` api_id ni tekshirmaydi.
* Session — nusxasi bilan ``get_me``; archiver hozir ishlayotgan bo'lsa, session tekshirilmaydi
  (bitta session'ni ikki jarayon ishlatmasin) — uning holati heartbeat'dan olinadi.
* Bot — ``getMe`` va ``getChat(OWNER_ID)``.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from tgarchive.config import ArchiverSettings, BaseAppSettings, BotSettings, format_validation_error
from tgarchive.db.media import format_size
from tgarchive.db.types import utcnow
from tgarchive.fs import IS_POSIX

Status = Literal["ok", "warn", "fail", "skip"]
ICONS = {"ok": "✅", "warn": "⚠️", "fail": "❌", "skip": "➖"}
HEARTBEAT_FRESH = timedelta(minutes=3)
NETWORK_TIMEOUT_S = 20


@dataclass(frozen=True, slots=True)
class Check:
    section: str
    name: str
    status: Status
    detail: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, section: str, name: str, status: Status, detail: str) -> None:
        self.checks.append(Check(section, name, status, detail))

    @property
    def failures(self) -> int:
        return sum(c.status == "fail" for c in self.checks)

    @property
    def warnings(self) -> int:
        return sum(c.status == "warn" for c in self.checks)

    def render(self) -> str:
        lines: list[str] = []
        section = None
        for c in self.checks:
            if c.section != section:
                section = c.section
                lines.append(f"[{section}]")
            lines.append(f"  {ICONS[c.status]} {c.name}: {c.detail}")
        lines.append(f"Natija: {self.failures} xato, {self.warnings} ogohlantirish")
        return "\n".join(lines)


# Tarmoq so'rovlari alohida — testlarda almashtiriladi
ApiProbe = Callable[[int, str], Awaitable[tuple[Status, str]]]
SessionProbe = Callable[[ArchiverSettings, Path], Awaitable[tuple[Status, str, int | None]]]
BotProbe = Callable[[BotSettings], Awaitable[list[tuple[str, Status, str]]]]


def _load(cls: type, env_file: str | Path | None) -> tuple[Any, bool, str]:
    """(sozlamalar | None, faqat "berilmagan" xatolarmi, xato matni)."""
    try:
        return cls(_env_file=env_file), False, ""
    except ValidationError as exc:
        missing_only = {e["type"] for e in exc.errors()} == {"missing"}
        return None, missing_only, format_validation_error(exc)


def _writable(path: Path) -> str | None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".doctor-", delete=True):
            pass
    except OSError as exc:
        return exc.strerror or type(exc).__name__
    return None


def _check_dirs(report: Report, section: str, writable: dict[str, Path], readable: dict[str, Path]) -> None:
    problems = [f"{name} ({path}): {err}" for name, path in writable.items() if (err := _writable(path))]
    problems += [f"{name} ({path}): o'qib bo'lmaydi" for name, path in readable.items() if not os.access(path, os.R_OK)]
    if problems:
        report.add(section, "Kataloglar", "fail", "; ".join(problems) + " — volume ruxsatlarini tekshiring")
    else:
        names = ", ".join(list(writable) + [f"{n} (o'qish)" for n in readable])
        report.add(section, "Kataloglar", "ok", f"yozish mumkin: {names}")


def _check_sqlite(report: Report, section: str) -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        version = conn.execute("SELECT sqlite_version()").fetchone()[0]
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        except sqlite3.OperationalError:
            report.add(section, "SQLite", "fail", f"{version}, FTS5 yo'q — qidiruv ishlamaydi")
            return
    report.add(section, "SQLite", "ok", f"{version}, FTS5 bor")


def _check_schema(report: Report, section: str, db_path: Path) -> int | None:
    """Bazaning migratsiyasi; qaytaradi: bog'langan akkaunt ID'si."""
    from tgarchive.db.migrate import head_revision

    head = head_revision()
    if not db_path.exists():
        report.add(section, "Baza", "warn", f"hali yaratilmagan ({db_path}) — archiver birinchi ishga tushganda yaratadi")
        return None
    try:
        with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            account = conn.execute("SELECT value FROM app_meta WHERE key = 'account_id'").fetchone()
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.Error as exc:
        report.add(section, "Baza", "fail", f"o'qib bo'lmadi: {exc}")
        return None
    revision = row[0] if row else None
    if check != "ok":
        report.add(section, "Baza", "fail", f"butunlik tekshiruvi: {check} — zaxiradan tiklash kerak bo'lishi mumkin")
    elif revision != head:
        report.add(section, "Baza", "warn", f"migratsiya {revision} (kod: {head}) — archiver ishga tushganda yangilaydi")
    else:
        report.add(section, "Baza", "ok", f"{db_path}, migratsiya {revision}, butun")
    return int(account[0]) if account else None


def _heartbeat(db_path: Path, component: str) -> tuple[str, float] | None:
    if not db_path.exists():
        return None
    try:
        with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as conn:
            row = conn.execute(
                "SELECT state, last_beat_at FROM component_heartbeats WHERE component = ?", (component,)
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    beat = datetime.fromisoformat(str(row[1])).replace(tzinfo=UTC)
    return row[0], (utcnow() - beat).total_seconds()


# ---------------------------------------------------------------- tarmoq


async def probe_api_credentials(api_id: int, api_hash: str) -> tuple[Status, str]:
    from telethon import TelegramClient, errors, functions
    from telethon.sessions import MemorySession

    client = TelegramClient(
        MemorySession(), api_id, api_hash, receive_updates=False, connection_retries=1, timeout=NETWORK_TIMEOUT_S
    )
    try:
        await client.connect()
        await client(functions.auth.ExportLoginTokenRequest(api_id=api_id, api_hash=api_hash, except_ids=[]))
        return "ok", "Telegram tasdiqladi"
    except errors.ApiIdInvalidError:
        return "fail", "Telegram rad etdi: TG_API_ID yoki TG_API_HASH noto'g'ri (my.telegram.org dan qayta ko'chiring)"
    except errors.FloodWaitError as exc:
        return "warn", f"Telegram cheklovi ({exc.seconds} s) — keyinroq qayta tekshiring"
    except (OSError, ConnectionError, TimeoutError) as exc:
        return "fail", f"Telegram'ga ulanib bo'lmadi: {type(exc).__name__} — server tarmog'i/firewall"
    except errors.RPCError as exc:
        return "warn", f"kutilmagan javob: {type(exc).__name__}"
    finally:
        await client.disconnect()


async def probe_session(settings: ArchiverSettings, workdir: Path) -> tuple[Status, str, int | None]:
    """Session nusxasi bilan ``get_me``. Asl fayl o'zgarmaydi."""
    from telethon import TelegramClient, errors

    copy = workdir / "doctor-check.session"
    shutil.copy2(settings.session_path, copy)
    client = TelegramClient(
        str(copy),
        settings.tg_api_id,
        settings.tg_api_hash.get_secret_value(),
        receive_updates=False,
        connection_retries=1,
        timeout=NETWORK_TIMEOUT_S,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return "fail", "session yaroqsiz yoki bekor qilingan — qayta: tgarchive login", None
        me = await client.get_me()
        name = " ".join(filter(None, [me.first_name, me.last_name])) or "-"
        username = f", @{me.username}" if me.username else ""
        return "ok", f"{name} (id {me.id}{username})", me.id
    except errors.RPCError as exc:
        return "fail", f"Telegram xatosi: {type(exc).__name__}", None
    except (OSError, ConnectionError, TimeoutError) as exc:
        return "fail", f"Telegram'ga ulanib bo'lmadi: {type(exc).__name__}", None
    finally:
        await client.disconnect()
        client.session.close()


async def probe_bot(settings: BotSettings) -> list[tuple[str, Status, str]]:
    from aiogram import Bot
    from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError, TelegramUnauthorizedError

    bot = Bot(settings.bot_token.get_secret_value())
    results: list[tuple[str, Status, str]] = []
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            return [("BOT_TOKEN", "fail", "Telegram rad etdi — @BotFather dagi tokenni qayta ko'chiring")]
        except TelegramNetworkError as exc:
            return [("BOT_TOKEN", "fail", f"api.telegram.org ga ulanib bo'lmadi: {exc}")]
        results.append(("BOT_TOKEN", "ok", f"@{me.username}"))
        if settings.owner_id == me.id:
            results.append(("OWNER_ID", "fail", "bu botning o'z ID'si — ikkinchi akkauntingiz ID'sini yozing"))
            return results
        try:
            chat = await bot.get_chat(settings.owner_id)
            who = " ".join(filter(None, [chat.first_name, chat.last_name])) or chat.username or "-"
            results.append(("OWNER_ID", "ok", f"{settings.owner_id} ({who}) — botga /start yuborgan"))
        except TelegramBadRequest:
            results.append(
                ("OWNER_ID", "warn", f"{settings.owner_id} botga hali /start yubormagan (yoki ID noto'g'ri) — ikkinchi akkauntdan botga /start yuboring")
            )
        except TelegramAPIError as exc:
            results.append(("OWNER_ID", "warn", f"tekshirib bo'lmadi: {type(exc).__name__}"))
    finally:
        await bot.session.close()
    return results


# ------------------------------------------------------------------ asosiy


async def run_doctor(
    env_file: str | Path | None,
    *,
    component: Literal["auto", "archiver", "bot", "all"] = "auto",
    offline: bool = False,
    api_probe: ApiProbe = probe_api_credentials,
    session_probe: SessionProbe = probe_session,
    bot_probe: BotProbe = probe_bot,
) -> Report:
    report = Report()
    base, _, error = _load(BaseAppSettings, env_file)
    if base is None:
        report.add("umumiy", "Konfiguratsiya", "fail", error.replace("\n", " "))
        return report
    paths = base.paths
    report.add("umumiy", "Konfiguratsiya", "ok", f"DATA_DIR={paths.data}, vaqt mintaqasi {base.display_timezone}")
    _check_sqlite(report, "umumiy")
    account_id = _check_schema(report, "umumiy", paths.db)

    archiver, archiver_missing, archiver_error = _load(ArchiverSettings, env_file)
    bot, bot_missing, bot_error = _load(BotSettings, env_file)
    want_archiver = component in ("archiver", "all") or (component == "auto" and not archiver_missing)
    want_bot = component in ("bot", "all") or (component == "auto" and not bot_missing)

    if want_archiver:
        await _archiver_checks(report, archiver, archiver_error, account_id, offline, api_probe, session_probe)
    else:
        report.add("archiver", "Sozlamalar", "skip", "bu muhitda TG_API_ID/TG_API_HASH yo'q (bot konteyneri bo'lsa — normal)")
    if want_bot:
        await _bot_checks(report, bot, bot_error, account_id, offline, bot_probe)
    else:
        report.add("bot", "Sozlamalar", "skip", "bu muhitda BOT_TOKEN/OWNER_ID yo'q (archiver konteyneri bo'lsa — normal)")
    return report


async def _archiver_checks(
    report: Report,
    settings: ArchiverSettings | None,
    error: str,
    account_id: int | None,
    offline: bool,
    api_probe: ApiProbe,
    session_probe: SessionProbe,
) -> None:
    section = "archiver"
    if settings is None:
        report.add(section, "Sozlamalar", "fail", error.replace("\n", " "))
        return
    paths = settings.paths
    _check_dirs(
        report,
        section,
        {"db": paths.db.parent, "media": paths.media, "exports": paths.exports, "backups": paths.backups, "session": paths.sessions},
        {},
    )
    usage = shutil.disk_usage(paths.data if paths.data.exists() else paths.data.parent)
    min_free = settings.media_min_free_gb * 1024**3
    status: Status = "ok" if usage.free > min_free * 2 else "warn"
    report.add(section, "Disk", status, f"bo'sh {format_size(usage.free)} (media yuklash {settings.media_min_free_gb} GB da to'xtaydi)")

    if offline:
        report.add(section, "TG_API_ID/TG_API_HASH", "skip", "--offline")
    else:
        status, detail = await api_probe(settings.tg_api_id, settings.tg_api_hash.get_secret_value())
        report.add(section, "TG_API_ID/TG_API_HASH", status, detail)

    running = _heartbeat(paths.db, "archiver")
    if not settings.session_path.exists():
        report.add(section, "Session", "fail", "yo'q — bir marta: docker compose run --rm archiver login")
    elif running and running[1] < HEARTBEAT_FRESH.total_seconds() and running[0] not in ("stopped", "auth_error"):
        report.add(section, "Session", "ok", f"archiver ishlayapti ({running[0]}, {int(running[1])} s oldin) — session band, alohida tekshirilmadi")
    elif offline:
        report.add(section, "Session", "skip", f"fayl bor ({settings.session_path}), --offline")
    else:
        if IS_POSIX and settings.session_path.stat().st_mode & 0o077:
            report.add(section, "Session ruxsati", "warn", "fayl boshqalarga ochiq — chmod 600")
        with tempfile.TemporaryDirectory(prefix="tgarchive-doctor-") as tmp:
            status, detail, me_id = await session_probe(settings, Path(tmp))
        if status == "ok" and account_id is not None and me_id is not None and me_id != account_id:
            status, detail = "fail", f"{detail}, lekin baza boshqa akkauntga ({account_id}) bog'langan"
        report.add(section, "Session", status, detail)
        if running and running[0] == "auth_error":
            report.add(section, "Archiver", "warn", "oxirgi holat auth_error — login'dan keyin: docker compose restart archiver")

    if settings.backup_age_recipients:
        report.add(section, "Zaxira", "ok", f"har {settings.backup_interval_h} soatda, age bilan shifrlanadi, {settings.backup_keep} ta saqlanadi")
    elif settings.backup_enabled:
        report.add(section, "Zaxira", "warn", "SHIFRLANMAYDI — BACKUP_AGE_RECIPIENTS ga age ochiq kalitini yozing (docs/10-backup-export.md)")
    else:
        report.add(section, "Zaxira", "warn", "o'chirilgan (BACKUP_ENABLED=false)")


async def _bot_checks(
    report: Report,
    settings: BotSettings | None,
    error: str,
    account_id: int | None,
    offline: bool,
    bot_probe: BotProbe,
) -> None:
    section = "bot"
    if settings is None:
        report.add(section, "Sozlamalar", "fail", error.replace("\n", " "))
        return
    paths = settings.paths
    _check_dirs(report, section, {"db": paths.db.parent, "exports": paths.exports}, {"media": paths.media} if paths.media.exists() else {})
    if account_id is not None and settings.owner_id == account_id:
        report.add(
            section, "OWNER_ID", "warn", "asosiy (arxivlanayotgan) akkaunt bilan bir xil — boshqaruv uchun ikkinchi akkaunt tavsiya etiladi"
        )
    if offline:
        report.add(section, "BOT_TOKEN", "skip", "--offline")
        return
    for name, status, detail in await bot_probe(settings):
        report.add(section, name, status, detail)
