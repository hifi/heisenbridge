import asyncio
import logging
import re
from argparse import Namespace
from typing import Any
from typing import Dict

import irc.client
import irc.client_aio
from jaraco.stream import buffer

from heisenbridge.channel_room import ChannelRoom
from heisenbridge.command_parse import CommandManager
from heisenbridge.command_parse import CommandParser
from heisenbridge.command_parse import CommandParserError
from heisenbridge.future_queue import FutureQueue
from heisenbridge.private_room import PrivateRoom
from heisenbridge.room import Room


# convert a synchronous method to asynchronous with a queue, recursion will lock
def future(f):
    def wrapper(*args, **kwargs):
        return asyncio.ensure_future(args[0].queue.schedule(f(*args, **kwargs)))

    return wrapper


def connected(f):
    async def wrapper(*args, **kwargs):
        self = args[0]

        if not self.conn or not self.conn.connected:
            await self.send_notice("Need to be connected to use this command.")
            return

        return await f(*args, **kwargs)

    return wrapper


# forwards events to private and channel rooms or queues them
def ircroom_event(target_arg=None):
    def outer(f):
        async def wrapper(self, conn, event):
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
                    return await room_f(conn, event)
                except AttributeError:
                    logging.warning(f"Expected {room.__name__} to have on_{event.type} but didn't")

            return await f(self, conn, event)

        return wrapper

    return outer


class NetworkRoom(Room):
    # configuration stuff
    name: str
    connected: bool
    nick: str
    password: str
    autocmd: str

    # state
    commands: CommandManager
    conn: Any
    rooms: Dict[str, Room]
    queue: FutureQueue
    connecting: bool

    def init(self):
        self.name = None
        self.connected = False
        self.nick = None
        self.password = None
        self.autocmd = None

        self.commands = CommandManager()
        self.conn = None
        self.rooms = {}
        self.queue = FutureQueue(timeout=30)
        self.connecting = False

        cmd = CommandParser(prog="NICK", description="Change nickname")
        cmd.add_argument("nick", nargs="?", help="new nickname")
        self.commands.register(cmd, self.cmd_nick)

        cmd = CommandParser(prog="PASSWORD", description="Set server password")
        cmd.add_argument("password", nargs="?", help="new password")
        cmd.add_argument("--remove", action="store_true", help="remove stored password")
        self.commands.register(cmd, self.cmd_password)

        cmd = CommandParser(prog="AUTOCMD", description="Run a RAW IRC command on connect (to identify)")
        cmd.add_argument("command", nargs="*", help="raw IRC command")
        cmd.add_argument("--remove", action="store_true", help="remove stored command")
        self.commands.register(cmd, self.cmd_autocmd)

        cmd = CommandParser(prog="CONNECT", description="Connect to network")
        self.commands.register(cmd, self.cmd_connect)

        cmd = CommandParser(prog="DISCONNECT", description="Disconnect from network")
        self.commands.register(cmd, self.cmd_disconnect)

        cmd = CommandParser(prog="RECONNECT", description="Reconnect to network")
        self.commands.register(cmd, self.cmd_reconnect)

        cmd = CommandParser(prog="RAW", description="Send raw IRC commands")
        cmd.add_argument("text", nargs="+", help="raw text")
        self.commands.register(cmd, self.cmd_raw)

        cmd = CommandParser(prog="QUERY", description="Start a private chat")
        cmd.add_argument("nick", help="target nickname")
        cmd.add_argument("message", nargs="*", help="optional message")
        self.commands.register(cmd, self.cmd_query)

        cmd = CommandParser(prog="MSG", description="Send a message without opening a DM")
        cmd.add_argument("nick", help="target nickname")
        cmd.add_argument("message", nargs="+", help="message")
        self.commands.register(cmd, self.cmd_msg)

        cmd = CommandParser(prog="JOIN", description="Join a channel")
        cmd.add_argument("channel", help="target channel")
        cmd.add_argument("key", nargs="?", help="channel key")
        self.commands.register(cmd, self.cmd_join)

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

        if "password" in config:
            self.password = config["password"]

        if "autocmd" in config:
            self.autocmd = config["autocmd"]

    def to_config(self) -> dict:
        return {
            "name": self.name,
            "connected": self.connected,
            "nick": self.nick,
            "password": self.password,
            "autocmd": self.autocmd,
        }

    def is_valid(self) -> bool:
        if self.name is None:
            return False

        # if user leaves network room and it's not connected we can clean it up
        if not self.in_room(self.user_id) and not self.connected:
            return False

        return True

    async def show_help(self):
        await self.send_notice_html("Welcome to the network room for <b>{}</b>!".format(self.name))

        try:
            return await self.commands.trigger("HELP")
        except CommandParserError as e:
            return await self.send_notice(str(e))

    async def on_mx_message(self, event) -> None:
        if event["content"]["msgtype"] != "m.text" or event["user_id"] == self.serv.user_id:
            return True

        try:
            return await self.commands.trigger(event["content"]["body"])
        except CommandParserError as e:
            return await self.send_notice(str(e))

    async def cmd_connect(self, args) -> None:
        await self.connect()

    @connected
    async def cmd_disconnect(self, args) -> None:
        if self.connected:
            self.connected = False
            await self.save()

        await self.send_notice("Disconnecting...")
        self.conn.disconnect()

    @connected
    async def cmd_reconnect(self, args) -> None:
        await self.send_notice("Reconnecting...")
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
            await self.send_notice("Inviting back to private chat with {}.".format(args.nick))
        else:
            room = await PrivateRoom.create(self, args.nick)
            self.rooms[room.name] = room
            await self.send_notice("You have been invited to private chat with {}.".format(args.nick))

        if len(message) > 0:
            self.conn.privmsg(target, message)
            await self.send_notice(f"Sent out-of-room message to {target}: {message}")

    @connected
    async def cmd_msg(self, args) -> None:
        # TODO: validate nick doesn't look like a channel
        target = args.nick.lower()
        message = " ".join(args.message)

        self.conn.privmsg(target, message)
        await self.send_notice(f"{self.conn.real_nickname} -> {target}: {message}")

    @connected
    async def cmd_join(self, args) -> None:
        channel = args.channel

        if re.match(r"^[A-Za-z0-9]", channel):
            channel = "#" + channel

        self.conn.join(channel, args.key)

    async def cmd_nick(self, args) -> None:
        if args.nick is None:
            if self.conn and self.conn.connected:
                await self.send_notice(f"Current nickname: {self.conn.real_nickname} (configured: {self.nick})")
            else:
                await self.send_notice("Configured nickname: {}".format(self.nick))
            return

        self.nick = args.nick
        await self.save()
        await self.send_notice("Nickname set to {}".format(self.nick))

        if self.conn and self.conn.connected:
            self.conn.nick(args.nick)

    async def cmd_password(self, args) -> None:
        if args.remove:
            self.password = None
            await self.save()
            await self.send_notice("Password removed.")
            return

        if args.password is None:
            await self.send_notice(f"Configured password: {self.password if self.password else ''}")
            return

        self.password = args.password
        await self.save()
        await self.send_notice(f"Password set to {self.password}")

    async def cmd_autocmd(self, args) -> None:
        autocmd = " ".join(args.command)

        if args.remove:
            self.autocmd = None
            await self.save()
            await self.send_notice("Autocmd removed.")
            return

        if autocmd == "":
            await self.send_notice(f"Configured autocmd: {self.autocmd if self.autocmd else ''}")
            return

        self.autocmd = autocmd
        await self.save()
        await self.send_notice(f"Autocmd set to {self.autocmd}")

    async def connect(self) -> None:
        if self.connecting or (self.conn and self.conn.connected):
            await self.send_notice("Already connected.")
            return

        if self.nick is None:
            await self.send_notice("You need to configure a nick first, see HELP")
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

        # force cleanup
        if self.conn:
            self.conn = None

        self.connecting = True

        network = self.serv.config["networks"][self.name]
        await self.send_notice("Connecting...")

        try:
            reactor = irc.client_aio.AioReactor(loop=asyncio.get_event_loop())
            server = reactor.server()
            server.buffer_class = buffer.LenientDecodingLineBuffer
            self.conn = await server.connect(network["servers"][0], 6667, self.nick, self.password)

            self.conn.add_global_handler("disconnect", self.on_disconnect)

            # 001-099
            self.conn.add_global_handler("welcome", self.on_server_message)
            self.conn.add_global_handler("yourhost", self.on_server_message)
            self.conn.add_global_handler("created", self.on_server_message)
            self.conn.add_global_handler("myinfo", self.on_server_message)
            self.conn.add_global_handler("featurelist", self.on_server_message)
            self.conn.add_global_handler("020", self.on_server_message)

            # 200-299
            self.conn.add_global_handler("tracelink", self.on_server_message)
            self.conn.add_global_handler("traceconnecting", self.on_server_message)
            self.conn.add_global_handler("tracehandshake", self.on_server_message)
            self.conn.add_global_handler("traceunknown", self.on_server_message)
            self.conn.add_global_handler("traceoperator", self.on_server_message)
            self.conn.add_global_handler("traceuser", self.on_server_message)
            self.conn.add_global_handler("traceserver", self.on_server_message)
            self.conn.add_global_handler("traceservice", self.on_server_message)
            self.conn.add_global_handler("tracenewtype", self.on_server_message)
            self.conn.add_global_handler("traceclass", self.on_server_message)
            self.conn.add_global_handler("tracereconnect", self.on_server_message)
            self.conn.add_global_handler("statslinkinfo", self.on_server_message)
            self.conn.add_global_handler("statscommands", self.on_server_message)
            self.conn.add_global_handler("statscline", self.on_server_message)
            self.conn.add_global_handler("statsnline", self.on_server_message)
            self.conn.add_global_handler("statsiline", self.on_server_message)
            self.conn.add_global_handler("statskline", self.on_server_message)
            self.conn.add_global_handler("statsqline", self.on_server_message)
            self.conn.add_global_handler("statsyline", self.on_server_message)
            self.conn.add_global_handler("endofstats", self.on_server_message)
            self.conn.add_global_handler("umodeis", self.on_umodeis)
            self.conn.add_global_handler("serviceinfo", self.on_server_message)
            self.conn.add_global_handler("endofservices", self.on_server_message)
            self.conn.add_global_handler("service", self.on_server_message)
            self.conn.add_global_handler("servlist", self.on_server_message)
            self.conn.add_global_handler("servlistend", self.on_server_message)
            self.conn.add_global_handler("statslline", self.on_server_message)
            self.conn.add_global_handler("statsuptime", self.on_server_message)
            self.conn.add_global_handler("statsoline", self.on_server_message)
            self.conn.add_global_handler("statshline", self.on_server_message)
            self.conn.add_global_handler("luserconns", self.on_server_message)
            self.conn.add_global_handler("luserclient", self.on_server_message)
            self.conn.add_global_handler("luserop", self.on_server_message)
            self.conn.add_global_handler("luserunknown", self.on_server_message)
            self.conn.add_global_handler("luserchannels", self.on_server_message)
            self.conn.add_global_handler("luserme", self.on_server_message)
            self.conn.add_global_handler("adminme", self.on_server_message)
            self.conn.add_global_handler("adminloc1", self.on_server_message)
            self.conn.add_global_handler("adminloc2", self.on_server_message)
            self.conn.add_global_handler("adminemail", self.on_server_message)
            self.conn.add_global_handler("tracelog", self.on_server_message)
            self.conn.add_global_handler("endoftrace", self.on_server_message)
            self.conn.add_global_handler("tryagain", self.on_server_message)
            self.conn.add_global_handler("n_local", self.on_server_message)
            self.conn.add_global_handler("n_global", self.on_server_message)

            # 300-399
            self.conn.add_global_handler("none", self.on_server_message)
            self.conn.add_global_handler("away", self.on_server_message)
            self.conn.add_global_handler("userhost", self.on_server_message)
            self.conn.add_global_handler("ison", self.on_server_message)
            self.conn.add_global_handler("unaway", self.on_server_message)
            self.conn.add_global_handler("nowaway", self.on_server_message)
            self.conn.add_global_handler("whoisuser", self.on_server_message)
            self.conn.add_global_handler("whoisserver", self.on_server_message)
            self.conn.add_global_handler("whoisoperator", self.on_server_message)
            self.conn.add_global_handler("whowasuser", self.on_server_message)
            self.conn.add_global_handler("endofwho", self.on_server_message)
            self.conn.add_global_handler("whoischanop", self.on_server_message)
            self.conn.add_global_handler("whoisidle", self.on_server_message)
            self.conn.add_global_handler("endofwhois", self.on_server_message)
            self.conn.add_global_handler("whoischannels", self.on_server_message)
            self.conn.add_global_handler("liststart", self.on_server_message)
            self.conn.add_global_handler("list", self.on_server_message)
            self.conn.add_global_handler("listend", self.on_server_message)
            self.conn.add_global_handler("channelmodeis", self.on_pass0)
            self.conn.add_global_handler("channelcreate", self.on_pass0)
            self.conn.add_global_handler("whoisaccount", self.on_server_message)
            self.conn.add_global_handler("notopic", self.on_pass)
            self.conn.add_global_handler("currenttopic", self.on_pass0)
            # self.conn.add_global_handler("topicinfo", self.on_server_message) # not needed right now
            self.conn.add_global_handler("inviting", self.on_server_message)
            self.conn.add_global_handler("summoning", self.on_server_message)
            self.conn.add_global_handler("invitelist", self.on_server_message)
            self.conn.add_global_handler("endofinvitelist", self.on_server_message)
            self.conn.add_global_handler("exceptlist", self.on_server_message)
            self.conn.add_global_handler("endofexceptlist", self.on_server_message)
            self.conn.add_global_handler("version", self.on_server_message)
            self.conn.add_global_handler("whoreply", self.on_server_message)
            self.conn.add_global_handler("namreply", self.on_pass1)
            self.conn.add_global_handler("whospcrpl", self.on_server_message)
            self.conn.add_global_handler("killdone", self.on_server_message)
            self.conn.add_global_handler("closing", self.on_server_message)
            self.conn.add_global_handler("closeend", self.on_server_message)
            self.conn.add_global_handler("links", self.on_server_message)
            self.conn.add_global_handler("endoflinks", self.on_server_message)
            self.conn.add_global_handler("endofnames", self.on_pass0)
            self.conn.add_global_handler("banlist", self.on_pass0)
            self.conn.add_global_handler("endofbanlist", self.on_pass0)
            self.conn.add_global_handler("endofwhowas", self.on_server_message)
            self.conn.add_global_handler("info", self.on_server_message)
            self.conn.add_global_handler("motd", self.on_server_message)
            self.conn.add_global_handler("infostart", self.on_server_message)
            self.conn.add_global_handler("endofinfo", self.on_server_message)
            self.conn.add_global_handler("motdstart", self.on_server_message)
            self.conn.add_global_handler("endofmotd", self.on_endofmotd)

            # 400-599
            self.conn.add_global_handler("nosuchnick", self.on_pass_if)
            self.conn.add_global_handler("nosuchserver", self.on_server_message)
            self.conn.add_global_handler("nosuchchannel", self.on_pass_if)
            self.conn.add_global_handler("cannotsendtochan", self.on_pass_if)
            self.conn.add_global_handler("toomanychannels", self.on_server_message)
            self.conn.add_global_handler("wasnosuchnick", self.on_server_message)
            self.conn.add_global_handler("toomanytargets", self.on_server_message)
            self.conn.add_global_handler("noorigin", self.on_server_message)
            self.conn.add_global_handler("invalidcapcmd", self.on_server_message)
            self.conn.add_global_handler("norecipient", self.on_server_message)
            self.conn.add_global_handler("notexttosend", self.on_server_message)
            self.conn.add_global_handler("notoplevel", self.on_server_message)
            self.conn.add_global_handler("wildtoplevel", self.on_server_message)
            self.conn.add_global_handler("unknowncommand", self.on_server_message)
            self.conn.add_global_handler("nomotd", self.on_server_message)
            self.conn.add_global_handler("noadmininfo", self.on_server_message)
            self.conn.add_global_handler("fileerror", self.on_server_message)
            self.conn.add_global_handler("nonicknamegiven", self.on_server_message)
            self.conn.add_global_handler("erroneusnickname", self.on_server_message)
            self.conn.add_global_handler("nicknameinuse", self.on_nicknameinuse)
            self.conn.add_global_handler("nickcollision", self.on_server_message)
            self.conn.add_global_handler("unavailresource", self.on_server_message)
            self.conn.add_global_handler("unavailresource", self.on_server_message)
            self.conn.add_global_handler("usernotinchannel", self.on_pass1)
            self.conn.add_global_handler("notonchannel", self.on_pass0)
            self.conn.add_global_handler("useronchannel", self.on_pass1)
            self.conn.add_global_handler("nologin", self.on_pass1)
            self.conn.add_global_handler("summondisabled", self.on_server_message)
            self.conn.add_global_handler("usersdisabled", self.on_server_message)
            self.conn.add_global_handler("notregistered", self.on_server_message)
            self.conn.add_global_handler("needmoreparams", self.on_server_message)
            self.conn.add_global_handler("alreadyregistered", self.on_server_message)
            self.conn.add_global_handler("nopermforhost", self.on_server_message)
            self.conn.add_global_handler("passwdmismatch", self.on_server_message)
            self.conn.add_global_handler("yourebannedcreep", self.on_server_message)
            self.conn.add_global_handler("youwillbebanned", self.on_server_message)
            self.conn.add_global_handler("keyset", self.on_pass)
            self.conn.add_global_handler("channelisfull", self.on_pass)
            self.conn.add_global_handler("unknownmode", self.on_server_message)
            self.conn.add_global_handler("inviteonlychan", self.on_pass)
            self.conn.add_global_handler("bannedfromchan", self.on_pass)
            self.conn.add_global_handler("badchannelkey", self.on_pass)
            self.conn.add_global_handler("badchanmask", self.on_pass)
            self.conn.add_global_handler("nochanmodes", self.on_pass)
            self.conn.add_global_handler("banlistfull", self.on_pass)
            self.conn.add_global_handler("cannotknock", self.on_pass)
            self.conn.add_global_handler("noprivileges", self.on_server_message)
            self.conn.add_global_handler("chanoprivsneeded", self.on_pass)
            self.conn.add_global_handler("cantkillserver", self.on_server_message)
            self.conn.add_global_handler("restricted", self.on_server_message)
            self.conn.add_global_handler("uniqopprivsneeded", self.on_server_message)
            self.conn.add_global_handler("nooperhost", self.on_server_message)
            self.conn.add_global_handler("noservicehost", self.on_server_message)
            self.conn.add_global_handler("umodeunknownflag", self.on_server_message)
            self.conn.add_global_handler("usersdontmatch", self.on_server_message)

            # protocol
            # FIXME: error
            self.conn.add_global_handler("join", self.on_join)
            self.conn.add_global_handler("kick", self.on_pass)
            self.conn.add_global_handler("mode", self.on_pass)
            self.conn.add_global_handler("part", self.on_pass)
            self.conn.add_global_handler("privmsg", self.on_privmsg)
            self.conn.add_global_handler("privnotice", self.on_privnotice)
            self.conn.add_global_handler("pubmsg", self.on_pass)
            self.conn.add_global_handler("pubnotice", self.on_pass)
            self.conn.add_global_handler("quit", self.on_quit)
            self.conn.add_global_handler("invite", self.on_invite)
            # FIXME: action
            self.conn.add_global_handler("topic", self.on_pass)
            self.conn.add_global_handler("nick", self.on_nick)
            self.conn.add_global_handler("umode", self.on_umode)

            self.conn.add_global_handler("kill", self.on_kill)
            self.conn.add_global_handler("error", self.on_error)

            # generated
            self.conn.add_global_handler("ctcp", self.on_ctcp)

            if not self.connected:
                self.connected = True
                await self.save()

        except TimeoutError:
            await self.send_notice("Connection timed out.")
        except irc.client.ServerConnectionError:
            await self.send_notice("Unexpected connection error, issue was logged.")
            logging.exception("Failed to connect")
        finally:
            self.connecting = False

    @future
    async def on_disconnect(self, conn, event) -> None:
        self.conn.disconnect()
        self.conn = None

        if self.connected:
            await self.send_notice("Disconnected, reconnecting in 10 seconds...")
            await asyncio.sleep(10)
            await self.connect()
        else:
            await self.send_notice("Disconnected.")

    @future
    @ircroom_event()
    async def on_pass(self, conn, event) -> None:
        logging.warning(f"IRC room event '{event.type}' fell through, target was from command.")
        source = self.source_text(conn, event)
        args = " ".join(event.arguments)
        source = self.source_text(conn, event)
        target = str(event.target)
        await self.send_notice_html(f"<b>{source} {event.type} {target}</b> {args}")

    @future
    @ircroom_event()
    async def on_pass_if(self, conn, event) -> None:
        await self.send_notice(" ".join(event.arguments))

    @future
    @ircroom_event()
    async def on_pass_or_ignore(self, conn, event) -> None:
        pass

    @future
    @ircroom_event(target_arg=0)
    async def on_pass0(self, conn, event) -> None:
        logging.warning(f"IRC room event '{event.type}' fell through, target was '{event.arguments[0]}'.")
        await self.send_notice(" ".join(event.arguments))

    @future
    @ircroom_event(target_arg=1)
    async def on_pass1(self, conn, event) -> None:
        logging.warning(f"IRC room event '{event.type}' fell through, target was '{event.arguments[1]}'.")
        await self.send_notice(" ".join(event.arguments))

    @future
    async def on_server_message(self, conn, event) -> None:
        await self.send_notice(" ".join(event.arguments))

    @future
    async def on_umodeis(self, conn, event) -> None:
        await self.send_notice(f"Your user mode is: {event.arguments[0]}")

    @future
    async def on_umode(self, conn, event) -> None:
        await self.send_notice(f"User mode changed for {event.target}: {event.arguments[0]}")

    def source_text(self, conn, event) -> str:
        source = None

        if event.source is not None:
            source = str(event.source.nick)

            if event.source.user is not None and event.source.host is not None:
                source += f" ({event.source.user}@{event.source.host})"
        else:
            source = conn.server

        return source

    @future
    @ircroom_event()
    async def on_privnotice(self, conn, event) -> None:
        # show unhandled notices in server room
        source = self.source_text(conn, event)
        await self.send_notice_html(f"Notice from <b>{source}:</b> {event.arguments[0]}")

    @future
    @ircroom_event()
    async def on_ctcp(self, conn, event) -> None:
        # show unhandled ctcps in server room
        source = self.source_text(conn, event)
        await self.send_notice_html(f"<b>{source}</b> requested <b>CTCP {event.arguments[0]}</b> which we ignored")

    @future
    async def on_endofmotd(self, conn, event) -> None:
        await self.send_notice(" ".join(event.arguments))

        if self.autocmd is not None:
            await self.send_notice("Sending autocmd and waiting a bit before joining channels...")
            self.conn.send_raw(self.autocmd)
            await asyncio.sleep(5)
        else:
            await asyncio.sleep(2)

        # rejoin channels (FIXME: change to comma separated join list)
        for room in self.rooms.values():
            if type(room) is ChannelRoom:
                await self.send_notice("Joining " + room.name)
                self.conn.join(room.name, room.key)

    @future
    @ircroom_event()
    async def on_privmsg(self, conn, event) -> bool:
        # slightly backwards
        target = event.source.nick.lower()

        if target not in self.rooms:
            # reuse query command to create a room
            await self.cmd_query(Namespace(nick=event.source.nick))

            # push the message
            room = self.rooms[target]
            await room.on_privmsg(conn, event)
        else:
            room = self.rooms[target]
            if not room.in_room(self.user_id):
                asyncio.ensure_future(self.serv.api.post_room_invite(self.rooms[target].id, self.user_id))

    @future
    @ircroom_event()
    async def on_join(self, conn, event) -> None:
        target = event.target.lower()

        logging.debug(f"Handling JOIN to {target} by {event.source.nick} (we are {self.conn.real_nickname})")

        # create a ChannelRoom in response to JOIN
        if event.source.nick == self.conn.real_nickname and target not in self.rooms:
            logging.debug("Pre-flight check for JOIN ok, going to create it...")

            self.rooms[target] = await ChannelRoom.create(self, event.target)

    @future
    async def on_quit(self, conn, event) -> None:
        irc_user_id = self.serv.irc_user_id(self.name, event.source.nick)

        # leave channels
        for room in self.rooms.values():
            if type(room) is ChannelRoom:
                if room.in_room(irc_user_id):
                    await self.serv.api.post_room_leave(room.id, irc_user_id)

    @future
    async def on_nick(self, conn, event) -> None:
        old_irc_user_id = self.serv.irc_user_id(self.name, event.source.nick)
        new_irc_user_id = await self.serv.ensure_irc_user_id(self.name, event.target)

        # special case where only cases change, ensure will update displayname
        if old_irc_user_id == new_irc_user_id:
            return

        # leave and join channels
        for room in self.rooms.values():
            if type(room) is ChannelRoom:
                if room.in_room(old_irc_user_id):
                    # notify mx user about the change
                    await room.send_notice("{} is changing nick to {}".format(event.source.nick, event.target))
                    await self.serv.api.post_room_leave(room.id, old_irc_user_id)
                    await self.serv.api.post_room_invite(room.id, new_irc_user_id)
                    await self.serv.api.post_room_join(room.id, new_irc_user_id)

    @future
    async def on_nicknameinuse(self, conn, event) -> None:
        newnick = event.arguments[0] + "_"
        self.conn.nick(newnick)
        await self.send_notice(f"Nickname {event.arguments[0]} is in use, trying {newnick}")

    @future
    async def on_invite(self, conn, event) -> bool:
        await self.send_notice_html(
            "<b>{}</b> has invited you to <b>{}</b>".format(event.source.nick, event.arguments[0])
        )
        return True

    @future
    @ircroom_event()
    async def on_kill(self, conn, event) -> None:
        if event.target == conn.real_nickname:
            source = self.source_text(conn, event)
            await self.send_notice_html(f"Killed by <b>{source}</b>: {event.arguments[0]}")

            # do not reconnect after KILL
            self.connected = False

    @future
    async def on_error(self, conn, event) -> None:
        await self.send_notice_html(f"<b>ERROR</b>: {event.target}")
