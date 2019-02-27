from redbot.core.bot import Red

from .GSR import GSR


def setup(bot: Red):
  bot.add_cog(GSR())
