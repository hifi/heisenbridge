import logging
import re
from typing import Any
from typing import Dict
from typing import Optional

from heisenbridge.command_parse import CommandManager
from heisenbridge.command_parse import CommandParserError
from heisenbridge.room import Room, INetworkRoom, IrcRoom


class PrivateRoom(IrcRoom):
    commands: CommandManager

    def init(self) -> None:
        self.commands = CommandManager()

        self.mx_register("m.room.message", self.on_mx_message)
        self.irc_register("PRIVMSG", self.on_irc_privmsg)
        self.irc_register("NOTICE", self.on_irc_notice)

    @staticmethod
    async def create(network: INetworkRoom, name: str) -> "PrivateRoom":
        logging.debug(f"PrivateRoom.create(network='{network.name}', name='{name}'")
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

    async def on_irc_privmsg(self, event) -> None:
        if self.network is None:
            return

        if self.network.is_ctcp(event):
            return

        irc_user_id = self.serv.irc_user_id(self.network.name, event.prefix.nick)

        if irc_user_id in self.members:
            await self.send_message(event.parameters[1], irc_user_id)
        else:
            await self.send_notice_html("<b>Message from {}</b>: {}".format(str(event.prefix), event.parameters[1]))

    async def on_irc_notice(self, event) -> None:
        if self.network is None:
            return

        if self.network.is_ctcp(event):
            return

        irc_user_id = self.serv.irc_user_id(self.network.name, event.prefix.nick)

        if irc_user_id in self.members:
            await self.send_notice(event.parameters[1], irc_user_id)
        else:
            await self.send_notice_html("<b>Notice from {}</b>: {}".format(str(event.prefix), event.parameters[1]))

    async def on_mx_message(self, event) -> None:
        if event["content"]["msgtype"] != "m.text" or event["user_id"] != self.user_id:
            return

        if self.network is None or self.network.conn is None or not self.network.conn.connected:
            await self.send_notice("Not connected to network.")
            return

        # allow commanding the appservice in rooms
        if "formatted_body" in event["content"] and self.serv.user_id in event["content"]["formatted_body"]:

            # try really hard to find the start of the message
            # FIXME: parse the formatted part instead as it has a link inside it
            text = re.sub(r"^[^:]+\s*:?\s*", "", event["content"]["body"])

            try:
                await self.commands.trigger(text)
            except CommandParserError as e:
                await self.send_notice(str(e))
            return

        self.network.conn.send("PRIVMSG {} :{}".format(self.name, event["content"]["body"]))
