import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import os

load_dotenv()

intents = discord.Intents.default()
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


def collect_channels(role: discord.Role, mode: str, guild: discord.Guild):
    """Returns list of (category_name, both_channels, view_only_channels) sorted by position."""
    cats: dict[int | None, dict] = {}

    for channel in sorted(
        guild.text_channels,
        key=lambda c: (c.category.position if c.category else -1, c.position),
    ):
        perms = channel.permissions_for(role)
        can_view = perms.view_channel
        can_send = perms.send_messages and perms.view_channel

        if mode == "view":
            if not can_view:
                continue
            bucket = "both"
        else:
            if not can_view:
                continue
            bucket = "both" if can_send else "view"

        cat_id = channel.category_id
        if cat_id not in cats:
            cats[cat_id] = {
                "name": channel.category.name if channel.category else "Uncategorized",
                "position": channel.category.position if channel.category else -1,
                "both": [],
                "view": [],
            }
        cats[cat_id][bucket].append(channel)

    return sorted(cats.values(), key=lambda c: c["position"])


def build_embeds(role: discord.Role, mode: str, guild: discord.Guild) -> list[discord.Embed]:
    color = role.color if role.color.value else discord.Color.blurple()
    filter_label = "View & Send" if mode == "both" else "View only"

    embeds: list[discord.Embed] = []
    embed = discord.Embed(
        title=f"📋  Channel Access — @{role.name}",
        description=f"🔍  Filter: **{filter_label}**\n🏠  Server: **{guild.name}**\n​",
        color=color,
    )

    for cat in collect_channels(role, mode, guild):
        both = cat["both"]
        view_only = cat["view"]
        if not both and not view_only:
            continue

        lines = []
        for ch in both:
            icon = "📨" if mode == "both" else "👁️"
            lines.append(f"{icon}  {ch.mention}")
        for ch in view_only:
            lines.append(f"👁️  {ch.mention}")

        value = "\n".join(lines) or "_None_"
        if len(value) > 1024:
            value = value[:1000] + "\n…*(truncated)*"

        field_name = f"📁  {cat['name'].upper()}  ({len(both) + len(view_only)})"

        # Start a new embed if we're hitting limits
        if len(embed.fields) >= 25 or len(embed) + len(field_name) + len(value) > 5900:
            embeds.append(embed)
            embed = discord.Embed(title=f"📋  Channel Access — @{role.name} (cont.)", color=color)

        embed.add_field(name=field_name, value=value, inline=False)

    embeds.append(embed)
    return embeds


def build_text(role: discord.Role, mode: str, guild: discord.Guild) -> list[str]:
    filter_label = "View & Send" if mode == "both" else "View only"

    lines = [
        f"📋  **Channel Access — @{role.name}**",
        f"🔍  Filter: **{filter_label}**  •  🏠  **{guild.name}**",
        "",
    ]

    for cat in collect_channels(role, mode, guild):
        both = cat["both"]
        view_only = cat["view"]
        if not both and not view_only:
            continue

        lines.append(f"📁  **{cat['name'].upper()}**")
        for ch in both:
            icon = "📨" if mode == "both" else "👁️"
            lines.append(f"　　{icon}  {ch.mention}")
        for ch in view_only:
            lines.append(f"　　👁️  {ch.mention}")
        lines.append("")

    # Split into chunks under Discord's 2000-char message limit
    messages: list[str] = []
    current = ""
    for line in lines:
        chunk = line + "\n"
        if len(current) + len(chunk) > 1900:
            messages.append(current)
            current = chunk
        else:
            current += chunk
    if current.strip():
        messages.append(current)

    return messages or ["_(No accessible channels found)_"]


@bot.tree.command(
    name="channellist",
    description="List channels a role can access, grouped by category",
)
@app_commands.describe(
    role="The role to check permissions for",
    filter="Permission filter (default: View & Send)",
    output="How to display the result (default: Embed)",
    public="Send so everyone can see it (default: only you)",
)
@app_commands.choices(
    filter=[
        app_commands.Choice(name="View & Send", value="both"),
        app_commands.Choice(name="View only", value="view"),
    ],
    output=[
        app_commands.Choice(name="Embed", value="embed"),
        app_commands.Choice(name="Text", value="text"),
    ],
)
async def channellist(
    interaction: discord.Interaction,
    role: discord.Role,
    filter: app_commands.Choice[str] | None = None,
    output: app_commands.Choice[str] | None = None,
    public: bool = False,
):
    await interaction.response.defer(ephemeral=not public)

    mode = filter.value if filter else "both"
    fmt = output.value if output else "embed"
    guild = interaction.guild

    if fmt == "embed":
        embeds = build_embeds(role, mode, guild)
        for embed in embeds:
            await interaction.followup.send(embed=embed, ephemeral=not public)
    else:
        chunks = build_text(role, mode, guild)
        for chunk in chunks:
            await interaction.followup.send(chunk, ephemeral=not public)


bot.run(os.getenv("DISCORD_TOKEN"))
