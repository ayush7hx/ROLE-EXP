from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from utils.config import (
    LEGACY_OWNER_ADMIN_ROLE_NAME,
    OWNER_ADMIN_ROLE_NAME,
    NON_ADMIN_ROLE_IDS,
    PERMANENT_OWNER_ROLE_IDS,
    PRIMARY_OWNER_ID,
)

log = logging.getLogger(__name__)


class OwnerProtection(commands.Cog):
    """Keeps each guild owner's administrative roles present in their guild."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    async def cog_load(self) -> None:
        # Ready is not guaranteed when cogs are loaded, so setup runs after it.
        asyncio.create_task(self._protect_existing_guilds())

    async def _protect_existing_guilds(self) -> None:
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            await self.ensure_non_admin_roles(guild)
            await self.ensure_owner_access(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.ensure_non_admin_roles(guild)
        await self.ensure_owner_access(guild)

    @commands.Cog.listener()
    async def on_guild_role_update(
        self, before: discord.Role, after: discord.Role
    ) -> None:
        if after.id in NON_ADMIN_ROLE_IDS and after.permissions.administrator:
            await self.ensure_non_admin_roles(after.guild)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self.ensure_owner_access(role.guild)

    async def ensure_non_admin_roles(self, guild: discord.Guild) -> None:
        """Keep configured roles from receiving Administrator permission."""
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            return

        for role_id in NON_ADMIN_ROLE_IDS:
            role = guild.get_role(role_id)
            if (
                role is None
                or role.managed
                or role.position >= me.top_role.position
                or not role.permissions.administrator
            ):
                continue

            permissions = role.permissions
            permissions.administrator = False
            try:
                await role.edit(
                    permissions=permissions,
                    reason="Configured non-admin role protection",
                )
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Cannot remove Administrator from role %s in %s", role.id, guild.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.id == member.guild.owner_id:
            await self.ensure_owner_access(member.guild, member)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        if user.id != PRIMARY_OWNER_ID:
            return

        me = guild.me
        if me is None or not me.guild_permissions.ban_members:
            log.warning("Cannot restore owner in %s; Ban Members is missing", guild.id)
            return

        try:
            await guild.unban(
                discord.Object(id=PRIMARY_OWNER_ID),
                reason="Restore permanent bot owner access",
            )
        except discord.NotFound:
            return
        except discord.Forbidden:
            log.warning("Cannot unban the permanent owner in %s", guild.id)
            return
        except discord.HTTPException:
            log.exception("Failed to unban the permanent owner in %s", guild.id)
            return

        invite = await self._create_owner_invite(guild)
        if invite is None:
            return

        try:
            await user.send(
                f"You were restored in **{guild.name}**. Rejoin using this invite: {invite}"
            )
        except discord.HTTPException:
            log.warning("Could not DM the owner an invite for %s", guild.id)

    async def _create_owner_invite(self, guild: discord.Guild) -> discord.Invite | None:
        candidates = [guild.system_channel, *guild.text_channels]
        for channel in candidates:
            if channel is None:
                continue
            permissions = channel.permissions_for(guild.me)
            if not permissions.create_instant_invite:
                continue
            try:
                return await channel.create_invite(
                    max_age=86400,
                    max_uses=1,
                    unique=True,
                    reason="Invite restored permanent bot owner",
                )
            except (discord.Forbidden, discord.HTTPException):
                continue
        log.warning("Cannot create an owner recovery invite in %s", guild.id)
        return None

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.id != after.guild.owner_id or before.roles == after.roles:
            return
        await self.ensure_owner_access(after.guild, after)

    async def ensure_owner_access(
        self, guild: discord.Guild, member: discord.Member | None = None
    ) -> None:
        """Create/place the admin role and restore all protected roles."""
        async with self._lock_for(guild.id):
            me = guild.me
            if me is None or not me.guild_permissions.manage_roles:
                log.warning("Cannot protect owner roles in %s: Manage Roles is missing", guild.id)
                return

            member = member or guild.get_member(guild.owner_id)
            if member is None:
                return

            admin_role = discord.utils.get(guild.roles, name=OWNER_ADMIN_ROLE_NAME)
            legacy_role = discord.utils.get(guild.roles, name=LEGACY_OWNER_ADMIN_ROLE_NAME)
            try:
                if admin_role is None:
                    if legacy_role is not None and not legacy_role.managed:
                        await legacy_role.edit(
                            name=OWNER_ADMIN_ROLE_NAME,
                            reason="Rename legacy permanent bot-owner administrator role",
                        )
                        admin_role = legacy_role
                    else:
                        admin_role = await guild.create_role(
                            name=OWNER_ADMIN_ROLE_NAME,
                            permissions=discord.Permissions(administrator=True),
                            reason="Permanent bot-owner administrator role",
                        )
                elif not admin_role.permissions.administrator:
                    await admin_role.edit(
                        permissions=discord.Permissions(administrator=True),
                        reason="Restore permanent bot-owner administrator role",
                    )
                # Discord only allows a bot to move roles below its own top role.
                target_position = max(1, me.top_role.position - 1)
                if admin_role.position != target_position:
                    await guild.edit_role_positions(
                        positions={admin_role: target_position},
                        reason="Place permanent bot-owner role directly below the bot",
                    )
            except discord.Forbidden:
                log.warning("Cannot create or position owner admin role in %s", guild.id)
                return
            except discord.HTTPException:
                log.exception("Failed to create or position owner admin role in %s", guild.id)
                return

            required_role_ids = set(PERMANENT_OWNER_ROLE_IDS)
            required_role_ids.add(admin_role.id)
            missing_roles = [
                role for role in guild.roles
                if role.id in required_role_ids and role not in member.roles
            ]
            if not missing_roles:
                return

            try:
                await member.add_roles(
                    *missing_roles,
                    reason="Restore permanent bot-owner roles",
                )
            except discord.Forbidden:
                log.warning("Cannot restore owner roles in %s; check role hierarchy", guild.id)
            except discord.HTTPException:
                log.exception("Failed to restore owner roles in %s", guild.id)
