import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
import discord
from discord.ext import commands
from enum import Enum
import io
import os
from os.path import split as path_split
import random
import re
import string
from typing import List, Optional, Union

from .utils import checks
from .utils.chat_formatting import box, error, warning
from .utils.dataIO import dataIO




DATA_DIR = 'data//GF1/'
CHARS = ''.join(sorted(set(string.digits + string.ascii_uppercase) - set('oO01lI')))
FONTS = [os.path.join(DATA_DIR, f + '.ttf') for f in ('CourierNew-Bold', 'DroidSansMono', 'LiberationMono-Bold')]
JSON = 'data/GF1/settings.json'
ZWSP = '\u200B'

try:
    from captcha.image import ImageCaptcha as ImageCaptchaClass
    ImageCaptcha = ImageCaptchaClass(fonts=FONTS, width=240)
except ImportError:
    ImageCaptcha = None


try:
    from captcha.image import WheezyCaptcha as WheezyCaptchaClass
    WheezyCaptcha = WheezyCaptchaClass(fonts=FONTS, width=320)
except ImportError:
    WheezyCaptcha = None


UNIT_TABLE = (
    (('weeks', 'wks', 'w'), 60 * 60 * 24 * 7),
    (('days', 'dys', 'd'), 60 * 60 * 24),
    (('hours', 'hrs', 'h'), 60 * 60),
    (('minutes', 'mins', 'm'), 60),
    (('seconds', 'secs', 's'), 1),
)




class BadTimeExpr(ValueError):
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


def _generate_timespec(sec, short=False, micro=False) -> str:
    timespec = []

    for names, length in UNIT_TABLE:
        n, sec = divmod(sec, length)

        if n:
            if micro:
                s = '%d%s' % (n, names[2])
            elif short:
                s = '%d%s' % (n, names[1])
            else:
                s = '%d %s' % (n, names[0])
            if n <= 1:
                s = s.rstrip('s')
            timespec.append(s)

    if len(timespec) > 1:
        if micro:
            return ''.join(timespec)

        segments = timespec[:-1], timespec[-1:]
        return ' and '.join(', '.join(x) for x in segments)

    return timespec[0]


def choices(population, k=1):
    return [random.choice(population) for _ in range(k)]


def okay(text):
    return "\N{WHITE HEAVY CHECK MARK} {}".format(text)


class ChallengeType(Enum):
    PLAIN = 'plain'
    IMAGE = 'image'
    WHEEZY = 'wheezy'


class ServerConfig:
    def __init__(self, cog, server_id, **data):
        self.cog = cog
        self.server_id = server_id
        self._handles = {}

        # prevents duplicate challenges from races in role add/remove
        self._pending_ids = set()

        # Generated challenge length
        self.challenge_length = data.get('challenge_length', 8)
        # Captcha generator (plain, image, or wheezy)
        self.challenge_type = ChallengeType(data.get('challenge_type', ChallengeType.WHEEZY))
        # ID of channel for posting challenges/responses
        self.channel_id = data.get('channel_id')
        # Whether the cog is enabled in a server
        self.enabled = data.get('enabled', False)
        # How long to wait for reply before kicking (0 = none)
        self.kick_timeout = data.get('kick_timeout', 5 * 60)
        # dict of UID : challenge data
        self.pending = data.get('pending', {})
        # The retry cooldown time for image-based captchas
        self.retry_cooldown = data.get('retry_cooldown', 60)
        # ID of dis/approval role
        self.role_id = data.get('role_id')
        # Role means approval? If false, disapproval.
        self.role_mode = data.get('role_mode', True)
        # Send challenge via DM?
        self.use_dm = data.get('use_dm', False)
        # Send challenge on member join?
        self.on_join = data.get('on_join', True)
        # Send challenge on role add/remove
        self.on_role = data.get('on_role', True)

    async def cancel(self, member: discord.Member) -> bool:
        if member.id not in self.pending:
            return False

        if member.id in self._handles:
            self._handles.pop(member.id).cancel()

        data = self.pending.pop(member.id, {})

        if data.get('use_dm'):
            self.cog.pending_dms.get(member.id, set()).discard(self.server_id)

        try:
            channel = self.bot.get_channel(data.get('channel_id'))
            message_id = data.get('message_id')

            if channel and message_id:
                message = await self.bot.get_message(channel, )
                await self.bot.delete_message(message)
        except Exception:
            pass  # ignore error if channel or message couldn't be found/deleted

        self.cog.save()
        return True

    async def attempt(self, message: discord.Message):
        data = self.pending.get(message.author.id, {})
        now_ts = datetime.now().timestamp()
        cooldown_left = data.get('sent_at', 0) - (now_ts - self.retry_cooldown)
        challenge = data.get('challenge')

        if not data:
            return
        elif message.content.lower() == 'retry':
            if cooldown_left > 0:
                time_left = _generate_timespec(cooldown_left)
                await self.cog.bot.send_message(message.channel, warning("%s, wait %s to retry."
                                                                         % (message.author.mention, time_left)))
            else:
                data['challenge'] = self.generate_code()
                await self.send_challenge(message.author, data)
                self.cog.save()
        elif message.content == challenge:
            if message.channel.is_private:
                await self.cog.bot.send_message(message.channel, okay("Correct!"))
            else:
                await self.cog.bot.send_message(message.channel, okay("%s, correct!") % message.author.mention)

            await self.approve(message.author, delay=2)
        elif message.content.replace(ZWSP, '') == challenge:
            if message.channel.is_private:
                msg = "Enter the code manually instead of copying and pasting, kthx."
            else:
                msg = '%s, enter the code manually instead of copying and pasting, kthx.' % message.author.mention

            await self.cog.bot.send_message(message.channel, warning(msg))

    async def approve(self, user: discord.User, delay=None):
        # wrap for race prevention
        if user.id in self._pending_ids:
            return False

        try:
            self._pending_ids.add(user.id)
            return await self._approve(user, delay=delay)
        finally:
            self._pending_ids.discard(user.id)

    async def _approve(self, user: discord.User, delay=None):
        await self.cancel(user)

        if delay:
            await asyncio.sleep(delay)

        if isinstance(user, discord.Member):
            member = user
        else:
            member = self.server.get_member(user.id)

        if not (member and self.enabled and self.role):
            return False
        elif self.role_mode:
            await self.cog.bot.add_roles(member, self.role)
        else:
            await self.cog.bot.remove_roles(member, self.role)

    async def challenge(self, member: discord.Member):
        # wrap for race prevention
        if member.id in self._pending_ids:
            return False

        try:
            self._pending_ids.add(member.id)
            return await self._challenge(member)
        finally:
            self._pending_ids.discard(member.id)

    async def _challenge(self, member: discord.Member):
        if not (self.enabled and (self.use_dm or self.channel)):
            return False
        elif member.id in self.pending:
            return False

        has_role = discord.utils.get(member.roles, id=self.role_id)

        if not self.role:
            pass
        elif self.role_mode and has_role:
            await self.cog.bot.remove_roles(member, self.role)
        elif not self.role_mode and not has_role:
            await self.cog.bot.add_roles(member, self.role)

        now_ts = datetime.now().timestamp()

        data = {
            'challenge'  : self.generate_code(),
            'channel_id' : self.channel_id,
            'role_mode'  : self.role_mode,
            'sent_at'    : now_ts,
            'kick_time'  : (now_ts + self.kick_timeout + 0.3) if self.kick_timeout else None,
            'use_dm'     : self.use_dm,
            'chal_type'  : self.challenge_type.value
        }

        data = await self.send_challenge(member, data)

        if self.kick_timeout:
            self.schedule_kick(self.kick_timeout, member)

        if self.use_dm:
            self.cog.pending_dms[member.id].add(self.server_id)

        self.pending[member.id] = data
        self.cog.save()
        return data

    async def send_challenge(self, target: discord.User, data: dict):
        use_dm = data['use_dm']

        if use_dm:
            msg_dest = target
        else:
            msg_dest = target.server.get_channel(data['channel_id'])

        msg = 'Please enter the code below'

        if data.get('kick_time'):
            time_left = data['kick_time'] - datetime.now().timestamp()
            msg += ' within %s' % _generate_timespec(time_left)

        msg += ':'

        embed = self.cog._build_embed(self.server, title=msg)
        challenge_type = ChallengeType(data['chal_type'])

        if challenge_type is ChallengeType.WHEEZY and not WheezyCaptcha:
            challenge_type = ChallengeType.IMAGE

        if challenge_type is ChallengeType.IMAGE and not ImageCaptcha:
            challenge_type = ChallengeType.PLAIN

        if challenge_type is ChallengeType.PLAIN:
            challenge = ZWSP.join(data['challenge'])  # inject zero-width spaces to mitigate copypaste
            embed.description = box(challenge) + '\nDo not copy and paste.'
            attachment = None
        elif challenge_type is ChallengeType.WHEEZY:
            attachment = WheezyCaptcha.generate(data['challenge'])
            embed.description = 'Letters are all uppercase.\nSay `retry` to get another image.'
        elif challenge_type is ChallengeType.IMAGE:
            attachment = ImageCaptcha.generate(data['challenge'])
            embed.description = 'Letters are all uppercase.\nSay `retry` to get another image.'

        content = target.mention

        if attachment:
            embed.set_image(url='attachment://captcha.png')
            sent = await self.cog.send_file(msg_dest, attachment, filename='captcha.png', content=content, embed=embed)
        else:
            sent = await self.cog.bot.send_message(msg_dest, content, embed=embed)

        data['message_id'] = sent.id

        if use_dm:
            data['channel_id'] = sent.channel.id

        return data

    def generate_code(self):
        return ''.join(choices(CHARS, self.challenge_length))

    def shutdown(self):
        for handle in self._handles.values():
            handle.cancel()

    def on_ready(self):
        server = self.cog.bot.get_server(self.server_id)
        now_ts = datetime.now().timestamp()

        if not server:
            return

        for uid, data in self.pending.copy().items():
            member = server.get_member(uid)
            kick_time = data.get('kick_time')

            if not (member and discord.utils.get(member.roles, id=self.role_id) != data['role_mode']):
                # member has left or is approved already
                self.pending.pop(uid)
                continue

            if data.get('use_dm'):
                # register at the cog level so incoming messages are routed back
                self.cog.pending_dms[uid].add(server.id)

            if kick_time:
                self.schedule_kick(data['kick_time'] - now_ts, member)

    def schedule_kick(self, delay: Union[int, float], member: discord.Member):
        loop = self.cog.bot.loop
        coro = self.cog.kick_after(member, delay)
        self._handles[member.id] = loop.create_task(coro)

    def to_kick(self) -> List[discord.Member]:
        server = self.cog.bot.get_server(self.server_id)
        ret = []

        if server and self.kick_timeout:
            cutoff = datetime.utcnow() - timedelta(seconds=self.kick_timeout)

            for mid in self.pending:
                member = server.get_member(mid)

                if member and member.joined_at < cutoff:
                    ret.append(member)

        return ret

    @property
    def role(self) -> Optional[discord.Role]:
        server = self.cog.bot.get_server(self.server_id)
        if server:
            return discord.utils.get(server.roles, id=self.role_id)
        else:
            return None

    @role.setter
    def role(self, role):
        assert role.server.id == self.server_id
        self.role_id = role.id

    @property
    def server(self) -> Optional[discord.Server]:
        return self.cog.bot.get_server(self.server_id)

    @property
    def channel(self) -> Optional[discord.Channel]:
        server = self.cog.bot.get_server(self.server_id)
        if server:
            return server.get_channel(self.channel_id)
        else:
            return None

    @channel.setter
    def channel(self, channel):
        assert channel.server.id == self.server_id
        self.channel_id = channel.id

    def to_json(self):
        return {
            'challenge_length' : self.challenge_length,
            'challenge_type'   : self.challenge_type.value,
            'channel_id'       : self.channel_id,
            'enabled'          : self.enabled,
            'kick_timeout'     : self.kick_timeout,
            'pending'          : self.pending,
            'retry_cooldown'   : self.retry_cooldown,
            'role_id'          : self.role_id,
            'role_mode'        : self.role_mode,
            'use_dm'           : self.use_dm,
            'on_join'          : self.on_join,
            'on_role'          : self.on_role
        }


class GF1:
    """
    A cog to challenge new members with a captcha upon joining a server.
    """
    def __init__(self, bot):
        self.bot = bot
        self.settings = {}
        self.misc_data = {}
        self.pending_dms = defaultdict(lambda: set())
        self.task = self.bot.loop.create_task(self.init_task())

        data = dataIO.load_json(JSON)

        for k, v in data.items():
            if k.startswith('_') or type(v) is not dict or not k.isnumeric():
                self.misc_data[k] = v
            else:
                self.settings[k] = ServerConfig(self, k, **v)



    def save(self):
        data = self.misc_data.copy()
        data.update({k: v.to_json() for k, v in self.settings.items()})
        dataIO.save_json(JSON, data)

    def __unload(self):
        self.task.cancel()
        for settings in self.settings.values():
            settings.shutdown()

        self.save()

    async def approve(self, member):
        if member.server and member.server.id in self.settings:
            await self.settings[member.server.id].approve(member)

    async def challenge(self, member):
        if member.server and member.server.id in self.settings:
            return await self.settings[member.server.id].challenge(member)

    async def cancel(self, member):
        settings = self.settings.get(member.server.id)

        if not settings:
            return

        await settings.cancel(member)

    async def kick_after(self, member, delay):
        try:
            await asyncio.sleep(delay)
            await self.bot.kick(member)
        except asyncio.CancelledError:
            pass

    async def init_task(self):
        await self.bot.wait_until_ready()
        for settings in self.settings.values():
            settings.on_ready()

    @checks.mod_or_permissions(mange_roles=True)
    @commands.group(pass_context=True, no_pm=True)
    async def captcha(self, ctx):
        """
        Captcha cog commands.
        """
        if ctx.invoked_subcommand is None:
            await self.bot.send_cmd_help(ctx)

    @captcha.command(pass_context=True, name='approve')
    async def captcha_approve(self, ctx, member: discord.Member):
        """
        Approves a user pending captcha confirmation
        """
        settings = self.settings.get(ctx.message.server.id)

        if not (settings and settings.enabled):
            await self.bot.say('Captcha approval is not enabled in this server.')
            return
        elif not settings.role:
            await self.bot.say('The approval role has not been set or does not exist anymore.')
            return
        elif member.id not in settings.pending:
            await self.bot.say('%s is not currently pending approval.' % member)
            return

        await self.approve(member)
        await self.bot.say('%s has been approved.' % member)

    @captcha.command(pass_context=True, name='challenge')
    async def captcha_challenge(self, ctx, member: discord.Member):
        """
        Unverifies a user and prompts them with a challenge
        """
        settings = self.settings.get(ctx.message.server.id)

        if not (settings and settings.enabled):
            await self.bot.say('Captcha approval is not enabled in this server.')
            return
        elif not settings.role:
            await self.bot.say('The approval role has not been set or does not exist anymore.')
            return
        elif member.bot:
            await self.bot.say("Can't challenge a bot.")
        elif member.id in settings.pending:
            await self.bot.say('%s is already pending approval.' % member)
            return

        await self.challenge(member)
        await self.bot.say('Challenge sent for %s.' % member)

    @captcha.command(pass_context=True, name='approve-all')
    async def captcha_approve_all(self, ctx):
        """
        Approves ALL pending members
        """
        server = ctx.message.server
        settings = self.settings.get(server.id)
        count = 0

        if not (settings and settings.enabled):
            await self.bot.say('Captcha is not enabled in this server.')
            return
        elif not settings.role:
            await self.bot.say('The approval role has not been set or does not exist anymore.')
            return

        for mid in settings.pending:
            member = server.get_member(mid)
            if not member:
                continue

            await self.approve(member)
            count += 1

        await self.bot.say('Approved %i member(s).' % count)

    @checks.admin_or_permissions(manage_server=True)
    @commands.group(pass_context=True, no_pm=True)
    async def captchaset(self, ctx):
        """
        Captcha cog configuration commands.

        Displays help and current settings if no subcommand is given
        """
        if ctx.invoked_subcommand is None:
            await self.bot.send_cmd_help(ctx)

            settings = self.settings.get(ctx.message.server.id)

            if not settings:
                return

            await self.bot.say('\n'.join((
                '**Current settings:**'
                'Enabled: ' + ('yes' if settings.enabled else 'no'),
                'Challenge on join: ' + ('yes' if settings.on_join else 'no'),
                'Challenge on role add/removal: ' + ('yes' if settings.on_role else 'no'),
                'Channel: ' + (settings.channel.mention if settings.channel else 'not set'),
                'Role: @' + (settings.role.name if settings.role else 'not set'),
                'Kick delay: ' + (_generate_timespec(settings.kick_timeout) if settings.kick_timeout else 'disabled'),
                'Use DMs: ' + ('yes' if settings.use_dm else 'no'),
                'Role mode: ' + ('verified' if settings.role_mode else 'unverified'),
                'Captcha type: ' + settings.challenge_type.value,
                'Retry delay: ' + (_generate_timespec(settings.retry_cooldown) if settings.retry_cooldown else 'none')
            )))

    @captchaset.command(pass_context=True, allow_dm=False, name='kick-delay')
    async def captchaset_kick_delay(self, ctx, *, timespec: str = None):
        """
        Set/disable or display new member kick delay

        New members will have this long to enter the correct CAPTCHA before being kicked.
        Specify 'disable' to disable kicking of members who do not complete the captcha.
        """
        server = ctx.message.server
        settings = self.settings.get(server.id)
        extra = None

        if timespec:
            if timespec.strip('\'"').lower() in ('disable', 'none'):
                kick_timeout = None
            else:
                try:
                    kick_timeout = _parse_time(timespec)
                except BadTimeExpr as e:
                    await self.bot.say(error(e.args[0]))
                    return

            if kick_timeout is not None and kick_timeout < 10:
                await self.bot.say(warning("Delay must be 10 seconds or longer."))
                return

        if timespec and not settings:
            self.settings[server.id] = settings = ServerConfig(self, server.id, kick_timeout=kick_timeout)
            adj = 'now'
            extra = "Note that Captcha hasn't been enabled yet; run `%scaptchaset enabled yes`." % ctx.prefix
            self.save()
        elif timespec is None:
            kick_timeout = settings and settings.kick_timeout
            adj = 'currently'
        elif settings.kick_timeout == kick_timeout:
            adj = 'already'
        else:
            adj = 'now'
            settings.kick_timeout = kick_timeout
            self.save()

        desc = _generate_timespec(kick_timeout) if kick_timeout else 'disabled'
        msg = 'Kick timeout is %s %s.' % (adj, desc)

        if extra:
            msg += '\n\n' + extra

        await self.bot.say(msg)

    @captchaset.command(pass_context=True, allow_dm=False, name='retry-delay')
    async def captchaset_retry_delay(self, ctx, *, timespec: str = None):
        """
        Set/disable or display image captcha retry delay

        Members will have to wait this long before generating another image to attempt.
        Specify 'disable' to allow instant retries.
        """
        server = ctx.message.server
        settings = self.settings.get(server.id)
        extra = None

        if timespec:
            if timespec.strip('\'"').lower() in ('disable', 'none'):
                retry_cooldown = None
            else:
                try:
                    retry_cooldown = _parse_time(timespec)
                except BadTimeExpr as e:
                    await self.bot.say(error(e.args[0]))
                    return

            if retry_cooldown is not None and retry_cooldown < 10:
                await self.bot.say(warning("Delay must be 10 seconds or longer."))
                return

        if timespec and not settings:
            self.settings[server.id] = settings = ServerConfig(self, server.id, retry_cooldown=retry_cooldown)
            adj = 'now'
            extra = "Note that Captcha hasn't been enabled yet; run `%scaptchaset enabled yes`." % ctx.prefix
            self.save()
        elif timespec is None:
            retry_cooldown = settings and settings.retry_cooldown
            adj = 'currently'
        elif settings.retry_cooldown == retry_cooldown:
            adj = 'already'
        else:
            adj = 'now'
            settings.retry_cooldown = retry_cooldown
            self.save()

        desc = _generate_timespec(retry_cooldown) if retry_cooldown else 'disabled'
        msg = 'Retry delay is %s %s.' % (adj, desc)

        if extra:
            msg += '\n\n' + extra

        await self.bot.say(msg)

    @captchaset.command(pass_context=True, no_pm=True, name='channel')
    async def captchaset_channel(self, ctx, channel: discord.Channel = None):
        """
        Sets or displays the current staging channel.
        """
        server = ctx.message.server
        settings = self.settings.get(server.id)
        extra = None

        if channel and not settings:
            self.settings[server.id] = settings = ServerConfig(self, server.id, channel_id=channel.id)
            adj = 'now'
            extra = "Note that Captcha hasn't been enabled yet; run `%scaptchaset enabled yes`." % ctx.prefix
            self.save()
        elif channel is None:
            channel = settings and settings.channel
            adj = 'currently'
        elif settings.channel == channel:
            adj = 'already'
        else:
            adj = 'now'
            settings.channel = channel
            self.save()

        desc = channel.mention if channel else 'not set'
        msg = 'Captcha channel is %s %s.' % (adj, desc)

        if settings and settings.use_dm:
            extra = 'Note that this setting will have no effect since Captcha is set to use DMs.'

        if extra:
            msg += '\n\n' + extra

        await self.bot.say(msg)

    @captchaset.command(pass_context=True, no_pm=True, name='role')
    async def captchaset_role(self, ctx, role: discord.Role = None):
        """
        Sets or displays the current un/verified role.
        """
        server = ctx.message.server
        settings = self.settings.get(server.id)
        extra = None

        if role and role >= server.me.top_role:
            await self.bot.say(warning("That role is too high for me to manage."))
            return

        if role and not settings:
            self.settings[server.id] = settings = ServerConfig(self, server.id, role_id=role.id)
            adj = 'now'
            extra = "Note that Captcha hasn't been enabled yet; run `%scaptchaset enabled yes`." % ctx.prefix
            self.save()
        elif role is None:
            role = settings and settings.role
            adj = 'currently'
        elif settings.role == role:
            adj = 'already'
        else:
            adj = 'now'
            settings.role = role
            self.save()

        desc = (role.name if role.mentionable else role.mention) if role else 'not set'
        role_type = 'Verified' if settings.role_mode else 'Unverified'
        msg = '%s role is %s %s.' % (role_type, adj, desc)

        if extra:
            msg += '\n\n' + extra

        await self.bot.say(msg)

    @captchaset.command(pass_context=True, no_pm=True, name='enabled')
    async def captchaset_enabled(self, ctx, yes_no: bool = None):
        """
        Sets or displays whether captcha is enabled in a server
        """
        server = ctx.message.server
        settings = self.settings.get(server.id)

        if (yes_no is not None) and not settings:
            self.settings[server.id] = settings = ServerConfig(self, server.id, enabled=yes_no)
            adj = 'are now'
            self.save()
        elif yes_no is None:
            yes_no = settings and settings.enabled
            adj = 'currently'
        elif settings.enabled == yes_no:
            adj = 'already'
        else:
            adj = 'are now'
            settings.enabled = yes_no
            self.save()

        desc = 'enabled' if yes_no else 'disabled'
        msg = 'Captcha challenges %s %s.' % (adj, desc)

        if yes_no:
            if not (settings.channel or settings.use_dm):
                msg += ('\n\nNote that a channel has not been selected and DM challenges are not enabled. '
                        'Use `{0}captchaset channel <channel>`. or `{0}captchaset dm on`.'.format(ctx.prefix))
            if not settings.role:
                role_type = 'Verified' if settings.role_mode else 'Unverified'
                msg += '\n\nA %s role must also be configured. Set it with `%scaptchaset role <role>`.' % (role_type,
                                                                                                           ctx.prefix)

        await self.bot.say(msg)

    @captchaset.command(pass_context=True, name='dm')
    async def captchaset_dm(self, ctx, yes_no: bool = None):
        """
        Sets or displays whether challenges will be sent using DMs
        """
        server = ctx.message.server
        settings = self.settings.get(server.id)
        extra = None

        if (yes_no is not None) and not settings:
            self.settings[server.id] = settings = ServerConfig(self, server.id, use_dm=yes_no)
            adj = 'will now'
            self.save()
        elif yes_no is None:
            yes_no = settings and settings.use_dm
            adj = 'currently'
        elif settings.use_dm == yes_no:
            adj = 'already'
        else:
            adj = 'will now'
            settings.use_dm = yes_no
            self.save()

        if yes_no:
            desc = 'use direct messages'
        else:
            desc = 'use a server channel'
            if settings and settings.channel:
                desc += ' (%s)' % settings.channel.mention
            else:
                extra = ('Note that a channel has not yet been selected. '
                         'Use `%scaptchaset channel <channel>`.' % ctx.prefix)

        msg = 'Captcha challenges %s %s.' % (adj, desc)

        if extra:
            msg += '\n\n' + extra

        await self.bot.say(msg)

    @captchaset.command(pass_context=True, name='on-join')
    async def captchaset_on_join(self, ctx, yes_no: bool = None):
        """
        Sets or displays whether members will be challenged upon joining
        """
        server = ctx.message.server
        settings = self.settings.get(server.id)

        if (yes_no is not None) and not settings:
            self.settings[server.id] = settings = ServerConfig(self, server.id, on_join=yes_no)
            desc = 'will ' + ('now' if yes_no else 'no longer')
            self.save()
        elif yes_no is None:
            yes_no = settings and settings.on_join
            desc = 'currently ' + ("will" if yes_no else "won't")
        elif settings.on_join == yes_no:
            desc = 'already ' + ("will" if yes_no else "won't")
        else:
            desc = 'will ' + ('now' if yes_no else 'no longer')
            settings.on_join = yes_no
            self.save()

        msg = 'Members %s be automatically challenged with a captcha upon joining.' % (desc)
        await self.bot.say(msg)

    @captchaset.command(pass_context=True, name='on-role')
    async def captchaset_on_role(self, ctx, yes_no: bool = None):
        """
        Sets or displays challenge upon add/removing the un/verified role
        """
        server = ctx.message.server
        settings = self.settings.get(server.id)

        if (yes_no is not None) and not settings:
            self.settings[server.id] = settings = ServerConfig(self, server.id, on_role=yes_no)
            desc = 'will ' + ('now' if yes_no else 'no longer')
            self.save()
        elif yes_no is None:
            yes_no = settings and settings.on_role
            desc = 'currently ' + ("will" if yes_no else "won't")
        elif settings.on_role == yes_no:
            desc = 'already ' + ("will" if yes_no else "won't")
        else:
            desc = 'will ' + ('now' if yes_no else 'no longer')
            settings.on_role = yes_no
            self.save()

        msg = 'Members %s be challenged when the un/verified role is added/removed.' % (desc)
        await self.bot.say(msg)

    @captchaset.command(pass_context=True, name='type')
    async def captchaset_type(self, ctx, challenge_type: str = None):
        """
        Sets or displays the challenge type

        Type must be one of the following (or left blank to show the current value):
        - plain:   plaintext captcha, no images
        - image:   use the captcha library from pypi
        - wheezy:  use the wheezy.captcha library from pypi
        """
        server = ctx.message.server
        settings = self.settings.get(server.id)
        extra = None

        try:
            challenge_type = challenge_type and ChallengeType(challenge_type)
        except ValueError:
            await self.bot.send_cmd_help(ctx)

        if (challenge_type is not None) and not settings:
            self.settings[server.id] = settings = ServerConfig(self, server.id, challenge_type=challenge_type)
            extra = "\n\nNote that Captcha hasn't been enabled yet; run `%scaptchaset enabled yes`." % ctx.prefix
            adj = 'now'
            self.save()
        elif challenge_type is None:
            challenge_type = settings.challenge_type
            adj = 'currently'
        elif settings.challenge_type == challenge_type:
            adj = 'already'
        else:
            adj = 'now'
            settings.challenge_type = challenge_type
            self.save()

        await self.bot.say('Captcha type is %s %s.%s' % (adj, challenge_type.value, extra or ''))

    @captchaset.command(pass_context=True, name='role-mode')
    async def captchaset_role_mode(self, ctx, role_mode: str = None):
        """
        Show/set role mode

        role_mode must be verified, unverified, or left blank to show the current setting
        """
        server = ctx.message.server
        settings = self.settings.get(server.id)
        extra = None

        if role_mode:
            if role_mode.lower().startswith('unverified'):
                role_mode = False
            elif role_mode.lower().startswith('verified'):
                role_mode = True
            else:
                await self.bot.send_cmd_help(ctx)

        if (role_mode is not None) and not settings:
            self.settings[server.id] = settings = ServerConfig(self, server.id, role_mode=role_mode)
            extra = "\n\nNote that Captcha hasn't been enabled yet; run `%scaptchaset enabled yes`." % ctx.prefix
            adj = 'now'
            self.save()
        elif role_mode is None:
            role_mode = settings.role_mode
            adj = 'currently'
        elif settings.role_mode == role_mode:
            adj = 'already'
        else:
            adj = 'now'
            settings.role_mode = role_mode
            self.save()

        desc = 'verified' if role_mode else 'unverified'
        await self.bot.say('Role mode is %s set to %s.%s' % (adj, desc, extra or ''))

    async def on_member_join(self, member):
        server = member.server
        settings = self.settings.get(server.id)

        if member.bot or not (settings and settings.enabled and settings.on_join):
            return

        await self.challenge(member)

    async def on_member_remove(self, member):
        server = member.server
        settings = self.settings.get(server.id)

        if member.bot or not (settings and settings.enabled):
            return

        await self.cancel(member)

    async def on_member_update(self, before, after):
        server = before.server
        settings = self.settings.get(server.id)

        if before.bot or before.roles == after.roles:
            return
        elif not (settings and settings.enabled and settings.role):
            return

        role = settings.role
        in_before = role in before.roles
        in_after = role in after.roles

        if in_before == in_after:
            return
        # verified mode and role was added OR unverified mode and was removed
        elif settings.role_mode == in_after:  # cancel challenge
            await settings.cancel(after)
        # verified mode and role was removed OR unverified mode and was added
        elif settings.on_role:  # trigger a challenge if configured to do so
            await settings.challenge(after)

    async def on_message(self, message):
        if message.author.bot:
            return
        elif message.channel.is_private:
            for sid in self.pending_dms.get(message.author.id, ()):
                await self.settings[sid].attempt(message)
        elif message.server:
            settings = self.settings.get(message.server.id)
            if settings and settings.enabled and message.author.id in settings.pending:
                await settings.attempt(message)

    def _build_embed(self, server, **kwargs):
        embed = discord.Embed(**kwargs)
        embed.set_author(name='%s verification system' % server.name, icon_url=server.icon_url or discord.Embed.Empty)
        return embed

    

    async def send_file(self, destination, fp, *, filename=None, content=None, tts=False, embed=None):
        """
        discord.py's send_file with embed support
        """

        channel_id, guild_id = await self.bot._resolve_destination(destination)

        try:
            with open(fp, 'rb') as f:
                buffer = io.BytesIO(f.read())
                if filename is None:
                    _, filename = path_split(fp)
        except TypeError:
            buffer = fp

        content = str(content) if content is not None else None

        if embed is not None:
            embed = embed.to_dict()

        data = await self.bot.http.send_file(channel_id, buffer, guild_id=guild_id,
                                             filename=filename, content=content, tts=tts, embed=embed)
        channel = self.bot.get_channel(data.get('channel_id'))
        message = self.bot.connection._create_message(channel=channel, **data)
        return message


def check_files():
    dirname = os.path.dirname(JSON)

    if not os.path.exists(dirname):
        print("Creating %s folder..." % dirname)
        os.makedirs(dirname)

    if not dataIO.is_valid_json(JSON):
        print("Creating %s..." % JSON)
        dataIO.save_json(JSON, {})


def setup(bot):
    check_files()
    bot.add_cog(GF1(bot))
