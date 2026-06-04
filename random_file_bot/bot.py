from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from html import escape

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaAnimation,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Config, load_config
from .db import Database, utcnow
from .models import ForceSubChat, IndexedFile, UserStats


REFRESH_CALLBACK = "refresh_file"
SUPPORTED_FILE_TYPES = {"document", "photo", "video", "audio", "animation"}
MEMBER_STATUSES = {
    "creator",
    "administrator",
    "member",
}


class BotRuntime:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db


def runtime(context: ContextTypes.DEFAULT_TYPE) -> BotRuntime:
    return context.application.bot_data["runtime"]


def is_private(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")


def sleek_title(user_name: str | None) -> str:
    name = escape(user_name or "there")
    return f"<b>Welcome, {name}.</b>\n\nI pick one file at random from the vault. Tap refresh whenever you want another draw."


def refresh_keyboard(file_db_id: int | None = None) -> InlineKeyboardMarkup:
    callback_data = REFRESH_CALLBACK if file_db_id is None else f"{REFRESH_CALLBACK}:{file_db_id}"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Refresh file", callback_data=callback_data)]]
    )


def force_sub_keyboard(chats: list[ForceSubChat]) -> InlineKeyboardMarkup | None:
    buttons: list[list[InlineKeyboardButton]] = []
    for chat in chats:
        title = chat.title or str(chat.chat_id)
        if chat.invite_link:
            buttons.append([InlineKeyboardButton(f"Join {short_text(title, 48)}", url=chat.invite_link)])
    buttons.append([InlineKeyboardButton("I joined", callback_data=REFRESH_CALLBACK)])
    return InlineKeyboardMarkup(buttons) if buttons else None


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


def status_value(status) -> str:
    return str(getattr(status, "value", status))


def user_label(stats: UserStats) -> str:
    if stats.username:
        return f"@{escape(stats.username)}"
    full_name = " ".join(part for part in [stats.first_name, stats.last_name] if part)
    return escape(full_name) if full_name else str(stats.user_id)


async def require_sudo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    rt = runtime(context)
    if user.id == rt.config.owner_id or rt.db.is_sudo(user.id):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("This panel is only for the owner and sudo users.")
    return False


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        runtime(context).db.upsert_user(user, blocked=False)


async def missing_force_sub_chats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> list[ForceSubChat]:
    user = update.effective_user
    if not user:
        return []
    rt = runtime(context)
    missing: list[ForceSubChat] = []
    for chat in rt.db.list_force_sub_chats():
        try:
            member = await context.bot.get_chat_member(chat.chat_id, user.id)
            status = status_value(member.status)
            if status in MEMBER_STATUSES:
                continue
            if status == "restricted" and getattr(member, "is_member", False):
                continue
            else:
                missing.append(chat)
        except TelegramError:
            missing.append(chat)
    return missing


async def enforce_force_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    missing = await missing_force_sub_chats(update, context)
    if not missing:
        return True
    user = update.effective_user
    if user:
        runtime(context).db.record_denied(user.id, "denied_fsub")

    text = (
        "<b>One step before the vault opens.</b>\n\n"
        "Join the required chat first, then tap <b>I joined</b> to continue."
    )
    required = "\n".join(f"- {escape(chat.title or chat.chat_id)}" for chat in missing)
    if required:
        text += f"\n\n<b>Required</b>\n{required}"
    markup = force_sub_keyboard(missing)
    if update.callback_query:
        await update.callback_query.answer("Join the required chat first.", show_alert=True)
        await edit_query_text_or_caption(update, text, markup)
    elif update.effective_message:
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    return False


async def enforce_rate_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    rt = runtime(context)
    since = utcnow() - timedelta(seconds=rt.config.request_window_seconds)
    used = rt.db.requests_since(user.id, since)
    if used < rt.config.request_limit:
        return True
    rt.db.record_denied(user.id, "denied_rate_limit")
    minutes = max(1, rt.config.request_window_seconds // 60)
    text = (
        "<b>You have reached the hourly draw limit.</b>\n\n"
        f"You can request {rt.config.request_limit} files every {minutes} minutes. "
        "Try again a little later."
    )
    if update.callback_query:
        await update.callback_query.answer("Rate limit reached. Try again later.", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)
    return False


async def send_or_edit_random_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    event_type: str,
    exclude_file_id: int | None = None,
) -> None:
    user = update.effective_user
    if not user:
        return
    rt = runtime(context)
    item = rt.db.random_file(exclude_id=exclude_file_id)
    if not item:
        await reply_or_edit_text(
            update,
            "<b>The vault is empty.</b>\n\nAn admin needs to index at least one file first.",
        )
        return

    title = escape(short_text(item.label, 900)) if item.label else "Your random file is ready."
    caption = f"<b>{title}</b>\nNeed another one? Tap refresh."
    if update.callback_query:
        await update.callback_query.answer("Drawing another file...")
        try:
            await edit_message_media(update, item, caption)
            rt.db.record_file_event(user.id, item.id, event_type)
            return
        except BadRequest:
            try:
                await update.callback_query.message.delete()
            except TelegramError:
                pass
            await send_file_message(update, context, item, caption)
            rt.db.record_file_event(user.id, item.id, event_type)
            return
    await send_file_message(update, context, item, caption)
    rt.db.record_file_event(user.id, item.id, event_type)


async def reply_or_edit_text(update: Update, text: str) -> None:
    if update.callback_query:
        await update.callback_query.answer()
        await edit_query_text_or_caption(update, text, refresh_keyboard())
        return
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def edit_query_text_or_caption(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> None:
    query = update.callback_query
    if not query:
        return
    try:
        await query.edit_message_caption(
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except BadRequest:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


async def edit_message_media(update: Update, item: IndexedFile, caption: str) -> None:
    query = update.callback_query
    if not query:
        return
    media = input_media_for(item, caption)
    await query.edit_message_media(
        media=media,
        reply_markup=refresh_keyboard(item.id),
    )


def input_media_for(item: IndexedFile, caption: str):
    kwargs = {"media": item.file_id, "caption": caption, "parse_mode": ParseMode.HTML}
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
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    item: IndexedFile,
    caption: str,
) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else update.effective_user.id
    kwargs = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": ParseMode.HTML,
        "reply_markup": refresh_keyboard(item.id),
    }
    if item.file_type == "photo":
        await context.bot.send_photo(photo=item.file_id, **kwargs)
    elif item.file_type == "video":
        await context.bot.send_video(video=item.file_id, **kwargs)
    elif item.file_type == "audio":
        await context.bot.send_audio(audio=item.file_id, **kwargs)
    elif item.file_type == "animation":
        await context.bot.send_animation(animation=item.file_id, **kwargs)
    else:
        await context.bot.send_document(document=item.file_id, **kwargs)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not is_private(update):
        return
    user = update.effective_user
    if update.effective_message and user:
        await update.effective_message.reply_text(
            sleek_title(user.first_name),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Draw a file", callback_data=REFRESH_CALLBACK)]]
            ),
        )


async def random_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not await enforce_force_sub(update, context):
        return
    if not await enforce_rate_limit(update, context):
        return
    await send_or_edit_random_file(update, context, event_type="request")


async def refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not await enforce_force_sub(update, context):
        return
    if not await enforce_rate_limit(update, context):
        return
    exclude_file_id = current_file_id(update.callback_query.data if update.callback_query else None)
    await send_or_edit_random_file(
        update,
        context,
        event_type="refresh" if exclude_file_id is not None else "request",
        exclude_file_id=exclude_file_id,
    )


def current_file_id(callback_data: str | None) -> int | None:
    if not callback_data or ":" not in callback_data:
        return None
    try:
        return int(callback_data.rsplit(":", 1)[1])
    except ValueError:
        return None


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    user = update.effective_user
    if not user or not update.effective_message:
        return
    stats = runtime(context).db.get_user_stats(user.id)
    if not stats:
        return
    await update.effective_message.reply_text(
        (
            "<b>Your usage</b>\n\n"
            f"Files sent: <b>{stats.files_sent}</b>\n"
            f"Refreshes: <b>{stats.refresh_count}</b>\n"
            f"Denied attempts: <b>{stats.denied_count}</b>\n"
            f"Last request: <b>{format_dt(stats.last_request_at)}</b>"
        ),
        parse_mode=ParseMode.HTML,
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not await require_sudo(update, context):
        return
    rt = runtime(context)
    totals = rt.db.total_stats()
    top = rt.db.top_users(5)
    top_lines = "\n".join(
        f"{idx}. {user_label(user)} - {user.request_count} requests"
        for idx, user in enumerate(top, start=1)
    ) or "No users yet."
    await update.effective_message.reply_text(
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


async def add_file_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not await require_sudo(update, context):
        return
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    media = extract_replied_media(message)
    if media:
        file_id, file_type, label = media
    elif context.args:
        if context.args[0].lower() in SUPPORTED_FILE_TYPES and len(context.args) >= 2:
            file_type = context.args[0].lower()
            file_id = context.args[1]
            label = " ".join(context.args[2:]) or None
        else:
            file_id = context.args[0]
            file_type = "document"
            label = " ".join(context.args[1:]) or None
    else:
        await message.reply_text(
            "Use /addfile <file_id> [label], /addfile <type> <file_id> [label], or reply to media with /addfile."
        )
        return

    runtime(context).db.add_file(file_id, file_type, label, user.id)
    await message.reply_text(
        f"Indexed <b>{escape(file_type)}</b> file <code>{escape(compact_file_id(file_id))}</code>.",
        parse_mode=ParseMode.HTML,
    )


def extract_replied_media(message) -> tuple[str, str, str | None] | None:
    reply = message.reply_to_message
    if not reply:
        return None
    if reply.document:
        return reply.document.file_id, "document", reply.document.file_name
    if reply.video:
        return reply.video.file_id, "video", reply.video.file_name
    if reply.audio:
        return reply.audio.file_id, "audio", reply.audio.file_name or reply.audio.title
    if reply.animation:
        return reply.animation.file_id, "animation", reply.animation.file_name
    if reply.photo:
        return reply.photo[-1].file_id, "photo", None
    return None


async def del_file_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not await require_sudo(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Use /delfile <file_id>.")
        return
    removed = runtime(context).db.remove_file(context.args[0])
    await update.effective_message.reply_text("File removed." if removed else "That file was not indexed.")


async def files_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not await require_sudo(update, context):
        return
    db = runtime(context).db
    rows = db.recent_files(10)
    lines = [
        f"{row.id}. {escape(row.file_type)} - <code>{escape(compact_file_id(row.file_id))}</code>"
        for row in rows
    ]
    await update.effective_message.reply_text(
        (
            f"<b>Indexed files:</b> {db.count_files()}\n\n"
            + ("\n".join(lines) if lines else "No files indexed yet.")
        ),
        parse_mode=ParseMode.HTML,
    )


async def add_fsub_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not await require_sudo(update, context):
        return
    message = update.effective_message
    if not message:
        return
    if not context.args:
        await message.reply_text("Use /addfsub <chat_id|@username> [invite_link] [title].")
        return
    chat_id = context.args[0]
    tail = context.args[1:]
    manual_invite = None
    title_parts: list[str] = []
    for part in tail:
        maybe_link = normalize_invite_link(part)
        if maybe_link and not manual_invite:
            manual_invite = maybe_link
        else:
            title_parts.append(part)
    custom_title = " ".join(title_parts) or None
    title = custom_title
    invite_link = manual_invite
    try:
        chat = await context.bot.get_chat(chat_id)
        title = custom_title or chat.title or chat.username or str(chat.id)
        invite_link = invite_link or chat.invite_link
        if not invite_link and chat.username:
            invite_link = f"https://t.me/{chat.username}"
    except TelegramError:
        if chat_id.startswith("@"):
            invite_link = f"https://t.me/{chat_id[1:]}"
        pass
    runtime(context).db.add_force_sub_chat(chat_id, title, invite_link)
    await message.reply_text(
        f"Force-sub enabled for <b>{escape(title or chat_id)}</b>.",
        parse_mode=ParseMode.HTML,
    )


async def del_fsub_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not await require_sudo(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Use /delfsub <chat_id|@username>.")
        return
    removed = runtime(context).db.remove_force_sub_chat(context.args[0])
    await update.effective_message.reply_text(
        "Force-sub chat removed." if removed else "That chat was not configured."
    )


async def fsubs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not await require_sudo(update, context):
        return
    chats = runtime(context).db.list_force_sub_chats(enabled_only=False)
    if not chats:
        await update.effective_message.reply_text("No force-sub chats configured.")
        return
    lines = [
        f"- <b>{escape(chat.title or chat.chat_id)}</b> | <code>{escape(chat.chat_id)}</code>"
        for chat in chats
    ]
    await update.effective_message.reply_text(
        "<b>Force-sub chats</b>\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def add_sudo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not await require_sudo(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Use /addsudo <user_id>.")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("User ID must be numeric.")
        return
    runtime(context).db.add_sudo(user_id, update.effective_user.id)
    await update.effective_message.reply_text(f"Added sudo user <code>{user_id}</code>.", parse_mode=ParseMode.HTML)


async def del_sudo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    rt = runtime(context)
    if not await require_sudo(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Use /delsudo <user_id>.")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("User ID must be numeric.")
        return
    if user_id == rt.config.owner_id:
        await update.effective_message.reply_text("The owner cannot be removed from sudo access.")
        return
    removed = rt.db.remove_sudo(user_id)
    await update.effective_message.reply_text("Sudo user removed." if removed else "That user was not sudo.")


async def sudos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not await require_sudo(update, context):
        return
    sudos = runtime(context).db.list_sudos()
    lines = "\n".join(f"- <code>{user_id}</code>" for user_id in sudos)
    await update.effective_message.reply_text(
        "<b>Sudo users</b>\n\n" + lines,
        parse_mode=ParseMode.HTML,
    )


async def user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not await require_sudo(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Use /user <user_id>.")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("User ID must be numeric.")
        return
    stats = runtime(context).db.get_user_stats(user_id)
    if not stats:
        await update.effective_message.reply_text("No record for that user.")
        return
    await update.effective_message.reply_text(
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


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not await require_sudo(update, context):
        return
    users = runtime(context).db.recent_users(15)
    if not users:
        await update.effective_message.reply_text("No users logged yet.")
        return
    lines = [
        f"- <code>{user.user_id}</code> | {user_label(user)} | seen {format_dt(user.first_seen)}"
        for user in users
    ]
    await update.effective_message.reply_text(
        "<b>Recent users</b>\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def blocked_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not await require_sudo(update, context):
        return
    users = runtime(context).db.blocked_users(15)
    if not users:
        await update.effective_message.reply_text("No blocked users logged.")
        return
    lines = [
        f"- <code>{user.user_id}</code> | {user_label(user)} | marked {format_dt(user.last_seen)}"
        for user in users
    ]
    await update.effective_message.reply_text(
        "<b>Blocked users</b>\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def membership_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not await require_sudo(update, context):
        return
    events = runtime(context).db.recent_membership_events(15)
    if not events:
        await update.effective_message.reply_text("No membership events logged yet.")
        return
    lines = [
        (
            f"- <code>{event.user_id}</code> in <code>{escape(event.chat_id)}</code> "
            f"-> <b>{escape(event.status)}</b> at {format_dt(event.created_at)}"
        )
        for event in events
    ]
    await update.effective_message.reply_text(
        "<b>Recent membership events</b>\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)
    if not await require_sudo(update, context):
        return
    message = update.effective_message
    if not message:
        return
    text = message.text.partition(" ")[2].strip() if message.text else ""
    if not text:
        await message.reply_text("Use /broadcast <text>.")
        return
    db = runtime(context).db
    sent = 0
    blocked = 0
    for user_id in db.active_user_ids():
        try:
            await context.bot.send_message(user_id, text)
            sent += 1
            await asyncio.sleep(0.05)
        except Forbidden:
            db.mark_user_blocked(user_id, True)
            blocked += 1
        except TelegramError:
            continue
    await message.reply_text(f"Broadcast sent to {sent} users. Marked {blocked} blocked.")


async def track_regular_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user(update, context)


async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    event = update.my_chat_member
    if not event:
        return
    user = event.from_user
    runtime(context).db.upsert_user(user, blocked=False)
    new_status = status_value(event.new_chat_member.status)
    if event.chat.type == "private" and new_status in {"kicked", "left"}:
        runtime(context).db.mark_user_blocked(user.id, True)


async def chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    event = update.chat_member
    if not event:
        return
    chat_id = str(event.chat.id)
    user_id = event.new_chat_member.user.id
    status = status_value(event.new_chat_member.status)
    runtime(context).db.record_membership_event(user_id, chat_id, status)


async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    request = update.chat_join_request
    if not request:
        return
    runtime(context).db.record_membership_event(
        request.from_user.id,
        str(request.chat.id),
        "join_request",
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Update failed", exc_info=context.error)


def format_dt(value) -> str:
    if not value:
        return "never"
    return value.strftime("%Y-%m-%d %H:%M UTC")


def build_application(config: Config, db: Database) -> Application:
    application = Application.builder().token(config.bot_token).build()
    application.bot_data["runtime"] = BotRuntime(config, db)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler(["random", "get"], random_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("blocked", blocked_command))
    application.add_handler(CommandHandler("membership", membership_command))
    application.add_handler(CommandHandler("addfile", add_file_command))
    application.add_handler(CommandHandler("delfile", del_file_command))
    application.add_handler(CommandHandler("files", files_command))
    application.add_handler(CommandHandler("addfsub", add_fsub_command))
    application.add_handler(CommandHandler("delfsub", del_fsub_command))
    application.add_handler(CommandHandler("fsubs", fsubs_command))
    application.add_handler(CommandHandler("addsudo", add_sudo_command))
    application.add_handler(CommandHandler("delsudo", del_sudo_command))
    application.add_handler(CommandHandler("sudos", sudos_command))
    application.add_handler(CommandHandler("user", user_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CallbackQueryHandler(refresh_callback, pattern=f"^{REFRESH_CALLBACK}(:\\d+)?$"))
    application.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(ChatMemberHandler(chat_member, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(ChatJoinRequestHandler(join_request))
    application.add_handler(MessageHandler(filters.ALL, track_regular_message))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    config = load_config()
    db = Database(config.database_path)
    db.init(config.owner_id)
    application = build_application(config, db)
    application.run_polling(
        allowed_updates=[
            "message",
            "callback_query",
            "my_chat_member",
            "chat_member",
            "chat_join_request",
        ]
    )
