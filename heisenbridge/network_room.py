import asyncio
import logging
from argparse import Namespace
from typing import Any
from typing import Dict

import irc.client
import irc.client_aio

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

    # state
    commands: CommandManager
    conn: Any
    rooms: Dict[str, Room]
    queue: FutureQueue
    reactor: Any
    connecting: bool

    def init(self):
        self.name = None
        self.connected = False
        self.nick = None

        self.commands = CommandManager()
        self.conn = None
        self.rooms = {}
        self.queue = FutureQueue(timeout=30)
        self.reactor = irc.client_aio.AioReactor(loop=asyncio.get_event_loop())
        self.connecting = False

        cmd = CommandParser(prog="NICK", description="Change nickname")
        cmd.add_argument("nick", nargs="?", help="new nickname")
        self.commands.register(cmd, self.cmd_nick)

        cmd = CommandParser(prog="CONNECT", description="Connect to network")
        self.commands.register(cmd, self.cmd_connect)

        cmd = CommandParser(prog="DISCONNECT", description="Disconnect from network")
        self.commands.register(cmd, self.cmd_disconnect)

        cmd = CommandParser(prog="RAW", description="Send raw IRC commands")
        cmd.add_argument("text", nargs="+", help="raw text")
        self.commands.register(cmd, self.cmd_raw)

        cmd = CommandParser(prog="QUERY", description="Start a private chat")
        cmd.add_argument("nick", help="target nickname")
        self.commands.register(cmd, self.cmd_query)

        cmd = CommandParser(prog="JOIN", description="Join a channel")
        cmd.add_argument("channel", help="target channel")
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

    def to_config(self) -> dict:
        return {"name": self.name, "connected": self.connected, "nick": self.nick}

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

    async def cmd_disconnect(self, args) -> None:
        if self.connected:
            self.connected = False
            await self.save()

        if not self.conn or not self.conn.connected:
            await self.send_notice("Not connected.")
            return

        await self.send_notice("Disconnecting...")
        self.conn.disconnect()

    async def cmd_raw(self, args) -> None:
        if not self.conn or not self.conn.connected:
            await self.send_notice("Need to be connected to use this command.")
            return

        self.conn.send_raw(" ".join(args.text))

    async def cmd_query(self, args) -> None:
        if not self.conn or not self.conn.connected:
            await self.send_notice("Need to be connected to use this command.")
            return

        # TODO: validate nick doesn't look like a channel
        target = args.nick.lower()

        if target in self.rooms:
            room = self.rooms[target]
            await self.serv.api.post_room_invite(room.id, self.user_id)
            await self.send_notice("Inviting back to private chat with {}.".format(args.nick))
        else:
            self.rooms[room.name] = await PrivateRoom.create(self, args.nick)
            await self.send_notice("You have been invited to private chat with {}.".format(args.nick))

    async def cmd_join(self, args) -> None:
        if not self.conn or not self.conn.connected:
            return

        # TODO: validate channel name and add # prefix if naked
        self.conn.join(args.channel)

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

    async def connect(self) -> None:
        if self.connecting or (self.conn and self.conn.connected):
            await self.send_notice("Already connected.")
            return

        if self.nick is None:
            await self.send_notice("You need to configure a nick first, see HELP")
            return

        # attach loose sub-rooms to us
        for room in self.serv.find_rooms(PrivateRoom, self.user_id):
            if room.network_name == self.name:
                logging.debug(f"NetworkRoom {self.id} attaching PrivateRoom {room.id}")
                room.network = self
                self.rooms[room.name] = room

        for room in self.serv.find_rooms(ChannelRoom, self.user_id):
            if room.network_name == self.name:
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
            self.conn = await self.reactor.server().connect(network["servers"][0], 6667, self.nick)

            self.conn.add_global_handler("disconnect", self.on_disconnect)
            self.conn.add_global_handler("020", self.on_server_message)
            self.conn.add_global_handler("welcome", self.on_server_message)
            self.conn.add_global_handler("yourhost", self.on_server_message)
            self.conn.add_global_handler("created", self.on_server_message)
            self.conn.add_global_handler("myinfo", self.on_server_message)
            self.conn.add_global_handler("featurelist", self.on_server_message)
            self.conn.add_global_handler("umodeunknownflag", self.on_server_message)
            self.conn.add_global_handler("unknowncommand", self.on_server_message)
            self.conn.add_global_handler("nochanmodes", self.on_server_message)
            self.conn.add_global_handler("nosuchnick", self.on_nosuchnick)
            self.conn.add_global_handler("motd", self.on_server_message)
            self.conn.add_global_handler("endofmotd", self.on_endofmotd)
            self.conn.add_global_handler("privnotice", self.on_privnotice)
            self.conn.add_global_handler("privmsg", self.on_privmsg)
            self.conn.add_global_handler("privmsg", self.on_pass)
            self.conn.add_global_handler("join", self.on_join)
            self.conn.add_global_handler("join", self.on_pass)  # for forward only
            self.conn.add_global_handler("quit", self.on_quit)
            self.conn.add_global_handler("nick", self.on_nick)
            self.conn.add_global_handler("nicknameinuse", self.on_nicknameinuse)
            self.conn.add_global_handler("invite", self.on_invite)

            self.conn.add_global_handler("namreply", self.on_namreply)
            self.conn.add_global_handler("endofnames", self.on_endofnames)
            self.conn.add_global_handler("mode", self.on_pass)
            self.conn.add_global_handler("notopic", self.on_pass)
            self.conn.add_global_handler("currenttopic", self.on_endofnames)
            self.conn.add_global_handler("topic", self.on_pass)
            self.conn.add_global_handler("part", self.on_pass)
            self.conn.add_global_handler("pubmsg", self.on_pass)
            self.conn.add_global_handler("pubnotice", self.on_pass)
            self.conn.add_global_handler("ctcp", self.on_pass)

            if not self.connected:
                self.connected = True
                await self.save()

        except irc.client.ServerConnectionError:
            logging.exception("Failed to connect")
        finally:
            self.connecting = False

    @future
    async def on_disconnect(self, conn, event) -> None:
        if self.connected:
            await self.send_notice("Disconnected, reconnecting in 10 seconds...")
            await asyncio.sleep(10)
            await self.connect()
        else:
            await self.send_notice("Disconnected.")

    @future
    @ircroom_event()
    async def on_pass(self, conn, event) -> None:
        logging.warning(f"IRC room event '{event.type}' fell through, target issues?")
        logging.warning(str(event))

    @future
    @ircroom_event(target_arg=1)
    async def on_namreply(self, conn, event) -> None:
        logging.warning(f"IRC room event '{event.type}' fell through.")

    @future
    @ircroom_event(target_arg=0)
    async def on_endofnames(self, conn, event) -> None:
        logging.warning(f"IRC room event '{event.type}' fell through.")
        pass

    @future
    @ircroom_event()
    async def on_nosuchnick(self, conn, event) -> None:
        await self.send_notice("{}: {}".format(event.arguments[0], event.arguments[1]))

    @future
    async def on_server_message(self, conn, event) -> None:
        await self.send_notice(" ".join(event.arguments))

    @future
    @ircroom_event()
    async def on_privnotice(self, conn, event) -> None:
        # show unhandled notices in server room
        await self.send_notice_html(
            "<b>{} ({}@{}):</b> {}".format(
                event.source.nick,
                event.source.user,
                event.source.host,
                event.arguments[0],
            )
        )

    @future
    async def on_endofmotd(self, conn, event) -> None:
        await self.send_notice(" ".join(event.arguments))

        # wait a bit for good measure after motd to send a join command
        await asyncio.sleep(2)

        # rejoin channels (FIXME: change to comma separated join list)
        for room in self.rooms.values():
            if type(room) is ChannelRoom:
                await self.send_notice("Joining " + room.name)
                self.conn.join(room.name)

    @future
    async def on_privmsg(self, conn, event) -> bool:
        # slightly backwards
        target = event.source.nick.lower()

        if target not in self.rooms:
            # reuse query command to create a room
            await self.cmd_query(Namespace(nick=event.source.nick))
        else:
            room = self.rooms[target]
            if not room.in_room(self.user_id):
                asyncio.ensure_future(self.serv.api.post_room_invite(self.rooms[target].id, self.user_id))

    @future
    async def on_join(self, conn, event) -> None:
        target = event.target.lower()

        logging.debug(f"Handling JOIN to {target} by {event.source.nick} (we are {self.conn.get_nickname()})")

        # create a ChannelRoom in response to JOIN
        if event.source.nick == self.conn.get_nickname() and target not in self.rooms:
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
