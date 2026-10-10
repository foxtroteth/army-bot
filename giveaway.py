"""Gacha card giveaway: collect Solana wallets from eligible members, then feedback.

There are two wallet giveaways (1 and 2), each with its own eligible roles, cap, panel and
banner. A member can hold one wallet across both: whoever submitted in one can't submit in the
other. Giveaway 1 keeps the original setting keys and button IDs; giveaway 2 uses `_2` keys.

Setup and posting are separate: /giveaway-setup only changes settings (and redraws a live
panel), /giveaway-post posts the panel. Posting again moves the panel: the old one is turned
into a pointer, so there is only ever one live panel per giveaway.

Phase "off":      never set up.
Phase "ready":    set up with /giveaway-setup, panel not posted yet.
Phase "wallet":   members with an eligible role submit a wallet (capped), get the gacha role.
Phase "feedback": (giveaway 1 slot only) wallet panels close; a feedback panel is posted.
Phase "closed":   the panel loses its button.

Everything is stored in a local SQLite file (data/armybot.db) and exported as CSV.
"""

import csv
import io
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands
from discord.http import Route

DB_PATH = Path(__file__).parent / "data" / "armybot.db"
EXPORT_DIR = Path(__file__).parent / "data" / "exports"
IMAGE_DIR = Path(__file__).parent / "data" / "panel-images"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
DEFAULT_CAP = 500
GIVEAWAYS = (1, 2)
GiveawayNo = Literal[1, 2]
FORMS = ("wallet", "wallet2", "feedback")
FORM_LABELS = {"wallet": "Giveaway 1 wallet", "wallet2": "Giveaway 2 wallet", "feedback": "Feedback"}
FORM_CHOICES = [app_commands.Choice(name=label, value=form) for form, label in FORM_LABELS.items()]


def key(name: str, giveaway: int) -> str:
    """Setting key for a giveaway: giveaway 1 keeps the original names, giveaway 2 adds `_2`."""
    return name if giveaway == 1 else f"{name}_2"


def wallet_form(giveaway: int) -> str:
    return "wallet" if giveaway == 1 else "wallet2"

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def is_valid_solana_address(address: str) -> bool:
    # A Solana address is a 32-byte public key encoded in base58 (32-44 chars)
    if not 32 <= len(address) <= 44:
        return False
    num = 0
    for ch in address:
        idx = _B58_ALPHABET.find(ch)
        if idx == -1:
            return False
        num = num * 58 + idx
    leading_zeros = len(address) - len(address.lstrip("1"))
    return leading_zeros + (num.bit_length() + 7) // 8 == 32


def csv_safe(value) -> str:
    # Stop Sheets/Excel from treating user text like "=HYPERLINK(...)" as a formula
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def iso(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if ts else ""


class GiveawayStore:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS submissions (
                user_id      INTEGER PRIMARY KEY,
                username     TEXT NOT NULL,
                wallet       TEXT UNIQUE,
                roles        TEXT NOT NULL DEFAULT '',
                submitted_at INTEGER,
                updated_at   INTEGER,
                worked       TEXT,
                feedback     TEXT,
                feedback_at  INTEGER
            );
            """
        )
        # Migration: rows from before giveaway 2 existed all belong to giveaway 1
        columns = [r["name"] for r in self.db.execute("PRAGMA table_info(submissions)")]
        if "giveaway" not in columns:
            self.db.execute("ALTER TABLE submissions ADD COLUMN giveaway INTEGER NOT NULL DEFAULT 1")
        self.db.commit()

    # --- settings ---

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def save(self, **values) -> None:
        self.db.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, None if v is None else str(v)) for k, v in values.items()],
        )
        self.db.commit()

    def phase(self, giveaway: int = 1) -> str:
        return self.get(key("phase", giveaway)) or "off"

    def cap(self, giveaway: int = 1) -> int:
        return int(self.get(key("cap", giveaway)) or DEFAULT_CAP)

    @property
    def gacha_role_id(self) -> int | None:
        value = self.get("gacha_role_id")
        return int(value) if value else None

    def role_ids(self, form: str) -> list[int]:
        # Roles allowed to use a form ("wallet", "wallet2" or "feedback"); no limit on how many
        return [int(r) for r in (self.get(f"{form}_role_ids") or "").split(",") if r]

    def set_role_ids(self, form: str, ids: list[int]) -> None:
        self.save(**{f"{form}_role_ids": ",".join(str(i) for i in dict.fromkeys(ids))})

    # --- submissions ---

    def wallet_count(self, giveaway: int | None = None) -> int:
        """Wallets in one giveaway, or across both when giveaway is None."""
        if giveaway is None:
            return self.db.execute("SELECT COUNT(*) FROM submissions WHERE wallet IS NOT NULL").fetchone()[0]
        return self.db.execute(
            "SELECT COUNT(*) FROM submissions WHERE wallet IS NOT NULL AND giveaway = ?", (giveaway,)
        ).fetchone()[0]

    def get_submission(self, user_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM submissions WHERE user_id = ?", (user_id,)).fetchone()

    def submit_wallet(self, user_id: int, username: str, wallet: str, roles: str, giveaway: int = 1) -> str:
        """Returns 'new', 'updated', 'unchanged', 'full', 'duplicate' or 'other_giveaway'.

        One wallet per member across both giveaways: a member with a wallet in the other
        giveaway gets 'other_giveaway'. No awaits happen in here, so the cap check and the
        insert can't interleave with another submission on the bot's event loop.
        """
        now = int(time.time())
        existing = self.get_submission(user_id)
        if existing and existing["wallet"] and existing["giveaway"] != giveaway:
            return "other_giveaway"

        owner = self.db.execute("SELECT user_id FROM submissions WHERE wallet = ?", (wallet,)).fetchone()
        if owner and owner["user_id"] != user_id:
            return "duplicate"

        if existing and existing["wallet"]:
            if existing["wallet"] == wallet:
                return "unchanged"
            # Updating an existing wallet doesn't use another slot
            self.db.execute(
                "UPDATE submissions SET wallet = ?, username = ?, roles = ?, updated_at = ? WHERE user_id = ?",
                (wallet, username, roles, now, user_id),
            )
            self.db.commit()
            return "updated"

        if self.wallet_count(giveaway) >= self.cap(giveaway):
            return "full"

        self.db.execute(
            "INSERT INTO submissions (user_id, username, wallet, roles, submitted_at, updated_at, giveaway) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET wallet = excluded.wallet, username = excluded.username, "
            "roles = excluded.roles, submitted_at = excluded.submitted_at, updated_at = excluded.updated_at, "
            "giveaway = excluded.giveaway",
            (user_id, username, wallet, roles, now, now, giveaway),
        )
        self.db.commit()
        return "new"

    def submit_feedback(self, user_id: int, username: str, worked: str, feedback: str) -> bool:
        """Saves feedback; returns True if it replaced earlier feedback from this user."""
        now = int(time.time())
        existing = self.get_submission(user_id)
        self.db.execute(
            "INSERT INTO submissions (user_id, username, worked, feedback, feedback_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET worked = excluded.worked, feedback = excluded.feedback, "
            "feedback_at = excluded.feedback_at",
            (user_id, username, worked, feedback, now),
        )
        self.db.commit()
        return bool(existing and existing["feedback_at"])

    def stats(self, giveaway: int | None = None) -> dict:
        """Wallet stats for one giveaway (or both when None); feedback counts are always overall."""
        where, args = ("AND giveaway = ?", (giveaway,)) if giveaway else ("", ())
        row = self.db.execute(
            f"SELECT COUNT(*) AS wallets, MAX(submitted_at) AS last_wallet_at FROM submissions "
            f"WHERE wallet IS NOT NULL {where}", args
        ).fetchone()
        fb = self.db.execute(
            "SELECT COUNT(feedback_at) AS feedback, MAX(feedback_at) AS last_feedback_at FROM submissions"
        ).fetchone()
        last = self.db.execute(
            f"SELECT user_id, submitted_at FROM submissions WHERE wallet IS NOT NULL {where} "
            "ORDER BY submitted_at DESC LIMIT 1", args
        ).fetchone()
        by_role: dict[str, int] = {}
        for (roles,) in self.db.execute(f"SELECT roles FROM submissions WHERE wallet IS NOT NULL {where}", args):
            for name in filter(None, roles.split(", ")):
                by_role[name] = by_role.get(name, 0) + 1
        return {**dict(row), **dict(fb), "last_user_id": last["user_id"] if last else None, "by_role": by_role}

    def export_csv(self) -> str:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(
            ["discord_id", "username", "wallet", "giveaway", "eligible_roles", "submitted_at",
             "updated_at", "everything_worked", "bugs_comments", "feedback_at"]
        )
        for r in self.db.execute(
            "SELECT * FROM submissions ORDER BY submitted_at IS NULL, giveaway, submitted_at"
        ):
            writer.writerow(
                [str(r["user_id"]), csv_safe(r["username"]), r["wallet"] or "",
                 r["giveaway"] if r["wallet"] else "", csv_safe(r["roles"]),
                 iso(r["submitted_at"]), iso(r["updated_at"]), csv_safe(r["worked"]),
                 csv_safe(r["feedback"]), iso(r["feedback_at"])]
            )
        return out.getvalue()


# --- Discord UI ---


def role_list(ids: list[int], limit: int = 30) -> str:
    if not ids:
        return "_nobody yet_"
    shown = ", ".join(f"<@&{i}>" for i in ids[:limit])
    return shown + (f" and {len(ids) - limit} more" if len(ids) > limit else "")


def panel_embed(store: GiveawayStore, giveaway: int = 1) -> discord.Embed:
    count, cap, phase = store.wallet_count(giveaway), store.cap(giveaway), store.phase(giveaway)
    # Only mention the cross-giveaway rule once a second giveaway exists
    both = store.phase(2) != "off"
    if phase == "wallet":
        full = count >= cap
        embed = discord.Embed(
            title="🎴 Free Gacha Card Giveaway" + (" #2" if giveaway == 2 else ""),
            description=(
                "Click **Submit wallet** and paste your **Solana wallet address**.\n"
                f"Open to: {role_list(store.role_ids(wallet_form(giveaway)))}\n"
                + ("One wallet per person across both giveaways, " if both else "One wallet per person, ")
                + "you can resubmit to fix a typo. Click **Check Wallet** to see what you submitted.\n\n"
                + ("**All spots are taken.**" if full else f"**{count} / {cap}** spots claimed")
            ),
            color=discord.Color.red() if full else discord.Color.gold(),
        )
    elif phase == "feedback":
        embed = discord.Embed(
            title="📝 Gacha Card Feedback",
            description=(
                "Cards are out! If you received one, click **Give feedback** and tell us "
                "whether everything worked or if you ran into any bugs.\n\n"
                f"Open to: {role_list(store.role_ids('feedback'))}"
            ),
            color=discord.Color.blurple(),
        )
    else:
        embed = discord.Embed(
            title="🎴 Gacha Card Giveaway",
            description="This giveaway is closed. Thanks everyone!",
            color=discord.Color.dark_grey(),
        )
    return embed


class WalletModal(discord.ui.Modal, title="Submit your Solana wallet"):
    def __init__(self, cog: "Giveaway", current: str | None, giveaway: int = 1):
        super().__init__()
        self.cog, self.giveaway = cog, giveaway
        self.wallet = discord.ui.TextInput(
            label="Solana wallet address", min_length=32, max_length=44,
            placeholder="e.g. 7xKX...", default=current,
        )
        self.add_item(self.wallet)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_wallet_submit(interaction, self.wallet.value.strip(), self.giveaway)


class FeedbackModal(discord.ui.Modal, title="Gacha card feedback"):
    worked = discord.ui.TextInput(
        label="Did everything work?", max_length=100, placeholder="Yes / No / Mostly",
    )
    feedback = discord.ui.TextInput(
        label="Bugs or comments", style=discord.TextStyle.paragraph, max_length=1000, required=False,
        placeholder="Anything that broke, confused you, or that you liked",
    )

    def __init__(self, cog: "Giveaway"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_feedback_submit(interaction, self.worked.value.strip(), self.feedback.value.strip())


class WalletView(discord.ui.View):
    """Submit wallet + Check Wallet for one giveaway.

    timeout=None + fixed custom_ids keep the buttons working after bot restarts. Giveaway 1
    keeps the original IDs (panels already posted use them); giveaway 2 adds a `:2` suffix.
    """

    def __init__(self, cog: "Giveaway", giveaway: int = 1):
        super().__init__(timeout=None)
        self.cog, self.giveaway = cog, giveaway
        suffix = "" if giveaway == 1 else f":{giveaway}"
        submit = discord.ui.Button(label="Submit wallet", emoji="💳", style=discord.ButtonStyle.success,
                                   custom_id=f"giveaway:wallet{suffix}")
        check = discord.ui.Button(label="Check Wallet", emoji="🔍", style=discord.ButtonStyle.secondary,
                                  custom_id=f"giveaway:mywallet{suffix}")
        submit.callback = self.on_submit
        check.callback = self.on_check
        self.add_item(submit)
        self.add_item(check)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_wallet_button(interaction, self.giveaway)

    async def on_check(self, interaction: discord.Interaction):
        await self.cog.handle_check_wallet(interaction)


class FeedbackView(discord.ui.View):
    def __init__(self, cog: "Giveaway"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Give feedback", emoji="📝", style=discord.ButtonStyle.primary,
                       custom_id="giveaway:feedback")
    async def give(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_feedback_button(interaction)


# Permissions a giveaway reward role must never carry: anyone who submits a wallet gets it
_DANGEROUS_PERMS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks",
    "manage_messages", "ban_members", "kick_members", "moderate_members", "mention_everyone",
)


def gacha_role_problem(invoker: discord.Member, role: discord.Role) -> str | None:
    """Why this role can't be the gacha role, or None if it's fine.

    The bot hands this role to every submitter, so it must be a role the admin
    could assign by hand and must not grant moderation powers.
    """
    if role.is_default() or role.managed:
        return f"{role.mention} can't be assigned (it's @everyone or managed by an integration)."
    risky = [p for p in _DANGEROUS_PERMS if getattr(role.permissions, p)]
    if risky:
        return (f"{role.mention} has elevated permissions ({', '.join(risky)}). "
                "Pick a plain role with no moderation permissions.")
    if invoker.id != invoker.guild.owner_id and not (
        invoker.guild_permissions.manage_roles and role < invoker.top_role
    ):
        return f"You need Manage Roles and a role above {role.mention} to make it the giveaway role."
    return None


class RoleEditorView(discord.ui.View):
    """Ephemeral editor listing every server role across several dropdowns.

    Discord's built-in role picker only preloads a few dozen roles and relies on search,
    so this lists them all: 25 per dropdown, 4 dropdowns per page (row 5 holds the
    buttons), with pages when a server has more than 100 roles. Roles already on the
    form start ticked; Save writes the ticked set back.
    """

    PER_MENU, MENUS_PER_PAGE = 25, 4

    def __init__(self, cog: "Giveaway", guild: discord.Guild, form: str, counts: dict[int, int]):
        super().__init__(timeout=600)
        self.cog, self.form, self.counts = cog, form, counts
        # Skip @everyone and bot/integration roles; nobody "joins" those
        self.roles = sorted((r for r in guild.roles if not r.is_default() and not r.managed),
                            key=lambda r: r.name.lower())
        self.selected = set(cog.store.role_ids(form))
        self.page = 0
        self.message: discord.WebhookMessage | None = None
        self.render()

    @property
    def page_size(self) -> int:
        return self.PER_MENU * self.MENUS_PER_PAGE

    @property
    def pages(self) -> int:
        return max(1, -(-len(self.roles) // self.page_size))

    def render(self):
        self.clear_items()
        start = self.page * self.page_size
        page_roles = self.roles[start:start + self.page_size]
        for row, i in enumerate(range(0, len(page_roles), self.PER_MENU)):
            chunk = page_roles[i:i + self.PER_MENU]
            menu = discord.ui.Select(
                placeholder=f"{chunk[0].name[:40]} … {chunk[-1].name[:40]}",
                min_values=0, max_values=len(chunk), row=row,
                options=[discord.SelectOption(
                    label=r.name[:100], value=str(r.id), default=r.id in self.selected,
                    description=f"{self.counts.get(r.id, '?')} members",
                ) for r in chunk],
            )
            menu.callback = self.make_menu_callback(menu, {r.id for r in chunk})
            self.add_item(menu)

        if self.pages > 1:
            prev = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, row=4,
                                     disabled=self.page == 0)
            nxt = discord.ui.Button(label=f"▶ ({self.page + 1}/{self.pages})", style=discord.ButtonStyle.secondary,
                                    row=4, disabled=self.page >= self.pages - 1)
            prev.callback, nxt.callback = self.make_page_callback(-1), self.make_page_callback(1)
            self.add_item(prev)
            self.add_item(nxt)
        save = discord.ui.Button(label="Save", emoji="💾", style=discord.ButtonStyle.success, row=4)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, row=4)
        save.callback, cancel.callback = self.on_save, self.on_cancel
        self.add_item(save)
        self.add_item(cancel)

    def make_menu_callback(self, menu: discord.ui.Select, chunk_ids: set[int]):
        async def callback(interaction: discord.Interaction):
            # Each dropdown owns its slice of roles: replace that slice with what's ticked now
            self.selected -= chunk_ids
            self.selected |= {int(v) for v in menu.values}
            for option in menu.options:
                option.default = int(option.value) in self.selected
            await interaction.response.defer()
        return callback

    def make_page_callback(self, step: int):
        async def callback(interaction: discord.Interaction):
            self.page = min(max(self.page + step, 0), self.pages - 1)
            self.render()
            await interaction.response.edit_message(view=self)
        return callback

    async def on_save(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.defer()
        note = self.cog.replace_roles(self.form, self.selected)
        await self.cog.after_role_change(self.form)
        await interaction.edit_original_response(
            content=note, embed=await self.cog.role_summary(interaction.guild, self.form), view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_cancel(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="Cancelled, nothing changed.", embed=None, view=None)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="⏱️ Role editor expired, nothing saved. Run the command again.",
                                        embed=None, view=None)
            except discord.HTTPException:
                pass


def can_export(perms: discord.Permissions) -> bool:
    return perms.administrator or perms.manage_guild


def export_outsiders(channel: discord.abc.GuildChannel) -> list[str]:
    """Who can read this channel but couldn't run /giveaway-export themselves.

    The export lists every wallet, so it may only be posted where all readers are admins
    already. Bot/integration roles are ignored. Returns mentions/labels; empty means safe.
    """
    outsiders = []
    for role in channel.guild.roles:
        if role.managed or can_export(role.permissions):
            continue
        if channel.permissions_for(role).view_channel:
            outsiders.append("@everyone" if role.is_default() else role.mention)
    for target, overwrite in channel.overwrites.items():
        if isinstance(target, discord.Role) or not overwrite.view_channel:
            continue
        # Member-specific access: only fine if we can see that member is an admin
        if not (isinstance(target, discord.Member) and (target.bot or can_export(target.guild_permissions))):
            outsiders.append(f"<@{target.id}>")
    return outsiders


class PostExportView(discord.ui.View):
    """Lets the admin knowingly post an export in a channel some non-admins can read."""

    def __init__(self, data: bytes, name: str, summary: str):
        super().__init__(timeout=600)
        self.data, self.name, self.summary = data, name, summary

    @discord.ui.button(label="Post here anyway", emoji="📦", style=discord.ButtonStyle.danger)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button):
        missing = missing_post_perms(interaction.channel, with_file=True)
        if missing:
            await interaction.response.send_message(
                f"❌ I can't post here. Give me: **{', '.join(missing)}**. Use the file above instead.",
                ephemeral=True,
            )
            return
        self.stop()
        await interaction.response.edit_message(view=None)
        await post_export(interaction, self.data, self.name, self.summary)


async def post_export(interaction: discord.Interaction, data: bytes, name: str, summary: str):
    """Posts the export as the bot's own message (no "X used /command" header above it)."""
    await interaction.channel.send(
        f"📦 Giveaway export by {interaction.user.mention}. {summary}",
        file=discord.File(io.BytesIO(data), filename=name),
        allowed_mentions=discord.AllowedMentions.none(),
    )


def missing_post_perms(channel: discord.abc.GuildChannel, with_file: bool = False) -> list[str]:
    """Permissions the bot lacks to post a panel/export as its own message in this channel."""
    perms = channel.permissions_for(channel.guild.me)
    needed = {"View Channel": perms.view_channel, "Send Messages": perms.send_messages,
              "Embed Links": perms.embed_links}
    if with_file:
        needed["Attach Files"] = perms.attach_files
    return [name for name, ok in needed.items() if not ok]


def admin_command(**kwargs):
    # Only visible to members with Manage Server by default (adjustable in Server Settings > Integrations)
    def wrap(func):
        func = app_commands.default_permissions(manage_guild=True)(func)
        func = app_commands.guild_only()(func)
        return app_commands.command(**kwargs)(func)
    return wrap


class Giveaway(commands.Cog):
    def __init__(self, bot: commands.Bot, store: GiveawayStore):
        self.bot = bot
        self.store = store

    async def cog_load(self):
        for giveaway in GIVEAWAYS:
            self.bot.add_view(WalletView(self, giveaway))
        self.bot.add_view(FeedbackView(self))

    # --- helpers ---

    def member_roles(self, member: discord.Member, form: str) -> list[discord.Role]:
        """The member's roles that are allowed to use this form."""
        allowed = set(self.store.role_ids(form))
        return [r for r in member.roles if r.id in allowed]

    async def role_counts(self, guild: discord.Guild) -> dict[int, int]:
        # Works without the privileged members intent (unlike role.members)
        try:
            data = await self.bot.http.request(
                Route("GET", "/guilds/{guild_id}/roles/member-counts", guild_id=guild.id)
            )
            return {int(k): v for k, v in data.items()}
        except discord.HTTPException as e:
            print(f"[giveaway] could not fetch role member counts: {e}")
            return {}

    async def role_summary(self, guild: discord.Guild, form: str) -> discord.Embed:
        # Roles deleted from the server can't be picked in /giveaway-role-remove, so drop them here
        ids = self.store.role_ids(form)
        live = [i for i in ids if guild.get_role(i)]
        if len(live) != len(ids):
            self.store.set_role_ids(form, live)
        counts = await self.role_counts(guild)
        lines = [f"- <@&{i}>: **{counts.get(i, '?')}** members" for i in live]
        body = "\n".join(lines) or "_No roles yet. Add one with /giveaway-role-add._"
        if len(live) != len(ids):
            body += f"\n\n_Removed {len(ids) - len(live)} role(s) that were deleted from the server._"
        if len(body) > 3800:  # embed description limit is 4096
            body = body[:3800].rsplit("\n", 1)[0] + "\n- ..."

        embed = discord.Embed(
            title=f"{'📝' if form == 'feedback' else '💳'} {FORM_LABELS[form]} form: eligible roles",
            description=body,
            color=discord.Color.blurple() if form == "feedback" else discord.Color.gold(),
        )
        embed.add_field(name="Roles", value=str(len(live)))
        per_role = [counts.get(i, 0) for i in live]
        if self.bot.intents.members and guild.chunked:
            # Exact: union of members across roles, each person counted once
            unique = {m.id for i in live for m in guild.get_role(i).members}
            embed.add_field(name="Unique members", value=f"**{len(unique)}**")
        elif len(live) <= 1:
            embed.add_field(name="Total members", value=f"**{sum(per_role)}**")
        else:
            # Without the members intent we only have per-role counts, so overlap is unknown
            embed.add_field(name="Total members", value=f"**{max(per_role)} to {sum(per_role)}**")
            embed.set_footer(text="Range because members with several roles can't be de-duplicated "
                                  "without the Server Members intent.")

        # The reward role, shown alongside every form's role list
        gacha_id = self.store.gacha_role_id
        if not gacha_id:
            gacha = "_Not set yet. Run /giveaway-setup with a gacha_role._"
        elif guild.get_role(gacha_id) is None:
            gacha = "⚠️ The gacha role was deleted. Pick a new one with /giveaway-setup gacha_role:."
        else:
            gacha = (f"<@&{gacha_id}>: **{counts.get(gacha_id, '?')}** members "
                     "(given to everyone who submits a wallet in either giveaway)")
        embed.add_field(name="🎴 Gacha role", value=gacha, inline=False)
        return embed

    def current_view(self, giveaway: int = 1) -> discord.ui.View | None:
        # Closed panels lose their button entirely (view=None removes components)
        phase = self.store.phase(giveaway)
        if phase == "wallet":
            return WalletView(self, giveaway)
        if phase == "feedback":
            return FeedbackView(self)
        return None

    def panel_content(self, giveaway: int = 1) -> tuple[discord.Embed, list[discord.File]]:
        """The panel embed plus its banner image file, if one was set with /giveaway-setup."""
        embed = panel_embed(self.store, giveaway)
        name = self.store.get(key("panel_image", giveaway))
        if name and (IMAGE_DIR / name).is_file():
            embed.set_image(url=f"attachment://{name}")
            return embed, [discord.File(IMAGE_DIR / name, filename=name)]
        return embed, []

    async def post_panel(self, interaction: discord.Interaction, view: discord.ui.View,
                         giveaway: int = 1) -> discord.Message | None:
        """Posts the panel as the bot's own message (no "X used /command" header above it)."""
        embed, files = self.panel_content(giveaway)
        missing = missing_post_perms(interaction.channel, with_file=bool(files))
        if missing:
            await interaction.followup.send(
                f"❌ I can't post in this channel. Give me: **{', '.join(missing)}** here, then run it again.",
                ephemeral=True,
            )
            return None
        message = await interaction.channel.send(embed=embed, view=view, files=files)
        self.store.save(**{key("panel_channel_id", giveaway): message.channel.id,
                           key("panel_message_id", giveaway): message.id})
        return message

    async def refresh_panel(self, giveaway: int = 1) -> bool:
        channel_id = self.store.get(key("panel_channel_id", giveaway))
        message_id = self.store.get(key("panel_message_id", giveaway))
        if not (channel_id and message_id):
            return False
        try:
            channel = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
            message = await channel.fetch_message(int(message_id))
            embed, files = self.panel_content(giveaway)
            if files:
                # Re-upload the banner: Discord's attachment links expire, a fresh upload never does
                await message.edit(embed=embed, view=self.current_view(giveaway), attachments=files)
            else:
                await message.edit(embed=embed, view=self.current_view(giveaway))
            return True
        except discord.HTTPException as e:
            print(f"[giveaway] could not update panel: {e}")
            return False

    # --- wallet phase ---

    async def handle_wallet_button(self, interaction: discord.Interaction, giveaway: int = 1):
        if self.store.phase(giveaway) != "wallet":
            await interaction.response.send_message("Wallet submissions are closed.", ephemeral=True)
            return
        existing = self.store.get_submission(interaction.user.id)
        if existing and existing["wallet"] and existing["giveaway"] != giveaway:
            await interaction.response.send_message(
                f"❌ You already submitted a wallet in Giveaway {existing['giveaway']}. "
                "It's one wallet per person, so you can't join this one too.", ephemeral=True
            )
            return
        if not self.member_roles(interaction.user, wallet_form(giveaway)):
            await interaction.response.send_message(
                "❌ Sorry, you don't have a role that's eligible for this giveaway.", ephemeral=True
            )
            return
        current = existing["wallet"] if existing else None
        if not current and self.store.wallet_count(giveaway) >= self.store.cap(giveaway):
            await interaction.response.send_message("❌ Sorry, all spots are taken.", ephemeral=True)
            return
        await interaction.response.send_modal(WalletModal(self, current, giveaway))

    async def handle_check_wallet(self, interaction: discord.Interaction):
        row = self.store.get_submission(interaction.user.id)
        if not row or not row["wallet"]:
            await interaction.response.send_message("You haven't submitted a wallet yet.", ephemeral=True)
            return
        changed = (f"\nLast changed <t:{row['updated_at']}:R>."
                   if row["updated_at"] != row["submitted_at"] else "")
        which = f" (Giveaway {row['giveaway']})" if self.store.phase(2) != "off" else ""
        await interaction.response.send_message(
            f"🔍 Your submitted wallet{which}:\n`{row['wallet']}`\n"
            f"Submitted <t:{row['submitted_at']}:f>.{changed}",
            ephemeral=True,
        )

    async def handle_wallet_submit(self, interaction: discord.Interaction, wallet: str, giveaway: int = 1):
        member = interaction.user
        # Re-check: phase, roles and cap may have changed while the modal was open
        roles = self.member_roles(member, wallet_form(giveaway))
        if self.store.phase(giveaway) != "wallet" or not roles:
            await interaction.response.send_message("❌ You can't submit a wallet right now.", ephemeral=True)
            return
        if not is_valid_solana_address(wallet):
            await interaction.response.send_message(
                "❌ That doesn't look like a valid Solana wallet address. Please check it and try again.",
                ephemeral=True,
            )
            return

        result = self.store.submit_wallet(member.id, str(member), wallet,
                                          ", ".join(r.name for r in roles), giveaway)
        if result == "other_giveaway":
            await interaction.response.send_message(
                "❌ You already submitted a wallet in the other giveaway. It's one wallet per person.",
                ephemeral=True,
            )
            return
        if result == "full":
            await interaction.response.send_message("❌ Sorry, all spots were just taken.", ephemeral=True)
            return
        if result == "duplicate":
            await interaction.response.send_message(
                "❌ That wallet was already submitted by someone else.", ephemeral=True
            )
            return

        role_note = await self.grant_gacha_role(member)
        messages = {
            "new": "✅ Wallet saved! You've been given the gacha role.",
            "updated": "✅ Wallet updated.",
            "unchanged": "✅ That's already your saved wallet.",
        }
        await interaction.response.send_message(
            f"{messages[result]}\n`{wallet}`{role_note}", ephemeral=True
        )
        if result == "new":
            await self.refresh_panel(giveaway)

    async def grant_gacha_role(self, member: discord.Member) -> str:
        role = member.guild.get_role(self.store.gacha_role_id or 0)
        if role is None:
            print("[giveaway] gacha role not found; was it deleted?")
            return "\n⚠️ Couldn't give you the role automatically, a mod will add it."
        if role in member.roles:
            return ""
        try:
            await member.add_roles(role, reason="Submitted giveaway wallet")
            return ""
        except discord.HTTPException as e:
            # Usually: bot lacks Manage Roles, or its role is below the gacha role
            print(f"[giveaway] could not give {role.name} to {member}: {e}")
            return "\n⚠️ Couldn't give you the role automatically, a mod will add it."

    # --- feedback phase ---

    async def handle_feedback_button(self, interaction: discord.Interaction):
        if self.store.phase(1) != "feedback":
            await interaction.response.send_message("Feedback is closed.", ephemeral=True)
            return
        if not self.member_roles(interaction.user, "feedback"):
            await interaction.response.send_message(
                "❌ Sorry, feedback is only open to giveaway participants.", ephemeral=True
            )
            return
        await interaction.response.send_modal(FeedbackModal(self))

    async def handle_feedback_submit(self, interaction: discord.Interaction, worked: str, feedback: str):
        if self.store.phase(1) != "feedback" or not self.member_roles(interaction.user, "feedback"):
            await interaction.response.send_message("❌ You can't send feedback right now.", ephemeral=True)
            return
        replaced = self.store.submit_feedback(interaction.user.id, str(interaction.user), worked, feedback)
        await interaction.response.send_message(
            "✅ Feedback updated, thanks!" if replaced else "✅ Thanks for the feedback!", ephemeral=True
        )

    # --- admin commands ---

    @admin_command(name="giveaway-setup",
                   description="Create or change a giveaway's settings (doesn't post anything)")
    @app_commands.describe(
        giveaway="Which giveaway (default 1)",
        gacha_role="Role given to everyone who submits a wallet (shared by both giveaways)",
        cap="Maximum number of wallets",
        image="Banner image shown in the panel (PNG, JPG, GIF or WebP, up to 8 MB)",
        remove_image="Remove the current banner image",
        eligible_role="Add one role allowed to submit (use /giveaway-role-add for several)",
    )
    async def giveaway_setup(
        self,
        interaction: discord.Interaction,
        giveaway: GiveawayNo = 1,
        gacha_role: discord.Role | None = None,
        cap: app_commands.Range[int, 1, 10000] | None = None,
        image: discord.Attachment | None = None,
        remove_image: bool = False,
        eligible_role: discord.Role | None = None,
    ):
        if gacha_role:
            me = interaction.guild.me
            if not me.guild_permissions.manage_roles or gacha_role >= me.top_role:
                await interaction.response.send_message(
                    f"❌ I can't assign {gacha_role.mention}. Give me **Manage Roles** and drag my role "
                    f"above {gacha_role.mention} in Server Settings > Roles, then run this again.",
                    ephemeral=True,
                )
                return
            problem = gacha_role_problem(interaction.user, gacha_role)
            if problem:
                await interaction.response.send_message(f"❌ {problem}", ephemeral=True)
                return
        elif not self.store.gacha_role_id:
            await interaction.response.send_message(
                "❌ Pick a `gacha_role` the first time you set up a giveaway.", ephemeral=True
            )
            return
        if image and not ((image.content_type or "").startswith("image/") and image.size <= MAX_IMAGE_BYTES):
            await interaction.response.send_message(
                "❌ The banner must be an image (PNG, JPG, GIF or WebP) of 8 MB or less.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        changes = []
        form = wallet_form(giveaway)
        if gacha_role and gacha_role.id != self.store.gacha_role_id:
            old = self.store.gacha_role_id
            self.store.save(gacha_role_id=gacha_role.id)
            changes.append(f"gacha role {'<@&%d> → ' % old if old else ''}{gacha_role.mention}"
                           + (" (for both giveaways)" if old else ""))
        if cap is not None and cap != self.store.cap(giveaway):
            count = self.store.wallet_count(giveaway)
            changes.append(f"cap {self.store.cap(giveaway)} → **{cap}**"
                           + (f" (already {count} wallets, so it's full; nobody is removed)" if count >= cap else ""))
            self.store.save(**{key("cap", giveaway): cap})
        if image:
            # Keep our own copy: the upload's CDN link expires, and panel edits re-attach it
            ext = Path(image.filename).suffix.lower() or ".png"
            image_name = f"wallet-panel{'' if giveaway == 1 else '-2'}{ext}"
            IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            await image.save(IMAGE_DIR / image_name)
            self.store.save(**{key("panel_image", giveaway): image_name})
            changes.append("new banner image")
        elif remove_image and self.store.get(key("panel_image", giveaway)):
            self.store.save(**{key("panel_image", giveaway): None})
            changes.append("banner removed")
        if eligible_role and eligible_role.id not in self.store.role_ids(form):
            self.store.set_role_ids(form, self.store.role_ids(form) + [eligible_role.id])
            changes.append(f"added eligible role {eligible_role.mention}")
        if not self.store.role_ids("feedback"):
            self.store.set_role_ids("feedback", [self.store.gacha_role_id])
        if self.store.phase(giveaway) == "off":
            self.store.save(**{key("phase", giveaway): "ready"})  # set up, not posted yet

        live = self.store.phase(giveaway) == "wallet"
        if live and changes:
            await self.refresh_panel(giveaway)
        status = (f"Its panel is live in <#{self.store.get(key('panel_channel_id', giveaway))}> and was updated."
                  if live else f"Not posted yet: run **/giveaway-post giveaway:{giveaway}** in the channel "
                               "where members should submit.")
        await interaction.followup.send(
            f"⚙️ **Giveaway {giveaway} settings** "
            + (f"changed: {'; '.join(changes)}. " if changes else "(nothing changed). ")
            + f"Cap {self.store.cap(giveaway)}, {self.store.wallet_count(giveaway)} wallets so far. {status}",
            embed=await self.role_summary(interaction.guild, form),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin_command(name="giveaway-post",
                   description="Post a giveaway's Submit wallet panel in this channel and open submissions")
    @app_commands.describe(giveaway="Which giveaway (default 1)")
    async def giveaway_post(self, interaction: discord.Interaction, giveaway: GiveawayNo = 1):
        phase = self.store.phase(giveaway)
        if phase == "off" or not self.store.gacha_role_id:
            await interaction.response.send_message(
                f"❌ Giveaway {giveaway} isn't set up yet. Run **/giveaway-setup giveaway:{giveaway}** first.",
                ephemeral=True,
            )
            return
        if giveaway == 1 and phase == "feedback":
            await interaction.response.send_message(
                "❌ Feedback is open in giveaway 1's panel slot. Close it with /giveaway-close first.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        old_channel = self.store.get(key("panel_channel_id", giveaway))
        old_message = self.store.get(key("panel_message_id", giveaway))

        self.store.save(**{key("phase", giveaway): "wallet"})
        message = await self.post_panel(interaction, WalletView(self, giveaway), giveaway)
        if not message:
            self.store.save(**{key("phase", giveaway): phase})  # nothing posted: keep the old state
            return
        moved = ""
        if old_channel and old_message and int(old_message) != message.id:
            # One live panel per giveaway: the previous one points here and loses its buttons
            if await self.retire_panel(int(old_channel), int(old_message), message):
                moved = f" The old panel in <#{old_channel}> now points here."
        roles = self.store.role_ids(wallet_form(giveaway))
        await interaction.followup.send(
            f"✅ Giveaway {giveaway} panel posted, submissions open.{moved}"
            + ("" if roles else " ⚠️ No eligible roles yet, so nobody can submit: add some with /giveaway-role-add."),
            ephemeral=True,
        )

    async def retire_panel(self, channel_id: int, message_id: int, new_message: discord.Message) -> bool:
        """Turns an old panel into a pointer to the new one (no buttons, no banner)."""
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            old = await channel.fetch_message(message_id)
            await old.edit(
                embed=discord.Embed(
                    title="🎴 This panel has moved",
                    description=f"Submit your wallet here instead: {new_message.jump_url}",
                    color=discord.Color.dark_grey(),
                ),
                view=None, attachments=[],
            )
            return True
        except discord.HTTPException as e:
            print(f"[giveaway] could not retire old panel {message_id}: {e}")  # usually already deleted
            return False

    @admin_command(name="giveaway-role-add",
                   description="Allow a role on the wallet or feedback form (leave role empty to edit the full list)")
    @app_commands.describe(form="Which form", role="One role to allow; leave empty to open the full role list")
    @app_commands.choices(form=FORM_CHOICES)
    async def giveaway_role_add(self, interaction: discord.Interaction, form: str,
                                role: discord.Role | None = None):
        await self.edit_roles(interaction, form, "add", role)

    @admin_command(name="giveaway-role-remove",
                   description="Remove a role from the wallet or feedback form (leave role empty to edit the full list)")
    @app_commands.describe(form="Which form", role="One role to remove; leave empty to open the full role list")
    @app_commands.choices(form=FORM_CHOICES)
    async def giveaway_role_remove(self, interaction: discord.Interaction, form: str,
                                   role: discord.Role | None = None):
        await self.edit_roles(interaction, form, "remove", role)

    async def edit_roles(self, interaction: discord.Interaction, form: str, action: str,
                         role: discord.Role | None):
        # Defer first: the panel edit and count lookup can take longer than Discord's 3s reply window
        await interaction.response.defer(ephemeral=True)
        if role:
            note = self.apply_role_change(form, action, [role.id])
            await self.after_role_change(form)
            await interaction.followup.send(
                note, embed=await self.role_summary(interaction.guild, form), ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        view = RoleEditorView(self, interaction.guild, form, await self.role_counts(interaction.guild))
        pages = f" Use ◀ ▶ to see all {len(view.roles)} roles." if view.pages > 1 else ""
        view.message = await interaction.followup.send(
            f"**Edit the {FORM_LABELS[form]} form roles.** Every role is listed A to Z; ticked roles are on the form. "
            f"Tick to add, untick to remove, then press **Save**.{pages}",
            view=view, ephemeral=True, wait=True,
        )

    def replace_roles(self, form: str, new_ids: set[int]) -> str:
        """Sets a form's roles to exactly new_ids (keeping existing order) and describes the change."""
        current = self.store.role_ids(form)
        added = [i for i in new_ids if i not in current]
        removed = [i for i in current if i not in new_ids]
        self.store.set_role_ids(form, [i for i in current if i in new_ids] + sorted(added))
        mentions = lambda ids: ", ".join(f"<@&{i}>" for i in ids)
        parts = []
        if added:
            parts.append(f"Added {mentions(added)}.")
        if removed:
            parts.append(f"Removed {mentions(removed)}.")
        if not parts:
            parts.append("Nothing changed.")
        if not new_ids:
            parts.append("⚠️ No roles left, so nobody can use this form now.")
        return " ".join(parts)

    def apply_role_change(self, form: str, action: str, role_ids: list[int]) -> str:
        """Adds or removes roles from a form's list and returns a note describing what changed."""
        current = self.store.role_ids(form)
        if action == "add":
            changed = [i for i in role_ids if i not in current]
            self.store.set_role_ids(form, current + changed)
            skipped, did, skip_text = [i for i in role_ids if i in current], "Added", "already on the list"
        else:
            changed = [i for i in role_ids if i in current]
            self.store.set_role_ids(form, [i for i in current if i not in role_ids])
            skipped, did, skip_text = [i for i in role_ids if i not in current], "Removed", "weren't on the list"
        mentions = lambda ids: ", ".join(f"<@&{i}>" for i in ids)
        parts = [f"{did} {mentions(changed)}." if changed else "Nothing changed."]
        if skipped:
            parts.append(f"Skipped ({skip_text}): {mentions(skipped)}.")
        if action == "remove" and not self.store.role_ids(form):
            parts.append("⚠️ No roles left, so nobody can use this form now.")
        return " ".join(parts)

    @admin_command(name="giveaway-roles", description="Show eligible roles and member counts for a form")
    @app_commands.describe(form="Which form (default: all in use)")
    @app_commands.choices(form=FORM_CHOICES)
    async def giveaway_roles(self, interaction: discord.Interaction, form: str | None = None):
        # Skip giveaway 2 until it's been set up
        forms = [form] if form else [f for f in FORMS
                                     if not (f == "wallet2" and self.store.phase(2) == "off"
                                             and not self.store.role_ids("wallet2"))]
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            embeds=[await self.role_summary(interaction.guild, f) for f in forms], ephemeral=True,
        )

    async def after_role_change(self, form: str):
        # The live panel lists the eligible roles, so redraw it if it's showing this form
        if form == "feedback":
            if self.store.phase(1) == "feedback":
                await self.refresh_panel(1)
            return
        giveaway = 1 if form == "wallet" else 2
        if self.store.phase(giveaway) == "wallet":
            await self.refresh_panel(giveaway)

    @admin_command(name="giveaway-feedback",
                   description="Close wallet submissions and post the feedback panel in this channel")
    async def giveaway_feedback(self, interaction: discord.Interaction):
        if not self.store.gacha_role_id:
            await interaction.response.send_message("Run /giveaway-setup first.", ephemeral=True)
            return
        if not self.store.role_ids("feedback"):
            self.store.set_role_ids("feedback", [self.store.gacha_role_id])
        missing = missing_post_perms(interaction.channel)
        if missing:
            await interaction.response.send_message(
                f"❌ I can't post in this channel. Give me: **{', '.join(missing)}** here, then run it again.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        # Both wallet panels (possibly in other channels) become closed panels with no button
        for giveaway in GIVEAWAYS:
            if self.store.phase(giveaway) == "wallet":
                self.store.save(**{key("phase", giveaway): "closed"})
                await self.refresh_panel(giveaway)

        # The feedback panel takes over giveaway 1's panel slot. The banner belongs to the
        # wallet panel, so the feedback panel is plain
        self.store.save(phase="feedback", panel_image=None)
        if not await self.post_panel(interaction, FeedbackView(self)):
            return
        await interaction.followup.send(
            "✅ Feedback panel posted, wallet submissions closed. Make sure these roles can see this channel. "
            "Change them with /giveaway-role-add and /giveaway-role-remove (form: feedback).",
            embed=await self.role_summary(interaction.guild, "feedback"),
            ephemeral=True,
        )

    @admin_command(name="giveaway-close", description="Close a giveaway panel (or all of them) and remove its button")
    @app_commands.describe(giveaway="Which giveaway to close (default: all open ones)")
    async def giveaway_close(self, interaction: discord.Interaction, giveaway: GiveawayNo | None = None):
        await interaction.response.defer(ephemeral=True)
        closed = []
        for g in [giveaway] if giveaway else GIVEAWAYS:
            if self.store.phase(g) in ("wallet", "feedback"):
                self.store.save(**{key("phase", g): "closed"})
                await self.refresh_panel(g)
                closed.append(str(g))
        msg = (f"Closed giveaway {' and '.join(closed)}. " if closed else "Nothing was open. ")
        await interaction.followup.send(msg + "Data is kept; use /giveaway-export.", ephemeral=True)

    @admin_command(name="giveaway-stats", description="Show giveaway submission stats")
    async def giveaway_stats(self, interaction: discord.Interaction):
        lines = ["📊 **Giveaway stats**"]
        for g in GIVEAWAYS:
            if g == 2 and self.store.phase(2) == "off" and not self.store.wallet_count(2):
                continue  # giveaway 2 not set up yet
            s, cap = self.store.stats(g), self.store.cap(g)
            last = (f"<@{s['last_user_id']}> <t:{s['last_wallet_at']}:R>" if s["last_user_id"] else "none yet")
            by_role = "\n".join(f"  - {n}: {c}" for n, c in sorted(s["by_role"].items(), key=lambda x: -x[1]))
            lines.append(
                f"\n**Giveaway {g}** (phase: `{self.store.phase(g)}`)\n"
                f"Wallets: **{s['wallets']} / {cap}** ({max(cap - s['wallets'], 0)} left)\n"
                f"Last wallet: {last}"
                + (f"\nSubmitters by role (members can have several):\n{by_role}" if by_role else "")
            )
        s = self.store.stats()
        last_fb = f"<t:{s['last_feedback_at']}:R>" if s["last_feedback_at"] else "none yet"
        lines.append(f"\nTotal wallets: **{s['wallets']}** · Feedback: **{s['feedback']}** (last {last_fb})")
        await interaction.response.send_message(
            "\n".join(lines)[:2000],
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin_command(name="giveaway-export", description="Post all wallets and feedback as a CSV in this channel")
    async def giveaway_export(self, interaction: discord.Interaction):
        data = self.store.export_csv().encode("utf-8-sig")  # BOM so Excel reads emoji correctly
        name = f"giveaway-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.csv"

        # Keep a copy on the Mac too
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        (EXPORT_DIR / name).write_bytes(data)

        s = self.store.stats()
        split = (f" (Giveaway 1: {self.store.wallet_count(1)}, Giveaway 2: {self.store.wallet_count(2)})"
                 if self.store.wallet_count(2) else "")
        summary = (f"**{s['wallets']}** wallets{split}, **{s['feedback']}** feedback responses. "
                   f"Mac copy: `data/exports/{name}`")
        # Posted as a normal message so it stays in the channel and opens on mobile (ephemeral
        # messages vanish on reload and iOS can't preview CSVs in them). The file lists every
        # wallet, so never post it where @everyone can read.
        outsiders = export_outsiders(interaction.channel)
        if outsiders:
            await interaction.response.send_message(
                f"⚠️ Non-admins can read this channel: {', '.join(outsiders[:10])}"
                f"{' and more' if len(outsiders) > 10 else ''}. Here's the export privately; download it now, "
                f"this message disappears when Discord reloads. If it's fine for them to see every wallet, "
                f"press **Post here anyway**.\n{summary}",
                file=discord.File(io.BytesIO(data), filename=name),
                view=PostExportView(data, name, summary),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        missing = missing_post_perms(interaction.channel, with_file=True)
        if missing:
            await interaction.response.send_message(
                f"❌ I can't post in this channel (missing **{', '.join(missing)}**), so here's the export "
                f"privately. Download it now: this message disappears when Discord reloads.\n{summary}",
                file=discord.File(io.BytesIO(data), filename=name),
                ephemeral=True,
            )
            return
        await interaction.response.send_message("📦 Export posted.", ephemeral=True)
        await post_export(interaction, data, name, summary)


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaway(bot, GiveawayStore()))
