# Random File Telegram Bot

A Telegram bot built on Pyroblack/Pyrogram that sends a random indexed file by Telegram `file_id`, supports refresh buttons, force-subscribe channels/chats, sudo admins, rate limiting, and usage analytics.

## Setup

1. Create a bot with BotFather and copy its token.
2. Create Telegram API credentials at `https://my.telegram.org` and copy your `API_ID` and `API_HASH`.
3. Install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

   `TgCrypto` is included because Pyroblack/Pyrogram downloads are much slower without its native crypto speedup.

4. Copy `.env.example` to `.env` and set:

   - `BOT_TOKEN`: your Telegram bot token.
   - `API_ID`: your Telegram API ID from `my.telegram.org`.
   - `API_HASH`: your Telegram API hash from `my.telegram.org`.
   - `OWNER_ID`: your Telegram numeric user ID.
   - `DATABASE_PATH`: optional SQLite path.
   - `SESSION_NAME`: optional Pyroblack session name.
   - `SESSION_WORKDIR`: optional directory for the Pyroblack session file.
   - `MAX_CONCURRENT_TRANSMISSIONS`: optional Pyroblack transfer concurrency, defaults to `4`.
   - `REQUEST_LIMIT`: optional, defaults to `30`.
   - `REQUEST_WINDOW_MINUTES`: optional, defaults to `60`.

5. Run the bot:

   ```powershell
   python -m random_file_bot
   ```

## Docker

Build the image:

```powershell
docker build -t random-file-bot .
```

Run it with your token, API credentials, and owner ID:

```powershell
docker run -d --name random-file-bot `
  -e BOT_TOKEN=123456:replace-me `
  -e API_ID=123456 `
  -e API_HASH=replace-me `
  -e OWNER_ID=123456789 `
  -e MAX_CONCURRENT_TRANSMISSIONS=4 `
  -v random-file-bot-data:/data `
  random-file-bot
```

## User Commands

- `/start` - open the bot and request a random file.
- `/random` or `/get` - request a random file.
- `/stats` - show your usage stats.

Every sent file includes a refresh button. Pressing it edits the same message with another random file when Telegram allows that media edit.

## Inline Mode

Enable inline mode for the bot in BotFather first. Then typing `@YourBotUsername` with no query text shows one result: `Send a random video from vault`. Selecting it sends a random cached video from the indexed vault.

Inline usage uses the same request limit as `/random` and refresh clicks.

## Owner and Sudo Commands

The owner is configured with `OWNER_ID`. Sudo users have the same bot-management permissions.

- `/admin` - usage dashboard.
- `/addfile <file_id> [label]` - add a Telegram file ID as a document.
- `/addfile <type> <file_id> [label]` - add a typed file ID. Types: `document`, `photo`, `video`, `audio`, `animation`.
- `/addfile` as a reply to a document/video/audio/photo/animation message - index that media.
- `/importjson` as a reply to a JSON document - import file IDs from JSON.
- `/importjson replace` as a reply to a JSON document - replace the indexed-file DB from JSON.
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

JSON imports accept either one object or a list of objects. `_id` is used as the Telegram file ID, and `caption` or `file_name` becomes the label:

Large JSON files are downloaded to a temporary file first, with progress shown in Telegram, then parsed and written to SQLite. Progress message edits are scheduled in the background so they do not block the download loop.

```json
{
  "_id": "BQACAgQAAyEFAAS_JUKvAAMeahiOUkOLWJblmwQl8K-xcXrghiMAAgYZAAJnLchTXODWcRkTgbkeBA",
  "file_name": "Forbidden Desire 03 Crazydad3D zip",
  "file_size": 53713452,
  "caption": "Forbidden Desire 03 [Crazydad3D] zip"
}
```
