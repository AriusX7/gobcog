# -*- coding: utf-8 -*-
import asyncio
import contextlib
import io
import json
import logging
import os
import random
import re
import time
from collections import OrderedDict, namedtuple
from datetime import date, datetime
from operator import itemgetter
from types import SimpleNamespace
from typing import MutableMapping, Optional

import discord
from discord.ext.commands.errors import BadArgument
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.commands import Context, get_dict_converter
from redbot.core.data_manager import bundled_data_path, cog_data_path
from redbot.core.errors import BalanceTooHigh
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils import AsyncIter
from redbot.core.utils.chat_formatting import box, humanize_list, humanize_number, humanize_timedelta, pagify
from redbot.core.utils.menus import menu
from redbot.core.utils.predicates import MessagePredicate, ReactionPredicate
from tabulate import tabulate

from . import bank
from .charsheet import (
    ORDER,
    RARITIES,
    AllItemConverter,
    ArgumentConverter,
    Character,
    DayConverter,
    EquipableItemConverter,
    EquipmentConverter,
    Item,
    ItemConverter,
    PercentageConverter,
    RarityConverter,
    SkillConverter,
    SlotConverter,
    Stats,
    ThemeSetMonterConverter,
    ThemeSetPetConverter,
    can_equip,
    equip_level,
    has_funds,
    no_dev_prompt,
    parse_timedelta,
)
from .menus import (
    BaseMenu,
    LeaderboardMenu,
    LeaderboardSource,
    ScoreBoardMenu,
    ScoreboardSource,
    WeeklyScoreboardSource,
)
from .misc import MiscMixin
from .role import RoleMixin
from .utils import (
    AdventureResults,
    DynamicInt,
    Emojis,
    FilterInt,
    FilterStr,
    Member,
    UserCtx,
    check_global_setting_admin,
    can_use_ability,
    has_separated_economy, order_slots_dict,
    smart_embed,
    AdventureCheckFailure,
    AdventureOnCooldown,
    start_adding_reactions,
    MENU_CONTROLS,
    is_dm,
)

_ = Translator("Adventure", __file__)

log = logging.getLogger("red.cogs.adventure")

TaxesConverter = get_dict_converter(delims=[" ", ",", ";"])


@cog_i18n(_)
class Adventure(MiscMixin, RoleMixin, commands.Cog):
    """Adventure, derived from the Goblins Adventure cog by locastan."""

    __version__ = "3.3.8"

    def __init__(self, bot: Red):
        self.bot = bot
        bank._init(bot)
        self._last_trade = {}
        self._adv_results = AdventureResults(20)
        self.emojis = SimpleNamespace()
        self.emojis.fumble = "\N{EXCLAMATION QUESTION MARK}"
        self.emojis.level_up = "\N{BLACK UP-POINTING DOUBLE TRIANGLE}"
        self.emojis.rebirth = "\N{BABY SYMBOL}"
        self.emojis.rage = Emojis.rage
        self.emojis.autoaim = Emojis.autoaim
        self.emojis.rant = Emojis.rant
        self.emojis.pray = Emojis.pray
        self.emojis.run = "\N{RUNNER}"
        self.emojis.crit = "\N{COLLISION SYMBOL}"
        self.emojis.magic_crit = "\N{HIGH VOLTAGE SIGN}"
        self.emojis.berserk = "\N{RIGHT ANGER BUBBLE}"
        self.emojis.dice = "\N{GAME DIE}"
        self.emojis.yes = "\N{WHITE HEAVY CHECK MARK}"
        self.emojis.no = "\N{NEGATIVE SQUARED CROSS MARK}"
        self.emojis.sell = "\N{MONEY BAG}"
        self.emojis.skills = SimpleNamespace()
        self.emojis.skills.report = Emojis.report,
        # self.emojis.skills.psychic = "\N{SIX POINTED STAR WITH MIDDLE DOT}"
        self.emojis.skills.berserker = Emojis.berserker
        self.emojis.skills.autoaimer1 = Emojis.autoaimer1
        self.emojis.skills.autoaimer2 = Emojis.autoaimer2
        self.emojis.skills.tilter1 = Emojis.tilter1
        self.emojis.skills.tilter2 = Emojis.tilter2
        self.emojis.hp = "\N{HEAVY BLACK HEART}\N{VARIATION SELECTOR-16}"
        self.emojis.dipl = self.emojis.rant

        self._adventure_actions = [
            self.emojis.rage,
            self.emojis.autoaim,
            self.emojis.rant,
            self.emojis.pray,
        ]
        self._adventure_controls = {
            "rage": self.emojis.rage,
            "autoaim": self.emojis.autoaim,
            "rant": self.emojis.rant,
            "pray": self.emojis.pray,
            "run": self.emojis.run,
        }
        self._order = [
            "head",
            "neck",
            "chest",
            "gloves",
            "belt",
            "legs",
            "boots",
            "left",
            "right",
            "two handed",
            "ring",
            "charm",
        ]
        self._treasure_controls = {
            self.emojis.yes: "equip",
            self.emojis.no: "backpack",
            self.emojis.sell: "sell",
        }
        self._yes_no_controls = {self.emojis.yes: "yes", self.emojis.no: "no"}

        self._adventure_countdown = {}
        self._rewards = {}
        self._trader_countdown = {}
        self._current_traders = {}
        self._curent_trader_stock = {}
        self._react_messaged = []
        self.tasks = {}
        self.locks: MutableMapping[int, asyncio.Lock] = {}
        self.gb_task = None

        self.config = Config.get_conf(self, 2_710_801_001, force_registration=True)
        self._daily_bonus = {}
        self._separate_economy = None

        default_user = {
            "exp": 0,
            "lvl": 1,
            "att": 0,
            "cha": 0,
            "int": 0,
            "last_skill_reset": 0,
            "last_known_currency": 0,
            "last_currency_check": 0,
            "treasure": [0, 0, 0, 0, 0, 0],
            "items": {
                "head": {},
                "neck": {},
                "chest": {},
                "gloves": {},
                "belt": {},
                "legs": {},
                "boots": {},
                "left": {},
                "right": {},
                "ring": {},
                "charm": {},
                "backpack": {},
            },
            "loadouts": {},
            "class": {"name": _("Hero"), "ability": False, "desc": _("Your basic adventuring hero."), "cooldown": 0,},
            "skill": {"pool": 0, "att": 0, "cha": 0, "int": 0},
        }

        default_guild = {
            "cart_channels": [],
            "god_name": "",
            "cart_name": "",
            "embed": True,
            "cartroom": None,
            "cart_timeout": 10800,
            "cooldown_timer_manual": 120,
            "rebirth_cost": 100.0,
            "disallow_withdraw": True,
            "max_allowed_withdraw": 50000,
            "error_channel": None,
            "general_ping_role": None,
            "boss_ping_role": None,
            "adventure_role": None,
            "noadventure_role": None,
            "muted_role": None,
            "timed_roles": {
                # The dictionaries are of the type `"user_id": timestamp`
                "general": {},
                "boss": {}
            },
            "rebirth_role": None,
            "react_role_emote": {
                "name": None,
                "id": None,
            },
            "react_role": {
                "emoji": {
                    "name": None,
                    "id": None,
                },
                "channel": None,
                "message": None,
                "rmemoji": {
                    "name": None,
                    "id": None,
                },
            },
        }

        default_channel = {
            "cooldown": 0,
        }

        default_global = {
            "god_name": _("Herbert"),
            "cart_name": _("Hawl's brother"),
            "theme": "default",
            "restrict": False,
            "embed": True,
            "enable_chests": True,
            "currentweek": date.today().isocalendar()[1],
            "schema_version": 1,
            "rebirth_cost": 100.0,
            "themes": {},
            "daily_bonus": {"1": 0, "2": 0, "3": 0.5, "4": 0, "5": 0.5, "6": 1.0, "7": 1.0},
            "tax_brackets": {"1000": 0.1, "5000": 0.2, "10000": 0.3, "50000": 0.4, "100000": 0.5},
            "separate_economy": True,
            "to_conversion_rate": 10,
            "from_conversion_rate": 11,
            "max_allowed_withdraw": 50000,
            "disallow_withdraw": False,
        }
        self.RAISINS: list = None
        self.THREATEE: list = None
        self.TR_GEAR_SET: dict = None
        self.ATTRIBS: dict = None
        self.MONSTERS: dict = None
        self.AS_MONSTERS: dict = None
        self.MONSTER_NOW: dict = None
        self.LOCATIONS: list = None
        self.PETS: dict = None

        self.config.register_guild(**default_guild)
        self.config.register_channel(**default_channel)
        self.config.register_global(**default_global)
        self.config.register_user(**default_user)
        self.cleanup_loop = self.bot.loop.create_task(self.cleanup_tasks())
        log.debug("Creating Task")
        self._init_task = self.bot.loop.create_task(self.initialize())
        self._timed_roles_task = self.timed_roles_task.start()
        self._ready_event = asyncio.Event()

    @commands.command()
    @commands.bot_has_permissions(add_reactions=True)
    @commands.is_owner()
    async def makecart(self, ctx: Context):
        """[Dev] Force a cart to appear."""
        if not await no_dev_prompt(ctx):
            return
        await self._trader(ctx, True)

    @commands.command()
    @commands.is_owner()
    async def genitems(self, ctx: Context, rarity: str, slot: str, num: int = 1):
        """[Dev] Generate random items."""
        if not await no_dev_prompt(ctx):
            return
        user = ctx.author
        rarity = rarity.lower()
        slot = slot.lower()
        if rarity not in RARITIES:
            raise AdventureCheckFailure(
                _("Invalid rarity; choose one of {list}.").format(list=humanize_list(RARITIES))
            )
        elif slot not in ORDER:
            raise AdventureCheckFailure(_("Invalid slot; choose one of {list}.").format(list=humanize_list(ORDER)))
        async with self.get_lock(user):
            c = await self.get_character_from_json(user)
            for i in range(num):
                await c.add_to_backpack(await self._genitem(rarity, slot))
            await self.config.user(ctx.author).set(await c.to_json(self.config))
        await ctx.invoke(self._backpack)

    @commands.command()
    @commands.bot_has_permissions(add_reactions=True)
    @commands.is_owner()
    async def copyuser(self, ctx: Context, user_id: int):
        """[Owner] Copy another members data to yourself.

        Note this overrides your current data.
        """
        user = namedtuple("User", "id")
        user = user(user_id)
        user_data = await self.config.user(user).all()
        await self.config.user(ctx.author).set(user_data)
        await ctx.tick()

    @commands.command(name="ebackpack", usage="--diff --level --degrade --rarity --order --slot --name")
    @commands.bot_has_permissions(add_reactions=True)
    async def commands_equipable_backpack(
        self,
        ctx: Context,
        *, args: ArgumentConverter(
            OrderedDict((
                ('diff', bool),
                ('level', FilterInt),
                ('degrade', FilterInt),
                ('rarity', RarityConverter),
                ('order', SkillConverter),
                ('slot', SlotConverter),
                ('name', FilterStr)
            )),
            allow_multiple=['level', 'degrade', 'name']
        )=None
    ):
        """This shows the contents of your backpack that can be equipped.

        Give it a rarity and/or slot to filter what backpack items to show.

        Note: An item **degrade** level is how many rebirths it will last, before it is broken down.
        """
        if args:
            show_diff = args['diff']
            level = args['level']
            degrade = args['degrade']
            rarity = args['rarity']
            sort_order = args['order']
            slot = args['slot']
            name = args['name']
        else:
            show_diff = None
            level = []
            degrade = []
            rarity = None
            sort_order = None
            slot = None
            name = []

        assert isinstance(rarity, str) or rarity is None
        assert isinstance(slot, str) or slot is None
        if not await self.allow_in_dm(ctx):
            raise AdventureCheckFailure(_("This command is not available in DM's on this bot."))
        if not ctx.invoked_subcommand:
            if ctx.guild:
                raise AdventureCheckFailure(_("This command is only available in DMs."))
            c = await self.get_character_from_json(ctx.author)
            if rarity:
                rarity = rarity.lower()
                if rarity not in RARITIES:
                    raise AdventureCheckFailure(
                        _("{} is not a valid rarity, select one of {}").format(rarity, humanize_list(RARITIES)),
                    )
            if slot:
                slot = slot.lower()
                if slot not in ORDER:
                    raise AdventureCheckFailure(
                        _("{} is not a valid slot, select one of {}").format(slot, humanize_list(ORDER))
                    )

            backpack_contents = _("{author}'s backpack \n\n{backpack}\n").format(
                author=self.escape(ctx.author.display_name),
                backpack=await c.get_backpack(name=name, level=level, degrade=degrade, rarity=rarity, slot=slot, show_delta=show_diff, equippable=True, sort_order=sort_order),
            )
            msgs = []
            async for page in AsyncIter(pagify(backpack_contents, delims=["\n"], shorten_by=20, page_length=1900)):
                msgs.append(box(page, lang="css"))
            return await menu(ctx, msgs, MENU_CONTROLS)

    @commands.command(name="ubackpack", usage="--diff --level --degrade --rarity --order --slot --name")
    @commands.bot_has_permissions(add_reactions=True)
    async def commands_unequipable_backpack(
        self,
        ctx: Context,
        *, args: ArgumentConverter(
            OrderedDict((
                ('diff', bool),
                ('level', FilterInt),
                ('degrade', FilterInt),
                ('rarity', RarityConverter),
                ('order', SkillConverter),
                ('slot', SlotConverter),
                ('name', FilterStr)
            )),
            allow_multiple=['level', 'degrade', 'name']
        )=None

    ):
        """This shows the contents of your backpack that cannot be equipped.

        Give it a rarity and/or slot to filter what backpack items to show.

        Note: An item **degrade** level is how many rebirths it will last, before it is broken down.
        """
        if args:
            show_diff = args['diff']
            level = args['level']
            degrade = args['degrade']
            rarity = args['rarity']
            sort_order = args['order']
            slot = args['slot']
            name = args['name']
        else:
            show_diff = None
            level = []
            degrade = []
            rarity = None
            sort_order = None
            slot = None
            name = []

        assert isinstance(rarity, str) or rarity is None
        assert isinstance(slot, str) or slot is None
        if not ctx.invoked_subcommand:
            c = await self.get_character_from_json(ctx.author)
            if rarity:
                rarity = rarity.lower()
                if rarity not in RARITIES:
                    raise AdventureCheckFailure(
                        _("{} is not a valid rarity, select one of {}").format(rarity, humanize_list(RARITIES)),
                    )
            if slot:
                slot = slot.lower()
                if slot not in ORDER:
                    raise AdventureCheckFailure(
                        _("{} is not a valid slot, select one of {}").format(slot, humanize_list(ORDER)),
                    )

            backpack_contents = _("{author}'s backpack \n\n{backpack}\n").format(
                author=self.escape(ctx.author.display_name),
                backpack=await c.get_backpack(name=name, level=level, degrade=degrade, rarity=rarity, slot=slot, show_delta=show_diff, unequippable=True, sort_order=sort_order),
            )
            msgs = []
            async for page in AsyncIter(pagify(backpack_contents, delims=["\n"], shorten_by=20, page_length=1900)):
                msgs.append(box(page, lang="css"))
            return await menu(ctx, msgs, MENU_CONTROLS)

    @commands.group(name="backpack", autohelp=False, usage="--diff --level --degrade --rarity --order --slot --name", invoke_without_command=True)
    @commands.bot_has_permissions(add_reactions=True)
    async def _backpack(
        self,
        ctx: Context,
        *, args: ArgumentConverter(
            OrderedDict((
                ('diff', bool),
                ('level', FilterInt),
                ('degrade', FilterInt),
                ('rarity', RarityConverter),
                ('order', SkillConverter),
                ('slot', SlotConverter),
                ('name', FilterStr)
            )),
            allow_multiple=['level', 'degrade', 'name']
        )=None
    ):
        """This shows the contents of your backpack.

        Give it a rarity and/or slot to filter what backpack items to show.

        Selling:     `[p]backpack sell item_name`
        Trading:     `[p]backpack trade @user price item_name`
        Equip:       `[p]backpack equip item_name`
        Sell All:    `[p]backpack sellall rarity slot`
        Disassemble: `[p]backpack disassemble item_name`

        Note: An item **degrade** level is how many rebirths it will last, before it is broken down.
        """
        if args:
            show_diff = args['diff']
            level = args['level']
            degrade = args['degrade']
            rarity = args['rarity']
            sort_order = args['order']
            slot = args['slot']
            name = args['name']
        else:
            show_diff = None
            level = []
            degrade = []
            rarity = None
            sort_order = None
            slot = None
            name = []

        assert isinstance(rarity, str) or rarity is None
        assert isinstance(slot, str) or slot is None

        if not ctx.invoked_subcommand:
            if ctx.guild:
                raise AdventureCheckFailure(_("This command is only available in DMs."))
            c = await self.get_character_from_json(ctx.author)
            if rarity:
                rarity = rarity.lower()
                if rarity not in RARITIES:
                    raise AdventureCheckFailure(
                        _("{} is not a valid rarity, select one of {}").format(rarity, humanize_list(RARITIES)),
                    )
            if slot:
                slot = slot.lower()
                if slot not in ORDER:
                    raise AdventureCheckFailure(
                        _("{} is not a valid slot, select one of {}").format(slot, humanize_list(ORDER)),
                    )

            backpack_contents = _("{author}'s backpack \n\n{backpack}\n").format(
                author=self.escape(ctx.author.display_name),
                backpack=await c.get_backpack(name=name, level=level, degrade=degrade, rarity=rarity, slot=slot, show_delta=show_diff, sort_order=sort_order),
            )
            msgs = []
            async for page in AsyncIter(pagify(backpack_contents, delims=["\n"], shorten_by=20, page_length=1900)):
                msgs.append(box(page, lang="css"))
            controls = MENU_CONTROLS.copy()

            async def _backpack_info(
                ctx: commands.Context,
                pages: list,
                controls: MutableMapping,
                message: discord.Message,
                page: int,
                timeout: float,
                emoji: str,
            ):
                if message:
                    await ctx.send_help(self._backpack)
                    with contextlib.suppress(discord.HTTPException):
                        await message.delete()
                    return None

            controls["\N{INFORMATION SOURCE}\N{VARIATION SELECTOR-16}"] = _backpack_info
            return await menu(ctx, msgs, controls)

    @_backpack.command(name="equip")
    @is_dm()
    async def backpack_equip(self, ctx: Context, *, equip_item: EquipableItemConverter):
        """Equip an item from your backpack."""
        assert isinstance(equip_item, Item)
        if self.in_adventure(ctx):
            raise AdventureCheckFailure(
                _("You tried to equip an item but the monster ahead of you commands your attention."),
            )
        async with self.get_lock(ctx.author):
            c = await self.get_character_from_json(ctx.author)
            equiplevel = equip_level(c, equip_item)
            if self.is_dev(ctx.author):  # FIXME:
                equiplevel = 0

            if not can_equip(c, equip_item):
                raise AdventureCheckFailure(
                    _("You need to be level `{level}` to equip this item.").format(level=equiplevel),
                )

            equip = c.backpack.get(equip_item.name)
            if equip:
                slot = equip.slot[0]
                if len(equip.slot) > 1:
                    slot = "two handed"
                if not getattr(c, equip.slot[0]):
                    equip_msg = box(
                        _("{author} equipped {item} ({slot} slot).").format(
                            author=self.escape(ctx.author.display_name), item=str(equip), slot=slot
                        ),
                        lang="css",
                    )
                else:
                    equip_msg = box(
                        _("{author} equipped {item} ({slot} slot) and put {put} into their backpack.").format(
                            author=self.escape(ctx.author.display_name),
                            item=str(equip),
                            slot=slot,
                            put=" and ".join(str(getattr(c, i)) for i in equip.slot if getattr(c, i, None)),
                        ),
                        lang="css",
                    )
                await ctx.send(equip_msg)
                c = await c.equip_item(equip, True, self.is_dev(ctx.author))  # FIXME:
                await self.config.user(ctx.author).set(await c.to_json(self.config))

    @_backpack.command(name="disassemble")
    async def backpack_disassemble(self, ctx: Context, *, backpack_item: ItemConverter):
        """
        Disassemble a set item from your backpack.
        This will provide a chance for a chest,
        or the item might break while you are handling it...
        """
        assert isinstance(backpack_item, Item)
        if self.in_adventure(ctx):
            raise AdventureCheckFailure(
                _("You tried to disassemble an item but the monster ahead of you commands your attention."),
            )
        async with self.get_lock(ctx.author):
            character = await self.get_character_from_json(ctx.author)
            try:
                item = character.backpack[backpack_item.name]
            except KeyError:
                return

            if item.rarity != "set":
                raise AdventureCheckFailure(_("You can only disassemble set items."))
            if character.heroclass["name"] != "Tinkerer":
                roll = random.randint(0, 1)
            else:
                roll = random.randint(0, 3)

            if roll == 0:
                item.owned -= 1
                if item.owned <= 0:
                    del character.backpack[item.name]
                await self.config.user(ctx.author).set(await character.to_json(self.config))
                return await smart_embed(
                    ctx, _("Your attempt at disassembling {} failed and it has been destroyed.").format(item.name),
                )
            else:
                item.owned -= 1
                if item.owned <= 0:
                    del character.backpack[item.name]
                character.treasure[3] += roll
                await self.config.user(ctx.author).set(await character.to_json(self.config))
                return await smart_embed(
                    ctx,
                    _("Your attempt at disassembling {} was successful and you have received {} legendary {}.").format(
                        item.name, roll, _("chests") if roll > 1 else _("chest")
                    ),
                )

    @_backpack.command(name="sellall", usage ='--name --level --degrade --rarity --slot')
    @is_dm()
    async def backpack_sellall(
        self, ctx: Context,
        *, args: ArgumentConverter(
            OrderedDict((
                ('level', FilterInt),
                ('degrade', FilterInt),
                ('rarity', RarityConverter),
                ('slot', SlotConverter),
                ('name', FilterStr)
            )),
            allow_multiple=['level', 'degrade', 'name']
        )=None
    ):
        """Sell all items in your backpack. Optionally specify name filter, degrade filter, level filter, rarity or slot.

        Level filter can be any number (level) followed by a `+` or a `-` sign. For example,
        if `70+` is specified, all items that can only be equipped above level 70 will be sold.

        Degrade filter works the same as level filters but only work for legendary and ascended items.

        Name filter works similarly to level and degrade filters, allowing you to include/exclude results.

        Note: The level filter has to be specified (e.g. 0+) to use the degrade filter
        """
        if args:
            name = args['name']
            level = args['level']
            degrade = args['degrade']
            rarity = args['rarity']
            slot = args['slot']
        else:
            name = []
            level = []
            degrade = []
            rarity = None
            slot = None

        assert isinstance(rarity, str) or rarity is None
        assert isinstance(slot, str) or slot is None
        if self.in_adventure(ctx):
            raise AdventureCheckFailure(
                _("You tried to go sell your items but the monster ahead is not allowing you to leave."),
            )
        if rarity:
            rarity = rarity.lower()
            if rarity not in RARITIES:
                raise AdventureCheckFailure(
                    _("{} is not a valid rarity, select one of {}").format(rarity, humanize_list(RARITIES)),
                )
            if rarity.lower() in ["set", "forged"]:
                raise AdventureCheckFailure(_("You cannot sell `{rarity}` rarity items.").format(rarity=rarity))
        if slot:
            slot = slot.lower()
            if slot not in ORDER:
                raise AdventureCheckFailure(
                    _("{} is not a valid slot, select one of {}").format(slot, humanize_list(ORDER)),
                )

        if level:
            vals = []
            for i in level:
                if i.sign == "+":
                    vals.append(_("above level {}").format(i.val))
                elif i.sign == "-":
                    vals.append(_("below level {}").format(i.val))
                elif i.sign == None:
                    vals.append(_("at level {}").format(i.val))
            level_str = " " + " and ".join(vals)
        else:
            level_str = ""

        if degrade:
            vals = []
            for i in degrade:
                if i.sign == "+":
                    vals.append(_("above degrade {}").format(i.val))
                elif i.sign == "-":
                    vals.append(_("below degrade {}").format(i.val))
                elif i.sign == None:
                    vals.append(_("at degrade {}").format(i.val))
            degrade_str = " " + " and ".join(vals)
        else:
            degrade_str = ""

        if rarity and rarity not in ('all', 'legendary', 'ascended'):
            degrade_str = ""

        if name:
            vals = []
            for i in name:
                if i.sign == "+":
                    vals.append(_("with name {}").format(i.val))
                elif i.sign == "-":
                    vals.append(_("without name {}").format(i.val))
            name_str = " " + " and ".join(vals)
        else:
            name_str = ""

        async with self.get_lock(ctx.author):
            fmt = ""
            c = await self.get_character_from_json(ctx.author)
            total_price = 0
            async with ctx.typing():
                items = [i for n, i in c.backpack.items() if i.rarity not in ["forged", "set"]]
                async for item in AsyncIter(items):
                    e_level = equip_level(c, item)

                    if name and not all(x.is_valid(item.name) for x in name):
                        continue

                    if level and not all(x.is_valid(e_level) for x in level):
                        continue

                    if degrade and not all(x.is_valid(item.degrade) for x in degrade):
                        continue

                    if rarity and item.rarity != rarity:
                        continue
                    if slot:
                        if len(item.slot) == 1 and slot != item.slot[0]:
                            continue
                        elif len(item.slot) == 2 and slot != "two handed":
                            continue
                    item_price = 0
                    old_owned = item.owned
                    async for x in AsyncIter(range(0, item.owned)):
                        item.owned -= 1
                        item_price += self._sell(c, item)
                        if item.owned <= 0:
                            del c.backpack[item.name]
                    item_price = max(item_price, 0)
                    fmt += _("{old_item} sells for {price}.\n").format(
                        old_item=str(old_owned) + " " + str(item), price=humanize_number(item_price),
                    )
                    total_price += item_price

            msg_list = []
            new_msg = _("Are you sure you want to sell all your{rarity} items{level}{degrade}{name} for {price}?\n\n{items}").format(
                author=self.escape(ctx.author.display_name),
                rarity=f" {rarity}" if rarity else "",
                price=humanize_number(total_price),
                items=fmt,
                level=level_str,
                degrade=degrade_str,
                name=name_str
            )
            msg = await ctx.send('Loading...')
            for page in pagify(new_msg, shorten_by=10, page_length=1900):
                msg_list.append(box(page, lang="css"))
            self.bot.loop.create_task(menu(ctx, msg_list, MENU_CONTROLS, message=msg))

            start_adding_reactions(msg, ReactionPredicate.YES_OR_NO_EMOJIS)
            pred = ReactionPredicate.yes_or_no(msg, ctx.author)
            try:
                await ctx.bot.wait_for("reaction_add", check=pred, timeout=60)
            except asyncio.TimeoutError:
                for r in ReactionPredicate.YES_OR_NO_EMOJIS:
                    await msg.remove_reaction(r, self.bot.user)
                return

            for r in ReactionPredicate.YES_OR_NO_EMOJIS:
                await msg.remove_reaction(r, self.bot.user)
    
            if not pred.result:
                await ctx.send("Not selling those items.")
                return

            for r in ReactionPredicate.YES_OR_NO_EMOJIS:
                await msg.remove_reaction(r, self.bot.user)
            await self.config.user(ctx.author).set(await c.to_json(self.config))
            
            if total_price > 0:
                try:
                    await bank.deposit_credits(ctx.author, total_price)
                except BalanceTooHigh as e:
                    await bank.set_balance(ctx.author, e.max_balance)
            c.last_known_currency = await bank.get_balance(ctx.author)
            c.last_currency_check = time.time()

            await ctx.send('Items sold.')


    @_backpack.command(name="sell", cooldown_after_parsing=True)
    @commands.cooldown(rate=3, per=60, type=commands.BucketType.user)
    async def backpack_sell(self, ctx: Context, *, item: ItemConverter):
        """Sell an item from your backpack."""

        if self.in_adventure(ctx):
            raise AdventureCheckFailure(
                _("You tried to go sell your items but the monster ahead is not allowing you to leave."),
            )
        if item.rarity == "forged":
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(
                box(
                    _("\n{author}, your {device} is refusing to be sold and bit your finger for trying.").format(
                        author=self.escape(ctx.author.display_name), device=str(item)
                    ),
                    lang="css",
                )
            )

        lock = self.get_lock(ctx.author)
        await lock.acquire()
        c = await self.get_character_from_json(ctx.author, release_lock=True)
        price_shown = self._sell(c, item)
        messages = [
            _("**{author}**, do you want to sell this item for {price} each? {item}").format(
                author=self.escape(ctx.author.display_name),
                item=box(str(item), lang="css"),
                price=humanize_number(price_shown),
            )
        ]
        try:
            item = c.backpack[item.name]
        except KeyError:
            return

        async def _backpack_sell_menu(
            ctx: commands.Context,
            pages: list,
            controls: dict,
            message: discord.Message,
            page: int,
            timeout: float,
            emoji: str,
        ):
            if message:
                with contextlib.suppress(discord.HTTPException):
                    await message.delete()
                await self._backpack_sell_button_action(ctx, emoji, page, item, price_shown, c)
                return None

        back_pack_sell_controls = {
            "\N{DIGIT ONE}\N{COMBINING ENCLOSING KEYCAP}": _backpack_sell_menu,
            "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}": _backpack_sell_menu,
            "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS WITH CIRCLED ONE OVERLAY}": _backpack_sell_menu,
            "\N{CROSS MARK}": _backpack_sell_menu,
        }

        await menu(ctx, messages, back_pack_sell_controls, timeout=60)

    @_backpack.command(name="trade", enabled=False)
    async def backpack_trade(
        self, ctx: Context, buyer: Member, asking: Optional[int] = 1000, *, item: ItemConverter,
    ):
        """Trade an item from your backpack to another user."""
        if ctx.author == buyer:
            return await smart_embed(
                ctx,
                _("You take the item and pass it from one hand to the other. Congratulations, you traded yourself."),
            )
        if self.in_adventure(ctx):
            raise AdventureCheckFailure(
                _("You tried to trade an item to a party member but the monster ahead commands your attention."),
            )
        if self.in_adventure(user=buyer):
            return await smart_embed(
                ctx,
                _("**{buyer}** is currently in an adventure... you were unable to reach them via pigeon.").format(
                    buyer=self.escape(buyer.display_name)
                ),
            )
        c = await self.get_character_from_json(ctx.author)
        if not any([x for x in c.backpack if item.name.lower() == x.lower()]):
            raise AdventureCheckFailure(
                _("**{author}**, you have to specify an item from your backpack to trade.").format(
                    author=self.escape(ctx.author.display_name)
                ),
            )
        lookup = list(x for n, x in c.backpack.items() if str(item) == str(x))
        if len(lookup) > 1:
            await smart_embed(
                ctx,
                _(
                    "**{author}**, I found multiple items ({items}) "
                    "matching that name in your backpack.\nPlease be more specific."
                ).format(author=self.escape(ctx.author.display_name), items=humanize_list([x.name for x in lookup])),
            )
            return
        if any([x for x in lookup if x.rarity == "forged"]):
            device = [x for x in lookup if x.rarity == "forged"]
            return await ctx.send(
                box(
                    _("\n{author}, your {device} does not want to leave you.").format(
                        author=self.escape(ctx.author.display_name), device=str(device[0])
                    ),
                    lang="css",
                )
            )
        elif any([x for x in lookup if x.rarity == "set"]):
            return await ctx.send(
                box(
                    _("\n{character}, you cannot trade Set items as they are bound to your soul.").format(
                        character=self.escape(ctx.author.display_name)
                    ),
                    lang="css",
                )
            )
        else:
            item = lookup[0]
            hand = item.slot[0] if len(item.slot) < 2 else "two handed"
            currency_name = await bank.get_currency_name(ctx.guild)
            if str(currency_name).startswith("<"):
                currency_name = "credits"
            trade_talk = box(
                _(
                    "{author} wants to sell {item}. "
                    "(RAGE: {att_item} | "
                    "RANT: {cha_item} | "
                    "ACC: {int_item} | "
                    "DEX: {dex_item} | "
                    "LUCK: {luck_item}) "
                    "[{hand}])\n{buyer}, "
                    "do you want to buy this item for {asking} {currency_name}?"
                ).format(
                    author=self.escape(ctx.author.display_name),
                    item=item,
                    att_item=str(item.att),
                    cha_item=str(item.cha),
                    int_item=str(item.int),
                    dex_item=str(item.dex),
                    luck_item=str(item.luck),
                    hand=hand,
                    buyer=self.escape(buyer.display_name),
                    asking=str(asking),
                    currency_name=currency_name,
                ),
                lang="css",
            )
            async with self.get_lock(ctx.author):
                trade_msg = await ctx.send(f"{buyer.mention}\n{trade_talk}")
                start_adding_reactions(trade_msg, ReactionPredicate.YES_OR_NO_EMOJIS)
                pred = ReactionPredicate.yes_or_no(trade_msg, buyer)
                try:
                    await ctx.bot.wait_for("reaction_add", check=pred, timeout=60)
                except asyncio.TimeoutError:
                    await self._clear_react(trade_msg)
                    return
                if pred.result:  # buyer reacted with Yes.
                    with contextlib.suppress(discord.errors.NotFound):
                        if await bank.can_spend(buyer, asking):
                            buy_user = await self.get_character_from_json(ctx.author)
                            if buy_user.rebirths + 1 < c.rebirths:
                                raise AdventureCheckFailure(
                                    _(
                                        "You can only trade with people that are the same "
                                        "rebirth level, one rebirth level less than you, "
                                        "or a higher rebirth level than yours."
                                    ),
                                )
                            try:
                                await bank.transfer_credits(buyer, ctx.author, asking)
                            except BalanceTooHigh as e:
                                await bank.withdraw_credits(buyer, asking)
                                await bank.set_balance(ctx.author, e.max_balance)
                            c.backpack[item.name].owned -= 1
                            newly_owned = c.backpack[item.name].owned
                            if c.backpack[item.name].owned <= 0:
                                del c.backpack[item.name]
                            async with self.get_lock(buyer):
                                if item.name in buy_user.backpack:
                                    buy_user.backpack[item.name].owned += 1
                                else:
                                    item.owned = 1
                                    buy_user.backpack[item.name] = item
                                await self.config.user(buyer).set(await buy_user.to_json(self.config))
                                item.owned = newly_owned
                                await self.config.user(ctx.author).set(await c.to_json(self.config))

                            await trade_msg.edit(
                                content=(
                                    box(
                                        _("\n{author} traded {item} to {buyer} for {asking} {currency_name}.").format(
                                            author=self.escape(ctx.author.display_name),
                                            item=item,
                                            buyer=self.escape(buyer.display_name),
                                            asking=asking,
                                            currency_name=currency_name,
                                        ),
                                        lang="css",
                                    )
                                )
                            )
                            await self._clear_react(trade_msg)
                        else:
                            await trade_msg.edit(
                                content=_("**{buyer}**, you do not have enough {currency_name}.").format(
                                    buyer=self.escape(buyer.display_name), currency_name=currency_name,
                                )
                            )
                else:
                    with contextlib.suppress(discord.HTTPException):
                        await trade_msg.delete()

    @commands.command()
    @commands.bot_has_permissions(add_reactions=True)
    @commands.guild_only()
    async def rebirth(self, ctx: Context):
        """Resets your character level and increases your rebirths by 1."""
        if self.in_adventure(ctx):
            raise AdventureCheckFailure(_("You tried to rebirth but the monster ahead is commanding your attention."))

        async with self.get_lock(ctx.author):
            c = await self.get_character_from_json(ctx.author)
            if c.lvl < c.maxlevel:
                raise AdventureCheckFailure( _("You need to be level `{c.maxlevel}` to rebirth.").format(c=c))
            if not c.last_currency_check + 10 < time.time():
                raise AdventureCheckFailure(_("You need to wait a little before rebirthing.").format(c=c))
            if not await bank.is_global():
                rebirth_cost = await self.config.guild(ctx.guild).rebirth_cost()
            else:
                rebirth_cost = await self.config.rebirth_cost()
            rebirthcost = 1000 * c.rebirths
            current_balance = c.bal
            last_known_currency = c.last_known_currency
            if last_known_currency and current_balance / last_known_currency < 0.25:
                currency_name = await bank.get_currency_name(ctx.guild)
                raise AdventureCheckFailure(
                    _(
                        "You tried to get rid of all your {currency_name} -- tsk tsk, "
                        "once you get back up to {cur} {currency_name} try again."
                    ).format(currency_name=currency_name, cur=humanize_number(last_known_currency)),
                )
            else:
                has_fund = await has_funds(ctx.author, rebirthcost)
            if not has_fund:
                currency_name = await bank.get_currency_name(ctx.guild)
                raise AdventureCheckFailure( _("You need more {currency_name} to be able to rebirth.").format(currency_name=currency_name),
                )
            space = "\N{EN SPACE}"
            open_msg = await smart_embed(
                ctx,
                _(
                    f"Rebirthing will:\n\n"
                    f"* cost {int(rebirth_cost)}% of your credits\n"
                    f"* cost all of your current gear\n"
                    f"{space*4}- Legendary items loose one degradation point per rebirth "
                    f"and are broken down when they have 0 left.\n"
                    f"{space*4}- Set items never disappear\n"
                    f"* set you back to level 1 while keeping your current class\n\n"
                    f"In turn, rebirthing will give you a higher stat base, a better chance "
                    f"for acquiring more powerful items, a higher max level, and the "
                    f"ability to convert chests to higher rarities after the second rebirth.\n\n"
                    f"Would you like to rebirth?"
                ),
            )
            start_adding_reactions(open_msg, ReactionPredicate.YES_OR_NO_EMOJIS)
            pred = ReactionPredicate.yes_or_no(open_msg, ctx.author)
            try:
                await ctx.bot.wait_for("reaction_add", check=pred, timeout=60)
            except asyncio.TimeoutError:
                await self._clear_react(open_msg)
                raise AdventureCheckFailure(_("I can't wait forever, you know."))
            else:
                if not pred.result:
                    await open_msg.edit(
                        content=box(
                            _("{c} decided not to rebirth.").format(c=self.escape(ctx.author.display_name)), lang="css",
                        )
                    )
                    return await self._clear_react(open_msg)

                c = await self.get_character_from_json(ctx.author)
                if c.lvl < c.maxlevel:
                    raise AdventureCheckFailure(_("You need to be level `{c.maxlevel}` to rebirth.").format(c=c))
                bal = await bank.get_balance(ctx.author)
                if bal >= 1000:
                    withdraw = int((bal - 1000) * (rebirth_cost / 100.0))
                    await bank.withdraw_credits(ctx.author, withdraw)
                else:
                    withdraw = int(bal * (rebirth_cost / 100.0))
                    await bank.set_balance(ctx.author, 0)

                await open_msg.edit(
                    content=(
                        box(
                            _("{c}, congratulations on your rebirth.\nYou paid {bal}.").format(
                                c=self.escape(ctx.author.display_name), bal=humanize_number(withdraw),
                            ),
                            lang="css",
                        )
                    )
                )
                await self.config.user(ctx.author).set(await c.rebirth())
                if c.rebirths == 5:
                    await self.add_rebirths_role(ctx.guild, ctx.author)
                    await self.remove_adv_role(ctx.guild, ctx.author)

    @commands.command()
    @commands.bot_has_permissions(add_reactions=True)
    @commands.is_owner()
    async def devrebirth(
        self, ctx: Context, rebirth_level: int = 1, character_level: int = 1, user: Member = None,
    ):
        """[Dev] Set a users rebirth level."""
        if not await no_dev_prompt(ctx):
            return
        target = user or ctx.author

        if not self.is_dev(ctx.author):
            if rebirth_level > 100:
                await ctx.send("Rebirth is too high.")
                await ctx.send_help()
                return
            elif character_level > 1000:
                await ctx.send("Level is too high.")
                await ctx.send_help()
                return

        async with self.get_lock(target):
            c = await self.get_character_from_json(user)

            bal = await bank.get_balance(target)
            if bal >= 1000:
                withdraw = bal - 1000
                await bank.withdraw_credits(target, withdraw)
            else:
                withdraw = bal
                await bank.set_balance(target, 0)

            await ctx.send(
                content=(
                    box(
                        _("{c}, congratulations on your rebirth.\nYou paid {bal}.").format(
                            c=self.escape(target.display_name), bal=humanize_number(withdraw)
                        ),
                        lang="css",
                    )
                )
            )
            character_data = await c.rebirth(dev_val=rebirth_level)
            await self.config.user(target).set(character_data)
        await self._add_rewards(ctx, target, int((character_level) ** 3.5) + 1, 0, False)
        await ctx.tick()

    @commands.command()
    @commands.bot_has_permissions(add_reactions=True)
    @commands.is_owner()
    async def devreset(self, ctx: commands.Context, user: Member = None):
        """[Dev] Reset the skill cooldown for this user."""
        if not await no_dev_prompt(ctx):
            return
        target = user or ctx.author
        async with self.get_lock(target):
            c = await self.get_character_from_json(target)
            c.heroclass["ability"] = False
            c.heroclass["cooldown"] = 0
            if "catch_cooldown" in c.heroclass:
                c.heroclass["catch_cooldown"] = 0
            await self.config.user(target).set(await c.to_json(self.config))
        await ctx.tick()

    @commands.command()
    @commands.is_owner()
    async def devclear(self, ctx: commands.Context):
        """[Dev] Clears raid history to reset monster generation."""
        if not await no_dev_prompt(ctx):
            return
        self._adv_results = AdventureResults(20)

    @commands.group(aliases=["loadouts"])
    async def loadout(self, ctx: Context):
        """Set up gear sets or loadouts."""

    @loadout.command(name="save")
    async def save_loadout(self, ctx: Context, name: str):
        """Save your current equipment as a loadout."""

        name = name.lower()
        async with self.get_lock(ctx.author):
            c = await self.get_character_from_json(ctx.author)
            if name in c.loadouts:
                await smart_embed(
                    ctx,
                    _("{author}, you already have a loadout named {name}.").format(
                        author=self.escape(ctx.author.display_name), name=name
                    ),
                )
                return
            else:
                c = await self.get_character_from_json(ctx.author)
                loadout = await Character.save_loadout(c)
                c.loadouts[name] = loadout
                await self.config.user(ctx.author).set(await c.to_json(self.config))
                await smart_embed(
                    ctx,
                    _("**{author}**, your current equipment has been saved to {name}.").format(
                        author=self.escape(ctx.author.display_name), name=name
                    ),
                )

    @loadout.command(name="delete", aliases=["del", "rem", "remove"])
    async def remove_loadout(self, ctx: Context, name: str):
        """Delete a saved loadout."""

        async with self.get_lock(ctx.author):
            name = name.lower()
            c = await self.get_character_from_json(ctx.author)
            if name not in c.loadouts:
                await smart_embed(
                    ctx,
                    _("**{author}**, you don't have a loadout named {name}.").format(
                        author=self.escape(ctx.author.display_name), name=name
                    ),
                )
                return
            else:
                del c.loadouts[name]
                await self.config.user(ctx.author).set(await c.to_json(self.config))
                await smart_embed(
                    ctx,
                    _("**{author}**, loadout {name} has been deleted.").format(
                        author=self.escape(ctx.author.display_name), name=name
                    ),
                )

    @loadout.command(name="show")
    @commands.bot_has_permissions(add_reactions=True)
    async def show_loadout(self, ctx: Context, name: str = None):
        """Show saved loadouts."""

        c = await self.get_character_from_json(ctx.author)
        if not c.loadouts:
            raise AdventureCheckFailure(
                _("**{author}**, you don't have any loadouts saved.").format(
                    author=self.escape(ctx.author.display_name)
                ),
            )
        if name is not None and name.lower() not in c.loadouts:
            raise AdventureCheckFailure(
                _("**{author}**, you don't have a loadout named {name}.").format(
                    author=self.escape(ctx.author.display_name), name=name
                ),
            )
        else:
            msg_list = []
            index = 0
            count = 0
            for (l_name, loadout) in c.loadouts.items():
                if name and name.lower() == l_name:
                    index = count
                stats = await self._build_loadout_display({"items": loadout})
                msg = _("[{name} Loadout for {author}]\n\n{stats}").format(
                    name=l_name, author=self.escape(ctx.author.display_name), stats=stats
                )
                msg_list.append(box(msg, lang="css"))
                count += 1
            await menu(ctx, msg_list, MENU_CONTROLS, page=index)

    @loadout.command(name="equip", aliases=["load"], cooldown_after_parsing=True)
    @commands.cooldown(rate=1, per=360, type=commands.BucketType.user)
    async def equip_loadout(self, ctx: Context, name: str):
        """Equip a saved loadout."""
        if self.in_adventure(ctx):
            raise AdventureCheckFailure(
                _("You tried to magically equip multiple items at once, but the monster ahead nearly killed you.")
            )

        name = name.lower()
        async with self.get_lock(ctx.author):
            c = await self.get_character_from_json(ctx.author)
            if name not in c.loadouts:
                raise AdventureCheckFailure(_("**{author}**, you don't have a loadout named {name}.").format(
                    author=self.escape(ctx.author.display_name), name=name)
                )
            else:
                c = await c.equip_loadout(name)
                current_stats = box(
                    _(
                        "{author}'s new stats: "
                        "Attack: {stat_att} [{skill_att}], "
                        "Charisma: {stat_cha} [{skill_cha}], "
                        "Intelligence: {stat_int} [{skill_int}], "
                        "Dexterity: {stat_dex}, "
                        "Luck: {stat_luck}."
                    ).format(
                        author=self.escape(ctx.author.display_name),
                        stat_att=c.get_stat_value("att")[0],
                        skill_att=c.skill["att"],
                        stat_int=c.get_stat_value("int")[0],
                        skill_int=c.skill["int"],
                        stat_cha=c.get_stat_value("cha")[0],
                        skill_cha=c.skill["cha"],
                        stat_dex=c.get_stat_value("dex")[0],
                        stat_luck=c.get_stat_value("luck")[0],
                    ),
                    lang="css",
                )
                await ctx.send(current_stats)
                await self.config.user(ctx.author).set(await c.to_json(self.config))

    @loadout.command(name="update")
    async def update_loadout(self, ctx: Context, name: str):
        """Updates specified loadout with current equipments."""
        async with self.get_lock(ctx.author):
            name = name.lower()
            c = await self.get_character_from_json(ctx.author)
            if name not in c.loadouts:
                raise AdventureCheckFailure(_("**{author}**, you don't have a loadout named {name}.").format(
                    author=self.escape(ctx.author.display_name), name=name)
                )
                return
            else:
                loadout = await Character.save_loadout(c)
                c.loadouts[name] = loadout
                await self.config.user(ctx.author).set(await c.to_json(self.config))
                await smart_embed(
                    ctx,
                    _("**{author}**, {name} has been updated with your current equipment!").format(
                        author=self.escape(ctx.author.display_name), name=name
                    ),
                    success=True
                )

    @commands.group()
    @commands.guild_only()
    async def adventureset(self, ctx: Context):
        """Setup various adventure settings."""

    @adventureset.command()
    @check_global_setting_admin()
    async def rebirthcost(self, ctx: Context, percentage: float):
        """[Admin] Set what percentage of the user balance to charge for rebirths.

        Unless the user's balance is under 1k, users that rebirth will be left with the base of 1k credits plus the remaining credit percentage after the rebirth charge.
        """
        if percentage < 0 or percentage > 100:
            raise AdventureCheckFailure(_("Percentage has to be between 0 and 100."))
        if not await bank.is_global():
            await self.config.guild(ctx.guild).rebirth_cost.set(percentage)
            await smart_embed(
                ctx, _("I will now charge {0:.0%} of the user's balance for a rebirth.").format(percentage / 100),
                success=True
            )
        else:
            await self.config.rebirth_cost.set(percentage)
            await smart_embed(
                ctx,
                _("I will now charge {0:.0%} of the user's global balance for a rebirth.").format(percentage / 100),
                success=True
            )

    @adventureset.command()
    @commands.admin_or_permissions(administrator=True)
    async def cartroom(self, ctx: Context, room: discord.TextChannel = None):
        """[Admin] Lock carts to a specific text channel."""
        if room is None:
            await self.config.guild(ctx.guild).cartroom.set(None)
            return await smart_embed(ctx, _("Done, carts will be able to appear in any text channel the bot can see."), success=True)

        await self.config.guild(ctx.guild).cartroom.set(room.id)
        await smart_embed(ctx, _("Done, carts will only appear in {room.mention}.").format(room=room), success=True)

    @adventureset.group(name="locks")
    @commands.bot_has_permissions(add_reactions=True)
    @commands.admin_or_permissions(administrator=True)
    async def adventureset_locks(self, ctx: Context):
        """[Admin] Reset Adventure locks."""

    @adventureset_locks.command(name="user")
    @commands.is_owner()
    async def adventureset_locks_user(self, ctx: Context, user: discord.User):
        """[Owner] Reset a guild member's user lock."""
        lock = self.get_lock(user)
        with contextlib.suppress(Exception):
            lock.release()
        await ctx.tick()

    @adventureset.command(name="dailybonus")
    @commands.is_owner()
    async def adventureset_daily_bonus(self, ctx: Context, day: DayConverter, percentage: PercentageConverter):
        """[Owner] Set the daily xp and currency bonus.

        **percentage** must be between 0% and 100%.
        """
        day_val, day_text = day
        async with self.config.daily_bonus.all() as daily_bonus_data:
            daily_bonus_data[day_val] = percentage
            self._daily_bonus = daily_bonus_data.copy()
        await smart_embed(
            ctx, _("Daily bonus for `{0}` has been set to: {1:.0%}").format(day_text.title(), percentage),
            success=True
        )

    @commands.guild_only()
    @adventureset_locks.command(name="adventure")
    async def adventureset_locks_adventure(self, ctx: Context):
        """[Admin] Reset the adventure game lock for the server."""
        while ctx.channel.id in self._sessions:
            del self._sessions[ctx.channel.id]
        await ctx.tick()

    @adventureset.command()
    @commands.is_owner()
    async def restrict(self, ctx: Context):
        """[Owner] Set whether or not adventurers are restricted to one adventure at a time."""
        toggle = await self.config.restrict()
        await self.config.restrict.set(not toggle)
        await smart_embed(ctx, _("Adventurers restricted to one adventure at a time: {}").format(not toggle), success=True)

    @adventureset.command()
    @commands.is_owner()
    async def sepcurrency(self, ctx: Context):
        """[Owner] Toggle whether the currency should be separated from main bot currency."""
        toggle = await self.config.separate_economy()
        await self.config.separate_economy.set(not toggle)
        self._separate_economy = not toggle
        await smart_embed(
            ctx, _("Adventurer currency is: **{}**").format(_("Separated" if not toggle else _("Unified"))),
            success=True
        )

    @adventureset.group(name="economy")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    @has_separated_economy()
    async def commands_adventureset_economy(self, ctx: Context):
        """[Admin] Manages the adventure economy."""

    @commands_adventureset_economy.command(name="tax", usage=" gold,tax gold,tax ...")
    @commands.is_owner()
    async def commands_adventureset_economy_tax(self, ctx: Context, *, taxes: TaxesConverter):
        """[Owner] Set the tax thresholds.

        **gold** must be positive
        **percentage** must be between 0 and 1.

        """
        new_taxes = {}
        for k, v in taxes.items():
            if int(k) >= 0 and 0 <= float(v) <= 1:
                new_taxes[k] = float(v)
        new_taxes = {k: v for k, v in sorted(new_taxes.items(), key=lambda item: item[1])}
        await self.config.tax_brackets.set(new_taxes)
        headers = ["Tax %", "Tax Threshold"]
        await smart_embed(
            ctx, box(tabulate([(f"{v:.2%}", humanize_number(int(k))) for k, v in new_taxes.items()], headers=headers)),
            success=True
        )

    @commands.is_owner()
    @commands_adventureset_economy.command(name="rate")
    async def commands_adventureset_economy_conversion_rate(self, ctx: Context, rate_in: int, rate_out: int):
        """[Owner] Set how much 1 bank credit is worth in adventure.

        **rate_in**: Is how much gold you will get for 1 bank credit. Default is 10
        **rate_out**: Is how much gold is needed to convert to 1 bank credit. Default is 11
        """
        if rate_in < 0 or rate_out < 0:
            raise AdventureCheckFailure(_("You are evil ... please DM me your phone number we need to hangout."))
        await self.config.to_conversion_rate.set(rate_in)
        await self.config.from_conversion_rate.set(rate_out)
        await smart_embed(
            ctx,
            _("1 {name} will be worth {rate_in} {a_name}.\n{rate_out} {a_name} will convert into 1 {name}").format(
                name=await bank.get_currency_name(ctx.guild, _forced=True),
                rate_in=humanize_number(rate_in),
                rate_out=humanize_number(rate_out),
                a_name=await bank.get_currency_name(ctx.guild),
            ),
            success=True
        )

    @commands_adventureset_economy.command(name="maxwithdraw")
    async def commands_adventureset_economy_maxwithdraw(self, ctx: Context, *, amount: int):
        """[Admin] Set how much players are allowed to withdraw."""
        if amount < 0:
            raise AdventureCheckFailure(_("You are evil ... please DM me your phone number we need to hangout."))
        if await bank.is_global(_forced=True):
            await self.config.max_allowed_withdraw.set(amount)
        else:
            await self.config.guild(ctx.guild).max_allowed_withdraw.set(amount)
        await smart_embed(
            ctx,
            _(
                "Adventurers will be able to withdraw up to {amount} {name} from their adventure bank and deposit into their bot economy."
            ).format(name=await bank.get_currency_name(ctx.guild, _forced=True), amount=humanize_number(amount)),
            success=True
        )

    @commands_adventureset_economy.command(name="withdraw")
    async def commands_adventureset_economy_withdraw(self, ctx: Context):
        """[Admin] Toggle whether users are allowed to withdraw from adventure currency to main currency."""

        if await bank.is_global(_forced=True):
            state = await self.config.disallow_withdraw()
            await self.config.disallow_withdraw.set(not state)
        else:
            state = await self.config.guild(ctx.guild).disallow_withdraw()
            await self.config.guild(ctx.guild).disallow_withdraw.set(not state)

        await smart_embed(
            ctx,
            _("Adventurers are now {state} to withdraw money from adventure currency.").format(
                state=_("allowed") if not state else _("disallowed")
            ),
            success=True
        )

    @adventureset.command(name="advcooldown", hidden=True)
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def advcooldown(self, ctx: Context, *, time_in_seconds: int):
        """[Admin] Changes the cooldown/gather time after an adventure.

        Default is 120 seconds.
        """
        if time_in_seconds < 30:
            raise AdventureCheckFailure(_("Cooldown cannot be set to less than 30 seconds."))

        await self.config.guild(ctx.guild).cooldown_timer_manual.set(time_in_seconds)
        await smart_embed(
            ctx, _("Adventure cooldown set to {cooldown} seconds.").format(cooldown=time_in_seconds),
            success=True
        )

    @adventureset.command()
    @commands.admin_or_permissions(administrator=True)
    async def god(self, ctx: Context, *, name):
        """[Admin] Set the server's name of the god."""
        await self.config.guild(ctx.guild).god_name.set(name)
        await ctx.tick()

    @adventureset.command()
    @commands.is_owner()
    async def globalgod(self, ctx: Context, *, name):
        """[Owner] Set the default name of the god."""
        await self.config.god_name.set(name)
        await ctx.tick()

    @adventureset.command(aliases=["embed"])
    @commands.admin_or_permissions(administrator=True)
    async def embeds(self, ctx: Context):
        """[Admin] Set whether or not to use embeds for the adventure game."""
        toggle = await self.config.guild(ctx.guild).embed()
        await self.config.guild(ctx.guild).embed.set(not toggle)
        await smart_embed(ctx, _("Embeds: {}").format(not toggle), success=True)

    @adventureset.command(aliases=["chests"])
    @commands.is_owner()
    async def cartchests(self, ctx: Context):
        """[Admin] Set whether or not to sell chests in the cart."""
        toggle = await self.config.enable_chests()
        await self.config.enable_chests.set(not toggle)
        await smart_embed(ctx, _("Carts can sell chests: {}").format(not toggle), success=True)

    @adventureset.command()
    @commands.admin_or_permissions(administrator=True)
    async def cartname(self, ctx: Context, *, name):
        """[Admin] Set the server's name of the cart."""
        await self.config.guild(ctx.guild).cart_name.set(name)
        await ctx.tick()

    @adventureset.command()
    @commands.admin_or_permissions(administrator=True)
    async def carttime(self, ctx: Context, *, time: str):
        """
        [Admin] Set the cooldown of the cart.
        Time can be in seconds, minutes, hours, or days.
        Examples: `1h 30m`, `2 days`, `300`
        The bot assumes seconds if no units are given.
        """
        time_delta = parse_timedelta(time)
        if time_delta is None:
            raise AdventureCheckFailure(_("You must supply an amount and time unit like `120 seconds`."))
        if time_delta.total_seconds() < 600:
            cartname = await self.config.guild(ctx.guild).cart_name()
            if not cartname:
                cartname = await self.config.cart_name()
            raise AdventureCheckFailure(_("{} doesn't have the energy to return that often.").format(cartname))
        await self.config.guild(ctx.guild).cart_timeout.set(time_delta.seconds)
        await ctx.tick()

    @adventureset.command(name="clear")
    @commands.is_owner()
    async def clear_user(self, ctx: Context, *, user: discord.User):
        """[Owner] Lets you clear a users entire character sheet."""
        await self.config.user(user).clear()
        await smart_embed(ctx, _("{user}'s character sheet has been erased.").format(user=user), success=True)

    @adventureset.command(name="remove")
    @commands.is_owner()
    async def remove_item(self, ctx: Context, user: Member, *, full_item_name: str):
        """[Owner] Lets you remove an item from a user.

        Use the full name of the item including the rarity characters like . or []  or {}.
        """
        async with self.get_lock(user):
            item = None
            c = await self.get_character_from_json(user)
            for slot in ORDER:
                if slot == "two handed":
                    continue
                equipped_item = getattr(c, slot)
                if equipped_item and equipped_item.name.lower() == full_item_name.lower():
                    item = equipped_item
            if item:
                with contextlib.suppress(Exception):
                    await c.unequip_item(item)
            else:
                try:
                    item = c.backpack[full_item_name]
                except KeyError:
                    raise AdventureCheckFailure(_("{} does not have an item named `{}`.").format(user, full_item_name))
            with contextlib.suppress(KeyError):
                del c.backpack[item.name]
            await self.config.user(user).set(await c.to_json(self.config))
        await ctx.send(_("{item} removed from {user}.").format(item=box(str(item), lang="css"), user=user))

    @adventureset.command()
    @commands.is_owner()
    async def globalcartname(self, ctx: Context, *, name):
        """[Owner] Set the default name of the cart."""
        await self.config.cart_name.set(name)
        await ctx.tick()

    @adventureset.command()
    @commands.is_owner()
    async def theme(self, ctx: Context, *, theme):
        """[Owner] Change the theme for adventure."""
        if theme == "default":
            await self.config.theme.set("default")
            await smart_embed(ctx, _("Going back to the default theme."), success=True)
            await self.initialize()
            return
        if theme not in os.listdir(bundled_data_path(self)):
            raise AdventureCheckFailure(_("That theme pack does not exist!"))
        good_files = [
            "as_monsters",
            "attribs",
            "locations",
            "monsters",
            "pets",
            "raisins",
            "threatee",
            "tr_set",
            "prefixes",
            "materials",
            "equipment",
            "suffixes",
            "set_bonuses",
        ]
        missing_files = set(good_files).difference('.'.join(i.split('.')[:-1]) for i in os.listdir(bundled_data_path(self) / theme))

        if missing_files:
            await smart_embed(
                ctx, _("That theme pack is missing the following files: {}.").format(humanize_list(list(missing_files))),
                success=False
            )
            return
        else:
            await self.config.theme.set(theme)
            await ctx.tick()
        await self.initialize()

    @commands.group()
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def themeset(self, ctx: Context):
        """[Admin] Modify themes."""

    @commands.is_owner()
    @themeset.group(name="add")
    async def themeset_add(self, ctx: Context):
        """[Owner] Add/Update objects in the specified theme."""

    @themeset_add.command(name="monster")
    async def themeset_add_monster(self, ctx: Context, *, theme_data: ThemeSetMonterConverter):
        """[Owner] Add/Update a monster object in the specified theme.

        Usage: `[p]themeset add monster theme++name++hp++dipl++pdef++mdef++boss++image`
        """
        assert isinstance(theme_data, dict)
        theme = theme_data.pop("theme", None)
        if theme != "default" and theme not in os.listdir(cog_data_path(self)):
            raise AdventureCheckFailure(_("That theme pack does not exist!"))
        updated = False
        monster = theme_data.pop("name", None)
        async with self.config.themes.all() as config_data:
            if theme not in config_data:
                config_data[theme] = {"monsters": {}}
            if "monsters" not in config_data[theme]:
                config_data[theme]["monsters"] = {}
            if monster in config_data[theme]["monsters"]:
                updated = True
            config_data[theme]["monsters"][monster] = theme_data
        image = theme_data.pop("image", None)
        text = _(
            "Monster: `{monster}` has been {status} the `{theme}` theme\n"
            "```ini\n"
            "HP:               [{hp}]\n"
            "Diplomacy:        [{dipl}]\n"
            "Physical defence: [{pdef}]\n"
            "Magical defence:  [{mdef}]\n"
            "Is a boss:        [{boss}]```"
        ).format(monster=monster, theme=theme, status=_("added to") if not updated else _("updated in"), **theme_data)

        embed = discord.Embed(description=text, colour=await ctx.embed_colour())
        embed.set_image(url=image)
        await ctx.send(embed=embed)

    @themeset_add.command(name="pet")
    async def themeset_add_pet(self, ctx: Context, *, pet_data: ThemeSetPetConverter):
        """[Owner] Add/Update a pet object in the specified theme.

        Usage: `[p]themeset add pet theme++name++bonus_multiplier++required_cha++crit_chance++always_crit`
        """
        assert isinstance(pet_data, dict)
        theme = pet_data.pop("theme", None)
        if theme != "default" and theme not in os.listdir(cog_data_path(self)):
            raise AdventureCheckFailure(_("That theme pack does not exist!"))
        updated = False
        pet = pet_data.pop("name", None)
        async with self.config.themes.all() as config_data:
            if theme not in config_data:
                config_data[theme] = {"pet": {}}
            if "pet" not in config_data[theme]:
                config_data[theme]["pet"] = {}
            if pet in config_data[theme]["pet"]:
                updated = True
            config_data[theme]["pet"][pet] = pet_data

        pet_bonuses = pet_data.pop("bonuses", {})
        text = _(
            "Pet: `{pet}` has been {status} the `{theme}` theme\n"
            "```ini\n"
            "Bonus Multiplier:  [{bonus}]\n"
            "Required Charisma: [{cha}]\n"
            "Pet always crits:  [{always}]\n"
            "Critical Chance:   [{crit}/100]```"
        ).format(
            pet=pet, theme=theme, status=_("added to") if not updated else _("updated in"), **pet_data, **pet_bonuses,
        )

        embed = discord.Embed(description=text, colour=await ctx.embed_colour())
        await ctx.send(embed=embed)

    @commands.is_owner()
    @themeset.group(name="delete", aliases=["del", "rem", "remove"])
    async def themeset_delete(self, ctx: Context):
        """[Owner] Remove objects in the specified theme."""

    @themeset_delete.command(name="monster")
    async def themeset_delete_monster(self, ctx: Context, theme: str, *, monster: str):
        """[Owner] Remove a monster object in the specified theme."""
        if theme != "default" and theme not in os.listdir(cog_data_path(self)):
            raise AdventureCheckFailure(_("That theme pack does not exist!"))
        async with self.config.themes.all() as config_data:
            if theme not in config_data:
                config_data[theme] = {"monsters": {}}
            if "monsters" not in config_data[theme]:
                config_data[theme]["monsters"] = {}
            if monster in config_data[theme]["monsters"]:
                del config_data[theme]["monsters"][monster]
            else:
                text = _("Monster: `{monster}` does not exist in `{theme}` theme").format(monster=monster, theme=theme)
                raise AdventureCheckFailure(text)

        text = _("Monster: `{monster}` has been deleted from the `{theme}` theme").format(monster=monster, theme=theme)
        await smart_embed(ctx, text, success=True)

    @themeset_delete.command(name="pet")
    async def themeset_delete_pet(self, ctx: Context, theme: str, *, pet: str):
        """[Owner] Remove a pet object in the specified theme."""
        if theme != "default" and theme not in os.listdir(cog_data_path(self)):
            raise AdventureCheckFailure(_("That theme pack does not exist!"))
        async with self.config.themes.all() as config_data:
            if theme not in config_data:
                config_data[theme] = {"pet": {}}
            if "pet" not in config_data[theme]:
                config_data[theme]["pet"] = {}
            if pet in config_data[theme]["pet"]:
                del config_data[theme]["pet"][pet]
            else:
                text = _("Pet: `{pet}` does not exist in `{theme}` theme").format(pet=pet, theme=theme)
                raise AdventureCheckFailure(text)

        text = _("Pet: `{pet}` has been deleted from the `{theme}` theme").format(pet=pet, theme=theme)
        await smart_embed(ctx, text, success=True)

    @themeset.group(name="list", aliases=["show"])
    async def themeset_list(self, ctx: Context):
        """[Admin] Show custom objects in the specified theme."""

    @themeset_list.command(name="monster")
    async def themeset_list_monster(self, ctx: Context, *, theme: str):
        """[Admin] Show monster objects in the specified theme."""
        if theme != "default" and theme not in os.listdir(cog_data_path(self)):
            raise AdventureCheckFailure(_("That theme pack does not exist!"))
        async with self.config.themes.all() as config_data:
            if theme not in config_data:
                raise AdventureCheckFailure(_("No custom monsters exist in this theme"))
            monster_data = config_data.get(theme, {}).get("monsters", {})
        embed_list = []
        for monster, monster_stats in monster_data.items():
            image = monster_stats.get("image")
            text = _(
                "```ini\n"
                "HP:               [{hp}]\n"
                "Diplomacy:        [{dipl}]\n"
                "Physical defence: [{pdef}]\n"
                "Magical defence:  [{mdef}]\n"
                "Is a boss:        [{boss}]```"
            ).format(**monster_stats)
            embed = discord.Embed(title=monster, description=text)
            embed.set_image(url=image)
            embed_list.append(embed)
        if embed_list:
            await menu(ctx, embed_list, MENU_CONTROLS)

    @themeset_list.command(name="pet")
    async def themeset_list_pet(self, ctx: Context, *, theme: str):
        """[Admin] Show pet objects in the specified theme."""
        if theme != "default" and theme not in os.listdir(cog_data_path(self)):
            raise AdventureCheckFailure(_("That theme pack does not exist!"))
        async with self.config.themes.all() as config_data:
            if theme not in config_data:
                raise AdventureCheckFailure(_("No custom monsters exist in this theme"))
            monster_data = config_data.get(theme, {}).get("pet", {})
        embed_list = []
        for pet, pet_stats in monster_data.items():
            pet_bonuses = pet_stats.pop("bonuses", {})
            text = _(
                "```ini\n"
                "Bonus Multiplier:  [{bonus}]\n"
                "Required Charisma: [{cha}]\n"
                "Pet always crits:  [{always}]\n"
                "Critical Chance:   [{crit}/100]```"
            ).format(theme=theme, **pet_stats, **pet_bonuses)
            embed = discord.Embed(title=pet, description=text)
            embed_list.append(embed)
        if embed_list:
            await menu(ctx, embed_list, MENU_CONTROLS)

    @adventureset.command()
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def cart(self, ctx: Context, *, channel: discord.TextChannel = None):
        """[Admin] Add or remove a text channel that the Trader cart can appear in.

        If the channel is already in the list, it will be removed.
        Use `[p]adventureset cart` with no arguments to show the channel list.
        """

        channel_list = await self.config.guild(ctx.guild).cart_channels()
        if not channel_list:
            channel_list = []
        if channel is None:
            msg = _("Active Cart Channels:\n")
            if not channel_list:
                msg += _("None.")
            else:
                name_list = []
                for chan_id in channel_list:
                    name_list.append(self.bot.get_channel(chan_id))
                msg += "\n".join(chan.name for chan in name_list)
            return await ctx.send(box(msg))
        elif channel.id in channel_list:
            new_channels = channel_list.remove(channel.id)
            await smart_embed(
                ctx, _("The {} channel has been removed from the cart delivery list.").format(channel),
                success=True
            )
            return await self.config.guild(ctx.guild).cart_channels.set(new_channels)
        else:
            channel_list.append(channel.id)
            await smart_embed(ctx, _("The {} channel has been added to the cart delivery list.").format(channel), success=True)
            await self.config.guild(ctx.guild).cart_channels.set(channel_list)

    @commands.guild_only()
    @commands.command()
    @commands.cooldown(rate=1, per=4, type=commands.BucketType.guild)
    async def adventuresettings(self, ctx: Context):
        """Display current settings."""
        global_data = await self.config.all()
        guild_data = await self.config.guild(ctx.guild).all()

        theme = global_data["theme"]
        god_name = global_data["god_name"] if not guild_data["god_name"] else guild_data["god_name"]
        cart_trader_name = global_data["cart_name"] if not guild_data["cart_name"] else guild_data["cart_name"]

        cart_channel_ids = guild_data["cart_channels"]
        if cart_channel_ids:
            cart_channels = humanize_list([f"{self.bot.get_channel(x).name}" for x in cart_channel_ids])
        else:
            cart_channels = _("None")

        cart_channel_lock_override_id = guild_data["cartroom"]
        if cart_channel_lock_override_id:
            cclo_channel_obj = self.bot.get_channel(cart_channel_lock_override_id)
            cart_channel_lock_override = f"{cclo_channel_obj.name}"
        else:
            cart_channel_lock_override = _("No channel lock present.")

        cart_timeout = parse_timedelta(f"{guild_data['cart_timeout']} seconds")
        lootbox_in_carts = _("Allowed") if global_data["enable_chests"] else _("Not allowed")

        if not await bank.is_global():
            rebirth_cost = guild_data["rebirth_cost"]
        else:
            rebirth_cost = global_data["rebirth_cost"]
        rebirth_cost = _("{0:.0%} of bank balance").format(rebirth_cost / 100)

        single_adventure_restrict = _("Restricted") if global_data["restrict"] else _("Unlimited")
        adventure_in_embed = _("Allow embeds") if guild_data["embed"] else _("No embeds")
        time_after_adventure = parse_timedelta(f"{guild_data['cooldown_timer_manual']} seconds")

        msg = _("Adventure Settings\n\n")
        msg += _("# Main Settings\n")
        msg += _("[Theme]:                                {theme}\n").format(theme=theme)
        msg += _("[God name]:                             {god_name}\n").format(god_name=god_name)
        msg += _("[Base rebirth cost]:                    {rebirth_cost}\n").format(rebirth_cost=rebirth_cost)
        msg += _("[Adventure message style]:              {adventure_in_embed}\n").format(
            adventure_in_embed=adventure_in_embed
        )
        msg += _("[Multi-adventure restriction]:          {single_adventure_restrict}\n").format(
            single_adventure_restrict=single_adventure_restrict
        )
        msg += _("[Post-adventure cooldown (hh:mm:ss)]:   {time_after_adventure}\n\n").format(
            time_after_adventure=time_after_adventure
        )
        msg += _("# Cart Settings\n")
        msg += _("[Cart trader name]:                     {cart_trader_name}\n").format(
            cart_trader_name=cart_trader_name
        )
        msg += _("[Cart delivery channels]:               {cart_channels}\n").format(cart_channels=cart_channels)
        msg += _("[Cart channel lock override]:           {cart_channel_lock_override}\n").format(
            cart_channel_lock_override=cart_channel_lock_override
        )
        msg += _("[Cart timeout (hh:mm:ss)]:              {cart_timeout}\n").format(cart_timeout=cart_timeout)
        msg += _("[Lootboxes in carts]:                   {lootbox_in_carts}\n").format(
            lootbox_in_carts=lootbox_in_carts
        )

        await ctx.send(box(msg, lang="ini"))

    @commands.command()
    @commands.cooldown(rate=1, per=4, type=commands.BucketType.guild)
    async def convert(self, ctx: Context, box_rarity: str, amount: int = 1):
        """Convert normal, rare or epic chests.

        Trade 25 normal chests for 1 rare chest.
        Trade 25 rare chests for 1 epic chest.
        Trade 25 epic chests for 1 legendary chest.
        """

        # Thanks to flare#0001 for the idea and writing the first instance of this
        if self.in_adventure(ctx):
            raise AdventureCheckFailure(
                _(
                    "You tried to magically combine some of your loot chests "
                    "but the monster ahead is commanding your attention."
                ),
            )
        normalcost = 25
        rarecost = 25
        epiccost = 25
        rebirth_normal = 2
        rebirth_rare = 8
        rebirth_epic = 10
        if amount < 1:
            raise AdventureCheckFailure(_("Nice try :smirk:"))
        if amount > 1:
            plural = "s"
        else:
            plural = ""
        async with self.get_lock(ctx.author):
            c = await self.get_character_from_json(ctx.author)

            if box_rarity.lower() == "rare" and c.rebirths < rebirth_rare:
                raise AdventureCheckFailure(_("**{}**, you need to have {} or more rebirths to convert rare treasure chests.").format(
                    self.escape(ctx.author.display_name), rebirth_rare
                ))
            elif box_rarity.lower() == "epic" and c.rebirths < rebirth_epic:
                raise AdventureCheckFailure(_("**{}**, you need to have {} or more rebirths to convert epic treasure chests.").format(
                    self.escape(ctx.author.display_name), rebirth_epic
                ))
            elif c.rebirths < 2:
                raise AdventureCheckFailure(_("**{c}**, you need to 3 rebirths to use this.").format(
                    c=self.escape(ctx.author.display_name),
                ))

            if box_rarity.lower() == "normal" and c.rebirths >= rebirth_normal:
                if c.treasure[0] >= (normalcost * amount):
                    c.treasure[0] -= normalcost * amount
                    c.treasure[1] += 1 * amount
                    await ctx.send(
                        box(
                            _(
                                "Successfully converted {converted} normal treasure "
                                "chests to {to} rare treasure chest{plur}.\n{author} "
                                "now owns {normal} normal, {rare} rare, {epic} epic, "
                                "{leg} legendary treasure chests, {asc} ascended and {set} set treasure chests."
                            ).format(
                                converted=humanize_number(normalcost * amount),
                                to=humanize_number(1 * amount),
                                plur=plural,
                                author=self.escape(ctx.author.display_name),
                                normal=c.treasure[0],
                                rare=c.treasure[1],
                                epic=c.treasure[2],
                                leg=c.treasure[3],
                                asc=c.treasure[4],
                                set=c.treasure[5],
                            ),
                            lang="css",
                        )
                    )
                    await self.config.user(ctx.author).set(await c.to_json(self.config))
                else:
                    raise AdventureCheckFailure(_("**{author}**, you do not have {amount} normal treasure chests to convert.").format(
                        author=self.escape(ctx.author.display_name), amount=humanize_number(normalcost * amount),
                    ))
            elif box_rarity.lower() == "rare" and c.rebirths >= rebirth_rare:
                if c.treasure[1] >= (rarecost * amount):
                    c.treasure[1] -= rarecost * amount
                    c.treasure[2] += 1 * amount
                    await ctx.send(
                        box(
                            _(
                                "Successfully converted {converted} rare treasure "
                                "chests to {to} epic treasure chest{plur}. \n{author} "
                                "now owns {normal} normal, {rare} rare, {epic} epic, "
                                "{leg} legendary treasure chests, {asc} ascended and {set} set treasure chests."
                            ).format(
                                converted=humanize_number(rarecost * amount),
                                to=humanize_number(1 * amount),
                                plur=plural,
                                author=self.escape(ctx.author.display_name),
                                normal=c.treasure[0],
                                rare=c.treasure[1],
                                epic=c.treasure[2],
                                leg=c.treasure[3],
                                asc=c.treasure[4],
                                set=c.treasure[5],
                            ),
                            lang="css",
                        )
                    )
                    await self.config.user(ctx.author).set(await c.to_json(self.config))
                else:
                    raise AdventureCheckFailure(_("{author}, you do not have {amount} rare treasure chests to convert.").format(
                        author=ctx.author.mention, amount=humanize_number(rarecost * amount)
                    ))
            elif box_rarity.lower() == "epic" and c.rebirths >= rebirth_epic:
                if c.treasure[2] >= (epiccost * amount):
                    c.treasure[2] -= epiccost * amount
                    c.treasure[3] += 1 * amount
                    await ctx.send(
                        box(
                            _(
                                "Successfully converted {converted} epic treasure "
                                "chests to {to} legendary treasure chest{plur}. \n{author} "
                                "now owns {normal} normal, {rare} rare, {epic} epic, "
                                "{leg} legendary treasure chests, {asc} ascended and {set} set treasure chests."
                            ).format(
                                converted=humanize_number(epiccost * amount),
                                to=humanize_number(1 * amount),
                                plur=plural,
                                author=self.escape(ctx.author.display_name),
                                normal=c.treasure[0],
                                rare=c.treasure[1],
                                epic=c.treasure[2],
                                leg=c.treasure[3],
                                asc=c.treasure[4],
                                set=c.treasure[5],
                            ),
                            lang="css",
                        )
                    )
                    await self.config.user(ctx.author).set(await c.to_json(self.config))
                else:
                    raise AdventureCheckFailure(_("**{author}**, you do not have {amount} epic treasure chests to convert.").format(
                        author=self.escape(ctx.author.display_name), amount=humanize_number(epiccost * amount),
                    ))
            else:
                raise AdventureCheckFailure(_("**{}**, please select between normal, rare, or epic treasure chests to convert.").format(
                    self.escape(ctx.author.display_name))
                )

    @commands.command()
    @is_dm()
    async def equip(self, ctx: Context, *, item: EquipableItemConverter):
        """This equips an item from your backpack."""
        if self.in_adventure(ctx):
            raise AdventureCheckFailure(
                _("You tried to equip your item but the monster ahead nearly decapitated you.")
            )

        await ctx.invoke(self.backpack_equip, equip_item=item)

    @commands.command()
    @commands.bot_has_permissions(add_reactions=True)
    @is_dm()
    async def forge(self, ctx):
        """[Tinkerer Class Only]

        This allows a Tinkerer to forge two items into a device. (1h cooldown)
        """
        if self.in_adventure(ctx):
            raise AdventureCheckFailure(_("You tried to forge an item but there were no forges nearby."))

        async with self.get_lock(ctx.author):
            c = await self.get_character_from_json(ctx.author)
            if c.heroclass["name"] != "Tinkerer":
                raise AdventureCheckFailure(
                    _("**{}**, you need to be a Tinkerer to do this.").format(self.escape(ctx.author.display_name)),
                )
            else:
                cooldown_time = max(1800, (7200 - ((c.luck + c.total_int) * 2)))
                if "cooldown" not in c.heroclass:
                    c.heroclass["cooldown"] = cooldown_time + 1
                if c.heroclass["cooldown"] > time.time():
                    cooldown_time = c.heroclass["cooldown"] - time.time()
                    raise AdventureCheckFailure(_("This command is on cooldown. Try again in {}").format(
                        humanize_timedelta(seconds=int(cooldown_time)) if cooldown_time >= 1 else _("1 second"))
                    )
                ascended_forge_msg = ""
                ignored_rarities = ["forged", "set", "event"]
                if c.rebirths < 30:
                    ignored_rarities.append("ascended")
                    ascended_forge_msg += _("\n\nAscended items will be forgeable after 30 rebirths.")
                consumed = []
                forgeables_items = [str(i) for n, i in c.backpack.items() if i.rarity not in ignored_rarities]
                if len(forgeables_items) <= 1:
                    raise AdventureCheckFailure(_("**{}**, you need at least two forgeable items in your backpack to forge.").format(
                        self.escape(ctx.author.display_name))
                    )
                forgeables = _("{author}'s forgeables\n\n{bc}\n").format(
                    author=self.escape(ctx.author.display_name), bc=await c.get_backpack(forging=True, clean=True)
                )
                pages = pagify(forgeables, delims=["\n"], shorten_by=20, page_length=1900)
                pages = [box(page, lang="css") for page in pages]
                task = asyncio.create_task(menu(ctx, pages, MENU_CONTROLS, timeout=180))
                await smart_embed(
                    ctx,
                    _(
                        "Reply with the full or partial name of item 1 to select for forging. "
                        "Try to be specific. (Say `cancel` to exit){}".format(ascended_forge_msg)
                    ),
                )
                try:
                    item = None
                    while not item:
                        reply = await ctx.bot.wait_for(
                            "message", check=MessagePredicate.same_context(user=ctx.author), timeout=30,
                        )
                        new_ctx = await self.bot.get_context(reply)
                        if reply.content.lower() in ["cancel", "exit"]:
                            task.cancel()
                            raise AdventureCheckFailure(_("Forging process has been cancelled."))
                        with contextlib.suppress(BadArgument):
                            item = None
                            item = await ItemConverter().convert(new_ctx, reply.content)
                            if str(item) not in forgeables_items:
                                item = None
                        if not item:
                            wrong_item = _("**{c}**, I could not find that item - check your spelling.").format(
                                c=self.escape(ctx.author.display_name)
                            )
                            await smart_embed(ctx, wrong_item, success=False)
                        else:
                            break
                    consumed.append(item)
                except asyncio.TimeoutError:
                    timeout_msg = _("I don't have all day you know, **{}**.").format(
                        self.escape(ctx.author.display_name)
                    )
                    task.cancel()
                    raise AdventureCheckFailure(timeout_msg)
                if item.rarity in ["forged", "set"]:
                    raise AdventureCheckFailure(_("**{c}**, {item.rarity} items cannot be reforged.").format(
                        c=self.escape(ctx.author.display_name), item=item)
                    )
                await smart_embed(
                    ctx,
                    _(
                        "Reply with the full or partial name of item 2 to select for forging. "
                        "Try to be specific. (Say `cancel` to exit)"
                    ),
                )
                try:
                    item = None
                    while not item:
                        reply = await ctx.bot.wait_for(
                            "message", check=MessagePredicate.same_context(user=ctx.author), timeout=30,
                        )
                        if reply.content.lower() in ["cancel", "exit"]:
                            raise AdventureCheckFailure(_("Forging process has been cancelled."))
                        new_ctx = await self.bot.get_context(reply)
                        with contextlib.suppress(BadArgument):
                            item = None
                            item = await ItemConverter().convert(new_ctx, reply.content)
                            if str(item) not in forgeables_items:
                                item = None
                        if item and consumed[0].owned <= 1 and str(consumed[0]) == str(item):
                            wrong_item = _(
                                "**{c}**, you only own 1 copy of this item and you've already selected it."
                            ).format(c=self.escape(ctx.author.display_name))
                            raise AdventureCheckFailure(wrong_item)
                            item = None
                            continue
                        if not item:
                            wrong_item = _("**{c}**, I could not find that item - check your spelling.").format(
                                c=self.escape(ctx.author.display_name)
                            )
                            raise AdventureCheckFailure(wrong_item)
                        else:
                            break
                    consumed.append(item)
                except asyncio.TimeoutError:
                    timeout_msg = _("I don't have all day you know, **{}**.").format(
                        self.escape(ctx.author.display_name)
                    )
                    return await smart_embed(ctx, timeout_msg, success=False)
                finally:
                    task.cancel()
                if item.rarity in ["forged", "set"]:
                    raise AdventureCheckFailure(_("**{c}**, {item.rarity} items cannot be reforged.").format(
                        c=self.escape(ctx.author.display_name), item=item)
                    )
                newitem = await self._to_forge(ctx, consumed, c)
                for x in consumed:
                    c.backpack[x.name].owned -= 1
                    if c.backpack[x.name].owned <= 0:
                        del c.backpack[x.name]
                    await self.config.user(ctx.author).set(await c.to_json(self.config))
                # save so the items are eaten up already
                for item in c.get_current_equipment():
                    if item.rarity == "forged":
                        c = await c.unequip_item(item)
                lookup = list(i for n, i in c.backpack.items() if i.rarity == "forged")
                if len(lookup) > 0:
                    forge_str = box(
                        _("{author}, you already have a device. Do you want to replace {replace}?").format(
                            author=self.escape(ctx.author.display_name), replace=", ".join([str(x) for x in lookup]),
                        ),
                        lang="css",
                    )
                    forge_msg = await ctx.send(forge_str)
                    start_adding_reactions(forge_msg, ReactionPredicate.YES_OR_NO_EMOJIS)
                    pred = ReactionPredicate.yes_or_no(forge_msg, ctx.author)
                    try:
                        await ctx.bot.wait_for("reaction_add", check=pred, timeout=60)
                    except asyncio.TimeoutError:
                        await self._clear_react(forge_msg)
                        return
                    with contextlib.suppress(discord.HTTPException):
                        await forge_msg.delete()
                    if pred.result:  # user reacted with Yes.
                        c.heroclass["cooldown"] = time.time() + cooldown_time
                        created_item = box(
                            _("{author}, your new {newitem} consumed {lk} and is now lurking in your backpack.").format(
                                author=self.escape(ctx.author.display_name),
                                newitem=newitem,
                                lk=", ".join([str(x) for x in lookup]),
                            ),
                            lang="css",
                        )
                        for item in lookup:
                            del c.backpack[item.name]
                        await ctx.send(created_item)
                        c.backpack[newitem.name] = newitem
                        await self.config.user(ctx.author).set(await c.to_json(self.config))
                    else:
                        c.heroclass["cooldown"] = time.time() + cooldown_time
                        await self.config.user(ctx.author).set(await c.to_json(self.config))
                        mad_forge = box(
                            _("{author}, {newitem} got mad at your rejection and blew itself up.").format(
                                author=self.escape(ctx.author.display_name), newitem=newitem
                            ),
                            lang="css",
                        )
                        return await ctx.send(mad_forge)
                else:
                    c.heroclass["cooldown"] = time.time() + cooldown_time
                    c.backpack[newitem.name] = newitem
                    await self.config.user(ctx.author).set(await c.to_json(self.config))
                    forged_item = box(
                        _("{author}, your new {newitem} is lurking in your backpack.").format(
                            author=self.escape(ctx.author.display_name), newitem=newitem
                        ),
                        lang="css",
                    )
                    await ctx.send(forged_item)

    @commands.group()
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def give(self, ctx: Context):
        """[Admin] Commands to add things to players' inventories."""

    @give.command(name="item")
    async def _give_item(self, ctx: Context, user: Member, item_name: str, *, stats: Stats):
        """[Admin] Adds a custom item to a specified member.

        Item names containing spaces must be enclosed in double quotes. `[p]give item @locastan
        "fine dagger" 1 att 1 charisma rare twohanded` will give a two handed .fine_dagger with 1
        attack and 1 charisma to locastan. if a stat is not specified it will default to 0, order
        does not matter. available stats are attack(att), charisma(diplo) or charisma(cha),
        intelligence(int), dexterity(dex), and luck.

        Item rarity is one of normal, rare, epic, legendary, set, forged, event.

        Event items can have their level requirement and degrade number set via:
        N degrade - (Set to -1 to never degrade on rebirths)
        N level

        `[p]give item @locastan "fine dagger" 1 att 1 charisma -1 degrade 100 level rare twohanded`
        """
        if item_name.isnumeric():
            raise AdventureCheckFailure(_("Item names cannot be numbers."))
        item_name = re.sub(r"[^\w ]", "", item_name)
        if user is None:
            user = ctx.author
        new_item = {item_name: stats}
        item = Item.from_json(new_item)
        async with self.get_lock(user):
            c = await self.get_character_from_json(user)
            await c.add_to_backpack(item)
            await self.config.user(user).set(await c.to_json(self.config))
        await ctx.send(
            box(
                _("An item named {item} has been created and placed in {author}'s backpack.").format(
                    item=item, author=self.escape(user.display_name)
                ),
                lang="css",
            )
        )

    @give.command(name="loot")
    async def _give_loot(self, ctx: Context, loot_type: str, user: Member = None, number: int = 1):
        """[Admin] Give treasure chest(s) to a specified member."""

        if user is None:
            user = ctx.author
        loot_types = ["normal", "rare", "epic", "legendary", "ascended", "set"]
        if loot_type not in loot_types:
            raise AdventureCheckFailure(
                (
                    "Valid loot types: `normal`, `rare`, `epic`, `legendary`, `ascended` or `set`: "
                    "ex. `{}give loot normal @locastan` "
                ).format(ctx.prefix),
            )
        if loot_type in ["legendary", "set", "ascended"] and not await ctx.bot.is_owner(ctx.author):
            raise AdventureCheckFailure(_("You are not worthy to award legendary loot."))
        async with self.get_lock(user):
            c = await self.get_character_from_json(user)
            if loot_type == "rare":
                c.treasure[1] += number
            elif loot_type == "epic":
                c.treasure[2] += number
            elif loot_type == "legendary":
                c.treasure[3] += number
            elif loot_type == "ascended":
                c.treasure[4] += number
            elif loot_type == "set":
                c.treasure[5] += number
            else:
                c.treasure[0] += number
            await self.config.user(user).set(await c.to_json(self.config))
            await ctx.send(
                box(
                    _(
                        "{author} now owns {normal} normal, "
                        "{rare} rare, {epic} epic, "
                        "{leg} legendary, {asc} ascended and {set} set treasure chests."
                    ).format(
                        author=self.escape(user.display_name),
                        normal=str(c.treasure[0]),
                        rare=str(c.treasure[1]),
                        epic=str(c.treasure[2]),
                        leg=str(c.treasure[3]),
                        asc=str(c.treasure[4]),
                        set=str(c.treasure[5]),
                    ),
                    lang="css",
                )
            )

    @commands.group(cooldown_after_parsing=True, invoke_without_command=True)
    @commands.bot_has_permissions(add_reactions=True)
    @commands.cooldown(rate=1, per=7200, type=commands.BucketType.user)
    async def heroclass(self, ctx: Context, clz: str = None, action: str = None):
        """Allows you to select a class if you are level 10 or above.

        For information on class use: `[p]heroclass classname info`.
        """
        if self.in_adventure(ctx):
            raise AdventureCheckFailure(_("The monster ahead growls menacingly, and will not let you leave."))

        classes = {
            "Autoaimer": {
                "name": _("Autoaimer"),
                "ability": False,
                "desc": _(
                    "Autoaimers have the option to use their gadget and add large bonuses to their accuracy, "
                    "but their gadget can sometimes go astray...\n"
                    "Use the gadget command when attacking in an adventure."
                ),
                "cooldown": time.time(),
            },
            "Tinkerer": {
                "name": _("Tinkerer"),
                "ability": False,
                "desc": _(
                    "Tinkerers can forge two different items into a device "
                    "bound to their very soul.\nUse the forge command."
                ),
                "cooldown": time.time(),
            },
            "Berserker": {
                "name": _("Berserker"),
                "ability": False,
                "desc": _(
                    "Berserkers have the option to use their super and add big bonuses to attacks, "
                    "but fumbles hurt.\nUse the super command when attacking in an adventure."
                ),
                "cooldown": time.time(),
            },
            "Samaritan": {
                "name": _("Samaritan"),
                "ability": False,
                "desc": _(
                    "Samaritans can report the opponent group when playing.\n"
                    "Use the report command when fighting in an adventure."
                ),
                "cooldown": time.time(),
            },
            "Ranger": {
                "name": _("Ranger"),
                "ability": False,
                "desc": _(
                    "Rangers can gain a special pet, which can find items and give "
                    "reward bonuses.\nUse the pet command to see pet options."
                ),
                "pet": {},
                "cooldown": time.time(),
                "catch_cooldown": time.time(),
            },
            "Tilter": {
                "name": _("Tilter"),
                "ability": False,
                "desc": _(
                    "Tilters can aid their comrades by distracting their enemies.\n"
                    "Use the emote command when being diplomatic in an adventure."
                ),
                "cooldown": time.time(),
            },
        }

        if clz is None:
            ctx.command.reset_cooldown(ctx)
            await smart_embed(
                ctx,
                _(
                    "So you feel like taking on a class, **{author}**?\n"
                    "Available classes are: Tinkerer, Berserker, "
                    "Autoaimer, Samaritan, Ranger and Tilter.\n"
                    "Use `{prefix}heroclass name-of-class` to choose one."
                ).format(author=self.escape(ctx.author.display_name), prefix=ctx.prefix),
            )

        else:
            clz = clz.title()
            if clz not in classes:
                raise AdventureCheckFailure(_("{} may be a class somewhere, but not on my watch.").format(clz))
            elif clz in classes and action is None:
                async with self.get_lock(ctx.author):
                    bal = await bank.get_balance(ctx.author)
                    currency_name = await bank.get_currency_name(ctx.guild)
                    if str(currency_name).startswith("<"):
                        currency_name = "credits"
                    spend = round(bal * 0.2)
                    c = await self.get_character_from_json(ctx.author)
                    if c.heroclass["name"] == clz:
                        raise AdventureCheckFailure(_("You already are a {}.").format(clz))
                    class_msg = await ctx.send(
                        box(
                            _("This will cost {spend} {currency_name}. Do you want to continue, {author}?").format(
                                spend=humanize_number(spend),
                                currency_name=currency_name,
                                author=self.escape(ctx.author.display_name),
                            ),
                            lang="css",
                        )
                    )
                    broke = box(
                        _("You don't have enough {currency_name} to train to be a {clz}.").format(
                            currency_name=currency_name, clz=clz.title()
                        ),
                        lang="css",
                    )
                    start_adding_reactions(class_msg, ReactionPredicate.YES_OR_NO_EMOJIS)
                    pred = ReactionPredicate.yes_or_no(class_msg, ctx.author)
                    try:
                        await ctx.bot.wait_for("reaction_add", check=pred, timeout=60)
                    except asyncio.TimeoutError:
                        await self._clear_react(class_msg)
                        ctx.command.reset_cooldown(ctx)
                        return

                    if not pred.result:
                        await class_msg.edit(
                            content=box(
                                _("{author} decided to continue being a {h_class}.").format(
                                    author=self.escape(ctx.author.display_name), h_class=c.heroclass["name"],
                                ),
                                lang="css",
                            )
                        )
                        ctx.command.reset_cooldown(ctx)
                        return await self._clear_react(class_msg)
                    if bal < spend:
                        await class_msg.edit(content=broke)
                        ctx.command.reset_cooldown(ctx)
                        return await self._clear_react(class_msg)
                    if not await bank.can_spend(ctx.author, spend):
                        return await class_msg.edit(content=broke)
                    c = await self.get_character_from_json(ctx.author)

                    clz = classes[clz]["name"]
                    article = "an" if clz[0] in ["A", "E", "I", "O", "U"] else "a"

                    now_class_msg = _("Congratulations, {author}.\nYou are now {article} {clz}.").format(
                        author=self.escape(ctx.author.display_name), clz=clz, article=article
                    )
                    if c.lvl >= 10:
                        if c.heroclass["name"] == "Tinkerer" or c.heroclass["name"] == "Ranger":
                            if c.heroclass["name"] == "Tinkerer":
                                await self._clear_react(class_msg)
                                await class_msg.edit(
                                    content=box(
                                        _(
                                            "{}, you will lose your forged "
                                            "device if you change your class.\nShall I proceed?"
                                        ).format(self.escape(ctx.author.display_name)),
                                        lang="css",
                                    )
                                )
                            else:
                                await self._clear_react(class_msg)
                                await class_msg.edit(
                                    content=box(
                                        _(
                                            "{}, you will lose your pet if you change your class.\nShall I proceed?"
                                        ).format(self.escape(ctx.author.display_name)),
                                        lang="css",
                                    )
                                )
                            start_adding_reactions(class_msg, ReactionPredicate.YES_OR_NO_EMOJIS)
                            pred = ReactionPredicate.yes_or_no(class_msg, ctx.author)
                            try:
                                await ctx.bot.wait_for("reaction_add", check=pred, timeout=60)
                            except asyncio.TimeoutError:
                                await self._clear_react(class_msg)
                                ctx.command.reset_cooldown(ctx)
                                return
                            if pred.result:  # user reacted with Yes.
                                tinker_wep = []
                                for item in c.get_current_equipment():
                                    if item.rarity == "forged":
                                        c = await c.unequip_item(item)
                                for (name, item) in c.backpack.items():
                                    if item.rarity == "forged":
                                        tinker_wep.append(item)
                                for item in tinker_wep:
                                    del c.backpack[item.name]
                                if c.heroclass["name"] == "Tinkerer":
                                    await self.config.user(ctx.author).set(await c.to_json(self.config))
                                    if tinker_wep:
                                        await class_msg.edit(
                                            content=box(
                                                _("{} has run off to find a new master.").format(
                                                    humanize_list(tinker_wep)
                                                ),
                                                lang="css",
                                            )
                                        )

                                else:
                                    c.heroclass["ability"] = False
                                    c.heroclass["pet"] = {}
                                    c.heroclass = classes[clz]

                                    await self.config.user(ctx.author).set(await c.to_json(self.config))
                                    await self._clear_react(class_msg)
                                    await class_msg.edit(
                                        content=box(
                                            _("{} released their pet into the wild.\n").format(
                                                self.escape(ctx.author.display_name)
                                            ),
                                            lang="css",
                                        )
                                    )
                                await class_msg.edit(content=class_msg.content + box(now_class_msg, lang="css"))
                            else:
                                ctx.command.reset_cooldown(ctx)
                                return
                        if c.skill["pool"] < 0:
                            c.skill["pool"] = 0
                        c.heroclass = classes[clz]
                        if c.heroclass["name"] == "Autoaimer":
                            c.heroclass["cooldown"] = max(240, (1140 - ((c.luck + c.total_int) * 2))) + time.time()
                        elif c.heroclass["name"] == "Samaritan":
                            c.heroclass["cooldown"] = 3 * max(240, (1140 - ((c.luck + c.total_int) * 2))) + time.time()
                        elif c.heroclass["name"] == "Ranger":
                            c.heroclass["cooldown"] = max(1800, (7200 - (c.luck * 2 + c.total_int * 2))) + time.time()
                            c.heroclass["catch_cooldown"] = (
                                max(600, (3600 - (c.luck * 2 + c.total_int * 2))) + time.time()
                            )
                        elif c.heroclass["name"] == "Berserker":
                            c.heroclass["cooldown"] = max(240, (1140 - ((c.luck + c.total_att) * 2))) + time.time()
                        elif c.heroclass["name"] == "Tilter":
                            c.heroclass["cooldown"] = max(240, (1140 - ((c.luck + c.total_cha) * 2))) + time.time()
                        elif c.heroclass["name"] == "Tinkerer":
                            c.heroclass["cooldown"] = max(900, (3600 - (c.luck + c.total_int) * 2)) + time.time()
                        await self.config.user(ctx.author).set(await c.to_json(self.config))
                        await self._clear_react(class_msg)
                        await class_msg.edit(content=box(now_class_msg, lang="css"))
                        try:
                            await bank.withdraw_credits(ctx.author, spend)
                        except ValueError:
                            return await class_msg.edit(content=broke)
                    else:
                        raise AdventureCheckFailure(_("**{}**, you need to be at least level 10 to choose a class.").format(
                            self.escape(ctx.author.display_name))
                        )

    @staticmethod
    def check_running_adventure(ctx):
        for (channel_id, session) in ctx.bot.get_cog("Adventure")._sessions.items():
            user_ids: list = []
            options = ["rage", "autoaim", "rant", "pray", "run"]
            for i in options:
                user_ids += [u.id for u in getattr(session, i)]
            if ctx.author.id in user_ids:
                return False
        return True

    @heroclass.command()
    async def info(self, ctx: Context, clz: str):
        classes = {
            "Autoaimer": {
                "name": _("Autoaimer"),
                "ability": False,
                "desc": _(
                    "Autoaimers have the option to use their gadget and add large bonuses to their accuracy, "
                    "but their gadget can sometimes go astray...\n"
                    "Use the gadget command when attacking in an adventure."
                ),
                "cooldown": time.time(),
            },
            "Tinkerer": {
                "name": _("Tinkerer"),
                "ability": False,
                "desc": _(
                    "Tinkerers can forge two different items into a device "
                    "bound to their very soul.\nUse the forge command."
                ),
                "cooldown": time.time(),
            },
            "Berserker": {
                "name": _("Berserker"),
                "ability": False,
                "desc": _(
                    "Berserkers have the option to use their super and add big bonuses to attacks, "
                    "but fumbles hurt.\nUse the super command when attacking in an adventure."
                ),
                "cooldown": time.time(),
            },
            "Samaritan": {
                "name": _("Samaritan"),
                "ability": False,
                "desc": _(
                    "Samaritans can report the opponent group when playing.\n"
                    "Use the report command when fighting in an adventure."
                ),
                "cooldown": time.time(),
            },
            "Ranger": {
                "name": _("Ranger"),
                "ability": False,
                "desc": _(
                    "Rangers can gain a special pet, which can find items and give "
                    "reward bonuses.\nUse the pet command to see pet options."
                ),
                "pet": {},
                "cooldown": time.time(),
                "catch_cooldown": time.time(),
            },
            "Tilter": {
                "name": _("Tilter"),
                "ability": False,
                "desc": _(
                    "Tilters can aid their comrades by distracting their enemies.\n"
                    "Use the emote command when being diplomatic in an adventure."
                ),
                "cooldown": time.time(),
            },
        }

        clz = clz.title()
        if clz in classes:
            ctx.command.reset_cooldown(ctx)
            return await smart_embed(ctx, f"{classes[clz]['desc']}")
        else:
            raise AdventureCheckFailure(_("{} may be a class somewhere, but not on my watch.").format(clz))

    @commands.command(cooldown_after_parsing=True)
    @commands.bot_has_permissions(add_reactions=True)
    @is_dm()
    @commands.cooldown(rate=1, per=4, type=commands.BucketType.user)
    async def loot(self, ctx: Context, box_type: str = None, number: DynamicInt = 1):
        """This opens one of your precious treasure chests.

        Use the box rarity type with the command: normal, rare, epic, legendary or set.
        """
        if isinstance(number, int) and ((not self.is_dev(ctx.author) and number > 100) or number < 1):
            raise AdventureCheckFailure(_("Nice try :smirk:."))
        if self.in_adventure(ctx):
            raise AdventureCheckFailure(
                _("You tried to open a loot chest but then realised you left them all back at the inn.")
            )

        msgs = []
        async with self.get_lock(ctx.author):
            c = await self.get_character_from_json(ctx.author)
            if not box_type:
                return await ctx.send(
                    box(
                        _(
                            "{author} owns {normal} normal, "
                            "{rare} rare, {epic} epic, {leg} legendary, {asc} ascended and {set} set chests."
                        ).format(
                            author=self.escape(ctx.author.display_name),
                            normal=str(c.treasure[0]),
                            rare=str(c.treasure[1]),
                            epic=str(c.treasure[2]),
                            leg=str(c.treasure[3]),
                            asc=str(c.treasure[4]),
                            set=str(c.treasure[5]),
                        ),
                        lang="css",
                    )
                )
            if box_type == "normal":
                if number == "all":
                    number = c.treasure[0]
                elif isinstance(number, str) and number.endswith("%"):
                    percent = int(number[:-1]) / 100
                    number = round(percent * c.treasure[0])
                redux = 0
            elif box_type == "rare":
                if number == "all":
                    number = c.treasure[1]
                elif isinstance(number, str) and number.endswith("%"):
                    percent = int(number[:-1]) / 100
                    number = round(percent * c.treasure[1])
                redux = 1
            elif box_type == "epic":
                if number == "all":
                    number = c.treasure[2]
                elif isinstance(number, str) and number.endswith("%"):
                    percent = int(number[:-1]) / 100
                    number = round(percent * c.treasure[2])
                redux = 2
            elif box_type == "legendary":
                if number == "all":
                    number = c.treasure[3]
                elif isinstance(number, str) and number.endswith("%"):
                    percent = int(number[:-1]) / 100
                    number = round(percent * c.treasure[3])
                redux = 3
            elif box_type == "ascended":
                if number == "all":
                    number = c.treasure[4]
                elif isinstance(number, str) and number.endswith("%"):
                    percent = int(number[:-1]) / 100
                    number = round(percent * c.treasure[4])
                redux = 4
            elif box_type == "set":
                if number == "all":
                    number = c.treasure[5]
                elif isinstance(number, str) and number.endswith("%"):
                    percent = int(number[:-1]) / 100
                    number = round(percent * c.treasure[5])
                redux = 5
            elif box_type != "all":
                raise AdventureCheckFailure(
                    _("There is talk of a {} treasure chest but nobody ever saw one.").format(box_type)
                )
            
            if box_type == "all":
                treasure = sum(i for i in c.treasure)
                number = treasure # min
            else:
                treasure = c.treasure[redux]

            if treasure < 1 or treasure < number:
                raise AdventureCheckFailure(_("**{author}**, you do not have enough {box} treasure chests to open.").format(
                    author=self.escape(ctx.author.display_name), box=box_type)
                )
            else:
                if number > 1:
                    async with ctx.typing():
                        # atomically save reduced loot count then lock again when saving inside
                        # open chests
                        if box_type == "all":
                            box_types = ["normal", "rare", "epic", "legendary", "ascended", "set"]
                        else:
                            box_types = [box_type]

                        msg = _(
                            "{}, you've opened the following items:\n"
                            "( RAGE | RANT | ACC | DEX | LUCK ) | LEVEL REQ | LOOTED | SET (SET PIECES)"
                        ).format(self.escape(ctx.author.display_name))

                        for type_ in box_types:
                            if len(box_types) > 1:
                                redux = box_types.index(type_)
                                number = c.treasure[redux]

                            if number > 0:
                                c.treasure[redux] -= number
                                items = await self._open_chests(ctx, ctx.author, type_, number, character=c)
                                rjust = max([len(str(i)) for i in items.values()])
                                async for item in AsyncIter(items.values()):
                                    settext = ""
                                    att_space = " " if len(str(item.att)) >= 1 else ""
                                    cha_space = " " if len(str(item.cha)) >= 1 else ""
                                    int_space = " " if len(str(item.int)) >= 1 else ""
                                    dex_space = " " if len(str(item.dex)) >= 1 else ""
                                    luck_space = " " if len(str(item.luck)) >= 1 else ""
                                    owned = f" | {item.owned}"
                                    if item.set:
                                        settext += f" | Set `{item.set}` ({item.parts}pcs)"

                                    equip_lvl = equip_level(c, item)
                                    if c.lvl < equip_lvl:
                                        lv_str = f"[{equip_lvl}]"
                                    else:
                                        lv_str = f"{equip_lvl}"

                                    msg += (
                                        f"\n{str(item):<{rjust}} - "
                                        f"({att_space}{item.att} |"
                                        f"{cha_space}{item.cha} |"
                                        f"{int_space}{item.int} |"
                                        f"{dex_space}{item.dex} |"
                                        f"{luck_space}{item.luck} )"
                                        f" | Lv {lv_str:<3}"
                                        f"{owned}{settext}"
                                    )

                        await self.config.user(ctx.author).set(await c.to_json(self.config))
                        msgs = []
                        async for page in AsyncIter(pagify(msg, page_length=1900)):
                            msgs.append(box(page, lang="css"))
                else:
                    msgs = []
                    # atomically save reduced loot count then lock again when saving inside
                    # open chests
                    if box_type == "all":
                        for redux in range(6):
                            if c.treasure[redux]:
                                c.treasure[redux] -= 1
                    else:
                        c.treasure[redux] -= 1
                    await self.config.user(ctx.author).set(await c.to_json(self.config))

                    await self._open_chest(ctx, ctx.author, box_type, character=c)  # returns item and msg
        if msgs:
            await menu(ctx, msgs, MENU_CONTROLS)

    @commands.command(name="negaverse", aliases=["nv"], cooldown_after_parsing=True)
    @commands.cooldown(rate=1, per=3600, type=commands.BucketType.user)
    @commands.guild_only()
    async def _negaverse(self, ctx: Context, offering: DynamicInt = None):
        """This will send you to fight a nega-member!"""
        if self.in_adventure(ctx):
            raise AdventureCheckFailure(
                _("You tried to teleport to another dimension but the monster ahead did not give you a chance.")
            )

        bal = await bank.get_balance(ctx.author)
        currency_name = await bank.get_currency_name(ctx.guild)
        if offering is None:
            raise AdventureCheckFailure(
                _(
                    "**{author}**, you need to specify how many "
                    "{currency_name} you are willing to offer to the gods for your success."
                ).format(author=self.escape(ctx.author.display_name), currency_name=currency_name),
            )
        if offering == "all":
            offering = int(bal)
        elif isinstance(offering, str) and offering.endswith("%"):
            percent = int(offering[:-1]) / 100
            offering = round(percent * int(bal))

        if offering <= 500 or bal <= 500:
            raise AdventureCheckFailure(_("The gods refuse your pitiful offering."))
        if offering > bal:
            offering = int(bal)
        lock = self.get_lock(ctx.author)
        await lock.acquire()
        try:
            nv_msg = await ctx.send(
                _(
                    "**{author}**, this will cost you at least {offer} {currency_name}.\n"
                    "You currently have {bal}. Do you want to proceed?"
                ).format(
                    author=self.escape(ctx.author.display_name),
                    offer=humanize_number(offering),
                    currency_name=currency_name,
                    bal=humanize_number(bal),
                )
            )
            start_adding_reactions(nv_msg, ReactionPredicate.YES_OR_NO_EMOJIS)
            pred = ReactionPredicate.yes_or_no(nv_msg, ctx.author)
            try:
                await ctx.bot.wait_for("reaction_add", check=pred, timeout=60)
            except asyncio.TimeoutError:
                ctx.command.reset_cooldown(ctx)
                await self._clear_react(nv_msg)
                lock.release()
                return
            if not pred.result:
                with contextlib.suppress(discord.HTTPException):
                    ctx.command.reset_cooldown(ctx)
                    await nv_msg.edit(
                        content=_("**{}** decides against visiting the negaverse... for now.").format(
                            self.escape(ctx.author.display_name)
                        )
                    )
                    lock.release()
                    return await self._clear_react(nv_msg)

            percentage_offered = (offering / bal) * 100
            min_roll = int(percentage_offered / 10)
            entry_roll = max(random.randint(max(1, min_roll), 20), 0)
            if entry_roll == 1:
                tax_mod = random.randint(4, 8)
                tax = round(bal / tax_mod)
                if tax > offering:
                    loss = tax
                else:
                    loss = offering
                await bank.withdraw_credits(ctx.author, loss)
                entry_msg = _(
                    "A swirling void slowly grows and you watch in horror as it rushes to "
                    "wash over you, leaving you cold... and your coin pouch significantly lighter. "
                    "The portal to the negaverse remains closed."
                )
                lock.release()
                return await nv_msg.edit(content=entry_msg)
            else:
                entry_msg = _(
                    "Shadowy hands reach out to take your offering from you and a swirling "
                    "black void slowly grows and engulfs you, transporting you to the negaverse."
                )
                await nv_msg.edit(content=entry_msg)
                await self._clear_react(nv_msg)
                await bank.withdraw_credits(ctx.author, offering)

            negachar = _("Nega-{c}").format(c=self.escape(random.choice(ctx.message.guild.members).display_name))

            nega_msg = await ctx.send(
                _("**{author}** enters the negaverse and meets **{negachar}**.").format(
                    author=self.escape(ctx.author.display_name), negachar=negachar
                )
            )

            character = await self.get_character_from_json(ctx.author, release_lock=True)
            roll = random.randint(max(1, min_roll * 2), 50)
            versus = random.randint(10, 60)
            xp_mod = random.randint(1, 10)
            daymult = self._daily_bonus.get(str(datetime.today().weekday()), 0)
            xp_won = int((offering / xp_mod))
            xp_to_max = int((character.maxlevel + 1) ** 3.5)
            ten_percent = xp_to_max * 0.1
            xp_won = ten_percent if xp_won > ten_percent else xp_won
            xp_won = int(xp_won * (min(max(random.randint(0, character.rebirths), 1), 50) / 100 + 1))
            xp_won = int(xp_won * (character.gear_set_bonus.get("xpmult", 1) + daymult))
            if roll < 10:
                loss = round(bal // 3)
                looted = ""
                try:
                    await bank.withdraw_credits(ctx.author, loss)
                    loss_string = humanize_number(loss)
                except ValueError:
                    await bank.set_balance(ctx.author, 0)
                    loss_string = _("all of their")
                if character.bal < loss:
                    items = await character.looted(how_many=max(int(10 - roll) // 2, 1))
                    if items:
                        item_string = "\n".join(
                            ["( RAGE | RANT | ACC | DEX | LUCK ) | LEVEL REQ | SET (SET PIECES)"] + [f"{i} - {character.get_looted_message(v)}" for v, i in items]
                        )
                        looted = box(f"{item_string}", lang="css")
                        await self.config.user(ctx.author).set(await character.to_json(self.config))
                loss_msg = _(
                    ", losing {loss} {currency_name} as **{negachar}** rifled through their belongings."
                ).format(loss=loss_string, currency_name=currency_name, negachar=negachar)
                if looted:
                    loss_msg += _(" **{negachar}** also stole the following items:\n\n{items}").format(
                        items=looted, negachar=negachar
                    )
                await nega_msg.edit(
                    content=_("{content}\n**{author}** fumbled and died to **{negachar}'s** savagery{loss_msg}").format(
                        content=nega_msg.content,
                        author=self.escape(ctx.author.display_name),
                        negachar=negachar,
                        loss_msg=loss_msg,
                    )
                )
                ctx.command.reset_cooldown(ctx)
            elif roll == 50 and versus < 50:
                await nega_msg.edit(
                    content=_(
                        "{content}\n**{author}** decapitated **{negachar}**. "
                        "You gain {xp_gain} xp and take "
                        "{offering} {currency_name} back from the shadowy corpse."
                    ).format(
                        content=nega_msg.content,
                        author=self.escape(ctx.author.display_name),
                        negachar=negachar,
                        xp_gain=humanize_number(xp_won),
                        offering=humanize_number(offering),
                        currency_name=currency_name,
                    )
                )
                with contextlib.suppress(Exception):
                    lock.release()
                msg = await self._add_rewards(ctx, ctx.author, xp_won, offering, False)
                if msg:
                    await smart_embed(ctx, msg, success=True)
            elif roll > versus:
                await nega_msg.edit(
                    content=_(
                        "{content}\n**{author}** "
                        "{dice}({roll}) bravely defeated **{negachar}** {dice}({versus}). "
                        "You gain {xp_gain} xp."
                    ).format(
                        dice=self.emojis.dice,
                        content=nega_msg.content,
                        author=self.escape(ctx.author.display_name),
                        roll=roll,
                        negachar=negachar,
                        versus=versus,
                        xp_gain=humanize_number(xp_won),
                    )
                )
                with contextlib.suppress(Exception):
                    lock.release()
                msg = await self._add_rewards(ctx, ctx.author, xp_won, 0, False)
                if msg:
                    await smart_embed(ctx, msg, success=True)
            elif roll == versus:
                ctx.command.reset_cooldown(ctx)
                await nega_msg.edit(
                    content=_(
                        "{content}\n**{author}** {dice}({roll}) almost killed **{negachar}** {dice}({versus})."
                    ).format(
                        dice=self.emojis.dice,
                        content=nega_msg.content,
                        author=self.escape(ctx.author.display_name),
                        roll=roll,
                        negachar=negachar,
                        versus=versus,
                    )
                )
            else:
                loss = round(bal / (random.randint(10, 25)))
                looted = ""
                try:
                    await bank.withdraw_credits(ctx.author, loss)
                    loss_string = humanize_number(loss)
                except ValueError:
                    await bank.set_balance(ctx.author, 0)
                    loss_string = _("all of their")
                if character.bal < loss:
                    items = await character.looted(how_many=max(int(10 - roll) // 2, 1))
                    if items:
                        item_string = "\n".join(
                            ["( RAGE | RANT | ACC | DEX | LUCK ) | LEVEL REQ | SET (SET PIECES)"] + [f"{i} - {character.get_looted_message(v)}" for v, i in items]
                        )
                        looted = box(f"{item_string}", lang="css")
                        await self.config.user(ctx.author).set(await character.to_json(self.config))
                loss_msg = _(", losing {loss} {currency_name} as **{negachar}** looted their backpack.").format(
                    loss=loss_string, currency_name=currency_name, negachar=negachar,
                )
                if looted:
                    loss_msg += _(" **{negachar}** also stole the following items\n\n{items}").format(
                        items=looted, negachar=negachar
                    )
                await nega_msg.edit(
                    content=_(
                        "**{author}** {dice}({roll}) was killed by **{negachar}** {dice}({versus}){loss_msg}"
                    ).format(
                        dice=self.emojis.dice,
                        author=self.escape(ctx.author.display_name),
                        roll=roll,
                        negachar=negachar,
                        versus=versus,
                        loss_msg=loss_msg,
                    )
                )
                ctx.command.reset_cooldown(ctx)
        finally:
            lock = self.get_lock(ctx.author)
            with contextlib.suppress(Exception):
                lock.release()
            character = await self.get_character_from_json(ctx.author)
            if character.last_currency_check + 600 < time.time() or character.bal > character.last_known_currency:
                character.last_known_currency = await bank.get_balance(ctx.author)
                character.last_currency_check = time.time()
                await self.config.user(ctx.author).set(await character.to_json(self.config))

    @commands.group(autohelp=False)
    @commands.cooldown(rate=1, per=60, type=commands.BucketType.user)
    async def pet(self, ctx: Context):
        """[Ranger Class Only]

        This allows a Ranger to tame or set free a pet or send it foraging.
        """
        if ctx.invoked_subcommand is None:
            if self.in_adventure(ctx):
                raise AdventureCheckFailure(_("You're too distracted with the monster you are facing."))

            async with self.get_lock(ctx.author):
                c = await self.get_character_from_json(ctx.author)
                if c.heroclass["name"] != "Ranger":
                    return await ctx.send(
                        box(
                            _("{}, you need to be a Ranger to do this.").format(self.escape(ctx.author.display_name)),
                            lang="css",
                        )
                    )
                if c.heroclass["pet"]:
                    ctx.command.reset_cooldown(ctx)
                    return await ctx.send(
                        box(
                            _("{author}, you already have a pet. Try foraging ({prefix}pet forage).").format(
                                author=self.escape(ctx.author.display_name), prefix=ctx.prefix
                            ),
                            lang="css",
                        )
                    )
                else:
                    cooldown_time = max(600, (3600 - ((c.luck + c.total_int) * 2)))
                    if "catch_cooldown" not in c.heroclass:
                        c.heroclass["catch_cooldown"] = cooldown_time + 1
                    if c.heroclass["catch_cooldown"] > time.time():
                        cooldown_time = c.heroclass["catch_cooldown"] - time.time()
                        raise AdventureCheckFailure(
                            _(
                                "You caught a pet recently, or you are a brand new Ranger. "
                                "You will be able to go hunting in {}."
                            ).format(
                                humanize_timedelta(seconds=int(cooldown_time))
                                if int(cooldown_time) >= 1
                                else _("1 second")
                            )
                        )
                    theme = await self.config.theme()
                    extra_pets = await self.config.themes.all()
                    extra_pets = extra_pets.get(theme, {}).get("pets", {})
                    pet_list = {**self.PETS, **extra_pets}
                    pet_choices = list(pet_list.keys())
                    pet = random.choice(pet_choices)
                    roll = random.randint(1, 50)
                    dipl_value = c.total_cha + (c.total_int // 3) + (c.luck // 2)
                    pet_reqs = pet_list[pet].get("bonuses", {}).get("req", {})
                    pet_msg4 = ""
                    can_catch = True
                    force_catch = False
                    if any(x in c.sets for x in ["The Supreme One", "Ainz Ooal Gown"]):
                        can_catch = True
                        pet = random.choice(
                            ["Albedo", "Rubedo", "Guardians of Nazarick", *random.choices(pet_choices, k=10),]
                        )
                        if pet in ["Albedo", "Rubedo", "Guardians of Nazarick"]:
                            force_catch = True
                    elif pet_reqs.get("bonuses", {}).get("req"):
                        if pet_reqs.get("set", None) in c.sets:
                            can_catch = True
                        else:
                            can_catch = False
                            pet_msg4 = _("\nPerhaps you're missing some requirements to tame {pet}.").format(pet=pet)
                    pet_msg = box(
                        _("{c} is trying to tame a pet.").format(c=self.escape(ctx.author.display_name)), lang="css",
                    )
                    user_msg = await ctx.send(pet_msg)
                    await asyncio.sleep(2)
                    pet_msg2 = box(
                        _("{author} started tracking a wild {pet_name} with a roll of {dice}({roll}).").format(
                            dice=self.emojis.dice, author=self.escape(ctx.author.display_name), pet_name=pet, roll=roll,
                        ),
                        lang="css",
                    )
                    await user_msg.edit(content=f"{pet_msg}\n{pet_msg2}")
                    await asyncio.sleep(2)
                    bonus = ""
                    if roll == 1:
                        bonus = _("But they stepped on a twig and scared it away.")
                    elif roll in [50, 25]:
                        bonus = _("They happen to have its favorite food.")
                    if force_catch is True or (dipl_value > pet_list[pet]["cha"] and roll > 1 and can_catch):
                        if force_catch:
                            roll = 0
                        else:
                            roll = random.randint(0, 2 if roll in [50, 25] else 5)
                        if roll == 0:
                            if force_catch and any(x in c.sets for x in ["The Supreme One", "Ainz Ooal Gown"]):
                                msg = random.choice(
                                    [
                                        _("{author} commands {pet} into submission.").format(
                                            pet=pet, author=self.escape(ctx.author.display_name)
                                        ),
                                        _("{pet} swears allegiance to the Supreme One.").format(
                                            pet=pet, author=self.escape(ctx.author.display_name)
                                        ),
                                        _("{pet} takes an Oath of Allegiance to the Supreme One.").format(
                                            pet=pet, author=self.escape(ctx.author.display_name)
                                        ),
                                    ]
                                )
                                pet_msg3 = box(msg, lang="css")
                            else:
                                pet_msg3 = box(
                                    _("{bonus}\nThey successfully tamed the {pet}.").format(bonus=bonus, pet=pet),
                                    lang="css",
                                )
                            await user_msg.edit(content=f"{pet_msg}\n{pet_msg2}\n{pet_msg3}")
                            c.heroclass["pet"] = pet_list[pet]
                            c.heroclass["catch_cooldown"] = time.time() + cooldown_time
                            await self.config.user(ctx.author).set(await c.to_json(self.config))
                        elif roll == 1:
                            bonus = _("But they stepped on a twig and scared it away.")
                            pet_msg3 = box(_("{bonus}\nThe {pet} escaped.").format(bonus=bonus, pet=pet), lang="css")
                            await user_msg.edit(content=f"{pet_msg}\n{pet_msg2}\n{pet_msg3}{pet_msg4}")
                        else:
                            bonus = ""
                            pet_msg3 = box(_("{bonus}\nThe {pet} escaped.").format(bonus=bonus, pet=pet), lang="css")
                            await user_msg.edit(content=f"{pet_msg}\n{pet_msg2}\n{pet_msg3}{pet_msg4}")
                    else:
                        pet_msg3 = box(_("{bonus}\nThe {pet} escaped.").format(bonus=bonus, pet=pet), lang="css")
                        await user_msg.edit(content=f"{pet_msg}\n{pet_msg2}\n{pet_msg3}{pet_msg4}")

    @pet.command(name="forage")
    @commands.bot_has_permissions(add_reactions=True)
    async def _forage(self, ctx: Context):
        """Use your pet to forage for items!"""
        if self.in_adventure(ctx):
            raise AdventureCheckFailure(_("You're too distracted with the monster you are facing."))
        async with self.get_lock(ctx.author):
            c = await self.get_character_from_json(ctx.author)
            if c.heroclass["name"] != "Ranger":
                return
            if not c.heroclass["pet"]:
                return await ctx.send(
                    box(
                        _("{}, you need to have a pet to do this.").format(self.escape(ctx.author.display_name)),
                        lang="css",
                    )
                )
            cooldown_time = max(1800, (7200 - ((c.luck + c.total_int) * 2)))
            if "cooldown" not in c.heroclass:
                c.heroclass["cooldown"] = cooldown_time + 1
            if c.heroclass["cooldown"] <= time.time():
                await self._open_chest(ctx, c.heroclass["pet"]["name"], "pet", character=c)
                c.heroclass["cooldown"] = time.time() + cooldown_time
                await self.config.user(ctx.author).set(await c.to_json(self.config))
            else:
                cooldown_time = c.heroclass["cooldown"] - time.time()
                raise AdventureCheckFailure(_("This command is on cooldown. Try again in {}.").format(
                    humanize_timedelta(seconds=int(cooldown_time)) if int(cooldown_time) >= 1 else _("1 second"))
                )

    @pet.command(name="free")
    async def _free(self, ctx: Context):
        """Free your pet :cry:"""
        if self.in_adventure(ctx):
            raise AdventureCheckFailure(_("You're too distracted with the monster you are facing."))
        async with self.get_lock(ctx.author):
            c = await self.get_character_from_json(ctx.author)
            if c.heroclass["name"] != "Ranger":
                return await ctx.send(
                    box(
                        _("{}, you need to be a Ranger to do this.").format(self.escape(ctx.author.display_name)),
                        lang="css",
                    )
                )
            if c.heroclass["pet"]:
                c.heroclass["pet"] = {}
                await self.config.user(ctx.author).set(await c.to_json(self.config))
                return await ctx.send(
                    box(
                        _("{} released their pet into the wild.").format(self.escape(ctx.author.display_name)),
                        lang="css",
                    )
                )
            else:
                return await ctx.send(box(_("You don't have a pet."), lang="css"))

    @can_use_ability()
    @commands.command()
    async def report(self, ctx: Context):
        """[Samaritan Class Only]

        This allows a praying Samaritan to add substantial bonuses for heroes fighting the battle.
        """
        async with self.get_lock(ctx.author):
            c = await self.get_character_from_json(ctx.author)
            c.heroclass["ability"] = True

            await self.config.user(ctx.author).set(await c.to_json(self.config))
            await smart_embed(
                ctx,
                _("{report} **{c}** is sorting evidence out to report the teamers... {report}").format(
                    c=self.escape(ctx.author.display_name), report=self.emojis.skills.report
                ),
                success=True
            )

    @can_use_ability()
    @commands.command(name='super')
    async def super_(self, ctx: Context):
        """[Berserker Class Only]

        This allows a Berserker to add substantial attack bonuses for one battle.
        """
        async with self.get_lock(ctx.author):
            c = await self.get_character_from_json(ctx.author)
            c.heroclass["ability"] = True

            await self.config.user(ctx.author).set(await c.to_json(self.config))
            await smart_embed(
                ctx,
                _("{skill} **{c}** has a rotating yellow circle beneath their feet...  {skill}").format(
                    c=self.escape(ctx.author.display_name), skill=self.emojis.skills.berserker,
                ),
                success=True
            )

    @can_use_ability()
    @commands.command()
    async def gadget(self, ctx: Context):
        """[Autoaimer Class Only]

        This allows an Autoaimer to add substantial magic bonuses for one battle.
        """
        async with self.get_lock(ctx.author):
            c = await self.get_character_from_json(ctx.author)
            c.heroclass["ability"] = True

            await self.config.user(ctx.author).set(await c.to_json(self.config))
            await smart_embed(
                ctx,
                _("{skill1} **{c}** is reaching out for their green button... {skill2}").format(
                    c=self.escape(ctx.author.display_name), skill1=self.emojis.skills.autoaimer1,
                    skill2=self.emojis.skills.autoaimer2,
                ),
                success=True
            )

    @can_use_ability()
    @commands.command()
    async def emote(self, ctx: Context):
        """[Tilter Class Only]

        This allows a Tilter to add substantial diplomacy bonuses for one battle.
        """
        async with self.get_lock(ctx.author):
            c = await self.get_character_from_json(ctx.author)
            c.heroclass["ability"] = True

            await self.config.user(ctx.author).set(await c.to_json(self.config))
            await smart_embed(
                ctx,
                _("{skill1} **{c}** is ready with a barrage of emotes... {skill2}").format(
                    c=self.escape(ctx.author.display_name), skill1=self.emojis.skills.tilter1, skill2=self.emojis.skills.tilter2
                ),
                success=True
            )

    @commands.command()
    @commands.cooldown(rate=1, per=2, type=commands.BucketType.user)
    async def skill(self, ctx: Context, spend: str = None, amount: DynamicInt = 1):
        """This allows you to spend skillpoints.

        `[p]skill rage/rant/autoaim`
        `[p]skill reset` Will allow you to reset your skill points for a cost.
        """
        if self.in_adventure(ctx):
            raise AdventureCheckFailure(
                _("The skill cleric is back in town and the monster ahead of you is demanding your attention.")
            )

        if isinstance(amount, int) and amount < 1:
            raise AdventureCheckFailure(_("Nice try :smirk:"))
        async with self.get_lock(ctx.author):
            c = await self.get_character_from_json(ctx.author)
            if spend == "reset":
                last_reset = await self.config.user(ctx.author).last_skill_reset()
                if last_reset + 3600 > time.time():
                    raise AdventureCheckFailure(_("You reset your skills within the last hour, try again later."))
                bal = c.bal
                currency_name = await bank.get_currency_name(ctx.guild)
                offering = min(int(bal / 5 + (c.total_int // 3)), 1000000000)
                nv_msg = await ctx.send(
                    _(
                        "{author}, this will cost you at least {offering} {currency_name}.\n"
                        "You currently have {bal}. Do you want to proceed?"
                    ).format(
                        author=self.escape(ctx.author.display_name),
                        offering=humanize_number(offering),
                        currency_name=currency_name,
                        bal=humanize_number(bal),
                    )
                )
                start_adding_reactions(nv_msg, ReactionPredicate.YES_OR_NO_EMOJIS)
                pred = ReactionPredicate.yes_or_no(nv_msg, ctx.author)
                try:
                    await ctx.bot.wait_for("reaction_add", check=pred, timeout=60)
                except asyncio.TimeoutError:
                    await self._clear_react(nv_msg)
                    return

                if pred.result:
                    c.skill["pool"] += c.skill["att"] + c.skill["cha"] + c.skill["int"]
                    c.skill["att"] = 0
                    c.skill["cha"] = 0
                    c.skill["int"] = 0
                    await self.config.user(ctx.author).set(await c.to_json(self.config))
                    await self.config.user(ctx.author).last_skill_reset.set(int(time.time()))
                    await bank.withdraw_credits(ctx.author, offering)
                    await smart_embed(
                        ctx, _("{}, your skill points have been reset.").format(self.escape(ctx.author.display_name)),
                        success=True
                    )
                else:
                    await smart_embed(
                        ctx, _("Don't play games with me, {}.").format(self.escape(ctx.author.display_name)),
                        success=False
                    )
                return

            if amount == "all":
                amount = c.skill["pool"]
            elif isinstance(amount, str) and amount.endswith("%"):
                percent = int(amount[:-1]) / 100
                amount = round(percent * c.skill["pool"])
                

            if c.skill["pool"] <= 0:
                raise AdventureCheckFailure(
                    _("{}, you do not have unspent skillpoints.").format(self.escape(ctx.author.display_name))
                )
            elif c.skill["pool"] < amount:
                raise AdventureCheckFailure(
                    _("{}, you do not have enough unspent skillpoints.").format(self.escape(ctx.author.display_name)),
                )
            if spend is None:
                await smart_embed(
                    ctx,
                    _(
                        "**{author}**, you currently have **{skillpoints}** unspent skillpoints.\n"
                        "If you want to put them towards a permanent attack, "
                        "charisma or intelligence bonus, use "
                        "`{prefix}skill rage`, `{prefix}skill rant` or "
                        "`{prefix}skill accuracy`"
                    ).format(
                        author=self.escape(ctx.author.display_name),
                        skillpoints=str(c.skill["pool"]),
                        prefix=ctx.prefix,
                    ),
                )
            else:
                att = ["rage"]
                cha = ["rant"]
                intel = ["accuracy"]
                if spend not in att + cha + intel:
                    raise AdventureCheckFailure(
                        _("Don't try to fool me! There is no such thing as {}.").format(spend)
                    )
                elif spend in att:
                    c.skill["pool"] -= amount
                    c.skill["att"] += amount
                    spend = "rage"
                elif spend in cha:
                    c.skill["pool"] -= amount
                    c.skill["cha"] += amount
                    spend = "rant"
                elif spend in intel:
                    c.skill["pool"] -= amount
                    c.skill["int"] += amount
                    spend = "accuracy"
                await self.config.user(ctx.author).set(await c.to_json(self.config))
                await smart_embed(
                    ctx,
                    _("{author}, you permanently raised your {spend} value by {amount}.").format(
                        author=self.escape(ctx.author.display_name), spend=spend, amount=amount
                    ),
                    success=True
                )

    @commands.command(name="setinfo")
    @commands.bot_has_permissions(add_reactions=True, embed_links=True)
    async def set_show(self, ctx: Context, *, set_name: str = None):
        """Show set bonuses for the specified set."""

        set_list = humanize_list(sorted([f"`{i}`" for i in self.SET_BONUSES.keys()], key=str.lower))
        if set_name is None:
            raise AdventureCheckFailure(
                _("Use this command with one of the following set names: \n{sets}").format(sets=set_list)
            )

        title_cased_set_name = await self._title_case(set_name)
        sets = self.SET_BONUSES.get(title_cased_set_name)
        if sets is None:
            raise AdventureCheckFailure(_("`{input}` is not a valid set.\n\nPlease use one of the following full set names: \n{sets}").format(
                input=title_cased_set_name, sets=set_list)
            )

        c = await self.get_character_from_json(ctx.author)

        bonus_list = sorted(sets, key=itemgetter("parts"))
        msg_list = []
        for bonus in bonus_list:
            parts = bonus.get("parts", 0)
            attack = bonus.get("att", 0)
            charisma = bonus.get("cha", 0)
            intelligence = bonus.get("int", 0)
            dexterity = bonus.get("dex", 0)
            luck = bonus.get("luck", 0)

            attack = f"+{attack}" if attack > 0 else f"{attack}"
            charisma = f"+{charisma}" if charisma > 0 else f"{charisma}"
            intelligence = f"+{intelligence}" if intelligence > 0 else f"{intelligence}"
            dexterity = f"+{dexterity}" if dexterity > 0 else f"{dexterity}"
            luck = f"+{luck}" if luck > 0 else f"{luck}"

            statmult = round((bonus.get("statmult", 1) - 1) * 100)
            xpmult = round((bonus.get("xpmult", 1) - 1) * 100)
            cpmult = round((bonus.get("cpmult", 1) - 1) * 100)

            statmult = f"+{statmult}%" if statmult > 0 else f"{statmult}%"
            xpmult = f"+{xpmult}%" if xpmult > 0 else f"{xpmult}%"
            cpmult = f"+{cpmult}%" if cpmult > 0 else f"{cpmult}%"

            breakdown = _(
                "Attack:                [{attack}]\n"
                "Charisma:              [{charisma}]\n"
                "Intelligence:          [{intelligence}]\n"
                "Dexterity:             [{dexterity}]\n"
                "Luck:                  [{luck}]\n"
                "Stat Mulitplier:       [{statmult}]\n"
                "XP Multiplier:         [{xpmult}]\n"
                "Currency Multiplier:   [{cpmult}]\n\n"
            ).format(
                attack=attack,
                charisma=charisma,
                intelligence=intelligence,
                dexterity=dexterity,
                luck=luck,
                statmult=statmult,
                xpmult=xpmult,
                cpmult=cpmult,
            )
            stats_msg = _("{set_name}\n{part_val} Part Bonus\n\n").format(set_name=title_cased_set_name, part_val=parts)
            stats_msg += breakdown
            stats_msg += "Multiple complete set bonuses stack."
            msg_list.append(box(stats_msg, lang="ini"))
        set_items = {key: value for key, value in self.TR_GEAR_SET.items() if value["set"] == title_cased_set_name}

        d = {}
        for k, v in set_items.items():
            if len(v["slot"]) > 1:
                d.update({v["slot"][0]: {k: v}})
                d.update({v["slot"][1]: {k: v}})
            else:
                d.update({v["slot"][0]: {k: v}})

        d = order_slots_dict(d)

        loadout_display = await self._build_loadout_display({"items": d}, loadout=False)
        set_msg = _("{set_name} Set Pieces\n\n").format(set_name=title_cased_set_name)
        set_msg += loadout_display
        msg_list.append(box(set_msg, lang="css"))

        backpack_contents = _("{author}'s backpack \n\n{backpack}\n").format(
            author=self.escape(ctx.author.display_name),
            backpack=await c.get_backpack(set_name=title_cased_set_name, clean=True),
        )
        async for page in AsyncIter(pagify(backpack_contents, delims=["\n"], shorten_by=20, page_length=1950)):
            msg_list.append(box(page, lang="css"))

        await menu(ctx, pages=msg_list, controls=MENU_CONTROLS)

    async def _setinfo_details(self, ctx: Context, title_cased_set_name: str):
        """
        Helper function for setinfo to display set pieces.
        Reformats TR_GEAR_SET to be displayed using the loadout display.
        """

        set_items = {key: value for key, value in self.TR_GEAR_SET.items() if value["set"] == title_cased_set_name}

        d = {}
        for k, v in set_items.items():
            if len(v["slot"]) > 1:
                d.update({v["slot"][0]: {k: v}})
                d.update({v["slot"][1]: {k: v}})
            else:
                d.update({v["slot"][0]: {k: v}})

        stats = await self._build_loadout_display({"items": d}, loadout=False)
        msg = _("{set_name} Set Pieces\n\n").format(set_name=title_cased_set_name)
        msg += stats
        await ctx.send(box(msg, lang="css"))

    @commands.command()
    @commands.bot_has_permissions(add_reactions=True)
    async def stats(self, ctx: Context, *, user: Member = None):
        """This draws up a character sheet of you or an optionally specified member."""

        if user is None:
            user = ctx.author
        if user.bot:
            return
        c = await self.get_character_from_json(user)

        legend = _("( RAGE | ACC | RANT | DEX | LUCK ) | LEVEL REQ | [DEGRADE#] | SET (SET PIECES)")
        equipped_gear_msg = _("{user}'s Character Sheet\n\nItems Equipped:\n{legend}{equip}").format(
            legend=legend, equip=c.get_equipment(), user=c.user.display_name
        )
        if ctx.guild:
            await ctx.send(_("{}, sending you the stats in DMs.").format(ctx.author.display_name))
            try:
                await menu(
                    UserCtx(ctx, ctx.author),
                    pages=[box(c, lang="css"),
                    box(equipped_gear_msg, lang="css")],
                    controls=MENU_CONTROLS,
                )
            except discord.Forbidden:
                await ctx.send(_("{}, I cannot DM you.").format(ctx.author.mention))
        else:
            await menu(
                ctx, pages=[box(c, lang="css"), box(equipped_gear_msg, lang="css")], controls=MENU_CONTROLS,
            )

    async def _build_loadout_display(self, userdata, loadout=True):
        form_string = _("( RAGE  |  RANT  |  ACC  |  DEX  |  LUCK)")
        form_string += _("\n\nItems Equipped:") if loadout else ""
        last_slot = ""
        att = 0
        cha = 0
        intel = 0
        dex = 0
        luck = 0
        for (slot, data) in userdata["items"].items():

            if slot == "backpack":
                continue
            if last_slot == "two handed":
                last_slot = slot
                continue

            if not data:
                last_slot = slot
                form_string += _("\n\n {} slot").format(slot.title())
                continue
            item = Item.from_json(data)
            slot_name = userdata["items"][slot]["".join(i for i in data.keys())]["slot"]
            slot_name = slot_name[0] if len(slot_name) < 2 else _("two handed")
            form_string += _("\n\n {} slot").format(slot_name.title())
            last_slot = slot_name
            rjust = max([len(i) for i in data.keys()])
            form_string += f"\n  - {str(item):<{rjust}} - "
            form_string += (
                f"({item.att if len(item.slot) < 2 else (item.att * 2)} | "
                f"{item.cha if len(item.slot) < 2 else (item.cha * 2)} | "
                f"{item.int if len(item.slot) < 2 else (item.int * 2)} | "
                f"{item.dex if len(item.slot) < 2 else (item.dex * 2)} | "
                f"{item.luck if len(item.slot) < 2 else (item.luck * 2)})"
            )
            att += item.att if len(item.slot) < 2 else (item.att * 2)
            cha += item.cha if len(item.slot) < 2 else (item.cha * 2)
            intel += item.int if len(item.slot) < 2 else (item.int * 2)
            dex += item.dex if len(item.slot) < 2 else (item.dex * 2)
            luck += item.luck if len(item.slot) < 2 else (item.luck * 2)
        form_string += _("\n\nTotal stats: ")
        form_string += f"({att} | {cha} | {intel} | {dex} | {luck})"
        return form_string + "\n"

    @commands.command()
    async def unequip(self, ctx: Context, *, item: EquipmentConverter):
        """This stashes a specified equipped item into your backpack.

        Use `[p]unequip name of item` or `[p]unequip slot`
        """
        if self.in_adventure(ctx):
            raise AdventureCheckFailure(
                _("You tried to unequip your items, but the monster ahead of you looks mighty hungry...")
            )

        async with self.get_lock(ctx.author):
            c = await self.get_character_from_json(ctx.author)
            slots = [
                "head",
                "neck",
                "chest",
                "gloves",
                "belt",
                "legs",
                "boots",
                "left",
                "right",
                "ring",
                "charm",
            ]
            msg = ""

            if item in slots:
                current_item = getattr(c, item, None)
                if not current_item:
                    msg = _("{author}, you do not have an item equipped in the {item} slot.").format(
                        author=self.escape(ctx.author.display_name), item=item
                    )
                    return await ctx.send(box(msg, lang="css"))
                await c.unequip_item(current_item)
                msg = _("{author} removed the {current_item} and put it into their backpack.").format(
                    author=self.escape(ctx.author.display_name), current_item=current_item
                )
            else:
                for current_item in c.get_current_equipment():
                    if item.name.lower() in current_item.name.lower():
                        await c.unequip_item(current_item)
                        msg = _("{author} removed the {current_item} and put it into their backpack.").format(
                            author=self.escape(ctx.author.display_name), current_item=current_item
                        )
                        # We break if this works because unequip
                        # will autmatically remove multiple items
                        break
            if msg:
                await ctx.send(box(msg, lang="css"))
                await self.config.user(ctx.author).set(await c.to_json(self.config))
            else:
                await smart_embed(
                    ctx,
                    _("{author}, you do not have an item matching {item} equipped.").format(
                        author=self.escape(ctx.author.display_name), item=item
                    ),
                )

    @commands.command(name="adventurestats")
    @commands.bot_has_permissions(add_reactions=True, embed_links=True)
    @commands.is_owner()
    async def _adventurestats(self, ctx: Context):
        """[Owner] Show all current adventures."""
        msg = "**Active Adventures**\n"
        embed_list = []

        if len(self._sessions) > 0:
            for server_id, adventure in self._sessions.items():
                msg += (
                    f"{self.bot.get_guild(server_id).name} - "
                    f"[{adventure.challenge}]({adventure.message.jump_url})\n"
                )
        else:
            msg += "None."
        for page in pagify(msg, delims=["\n"], page_length=1000):
            embed = discord.Embed(description=page)
            embed_list.append(embed)
        if len(embed_list) > 1:
            await menu(ctx, embed_list, MENU_CONTROLS)
        else:
            await ctx.send(embed=embed_list[0])

    @commands.command(name="devcooldown")
    @commands.bot_has_permissions(add_reactions=True)
    @commands.is_owner()
    async def _devcooldown(self, ctx: Context):
        """[Dev] Resets the after-adventure cooldown in this server."""
        if not await no_dev_prompt(ctx):
            return
        await self.config.channel(ctx.channel).cooldown.set(0)
        await ctx.tick()

    @commands.cooldown(rate=1, per=5, type=commands.BucketType.guild)
    @commands.command(name="adventure", aliases=["a"], cooldown_after_parsing=True)
    @commands.bot_has_permissions(add_reactions=True)
    @commands.guild_only()
    async def _adventure(self, ctx: Context, *, challenge=None):
        """This will send you on an adventure!

        You play by reacting with the offered emojis.
        """

        if ctx.channel.id in self._sessions:
            adventure_obj = self._sessions[ctx.channel.id]
            link = adventure_obj.message.jump_url

            raise AdventureCheckFailure(
                _(
                    f"There's already another adventure going on in this channel.\n"
                    f"Currently fighting: [{adventure_obj.challenge}]({link})"
                ),
                reply=adventure_obj.countdown_message
            )

        if not await has_funds(ctx.author, 250):
            currency_name = await bank.get_currency_name(ctx.guild)
            ctx.command.reset_cooldown(ctx)
            extra = (
                _("\nRun `{ctx.clean_prefix}apayday` to get some gold.").format(ctx=ctx)
                if self._separate_economy
                else ""
            )
            raise AdventureCheckFailure(_("You need {req} {name} to start an adventure.{extra}").format(
                req=250, name=currency_name, extra=extra)
            )
        channel_settings = await self.config.channel(ctx.channel).all()
        cooldown = channel_settings["cooldown"]

        cooldown_time = await self.config.guild(ctx.guild).cooldown_timer_manual()

        if cooldown + cooldown_time > time.time():
            cooldown_time = cooldown + cooldown_time - time.time()
            ctx.command.reset_cooldown(ctx)
            raise AdventureOnCooldown(
                message=_("No heroes are ready to depart in an adventure, try again in {delay}."),
                retry_after=cooldown_time
            )

        if challenge and not (self.is_dev(ctx.author) or await ctx.bot.is_owner(ctx.author)):
            # Only let the bot owner specify a specific challenge
            challenge = None

        adventure_msg = _("You feel adventurous, **{}**?").format(self.escape(ctx.author.display_name))
        try:
            reward, participants = await self._simple(ctx, adventure_msg, challenge)
            await self.config.channel(ctx.channel).cooldown.set(time.time())
        except Exception as exc:
            await self.config.channel(ctx.channel).cooldown.set(0)
            log.exception("Something went wrong controlling the game", exc_info=exc)
            while ctx.channel.id in self._sessions:
                del self._sessions[ctx.channel.id]
            return
        if not reward and not participants:
            await self.config.channel(ctx.channel).cooldown.set(0)
            while ctx.channel.id in self._sessions:
                del self._sessions[ctx.channel.id]
            return

        if participants:
            for user in participants:  # reset activated abilities
                async with self.get_lock(user):
                    c = await self.get_character_from_json(user)
                    if c.heroclass["name"] != "Ranger" and c.heroclass["ability"]:

                        cooldown_time = 0
                        session = self._sessions[ctx.channel.id]
                        if c.heroclass["name"] == "Berserker" and user in session.rage:
                            cooldown_time = max(240, (1140 - ((c.luck + c.total_att) * 2)))
                        elif c.heroclass["name"] == "Tilter" and user in session.rant:
                            cooldown_time = max(240, (1140 - ((c.luck + c.total_cha) * 2)))
                        elif c.heroclass["name"] == "Autoaimer" and user in session.autoaim:
                            cooldown_time = max(240, (1140 - ((c.luck + c.total_int) * 2)))
                        elif c.heroclass["name"] == "Samaritan" and user in session.pray:
                            cooldown_time = 3 * max(240, (1140 - ((c.luck + c.total_int) * 2)))

                        if cooldown_time:
                            c.heroclass["ability"] = False

                        c.heroclass["cooldown"] = time.time() + cooldown_time
                    if c.last_currency_check + 600 < time.time() or c.bal > c.last_known_currency:
                        c.last_known_currency = await bank.get_balance(user)
                        c.last_currency_check = time.time()
                    await self.config.user(user).set(await c.to_json(self.config))

        reward_copy = reward.copy()
        send_message = ""
        for (userid, rewards) in reward_copy.items():
            if rewards:
                user = ctx.guild.get_member(userid)  # bot.get_user breaks sometimes :ablobsweats:
                if user is None:
                    # sorry no rewards if you leave the server
                    continue
                msg = await self._add_rewards(ctx, user, rewards["xp"], rewards["cp"], rewards["special"])
                if msg:
                    send_message += f"{msg}\n"
                self._rewards[userid] = {}
        if send_message:
            for page in pagify(send_message):
                await smart_embed(ctx, page, success=True)

        while ctx.channel.id in self._sessions:
            del self._sessions[ctx.channel.id]

    @_adventure.error
    async def _error_handler(self, ctx: commands.Context, error: Exception) -> None:
        error = getattr(error, "original", error)
        if not isinstance(
            error,
            (commands.CheckFailure, commands.UserInputError, commands.DisabledCommand, commands.CommandOnCooldown),
        ):
            while ctx.channel.id in self._sessions:
                del self._sessions[ctx.channel.id]

    @commands.command()
    @commands.bot_has_permissions(add_reactions=True, embed_links=True)
    @commands.guild_only()
    async def aleaderboard(self, ctx: Context, show_global: bool = False):
        """Print the leaderboard."""
        guild = ctx.guild
        rebirth_sorted = await self.get_leaderboard(guild=guild if not show_global else None)
        if rebirth_sorted:
            await LeaderboardMenu(
                source=LeaderboardSource(entries=rebirth_sorted),
                delete_message_after=True,
                clear_reactions_after=True,
                timeout=60,
                cog=self,
                show_global=show_global,
            ).start(ctx=ctx)
        else:
            raise AdventureCheckFailure(_("There are no adventurers in the server"))

    @commands.command()
    @commands.bot_has_permissions(add_reactions=True, embed_links=True)
    @commands.guild_only()
    async def scoreboard(self, ctx: Context, show_global: bool = False):
        """Print the scoreboard."""

        rebirth_sorted = await self.get_global_scoreboard(guild=ctx.guild if not show_global else None, keyword="wins")
        if rebirth_sorted:
            await ScoreBoardMenu(
                source=ScoreboardSource(entries=rebirth_sorted, stat="wins"),
                delete_message_after=True,
                clear_reactions_after=True,
                timeout=60,
                cog=self,
                show_global=show_global,
            ).start(ctx=ctx)
        else:
            raise AdventureCheckFailure(_("There are no adventurers in the server"))

    @commands.command()
    @commands.bot_has_permissions(add_reactions=True, embed_links=True)
    @commands.guild_only()
    async def wscoreboard(self, ctx: Context, show_global: bool = False):
        """Print the weekly scoreboard."""

        stats = "adventures"
        guild = ctx.guild
        adventures = await self.get_weekly_scoreboard(guild=guild if not show_global else None)
        if adventures:
            await BaseMenu(
                source=WeeklyScoreboardSource(entries=adventures, stat=stats.lower()),
                delete_message_after=True,
                clear_reactions_after=True,
                timeout=60,
            ).start(ctx=ctx)
        else:
            raise AdventureCheckFailure(_("No stats to show for this week."))

    @commands.command(name="apayday", aliases=['apd'], cooldown_after_parsing=True)
    @has_separated_economy()
    @commands.cooldown(rate=1, per=600, type=commands.BucketType.user)
    async def commands_apayday(self, ctx: commands.Context):
        """Get some free gold."""
        author = ctx.author
        adventure_credits_name = await bank.get_currency_name(ctx.guild)

        try:
            amount = 250 * max(await self.config.user(ctx.author).get_raw("rebirths"), 2)
        except KeyError:
            amount = 500 # default

        try:
            await bank.deposit_credits(author, amount)
        except BalanceTooHigh as exc:
            await bank.set_balance(author, exc.max_balance)
            raise AdventureCheckFailure(
                _(
                    "You're struggling to move under the weight of all your {currency}! "
                    "Please spend some more \N{GRIMACING FACE}\n\n"
                    "You currently have {new_balance} {currency}."
                ).format(currency=adventure_credits_name, new_balance=humanize_number(exc.max_balance)),
            )
        else:
            await smart_embed(
                ctx,
                _(
                    "You receive a letter by post from the town's courier! "
                    "{author.mention}, you've gained some interest on your {currency}. "
                    "You've been paid +{amount} {currency}!\n\n"
                    "You currently have {new_balance} {currency}."
                ).format(
                    author=author,
                    currency=adventure_credits_name,
                    amount=humanize_number(amount),  # Make customizable?
                    new_balance=humanize_number(await bank.get_balance(author)),
                ),
                success=True
            )
        character = await self.get_character_from_json(ctx.author)
        if character.last_currency_check + 600 < time.time() or character.bal > character.last_known_currency:
            character.last_known_currency = await bank.get_balance(ctx.author)
            character.last_currency_check = time.time()
            await self.config.user(ctx.author).set(await character.to_json(self.config))

    @commands.group(name="atransfer")
    @has_separated_economy()
    async def commands_atransfer(self, ctx: commands.Context):
        """Transfer currency between players/economies."""

    @commands_atransfer.command(name="player", cooldown_after_parsing=True)
    @commands.guild_only()
    @commands.cooldown(rate=1, per=600, type=commands.BucketType.user)
    async def commands_atransfer_player(self, ctx: commands.Context, amount: int, *, player: discord.User):
        """Transfer gold to another player."""
        if amount <= 0:
            raise AdventureCheckFailure(
                _("{author.mention} You can't transfer 0 or negative values.").format(author=ctx.author),
            )
            ctx.command.reset_cooldown(ctx)
            return
        currency = await bank.get_currency_name(ctx.guild)
        if not await bank.can_spend(member=ctx.author, amount=amount):
            raise AdventureCheckFailure(
                _("{author.mention} you don't have enough {name}.").format(
                    author=ctx.author, name=await bank.get_currency_name(ctx.guild))
            )

        c = await self.get_character_from_json(ctx.author)
        if c.lvl == c.maxlevel:
            raise AdventureCheckFailure(
                _("{author.mention} you can't transfer money when you're at the max level.").format(
                    author=ctx.author, name=await bank.get_currency_name(ctx.guild)
                )
            )
        
        tax = await self.config.tax_brackets.all()
        highest = 0
        for tax, percent in tax.items():
            tax = int(tax)
            if tax >= amount:
                break
            highest = percent

        try:
            transfered = await bank.transfer_credits(
                from_=ctx.author, to=player, amount=amount, tax=highest
            )  # Customizable Tax
        except (ValueError, BalanceTooHigh) as e:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(str(e))

        await ctx.send(
            _(
                "{user} transferred {num} {currency} to {other_user} (You have been taxed {tax:.2%}, total transfered: {transfered})"
            ).format(
                user=ctx.author.display_name,
                num=humanize_number(amount),
                currency=currency,
                other_user=player.display_name,
                tax=highest,
                transfered=humanize_number(transfered),
            ),
        )

    @commands_atransfer.command(name="give")
    @commands.is_owner()
    async def commands_atransfer_give(self, ctx: commands.Context, amount: int, *players: discord.User):
        """[Owner] Give gold to adventurers."""
        if amount <= 0:
            raise AdventureCheckFailure(
                _("{author.mention} You can't give 0 or negative values.").format(author=ctx.author),
            )
            return
        players_string = ""
        for player in players:
            try:
                await bank.deposit_credits(member=player, amount=amount)
                players_string += f"{player.display_name}\n"
            except BalanceTooHigh as exc:
                await bank.set_balance(member=player, amount=exc.max_balance)
                players_string += f"{player.display_name}\n"

        await smart_embed(
            ctx,
            _("{author.mention} I've given {amount} {name} to the following adventurers:\n\n{players}").format(
                author=ctx.author,
                amount=humanize_number(amount),
                players=players_string,
                name=await bank.get_currency_name(ctx.guild),
            ),
            success=True
        )

    @commands.command(name="balance", aliases=["bal", "credits"])
    async def _balance(self, ctx: commands.Context, *, member: Member = None):
        """Show your or a specific member's balance."""

        member = member or ctx.author
        bal = await bank.get_balance(member)

        await smart_embed(
            ctx,
            _("{member.mention} currently has {new_balance} {currency}.").format(
                member=member,
                new_balance=humanize_number(bal),
                currency=await bank.get_currency_name(ctx.guild),
            ),
        )

    @commands.group()
    @commands.is_owner()
    async def aperms(self, ctx: commands.Context):
        """Configures permissions within Adventure"""

    @aperms.command(name="import")
    @commands.is_owner()
    async def _import(self, ctx: commands.Context):
        """Imports permissions into Adventure"""
        file = ctx.message.attachments[0]
        if file.filename.endswith(".json"):
            data = (await file.read()).decode()
            try:
                self.PERMS = json.loads(data)
                data = json.dumps(self.PERMS)  # minify data
            except json.JSONDecodeError:
                await smart_embed(ctx, _("Invalid JSON format, send a json file."), success=False)
            else:
                with open(cog_data_path(self) / "perms.json", "w+") as f:
                    f.write(data)

                await smart_embed(ctx, _("File Saved"), success=True)
        else:
            await smart_embed(ctx, _("Invalid file format, send a json file."), success=False)

    @aperms.command(name="export")
    @commands.is_owner()
    async def _export(self, ctx: commands.Context):
        """Exports permissions from Adventure"""
        with io.StringIO(json.dumps(self.PERMS, indent=4)) as stream:
            await ctx.author.send(file=discord.File(stream, filename='perms.json'))
        await smart_embed(ctx, _("Sent to your DM"), success=True)

    @commands.command()
    async def compare(
        self,
        ctx: commands.Context,
        *,
        item: ItemConverter,
    ):
        """Compares given item with equipped item of same slot."""

        character = await self.get_character_from_json(ctx.author)
        # other = getattr(character, item.slot[0], None)

        others = [getattr(character, i, None) for i in item.slot if getattr(character, i, None) is not None]

        if not others:
            slot_str = " and/or ".join(item.slot)
            if len(item.slot) == 1:
                item_str = "item"
            else:
                item_str = "items"

            msg = await ctx.send(
                box(
                    _("{item}\n\nYou don't have any [{slot}] {item_str} equipped. Equip this?".format(
                        item=self.display_item(item, character), slot=slot_str, item_str=item_str
                    )),
                    lang="css"
                )
            )
        elif item.name in [i.name for i in others]:
            # This is actually reachable in case of multiple items with same name.
            return await ctx.send(box(self.display_item(item, character, True), lang="css"))
        else:
            sep = f"\n\n#{'-' * 40}\n\n"

            if len(others) > 1:
                extra_items = ""
                for i in others[1:]:
                    extra_items += f"\n\n{self.display_item(i, character, True)}"
            else:
                extra_items = ""

            msg = await ctx.send(
                box(
                    _(
                        "{item_one}{sep}{item_two}{extra}{sep}Do you want to equip {item_one_name}?"
                    ).format(
                        item_one=self.display_item(item, character),
                        item_two=self.display_item(others[0], character, True),
                        extra=extra_items,
                        sep=sep,
                        item_one_name=str(item)
                    ),
                    lang="css"
                )
            )

        start_adding_reactions(msg, ReactionPredicate.YES_OR_NO_EMOJIS)
        pred = ReactionPredicate.yes_or_no(msg, ctx.author)

        try:
            await self.bot.wait_for("reaction_add", check=pred, timeout=60)
        except asyncio.TimeoutError:
            await self._clear_react(msg)
            return

        if pred.result:
            if self.in_adventure(ctx):
                raise AdventureCheckFailure(
                    _("You tried to equip your item but the monster ahead nearly decapitated you.")
                )

            equiplevel = equip_level(character, item)
            if self.is_dev(ctx.author):
                equiplevel = 0
            if not can_equip(character, item):
                await self._clear_react(msg)
                return await smart_embed(
                    ctx,
                    f"**{self.escape(ctx.author.display_name)}**, you need to be level "
                    f"`{equiplevel}` to equip this item.",
                    success=True
                )
            if not others:
                equip_msg = box(
                    _("{user} equipped {item} ({slot} slot).").format(
                        user=self.escape(ctx.author.display_name), item=item, slot=item.slot[0]
                    ),
                    lang="css",
                )
            else:
                equip_msg = box(
                    _("{user} equipped {item} ({slot} slot) and put {old_items} into their backpack.").format(
                        user=self.escape(ctx.author.display_name),
                        item=item,
                        slot=item.slot[0],
                        old_items=" and ".join(str(i) for i in others),
                    ),
                    lang="css",
                )
            await msg.edit(content=equip_msg)
            character = await character.equip_item(item, False, self.is_dev(ctx.author))
            await self.config.user(ctx.author).set(await character.to_json(self.config))
        await self._clear_react(msg)
