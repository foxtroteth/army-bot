# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this bot does

ArmyBot is a Discord bot with two slash commands:

- **`/channel-list`** — lists all text channels a given role can access, grouped by category, with emoji indicators for view-only vs view & send permissions.
- **`/member-export`** — exports everyone who currently has a given role, along with the date they were given it, as a CSV attachment or as a text list in chat.

## Running the bot

```bash
# Install dependencies
pip3 install -r requirements.txt

# Run
python3 bot.py
```

Requires a `.env` file in the project root with:
```
DISCORD_TOKEN=your-token-here
```

Slash commands sync automatically on startup via `bot.tree.sync()` in `on_ready`. Changes to command signatures may take up to a minute to reflect in Discord.

The bot also needs, in the Discord Developer Portal → your app → Bot → Privileged Gateway Intents:

- **Server Members Intent** — on. Without it the bot cannot log in at all (it exits with a message pointing here), because `/member-export` reads `role.members`, which is only populated when that intent is enabled.

And in the server itself, the bot's role needs **View Audit Log**. Without that permission `/member-export` still lists members, but every grant date comes back unknown.

## Architecture

Everything lives in `bot.py`. Both commands defer the response immediately (required for commands that may take time), then call helpers and send output as followup messages.

`/channel-list`:

1. **`channel_list`** — the slash command handler.
2. **`get_channel_lines`** — collects all text channels the role can view, groups them by category (sorted by position), skips separator categories, and returns a flat list of formatted strings ready to concatenate.
3. **`is_separator`** — filters out decorative Discord category names (e.g. `➖➖➖➖`) that contain no alphanumeric characters.

`/member-export`:

1. **`member_export`** — the slash command handler. Chunks the guild first if the member cache is cold, then branches on the `format` option: a `discord.File` CSV attachment, or text lines sent through `chunk_messages`.
2. **`get_role_grants`** — scans the guild audit log for `member_role_update` entries and maps member ID → (grant time, who granted it). Entries arrive newest first, so the **first** "role added" entry seen for a member is the grant still in effect; members are dropped from the pending set as they're found so the scan can stop early.
3. **`sort_members`** — orders by grant time, oldest first, with unknown dates last.
4. **`build_csv`** / **`get_member_lines`** — render the two output formats.
5. **`csv_safe`** — prefixes `'` to values starting with `=`, `+`, `-`, or `@` so a hostile display name isn't run as a formula by Excel/Sheets.
6. **`safe_filename`** — strips non-`[\w.-]` characters out of the role name for the attachment filename.

Shared:

- **`chunk_messages`** — splits the body lines into chunks under 1500 characters and prepends the header to the first chunk. The 1500-char limit (below Discord's 2000-char cap) is intentional — Discord followup ephemeral messages fail to render channel mentions (`<#id>`) when content is too large.

## Key constraints to keep in mind

- **Do not use `>` (blockquote) syntax** before channel mentions. Discord does not render `<#channel_id>` as a clickable mention inside blockquote lines in ephemeral followup messages. Use `- ` bullet list prefix instead.
- **Chunk size is 1500**, not 2000. Keep it at or below 1500 to avoid mention rendering failures.
- **`await interaction.response.defer()`** must be called before any async work. All replies after that must use `interaction.followup.send()`, not `interaction.response.send_message()`.
- The bot needs the `guilds` and `members` intents. `members` is privileged and exists solely so `/member-export` can read `role.members` — Discord offers no other way to list a role's members. Do **not** enable `message_content`; nothing needs it.
- **Role grant dates only come from the audit log**, which Discord retains for roughly 90 days. There is no API field for "when did this member get this role". Anyone who got the role before that window shows an empty/unknown date, and that is expected, not a bug. `AUDIT_LOG_SCAN_LIMIT` caps how far back the scan goes.
- **Pass `allowed_mentions=NO_PINGS`** on anything `/member-export` sends. Role names and display names are user-controlled — a role named `everyone` would otherwise mass-ping the server.

## Deployment

Deployed on Railway. It auto-deploys on every push to `main`. The `DISCORD_TOKEN` environment variable is set in Railway's Variables tab — it is not committed to the repo.
