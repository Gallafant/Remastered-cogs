import asyncio
import os
from datetime import datetime, timedelta

import discord
from discord.ext import commands

from cogs.utils import checks
from cogs.utils.chat_formatting import pagify
from cogs.utils.dataIO import dataIO


class GTR1:
    """Add roles to users based on time on server"""

    def __init__(self, bot):
        self.bot = bot
        self.path = "data/GTR/GTR1"
        self.file_path = "data/GTR/GTR1/GTR1.json"
        self.the_data = dataIO.load_json(self.file_path)

    def save_data(self):
        """Saves the json"""
        dataIO.save_json(self.file_path, self.the_data)

    @commands.command(pass_context=True, no_pm=True)
    @checks.is_owner()
    async def runGTR1(self, ctx):
        """Trigger the daily GTR1"""

        await self.GTR1_update()
        await self.bot.say("Success")

    @commands.group(pass_context=True, no_pm=True)
    @checks.mod_or_permissions(administrator=True)
    async def GTR1(self, ctx):
        """Adjust GTR1 settings"""

        if ctx.invoked_subcommand is None:
            await self.bot.send_cmd_help(ctx)

    @GTR1.command(pass_context=True, no_pm=True)
    async def addrole(self, ctx, role: discord.Role, days: int, *requiredroles: discord.Role):
        """Add a role to be added after specified time on server"""
        server = ctx.message.server
        if server.id not in self.the_data:
            self.the_data[server.id] = {}
            self.save_data()

        if 'ROLES' not in self.the_data[server.id]:
            self.the_data[server.id]['ROLES'] = {}

        self.the_data[server.id]['ROLES'][role.id] = {'DAYS': days}

        if requiredroles:
            self.the_data[server.id]['ROLES'][role.id]['REQUIRED'] = [r.id for r in requiredroles]

        self.save_data()
        await self.bot.say("Time Role for {0} set to {1} days".format(role.name, days))

    @GTR1.command(pass_context=True, no_pm=True)
    async def channel(self, ctx, channel: discord.Channel):
        """Sets the announce channel for role adds"""
        server = ctx.message.server
        if server.id not in self.the_data:
            self.the_data[server.id] = {}
            self.save_data()

        self.the_data[server.id]['ANNOUNCE'] = channel.id

        self.save_data()
        await self.bot.say("Announce channel set to {0}".format(channel.mention))

    @GTR1.command(pass_context=True, no_pm=True)
    async def removerole(self, ctx, role: discord.Role):
        """Removes a role from being added after specified time"""
        server = ctx.message.server
        if server.id not in self.the_data:
            self.the_data[server.id] = {}
            self.save_data()

        self.the_data[server.id]['ROLES'].pop(role.id, None)

        self.save_data()
        await self.bot.say("{0} will no longer be applied".format(role.name))

    async def GTR1_update(self):
        for server in self.bot.servers:
            print("In server {}".format(server.name))
            addlist = []
            if server.id not in self.the_data:  # Hasn't been configured
                print("Not configured")
                continue

            if 'ROLES' not in self.the_data[server.id]:  # No roles
                print("No roles")
                continue

            for member in server.members:
                has_roles = [r.id for r in member.roles]

                get_roles = [rID for rID in self.the_data[server.id]['ROLES']]

                check_roles = set(get_roles) - set(has_roles)

                print("{} is being checked for {}".format(member.display_name, list(check_roles)))

                for role_id in check_roles:
                    # Check for required role
                    if 'REQUIRED' in self.the_data[server.id]['ROLES'][role_id]:
                        if not set(self.the_data[server.id]['ROLES'][role_id]['REQUIRED']) & set(has_roles):
                            print("Doesn't have required role")
                            continue

                    if member.joined_at + timedelta(
                            days=self.the_data[server.id]['ROLES'][role_id]['DAYS']) <= datetime.today():
                        print("Qualifies")
                        addlist.append((member, role_id))
                    print("Out")
            channel = None
            if "ANNOUNCE" in self.the_data[server.id]:
                channel = server.get_channel(self.the_data[server.id]["ANNOUNCE"])

            title = "**These members have received the following roles**\n"
            results = ""
            for member, role_id in addlist:
                role = discord.utils.get(server.roles, id=role_id)
                await self.bot.add_roles(member, role)
                results += "{} : {}\n".format(member.display_name, role.name)

            if channel and results:
                await self.bot.send_message(channel, title)
                for page in pagify(
                        results, shorten_by=50):
                    await self.bot.send_message(channel, page)

            print(title + results)

    async def check_day(self):
        while self is self.bot.get_cog("GTR1"):
            tomorrow = datetime.now() + timedelta(days=1)
            midnight = datetime(year=tomorrow.year, month=tomorrow.month,
                                day=tomorrow.day, hour=0, minute=0, second=0)

            await asyncio.sleep((midnight - datetime.now()).seconds)

            await self.GTR1_update()

            await asyncio.sleep(3)
            # then start loop over again


def check_folders():
    if not os.path.exists("data/GTR"):
        print("Creating data/GTR folder...")
        os.makedirs("data/GTR")

    if not os.path.exists("data/GTR/GTR1"):
        print("Creating data/GTR/GTR1 folder...")
        os.makedirs("data/GTR/GTR1")


def check_files():
    if not dataIO.is_valid_json("data/GTR/GTR1/GTR1.json"):
        dataIO.save_json("data/GTR/GTR1/GTR1.json", {})


def setup(bot):
    check_folders()
    check_files()
    q = GTR1(bot)
    loop = asyncio.get_event_loop()
    loop.create_task(q.check_day())
    bot.add_cog(q)
