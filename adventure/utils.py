import logging
from typing import List, MutableMapping

import discord
from discord.ext import commands
from discord.ext.commands import BadArgument, CheckFailure, Converter
from redbot.core.commands import Context, check
from redbot.core.i18n import Translator

from . import bank

_ = Translator("Adventure", __file__)
log = logging.getLogger("red.cogs.adventure")

async def smart_embed(ctx, message, success=None):
    # use_emebd has been disabled here.. for reasons
    if await ctx.embed_requested():
        if success is True:
            colour = discord.Colour.dark_green()
        elif success is False:
            colour = discord.Colour.dark_red()
        else:
            colour = await ctx.embed_colour()
        return await ctx.send(embed=discord.Embed(description=message, color=colour))
    else:
        return await ctx.send(message)


def check_global_setting_admin():
    """
    Command decorator. If the bank is not global, it checks if the author is 
    either a bot admin or has the manage_guild permission.
    """

    async def pred(ctx: commands.Context):
        author = ctx.author
        if not await bank.is_global():
            if not isinstance(ctx.channel, discord.abc.GuildChannel):
                return False
            if await ctx.bot.is_owner(author):
                return True
            if author == ctx.guild.owner:
                return True
            if ctx.channel.permissions_for(author).manage_guild:
                return True
            admin_role_ids = await ctx.bot.get_admin_role_ids(ctx.guild.id)
            for role in author.roles:
                if role.id in admin_role_ids:
                    return True
        else:
            return await ctx.bot.is_owner(author)

    return commands.check(pred)


def has_separated_economy():
    async def predicate(ctx):
        if not (ctx.cog and getattr(ctx.cog, "_separate_economy", False)):
            raise CheckFailure
        return True

    return check(predicate)


class DynamicInt(Converter):
    async def convert(self, ctx, argument):
        if argument == "all":
            return argument
        elif argument.endswith("%"):
            if argument[:-1].isnumeric():
                return argument

        if argument.isnumeric():
            return int(argument)

        raise BadArgument(_('{} is not a valid number and is not "all" or a percentage.').format(argument))


class Member(commands.converter.MemberConverter):
    """Overwrites original memberconverter to be case insensitive"""

    async def query_member_named(self, guild, argument):
        # try to get it by using case insensitivity first and weak connection
        argument = argument.lower()
        result = discord.utils.find(
            lambda m: m.name.lower() == argument or getattr(m.nick, 'lower', lambda: '')() == argument
                   or m.name.lower().startswith(argument) or getattr(m.nick, 'lower', lambda: '')().startswith(argument),
            guild.members
        )
        if result:
            return result

        # else fallback to original (with case insensitivty)
        cache = guild._state._member_cache_flags.joined
        if len(argument) > 5 and argument[-5] == '#':
            username, _, discriminator = argument.rpartition('#')
            members = await guild.query_members(username, limit=100, cache=cache)
            return discord.utils.find(lambda m: m.name.lower() == username and m.discriminator == discriminator, members)
        else:
            members = await guild.query_members(argument, limit=100, cache=cache)
            return discord.utils.find(lambda m: m.name.lower() == argument or getattr(m.nick, 'lower', None) == argument, members)


class AdventureResults:
    """Object to store recent adventure results."""

    def __init__(self, num_raids):
        self._num_raids = num_raids
        self._last_raids: MutableMapping[int, List] = {}

    def add_result(self, ctx: Context, main_action, amount, num_ppl, success):
        """Add result to this object.
        :main_action: Main damage action taken by the adventurers
            (highest amount dealt). Should be either "attack" or
            "talk". Running will just be notated by a 0 amount.
        :amount: Amount dealt.
        :num_ppl: Number of people in adventure.
        :success: Whether adventure was successful or not.
        """
        if ctx.guild.id not in self._last_raids:
            self._last_raids[ctx.guild.id] = []

        if len(self._last_raids.get(ctx.guild.id, [])) >= self._num_raids:
            if ctx.guild.id in self._last_raids:
                self._last_raids[ctx.guild.id].pop(0)
        raid_dict = {}
        for var in ("main_action", "amount", "num_ppl", "success"):
            raid_dict[var] = locals()[var]
        self._last_raids[ctx.guild.id].append(raid_dict)

    def get_stat_range(self, ctx: Context):
        """Return reasonable stat range for monster pool to have based
        on last few raids' damage.

        :returns: Dict with stat_type, min_stat and max_stat.
        """
        # how much % to increase damage for solo raiders so that they
        # can't just solo every monster based on their own average
        # damage
        if ctx.guild.id not in self._last_raids:
            self._last_raids[ctx.guild.id] = []
        SOLO_RAID_SCALE = 0.25
        if len(self._last_raids.get(ctx.guild.id, [])) == 0:
            return {"stat_type": "hp", "min_stat": 0, "max_stat": 0}

        # tally up stats for raids
        num_attack = 0
        dmg_amount = 0
        num_talk = 0
        talk_amount = 0
        num_wins = 0
        stat_type = "hp"
        avg_amount = 0
        raids = self._last_raids.get(ctx.guild.id, [])[-3:]  # only use last 3 raids for stat measurement
        raid_count = len(raids)
        if raid_count == 0:
            num_wins = self._num_raids // 2
            raid_count = self._num_raids
            win_percent = 0.6
        else:
            for raid in raids:
                if raid["main_action"] == "attack":
                    num_attack += 1
                    dmg_amount += raid["amount"]
                    if raid["num_ppl"] == 1:
                        dmg_amount += raid["amount"] * SOLO_RAID_SCALE
                else:
                    num_talk += 1
                    talk_amount += raid["amount"]
                    if raid["num_ppl"] == 1:
                        talk_amount += raid["amount"] * SOLO_RAID_SCALE
                log.debug(f"raid dmg: {raid['amount']}")
                if raid["success"]:
                    num_wins += 1
            if num_attack > 0:
                avg_amount = dmg_amount / num_attack
            if dmg_amount < talk_amount:
                stat_type = "dipl"
                avg_amount = talk_amount / num_talk
            win_percent = num_wins / raid_count
            min_stat = avg_amount * 0.75
            max_stat = avg_amount * 2
            # want win % to be at least 50%, even when solo
            # if win % is below 50%, scale back min/max for easier mons
            if win_percent < 0.6:
                min_stat = avg_amount * win_percent
                max_stat = avg_amount * 1.5

        stats_dict = {}
        for var in ("stat_type", "min_stat", "max_stat", "win_percent"):
            stats_dict[var] = locals()[var]
        return stats_dict

    def __str__(self):
        return str(self._last_raids)


class AdventureCheckFailure(commands.CheckFailure):
    pass
