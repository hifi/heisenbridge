import logging
import re
from typing import Optional

from heisenbridge.command_parse import CommandManager
from heisenbridge.command_parse import CommandParserError
from heisenbridge.room import Room


class NetworkRoom:
    pass


# this is very naive and will break html tag close/open order right now
def parse_irc_formatting(input: str) -> (str, str):
    plain = []
    formatted = []

    have_formatting = False
    bold = False
    italic = False
    underline = False

    for m in re.finditer(
        r"(\x02|\x03([0-9]+)?(,([0-9]+))?|\x1D|\x1F|\x16|\x0F)?([^\x02\x03\x1D\x1F\x16\x0F]*)", input + "\x0F"
    ):
        # fg is group 2, bg is group 4 but we're ignoring them now
        (ctrl, text) = (m.group(1), m.group(5))

        if ctrl:
            have_formatting = True

            if ctrl[0] == "\x02":
                if not bold:
                    formatted.append("<b>")
                else:
                    formatted.append("</b>")

                bold = not bold
            if ctrl[0] == "\x03":
                """
                ignoring color codes for now
                """
            elif ctrl[0] == "\x1D":
                if not italic:
                    formatted.append("<i>")
                else:
                    formatted.append("</i>")

                italic = not italic
            elif ctrl[0] == "\x1F":
                if not underline:
                    formatted.append("<u>")
                else:
                    formatted.append("</u>")

                underline = not underline
            elif ctrl[0] == "\x16":
                """
                ignore reverse
                """
            elif ctrl[0] == "\x0F":
                if bold:
                    formatted.append("</b>")
                if italic:
                    formatted.append("</i>")
                if underline:
                    formatted.append("</u>")

                bold = italic = underline = False

        if text:
            plain.append(text)
            formatted.append(text)

    return ("".join(plain), "".join(formatted) if have_formatting else None)


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

        (plain, formatted) = parse_irc_formatting(event.arguments[0])

        if irc_user_id in self.members:
            await self.send_message(plain, irc_user_id, formatted=formatted)
        else:
            await self.send_notice_html("<b>Message from {}</b>: {}".format(str(event.source), plain))

        # if the user has left this room invite them back
        if self.user_id not in self.members:
            await self.serv.api.post_room_invite(self.id, self.user_id)

    async def on_privnotice(self, conn, event) -> None:
        if self.network is None:
            return

        (plain, formatted) = parse_irc_formatting(event.arguments[0])

        # if the user has left this room notify in network
        if self.user_id not in self.members:
            source = self.network.source_text(conn, event)
            await self.network.send_notice_html(f"Notice from <b>{source}:</b> {formatted if formatted else plain}")
            return

        irc_user_id = self.serv.irc_user_id(self.network.name, event.source.nick)

        if irc_user_id in self.members:
            await self.send_notice(plain, irc_user_id, formatted=formatted)
        else:
            await self.send_notice_html(f"<b>Notice from {str(event.source)}</b>: {formatted if formatted else plain}")

    async def on_ctcp(self, conn, event) -> None:
        if self.network is None:
            return

        irc_user_id = self.serv.irc_user_id(self.network.name, event.source.nick)

        if event.arguments[0].upper() != "ACTION":
            return

        (plain, formatted) = parse_irc_formatting(event.arguments[1])

        if irc_user_id in self.members:
            await self.send_emote(plain, irc_user_id)
        else:
            await self.send_notice_html(f"<b>Emote from {str(event.source)}</b>: {plain}")

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

            if "m.new_content" in event["content"]:
                await self.send_notice("Editing messages is not supported on IRC, edited text was NOT sent.")
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
