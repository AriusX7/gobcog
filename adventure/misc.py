import traceback
from datetime import datetime

import discord

from redbot.core import commands, Config
from redbot.core.bot import Red
from redbot.core.commands import Context
from redbot.core.utils.chat_formatting import box, pagify

class MiscMixin(commands.Cog):
    def __init__(self, bot: Red) -> None:
        self.bot = bot

        self.config: Config

    @commands.group(name="errorch")
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

        await ctx.send(f"Cleared error channel.")

    async def cog_command_error(self, ctx: Context, error: Exception):
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
