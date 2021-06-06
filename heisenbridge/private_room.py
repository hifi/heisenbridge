import asyncio
import logging
import re
from datetime import datetime
from datetime import timezone
from html import escape
from typing import Dict
from typing import Optional
from typing import Tuple

from heisenbridge.command_parse import CommandManager
from heisenbridge.command_parse import CommandParserError
from heisenbridge.room import Room


class NetworkRoom:
    pass


def unix_to_local(timestamp: Optional[str]):
    try:
        dt = datetime.fromtimestamp(int(timestamp), timezone.utc)
        return dt.strftime("%c %Z")  # intentionally UTC for now
    except ValueError:
        logging.debug("Tried to convert '{timestamp}' to int")
        return timestamp


# this is very naive and will break html tag close/open order right now
def parse_irc_formatting(input: str) -> Tuple[str, Optional[str]]:
    plain = []
    formatted = []

    have_formatting = False
    bold = False
    italic = False
    underline = False

    for m in re.finditer(
        r"(\x02|\x03([0-9]{1,2})?(,([0-9]{1,2}))?|\x1D|\x1F|\x16|\x0F)?([^\x02\x03\x1D\x1F\x16\x0F]*)", input
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
            formatted.append(escape(text))

    if bold:
        formatted.append("</b>")
    if italic:
        formatted.append("</i>")
    if underline:
        formatted.append("</u>")

    return ("".join(plain), "".join(formatted) if have_formatting else None)


def split_long(nick, user, host, target, message):
    out = []

    # this is an easy template to calculate the overhead of the sender and target
    template = f":{nick}!{user}@{host} PRIVMSG {target} :\r\n"
    maxlen = 512 - len(template.encode())
    dots = "..."

    words = []
    for word in message.split(" "):
        words.append(word)
        line = " ".join(words)

        if len(line.encode()) + len(dots) > maxlen:
            words.pop()
            out.append(" ".join(words) + dots)
            words = [dots, word]

    out.append(" ".join(words))

    return out


class PrivateRoom(Room):
    # irc nick of the other party, name for consistency
    name: str
    network: Optional[NetworkRoom]
    network_name: str

    commands: CommandManager
    displaynames: Dict[str, str]

    def init(self) -> None:
        self.name = None
        self.network = None
        self.network_name = None

        self.commands = CommandManager()
        self.displaynames = {}

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
    def create(network: NetworkRoom, name: str) -> "PrivateRoom":
        logging.debug(f"PrivateRoom.create(network='{network.name}', name='{name}')")
        irc_user_id = network.serv.irc_user_id(network.name, name)
        room = PrivateRoom(
            None,
            network.user_id,
            network.serv,
            [network.user_id, irc_user_id, network.serv.user_id],
        )
        room.name = name.lower()
        room.network = network
        room.network_name = network.name
        asyncio.ensure_future(room._create_mx())
        return room

    async def _create_mx(self) -> None:
        if self.id is None:
            irc_user_id = await self.network.serv.ensure_irc_user_id(self.network.name, self.name)
            self.id = await self.network.serv.create_room(
                "{} ({})".format(self.name, self.network.name),
                "Private chat with {} on {}".format(self.name, self.network.name),
                [self.network.user_id, irc_user_id],
            )
            self.serv.register_room(self)
            await self.network.serv.api.post_room_join(self.id, irc_user_id)
            await self.save()
            # start event queue now that we have an id
            self._queue.start()

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

    def cleanup(self) -> None:
        # cleanup us from network rooms
        if self.network and self.name in self.network.rooms:
            del self.network.rooms[self.name]

        super().cleanup()

    def on_privmsg(self, conn, event) -> None:
        if self.network is None:
            return

        irc_user_id = self.serv.irc_user_id(self.network.name, event.source.nick)

        (plain, formatted) = parse_irc_formatting(event.arguments[0])

        if event.source.nick == self.network.conn.real_nickname:
            self.send_message(f"You said: {plain}", formatted=(f"You said: {formatted}" if formatted else None))
            return

        self.send_message(
            plain,
            irc_user_id,
            formatted=formatted,
            fallback_html="<b>Message from {}</b>: {}".format(str(event.source), plain),
        )

        # if the local user has left this room invite them back
        if self.user_id not in self.members:
            asyncio.ensure_future(self.serv.api.post_room_invite(self.id, self.user_id))

        # lazy update displayname if we detect a change
        if not self.serv.is_user_cached(irc_user_id, event.source.nick):
            asyncio.ensure_future(self.serv.cache_user(irc_user_id, event.source.nick))

    def on_privnotice(self, conn, event) -> None:
        if self.network is None:
            return

        (plain, formatted) = parse_irc_formatting(event.arguments[0])

        if event.source.nick == self.network.conn.real_nickname:
            self.send_notice(f"You noticed: {plain}", formatted=(f"You noticed: {formatted}" if formatted else None))
            return

        # if the local user has left this room notify in network
        if self.user_id not in self.members:
            source = self.network.source_text(conn, event)
            self.network.send_notice_html(f"Notice from <b>{source}:</b> {formatted if formatted else plain}")
            return

        irc_user_id = self.serv.irc_user_id(self.network.name, event.source.nick)
        self.send_notice(
            plain,
            irc_user_id,
            formatted=formatted,
            fallback_html=f"<b>Notice from {str(event.source)}</b>: {formatted if formatted else plain}",
        )

    def on_ctcp(self, conn, event) -> None:
        if self.network is None:
            return

        irc_user_id = self.serv.irc_user_id(self.network.name, event.source.nick)

        command = event.arguments[0].upper()

        if command == "ACTION" and len(event.arguments) > 1:
            (plain, formatted) = parse_irc_formatting(event.arguments[1])

            if event.source.nick == self.network.conn.real_nickname:
                self.send_emote(f"(you) {plain}")
                return

            self.send_emote(plain, irc_user_id, fallback_html=f"<b>Emote from {str(event.source)}</b>: {plain}")
        else:
            self.send_notice_html(f"<b>{event.source.nick}</b> requested <b>CTCP {command}</b (ignored)")

    async def on_mx_message(self, event) -> None:
        if event["sender"] != self.user_id:
            return

        if self.network is None or self.network.conn is None or not self.network.conn.connected:
            self.send_notice("Not connected to network.")
            return

        body = None
        if "body" in event["content"]:
            body = event["content"]["body"]

            # replace mentioning us with our name
            body = body.replace(self.serv.user_id, "Heisenbridge")

            # try to replace puppet matrix id mentions with displaynames
            for user_id, displayname in self.displaynames.items():
                body = body.replace(user_id, displayname)

        if event["content"]["msgtype"] == "m.emote":
            self.network.conn.action(self.name, body)
        elif event["content"]["msgtype"] == "m.image":
            self.network.conn.privmsg(self.name, self.serv.mxc_to_url(event["content"]["url"]))
        elif event["content"]["msgtype"] == "m.text":
            if "m.new_content" in event["content"]:
                self.send_notice("Editing messages is not supported on IRC, edited text was NOT sent.")
                return

            # allow commanding the appservice in rooms
            match = re.match(r"^\s*([^:,\s]+)[\s:,]*(.+)$", body)
            if match and match.group(1).lower() == self.serv.registration["sender_localpart"]:
                try:
                    await self.commands.trigger(match.group(2))
                except CommandParserError as e:
                    self.send_notice(str(e))
                finally:
                    return

            for line in body.split("\n"):
                if line == "":
                    continue

                messages = split_long(
                    self.network.conn.real_nickname,
                    self.network.conn.user,
                    self.network.real_host,
                    self.name,
                    line,
                )

                for message in messages:
                    self.network.conn.privmsg(self.name, message)
