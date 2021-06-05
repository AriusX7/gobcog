import asyncio
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
from .utils import smart_embed, AdventureCheckFailure

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
        """Set adventure related roles."""

    @_roleset.command(name="general")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _roleset_general(self, ctx: Context, *, role: discord.Role = None):
        """Set role for all adventure pings."""

        await self.config.guild(ctx.guild).general_ping_role.set(getattr(role, "id", None))

        if not role:
            await smart_embed(ctx, _("Unset all adventures role."), success=True)
        else:
            await smart_embed(
                ctx,
                _("Set {role} as all adventures role.").format(role=role.mention),
                success=True
            )

    @_roleset.command(name="boss")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _roleset_boss(self, ctx: Context, *, role: discord.Role = None):
        """Set role for boss-only adventure pings."""

        await self.config.guild(ctx.guild).boss_ping_role.set(getattr(role, "id", None))

        if not role:
            await smart_embed(ctx, _("Unset boss-only adventures role."), success=True)
        else:
            await smart_embed(
                ctx,
                _("Set {role} as boss-only adventures role.").format(role=role.mention),
                success=True
            )

    @_roleset.command(name="adventure")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _roleset_adventure(self, ctx: Context, *, role: discord.Role):
        """Set role for adventure."""

        await self.config.guild(ctx.guild).adventure_role.set(getattr(role, "id", None))

        await smart_embed(
            ctx,
            _("Set {role} as adventure role.").format(role=role.mention),
            success=True
        )

    @_roleset.command(name="noadventure")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _roleset_noadventure(self, ctx: Context, *, role: discord.Role):
        """Set role for no adventure."""

        await self.config.guild(ctx.guild).noadventure_role.set(getattr(role, "id", None))

        await smart_embed(
            ctx,
            _("Set {role} as noadventure role.").format(role=role.mention),
            success=True
        )

    @_roleset.command(name="rebirth")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _roleset_rebirth(self, ctx: Context, *, role: discord.Role = None):
        """Set role for people at and above rebirth 5."""

        await self.config.guild(ctx.guild).rebirth_role.set(getattr(role, "id", None))

        if not role:
            await smart_embed(ctx, _("Unset rebirth 5 role."), success=True)
        else:
            await smart_embed(
                ctx,
                _("Set {role} as rebirth 5 role.").format(role=role.mention),
                success=True
            )

    @staticmethod
    async def make_mentionable(role: Role) -> bool:
        if role.mentionable:
            return

        await role.edit(reason=_("Adventure ping"), mentionable=True)

    @staticmethod
    async def make_unmentionable(role: Role):
        if not role.mentionable:
            return

        await role.edit(reason=_("Adventure ping"), mentionable=False)

    async def get_role(self, guild: discord.Guild, role_iden: str) -> Optional[discord.Role]:
        role_id = await getattr(self.config.guild(guild), role_iden)()

        if not role_id:
            return

        return guild.get_role(role_id)

    async def ping(self, ctx, role_iden: str):
        role = await self.get_role(ctx.guild, role_iden + "_ping_role")
        if not role:
            raise AdventureCheckFailure(_("I could not find the set role."))

        session = self._sessions.get(ctx.channel.id)

        if session is None:
            raise AdventureCheckFailure(_("You must be in an adventure to use this command."))

        if role_iden == 'boss' and not session.boss and not session.transcended:
            raise AdventureCheckFailure(
                _("You must be fighting a boss or transcended monster to use this command. Use `{prefix}pingadv` instead!").format(prefix=ctx.prefix)
            )

        try:
            await self.make_mentionable(role)
        except discord.HTTPException:
            log.exception(_("There was an error editing role permissions."))
            return
        except discord.Forbidden:
            log.exception(
                _("I don't have the permission to edit role permissions in {guild}.").format(
                    guild=role.guild.name
                )
            )
            return
        try:
            await ctx.send(_(
                    "{mention}, an adventurer needs your assistance in fighting the"
                    " **{session.attribute} {session.challenge}** ahead!"
                ).format(
                    mention=role.mention, session=session
                ),
                allowed_mentions=discord.AllowedMentions(roles=True)
            )
            await asyncio.sleep(2)
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
    @commands.cooldown(rate=1, per=180, type=BucketType.guild)
    @commands.bot_has_permissions(manage_roles=True)
    @commands.guild_only()
    async def pingadv(self, ctx: Context):
        """Ping the all adventures role."""
        await self.ping(ctx, 'general')

    @commands.command()
    @commands.cooldown(rate=1, per=300, type=BucketType.guild)
    @commands.bot_has_permissions(manage_roles=True)
    @commands.guild_only()
    async def pingboss(self, ctx: Context):
        """Ping the transcended or boss-only adventures role."""
        await self.ping(ctx, 'boss')

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
            raise AdventureCheckFailure(_("Invalid duration provided."))

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
            raise AdventureCheckFailure(_("All adventures role is not set."))

        if await self.add_ping_role(ctx, role, duration, "general") is False:
            raise AdventureCheckFailure(_("Unable to add the role for unknown reason."))

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
            raise AdventureCheckFailure(_("Boss-only adventures role is not set."))

        if await self.add_ping_role(ctx, role, duration, "boss") is False:
            raise AdventureCheckFailure(_("Unable to add the role for unknown reason."))

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
            raise AdventureCheckFailure(_("All adventures role is not set."))

        if not await self.remove_ping_role(ctx.author, role):
            raise AdventureCheckFailure(_("Unable to remove the role for unknown reason."))

        async with self.config.guild(ctx.guild).timed_roles() as timed_roles:
            str_id = str(ctx.author.id)
            if str_id in timed_roles["general"]:
                del timed_roles["general"][str_id]

        await ctx.tick()

    @commands.command()
    @commands.guild_only()
    @commands.bot_has_permissions(manage_roles=True)
    async def rbossrole(self, ctx: Context):
        """Removes the boss-only adventure role."""

        role = await self.get_role(ctx.guild, "boss_ping_role")
        if not role:
            raise AdventureCheckFailure(_("Boss-only adventures role is not set."))

        if not await self.remove_ping_role(ctx.author, role):
            raise AdventureCheckFailure(_("Unable to remove the role for unknown reason."))


        async with self.config.guild(ctx.guild).timed_roles() as timed_roles:
            str_id = str(ctx.author.id)
            if str_id in timed_roles["boss"]:
                del timed_roles["boss"][str_id]

        await ctx.tick()

    async def add_rebirths_role(self, guild: discord.Guild, user: discord.Member):
        role = await self.get_role(guild, "rebirth_role")
        if role and role not in user.roles:
            await user.add_roles(role)

    async def remove_adv_role(self, guild: discord.Guild, user: discord.Member):
        role = await self.get_role(guild, "adventure_role")
        if role and role in user.roles:
            await user.remove_roles(role)

    @commands.group(name="reactrole")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_roles=True)
    async def _reactrole(self, ctx: Context):
        """Settings related to the adventure reaction role."""

    @_reactrole.command(name="emoji")
    async def _reactrole_emoji(self, ctx: Context, emoji: discord.Emoji):
        """Set the react role emoji. You must set the react role message first."""

        async with self.config.guild(ctx.guild).react_role() as react_role:
            channel_id = react_role["channel"]
            message_id = react_role["message"]
            if not channel_id or not message_id:
                raise AdventureCheckFailure(_("Reaction roles channel and message not set."))

            channel = ctx.guild.get_channel(channel_id)
            if not channel:
                raise AdventureCheckFailure(_("Reaction roles channel cannot be found."))
            try:
                message: discord.Message = await channel.fetch_message(message_id)
            except Exception:
                raise AdventureCheckFailure(_("Reaction roles message cannot be found."))

            try:
                await message.add_reaction(emoji)
            except Exception:
                raise AdventureCheckFailure(_("Cannot react to the reaction roles message."))

            react_role["emoji"]["name"] = emoji.name
            react_role["emoji"]["id"] = emoji.id

        await ctx.tick()

    @_reactrole.command(name="message")
    async def _reactrole_message(self, ctx: Context, message_id: int, *, channel: discord.TextChannel):
        """Set the react role message."""

        async with self.config.guild(ctx.guild).react_role() as react_role:
            react_role["message"] = message_id
            react_role["channel"] = channel.id
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

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        adv_role = await self.get_role(after.guild, "adventure_role")
        rebirth_role = await self.get_role(after.guild, "rebirth_role")
        noadv_role = await self.get_role(after.guild, "noadventure_role")
        if adv_role and noadv_role:
            if before.roles != after.roles:
                if all(x in after.roles for x in (adv_role, noadv_role)):
                    # remove adv_role
                    await after.remove_roles(adv_role, reason='NoAdv and Adv role cannot be applied at the same time. Remove NoAdv role to disable this behaviour.')

                if all(x in after.roles for x in (rebirth_role, noadv_role)):
                    # remove rebirth_role
                    await after.remove_roles(rebirth_role, reason='NoAdv and Rebirth role cannot be applied at the same time. Remove NoAdv role to disable this behaviour.')

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        member = payload.member

        # `member` is `None` in DMs.
        if not member or member.bot:
            return

        guild = member.guild

        # `emoji` is a `PartialEmoji`.
        emoji = payload.emoji

        react_role = await self.config.guild(guild).react_role()

        if (
            react_role["message"] != payload.message_id
            or react_role["channel"] != payload.channel_id
            or react_role["emoji"]["name"] != emoji.name
            or react_role["emoji"]["id"] != emoji.id
        ):
            return

        await self.remove_reaction(guild, payload.channel_id, payload.message_id, emoji, member)

        try:
            rebirths = await self.config.user(member).get_raw("rebirths")
        except KeyError:
            rebirths = 1

        if rebirths >= 5:
            await self.add_rebirths_role(guild, member)
        else:
            role = await self.get_role(guild, "adventure_role")
            if role and role not in member.roles:
                await member.add_roles(role)

    @staticmethod
    async def remove_reaction(
        guild: discord.Guild,
        channel_id: int,
        message_id: int,
        emoji: discord.PartialEmoji,
        member: discord.Member
    ):
        channel = guild.get_channel(channel_id)
        if not channel:
            return
        try:
            message = await channel.fetch_message(message_id)
        except Exception:
            return

        try:
            await message.remove_reaction(emoji, member)
        except Exception:
            pass
