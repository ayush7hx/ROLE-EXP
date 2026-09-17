from __future__ import annotations

import asyncio
import io
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

DB_PATH = Path(__file__).resolve().parent / "ticket.sqlite3"
EMBED_COLOR = discord.Color.red()
MAX_OPEN_TICKETS = 3


class TicketStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ticket_config (
                guild_id INTEGER PRIMARY KEY,
                panel_channel_id INTEGER NOT NULL,
                panel_message_id INTEGER NOT NULL,
                panel_type TEXT NOT NULL DEFAULT 'buttons',
                logging_channel_id INTEGER,
                staff_role_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                closed_category_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS tickets (
                channel_id INTEGER PRIMARY KEY,
                message_id INTEGER,
                guild_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                closed_at TEXT,
                closed_by_id INTEGER,
                claimed_by_id INTEGER,
                is_locked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS ticket_counts (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                open_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );
            """
        )
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(tickets)")}
        if "message_id" not in columns:
            self.connection.execute("ALTER TABLE tickets ADD COLUMN message_id INTEGER")
        self.connection.commit()

    def one(self, query: str, values: tuple = ()) -> sqlite3.Row | None:
        return self.connection.execute(query, values).fetchone()

    def many(self, query: str, values: tuple = ()) -> list[sqlite3.Row]:
        return self.connection.execute(query, values).fetchall()

    def run(self, query: str, values: tuple = ()) -> None:
        self.connection.execute(query, values)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class TicketSystem(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = TicketStore()

    async def load_views(self) -> None:
        await self.bot.wait_until_ready()
        for config in self.store.many("SELECT * FROM ticket_config"):
            if config["panel_message_id"]:
                self.bot.add_view(TicketPanel(self, style=config["panel_type"]), message_id=config["panel_message_id"])
        for ticket in self.store.many("SELECT * FROM tickets WHERE closed_at IS NULL AND message_id IS NOT NULL"):
            self.bot.add_view(TicketActions(self, ticket["channel_id"]), message_id=ticket["message_id"])

    def is_staff(self, member: discord.Member, guild_id: int) -> bool:
        config = self.store.one("SELECT staff_role_id FROM ticket_config WHERE guild_id = ?", (guild_id,))
        return bool(member.guild_permissions.manage_channels or (config and member.get_role(config["staff_role_id"])))

    async def setup_panel(self, ctx: commands.Context, style: str, channel: discord.TextChannel, staff_role: discord.Role) -> None:
        if ctx.guild is None:
            return
        if style.lower() not in {"buttons", "dropdown"}:
            await ctx.send("Style must be `buttons` or `dropdown`.")
            return
        category = discord.utils.get(ctx.guild.categories, name="Tickets")
        if category is None:
            category = await ctx.guild.create_category("Tickets", reason="Configure ticket system")
        embed = discord.Embed(
            title="Support Tickets",
            description="Choose a category below to open a private support ticket.",
            color=EMBED_COLOR,
        )
        view = TicketPanel(self, style=style.lower(), staff_role_id=staff_role.id, category_id=category.id)
        message = await channel.send(embed=embed, view=view)
        self.store.run(
            "INSERT INTO ticket_config (guild_id, panel_channel_id, panel_message_id, panel_type, staff_role_id, category_id) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET panel_channel_id=excluded.panel_channel_id, "
            "panel_message_id=excluded.panel_message_id, panel_type=excluded.panel_type, staff_role_id=excluded.staff_role_id, category_id=excluded.category_id",
            (ctx.guild.id, channel.id, message.id, style.lower(), staff_role.id, category.id),
        )
        await ctx.send(f"Ticket panel created in {channel.mention} using **{style.lower()}** mode.")

    async def create_ticket(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Tickets can only be opened in a server.", ephemeral=True)
            return
        config = self.store.one("SELECT * FROM ticket_config WHERE guild_id = ?", (interaction.guild.id,))
        if config is None:
            await interaction.response.send_message("Ticket system is not configured.", ephemeral=True)
            return
        count = self.store.one("SELECT open_count FROM ticket_counts WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, interaction.user.id))
        if count and count["open_count"] >= MAX_OPEN_TICKETS:
            await interaction.response.send_message(f"You can have up to {MAX_OPEN_TICKETS} open tickets.", ephemeral=True)
            return
        existing = self.store.one("SELECT channel_id FROM tickets WHERE guild_id = ? AND creator_id = ? AND closed_at IS NULL", (interaction.guild.id, interaction.user.id))
        if existing and interaction.guild.get_channel(existing["channel_id"]):
            await interaction.response.send_message(f"You already have an open ticket: <#{existing['channel_id']}>", ephemeral=True)
            return
        category = interaction.guild.get_channel(config["category_id"])
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("The ticket category no longer exists. Run setup again.", ephemeral=True)
            return
        staff_role = interaction.guild.get_role(config["staff_role_id"])
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        await interaction.response.defer(ephemeral=True)
        channel = await category.create_text_channel(f"ticket-{interaction.user.name}"[:95], overwrites=overwrites)
        embed = discord.Embed(title="Ticket opened", description=f"Welcome {interaction.user.mention}. Explain your request and staff will help you.", color=EMBED_COLOR)
        ticket_message = await channel.send(content=staff_role.mention if staff_role else None, embed=embed, view=TicketActions(self, channel.id))
        self.store.run("INSERT INTO tickets (channel_id, message_id, guild_id, creator_id, created_at) VALUES (?, ?, ?, ?, ?)", (channel.id, ticket_message.id, interaction.guild.id, interaction.user.id, datetime.now(timezone.utc).isoformat()))
        self.store.run("INSERT INTO ticket_counts (guild_id, user_id, open_count) VALUES (?, ?, 1) ON CONFLICT(guild_id, user_id) DO UPDATE SET open_count=open_count+1", (interaction.guild.id, interaction.user.id))
        await interaction.followup.send(f"Your ticket is ready: {channel.mention}", ephemeral=True)
        await self.log(interaction.guild, "Ticket opened", interaction.user, channel)

    async def log(self, guild: discord.Guild, title: str, actor: discord.Member | discord.User, channel: discord.abc.GuildChannel, transcript: discord.File | None = None) -> None:
        config = self.store.one("SELECT logging_channel_id FROM ticket_config WHERE guild_id = ?", (guild.id,))
        log_channel = guild.get_channel(config["logging_channel_id"]) if config and config["logging_channel_id"] else None
        if not isinstance(log_channel, discord.TextChannel):
            return
        embed = discord.Embed(title=title, color=EMBED_COLOR, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Actor", value=f"{actor.mention} (`{actor.id}`)")
        embed.add_field(name="Channel", value=f"`{channel.name}` (`{channel.id}`)")
        await log_channel.send(embed=embed, file=transcript) if transcript else await log_channel.send(embed=embed)

    async def transcript(self, channel: discord.TextChannel) -> discord.File:
        lines = [f"Transcript: #{channel.name}", ""]
        async for message in channel.history(limit=None, oldest_first=True):
            content = message.clean_content or "[embed/attachment]"
            lines.append(f"[{message.created_at.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S UTC}] {message.author} ({message.author.id}): {content}")
            lines.extend(f"  Attachment: {attachment.url}" for attachment in message.attachments)
        return discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"{channel.name}-transcript.txt")

    @commands.hybrid_group(name="ticket", description="Manage the ticket system.")
    @commands.guild_only()
    async def ticket(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @ticket.command(name="setup")
    @commands.has_permissions(manage_guild=True)
    async def ticket_setup(self, ctx: commands.Context, style: str, channel: discord.TextChannel, staff_role: discord.Role) -> None:
        await self.setup_panel(ctx, style, channel, staff_role)

    @ticket.command(name="log")
    @commands.has_permissions(manage_guild=True)
    async def ticket_log(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        if ctx.guild:
            self.store.run("UPDATE ticket_config SET logging_channel_id = ? WHERE guild_id = ?", (channel.id, ctx.guild.id))
            await ctx.send(f"Ticket logs will be sent to {channel.mention}.")

    @ticket.command(name="customize")
    @commands.has_permissions(manage_guild=True)
    async def ticket_customize(self, ctx: commands.Context) -> None:
        config = self.store.one("SELECT * FROM ticket_config WHERE guild_id = ?", (ctx.guild.id if ctx.guild else 0,))
        if config is None:
            await ctx.send("Run `*ticket setup buttons #panel-channel @Staff` first.")
            return
        await ctx.send("Click below to customize the ticket panel.", view=CustomPanelView(self))

    async def update_panel(self, interaction: discord.Interaction, values: dict[str, str]) -> None:
        if interaction.guild is None:
            return
        config = self.store.one("SELECT * FROM ticket_config WHERE guild_id = ?", (interaction.guild.id,))
        if config is None:
            await interaction.response.send_message("Ticket system is not configured.", ephemeral=True)
            return
        embed = discord.Embed(title=values["title"], description=values["description"], color=EMBED_COLOR)
        if values["image_url"]:
            embed.set_image(url=values["image_url"])
        if values["thumbnail_url"]:
            embed.set_thumbnail(url=values["thumbnail_url"])
        if values["footer"]:
            embed.set_footer(text=values["footer"])
        panel_channel = interaction.guild.get_channel(config["panel_channel_id"])
        if not isinstance(panel_channel, discord.TextChannel):
            await interaction.response.send_message("The configured panel channel no longer exists.", ephemeral=True)
            return
        view = TicketPanel(self, style=config["panel_type"])
        try:
            panel_message = await panel_channel.fetch_message(config["panel_message_id"])
            await panel_message.edit(embed=embed, view=view)
        except discord.HTTPException:
            panel_message = await panel_channel.send(embed=embed, view=view)
            self.store.run("UPDATE ticket_config SET panel_message_id = ? WHERE guild_id = ?", (panel_message.id, interaction.guild.id))
        await interaction.response.send_message("Ticket panel design updated.", ephemeral=True)

    @ticket.command(name="close")
    @commands.has_permissions(manage_channels=True)
    async def ticket_close(self, ctx: commands.Context) -> None:
        await self._close(ctx.channel, ctx.author, ctx)

    @ticket.command(name="claim")
    @commands.has_permissions(manage_channels=True)
    async def ticket_claim(self, ctx: commands.Context) -> None:
        ticket = self.store.one("SELECT * FROM tickets WHERE channel_id = ? AND closed_at IS NULL", (ctx.channel.id,))
        if not ticket:
            await ctx.send("This is not an open ticket.")
            return
        self.store.run("UPDATE tickets SET claimed_by_id = ? WHERE channel_id = ?", (ctx.author.id, ctx.channel.id))
        await ctx.send(f"Ticket claimed by {ctx.author.mention}.")

    async def _close(self, channel: discord.abc.GuildChannel, actor: discord.Member | discord.User, source: discord.Interaction | commands.Context) -> None:
        if not isinstance(channel, discord.TextChannel) or not source.guild:
            return
        ticket = self.store.one("SELECT * FROM tickets WHERE channel_id = ? AND closed_at IS NULL", (channel.id,))
        if not ticket:
            if isinstance(source, discord.Interaction):
                await source.followup.send("This is not an open ticket.", ephemeral=True)
            else:
                await source.send("This is not an open ticket.")
            return
        transcript = await self.transcript(channel)
        self.store.run("UPDATE tickets SET closed_at = ?, closed_by_id = ? WHERE channel_id = ?", (datetime.now(timezone.utc).isoformat(), actor.id, channel.id))
        self.store.run("UPDATE ticket_counts SET open_count = MAX(0, open_count - 1) WHERE guild_id = ? AND user_id = ?", (source.guild.id, ticket["creator_id"]))
        await self.log(source.guild, "Ticket closed", actor, channel, transcript)
        if isinstance(source, discord.Interaction):
            await source.followup.send("Ticket closed and transcript saved.", ephemeral=True)
        else:
            await source.send("Ticket closed and transcript saved.")
        await channel.delete(reason=f"Ticket closed by {actor}")


class TicketPanel(discord.ui.View):
    def __init__(self, system: TicketSystem, style: str = "buttons", staff_role_id: int | None = None, category_id: int | None = None) -> None:
        super().__init__(timeout=None)
        self.system = system
        if style == "dropdown":
            select = discord.ui.Select(placeholder="Select a ticket category", custom_id="ticket:open:select", options=[discord.SelectOption(label="Support", value="support")])
            select.callback = self.open_select
            self.add_item(select)
        else:
            button = discord.ui.Button(label="Open Ticket", style=discord.ButtonStyle.danger, custom_id="ticket:open:button")
            button.callback = self.open_button
            self.add_item(button)

    async def open_button(self, interaction: discord.Interaction) -> None:
        await self.system.create_ticket(interaction)

    async def open_select(self, interaction: discord.Interaction) -> None:
        await self.system.create_ticket(interaction)


class TicketActions(discord.ui.View):
    def __init__(self, system: TicketSystem, channel_id: int) -> None:
        super().__init__(timeout=None)
        self.system = system
        self.channel_id = channel_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return isinstance(interaction.user, discord.Member) and self.system.is_staff(interaction.user, interaction.guild.id if interaction.guild else 0)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.system.store.run("UPDATE tickets SET claimed_by_id = ? WHERE channel_id = ?", (interaction.user.id, self.channel_id))
        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}.")

    @discord.ui.button(label="Lock", style=discord.ButtonStyle.secondary, custom_id="ticket:lock")
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ticket = self.system.store.one("SELECT creator_id FROM tickets WHERE channel_id = ?", (self.channel_id,))
        member = interaction.guild.get_member(ticket["creator_id"]) if ticket else None
        if member:
            await interaction.channel.set_permissions(member, send_messages=False)
        self.system.store.run("UPDATE tickets SET is_locked = 1 WHERE channel_id = ?", (self.channel_id,))
        await interaction.response.send_message("Ticket locked.")

    @discord.ui.button(label="Unlock", style=discord.ButtonStyle.secondary, custom_id="ticket:unlock")
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ticket = self.system.store.one("SELECT creator_id FROM tickets WHERE channel_id = ?", (self.channel_id,))
        member = interaction.guild.get_member(ticket["creator_id"]) if ticket else None
        if member:
            await interaction.channel.set_permissions(member, send_messages=True)
        self.system.store.run("UPDATE tickets SET is_locked = 0 WHERE channel_id = ?", (self.channel_id,))
        await interaction.response.send_message("Ticket unlocked.")

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.system._close(interaction.channel, interaction.user, interaction)


async def setup(bot: commands.Bot) -> None:
    cog = TicketSystem(bot)
    await bot.add_cog(cog)
    bot.loop.create_task(cog.load_views())


class CustomPanelView(discord.ui.View):
    def __init__(self, system: TicketSystem) -> None:
        super().__init__(timeout=300)
        self.system = system

    @discord.ui.button(label="Customize Ticket Panel", style=discord.ButtonStyle.primary)
    async def customize(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CustomPanelModal(self.system))


class CustomPanelModal(discord.ui.Modal, title="Customize Ticket Panel"):
    panel_title = discord.ui.TextInput(label="Title", placeholder="Support Tickets", max_length=256)
    panel_description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, placeholder="Choose a category below to open a private support ticket.", max_length=4000)
    image_url = discord.ui.TextInput(label="Banner image URL", required=False, placeholder="https://example.com/banner.png", max_length=1000)
    thumbnail_url = discord.ui.TextInput(label="Thumbnail URL", required=False, placeholder="https://example.com/logo.png", max_length=1000)
    footer = discord.ui.TextInput(label="Footer", required=False, placeholder="Our support team will help you shortly.", max_length=2048)

    def __init__(self, system: TicketSystem) -> None:
        super().__init__()
        self.system = system

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.system.update_panel(
            interaction,
            {
                "title": str(self.panel_title),
                "description": str(self.panel_description),
                "image_url": str(self.image_url).strip(),
                "thumbnail_url": str(self.thumbnail_url).strip(),
                "footer": str(self.footer).strip(),
            },
        )
