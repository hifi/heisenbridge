import asyncio
import html
import logging
import re
from datetime import datetime
from datetime import timezone
from html import escape
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
def parse_irc_formatting(input: str, pills=None) -> Tuple[str, Optional[str]]:
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

            # escape any existing html in the text
            text = escape(text)

            # create pills
            if pills:

                def replace_pill(m):
                    word = m.group(0).lower()

                    if word in pills:
                        mxid, displayname = pills[word]
                        return f'<a href="https://matrix.to/#/{escape(mxid)}">{escape(displayname)}</a>'

                    return m.group(0)

                # this will also match some non-nick characters so pillify fails on purpose
                text = re.sub(r"[^\s\?!:,\.]+(\.[A-Za-z0-9])?", replace_pill, text)

            # if the formatted version has a link, we took some pills
            if "<a href" in text:
                have_formatting = True

            formatted.append(text)

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

    # for compatibility with plumbed rooms
    max_lines = 0

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
        asyncio.ensure_future(room._create_mx(name))
        return room

    async def _create_mx(self, displayname) -> None:
        if self.id is None:
            irc_user_id = await self.network.serv.ensure_irc_user_id(self.network.name, displayname)
            self.id = await self.network.serv.create_room(
                "{} ({})".format(displayname, self.network.name),
                "Private chat with {} on {}".format(displayname, self.network.name),
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

        if not self.in_room(self.user_id):
            return False

        return True

    def cleanup(self) -> None:
        # cleanup us from network rooms
        if self.network and self.name in self.network.rooms:
            del self.network.rooms[self.name]

        super().cleanup()

    def pills(self):
        # if pills are disabled, don't generate any
        if self.network.pills_length < 1:
            return None

        ret = {}
        ignore = list(map(lambda x: x.lower(), self.network.pills_ignore))

        # push our own name first
        lnick = self.network.conn.real_nickname.lower()
        if self.user_id in self.displaynames and len(lnick) >= self.network.pills_length and lnick not in ignore:
            ret[lnick] = (self.user_id, self.displaynames[self.user_id])

        # assuming displayname of a puppet matches nick
        for member in self.members:
            if not member.startswith("@" + self.serv.puppet_prefix) or not member.endswith(":" + self.serv.server_name):
                continue

            if member in self.displaynames:
                nick = self.displaynames[member]
                lnick = nick.lower()
                if len(nick) >= self.network.pills_length and lnick not in ignore:
                    ret[lnick] = (member, nick)

        return ret

    def on_privmsg(self, conn, event) -> None:
        if self.network is None:
            return

        irc_user_id = self.serv.irc_user_id(self.network.name, event.source.nick)

        (plain, formatted) = parse_irc_formatting(event.arguments[0], self.pills())

        if event.source.nick == self.network.conn.real_nickname:
            self.send_message(f"You said: {plain}", formatted=(f"You said: {formatted}" if formatted else None))
            return

        self.send_message(
            plain,
            irc_user_id,
            formatted=formatted,
            fallback_html=f"<b>Message from {str(event.source)}</b>: {html.escape(plain)}",
        )

        # if the local user has left this room invite them back
        if self.user_id not in self.members:
            asyncio.ensure_future(self.serv.api.post_room_invite(self.id, self.user_id))

        # lazy update displayname if we detect a change
        if (
            not self.serv.is_user_cached(irc_user_id, event.source.nick)
            and irc_user_id not in self.lazy_members
            and irc_user_id in self.members
        ):
            asyncio.ensure_future(self.serv.ensure_irc_user_id(self.network.name, event.source.nick))

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
            self.network.send_notice_html(
                f"Notice from <b>{source}:</b> {formatted if formatted else html.escape(plain)}"
            )
            return

        irc_user_id = self.serv.irc_user_id(self.network.name, event.source.nick)
        self.send_notice(
            plain,
            irc_user_id,
            formatted=formatted,
            fallback_html=f"<b>Notice from {str(event.source)}</b>: {formatted if formatted else html.escape(plain)}",
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

            self.send_emote(
                plain, irc_user_id, fallback_html=f"<b>Emote from {str(event.source)}</b>: {html.escape(plain)}"
            )
        else:
            self.send_notice_html(f"<b>{event.source.nick}</b> requested <b>CTCP {html.escape(command)}</b> (ignored)")

    async def _send_message(self, event, func, prefix=""):
        body = None
        if "body" in event["content"]:
            body = event["content"]["body"]

            for user_id, displayname in self.displaynames.items():
                body = body.replace(user_id, displayname)

                # XXX: FluffyChat started doing this...
                body = body.replace("@" + displayname, displayname)

        # ignore edits for now
        if "m.new_content" in event["content"]:
            return

        lines = body.split("\n")

        # remove reply text but preserve mention
        if "m.relates_to" in event["content"] and "m.in_reply_to" in event["content"]["m.relates_to"]:
            # pull the mention out, it's already converted to IRC nick but the regex still matches
            m = re.match(r"> <([^>]+)>", lines.pop(0))
            reply_to = m.group(1) if m else None

            # skip all quoted lines, it will skip the next empty line as well (it better be empty)
            while len(lines) > 0 and lines.pop(0).startswith(">"):
                pass

            # convert mention to IRC convention
            if reply_to:
                first_line = reply_to + ": " + lines.pop(0)
                lines.insert(0, first_line)

        messages = []

        for line in lines:
            # drop all whitespace-only lines
            if re.match(r"^\s*$", line):
                continue

            # drop all code block lines
            if re.match(r"^\s*```\s*$", line):
                continue

            messages += split_long(
                self.network.conn.real_nickname,
                self.network.conn.username,
                self.network.real_host,
                self.name,
                prefix + line,
            )

        for i, message in enumerate(messages):
            if self.max_lines > 0 and i == self.max_lines - 1 and len(messages) > self.max_lines:
                self.react(event["event_id"], "\u2702")  # scissors

                resp = await self.serv.api.post_media_upload(
                    body.encode("utf-8"), content_type="text/plain; charset=UTF-8"
                )

                if self.use_pastebin:
                    func(
                        self.name,
                        f"... long message truncated: {self.serv.mxc_to_url(resp['content_uri'])} ({len(messages)} lines)",
                    )
                    self.react(event["event_id"], "\U0001f4dd")  # memo
                else:
                    func(self.name, "... long message truncated")

                return

            func(self.name, message)

        # show number of lines sent to IRC
        if self.max_lines == 0 and len(messages) > 1:
            self.react(event["event_id"], f"\u2702 {lines} lines")

    async def on_mx_message(self, event) -> None:
        if event["sender"] != self.user_id:
            return

        if self.network is None or self.network.conn is None or not self.network.conn.connected:
            self.send_notice("Not connected to network.")
            return

        if event["content"]["msgtype"] == "m.emote":
            await self._send_message(event, self.network.conn.action, "emote")
        elif event["content"]["msgtype"] in ["m.image", "m.file", "m.audio", "m.video"]:
            self.network.conn.privmsg(
                self.name, self.serv.mxc_to_url(event["content"]["url"], event["content"]["body"])
            )
            self.react(event["event_id"], "\U0001F517")  # link
        elif event["content"]["msgtype"] == "m.text":
            if "m.new_content" in event["content"]:
                self.send_notice("Editing messages is not supported on IRC, edited text was NOT sent.")
                return

            # allow commanding the appservice in rooms
            match = re.match(r"^\s*@?([^:,\s]+)[\s:,]*(.+)$", event["content"]["body"])
            if match and match.group(1).lower() == self.serv.registration["sender_localpart"]:
                try:
                    await self.commands.trigger(match.group(2))
                except CommandParserError as e:
                    self.send_notice(str(e))
                finally:
                    return

            await self._send_message(event, self.network.conn.privmsg)
