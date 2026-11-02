"""Buyruqlar va inline tugmalar. Mantiq ``views.py`` da — bu yer faqat yuborish.

Barcha javoblar egasining chatiga (``OWNER_ID``) yuboriladi; middleware boshqa chatlarni
bu yerga umuman yetkazmaydi.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    FSInputFile,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from tgarchive.bot.callbacks import (
    Album,
    ChatMessages,
    ChatsPage,
    Conversation,
    FetchFile,
    FullText,
    LargePage,
    MediaPage,
    Menu,
    OpenChat,
    OpenMessage,
    ReadAround,
    SearchPage,
    SendAlbum,
    SendFile,
    Thread,
    Versions,
)
from tgarchive.bot.format import esc
from tgarchive.bot.views import BOT_UPLOAD_LIMIT, EXPORT_HELP, FileToSend, View, Views
from tgarchive.db.media import format_size
from tgarchive.db.enums import MediaType
from tgarchive.db.search import parse_search
from tgarchive.export import export_messages, parse_export_args
from tgarchive.logging_setup import get_logger

log = get_logger(__name__)

COMMANDS = [
    BotCommand(command="search", description="Matn va filtrlar bo'yicha qidirish"),
    BotCommand(command="recent", description="So'nggi xabarlar"),
    BotCommand(command="chats", description="Arxivdagi chatlar"),
    BotCommand(command="deleted", description="O'chirilgan xabarlar"),
    BotCommand(command="media", description="Media va katta fayllar"),
    BotCommand(command="stats", description="Arxiv statistikasi"),
    BotCommand(command="digest", description="Oxirgi 7 kun hisoboti"),
    BotCommand(command="status", description="Archiver, zaxira va server holati"),
    BotCommand(command="sessions", description="Akkaunt qurilmalari va online holati"),
    BotCommand(command="export", description="Eksport: chat yoki sana oralig'i"),
    BotCommand(command="audit", description="Audit jurnali"),
    BotCommand(command="help", description="Yordam va qidiruv sintaksisi"),
]

_SENDERS = {
    MediaType.PHOTO: ("send_photo", "photo"),
    MediaType.VIDEO: ("send_video", "video"),
    MediaType.VOICE: ("send_voice", "voice"),
    MediaType.AUDIO: ("send_audio", "audio"),
    MediaType.ANIMATION: ("send_animation", "animation"),
    MediaType.VIDEO_NOTE: ("send_video_note", "video_note"),
}


async def set_owner_commands(bot: Bot, owner_id: int) -> None:
    """Buyruqlar menyusi faqat egasining chatida ko'rinadi (begonalarga ro'yxat ko'rsatilmaydi)."""
    try:
        await bot.set_my_commands(COMMANDS, scope=BotCommandScopeChat(chat_id=owner_id))
    except TelegramAPIError as exc:
        log.warning("bot_commands_not_set", error=str(exc)[:200], hint="egasi botga avval /start yuborsin")


def build_router(views: Views) -> Router:
    router = Router(name="tgarchive")
    owner = views.settings.owner_id
    export_lock = asyncio.Lock()
    pending_fetches: set[int] = set()  # fonda kutilayotgan fayllar (bir faylga bitta kutuvchi)

    async def send(bot: Bot, view: View) -> None:
        await bot.send_message(owner, view.text, reply_markup=view.keyboard)

    async def edit(bot: Bot, callback: CallbackQuery, view: View) -> None:
        """Sahifalash: shu xabarni almashtirish; iloji bo'lmasa — yangi xabar."""
        if callback.message is not None:
            try:
                await bot.edit_message_text(
                    view.text, chat_id=owner, message_id=callback.message.message_id, reply_markup=view.keyboard
                )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc):
                    return
        await send(bot, view)

    async def search_text(bot: Bot, raw: str) -> None:
        query, errors = parse_search(raw, views.tz)
        if errors:
            await bot.send_message(owner, "Qidiruvda xato:\n" + "\n".join(f"• {e}" for e in errors) + "\n\n/help")
            return
        await send(bot, await views.search(query))

    # ------------------------------------------------------------- buyruqlar

    @router.message(CommandStart())
    async def on_start(message: Message, bot: Bot) -> None:
        await set_owner_commands(bot, owner)
        await send(bot, views.menu())

    @router.message(Command("help"))
    async def on_help(message: Message, bot: Bot) -> None:
        await send(bot, views.help())

    @router.message(Command("search"))
    async def on_search(message: Message, bot: Bot, command: CommandObject) -> None:
        if not command.args:
            await bot.send_message(owner, "Foydalanish: <code>/search matn [filtrlar]</code>\nFiltrlar: /help")
            return
        await search_text(bot, command.args)

    @router.message(Command("recent"))
    async def on_recent(message: Message, bot: Bot) -> None:
        await send(bot, await views.recent())

    @router.message(Command("chats"))
    async def on_chats(message: Message, bot: Bot) -> None:
        await send(bot, await views.chats())

    @router.message(Command("deleted"))
    async def on_deleted(message: Message, bot: Bot) -> None:
        await send(bot, await views.deleted())

    @router.message(Command("media"))
    async def on_media(message: Message, bot: Bot, command: CommandObject) -> None:
        if command.args:
            await send(bot, await views.media_by_name(command.args.strip()))
        else:
            await send(bot, await views.media_menu())

    @router.message(Command("stats"))
    async def on_stats(message: Message, bot: Bot) -> None:
        await send(bot, await views.stats())

    @router.message(Command("digest"))
    async def on_digest(message: Message, bot: Bot, command: CommandObject) -> None:
        days = 7
        if command.args and command.args.strip().isdigit():
            days = max(1, min(90, int(command.args.strip())))
        await send(bot, await views.digest_report(days))

    @router.message(Command("status"))
    async def on_status(message: Message, bot: Bot) -> None:
        await send(bot, await views.status())

    @router.message(Command("sessions"))
    async def on_sessions(message: Message, bot: Bot) -> None:
        await send(bot, await views.sessions())

    @router.message(Command("export"))
    async def on_export(message: Message, bot: Bot, command: CommandObject) -> None:
        if not command.args:
            await bot.send_message(owner, EXPORT_HELP)
            return
        request, errors = parse_export_args(command.args, views.tz)
        if request is None:
            await bot.send_message(owner, "Eksportda xato:\n" + "\n".join(f"• {esc(e)}" for e in errors))
            return
        if export_lock.locked():
            await bot.send_message(owner, "Boshqa eksport tayyorlanmoqda — tugashini kuting.")
            return
        async with export_lock:
            await bot.send_message(owner, f"⏳ Eksport tayyorlanmoqda: {esc(request.query.describe())}…")
            result = await export_messages(views.db, views.settings, request, actor="owner", actor_id=owner)
        caption = (
            f"📦 {result.messages} xabar, {result.chats} chat, fayllar {result.media_included}"
            f" · {format_size(result.size)}"
        )
        if result.messages == 0:
            await bot.send_message(owner, "Filtrga mos xabar yo'q — eksport bo'sh.")
        elif result.size <= BOT_UPLOAD_LIMIT:
            await bot.send_document(owner, FSInputFile(result.path), caption=caption)
        else:
            await bot.send_message(
                owner,
                f"{caption}\nFayl 50 MB dan katta — bot yubora olmaydi. Serverda: <code>{esc(result.path.name)}</code>\n"
                f"Olish: <code>docker compose cp bot:/data/exports/{esc(result.path.name)} .</code>",
            )

    @router.message(Command("audit"))
    async def on_audit(message: Message, bot: Bot) -> None:
        await send(bot, await views.audit())

    @router.message(F.text & ~F.text.startswith("/"))
    async def on_text(message: Message, bot: Bot) -> None:
        await search_text(bot, message.text or "")

    @router.message()
    async def on_other(message: Message, bot: Bot) -> None:
        await bot.send_message(owner, "Qidirish uchun matn yuboring yoki /help.")

    # --------------------------------------------------------------- tugmalar

    @router.callback_query(Menu.filter())
    async def on_menu(callback: CallbackQuery, callback_data: Menu, bot: Bot) -> None:
        actions = {
            "recent": views.recent,
            "chats": views.chats,
            "deleted": views.deleted,
            "media": views.media_menu,
            "stats": views.stats,
            "status": views.status,
            "sessions": views.sessions,
        }
        if callback_data.action == "help":
            await send(bot, views.help())
        elif callback_data.action in actions:
            await send(bot, await actions[callback_data.action]())
        await callback.answer()

    @router.callback_query(SearchPage.filter())
    async def on_search_page(callback: CallbackQuery, callback_data: SearchPage, bot: Bot) -> None:
        view = await views.search_page(callback_data.token, callback_data.offset)
        if view is None:
            await callback.answer("Bu qidiruv eskirgan (bot qayta ishga tushgan). Qidiruvni qayta yuboring.", show_alert=True)
            return
        await edit(bot, callback, view)
        await callback.answer()

    @router.callback_query(OpenMessage.filter())
    async def on_open_message(callback: CallbackQuery, callback_data: OpenMessage, bot: Bot) -> None:
        await send(bot, await views.message(callback_data.pk))
        await callback.answer()

    @router.callback_query(FullText.filter())
    async def on_full_text(callback: CallbackQuery, callback_data: FullText, bot: Bot) -> None:
        for chunk in await views.full_text(callback_data.pk):
            await bot.send_message(owner, chunk, parse_mode=None)  # xom matn, HTML emas
        await callback.answer()

    @router.callback_query(Versions.filter())
    async def on_versions(callback: CallbackQuery, callback_data: Versions, bot: Bot) -> None:
        await send(bot, await views.versions(callback_data.pk))
        await callback.answer()

    @router.callback_query(Thread.filter())
    async def on_thread(callback: CallbackQuery, callback_data: Thread, bot: Bot) -> None:
        await send(bot, await views.thread(callback_data.pk))
        await callback.answer()

    @router.callback_query(ReadAround.filter())
    async def on_read_around(callback: CallbackQuery, callback_data: ReadAround, bot: Bot) -> None:
        await send(bot, await views.read_around(callback_data.pk))
        await callback.answer()

    @router.callback_query(Album.filter())
    async def on_album(callback: CallbackQuery, callback_data: Album, bot: Bot) -> None:
        await send(bot, await views.album(callback_data.pk))
        await callback.answer()

    @router.callback_query(SendAlbum.filter())
    async def on_send_album(callback: CallbackQuery, callback_data: SendAlbum, bot: Bot) -> None:
        result = await views.album_files(callback_data.pk)
        if isinstance(result, str):
            await bot.send_message(owner, result)
            await callback.answer()
            return
        await callback.answer("Yuborilyapti…")
        await _send_album(bot, owner, result)

    @router.callback_query(SendFile.filter())
    async def on_send_file(callback: CallbackQuery, callback_data: SendFile, bot: Bot) -> None:
        result = await views.file_to_send(callback_data.media_id)
        if isinstance(result, str):
            await bot.send_message(owner, result)
        else:
            await callback.answer("Yuborilyapti…")
            await _send_file(bot, owner, result)
            return
        await callback.answer()

    @router.callback_query(FetchFile.filter())
    async def on_fetch(callback: CallbackQuery, callback_data: FetchFile, bot: Bot) -> None:
        media_id = callback_data.media_id
        await callback.answer("📥 So'rov yuborildi…")
        if media_id in pending_fetches:
            await bot.send_message(owner, "⏳ Bu fayl allaqachon navbatda — tayyor bo'lsa shu yerga o'zim yuboraman.")
            return
        started = await views.start_fetch(media_id)
        if isinstance(started, FileToSend):
            await _send_file(bot, owner, started)
            return
        if isinstance(started, str):
            await bot.send_message(owner, started)
            return
        # So'rov qabul qilindi, fayl hali tayyor emas. Avval qisqa kutamiz (tez javob uchun):
        quick = await views.poll_file(media_id, views.fetch_attempts, views.fetch_interval)
        if isinstance(quick, FileToSend):
            await _send_file(bot, owner, quick)
            return
        if isinstance(quick, str):
            await bot.send_message(owner, quick)
            return
        # Hali tayyor emas — fonda kutamiz. aiogram handler'ni alohida task'da ishlatadi,
        # shuning uchun bu boshqa yangilanishlarni to'smaydi. Tayyor bo'lgach o'zimiz yuboramiz.
        pending_fetches.add(media_id)
        try:
            await bot.send_message(owner, "⏳ Fayl yuklanmoqda. Tayyor bo'lgach, shu yerga o'zim yuboraman.")
            result = await views.poll_file(media_id, views.fetch_bg_attempts, views.fetch_bg_interval)
            if isinstance(result, FileToSend):
                await bot.send_message(owner, "✅ Fayl tayyor:")
                await _send_file(bot, owner, result)
            elif isinstance(result, str):
                await bot.send_message(owner, result)
            else:
                await bot.send_message(
                    owner, "⌛️ Fayl hali tayyor emas (katta yoki navbat uzun). Keyinroq «📥 Yuklab yuborish» ni qayta bosing."
                )
        finally:
            pending_fetches.discard(media_id)

    @router.callback_query(ChatsPage.filter())
    async def on_chats_page(callback: CallbackQuery, callback_data: ChatsPage, bot: Bot) -> None:
        await edit(bot, callback, await views.chats(callback_data.offset))
        await callback.answer()

    @router.callback_query(OpenChat.filter())
    async def on_open_chat(callback: CallbackQuery, callback_data: OpenChat, bot: Bot) -> None:
        await edit(bot, callback, await views.chat(callback_data.chat_id))
        await callback.answer()

    @router.callback_query(Conversation.filter())
    async def on_conversation(callback: CallbackQuery, callback_data: Conversation, bot: Bot) -> None:
        # Chatga kirish va varaqlash — shu xabarni almashtirib boradi (Telegram'dagidek bitta ekran)
        await edit(bot, callback, await views.conversation(callback_data.chat_id, callback_data.offset))
        await callback.answer()

    @router.callback_query(ChatMessages.filter())
    async def on_chat_messages(callback: CallbackQuery, callback_data: ChatMessages, bot: Bot) -> None:
        await send(bot, await views.chat_messages(callback_data.chat_id, deleted=callback_data.deleted))
        await callback.answer()

    @router.callback_query(LargePage.filter())
    async def on_large(callback: CallbackQuery, callback_data: LargePage, bot: Bot) -> None:
        view = await views.large_files(callback_data.offset)
        if callback_data.offset:
            await edit(bot, callback, view)
        else:
            await send(bot, view)
        await callback.answer()

    @router.callback_query(MediaPage.filter())
    async def on_media_page(callback: CallbackQuery, callback_data: MediaPage, bot: Bot) -> None:
        view = await views.media_browse(
            chat_id=callback_data.chat_id, kind=callback_data.kind, offset=callback_data.offset
        )
        await edit(bot, callback, view)
        await callback.answer()

    @router.callback_query()
    async def on_unknown_callback(callback: CallbackQuery) -> None:
        await callback.answer("Bu tugma eskirgan.")

    return router


_ALBUM_INPUT = {
    MediaType.PHOTO: InputMediaPhoto,
    MediaType.VIDEO: InputMediaVideo,
    MediaType.DOCUMENT: InputMediaDocument,
    MediaType.AUDIO: InputMediaAudio,
}
# Bitta albomda birga ketadigan turlar (Telegram sendMediaGroup qoidasi)
_ALBUM_GROUPS = ((MediaType.PHOTO, MediaType.VIDEO), (MediaType.DOCUMENT,), (MediaType.AUDIO,))


async def _send_album(bot: Bot, owner: int, items: list[FileToSend]) -> None:
    """Albomni Telegram media-guruhi sifatida yuboradi; mos kelmaydigan turlar alohida.

    Telegram albomi 2–10 ta rasm/video (yoki alohida hujjat / audio guruhi) bo'lishi mumkin;
    animatsiya, ovozli, video xabar, stiker albomga sig'maydi — alohida yuboriladi.
    """
    caption = next((it.caption for it in items if it.caption), None)
    if caption:
        await bot.send_message(owner, caption)
    leftover = list(items)
    for kinds in _ALBUM_GROUPS:
        group = [it for it in leftover if it.media_type in kinds]
        leftover = [it for it in leftover if it.media_type not in kinds]
        for start in range(0, len(group), 10):
            chunk = group[start : start + 10]
            if len(chunk) < 2:  # albom kamida 2 ta; bittasini oddiy yuboramiz
                leftover.extend(chunk)
                continue
            media = [
                _ALBUM_INPUT[it.media_type](media=FSInputFile(it.path, filename=it.file_name)) for it in chunk
            ]
            try:
                await bot.send_media_group(owner, media=media)
            except TelegramBadRequest as exc:
                log.info("album_group_failed", reason=str(exc)[:200])
                leftover.extend(chunk)
    for it in leftover:  # qolganlari (bittalik yoki albomga sig'maydigan turlar)
        await _send_file(bot, owner, FileToSend(it.path, it.media_type, "", it.file_name))


async def _send_file(bot: Bot, owner: int, item: FileToSend) -> None:
    file = FSInputFile(item.path, filename=item.file_name)
    method_name, field = _SENDERS.get(item.media_type, ("send_document", "document"))
    kwargs: dict[str, object] = {field: file}
    if item.media_type is not MediaType.VIDEO_NOTE and item.caption:
        kwargs["caption"] = item.caption
    try:
        await getattr(bot, method_name)(owner, **kwargs)
    except TelegramBadRequest as exc:
        # Masalan, ovozli xabar formati mos kelmasa — hujjat sifatida yuboriladi
        log.info("bot_send_as_document", reason=str(exc)[:200])
        await bot.send_document(
            owner, FSInputFile(item.path, filename=item.file_name), caption=item.caption or None
        )
