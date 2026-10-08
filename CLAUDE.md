# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this bot does

ArmyBot is a Discord bot for the LP Army server. `/channel-list` lists all text channels a given role can access, grouped by category, with emoji indicators for view-only vs view & send permissions. The giveaway feature (`giveaway.py`) collects Solana wallets from eligible role holders for a gacha card giveaway, then collects feedback from the recipients.

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

`/channel-list` lives in `bot.py`. The command flow is:

1. **`channel_list`** — the slash command handler. Defers the response immediately (required for commands that may take time), then calls helpers and sends output as one or more followup messages.
2. **`get_channel_lines`** — collects all text channels the role can view, groups them by category (sorted by position), skips separator categories, and returns a flat list of formatted strings ready to concatenate.
3. **`chunk_messages`** — splits the body lines into chunks under 1500 characters and prepends the header to the first chunk. The 1500-char limit (below Discord's 2000-char cap) is intentional — Discord followup ephemeral messages fail to render channel mentions (`<#id>`) when content is too large.
4. **`is_separator`** — filters out decorative Discord category names (e.g. `➖➖➖➖`) that contain no alphanumeric characters.

## Giveaway (`giveaway.py`)

Loaded as an extension from `setup_hook` in `bot.py`. All `/giveaway-*` commands are admin-only (`default_permissions(manage_guild=True)`), so regular members never see them. Members interact only through panel buttons that admins post into role-locked channels.

- **Flow:** `/giveaway-start` (gacha role, optional first eligible role, cap default 500) posts the wallet panel (**Submit wallet** + **Check Wallet**, which shows the member their saved wallet) in the current channel. `/giveaway-feedback` closes the wallet panel and posts a feedback panel in the channel it is run in. `/giveaway-close` removes the panel button. `/giveaway-stats` and `/giveaway-export` read the data. The export DMs the CSV to the admin and saves a copy in `data/exports/`; it only falls back to an ephemeral attachment if the DM fails, because ephemeral messages vanish when Discord reloads and iOS can't preview CSVs in them.
- **Eligible roles:** each form (`wallet`, `feedback`) has its own unlimited role list in settings (`wallet_role_ids`, `feedback_role_ids`). `/giveaway-role-add` and `/giveaway-role-remove` edit it: with a `role` option they change one role, without it they open an ephemeral `RoleEditorView` that lists every role (A to Z, excluding @everyone and integration-managed roles) in dropdowns of 25, 4 per page, with ◀ ▶ paging past 100 roles. Roles on the form start ticked, Save writes the ticked set via `replace_roles`. It exists because Discord's built-in RoleSelect only preloads part of the list and relies on search. Single-role edits go through `apply_role_change`. `/giveaway-roles` shows the list. Every summary also shows the gacha role and its member count. Every reply includes member counts per role (`role_summary`), fetched from `GET /guilds/{id}/roles/member-counts`, which needs no privileged intent. Without the members intent the total is shown as a range (largest role to sum), because overlap between roles is unknown. With `MEMBERS_INTENT=1` it is an exact unique count. The feedback list starts as just the gacha role. The live panel lists the roles and is redrawn on change. Roles deleted from the server are pruned when a summary is built.
- **Storage:** `GiveawayStore` wraps SQLite at `data/armybot.db` (gitignored). One row per Discord user holds their wallet and feedback, so the CSV export is "the sheet". Settings (phase, roles, cap, panel message location) live in the `settings` table, not `.env`.
- **Rules:** wallets must decode as 32-byte base58 (Solana). One wallet per user (resubmitting replaces it without using a slot). A wallet can't belong to two users. The cap counts users with a wallet. Feedback requires a role from the feedback list.
- **Persistent buttons:** `WalletView`/`FeedbackView` use `timeout=None` and fixed `custom_id`s (`giveaway:wallet`, `giveaway:mywallet`, `giveaway:feedback`) and are registered in `cog_load`. Do not change those IDs or panels already posted stop working.
- **Cap race safety:** `submit_wallet` does the count check and insert with no `await` in between, so concurrent submissions can't exceed the cap. Keep it synchronous.
- **Role grant:** the bot needs Manage Roles and its top role must be above the gacha role. `/giveaway-start` checks this, and `gacha_role_problem` also refuses a gacha role the invoker could not assign by hand (needs Manage Roles and a higher role, or server owner) or one carrying moderation permissions, since every submitter receives it. A failed role grant still keeps the wallet and tells the member a mod will add the role.
- CSV cells from user input go through `csv_safe` to block spreadsheet formula injection.

## Key constraints to keep in mind

- **Do not use `>` (blockquote) syntax** before channel mentions. Discord does not render `<#channel_id>` as a clickable mention inside blockquote lines in ephemeral followup messages. Use `- ` bullet list prefix instead.
- **Chunk size is 1500**, not 2000. Keep it at or below 1500 to avoid mention rendering failures.
- **`await interaction.response.defer()`** must be called before any async work. All replies after that must use `interaction.followup.send()`, not `interaction.response.send_message()`.
- Intents: `guilds` only by default. The privileged `members` intent is opt-in via `MEMBERS_INTENT=1` in `.env` and only adds exact unique totals to `/giveaway-roles`. Only set it after Discord has enabled the Server Members intent for the app, otherwise login fails and launchd restarts the bot in a loop. Do not enable `message_content`; it is not needed.

## Deployment

Runs 24/7 on a Mac mini as a launchd LaunchAgent labelled `com.apex.army-bot`, checked out at `~/army-bot` with a virtualenv in `.venv`. The template lives in `deploy/com.apex.army-bot.plist` (`__HOME__` is substituted at install time). launchd starts it at login, restarts it on crash, and writes output to `~/army-bot/logs/stdout.log` and `stderr.log`. `DISCORD_TOKEN` lives in `~/army-bot/.env` on the Mac mini and is never committed (`.env`, `logs/` and `.venv/` are gitignored).

Pushing to `main` does **not** auto-deploy. To ship: `cd ~/army-bot && git pull && launchctl kickstart -k gui/$(id -u)/com.apex.army-bot`. See `DEPLOY.md` for full setup, logs, and stop/restart commands.

Output is unbuffered (`PYTHONUNBUFFERED=1` in the plist) so `print` calls reach the log file immediately. Keep that if you change how the bot is launched.
