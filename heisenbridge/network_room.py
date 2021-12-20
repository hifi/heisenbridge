import argparse
import asyncio
import datetime
import hashlib
import html
import logging
import re
import ssl
import tempfile
from argparse import Namespace
from base64 import b32encode
from collections import defaultdict
from time import time
from typing import Any
from typing import Dict
from typing import List
from typing import Tuple

import irc.client
import irc.client_aio
import irc.connection
from jaraco.stream import buffer
from python_socks.async_.asyncio import Proxy

from heisenbridge import __version__
from heisenbridge.channel_room import ChannelRoom
from heisenbridge.command_parse import CommandManager
from heisenbridge.command_parse import CommandParser
from heisenbridge.command_parse import CommandParserError
from heisenbridge.irc import HeisenReactor
from heisenbridge.parser import IRCMatrixParser
from heisenbridge.plumbed_room import PlumbedRoom
from heisenbridge.private_room import parse_irc_formatting
from heisenbridge.private_room import PrivateRoom
from heisenbridge.private_room import unix_to_local
from heisenbridge.room import Room


def connected(f):
    def wrapper(*args, **kwargs):
        self = args[0]

        if not self.conn or not self.conn.connected:
            self.send_notice("Need to be connected to use this command.")
            return asyncio.sleep(0)

        return f(*args, **kwargs)

    return wrapper


# forwards events to private and channel rooms
def ircroom_event(target_arg=None):
    def outer(f):
        def wrapper(self, conn, event):
            if target_arg is not None:
                # if we have target arg use that
                target = event.arguments[target_arg].lower()
            else:
                # switch target around if it's targeted towards us directly
                target = event.target.lower() if event.target != conn.real_nickname else event.source.nick.lower()

            if target in self.rooms:
                room = self.rooms[target]
                try:
                    room_f = getattr(room, "on_" + event.type)
                    try:
                        return room_f(conn, event)
                    except Exception:
                        logging.exception(f"Calling on_{event.type} failed for {target}")
                except AttributeError:
                    logging.warning(f"Expected {room} to have on_{event.type} but didn't")

            return f(self, conn, event)

        return wrapper

    return outer


class NetworkRoom(Room):
    # configuration stuff
    name: str
    connected: bool
    nick: str
    username: str
    ircname: str
    password: str
    sasl_username: str
    sasl_password: str
    autocmd: str
    pills_length: int
    pills_ignore: list
    autoquery: bool
    tls_cert: str
    rejoin_invite: bool
    rejoin_kick: bool

    # state
    commands: CommandManager
    conn: Any
    rooms: Dict[str, Room]
    connecting: bool
    real_host: str
    real_user: str
    pending_kickbans: Dict[str, List[Tuple[str, str]]]
    backoff: int
    backoff_task: Any
    next_server: int
    connected_at: int

    def init(self):
        self.name = None
        self.connected = False
        self.nick = None
        self.username = None
        self.ircname = None
        self.password = None
        self.sasl_username = None
        self.sasl_password = None
        self.autocmd = None
        self.pills_length = 2
        self.pills_ignore = []
        self.autoquery = True
        self.allow_ctcp = False
        self.tls_cert = None
        self.rejoin_invite = True
        self.rejoin_kick = False
        self.backoff = 0
        self.backoff_task = None
        self.next_server = 0
        self.connected_at = 0

        self.commands = CommandManager()
        self.conn = None
        self.rooms = {}
        self.connlock = asyncio.Lock()
        self.disconnect = True
        self.real_host = "?" * 63  # worst case default
        self.real_user = "?" * 8  # worst case default
        self.keys = {}  # temp dict of join channel keys
        self.keepnick_task = None  # async task
        self.whois_data = defaultdict(dict)  # buffer for keeping partial whois replies
        self.pending_kickbans = defaultdict(list)

        cmd = CommandParser(
            prog="NICK",
            description="set/change nickname",
            epilog=(
                "You can always see your current nickname on the network without arguments.\n"
                "If connected new nickname will be sent to the server immediately. It may be rejected and an underscore appended"
                " to it automatically.\n"
            ),
        )
        cmd.add_argument("nick", nargs="?", help="new nickname")
        self.commands.register(cmd, self.cmd_nick)

        cmd = CommandParser(
            prog="USERNAME",
            description="set username",
            epilog=(
                "Setting a new username requires reconnecting to the network.\n"
                "\n"
                "Note: If identd is enabled this will be ignored and Matrix ID hash or admin set custom ident is used."
            ),
        )
        cmd.add_argument("username", nargs="?", help="new username")
        cmd.add_argument("--remove", action="store_true", help="remove stored username")
        self.commands.register(cmd, self.cmd_username)

        cmd = CommandParser(
            prog="REALNAME",
            description="set realname",
            epilog=("Setting a new realname requires reconnecting to the network.\n"),
        )
        cmd.add_argument("name", nargs="?", help="new realname")
        cmd.add_argument("--remove", action="store_true", help="remove stored name")
        self.commands.register(cmd, self.cmd_realname)

        cmd = CommandParser(
            prog="PASSWORD",
            description="set server password",
            epilog=(
                "You can store your network password using this command and it will be automatically offered on connect.\n"
                "Some networks allow using this to identify with NickServ on connect without sending a separate message.\n"
                "\n"
                "Note: Bridge administrators can trivially see the stored password if they want to.\n"
            ),
        )
        cmd.add_argument("password", nargs="?", help="new password")
        cmd.add_argument("--remove", action="store_true", help="remove stored password")
        self.commands.register(cmd, self.cmd_password)

        cmd = CommandParser(
            prog="SASL",
            description="set SASL PLAIN credentials",
            epilog=(
                "If the network supports SASL authentication you can configure them with this command.\n"
                "\n"
                "Note: Bridge administrators can trivially see the stored password if they want to.\n"
            ),
        )
        cmd.add_argument("--username", help="SASL username")
        cmd.add_argument("--password", help="SASL password")
        cmd.add_argument("--remove", action="store_true", help="remove stored credentials")
        self.commands.register(cmd, self.cmd_sasl)

        cmd = CommandParser(
            prog="CERTFP",
            description="configure CertFP authentication for this network",
            epilog=(
                "Using the set command requires you to paste a bundled PEM certificate (cert + key) on the next line"
                " after the command within the same message. The certificate needs to include both the certificate and"
                " the private key for it to be accepted.\n"
                "\n"
                "OpenSSL generation example (from Libera.Chat guides):\n"
                "$ openssl req -x509 -new -newkey rsa:4096 -sha256 -days 1096 -nodes -out libera.pem -keyout libera.pem"
            ),
        )
        cmd.add_argument("--set", action="store_true", help="set X509 certificate bundle (PEM)")
        cmd.add_argument("--remove", action="store_true", help="remove stored certificate")
        self.commands.register(cmd, self.cmd_certfp)

        cmd = CommandParser(
            prog="AUTOCMD",
            description="run commands on connect",
            epilog=(
                "If the network you are connecting to does not support server password to identify you automatically"
                " can set this to send a command before joining channels.\n"
                "\n"
                'Example (QuakeNet): AUTOCMD "UMODE +x; MSG -s Q@CServe.quakenet.org auth foo bar"\n'
                "Example (OFTC): AUTOCMD NICKSERV -s identify foo bar\n"
            ),
        )
        cmd.add_argument("command", nargs="*", help="commands separated with ';'")
        cmd.add_argument("--remove", action="store_true", help="remove stored command")
        self.commands.register(cmd, self.cmd_autocmd)

        cmd = CommandParser(
            prog="CONNECT",
            description="connect to network",
            epilog=(
                "When this command is invoked the connection to this network will be persisted across disconnects and"
                " bridge restart.\n"
                "Only if the server KILLs your connection it will stay disconnected until CONNECT is invoked again.\n"
                "\n"
                "If you want to cancel automatic reconnect you need to issue the DISCONNECT command.\n"
            ),
        )
        self.commands.register(cmd, self.cmd_connect)

        cmd = CommandParser(
            prog="DISCONNECT",
            description="disconnect from network",
            epilog=(
                "In addition to disconnecting from an active network connection this will also cancel any automatic"
                "reconnection attempt.\n"
            ),
        )
        self.commands.register(cmd, self.cmd_disconnect)

        cmd = CommandParser(prog="RECONNECT", description="reconnect to network")
        self.commands.register(cmd, self.cmd_reconnect)

        cmd = CommandParser(
            prog="RAW",
            description="send raw IRC commands",
            epilog=(
                "Arguments (text) are not quoted in any way so it's possible to send ANY command to the server.\n"
                "This is meant as a last resort if the bridge does not have built-in support for some IRC command.\n"
                "\n"
                "Note: You may need to use colon (:) for multi-word arguments, see the IRC RFC for details.\n"
            ),
        )
        cmd.add_argument("text", nargs="+", help="raw text")
        self.commands.register(cmd, self.cmd_raw)

        cmd = CommandParser(
            prog="QUERY",
            description="start a private chat",
            epilog=(
                "Creates a new DM with the target nick. They do not need to be connected for this command to work.\n"
            ),
        )
        cmd.add_argument("nick", help="target nickname")
        cmd.add_argument("message", nargs="*", help="optional message")
        self.commands.register(cmd, self.cmd_query)

        cmd = CommandParser(
            prog="MSG",
            description="send a message without opening a DM",
            epilog=(
                "If the target nick does not exist on the network an error reply may be generated by the server.\n"
            ),
        )
        cmd.add_argument("-s", "--sensitive", action="store_true", help="hide message content from network room")
        cmd.add_argument("nick", help="target nickname")
        cmd.add_argument("message", nargs="+", help="message")
        self.commands.register(cmd, self.cmd_msg)

        cmd = CommandParser(
            prog="CTCP",
            description="send a CTCP command",
            epilog="You probably know what you are doing.",
        )
        cmd.add_argument("nick", help="target nickname")
        cmd.add_argument("command", nargs="+", help="command and arguments")
        self.commands.register(cmd, self.cmd_ctcp)

        cmd = CommandParser(
            prog="CTCPCFG",
            description="enable/disable automatic CTCP replies",
        )
        cmd.add_argument("--enable", dest="enabled", action="store_true", help="Enable CTCP replies")
        cmd.add_argument("--disable", dest="enabled", action="store_false", help="Disable CTCP replies")
        cmd.set_defaults(enabled=None)
        self.commands.register(cmd, self.cmd_ctcpcfg)

        cmd = CommandParser(
            prog="NICKSERV",
            description="send a message to NickServ (if supported by network)",
            epilog="Alias: NS",
        )
        cmd.add_argument("-s", "--sensitive", action="store_true", help="hide message content from network room")
        cmd.add_argument("message", nargs="+", help="message")
        self.commands.register(cmd, self.cmd_nickserv, ["NS"])

        cmd = CommandParser(
            prog="CHANSERV",
            description="send a message to ChanServ (if supported by network)",
            epilog="Alias: CS",
        )
        cmd.add_argument("message", nargs="+", help="message")
        self.commands.register(cmd, self.cmd_chanserv, ["CS"])

        cmd = CommandParser(
            prog="JOIN",
            description="join a channel",
            epilog=(
                "Any channels joined will be persisted between reconnects.\n"
                "\n"
                "Note: Bridge administrators can trivially see the stored channel key if they want to.\n"
            ),
        )
        cmd.add_argument("channel", help="target channel")
        cmd.add_argument("key", nargs="?", help="channel key")
        self.commands.register(cmd, self.cmd_join)

        cmd = CommandParser(
            prog="PLUMB",
            description="plumb a room",
            epilog=(
                "Plumbs a channel in single-puppeted mode. This will make the bridge join the room and then join the"
                " configured IRC channel.\n"
            ),
        )
        cmd.add_argument("room", help="target Matrix room ID (eg. !uniqueid:your-homeserver)")
        cmd.add_argument("channel", help="target channel")
        cmd.add_argument("key", nargs="?", help="channel key")
        self.commands.register(cmd, self.cmd_plumb)

        cmd = CommandParser(prog="UMODE", description="set user modes")
        cmd.add_argument("flags", help="user mode flags")
        self.commands.register(cmd, self.cmd_umode)

        cmd = CommandParser(
            prog="WAIT",
            description="wait specified amount of time",
            epilog=("Use with AUTOCMD to add delays between commands."),
        )
        cmd.add_argument("seconds", help="how many seconds to wait")
        self.commands.register(cmd, self.cmd_wait)

        cmd = CommandParser(
            prog="PILLS",
            description="configure automatic pills",
        )
        cmd.add_argument(
            "--length", help="minimum length of nick to generate a pill, setting to 0 disables this feature", type=int
        )
        cmd.add_argument("--ignore", help="comma separated list of nicks to ignore for pills")
        self.commands.register(cmd, self.cmd_pills)

        cmd = CommandParser(
            prog="AUTOQUERY",
            description="enable or disable automatic room creation when getting a message",
        )
        cmd.add_argument("--enable", dest="enabled", action="store_true", help="Enable autoquery")
        cmd.add_argument("--disable", dest="enabled", action="store_false", help="Disable autoquery")
        cmd.set_defaults(enabled=None)
        self.commands.register(cmd, self.cmd_autoquery)

        cmd = CommandParser(prog="WHOIS", description="send a WHOIS(IS) command")
        cmd.add_argument("nick", help="target nick")
        self.commands.register(cmd, self.cmd_whois)

        cmd = CommandParser(prog="WHOAMI", description="send a WHOIS(IS) for ourself")
        self.commands.register(cmd, self.cmd_whoami)

        cmd = CommandParser(
            prog="ROOM",
            description="run a room command from network room",
            epilog=(
                "Try 'ROOM #foo' to get the list of commands for a room."
                "If a command generates IRC replies in a bouncer room they will appear in the room itself."
            ),
        )
        cmd.add_argument("target", help="IRC channel or nick that has a room")
        cmd.add_argument("command", help="Command and arguments", nargs=argparse.REMAINDER)
        self.commands.register(cmd, self.cmd_room)

        cmd = CommandParser(
            prog="AVATAR",
            description="change or show IRC network ghost avatar",
            epilog="Note: This changes the avatar for everyone using this bridge, use with caution.",
        )
        cmd.add_argument("nick", help="nick")
        cmd.add_argument("url", nargs="?", help="new avatar URL (mxc:// format)")
        cmd.add_argument("--remove", help="remove avatar", action="store_true")
        self.commands.register(cmd, self.cmd_avatar)

        cmd = CommandParser(prog="REJOIN", description="configure rejoin behavior for channel rooms")
        cmd.add_argument("--enable-invite", dest="invite", action="store_true", help="Enable rejoin on invite")
        cmd.add_argument("--disable-invite", dest="invite", action="store_false", help="Disable rejoin on invite")
        cmd.add_argument("--enable-kick", dest="kick", action="store_true", help="Enable rejoin on kick")
        cmd.add_argument("--disable-kick", dest="kick", action="store_false", help="Disable rejoin on kick")
        cmd.set_defaults(invite=None, kick=None)
        self.commands.register(cmd, self.cmd_rejoin)

        cmd = CommandParser(prog="STATUS", description="show current network status")
        self.commands.register(cmd, self.cmd_status)

        self.mx_register("m.room.message", self.on_mx_message)

    @staticmethod
    async def create(serv, network, user_id, name):
        room_id = await serv.create_room(name, "Network room for {}".format(network), [user_id])
        room = NetworkRoom(room_id, user_id, serv, [serv.user_id, user_id], bans=[])
        room.from_config({"name": network})
        await room.save()
        serv.register_room(room)
        await room.show_help()
        return room

    def from_config(self, config: dict):
        if "name" in config:
            self.name = config["name"]
        else:
            raise Exception("No name key in config for NetworkRoom")

        if "connected" in config:
            self.connected = config["connected"]

        if "nick" in config:
            self.nick = config["nick"]

        if "username" in config:
            self.username = config["username"]

        if "ircname" in config:
            self.ircname = config["ircname"]

        if "password" in config:
            self.password = config["password"]

        if "sasl_username" in config:
            self.sasl_username = config["sasl_username"]

        if "sasl_password" in config:
            self.sasl_password = config["sasl_password"]

        if "autocmd" in config:
            self.autocmd = config["autocmd"]

        if "pills_length" in config:
            self.pills_length = config["pills_length"]

        if "pills_ignore" in config:
            self.pills_ignore = config["pills_ignore"]

        if "autoquery" in config:
            self.autoquery = config["autoquery"]

        if "allow_ctcp" in config:
            self.allow_ctcp = config["allow_ctcp"]

        if "tls_cert" in config:
            self.tls_cert = config["tls_cert"]

        if "rejoin_invite" in config:
            self.rejoin_invite = config["rejoin_invite"]

        if "rejoin_kick" in config:
            self.rejoin_kick = config["rejoin_kick"]

    def to_config(self) -> dict:
        return {
            "name": self.name,
            "connected": self.connected,
            "nick": self.nick,
            "username": self.username,
            "ircname": self.ircname,
            "password": self.password,
            "sasl_username": self.sasl_username,
            "sasl_password": self.sasl_password,
            "autocmd": self.autocmd,
            "allow_ctcp": self.allow_ctcp,
            "tls_cert": self.tls_cert,
            "pills_length": self.pills_length,
            "pills_ignore": self.pills_ignore,
            "rejoin_invite": self.rejoin_invite,
            "rejoin_kick": self.rejoin_kick,
        }

    def is_valid(self) -> bool:
        if self.name is None:
            return False

        # if user leaves network room and it's not connected we can clean it up
        if not self.in_room(self.user_id) and not self.connected:
            return False

        return True

    async def show_help(self):
        self.send_notice_html(f"Welcome to the network room for <b>{html.escape(self.name)}</b>!")

        try:
            return await self.commands.trigger("HELP")
        except CommandParserError as e:
            return self.send_notice(str(e))

    async def on_mx_message(self, event) -> None:
        if str(event.content.msgtype) != "m.text" or event.sender == self.serv.user_id:
            return

        # ignore edits
        if event.content.get_edit():
            return

        try:
            if event.content.formatted_body:
                lines = str(IRCMatrixParser.parse(event.content.formatted_body)).split("\n")
            else:
                lines = event.content.body.split("\n")

            command = lines.pop(0)
            tail = "\n".join(lines) if len(lines) > 0 else None

            await self.commands.trigger(command, tail)
        except CommandParserError as e:
            self.send_notice(str(e))

    async def cmd_connect(self, args) -> None:
        await self.connect()

    async def cmd_disconnect(self, args) -> None:
        self.disconnect = True

        if self.backoff_task:
            self.backoff_task.cancel()

        self.backoff = 0
        self.next_server = 0
        self.connected_at = 0

        if self.connected:
            self.connected = False
            await self.save()

        if self.conn:
            self.send_notice("Disconnecting...")
            self.conn.disconnect()

    @connected
    async def cmd_reconnect(self, args) -> None:
        await self.cmd_disconnect(Namespace())
        await self.cmd_connect(Namespace())

    @connected
    async def cmd_raw(self, args) -> None:
        self.conn.send_raw(" ".join(args.text))

    @connected
    async def cmd_query(self, args) -> None:
        # TODO: validate nick doesn't look like a channel
        target = args.nick.lower()
        message = " ".join(args.message)

        if target in self.rooms:
            room = self.rooms[target]
            await self.az.intent.invite_user(room.id, self.user_id)
            self.send_notice("Inviting back to private chat with {}.".format(args.nick))
        else:
            room = PrivateRoom.create(self, args.nick)
            self.rooms[room.name] = room
            self.send_notice("You have been invited to private chat with {}.".format(args.nick))

        if len(message) > 0:
            self.conn.privmsg(target, message)
            self.send_notice(f"Sent out-of-room message to {target}: {message}")

    @connected
    async def cmd_msg(self, args) -> None:
        message = " ".join(args.message)
        self.conn.privmsg(args.nick, message)

        if args.sensitive:
            message = "***"

        self.send_notice(f"{self.conn.real_nickname} -> {args.nick}: {message}")

    @connected
    async def cmd_ctcp(self, args) -> None:
        command = args.command[0].upper()
        command_args = " ".join(args.command[1:])
        self.conn.ctcp(command, args.nick, command_args)
        self.send_notice_html(
            f"{self.conn.real_nickname} -> <b>{args.nick}</b> CTCP <b>{html.escape(command)}</b> {html.escape(command_args)}"
        )

    async def cmd_ctcpcfg(self, args) -> None:
        if args.enabled is not None:
            self.allow_ctcp = args.enabled
            await self.save()

        self.send_notice(f"CTCP replies are {'enabled' if self.allow_ctcp else 'disabled'}")

    @connected
    async def cmd_nickserv(self, args) -> None:
        message = " ".join(args.message)
        self.conn.send_raw("NICKSERV " + message)

        if args.sensitive:
            message = "***"

        self.send_notice(f"{self.conn.real_nickname} -> NickServ: {message}")

    @connected
    async def cmd_chanserv(self, args) -> None:
        message = " ".join(args.message)
        self.conn.send_raw("CHANSERV " + message)
        self.send_notice(f"{self.conn.real_nickname} -> ChanServ: {message}")

    @connected
    async def cmd_join(self, args) -> None:
        channel = args.channel

        if re.match(r"^[A-Za-z0-9]", channel):
            channel = "#" + channel

        # cache key so we can store later if join succeeds
        self.keys[channel.lower()] = args.key

        self.conn.join(channel, args.key)

    @connected
    async def cmd_plumb(self, args) -> None:
        channel = args.channel

        if re.match(r"^[A-Za-z0-9]", channel):
            channel = "#" + channel

        if not self.serv.is_admin(self.user_id):
            self.send_notice("Plumbing is currently reserved for admins only.")
            return

        room = await PlumbedRoom.create(id=args.room, network=self, channel=channel, key=args.key)
        self.conn.join(room.name, room.key)

    @connected
    async def cmd_umode(self, args) -> None:
        self.conn.mode(self.conn.real_nickname, args.flags)

    async def cmd_wait(self, args) -> None:
        try:
            seconds = float(args.seconds)
            if seconds > 0 and seconds < 30:
                await asyncio.sleep(seconds)
            else:
                self.send_notice(f"Unreasonable wait time: {args.seconds}")
        except ValueError:
            self.send_notice(f"Invalid wait time: {args.seconds}")

    def get_nick(self):
        if self.nick:
            return self.nick

        return self.user_id.split(":")[0][1:]

    async def cmd_nick(self, args) -> None:
        if args.nick is None:
            nick = self.get_nick()
            if self.conn and self.conn.connected:
                self.send_notice(f"Current nickname: {self.conn.real_nickname} (configured: {nick})")
            else:
                self.send_notice(f"Configured nickname: {nick}")
            return

        self.nick = args.nick
        await self.save()
        self.send_notice("Nickname set to {}".format(self.nick))

        if self.conn and self.conn.connected:
            if self.keepnick_task:
                self.keepnick_task.cancel()
                self.keepnick_task = None

            self.conn.nick(args.nick)

    def get_ident(self):
        idents = self.serv.config["idents"]

        # use admin set override if exists
        if self.user_id in idents:
            return idents[self.user_id][:8]

        # return mxid digest if no custom ident
        return (
            "m-"
            + b32encode(hashlib.sha1(self.user_id.encode("utf-8")).digest())
            .decode("utf-8")
            .replace("=", "")[:6]
            .lower()
        )

    async def cmd_username(self, args) -> None:
        if args.remove:
            self.username = None
            await self.save()
            self.send_notice("Username removed.")
            return

        if args.username is None:
            self.send_notice(f"Configured username: {str(self.username)}")
            return

        self.username = args.username
        await self.save()
        self.send_notice(f"Username set to {self.username}")

    async def cmd_realname(self, args) -> None:
        if args.remove:
            self.ircname = None
            await self.save()
            self.send_notice("Realname removed.")
            return

        if args.name is None:
            if self.ircname:
                self.send_notice(f"Configured realname: {self.ircname}")
            else:
                self.send_notice("No configured realname.")
            return

        self.ircname = args.name
        await self.save()
        self.send_notice(f"Realname set to {self.ircname}")

    async def cmd_password(self, args) -> None:
        if args.remove:
            self.password = None
            await self.save()
            self.send_notice("Password removed.")
            return

        if args.password is None:
            self.send_notice(f"Configured password: {self.password if self.password else ''}")
            return

        self.password = args.password
        await self.save()
        self.send_notice(f"Password set to {self.password}")

    async def cmd_sasl(self, args) -> None:
        if args.remove:
            self.sasl_username = None
            self.sasl_password = None
            await self.save()
            self.send_notice("SASL credentials removed.")
            return

        if args.username is None and args.password is None:
            self.send_notice(f"SASL username: {self.sasl_username}")
            self.send_notice(f"SASL password: {self.sasl_password}")
            return

        if args.username:
            self.sasl_username = args.username

        if args.password:
            self.sasl_password = args.password

        await self.save()
        self.send_notice("SASL credentials updated.")

    async def cmd_certfp(self, args) -> None:
        if args.remove:
            self.tls_cert = None
            await self.save()
            self.send_notice("CertFP certificate removed.")
        elif args.set:
            if args._tail is None:
                example = (
                    "CERTFP --set\n"
                    "-----BEGIN CERTIFICATE-----\n"
                    "...\n"
                    "-----END CERTIFICATE-----\n"
                    "-----BEGIN PRIVATE KEY-----\n"
                    "...\n"
                    "-----END PRIVATE KEY-----\n"
                )
                self.send_notice_html(
                    f"<p>Expected the certificate to follow command. Certificate not updated.</p><pre><code>{example}</code></pre>"
                )
                return

            # simple sanity checks it possibly looks alright
            if not args._tail.startswith("-----"):
                self.send_notice("This does not look like a PEM certificate.")
                return

            if "-----BEGIN CERTIFICATE----" not in args._tail:
                self.send_notice("Certificate section is missing.")
                return

            if "-----BEGIN PRIVATE KEY----" not in args._tail:
                self.send_notice("Private key section is missing.")
                return

            self.tls_cert = args._tail
            await self.save()
            self.send_notice("Client certificate saved.")
        else:
            if self.tls_cert:
                self.send_notice("CertFP certificate exists.")
            else:
                self.send_notice("CertFP certificate does not exist.")

    async def cmd_autocmd(self, args) -> None:
        autocmd = " ".join(args.command)

        if args.remove:
            self.autocmd = None
            await self.save()
            self.send_notice("Autocmd removed.")
            return

        if autocmd == "":
            self.send_notice(f"Configured autocmd: {self.autocmd if self.autocmd else ''}")
            return

        self.autocmd = autocmd
        await self.save()
        self.send_notice(f"Autocmd set to {self.autocmd}")

    @connected
    async def cmd_whois(self, args) -> None:
        self.conn.whois(f"{args.nick} {args.nick}")

    @connected
    async def cmd_whoami(self, args) -> None:
        self.conn.whois(f"{self.conn.real_nickname} {self.conn.real_nickname}")

    async def cmd_room(self, args) -> None:
        target = args.target.lower()

        if target not in self.rooms:
            self.send_notice(f"No room for {args.target}")
            return

        room = self.rooms[target]

        if len(args.command) == 0:
            args.command = ["HELP"]

        await room.commands.trigger_args(args.command, forward=True)

    async def cmd_avatar(self, args):
        if not self.serv.is_admin(self.user_id):
            self.send_notice("Setting avatars is reserved for admins only.")
            return

        # ensure the ghost exists
        irc_user_id = await self.serv.ensure_irc_user_id(self.name, args.nick, update_cache=False)

        if args.remove:
            await self.az.intent.user(irc_user_id).set_avatar_url("")
            self.send_notice("Avatar removed.")
        elif args.url:
            await self.az.intent.user(irc_user_id).set_avatar_url(args.url)
            self.send_notice("Avatar updated.")
        else:
            avatar_url = await self.az.intent.user(irc_user_id).get_avatar_url(irc_user_id)
            if avatar_url:
                self.send_notice(f"Current avatar for {args.nick} is {avatar_url}")
            else:
                self.send_notice(f"{args.nick} does not have a custom avatar.")

    async def cmd_pills(self, args) -> None:
        save = False

        if args.length is not None:
            self.pills_length = args.length
            self.send_notice(f"Pills minimum length set to {self.pills_length}")
            save = True
        else:
            self.send_notice(f"Pills minimum length is {self.pills_length}")

        if args.ignore is not None:
            self.pills_ignore = list(map(lambda x: x.strip(), args.ignore.split(",")))
            self.send_notice(f"Pills ignore list set to {', '.join(self.pills_ignore)}")
            save = True
        else:
            if len(self.pills_ignore) == 0:
                self.send_notice("Pills ignore list is empty.")
            else:
                self.send_notice(f"Pills ignore list: {', '.join(self.pills_ignore)}")

        if save:
            await self.save()

    async def cmd_autoquery(self, args) -> None:
        if args.enabled is not None:
            self.autoquery = args.enabled
            await self.save()

        self.send_notice(f"Autoquery is {'enabled' if self.autoquery else 'disabled'}")

    async def cmd_rejoin(self, args) -> None:
        if args.invite is not None:
            self.rejoin_invite = args.invite
            await self.save()

        if args.kick is not None:
            self.rejoin_kick = args.kick
            await self.save()

        self.send_notice(f"Rejoin on invite is {'enabled' if self.rejoin_invite else 'disabled'}")
        self.send_notice(f"Rejoin on kick is {'enabled' if self.rejoin_kick else 'disabled'}")

    async def cmd_status(self, args) -> None:
        if self.connected_at > 0:
            conntime = asyncio.get_event_loop().time() - self.connected_at
            conntime = str(datetime.timedelta(seconds=int(conntime)))
            self.send_notice(f"Connected for {conntime}")

            if self.real_host[0] != "?":
                self.send_notice(f"Connected from host {self.real_host}")
        else:
            self.send_notice("Not connected to server.")

        await self.cmd_nick(Namespace(nick=None))

        pms = []
        chans = []
        plumbs = []

        for room in self.rooms.values():
            if type(room) == PrivateRoom:
                pms.append(room.name)
            elif type(room) == ChannelRoom:
                chans.append(room.name)
            elif type(room) == PlumbedRoom:
                plumbs.append(room.name)

        if len(chans) > 0:
            self.send_notice(f"Channels: {', '.join(chans)}")

        if len(plumbs) > 0:
            self.send_notice(f"Plumbs: {', '.join(plumbs)}")

        if len(pms) > 0:
            self.send_notice(f"PMs: {', '.join(pms)}")

    def kickban(self, channel: str, nick: str, reason: str) -> None:
        self.pending_kickbans[nick].append((channel, reason))
        self.conn.whois(f"{nick}")

    def _do_kickban(self, channel: str, user_data: Dict[str, str], reason: str) -> None:
        self.conn.mode(channel, f"+b *!*@{user_data['host']}")
        self.conn.kick(channel, user_data["nick"], reason)

    async def connect(self) -> None:
        if self.connlock.locked():
            self.send_notice("Already connecting.")
            return

        async with self.connlock:
            if self.conn and self.conn.connected:
                self.send_notice("Already connected.")
                return

            self.disconnect = False
            await self._connect()

    async def _connect(self) -> None:
        # attach loose sub-rooms to us
        for type in [PrivateRoom, ChannelRoom, PlumbedRoom]:
            for room in self.serv.find_rooms(type, self.user_id):
                if room.name not in self.rooms and (
                    room.network_id == self.id or (room.network_id is None and room.network_name == self.name)
                ):
                    room.network = self
                    # this doubles as a migration
                    if room.network_id is None:
                        logging.debug(f"{self.id} attaching and migrating {room.id}")
                        room.network_id = self.id
                        await room.save()
                    else:
                        logging.debug(f"{self.id} attaching {room.id}")
                    self.rooms[room.name] = room

        # force cleanup
        if self.conn:
            self.conn.close()
            self.conn = None

        network = self.serv.config["networks"][self.name]

        # reset whois and kickbans buffers
        self.whois_data.clear()
        self.pending_kickbans.clear()

        while not self.disconnect:
            if self.name not in self.serv.config["networks"]:
                self.send_notice("This network does not exist on this bridge anymore.")
                return

            if len(network["servers"]) == 0:
                self.connected = False
                self.send_notice("No servers to connect for this network.")
                await self.save()
                return

            server = network["servers"][self.next_server % len(network["servers"])]
            self.next_server += 1

            try:
                with_tls = ""
                ssl_ctx = False
                server_hostname = None
                if server["tls"] or ("tls_insecure" in server and server["tls_insecure"]):
                    ssl_ctx = ssl.create_default_context()
                    if "tls_insecure" in server and server["tls_insecure"]:
                        with_tls = " with insecure TLS"
                        ssl_ctx.check_hostname = False
                        ssl_ctx.verify_mode = ssl.CERT_NONE
                    else:
                        with_tls = " with TLS"
                        ssl_ctx.verify_mode = ssl.CERT_REQUIRED

                    if self.tls_cert:
                        with_tls += " and CertFP"

                        # do this awful hack to allow the SSL stack to load the cert and key
                        cert_file = tempfile.NamedTemporaryFile()
                        cert_file.write(self.tls_cert.encode("utf-8"))
                        cert_file.flush()

                        ssl_ctx.load_cert_chain(cert_file.name)

                        cert_file.close()

                    server_hostname = server["address"]

                proxy = None
                sock = None
                address = server["address"]
                port = server["port"]

                with_proxy = ""
                if "proxy" in server and server["proxy"] is not None and len(server["proxy"]) > 0:
                    proxy = Proxy.from_url(server["proxy"])
                    address = port = None
                    with_proxy = " through a SOCKS proxy"

                self.send_notice(f"Connecting to {server['address']}:{server['port']}{with_tls}{with_proxy}...")

                if proxy:
                    sock = await proxy.connect(dest_host=server["address"], dest_port=server["port"])

                if self.sasl_username and self.sasl_password:
                    self.send_notice(f"Using SASL credentials for username {self.sasl_username}")

                reactor = HeisenReactor(loop=asyncio.get_event_loop())
                irc_server = reactor.server()
                irc_server.buffer_class = buffer.LenientDecodingLineBuffer
                factory = irc.connection.AioFactory(ssl=ssl_ctx, sock=sock, server_hostname=server_hostname)
                self.conn = await irc_server.connect(
                    address,
                    port,
                    self.get_nick(),
                    self.password,
                    username=self.get_ident() if self.username is None else self.username,
                    ircname=self.ircname,
                    connect_factory=factory,
                    sasl_username=self.sasl_username,
                    sasl_password=self.sasl_password,
                )

                self.conn.add_global_handler("disconnect", self.on_disconnect)

                self.conn.add_global_handler("welcome", self.on_welcome)
                self.conn.add_global_handler("umodeis", self.on_umodeis)
                self.conn.add_global_handler("channelmodeis", self.on_pass0)
                self.conn.add_global_handler("channelcreate", self.on_pass0)
                self.conn.add_global_handler("notopic", self.on_pass0)
                self.conn.add_global_handler("currenttopic", self.on_pass0)
                self.conn.add_global_handler("topicinfo", self.on_pass0)
                self.conn.add_global_handler("namreply", self.on_pass1)
                self.conn.add_global_handler("endofnames", self.on_pass0)
                self.conn.add_global_handler("banlist", self.on_pass0)
                self.conn.add_global_handler("endofbanlist", self.on_pass0)
                self.conn.add_global_handler("328", self.on_pass0)  # channel URL
                self.conn.add_global_handler("396", self.on_displayed_host)

                # 400-599
                self.conn.add_global_handler("nosuchnick", self.on_pass_if)
                self.conn.add_global_handler("nosuchchannel", self.on_pass_if)
                self.conn.add_global_handler("cannotsendtochan", self.on_pass0)
                self.conn.add_global_handler("nicknameinuse", self.on_nicknameinuse)
                self.conn.add_global_handler("erroneusnickname", self.on_erroneusnickname)
                self.conn.add_global_handler("unavailresource", self.on_unavailresource)
                self.conn.add_global_handler("usernotinchannel", self.on_pass1)
                self.conn.add_global_handler("notonchannel", self.on_pass0)
                self.conn.add_global_handler("useronchannel", self.on_pass1)
                self.conn.add_global_handler("nologin", self.on_pass1)
                self.conn.add_global_handler("keyset", self.on_pass)
                self.conn.add_global_handler("channelisfull", self.on_pass)
                self.conn.add_global_handler("inviteonlychan", self.on_pass)
                self.conn.add_global_handler("bannedfromchan", self.on_pass)
                self.conn.add_global_handler("badchannelkey", self.on_pass0)
                self.conn.add_global_handler("badchanmask", self.on_pass)
                self.conn.add_global_handler("nochanmodes", self.on_pass)
                self.conn.add_global_handler("banlistfull", self.on_pass)
                self.conn.add_global_handler("cannotknock", self.on_pass)
                self.conn.add_global_handler("chanoprivsneeded", self.on_pass0)

                # protocol
                # FIXME: error
                self.conn.add_global_handler("join", self.on_join)
                self.conn.add_global_handler("join", self.on_join_update_host)
                self.conn.add_global_handler("kick", self.on_pass)
                self.conn.add_global_handler("mode", self.on_pass)
                self.conn.add_global_handler("part", self.on_part)
                self.conn.add_global_handler("privmsg", self.on_privmsg)
                self.conn.add_global_handler("privnotice", self.on_privnotice)
                self.conn.add_global_handler("pubmsg", self.on_pass)
                self.conn.add_global_handler("pubnotice", self.on_pass)
                self.conn.add_global_handler("quit", self.on_quit)
                self.conn.add_global_handler("invite", self.on_invite)
                self.conn.add_global_handler("wallops", self.on_wallops)
                # FIXME: action
                self.conn.add_global_handler("topic", self.on_pass)
                self.conn.add_global_handler("nick", self.on_nick)
                self.conn.add_global_handler("umode", self.on_umode)

                self.conn.add_global_handler("kill", self.on_kill)
                self.conn.add_global_handler("error", self.on_error)

                # whois
                self.conn.add_global_handler("whoisuser", self.on_whoisuser)
                self.conn.add_global_handler("whoisserver", self.on_whoisserver)
                self.conn.add_global_handler("whoischannels", self.on_whoischannels)
                self.conn.add_global_handler("whoisidle", self.on_whoisidle)
                self.conn.add_global_handler("whoisaccount", self.on_whoisaccount)  # is logged in as
                self.conn.add_global_handler("whoisoperator", self.on_whoisoperator)
                self.conn.add_global_handler("338", self.on_whoisrealhost)  # is actually using host
                self.conn.add_global_handler("away", self.on_away)
                self.conn.add_global_handler("endofwhois", self.on_endofwhois)

                # generated
                self.conn.add_global_handler("ctcp", self.on_ctcp)
                self.conn.add_global_handler("ctcpreply", self.on_ctcpreply)
                self.conn.add_global_handler("action", lambda conn, event: None)

                # anything not handled above
                self.conn.add_global_handler("unhandled_events", self.on_server_message)

                if not self.connected:
                    self.connected = True
                    await self.save()

                self.disconnect = False
                self.connected_at = asyncio.get_event_loop().time()

                # run connection registration (SASL, user, nick)
                await self.conn.register()

                return
            except TimeoutError:
                self.send_notice("Connection timed out.")
            except irc.client.ServerConnectionError as e:
                self.send_notice(str(e))
                self.send_notice(f"Failed to connect: {str(e)}")
            except Exception as e:
                self.send_notice(f"Failed to connect: {str(e)}")

            if self.backoff < 1800:
                self.backoff += 5

            self.send_notice(f"Trying next server in {self.backoff} seconds...")

            self.backoff_task = asyncio.ensure_future(asyncio.sleep(self.backoff))
            try:
                await self.backoff_task
            except asyncio.CancelledError:
                break
            finally:
                self.backoff_task = None

        self.send_notice("Connection aborted.")

    def on_disconnect(self, conn, event) -> None:
        self.conn.disconnect()
        self.conn.close()
        self.conn = None

        # if we were connected for a while, consider the server working
        if self.connected_at > 0 and asyncio.get_event_loop().time() - self.connected_at > 300:
            self.backoff = 0
            self.next_server = 0
            self.connected_at = 0

        if self.connected and not self.disconnect:
            if self.backoff < 1800:
                self.backoff += 5

            self.send_notice(f"Disconnected, reconnecting in {self.backoff} seconds...")

            async def later(self):
                self.backoff_task = asyncio.ensure_future(asyncio.sleep(self.backoff))
                try:
                    await self.backoff_task
                    await self.connect()
                except asyncio.CancelledError:
                    self.send_notice("Reconnect cancelled.")
                finally:
                    self.backoff_task = None

            asyncio.ensure_future(later(self))
        else:
            self.send_notice("Disconnected.")

    @ircroom_event()
    def on_pass(self, conn, event) -> None:
        logging.warning(f"IRC room event '{event.type}' fell through, target was from command.")
        source = self.source_text(conn, event)
        args = " ".join(event.arguments)
        source = self.source_text(conn, event)
        target = str(event.target)
        self.send_notice_html(f"<b>{source} {event.type} {target}</b> {html.escape(args)}")

    @ircroom_event()
    def on_pass_if(self, conn, event) -> None:
        self.send_notice(" ".join(event.arguments))

    @ircroom_event()
    def on_pass_or_ignore(self, conn, event) -> None:
        pass

    @ircroom_event(target_arg=0)
    def on_pass0(self, conn, event) -> None:
        logging.warning(f"IRC room event '{event.type}' fell through, target was '{event.arguments[0]}'.")
        self.send_notice(" ".join(event.arguments))

    @ircroom_event(target_arg=1)
    def on_pass1(self, conn, event) -> None:
        logging.warning(f"IRC room event '{event.type}' fell through, target was '{event.arguments[1]}'.")
        self.send_notice(" ".join(event.arguments))

    def on_server_message(self, conn, event) -> None:
        # test if the first argument is an ongoing whois target
        if len(event.arguments) > 0 and event.arguments[0].lower() in self.whois_data:
            data = self.whois_data[event.arguments[0].lower()]
            if "extra" not in data:
                data["extra"] = []

            data["extra"].append(" ".join(event.arguments[1:]))
        else:
            self.send_notice(" ".join(event.arguments))

    def on_umodeis(self, conn, event) -> None:
        self.send_notice(f"Your user mode is: {event.arguments[0]}")

    def on_umode(self, conn, event) -> None:
        self.send_notice(f"User mode changed for {event.target}: {event.arguments[0]}")

    def on_displayed_host(self, conn, event) -> None:
        self.send_notice(" ".join(event.arguments))
        if event.target == conn.real_nickname:
            self.real_host = event.arguments[0]

    def source_text(self, conn, event) -> str:
        source = None

        if event.source is not None:
            source = str(event.source.nick)

            if event.source.user is not None and event.source.host is not None:
                source += f" ({event.source.user}@{event.source.host})"
        else:
            source = conn.server

        return source

    @ircroom_event()
    def on_privnotice(self, conn, event) -> None:
        # show unhandled notices in server room
        source = self.source_text(conn, event)
        plain, formatted = parse_irc_formatting(event.arguments[0])
        self.send_notice_html(f"Notice from <b>{source}:</b> {formatted if formatted else html.escape(plain)}")

    @ircroom_event()
    def on_ctcp(self, conn, event) -> None:
        source = self.source_text(conn, event)

        reply = None
        if self.allow_ctcp:
            if event.arguments[0] == "VERSION":
                reply = f"VERSION Heisenbridge v{__version__}"
            elif event.arguments[0] == "PING" and len(event.arguments) > 1:
                reply = f"PING {event.arguments[1]}"
            elif event.arguments[0] == "TIME":
                reply = f"TIME {unix_to_local(time())}"
            else:
                self.send_notice_html(
                    f"<b>{source}</b> requested unknown <b>CTCP {html.escape(' '.join(event.arguments))}</b>"
                )

        if reply is not None:
            self.conn.ctcp_reply(event.source.nick, reply)
            self.send_notice_html(
                f"<b>{source}</b> requested CTCP <b>{html.escape(event.arguments[0])}</b> -> {html.escape(reply)}"
            )
        else:
            self.send_notice_html(f"<b>{source}</b> requested CTCP <b>{html.escape(event.arguments[0])}</b> (ignored)")

    @ircroom_event()
    def on_ctcpreply(self, conn, event) -> None:
        command = event.arguments[0].upper()
        reply = event.arguments[1]

        self.send_notice_html(
            f"CTCP <b>{html.escape(command)}</b> reply from <b>{event.source.nick}</b>: {html.escape(reply)}"
        )

    def on_welcome(self, conn, event) -> None:
        self.on_server_message(conn, event)

        async def later():
            await asyncio.sleep(2)

            if self.autocmd is not None:
                self.send_notice("Executing autocmd and waiting a bit before joining channels...")
                try:
                    await self.commands.trigger(
                        self.autocmd, allowed=["RAW", "MSG", "NICKSERV", "NS", "CHANSERV", "CS", "UMODE", "WAIT"]
                    )
                except Exception as e:
                    self.send_notice(f"Autocmd failed: {str(e)}")
                await asyncio.sleep(4)

            # detect disconnect before we get to join
            if not self.conn or not self.conn.connected:
                return

            channels = []
            keyed_channels = []

            for room in self.rooms.values():
                if type(room) is ChannelRoom or type(room) is PlumbedRoom:
                    if room.key:
                        keyed_channels.append((room.name, room.key))
                    else:
                        channels.append(room.name)

            if len(channels) > 0:
                self.send_notice(f"Joining channels {', '.join(channels)}")
                self.conn.join(",".join(channels))

            if len(keyed_channels) > 0:
                for channel, key in keyed_channels:
                    self.send_notice(f"Joining {channel} with a key")
                    self.conn.join(channel, key)

        asyncio.ensure_future(later())

    @ircroom_event()
    def on_privmsg(self, conn, event) -> None:
        # slightly backwards
        target = event.source.nick.lower()

        if target not in self.rooms:

            if self.autoquery:

                async def later():
                    # reuse query command to create a room
                    await self.cmd_query(Namespace(nick=event.source.nick, message=[]))

                    # push the message
                    room = self.rooms[target]
                    room.on_privmsg(conn, event)

                asyncio.ensure_future(later())
            else:
                source = self.source_text(conn, event)
                self.send_notice_html(f"Message from <b>{source}:</b> {html.escape(event.arguments[0])}")
        else:
            room = self.rooms[target]
            if not room.in_room(self.user_id):
                asyncio.ensure_future(self.az.intent.invite_user(self.rooms[target].id, self.user_id))

    @ircroom_event()
    def on_join(self, conn, event) -> None:
        target = event.target.lower()

        logging.debug(f"Handling JOIN to {target} by {event.source.nick} (we are {self.conn.real_nickname})")

        # create a ChannelRoom in response to JOIN
        if event.source.nick == self.conn.real_nickname and target not in self.rooms:
            logging.debug("Pre-flight check for JOIN ok, going to create it...")
            self.rooms[target] = ChannelRoom.create(self, event.target)

            # pass this event through
            self.rooms[target].on_join(conn, event)

    def on_join_update_host(self, conn, event) -> None:
        # update for split long
        if event.source.nick == self.conn.real_nickname and (
            self.real_host != event.source.host or self.real_user != event.source.user
        ):
            self.real_host = event.source.host
            self.real_user = event.source.user
            logging.debug(f"Self host updated to '{self.real_host}', user to '{self.real_user}'")

    @ircroom_event()
    def on_part(self, conn, event) -> None:
        if conn.real_nickname == event.source.nick:
            self.send_notice_html(f"You left <b>{html.escape(event.target)}</b>")
        else:
            # should usually never end up here
            self.send_notice_html(f"<b>{html.escape(event.source.nick)}</b> left <b>{html.escape(event.target)}</b>")

    def on_quit(self, conn, event) -> None:
        # leave channels
        for room in self.rooms.values():
            if type(room) is ChannelRoom or type(room) is PlumbedRoom:
                room.on_quit(conn, event)

    def on_nick(self, conn, event) -> None:
        # the IRC library changes real_nickname before running handlers
        if event.target == self.conn.real_nickname:
            logging.debug(f"Detected own nick change to {event.target}")
            if event.target == self.get_nick():
                self.send_notice(f"You're now known as {event.target}")

        old_irc_user_id = self.serv.irc_user_id(self.name, event.source.nick)
        new_irc_user_id = self.serv.irc_user_id(self.name, event.target)

        # special case where only cases change, ensure will update displayname sometime in the future
        if old_irc_user_id == new_irc_user_id:
            asyncio.ensure_future(self.serv.ensure_irc_user_id(self.name, event.target))

        # leave and join channels
        for room in self.rooms.values():
            if type(room) is ChannelRoom or type(room) is PlumbedRoom:
                room.rename(event.source.nick, event.target)

    def on_nicknameinuse(self, conn, event) -> None:
        self.send_notice(f"Nickname {event.arguments[0]} is in use")
        if self.conn.real_nickname == "":
            newnick = event.arguments[0] + "_"
            self.conn.nick(newnick)
        self.keepnick()

    def on_erroneusnickname(self, conn, event) -> None:
        self.send_notice(f"Nickname {event.arguments[0]} is erroneus and was rejected by the server")

    @ircroom_event()
    def on_unavailresource(self, conn, event) -> None:
        if event.arguments[0][0] not in ["#", "!", "&"]:
            self.send_notice(f"Nickname {event.arguments[0]} is currently unavailable")
            if self.conn.real_nickname == "":
                newnick = event.arguments[0] + "_"
                self.conn.nick(newnick)
            self.keepnick()
        else:
            self.send_notice(f"Channel {event.arguments[0]} is currently unavailable")

    def keepnick(self):
        if self.keepnick_task:
            self.keepnick_task.cancel()

        self.send_notice(f"Trying to set nickname to {self.get_nick()} again after five minutes.")

        def try_keepnick():
            self.keepnick_task = None

            if not self.conn or not self.conn.connected:
                return

            self.conn.nick(self.get_nick())

        self.keepnick_task = asyncio.get_event_loop().call_later(300, try_keepnick)

    def on_invite(self, conn, event) -> None:
        rejoin = ""

        target = event.arguments[0].lower()
        if self.rejoin_invite and target in self.rooms:
            self.conn.join(event.arguments[0])
            rejoin = " (rejoin on invite is enabled, joining back)"

        self.send_notice_html(
            f"<b>{event.source.nick}</b> has invited you to <b>{html.escape(event.arguments[0])}</b>{rejoin}"
        )

    def on_wallops(self, conn, event) -> None:
        plain, formatted = parse_irc_formatting(event.target)
        self.send_notice_html(f"<b>WALLOPS {event.source.nick}</b>: {formatted if formatted else html.escape(plain)}")

    @ircroom_event()
    def on_kill(self, conn, event) -> None:
        if event.target == conn.real_nickname:
            source = self.source_text(conn, event)
            self.send_notice_html(f"Killed by <b>{source}</b>: {html.escape(event.arguments[0])}")

            # do not reconnect after KILL
            self.disconnect = True

    def on_error(self, conn, event) -> None:
        self.send_notice_html(f"<b>ERROR</b>: {html.escape(event.target)}")

    def on_whoisuser(self, conn, event) -> None:
        data = self.whois_data[event.arguments[0].lower()]
        data["nick"] = event.arguments[0]
        data["user"] = event.arguments[1]
        data["host"] = event.arguments[2]
        data["realname"] = event.arguments[4]

    def on_whoisserver(self, conn, event) -> None:
        data = self.whois_data[event.arguments[0].lower()]
        data["server"] = f"{event.arguments[1]} ({event.arguments[2]})"

    def on_whoischannels(self, conn, event) -> None:
        data = self.whois_data[event.arguments[0].lower()]
        data["channels"] = event.arguments[1]

    def on_whoisidle(self, conn, event) -> None:
        data = self.whois_data[event.arguments[0].lower()]
        data["idle"] = str(datetime.timedelta(seconds=int(event.arguments[1])))
        if len(event.arguments) > 2:
            data["signon"] = unix_to_local(int(event.arguments[2]))

    def on_whoisaccount(self, conn, event) -> None:
        data = self.whois_data[event.arguments[0].lower()]
        data["account"] = event.arguments[1]

    def on_whoisoperator(self, conn, event) -> None:
        data = self.whois_data[event.arguments[0].lower()]
        data["ircop"] = event.arguments[1]

    def on_whoisrealhost(self, conn, event) -> None:
        data = self.whois_data[event.arguments[0].lower()]
        data["realhost"] = event.arguments[1]

    def on_away(self, conn, event) -> None:
        if event.arguments[0].lower() in self.whois_data:
            self.whois_data[event.arguments[0].lower()]["away"] = event.arguments[1]
        else:
            self.send_notice(f"{event.arguments[0]} is away: {event.arguments[1]}")

    def on_endofwhois(self, conn, event) -> None:
        nick = event.arguments[0].lower()
        data = self.whois_data[nick]
        del self.whois_data[nick]

        if nick in self.pending_kickbans:
            channels = self.pending_kickbans[nick]
            del self.pending_kickbans[nick]
            for channel, reason in channels:
                self._do_kickban(channel, data, reason)
            return

        reply = []
        fallback = []
        reply.append("<table>")

        for k in [
            "nick",
            "user",
            "host",
            "realname",
            "realhost",
            "away",
            "channels",
            "server",
            "ircop",
            "idle",
            "signon",
            "account",
        ]:
            if k in data:
                reply.append(f"<tr><td>{k}</td><td>{html.escape(data[k])}</td>")
                fallback.append(f"{k}: {data[k]}")

        if "extra" in data:
            for v in data["extra"]:
                reply.append(f"<tr><td></td><td>{html.escape(v)}</td>")
                fallback.append(f"{data['nick']} {v}")

        reply.append("</table>")

        # forward whois reply to a DM if exists
        target = self
        if nick in self.rooms:
            target = self.rooms[nick]

        target.send_notice(formatted="".join(reply), text="\n".join(fallback))
