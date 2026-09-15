from __future__ import annotations

import asyncio
import io
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

import discord
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask, jsonify

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PREFIX = "*"
OWNER_ID = 1255716509443948648
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = ROOT / "data.sqlite3"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("role-exp")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with db_connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER PRIMARY KEY,
                ticket_category_id INTEGER,
                ticket_log_channel_id INTEGER,
                role_log_channel_id INTEGER,
                staff_role_id INTEGER
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS tickets (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                opener_id INTEGER NOT NULL,
                claimed_by INTEGER,
                created_at TEXT NOT NULL,
                closed_at TEXT
            )"""
        )
        db.commit()


def get_config(guild_id: int) -> sqlite3.Row | None:
    with db_connect() as db:
        return db.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)).fetchone()


def save_config(guild_id: int, **values: int | None) -> None:
    columns = ["ticket_category_id", "ticket_log_channel_id", "role_log_channel_id", "staff_role_id"]
    with db_connect() as db:
        existing = db.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)).fetchone()
        current = {column: (existing[column] if existing else None) for column in columns}
        current.update({key: value for key, value in values.items() if key in columns})
        db.execute(
            """INSERT INTO guild_config
            (guild_id, ticket_category_id, ticket_log_channel_id, role_log_channel_id, staff_role_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                ticket_category_id=excluded.ticket_category_id,
                ticket_log_channel_id=excluded.ticket_log_channel_id,
                role_log_channel_id=excluded.role_log_channel_id,
                staff_role_id=excluded.staff_role_id""",
            (guild_id, current["ticket_category_id"], current["ticket_log_channel_id"], current["role_log_channel_id"], current["staff_role_id"]),
        )
        db.commit()


def add_ticket(channel_id: int, guild_id: int, opener_id: int) -> None:
    with db_connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO tickets (channel_id, guild_id, opener_id, created_at) VALUES (?, ?, ?, ?)",
            (channel_id, guild_id, opener_id, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()


def get_ticket(channel_id: int) -> sqlite3.Row | None:
    with db_connect() as db:
        return db.execute("SELECT * FROM tickets WHERE channel_id = ?", (channel_id,)).fetchone()


def claim_ticket(channel_id: int, user_id: int) -> None:
    with db_connect() as db:
        db.execute("UPDATE tickets SET claimed_by = ? WHERE channel_id = ?", (user_id, channel_id))
        db.commit()


def close_ticket(channel_id: int) -> None:
    with db_connect() as db:
        db.execute("UPDATE tickets SET closed_at = ? WHERE channel_id = ?", (datetime.now(timezone.utc).isoformat(), channel_id))
        db.commit()


def channel_from_config(guild: discord.Guild, channel_id: int | None) -> discord.TextChannel | None:
    channel = guild.get_channel(channel_id) if channel_id else None
    return channel if isinstance(channel, discord.TextChannel) else None


class TicketPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.success, custom_id="role_exp:ticket_open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Tickets can only be opened inside a server.", ephemeral=True)
            return
        config = get_config(interaction.guild.id)
        if config is None or not config["ticket_log_channel_id"]:
            await interaction.response.send_message("Ticket system is not configured yet. Ask the owner to run `*ticketconfig`.", ephemeral=True)
            return
        existing = next((channel for channel in interaction.guild.text_channels if channel.topic == f"role-exp-ticket:{interaction.user.id}"), None)
        if existing:
            await interaction.response.send_message(f"You already have an open ticket: {existing.mention}", ephemeral=True)
            return
        category = interaction.guild.get_channel(config["ticket_category_id"]) if config["ticket_category_id"] else None
        if not isinstance(category, discord.CategoryChannel):
            category = await interaction.guild.create_category("Tickets", reason="Create ticket category")
            save_config(interaction.guild.id, ticket_category_id=category.id)
        bot_member = interaction.guild.me
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
            bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
        }
        staff_role = interaction.guild.get_role(config["staff_role_id"]) if config["staff_role_id"] else None
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        channel = await interaction.guild.create_text_channel(
            f"ticket-{interaction.user.name}"[:95],
            category=category,
            overwrites=overwrites,
            topic=f"role-exp-ticket:{interaction.user.id}",
            reason=f"Ticket opened by {interaction.user}",
        )
        add_ticket(channel.id, interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(f"Your ticket is ready: {channel.mention}", ephemeral=True)
        embed = discord.Embed(title="Ticket opened", description=f"Welcome {interaction.user.mention}. Please explain your request clearly.", color=discord.Color.blurple())
        embed.add_field(name="Opened by", value=f"{interaction.user.mention} (`{interaction.user.id}`)")
        embed.set_footer(text="Use the buttons below to claim or close this ticket.")
        await channel.send(embed=embed, view=TicketControls())
        await send_ticket_log(interaction.guild, "Ticket opened", interaction.user, channel, discord.Color.green())


class TicketControls(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, emoji="🙋", custom_id="role_exp:ticket_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return
        ticket = get_ticket(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message("This is not a tracked ticket.", ephemeral=True)
            return
        config = get_config(interaction.guild.id)
        staff_role = interaction.guild.get_role(config["staff_role_id"]) if config and config["staff_role_id"] else None
        if not (interaction.user.guild_permissions.manage_channels or (staff_role and staff_role in interaction.user.roles)):
            await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)
            return
        claim_ticket(interaction.channel_id, interaction.user.id)
        await interaction.response.send_message(f"This ticket was claimed by {interaction.user.mention}.")
        await send_ticket_log(interaction.guild, "Ticket claimed", interaction.user, interaction.channel, discord.Color.gold())

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="role_exp:ticket_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            return
        ticket = get_ticket(interaction.channel.id)
        if ticket is None:
            await interaction.response.send_message("This is not a tracked ticket.", ephemeral=True)
            return
        member = interaction.guild.get_member(interaction.user.id)
        config = get_config(interaction.guild.id)
        staff_role = interaction.guild.get_role(config["staff_role_id"]) if config and config["staff_role_id"] else None
        allowed = member and (member.id == ticket["opener_id"] or member.guild_permissions.manage_channels or (staff_role and staff_role in member.roles))
        if not allowed:
            await interaction.response.send_message("Only the ticket opener or staff can close this ticket.", ephemeral=True)
            return
        await interaction.response.defer()
        transcript = await build_transcript(interaction.channel)
        close_ticket(interaction.channel.id)
        await send_ticket_log(interaction.guild, "Ticket closed", interaction.user, interaction.channel, discord.Color.red(), transcript)
        await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")


class RoleExp(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=PREFIX, intents=intents, help_command=None)
        self.role_log_locks: dict[int, asyncio.Lock] = {}

    async def setup_hook(self) -> None:
        init_db()
        self.add_view(TicketPanel())
        self.add_view(TicketControls())

    async def on_ready(self) -> None:
        await self.change_presence(status=discord.Status.dnd)
        log.info("Online as %s in %d guild(s)", self.user, len(self.guilds))

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.bot:
            return
        before_roles = {role.id: role for role in before.roles}
        after_roles = {role.id: role for role in after.roles}
        added = [after_roles[role_id] for role_id in after_roles.keys() - before_roles.keys()]
        removed = [before_roles[role_id] for role_id in before_roles.keys() - after_roles.keys()]
        if not added and not removed:
            return
        actor = await find_role_change_actor(after.guild, after.id)
        if actor is None or actor.bot:
            return
        config = get_config(after.guild.id)
        log_channel = channel_from_config(after.guild, config["role_log_channel_id"] if config else None)
        if log_channel is None:
            return
        embed = discord.Embed(title="Role update", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
        embed.set_author(name=str(after), icon_url=after.display_avatar.url)
        embed.add_field(name="Member", value=f"{after.mention}\n`{after.id}`", inline=False)
        embed.add_field(name="Changed by", value=format_actor(actor), inline=False)
        if added:
            embed.add_field(name="Added", value="\n".join(f"{role.mention} (`{role.id}`)" for role in added), inline=False)
        if removed:
            embed.add_field(name="Removed", value="\n".join(f"{role.name} (`{role.id}`)" for role in removed), inline=False)
        await log_channel.send(embed=embed)


async def find_role_change_actor(guild: discord.Guild, member_id: int) -> discord.User | discord.Member | None:
    try:
        async for entry in guild.audit_logs(limit=8, action=discord.AuditLogAction.member_role_update):
            if entry.target and entry.target.id == member_id and (datetime.now(timezone.utc) - entry.created_at).total_seconds() < 15:
                return entry.user
    except (discord.Forbidden, discord.HTTPException):
        log.warning("Cannot read audit log in %s; grant View Audit Log", guild.id)
    return None


def format_actor(actor: discord.User | discord.Member | None) -> str:
    return f"{actor.mention} (`{actor.id}`)" if actor else "Unknown / Discord system"


async def build_transcript(channel: discord.TextChannel) -> discord.File:
    lines = [f"Transcript: #{channel.name}", ""]
    async for message in channel.history(limit=None, oldest_first=True):
        timestamp = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        content = message.content or "[embed/attachment]"
        lines.append(f"[{timestamp}] {message.author} ({message.author.id}): {content}")
    return discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"{channel.name}-transcript.txt")


async def send_ticket_log(guild: discord.Guild, title: str, actor: discord.Member | discord.User, channel: discord.abc.GuildChannel, color: discord.Color, transcript: discord.File | None = None) -> None:
    config = get_config(guild.id)
    log_channel = channel_from_config(guild, config["ticket_log_channel_id"] if config else None)
    if log_channel is None:
        return
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Actor", value=f"{actor.mention} (`{actor.id}`)", inline=False)
    embed.add_field(name="Channel", value=f"`{channel.name}` (`{channel.id}`)", inline=False)
    await log_channel.send(embed=embed, file=transcript) if transcript else await log_channel.send(embed=embed)


bot = RoleExp()


@bot.check
async def owner_only(ctx: commands.Context) -> bool:
    return ctx.author.id == OWNER_ID


@bot.command(name="rolelog")
@commands.guild_only()
async def rolelog_command(ctx: commands.Context, channel: discord.TextChannel) -> None:
    if ctx.guild is None:
        return
    save_config(ctx.guild.id, role_log_channel_id=channel.id)
    await ctx.send(f"Manual role add/remove logs will be sent to {channel.mention}.")


@bot.command(name="rolelogoff")
@commands.guild_only()
async def rolelog_disable(ctx: commands.Context) -> None:
    if ctx.guild is None:
        return
    save_config(ctx.guild.id, role_log_channel_id=None)
    await ctx.send("Manual role add/remove logs have been disabled.")


@bot.command(name="ticketconfig")
@commands.guild_only()
async def ticketconfig(ctx: commands.Context, ticket_logs: discord.TextChannel, staff_role: discord.Role | None = None) -> None:
    if ctx.guild is None:
        return
    category = discord.utils.get(ctx.guild.categories, name="Tickets")
    if category is None:
        category = await ctx.guild.create_category("Tickets", reason="Configure ticket system")
    save_config(ctx.guild.id, ticket_category_id=category.id, ticket_log_channel_id=ticket_logs.id, staff_role_id=staff_role.id if staff_role else None)
    embed = discord.Embed(title="Ticket configuration saved", description=f"Ticket category: {category.mention}\nTicket logs: {ticket_logs.mention}\nStaff role: {staff_role.mention if staff_role else 'Anyone with Manage Channels'}\n\nRole logs are configured separately with `*rolelog #role-logs`.", color=discord.Color.green())
    await ctx.send(embed=embed)


@bot.command(name="ticketpanel")
@commands.guild_only()
async def ticketpanel(ctx: commands.Context, thumbnail_url: str | None = None, image_url: str | None = None) -> None:
    if ctx.guild is None or not isinstance(ctx.channel, discord.TextChannel):
        return
    config = get_config(ctx.guild.id)
    if config is None or not config["ticket_log_channel_id"]:
        await ctx.send("Run `*ticketconfig #ticket-logs @Staff` first.")
        return
    attachments = [attachment.url for attachment in ctx.message.attachments]
    thumbnail_url = thumbnail_url or (attachments[0] if attachments else None)
    image_url = image_url or (attachments[1] if len(attachments) > 1 else None)
    embed = discord.Embed(
        title="Need Help? Open a Ticket!",
        description=(
            "🎫 **Need Assistance? We're Here to Help!**\n\n"
            "Have a question, issue, or need support?\n"
            "Simply open a ticket and our team will assist you as soon as possible.\n\n"
            "📌 **Before Opening a Ticket:**\n"
            "• Explain your issue clearly\n"
            "• Provide screenshots/details if needed\n"
            "• Please be patient while waiting for a response\n"
            "• Do not spam or create multiple tickets for the same issue\n\n"
            "💙 **Thank you for contacting our Support Team!**"
        ),
        color=discord.Color.red(),
    )
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text="🛠️ Staff will assist you shortly.")
    await ctx.send(embed=embed, view=TicketPanel())


@bot.command(name="ticketclaim")
@commands.guild_only()
async def ticketclaim(ctx: commands.Context) -> None:
    if ctx.guild is None or not isinstance(ctx.channel, discord.TextChannel) or not isinstance(ctx.author, discord.Member):
        return
    ticket = get_ticket(ctx.channel.id)
    if ticket is None:
        await ctx.send("This channel is not a tracked ticket.")
        return
    config = get_config(ctx.guild.id)
    staff_role = ctx.guild.get_role(config["staff_role_id"]) if config and config["staff_role_id"] else None
    if not (ctx.author.guild_permissions.manage_channels or (staff_role and staff_role in ctx.author.roles)):
        await ctx.send("Only staff can claim tickets.")
        return
    claim_ticket(ctx.channel.id, ctx.author.id)
    await ctx.send(f"This ticket was claimed by {ctx.author.mention}.")
    await send_ticket_log(ctx.guild, "Ticket claimed", ctx.author, ctx.channel, discord.Color.gold())


@bot.command(name="ticketclose")
@commands.guild_only()
async def ticketclose(ctx: commands.Context) -> None:
    if ctx.guild is None or not isinstance(ctx.channel, discord.TextChannel) or get_ticket(ctx.channel.id) is None:
        await ctx.send("This channel is not a tracked ticket.")
        return
    transcript = await build_transcript(ctx.channel)
    close_ticket(ctx.channel.id)
    await ctx.send("Closing ticket and saving transcript...")
    await send_ticket_log(ctx.guild, "Ticket closed", ctx.author, ctx.channel, discord.Color.red(), transcript)
    await ctx.channel.delete(reason=f"Ticket closed by {ctx.author}")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CheckFailure):
        if ctx.author.id != OWNER_ID:
            await ctx.send("Only the bot owner can use these commands.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument. Example: `{PREFIX}rolelog #role-logs`")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send("Mention a valid channel or role and try again.")
        return
    log.exception("Command failed", exc_info=error)


app = Flask(__name__)


@app.get("/")
def home():
    return "ROLE-EXP is online"


@app.get("/health")
def health():
    return jsonify(status="ok", bot_ready=bot.is_ready())


def run_web() -> None:
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Add it to .env or Render environment variables.")
    Thread(target=run_web, daemon=True, name="render-health").start()
    bot.run(TOKEN, log_handler=None)
