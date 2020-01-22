# -*- coding: utf-8 -*-
from .adventure import Adventure


async def setup(bot):
    bot.add_cog(Adventure(bot))
