import logging
import re
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

    commands: CommandManager

    def init(self) -> None:
        self.name = None
        self.network = None
        self.network_name = None

        self.commands = CommandManager()

        self.mx_register("m.room.message", self.on_mx_message)

    def from_config(self, config: dict) -> None:
        if "name" not in config:
            raise Exception("No name key in config for ChatRoom")

        if "network" not in config:
            raise Exception("No network key in config for ChatRoom")

        self.name = config["name"]
        self.network_name = config["network"]

    def to_config(self) -> dict:
        return {"name": self.name, "network": self.network_name}

    @staticmethod
    async def create(network: NetworkRoom, name: str) -> "PrivateRoom":
        logging.debug(f"PrivateRoom.create(network='{network.name}', name='{name}')")
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

    async def on_privmsg(self, conn, event) -> None:
        if self.network is None:
            return

        irc_user_id = self.serv.irc_user_id(self.network.name, event.source.nick)

        if irc_user_id in self.members:
            await self.send_message(event.arguments[0], irc_user_id)
        else:
            await self.send_notice_html("<b>Message from {}</b>: {}".format(str(event.source), event.arguments[0]))

    async def on_privnotice(self, conn, event) -> None:
        if self.network is None:
            return

        irc_user_id = self.serv.irc_user_id(self.network.name, event.source.nick)

        if irc_user_id in self.members:
            await self.send_notice(event.arguments[0], irc_user_id)
        else:
            await self.send_notice_html("<b>Notice from {}</b>: {}".format(str(event.source), event.arguments[0]))

    async def on_ctcp(self, conn, event) -> None:
        if self.network is None:
            return

        irc_user_id = self.serv.irc_user_id(self.network.name, event.source.nick)

        if event.arguments[0].upper() != "ACTION":
            return

        if irc_user_id in self.members:
            await self.send_emote(event.arguments[1], irc_user_id)
        else:
            await self.send_notice_html("<b>Emote from {}</b>: {}".format(str(event.source), event.arguments[1]))

    async def on_mx_message(self, event) -> None:
        if event["user_id"] != self.user_id:
            return

        if self.network is None or self.network.conn is None or not self.network.conn.connected:
            await self.send_notice("Not connected to network.")
            return

        if event["content"]["msgtype"] == "m.emote":
            self.network.conn.action(self.name, event["content"]["body"])
        elif event["content"]["msgtype"] == "m.image":
            self.network.conn.privmsg(self.name, self.serv.mxc_to_url(event["content"]["url"]))
        elif event["content"]["msgtype"] == "m.text":
            if "\n" in event["content"]["body"]:
                await self.send_notice("Multiline text is not allowed on IRC, previous message was NOT sent.")
                return

            # allow commanding the appservice in rooms
            match = re.match(r"^\s*([^:,\s]+)[\s:,]*(.+)$", event["content"]["body"])
            if match and match.group(1).lower() == "heisenbridge":
                try:
                    await self.commands.trigger(match.group(2))
                except CommandParserError as e:
                    await self.send_notice(str(e))
                finally:
                    return

            self.network.conn.privmsg(self.name, event["content"]["body"])
