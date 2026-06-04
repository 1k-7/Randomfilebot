from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import replace
from datetime import timedelta
from html import escape

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import RPCError
from pyrogram.file_id import FileId, FileType
from pyrogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    InlineQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultCachedVideo,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from .config import Config, load_config
from .db import Database, utcnow
from .models import ForceSubChat, IndexedFile, UserStats


REFRESH_CALLBACK = "refresh_file"
FSUB_MEMBER_MODE = "member"
FSUB_REQUEST_MODE = "request"
SUPPORTED_FILE_TYPES = {"document", "photo", "video", "audio", "animation"}
PYROGRAM_FILE_TYPE_MAP = {
    FileType.PHOTO: "photo",
    FileType.VIDEO: "video",
    FileType.AUDIO: "audio",
    FileType.ANIMATION: "animation",
    FileType.DOCUMENT: "document",
    FileType.DOCUMENT_AS_FILE: "document",
}
MEMBER_STATUSES = {"owner", "creator", "administrator", "member"}


class BotRuntime:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.delete_tasks: dict[tuple[int, int], asyncio.Task] = {}


def sleek_title(user_name: str | None) -> str:
    name = escape(user_name or "there")
    return (
        f"<b>Welcome, {name}.</b>\n\n"
        "I pick one file at random from the vault. Tap refresh whenever you want another draw."
    )


def refresh_keyboard(file_db_id: int | None = None) -> InlineKeyboardMarkup:
    callback_data = REFRESH_CALLBACK if file_db_id is None else f"{REFRESH_CALLBACK}:{file_db_id}"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Refresh file", callback_data=callback_data)]]
    )


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Draw a file", callback_data=REFRESH_CALLBACK)]]
    )


def force_sub_keyboard(chats: list[ForceSubChat]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for chat in chats:
        title = chat.title or str(chat.chat_id)
        if chat.invite_link:
            buttons.append([InlineKeyboardButton(short_text(title, 48), url=chat.invite_link)])
    buttons.append([InlineKeyboardButton("I joined", callback_data=REFRESH_CALLBACK)])
    return InlineKeyboardMarkup(buttons)


def compact_file_id(file_id: str) -> str:
    return file_id if len(file_id) <= 18 else f"{file_id[:8]}...{file_id[-6:]}"


def short_text(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def normalize_invite_link(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith(("https://t.me/", "http://t.me/", "https://telegram.me/")):
        return value
    if value.startswith("t.me/"):
        return f"https://{value}"
    return None


def chat_ref(value: str):
    return int(value) if value.lstrip("-").isdigit() else value


def status_value(status) -> str:
    return str(getattr(status, "value", status)).lower()


def chat_type_value(chat) -> str:
    return status_value(getattr(chat, "type", ""))


def command_args(message: Message) -> list[str]:
    if message.command:
        return message.command[1:]
    text = message.text or message.caption or ""
    return text.split()[1:]


def caption_command_is(message: Message, command: str) -> bool:
    text = (message.caption or "").strip()
    if not text.startswith("/"):
        return False
    token = text.split(maxsplit=1)[0][1:]
    name = token.split("@", 1)[0].lower()
    return name == command.lower()


def user_label(stats: UserStats) -> str:
    if stats.username:
        return f"@{escape(stats.username)}"
    full_name = " ".join(part for part in [stats.first_name, stats.last_name] if part)
    return escape(full_name) if full_name else str(stats.user_id)


def is_block_error(error: RPCError) -> bool:
    name = error.__class__.__name__.lower()
    text = str(error).upper()
    return (
        "blocked" in name
        or "peeridinvalid" in name
        or "USER_IS_BLOCKED" in text
        or "PEER_ID_INVALID" in text
    )


def detect_file_type(file_id: str, declared_type: str | None = None) -> str:
    try:
        decoded = FileId.decode(file_id)
        mapped = PYROGRAM_FILE_TYPE_MAP.get(decoded.file_type)
        if mapped:
            return mapped
    except Exception:
        pass
    if declared_type and declared_type.lower() in SUPPORTED_FILE_TYPES:
        return declared_type.lower()
    return "document"


def file_type_from_mismatch(error: Exception) -> str | None:
    match = re.search(r"got\s+([A-Z_]+)\s+file id", str(error), flags=re.IGNORECASE)
    if not match:
        return None
    value = match.group(1).lower()
    if value == "document_as_file":
        return "document"
    return value if value in SUPPORTED_FILE_TYPES else None


def correct_file_type(rt: BotRuntime, item: IndexedFile, file_type: str) -> IndexedFile:
    if file_type == item.file_type:
        return item
    rt.db.update_file_type(item.id, file_type)
    return replace(item, file_type=file_type)


def normalize_indexed_type(rt: BotRuntime, item: IndexedFile) -> IndexedFile:
    detected = detect_file_type(item.file_id, item.file_type)
    return correct_file_type(rt, item, detected)


def find_random_video(rt: BotRuntime) -> IndexedFile | None:
    for _ in range(5):
        item = rt.db.random_file(file_type="video")
        if not item:
            break
        item = normalize_indexed_type(rt, item)
        if item.file_type == "video":
            return item

    for item in rt.db.random_files(100):
        item = normalize_indexed_type(rt, item)
        if item.file_type == "video":
            return item
    return None


async def require_sudo(rt: BotRuntime, message: Message) -> bool:
    user = message.from_user
    if not user:
        await message.reply_text("I can only accept admin commands from a visible user account.")
        return False
    if is_sudo_user(rt, user.id):
        return True
    await message.reply_text("This panel is only for the owner and sudo users.")
    return False


def is_sudo_user(rt: BotRuntime, user_id: int) -> bool:
    return user_id == rt.config.owner_id or rt.db.is_sudo(user_id)


async def track_user(rt: BotRuntime, user) -> None:
    if user:
        rt.db.upsert_user(user, blocked=False)


async def missing_force_sub_chats(
    client: Client,
    rt: BotRuntime,
    user_id: int,
) -> list[ForceSubChat]:
    missing: list[ForceSubChat] = []
    for chat in rt.db.list_force_sub_chats():
        if await force_sub_chat_fulfilled(client, rt, chat, user_id):
            continue
        missing.append(chat)
    return missing


async def force_sub_chat_fulfilled(
    client: Client,
    rt: BotRuntime,
    chat: ForceSubChat,
    user_id: int,
) -> bool:
    try:
        member = await client.get_chat_member(chat_ref(chat.chat_id), user_id)
        status = status_value(member.status)
        if status in MEMBER_STATUSES:
            return True
        if status == "restricted" and getattr(member, "is_member", False):
            return True
    except RPCError:
        pass
    return chat.mode == FSUB_REQUEST_MODE and rt.db.has_join_request(user_id, chat.chat_id)


async def enforce_force_sub_message(client: Client, rt: BotRuntime, message: Message) -> bool:
    user = message.from_user
    if not user:
        return False
    missing = await missing_force_sub_chats(client, rt, user.id)
    if not missing:
        return True
    rt.db.record_denied(user.id, "denied_fsub")
    await message.reply_text(
        force_sub_text(missing),
        parse_mode=ParseMode.HTML,
        reply_markup=force_sub_keyboard(missing),
        disable_web_page_preview=True,
    )
    return False


async def enforce_force_sub_query(client: Client, rt: BotRuntime, query: CallbackQuery) -> bool:
    user = query.from_user
    if not user:
        return False
    missing = await missing_force_sub_chats(client, rt, user.id)
    if not missing:
        return True
    rt.db.record_denied(user.id, "denied_fsub")
    await query.answer("Join the required chat first.", show_alert=True)
    await edit_query_text_or_caption(query, force_sub_text(missing), force_sub_keyboard(missing))
    return False


def force_sub_text(missing: list[ForceSubChat]) -> str:
    request_note = any(chat.mode == FSUB_REQUEST_MODE for chat in missing)
    text = (
        "<b>One step before the vault opens.</b>\n\n"
        "Use the required chat buttons below, then tap <b>I joined</b> to continue."
    )
    if request_note:
        text += "\n\nFor request-only chats, sending the join request is enough."
    return text


async def enforce_rate_limit_message(rt: BotRuntime, message: Message) -> bool:
    user = message.from_user
    if not user:
        return False
    if is_under_rate_limit(rt, user.id):
        return True
    rt.db.record_denied(user.id, "denied_rate_limit")
    await message.reply_text(rate_limit_text(rt), parse_mode=ParseMode.HTML)
    return False


async def enforce_rate_limit_query(rt: BotRuntime, query: CallbackQuery) -> bool:
    user = query.from_user
    if not user:
        return False
    if is_under_rate_limit(rt, user.id):
        return True
    rt.db.record_denied(user.id, "denied_rate_limit")
    await query.answer("Rate limit reached. Try again later.", show_alert=True)
    return False


async def enforce_force_sub_inline(client: Client, rt: BotRuntime, query: InlineQuery) -> bool:
    user = query.from_user
    if not user:
        return False
    missing = await missing_force_sub_chats(client, rt, user.id)
    if not missing:
        return True
    rt.db.record_denied(user.id, "denied_fsub_inline")
    await query.answer(
        [],
        cache_time=0,
        is_personal=True,
        switch_pm_text="Join required chats first",
        switch_pm_parameter="fsub",
    )
    return False


async def enforce_rate_limit_inline(rt: BotRuntime, query: InlineQuery) -> bool:
    user = query.from_user
    if not user:
        return False
    if is_under_rate_limit(rt, user.id):
        return True
    rt.db.record_denied(user.id, "denied_rate_limit_inline")
    await query.answer(
        [],
        cache_time=0,
        is_personal=True,
        switch_pm_text="Rate limit reached",
        switch_pm_parameter="rate_limited",
    )
    return False


def is_under_rate_limit(rt: BotRuntime, user_id: int) -> bool:
    since = utcnow() - timedelta(seconds=rt.config.request_window_seconds)
    used = rt.db.requests_since(user_id, since)
    return used < rt.config.request_limit


def rate_limit_text(rt: BotRuntime) -> str:
    minutes = max(1, rt.config.request_window_seconds // 60)
    return (
        "<b>You have reached the hourly draw limit.</b>\n\n"
        f"You can request {rt.config.request_limit} files every {minutes} minutes. "
        "Try again a little later."
    )


def cancel_delete_task(rt: BotRuntime, chat_id: int, message_id: int) -> None:
    task = rt.delete_tasks.pop((chat_id, message_id), None)
    if task and not task.done():
        task.cancel()


def cancel_all_delete_tasks(rt: BotRuntime) -> None:
    for task in rt.delete_tasks.values():
        if not task.done():
            task.cancel()
    rt.delete_tasks.clear()


async def reset_file_message_to_start(
    client: Client,
    *,
    user_name: str | None,
    chat_id: int,
    message_id: int,
) -> bool:
    try:
        await client.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=sleek_title(user_name),
            parse_mode=ParseMode.HTML,
            reply_markup=start_keyboard(),
            disable_web_page_preview=True,
        )
        # Some Telegram clients may accept the edit but leave media in place
        # (resulting in a caption change instead of conversion to a text message).
        # Detect that case and delete the message instead, matching the intended
        # single-mode behavior described in README.
        try:
            msg = await client.get_messages(chat_id, message_id)
            if msg is not None and any(
                getattr(msg, attr, None)
                for attr in ("photo", "video", "audio", "document", "animation")
            ):
                with suppress(RPCError):
                    await client.delete_messages(chat_id=chat_id, message_ids=message_id)
                return True
        except RPCError:
            # If we can't fetch the message, fall through and treat edit as success.
            pass
        return True
    except RPCError:
        pass

    try:
        await client.delete_messages(chat_id=chat_id, message_ids=message_id)
        return True
    except RPCError:
        return False


async def reset_previous_active_file_message(
    client: Client,
    rt: BotRuntime,
    user,
    *,
    skip: tuple[int, int] | None = None,
) -> None:
    previous = rt.db.get_active_file_message(user.id)
    if not previous or previous == skip:
        return
    chat_id, message_id = previous
    cancel_delete_task(rt, chat_id, message_id)
    await reset_file_message_to_start(
        client,
        user_name=getattr(user, "first_name", None),
        chat_id=chat_id,
        message_id=message_id,
    )
    rt.db.clear_active_file_message(user.id, chat_id, message_id)


async def remember_file_message(
    client: Client,
    rt: BotRuntime,
    user,
    message: Message,
) -> None:
    chat_id = int(message.chat.id)
    message_id = int(message.id)
    rt.db.set_active_file_message(user.id, chat_id, message_id)
    schedule_file_delete_timer(
        client,
        rt,
        user_id=user.id,
        user_name=getattr(user, "first_name", None),
        chat_id=chat_id,
        message_id=message_id,
    )


def schedule_file_delete_timer(
    client: Client,
    rt: BotRuntime,
    *,
    user_id: int,
    user_name: str | None,
    chat_id: int,
    message_id: int,
) -> None:
    seconds = rt.db.delete_timer_seconds()
    cancel_delete_task(rt, chat_id, message_id)
    if seconds <= 0 or is_sudo_user(rt, user_id):
        return
    rt.delete_tasks[(chat_id, message_id)] = asyncio.create_task(
        expire_file_message_after(
            client,
            rt,
            user_id=user_id,
            user_name=user_name,
            chat_id=chat_id,
            message_id=message_id,
            seconds=seconds,
        )
    )


async def expire_file_message_after(
    client: Client,
    rt: BotRuntime,
    *,
    user_id: int,
    user_name: str | None,
    chat_id: int,
    message_id: int,
    seconds: int,
) -> None:
    key = (chat_id, message_id)
    try:
        await asyncio.sleep(seconds)
        if rt.db.delete_timer_seconds() <= 0 or is_sudo_user(rt, user_id):
            return
        if rt.db.single_file_mode_enabled() and rt.db.get_active_file_message(user_id) != key:
            return
        await reset_file_message_to_start(
            client,
            user_name=user_name,
            chat_id=chat_id,
            message_id=message_id,
        )
        rt.db.clear_active_file_message(user_id, chat_id, message_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("File delete timer failed")
    finally:
        if rt.delete_tasks.get(key) is asyncio.current_task():
            rt.delete_tasks.pop(key, None)


async def send_random_file_message(
    client: Client,
    rt: BotRuntime,
    message: Message,
    *,
    event_type: str,
) -> None:
    user = message.from_user
    if not user:
        return
    item = rt.db.random_file()
    if not item:
        await message.reply_text(
            "<b>The vault is empty.</b>\n\nAn admin needs to index at least one file first.",
            parse_mode=ParseMode.HTML,
        )
        return
    if rt.db.single_file_mode_enabled():
        await reset_previous_active_file_message(client, rt, user)
    sent = await send_file_message(client, message.chat.id, item)
    rt.db.record_file_event(user.id, item.id, event_type)
    await remember_file_message(client, rt, user, sent)


async def refresh_random_file(
    client: Client,
    rt: BotRuntime,
    query: CallbackQuery,
    *,
    exclude_file_id: int | None,
    event_type: str,
) -> None:
    user = query.from_user
    if not user or not query.message:
        return
    item = rt.db.random_file(exclude_id=exclude_file_id)
    if not item:
        await query.answer()
        await edit_query_text_or_caption(
            query,
            "<b>The vault is empty.</b>\n\nAn admin needs to index at least one file first.",
            refresh_keyboard(),
        )
        return
    await query.answer("Drawing another file...")
    target = (int(query.message.chat.id), int(query.message.id))
    if rt.db.single_file_mode_enabled():
        await reset_previous_active_file_message(client, rt, user, skip=target)
    try:
        edited = await edit_message_media(rt, query, item)
        rt.db.record_file_event(user.id, item.id, event_type)
        await remember_file_message(client, rt, user, edited)
        return
    except (RPCError, ValueError):
        try:
            cancel_delete_task(rt, int(query.message.chat.id), int(query.message.id))
            await query.message.delete()
        except RPCError:
            pass
        sent = await send_file_message(client, query.message.chat.id, item)
        rt.db.record_file_event(user.id, item.id, event_type)
        await remember_file_message(client, rt, user, sent)


def file_caption(item: IndexedFile) -> str:
    title = escape(short_text(item.label, 900)) if item.label else "Your random file is ready."
    return f"<b>{title}</b>\nNeed another one? Tap refresh."


async def edit_query_text_or_caption(
    query: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> None:
    message = query.message
    if not message:
        return
    try:
        await message.edit_caption(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except RPCError:
        await message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


async def edit_message_media(rt: BotRuntime, query: CallbackQuery, item: IndexedFile) -> Message:
    message = query.message
    if not message:
        raise ValueError("No message attached to callback query.")
    try:
        edited = await message.edit_media(
            media=input_media_for(item),
            reply_markup=refresh_keyboard(item.id),
        )
        return edited or message
    except ValueError as error:
        corrected_type = file_type_from_mismatch(error) or detect_file_type(item.file_id, item.file_type)
        if corrected_type == item.file_type:
            raise
        corrected = correct_file_type(rt, item, corrected_type)
        edited = await message.edit_media(
            media=input_media_for(corrected),
            reply_markup=refresh_keyboard(corrected.id),
        )
        return edited or message


def input_media_for(item: IndexedFile):
    kwargs = {"media": item.file_id, "caption": file_caption(item), "parse_mode": ParseMode.HTML}
    if item.file_type == "photo":
        return InputMediaPhoto(**kwargs)
    if item.file_type == "video":
        return InputMediaVideo(**kwargs)
    if item.file_type == "audio":
        return InputMediaAudio(**kwargs)
    if item.file_type == "animation":
        return InputMediaAnimation(**kwargs)
    return InputMediaDocument(**kwargs)


async def send_file_message(client: Client, chat_id: int, item: IndexedFile) -> Message:
    return await client.send_cached_media(
        chat_id=chat_id,
        file_id=item.file_id,
        caption=file_caption(item),
        parse_mode=ParseMode.HTML,
        reply_markup=refresh_keyboard(item.id),
    )


async def add_force_sub_command(
    client: Client,
    rt: BotRuntime,
    message: Message,
    *,
    mode: str,
) -> None:
    await track_user(rt, message.from_user)
    if not await require_sudo(rt, message):
        return
    args = command_args(message)
    if not args:
        command = "addreqfsub" if mode == FSUB_REQUEST_MODE else "addfsub"
        await message.reply_text(f"Use /{command} <chat_id|@username> [invite_link] [title].")
        return

    chat_id = args[0]
    tail = args[1:]
    manual_invite = None
    title_parts: list[str] = []
    for part in tail:
        maybe_link = normalize_invite_link(part)
        if maybe_link and not manual_invite:
            manual_invite = maybe_link
        else:
            title_parts.append(part)
    custom_title = " ".join(title_parts) or None

    try:
        stored_chat_id, title, invite_link = await resolve_force_sub_chat(
            client,
            chat_id,
            manual_invite=manual_invite,
            custom_title=custom_title,
            mode=mode,
        )
    except ValueError as error:
        await message.reply_text(str(error))
        return

    rt.db.add_force_sub_chat(stored_chat_id, title, invite_link, mode)
    mode_label = "join-request" if mode == FSUB_REQUEST_MODE else "membership"
    await message.reply_text(
        (
            f"Force-sub enabled for <b>{escape(title)}</b>.\n"
            f"Mode: <b>{mode_label}</b>.\n"
            "Users will see it as a button, not plain text."
        ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def resolve_force_sub_chat(
    client: Client,
    chat_id: str,
    *,
    manual_invite: str | None,
    custom_title: str | None,
    mode: str,
) -> tuple[str, str, str]:
    try:
        chat = await client.get_chat(chat_ref(chat_id))
    except RPCError as error:
        raise ValueError(
            "I could not access that chat. Add me there as admin first, then try again."
        ) from error

    me = await client.get_me()
    try:
        bot_member = await client.get_chat_member(chat.id, me.id)
    except RPCError as error:
        raise ValueError(
            "I could not verify my admin status in that chat. Make me admin first."
        ) from error

    bot_status = status_value(bot_member.status)
    if bot_status not in {"administrator", "creator", "owner"}:
        raise ValueError("I need to be an admin in that chat before it can be used for force-sub.")

    title = custom_title or chat.title or chat.username or str(chat.id)
    invite_link = manual_invite

    if mode == FSUB_REQUEST_MODE and not invite_link:
        invite_link = await create_force_sub_invite_link(
            client,
            chat.id,
            creates_join_request=True,
        )
        if not invite_link:
            raise ValueError(
                "I could not create a join-request invite link. Give me invite-link permission or pass one manually."
            )
    elif not invite_link:
        invite_link = getattr(chat, "invite_link", None)
        if not invite_link and getattr(chat, "username", None):
            invite_link = f"https://t.me/{chat.username}"
        if not invite_link:
            invite_link = await create_force_sub_invite_link(
                client,
                chat.id,
                creates_join_request=False,
            )
        if not invite_link:
            raise ValueError(
                "I could not find or create an invite link. Give me invite-link permission or pass one manually."
            )

    return str(chat.id), title, invite_link


async def create_force_sub_invite_link(
    client: Client,
    chat_id: int,
    *,
    creates_join_request: bool,
) -> str | None:
    try:
        invite = await client.create_chat_invite_link(
            chat_id,
            creates_join_request=creates_join_request,
        )
    except RPCError:
        return None
    return getattr(invite, "invite_link", None)


async def export_db_command(client: Client, rt: BotRuntime, message: Message) -> None:
    await track_user(rt, message.from_user)
    if not await require_sudo(rt, message):
        return
    status = await message.reply_text(
        "<b>Database export started.</b>\n\nCreating a live SQLite backup...",
        parse_mode=ParseMode.HTML,
    )
    temp_dir = tempfile.mkdtemp(prefix="random-file-bot-db-")
    backup_path: str | None = None
    try:
        timestamp = utcnow().strftime("%Y%m%d-%H%M%S")
        backup_path = os.path.join(temp_dir, f"random-file-bot-backup-{timestamp}.sqlite3")
        await asyncio.to_thread(rt.db.backup_to, backup_path)
        backup_size = os.path.getsize(backup_path)
        await status.edit_text(
            (
                "<b>Database export ready.</b>\n\n"
                f"Size: <b>{backup_size / 1024 / 1024:.2f} MB</b>\n"
                "Uploading backup file..."
            ),
            parse_mode=ParseMode.HTML,
        )
        await client.send_document(
            chat_id=message.chat.id,
            document=backup_path,
            caption=(
                "<b>Full database backup</b>\n\n"
                "This SQLite file contains all bot tables, including indexed files, users, "
                "force-sub chats, sudo users, usage events, and membership logs."
            ),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message.id,
        )
        await status.edit_text(
            "<b>Database export complete.</b>\n\nThe backup file was sent above.",
            parse_mode=ParseMode.HTML,
        )
    except RPCError:
        logging.exception("Telegram rejected database export upload")
        await status.edit_text("Telegram would not let me send the database backup.")
    except Exception as error:
        logging.exception("Database export failed")
        await status.edit_text(
            f"Export failed: <code>{escape(error.__class__.__name__)}</code>.",
            parse_mode=ParseMode.HTML,
        )
    finally:
        if backup_path and os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except OSError:
                pass
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass


async def import_json_command(client: Client, rt: BotRuntime, message: Message) -> None:
    await track_user(rt, message.from_user)
    if not await require_sudo(rt, message):
        return
    reply = message.reply_to_message
    source = reply if reply and reply.document else message if message.document else None
    document = source.document if source else None
    if not source or not document:
        await message.reply_text(
            "Send /importjson as a reply to a JSON file, or upload the JSON file with /importjson as its caption."
        )
        return
    file_name = document.file_name or ""
    if file_name and not file_name.lower().endswith(".json"):
        await message.reply_text("That does not look like a .json file.")
        return

    args = command_args(message)
    replace = bool(args and args[0].lower() == "replace")
    status = await message.reply_text(
        "<b>JSON import started.</b>\n\nDownloading file...",
        parse_mode=ParseMode.HTML,
    )
    temp_dir = tempfile.mkdtemp(prefix="random-file-bot-json-")
    downloaded_path: str | None = None
    try:
        temp_path = os.path.join(temp_dir, "import.json")
        progress_state = {"last_edit": 0.0, "pending": None}
        downloaded_path = await source.download(
            file_name=temp_path,
            progress=download_progress,
            progress_args=(status, progress_state),
        )
        pending = progress_state.get("pending")
        if isinstance(pending, asyncio.Task):
            with suppress(Exception):
                await pending
        if not downloaded_path:
            await status.edit_text("Import failed: Telegram did not return a downloaded file path.")
            return
        await status.edit_text(
            "<b>JSON import in progress.</b>\n\nDownload complete. Parsing JSON...",
            parse_mode=ParseMode.HTML,
        )
        files, skipped = await asyncio.to_thread(load_import_file, downloaded_path)
    except (UnicodeDecodeError, json.JSONDecodeError):
        await status.edit_text("I could not parse that file as valid UTF-8 JSON.")
        return
    except RPCError:
        await status.edit_text("Telegram would not let me download that JSON file.")
        return
    except Exception as error:
        logging.exception("JSON import failed")
        await status.edit_text(
            f"Import failed before anything was written: <code>{escape(error.__class__.__name__)}</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    finally:
        if downloaded_path and os.path.exists(downloaded_path):
            try:
                os.remove(downloaded_path)
            except OSError:
                pass
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass

    if not files:
        await status.edit_text("No importable file records found. Each record needs an _id file ID.")
        return
    await status.edit_text(
        f"<b>JSON import in progress.</b>\n\nWriting <b>{len(files)}</b> file records to SQLite...",
        parse_mode=ParseMode.HTML,
    )
    try:
        imported = await asyncio.to_thread(
            rt.db.import_files,
            files,
            added_by=message.from_user.id,
            replace=replace,
        )
    except Exception as error:
        logging.exception("JSON import database write failed")
        await status.edit_text(
            f"Import failed while writing the DB: <code>{escape(error.__class__.__name__)}</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    mode = "Replaced the indexed-file DB with" if replace else "Imported"
    await status.edit_text(
        (
            f"{mode} <b>{imported}</b> files from JSON.\n"
            f"Skipped records: <b>{skipped}</b>."
        ),
        parse_mode=ParseMode.HTML,
    )


async def download_progress(current: int, total: int, status: Message, state: dict[str, object]) -> None:
    loop_time = asyncio.get_running_loop().time()
    if current < total and loop_time - state["last_edit"] < 5:
        return
    pending = state.get("pending")
    if isinstance(pending, asyncio.Task) and not pending.done():
        return
    state["last_edit"] = loop_time
    if total:
        percent = current * 100 / total
        text = (
            "<b>JSON import in progress.</b>\n\n"
            f"Downloading: <b>{percent:.1f}%</b>\n"
            f"{current / 1024 / 1024:.1f} MB / {total / 1024 / 1024:.1f} MB"
        )
    else:
        text = (
            "<b>JSON import in progress.</b>\n\n"
            f"Downloading: <b>{current / 1024 / 1024:.1f} MB</b>"
        )
    state["pending"] = asyncio.create_task(edit_download_status(status, text))


async def edit_download_status(status: Message, text: str) -> None:
    try:
        await status.edit_text(text, parse_mode=ParseMode.HTML)
    except RPCError:
        pass


def load_import_file(path: str) -> tuple[list[tuple[str, str, str | None]], int]:
    with open(path, "r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    return parse_import_payload(payload)


def parse_import_payload(payload) -> tuple[list[tuple[str, str, str | None]], int]:
    if isinstance(payload, dict) and "_id" in payload:
        records = [payload]
    elif isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = next(
            (value for value in payload.values() if isinstance(value, list)),
            [],
        )
    else:
        records = []

    files: list[tuple[str, str, str | None]] = []
    skipped = 0
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            skipped += 1
            continue
        file_id = record.get("_id") or record.get("file_id") or record.get("id")
        if not isinstance(file_id, str) or not file_id.strip():
            skipped += 1
            continue
        file_id = file_id.strip()
        if file_id in seen:
            skipped += 1
            continue
        seen.add(file_id)

        raw_type = str(record.get("file_type") or record.get("type") or "").lower()
        file_type = detect_file_type(file_id, raw_type)
        label = record.get("caption") or record.get("file_name") or record.get("name")
        if label is not None:
            label = str(label).strip() or None
        files.append((file_id, file_type, label))
    return files, skipped


def extract_replied_media(message: Message) -> tuple[str, str, str | None] | None:
    reply = message.reply_to_message
    if not reply:
        return None
    if reply.document:
        return reply.document.file_id, "document", reply.document.file_name
    if reply.video:
        return reply.video.file_id, "video", getattr(reply.video, "file_name", None)
    if reply.audio:
        return reply.audio.file_id, "audio", reply.audio.file_name or reply.audio.title
    if reply.animation:
        return reply.animation.file_id, "animation", reply.animation.file_name
    if reply.photo:
        return reply.photo.file_id, "photo", None
    return None


def current_file_id(callback_data: str | None) -> int | None:
    if not callback_data or ":" not in callback_data:
        return None
    try:
        return int(callback_data.rsplit(":", 1)[1])
    except ValueError:
        return None


def format_dt(value) -> str:
    if not value:
        return "never"
    return value.strftime("%Y-%m-%d %H:%M UTC")


def parse_duration_seconds(args: list[str]) -> int | None:
    if not args:
        return None
    raw = "".join(args).strip().lower()
    if raw in {"off", "disable", "disabled", "none", "0", "0s"}:
        return 0
    match = re.fullmatch(
        r"(\d+)(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)?",
        raw,
    )
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    if unit.startswith("m"):
        return amount * 60
    if unit.startswith("h"):
        return amount * 60 * 60
    if unit.startswith("d"):
        return amount * 24 * 60 * 60
    return amount


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "off"
    units = [
        (24 * 60 * 60, "day"),
        (60 * 60, "hour"),
        (60, "minute"),
        (1, "second"),
    ]
    for size, label in units:
        if seconds >= size and seconds % size == 0:
            value = seconds // size
            suffix = "" if value == 1 else "s"
            return f"{value} {label}{suffix}"
    return f"{seconds} seconds"


def user_help_text(rt: BotRuntime) -> str:
    minutes = max(1, rt.config.request_window_seconds // 60)
    return (
        "<b>Vault help</b>\n\n"
        "I pull a random file from the vault. No maze, no ceremony, just a clean draw.\n\n"
        "<b>Commands</b>\n"
        "- /random or /get - draw a file\n"
        "- /stats - see your usage\n"
        "- /help - open this note\n\n"
        "Use the refresh button under a file for another draw. "
        f"Limit: <b>{rt.config.request_limit}</b> files every <b>{minutes}</b> minutes."
    )


def admin_help_text(rt: BotRuntime) -> str:
    single_mode = "on" if rt.db.single_file_mode_enabled() else "off"
    delete_timer = format_duration(rt.db.delete_timer_seconds())
    return (
        "<b>Admin manual</b>\n\n"
        "Everything sharp, logged, and mildly suspicious of chaos.\n\n"
        "<b>User flow</b>\n"
        "- /start - show the draw prompt.\n"
        "- /random or /get - send a random vault file.\n"
        "- /stats - show your own usage.\n"
        "- /help - role-aware help.\n"
        "- Inline: <code>@YourBotUsername</code> - send a random cached video.\n\n"
        "<b>Vault files</b>\n"
        "- /addfile &lt;file_id&gt; [label] - index a document file ID.\n"
        "- /addfile &lt;type&gt; &lt;file_id&gt; [label] - index a typed file. "
        "Types: document, photo, video, audio, animation.\n"
        "- Reply to media with /addfile - index that media directly.\n"
        "- /importjson - reply to a JSON document and import file IDs.\n"
        "- /importjson replace - replace indexed files from JSON.\n"
        "- /delfile &lt;file_id&gt; - remove one indexed file.\n"
        "- /files - show total indexed files and recent entries.\n\n"
        "<b>Access control</b>\n"
        "- /addfsub &lt;chat_id|@username&gt; [invite_link] [title] - require chat/channel membership.\n"
        "- /addreqfsub &lt;chat_id|@username&gt; [invite_link] [title] - require a join request.\n"
        "- /delfsub &lt;chat_id|@username&gt; - remove a force-sub chat.\n"
        "- /fsubs - list force-sub chats.\n"
        "- /addsudo &lt;user_id&gt; - grant sudo access.\n"
        "- /delsudo &lt;user_id&gt; - revoke sudo access.\n"
        "- /sudos - list sudo users.\n\n"
        "<b>Monitoring</b>\n"
        "- /admin - dashboard totals and top users.\n"
        "- /user &lt;user_id&gt; - inspect one user.\n"
        "- /users - recent users.\n"
        "- /blocked - users marked blocked.\n"
        "- /membership - recent join/member events.\n"
        "- /broadcast &lt;text&gt; - message active users.\n"
        "- /exportdb - send a full SQLite backup.\n\n"
        "<b>Modes</b>\n"
        f"- /singlemode [on|off|status] - one active file message per user. Current: <b>{single_mode}</b>.\n"
        f"- /deletetimer &lt;time|off&gt; - reset/delete non-admin file messages. Current: <b>{delete_timer}</b>.\n"
        "  Examples: <code>/deletetimer 30s</code>, <code>/deletetimer 10m</code>, "
        "<code>/deletetimer 1h</code>, <code>/deletetimer off</code>."
    )


def build_client(config: Config, db: Database) -> Client:
    app = Client(
        config.session_name,
        api_id=config.api_id,
        api_hash=config.api_hash,
        bot_token=config.bot_token,
        workdir=config.session_workdir,
        parse_mode=ParseMode.HTML,
        max_concurrent_transmissions=config.max_concurrent_transmissions,
    )
    rt = BotRuntime(config, db)

    @app.on_message(filters.command("start"))
    async def start(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        await message.reply_text(
            sleek_title(message.from_user.first_name if message.from_user else None),
            reply_markup=start_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("help"))
    async def help_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        user = message.from_user
        text = admin_help_text(rt) if user and is_sudo_user(rt, user.id) else user_help_text(rt)
        await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    @app.on_message(filters.command(["random", "get"]))
    async def random_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await enforce_force_sub_message(client, rt, message):
            return
        if not await enforce_rate_limit_message(rt, message):
            return
        await send_random_file_message(client, rt, message, event_type="request")

    @app.on_callback_query(filters.regex(re.compile(rf"^{REFRESH_CALLBACK}(:\d+)?$")))
    async def refresh_callback(client: Client, query: CallbackQuery) -> None:
        await track_user(rt, query.from_user)
        if not await enforce_force_sub_query(client, rt, query):
            return
        if not await enforce_rate_limit_query(rt, query):
            return
        exclude_file_id = current_file_id(query.data)
        await refresh_random_file(
            client,
            rt,
            query,
            exclude_file_id=exclude_file_id,
            event_type="refresh" if exclude_file_id is not None else "request",
        )

    @app.on_inline_query()
    async def inline_random_video(client: Client, query: InlineQuery) -> None:
        await track_user(rt, query.from_user)
        if query.query.strip():
            await query.answer([], cache_time=0, is_personal=True)
            return
        if not await enforce_force_sub_inline(client, rt, query):
            return
        if not await enforce_rate_limit_inline(rt, query):
            return
        item = find_random_video(rt)
        if not item:
            await query.answer(
                [],
                cache_time=0,
                is_personal=True,
                switch_pm_text="No videos in the vault yet",
                switch_pm_parameter="no_videos",
            )
            return
        await query.answer(
            [
                InlineQueryResultCachedVideo(
                    video_file_id=item.file_id,
                    title="Send a random video from vault",
                    id=f"vault-video-{item.id}",
                    description=item.label or "Tap to send a random vault video.",
                    caption=file_caption(item),
                    parse_mode=ParseMode.HTML,
                )
            ],
            cache_time=0,
            is_personal=True,
        )
        rt.db.record_file_event(query.from_user.id, item.id, "inline")

    @app.on_message(filters.command("stats"))
    async def stats_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not message.from_user:
            return
        stats = rt.db.get_user_stats(message.from_user.id)
        if not stats:
            return
        await message.reply_text(
            (
                "<b>Your usage</b>\n\n"
                f"Files sent: <b>{stats.files_sent}</b>\n"
                f"Refreshes: <b>{stats.refresh_count}</b>\n"
                f"Denied attempts: <b>{stats.denied_count}</b>\n"
                f"Last request: <b>{format_dt(stats.last_request_at)}</b>"
            ),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("admin"))
    async def admin_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        totals = rt.db.total_stats()
        top = rt.db.top_users(5)
        top_lines = "\n".join(
            f"{idx}. {user_label(user)} - {user.request_count} requests"
            for idx, user in enumerate(top, start=1)
        ) or "No users yet."
        await message.reply_text(
            (
                "<b>Admin panel</b>\n\n"
                f"Users: <b>{totals['users']}</b>\n"
                f"Blocked: <b>{totals['blocked']}</b>\n"
                f"Indexed files: <b>{totals['files']}</b>\n"
                f"Force-sub chats: <b>{totals['fsubs']}</b>\n"
                f"Total requests: <b>{totals['requests']}</b>\n"
                f"Last 60 minutes: <b>{totals['recent_requests']}</b>\n"
                f"Denied attempts: <b>{totals['denied']}</b>\n\n"
                f"<b>Top users</b>\n{top_lines}"
            ),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("exportdb"))
    async def export_db(client: Client, message: Message) -> None:
        await export_db_command(client, rt, message)

    @app.on_message(filters.command("singlemode"))
    async def single_mode_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        current = rt.db.single_file_mode_enabled()
        if not args:
            enabled = not current
        else:
            value = args[0].lower()
            if value in {"on", "enable", "enabled", "yes", "true", "1"}:
                enabled = True
            elif value in {"off", "disable", "disabled", "no", "false", "0"}:
                enabled = False
            elif value in {"status", "state"}:
                await message.reply_text(
                    (
                        "<b>Single-message mode</b>\n\n"
                        f"Current state: <b>{'on' if current else 'off'}</b>.\n"
                        "When on, each user gets one active file message at a time."
                    ),
                    parse_mode=ParseMode.HTML,
                )
                return
            else:
                await message.reply_text("Use /singlemode, /singlemode on, /singlemode off, or /singlemode status.")
                return
        rt.db.set_single_file_mode(enabled)
        await message.reply_text(
            (
                "<b>Single-message mode updated.</b>\n\n"
                f"State: <b>{'on' if enabled else 'off'}</b>.\n"
                "When on, a user's previous active file message is reset before a new draw is shown."
            ),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("deletetimer"))
    async def delete_timer_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        if not args:
            current = format_duration(rt.db.delete_timer_seconds())
            await message.reply_text(
                (
                    "<b>Delete timer</b>\n\n"
                    f"Current timer: <b>{current}</b>.\n"
                    "Use /deletetimer 30s, /deletetimer 10m, /deletetimer 1h, or /deletetimer off."
                ),
                parse_mode=ParseMode.HTML,
            )
            return
        seconds = parse_duration_seconds(args)
        if seconds is None:
            await message.reply_text("Use a time like 30s, 10m, 1h, 2d, or off.")
            return
        rt.db.set_delete_timer_seconds(seconds)
        cancel_all_delete_tasks(rt)
        if seconds <= 0:
            await message.reply_text(
                "<b>Delete timer disabled.</b>\n\nPending file timers were cancelled.",
                parse_mode=ParseMode.HTML,
            )
            return
        await message.reply_text(
            (
                "<b>Delete timer updated.</b>\n\n"
                f"New non-admin file messages will reset/delete after <b>{format_duration(seconds)}</b>."
            ),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("addfile"))
    async def add_file_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        media = extract_replied_media(message)
        args = command_args(message)
        if media:
            file_id, file_type, label = media
        elif args:
            if args[0].lower() in SUPPORTED_FILE_TYPES and len(args) >= 2:
                file_type = args[0].lower()
                file_id = args[1]
                label = " ".join(args[2:]) or None
            else:
                file_id = args[0]
                file_type = "document"
                label = " ".join(args[1:]) or None
        else:
            await message.reply_text(
                "Use /addfile <file_id> [label], /addfile <type> <file_id> [label], or reply to media with /addfile."
            )
            return

        rt.db.add_file(file_id, file_type, label, message.from_user.id)
        await message.reply_text(
            f"Indexed <b>{escape(file_type)}</b> file <code>{escape(compact_file_id(file_id))}</code>.",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("importjson"))
    async def import_json(client: Client, message: Message) -> None:
        await import_json_command(client, rt, message)

    @app.on_message(filters.regex(re.compile(r"^/importjson(?:@\w+)?(?:\s|$)", re.IGNORECASE)))
    async def import_json_regex(client: Client, message: Message) -> None:
        await import_json_command(client, rt, message)

    @app.on_message(filters.document)
    async def import_json_caption(client: Client, message: Message) -> None:
        if caption_command_is(message, "importjson"):
            await import_json_command(client, rt, message)
            return
        await track_user(rt, message.from_user)

    @app.on_message(filters.command("delfile"))
    async def del_file_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        if not args:
            await message.reply_text("Use /delfile <file_id>.")
            return
        removed = rt.db.remove_file(args[0])
        await message.reply_text("File removed." if removed else "That file was not indexed.")

    @app.on_message(filters.command("files"))
    async def files_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        rows = rt.db.recent_files(10)
        lines = [
            f"{row.id}. {escape(row.file_type)} - <code>{escape(compact_file_id(row.file_id))}</code>"
            for row in rows
        ]
        await message.reply_text(
            (
                f"<b>Indexed files:</b> {rt.db.count_files()}\n\n"
                + ("\n".join(lines) if lines else "No files indexed yet.")
            ),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("addfsub"))
    async def add_fsub_command(client: Client, message: Message) -> None:
        await add_force_sub_command(client, rt, message, mode=FSUB_MEMBER_MODE)

    @app.on_message(filters.command(["addreqfsub", "addfsubreq"]))
    async def add_request_fsub_command(client: Client, message: Message) -> None:
        await add_force_sub_command(client, rt, message, mode=FSUB_REQUEST_MODE)

    @app.on_message(filters.command("delfsub"))
    async def del_fsub_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        if not args:
            await message.reply_text("Use /delfsub <chat_id|@username>.")
            return
        target = args[0]
        resolved_target = target
        try:
            chat = await client.get_chat(chat_ref(target))
            resolved_target = str(chat.id)
        except RPCError:
            pass
        removed = rt.db.remove_force_sub_chat(resolved_target)
        if not removed and resolved_target != target:
            removed = rt.db.remove_force_sub_chat(target)
        await message.reply_text(
            "Force-sub chat removed." if removed else "That chat was not configured."
        )

    @app.on_message(filters.command("fsubs"))
    async def fsubs_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        chats = rt.db.list_force_sub_chats(enabled_only=False)
        if not chats:
            await message.reply_text("No force-sub chats configured.")
            return
        lines = [
            (
                f"- <b>{escape(chat.title or chat.chat_id)}</b> | "
                f"<code>{escape(chat.chat_id)}</code> | "
                f"<b>{escape(chat.mode)}</b>"
            )
            for chat in chats
        ]
        await message.reply_text(
            "<b>Force-sub chats</b>\n\n" + "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("addsudo"))
    async def add_sudo_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        if not args:
            await message.reply_text("Use /addsudo <user_id>.")
            return
        try:
            user_id = int(args[0])
        except ValueError:
            await message.reply_text("User ID must be numeric.")
            return
        rt.db.add_sudo(user_id, message.from_user.id)
        await message.reply_text(f"Added sudo user <code>{user_id}</code>.", parse_mode=ParseMode.HTML)

    @app.on_message(filters.command("delsudo"))
    async def del_sudo_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        if not args:
            await message.reply_text("Use /delsudo <user_id>.")
            return
        try:
            user_id = int(args[0])
        except ValueError:
            await message.reply_text("User ID must be numeric.")
            return
        if user_id == rt.config.owner_id:
            await message.reply_text("The owner cannot be removed from sudo access.")
            return
        removed = rt.db.remove_sudo(user_id)
        await message.reply_text("Sudo user removed." if removed else "That user was not sudo.")

    @app.on_message(filters.command("sudos"))
    async def sudos_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        sudos = rt.db.list_sudos()
        lines = "\n".join(f"- <code>{user_id}</code>" for user_id in sudos)
        await message.reply_text(
            "<b>Sudo users</b>\n\n" + lines,
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("user"))
    async def user_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        if not args:
            await message.reply_text("Use /user <user_id>.")
            return
        try:
            user_id = int(args[0])
        except ValueError:
            await message.reply_text("User ID must be numeric.")
            return
        stats = rt.db.get_user_stats(user_id)
        if not stats:
            await message.reply_text("No record for that user.")
            return
        await message.reply_text(
            (
                f"<b>User {stats.user_id}</b>\n\n"
                f"Name: <b>{user_label(stats)}</b>\n"
                f"Blocked: <b>{'yes' if stats.is_blocked else 'no'}</b>\n"
                f"Requests: <b>{stats.request_count}</b>\n"
                f"Refreshes: <b>{stats.refresh_count}</b>\n"
                f"Files sent: <b>{stats.files_sent}</b>\n"
                f"Denied: <b>{stats.denied_count}</b>\n"
                f"First seen: <b>{format_dt(stats.first_seen)}</b>\n"
                f"Last seen: <b>{format_dt(stats.last_seen)}</b>\n"
                f"Last request: <b>{format_dt(stats.last_request_at)}</b>"
            ),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("users"))
    async def users_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        users = rt.db.recent_users(15)
        if not users:
            await message.reply_text("No users logged yet.")
            return
        lines = [
            f"- <code>{user.user_id}</code> | {user_label(user)} | seen {format_dt(user.first_seen)}"
            for user in users
        ]
        await message.reply_text(
            "<b>Recent users</b>\n\n" + "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("blocked"))
    async def blocked_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        users = rt.db.blocked_users(15)
        if not users:
            await message.reply_text("No blocked users logged.")
            return
        lines = [
            f"- <code>{user.user_id}</code> | {user_label(user)} | marked {format_dt(user.last_seen)}"
            for user in users
        ]
        await message.reply_text(
            "<b>Blocked users</b>\n\n" + "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("membership"))
    async def membership_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        events = rt.db.recent_membership_events(15)
        if not events:
            await message.reply_text("No membership events logged yet.")
            return
        lines = [
            (
                f"- <code>{event.user_id}</code> in <code>{escape(event.chat_id)}</code> "
                f"-> <b>{escape(event.status)}</b> at {format_dt(event.created_at)}"
            )
            for event in events
        ]
        await message.reply_text(
            "<b>Recent membership events</b>\n\n" + "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("broadcast"))
    async def broadcast_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        text = (message.text or message.caption or "").partition(" ")[2].strip()
        if not text:
            await message.reply_text("Use /broadcast <text>.")
            return
        sent = 0
        blocked = 0
        for user_id in rt.db.active_user_ids():
            try:
                await client.send_message(user_id, text)
                sent += 1
                await asyncio.sleep(0.05)
            except RPCError as error:
                if is_block_error(error):
                    rt.db.mark_user_blocked(user_id, True)
                    blocked += 1
        await message.reply_text(f"Broadcast sent to {sent} users. Marked {blocked} blocked.")

    @app.on_message(filters.all)
    async def track_regular_message(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)

    @app.on_chat_member_updated()
    async def chat_member_updated(client: Client, update: ChatMemberUpdated) -> None:
        user = getattr(update, "from_user", None)
        await track_user(rt, user)
        new_member = getattr(update, "new_chat_member", None)
        if not new_member:
            return
        member_user = getattr(new_member, "user", None)
        status = status_value(getattr(new_member, "status", ""))
        if member_user:
            rt.db.record_membership_event(member_user.id, str(update.chat.id), status)
            if chat_type_value(update.chat) == "private" and status in {"kicked", "left", "banned"}:
                rt.db.mark_user_blocked(member_user.id, True)

    @app.on_chat_join_request()
    async def chat_join_request(client: Client, request: ChatJoinRequest) -> None:
        if request.from_user:
            await track_user(rt, request.from_user)
            rt.db.record_membership_event(
                request.from_user.id,
                str(request.chat.id),
                "join_request",
            )

    return app


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    config = load_config()
    db = Database(config.database_path)
    db.init(config.owner_id)
    app = build_client(config, db)
    app.run()
