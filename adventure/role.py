from datetime import datetime, timezone
import logging
from typing import Optional

import discord
from discord.ext import tasks
from discord.ext.commands.cooldowns import BucketType
from discord.role import Role
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.commands import Context
from redbot.core.i18n import Translator, cog_i18n

from .charsheet import parse_timedelta
from .utils import smart_embed

_ = Translator("Adventure", __file__)

log = logging.getLogger("red.cogs.adventure")

@cog_i18n(_)
class RoleMixin(commands.Cog):
    def __init__(self, bot: Red):
        self.bot = bot

        self.config: Config

    @commands.group(name="roleset")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _roleset(self, ctx: Context):
        """Set roles for adventure pings."""

    @_roleset.command(name="general")
    async def _roleset_general(self, ctx: Context, *, role: discord.Role = None):
        """Set role for all adventure pings."""

        await self.config.guild(ctx.guild).general_ping_role.set(getattr(role, "id", None))

        if not role:
            await smart_embed(ctx, _("Unset all adventures role."), True)
        else:
            await smart_embed(
                ctx,
                _("Set {role} as all adventures role.".format(role=role.mention)),
                True
            )

    @_roleset.command(name="boss")
    async def _roleset_boss(self, ctx: Context, *, role: discord.Role = None):
        """Set role for boss-only adventure pings."""

        await self.config.guild(ctx.guild).boss_ping_role.set(getattr(role, "id", None))

        if not role:
            await smart_embed(ctx, _("Unset bose-only adventures role."), True)
        else:
            await smart_embed(
                ctx,
                _("Set {role} as boss-only adventures role.".format(role=role.mention)),
                True
            )

    async def make_mentionable(self, role: Role) -> bool:
        if role.mentionable:
            return

        await role.edit(reason=_("Adventure ping"), mentionable=True)

    async def make_unmentionable(self, role: Role):
        if not role.mentionable:
            return

        await role.edit(reason=_("Adventure ping"), mentionable=False)

    async def get_role(self, guild: discord.Guild, role_iden: str) -> Optional[discord.Role]:
        role_id = await getattr(self.config.guild(guild), role_iden)()

        if not role_id:
            return

        return guild.get_role(role_id)

    @commands.command()
    @commands.cooldown(rate=1, per=5, type=BucketType.guild)
    @commands.bot_has_permissions(manage_roles=True)
    @commands.guild_only()
    async def pingadv(self, ctx: Context):
        """Ping the all adventures role."""

        role_id = await self.config.guild(ctx.guild).general_ping_role()
        if not role_id:
            return await smart_embed(ctx, _("Role is not set."))

        role = ctx.guild.get_role(role_id)
        if not role:
            return await smart_embed(ctx, _("I could not find the set role."))

        try:
            await self.make_mentionable(role)
        except discord.HTTPException:
            log.exception(_("There was an error editing role permissions."))
        except discord.Forbidden:
            log.exception(
                _("I don't have the permission to edit role permissions in {guild}.").format(
                    guild=role.guild.name
                )
            )

        try:
            await ctx.send(
                _("{mention}").format(mention=role.mention),
                allowed_mentions=discord.AllowedMentions(roles=True)
            )
        finally:
            try:
                await self.make_unmentionable(role)
            except discord.HTTPException:
                log.exception(_("There was an error editing role permissions."))
            except discord.Forbidden:
                log.exception(
                    _("I don't have the permission to edit role permissions in {guild}.").format(
                        guild=role.guild.name
                    )
                )

    @commands.command()
    @commands.cooldown(rate=1, per=10, type=BucketType.guild)
    @commands.bot_has_permissions(manage_roles=True)
    @commands.guild_only()
    async def pingboss(self, ctx: Context):
        """Ping the boss-only adventures role."""

        role_id = await self.config.guild(ctx.guild).boss_ping_role()
        if not role_id:
            await smart_embed(ctx, _("Role is not set."))

        role = ctx.guild.get_role(role_id)
        if not role:
            await smart_embed(ctx, _("I could not find the set role."))

        try:
            await self.make_mentionable(role)
        except discord.HTTPException:
            log.exception(_("There was an error editing role permissions."))
        except discord.Forbidden:
            log.exception(
                _("I don't have the permission to edit role permissions in {guild}.").format(
                    guild=role.guild.name
                )
            )

        try:
            await ctx.send(
                _("{mention}").format(mention=role.mention),
                allowed_mentions=discord.AllowedMentions(roles=True)
            )
        finally:
            try:
                await self.make_unmentionable(role)
            except discord.HTTPException:
                log.exception(_("There was an error editing role permissions."))
            except discord.Forbidden:
                log.exception(
                    _("I don't have the permission to edit role permissions in {guild}.").format(
                        guild=role.guild.name
                    )
                )

    async def add_ping_role(self, ctx: Context, role: discord.Role, duration: Optional[str], role_type: str):
        async def add_role():
            try:
                await ctx.author.add_roles(role)
            except discord.HTTPException:
                log.exception(_("Adding role failed for unknown reason."))
                return False
            except discord.Forbidden:
                log.exception(
                    _("I don't have the permissions to add roles in {guild}.").format(guild=ctx.guild)
                )
                return False

        if not duration:
            return await add_role()

        delta = parse_timedelta(duration)
        if not delta:
            return await smart_embed(ctx, _("Invalid duration provided."))

        if await add_role() is False:
            return False

        # the replace is needed to get the UTC timestamp
        remove_at = (datetime.utcnow() + delta).replace(tzinfo=timezone.utc).timestamp()

        async with self.config.guild(ctx.guild).timed_roles() as timed_roles:
            timed_roles[role_type][str(ctx.author.id)] = remove_at

    @commands.command()
    @commands.guild_only()
    @commands.bot_has_permissions(manage_roles=True)
    async def advrole(self, ctx: Context, *, duration: str = None):
        """Adds the all adventure role for optionally specified duration.

        Duration can be specified like `2days 4h5m 2sec` to mean 2 days, 4 hours,
        5 minutes and 2 seconds.
        """

        role = await self.get_role(ctx.guild, "general_ping_role")
        if not role:
            return await smart_embed(ctx, _("All adventures role is not set."))

        if await self.add_ping_role(ctx, role, duration, "general") is False:
            return await smart_embed(ctx, _("Unable to add the role for unknown reason."))

        await ctx.tick()

    @commands.command()
    @commands.guild_only()
    @commands.bot_has_permissions(manage_roles=True)
    async def bossrole(self, ctx: Context, *, duration: str = None):
        """Adds the boss-only adventure role for optionally specified duration.

        Duration can be specified like `2days 4h5m 2sec` to mean 2 days, 4 hours,
        5 minutes and 2 seconds.
        """

        role = await self.get_role(ctx.guild, "boss_ping_role")
        if not role:
            return await smart_embed(ctx, _("Boss-only adventures role is not set."))

        if await self.add_ping_role(ctx, role, duration, "boss") is False:
            return await smart_embed(ctx, _("Unable to add the role for unknown reason."))

        await ctx.tick()

    async def remove_ping_role(self, user: discord.Member, role: discord.Role):
        try:
            await user.remove_roles(role)
        except discord.HTTPException:
            log.exception(_("Removing role failed for unknown reason."))
        except discord.Forbidden:
            log.exception(
                _("I don't have the permissions to remove roles in {guild}.").format(guild=user.guild)
            )

        return True

    @commands.command()
    @commands.guild_only()
    @commands.bot_has_permissions(manage_roles=True)
    async def radvrole(self, ctx: Context):
        """Removes the all adventure role."""

        role = await self.get_role(ctx.guild, "general_ping_role")
        if not role:
            return await smart_embed(ctx, _("All adventures role is not set."))

        if not await self.remove_ping_role(ctx.author, role):
            return await smart_embed(ctx, _("Unable to remove the role for unknown reason."))

        async with self.config.guild(ctx.guild).timed_roles() as timed_roles:
            str_id = str(ctx.author.id)
            if str_id in timed_roles["general"]:
                del timed_roles["general"][str_id]

        await ctx.tick()

    @commands.command()
    @commands.guild_only()
    @commands.bot_has_permissions(manage_roles=True)
    async def rbossrole(self, ctx: Context):
        """Removes the boss-only adventure role for optionally specified duration."""

        role = await self.get_role(ctx.guild, "boss_ping_role")
        if not role:
            return await smart_embed(ctx, _("Boss-only adventures role is not set."))

        if not await self.remove_ping_role(ctx.author, role):
            return await smart_embed(ctx, _("Unable to remove the role for unknown reason."))


        async with self.config.guild(ctx.guild).timed_roles() as timed_roles:
            str_id = str(ctx.author.id)
            if str_id in timed_roles["boss"]:
                del timed_roles["boss"][str_id]

        await ctx.tick()

    @tasks.loop(seconds=20)
    async def timed_roles_task(self):
        for guild in self.bot.guilds:
            general_role = await self.get_role(guild, "general_ping_role")
            boss_role = await self.get_role(guild, "boss_ping_role")

            if not general_role and not boss_role:
                continue
            now = datetime.utcnow().replace(tzinfo=timezone.utc).timestamp()

            timed_roles = await self.config.guild(guild).timed_roles()
            async with self.config.guild(guild).timed_roles() as timed_roles:
                if general_role:
                    remove_general = []
                    for str_id, ts in timed_roles["general"].items():
                        if now > ts:
                            user = guild.get_member(int(str_id))
                            if user:
                                await self.remove_ping_role(user, general_role)

                            remove_general.append(str_id)
                    for k in remove_general: del timed_roles["general"][k]

                if boss_role:
                    remove_boss = []
                    for str_id, ts in timed_roles["boss"].items():
                        if now > ts:
                            user = guild.get_member(int(str_id))
                            if user:
                                await self.remove_ping_role(user, boss_role)

                            remove_boss.append(str_id)
                    for k in remove_boss: del timed_roles["boss"][k]
