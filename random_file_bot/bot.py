from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import string
import tempfile
from contextlib import suppress
from dataclasses import replace
from datetime import timedelta
from html import escape

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import RPCError
from pyrogram.raw.functions import bots
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
from .db import Database, from_db_time, utcnow
from .models import ForceSubChat, IndexedFile, UserStats


REFRESH_CALLBACK = "refresh_file"
HELP_CALLBACK = "help_page"
FSUB_MEMBER_MODE = "member"
FSUB_REQUEST_MODE = "request"
SUPPORTED_FILE_TYPES = {"document", "photo", "video", "audio", "animation"}
PROFILE_FIELDS = {"name", "about", "description", "username"}
ON_VALUES = {"on", "enable", "enabled", "yes", "true", "1"}
OFF_VALUES = {"off", "disable", "disabled", "no", "false", "0"}
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
    buttons.append([InlineKeyboardButton("Refresh the velvet rope", callback_data=REFRESH_CALLBACK)])
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
    return user_mention(stats)


def user_mention(stats: UserStats) -> str:
    if stats.username:
        text = f"@{escape(stats.username)}"
    else:
        full_name = " ".join(part for part in [stats.first_name, stats.last_name] if part)
        text = escape(full_name) if full_name else str(stats.user_id)
    return f'<a href="tg://user?id={stats.user_id}">{text}</a>'


def user_text_mention(user_id: int, username: str | None = None, name: str | None = None) -> str:
    text = f"@{escape(username)}" if username else escape(name or str(user_id))
    return f'<a href="tg://user?id={user_id}">{text}</a>'


def parse_bool_arg(args: list[str], current: bool) -> tuple[bool | None, bool]:
    if not args:
        return not current, False
    value = args[0].lower()
    if value in ON_VALUES:
        return True, False
    if value in OFF_VALUES:
        return False, False
    if value in {"status", "state"}:
        return current, True
    return None, False


def is_block_error(error: RPCError) -> bool:
    name = error.__class__.__name__.lower()
    text = str(error).upper()
    return (
        "blocked" in name
        or "forbidden" in name
        or "peeridinvalid" in name
        or "403" in text
        or "FORBIDDEN" in text
        or "USER_IS_BLOCKED" in text
        or "BOT_BLOCKED" in text
        or "BOT WAS BLOCKED" in text
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
        if rt.db.maintenance_mode_enabled():
            return False
        await message.reply_text("I can only accept admin commands from a visible user account.")
        return False
    if is_sudo_user(rt, user.id):
        return True
    if rt.db.maintenance_mode_enabled():
        return False
    await message.reply_text("This panel is only for the owner and sudo users.")
    return False


def is_sudo_user(rt: BotRuntime, user_id: int) -> bool:
    return user_id == rt.config.owner_id or rt.db.is_sudo(user_id)


def is_unlimited_user(rt: BotRuntime, user_id: int) -> bool:
    return is_sudo_user(rt, user_id) or rt.db.is_privileged(user_id)


async def track_user(rt: BotRuntime, user) -> None:
    if user:
        rt.db.upsert_user(user, blocked=False)


def maintenance_blocks(rt: BotRuntime, user_id: int | None) -> bool:
    if not rt.db.maintenance_mode_enabled():
        return False
    return user_id is None or not is_sudo_user(rt, user_id)


async def enforce_maintenance_message(rt: BotRuntime, message: Message) -> bool:
    user = message.from_user
    return not maintenance_blocks(rt, user.id if user else None)


async def enforce_maintenance_query(rt: BotRuntime, query: CallbackQuery) -> bool:
    return not maintenance_blocks(rt, query.from_user.id if query.from_user else None)


async def enforce_maintenance_inline(rt: BotRuntime, query: InlineQuery) -> bool:
    return not maintenance_blocks(rt, query.from_user.id if query.from_user else None)


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
        "Use the required chat buttons below, then tap <b>Refresh the velvet rope</b> to continue."
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
    if is_unlimited_user(rt, user_id):
        return True
    since = utcnow() - timedelta(seconds=rt.config.request_window_seconds)
    used = rt.db.requests_since(user_id, since)
    return used < rt.config.request_limit + rt.db.bonus_requests(user_id)


def rate_limit_text(rt: BotRuntime) -> str:
    minutes = max(1, rt.config.request_window_seconds // 60)
    return (
        "<b>You have reached the hourly draw limit.</b>\n\n"
        f"You can request {rt.config.request_limit} files every {minutes} minutes. "
        "Try again a little later."
    )


def user_limit_text(rt: BotRuntime, user_id: int) -> str:
    if is_unlimited_user(rt, user_id):
        return "unlimited"
    return str(rt.config.request_limit + rt.db.bonus_requests(user_id))


def spoiler_enabled_for(rt: BotRuntime, user_id: int | None) -> bool:
    return rt.db.spoiler_mode_enabled() or bool(user_id and rt.db.user_spoiler_enabled(user_id))


def file_send_kwargs(rt: BotRuntime) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    if rt.db.protect_content_enabled():
        kwargs["protect_content"] = True
    return kwargs


def record_successful_file_request(rt: BotRuntime, user_id: int, file_db_id: int, event_type: str) -> None:
    rt.db.record_file_event(user_id, file_db_id, event_type)
    if event_type in {"request", "refresh"}:
        rt.db.fulfill_referral(user_id)


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
    rt: BotRuntime,
    *,
    user_name: str | None,
    chat_id: int,
    message_id: int,
) -> bool:
    start_img = rt.db.get_setting("start_image_id")
    text = sleek_title(user_name)
    try:
        if start_img:
            await client.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=InputMediaPhoto(
                    media=start_img,
                    caption=text,
                    parse_mode=ParseMode.HTML
                ),
                reply_markup=start_keyboard()
            )
        else:
            await client.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=start_keyboard(),
                disable_web_page_preview=True,
            )
        return True
    except RPCError:
        pass

    try:
        await client.delete_messages(chat_id=chat_id, message_ids=message_id)
        if start_img:
            await client.send_photo(
                chat_id=chat_id,
                photo=start_img,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=start_keyboard()
            )
        else:
            await client.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=start_keyboard(),
                disable_web_page_preview=True,
            )
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
        rt,
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
            rt,
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
    try:
        sent = await send_file_message(client, rt, message.chat.id, item, user_id=user.id)
    except RPCError as error:
        if is_block_error(error):
            rt.db.mark_user_blocked(user.id, True)
            return
        raise
    record_successful_file_request(rt, user.id, item.id, event_type)
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
    if not rt.db.protect_content_enabled():
        try:
            edited = await edit_message_media(rt, query, item, user_id=user.id)
            record_successful_file_request(rt, user.id, item.id, event_type)
            await remember_file_message(client, rt, user, edited)
            return
        except (RPCError, ValueError):
            pass
    try:
        cancel_delete_task(rt, int(query.message.chat.id), int(query.message.id))
        await query.message.delete()
    except RPCError:
        pass
    try:
        sent = await send_file_message(client, rt, query.message.chat.id, item, user_id=user.id)
    except RPCError as error:
        if is_block_error(error):
            rt.db.mark_user_blocked(user.id, True)
            return
        raise
    record_successful_file_request(rt, user.id, item.id, event_type)
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


async def edit_message_media(
    rt: BotRuntime,
    query: CallbackQuery,
    item: IndexedFile,
    *,
    user_id: int | None,
) -> Message:
    message = query.message
    if not message:
        raise ValueError("No message attached to callback query.")
    try:
        edited = await message.edit_media(
            media=input_media_for(rt, item, user_id=user_id),
            reply_markup=refresh_keyboard(item.id),
        )
        return edited or message
    except ValueError as error:
        corrected_type = file_type_from_mismatch(error) or detect_file_type(item.file_id, item.file_type)
        if corrected_type == item.file_type:
            raise
        corrected = correct_file_type(rt, item, corrected_type)
        edited = await message.edit_media(
            media=input_media_for(rt, corrected, user_id=user_id),
            reply_markup=refresh_keyboard(corrected.id),
        )
        return edited or message


def input_media_for(rt: BotRuntime, item: IndexedFile, *, user_id: int | None):
    kwargs = {"media": item.file_id, "caption": file_caption(item), "parse_mode": ParseMode.HTML}
    if spoiler_enabled_for(rt, user_id) and item.file_type in {"photo", "video", "animation"}:
        kwargs["has_spoiler"] = True
    if item.file_type == "photo":
        return InputMediaPhoto(**kwargs)
    if item.file_type == "video":
        return InputMediaVideo(**kwargs)
    if item.file_type == "audio":
        return InputMediaAudio(**kwargs)
    if item.file_type == "animation":
        return InputMediaAnimation(**kwargs)
    return InputMediaDocument(**kwargs)


async def send_file_message(
    client: Client,
    rt: BotRuntime,
    chat_id: int,
    item: IndexedFile,
    *,
    user_id: int | None,
) -> Message:
    kwargs = {
        "chat_id": chat_id,
        "caption": file_caption(item),
        "parse_mode": ParseMode.HTML,
        "reply_markup": refresh_keyboard(item.id),
        **file_send_kwargs(rt),
    }
    if spoiler_enabled_for(rt, user_id):
        if item.file_type == "photo":
            return await client.send_photo(photo=item.file_id, has_spoiler=True, **kwargs)
        if item.file_type == "video":
            return await client.send_video(video=item.file_id, has_spoiler=True, **kwargs)
        if item.file_type == "animation":
            return await client.send_animation(animation=item.file_id, has_spoiler=True, **kwargs)
    return await client.send_cached_media(file_id=item.file_id, **kwargs)


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
        await message.reply_text(f"Use /{command} &lt;chat_id|@username&gt; [invite_link] [title].")
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
            "<b>Database export complete.</b>\n\nThe backup file was sent below.",
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


async def import_db_command(client: Client, rt: BotRuntime, message: Message) -> None:
    await track_user(rt, message.from_user)
    if not await require_sudo(rt, message):
        return
    reply = message.reply_to_message
    source = reply if reply and reply.document else message if message.document else None
    document = source.document if source else None
    if not source or not document:
        await message.reply_text(
            "Send /importdb as a reply to an exported SQLite database, or upload it with /importdb as its caption."
        )
        return
    file_name = (document.file_name or "").lower()
    if file_name and not file_name.endswith((".sqlite", ".sqlite3", ".db")):
        await message.reply_text("That does not look like an exported SQLite database.")
        return
    status = await message.reply_text(
        "<b>Database import started.</b>\n\nDownloading SQLite backup...",
        parse_mode=ParseMode.HTML,
    )
    temp_dir = tempfile.mkdtemp(prefix="random-file-bot-db-import-")
    downloaded_path: str | None = None
    try:
        downloaded_path = await source.download(file_name=os.path.join(temp_dir, "import.sqlite3"))
        if not downloaded_path:
            await status.edit_text("Import failed: Telegram did not return a downloaded file path.")
            return
        await status.edit_text(
            "<b>Database import in progress.</b>\n\nValidating and restoring backup...",
            parse_mode=ParseMode.HTML,
        )
        await asyncio.to_thread(rt.db.restore_from, downloaded_path)
        await asyncio.to_thread(rt.db.init, rt.config.owner_id)
        await status.edit_text(
            "<b>Database import complete.</b>\n\nThe imported database is now active.",
            parse_mode=ParseMode.HTML,
        )
    except RPCError:
        await status.edit_text("Telegram would not let me download that database file.")
    except Exception as error:
        logging.exception("Database import failed")
        await status.edit_text(
            f"Import failed: <code>{escape(error.__class__.__name__)}</code>.",
            parse_mode=ParseMode.HTML,
        )
    finally:
        if downloaded_path and os.path.exists(downloaded_path):
            with suppress(OSError):
                os.remove(downloaded_path)
        with suppress(OSError):
            os.rmdir(temp_dir)


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


def random_code(length: int = 10) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def export_record_with_limit(rt: BotRuntime, record: dict[str, object]) -> dict[str, object]:
    bonus = int(record["bonus_requests"])
    unlimited = bool(record["is_sudo"]) or bool(record["is_privileged"])
    return {
        **record,
        "effective_limit": "unlimited" if unlimited else rt.config.request_limit + bonus,
        "first_seen": format_dt(from_db_time_text(record["first_seen"])),
        "last_seen": format_dt(from_db_time_text(record["last_seen"])),
        "last_request_at": format_dt(from_db_time_text(record["last_request_at"])),
    }


def from_db_time_text(value: object):
    if value is None:
        return None
    return from_db_time(str(value))


def users_export_text(records: list[dict[str, object]]) -> str:
    lines = [
        "user_id\tusername\tname\tblocked\trequests\trefreshes\tfiles_sent\tdenied\tbonus\teffective_limit\tsudo\tprivileged\treferrals\tfirst_seen\tlast_seen\tlast_request_at"
    ]
    for record in records:
        name = " ".join(
            str(part)
            for part in [record["first_name"], record["last_name"]]
            if part
        )
        lines.append(
            "\t".join(
                str(value if value is not None else "")
                for value in [
                    record["user_id"],
                    record["username"],
                    name,
                    "yes" if record["is_blocked"] else "no",
                    record["request_count"],
                    record["refresh_count"],
                    record["files_sent"],
                    record["denied_count"],
                    record["bonus_requests"],
                    record["effective_limit"],
                    "yes" if record["is_sudo"] else "no",
                    "yes" if record["is_privileged"] else "no",
                    record["referrals"],
                    record["first_seen"],
                    record["last_seen"],
                    record["last_request_at"],
                ]
            )
        )
    return "\n".join(lines) + "\n"


def write_users_export(records: list[dict[str, object]], path: str, fmt: str) -> None:
    if fmt == "json":
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(records, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        return
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(users_export_text(records))


async def send_full_users_export(client: Client, rt: BotRuntime, message: Message, fmt: str) -> None:
    raw_records = await asyncio.to_thread(rt.db.user_export_records)
    records = [export_record_with_limit(rt, record) for record in raw_records]
    temp_dir = tempfile.mkdtemp(prefix="random-file-bot-users-")
    path = os.path.join(temp_dir, f"users-full.{fmt}")
    try:
        await asyncio.to_thread(write_users_export, records, path, fmt)
        await client.send_document(
            chat_id=message.chat.id,
            document=path,
            caption=f"<b>Full user export</b>\n\nUsers: <b>{len(records)}</b>.",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message.id,
        )
    finally:
        with suppress(OSError):
            os.remove(path)
        with suppress(OSError):
            os.rmdir(temp_dir)


def admin_help_pages(rt: BotRuntime) -> list[str]:
    single_mode = "on" if rt.db.single_file_mode_enabled() else "off"
    global_spoiler = "on" if rt.db.spoiler_mode_enabled() else "off"
    protect = "on" if rt.db.protect_content_enabled() else "off"
    maintenance = "on" if rt.db.maintenance_mode_enabled() else "off"
    referrals = "on" if rt.db.referral_mode_enabled() else "off"
    delete_timer = format_duration(rt.db.delete_timer_seconds())
    return [
        (
            "<b>Admin help 1/2</b>\n\n"
            "<b>User commands</b>\n"
            "- /random or /get - draw a file. Example: <code>/random</code>\n"
            "- /stats - usage, bonus requests, referrals. Example: <code>/stats</code>\n"
            "- /spoiler [on|off|status] - personal spoiler preference. Example: <code>/spoiler on</code>\n"
            "- /refer - create a referral link. Valid after the referred user completes fsub and gets a file.\n"
            "- /redeem &lt;code&gt; - redeem bonus requests. Example: <code>/redeem ABC123</code>\n\n"
            "<b>Files and backups</b>\n"
            "- /addfile &lt;file_id&gt; [label] - index a file. Example: <code>/addfile document FILE_ID Movie</code>\n"
            "- /addfile as a reply to media - index replied media.\n"
            "- /importjson [replace] - import file IDs. Example: <code>/importjson replace</code>\n"
            "- /importdb - restore exported SQLite DB. Example: reply to DB with <code>/importdb</code>\n"
            "- /exportdb - export full SQLite DB.\n"
            "- /delfile &lt;file_id&gt;, /files - remove/list indexed files."
        ),
        (
            "<b>Admin help 2/2</b>\n\n"
            "<b>Access and users</b>\n"
            "- /addfsub &lt;chat_id|@username&gt; [invite_link] [title]. Example: <code>/addfsub @channel</code>\n"
            "- /addreqfsub &lt;chat_id|@username&gt; [invite_link] [title]. Example: <code>/addreqfsub @requests</code>\n"
            "- /delfsub &lt;chat_id|@username&gt;, /fsubs - remove/list force-sub.\n"
            "- /addsudo &lt;user_id&gt;, /delsudo &lt;user_id&gt;, /sudos - manage admins.\n"
            "- /addpriv &lt;user_id&gt;, /delpriv &lt;user_id&gt;, /privs - unlimited non-admin users.\n"
            "- /users [-full] [-txt|-json]. Examples: <code>/users</code>, <code>/users -full -json</code>\n"
            "- /user &lt;user_id&gt;, /blocked, /membership, /broadcast &lt;text&gt;.\n\n"
            "<b>Modes and profile</b>\n"
            f"- /maintenance [on|off|status] - dead mode for non-admins. Current: <b>{maintenance}</b>.\n"
            f"- /globalspoiler [on|off|status] - global spoiler media. Current: <b>{global_spoiler}</b>.\n"
            f"- /protect [on|off|status] - prevent forwarding/saving sent files. Current: <b>{protect}</b>.\n"
            f"- /singlemode [on|off|status] - one active file message. Current: <b>{single_mode}</b>.\n"
            f"- /deletetimer &lt;time|off&gt; - reset/delete messages. Current: <b>{delete_timer}</b>.\n"
            f"- /referrals [on|off|status] [bonus n] - current <b>{referrals}</b>, bonus <b>{rt.db.referral_bonus()}</b>. Example: <code>/referrals on bonus 5</code>\n"
            "- /promo &lt;bonus&gt; &lt;max_uses&gt; [expiry] [code]. Example: <code>/promo 10 100 2d LAUNCH</code>\n"
            "- /setbot &lt;name|about|description|username&gt; &lt;text&gt;, /delbot &lt;field&gt;, /setbotpic."
        ),
    ]


def help_keyboard(page: int, total: int) -> InlineKeyboardMarkup:
    prev_page = (page - 1) % total
    next_page = (page + 1) % total
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Prev", callback_data=f"{HELP_CALLBACK}:{prev_page}"),
                InlineKeyboardButton(f"{page + 1}/{total}", callback_data=f"{HELP_CALLBACK}:{page}"),
                InlineKeyboardButton("Next", callback_data=f"{HELP_CALLBACK}:{next_page}"),
            ]
        ]
    )


def user_help_text(rt: BotRuntime) -> str:
    minutes = max(1, rt.config.request_window_seconds // 60)
    return (
        "<b>Vault help</b>\n\n"
        "I pull a random file from the vault. No maze, no ceremony, just a clean draw.\n\n"
        "<b>Commands</b>\n"
        "- /random or /get - draw a file\n"
        "- /stats - see your usage\n"
        "- /spoiler [on|off|status] - toggle spoiler media for yourself\n"
        "- /refer - get your referral link\n"
        "- /redeem &lt;code&gt; - add promo requests\n"
        "- /help - open this note\n\n"
        "Use the refresh button under a file for another draw. "
        f"Limit: <b>{rt.config.request_limit}</b> files every <b>{minutes}</b> minutes."
    )


def admin_help_text(rt: BotRuntime) -> str:
    return admin_help_pages(rt)[0]


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
        if not await enforce_maintenance_message(rt, message):
            return
        args = command_args(message)
        if message.from_user and args and args[0].startswith("ref_"):
            with suppress(ValueError):
                referrer_id = int(args[0].removeprefix("ref_"))
                rt.db.create_referral(referrer_id, message.from_user.id)
        start_img = rt.db.get_setting("start_image_id")
        text = sleek_title(message.from_user.first_name if message.from_user else None)
        
        if start_img:
            await message.reply_photo(
                photo=start_img,
                caption=text,
                reply_markup=start_keyboard(),
                parse_mode=ParseMode.HTML
            )
        else:
            await message.reply_text(
                text,
                reply_markup=start_keyboard(),
                parse_mode=ParseMode.HTML,
            )

    @app.on_message(filters.command("help"))
    async def help_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await enforce_maintenance_message(rt, message):
            return
        user = message.from_user
        if user and is_sudo_user(rt, user.id):
            pages = admin_help_pages(rt)
            await message.reply_text(
                pages[0],
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=help_keyboard(0, len(pages)),
            )
            return
        text = user_help_text(rt)
        await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    @app.on_callback_query(filters.regex(re.compile(rf"^{HELP_CALLBACK}:\d+$")))
    async def help_callback(client: Client, query: CallbackQuery) -> None:
        await track_user(rt, query.from_user)
        if not query.from_user or not is_sudo_user(rt, query.from_user.id):
            await query.answer("Admin help only.", show_alert=True)
            return
        pages = admin_help_pages(rt)
        page = int((query.data or "0").rsplit(":", 1)[1]) % len(pages)
        await query.answer()
        if query.message:
            await query.message.edit_text(
                pages[page],
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=help_keyboard(page, len(pages)),
            )

    @app.on_message(filters.command(["random", "get"]))
    async def random_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await enforce_maintenance_message(rt, message):
            return
        if not await enforce_force_sub_message(client, rt, message):
            return
        if not await enforce_rate_limit_message(rt, message):
            return
        await send_random_file_message(client, rt, message, event_type="request")

    @app.on_callback_query(filters.regex(re.compile(rf"^{REFRESH_CALLBACK}(:\d+)?$")))
    async def refresh_callback(client: Client, query: CallbackQuery) -> None:
        await track_user(rt, query.from_user)
        if not await enforce_maintenance_query(rt, query):
            return
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
        if not await enforce_maintenance_inline(rt, query):
            await query.answer([], cache_time=0, is_personal=True)
            return
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
                    title="Mystery vault draw",
                    id=f"vault-video-{item.id}",
                    description="Tap to reveal it in chat.",
                    caption="<b>Mystery file unlocked.</b>",
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
        if not await enforce_maintenance_message(rt, message):
            return
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
                f"Current limit: <b>{user_limit_text(rt, stats.user_id)}</b>\n"
                f"Bonus requests: <b>{rt.db.bonus_requests(stats.user_id)}</b>\n"
                f"Referrals: <b>{rt.db.referral_count(stats.user_id)}</b>\n"
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

    @app.on_message(filters.command("importdb"))
    async def import_db(client: Client, message: Message) -> None:
        await import_db_command(client, rt, message)

    @app.on_message(filters.command("maintenance"))
    async def maintenance_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        current = rt.db.maintenance_mode_enabled()
        enabled, status_only = parse_bool_arg(args, current)
        if enabled is None:
            await message.reply_text("Use /maintenance, /maintenance on, /maintenance off, or /maintenance status.")
            return
        if status_only:
            await message.reply_text(
                f"<b>Maintenance mode</b>\n\nCurrent state: <b>{'on' if current else 'off'}</b>.",
                parse_mode=ParseMode.HTML,
            )
            return
        rt.db.set_maintenance_mode(enabled)
        await message.reply_text(
            (
                "<b>Maintenance mode updated.</b>\n\n"
                f"State: <b>{'on' if enabled else 'off'}</b>.\n"
                "When on, non-admin users get no bot responses."
            ),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("refer"))
    async def refer_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await enforce_maintenance_message(rt, message):
            return
        if not message.from_user:
            return
        if not rt.db.referral_mode_enabled():
            await message.reply_text("Referrals are not open right now.")
            return
        me = await client.get_me()
        if not me.username:
            await message.reply_text("I need a public bot username before referral links can be created.")
            return
        link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
        await message.reply_text(
            (
                "<b>Your referral link</b>\n\n"
                f"<code>{escape(link)}</code>\n\n"
                "A referral counts after the new user completes force-sub and receives a file. "
                f"Each valid referral adds <b>{rt.db.referral_bonus()}</b> bonus requests."
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    @app.on_message(filters.command("redeem"))
    async def redeem_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await enforce_maintenance_message(rt, message):
            return
        if not message.from_user:
            return
        args = command_args(message)
        if not args:
            await message.reply_text("Use /redeem &lt;code&gt;.")
            return
        ok, text, total = rt.db.redeem_promo_code(args[0].strip(), message.from_user.id)
        if not ok:
            await message.reply_text(text)
            return
        await message.reply_text(
            f"<b>{escape(text)}</b>\n\nBonus requests now: <b>{total}</b>.",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("spoiler"))
    async def spoiler_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await enforce_maintenance_message(rt, message):
            return
        if not message.from_user:
            return
        args = command_args(message)
        current = rt.db.user_spoiler_enabled(message.from_user.id)
        enabled, status_only = parse_bool_arg(args, current)
        if enabled is None:
            await message.reply_text("Use /spoiler, /spoiler on, /spoiler off, or /spoiler status.")
            return
        if status_only:
            await message.reply_text(
                f"<b>Your spoiler mode</b>\n\nCurrent state: <b>{'on' if current else 'off'}</b>.",
                parse_mode=ParseMode.HTML,
            )
            return
        rt.db.set_user_spoiler(message.from_user.id, enabled)
        await message.reply_text(
            f"<b>Your spoiler mode updated.</b>\n\nState: <b>{'on' if enabled else 'off'}</b>.",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("globalspoiler"))
    async def global_spoiler_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        current = rt.db.spoiler_mode_enabled()
        enabled, status_only = parse_bool_arg(args, current)
        if enabled is None:
            await message.reply_text("Use /globalspoiler, /globalspoiler on, /globalspoiler off, or /globalspoiler status.")
            return
        if status_only:
            await message.reply_text(
                f"<b>Global spoiler mode</b>\n\nCurrent state: <b>{'on' if current else 'off'}</b>.",
                parse_mode=ParseMode.HTML,
            )
            return
        rt.db.set_spoiler_mode(enabled)
        await message.reply_text(
            f"<b>Global spoiler mode updated.</b>\n\nState: <b>{'on' if enabled else 'off'}</b>.",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("protect"))
    async def protect_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        current = rt.db.protect_content_enabled()
        enabled, status_only = parse_bool_arg(args, current)
        if enabled is None:
            await message.reply_text("Use /protect, /protect on, /protect off, or /protect status.")
            return
        if status_only:
            await message.reply_text(
                f"<b>Protect-content mode</b>\n\nCurrent state: <b>{'on' if current else 'off'}</b>.",
                parse_mode=ParseMode.HTML,
            )
            return
        rt.db.set_protect_content(enabled)
        await message.reply_text(
            (
                "<b>Protect-content mode updated.</b>\n\n"
                f"State: <b>{'on' if enabled else 'off'}</b>.\n"
                "When on, new file messages are sent with Telegram forwarding/saving protection."
            ),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("referrals"))
    async def referrals_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        current = rt.db.referral_mode_enabled()
        enabled: bool | None = current
        status_only = not args
        if args:
            first = args[0].lower()
            if first in ON_VALUES:
                enabled = True
                status_only = False
            elif first in OFF_VALUES:
                enabled = False
                status_only = False
            elif first in {"status", "state"}:
                status_only = True
            elif first != "bonus":
                await message.reply_text("Use /referrals [on|off|status] [bonus n].")
                return
        if "bonus" in [arg.lower() for arg in args]:
            try:
                idx = [arg.lower() for arg in args].index("bonus")
                bonus = int(args[idx + 1])
            except (ValueError, IndexError):
                await message.reply_text("Use /referrals bonus &lt;number&gt;.")
                return
            rt.db.set_referral_bonus(bonus)
            status_only = False
        if enabled is not None:
            rt.db.set_referral_mode(enabled)
        await message.reply_text(
            (
                "<b>Referral settings</b>\n\n"
                f"State: <b>{'on' if rt.db.referral_mode_enabled() else 'off'}</b>\n"
                f"Bonus per referral: <b>{rt.db.referral_bonus()}</b>"
            ),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("promo"))
    async def promo_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        if len(args) < 2:
            await message.reply_text("Use /promo &lt;bonus_requests&gt; &lt;max_uses&gt; [expiry] [code].")
            return
        try:
            bonus = int(args[0])
            max_uses = int(args[1])
        except ValueError:
            await message.reply_text("Bonus requests and max uses must be numbers.")
            return
        expires_at = None
        code = None
        if len(args) >= 3:
            maybe_expiry = parse_duration_seconds([args[2]])
            if maybe_expiry and maybe_expiry > 0:
                expires_at = utcnow() + timedelta(seconds=maybe_expiry)
                code = args[3] if len(args) >= 4 else None
            else:
                code = args[2]
        code = (code or random_code()).upper()
        if not re.fullmatch(r"[A-Z0-9_-]{4,32}", code):
            await message.reply_text("Promo code must be 4-32 characters: letters, numbers, _ or -.")
            return
        rt.db.create_promo_code(
            code,
            bonus_requests=bonus,
            max_uses=max_uses,
            expires_at=expires_at,
            created_by=message.from_user.id,
        )
        expiry_text = format_dt(expires_at) if expires_at else "never"
        await message.reply_text(
            (
                "<b>Promo code ready</b>\n\n"
                f"Code: <code>{escape(code)}</code>\n"
                f"Bonus requests: <b>{max(0, bonus)}</b>\n"
                f"Max redemptions: <b>{max(1, max_uses)}</b>\n"
                f"Expires: <b>{expiry_text}</b>"
            ),
            parse_mode=ParseMode.HTML,
        )

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

    @app.on_message(filters.command("setimg"))
    async def set_start_image_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        reply = message.reply_to_message
        if not reply or not reply.photo:
            await message.reply_text("Please reply to an image message with /setimg to set the placeholder.")
            return
        rt.db.set_setting("start_image_id", reply.photo.file_id)
        await message.reply_text("Start menu placeholder image set successfully.")

    @app.on_message(filters.command("delimg"))
    async def del_start_image_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        rt.db.set_setting("start_image_id", "")
        await message.reply_text("Start menu placeholder image removed. The bot will now default back to text-only.")

    @app.on_message(filters.command("setbot"))
    async def set_bot_profile_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        if len(args) < 2 or args[0].lower() not in PROFILE_FIELDS:
            await message.reply_text("Use /setbot &lt;name|about|description|username&gt; &lt;text&gt;.")
            return
        field = args[0].lower()
        value = " ".join(args[1:]).strip()
        try:
            if field == "username":
                await client.set_username(value.lstrip("@"))
            elif field == "name":
                await client.invoke(bots.SetBotInfo(lang_code="", name=value))
            elif field == "about":
                await client.invoke(bots.SetBotInfo(lang_code="", about=value))
            else:
                await client.invoke(bots.SetBotInfo(lang_code="", description=value))
        except RPCError as error:
            await message.reply_text(f"Telegram rejected that update: <code>{escape(error.__class__.__name__)}</code>.", parse_mode=ParseMode.HTML)
            return
        await message.reply_text(f"Bot {field} updated.")

    @app.on_message(filters.command("delbot"))
    async def delete_bot_profile_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        if not args or args[0].lower() not in {"name", "about", "description", "username", "botpic", "photo", "pic"}:
            await message.reply_text("Use /delbot &lt;name|about|description|username|botpic&gt;.")
            return
        field = args[0].lower()
        try:
            if field == "username":
                await client.set_username(None)
            elif field == "about":
                await client.invoke(bots.SetBotInfo(lang_code="", about=""))
            elif field == "description":
                await client.invoke(bots.SetBotInfo(lang_code="", description=""))
            elif field == "name":
                await client.invoke(bots.SetBotInfo(lang_code="", name=""))
            else:
                photos = []
                async for photo in client.get_chat_photos("me", limit=1):
                    photos.append(photo.file_id)
                if not photos:
                    await message.reply_text("No bot profile photo is set.")
                    return
                await client.delete_profile_photos(photos)
        except RPCError as error:
            await message.reply_text(f"Telegram rejected that update: <code>{escape(error.__class__.__name__)}</code>.", parse_mode=ParseMode.HTML)
            return
        await message.reply_text(f"Bot {field} cleared.")

    @app.on_message(filters.command("setbotpic"))
    async def set_bot_picture_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        reply = message.reply_to_message
        if not reply or not reply.photo:
            await message.reply_text("Reply to a photo with /setbotpic.")
            return
        temp_dir = tempfile.mkdtemp(prefix="random-file-bot-profile-")
        path: str | None = None
        try:
            path = await reply.download(file_name=os.path.join(temp_dir, "botpic.jpg"))
            await client.set_profile_photo(photo=path)
        except RPCError as error:
            await message.reply_text(f"Telegram rejected that photo: <code>{escape(error.__class__.__name__)}</code>.", parse_mode=ParseMode.HTML)
            return
        finally:
            if path and os.path.exists(path):
                with suppress(OSError):
                    os.remove(path)
            with suppress(OSError):
                os.rmdir(temp_dir)
        await message.reply_text("Bot profile photo updated.")

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
                "Use /addfile &lt;file_id&gt; [label], /addfile &lt;type&gt; &lt;file_id&gt; [label], or reply to media with /addfile."
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
        if caption_command_is(message, "importdb"):
            await import_db_command(client, rt, message)
            return
        await track_user(rt, message.from_user)

    @app.on_message(filters.command("delfile"))
    async def del_file_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        if not args:
            await message.reply_text("Use /delfile &lt;file_id&gt;.")
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
            await message.reply_text("Use /delfsub &lt;chat_id|@username&gt;.")
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
            await message.reply_text("Use /addsudo &lt;user_id&gt;.")
            return
        try:
            user_id = int(args[0])
        except ValueError:
            await message.reply_text("User ID must be numeric.")
            return
        rt.db.ensure_user_record(user_id)
        rt.db.add_sudo(user_id, message.from_user.id)
        await message.reply_text(f"Added sudo user <code>{user_id}</code>.", parse_mode=ParseMode.HTML)

    @app.on_message(filters.command("delsudo"))
    async def del_sudo_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        if not args:
            await message.reply_text("Use /delsudo &lt;user_id&gt;.")
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
        lines = []
        for user_id in sudos:
            stats = rt.db.get_user_stats(user_id)
            if stats:
                lines.append(f"- <code>{user_id}</code> | {user_label(stats)}")
            else:
                lines.append(f"- <code>{user_id}</code> | {user_text_mention(user_id)}")
        text = "\n".join(lines) or "No sudo users configured."
        await message.reply_text(
            "<b>Sudo users</b>\n\n" + text,
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("addpriv"))
    async def add_privileged_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        if not args:
            await message.reply_text("Use /addpriv &lt;user_id&gt;.")
            return
        try:
            user_id = int(args[0])
        except ValueError:
            await message.reply_text("User ID must be numeric.")
            return
        rt.db.ensure_user_record(user_id)
        rt.db.add_privileged(user_id, message.from_user.id)
        await message.reply_text(
            f"Added unlimited user {user_text_mention(user_id)}.",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("delpriv"))
    async def del_privileged_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        if not args:
            await message.reply_text("Use /delpriv &lt;user_id&gt;.")
            return
        try:
            user_id = int(args[0])
        except ValueError:
            await message.reply_text("User ID must be numeric.")
            return
        removed = rt.db.remove_privileged(user_id)
        await message.reply_text("Unlimited user removed." if removed else "That user was not unlimited.")

    @app.on_message(filters.command("privs"))
    async def privileged_users_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        privileged = rt.db.list_privileged()
        lines = []
        for user_id in privileged:
            stats = rt.db.get_user_stats(user_id)
            if stats:
                lines.append(f"- <code>{user_id}</code> | {user_label(stats)}")
            else:
                lines.append(f"- <code>{user_id}</code> | {user_text_mention(user_id)}")
        await message.reply_text(
            "<b>Unlimited users</b>\n\n" + ("\n".join(lines) if lines else "No unlimited users configured."),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("user"))
    async def user_command(client: Client, message: Message) -> None:
        await track_user(rt, message.from_user)
        if not await require_sudo(rt, message):
            return
        args = command_args(message)
        if not args:
            await message.reply_text("Use /user &lt;user_id&gt;.")
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
                f"Bonus requests: <b>{rt.db.bonus_requests(stats.user_id)}</b>\n"
                f"Effective limit: <b>{user_limit_text(rt, stats.user_id)}</b>\n"
                f"Sudo: <b>{'yes' if is_sudo_user(rt, stats.user_id) else 'no'}</b>\n"
                f"Unlimited: <b>{'yes' if rt.db.is_privileged(stats.user_id) else 'no'}</b>\n"
                f"Referrals: <b>{rt.db.referral_count(stats.user_id)}</b>\n"
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
        args = [arg.lower() for arg in command_args(message)]
        if "-full" in args or "full" in args:
            fmt = "json" if "-json" in args or "json" in args else "txt"
            if "-txt" in args or "txt" in args:
                fmt = "txt"
            await send_full_users_export(client, rt, message, fmt)
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
            await message.reply_text("Use /broadcast &lt;text&gt;.")
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
