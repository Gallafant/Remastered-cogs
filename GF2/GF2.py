import discord
import asyncio
from sys import argv
from discord.ext import commands
from .utils.dataIO import dataIO
import os


class GF2:

    def __init__(self, bot):
        self.bot = bot
        self.settings = dataIO.load_json("data/GF2/settings.json")

    def savefile(self):
        dataIO.save_json("data/GF2/settings.json", self.settings)

    @commands.group(pass_context=True)
    async def GF2(self, ctx):
        if ctx.invoked_subcommand is None:
            await self.bot.send_cmd_help(ctx)

    @GF2.command(pass_context=True, name="set", aliases=["setup"])
    async def _set(self, ctx, channel:discord.Channel, role:discord.Role=None):
        """Setup the GF2 channel and role to be applied"""
        if channel is None:
            channel = ctx.message.channel
        role_id = None
        if role is not None:
            role_id = role.id
        self.settings[ctx.message.server.id] = {"GF2": "Welcome! Please react with 🇾 to accept the GF2.", "channel": channel.id,
        "role": role_id}
        self.savefile()
        await self.bot.say("GF2 set to {} in channel {} applying role {}".format(self.settings[ctx.message.server.id]["GF2"], channel.mention, role))

    @GF2.command(pass_context=True)
    async def channel(self, ctx, channel: discord.Channel):
        """Set the GF2 channel to be accepted in"""
        server = ctx.message.server
        if server.id not in self.settings:
            await self.bot.say("Please use the GF2 set command to change the GF2 channel")
            return
        self.settings[ctx.message.server.id]["channel"] = channel.id
        await self.bot.say("Channel changed to {}".format(channel.mention))
        self.savefile()

    @GF2.command(pass_context=True)
    async def change(self, ctx, *, message):
        """Set the GF2 message to be sent"""
        if ctx.message.server.id not in self.settings:
            await self.bot.say("Please use the GF2 set command to change the GF2 message")
            return
        self.settings[ctx.message.server.id]["GF2"] = message
        await self.bot.say("Message changed to {}".format(message))
        self.savefile()

    @GF2.command(pass_context=True)
    async def role(self, ctx, role:discord.Role):
        """Set the role to be applied when GF2 are accepted"""
        if ctx.message.server.id not in self.settings:
            await self.bot.say("Please use the GF2 set command to change the role added")
            return
        self.settings[ctx.message.server.id]["role"] = role.id
        await self.bot.say("Role changed to {}".format(role.name))
        self.savefile()


    async def on_member_join(self, member):
        server = member.server
        if server.id not in self.settings:
            return
        channel = discord.Object(id=self.settings[server.id]["channel"])
        message = await self.bot.send_message(channel, "{} {}".format(member.mention, self.settings[server.id]["GF2"]))
        role_id = self.settings[server.id]["role"]
        if role_id is None:
            return
        try:
            role = [role for role in server.roles if role.id == role_id][0]
        except:
            print("{} Role does not exist to be applied to the user.".format(self.settings[server.id]["role"]))
            return

        await self.bot.add_reaction(message, "🇾")
        await self.bot.add_reaction(message, "🇳")
        answer = await self.bot.wait_for_reaction(emoji=["🇾", "🇳"], user=member, message=message)
        if answer.reaction.emoji == "🇾":
            await self.bot.replace_roles(member, role)
            await self.bot.delete_message(message)
        if answer.reaction.emoji == "🇳":
            await self.bot.kick(member)
            await self.bot.delete_message(message)

def check_folder():
    if not os.path.exists("data/GF2"):
        print("Creating data/GF2 folder")
        os.makedirs("data/GF2")


def check_file():
    data = {}
    f = "data/GF2/settings.json"
    if not dataIO.is_valid_json(f):
        print("Creating default settings.json...")
        dataIO.save_json(f, data)


def setup(bot):
    check_folder()
    check_file()
    n = GF2(bot)
    bot.add_cog(n)
