from __future__ import annotations

import asyncio
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

from ticket_system import TicketSystem

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PREFIX = os.getenv("PREFIX", "*")
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
                role_log_channel_id INTEGER
            )"""
        )
        db.commit()


def get_config(guild_id: int) -> sqlite3.Row | None:
    with db_connect() as db:
        return db.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)).fetchone()


def save_config(guild_id: int, role_log_channel_id: int | None) -> None:
    with db_connect() as db:
        db.execute(
            """INSERT INTO guild_config (guild_id, role_log_channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET role_log_channel_id=excluded.role_log_channel_id""",
            (guild_id, role_log_channel_id),
        )
        db.commit()


def channel_from_config(guild: discord.Guild, channel_id: int | None) -> discord.TextChannel | None:
    channel = guild.get_channel(channel_id) if channel_id else None
    return channel if isinstance(channel, discord.TextChannel) else None


class RoleExp(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=PREFIX, intents=intents, help_command=None)
        self.role_log_locks: dict[int, asyncio.Lock] = {}

    async def setup_hook(self) -> None:
        init_db()
        await self.add_cog(TicketSystem(self))

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
        if actor is None or actor.bot or actor.id == after.id:
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


bot = RoleExp()


@bot.check
async def owner_only(ctx: commands.Context) -> bool:
    return ctx.author.id == OWNER_ID


@bot.command(name="rolelog")
@commands.guild_only()
async def rolelog_command(ctx: commands.Context, channel: discord.TextChannel) -> None:
    if ctx.guild is None:
        return
    save_config(ctx.guild.id, channel.id)
    await ctx.send(f"Manual role add/remove logs will be sent to {channel.mention}.")


@bot.command(name="rolelogoff")
@commands.guild_only()
async def rolelog_disable(ctx: commands.Context) -> None:
    if ctx.guild is None:
        return
    save_config(ctx.guild.id, None)
    await ctx.send("Manual role add/remove logs have been disabled.")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CheckFailure):
        if ctx.author.id != OWNER_ID:
            await ctx.send("Only the bot owner can use these commands.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument. Example: `{PREFIX}ticket setup buttons #panel @Staff`")
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
