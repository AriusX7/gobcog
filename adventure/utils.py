import asyncio
import contextlib
import logging
import time
from typing import List, MutableMapping

import discord
from discord.ext import commands
from discord.utils import get
from redbot.core.utils.chat_formatting import humanize_timedelta
from discord.ext.commands import BadArgument, CheckFailure, Converter
from redbot.core.commands import Context, check
from redbot.core.i18n import Translator
from redbot.core.utils.menus import prev_page, next_page

from . import bank

_ = Translator("Adventure", __file__)
log = logging.getLogger("red.cogs.adventure")

async def smart_embed(ctx, message, success=None, **kwargs):
    # use_emebd has been disabled here.. for reasons
    if await ctx.embed_requested():
        if success is True:
            colour = discord.Colour.green()
        elif success is False:
            colour = discord.Colour.red()
        else:
            colour = discord.Colour.blurple()
        return await ctx.send(embed=discord.Embed(description=message, color=colour), **kwargs)
    else:
        return await ctx.send(message, **kwargs)


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


def can_use_ability():
    async def predicate(ctx):
        heroclass = {
            'bless': 'Cleric',
            'rage': 'Berserker',
            'focus': 'Wizard',
            'music': 'Bard'
        }

        async with ctx.cog.get_lock(ctx.author):
            c = await ctx.cog.get_character_from_json(ctx.author)
            if c.heroclass["name"] != heroclass[ctx.command.name]:
                raise AdventureCheckFailure(
                    _("**{name}**, you need to be a {heroclass} to do this.").format(name=ctx.cog.escape(ctx.author.display_name), heroclass=heroclass[ctx.command.name])
                )
            else:
                if c.heroclass["ability"]:
                    raise AdventureCheckFailure(
                        _("**{}**, ability already in use.").format(ctx.cog.escape(ctx.author.display_name))
                    )
                cooldown_time = max(240, (1140 - ((c.luck + c.total_int) * 2)))
                if "cooldown" not in c.heroclass:
                    c.heroclass["cooldown"] = cooldown_time + 1
                if c.heroclass["cooldown"] > time.time():
                    cooldown_time = c.heroclass["cooldown"] - time.time()
                    raise AdventureOnCooldown(
                        message=_(
                            "Your hero is currently recovering from the last time "
                            "they used this skill. Try again in {delay}."
                        ),
                        retry_after=cooldown_time
                    )
        
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

    def add_result(self, ctx: Context, main_action, amount, num_ppl, success, boss):
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
        for var in ("main_action", "amount", "num_ppl", "success", "boss"):
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
        raids = self._last_raids.get(ctx.guild.id, [])
        raid_count = len(raids)
        if raid_count == 0:
            num_wins = self._num_raids // 2
            raid_count = self._num_raids
            win_percent = 0.5
        else:
            avg_count = 3
            winrate_count = 6

            for n, raid in enumerate(reversed(raids)):
                if n < avg_count:
                    if not raid.get("amount"):
                        # Incrementing `avg_count` makes sure we still consider 3 raids (if possible).
                        avg_count += 1
                        # Similarly, incrementing `winrate_count` makes sure we consider 6 raids (if possible).
                        winrate_count += 1
                        continue
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
                if raid["success"] and n < winrate_count:
                    num_wins += 1
            if num_attack > 0:
                avg_amount = dmg_amount / num_attack
            if dmg_amount < talk_amount:
                stat_type = "dipl"
                avg_amount = talk_amount / num_talk
            win_percent = num_wins / min(winrate_count, raid_count)
            min_stat = avg_amount * 0.75
            max_stat = avg_amount * 2
            # want win % to be at least 50%, even when solo
            # if win % is below 50%, scale back min/max for easier mons
            if win_percent < 0.5:
                min_stat = avg_amount * win_percent
                max_stat = avg_amount * 1.5

        stats_dict = {}
        for var in ("stat_type", "min_stat", "max_stat", "win_percent"):
            stats_dict[var] = locals()[var]
        return stats_dict

    def can_spawn_boss(self, ctx):
        """Ensures that the last 2 monsters are not bosses"""
        raids = self._last_raids.get(ctx.guild.id, [])[-2:]
        if any(i["boss"] for i in raids):
            return False
        return True

    def __str__(self):
        return str(self._last_raids)

    def __getstate__(self):
        state = self._last_raids.copy()
        state["num_raids"] = self._num_raids
        return state

    def __setstate__(self, state):
        self._num_raids = state["num_raids"]
        del state["num_raids"]
        self._last_raids: MutableMapping[int, List] = state

class AdventureCheckFailure(commands.CheckFailure):
    pass

class AdventureOnCooldown(AdventureCheckFailure):
    def __init__(self, retry_after, *, message=None):
        self.retry_after = int(retry_after)

        if message is None:
            message = _("This command is on cooldown. Try again in {delay}.")

        message = message.format(
            delay=humanize_timedelta(seconds=self.retry_after) if self.retry_after >= 1 else _("1 second")
        )
        super().__init__(message)


class FilterInt:
    def __init__(self, val: int, sign: str):
        self.val = val
        self.sign = sign

    @classmethod
    async def convert(cls, __: commands.Context, argument: str):
        if argument.endswith("+") or argument.endswith("-"):
            if argument[0:-1].isnumeric():
                return cls(int(argument[0:-1]), argument[-1])
        elif argument.startswith("+") or argument.startswith("-"):
            if argument[1:].isnumeric():
                return cls(int(argument[1:]), argument[0])
        elif argument.isnumeric():
            return cls(int(argument), None)

        raise BadArgument(_('{} is not a valid filter number.').format(argument))

    def is_valid(self, x):
        return (self.sign == '+' and x > self.val) or (self.sign == '-' and x < self.val) or (self.sign == None and x == self.val)


class FilterStr:
    def __init__(self, val: str, sign: str):
        self.val = val
        self.sign = sign

    @classmethod
    async def convert(cls, __: commands.Context, argument: str):
        if argument.endswith("+") or argument.endswith("-"):
            return cls(argument[0:-1], argument[-1])
        elif argument.startswith("+") or argument.startswith("-"):
            return cls(argument[1:], argument[0])

        raise BadArgument(_('{} is not a valid filter string.').format(argument))

    def is_valid(self, x):
        x = x.lower()
        val = self.val.lower()
        return (self.sign == '+' and val in x) or (self.sign == '-' and val not in x)


def start_adding_reactions(
    message: discord.Message, emojis
) -> asyncio.Task:
    """
    [Overwrites original Red function to add a 0.3s delay]
    Start adding reactions to a message.

    This is a non-blocking operation - calling this will schedule the
    reactions being added, but the calling code will continue to
    execute asynchronously. There is no need to await this function.

    This is particularly useful if you wish to start waiting for a
    reaction whilst the reactions are still being added - in fact,
    this is exactly what `menu` uses to do that.

    Parameters
    ----------
    message: discord.Message
        The message to add reactions to.
    emojis : Iterable[Union[str, discord.Emoji]]
        The emojis to react to the message with.

    Returns
    -------
    asyncio.Task
        The task for the coroutine adding the reactions.

    """

    async def task():
        # The task should exit silently if the message is deleted
        with contextlib.suppress(discord.NotFound):
            for emoji in emojis:
                await message.add_reaction(emoji)
                await asyncio.sleep(0.3)

    return asyncio.create_task(task())


async def close_menu(
    ctx: commands.Context,
    pages: list,
    controls: dict,
    message: discord.Message,
    page: int,
    timeout: float,
    emoji: str,
):
    with contextlib.suppress(discord.NotFound):
        await ctx.tick()
        await message.delete()



MENU_CONTROLS = {
    "\N{LEFTWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}": prev_page,
    "\N{CROSS MARK}": close_menu,
    "\N{BLACK RIGHTWARDS ARROW}\N{VARIATION SELECTOR-16}": next_page,
}