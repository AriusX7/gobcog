from .adventure import Adventure


async def setup(bot):
    cog = Adventure(bot)
    await cog.initialize()
    bot.add_cog(cog)
