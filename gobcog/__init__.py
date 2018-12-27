from .gobcog import GobCog


async def setup(bot):
    bot.add_cog(GobCog(bot))
