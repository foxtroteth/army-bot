# Deploying ArmyBot on the Mac mini

ArmyBot runs 24/7 on the Mac mini as a launchd LaunchAgent (`com.apex.army-bot`), the same way the other bots on that machine are managed. launchd starts it at login, restarts it if it crashes (30s throttle), and writes its output to `~/Projects/army-bot/logs/`.

## Setup from scratch

The bot lives in `~/Projects/army-bot`. On the Mac mini, `~/army-bot` is a symlink to it, kept for old notes and scripts.

```bash
# 1. Clone and create the virtualenv
git clone https://github.com/foxtroteth/army-bot.git ~/Projects/army-bot
cd ~/Projects/army-bot
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p logs

# 2. Create .env (gitignored) and put the real token in it
printf 'DISCORD_TOKEN=PASTE_YOUR_TOKEN_HERE\n' > .env
chmod 600 .env
open -e .env   # replace the placeholder, save

# 3. Optional: test in the foreground, wait for "Logged in as ...", then Ctrl-C
.venv/bin/python bot.py

# 4. Install the LaunchAgent (fills in your home directory) and start it
sed "s|__HOME__|$HOME|g" deploy/com.apex.army-bot.plist > ~/Library/LaunchAgents/com.apex.army-bot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.apex.army-bot.plist
```

Optional: if Discord approves the **Server Members Intent** for the app, add `MEMBERS_INTENT=1` to `.env` and restart to get exact unique-member totals in `/giveaway-roles`. Never set it before the intent is enabled, or the bot can't log in.

The Mac must be set to never sleep and to restart after a power failure (System Settings > Energy, or `sudo pmset -a sleep 0 autorestart 1`). LaunchAgents only start once the user is logged in, so for the bot to come back after a power cut with nobody at the keyboard, automatic login must be enabled (System Settings > Users & Groups > Automatically log in as). macOS refuses automatic login while FileVault is on, and a FileVault Mac waits at the unlock screen after a power loss, so unattended recovery needs FileVault off. For planned reboots with FileVault on, `sudo fdesetup authrestart` reboots without stopping at the unlock screen.

## Updating after a push to main

```bash
cd ~/Projects/army-bot && git pull
launchctl kickstart -k gui/$(id -u)/com.apex.army-bot
```

If `requirements.txt` changed, run `.venv/bin/pip install -r requirements.txt` before the restart. If `deploy/com.apex.army-bot.plist` changed, re-run the `sed` line from step 4, then `bootout` and `bootstrap` (below).

## Checking status and logs

```bash
launchctl print gui/$(id -u)/com.apex.army-bot | grep -E 'state|pid|last exit'
tail -f ~/Projects/army-bot/logs/stdout.log ~/Projects/army-bot/logs/stderr.log
```

`stdout.log` has the bot's own prints (for example `Logged in as ...`). `stderr.log` has discord.py's logging and any tracebacks. The logs are not rotated; truncate them with `: > ~/Projects/army-bot/logs/stderr.log` if they get large.

## Giveaway data

Wallets and feedback are stored in `~/Projects/army-bot/data/armybot.db` (gitignored, never pushed). Export it from Discord with `/giveaway-export`. To back it up, copy the file: `cp ~/Projects/army-bot/data/armybot.db ~/armybot-backup-$(date +%F).db`. To start a fresh giveaway later, stop the bot, move that file away, then start the bot again.

## Stop, start, restart

```bash
# Restart (kills and relaunches)
launchctl kickstart -k gui/$(id -u)/com.apex.army-bot

# Stop and unload (stays stopped until bootstrapped again)
launchctl bootout gui/$(id -u)/com.apex.army-bot

# Load and start again
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.apex.army-bot.plist
```

`launchctl kill TERM ...` alone will not keep it stopped, because `KeepAlive` relaunches it. Use `bootout` to stop it for real.
