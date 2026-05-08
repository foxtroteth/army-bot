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


def get_channel_lines(role: discord.Role, guild: discord.Guild) -> list[str]:
    # Group text channels by category, sorted by position
    cats: dict[int | None, dict] = {}

    for channel in sorted(
        guild.text_channels,
        key=lambda c: (c.category.position if c.category else -1, c.position),
    ):
        perms = channel.permissions_for(role)
        if not perms.view_channel:
            continue

        can_send = perms.send_messages
        cat_id = channel.category_id

        if cat_id not in cats:
            cats[cat_id] = {
                "name": channel.category.name if channel.category else "Uncategorized",
                "position": channel.category.position if channel.category else -1,
                "channels": [],
            }
        cats[cat_id]["channels"].append((channel, can_send))

    lines = []
    for cat in sorted(cats.values(), key=lambda c: c["position"]):
        lines.append(f"\n📁  **{cat['name'].upper()}**")
        for channel, can_send in cat["channels"]:
            emoji = "💬" if can_send else "👁️"
            lines.append(f"　{emoji}  {channel.mention}")

    return lines


def chunk_messages(header: list[str], body: list[str]) -> list[str]:
    messages: list[str] = []
    current = "\n".join(header) + "\n"

    for line in body:
        chunk = line + "\n"
        if len(current) + len(chunk) > 1900:
            messages.append(current)
            current = chunk
        else:
            current += chunk

    if current.strip():
        messages.append(current)

    return messages or ["_(No accessible channels found)_"]


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

    header = [
        f"📋  **Channel Access — @{role.name}**",
        f"🏠  {interaction.guild.name}",
        "",
        f"💬  = can view & send　　👁️  = view only",
    ]

    body = get_channel_lines(role, interaction.guild)
    if not body:
        await interaction.followup.send("_(No accessible channels found for this role)_", ephemeral=not public)
        return

    for chunk in chunk_messages(header, body):
        await interaction.followup.send(chunk, ephemeral=not public)


bot.run(os.getenv("DISCORD_TOKEN"))
