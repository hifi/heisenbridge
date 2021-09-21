import asyncio
import hashlib
import html
import logging
import re
import ssl
from argparse import Namespace
from base64 import b32encode
from typing import Any
from typing import Dict

import irc.client
import irc.client_aio
import irc.connection
from jaraco.stream import buffer

from heisenbridge.channel_room import ChannelRoom
from heisenbridge.command_parse import CommandManager
from heisenbridge.command_parse import CommandParser
from heisenbridge.command_parse import CommandParserError
from heisenbridge.irc import HeisenReactor
from heisenbridge.plumbed_room import PlumbedRoom
from heisenbridge.private_room import parse_irc_formatting
from heisenbridge.private_room import PrivateRoom
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

    # state
    commands: CommandManager
    conn: Any
    rooms: Dict[str, Room]
    connecting: bool
    real_host: str

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

        self.commands = CommandManager()
        self.conn = None
        self.rooms = {}
        self.connlock = asyncio.Lock()
        self.disconnect = True
        self.real_host = "?" * 63  # worst case default
        self.keys = {}  # temp dict of join channel keys
        self.keepnick_task = None  # async task

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
            prog="IRCNAME",
            description="set ircname (realname)",
            epilog=("Setting a new ircname requires reconnecting to the network.\n"),
        )
        cmd.add_argument("ircname", nargs="?", help="new ircname")
        cmd.add_argument("--remove", action="store_true", help="remove stored ircname")
        self.commands.register(cmd, self.cmd_ircname)

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
            prog="AUTOCMD",
            description="run commands on connect",
            epilog=(
                "If the network you are connecting to does not support server password to identify you automatically"
                " can set this to send a command before joining channels.\n"
                "\n"
                'Example (QuakeNet): AUTOCMD "UMODE +x; MSG Q@CServe.quakenet.org auth foo bar"\n'
                "Example (OFTC): AUTOCMD NICKSERV identify foo bar\n"
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
        cmd.add_argument("nick", help="target nickname")
        cmd.add_argument("message", nargs="+", help="message")
        self.commands.register(cmd, self.cmd_msg)

        cmd = CommandParser(
            prog="NICKSERV",
            description="send a message to NickServ (if supported by network)",
            epilog="Alias: NS",
        )
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
            prog="PLUMBCFG",
            description="set configurable settings for plumbed rooms",
            epilog=("Use the 'no' version of a boolean setting to disable it."),
        )
        cmd.add_argument("channel", help="plumbed channel")
        cmd.add_argument(
            "--max-lines", type=int, help="Number of lines to pass through from a message before truncating"
        )
        cmd.add_argument("--pastebin", dest="pastebin", action="store_true", help="Enable pastebin of long messages")
        cmd.add_argument(
            "--no-pastebin", dest="pastebin", action="store_false", help="Disable pastebin of long messages"
        )
        cmd.add_argument(
            "--displaynames", dest="displaynames", action="store_true", help="Enable displaynames for relaybot mode"
        )
        cmd.add_argument(
            "--no-displaynames",
            dest="displaynames",
            action="store_false",
            help="Disable displaynames for relaybot mode",
        )
        cmd.add_argument("--sync", choices=["off", "lazy", "half", "full"], help="Set member sync for room")
        cmd.add_argument(
            "--disambiguation",
            dest="disambiguation",
            action="store_true",
            help="Enable disambiguation for relaybot mode",
        )
        cmd.add_argument(
            "--no-disambiguation",
            dest="disambiguation",
            action="store_false",
            help="Disable disambiguation for relaybot mode",
        )
        cmd.add_argument(
            "--zwsp", dest="zwsp", action="store_true", help="Enable Zero-Width-Space anti-ping for relaybot mode"
        )
        cmd.add_argument(
            "--no-zwsp", dest="zwsp", action="store_false", help="Disable Zero-Width-Space anti-ping for relaybot mode"
        )
        cmd.add_argument("--notice", dest="notice", action="store_true", help="Allow Matrix notices to be relayed")
        cmd.add_argument(
            "--no-notice", dest="notice", action="store_false", help="Disallow Matrix notices to be relayed"
        )
        cmd.set_defaults(max_lines=None, pastebin=None, displaynames=None, disambiguation=None, zwsp=None, notice=None)
        self.commands.register(cmd, self.cmd_plumbcfg)

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

        self.mx_register("m.room.message", self.on_mx_message)

    @staticmethod
    async def create(serv, name, user_id):
        room_id = await serv.create_room(name, "Network room for {}".format(name), [user_id])
        room = NetworkRoom(room_id, user_id, serv, [serv.user_id, user_id])
        room.from_config({"name": name})
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
            "pills_length": self.pills_length,
            "pills_ignore": self.pills_ignore,
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
        if event["content"]["msgtype"] != "m.text" or event["sender"] == self.serv.user_id:
            return

        # ignore edits
        if "m.new_content" in event["content"]:
            return

        try:
            await self.commands.trigger(event["content"]["body"])
        except CommandParserError as e:
            self.send_notice(str(e))

    async def cmd_connect(self, args) -> None:
        await self.connect()

    async def cmd_disconnect(self, args) -> None:
        if not self.disconnect:
            self.send_notice("Aborting connection attempt after backoff.")
            self.disconnect = True

        if self.connected:
            self.connected = False
            await self.save()

        if self.conn:
            self.send_notice("Disconnecting...")
            self.conn.disconnect()

    @connected
    async def cmd_reconnect(self, args) -> None:
        self.send_notice("Reconnecting...")
        self.conn.disconnect()
        await self.connect()

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
            await self.serv.api.post_room_invite(room.id, self.user_id)
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
        # TODO: validate nick doesn't look like a channel
        target = args.nick.lower()
        message = " ".join(args.message)

        self.conn.privmsg(target, message)
        self.send_notice(f"{self.conn.real_nickname} -> {target}: {message}")

    @connected
    async def cmd_nickserv(self, args) -> None:
        message = " ".join(args.message)

        self.send_notice(f"{self.conn.real_nickname} -> NickServ: {message}")
        self.conn.send_raw("NICKSERV " + message)

    @connected
    async def cmd_chanserv(self, args) -> None:
        message = " ".join(args.message)

        self.send_notice(f"{self.conn.real_nickname} -> ChanServ: {message}")
        self.conn.send_raw("CHANSERV " + message)

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

    async def cmd_plumbcfg(self, args) -> None:
        if args.channel not in self.rooms:
            self.send_notice(f"Not in {args.channel}")
            return

        room = self.rooms[args.channel]
        if type(room) is not PlumbedRoom:
            self.send_notice(f"{args.channel} is not a plumbed room")
            return

        save = False

        if args.max_lines is not None:
            room.max_lines = args.max_lines
            save = True

        if args.pastebin is not None:
            room.use_pastebin = args.pastebin
            save = True

        if args.displaynames is not None:
            room.use_displaynames = args.displaynames
            save = True

        if args.sync is not None:
            room.member_sync = args.sync
            save = True

        if args.disambiguation is not None:
            room.use_disambiguation = args.disambiguation
            save = True

        if args.zwsp is not None:
            room.use_zwsp = args.zwsp
            save = True

        if args.notice is not None:
            room.allow_notice = args.notice
            save = True

        self.send_notice(f"{args.channel} settings:")
        self.send_notice(f"\tMax lines is {room.max_lines}")
        self.send_notice(f"\tPastebin is {'enabled' if room.use_pastebin else 'disabled'}")
        self.send_notice(f"\tDisplaynames is {'enabled' if room.use_displaynames else 'disabled'}")
        self.send_notice(f"\tDisambiguation is {'enabled' if room.use_disambiguation else 'disabled'}")
        self.send_notice(f"\tZero-Width-Space is {'enabled' if room.use_zwsp else 'disabled'}")
        self.send_notice(f"\tMember sync is {room.member_sync}")
        self.send_notice(f"\tNotice relay is {'enabled' if room.allow_notice else 'disabled'}")

        if save:
            await room.save()
            self.send_notice("Settings saved.")

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

    async def cmd_ircname(self, args) -> None:
        if args.remove:
            self.ircname = None
            await self.save()
            self.send_notice("Ircname removed.")
            return

        if args.ircname is None:
            self.send_notice(f"Configured ircname: {str(self.ircname)}")
            return

        self.ircname = args.ircname
        await self.save()
        self.send_notice(f"Ircname set to {self.ircname}")

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

    async def connect(self) -> None:
        if self.connlock.locked():
            self.send_notice("Already connecting.")
            return

        async with self.connlock:
            await self._connect()

    async def _connect(self) -> None:
        self.disconnect = False

        if self.conn and self.conn.connected:
            self.send_notice("Already connected.")
            return

        # attach loose sub-rooms to us
        for room in self.serv.find_rooms(PrivateRoom, self.user_id):
            if room.name not in self.rooms and room.network_name == self.name:
                logging.debug(f"NetworkRoom {self.id} attaching PrivateRoom {room.id}")
                room.network = self
                self.rooms[room.name] = room

        for room in self.serv.find_rooms(ChannelRoom, self.user_id):
            if room.name not in self.rooms and room.network_name == self.name:
                logging.debug(f"NetworkRoom {self.id} attaching ChannelRoom {room.id}")
                room.network = self
                self.rooms[room.name] = room

        for room in self.serv.find_rooms(PlumbedRoom, self.user_id):
            if room.name not in self.rooms and room.network_name == self.name:
                logging.debug(f"NetworkRoom {self.id} attaching PlumbedRoom {room.id}")
                room.network = self
                self.rooms[room.name] = room

        # force cleanup
        if self.conn:
            self.conn.close()
            self.conn = None

        network = self.serv.config["networks"][self.name]

        backoff = 10

        while not self.disconnect:
            if self.name not in self.serv.config["networks"]:
                self.send_notice("This network does not exist on this bridge anymore.")
                return

            if len(network["servers"]) == 0:
                self.connected = False
                self.send_notice("No servers to connect for this network.")
                await self.save()
                return

            for i, server in enumerate(network["servers"]):
                if i > 0:
                    await asyncio.sleep(10)

                try:
                    with_tls = ""
                    ssl_ctx = False
                    if server["tls"]:
                        ssl_ctx = ssl.create_default_context()
                        if "tls_insecure" in server and server["tls_insecure"]:
                            with_tls = " with insecure TLS"
                            ssl_ctx.check_hostname = False
                            ssl_ctx.verify_mode = ssl.CERT_NONE
                        else:
                            with_tls = " with TLS"
                            ssl_ctx.verify_mode = ssl.CERT_REQUIRED

                    self.send_notice(f"Connecting to {server['address']}:{server['port']}{with_tls}...")

                    if self.sasl_username and self.sasl_password:
                        self.send_notice(f"Using SASL credentials for username {self.sasl_username}")

                    reactor = HeisenReactor(loop=asyncio.get_event_loop())
                    irc_server = reactor.server()
                    irc_server.buffer_class = buffer.LenientDecodingLineBuffer
                    factory = irc.connection.AioFactory(ssl=ssl_ctx)
                    self.conn = await irc_server.connect(
                        server["address"],
                        server["port"],
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

                    # 400-599
                    self.conn.add_global_handler("nosuchnick", self.on_pass_if)
                    self.conn.add_global_handler("nosuchchannel", self.on_pass_if)
                    self.conn.add_global_handler("cannotsendtochan", self.on_pass_if)
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
                    self.conn.add_global_handler("part", self.on_pass)
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

                    # generated
                    self.conn.add_global_handler("ctcp", self.on_ctcp)
                    self.conn.add_global_handler("ctcpreply", self.on_pass)
                    self.conn.add_global_handler("action", lambda conn, event: None)

                    # anything not handled above
                    self.conn.add_global_handler("unhandled_events", self.on_server_message)

                    if not self.connected:
                        self.connected = True
                        await self.save()

                    self.disconnect = False

                    # run connection registration (SASL, user, nick)
                    await self.conn.register()

                    return
                except TimeoutError:
                    self.send_notice("Connection timed out.")
                except irc.client.ServerConnectionError as e:
                    self.send_notice(str(e))
                    self.send_notice(f"Failed to connect: {str(e)}")
                    self.disconnect = True
                except Exception as e:
                    self.send_notice(f"Failed to connect: {str(e)}")

            if not self.disconnect:
                self.send_notice(f"Tried all servers, waiting {backoff} seconds before trying again.")
                await asyncio.sleep(backoff)

                if backoff < 60:
                    backoff += 5

        self.send_notice("Connection aborted.")

    def on_disconnect(self, conn, event) -> None:
        self.conn.disconnect()
        self.conn.close()
        self.conn = None

        if self.connected and not self.disconnect:
            self.send_notice("Disconnected, reconnecting...")

            async def later():
                await asyncio.sleep(10)
                if not self.disconnect:
                    await self.connect()

            asyncio.ensure_future(later())
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
        self.send_notice(" ".join(event.arguments))

    def on_umodeis(self, conn, event) -> None:
        self.send_notice(f"Your user mode is: {event.arguments[0]}")

    def on_umode(self, conn, event) -> None:
        self.send_notice(f"User mode changed for {event.target}: {event.arguments[0]}")

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
        self.send_notice_html(f"Notice from <b>{source}:</b> {html.escape(event.arguments[0])}")

    @ircroom_event()
    def on_ctcp(self, conn, event) -> None:
        # show unhandled ctcps in server room
        source = self.source_text(conn, event)
        self.send_notice_html(f"<b>{source}</b> requested <b>CTCP {html.escape(event.arguments[0])}</b> (ignored)")

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
                asyncio.ensure_future(self.serv.api.post_room_invite(self.rooms[target].id, self.user_id))

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
        if event.source.nick == self.conn.real_nickname and self.real_host != event.source.host:
            self.real_host = event.source.host
            logging.debug(f"Self host updated to '{self.real_host}'")

    def on_quit(self, conn, event) -> None:
        irc_user_id = self.serv.irc_user_id(self.name, event.source.nick)

        # leave channels
        for room in self.rooms.values():
            if type(room) is ChannelRoom or type(room) is PlumbedRoom:
                room._remove_puppet(irc_user_id, f"Quit: {event.arguments[0]}")

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
        self.send_notice_html(f"<b>{event.source.nick}</b> has invited you to <b>{html.escape(event.arguments[0])}</b>")

    def on_wallops(self, conn, event) -> None:
        plain, formatted = parse_irc_formatting(event.target)
        self.send_notice_html(f"<b>WALLOPS {event.source.nick}</b>: {formatted if formatted else html.escape(plain)}")

    @ircroom_event()
    def on_kill(self, conn, event) -> None:
        if event.target == conn.real_nickname:
            source = self.source_text(conn, event)
            self.send_notice_html(f"Killed by <b>{source}</b>: {html.escape(event.arguments[0])}")

            # do not reconnect after KILL
            self.connected = False

    def on_error(self, conn, event) -> None:
        self.send_notice_html(f"<b>ERROR</b>: {html.escape(event.target)}")
