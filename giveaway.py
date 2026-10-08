"""Gacha card giveaway: collect Solana wallets from eligible members, then feedback.

Phase "wallet":   members with an eligible role submit a wallet (capped), get the gacha role.
Phase "feedback": wallet panel closes; a feedback panel for gacha-role members is posted.
Phase "closed":   the panel loses its button.

Everything is stored in a local SQLite file (data/armybot.db) and exported as CSV.
"""

import csv
import io
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

DB_PATH = Path(__file__).parent / "data" / "armybot.db"
DEFAULT_CAP = 500

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

    @property
    def phase(self) -> str:
        return self.get("phase", "off")

    @property
    def cap(self) -> int:
        return int(self.get("cap", DEFAULT_CAP))

    @property
    def gacha_role_id(self) -> int | None:
        value = self.get("gacha_role_id")
        return int(value) if value else None

    @property
    def eligible_role_ids(self) -> set[int]:
        return {int(r) for r in (self.get("eligible_role_ids") or "").split(",") if r}

    # --- submissions ---

    def wallet_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM submissions WHERE wallet IS NOT NULL").fetchone()[0]

    def get_submission(self, user_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM submissions WHERE user_id = ?", (user_id,)).fetchone()

    def submit_wallet(self, user_id: int, username: str, wallet: str, roles: str) -> str:
        """Returns 'new', 'updated', 'unchanged', 'full' or 'duplicate'.

        No awaits happen in here, so the cap check and the insert can't interleave
        with another submission on the bot's event loop.
        """
        now = int(time.time())
        owner = self.db.execute("SELECT user_id FROM submissions WHERE wallet = ?", (wallet,)).fetchone()
        if owner and owner["user_id"] != user_id:
            return "duplicate"

        existing = self.get_submission(user_id)
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

        if self.wallet_count() >= self.cap:
            return "full"

        self.db.execute(
            "INSERT INTO submissions (user_id, username, wallet, roles, submitted_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET wallet = excluded.wallet, username = excluded.username, "
            "roles = excluded.roles, submitted_at = excluded.submitted_at, updated_at = excluded.updated_at",
            (user_id, username, wallet, roles, now, now),
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

    def stats(self) -> dict:
        row = self.db.execute(
            "SELECT COUNT(wallet) AS wallets, COUNT(feedback_at) AS feedback, "
            "MAX(submitted_at) AS last_wallet_at, MAX(feedback_at) AS last_feedback_at FROM submissions"
        ).fetchone()
        last = self.db.execute(
            "SELECT user_id, submitted_at FROM submissions WHERE wallet IS NOT NULL "
            "ORDER BY submitted_at DESC LIMIT 1"
        ).fetchone()
        by_role: dict[str, int] = {}
        for (roles,) in self.db.execute("SELECT roles FROM submissions WHERE wallet IS NOT NULL"):
            for name in filter(None, roles.split(", ")):
                by_role[name] = by_role.get(name, 0) + 1
        return {**dict(row), "last_user_id": last["user_id"] if last else None, "by_role": by_role}

    def export_csv(self) -> str:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(
            ["discord_id", "username", "wallet", "eligible_roles", "submitted_at",
             "updated_at", "everything_worked", "bugs_comments", "feedback_at"]
        )
        for r in self.db.execute("SELECT * FROM submissions ORDER BY submitted_at IS NULL, submitted_at"):
            writer.writerow(
                [str(r["user_id"]), csv_safe(r["username"]), r["wallet"] or "", csv_safe(r["roles"]),
                 iso(r["submitted_at"]), iso(r["updated_at"]), csv_safe(r["worked"]),
                 csv_safe(r["feedback"]), iso(r["feedback_at"])]
            )
        return out.getvalue()


# --- Discord UI ---


def panel_embed(store: GiveawayStore) -> discord.Embed:
    count, cap = store.wallet_count(), store.cap
    if store.phase == "wallet":
        full = count >= cap
        embed = discord.Embed(
            title="🎴 Free Gacha Card Giveaway",
            description=(
                "Click **Submit wallet** and paste your **Solana wallet address**.\n"
                "Open to LP Army and activity role holders. One wallet per person, "
                "you can resubmit to fix a typo.\n\n"
                + ("**All spots are taken.**" if full else f"**{count} / {cap}** spots claimed")
            ),
            color=discord.Color.red() if full else discord.Color.gold(),
        )
    elif store.phase == "feedback":
        embed = discord.Embed(
            title="📝 Gacha Card Feedback",
            description=(
                "Cards are out! If you received one, click **Give feedback** and tell us "
                "whether everything worked or if you ran into any bugs."
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
    def __init__(self, cog: "Giveaway", current: str | None):
        super().__init__()
        self.cog = cog
        self.wallet = discord.ui.TextInput(
            label="Solana wallet address", min_length=32, max_length=44,
            placeholder="e.g. 7xKX...", default=current,
        )
        self.add_item(self.wallet)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_wallet_submit(interaction, self.wallet.value.strip())


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
    # timeout=None + fixed custom_id makes the button keep working after bot restarts
    def __init__(self, cog: "Giveaway"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Submit wallet", emoji="💳", style=discord.ButtonStyle.success,
                       custom_id="giveaway:wallet")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_wallet_button(interaction)


class FeedbackView(discord.ui.View):
    def __init__(self, cog: "Giveaway"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Give feedback", emoji="📝", style=discord.ButtonStyle.primary,
                       custom_id="giveaway:feedback")
    async def give(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_feedback_button(interaction)


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
        self.bot.add_view(WalletView(self))
        self.bot.add_view(FeedbackView(self))

    # --- helpers ---

    def eligible_roles(self, member: discord.Member) -> list[discord.Role]:
        allowed = self.store.eligible_role_ids
        return [r for r in member.roles if r.id in allowed]

    def has_gacha_role(self, member: discord.Member) -> bool:
        return any(r.id == self.store.gacha_role_id for r in member.roles)

    def current_view(self) -> discord.ui.View | None:
        # Closed panels lose their button entirely (view=None removes components)
        return {"wallet": WalletView, "feedback": FeedbackView}.get(self.store.phase, lambda _: None)(self)

    async def refresh_panel(self) -> bool:
        channel_id, message_id = self.store.get("panel_channel_id"), self.store.get("panel_message_id")
        if not (channel_id and message_id):
            return False
        try:
            channel = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
            message = await channel.fetch_message(int(message_id))
            await message.edit(embed=panel_embed(self.store), view=self.current_view())
            return True
        except discord.HTTPException as e:
            print(f"[giveaway] could not update panel: {e}")
            return False

    # --- wallet phase ---

    async def handle_wallet_button(self, interaction: discord.Interaction):
        if self.store.phase != "wallet":
            await interaction.response.send_message("Wallet submissions are closed.", ephemeral=True)
            return
        if not self.eligible_roles(interaction.user):
            await interaction.response.send_message(
                "❌ Sorry, this giveaway is only for LP Army and activity role holders.", ephemeral=True
            )
            return
        existing = self.store.get_submission(interaction.user.id)
        current = existing["wallet"] if existing else None
        if not current and self.store.wallet_count() >= self.store.cap:
            await interaction.response.send_message("❌ Sorry, all spots are taken.", ephemeral=True)
            return
        await interaction.response.send_modal(WalletModal(self, current))

    async def handle_wallet_submit(self, interaction: discord.Interaction, wallet: str):
        member = interaction.user
        # Re-check: phase, roles and cap may have changed while the modal was open
        roles = self.eligible_roles(member)
        if self.store.phase != "wallet" or not roles:
            await interaction.response.send_message("❌ You can't submit a wallet right now.", ephemeral=True)
            return
        if not is_valid_solana_address(wallet):
            await interaction.response.send_message(
                "❌ That doesn't look like a valid Solana wallet address. Please check it and try again.",
                ephemeral=True,
            )
            return

        result = self.store.submit_wallet(member.id, str(member), wallet, ", ".join(r.name for r in roles))
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
            await self.refresh_panel()

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
        if self.store.phase != "feedback":
            await interaction.response.send_message("Feedback is closed.", ephemeral=True)
            return
        if not self.has_gacha_role(interaction.user):
            await interaction.response.send_message(
                "❌ Feedback is only open to members with the gacha role.", ephemeral=True
            )
            return
        await interaction.response.send_modal(FeedbackModal(self))

    async def handle_feedback_submit(self, interaction: discord.Interaction, worked: str, feedback: str):
        if self.store.phase != "feedback" or not self.has_gacha_role(interaction.user):
            await interaction.response.send_message("❌ You can't send feedback right now.", ephemeral=True)
            return
        replaced = self.store.submit_feedback(interaction.user.id, str(interaction.user), worked, feedback)
        await interaction.response.send_message(
            "✅ Feedback updated, thanks!" if replaced else "✅ Thanks for the feedback!", ephemeral=True
        )

    # --- admin commands ---

    @admin_command(name="giveaway-start", description="Post the wallet submission panel in this channel")
    @app_commands.describe(
        gacha_role="Role given to everyone who submits a wallet",
        eligible_1="A role allowed to submit (e.g. LP Army)",
        cap="Maximum number of wallets (default 500)",
    )
    async def giveaway_start(
        self,
        interaction: discord.Interaction,
        gacha_role: discord.Role,
        eligible_1: discord.Role,
        eligible_2: discord.Role | None = None,
        eligible_3: discord.Role | None = None,
        eligible_4: discord.Role | None = None,
        eligible_5: discord.Role | None = None,
        eligible_6: discord.Role | None = None,
        eligible_7: discord.Role | None = None,
        eligible_8: discord.Role | None = None,
        cap: app_commands.Range[int, 1, 10000] = DEFAULT_CAP,
    ):
        eligible = [r for r in (eligible_1, eligible_2, eligible_3, eligible_4,
                                eligible_5, eligible_6, eligible_7, eligible_8) if r]
        me = interaction.guild.me
        if not me.guild_permissions.manage_roles or gacha_role >= me.top_role:
            await interaction.response.send_message(
                f"❌ I can't assign {gacha_role.mention}. Give me **Manage Roles** and drag my role "
                f"above {gacha_role.mention} in Server Settings > Roles, then run this again.",
                ephemeral=True,
            )
            return

        self.store.save(
            phase="wallet",
            cap=cap,
            gacha_role_id=gacha_role.id,
            eligible_role_ids=",".join(str(r.id) for r in eligible),
        )
        await interaction.response.send_message(embed=panel_embed(self.store), view=WalletView(self))
        message = await interaction.original_response()
        self.store.save(panel_channel_id=message.channel.id, panel_message_id=message.id)
        await interaction.followup.send(
            f"Giveaway open. Eligible: {', '.join(r.mention for r in eligible)}. "
            f"Gacha role: {gacha_role.mention}. Cap: {cap}.",
            ephemeral=True,
        )

    @admin_command(name="giveaway-feedback",
                   description="Close wallet submissions and post the feedback panel in this channel")
    async def giveaway_feedback(self, interaction: discord.Interaction):
        if not self.store.gacha_role_id:
            await interaction.response.send_message("Run /giveaway-start first.", ephemeral=True)
            return
        # The old wallet panel (possibly in another channel) becomes a closed panel with no button
        self.store.save(phase="closed")
        await self.refresh_panel()

        self.store.save(phase="feedback")
        await interaction.response.send_message(embed=panel_embed(self.store), view=FeedbackView(self))
        message = await interaction.original_response()
        self.store.save(panel_channel_id=message.channel.id, panel_message_id=message.id)
        gacha = interaction.guild.get_role(self.store.gacha_role_id)
        await interaction.followup.send(
            f"Feedback open for {gacha.mention if gacha else 'the gacha role'}. "
            "Wallet submissions are closed. Make sure this channel is visible to that role.",
            ephemeral=True,
        )

    @admin_command(name="giveaway-close", description="Close the giveaway and disable the panel button")
    async def giveaway_close(self, interaction: discord.Interaction):
        self.store.save(phase="closed")
        await self.refresh_panel()
        await interaction.response.send_message("Giveaway closed. Data is kept; use /giveaway-export.",
                                                ephemeral=True)

    @admin_command(name="giveaway-stats", description="Show giveaway submission stats")
    async def giveaway_stats(self, interaction: discord.Interaction):
        s = self.store.stats()
        cap = self.store.cap
        last = (f"<@{s['last_user_id']}> <t:{s['last_wallet_at']}:R>" if s["last_user_id"] else "none yet")
        last_fb = f"<t:{s['last_feedback_at']}:R>" if s["last_feedback_at"] else "none yet"
        by_role = "\n".join(f"- {name}: {n}" for name, n in sorted(s["by_role"].items(), key=lambda x: -x[1]))
        await interaction.response.send_message(
            f"📊 **Giveaway stats** (phase: `{self.store.phase}`)\n"
            f"Wallets: **{s['wallets']} / {cap}** ({max(cap - s['wallets'], 0)} left)\n"
            f"Last wallet: {last}\n"
            f"Feedback: **{s['feedback']}** (last {last_fb})\n"
            + (f"\nSubmitters by role (members can have several):\n{by_role}" if by_role else ""),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin_command(name="giveaway-export", description="Download all wallets and feedback as a CSV")
    async def giveaway_export(self, interaction: discord.Interaction):
        data = self.store.export_csv().encode("utf-8-sig")  # BOM so Excel reads emoji correctly
        name = f"giveaway-{datetime.now(timezone.utc):%Y%m%d-%H%M}.csv"
        await interaction.response.send_message(
            f"{self.store.wallet_count()} wallets.",
            file=discord.File(io.BytesIO(data), filename=name),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaway(bot, GiveawayStore()))
