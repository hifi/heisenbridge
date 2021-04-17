import re
from typing import Any
from typing import Dict
from typing import Optional

from heisenbridge.command_parse import CommandManager
from heisenbridge.command_parse import CommandParserError
from heisenbridge.room import Room


class NetworkRoom:
    pass


class PrivateRoom(Room):
    # irc nick of the other party, name for consistency
    name: str
    network: Optional[NetworkRoom]
    network_name: str

    irc_handlers: Dict[str, Any]

    commands: CommandManager

    def init(self):
        self.name = None
        self.network = None
        self.network_name = None
        self.irc_handlers = {}

        self.commands = CommandManager()

        self.mx_register("m.room.message", self.on_mx_message)
        self.irc_register("PRIVMSG", self.on_irc_privmsg)
        self.irc_register("NOTICE", self.on_irc_notice)

    def from_config(self, config: dict):
        if "name" not in config:
            raise Exception("No name key in config for ChatRoom")

        if "network" not in config:
            raise Exception("No network key in config for ChatRoom")

        self.name = config["name"]
        self.network_name = config["network"]

    def to_config(self) -> dict:
        return {"name": self.name, "network": self.network_name}

    @staticmethod
    async def create(network: NetworkRoom, name: str):
        irc_user_id = await network.serv.ensure_irc_user_id(network.name, name)
        room_id = await network.serv.create_room(
            "{} ({})".format(name, network.name),
            "Private chat with {} on {}".format(name, network.name),
            [network.user_id, irc_user_id],
        )
        room = PrivateRoom(
            room_id,
            network.user_id,
            network.serv,
            [network.user_id, irc_user_id, network.serv.user_id],
        )
        room.name = name.lower()
        room.network = network
        room.network_name = network.name
        await room.save()
        network.serv.register_room(room)
        await network.serv.api.post_room_join(room.id, irc_user_id)
        return room

    def is_valid(self) -> bool:
        if self.network_name is None:
            return False

        if self.name is None:
            return False

        if self.user_id is None:
            return False

        if self.network_name is None:
            return False

        return True

    async def cleanup(self) -> None:
        # cleanup us from network rooms
        if self.network and self.name in self.network.rooms:
            del self.network.rooms[self.name]

    async def on_irc_privmsg(self, event):
        if self.network is None:
            return True

        if self.network.is_ctcp(event):
            return

        irc_user_id = self.serv.irc_user_id(self.network.name, event.prefix.nick)

        if irc_user_id in self.members:
            await self.send_message(event.parameters[1], irc_user_id)
        else:
            await self.send_notice_html(
                "<b>Message from {}</b>: {}".format(
                    str(event.prefix), event.parameters[1]
                )
            )

    async def on_irc_notice(self, event):
        if self.network is None:
            return True

        if self.network.is_ctcp(event):
            return

        irc_user_id = self.serv.irc_user_id(self.network.name, event.prefix.nick)

        if irc_user_id in self.members:
            await self.send_notice(event.parameters[1], irc_user_id)
        else:
            await self.send_notice_html(
                "<b>Notice from {}</b>: {}".format(
                    str(event.prefix), event.parameters[1]
                )
            )

    async def on_irc_event(self, event: dict) -> None:
        handlers = self.irc_handlers.get(event.command, [self._on_irc_room_event])
        for handler in handlers:
            await handler(event)

    async def _on_irc_room_event(self, event: dict) -> None:
        await self.send_notice("Unhandled PrivateRoom IRC event:" + str(event))

    def irc_register(self, type, func):
        if type not in self.irc_handlers:
            self.irc_handlers[type] = []

        self.irc_handlers[type].append(func)

    async def on_mx_message(self, event):
        if event["user_id"] != self.user_id:
            return True

        if (
            self.network is None
            or self.network.conn is None
            or not self.network.conn.connected
        ):
            return await self.send_notice("Not connected to network.")

        if event["content"]["msgtype"] == "m.image":
            self.network.conn.send(
                "PRIVMSG {} :{}".format(
                    self.name, self.serv.mxc_to_url(event["content"]["url"])
                )
            )
            return True
        elif event["content"]["msgtype"] == "m.text":
            # allow commanding the appservice in rooms
            if (
                "formatted_body" in event["content"]
                and self.serv.user_id in event["content"]["formatted_body"]
            ):

                # try really hard to find the start of the message
                # FIXME: parse the formatted part instead as it has a link inside it
                text = re.sub(r"^[^:]+\s*:?\s*", "", event["content"]["body"])

                try:
                    return await self.commands.trigger(text)
                except CommandParserError as e:
                    return await self.send_notice(str(e))

            self.network.conn.send(
                "PRIVMSG {} :{}".format(self.name, event["content"]["body"])
            )
        return True
