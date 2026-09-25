# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this bot does

ArmyBot is a Discord bot with a single slash command (`/channel-list`) that lists all text channels a given role can access, grouped by category, with emoji indicators for view-only vs view & send permissions.

## Running the bot

```bash
# Install dependencies into the project virtualenv
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Run
.venv/bin/python bot.py
```

Requires a `.env` file in the project root with:
```
DISCORD_TOKEN=your-token-here
```

Slash commands sync automatically on startup via `bot.tree.sync()` in `on_ready`. Changes to command signatures may take up to a minute to reflect in Discord.

## Architecture

Everything lives in `bot.py`. The command flow is:

1. **`channel_list`** — the slash command handler. Defers the response immediately (required for commands that may take time), then calls helpers and sends output as one or more followup messages.
2. **`get_channel_lines`** — collects all text channels the role can view, groups them by category (sorted by position), skips separator categories, and returns a flat list of formatted strings ready to concatenate.
3. **`chunk_messages`** — splits the body lines into chunks under 1500 characters and prepends the header to the first chunk. The 1500-char limit (below Discord's 2000-char cap) is intentional — Discord followup ephemeral messages fail to render channel mentions (`<#id>`) when content is too large.
4. **`is_separator`** — filters out decorative Discord category names (e.g. `➖➖➖➖`) that contain no alphanumeric characters.

## Key constraints to keep in mind

- **Do not use `>` (blockquote) syntax** before channel mentions. Discord does not render `<#channel_id>` as a clickable mention inside blockquote lines in ephemeral followup messages. Use `- ` bullet list prefix instead.
- **Chunk size is 1500**, not 2000. Keep it at or below 1500 to avoid mention rendering failures.
- **`await interaction.response.defer()`** must be called before any async work. All replies after that must use `interaction.followup.send()`, not `interaction.response.send_message()`.
- The bot only needs the `guilds` intent. Do not enable `message_content` or `members` privileged intents — they are not needed and require additional approval from Discord.

## Deployment

Runs 24/7 on a Mac mini as a launchd LaunchAgent labelled `com.apex.army-bot`, checked out at `~/army-bot` with a virtualenv in `.venv`. The template lives in `deploy/com.apex.army-bot.plist` (`__HOME__` is substituted at install time). launchd starts it at login, restarts it on crash, and writes output to `~/army-bot/logs/stdout.log` and `stderr.log`. `DISCORD_TOKEN` lives in `~/army-bot/.env` on the Mac mini and is never committed (`.env`, `logs/` and `.venv/` are gitignored).

Pushing to `main` does **not** auto-deploy. To ship: `cd ~/army-bot && git pull && launchctl kickstart -k gui/$(id -u)/com.apex.army-bot`. See `DEPLOY.md` for full setup, logs, and stop/restart commands.

Output is unbuffered (`PYTHONUNBUFFERED=1` in the plist) so `print` calls reach the log file immediately. Keep that if you change how the bot is launched.
