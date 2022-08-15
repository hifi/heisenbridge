import asyncio
import collections
import html
import logging
import re
import unicodedata
from datetime import datetime
from datetime import timezone
from html import escape
from typing import List
from typing import Optional
from typing import Tuple
from urllib.parse import urlparse

from mautrix.api import Method
from mautrix.api import SynapseAdminPath
from mautrix.errors import MatrixStandardRequestError
from mautrix.types.event.state import JoinRestriction
from mautrix.types.event.state import JoinRestrictionType
from mautrix.types.event.state import JoinRule
from mautrix.types.event.state import JoinRulesStateEventContent
from mautrix.types.event.type import EventType

from heisenbridge.command_parse import CommandManager
from heisenbridge.command_parse import CommandParser
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


def connected(f):
    def wrapper(*args, **kwargs):
        self = args[0]

        if not self.network or not self.network.conn or not self.network.conn.connected:
            self.send_notice("Need to be connected to use this command.")
            return asyncio.sleep(0)

        return f(*args, **kwargs)

    return wrapper


def parse_irc_formatting(input: str, pills=None, color=None) -> Tuple[str, Optional[str]]:
    plain = []
    formatted = []

    color_table = collections.defaultdict(
        lambda: None,
        {
            "0": "#ffffff",
            "00": "#ffffff",
            "1": "#000000",
            "01": "#000000",
            "2": "#00007f",
            "02": "#00007f",
            "3": "#009300",
            "03": "#009300",
            "4": "#ff0000",
            "04": "#ff0000",
            "5": "#7f0000",
            "05": "#7f0000",
            "6": "#9c009c",
            "06": "#9c009c",
            "7": "#fc7f00",
            "07": "#fc7f00",
            "8": "#ffff00",
            "08": "#ffff00",
            "9": "#00fc00",
            "09": "#00fc00",
            "10": "#009393",
            "11": "#00ffff",
            "12": "#0000fc",
            "13": "#ff00ff",
            "14": "#7f7f7f",
            "15": "#d2d2d2",
            "16": "#470000",
            "17": "#472100",
            "18": "#474700",
            "19": "#324700",
            "20": "#004700",
            "21": "#00472c",
            "22": "#004747",
            "23": "#002747",
            "24": "#000047",
            "25": "#2e0047",
            "26": "#470047",
            "27": "#47002a",
            "28": "#740000",
            "29": "#743a00",
            "30": "#747400",
            "31": "#517400",
            "32": "#007400",
            "33": "#007449",
            "34": "#007474",
            "35": "#004074",
            "36": "#000074",
            "37": "#4b0074",
            "38": "#740074",
            "39": "#740045",
            "40": "#b50000",
            "41": "#b56300",
            "42": "#b5b500",
            "43": "#7db500",
            "44": "#00b500",
            "45": "#00b571",
            "46": "#00b5b5",
            "47": "#0063b5",
            "48": "#0000b5",
            "49": "#7500b5",
            "50": "#b500b5",
            "51": "#b5006b",
            "52": "#ff0000",
            "53": "#ff8c00",
            "54": "#ffff00",
            "55": "#b2ff00",
            "56": "#00ff00",
            "57": "#00ffa0",
            "58": "#00ffff",
            "59": "#008cff",
            "60": "#0000ff",
            "61": "#a500ff",
            "62": "#ff00ff",
            "63": "#ff0098",
            "64": "#ff5959",
            "65": "#ffb459",
            "66": "#ffff71",
            "67": "#cfff60",
            "68": "#6fff6f",
            "69": "#65ffc9",
            "70": "#6dffff",
            "71": "#59b4ff",
            "72": "#5959ff",
            "73": "#c459ff",
            "74": "#ff66ff",
            "75": "#ff59bc",
            "76": "#ff9c9c",
            "77": "#ffd39c",
            "78": "#ffff9c",
            "79": "#e2ff9c",
            "80": "#9cff9c",
            "81": "#9cffdb",
            "82": "#9cffff",
            "83": "#9cd3ff",
            "84": "#9c9cff",
            "85": "#dc9cff",
            "86": "#ff9cff",
            "87": "#ff94d3",
            "88": "#000000",
            "89": "#131313",
            "90": "#282828",
            "91": "#363636",
            "92": "#4d4d4d",
            "93": "#656565",
            "94": "#818181",
            "95": "#9f9f9f",
            "96": "#bcbcbc",
            "97": "#e2e2e2",
            "98": "#ffffff",
        },
    )

    have_formatting = False
    bold = False
    foreground = None
    background = None
    reversed = False
    monospace = False
    italic = False
    strikethrough = False
    underline = False

    for m in re.finditer(
        r"(\x02|\x03(?:([0-9]{1,2})(?:,([0-9]{1,2}))?)?|\x04(?:([0-9A-Fa-f]{6})(?:,([0-9A-Fa-f]{6}))?)?|\x11|\x1D|\x1E|\x1F|\x16|\x0F)?([^\x02\x03\x04\x11\x1D\x1E\x1F\x16\x0F]*)",  # noqa: E501
        input,
    ):
        (ctrl, fg, bg, fghex, bghex, text) = (m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6))

        if ctrl:
            have_formatting = True

            if underline:
                formatted.append("</u>")
            if strikethrough:
                formatted.append("</strike>")
            if italic:
                formatted.append("</i>")
            if monospace:
                formatted.append("</code>")
            if color and (foreground is not None or background is not None):
                formatted.append("</font>")
            if bold:
                formatted.append("</b>")

            if ctrl[0] == "\x02":
                bold = not bold
            elif ctrl[0] == "\x03":
                foreground = color_table[fg]
                background = color_table[bg]
            elif ctrl[0] == "\x04":
                foreground = f"#{fghex}"
                background = f"#{bghex}"
            elif ctrl[0] == "\x11":
                monospace = not monospace
            elif ctrl[0] == "\x1D":
                italic = not italic
            elif ctrl[0] == "\x1E":
                strikethrough = not strikethrough
            elif ctrl[0] == "\x1F":
                underline = not underline
            elif ctrl[0] == "\x16":
                reversed = not reversed
            elif ctrl[0] == "\x0F":
                foreground = background = None
                bold = reversed = monospace = italic = strikethrough = underline = False

            if bold:
                formatted.append("<b>")
            if color and (foreground is not None or background is not None):
                formatted.append("<font")
                if not reversed:
                    if foreground is not None:
                        formatted.append(f" data-mx-color='{foreground}'")
                    if background is not None:
                        formatted.append(f" data-mx-bg-color='{background}'")
                else:
                    if background is not None:
                        formatted.append(f" data-mx-color='{background}'")
                    if foreground is not None:
                        formatted.append(f" data-mx-bg-color='{foreground}'")
                formatted.append(">")
            if monospace:
                formatted.append("<code>")
            if italic:
                formatted.append("<i>")
            if strikethrough:
                formatted.append("<strike>")
            if underline:
                formatted.append("<u>")

        if text:
            plain.append(text)

            # escape any existing html in the text
            text = escape(text)

            # create pills
            if pills:
                punct = "?!:;,."

                words = []
                for word in text.split(" "):
                    wlen = len(word)
                    while wlen > 0 and word[wlen - 1] in punct:
                        wlen -= 1

                    word_start = word[:wlen].lower()
                    word_end = word[wlen:]

                    if word_start in pills:
                        mxid, displayname = pills[word_start]
                        words.append(
                            f'<a href="https://matrix.to/#/{escape(mxid)}">{escape(displayname)}</a>{word_end}'
                        )
                    else:
                        words.append(word)

                text = " ".join(words)

            # if the formatted version has a link, we took some pills
            if "<a href" in text:
                have_formatting = True

            formatted.append(text)

    if underline:
        formatted.append("</u>")
    if strikethrough:
        formatted.append("</strike>")
    if italic:
        formatted.append("</i>")
    if monospace:
        formatted.append("</code>")
    if color and (foreground is not None or background is not None):
        formatted.append("</font>")
    if bold:
        formatted.append("</b>")

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


# generate an edit that follows usual IRC conventions
def line_diff(a, b):
    a = a.split()
    b = b.split()

    pre = None
    post = None
    mlen = min(len(a), len(b))

    for i in range(0, mlen):
        if a[i] != b[i]:
            break

        pre = i + 1

    for i in range(1, mlen + 1):
        if a[-i] != b[-i]:
            break

        post = -i

    rem = a[pre:post]
    add = b[pre:post]

    if len(add) == 0 and len(rem) > 0:
        return "-" + (" ".join(rem))

    if len(rem) == 0 and len(add) > 0:
        return "+" + (" ".join(add))

    if len(add) > 0:
        return "* " + (" ".join(add))

    return None


class PrivateRoom(Room):
    # irc nick of the other party, name for consistency
    name: str
    network: Optional[NetworkRoom]
    network_id: str
    network_name: Optional[str]
    media: List[List[str]]

    max_lines = 0
    use_pastebin = False
    force_forward = False

    commands: CommandManager

    def init(self) -> None:
        self.name = None
        self.network = None
        self.network_id = None
        self.network_name = None  # deprecated
        self.media = []
        self.lazy_members = {}  # allow lazy joining your own ghost for echo

        self.commands = CommandManager()

        if type(self) == PrivateRoom:
            cmd = CommandParser(prog="WHOIS", description="WHOIS the other user")
            self.commands.register(cmd, self.cmd_whois)

        cmd = CommandParser(
            prog="MAXLINES", description="set maximum number of lines per message until truncation or pastebin"
        )
        cmd.add_argument("lines", type=int, nargs="?", help="Number of lines")
        self.commands.register(cmd, self.cmd_maxlines)

        cmd = CommandParser(prog="PASTEBIN", description="enable or disable automatic pastebin of long messages")
        cmd.add_argument("--enable", dest="enabled", action="store_true", help="Enable pastebin")
        cmd.add_argument(
            "--disable", dest="enabled", action="store_false", help="Disable pastebin (messages will be truncated)"
        )
        cmd.set_defaults(enabled=None)
        self.commands.register(cmd, self.cmd_pastebin)

        self.mx_register("m.room.message", self.on_mx_message)
        self.mx_register("m.room.redaction", self.on_mx_redaction)

    def from_config(self, config: dict) -> None:
        if "max_lines" in config:
            self.max_lines = config["max_lines"]

        if "use_pastebin" in config:
            self.use_pastebin = config["use_pastebin"]

        if "name" not in config:
            raise Exception("No name key in config for ChatRoom")

        self.name = config["name"]

        if "network_id" in config:
            self.network_id = config["network_id"]

        if "media" in config:
            self.media = config["media"]

        # only used for migration
        if "network" in config:
            self.network_name = config["network"]

        if self.network_name is None and self.network_id is None:
            raise Exception("No network or network_id key in config for PrivateRoom")

    def to_config(self) -> dict:
        return {
            "name": self.name,
            "network": self.network_name,
            "network_id": self.network_id,
            "media": self.media[:5],
            "max_lines": self.max_lines,
            "use_pastebin": self.use_pastebin,
        }

    @staticmethod
    def create(network: NetworkRoom, name: str) -> "PrivateRoom":
        logging.debug(f"PrivateRoom.create(network='{network.name}', name='{name}')")
        irc_user_id = network.serv.irc_user_id(network.name, name)
        room = PrivateRoom(
            None,
            network.user_id,
            network.serv,
            [network.user_id, irc_user_id, network.serv.user_id],
            [],
        )
        room.name = name.lower()
        room.network = network
        room.network_id = network.id
        room.network_name = network.name

        room.max_lines = network.serv.config["max_lines"]
        room.use_pastebin = network.serv.config["use_pastebin"]

        asyncio.ensure_future(room._create_mx(name))
        return room

    async def _create_mx(self, displayname) -> None:
        if self.id is None:
            irc_user_id = await self.network.serv.ensure_irc_user_id(self.network.name, displayname, update_cache=False)
            self.id = await self.network.serv.create_room(
                "{} ({})".format(displayname, self.network.name),
                "Private chat with {} on {}".format(displayname, self.network.name),
                [self.network.user_id, irc_user_id],
            )
            self.serv.register_room(self)
            await self.az.intent.user(irc_user_id).ensure_joined(self.id)
            await self.save()
            # start event queue now that we have an id
            self._queue.start()

            # attach to network space
            if self.network.space:
                await self.network.space.attach(self.id)

    def is_valid(self) -> bool:
        if self.network_id is None and self.network_name is None:
            return False

        if self.name is None:
            return False

        if self.user_id is None:
            return False

        if not self.in_room(self.user_id):
            return False

        return True

    def cleanup(self) -> None:
        logging.debug(f"Cleaning up network connected room {self.id}.")

        # cleanup us from network space if we have it
        if self.network and self.network.space:
            asyncio.ensure_future(self.network.space.detach(self.id))

        # cleanup us from network rooms
        if self.network and self.name in self.network.rooms:
            logging.debug(f"... and we are attached to network {self.network.id}, detaching.")
            del self.network.rooms[self.name]

            # if leaving this room invalidated the network, clean it up
            if not self.network.is_valid():
                logging.debug(f"... and we invalidated network {self.network.id} while cleaning up.")
                self.network.serv.unregister_room(self.network.id)
                self.network.cleanup()
                asyncio.ensure_future(self.network.serv.leave_room(self.network.id, self.network.members))

        super().cleanup()

    def send_notice(
        self,
        text: str,
        user_id: Optional[str] = None,
        formatted=None,
        fallback_html: Optional[str] = None,
        forward=False,
    ):
        if (self.force_forward or forward or self.network.forward) and user_id is None:
            self.network.send_notice(text=f"{self.name}: {text}", formatted=formatted, fallback_html=fallback_html)
        else:
            super().send_notice(text=text, user_id=user_id, formatted=formatted, fallback_html=fallback_html)

    def send_notice_html(self, text: str, user_id: Optional[str] = None, forward=False) -> None:
        if (self.force_forward or forward or self.network.forward) and user_id is None:
            self.network.send_notice_html(text=f"{self.name}: {text}")
        else:
            super().send_notice_html(text=text, user_id=user_id)

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

        (plain, formatted) = parse_irc_formatting(event.arguments[0], self.pills(), self.network.color)

        # ignore relaymsgs by us
        if event.tags:
            for tag in event.tags:
                if tag["key"] == "draft/relaymsg" and tag["value"] == self.network.conn.real_nickname:
                    return

        if event.source.nick == self.network.conn.real_nickname:
            source_irc_user_id = self.serv.irc_user_id(self.network.name, event.source.nick)

            if self.lazy_members is None:
                self.send_message(f"You said: {plain}", formatted=(f"You said: {formatted}" if formatted else None))
                return
            elif source_irc_user_id not in self.lazy_members:
                # if we are a PM room, remove all other IRC users than the target
                if type(self) == PrivateRoom:
                    target_irc_user_id = self.serv.irc_user_id(self.network.name, self.name)

                    for user_id in self.members:
                        if user_id.startswith("@" + self.serv.puppet_prefix) and user_id != target_irc_user_id:
                            if user_id in self.lazy_members:
                                del self.lazy_members[user_id]
                            self.leave(user_id)

                # add self to lazy members list so it'll echo
                self.lazy_members[source_irc_user_id] = event.source.nick

        if (
            "twitch.tv/membership" in self.network.caps
            and irc_user_id not in self.members
            and irc_user_id not in self.lazy_members
        ):
            self.lazy_members[irc_user_id] = event.source.nick

        self.send_message(
            plain,
            irc_user_id,
            formatted=formatted,
            fallback_html=f"<b>Message from {str(event.source)}</b>: {html.escape(plain)}",
        )

        # lazy update displayname if we detect a change
        if (
            not self.serv.is_user_cached(irc_user_id, event.source.nick)
            and irc_user_id not in (self.lazy_members or {})
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

        # ignore relaymsgs by us
        if event.tags:
            for tag in event.tags:
                if tag["key"] == "draft/relaymsg" and tag["value"] == self.network.conn.real_nickname:
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
            (plain, formatted) = parse_irc_formatting(" ".join(event.arguments))
            self.send_notice_html(f"<b>{str(event.source)}</b> requested <b>CTCP {html.escape(plain)}</b> (ignored)")

    def on_ctcpreply(self, conn, event) -> None:
        if self.network is None:
            return

        (plain, formatted) = parse_irc_formatting(" ".join(event.arguments))
        self.send_notice_html(f"<b>{str(event.source)}</b> sent <b>CTCP REPLY {html.escape(plain)}</b> (ignored)")

    async def _process_event_content(self, event, prefix, reply_to=None):
        content = event.content

        if content.formatted_body:
            lines = str(await self.parser.parse(content.formatted_body)).split("\n")
        elif content.body:
            lines = content.body.split("\n")
        else:
            logging.warning("_process_event_content called with no usable body")
            return

        # drop all whitespace-only lines
        lines = [x for x in lines if not re.match(r"^\s*$", x)]

        # handle replies
        if reply_to and reply_to.sender != event.sender:
            # resolve displayname
            sender = reply_to.sender
            if sender in self.displaynames:
                sender = self.displaynames[sender]

            # prefix first line with nickname of the reply_to source
            first_line = sender + ": " + lines.pop(0)
            lines.insert(0, first_line)

        messages = []

        for i, line in enumerate(lines):
            # prefix first line if needed
            if i == 0 and prefix and len(prefix) > 0:
                line = prefix + line

                # filter control characters except ZWSP
                line = "".join(c for c in line if unicodedata.category(c)[0] != "C" or c == "\u200B")

            messages += split_long(
                self.network.conn.real_nickname,
                self.network.real_user,
                self.network.real_host,
                self.name,
                line,
            )

        return messages

    async def _send_message(self, event, func, prefix=""):
        # try to find out if this was a reply
        reply_to = None
        if event.content.get_reply_to():
            rel_event = event

            # traverse back all edits
            while rel_event.content.get_edit():
                rel_event = await self.az.intent.get_event(self.id, rel_event.content.get_edit())

            # see if the original is a reply
            if rel_event.content.get_reply_to():
                reply_to = await self.az.intent.get_event(self.id, rel_event.content.get_reply_to())

        if event.content.get_edit():
            messages = await self._process_event_content(event, prefix, reply_to)
            event_id = event.content.relates_to.event_id
            prev_event = self.last_messages[event.sender]
            if prev_event and prev_event.event_id == event_id:
                old_messages = await self._process_event_content(prev_event, prefix, reply_to)

                mlen = max(len(messages), len(old_messages))
                edits = []
                for i in range(0, mlen):
                    try:
                        old_msg = old_messages[i]
                    except IndexError:
                        old_msg = ""
                    try:
                        new_msg = messages[i]
                    except IndexError:
                        new_msg = ""

                    edit = line_diff(old_msg, new_msg)
                    if edit:
                        edits.append(prefix + edit)

                # use edits only if one line was edited
                if len(edits) == 1:
                    messages = edits

                # update last message _content_ to current so re-edits work
                self.last_messages[event.sender].content = event.content
            else:
                # last event was not found so we fall back to full message BUT we can reconstrut enough of it
                self.last_messages[event.sender] = event
        else:
            # keep track of the last message
            self.last_messages[event.sender] = event
            messages = await self._process_event_content(event, prefix, reply_to)

        for i, message in enumerate(messages):
            if self.max_lines > 0 and i == self.max_lines - 1 and len(messages) > self.max_lines:
                self.react(event.event_id, "\u2702")  # scissors

                if self.use_pastebin:
                    content_uri = await self.az.intent.upload_media(
                        "\n".join(messages).encode("utf-8"), mime_type="text/plain; charset=UTF-8"
                    )

                    if self.max_lines == 1:
                        func(
                            self.name,
                            f"{prefix}{self.serv.mxc_to_url(str(content_uri))} (long message, {len(messages)} lines)",
                        )
                    else:
                        func(
                            self.name,
                            f"... long message truncated: {self.serv.mxc_to_url(str(content_uri))} ({len(messages)} lines)",
                        )
                    self.react(event.event_id, "\U0001f4dd")  # memo

                    self.media.append([event.event_id, str(content_uri)])
                    await self.save()
                else:
                    if self.max_lines == 1:
                        # best effort is to send the first line and give up
                        func(self.name, message)
                    else:
                        func(self.name, "... long message truncated")

                return

            func(self.name, message)

        # show number of lines sent to IRC
        if self.max_lines == 0 and len(messages) > 1:
            self.react(event.event_id, f"\u2702 {len(messages)} lines")

    async def on_mx_message(self, event) -> None:
        if event.sender != self.user_id:
            return

        if self.network is None or self.network.conn is None or not self.network.conn.connected:
            self.send_notice("Not connected to network.")
            return

        if str(event.content.msgtype) == "m.emote":
            await self._send_message(event, self.network.conn.action)
        elif str(event.content.msgtype) in ["m.image", "m.file", "m.audio", "m.video"]:
            self.network.conn.privmsg(self.name, self.serv.mxc_to_url(event.content.url, event.content.body))
            self.react(event.event_id, "\U0001F517")  # link
            self.media.append([event.event_id, event.content.url])
            await self.save()
        elif str(event.content.msgtype) == "m.text":
            # allow commanding the appservice in rooms
            match = re.match(r"^\s*@?([^:,\s]+)[\s:,]*(.+)$", event.content.body)
            if match and match.group(1).lower() == self.serv.registration["sender_localpart"]:
                try:
                    await self.commands.trigger(match.group(2))
                except CommandParserError as e:
                    self.send_notice(str(e))
                finally:
                    return

            await self._send_message(event, self.network.conn.privmsg)

        await self.az.intent.send_receipt(event.room_id, event.event_id)

    async def on_mx_redaction(self, event) -> None:
        for media in self.media:
            if media[0] == event.redacts:
                url = urlparse(media[1])
                if self.serv.synapse_admin:
                    try:
                        await self.az.intent.api.request(
                            Method.POST, SynapseAdminPath.v1.media.quarantine[url.netloc][url.path[1:]]
                        )

                        self.network.send_notice(
                            f"Associated media {media[1]} for redacted event {event.redacts} "
                            + f"in room {self.name} was quarantined."
                        )
                    except Exception:
                        self.network.send_notice(
                            f"Failed to quarantine media! Associated media {media[1]} "
                            + f"for redacted event {event.redacts} in room {self.name} is left available."
                        )
                else:
                    self.network.send_notice(
                        f"No permission to quarantine media! Associated media {media[1]} "
                        + f"for redacted event {event.redacts} in room {self.name} is left available."
                    )
                return

    @connected
    async def cmd_whois(self, args) -> None:
        self.network.conn.whois(f"{self.name} {self.name}")

    async def cmd_maxlines(self, args) -> None:
        if args.lines is not None:
            self.max_lines = args.lines
            await self.save()

        self.send_notice(f"Max lines is {self.max_lines}")

    async def cmd_pastebin(self, args) -> None:
        if args.enabled is not None:
            self.use_pastebin = args.enabled
            await self.save()

        self.send_notice(f"Pastebin is {'enabled' if self.use_pastebin else 'disabled'}")

    async def _attach_hidden_room_internal(self) -> None:
        await self.az.intent.send_state_event(
            self.id,
            EventType.ROOM_JOIN_RULES,
            content=JoinRulesStateEventContent(
                join_rule=JoinRule.RESTRICTED,
                allow=[
                    JoinRestriction(type=JoinRestrictionType.ROOM_MEMBERSHIP, room_id=self.serv.hidden_room.id),
                ],
            ),
        )
        self.hidden_room_id = self.serv.hidden_room.id

    async def _detach_hidden_room_internal(self) -> None:
        await self.az.intent.send_state_event(
            self.id,
            EventType.ROOM_JOIN_RULES,
            content=JoinRulesStateEventContent(join_rule=JoinRule.INVITE),
        )
        self.hidden_room_id = None

    async def _attach_hidden_room(self) -> None:
        if self.hidden_room_id:
            self.send_notice("Room already has a hidden room attached.")
            return
        if not self.serv.hidden_room:
            self.send_notice("Server has no hidden room!")
            return

        logging.debug(f"Attaching room {self.id} to servers hidden room {self.serv.hidden_room.id}.")
        try:
            room_create = await self.az.intent.get_state_event(self.id, EventType.ROOM_CREATE)
            if room_create.room_version in [str(v) for v in range(1, 9)]:
                self.send_notice("Only rooms of version 9 or greater can be attached to a hidden room.")
                self.send_notice("Leave and re-create the room to ensure the correct version.")
                return

            await self._attach_hidden_room_internal()
            self.send_notice("Hidden room attached, invites should now be gone.")
        except MatrixStandardRequestError as e:
            logging.debug("Setting join_rules for hidden room failed.", exc_info=True)
            self.send_notice(f"Failed attaching hidden room: {e.message}")
            self.send_notice("Make sure the room is at least version 9.")
        except Exception:
            logging.exception(f"Failed to attach {self.id} to hidden room {self.serv.hidden_room.id}.")

    async def _detach_hidden_room(self) -> None:
        if not self.hidden_room_id:
            self.send_notice("Room already detached from hidden room.")
            return

        logging.debug(f"Detaching room {self.id} from hidden room {self.hidden_room_id}.")
        try:
            await self._detach_hidden_room_internal()
            self.send_notice("Hidden room detached.")
        except MatrixStandardRequestError as e:
            logging.debug("Setting join_rules for hidden room failed.", exc_info=True)
            self.send_notice(f"Failed detaching hidden room: {e.message}")
        except Exception:
            logging.exception(f"Failed to detach {self.id} from hidden room {self.hidden_room_id}.")

    async def cmd_upgrade(self, args) -> None:
        if args.undo:
            await self._detach_hidden_room()
        else:
            await self._attach_hidden_room()

    async def post_init(self) -> None:
        if self.hidden_room_id and not self.serv.hidden_room:
            logging.debug(
                f"Server has no hidden room, detaching room {self.id} from hidden room {self.hidden_room_id}."
            )
            await self._detach_hidden_room_internal()
        elif self.hidden_room_id and self.hidden_room_id != self.serv.hidden_room.id:
            logging.debug(f"Server has different hidden room, reattaching room {self.id}.")
            await self._attach_hidden_room_internal()
