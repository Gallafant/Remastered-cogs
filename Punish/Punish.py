import discord
from discord.ext import commands
from .utils import checks
import asyncio
import logging
# Casglu Ffurfiwch Ddata
from __main__ import send_cmd_help
from cogs.utils.dataIO import dataIO
import os
import time
import copy
# Dablu
try:
    import tabulate
except ImportError as e:
    raise RuntimeError("Punish requires tabulate. To install it, run `pip3 install tabulate` from the console or "
                       "`[p]debug bot.pip_install('tabulate')` from in Discord.") from e

log = logging.getLogger('red.punish')

try:
    from .mod import CaseMessageNotFound, NoModLogAccess
    ENABLE_MODLOG = True
except ImportError:
    log.warn("Could not import modlog exceptions from mod cog, most likely because mod.py was deleted or Red is out of "
             "date. Modlog integration will be disabled.")
    ENABLE_MODLOG = False

DB_VERSION = 1.49

ACTION_STR = "Timed mute \N{HOURGLASS WITH FLOWING SAND} \N{SPEAKER WITH CANCELLATION STROKE}"
PURGE_MESSAGES = 1  # for cpunish
PATH = 'data/punish/'
JSON = PATH + 'settings.json'

DEFAULT_ROLE_NAME = 'Punished'
DEFAULT_TEXT_OVERWRITE = discord.PermissionOverwrite(send_messages=False, send_tts_messages=False, add_reactions=False)
DEFAULT_VOICE_OVERWRITE = discord.PermissionOverwrite(speak=False)
DEFAULT_TIMEOUT_OVERWRITE = discord.PermissionOverwrite(send_messages=True, read_messages=True)

QUEUE_TIME_CUTOFF = 30

DEFAULT_TIMEOUT = '30m'
DEFAULT_CASE_MIN_LENGTH = '30m'  # only create modlog cases when length is longer than this

UNIT_TABLE = (
    (('weeks', 'wks', 'w'),    60 * 60 * 24 * 7),
    (('days',  'dys', 'd'),    60 * 60 * 24),
    (('hours', 'hrs', 'h'),    60 * 60),
    (('minutes', 'mins', 'm'), 60),
    (('seconds', 'secs', 's'), 1),
)



class BadTimeExpr(Exception):
    pass


def _find_unit(unit):
    for names, length in UNIT_TABLE:
        if any(n.startswith(unit) for n in names):
            return names, length
    raise BadTimeExpr("Invalid unit: %s" % unit)


def _parse_time(time):
    time = time.lower()
    if not time.isdigit():
        time = re.split(r'\s*([\d.]+\s*[^\d\s,;]*)(?:[,;\s]|and)*', time)
        time = sum(map(_timespec_sec, filter(None, time)))
    return int(time)


def _timespec_sec(expr):
    atoms = re.split(r'([\d.]+)\s*([^\d\s]*)', expr)
    atoms = list(filter(None, atoms))

    if len(atoms) > 2:  # This shouldn't ever happen
        raise BadTimeExpr("invalid expression: '%s'" % expr)
    elif len(atoms) == 2:
        names, length = _find_unit(atoms[1])
        if atoms[0].count('.') > 1 or \
                not atoms[0].replace('.', '').isdigit():
            raise BadTimeExpr("Not a number: '%s'" % atoms[0])
    else:
        names, length = _find_unit('seconds')

    try:
        return float(atoms[0]) * length
    except ValueError:
        raise BadTimeExpr("invalid value: '%s'" % atoms[0])


def _generate_timespec(sec: int, short=False, micro=False) -> str:
    timespec = []
    sec = int(sec)
    neg = sec < 0
    sec = abs(sec)

    for names, length in UNIT_TABLE:
        n, sec = divmod(sec, length)

        if n:
            if micro:
                s = '%d%s' % (n, names[2])
            elif short:
                s = '%d%s' % (n, names[1])
            else:
                s = '%d %s' % (n, names[0])

            if n <= 1 and not (micro and names[2] == 's'):
                s = s.rstrip('s')

            timespec.append(s)

    if len(timespec) > 1:
        if micro:
            spec = ''.join(timespec)

        segments = timespec[:-1], timespec[-1:]
        spec = ' and '.join(', '.join(x) for x in segments)
    elif timespec:
        spec = timespec[0]
    else:
        return '0'

    if neg:
        spec += ' ago'

    return spec


def format_list(*items, join='and', delim=', '):
    if len(items) > 1:
        return (' %s ' % join).join((delim.join(items[:-1]), items[-1]))
    elif items:
        return items[0]
    else:
        return ''


def permissions_for_roles(channel, *roles):
    """
    Calculates the effective permissions for a role or combination of roles.
    Naturally, if no roles are given, the default role's permissions are used
    """
    default = channel.server.default_role
    base = discord.Permissions(default.permissions.value)

    # Apply all role values
    for role in roles:
        base.value |= role.permissions.value

    # Server-wide Administrator -> True for everything
    # Bypass all channel-specific overrides
    if base.administrator:
        return discord.Permissions.all()

    role_ids = set(map(lambda r: r.id, roles))
    denies = 0
    allows = 0

    # Apply channel specific role permission overwrites
    for overwrite in channel._permission_overwrites:
        # Handle default role first, if present
        if overwrite.id == default.id:
            base.handle_overwrite(allow=overwrite.allow, deny=overwrite.deny)

        if overwrite.type == 'role' and overwrite.id in role_ids:
            denies |= overwrite.deny
            allows |= overwrite.allow

    base.handle_overwrite(allow=allows, deny=denies)

    # default channels can always be read
    if channel.is_default:
        base.read_messages = True

    # if you can't send a message in a channel then you can't have certain
    # permissions as well
    if not base.send_messages:
        base.send_tts_messages = False
        base.mention_everyone = False
        base.embed_links = False
        base.attach_files = False

    # if you can't read a channel then you have no permissions there
    if not base.read_messages:
        denied = discord.Permissions.all_channel()
        base.value &= ~denied.value

    # text channels do not have voice related permissions
    if channel.type is discord.ChannelType.text:
        denied = discord.Permissions.voice()
        base.value &= ~denied.value

    return base


def overwrite_from_dict(data):
    allow = discord.Permissions(data.get('allow', 0))
    deny = discord.Permissions(data.get('deny', 0))
    return discord.PermissionOverwrite.from_pair(allow, deny)


def overwrite_to_dict(overwrite):
    allow, deny = overwrite.pair()
    return {
        'allow' : allow.value,
        'deny'  : deny.value
    }


def format_permissions(permissions, include_null=False):
    entries = []

    for perm, value in sorted(permissions, key=lambda t: t[0]):
        if value is True:
            symbol = "\N{WHITE HEAVY CHECK MARK}"
        elif value is False:
            symbol = "\N{NO ENTRY SIGN}"
        elif include_null:
            symbol = "\N{RADIO BUTTON}"
        else:
            continue

        entries.append(symbol + ' ' + perm.replace('_', ' ').title().replace("Tts", "TTS"))

    if entries:
        return '\n'.join(entries)
    else:
        return "No permission entries."


def getmname(mid, server):
    member = discord.utils.get(server.members, id=mid)

    if member:
        return str(member)
    else:
        return '(absent user #%s)' % mid




class Punish:
    """
    Put misbehaving users in timeout where they are unable to speak, read, or
    do other things that can be denied using discord permissions. Includes
    auto-setup and more.
    """

    def __init__(self, bot):
        self.bot = bot
        self.location = 'data/RM/Punish/settings.json'
        self.json = dataIO.load_json(self.location)
        self.min = ['m', 'min', 'mins', 'minutes', 'minute']
        self.hour = ['h', 'hour', 'hours']
        self.day = ['d', 'day', 'days']
        self.task = bot.loop.create_task(self.check_time())

    def __unload(self):
        self.task.cancel()
        log.debug('Stopped task')

    def _timestamp(self, t, unit):
        if unit in self.min:
            return t * 60 + int(time.time())
        elif unit in self.hour:
            return t * 60 * 60 + int(time.time())
        elif unit in self.day:
            return t * 60 * 60 * 24 + int(time.time())
        else:
            raise Exception('Invalid Unit')

    @commands.command(pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_messages=True)
    async def P(self, ctx, user: discord.Member, t: int=1, unit='hour'):
        """Places a user in timeout for a period of time.

        Valid unit of times are minutes, hours & days.
        Example usage: !p @user 3 hours"""
        server = ctx.message.server
        # --- RÔL CREU ---
        if '❃Brad' not in [r.name for r in server.roles]:
            await self.bot.say('The ❃Brad role doesn\'t exist! Creating it now!')
            log.debug('Creating ❃Brad role in {}'.format(server.id))
            try:
                perms = discord.Permissions.none()
                await self.bot.create_role(server, name='❃Brad', permissions=perms)
                await self.bot.say("Role created! Setting channel permissions!\nPlease ensure that your moderator roles are ABOVE the ❃Brad role!\nPlease wait until the user has been added to the Timeout role!")
                try:
                    r = discord.utils.get(server.roles, name='❃Brad')
                    perms = discord.PermissionOverwrite()
                    perms.send_messages = False
                    for c in server.channels:
                        if c.type.name == 'text':
                            await self.bot.edit_channel_permissions(c, r, perms)
                            await asyncio.sleep(1.5)
                except discord.Forbidden:
                    await self.bot.say("A error occured while making channel permissions.\nPlease check your channel permissions for the ❃Brad role!")
            except discord.Forbidden:
                await self.bot.say("I cannot create a role. Please assign Manage Roles to me!")
        role = discord.utils.get(server.roles, name='❃Brad')
        # --- RHAID I WNEUD CREU RÔL! ---
        # --- LOGI GWASANAETH JSON ---
        if server.id not in self.json:
            log.debug('Adding server({}) in Json'.format(server.id))
            self.json[server.id] = {}
            dataIO.save_json(self.location, self.json)
        # --- GWNEUD I'W DDEFNYDDIO JSON YN YMWNEUD! ---
        # --- ASEINU'R AMSERLEN A'R RÔL ---
        try:
            if 'Mod' in self.bot.cogs:
                cog_mod = self.bot.get_cog('Mod')
                cog_mod_enabled = True
            if user.id == ctx.message.author.id:
                await self.bot.say('Please don\'t punish yourself :(')
            elif user.id not in self.json[server.id] and role not in user.roles:
                # NOD YW'R DEFNYDDWR MEWN PUNISH, NA RÔL
                until = self._timestamp(t, unit)
                self.json[server.id][user.id] = {'until': until, 'givenby': ctx.message.author.id}
                dataIO.save_json(self.location, self.json)
                await self.bot.add_roles(user, role)
                await self.bot.say('``{}`` is now Punished for {} {} by ``{}``.'.format(user.display_name, str(t), unit, ctx.message.author.display_name))
                if cog_mod_enabled is True:
                    await cog_mod.new_case(server, action="❃Brad for {} {}".format(t, unit), mod=ctx.message.author, user=user)
            elif user.id in self.json[server.id] and role not in user.roles:
                # DEFNYDDWYR MEWN PUNISH, RHIF OES
                    await self.bot.add_roles(user, role)
                    await self.bot.say('Role reapplied on {}'.format(user.display_name))
            elif user.id not in self.json[server.id] and role in user.roles:
                # NOD YR UNRHYW YN UNRHYW, YN YSTAFELL
                until = self._timestamp(t, unit)
                self.json[server.id][user.id] = {'until': until, 'givenby': ctx.message.author.id}
                dataIO.save_json(self.location, self.json)
                await self.bot.say('``{}`` is now Punished for {} {} by ``{}``.'.format(user.display_name, str(t), unit, ctx.message.author.display_name))
                if cog_mod_enabled is True:
                    await cog_mod.new_case(server, action="❃Brad for {} {}".format(t, unit), mod=ctx.message.author, user=user)
            else:
                # DEFNYDDWYR MEWN PUNISH, YN YSTAFELL
                await self.bot.say('``{}`` is already punished. Please use ``unpunish`` to unpunish the user.'.format(user.display_name))
        except:
            await self.bot.say('Invalid unit')

    @commands.command(pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_messages=True)
    async def UP(self, ctx, user: discord.Member):
        """Unpunishes a punished user"""
        if user.id in self.json[ctx.message.server.id]:
            r = discord.utils.get(ctx.message.server.roles, name='❃Brad')
            del self.json[ctx.message.server.id][user.id]
            await self.bot.remove_roles(user, r)
            dataIO.save_json(self.location, self.json)
            await self.bot.say('``{}`` is now unpunished.'.format(user.display_name))

    @commands.command(pass_context=True, no_pm=True)
    async def PU(self, ctx):
        """Shows the list of punished users"""
        # Poblogwch restr gyda rhestrau eraill, maent yn gweithredu fel tablau
        server = ctx.message.server
        table = []
        if server.id in self.json:
            for user in self.json[server.id]:
                temp = []
                # Cael y defnyddiwr display_name
                user_obj = discord.utils.get(server.members, id=user)
                log.debug(user_obj)
                if user_obj is None:
                    temp.append('ID: {}'.format(user))
                else:
                    temp.append(user_obj.display_name)
                    # Cael yr amser mewn munudau neu oriau, (gobeithio)
                    remaining = self.json[server.id][user]['until'] - int(time.time())
                if remaining < 60:
                    temp.append('<1 Minute')
                elif remaining < 120:
                    temp.append('1 Minute')
                elif remaining < 3600:
                    remaining = remaining / 60
                    temp.append('{} Minutes'.format(int(remaining)))
                elif remaining < 86400:
                    remaining = remaining / 60 / 60
                    temp.append('{} Hours'.format(int(remaining)))
                else:
                    remaining = remaining / 60 / 60 / 24
                    temp.append('{} Days'.format(int(remaining)))
                # Cael y rhoddgan
                given_obj = discord.utils.get(server.members, id=self.json[server.id][user]['givenby'])
                if given_obj is None:
                    temp.append('ID: {}'.format(self.json[server.id][user]['givenby']))
                else:
                    temp.append(given_obj.display_name)
                    table.append(temp)
            header = ['Member', 'Time Remaining', 'Given By']
            await self.bot.say('```\n{}```'.format(tabulate(table, headers=header, tablefmt='simple')))
        else:
            await self.bot.say('No punishments are given out on this server.')

    # Edrychwch am sianeli newydd, a chadwch y rôl yn eu hwyneb!
    async def new_channel(self, c):
        if 'Punished' in [r.name for r in c.server.roles]:
            if c.type.name == 'text':
                perms = discord.PermissionOverwrite()
                perms.send_messages = False
                r = discord.utils.get(c.server.roles, name='❃Brad')
                await self.bot.edit_channel_permissions(c, r, perms)
                log.debug('Punished role created on channel: {}'.format(c.id))

    async def check_time(self):
        while True:
            await asyncio.sleep(30)
            json = copy.deepcopy(self.json)
            log.debug('First Timer')
            for server in json:
                server_obj = discord.utils.get(self.bot.servers, id=server)
                role_obj = discord.utils.get(server_obj.roles, name='❃Brad')
                log.debug('Server Object = {}'.format(server_obj))
                for user in json[server]:
                    user_obj = discord.utils.get(server_obj.members, id=user)
                    log.debug('User Object = {}'.format(user_obj))
                    if json[server][user]['until'] < int(time.time()):
                        log.debug('Expired user ({})'.format(user))
                        await self.bot.remove_roles(user_obj, role_obj)
                        del self.json[server][user]
                        dataIO.save_json(self.location, self.json)
            log.debug('after loops')

    async def new_member(self, member):
        if member.server.id in self.json:
            if member.id in self.json[member.server.id]:
                r = discord.utils.get(member.server.roles, name='❃Brad')
                await self.bot.add_roles(member, r)
                log.debug('User ({}) joined while punished.'.format(member.id))


def check_folder():
    if not os.path.exists('data/RM/Punish'):
        log.debug('Creating folder: data/RM/Punish')
        os.makedirs('data/RM/Punish')


def check_file():
    data = {}

    data['db_version'] = DB_VERSION
    settings_file = 'data/RM/Punish/settings.json'
    if not dataIO.is_valid_json(settings_file):
        print('Creating default settings.json...')
        dataIO.save_json(settings_file, data)
    else:
        check = dataIO.load_json(settings_file)
        if 'db_version' in check:
            if check['db_version'] < DB_VERSION:
                data = {}
                data['db_version'] = DB_VERSION
                print('WARNING: Database version too old, please update!')
                dataIO.save_json(settings_file, data)


def setup(bot):
    check_folder()
    check_file()
    n = Punish(bot)
    bot.add_cog(n)
    bot.add_listener(n.new_member, 'on_member_join')
    bot.add_listener(n.new_channel, 'on_channel_create')
