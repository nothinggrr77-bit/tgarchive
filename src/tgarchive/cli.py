"""Boshqaruv buyruqlari: ``python -m tgarchive <buyruq>`` yoki ``tgarchive <buyruq>``.

Chiqish kodlari: 0 — muvaffaqiyat, 1 — ish vaqtidagi muammo, 2 — konfiguratsiya xatosi,
3 — avtorizatsiya muammosi (session yo'q, bekor qilingan yoki boshqa akkaunt).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import text

from tgarchive import __version__
from tgarchive.config import ArchiverSettings, BaseAppSettings, BotSettings, ConfigError, load_settings
from tgarchive.db.engine import Database
from tgarchive.db.meta import AccountMismatchError, bind_account, bound_account_id
from tgarchive.db.migrate import current_revision, head_revision, upgrade
from tgarchive.db.status import ARCHIVER, format_summary, get_heartbeat, import_summary
from tgarchive.db.store import add_audit
from tgarchive.db.types import utcnow
from tgarchive.exitcodes import EXIT_AUTH, EXIT_CONFIG, EXIT_OK, EXIT_RUNTIME
from tgarchive.fs import ensure_private_dir, permission_warnings, set_private_umask
from tgarchive.logging_setup import setup_logging

# archiver holatlari + bot ("running")
HEALTHY_STATES = frozenset({"starting", "syncing", "importing", "idle", "reconnecting", "running"})

_INFO_TABLES = ("chats", "messages", "message_versions", "media", "deletion_events", "audit_log")
_HIDDEN = "o'rnatilgan (yashirin)"


class AuthProblem(Exception):
    """Session yo'q, yaroqsiz yoki baza boshqa akkauntga tegishli."""


# ------------------------------------------------------------------ check-config


def _cmd_check_config(args: argparse.Namespace) -> int:
    base = load_settings(BaseAppSettings, args.env_file)
    paths = base.paths
    print(f"tgarchive {__version__}")
    print(f"  DATA_DIR    {paths.data}")
    print(f"  DB          {paths.db}")
    print(f"  MEDIA_DIR   {paths.media}")
    print(f"  EXPORT_DIR  {paths.exports}")
    print(f"  BACKUP_DIR  {paths.backups}")
    print(f"  LOG         {base.log_level} / {base.log_format}")
    retention = f"{base.retention_days} kun" if base.retention_days else "cheksiz"
    print(f"  RETENTION   {retention}")

    components = ("archiver", "bot") if args.component == "all" else (args.component,)
    failed = 0
    for component in components:
        try:
            if component == "archiver":
                archiver = load_settings(ArchiverSettings, args.env_file)
                print("[archiver] OK")
                print(f"  TG_API_ID      {_HIDDEN}")
                print(f"  TG_API_HASH    {_HIDDEN}")
                session_state = "mavjud" if archiver.session_path.exists() else "hali yo'q (tgarchive login)"
                print(f"  SESSION        {archiver.session_path} — {session_state}")
                history = "yoqilgan" if archiver.history_enabled else "o'chiq"
                channels = (
                    f"oxirgi {archiver.channel_keep_posts} ta post"
                    if archiver.channel_keep_posts
                    else "to'liq tarix"
                )
                comments = "har biri saqlanadi" if archiver.channel_save_comments else "saqlanmaydi"
                media = "yoqilgan: " + ",".join(sorted(archiver.media_types)) if archiver.media_enabled else "o'chiq"
                print(f"  HISTORY        {history}")
                print(f"  KANALLAR       {channels}; izohlar {comments}")
                print(f"  MEDIA          {media}")
            else:
                bot = load_settings(BotSettings, args.env_file)
                print("[bot] OK")
                print(f"  BOT_TOKEN      {_HIDDEN}")
                print(f"  OWNER_ID       {bot.owner_id}")
        except ConfigError as exc:
            failed += 1
            print(f"[{component}] XATO")
            print(exc)

    for warning in permission_warnings([Path(args.env_file), paths.data, paths.sessions, paths.db.parent]):
        print(f"OGOHLANTIRISH: {warning}")
    return EXIT_RUNTIME if failed else EXIT_OK


# ---------------------------------------------------------------------- database


def _prepare_database(settings: BaseAppSettings) -> None:
    ensure_private_dir(settings.paths.data)
    ensure_private_dir(settings.paths.db.parent)
    upgrade(settings.database_url)


def _open_db(settings: BaseAppSettings) -> Database:
    return Database(settings.database_url, busy_timeout_ms=settings.db_busy_timeout_ms)


def _cmd_db_upgrade(args: argparse.Namespace) -> int:
    settings = load_settings(BaseAppSettings, args.env_file)
    setup_logging(settings.log_level, "console")
    logging.getLogger("alembic").setLevel(logging.INFO)  # qaysi migratsiyalar qo'llanganini ko'rsatish
    _prepare_database(settings)
    print(f"Baza tayyor: {settings.paths.db} (revision {head_revision()})")
    return EXIT_OK


async def _db_info(settings: BaseAppSettings) -> int:
    db = _open_db(settings)
    try:
        async with db.read() as session:
            conn = await session.connection()
            revision = await conn.run_sync(current_revision)
            print(f"Baza:      {settings.paths.db}")
            print(f"SQLite:    {await session.scalar(text('SELECT sqlite_version()'))}")
            print(f"Journal:   {await session.scalar(text('PRAGMA journal_mode'))}")
            head = head_revision()
            print(f"Revision:  {revision or '-'} (kod: {head})")
            if revision != head:
                print("Baza sxemasi eski yoki migratsiya qo'llanmagan. Bajaring: tgarchive db-upgrade")
                return EXIT_RUNTIME
            account = await bound_account_id(session)
            account_text = str(account) if account is not None else "hali bog'lanmagan (tgarchive login)"
            print(f"Akkaunt:   {account_text}")
            for table in _INFO_TABLES:
                count = await session.scalar(text(f"SELECT count(*) FROM {table}"))
                print(f"  {table:<18} {count}")
    finally:
        await db.dispose()
    return EXIT_OK


def _cmd_db_info(args: argparse.Namespace) -> int:
    settings = load_settings(BaseAppSettings, args.env_file)
    if not settings.paths.db.exists():
        print(f"Baza topilmadi: {settings.paths.db}\nAvval bajaring: tgarchive db-upgrade")
        return EXIT_RUNTIME
    return asyncio.run(_db_info(settings))


# ------------------------------------------------------------------- account


class ConsolePrompter:
    def ask_phone(self) -> str:
        return input("Telefon raqam (+998...): ")

    def ask_code(self) -> str:
        return input("Telegram'dan kelgan kod: ")

    def ask_password(self) -> str:
        return getpass.getpass("2FA parol (ekranda ko'rinmaydi): ")

    def notify(self, text: str) -> None:
        print(text)


def _load_archiver(args: argparse.Namespace) -> ArchiverSettings:
    settings = load_settings(ArchiverSettings, args.env_file)
    setup_logging(settings.log_level, "console", secrets=settings.secret_values())
    return settings


def _describe(user: Any) -> str:
    from telethon import utils

    username = f", @{user.username}" if user.username else ""
    return f"{utils.get_display_name(user) or '-'} (id={user.id}{username})"


@asynccontextmanager
async def _authorized(settings: ArchiverSettings) -> AsyncIterator[tuple[Any, Any, Database]]:
    """Mavjud session bilan ulanadi va baza shu akkauntga tegishliligini tekshiradi."""
    from tgarchive.archiver.client import create_client

    if not settings.session_path.exists():
        raise AuthProblem("Session yo'q. Avval bajaring: tgarchive login")
    client = create_client(settings)
    db = _open_db(settings)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise AuthProblem("Session yaroqsiz yoki Telegram'da bekor qilingan. Qayta: tgarchive login")
        me = await client.get_me()
        async with db.read() as session:
            bound = await bound_account_id(session)
        if bound is not None and bound != me.id:
            raise AuthProblem(str(AccountMismatchError(bound, me.id)))
        yield client, me, db
    finally:
        await client.disconnect()
        await db.dispose()


async def _login(settings: ArchiverSettings) -> int:
    from tgarchive.archiver.auth import LoginError, interactive_login
    from tgarchive.archiver.client import create_client, secure_session_files

    client = create_client(settings)
    db = _open_db(settings)
    try:
        try:
            result = await interactive_login(client, ConsolePrompter())
        except LoginError as exc:
            print(f"Kirish amalga oshmadi: {exc}", file=sys.stderr)
            return EXIT_AUTH
        try:
            async with db.write() as session:
                newly_bound = await bind_account(session, result.user.id)
                await add_audit(
                    session,
                    "login",
                    target=f"account:{result.user.id}",
                    details={"new_session": result.new_session, "new_binding": newly_bound},
                )
        except AccountMismatchError as exc:
            print(exc, file=sys.stderr)
            if result.new_session:
                await client.log_out()
                print("Shu buyruqda yaratilgan session bekor qilindi.", file=sys.stderr)
            return EXIT_AUTH
        secure_session_files(settings.session_path)
        state = "Yangi session yaratildi" if result.new_session else "Session allaqachon faol"
        print(f"{state}: {_describe(result.user)}")
        print(f"Session fayli: {settings.session_path}")
        print("Telegram -> Settings -> Devices ro'yxatida 'tgarchive' qurilmasi ko'rinadi.")
        print("Session fayli akkauntga to'liq kirish beradi: uni hech kimga bermang, bot orqali ham.")
        return EXIT_OK
    finally:
        await client.disconnect()
        await db.dispose()


def _cmd_login(args: argparse.Namespace) -> int:
    settings = _load_archiver(args)
    _prepare_database(settings)
    return asyncio.run(_login(settings))


async def _whoami(settings: ArchiverSettings) -> int:
    async with _authorized(settings) as (_client, me, db):
        print(f"Akkaunt: {_describe(me)}")
        async with db.read() as session:
            bound = await bound_account_id(session)
        binding = "shu akkauntga bog'langan" if bound == me.id else "hali bog'lanmagan (tgarchive login)"
        print(f"Baza:    {binding}")
    return EXIT_OK


def _cmd_whoami(args: argparse.Namespace) -> int:
    settings = _load_archiver(args)
    _prepare_database(settings)
    return asyncio.run(_whoami(settings))


async def _dialogs(settings: ArchiverSettings, *, save: bool, limit: int | None) -> int:
    from tgarchive.archiver.dialogs import fetch_dialogs, format_plan, plan_dialogs

    async with _authorized(settings) as (client, _me, db):
        dialogs = await fetch_dialogs(client, limit=limit)
        planned = await plan_dialogs(db, settings, dialogs, save=save)
    print(format_plan(planned))
    print("Bazaga yozildi." if save else "Faqat ko'rish rejimi. Bazaga yozish uchun: tgarchive dialogs --save")
    return EXIT_OK


def _cmd_dialogs(args: argparse.Namespace) -> int:
    settings = _load_archiver(args)
    _prepare_database(settings)
    return asyncio.run(_dialogs(settings, save=args.save, limit=args.limit))


async def _logout(settings: ArchiverSettings) -> int:
    from tgarchive.archiver.auth import logout
    from tgarchive.archiver.client import create_client

    if not settings.session_path.exists():
        print("Session yo'q — chiqish shart emas.")
        return EXIT_OK
    client = create_client(settings)
    try:
        done = await logout(client)
    finally:
        await client.disconnect()
    print("Session Telegram serverida tugatildi va fayli o'chirildi." if done else "Session allaqachon yaroqsiz edi.")
    return EXIT_OK


def _cmd_logout(args: argparse.Namespace) -> int:
    settings = _load_archiver(args)
    if not args.yes:
        answer = input("Session serverda tugatiladi va fayl o'chiriladi. Davom etish uchun 'ha' deb yozing: ")
        if answer.strip().lower() != "ha":
            print("Bekor qilindi.")
            return EXIT_OK
    return asyncio.run(_logout(settings))


# ------------------------------------------------------------------- service


async def _run_service(settings: ArchiverSettings, *, once: bool) -> int:
    import signal

    from tgarchive.archiver.service import ArchiverService

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows: Ctrl+C KeyboardInterrupt sifatida keladi
            pass
    code = await ArchiverService(settings, stop=stop).run(once=once)
    if once:
        db = _open_db(settings)
        try:
            async with db.read() as session:
                summary = await import_summary(session)
                heartbeat = await get_heartbeat(session, ARCHIVER)
            print(format_summary(summary, heartbeat, utcnow()))
        finally:
            await db.dispose()
    return code


def _service_settings(args: argparse.Namespace) -> ArchiverSettings:
    settings = load_settings(ArchiverSettings, args.env_file)
    setup_logging(settings.log_level, settings.log_format, secrets=settings.secret_values())
    _prepare_database(settings)
    return settings


def _cmd_run(args: argparse.Namespace) -> int:
    return asyncio.run(_run_service(_service_settings(args), once=False))


def _cmd_bot(args: argparse.Namespace) -> int:
    from aiogram.exceptions import TelegramUnauthorizedError

    from tgarchive.bot.app import run_bot

    settings = load_settings(BotSettings, args.env_file)
    setup_logging(settings.log_level, settings.log_format, secrets=settings.secret_values())
    try:
        asyncio.run(run_bot(settings))
    except TelegramUnauthorizedError:
        print("BOT_TOKEN Telegram tomonidan rad etildi — @BotFather dagi tokenni tekshiring.", file=sys.stderr)
        return EXIT_CONFIG
    return EXIT_OK


def _cmd_import(args: argparse.Namespace) -> int:
    return asyncio.run(_run_service(_service_settings(args), once=True))


def _media_limit_mb(env_file: str) -> int | None:
    """Hajm chegarasi faqat ko'rsatish uchun; archiver sozlamalari bo'lmasa — None."""
    try:
        return load_settings(ArchiverSettings, env_file).media_max_file_mb
    except ConfigError:
        return None


def _disk_line(path: Path) -> str | None:
    import shutil

    from tgarchive.db.media import format_size

    if not path.exists():
        return None
    usage = shutil.disk_usage(path)
    return f"Disk (MEDIA_DIR): bo'sh {format_size(usage.free)} / {format_size(usage.total)}"


async def _status(settings: BaseAppSettings, limit_mb: int | None = None) -> int:
    from tgarchive.db.media import format_media_summary, media_summary

    db = _open_db(settings)
    try:
        async with db.read() as session:
            summary = await import_summary(session)
            heartbeat = await get_heartbeat(session, ARCHIVER)
            media = await media_summary(session)
    finally:
        await db.dispose()
    print(format_summary(summary, heartbeat, utcnow()))
    print(format_media_summary(media, limit_mb))
    disk = _disk_line(settings.paths.media)
    if disk:
        print(disk)
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    settings = load_settings(BaseAppSettings, args.env_file)
    if not settings.paths.db.exists():
        print("Baza hali yaratilmagan: tgarchive db-upgrade")
        return EXIT_RUNTIME
    return asyncio.run(_status(settings, _media_limit_mb(args.env_file)))


# --------------------------------------------------------------------- media


async def _media(args: argparse.Namespace, settings: BaseAppSettings) -> int:
    from tgarchive.db.media import (
        FetchRefused,
        format_large_files,
        format_media_summary,
        list_large_files,
        media_summary,
        request_fetch,
    )

    db = _open_db(settings)
    try:
        if args.media_command == "stats":
            async with db.read() as session:
                print(format_media_summary(await media_summary(session), _media_limit_mb(args.env_file)))
            disk = _disk_line(settings.paths.media)
            if disk:
                print(disk)
        elif args.media_command == "large":
            async with db.read() as session:
                files = await list_large_files(session, limit=args.limit, offset=args.offset)
            print(format_large_files(files))
            if files:
                print("Bittasini yuklash: tgarchive media fetch <ID>")
        elif args.media_command == "fetch":
            try:
                async with db.write() as session:
                    media = await request_fetch(session, args.media_id)
                    await add_audit(session, "media_fetch_requested", actor="owner", target=f"media:{media.id}")
            except FetchRefused as exc:
                print(exc)
                return EXIT_RUNTIME
            print(f"Media {args.media_id} navbatga qo'yildi — archiver uni yaqin daqiqalarda yuklaydi.")
        elif args.media_command == "verify":
            from tgarchive.archiver.media import MediaWorker

            archiver = load_settings(ArchiverSettings, args.env_file)
            report = await MediaWorker(None, db, archiver).verify(checksum=args.checksum)
            print(
                f"Tekshirildi: {report.checked}, diskda yo'q: {report.missing}, buzilgan: {report.mismatched}"
                + (" (qayta yuklash uchun navbatga qo'yildi)" if report.missing or report.mismatched else "")
            )
    finally:
        await db.dispose()
    return EXIT_OK


def _cmd_media(args: argparse.Namespace) -> int:
    settings = load_settings(BaseAppSettings, args.env_file)
    if not settings.paths.db.exists():
        print("Baza hali yaratilmagan: tgarchive db-upgrade")
        return EXIT_RUNTIME
    return asyncio.run(_media(args, settings))


async def _health(settings: BaseAppSettings, component: str, max_age_s: int) -> int:
    db = _open_db(settings)
    try:
        async with db.read() as session:
            heartbeat = await get_heartbeat(session, component)
    finally:
        await db.dispose()
    if heartbeat is None:
        print(f"{component}: heartbeat yo'q")
        return EXIT_RUNTIME
    age = (utcnow() - heartbeat.last_beat_at).total_seconds()
    healthy = heartbeat.state in HEALTHY_STATES and age <= max_age_s
    print(f"{component}: {heartbeat.state}, {int(age)} s oldin — {'OK' if healthy else 'NOSOZ'}")
    return EXIT_OK if healthy else EXIT_RUNTIME


def _cmd_health(args: argparse.Namespace) -> int:
    settings = load_settings(BaseAppSettings, args.env_file)
    if not settings.paths.db.exists():
        print("baza yo'q")
        return EXIT_RUNTIME
    return asyncio.run(_health(settings, args.component, args.max_age))


# ----------------------------------------------------------------------- main


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tgarchive", description="Telegram shaxsiy arxiv tizimi")
    parser.add_argument("--env-file", default=".env", help="sozlamalar fayli (sukut: .env)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-config", help="konfiguratsiya va ruxsatlarni tekshirish")
    check.add_argument("--component", choices=("archiver", "bot", "all"), default="all")
    check.set_defaults(handler=_cmd_check_config)

    sub.add_parser("db-upgrade", help="migratsiyalarni qo'llash").set_defaults(handler=_cmd_db_upgrade)
    sub.add_parser("db-info", help="baza holati va statistikasi").set_defaults(handler=_cmd_db_info)

    sub.add_parser("login", help="asosiy akkauntga kirish (bir marta, interaktiv)").set_defaults(handler=_cmd_login)
    sub.add_parser("whoami", help="session qaysi akkauntga tegishli").set_defaults(handler=_cmd_whoami)

    dialogs = sub.add_parser("dialogs", help="dialoglar va ularning arxivlash siyosati")
    dialogs.add_argument("--save", action="store_true", help="chatlarni bazaga yozish")
    dialogs.add_argument("--limit", type=int, default=None, help="ko'pi bilan N ta dialog")
    dialogs.set_defaults(handler=_cmd_dialogs)

    logout = sub.add_parser("logout", help="session'ni Telegram'da tugatish va faylini o'chirish")
    logout.add_argument("--yes", action="store_true", help="tasdiqlash so'ralmasin")
    logout.set_defaults(handler=_cmd_logout)

    sub.add_parser("run", help="archiver xizmati (doimiy, 24/7)").set_defaults(handler=_cmd_run)
    sub.add_parser("bot", help="admin bot (doimiy)").set_defaults(handler=_cmd_bot)
    sub.add_parser("import", help="tarixni bir marta import qilib chiqish").set_defaults(handler=_cmd_import)
    sub.add_parser("status", help="import statistikasi va archiver holati").set_defaults(handler=_cmd_status)

    health = sub.add_parser("health", help="healthcheck: heartbeat yangi bo'lsa 0 qaytaradi")
    health.add_argument("--component", default=ARCHIVER)
    health.add_argument("--max-age", type=int, default=180, help="heartbeat necha sekunddan eski bo'lmasin")
    health.set_defaults(handler=_cmd_health)

    media = sub.add_parser("media", help="media arxivi: statistika, katta fayllar, tekshiruv")
    media_sub = media.add_subparsers(dest="media_command", required=True)
    media_sub.add_parser("stats", help="yuklangan, navbatdagi va katta fayllar soni, disk")
    large = media_sub.add_parser("large", help="hajm chegarasidan katta, yuklanmagan fayllar ro'yxati")
    large.add_argument("--limit", type=int, default=20)
    large.add_argument("--offset", type=int, default=0)
    fetch = media_sub.add_parser("fetch", help="bitta faylni chegaraga qaramay yuklash")
    fetch.add_argument("media_id", type=int, help="tgarchive media large ro'yxatidagi ID")
    verify = media_sub.add_parser("verify", help="yuklangan fayllar diskda bormi")
    verify.add_argument("--checksum", action="store_true", help="sha256 bilan to'liq tekshirish (sekin)")
    media.set_defaults(handler=_cmd_media)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure: Callable[..., None] | None = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
    args = _build_parser().parse_args(argv)
    set_private_umask()
    try:
        return args.handler(args)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return EXIT_CONFIG
    except AuthProblem as exc:
        print(exc, file=sys.stderr)
        return EXIT_AUTH
    except KeyboardInterrupt:
        print("\nTo'xtatildi.", file=sys.stderr)
        return EXIT_RUNTIME
