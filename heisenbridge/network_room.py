import asyncio
from argparse import Namespace
from typing import Any, Dict, List

from asyncirc.protocol import IrcProtocol
from asyncirc.server import Server

from heisenbridge.channel_room import ChannelRoom
from heisenbridge.command_parse import (CommandManager, CommandParser,
                                              CommandParserError)
from heisenbridge.private_room import PrivateRoom
from heisenbridge.room import Room


class NetworkRoom(Room):
    # configuration stuff
    name: str
    connected: bool
    nick: str

    # state
    commands: CommandManager
    conn: IrcProtocol
    rooms: Dict[str, Room]
    queue: Dict[str, Room]

    irc_ignore: List[str]
    irc_handlers: Dict[str, Any]
    irc_forwards: Dict[str, Any]

    def init(self):
        self.name = None
        self.connected = False
        self.nick = None

        self.commands = CommandManager()
        self.conn = None
        self.rooms = {}
        self.queue = {}

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

        # these messages are competely ignored by us
        self.irc_ignore = ["PING", "PONG", "333"]

        # these we handle but may also forward
        self.irc_handlers = {
            "001": self.on_server_message,
            "002": self.on_server_message,
            "003": self.on_server_message,
            "004": self.on_server_message,
            "005": self.on_server_message,
            "250": self.on_server_message,
            "251": self.on_server_message,
            "252": self.on_server_message,
            "253": self.on_server_message,
            "254": self.on_server_message,
            "255": self.on_server_message,
            "265": self.on_server_message,
            "266": self.on_server_message,
            "401": self.on_no_such_nick,
            "465": self.on_server_message,
            "473": self.on_server_message,
            "476": self.on_server_message,
            "501": self.on_server_message,
            "CAP": self.on_server_message,
            "NOTICE": self.on_notice,
            "375": self.on_server_message,
            "372": self.on_server_message,
            "376": self.on_motd_end,
            "PRIVMSG": self.on_privmsg,
            "JOIN": self.on_join,
            "QUIT": self.on_quit,
            "NICK": self.on_nick,
            "INVITE": self.on_invite,
        }

        # forward these messages to target specifier in arguments
        self.irc_forwards = {
            "PRIVMSG": 0,
            "JOIN": 0,
            "NOTICE": 0,
            "PART": 0,
            "MODE": 0,
            "TOPIC": 0,
            "331": 1,
            "332": 1,
            "366": 1,
            "353": 2,
            "473": 0,
        }

    @staticmethod
    async def create(serv, name, user_id):
        room_id = await serv.create_room(
            name, "Network room for {}".format(name), [user_id]
        )
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
        if self.name == None:
            return False

        # if user leaves network room and it's not connected we can clean it up
        if not self.in_room(self.user_id) and not self.connected:
            return False

        return True

    async def show_help(self):
        await self.send_notice_html(
            "Welcome to the network room for <b>{}</b>!".format(self.name)
        )

        try:
            return await self.commands.trigger("HELP")
        except CommandParserError as e:
            return await self.send_notice(str(e))

    async def on_mx_message(self, event) -> None:
        if (
            event["content"]["msgtype"] != "m.text"
            or event["user_id"] == self.serv.user_id
        ):
            return True

        try:
            return await self.commands.trigger(event["content"]["body"])
        except CommandParserError as e:
            return await self.send_notice(str(e))

    async def cmd_connect(self, args):
        return await self.connect()

    async def cmd_disconnect(self, args):
        self.connected = False
        await self.save()

        if not self.conn:
            return True

        self.conn.quit()
        return await self.send_notice("Disconnecting...")

    async def cmd_raw(self, args):
        if not self.conn or not self.conn.connected:
            return await self.send_notice("Need to be connected to use this command.")

        self.conn.send(" ".join(args.text))
        return True

    async def cmd_query(self, args):
        if not self.conn or not self.conn.connected:
            return await self.send_notice("Need to be connected to use this command.")

        ## TODO: validate nick doesn't look like a channel
        target = args.nick.lower()

        if target in self.rooms:
            room = self.rooms[target]
            await self.serv.api.post_room_invite(room.id, self.user_id)
            return await self.send_notice(
                "Inviting back to private chat with {}.".format(args.nick)
            )
        else:
            room = await PrivateRoom.create(self, args.nick)
            self.rooms[room.name] = room
            return await self.send_notice(
                "You have been invited to private chat with {}.".format(args.nick)
            )

    async def cmd_join(self, args):
        if not self.conn or not self.conn.connected:
            return

        ## TODO: validate channel name and add # prefix if naked

        self.conn.send("JOIN {}".format(args.channel))
        return True

    async def cmd_nick(self, args):
        if args.nick == None:
            return await self.send_notice("Current nickname: {}".format(self.nick))

        self.nick = args.nick
        await self.save()
        return await self.send_notice("Nickname set to {}".format(self.nick))

    async def connect(self):
        if self.conn and self.conn.connected:
            return True

        if self.nick == None:
            return await self.send_notice(
                "You need to configure a nick first, see HELP"
            )

        # attach loose sub-rooms to us
        for room in self.serv.find_rooms(PrivateRoom, self.user_id):
            if room.network_name == self.name:
                print("Attaching PrivateRoom")
                room.network = self
                self.rooms[room.name] = room

        for room in self.serv.find_rooms(ChannelRoom, self.user_id):
            if room.network_name == self.name:
                print("Attaching ChannelRoom")
                room.network = self
                self.rooms[room.name] = room

        network = self.serv.config["networks"][self.name]

        servers = []
        for server in network["servers"]:
            servers.append(Server(server, 6667))

        if self.conn == None:
            self.conn = IrcProtocol(servers, self.nick, loop=asyncio.get_event_loop())
            self.conn.register("*", self.on_irc_event)

        await self.send_notice("Connecting...")
        await self.conn.connect()

        if not self.connected:
            self.connected = True
            await self.save()

        return True

    async def on_irc_event(self, conn, message):
        handled = False
        if message.command in self.irc_handlers:
            handled = await self.irc_handlers[message.command](message)

        if message.command in self.irc_forwards:
            target = message.parameters[self.irc_forwards[message.command]].lower()

            # direct target means the target room is the sender
            if target == self.nick.lower():
                target = message.prefix.nick.lower()

            if target in self.queue:
                self.queue[target].append(message)
            elif target in self.rooms:
                await self.rooms[target].on_irc_event(message)
            elif not handled:
                await self.send_notice(
                    "No room for targeted event ({}): {}".format(target, str(message))
                )

            # dequeue events if needed
            if target in self.queue and target in self.rooms:
                queue = self.queue[target]
                del self.queue[target]

                for e in queue:
                    await self.rooms[target].on_irc_event(e)
        elif not handled and message.command not in self.irc_ignore:
            await self.send_notice("Unhandled IRC event: " + str(message))

    async def on_no_such_nick(self, message):
        if message.parameters[0] != self.nick:
            return True

        # tell the sender
        for room in self.serv.find_rooms(PrivateRoom, self.user_id):
            if room.network_name == self.name and room.name == message.parameters[1]:
                return await room.send_notice(
                    "{}: {}".format(message.parameters[1], message.parameters[2])
                )

        return False

    async def on_server_message(self, message):
        parameters = list(message.parameters)
        parameters.pop(0)
        return await self.send_notice(" ".join(parameters))

    async def on_notice(self, message):
        source = message.prefix.nick.lower()
        target = message.parameters[0].lower()

        # show unhandled notices in server room
        if source not in self.rooms:
            return await self.send_notice_html(
                "<b>{} ({}@{}):</b> {}".format(
                    message.prefix.nick,
                    message.prefix.user,
                    message.prefix.host,
                    message.parameters[1],
                )
            )

        return False

    async def on_motd_end(self, message):
        await self.on_server_message(message)

        # wait a bit for good measure after motd to send a join command
        await asyncio.sleep(2)

        # rejoin channels (FIXME: change to comma separated join list)
        for room in self.rooms.values():
            if type(room) is ChannelRoom:
                await self.send_notice("Joining " + room.name)
                self.conn.send("JOIN {}".format(room.name))

        return True

    def is_ctcp(self, message):
        return len(message.parameters) > 1 and message.parameters[1][0] == "\x01"

    async def on_privmsg(self, message):
        if message.parameters[0] != self.nick:
            return

        target = message.prefix.nick.lower()

        if self.is_ctcp(message):
            return await self.send_notice(
                "Ignored CTCP from {}".format(message.prefix.nick)
            )

        # prevent creating a room while queue is in effect
        if target in self.queue:
            return

        if target not in self.rooms:
            # create queue for subsequent messages
            self.queue[target] = []

            # reuse query command to create a room
            await self.cmd_query(Namespace(nick=message.prefix.nick))

            # dequeue events if needed
            queue = self.queue[target]
            del self.queue[target]

            for e in queue:
                await self.rooms[target].on_irc_event(e)
        else:
            room = self.rooms[target]
            if not room.in_room(self.user_id):
                await self.serv.api.post_room_invite(
                    self.rooms[target].id, self.user_id
                )

    async def on_join(self, message):
        target = message.parameters[0].lower()

        # create a ChannelRoom in response to JOIN
        if message.prefix.nick == self.nick and target not in self.rooms:
            self.queue[target] = []
            self.rooms[target] = await ChannelRoom.create(self, message.parameters[0])

            # dequeue events if needed
            queue = self.queue[target]
            del self.queue[target]

            for e in queue:
                await self.rooms[target].on_irc_event(e)

        return True

    async def on_quit(self, message):
        irc_user_id = self.serv.irc_user_id(self.name, message.prefix.nick)

        # leave channels
        for room in self.rooms.values():
            if type(room) is ChannelRoom:
                if room.in_room(irc_user_id):
                    await self.serv.api.post_room_leave(room.id, irc_user_id)

        return True

    async def on_nick(self, message):
        old_irc_user_id = self.serv.irc_user_id(self.name, message.prefix.nick)
        new_irc_user_id = await self.serv.ensure_irc_user_id(
            self.name, message.parameters[0]
        )

        # special case where only cases change
        if old_irc_user_id == new_irc_user_id:
            return True

        # leave and join channels
        for room in self.rooms.values():
            if type(room) is ChannelRoom:
                if room.in_room(old_irc_user_id):
                    # notify mx user about the change
                    await room.send_notice(
                        "{} is changing nick to {}".format(
                            message.prefix.nick, message.parameters[0]
                        )
                    )
                    await self.serv.api.post_room_leave(room.id, old_irc_user_id)
                    await self.serv.api.post_room_invite(room.id, new_irc_user_id)
                    await self.serv.api.post_room_join(room.id, new_irc_user_id)

        return True

    async def on_invite(self, message):
        await self.send_notice_html(
            "<b>{}</b> has invited you to <b>{}</b>".format(
                message.prefix.nick, message.parameters[1]
            )
        )
        return True
