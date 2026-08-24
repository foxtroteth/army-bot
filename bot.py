import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime, timezone
import csv
import io
import re
import os

load_dotenv()

intents = discord.Intents.default()
intents.guilds = True
# Required to list the members of a role. This is a privileged intent — it must
# be switched on under "Server Members Intent" in the Discord Developer Portal
# or the bot cannot log in at all.
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Discord only keeps about 90 days of audit log history, so role grants older
# than that are unrecoverable. The cap keeps the scan bounded on busy servers.
AUDIT_LOG_SCAN_LIMIT = 5000

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Nothing this command sends should ever ping anyone — role names and display
# names are attacker-controlled text (e.g. a role literally named "everyone").
NO_PINGS = discord.AllowedMentions.none()


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


def is_separator(name: str) -> bool:
    # Skip decorative categories that have no real alphanumeric text
    return not bool(re.search(r"[a-zA-Z0-9]", name))


def get_channel_lines(role: discord.Role, guild: discord.Guild) -> list[str]:
    cats: dict[int | None, dict] = {}

    for channel in sorted(
        guild.text_channels,
        key=lambda c: (c.category.position if c.category else -1, c.position),
    ):
        perms = channel.permissions_for(role)
        if not perms.view_channel:
            continue

        cat_id = channel.category_id
        cat_name = channel.category.name if channel.category else "Uncategorized"

        if is_separator(cat_name):
            continue

        if cat_id not in cats:
            cats[cat_id] = {
                "name": cat_name,
                "position": channel.category.position if channel.category else -1,
                "channels": [],
            }
        cats[cat_id]["channels"].append((channel, perms.send_messages))

    lines = []
    for cat in sorted(cats.values(), key=lambda c: c["position"]):
        lines.append(f"\n**📁 {cat['name']}**")
        for channel, can_send in cat["channels"]:
            emoji = "💬" if can_send else "👁️"
            lines.append(f"- {emoji} {channel.mention}")

    return lines


def chunk_messages(header: str, body: list[str]) -> list[str]:
    messages: list[str] = []
    current = header

    for line in body:
        chunk = line + "\n"
        if len(current) + len(chunk) > 1500:
            messages.append(current)
            current = chunk
        else:
            current += chunk

    if current.strip():
        messages.append(current)

    return messages or ["_(No accessible channels found)_"]


async def get_role_grants(
    role: discord.Role,
    guild: discord.Guild,
    member_ids: set[int],
) -> tuple[dict[int, tuple[datetime, discord.abc.User | None]], str | None]:
    """Find when each member was given the role, from the guild audit log.

    Entries arrive newest first, so the first "role added" entry seen for a
    member is the grant that is still in effect. Returns the grants found plus
    an optional warning to show the user.
    """
    grants: dict[int, tuple[datetime, discord.abc.User | None]] = {}
    pending = set(member_ids)

    try:
        async for entry in guild.audit_logs(
            limit=AUDIT_LOG_SCAN_LIMIT,
            action=discord.AuditLogAction.member_role_update,
        ):
            target_id = getattr(entry.target, "id", None)
            if target_id not in pending:
                continue

            added = getattr(entry.after, "roles", None) or []
            if not any(r.id == role.id for r in added):
                continue

            grants[target_id] = (entry.created_at, entry.user)
            pending.discard(target_id)
            if not pending:
                break
    except discord.Forbidden:
        return {}, "⚠️ I need the **View Audit Log** permission to read grant dates."
    except discord.HTTPException:
        return grants, "⚠️ The audit log lookup failed partway — some dates may be missing."

    return grants, None


def sort_members(
    members: list[discord.Member],
    grants: dict[int, tuple[datetime, discord.abc.User | None]],
) -> list[discord.Member]:
    # Oldest grant first, members with no known grant date last.
    def key(member: discord.Member):
        granted_at = grants.get(member.id, (None, None))[0]
        return (granted_at is None, granted_at or EPOCH, member.display_name.lower())

    return sorted(members, key=key)


def csv_safe(value: str) -> str:
    # Display names are user-controlled: stop spreadsheets treating one as a formula.
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def as_utc(moment: datetime | None) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds") if moment else ""


def build_csv(
    members: list[discord.Member],
    grants: dict[int, tuple[datetime, discord.abc.User | None]],
) -> io.BytesIO:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["user_id", "username", "display_name", "role_granted_at_utc", "granted_by", "joined_server_at_utc"]
    )

    for member in members:
        granted_at, granted_by = grants.get(member.id, (None, None))
        writer.writerow(
            [
                str(member.id),
                csv_safe(member.name),
                csv_safe(member.display_name),
                as_utc(granted_at),
                csv_safe(str(granted_by)) if granted_by else "",
                as_utc(member.joined_at),
            ]
        )

    return io.BytesIO(buffer.getvalue().encode("utf-8"))


def get_member_lines(
    members: list[discord.Member],
    grants: dict[int, tuple[datetime, discord.abc.User | None]],
) -> list[str]:
    lines = []
    for member in members:
        granted_at = grants.get(member.id, (None, None))[0]
        when = discord.utils.format_dt(granted_at, "f") if granted_at else "_unknown_"
        lines.append(f"- **{member.display_name}** (`{member.name}`) — {when}")
    return lines


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "-", name).strip("-")
    return cleaned or "role"


@bot.tree.command(name="channel-list", description="Show channels a role can access")
@app_commands.describe(
    role="The role to check",
    public="Send so everyone can see it (default: only you)",
)
async def channel_list(
    interaction: discord.Interaction,
    role: discord.Role,
    public: bool = False,
):
    await interaction.response.defer(ephemeral=not public)

    header = (
        f"📋 **Channel Access — @{role.name}**\n"
        f"🏠 {interaction.guild.name}\n"
        f"\n"
        f"💬 = view & send  |  👁️ = view only\n"
    )

    body = get_channel_lines(role, interaction.guild)
    if not body:
        await interaction.followup.send("_(No accessible channels found for this role)_", ephemeral=not public)
        return

    for chunk in chunk_messages(header, body):
        await interaction.followup.send(chunk, ephemeral=not public)


@bot.tree.command(name="member-export", description="Export the members in a role with the date they got it")
@app_commands.describe(
    role="The role to export",
    output="csv = downloadable file (default), text = listed in chat",
    public="Send so everyone can see it (default: only you)",
)
@app_commands.rename(output="format")
@app_commands.choices(
    output=[
        app_commands.Choice(name="CSV file", value="csv"),
        app_commands.Choice(name="Text in chat", value="text"),
    ]
)
@app_commands.guild_only()
async def member_export(
    interaction: discord.Interaction,
    role: discord.Role,
    output: str = "csv",
    public: bool = False,
):
    await interaction.response.defer(ephemeral=not public)

    guild = interaction.guild
    if not guild.chunked:
        await guild.chunk()

    members = role.members
    if not members:
        await interaction.followup.send(
            f"_(Nobody currently has @{role.name})_",
            ephemeral=not public,
            allowed_mentions=NO_PINGS,
        )
        return

    grants, warning = await get_role_grants(role, guild, {m.id for m in members})
    members = sort_members(members, grants)

    header = (
        f"📋 **Member Export — @{role.name}**\n"
        f"🏠 {guild.name}\n"
        f"👥 {len(members)} member{'s' if len(members) != 1 else ''}"
        f" · 🕒 {len(grants)} with a known grant date\n"
    )
    if len(grants) < len(members):
        header += "_Dates come from the audit log, which Discord keeps for ~90 days._\n"
    if warning:
        header += f"{warning}\n"

    if output == "text":
        for chunk in chunk_messages(header + "\n", get_member_lines(members, grants)):
            await interaction.followup.send(chunk, ephemeral=not public, allowed_mentions=NO_PINGS)
        return

    payload = build_csv(members, grants)
    if payload.getbuffer().nbytes > guild.filesize_limit:
        await interaction.followup.send(
            f"{header}\n⚠️ That export is too big to upload here. Try `format: text` instead.",
            ephemeral=not public,
            allowed_mentions=NO_PINGS,
        )
        return

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    export = discord.File(
        payload, filename=f"{safe_filename(role.name)}-members-{stamp}.csv"
    )
    await interaction.followup.send(
        header, file=export, ephemeral=not public, allowed_mentions=NO_PINGS
    )


try:
    bot.run(os.getenv("DISCORD_TOKEN"))
except discord.PrivilegedIntentsRequired:
    print(
        "Login failed: this bot needs the Server Members Intent to list role members.\n"
        "Enable it at https://discord.com/developers/applications → your app → "
        "Bot → Privileged Gateway Intents → Server Members Intent."
    )
    raise SystemExit(1)
