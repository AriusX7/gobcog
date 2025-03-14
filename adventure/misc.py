import asyncio
import contextlib
import copy
import json
import logging
import os
import pickle
import random
import re
import time
import traceback
from datetime import date, datetime, timedelta
from typing import List, MutableMapping, Union

import discord
from cryptography.fernet import Fernet
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.commands import Context
from redbot.core.data_manager import bundled_data_path, cog_data_path
from redbot.core.errors import BalanceTooHigh
from redbot.core.i18n import Translator
from redbot.core.utils import AsyncIter
from redbot.core.utils.chat_formatting import box, escape, humanize_list, humanize_number, pagify
from redbot.core.utils.common_filters import filter_various_mentions
from redbot.core.utils.menus import menu
from redbot.core.utils.predicates import MessagePredicate, ReactionPredicate

import adventure.charsheet
from . import bank
from .charsheet import ORDER, RARITIES, Character, GameSession, Item, calculate_sp, can_equip, equip_level, has_funds
from .utils import AdventureCheckFailure, AdventureOnCooldown, smart_embed, start_adding_reactions, MENU_CONTROLS

DEV_LIST = [208903205982044161, 154497072148643840, 218773382617890828]
REBIRTH_LVL = 20
REBIRTH_STEP = 10
_SCHEMA_VERSION = 4

_ = Translator("Adventure", __file__)
_config: Config = None

log = logging.getLogger("red.cogs.adventure")

class MiscMixin(commands.Cog):
    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self._key = None

        self.config: Config

        self.maintenance: bool

    @staticmethod
    def is_dev(user: Union[discord.User, discord.Member]):
        return user.id in DEV_LIST

    def parse_file(self, fp):
        if str(fp).endswith('.enc'):
            with open(fp, 'rb') as f:
                data = f.read()
                if self._key is not None:
                    data = self._key.decrypt(data)
                return json.loads(data)
        else:
            with open(fp, encoding='utf8') as f:
                return json.load(f)
                

    async def initialize(self):
        """This will load all the bundled data into respective variables."""
        await self.bot.wait_until_red_ready()
        try:
            global _config
            _config = self.config
            theme = await self.config.theme()
            self._separate_economy = await self.config.separate_economy()

            key_fp = bundled_data_path(self) / theme / "key.key"
            if key_fp.exists():
                with open(key_fp, 'rb') as f:
                    self._key = Fernet(f.read())

            as_monster_fp = bundled_data_path(self) / theme / "as_monsters.json"
            attribs_fp = bundled_data_path(self) / theme / "attribs.json"
            locations_fp = bundled_data_path(self) / theme / "locations.json"
            monster_fp = bundled_data_path(self) / theme / "monsters.json"
            pets_fp = bundled_data_path(self) / theme / "pets.json"
            raisins_fp = bundled_data_path(self) / theme / "raisins.json"
            threatee_fp = bundled_data_path(self) / theme / "threatee.json"
            tr_set_fp = bundled_data_path(self) / theme / "tr_set.json"
            prefixes_fp = bundled_data_path(self) / theme / "prefixes.json"
            materials_fp = bundled_data_path(self) / theme / "materials.json"
            equipment_fp = bundled_data_path(self) / theme / "equipment.json"
            suffixes_fp = bundled_data_path(self) / theme / "suffixes.json"
            set_bonuses = bundled_data_path(self) / theme / "set_bonuses.json"
            files = {
                "pets": pets_fp,
                "attr": attribs_fp,
                "monster": monster_fp,
                "location": locations_fp,
                "raisins": raisins_fp,
                "threatee": threatee_fp,
                "set": tr_set_fp,
                "as_monsters": as_monster_fp,
                "prefixes": prefixes_fp,
                "materials": materials_fp,
                "equipment": equipment_fp,
                "suffixes": suffixes_fp,
                "set_bonuses": set_bonuses,
            }
            for (name, file) in files.items():
                if not file.exists():
                    # check if its encrypted instead
                    if self._key:
                        files[name] = bundled_data_path(self) / theme / f"{file.name[:-5]}.enc"
                if not files[name].exists():
                    files[name] = bundled_data_path(self) / "default" / file.name

            self.PETS = self.parse_file(files["pets"])
            self.ATTRIBS = self.parse_file(files["attr"])
            self.MONSTERS = self.parse_file(files["monster"])
            self.AS_MONSTERS = self.parse_file(files["as_monsters"])
            self.LOCATIONS = self.parse_file(files["location"])
            self.RAISINS = self.parse_file(files["raisins"])
            self.THREATEE = self.parse_file(files["threatee"])
            self.TR_GEAR_SET = self.parse_file(files["set"])
            self.PREFIXES = self.parse_file(files["prefixes"])
            self.MATERIALS = self.parse_file(files["materials"])
            self.EQUIPMENT = self.parse_file(files["equipment"])
            self.SUFFIXES = self.parse_file(files["suffixes"])
            self.SET_BONUSES = self.parse_file(files["set_bonuses"])

            try:
                with open(cog_data_path(self) / "perms.json") as f:
                    self.PERMS = json.load(f)
            except FileNotFoundError:
                self.PERMS = {}

            if not all(
                i
                for i in [
                    len(self.PETS) > 0,
                    len(self.ATTRIBS) > 0,
                    len(self.MONSTERS) > 0,
                    len(self.LOCATIONS) > 0,
                    len(self.RAISINS) > 0,
                    len(self.THREATEE) > 0,
                    len(self.TR_GEAR_SET) > 0,
                    len(self.PREFIXES) > 0,
                    len(self.MATERIALS) > 0,
                    len(self.EQUIPMENT) > 0,
                    len(self.SUFFIXES) > 0,
                    len(self.SET_BONUSES) > 0,
                ]
            ):
                log.critical(f"{theme} theme is invalid, resetting it to the default theme.")
                await self.config.theme.set("default")
                await self.initialize()
                return
            adventure.charsheet.TR_GEAR_SET = self.TR_GEAR_SET
            adventure.charsheet.PETS = self.PETS
            adventure.charsheet.REBIRTH_LVL = REBIRTH_LVL
            adventure.charsheet.REBIRTH_STEP = REBIRTH_STEP
            adventure.charsheet.SET_BONUSES = self.SET_BONUSES
            await self._migrate_config(from_version=await self.config.schema_version(), to_version=_SCHEMA_VERSION)
            self._daily_bonus = await self.config.daily_bonus.all()

            await self.bot.wait_until_ready()
            
            results_path = cog_data_path(self) / "results.pickle"
            if os.path.isfile(results_path):
                with open(results_path, "rb") as f:
                    try:
                        self._adv_results = pickle.load(f)
                    except EOFError:
                        pass

            session_path = cog_data_path(self) / "sessions.pickle"
            if os.path.isfile(session_path):
                with open(session_path, "rb") as f:
                    try:
                        self._sessions = pickle.load(f)
                    except EOFError:
                        self._sessions = {}
            else:
                self._sessions = {}

            to_delete = []
            for k, v in self._sessions.items():
                try:
                    await v.load_from_pickle(self.bot)
                except discord.NotFound:
                    to_delete.append(k)
                    continue

                async def refresh_timer():
                    # emulate everything after message is sent incl countdowns
                    if not isinstance(v.message, discord.Message):
                        # something went wrong in parsing the pickle
                        return

                    ctx = await self.bot.get_context(v.message)
                    timer = await self._adv_countdown(ctx, v.timer, "Time remaining")

                    self.tasks[v.message_id] = timer
                    try:
                        await asyncio.wait_for(timer, timeout=v.timeout + 5)
                    except Exception as exc:
                        timer.cancel()
                        log.exception("Error with the countdown timer", exc_info=exc)

                    try:
                        await self._result(ctx, v.message)
                        if ctx.channel.id not in self._sessions:
                            reward = None
                            participants = None
                        else:
                            reward = self._rewards
                            participants = self._sessions[ctx.channel.id].participants
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
                    if participants:
                        for user in participants:  # reset activated abilities
                            async with self.get_lock(user):
                                c = await self.get_character_from_json(user)
                                if c.heroclass["name"] != "Ranger" and c.heroclass["ability"]:
                                    c.heroclass["ability"] = False
                                if c.last_currency_check + 600 < time.time() or c.bal > c.last_known_currency:
                                    c.last_known_currency = await bank.get_balance(user)
                                    c.last_currency_check = time.time()
                                await self.config.user(user).set(await c.to_json(self.config))

                    while ctx.channel.id in self._sessions:
                        del self._sessions[ctx.channel.id]

                task = self.bot.loop.create_task(refresh_timer())
                self.tasks[v.countdown_message.id] = task

            for k in to_delete:
                del self._sessions[k]
        except Exception as err:
            log.exception("There was an error starting up the cog", exc_info=err)
        else:
            self._ready_event.set()
            self.gb_task = self.bot.loop.create_task(self._garbage_collection())

    async def get_character_from_json(self, user, *, release_lock=False):
        try:
            return await Character.from_json(self.config, user, self._daily_bonus)
        except Exception as exc:
            log.exception("Error with the new character sheet", exc_info=exc)
        finally:
            if release_lock:
                lock = self.get_lock(user)
                with contextlib.suppress(Exception):
                    lock.release()

    async def cleanup_tasks(self):
        await self._ready_event.wait()
        while self is self.bot.get_cog("Adventure"):
            to_delete = []
            for (msg_id, task) in self.tasks.items():
                if task.done():
                    to_delete.append(msg_id)
            for task in to_delete:
                del self.tasks[task]
            await asyncio.sleep(300)

    async def _migrate_config(self, from_version: int, to_version: int) -> None:
        log.debug(f"from_version: {from_version} to_version:{to_version}")
        if from_version == to_version:
            return
        if from_version < 2 <= to_version:
            group = self.config._get_base_group(self.config.USER)
            accounts = await group.all()
            tmp = accounts.copy()
            async with group.all() as adventurers_data:
                for user in tmp:
                    new_backpack = {}
                    new_loadout = {}
                    user_equipped_items = adventurers_data[user]["items"]
                    for slot in user_equipped_items.keys():
                        if user_equipped_items[slot]:
                            for (slot_item_name, slot_item) in list(user_equipped_items[slot].items())[:1]:
                                new_name, slot_item = self._convert_item_migration(slot_item_name, slot_item)
                                adventurers_data[user]["items"][slot] = {new_name: slot_item}
                    if "backpack" not in adventurers_data[user]:
                        adventurers_data[user]["backpack"] = {}
                    for (backpack_item_name, backpack_item) in adventurers_data[user]["backpack"].items():
                        new_name, backpack_item = self._convert_item_migration(backpack_item_name, backpack_item)
                        new_backpack[new_name] = backpack_item
                    adventurers_data[user]["backpack"] = new_backpack
                    if "loadouts" not in adventurers_data[user]:
                        adventurers_data[user]["loadouts"] = {}
                    try:
                        for (loadout_name, loadout) in adventurers_data[user]["loadouts"].items():
                            for (slot, equipped_loadout) in loadout.items():
                                new_loadout[slot] = {}
                                for (loadout_item_name, loadout_item) in equipped_loadout.items():

                                    new_name, loadout_item = self._convert_item_migration(
                                        loadout_item_name, loadout_item
                                    )
                                    new_loadout[slot][new_name] = loadout_item
                        adventurers_data[user]["loadouts"] = new_loadout
                    except Exception:
                        adventurers_data[user]["loadouts"] = {}
            await self.config.schema_version.set(2)
            from_version = 2
        if from_version < 3 <= to_version:
            group = self.config._get_base_group(self.config.USER)
            accounts = await group.all()
            tmp = accounts.copy()
            async with group.all() as adventurers_data:
                for user in tmp:
                    new_loadout = {}
                    if "loadouts" not in adventurers_data[user]:
                        adventurers_data[user]["loadouts"] = {}
                    try:
                        for (loadout_name, loadout) in adventurers_data[user]["loadouts"].items():
                            if loadout_name in {
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
                            }:
                                continue
                            new_loadout[loadout_name] = loadout
                        adventurers_data[user]["loadouts"] = new_loadout
                    except Exception:
                        adventurers_data[user]["loadouts"] = {}
            await self.config.schema_version.set(3)

        if from_version < 4 <= to_version:
            group = self.config._get_base_group(self.config.USER)
            accounts = await group.all()
            tmp = accounts.copy()
            async with group.all() as adventurers_data:
                async for user in AsyncIter(tmp, steps=10):
                    if "items" in tmp[user]:
                        equipped = tmp[user]["items"]
                        for slot, item in equipped.items():
                            for item_name, item_data in item.items():
                                if "King Solomos" in item_name:
                                    del adventurers_data[user]["items"][slot][item_name]
                                    item_name = item_name.replace("Solomos", "Solomons")
                                    adventurers_data[user]["items"][slot][item_name] = item_data
                    if "loadouts" in tmp[user]:
                        loadout = tmp[user]["loadouts"]
                        for loadout_name, loadout_data in loadout.items():
                            for slot, item in equipped.items():
                                for item_name, item_data in item.items():
                                    if "King Solomos" in item_name:
                                        del adventurers_data[user]["loadouts"][loadout_name][slot][item_name]
                                        item_name = item_name.replace("Solomos", "Solomons")
                                        adventurers_data[user]["loadouts"][loadout_name][slot][item_name] = item_data
                    if "backpack" in tmp[user]:
                        backpack = tmp[user]["backpack"]
                        async for item_name, item_data in AsyncIter(backpack.items(), steps=25):
                            if "King Solomos" in item_name:
                                del adventurers_data[user]["backpack"][item_name]
                                item_name = item_name.replace("Solomos", "Solomons")
                                adventurers_data[user]["backpack"][item_name] = item_data
            await self.config.schema_version.set(4)

    def _convert_item_migration(self, item_name, item_dict):
        new_name = item_name
        if "name" in item_dict:
            del item_dict["name"]
        if "rarity" not in item_dict:
            item_dict["rarity"] = "common"
        if item_dict["rarity"] == "legendary":
            new_name = item_name.replace("{Legendary:'", "").replace("legendary:'", "").replace("'}", "")
        if item_dict["rarity"] == "epic":
            new_name = item_name.replace("[", "").replace("]", "")
        if item_dict["rarity"] == "rare":
            new_name = item_name.replace("_", " ").replace(".", "")
        if item_dict["rarity"] == "set":
            new_name = (
                item_name.replace("{Gear_Set:'", "")
                .replace("{gear_set:'", "")
                .replace("{Gear Set:'", "")
                .replace("'}", "")
            )
        if item_dict["rarity"] != "set":
            if "bonus" in item_dict:
                del item_dict["bonus"]
            if "parts" in item_dict:
                del item_dict["parts"]
            if "set" in item_dict:
                del item_dict["set"]
        return (new_name, item_dict)

    def in_adventure(self, ctx=None, user=None, *, channel=False):
        """channel argument ensures that user is in the guild of trigger"""
        author = user or ctx.author
        
        if channel:
            channel_id = getattr(channel, 'id', None)
            try:
                sessions = {channel_id: self._sessions[channel_id]}
            except KeyError:
                return False
        else:
            sessions = self._sessions

        if not sessions:
            return False

        participants_ids = set(
            [
                p.id
                for _channel_id, session in sessions.items()
                for p in session.reactors
            ]
        )
        return bool(author.id in participants_ids)

    async def allow_in_dm(self, ctx):
        """Checks if the bank is global and allows the command in dm."""
        if ctx.guild is not None:
            return True
        return bool(ctx.guild is None and await bank.is_global())

    def get_lock(self, member: discord.User):
        if member.id not in self.locks:
            self.locks[member.id] = asyncio.Lock()
        return self.locks[member.id]

    async def get_challenge(self, ctx: Context, monsters):
        c = await self.get_character_from_json(ctx.author)
        possible_monsters = []
        stat_range = self._adv_results.get_stat_range(ctx)
        can_spawn_boss = self._adv_results.can_spawn_boss(ctx)
        async for (e, (m, stats)) in AsyncIter(monsters.items()).enumerate(start=1):
            appropriate_range = max(stats["hp"], stats["dipl"]) <= (max(c.att, c.int, c.cha) * 5)
            if stat_range["max_stat"] > 0:
                main_stat = stats["hp"] if (stat_range["stat_type"] == "hp") else stats["dipl"]
                appropriate_range = (stat_range["min_stat"] * 0.75) <= main_stat <= (stat_range["max_stat"] * 1.2)
            if not appropriate_range:
                continue
            if stats["boss"] and not can_spawn_boss:
                continue
            if not stats["boss"] and not stats["miniboss"]:
                count = 0
                break_at = random.randint(1, 15)
                while count < break_at:
                    count += 1
                    possible_monsters.append(m)
                    if count == break_at:
                        break
            else:
                possible_monsters.append(m)

        if len(possible_monsters) == 0:
            choice = random.choice(list(monsters.keys()) * 3)
        else:
            choice = random.choice(possible_monsters)
        return choice

    def _dynamic_monster_stats(self, ctx: Context, choice: MutableMapping):
        stat_range = self._adv_results.get_stat_range(ctx)
        win_percentage = stat_range.get("win_percent", 0.5)
        if win_percentage >= 0.90:
            monster_hp_min = int(choice["hp"] * 2)
            monster_hp_max = int(choice["hp"] * 3)
            monster_diplo_min = int(choice["dipl"] * 2)
            monster_diplo_max = int(choice["dipl"] * 3)
            percent_pdef = random.randrange(25, 30) / 100
            monster_pdef = choice["pdef"] * percent_pdef
            percent_mdef = random.randrange(25, 30) / 100
            monster_mdef = choice["mdef"] * percent_mdef
        elif win_percentage >= 0.75:
            monster_hp_min = int(choice["hp"] * 1.5)
            monster_hp_max = int(choice["hp"] * 2)
            monster_diplo_min = int(choice["dipl"] * 1.5)
            monster_diplo_max = int(choice["dipl"] * 2)
            percent_pdef = random.randrange(15, 25) / 100
            monster_pdef = choice["pdef"] * percent_pdef
            percent_mdef = random.randrange(15, 25) / 100
            monster_mdef = choice["mdef"] * percent_mdef
        elif win_percentage >= 0.50:
            monster_hp_min = int(choice["hp"])
            monster_hp_max = int(choice["hp"] * 1.5)
            monster_diplo_min = int(choice["dipl"])
            monster_diplo_max = int(choice["dipl"] * 1.5)
            percent_pdef = random.randrange(1, 15) / 100
            monster_pdef = choice["pdef"] * percent_pdef
            percent_mdef = random.randrange(1, 15) / 100
            monster_mdef = choice["mdef"] * percent_mdef
        elif win_percentage >= 0.35:
            monster_hp_min = int(choice["hp"] * 0.9)
            monster_hp_max = int(choice["hp"])
            monster_diplo_min = int(choice["dipl"] * 0.9)
            monster_diplo_max = int(choice["dipl"])
            percent_pdef = random.randrange(1, 15) / 100
            monster_pdef = choice["pdef"] * percent_pdef * -1
            percent_mdef = random.randrange(1, 15) / 100
            monster_mdef = choice["mdef"] * percent_mdef * -1
        elif win_percentage >= 0.15:
            monster_hp_min = int(choice["hp"] * 0.8)
            monster_hp_max = int(choice["hp"] * 0.9)
            monster_diplo_min = int(choice["dipl"] * 0.8)
            monster_diplo_max = int(choice["dipl"] * 0.9)
            percent_pdef = random.randrange(15, 25) / 100
            monster_pdef = choice["pdef"] * percent_pdef * -1
            percent_mdef = random.randrange(15, 25) / 100
            monster_mdef = choice["mdef"] * percent_mdef * -1
        else:
            monster_hp_min = int(choice["hp"] * 0.6)
            monster_hp_max = int(choice["hp"] * 0.8)
            monster_diplo_min = int(choice["dipl"] * 0.6)
            monster_diplo_max = int(choice["dipl"] * 0.8)
            percent_pdef = random.randrange(25, 30) / 100
            monster_pdef = choice["pdef"] * percent_pdef * -1
            percent_mdef = random.randrange(25, 30) / 100
            monster_mdef = choice["mdef"] * percent_mdef * -1

        if monster_hp_min < monster_hp_max:
            new_hp = random.randrange(monster_hp_min, monster_hp_max)
        elif monster_hp_max < monster_hp_min:
            new_hp = random.randrange(monster_hp_max, monster_hp_min)
        else:
            new_hp = max(monster_hp_max, monster_hp_min)
        if monster_diplo_min < monster_diplo_max:
            new_diplo = random.randrange(monster_diplo_min, monster_diplo_max)
        elif monster_diplo_max < monster_diplo_min:
            new_diplo = random.randrange(monster_diplo_max, monster_diplo_min)
        else:
            new_diplo = max(monster_diplo_max, monster_diplo_min)
        new_pdef = choice["pdef"] + monster_pdef
        new_mdef = choice["mdef"] + monster_mdef
        choice["hp"] = max(new_hp, 1)
        choice["dipl"] = max(new_diplo, 1)
        choice["pdef"] = new_pdef
        choice["mdef"] = new_mdef
        return choice

    async def update_monster_roster(self, ctx: Context, user: discord.Member):

        try:
            c = await Character.from_json(self.config, user, self._daily_bonus)
        except Exception as exc:
            log.exception("Error with the new character sheet", exc_info=exc)
            return ({**self.MONSTERS, **self.AS_MONSTERS}, 1)

        transcended_chance = random.randint(0, 10)
        theme = await self.config.theme()
        extra_monsters = await self.config.themes.all()
        extra_monsters = extra_monsters.get(theme, {}).get("monsters", {})
        monster_stats = 1
        monsters = {**self.MONSTERS, **self.AS_MONSTERS, **extra_monsters}
        transcended = False
        if transcended_chance == 5 and self._adv_results.can_spawn_boss(ctx):
            monster_stats = 2 + max((c.rebirths // 10) - 1, 0)
            transcended = True
        elif c.rebirths >= 10:
            monster_stats = 1 + max((c.rebirths // 10) - 1, 0) / 2
        return monsters, monster_stats, transcended

    async def _simple(self, ctx: Context, adventure_msg, challenge: str = None, attribute: str = None):
        self.bot.dispatch("adventure", ctx)
        text = ""
        monster_roster, monster_stats, transcended = await self.update_monster_roster(ctx, ctx.author)
        if challenge and challenge not in monster_roster:
            for m in monster_roster:
                if challenge.lower() == m.lower():
                    challenge = m

        if not challenge or challenge not in monster_roster:
            challenge = await self.get_challenge(ctx, monster_roster)

        if attribute and attribute.lower() in self.ATTRIBS:
            attribute = attribute.lower()
        elif "Clone of Zrib" in challenge:
            attribute = "god-wise"
        else:
            attribute = random.choice(list(self.ATTRIBS.keys()))

        if transcended and "Ascended" in challenge:
            new_challenge = challenge.replace("Ascended", "Transcended")
            if "Transcended" in new_challenge:
                self.bot.dispatch("adventure_transcended", ctx)
        else:
            transcended = False
            new_challenge = challenge

        if "Ascended" in new_challenge:
            self.bot.dispatch("adventure_ascended", ctx)
        if monster_roster[challenge]["boss"]:
            timer = 60 * 5
            text = box(_("\n [{} Alarm!]").format(new_challenge), lang="css")
            self.bot.dispatch("adventure_boss", ctx)  # dispatches an event on bosses
        elif monster_roster[challenge]["miniboss"]:
            timer = 60 * 3
            self.bot.dispatch("adventure_miniboss", ctx)
        else:
            timer = 60 * 2

        if transcended and not monster_roster[challenge]["boss"] and not monster_roster[challenge]["miniboss"]:
            timer = 60 * 3

        self._sessions[ctx.channel.id] = GameSession(
            challenge=new_challenge,
            attribute=attribute,
            channel=ctx.channel,
            boss=monster_roster[challenge]["boss"],
            miniboss=monster_roster[challenge]["miniboss"],
            timer=timer,
            monster=monster_roster[challenge],
            monsters=monster_roster,
            monster_stats=monster_stats,
            message=ctx.message,
            transcended=transcended,
            monster_modified_stats=self._dynamic_monster_stats(ctx, monster_roster[challenge]),
        )
        adventure_msg = (
            f"{adventure_msg}{text}\n{random.choice(self.LOCATIONS)}\n"
            f"**{self.escape(ctx.author.display_name)}**{random.choice(self.RAISINS)}"
        )
        await self._choice(ctx, adventure_msg)
        if ctx.channel.id not in self._sessions:
            return (None, None)
        rewards = self._rewards
        participants = self._sessions[ctx.channel.id].participants
        return (rewards, participants)

    async def _choice(self, ctx: Context, adventure_msg):
        session = self._sessions[ctx.channel.id]
        dragon_text = _(
            "but **{attr} {chall}** "
            "just landed in front of you glaring! \n\n"
            "What will you do and will other heroes be brave enough to help you?\n"
            "Heroes have 5 minutes to participate via reaction:"
            "\n\nReact with: {reactions}"
        ).format(
            attr=session.fmt_attribute,
            chall=session.challenge,
            reactions="**"
            + _("Rage")
            + "** - **"
            + _("Autoaim")
            + "** - **"
            + _("Rant")
            + "** - **"
            + _("Pray")
            + "**",
        )
        basilisk_text = _(
            "but **{attr} {chall}** stepped out looking around. \n\n"
            "What will you do and will other heroes help your cause?\n"
            "Heroes have 3 minutes to participate via reaction:"
            "\n\nReact with: {reactions}"
        ).format(
            attr=session.fmt_attribute,
            chall=session.challenge,
            reactions="**"
            + _("Rage")
            + "** - **"
            + _("Autoaim")
            + "** - **"
            + _("Rant")
            + "** - **"
            + _("Pray")
            + "**",
        )
        normal_text = _(
            "but **{attr} {chall}** "
            "is guarding it with{threat}. \n\n"
            "What will you do and will other heroes help your cause?\n"
            "Heroes have {duration} minutes to participate via reaction:"
            "\n\nReact with: {reactions}"
        ).format(
            attr=session.fmt_attribute,
            chall=session.challenge,
            threat=random.choice(self.THREATEE),
            duration=3 if session.transcended else 2,
            reactions="**"
            + _("Rage")
            + "** - **"
            + _("Autoaim")
            + "** - **"
            + _("Rant")
            + "** - **"
            + _("Pray")
            + "**",
        )

        embed = discord.Embed(colour=discord.Colour.blurple())
        use_embeds = await self.config.guild(ctx.guild).embed() and ctx.channel.permissions_for(ctx.me).embed_links
        if session.boss:
            if use_embeds:
                embed.description = f"{adventure_msg}\n{dragon_text}"
                embed.colour = discord.Colour.dark_red()
                if session.monster["image"]:
                    embed.set_image(url=session.monster["image"])
                adventure_msg = await ctx.send(embed=embed)
            else:
                adventure_msg = await ctx.send(f"{adventure_msg}\n{dragon_text}")
            session.timeout = 60 * 5

        elif session.miniboss:
            if use_embeds:
                embed.description = f"{adventure_msg}\n{basilisk_text}"
                embed.colour = discord.Colour.dark_green()
                if session.monster["image"]:
                    embed.set_image(url=session.monster["image"])
                adventure_msg = await ctx.send(embed=embed)
            else:
                adventure_msg = await ctx.send(f"{adventure_msg}\n{basilisk_text}")
            session.timeout = 60 * 3
        else:
            if use_embeds:
                embed.description = f"{adventure_msg}\n{normal_text}"
                if session.monster["image"]:
                    embed.set_thumbnail(url=session.monster["image"])
                adventure_msg = await ctx.send(embed=embed)
            else:
                adventure_msg = await ctx.send(f"{adventure_msg}\n{normal_text}")

            if session.transcended:
                session.timeout = 60 * 3
            else:
                session.timeout = 60 * 2

        session.message_id = adventure_msg.id
        session.message = adventure_msg
        
        start_adding_reactions(adventure_msg, self._adventure_actions)

        timer = await self._adv_countdown(ctx, session.timer, "Time remaining")
        self.tasks[adventure_msg.id] = timer
        try:
            await asyncio.wait_for(timer, timeout=session.timeout + 5)
        except Exception as exc:
            timer.cancel()
            log.exception("Error with the countdown timer", exc_info=exc)

        return await self._result(ctx, adventure_msg)

    async def local_perms(self, user):
        """Check the user is/isn't locally whitelisted/blacklisted.

        https://github.com/Cog-Creators/Red-DiscordBot/blob/V3/release/3.0.0/redbot/core/global_checks.py
        """
        if await self.bot.is_owner(user):
            return True
        guild_settings = self.bot.db.guild(user.guild)
        local_blacklist = await guild_settings.blacklist()
        local_whitelist = await guild_settings.whitelist()

        _ids = [r.id for r in user.roles if not r.is_default()]
        _ids.append(user.id)
        if local_whitelist:
            return any(i in local_whitelist for i in _ids)

        return not any(i in local_blacklist for i in _ids)

    async def global_perms(self, user):
        """Check the user is/isn't globally whitelisted/blacklisted.

        https://github.com/Cog-Creators/Red-DiscordBot/blob/V3/release/3.0.0/redbot/core/global_checks.py
        """
        if await self.bot.is_owner(user):
            return True
        whitelist = await self.bot.db.whitelist()
        if whitelist:
            return user.id in whitelist

        return user.id not in await self.bot.db.blacklist()

    async def has_perm(self, user):
        if hasattr(self.bot, "allowed_by_whitelist_blacklist"):
            return await self.bot.allowed_by_whitelist_blacklist(user)
        else:
            return await self.local_perms(user) or await self.global_perms(user)

    async def _handle_adventure(self, reaction, user):
        channel = reaction.message.channel
        action = {str(v): k for k, v in self._adventure_controls.items()}[str(reaction.emoji)]
        session = self._sessions[channel.id]
        has_fund = await has_funds(user, 250)
        for x in ["rage", "autoaim", "rant", "pray", "run"]:
            if not has_fund or user in getattr(session, x, []):
                with contextlib.suppress(discord.HTTPException):
                    symbol = self._adventure_controls[x]
                    await reaction.message.remove_reaction(symbol, user)

        restricted = await self.config.restrict()
        if user not in getattr(session, action, []):
            if has_fund:
                if restricted:
                    all_users = []
                    for (channel_id, channel_session) in self._sessions.items():
                        channel_users_in_game = (
                            channel_session.rage
                            | channel_session.autoaim
                            | channel_session.rant
                            | channel_session.pray
                            | channel_session.run
                        )
                        all_users = all_users + channel_users_in_game

                    if user in all_users:
                        user_id = f"{user.id}-{channel.id}"
                        # iterating through reactions here and removing them seems to be expensive
                        # so they can just keep their react on the adventures they can't join
                        if user_id not in self._react_messaged:
                            await channel.send(
                                _(
                                    "**{c}**, you are already in an existing adventure. "
                                    "Wait for it to finish before joining another one."
                                ).format(c=self.escape(user.display_name))
                            )
                            self._react_messaged.append(user_id)
                            return
                    else:
                        session.reactors.add(user)
                else:
                    session.reactors.add(user)
            else:
                with contextlib.suppress(discord.HTTPException):
                    await user.send(
                        _(
                            "You contemplate going on an adventure with your friends, so "
                            "you go to your bank to get some money to prepare and they "
                            "tell you that your bank is empty!\n"
                            "You run home to look for some spare coins and you can't "
                            "even find a single one, so you tell your friends that you can't "
                            "join them as you already have plans... as you are too embarrassed "
                            "to tell them you are broke!"
                        )
                    )

    async def _handle_cart(self, reaction, user):
        # This needs to be above here so a user isn't added to `_current_traders`
        # if he's in an adventure.
        if self.in_adventure(user=user):
            with contextlib.suppress(discord.HTTPException):
                await reaction.remove(user)
            return await reaction.message.channel.send(
                _("**{author}**, you have to be back in town to buy things from the cart!").format(
                    author=self.escape(user.display_name)
                ), delete_after=10
            )

        guild = user.guild
        emojis = ReactionPredicate.NUMBER_EMOJIS
        itemindex = emojis.index(str(reaction.emoji)) - 1
        items = self._current_traders[guild.id]["stock"][itemindex]
        self._current_traders[guild.id]["users"].append(user)
        spender = user
        channel = reaction.message.channel

        currency_name = await bank.get_currency_name(guild,)
        if currency_name.startswith("<"):
            currency_name = "credits"
        item_data = box(items["item"].formatted_name + " - " + humanize_number(items["price"]), lang="css")
        to_delete = await channel.send(
            _("{user}, how many {item} would you like to buy?").format(user=user.mention, item=item_data)
        )
        ctx = await self.bot.get_context(reaction.message)
        ctx.author = user
        pred = MessagePredicate.valid_int(ctx)
        try:
            msg = await self.bot.wait_for("message", check=pred, timeout=30)
        except asyncio.TimeoutError:
            self._current_traders[guild.id]["users"].remove(user)
            return
        if pred.result < 1:
            with contextlib.suppress(discord.HTTPException):
                await to_delete.delete()
                await msg.delete()
            await smart_embed(ctx, _("You're wasting my time."))
            self._current_traders[guild.id]["users"].remove(user)
            return
        if await bank.can_spend(spender, int(items["price"]) * pred.result):
            await bank.withdraw_credits(spender, int(items["price"]) * pred.result)
            async with self.get_lock(user):
                c = await self.get_character_from_json(user)
                item = items["item"]
                item.owned = pred.result
                await c.add_to_backpack(item, number=pred.result)
                await self.config.user(user).set(await c.to_json(self.config))
                with contextlib.suppress(discord.HTTPException):
                    await to_delete.delete()
                    await msg.delete()
                await channel.send(
                    box(
                        _(
                            "{author} bought {p_result} {item_name} for "
                            "{item_price} {currency_name} and put it into their backpack."
                        ).format(
                            author=self.escape(user.display_name),
                            p_result=pred.result,
                            item_name=item.formatted_name,
                            item_price=humanize_number(items["price"] * pred.result),
                            currency_name=currency_name,
                        ),
                        lang="css",
                    )
                )
                self._current_traders[guild.id]["users"].remove(user)
        else:
            with contextlib.suppress(discord.HTTPException):
                await to_delete.delete()
                await msg.delete()
                await reaction.remove(user)
            await channel.send(
                _("**{author}**, you do not have enough {currency_name}.").format(
                    author=self.escape(user.display_name), currency_name=currency_name
                ), delete_after=10
            )
            self._current_traders[guild.id]["users"].remove(user)

    async def _result(self, ctx: Context, message: discord.Message):
        if ctx.channel.id not in self._sessions:
            return
        calc_msg = await ctx.send(_("Calculating..."))
        attack = 0
        diplomacy = 0
        magic = 0
        fumblelist: set = set()
        critlist: set = set()
        failed = False
        lost = False
        session = self._sessions[ctx.channel.id]

        # update message object
        message = await message.channel.fetch_message(message.id)

        for r in message.reactions:
            if str(r.emoji) in self._adventure_actions_emoji_names:
                action = {str(v): k for k, v in self._adventure_controls.items()}[str(r.emoji)]
                async for user in r.users():
                    if not user.bot:
                        # only allow user to do one action, so remove from all
                        # others if found
                        for x in ["rage", "autoaim", "rant", "pray", "run"]:
                            if user in getattr(session, x, []):
                                getattr(session, x).remove(user)

                        getattr(session, action).add(user)
                await asyncio.sleep(0.3)

        with contextlib.suppress(discord.HTTPException):
            await message.clear_reactions()

        people = len(session.rage | session.autoaim | session.rant | session.pray | session.run)

        challenge = session.challenge

        attack, diplomacy, magic, run_msg = await self.handle_run(ctx.channel.id, attack, diplomacy, magic)
        failed = await self.handle_basilisk(ctx, failed)
        fumblelist, attack, diplomacy, magic, pray_msg = await self.handle_pray(
            ctx.channel.id, fumblelist, attack, diplomacy, magic
        )
        fumblelist, critlist, diplomacy, talk_msg = await self.handle_talk(
            ctx.channel.id, fumblelist, critlist, diplomacy
        )

        # need to pass challenge because we need to query MONSTERS[challenge]["pdef"] (and mdef)
        fumblelist, critlist, attack, magic, fight_msg = await self.handle_fight(
            ctx.channel.id, fumblelist, critlist, attack, magic, challenge
        )

        result_msg = run_msg + pray_msg + fight_msg + talk_msg
        challenge_attrib = session.attribute
        hp = int(session.monster_modified_stats["hp"] * self.ATTRIBS[challenge_attrib][0] * session.monster_stats)
        dipl = int(session.monster_modified_stats["dipl"] * self.ATTRIBS[challenge_attrib][1] * session.monster_stats)

        dmg_dealt = int(attack + magic)
        diplomacy = int(diplomacy)
        slain = dmg_dealt >= int(hp)
        persuaded = diplomacy >= int(dipl)
        damage_str = ""
        diplo_str = ""
        if dmg_dealt > 0:
            damage_str = _("The group {status} {challenge} **({result}/{int_hp})**.\n").format(
                status=_("hit the") if failed or not slain else _("killed the"),
                challenge=challenge,
                result=humanize_number(dmg_dealt),
                int_hp=humanize_number(hp),
            )
        if diplomacy > 0:
            diplo_str = _("The group {status} the {challenge} with {how} **({diplomacy}/{int_dipl})**.\n").format(
                status=_("tried to persuade") if not persuaded else _("distracted"),
                challenge=challenge,
                how=_("flattery") if failed or not persuaded else _("insults"),
                diplomacy=humanize_number(diplomacy),
                int_dipl=humanize_number(dipl),
            )
        if dmg_dealt >= diplomacy:
            self._adv_results.add_result(ctx, "attack", dmg_dealt, people, slain, session.boss or session.transcended)
        else:
            self._adv_results.add_result(ctx, "talk", diplomacy, people, persuaded, session.boss or session.transcended)
        result_msg = result_msg + "\n" + damage_str + diplo_str

        fight_name_list = []
        wizard_name_list = []
        talk_name_list = []
        pray_name_list = []
        repair_list = []
        for user in session.rage:
            fight_name_list.append(f"**{self.escape(user.display_name)}**")
        for user in session.autoaim:
            wizard_name_list.append(f"**{self.escape(user.display_name)}**")
        for user in session.rant:
            talk_name_list.append(f"**{self.escape(user.display_name)}**")
        for user in session.pray:
            pray_name_list.append(f"**{self.escape(user.display_name)}**")

        fighters_final_string = _(" and ").join(
            [", ".join(fight_name_list[:-1]), fight_name_list[-1]] if len(fight_name_list) > 2 else fight_name_list
        )
        wizards_final_string = _(" and ").join(
            [", ".join(wizard_name_list[:-1]), wizard_name_list[-1]] if len(wizard_name_list) > 2 else wizard_name_list
        )
        talkers_final_string = _(" and ").join(
            [", ".join(talk_name_list[:-1]), talk_name_list[-1]] if len(talk_name_list) > 2 else talk_name_list
        )
        preachermen_final_string = _(" and ").join(
            [", ".join(pray_name_list[:-1]), pray_name_list[-1]] if len(pray_name_list) > 2 else pray_name_list
        )
        await calc_msg.delete()
        text = ""
        success = False
        treasure = [0, 0, 0, 0, 0, 0]
        if (slain or persuaded) and not failed:
            success = True
            roll = random.randint(1, 10)
            monster_amount = hp + dipl if slain and persuaded else hp if slain else dipl
            if session.transcended:
                if session.boss and "Trancended" in session.challenge:
                    avaliable_loot = [
                        [0, 0, 1, 5, 2, 1],
                        [0, 0, 0, 0, 1, 2],
                    ]
                else:
                    avaliable_loot = [
                        [0, 0, 1, 5, 1, 1],
                        [0, 0, 1, 3, 0, 1],
                        [0, 0, 1, 1, 1, 1],
                        [0, 0, 0, 0, 0, 1],
                    ]
                treasure = random.choice(avaliable_loot)
            elif session.boss:  # rewards 60:30:10 Epic Legendary Gear Set items
                avaliable_loot = [[0, 0, 3, 1, 0, 0], [0, 0, 1, 2, 0, 0], [0, 0, 0, 3, 0, 0]]
                treasure = random.choice(avaliable_loot)
            elif session.miniboss:  # rewards 50:50 rare:normal chest for killing something like the basilisk
                treasure = random.choice(
                    [[1, 1, 1, 0, 0, 0], [0, 0, 1, 1, 0, 0], [0, 0, 2, 2, 0, 0], [0, 1, 0, 2, 0, 0]]
                )
            elif monster_amount >= 700:  # super hard stuff
                if roll <= 7:
                    treasure = random.choice([[0, 0, 1, 0, 0, 0], [0, 1, 0, 0, 0, 0], [0, 0, 0, 1, 0, 0]])
            elif monster_amount >= 500:  # rewards 50:50 rare:epic chest for killing hard stuff.
                if roll <= 5:
                    treasure = random.choice([[0, 0, 1, 0, 0, 0], [0, 1, 0, 0, 0, 0], [0, 1, 1, 0, 0, 0]])
            elif monster_amount >= 300:  # rewards 50:50 rare:normal chest for killing hardish stuff
                if roll <= 2:
                    treasure = random.choice([[1, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0], [1, 1, 0, 0, 0, 0]])
            elif monster_amount >= 80:  # small chance of a normal chest on killing stuff that's not terribly weak
                if roll == 1:
                    treasure = [1, 0, 0, 0, 0, 0]

            if session.boss:  # always rewards at least an epic chest.
                # roll for legendary chest
                roll = random.randint(1, 100)
                if roll <= 20:
                    treasure[3] += 1
                else:
                    treasure[2] += 1
            if len(critlist) != 0:
                treasure[0] += 1
            if treasure == [0, 0, 0, 0, 0, 0]:
                treasure = False
        if session.miniboss and failed:
            session.participants = session.rage | session.rant | session.pray | session.autoaim | fumblelist
            currency_name = await bank.get_currency_name(ctx.guild,)
            for user in session.participants:
                c = await self.get_character_from_json(user)
                multiplier = 0.2
                if c.dex != 0:
                    if c.dex < 0:
                        dex = min(1 / abs(c.dex), 1)
                    else:
                        dex = max(abs(c.dex), 3)
                    multiplier = multiplier / dex
                loss = round(c.bal * multiplier)
                if loss > c.bal:
                    loss = c.bal
                balance = c.bal
                loss = min(min(loss, balance), 1000000000)
                if c.bal > 0:
                    if user not in [u for u, t in repair_list]:
                        repair_list.append([user, loss])
                        if c.bal > loss:
                            await bank.withdraw_credits(user, loss)
                        else:
                            await bank.set_balance(user, 0)
                c.adventures.update({"loses": c.adventures.get("loses", 0) + 1})
                c.weekly_score.update({"adventures": c.weekly_score.get("adventures", 0) + 1})
                await self.config.user(user).set(await c.to_json(self.config))
            loss_list = []
            result_msg += session.miniboss["defeat"]
            if len(repair_list) > 0:
                temp_repair = []
                for (user, loss) in repair_list:
                    if user not in temp_repair:
                        loss_list.append(
                            _("**{user}** used {loss} {currency_name}").format(
                                user=self.escape(user.display_name),
                                loss=humanize_number(loss),
                                currency_name=currency_name,
                            )
                        )
                        temp_repair.append(user)
                result_msg += _("\n{loss_list} to repay a passing samaritan that unfroze the group.").format(
                    loss_list=humanize_list(loss_list)
                )
            return await smart_embed(ctx, result_msg)
        if session.miniboss and not slain and not persuaded:
            lost = True
            session.participants = session.rage | session.rant | session.pray | session.autoaim | fumblelist
            currency_name = await bank.get_currency_name(ctx.guild,)
            for user in session.participants:
                c = await self.get_character_from_json(user)
                multiplier = 0.2
                if c.dex != 0:
                    if c.dex < 0:
                        dex = min(1 / abs(c.dex), 1)
                    else:
                        dex = max(abs(c.dex), 3)
                    multiplier = multiplier / dex
                loss = round(c.bal * multiplier)
                if loss > c.bal:
                    loss = c.bal
                balance = c.bal
                loss = min(min(loss, balance), 1000000000)
                if c.bal > 0:
                    if user not in [u for u, t in repair_list]:
                        repair_list.append([user, loss])
                        if c.bal > loss:
                            await bank.withdraw_credits(user, loss)
                        else:
                            await bank.set_balance(user, 0)
            loss_list = []
            if len(repair_list) > 0:
                temp_repair = []
                for (user, loss) in repair_list:
                    if user not in temp_repair:
                        loss_list.append(
                            f"**{self.escape(user.display_name)}** used {humanize_number(loss)} {currency_name}"
                        )
                        temp_repair.append(user)
            miniboss = session.challenge
            special = session.miniboss["special"]
            result_msg += _(
                "The {miniboss}'s "
                "{special} was countered, but he still managed to kill you."
                "\n{loss_l} to repay a passing "
                "samaritan that resurrected the group."
            ).format(miniboss=miniboss, special=special, loss_l=humanize_list(loss_list))
        amount = 1 * session.monster_stats
        amount *= (hp + dipl) if slain and persuaded else hp if slain else dipl
        amount += int(amount * (0.25 * people))
        if people == 1:
            if slain:
                group = fighters_final_string if len(session.rage) == 1 else wizards_final_string
                text = _("{b_group} has slain the {chall} in an epic battle!").format(
                    b_group=group, chall=session.challenge
                )
                text += await self._reward(
                    ctx,
                    [u for u in session.rage | session.autoaim | session.pray if u not in fumblelist],
                    amount,
                    round(((attack if group == fighters_final_string else magic) / hp) * 0.25),
                    treasure,
                )

            if persuaded:
                text = _("{b_talkers} almost died in battle, but confounded the {chall} in the last second.").format(
                    b_talkers=talkers_final_string, chall=session.challenge
                )
                text += await self._reward(
                    ctx,
                    [u for u in session.rant | session.pray if u not in fumblelist],
                    amount,
                    round((diplomacy / dipl) * 0.25),
                    treasure,
                )

            if not slain and not persuaded:
                lost = True
                currency_name = await bank.get_currency_name(ctx.guild,)
                users = session.rage | session.rant | session.pray | session.autoaim | fumblelist
                for user in users:
                    c = await self.get_character_from_json(user)
                    multiplier = 0.2
                    if c.dex != 0:
                        if c.dex < 0:
                            dex = min(1 / abs(c.dex), 1)
                        else:
                            dex = max(abs(c.dex), 3)
                        multiplier = multiplier / dex
                    loss = round(c.bal * multiplier)
                    if loss > c.bal:
                        loss = c.bal
                    balance = c.bal
                    loss = min(min(loss, balance), 1000000000)
                    if c.bal > 0:
                        if user not in [u for u, t in repair_list]:
                            repair_list.append([user, loss])
                            if c.bal > loss:
                                await bank.withdraw_credits(user, loss)
                            else:
                                await bank.set_balance(user, 0)
                loss_list = []
                if len(repair_list) > 0:
                    temp_repair = []
                    for (user, loss) in repair_list:
                        if user not in temp_repair:
                            loss_list.append(
                                f"**{self.escape(user.display_name)}** used {humanize_number(loss)} {currency_name}"
                            )
                            temp_repair.append(user)
                repair_text = "" if not loss_list else f"{humanize_list(loss_list)} " + _("to repair their gear.")
                options = [
                    _("No amount of ranting or valiant raging could save you.\n{}").format(repair_text),
                    _("This challenge was too much for one hero.\n{}").format(repair_text),
                    _("You tried your best, but the group couldn't succeed at their attempt.\n{}").format(repair_text),
                ]
                text = random.choice(options)
        else:
            if slain and persuaded:
                if len(session.pray) > 0:
                    god = await self.config.god_name()
                    if await self.config.guild(ctx.guild).god_name():
                        god = await self.config.guild(ctx.guild).god_name()
                    if len(session.autoaim) > 0 and len(session.rage) > 0:
                        text = _(
                            "{b_fighters} slayed the {chall} "
                            "in battle, while {b_talkers} distracted with flattery, "
                            "{b_wizard} chanted magical incantations and "
                            "{b_preachers} aided in {god}'s name."
                        ).format(
                            b_fighters=fighters_final_string,
                            chall=session.challenge,
                            b_talkers=talkers_final_string,
                            b_wizard=wizards_final_string,
                            b_preachers=preachermen_final_string,
                            god=god,
                        )
                    else:
                        group = fighters_final_string if len(session.rage) > 0 else wizards_final_string
                        text = _(
                            "{b_group} slayed the {chall} "
                            "in battle, while {b_talkers} distracted with flattery and "
                            "{b_preachers} aided in {god}'s name."
                        ).format(
                            b_group=group,
                            chall=session.challenge,
                            b_talkers=talkers_final_string,
                            b_preachers=preachermen_final_string,
                            god=god,
                        )
                else:
                    if len(session.autoaim) > 0 and len(session.rage) > 0:
                        text = _(
                            "{b_fighters} slayed the {chall} "
                            "in battle, while {b_talkers} distracted with insults and "
                            "{b_wizard} chanted magical incantations."
                        ).format(
                            b_fighters=fighters_final_string,
                            chall=session.challenge,
                            b_talkers=talkers_final_string,
                            b_wizard=wizards_final_string,
                        )
                    else:
                        group = fighters_final_string if len(session.rage) > 0 else wizards_final_string
                        text = _(
                            "{b_group} slayed the {chall} in battle, while {b_talkers} distracted with insults."
                        ).format(b_group=group, chall=session.challenge, b_talkers=talkers_final_string)
                text += await self._reward(
                    ctx,
                    [u for u in session.rage | session.autoaim | session.pray | session.rant if u not in fumblelist],
                    amount,
                    round(((dmg_dealt / hp) + (diplomacy / dipl)) * 0.25),
                    treasure,
                )

            if not slain and persuaded:
                if len(session.pray) > 0:
                    text = _("{b_talkers} talked the {chall} down with {b_preachers}'s blessing.").format(
                        b_talkers=talkers_final_string, chall=session.challenge, b_preachers=preachermen_final_string,
                    )
                else:
                    text = _("{b_talkers} talked the {chall} down.").format(
                        b_talkers=talkers_final_string, chall=session.challenge
                    )
                text += await self._reward(
                    ctx,
                    [u for u in session.rant | session.pray if u not in fumblelist],
                    amount,
                    round((diplomacy / dipl) * 0.25),
                    treasure,
                )

            if slain and not persuaded:
                if len(session.pray) > 0:
                    if len(session.autoaim) > 0 and len(session.rage) > 0:
                        text = _(
                            "{b_fighters} killed the {chall} "
                            "in a most heroic battle with a little help from {b_preachers} and "
                            "{b_wizard} providing backup autoaimed shots."
                        ).format(
                            b_fighters=fighters_final_string,
                            chall=session.challenge,
                            b_preachers=preachermen_final_string,
                            b_wizard=wizards_final_string,
                        )
                    else:
                        group = fighters_final_string if len(session.rage) > 0 else wizards_final_string
                        text = _(
                            "{b_group} killed the {chall} "
                            "in a most heroic battle with a little help from {b_preachers}."
                        ).format(b_group=group, chall=session.challenge, b_preachers=preachermen_final_string,)
                else:
                    if len(session.autoaim) > 0 and len(session.rage) > 0:
                        text = _(
                            "{b_fighters} killed the {chall} "
                            "in a most heroic battle with {b_wizard} providing backup autoaimed shots."
                        ).format(
                            b_fighters=fighters_final_string, chall=session.challenge, b_wizard=wizards_final_string,
                        )
                    else:
                        group = fighters_final_string if len(session.rage) > 0 else wizards_final_string
                        text = _("{b_group} killed the {chall} in an epic fight.").format(
                            b_group=group, chall=session.challenge
                        )
                text += await self._reward(
                    ctx,
                    [u for u in session.rage | session.autoaim | session.pray if u not in fumblelist],
                    amount,
                    round((dmg_dealt / hp) * 0.25),
                    treasure,
                )

            if not slain and not persuaded:
                lost = True
                currency_name = await bank.get_currency_name(ctx.guild,)
                users = session.rage | session.rant | session.pray | session.autoaim | fumblelist
                for user in users:
                    c = await self.get_character_from_json(user)
                    multiplier = 0.2
                    if c.dex != 0:
                        if c.dex < 0:
                            dex = min(1 / abs(c.dex), 1)
                        else:
                            dex = max(abs(c.dex), 3)
                        multiplier = multiplier / dex
                    loss = round(c.bal * multiplier)
                    if loss > c.bal:
                        loss = c.bal
                    balance = c.bal
                    loss = min(min(loss, balance), 1000000000)
                    if c.bal > 0:
                        if user not in [u for u, t in repair_list]:
                            repair_list.append([user, loss])
                            if c.bal > loss:
                                await bank.withdraw_credits(user, loss)
                            else:
                                await bank.set_balance(user, 0)
                if session.run:
                    users = session.run
                    for user in users:
                        c = await self.get_character_from_json(user)
                        multiplier = 0.2
                        if c.dex != 0:
                            if c.dex < 0:
                                dex = min(1 / abs(c.dex), 1)
                            else:
                                dex = max(abs(c.dex), 3)
                            multiplier = multiplier / dex
                        loss = round(c.bal * multiplier)
                        if loss > c.bal:
                            loss = c.bal
                        balance = c.bal
                        loss = min(min(loss, balance), 1000000000)
                        if c.bal > 0:
                            if user not in [u for u, t in repair_list]:
                                repair_list.append([user, loss])
                                if user not in [u for u, t in repair_list]:
                                    if c.bal > loss:
                                        await bank.withdraw_credits(user, loss)
                                    else:
                                        await bank.set_balance(user, 0)
                loss_list = []
                if len(repair_list) > 0:
                    temp_repair = []
                    for (user, loss) in repair_list:
                        if user not in temp_repair:
                            loss_list.append(
                                _("**{user}** used {loss} {currency_name}").format(
                                    user=self.escape(user.display_name),
                                    loss=humanize_number(loss),
                                    currency_name=currency_name,
                                )
                            )
                            temp_repair.append(user)
                repair_text = "" if not loss_list else _("{} to repair their gear.").format(humanize_list(loss_list))
                options = [
                    _("No amount of ranting or valiant raging could save you.\n{}").format(repair_text),
                    _("This challenge was too much for the group.\n{}").format(repair_text),
                    _("You tried your best, but couldn't succeed.\n{}").format(repair_text),
                ]
                text = random.choice(options)

        output = f"{result_msg}\n{text}"
        output = pagify(output, page_length=1900)
        for i in output:
            await smart_embed(ctx, i, success=success)
        await self._data_check(ctx)
        session.participants = session.rage | session.rant | session.pray | session.run | session.autoaim | fumblelist

        participants = {
            "rage": session.rage,
            "autoaim": session.autoaim,
            "rant": session.rant,
            "pray": session.pray,
            "run": session.run,
            "fumbles": fumblelist,
        }

        parsed_users = []
        for (action_name, action) in participants.items():
            for user in action:
                c = await self.get_character_from_json(user)
                current_val = c.adventures.get(action_name, 0)
                c.adventures.update({action_name: current_val + 1})
                if user not in parsed_users:
                    special_action = "loses" if lost or user in participants["run"] else "wins"
                    current_val = c.adventures.get(special_action, 0)
                    c.adventures.update({special_action: current_val + 1})
                    c.weekly_score.update({"adventures": c.weekly_score.get("adventures", 0) + 1})
                    parsed_users.append(user)
                await self.config.user(user).set(await c.to_json(self.config))

    async def handle_run(self, channel_id, attack, diplomacy, magic):
        runners = []
        msg = ""
        session = self._sessions[channel_id]
        if len(session.run) != 0:
            for user in session.run:
                runners.append(f"**{self.escape(user.display_name)}**")
            msg += _("{} just ran away.\n").format(humanize_list(runners))
        return (attack, diplomacy, magic, msg)

    async def handle_fight(self, channel_id, fumblelist, critlist, attack, magic, challenge):
        session = self._sessions[channel_id]
        attack_list = session.rage | session.autoaim
        pdef = max(session.monster_modified_stats["pdef"], 0.5)
        mdef = max(session.monster_modified_stats["mdef"], 0.5)

        fumble_count = 0
        # make sure we pass this check first
        failed_emoji = self.emojis.fumble
        if len(attack_list) >= 1:
            msg = ""
            if len(session.rage) >= 1:
                if pdef >= 1.5:
                    msg += _("Swords bounce off this monster as its skin is **almost impenetrable!**\n")
                elif pdef >= 1.25:
                    msg += _("This monster has **extremely tough** armour!\n")
                elif pdef > 1:
                    msg += _("Swords don't cut this monster **quite as well!**\n")
                elif 0.75 <= pdef < 1:
                    msg += _("This monster is **soft and easy** to slice!\n")
                elif pdef > 0 and pdef != 1:
                    msg += _("Swords slice through this monster like a **hot knife through butter!**\n")
            if len(session.autoaim) >= 1:
                if mdef >= 1.5:
                    msg += _("Bullets? Pfft, your puny bullets are **no match** for this creature!\n")
                elif mdef >= 1.25:
                    msg += _("This monster has **substantial bullet resistance!**\n")
                elif mdef > 1:
                    msg += _("This monster has increased **bullet resistance!**\n")
                elif 0.75 <= mdef < 1:
                    msg += _("This monster's hide **melts to bullets!**\n")
                elif mdef > 0 and mdef != 1:
                    msg += _("Bullets are **hugely effective** against this monster!\n")

            report = _("Attack Party: \n\n")
        else:
            return (fumblelist, critlist, attack, magic, "")

        for user in session.rage:
            c = await self.get_character_from_json(user)
            crit_mod = max(max(c.dex, c.luck) + (c.total_att // 20), 1)  # Thanks GoaFan77
            mod = 0
            max_roll = 50 if c.rebirths >= 15 else 20
            if crit_mod != 0:
                mod = round(crit_mod / 10)
            if c.rebirths < 15 < mod:
                mod = 15
                max_roll = 20
            elif (mod + 1) > 45:
                mod = 45

            roll = max(random.randint((1 + mod), max_roll), 1)
            if c.heroclass.get("pet", {}).get("bonuses", {}).get("crit", False):
                pet_crit = c.heroclass.get("pet", {}).get("bonuses", {}).get("crit", 0)
                pet_crit = random.randint(pet_crit, 100)
                if pet_crit == 100:
                    roll = max_roll
                elif roll <= 25 and pet_crit >= 95:
                    roll = random.randint(max_roll - 5, max_roll)
                elif roll > 25 and pet_crit >= 95:
                    roll = random.randint(roll, max_roll)

            att_value = c.total_att
            rebirths = c.rebirths * 3 if c.heroclass["name"] == "Berserker" else 0
            if roll == 1:
                if c.heroclass["name"] == "Berserker" and c.heroclass["ability"]:
                    bonus_roll = random.randint(5, 15)
                    bonus_multi = random.choice([0.2, 0.3, 0.4, 0.5])
                    bonus = max(bonus_roll, int((roll + att_value + rebirths) * bonus_multi))
                    attack += max(int((roll - bonus + att_value) / pdef), 0)
                    report += (
                        f"**{self.escape(user.display_name)}**: "
                        f"{self.emojis.dice}({roll}) + "
                        f"{self.emojis.berserk}{humanize_number(bonus)} + "
                        f"{self.emojis.rage}{str(humanize_number(att_value))}\n"
                    )
                else:
                    msg += _("**{}** fumbled the attack.\n").format(self.escape(user.display_name))
                    fumblelist.add(user)
                    fumble_count += 1
            elif roll == max_roll or c.heroclass["name"] == "Berserker":
                crit_str = ""
                crit_bonus = 0
                base_bonus = random.randint(5, 10) + rebirths
                if roll == max_roll:
                    msg += _("**{}** landed a critical hit.\n").format(self.escape(user.display_name))
                    critlist.add(user)
                    crit_bonus = random.randint(5, 20) + 2 * rebirths
                    crit_str = f"{self.emojis.crit} {humanize_number(crit_bonus)}"
                if c.heroclass["ability"]:
                    base_bonus = random.randint(15, 50) + 5 * rebirths
                base_str = f"{self.emojis.crit}️ {humanize_number(base_bonus)}"
                attack += max(int((roll + base_bonus + crit_bonus + att_value) / pdef), 0)
                bonus = base_str + crit_str
                report += (
                    f"**{self.escape(user.display_name)}**: "
                    f"{self.emojis.dice}({roll}) + "
                    f"{self.emojis.berserk}{bonus} + "
                    f"{self.emojis.rage}{str(humanize_number(att_value))}\n"
                )
            else:
                attack += max(int((roll + att_value) / pdef) + rebirths, 0)
                report += (
                    f"**{self.escape(user.display_name)}**: "
                    f"{self.emojis.dice}({roll}) + "
                    f"{self.emojis.rage}{str(humanize_number(att_value))}\n"
                )
        for user in session.autoaim:
            c = await self.get_character_from_json(user)
            crit_mod = max(max(c.dex, c.luck) + (c.total_int // 20), 0)
            mod = 0
            max_roll = 50 if c.rebirths >= 15 else 20
            if crit_mod != 0:
                mod = round(crit_mod / 10)
            if c.rebirths < 15 < mod:
                mod = 15
                max_roll = 20
            elif (mod + 1) > 45:
                mod = 45
            roll = max(random.randint((1 + mod), max_roll), 1)
            if c.heroclass.get("pet", {}).get("bonuses", {}).get("crit", False):
                pet_crit = c.heroclass.get("pet", {}).get("bonuses", {}).get("crit", 0)
                pet_crit = random.randint(pet_crit, 100)
                if pet_crit == 100:
                    roll = max_roll
                elif roll <= 25 and pet_crit >= 95:
                    roll = random.randint(max_roll - 5, max_roll)
                elif roll > 25 and pet_crit >= 95:
                    roll = random.randint(roll, max_roll)
            int_value = c.total_int
            rebirths = c.rebirths * 3 if c.heroclass["name"] == "Autoaimer" else 0
            if roll == 1:
                msg += _("{}**{}** almost set themselves on fire.\n").format(
                    failed_emoji, self.escape(user.display_name)
                )
                fumblelist.add(user)
                fumble_count += 1
                if c.heroclass["name"] == "Autoaimer" and c.heroclass["ability"]:
                    bonus_roll = random.randint(5, 15)
                    bonus_multi = random.choice([0.2, 0.3, 0.4, 0.5])
                    bonus = max(bonus_roll, int((roll + int_value + rebirths) * bonus_multi))
                    magic += max(int((roll - bonus + int_value) / mdef), 0)
                    report += (
                        f"**{self.escape(user.display_name)}**: "
                        f"{self.emojis.dice}({roll}) + "
                        f"{self.emojis.magic_crit}{humanize_number(bonus)} + "
                        f"{self.emojis.autoaim}{str(humanize_number(int_value))}\n"
                    )
            elif roll == max_roll or (c.heroclass["name"] == "Autoaimer"):
                crit_str = ""
                crit_bonus = 0
                base_bonus = random.randint(5, 10) + rebirths
                base_str = f"{self.emojis.magic_crit}️ {humanize_number(base_bonus)}"
                if roll == max_roll:
                    msg += _("**{}** had a surge of energy.\n").format(self.escape(user.display_name))
                    critlist.add(user)
                    crit_bonus = random.randint(5, 20) + 2 * rebirths
                    crit_str = f"{self.emojis.crit} {humanize_number(crit_bonus)}"
                if c.heroclass["ability"]:
                    base_bonus = random.randint(15, 50) + 5 * rebirths
                    base_str = f"{self.emojis.magic_crit}️ {humanize_number(base_bonus)}"
                magic += max(int((roll + base_bonus + crit_bonus + int_value) / mdef), 0)
                bonus = base_str + crit_str
                report += (
                    f"**{self.escape(user.display_name)}**: "
                    f"{self.emojis.dice}({roll}) + "
                    f"{bonus} + "
                    f"{self.emojis.autoaim}{humanize_number(int_value)}\n"
                )
            else:
                magic += max(int((roll + int_value) / mdef) + c.rebirths // 5, 0)
                report += (
                    f"**{self.escape(user.display_name)}**: "
                    f"{self.emojis.dice}({roll}) + "
                    f"{self.emojis.autoaim}{humanize_number(int_value)}\n"
                )
        if fumble_count == len(attack_list):
            report += _("No one!")
        msg += report + "\n"
        for user in fumblelist:
            if user in session.rage:
                session.rage.remove(user)
            elif user in session.autoaim:
                session.autoaim.remove(user)
        return (fumblelist, critlist, attack, magic, msg)

    async def handle_pray(self, channel_id, fumblelist, attack, diplomacy, magic):
        session = self._sessions[channel_id]
        god = await self.config.god_name()
        guild_god_name = await self.config.guild(session.guild).god_name()
        if guild_god_name:
            god = guild_god_name
        msg = ""
        failed_emoji = self.emojis.fumble
        for user in session.pray:
            c = await self.get_character_from_json(user)
            rebirths = c.rebirths * (3 if c.heroclass["name"] == "Samaritan" else 1)
            if c.heroclass["name"] == "Samaritan":
                crit_mod = c.dex + (c.total_int // 20)
                mod = 0
                max_roll = 50 if c.rebirths >= 15 else 20
                if crit_mod != 0:
                    mod = round(crit_mod / 10)
                if c.rebirths < 15 < mod:
                    mod = 15
                    max_roll = 20
                elif (mod + 1) > 45:
                    mod = 45
                roll = max(random.randint((1 + mod), max_roll), 1)
                if len(session.rage | session.rant | session.autoaim) == 0:
                    msg += _("**{}** reported like a madman but nobody was there to receive it.\n").format(
                        self.escape(user.display_name)
                    )
                if roll == 1:
                    pray_att_bonus = 0
                    pray_diplo_bonus = 0
                    pray_magic_bonus = 0
                    if session.rage:
                        pray_att_bonus = max((5 * len(session.rage)) - ((5 * len(session.rage)) * max(rebirths * 0.01, 1.5)), 0)
                    if session.rant:
                        pray_diplo_bonus = max((5 * len(session.rant)) - ((5 * len(session.rant)) * max(rebirths * 0.01, 1.5)), 0)
                    if session.autoaim:
                        pray_magic_bonus = max((5 * len(session.autoaim)) - ((5 * len(session.autoaim)) * max(rebirths * 0.01, 1.5)), 0)
                    attack -= pray_att_bonus
                    diplomacy -= pray_diplo_bonus
                    magic -= pray_magic_bonus
                    fumblelist.add(user)
                    msg += _(
                        "**{user}'s** sermon offended the mighty {god}. {failed_emoji}"
                        "(-{len_f_list}{attack}/-{len_m_list}{magic}/-{len_t_list}{talk}) {roll_emoji}({roll})\n"
                    ).format(
                        user=self.escape(user.display_name),
                        god=god,
                        failed_emoji=failed_emoji,
                        attack=self.emojis.rage,
                        talk=self.emojis.rant,
                        magic=self.emojis.autoaim,
                        len_f_list=humanize_number(pray_att_bonus),
                        len_t_list=humanize_number(pray_diplo_bonus),
                        len_m_list=humanize_number(pray_magic_bonus),
                        roll_emoji=self.emojis.dice,
                        roll=roll,
                    )

                else:
                    mod = roll // 3 if not c.heroclass["ability"] else roll
                    pray_att_bonus = 0
                    pray_diplo_bonus = 0
                    pray_magic_bonus = 0

                    if session.rage:
                        pray_att_bonus = int(
                            (mod * len(session.rage)) + ((mod * len(session.rage)) * max(rebirths * 0.1, 1.5))
                        )
                    if session.rant:
                        pray_diplo_bonus = int(
                            (mod * len(session.rant)) + ((mod * len(session.rant)) * max(rebirths * 0.1, 1.5))
                        )
                    if session.autoaim:
                        pray_magic_bonus = int(
                            (mod * len(session.autoaim)) + ((mod * len(session.autoaim)) * max(rebirths * 0.1, 1.5))
                        )
                    attack += max(pray_att_bonus, 0)
                    magic += max(pray_magic_bonus, 0)
                    diplomacy += max(pray_diplo_bonus, 0)
                    if roll == 50:
                        roll_msg = _(
                            "**{user}** turned into an avatar of mighty {god}. "
                            "(+{len_f_list}{attack}/+{len_m_list}{magic}/+{len_t_list}{talk}) {roll_emoji}({roll})\n"
                        )
                    else:
                        roll_msg = _(
                            "**{user}** blessed you all in {god}'s name. "
                            "(+{len_f_list}{attack}/+{len_m_list}{magic}/+{len_t_list}{talk}) {roll_emoji}({roll})\n"
                        )
                    msg += roll_msg.format(
                        user=self.escape(user.display_name),
                        god=god,
                        attack=self.emojis.rage,
                        talk=self.emojis.rant,
                        magic=self.emojis.autoaim,
                        len_f_list=humanize_number(pray_att_bonus),
                        len_t_list=humanize_number(pray_diplo_bonus),
                        len_m_list=humanize_number(pray_magic_bonus),
                        roll_emoji=self.emojis.dice,
                        roll=roll,
                    )
            else:
                roll = random.randint(1, 10)
                if len(session.rage | session.rant | session.autoaim) == 0:
                    msg += _("**{}** reported like a madman but nobody else helped them.\n").format(
                        self.escape(user.display_name)
                    )

                elif roll == 5:
                    attack_buff = 0
                    talk_buff = 0
                    magic_buff = 0
                    if session.rage:
                        attack_buff = 10 * (len(session.rage) + rebirths // 15)
                    if session.rant:
                        talk_buff = 10 * (len(session.rant) + rebirths // 15)
                    if session.autoaim:
                        magic_buff = 10 * (len(session.autoaim) + rebirths // 15)

                    attack += max(attack_buff, 0)
                    magic += max(magic_buff, 0)
                    diplomacy += max(talk_buff, 0)
                    msg += _(
                        "**{user}'s** report called upon the mighty {god} to help you. "
                        "(+{len_f_list}{attack}/+{len_m_list}{magic}/+{len_t_list}{talk}) {roll_emoji}({roll})\n"
                    ).format(
                        user=self.escape(user.display_name),
                        god=god,
                        attack=self.emojis.rage,
                        talk=self.emojis.rant,
                        magic=self.emojis.autoaim,
                        len_f_list=humanize_number(attack_buff),
                        len_t_list=humanize_number(talk_buff),
                        len_m_list=humanize_number(magic_buff),
                        roll_emoji=self.emojis.dice,
                        roll=roll,
                    )
                else:
                    fumblelist.add(user)
                    msg += _("{}**{}'s** reports went unanswered.\n").format(
                        failed_emoji, self.escape(user.display_name)
                    )
        for user in fumblelist:
            if user in session.pray:
                session.pray.remove(user)
        return (fumblelist, attack, diplomacy, magic, msg)

    async def handle_talk(self, channel_id, fumblelist, critlist, diplomacy):
        session = self._sessions[channel_id]
        if len(session.rant) >= 1:
            report = _("Talking Party: \n\n")
            msg = ""
            fumble_count = 0
        else:
            return (fumblelist, critlist, diplomacy, "")
        failed_emoji = self.emojis.fumble
        for user in session.rant:
            c = await self.get_character_from_json(user)
            crit_mod = max(max(c.dex, c.luck) + (c.total_int // 50) + (c.total_cha // 20), 1)
            mod = 0
            max_roll = 50 if c.rebirths >= 15 else 20
            if crit_mod != 0:
                mod = round(crit_mod / 10)
            if c.rebirths < 15 < mod:
                mod = 15
            elif (mod + 1) > 45:
                mod = 45
            roll = max(random.randint((1 + mod), max_roll), 1)
            dipl_value = c.total_cha
            rebirths = c.rebirths * 3 if c.heroclass["name"] == "Tilter" else 0
            if roll == 1:
                if c.heroclass["name"] == "Tilter" and c.heroclass["ability"]:
                    bonus = random.randint(5, 15)
                    diplomacy += max(roll - bonus + dipl_value + rebirths, 0)
                    report += (
                        f"**{self.escape(user.display_name)}** "
                        f"🎲({roll}) +💥{bonus} +🗨{humanize_number(dipl_value)}\n"
                    )
                else:
                    msg += _("{}**{}** accidentally offended the enemy.\n").format(
                        failed_emoji, self.escape(user.display_name)
                    )
                    fumblelist.add(user)
                    fumble_count += 1
            elif roll == max_roll or c.heroclass["name"] == "Tilter":
                crit_str = ""
                crit_bonus = 0
                base_bonus = random.randint(5, 10) + rebirths
                if roll == max_roll:
                    msg += _("**{}** made a compelling argument.\n").format(self.escape(user.display_name))
                    critlist.add(user)
                    crit_bonus = random.randint(5, 20) + 2 * rebirths
                    crit_str = f"{self.emojis.crit} {crit_bonus}"

                if c.heroclass["ability"]:
                    base_bonus = random.randint(15, 50) + 5 * rebirths
                base_str = f"🎵 {humanize_number(base_bonus)}"
                diplomacy += max(roll + base_bonus + crit_bonus + dipl_value, 0)
                bonus = base_str + crit_str
                report += (
                    f"**{self.escape(user.display_name)}** "
                    f"{self.emojis.dice}({roll}) + "
                    f"{bonus} + "
                    f"{self.emojis.rant}{humanize_number(dipl_value)}\n"
                )
            else:
                diplomacy += max(roll + dipl_value + c.rebirths // 5, 0)
                report += (
                    f"**{self.escape(user.display_name)}** "
                    f"{self.emojis.dice}({roll}) + "
                    f"{self.emojis.rant}{humanize_number(dipl_value)}\n"
                )
        if fumble_count == len(session.rant):
            report += _("No one!")
        msg = msg + report + "\n"
        for user in fumblelist:
            if user in session.rant:
                session.rant.remove(user)
        return (fumblelist, critlist, diplomacy, msg)

    async def handle_basilisk(self, ctx: Context, failed):
        session = self._sessions[ctx.channel.id]
        participants = session.rage | session.rant | session.pray | session.autoaim
        if session.miniboss:
            failed = True
            req_item, slot = session.miniboss["requirements"]
            if req_item == "members":
                if len(participants) > int(slot):
                    failed = False
            elif req_item == "emoji" and session.reacted:
                failed = False
            else:
                for user in participants:  # check if any fighter has an equipped mirror shield to give them a chance.
                    c = await self.get_character_from_json(user)
                    if any(x in c.sets for x in ["The Supreme One", "Ainz Ooal Gown"]):
                        failed = False
                        break
                    with contextlib.suppress(KeyError):
                        current_equipment = c.get_current_equipment()
                        for item in current_equipment:
                            item_name = str(item)
                            if item.rarity != "forged" and (req_item in item_name or "shiny" in item_name.lower()):
                                failed = False
                                break
        else:
            failed = False
        return failed

    async def _add_rewards(self, ctx: Context, user, exp, cp, special):
        lock = self.get_lock(user)
        if not lock.locked():
            await lock.acquire()
        c = await self.get_character_from_json(user, release_lock=True)
        rebirth_text = ""
        c.exp += exp
        member = ctx.guild.get_member(user.id)
        cp = max(cp, 0)
        if cp > 0:
            try:
                await bank.deposit_credits(member, cp)
            except BalanceTooHigh as e:
                await bank.set_balance(member, e.max_balance)
        extra = ""
        rebirthextra = ""
        lvl_start = c.lvl
        lvl_end = int(max(c.exp, 0) ** (1 / 3.5))
        lvl_end = lvl_end if lvl_end < c.maxlevel else c.maxlevel
        levelup_emoji = self.emojis.level_up
        rebirth_emoji = self.emojis.rebirth
        if lvl_end >= c.maxlevel:
            rebirthextra = _("{} {} You can now rebirth.\n").format(rebirth_emoji, user.mention)
        if lvl_start < lvl_end:
            # recalculate free skillpoint pool based on new level and already spent points.
            c.lvl = lvl_end
            assigned_stats = c.skill["att"] + c.skill["cha"] + c.skill["int"]
            starting_points = await calculate_sp(lvl_start, c) + assigned_stats
            ending_points = await calculate_sp(lvl_end, c) + assigned_stats

            if c.skill["pool"] < 0:
                c.skill["pool"] = 0
            c.skill["pool"] += ending_points - starting_points
            if c.skill["pool"] > 0:
                extra = _(" You have **{}** skill points available.").format(c.skill["pool"])
            rebirth_text = _("{} {} is now level **{}**!{}\n{}").format(
                levelup_emoji, user.mention, lvl_end, extra, rebirthextra
            )
        if c.rebirths > 1:
            roll = random.randint(1, 100)
            if lvl_end == c.maxlevel:
                roll += random.randint(50, 100)
            if special is False:
                special = [0, 0, 0, 0, 0, 0]
                if c.rebirths > 1 and roll < 50:
                    special[0] += 1
                if c.rebirths > 5 and roll < 30:
                    special[1] += 1
                if c.rebirths > 10 > roll:
                    special[2] += 1
                if c.rebirths > 15 and roll < 5:
                    special[3] += 1
                if special == [0, 0, 0, 0, 0, 0]:
                    special = False
            else:
                if c.rebirths > 1 and roll < 50:
                    special[0] += 1
                if c.rebirths > 5 and roll < 30:
                    special[1] += 1
                if c.rebirths > 10 > roll:
                    special[2] += 1
                if c.rebirths > 15 and roll < 5:
                    special[3] += 1
                if special == [0, 0, 0, 0, 0, 0]:
                    special = False
        if special is not False:
            c.treasure = [sum(x) for x in zip(c.treasure, special)]
        await self.config.user(user).set(await c.to_json(self.config))
        with contextlib.suppress(Exception):
            lock.release()
        return rebirth_text

    async def _adv_countdown(self, ctx: Context, seconds, title) -> asyncio.Task:
        await self._data_check(ctx)

        async def adv_countdown():
            secondint = int(seconds)
            adv_end = await self._get_epoch(secondint)
            timer, done, sremain = await self._remaining(adv_end)

            message_adv = self._sessions[ctx.channel.id].countdown_message
            if message_adv is None:
                message_adv = await ctx.send(f"⏳ [{title}] {timer}s")
                self._sessions[ctx.channel.id].countdown_message = message_adv

            deleted = False
            while not done:
                timer, done, sremain = await self._remaining(adv_end)
                self._adventure_countdown[ctx.channel.id] = (timer, done, sremain)
                if done:
                    if not deleted:
                        await message_adv.delete()
                    break
                elif not deleted and ((int(sremain) % 5 == 0 and int(sremain) <= 20) or int(sremain) % 10 == 0):
                    try:
                        await message_adv.edit(content=f"⏳ [{title}] {timer}s")
                    except discord.NotFound:
                        deleted = True
                await asyncio.sleep(1)
            log.debug("Timer countdown done.")

        return self.bot.loop.create_task(adv_countdown())

    async def _cart_countdown(self, ctx: Context, seconds, title, room=None) -> asyncio.Task:
        room = room or ctx
        await self._data_check(ctx)

        async def cart_countdown():
            secondint = int(seconds)
            cart_end = await self._get_epoch(secondint)
            timer, done, sremain = await self._remaining(cart_end)
            message_cart = await room.send(f"⏳ [{title}] {timer}s")
            deleted = False
            while not done:
                timer, done, sremain = await self._remaining(cart_end)
                self._trader_countdown[ctx.guild.id] = (timer, done, sremain)
                if done:
                    if not deleted:
                        await message_cart.delete()
                    break
                if not deleted and int(sremain) % 5 == 0:
                    try:
                        await message_cart.edit(content=f"⏳ [{title}] {timer}s")
                    except discord.NotFound:
                        deleted = True
                await asyncio.sleep(1)

        return ctx.bot.loop.create_task(cart_countdown())

    async def _genitem(self, rarity: str = None, slot: str = None):
        """Generate an item."""
        if rarity == "set":
            items = list(self.TR_GEAR_SET.items())
            items = (
                [
                    i
                    for i in items
                    if i[1]["slot"] == [slot] or (slot == "two handed" and i[1]["slot"] == ["left", "right"])
                ]
                if slot
                else items
            )
            item_name, item_data = random.choice(items)
            return Item.from_json({item_name: item_data})

        RARE_INDEX = RARITIES.index("rare")
        EPIC_INDEX = RARITIES.index("epic")
        PREFIX_CHANCE = {"rare": 0.5, "epic": 0.75, "legendary": 0.9, "ascended": 1.0, "set": 0}
        SUFFIX_CHANCE = {"epic": 0.5, "legendary": 0.75, "ascended": 0.5}

        if rarity not in RARITIES:
            rarity = "normal"
        if slot is None:
            slot = random.choice(ORDER)
        name = ""
        stats = {"att": 0, "cha": 0, "int": 0, "dex": 0, "luck": 0}

        def add_stats(word_stats):
            """Add stats in word's dict to local stats dict."""
            for stat in stats.keys():
                if stat in word_stats:
                    stats[stat] += word_stats[stat]

        # only rare and above should have prefix with PREFIX_CHANCE
        if RARITIES.index(rarity) >= RARE_INDEX and random.random() <= PREFIX_CHANCE[rarity]:
            #  log.debug(f"Prefix %: {PREFIX_CHANCE[rarity]}")
            prefix, prefix_stats = random.choice(list(self.PREFIXES.items()))
            name += f"{prefix} "
            add_stats(prefix_stats)

        material, material_stat = random.choice(list(self.MATERIALS[rarity].items()))
        name += f"{material} "
        for stat in stats.keys():
            stats[stat] += material_stat

        equipment, equipment_stats = random.choice(list(self.EQUIPMENT[slot].items()))
        name += f"{equipment}"
        add_stats(equipment_stats)

        # only epic and above should have suffix with SUFFIX_CHANCE
        if RARITIES.index(rarity) >= EPIC_INDEX and random.random() <= SUFFIX_CHANCE[rarity]:
            #  log.debug(f"Suffix %: {SUFFIX_CHANCE[rarity]}")
            suffix, suffix_stats = random.choice(list(self.SUFFIXES.items()))
            of_keyword = "of" if "the" not in suffix_stats else "of the"
            name += f" {of_keyword} {suffix}"
            add_stats(suffix_stats)

        slot_list = [slot] if slot != "two handed" else ["left", "right"]
        return Item(
            name=name,
            slot=slot_list,
            rarity=rarity,
            att=stats["att"],
            int=stats["int"],
            cha=stats["cha"],
            dex=stats["dex"],
            luck=stats["luck"],
            owned=1,
            parts=1,
        )

    async def _backpack_sell_button_action(self, ctx, emoji, page, item, price_shown, character):
        currency_name = await bank.get_currency_name(ctx.guild,)
        msg = ""
        try:
            if emoji == "\N{DIGIT ONE}\N{COMBINING ENCLOSING KEYCAP}":  # user reacted with one to sell.
                ctx.command.reset_cooldown(ctx)
                # sell one of the item
                price = 0
                item.owned -= 1
                price += price_shown
                msg += _("**{author}** sold one {item} for {price} {currency_name}.\n").format(
                    author=self.escape(ctx.author.display_name),
                    item=box(item, lang="css"),
                    price=humanize_number(price),
                    currency_name=currency_name,
                )
                if item.owned <= 0:
                    del character.backpack[item.name]
                price = max(price, 0)
                if price > 0:
                    try:
                        await bank.deposit_credits(ctx.author, price)
                    except BalanceTooHigh as e:
                        await bank.set_balance(ctx.author, e.max_balance)
            elif emoji == "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}":  # user wants to sell all owned.
                ctx.command.reset_cooldown(ctx)
                price = 0
                old_owned = item.owned
                count = 0
                for x in range(0, item.owned):
                    item.owned -= 1
                    price += price_shown
                    if item.owned <= 0:
                        del character.backpack[item.name]
                    count += 1
                msg += _("**{author}** sold all their {old_item} for {price} {currency_name}.\n").format(
                    author=self.escape(ctx.author.display_name),
                    old_item=box(str(item) + " - " + str(old_owned), lang="css"),
                    price=humanize_number(price),
                    currency_name=currency_name,
                )
                price = max(price, 0)
                if price > 0:
                    try:
                        await bank.deposit_credits(ctx.author, price)
                    except BalanceTooHigh as e:
                        await bank.set_balance(ctx.author, e.max_balance)
            elif (
                emoji == "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS WITH CIRCLED ONE OVERLAY}"
            ):  # user wants to sell all but one.
                if item.owned == 1:
                    raise AdventureCheckFailure(_("You already only own one of those items."))
                price = 0
                old_owned = item.owned
                count = 0
                for x in range(1, item.owned):
                    item.owned -= 1
                    price += price_shown
                if not count % 10:
                    await asyncio.sleep(0.1)
                count += 1
                if price != 0:
                    msg += _("**{author}** sold all but one of their {old_item} for {price} {currency_name}.\n").format(
                        author=self.escape(ctx.author.display_name),
                        old_item=box(str(item) + " - " + str(old_owned - 1), lang="css"),
                        price=humanize_number(price),
                        currency_name=currency_name,
                    )
                    price = max(price, 0)
                    if price > 0:
                        try:
                            await bank.deposit_credits(ctx.author, price)
                        except BalanceTooHigh as e:
                            await bank.set_balance(ctx.author, e.max_balance)
            else:  # user doesn't want to sell those items.
                msg = _("Not selling those items.")
        finally:
            lock = self.get_lock(ctx.author)
            with contextlib.suppress(Exception):
                lock.release()

        if msg:
            character.last_known_currency = await bank.get_balance(ctx.author)
            character.last_currency_check = time.time()
            await self.config.user(ctx.author).set(await character.to_json(self.config))
            pages = [page for page in pagify(msg, delims=["\n"], page_length=1900)]
            if len(pages) > 1:
                await menu(ctx, pages, MENU_CONTROLS)
            else:
                await ctx.send(pages[0])

    async def get_leaderboard(self, positions: int = None, guild: discord.Guild = None) -> List[tuple]:
        """Gets the Adventure's leaderboard.

        Parameters
        ----------
        positions : `int`
            The number of positions to get
        guild : discord.Guild
            The guild to get the leaderboard of. If this
            is provided, get only guild members on the leaderboard

        Returns
        -------
        `list` of `tuple`
            The sorted leaderboard in the form of :code:`(user_id, raw_account)`
        """
        raw_accounts = await self.config.all_users()
        if guild is not None:
            tmp = raw_accounts.copy()
            for acc in tmp:
                if not guild.get_member(acc):
                    del raw_accounts[acc]
        raw_accounts_new = {}
        async for (k, v) in AsyncIter(raw_accounts.items()):
            user_data = {}
            for item in ["lvl", "rebirths", "set_items"]:
                if item not in v:
                    v.update({item: 0})
            for (vk, vi) in v.items():
                if vk in ["lvl", "rebirths", "set_items"]:
                    user_data.update({vk: vi})

            if user_data:
                user_data = {k: user_data}
            raw_accounts_new.update(user_data)
        sorted_acc = sorted(
            raw_accounts_new.items(),
            key=lambda x: (x[1].get("rebirths", 0), x[1].get("lvl", 1), x[1].get("set_items", 0)),
            reverse=True,
        )
        if positions is None:
            return sorted_acc
        else:
            return sorted_acc[:positions]

    async def get_weekly_scoreboard(self, positions: int = None, guild: discord.Guild = None) -> List[tuple]:
        """Gets the bank's leaderboard.

        Parameters
        ----------
        positions : `int`
            The number of positions to get
        guild : discord.Guild
            The guild to get the leaderboard of. If this
            is provided, get only guild members on the leaderboard

        Returns
        -------
        `list` of `tuple`
            The sorted leaderboard in the form of :code:`(user_id, raw_account)`

        Raises
        ------
        TypeError
            If the bank is guild-specific and no guild was specified
        """
        current_week = date.today().isocalendar()[1]
        keyword = "adventures"
        raw_accounts = await self.config.all_users()
        if guild is not None:
            tmp = raw_accounts.copy()
            for acc in tmp:
                if not guild.get_member(acc):
                    del raw_accounts[acc]
        raw_accounts_new = {}
        async for (k, v) in AsyncIter(raw_accounts.items(), steps=200):
            if "weekly_score" not in v:
                v["weekly_score"] = {keyword: 0, "rebirths": 0}

            if v["weekly_score"].get("week", -1) == current_week and keyword in v["weekly_score"]:
                user_data = {k: v["weekly_score"]}
                raw_accounts_new.update(user_data)

        sorted_acc = sorted(
            raw_accounts_new.items(), key=lambda x: (x[1].get(keyword, 0), x[1].get("rebirths", 0)), reverse=True,
        )
        if positions is None:
            return sorted_acc
        else:
            return sorted_acc[:positions]

    async def get_global_scoreboard(
        self, positions: int = None, guild: discord.Guild = None, keyword: str = None
    ) -> List[tuple]:
        """Gets the bank's leaderboard.

        Parameters
        ----------
        positions : `int`
            The number of positions to get
        guild : discord.Guild
            The guild to get the leaderboard of. If this
            is provided, get only guild members on the leaderboard

        Returns
        -------
        `list` of `tuple`
            The sorted leaderboard in the form of :code:`(user_id, raw_account)`

        Raises
        ------
        TypeError
            If the bank is guild-specific and no guild was specified
        """
        if keyword is None:
            keyword = "wins"
        raw_accounts = await self.config.all_users()
        if guild is not None:
            tmp = raw_accounts.copy()
            for acc in tmp:
                if not guild.get_member(acc):
                    del raw_accounts[acc]
        raw_accounts_new = {}
        async for (k, v) in AsyncIter(raw_accounts.items(), steps=200):
            user_data = {}
            for item in ["adventures", "rebirths"]:
                if item not in v:
                    if item == "adventures":
                        v.update({item: {keyword: 0}})
                    else:
                        v.update({item: 0})

            for (vk, vi) in v.items():
                if vk in ["rebirths"]:
                    user_data.update({vk: vi})
                elif vk in ["adventures"]:
                    for (s, sv) in vi.items():
                        if s == keyword:
                            user_data.update(vi)

            if user_data:
                user_data = {k: user_data}
            raw_accounts_new.update(user_data)

        sorted_acc = sorted(
            raw_accounts_new.items(), key=lambda x: (x[1].get(keyword, 0), x[1].get("rebirths", 0)), reverse=True,
        )
        if positions is None:
            return sorted_acc
        else:
            return sorted_acc[:positions]

    async def _roll_chest(self, chest_type: str, c: Character):
        # set rarity to chest by default
        rarity = chest_type
        if chest_type == "pet":
            rarity = "normal"
        INITIAL_MAX_ROLL = 400
        # max luck for best chest odds
        MAX_CHEST_LUCK = 200
        # lower gives you better chances for better items
        max_roll = INITIAL_MAX_ROLL - round(c.luck) - (c.rebirths // 2)
        top_range = max(max_roll, INITIAL_MAX_ROLL - MAX_CHEST_LUCK)
        roll = max(random.randint(1, top_range), 1)
        if chest_type == "normal":
            if roll <= INITIAL_MAX_ROLL * 0.05:  # 5% to roll rare
                rarity = "rare"
            else:
                pass  # 95% to roll common
        elif chest_type == "rare":
            if roll <= INITIAL_MAX_ROLL * 0.05:  # 5% to roll epic
                rarity = "epic"
            elif roll <= INITIAL_MAX_ROLL * 0.95:  # 90% to roll rare
                pass
            else:
                rarity = "normal"  # 5% to roll normal
        elif chest_type == "epic":
            if roll <= INITIAL_MAX_ROLL * 0.05:  # 5% to roll legendary
                rarity = "legendary"
            elif roll <= INITIAL_MAX_ROLL * 0.90:  # 85% to roll epic
                pass
            else:  # 10% to roll rare
                rarity = "rare"
        elif chest_type == "legendary":
            if roll <= INITIAL_MAX_ROLL * 0.75:  # 75% to roll legendary
                pass
            elif roll <= INITIAL_MAX_ROLL * 0.95:  # 20% to roll epic
                rarity = "epic"
            else:
                rarity = "rare"  # 5% to roll rare
        elif chest_type == "ascended":
            if roll <= INITIAL_MAX_ROLL * 0.55:  # 55% to roll set
                rarity = "ascended"
            else:
                rarity = "legendary"  # 45% to roll legendary
        elif chest_type == "pet":
            if roll <= INITIAL_MAX_ROLL * 0.05:  # 5% to roll legendary
                rarity = "legendary"
            elif roll <= INITIAL_MAX_ROLL * 0.15:  # 10% to roll epic
                rarity = "epic"
            elif roll <= INITIAL_MAX_ROLL * 0.57:  # 42% to roll rare
                rarity = "rare"
            else:
                rarity = "normal"  # 47% to roll common
        elif chest_type == "set":
            if roll <= INITIAL_MAX_ROLL * 0.55:  # 55% to roll set
                rarity = "set"
            elif roll <= INITIAL_MAX_ROLL * 0.87:
                rarity = "ascended"  # 45% to roll legendary
            else:
                rarity = "legendary"  # 45% to roll legendary

        return await self._genitem(rarity)

    async def _open_chests(
        self, ctx: Context, user: discord.Member, chest_type: str, amount: int, character: Character,
    ):
        items = {}
        async for i in AsyncIter(range(0, max(amount, 0))):
            item = await self._roll_chest(chest_type, character)
            item_name = str(item)
            if item_name in items:
                items[item_name].owned += 1
            else:
                items[item_name] = item
            await character.add_to_backpack(item)
        await self.config.user(ctx.author).set(await character.to_json(self.config))
        return items

    async def _open_chest(self, ctx: Context, user, chest_type, character):
        if hasattr(user, "display_name"):
            chest_msg = _("{} is opening a treasure chest. What riches lay inside?").format(
                self.escape(user.display_name)
            )
        else:
            chest_msg = _("{user}'s {f} is foraging for treasure. What will it find?").format(
                user=self.escape(ctx.author.display_name), f=(user[:1] + user[1:])
            )
        open_msg = await ctx.send(box(chest_msg, lang="css"))
        await asyncio.sleep(2)
        item = await self._roll_chest(chest_type, character)
        if chest_type == "pet" and not item:
            await open_msg.edit(
                content=box(
                    _("{c_msg}\nThe {user} found nothing of value.").format(
                        c_msg=chest_msg, user=(user[:1] + user[1:])
                    ),
                    lang="css",
                )
            )
            return None

        # Just in case old_items is empty.
        slot = item.slot[0]
        old_items = [(i, getattr(character, i, None)) for i in item.slot]
        old_stats = ""

        for num, (slot, old_item) in enumerate(old_items):
            if old_item:
                old_slot = old_item.slot[0]
                if len(old_item.slot) > 1:
                    old_slot = _("two handed")
                    att = old_item.att * 2
                    cha = old_item.cha * 2
                    intel = old_item.int * 2
                    luck = old_item.luck * 2
                    dex = old_item.dex * 2
                else:
                    att = old_item.att
                    cha = old_item.cha
                    intel = old_item.int
                    luck = old_item.luck
                    dex = old_item.dex

                    if num == 0:
                        old_stats += _("You currently have {item} [{slot}] | Lvl req {lv}").format(
                            item=old_item, slot=old_slot, lv=equip_level(character, old_item)
                        )
                        if len(old_items) == 1:
                            old_stats += " equipped."
                        else:
                            old_stats += "."
                    else:
                        # we can put equipped here because `num` be only `0` or `1`.
                        # might have to change this if that changes.
                        old_stats += _(" and {item} [{slot}] | Lvl req {lv} equipped.").format(
                            item=old_item, slot=old_slot, lv=equip_level(character, old_item)
                        )

                    old_stats += (
                        f" (RAGE: {str(att)}, "
                        f"RANT: {str(cha)}, "
                        f"ACC: {str(intel)}, "
                        f"DEX: {str(dex)}, "
                        f"LUCK: {str(luck)})"
                    )
                    if old_item.set:
                        old_stats += f" | Set `{old_item.set}` ({old_item.parts}pcs)\n"

        if len(item.slot) > 1:
            slot = _("two handed")
            att = item.att * 2
            cha = item.cha * 2
            intel = item.int * 2
            luck = item.luck * 2
            dex = item.dex * 2
        else:
            att = item.att
            cha = item.cha
            intel = item.int
            luck = item.luck
            dex = item.dex

        equip_lvl = equip_level(character, item)
        if character.lvl < equip_lvl:
            lv_str = f"[{equip_lvl}]"
        else:
            lv_str = f"{equip_lvl}"

        if hasattr(user, "display_name"):
            chest_msg2 = (
                _("{user} found {item} [{slot}] | Lvl req {lv}.").format(
                    user=self.escape(user.display_name), item=str(item), slot=slot, lv=lv_str,
                )
                + f" (RAGE: {str(att)}, "
                f"RANT: {str(cha)}, "
                f"ACC: {str(intel)}, "
                f"DEX: {str(dex)}, "
                f"LUCK: {str(luck)})"
            )
            if item.set:
                chest_msg2 += f" | Set `{item.set}` ({item.parts}pcs) "

            await open_msg.edit(
                content=box(
                    _(
                        "{c_msg}\n\n{c_msg_2}\n\nDo you want to equip "
                        "this item, put in your backpack, or sell this item?\n\n"
                        "{old_stats}"
                    ).format(c_msg=chest_msg, c_msg_2=chest_msg2, old_stats=old_stats),
                    lang="css",
                )
            )
        else:
            chest_msg2 = (
                _("The {user} found {item} [{slot}] | Lvl req {lv}.").format(
                    user=user, item=str(item), slot=slot, lv=lv_str
                )
                + f" (RAGE: {str(att)}, "
                f"RANT: {str(cha)}, "
                f"ACC: {str(intel)}, "
                f"DEX: {str(dex)}, "
                f"LUCK: {str(luck)}) "
            )
            if item.set:
                chest_msg2 += f" | Set `{item.set}` ({item.parts}pcs) "

            await open_msg.edit(
                content=box(
                    _(
                        "{c_msg}\n{c_msg_2}\nDo you want to equip "
                        "this item, put in your backpack, or sell this item?\n\n{old_stats}"
                    ).format(c_msg=chest_msg, c_msg_2=chest_msg2, old_stats=old_stats),
                    lang="css",
                )
            )

        start_adding_reactions(open_msg, self._treasure_controls.keys())
        if hasattr(user, "id"):
            pred = ReactionPredicate.with_emojis(tuple(self._treasure_controls.keys()), open_msg, user)
        else:
            pred = ReactionPredicate.with_emojis(tuple(self._treasure_controls.keys()), open_msg, ctx.author)
        try:
            react, user = await self.bot.wait_for("reaction_add", check=pred, timeout=60)
        except asyncio.TimeoutError:
            await self._clear_react(open_msg)
            await character.add_to_backpack(item)
            await open_msg.edit(
                content=(
                    box(
                        _("{user} put the {item} into their backpack.").format(
                            user=self.escape(ctx.author.display_name), item=item
                        ),
                        lang="css",
                    )
                )
            )
            await self.config.user(ctx.author).set(await character.to_json(self.config))
            return
        await self._clear_react(open_msg)
        if self._treasure_controls[react.emoji] == "sell":
            price = self._sell(character, item)
            price = max(price, 0)
            if price > 0:
                try:
                    await bank.deposit_credits(ctx.author, price)
                except BalanceTooHigh as e:
                    await bank.set_balance(ctx.author, e.max_balance)
            currency_name = await bank.get_currency_name(ctx.guild,)
            if str(currency_name).startswith("<"):
                currency_name = "credits"
            await open_msg.edit(
                content=(
                    box(
                        _("{user} sold the {item} for {price} {currency_name}.").format(
                            user=self.escape(ctx.author.display_name),
                            item=item,
                            price=humanize_number(price),
                            currency_name=currency_name,
                        ),
                        lang="css",
                    )
                )
            )
            await self._clear_react(open_msg)
            character.last_known_currency = await bank.get_balance(ctx.author)
            character.last_currency_check = time.time()
            await self.config.user(ctx.author).set(await character.to_json(self.config))
            return
        elif self._treasure_controls[react.emoji] == "equip":
            equiplevel = equip_level(character, item)
            if self.is_dev(ctx.author):
                equiplevel = 0
            if not can_equip(character, item):
                await character.add_to_backpack(item)
                await self.config.user(ctx.author).set(await character.to_json(self.config))
                return await smart_embed(
                    ctx,
                    f"**{self.escape(ctx.author.display_name)}**, you need to be level "
                    f"`{equiplevel}` to equip this item. I've put it in your backpack.",
                )
            if not getattr(character, item.slot[0]):
                equip_msg = box(
                    _("{user} equipped {item} ({slot} slot).").format(
                        user=self.escape(ctx.author.display_name), item=item, slot=slot
                    ),
                    lang="css",
                )
            else:
                equip_msg = box(
                    _("{user} equipped {item} ({slot} slot) and put {old_items} into their backpack.").format(
                        user=self.escape(ctx.author.display_name),
                        item=item,
                        slot=slot,
                        old_items=" and ".join(
                            str(getattr(character, i)) for i in item.slot if getattr(character, i, None)
                        ),
                    ),
                    lang="css",
                )
            await open_msg.edit(content=equip_msg)
            character = await character.equip_item(item, False, self.is_dev(ctx.author))
            await self.config.user(ctx.author).set(await character.to_json(self.config))
            return
        else:
            await character.add_to_backpack(item)
            await open_msg.edit(
                content=(
                    box(
                        _("{user} put the {item} into their backpack.").format(
                            user=self.escape(ctx.author.display_name), item=item
                        ),
                        lang="css",
                    )
                )
            )
            await self._clear_react(open_msg)
            await self.config.user(ctx.author).set(await character.to_json(self.config))
            return

    @staticmethod
    async def _remaining(epoch):
        remaining = epoch - time.time()
        finish = remaining < 0
        m, s = divmod(remaining, 60)
        h, m = divmod(m, 60)
        s = int(s)
        m = int(m)
        h = int(h)
        if h == 0 and m == 0:
            out = "{:02d}".format(s)
        elif h == 0:
            out = "{:02d}:{:02d}".format(m, s)
        else:
            out = "{:01d}:{:02d}:{:02d}".format(h, m, s)
        return (out, finish, remaining)

    async def _reward(self, ctx: Context, userlist, amount, modif, special):
        if modif == 0:
            modif = 0.5
        daymult = self._daily_bonus.get(str(datetime.today().weekday()), 0)
        xp = max(1, round(amount))
        cp = max(1, round(amount))
        newxp = 0
        newcp = 0
        rewards_list = []
        phrase = ""
        async for user in AsyncIter(userlist):
            self._rewards[user.id] = {}
            c = await self.get_character_from_json(user)
            userxp = int(xp + (xp * 0.5 * c.rebirths) + (xp * 0.1 * min(250, c.total_int / 10)))
            # This got exponentially out of control before checking 1 skill
            # To the point where you can spec into only INT and
            # Reach level 1000 in a matter of days
            usercp = int(cp + (cp * c.luck) // 2)
            userxp = int(userxp * (c.gear_set_bonus.get("xpmult", 1) + daymult))
            usercp = int(usercp * (c.gear_set_bonus.get("cpmult", 1) + daymult))
            newxp += userxp
            newcp += usercp
            roll = random.randint(1, 5)
            if c.heroclass.get("pet", {}).get("bonuses", {}).get("always", False):
                roll = 5
            if roll == 5 and c.heroclass["name"] == "Ranger" and c.heroclass["pet"]:
                petxp = int(userxp * c.heroclass["pet"]["bonus"])
                newxp += petxp
                userxp += petxp
                self._rewards[user.id]["xp"] = userxp
                petcp = int(usercp * c.heroclass["pet"]["bonus"])
                newcp += petcp
                usercp += petcp
                self._rewards[user.id]["cp"] = usercp + petcp
                percent = round((c.heroclass["pet"]["bonus"] - 1.0) * 100)
                phrase += _("\n**{user}** received a **{percent}%** reward bonus from their {pet_name}.").format(
                    user=self.escape(user.display_name), percent=str(percent), pet_name=c.heroclass["pet"]["name"],
                )

            else:
                self._rewards[user.id]["xp"] = userxp
                self._rewards[user.id]["cp"] = usercp
            if special is not False:
                self._rewards[user.id]["special"] = special
            else:
                self._rewards[user.id]["special"] = False
            rewards_list.append(f"**{self.escape(user.display_name)}**")

        currency_name = await bank.get_currency_name(ctx.guild,)
        to_reward = " and ".join(
            [", ".join(rewards_list[:-1]), rewards_list[-1]] if len(rewards_list) > 2 else rewards_list
        )

        if int(newcp) > 0:
            newcp = f' {humanize_number(int(newcp))}'
        else:
            newcp = ''

        word = "has" if len(userlist) == 1 else "have"
        if special is not False and sum(special) == 1:
            types = [" normal", " rare", "n epic", " legendary", " ascended", " set"]
            chest_type = types[special.index(1)]
            phrase += _(
                "\n{b_reward} {word} been awarded {xp} xp and found"
                "{cp} {currency_name} (split based on stats). "
                "You also secured **a{chest_type} treasure chest**!"
            ).format(
                b_reward=to_reward,
                word=word,
                xp=humanize_number(int(newxp)),
                cp=newcp,
                currency_name=currency_name,
                chest_type=chest_type,
            )
        elif special is not False and sum(special) > 1:
            phrase += _(
                "\n{b_reward} {word} been awarded {xp} xp and found{cp} {currency_name} (split based on stats). "
                "You also secured **several treasure chests**!"
            ).format(
                b_reward=to_reward,
                word=word,
                xp=humanize_number(int(newxp)),
                cp=newcp,
                currency_name=currency_name,
            )
        else:
            phrase += _(
                "\n{b_reward} {word} been awarded {xp} xp and found{cp} {currency_name} (split based on stats)."
            ).format(
                b_reward=to_reward,
                word=word,
                xp=humanize_number(int(newxp)),
                cp=newcp,
                currency_name=currency_name,
            )
        return phrase

    @staticmethod
    def _sell(c: Character, item: Item, *, amount: int = 1):
        if item.rarity == "ascended":
            base = (5000, 10000)
        elif item.rarity == "legendary":
            base = (1000, 2000)
        elif item.rarity == "epic":
            base = (500, 750)
        elif item.rarity == "rare":
            base = (250, 500)
        else:
            base = (10, 100)
        price = random.randint(base[0], base[1]) * abs(item.max_main_stat)
        price += price * int((c.total_cha) / 1000)

        if c.luck > 0:
            price = price + round(price * (c.luck / 1000))
        if c.luck < 0:
            price = price - round(price * (abs(c.luck) / 1000))
            if price < 0:
                price = 0
        price += round(price * min(0.1 * c.rebirths / 15, 0.4))

        return max(price, base[0])

    async def _trader(self, ctx: Context, bypass=False):
        em_list = ReactionPredicate.NUMBER_EMOJIS

        cart = await self.config.cart_name()
        if await self.config.guild(ctx.guild).cart_name():
            cart = await self.config.guild(ctx.guild).cart_name()
        text = box(_("[{} is bringing the cart around!]").format(cart), lang="css")
        timeout = await self.config.guild(ctx.guild).cart_timeout()
        if ctx.guild.id not in self._last_trade:
            self._last_trade[ctx.guild.id] = 0

        if not bypass:
            if self._last_trade[ctx.guild.id] == 0:
                self._last_trade[ctx.guild.id] = time.time()
            elif self._last_trade[ctx.guild.id] >= time.time() - timeout:
                # trader can return after 3 hours have passed since last visit.
                return  # silent return.
        self._last_trade[ctx.guild.id] = time.time()

        room = await self.config.guild(ctx.guild).cartroom()
        if room:
            room = ctx.guild.get_channel(room)
        if room is None or bypass:
            room = ctx
        self.bot.dispatch("adventure_cart", ctx)  # dispatch after silent return
        stockcount = random.randint(3, 9)
        controls = {em_list[i + 1]: i for i in range(stockcount)}
        self._curent_trader_stock[ctx.guild.id] = (stockcount, controls)

        stock = await self._trader_get_items(stockcount)
        currency_name = await bank.get_currency_name(ctx.guild,)
        if str(currency_name).startswith("<"):
            currency_name = "credits"
        for (index, item) in enumerate(stock):
            item = stock[index]
            if len(item["item"].slot) == 2:  # two handed weapons add their bonuses twice
                hand = "two handed"
                rage = item["item"].att * 2
                rant = item["item"].cha * 2
                acc = item["item"].int * 2
                luck = item["item"].luck * 2
                dex = item["item"].dex * 2
            else:
                if item["item"].slot[0] == "right" or item["item"].slot[0] == "left":
                    hand = item["item"].slot[0] + _(" handed")
                else:
                    hand = item["item"].slot[0] + _(" slot")
                rage = item["item"].att
                rant = item["item"].cha
                acc = item["item"].int
                luck = item["item"].luck
                dex = item["item"].dex
            text += box(
                _(
                    "\n[{i}] Lvl req {lvl} | {item_name} ("
                    "Rage: {str_rage}, "
                    "Rant: {str_rant}, "
                    "Accuracy: {str_acc}, "
                    "Dexterity: {str_dex}, "
                    "Luck: {str_luck} "
                    "[{hand}]) for {item_price} {currency_name}."
                ).format(
                    i=str(index + 1),
                    item_name=item["item"].formatted_name,
                    lvl=item["item"].lvl,
                    str_rage=str(rage),
                    str_acc=str(acc),
                    str_rant=str(rant),
                    str_luck=str(luck),
                    str_dex=str(dex),
                    hand=hand,
                    item_price=humanize_number(item["price"]),
                    currency_name=currency_name,
                ),
                lang="css",
            )
        text += _("Do you want to buy any of these fine items? Tell me which one below:")
        msg = await room.send(text)
        start_adding_reactions(msg, controls.keys())
        self._current_traders[ctx.guild.id] = {"msg": msg.id, "stock": stock, "users": []}
        timeout = self._last_trade[ctx.guild.id] + 180 - time.time()
        if timeout <= 0:
            timeout = 0
        timer = await self._cart_countdown(ctx, timeout, _("The cart will leave in: "), room=room)
        self.tasks[msg.id] = timer
        try:
            await asyncio.wait_for(timer, timeout + 5)
        except asyncio.TimeoutError:
            await self._clear_react(msg)
            return
        with contextlib.suppress(discord.HTTPException):
            await msg.delete()

    async def _trader_get_items(self, howmany: int):
        items = {}
        output = {}

        while len(items) < howmany:
            rarity_roll = random.random()
            #  rarity_roll = .9
            # 1% legendary
            if rarity_roll >= 0.95:
                item = await self._genitem("legendary")
                # min. 10 stat for legendary, want to be about 50k
                price = random.randint(2500, 5000)
            # 20% epic
            elif rarity_roll >= 0.7:
                item = await self._genitem("epic")
                # min. 5 stat for epic, want to be about 25k
                price = random.randint(1000, 2000)
            # 35% rare
            elif rarity_roll >= 0.35:
                item = await self._genitem("rare")
                # around 3 stat for rare, want to be about 3k
                price = random.randint(500, 1000)
            else:
                item = await self._genitem("normal")
                # 1 stat for normal, want to be <1k
                price = random.randint(100, 500)
            # 35% normal
            price *= item.max_main_stat

            items.update({item.name: {"itemname": item.name, "item": item, "price": price, "lvl": item.lvl}})

        for (index, item) in enumerate(items):
            output.update({index: items[item]})
        return output

    @staticmethod
    def escape(t: str) -> str:
        return escape(filter_various_mentions(t), mass_mentions=True, formatting=True)

    @staticmethod
    async def _clear_react(msg):
        with contextlib.suppress(discord.HTTPException):
            await msg.clear_reactions()

    async def _data_check(self, ctx: Context):
        try:
            self._adventure_countdown[ctx.channel.id]
        except KeyError:
            self._adventure_countdown[ctx.channel.id] = 0
        try:
            self._rewards[ctx.author.id]
        except KeyError:
            self._rewards[ctx.author.id] = {}
        try:
            self._trader_countdown[ctx.guild.id]
        except KeyError:
            self._trader_countdown[ctx.guild.id] = 0

    @staticmethod
    async def _get_epoch(seconds: int):
        epoch = time.time()
        epoch += seconds
        return epoch

    @staticmethod
    async def _title_case(phrase: str):
        exceptions = ["a", "and", "in", "of", "or", "the"]
        lowercase_words = re.split(" ", phrase.lower())
        final_words = [lowercase_words[0].capitalize()]
        final_words += [word if word in exceptions else word.capitalize() for word in lowercase_words[1:]]
        return " ".join(final_words)

    async def cog_check(self, ctx: Context):
        await self._ready_event.wait()

        if self.maintenance and not await ctx.bot.is_owner(ctx.author):
            raise AdventureCheckFailure("The bot is currently under maintenance.")

        if ctx.author.id in self.locks and self.locks[ctx.author.id].locked():
            raise AdventureCheckFailure(f"Another operation is currently executing for {ctx.author.mention}. Try again later.")

        if ctx.guild:
            guild_perms = self.PERMS.get(str(ctx.guild.id), {})
            if self.__class__.__name__ in guild_perms.get("cog", {}):
                perms_data = copy.copy(guild_perms["cog"][self.__class__.__name__])
            else:
                perms_data = {}

            if ctx.command.qualified_name in guild_perms.get("command", {}):
                perms_data.update(guild_perms["command"][ctx.command.qualified_name])

            # override if owner
            if not await ctx.bot.is_owner(ctx.author):
                # 3 things, user, role, channel
                for i in ctx.author.roles:
                    if not perms_data.get(str(i.id), True):
                        raise AdventureCheckFailure(_("You are not allowed to use this command."))

                if not perms_data.get(str(ctx.author.id), True):
                    raise AdventureCheckFailure(_("You are not allowed to use this command."))

                default = perms_data.get('default', True)
                if not perms_data.get(str(ctx.channel.id), default):
                    channels = [ctx.bot.get_channel(int(x)).mention for x in perms_data if x.isdigit() and perms_data[x] and ctx.bot.get_channel(int(x))]
                    raise AdventureCheckFailure(
                        _("Try this in {channels}.").format(channels=', '.join(channels)),
                        reset_cooldown=False
                    )

        if not await self.allow_in_dm(ctx):
            raise AdventureCheckFailure(_("This command is not available in DM's on this bot."), reset_cooldown=False)

        return True

    @commands.group(name="errorch")
    @commands.is_owner()
    @commands.guild_only()
    async def _errorch(self, ctx: Context):
        """Configure channel for logging all adventure errors."""

    @_errorch.command(name="show", aliases=["get"])
    async def _errorch_show(self, ctx: Context):
        """Shows channel set for logging all adventure errors."""

        channel_id = await self.config.guild(ctx.guild).error_channel()
        if not channel_id:
            return await ctx.send("Error channel not set.")

        channel = self.bot.get_channel(channel_id)
        if not channel:
            return await ctx.send(f"No channel found with ID: {channel_id}")

        await ctx.send(f"Error channel is set to {channel.mention}.")

    @_errorch.command(name="set")
    async def _errorch_set(self, ctx: Context, channel: discord.TextChannel):
        """Sets channel for logging all adventure errors."""

        await self.config.guild(ctx.guild).error_channel.set(channel.id)

        await ctx.send(f"Set error channel to {channel.mention}.")

    @_errorch.command(name="clear")
    async def _errorch_clear(self, ctx: Context):
        """Clears channel set for logging all adventure errors."""

        await self.config.guild(ctx.guild).error_channel.clear()

        await ctx.send("Cleared error channel.")

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        """This will be a cog level reaction_add listener for game logic."""
        await self.bot.wait_until_ready()
        if user.bot:
            return
        try:
            channel = reaction.message.channel
            guild = user.guild
        except AttributeError:
            return
        emojis = list(ReactionPredicate.NUMBER_EMOJIS) + self._adventure_actions_emoji_names
        if str(reaction.emoji) not in emojis:
            return
        if not await self.has_perm(user):
            return
        if channel.id in self._sessions:
            if reaction.message.id == self._sessions[channel.id].message_id:
                if channel.id in self._adventure_countdown:
                    (timer, done, sremain) = self._adventure_countdown[channel.id]
                    if sremain > 0:
                        await self._handle_adventure(reaction, user)
        if guild.id in self._current_traders:
            if reaction.message.id == self._current_traders[guild.id]["msg"]:
                if user in self._current_traders[guild.id]["users"]:
                    return
                if guild.id in self._trader_countdown:
                    (timer, done, sremain) = self._trader_countdown[guild.id]
                    if sremain > 0:
                        await self._handle_cart(reaction, user)

    @commands.Cog.listener()
    async def on_reaction_remove(self, reaction, user):
        """This will be a cog level reaction_add listener for game logic."""
        await self.bot.wait_until_ready()
        if user.bot:
            return
        try:
            channel = reaction.message.channel
        except AttributeError:
            return
        emojis = list(ReactionPredicate.NUMBER_EMOJIS) + self._adventure_actions_emoji_names
        if str(reaction.emoji) not in emojis:
            return
        if not await self.has_perm(user):
            return
        if channel.id in self._sessions:
            if reaction.message.id == self._sessions[channel.id].message_id:
                if channel.id in self._adventure_countdown:
                    (timer, done, sremain) = self._adventure_countdown[channel.id]
                    if sremain > 0:
                        session = self._sessions[channel.id]
                        if user in session.reactors:
                            session.run.add(user)

    @commands.Cog.listener()
    async def on_message_without_command(self, message):
        await self._ready_event.wait()
        if not message.guild:
            return
        channels = await self.config.guild(message.guild).cart_channels()
        if not channels:
            return
        if message.channel.id not in channels:
            return
        if message.channel.id in self._sessions:
            return
        if not message.author.bot:
            roll = random.randint(1, 20)
            if roll == 20:
                try:
                    self._last_trade[message.guild.id]
                except KeyError:
                    self._last_trade[message.guild.id] = 0
                ctx = await self.bot.get_context(message)
                await asyncio.sleep(5)
                await self._trader(ctx)

    async def cog_command_error(self, ctx: Context, error: Exception):
        if isinstance(error, commands.CommandOnCooldown):
            error = AdventureOnCooldown(retry_after=error.retry_after)

        if isinstance(error, AdventureOnCooldown):
            await smart_embed(ctx, str(error), success=False, delete_after=error.retry_after)
            await asyncio.sleep(error.retry_after)
            await ctx.tick()

        elif isinstance(error, AdventureCheckFailure):
            if error.reset_cooldown:
                ctx.command.reset_cooldown(ctx)
            await smart_embed(ctx, str(error), reference=error.reply, success=False, delete_after=15)
            await asyncio.sleep(15)
            with contextlib.suppress(discord.HTTPException):
                await ctx.message.delete()
        else:
            if ctx.guild:
                dest_id = await self.config.guild(ctx.guild).error_channel()
                dest = self.bot.get_channel(dest_id)

                if dest:
                    cmd_name = ctx.command.qualified_name
                    embed = discord.Embed(
                        title=f"Exception in command `{cmd_name}`",
                        description=f"[Jump to message]({ctx.message.jump_url})",
                        timestamp=datetime.utcnow()
                    )

                    embed.add_field(name="Invoker", value=f"{ctx.author.mention} {ctx.author}")
                    embed.add_field(name="Content", value=f"{ctx.message.content}")
                    embed.add_field(name="Channel", value=f"{ctx.channel.mention} ({ctx.channel.name})")
                    embed.add_field(name="Server", value=f"{ctx.guild.name}")

                    await dest.send(embed=embed)

                    exception_log = "Exception in command '{}'\n" "".format(cmd_name)
                    exception_log += "".join(
                        traceback.format_exception(type(error), error, error.__traceback__)
                    )

                    for page in pagify(exception_log, shorten_by=10):
                        await dest.send(box(page, lang="py"))

            await ctx.bot.on_command_error(
                ctx, getattr(error, "original", error), unhandled_by_cog=True
            )

    def cog_unload(self):
        if self.cleanup_loop:
            self.cleanup_loop.cancel()
        if self._init_task:
            self._init_task.cancel()
        if self.gb_task:
            self.gb_task.cancel()
        if self._timed_roles_task:
            self._timed_roles_task.cancel()

        for (msg_id, task) in self.tasks.items():
            task.cancel()

        for lock in self.locks.values():
            with contextlib.suppress(Exception):
                lock.release()

        with open(cog_data_path(self) / "sessions.pickle", "wb+") as f:
            pickle.dump(self._sessions, f)

        with open(cog_data_path(self) / "results.pickle", "wb+") as f:
            pickle.dump(self._adv_results, f)

    async def _garbage_collection(self):
        await self.bot.wait_until_red_ready()
        delta = timedelta(minutes=6)
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                async for channel_id, session in AsyncIter(self._sessions.copy(), steps=5):
                    if session.start_time + delta > datetime.now():
                        if channel_id in self._sessions:
                            del self._sessions[channel_id]
                await asyncio.sleep(5)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: Context, error: Exception):
        if isinstance(error, commands.CommandNotFound):
            # case insentivity
            command = ctx.bot.get_command(ctx.invoked_with.lower())
            if command and command.cog == self:
                ctx.message.content = ctx.message.content.lower()
                await self.bot.process_commands(ctx.message)

    def display_item(self, item: Item, character: Character, equipped=False) -> str:
        """Returns a formatted string to display item's stats based on the provided character."""

        slot = item.slot[0]

        if len(item.slot) > 1:
            slot = _("two handed")
            att = item.att * 2
            cha = item.cha * 2
            intel = item.int * 2
            luck = item.luck * 2
            dex = item.dex * 2
        else:
            att = item.att
            cha = item.cha
            intel = item.int
            luck = item.luck
            dex = item.dex

        lvl = equip_level(character, item)
        if lvl > character.lvl:
            lvl = f'[{lvl}]'

        msg = (
            _("{item} [{slot}] | Lvl req {lv}{equipped}").format(
                item=str(item), slot=slot, lv=lvl,
                equipped=_(" | Equipped") if equipped else ""
            )
            + f"\n\nATT: {str(att)}, "
            f"CHA: {str(cha)}, "
            f"INT: {str(intel)}, "
            f"DEX: {str(dex)}, "
            f"LUCK: {str(luck)}"
        )

        return msg

    async def _to_forge(self, ctx: commands.Context, consumed, character):
        item1 = consumed[0]
        item2 = consumed[1]

        roll = random.randint(1, 20)
        modifier = (roll / 20) + 0.3
        base_cha = max(character._cha, 1)
        base_int = character._int
        base_luck = character._luck
        base_att = max(character._att, 1)
        modifier_bonus_luck = 0.01 * base_luck // 10
        modifier_bonus_int = 0.01 * base_int // 20
        modifier_penalty_str = -0.01 * base_att // 20
        modifier_penalty_cha = -0.01 * base_cha // 10
        modifier = sum([modifier_bonus_int, modifier_bonus_luck, modifier_penalty_cha, modifier_penalty_str, modifier])
        modifier = max(0.001, modifier)

        base_int = int(item1.int) + int(item2.int)
        base_cha = int(item1.cha) + int(item2.cha)
        base_att = int(item1.att) + int(item2.att)
        base_dex = int(item1.dex) + int(item2.dex)
        base_luck = int(item1.luck) + int(item2.luck)
        newatt = int((base_att * modifier) + base_att)
        newdip = int((base_cha * modifier) + base_cha)
        newint = int((base_int * modifier) + base_int)
        newdex = int((base_dex * modifier) + base_dex)
        newluck = int((base_luck * modifier) + base_luck)
        newslot = random.choice(ORDER)
        if newslot == "two handed":
            newslot = ["right", "left"]
        else:
            newslot = [newslot]
        if len(newslot) == 2:  # two handed weapons add their bonuses twice
            hand = "two handed"
        else:
            if newslot[0] == "right" or newslot[0] == "left":
                hand = newslot[0] + " handed"
            else:
                hand = newslot[0] + " slot"
        if len(newslot) == 2:
            two_handed_msg = box(
                _(
                    "{author}, your forging roll was {dice}({roll}).\n"
                    "The device you tinkered will have "
                    "(RAG {new_att} | "
                    "RAN {new_cha} | "
                    "ACC {new_int} | "
                    "DEX {new_dex} | "
                    "LUCK {new_luck})"
                    " and be {hand}."
                ).format(
                    author=self.escape(ctx.author.display_name),
                    roll=roll,
                    dice=self.emojis.dice,
                    new_att=(newatt * 2),
                    new_cha=(newdip * 2),
                    new_int=(newint * 2),
                    new_dex=(newdex * 2),
                    new_luck=(newluck * 2),
                    hand=hand,
                ),
                lang="css",
            )
            await ctx.send(two_handed_msg)
        else:
            reg_item = box(
                _(
                    "{author}, your forging roll was {dice}({roll}).\n"
                    "The device you tinkered will have "
                    "(RAG {new_att} | "
                    "RAN {new_dip} | "
                    "ACC {new_int} | "
                    "DEX {new_dex} | "
                    "LUCK {new_luck})"
                    " and be {hand}."
                ).format(
                    author=self.escape(ctx.author.display_name),
                    roll=roll,
                    dice=self.emojis.dice,
                    new_att=newatt,
                    new_dip=newdip,
                    new_int=newint,
                    new_dex=newdex,
                    new_luck=newluck,
                    hand=hand,
                ),
                lang="css",
            )
            await ctx.send(reg_item)
        get_name = _(
            "**{}**, please respond with "
            "a name for your creation within 30s.\n"
            "(You will not be able to change it afterwards. 40 characters maximum.)"
        ).format(self.escape(ctx.author.display_name))
        await smart_embed(ctx, get_name)
        reply = None
        name = _("Unnamed Artifact")
        try:
            reply = await ctx.bot.wait_for("message", check=MessagePredicate.same_context(user=ctx.author), timeout=30)
        except asyncio.TimeoutError:
            name = _("Unnamed Artifact")
        if reply is None:
            name = _("Unnamed Artifact")
        else:
            if hasattr(reply, "content"):
                if len(reply.content) > 40:
                    name = _("Long-winded Artifact")
                else:
                    name = reply.content.lower()
        item = {
            name: {
                "slot": newslot,
                "att": newatt,
                "cha": newdip,
                "int": newint,
                "dex": newdex,
                "luck": newluck,
                "rarity": "forged",
            }
        }
        item = Item.from_json(item)
        return item
