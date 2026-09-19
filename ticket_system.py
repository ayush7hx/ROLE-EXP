from __future__ import annotations

import io
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands

DB_PATH = Path(__file__).resolve().parent / "ticket.sqlite3"
MAX_OPEN_TICKETS = 3


def safe_name(value: str) -> str:
    value = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")
    return value[:70] or "ticket"


class TicketStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ticket_panels (
                panel_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                panel_channel_id INTEGER NOT NULL,
                panel_message_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                form_type TEXT NOT NULL,
                staff_role_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                logging_channel_id INTEGER,
                image_url TEXT,
                thumbnail_url TEXT,
                footer TEXT
            );
            CREATE TABLE IF NOT EXISTS tickets (
                channel_id INTEGER PRIMARY KEY,
                message_id INTEGER,
                guild_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                panel_id INTEGER NOT NULL,
                staff_role_id INTEGER NOT NULL,
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
        for name, definition in (("panel_id", "INTEGER NOT NULL DEFAULT 0"), ("staff_role_id", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE tickets ADD COLUMN {name} {definition}")
        panel_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(ticket_panels)")}
        for name, definition in (("logging_channel_id", "INTEGER"), ("image_url", "TEXT"), ("thumbnail_url", "TEXT"), ("footer", "TEXT")):
            if name not in panel_columns:
                self.connection.execute(f"ALTER TABLE ticket_panels ADD COLUMN {name} {definition}")
        old_config = self.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'ticket_config'").fetchone()
        if old_config and self.connection.execute("SELECT COUNT(*) FROM ticket_panels").fetchone()[0] == 0:
            for row in self.connection.execute("SELECT * FROM ticket_config").fetchall():
                self.connection.execute(
                    "INSERT INTO ticket_panels (guild_id, panel_channel_id, panel_message_id, title, description, form_type, staff_role_id, category_id, logging_channel_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (row["guild_id"], row["panel_channel_id"], row["panel_message_id"], "Support Tickets", "Complete the form before a ticket is created.", "support", row["staff_role_id"], row["category_id"], row["logging_channel_id"]),
                )
        self.connection.commit()

    def one(self, query: str, values: tuple = ()) -> sqlite3.Row | None:
        return self.connection.execute(query, values).fetchone()

    def many(self, query: str, values: tuple = ()) -> list[sqlite3.Row]:
        return self.connection.execute(query, values).fetchall()

    def run(self, query: str, values: tuple = ()) -> sqlite3.Cursor:
        cursor = self.connection.execute(query, values)
        self.connection.commit()
        return cursor

    def close(self) -> None:
        self.connection.close()


class TicketModal(discord.ui.Modal):
    def __init__(self, system: "TicketSystem", panel: sqlite3.Row) -> None:
        self.system = system
        self.panel = panel
        super().__init__(title="Guild Application" if panel["form_type"] == "application" else "Support Ticket")
        if panel["form_type"] == "application":
            self.first = discord.ui.TextInput(label="Game UID", placeholder="Enter your in-game UID", max_length=100)
            self.second = discord.ui.TextInput(label="Rank", placeholder="Enter your current rank", max_length=100)
            self.third = discord.ui.TextInput(label="Age", placeholder="Enter your age", max_length=3)
            self.fourth = discord.ui.TextInput(label="Game name", placeholder="Enter your in-game name", max_length=100)
            self.fifth = discord.ui.TextInput(label="Why join our guild?", style=discord.TextStyle.paragraph, max_length=1000)
        else:
            self.first = discord.ui.TextInput(label="What is your issue?", placeholder="Explain your query in detail...", style=discord.TextStyle.paragraph, max_length=2000)
            self.second = discord.ui.TextInput(label="Extra details", required=False, max_length=1000)
            self.third = None
            self.fourth = None
            self.fifth = None
        self.add_item(self.first)
        self.add_item(self.second)
        if self.third:
            self.add_item(self.third)
        if self.fourth:
            self.add_item(self.fourth)
        if self.fifth:
            self.add_item(self.fifth)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        answers = [self.first.value, self.second.value, self.third.value if self.third else ""]
        if self.fourth:
            answers.extend([self.fourth.value, self.fifth.value])
        await self.system.create_ticket(interaction, self.panel, answers)


class TicketPanel(discord.ui.View):
    def __init__(self, system: "TicketSystem", panel_id: int) -> None:
        super().__init__(timeout=None)
        self.system = system
        button = discord.ui.Button(label="Open Ticket", style=discord.ButtonStyle.success, emoji="🎫", custom_id=f"ticket:open:{panel_id}")
        button.callback = self.open_ticket
        self.add_item(button)

    async def open_ticket(self, interaction: discord.Interaction) -> None:
        panel_id = int(interaction.data["custom_id"].rsplit(":", 1)[-1])
        panel = self.system.store.one("SELECT * FROM ticket_panels WHERE panel_id = ?", (panel_id,))
        if panel is None:
            return await interaction.response.send_message("This ticket panel is no longer configured.", ephemeral=True)
        await interaction.response.send_modal(TicketModal(self.system, panel))


class TicketActions(discord.ui.View):
    def __init__(self, system: "TicketSystem", channel_id: int) -> None:
        super().__init__(timeout=None)
        self.system = system
        self.channel_id = channel_id

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not isinstance(interaction.user, discord.Member) or not self.system.is_staff(interaction.user, interaction.guild.id):
            return await interaction.response.send_message("Only ticket staff can claim tickets.", ephemeral=True)
        self.system.store.run("UPDATE tickets SET claimed_by_id = ? WHERE channel_id = ?", (interaction.user.id, self.channel_id))
        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}.")

    @discord.ui.button(label="Lock", style=discord.ButtonStyle.secondary, custom_id="ticket:lock")
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not isinstance(interaction.user, discord.Member) or not self.system.is_staff(interaction.user, interaction.guild.id):
            return await interaction.response.send_message("Only ticket staff can lock tickets.", ephemeral=True)
        ticket = self.system.store.one("SELECT creator_id FROM tickets WHERE channel_id = ?", (self.channel_id,))
        member = interaction.guild.get_member(ticket["creator_id"]) if ticket else None
        if member:
            await interaction.channel.set_permissions(member, send_messages=False)
        self.system.store.run("UPDATE tickets SET is_locked = 1 WHERE channel_id = ?", (self.channel_id,))
        await interaction.response.send_message("Ticket locked.")

    @discord.ui.button(label="Unlock", style=discord.ButtonStyle.secondary, custom_id="ticket:unlock")
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not isinstance(interaction.user, discord.Member) or not self.system.is_staff(interaction.user, interaction.guild.id):
            return await interaction.response.send_message("Only ticket staff can unlock tickets.", ephemeral=True)
        ticket = self.system.store.one("SELECT creator_id FROM tickets WHERE channel_id = ?", (self.channel_id,))
        member = interaction.guild.get_member(ticket["creator_id"]) if ticket else None
        if member:
            await interaction.channel.set_permissions(member, send_messages=True)
        self.system.store.run("UPDATE tickets SET is_locked = 0 WHERE channel_id = ?", (self.channel_id,))
        await interaction.response.send_message("Ticket unlocked.")

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ticket = self.system.store.one("SELECT creator_id FROM tickets WHERE channel_id = ? AND closed_at IS NULL", (self.channel_id,))
        if not ticket or (interaction.user.id != ticket["creator_id"] and not self.system.is_staff(interaction.user, interaction.guild.id)):
            return await interaction.response.send_message("Only the ticket creator or staff can close this ticket.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self.system.close_ticket(interaction.channel, interaction.user, interaction)


class TicketSystem(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = TicketStore()

    async def load_views(self) -> None:
        await self.bot.wait_until_ready()
        for panel in self.store.many("SELECT panel_id, panel_message_id FROM ticket_panels"):
            self.bot.add_view(TicketPanel(self, panel["panel_id"]), message_id=panel["panel_message_id"])
        for ticket in self.store.many("SELECT channel_id, message_id FROM tickets WHERE closed_at IS NULL AND message_id IS NOT NULL"):
            self.bot.add_view(TicketActions(self, ticket["channel_id"]), message_id=ticket["message_id"])

    def is_staff(self, member: discord.Member, guild_id: int) -> bool:
        config = self.store.one("SELECT staff_role_id FROM ticket_panels WHERE guild_id = ? LIMIT 1", (guild_id,))
        return bool(member.guild_permissions.manage_channels or (config and member.get_role(config["staff_role_id"])))

    async def setup_panel(self, ctx: commands.Context, name: str, channel: discord.TextChannel, staff_role: discord.Role, category: discord.CategoryChannel, form_type: str) -> None:
        if ctx.guild is None or form_type not in {"support", "application"}:
            return await ctx.send("Form type must be `support` or `application`.")
        title = name[:256]
        description = "Complete the form before a ticket is created."
        cursor = self.store.run(
            "INSERT INTO ticket_panels (guild_id, panel_channel_id, panel_message_id, title, description, form_type, staff_role_id, category_id) VALUES (?, ?, 0, ?, ?, ?, ?, ?)",
            (ctx.guild.id, channel.id, title, description, form_type, staff_role.id, category.id),
        )
        panel_id = cursor.lastrowid
        embed = discord.Embed(title=title, description=description, color=discord.Color.red())
        embed.set_footer(text=f"{form_type.title()} | Complete the form before a ticket is created")
        message = await channel.send(embed=embed, view=TicketPanel(self, panel_id))
        self.store.run("UPDATE ticket_panels SET panel_message_id = ? WHERE panel_id = ?", (message.id, panel_id))
        self.bot.add_view(TicketPanel(self, panel_id), message_id=message.id)
        await ctx.send(f"{form_type.title()} panel `{panel_id}` created in {channel.mention}. Tickets will open under {category.mention}.")

    async def update_panel(self, interaction: discord.Interaction, panel_id: int, values: dict[str, str]) -> None:
        panel = self.store.one(
            "SELECT * FROM ticket_panels WHERE guild_id = ? AND (panel_id = ? OR panel_message_id = ? OR panel_channel_id = ?)",
            (interaction.guild.id if interaction.guild else 0, panel_id, panel_id, panel_id),
        )
        if panel is None:
            return await interaction.response.send_message("Panel ID not found in this server.", ephemeral=True)
        panel_channel = interaction.guild.get_channel(panel["panel_channel_id"]) if interaction.guild else None
        if not isinstance(panel_channel, discord.TextChannel):
            return await interaction.response.send_message("The panel channel no longer exists.", ephemeral=True)
        embed = discord.Embed(title=values["title"], description=values["description"], color=discord.Color.red())
        if values["image_url"]:
            embed.set_image(url=values["image_url"])
        if values["thumbnail_url"]:
            embed.set_thumbnail(url=values["thumbnail_url"])
        if values["footer"]:
            embed.set_footer(text=values["footer"])
        try:
            message = await panel_channel.fetch_message(panel["panel_message_id"])
            await message.edit(embed=embed, view=TicketPanel(self, panel_id))
        except discord.HTTPException:
            message = await panel_channel.send(embed=embed, view=TicketPanel(self, panel_id))
            self.store.run("UPDATE ticket_panels SET panel_message_id = ? WHERE panel_id = ?", (message.id, panel_id))
        self.store.run(
            "UPDATE ticket_panels SET title = ?, description = ?, image_url = ?, thumbnail_url = ?, footer = ? WHERE panel_id = ?",
            (values["title"], values["description"], values["image_url"], values["thumbnail_url"], values["footer"], panel_id),
        )
        self.bot.add_view(TicketPanel(self, panel_id), message_id=message.id)
        await interaction.response.send_message("Ticket panel customized.", ephemeral=True)

    async def create_ticket(self, interaction: discord.Interaction, panel: sqlite3.Row, answers: list[str]) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Tickets can only be opened in a server.", ephemeral=True)
        existing = self.store.one("SELECT channel_id FROM tickets WHERE guild_id = ? AND creator_id = ? AND closed_at IS NULL", (guild.id, interaction.user.id))
        if existing and guild.get_channel(existing["channel_id"]):
            return await interaction.response.send_message(f"You already have an open ticket: <#{existing['channel_id']}>", ephemeral=True)
        count = self.store.one("SELECT open_count FROM ticket_counts WHERE guild_id = ? AND user_id = ?", (guild.id, interaction.user.id))
        if count and count["open_count"] >= MAX_OPEN_TICKETS:
            return await interaction.response.send_message(f"You can have up to {MAX_OPEN_TICKETS} open tickets.", ephemeral=True)
        category = guild.get_channel(panel["category_id"])
        staff_role = guild.get_role(panel["staff_role_id"])
        if not isinstance(category, discord.CategoryChannel) or staff_role is None:
            return await interaction.response.send_message("This panel's category or staff role is missing.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False), interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True), staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True), guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)}
        channel = await category.create_text_channel(safe_name(f"{panel['form_type']}-{interaction.user.name}"), overwrites=overwrites)
        labels = ["Game UID", "Rank", "Age", "Game name", "Why join our guild?"] if panel["form_type"] == "application" else ["What is your issue?", "Extra details"]
        answer_lines = [f"**{label}**\n{answer}" for label, answer in zip(labels, answers) if answer]
        embed = discord.Embed(title=panel["title"], description="\n\n".join(answer_lines), color=discord.Color.red())
        message = await channel.send(content=f"{interaction.user.mention} {staff_role.mention}", embed=embed, view=TicketActions(self, channel.id))
        self.store.run("INSERT INTO tickets (channel_id, message_id, guild_id, creator_id, panel_id, staff_role_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (channel.id, message.id, guild.id, interaction.user.id, panel["panel_id"], staff_role.id, datetime.now(timezone.utc).isoformat()))
        self.store.run("INSERT INTO ticket_counts (guild_id, user_id, open_count) VALUES (?, ?, 1) ON CONFLICT(guild_id, user_id) DO UPDATE SET open_count = open_count + 1", (guild.id, interaction.user.id))
        self.bot.add_view(TicketActions(self, channel.id), message_id=message.id)
        await interaction.followup.send(f"Your ticket is ready: {channel.mention}", ephemeral=True)

    async def close_ticket(self, channel: discord.TextChannel, actor: discord.Member | discord.User, source: discord.Interaction) -> None:
        ticket = self.store.one("SELECT * FROM tickets WHERE channel_id = ? AND closed_at IS NULL", (channel.id,))
        if not ticket:
            return await source.followup.send("This is not an open ticket.", ephemeral=True)
        transcript = await self.transcript(channel)
        self.store.run("UPDATE tickets SET closed_at = ?, closed_by_id = ? WHERE channel_id = ?", (datetime.now(timezone.utc).isoformat(), actor.id, channel.id))
        self.store.run("UPDATE ticket_counts SET open_count = MAX(0, open_count - 1) WHERE guild_id = ? AND user_id = ?", (source.guild.id, ticket["creator_id"]))
        await self.log(source.guild, "Ticket closed", actor, channel, transcript, ticket["panel_id"])
        await source.followup.send("Ticket closed and transcript saved.", ephemeral=True)
        await channel.delete(reason=f"Ticket closed by {actor}")

    async def transcript(self, channel: discord.TextChannel) -> discord.File:
        lines = [f"Transcript: #{channel.name}", ""]
        async for message in channel.history(limit=None, oldest_first=True):
            content = message.clean_content or "[embed/attachment]"
            lines.append(f"[{message.created_at.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S UTC}] {message.author} ({message.author.id}): {content}")
            lines.extend(f"  Attachment: {attachment.url}" for attachment in message.attachments)
        return discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"{channel.name}-transcript.txt")

    async def log(self, guild: discord.Guild, title: str, actor: discord.Member | discord.User, channel: discord.abc.GuildChannel, transcript: discord.File | None, panel_id: int) -> None:
        panel = self.store.one("SELECT logging_channel_id FROM ticket_panels WHERE panel_id = ?", (panel_id,))
        log_channel = guild.get_channel(panel["logging_channel_id"]) if panel and panel["logging_channel_id"] else None
        if isinstance(log_channel, discord.TextChannel):
            embed = discord.Embed(title=title, color=discord.Color.red(), timestamp=datetime.now(timezone.utc))
            embed.add_field(name="Actor", value=f"{actor.mention} (`{actor.id}`)")
            embed.add_field(name="Channel", value=f"`{channel.name}` (`{channel.id}`)")
            await log_channel.send(embed=embed, file=transcript) if transcript else await log_channel.send(embed=embed)

    @commands.hybrid_group(name="ticket", description="Manage the ticket system.")
    @commands.guild_only()
    async def ticket(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @ticket.command(name="setup")
    @commands.has_permissions(manage_guild=True)
    async def ticket_setup(self, ctx: commands.Context, name: str, channel: discord.TextChannel, staff_role: discord.Role, category: discord.CategoryChannel, form_type: str = "support") -> None:
        await self.setup_panel(ctx, name, channel, staff_role, category, form_type.lower())

    @ticket.command(name="log")
    @commands.has_permissions(manage_guild=True)
    async def ticket_log(self, ctx: commands.Context, panel_id: int, channel: discord.TextChannel) -> None:
        if self.store.one("SELECT panel_id FROM ticket_panels WHERE panel_id = ? AND guild_id = ?", (panel_id, ctx.guild.id)) is None:
            return await ctx.send("Panel ID not found in this server.")
        self.store.run("UPDATE ticket_panels SET logging_channel_id = ? WHERE panel_id = ?", (channel.id, panel_id))
        await ctx.send(f"Ticket logs for panel `{panel_id}` will be sent to {channel.mention}.")

    @ticket.command(name="form")
    @commands.has_permissions(manage_guild=True)
    async def ticket_form(self, ctx: commands.Context, panel_id: int, form_type: str) -> None:
        form_type = form_type.lower()
        if form_type not in {"support", "application"}:
            return await ctx.send("Form type must be `support` or `application`.")
        panel = self.store.one(
            "SELECT panel_id FROM ticket_panels WHERE guild_id = ? AND (panel_id = ? OR panel_message_id = ? OR panel_channel_id = ?)",
            (ctx.guild.id, panel_id, panel_id, panel_id),
        )
        if panel is None:
            return await ctx.send("Panel ID not found in this server.")
        self.store.run("UPDATE ticket_panels SET form_type = ? WHERE panel_id = ?", (form_type, panel["panel_id"]))
        await ctx.send(f"Panel `{panel['panel_id']}` is now configured as `{form_type}`.")

    @ticket.command(name="customize")
    @commands.has_permissions(manage_guild=True)
    async def ticket_customize(self, ctx: commands.Context, panel_id: int) -> None:
        panel = self.store.one(
            "SELECT panel_id FROM ticket_panels WHERE guild_id = ? AND (panel_id = ? OR panel_message_id = ? OR panel_channel_id = ?)",
            (ctx.guild.id, panel_id, panel_id, panel_id),
        )
        if panel is None:
            return await ctx.send("Panel ID not found in this server.")
        await ctx.send(f"Customize panel `{panel['panel_id']}`:", view=CustomPanelView(self, panel["panel_id"]))


async def setup(bot: commands.Bot) -> None:
    cog = TicketSystem(bot)
    await bot.add_cog(cog)
    bot.loop.create_task(cog.load_views())


class CustomPanelView(discord.ui.View):
    def __init__(self, system: TicketSystem, panel_id: int) -> None:
        super().__init__(timeout=300)
        self.system = system
        self.panel_id = panel_id

    @discord.ui.button(label="Customize Ticket Panel", style=discord.ButtonStyle.primary)
    async def customize(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CustomPanelModal(self.system, self.panel_id))


class CustomPanelModal(discord.ui.Modal, title="Customize Ticket Panel"):
    panel_title = discord.ui.TextInput(label="Title", placeholder="Support Tickets", max_length=256)
    panel_description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, placeholder="Complete the form before a ticket is created.", max_length=4000)
    image_url = discord.ui.TextInput(label="Banner image URL", required=False, max_length=1000)
    thumbnail_url = discord.ui.TextInput(label="Thumbnail URL", required=False, max_length=1000)
    footer = discord.ui.TextInput(label="Footer", required=False, max_length=2048)

    def __init__(self, system: TicketSystem, panel_id: int) -> None:
        super().__init__()
        self.system = system
        self.panel_id = panel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.system.update_panel(
            interaction,
            self.panel_id,
            {
                "title": str(self.panel_title),
                "description": str(self.panel_description),
                "image_url": str(self.image_url).strip(),
                "thumbnail_url": str(self.thumbnail_url).strip(),
                "footer": str(self.footer).strip(),
            },
        )
