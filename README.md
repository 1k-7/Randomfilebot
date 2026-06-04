# Random File Telegram Bot

A Telegram bot that sends a random indexed file by Telegram `file_id`, supports refresh buttons, force-subscribe channels/chats, sudo admins, rate limiting, and usage analytics.

## Setup

1. Create a bot with BotFather and copy its token.
2. Install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and set:

   - `BOT_TOKEN`: your Telegram bot token.
   - `OWNER_ID`: your Telegram numeric user ID.
   - `DATABASE_PATH`: optional SQLite path.
   - `REQUEST_LIMIT`: optional, defaults to `30`.
   - `REQUEST_WINDOW_MINUTES`: optional, defaults to `60`.

4. Run the bot:

   ```powershell
   python -m random_file_bot
   ```

## Docker

Build the image:

```powershell
docker build -t random-file-bot .
```

Run it with your token and owner ID:

```powershell
docker run -d --name random-file-bot `
  -e BOT_TOKEN=123456:replace-me `
  -e OWNER_ID=123456789 `
  -v random-file-bot-data:/data `
  random-file-bot
```

## User Commands

- `/start` - open the bot and request a random file.
- `/random` or `/get` - request a random file.
- `/stats` - show your usage stats.

Every sent file includes a refresh button. Pressing it edits the same message with another random file when Telegram allows that media edit.

## Owner and Sudo Commands

The owner is configured with `OWNER_ID`. Sudo users have the same bot-management permissions.

- `/admin` - usage dashboard.
- `/addfile <file_id> [label]` - add a Telegram file ID as a document.
- `/addfile <type> <file_id> [label]` - add a typed file ID. Types: `document`, `photo`, `video`, `audio`, `animation`.
- `/addfile` as a reply to a document/video/audio/photo/animation message - index that media.
- `/delfile <file_id>` - remove a file ID.
- `/files` - show indexed file count and recent entries.
- `/users` - list recent users.
- `/blocked` - list users marked as having blocked the bot.
- `/membership` - show recent join request and membership updates.
- `/broadcast <text>` - send a text broadcast to known active users.
- `/addsudo <user_id>` - grant sudo access.
- `/delsudo <user_id>` - revoke sudo access.
- `/sudos` - list sudo users.
- `/addfsub <chat_id|@username> [invite_link] [title]` - require membership in a chat/channel.
- `/delfsub <chat_id|@username>` - remove a force-sub chat.
- `/fsubs` - list force-sub chats.
- `/user <user_id>` - inspect a user's usage and status.

## Force Subscribe Notes

Add the bot as admin in each force-sub channel/chat. For private channels, use the numeric chat ID and make sure the bot can call `getChatMember`. The bot checks current membership before serving files, so users who leave required chats lose access until they rejoin.

If a required chat uses join requests, approve the user in Telegram or through your moderation setup. The bot listens for membership updates and join requests when Telegram sends them, but access is ultimately enforced by live membership checks.

## File IDs

Telegram `file_id`s are bot-specific enough that you should index media using this bot where possible. Replying to a media message with `/addfile` is the easiest path.
