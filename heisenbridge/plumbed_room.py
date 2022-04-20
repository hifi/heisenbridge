import asyncio
import logging
import re
from typing import Optional

from irc.modes import parse_channel_modes
from mautrix.errors import MatrixRequestError
from mautrix.types import Membership
from mautrix.types import MessageEvent
from mautrix.types import MessageType
from mautrix.types import TextMessageEventContent

from heisenbridge.channel_room import ChannelRoom
from heisenbridge.command_parse import CommandParser
from heisenbridge.private_room import parse_irc_formatting


class NetworkRoom:
    pass


def connected(f):
    def wrapper(*args, **kwargs):
        self = args[0]

        if not self.network or not self.network.conn or not self.network.conn.connected:
            return asyncio.sleep(0)

        return f(*args, **kwargs)

    return wrapper


def send_relaymsg(room, func, sender):
    def wrapper(target, message):
        message = f":\001ACTION {message}\001" if func == room.network.conn.action else f":{message}"
        room.network.conn.send_items("RELAYMSG", target, f"{sanitize_irc_nick(sender)}/{room.relaytag}", message)

    return wrapper


def sanitize_irc_nick(nick):
    return re.sub(r"[^A-Za-z0-9_\[\]|{}-]", "", nick)


class PlumbedRoom(ChannelRoom):
    max_lines = 5
    use_pastebin = True
    use_displaynames = True
    use_disambiguation = True
    use_zwsp = False
    allow_notice = False
    force_forward = True
    topic_sync = None
    relaytag = "m"

    def init(self) -> None:
        super().init()

        cmd = CommandParser(
            prog="DISPLAYNAMES", description="enable or disable use of displaynames in relayed messages"
        )
        cmd.add_argument("--enable", dest="enabled", action="store_true", help="Enable displaynames")
        cmd.add_argument(
            "--disable", dest="enabled", action="store_false", help="Disable displaynames (fallback to MXID)"
        )
        cmd.set_defaults(enabled=None)
        self.commands.register(cmd, self.cmd_displaynames)

        cmd = CommandParser(
            prog="DISAMBIGUATION", description="enable or disable disambiguation of conflicting displaynames"
        )
        cmd.add_argument(
            "--enable", dest="enabled", action="store_true", help="Enable disambiguation (postfix with MXID)"
        )
        cmd.add_argument("--disable", dest="enabled", action="store_false", help="Disable disambiguation")
        cmd.set_defaults(enabled=None)
        self.commands.register(cmd, self.cmd_disambiguation)

        cmd = CommandParser(prog="ZWSP", description="enable or disable Zero-Width-Space anti-ping")
        cmd.add_argument("--enable", dest="enabled", action="store_true", help="Enable ZWSP anti-ping")
        cmd.add_argument("--disable", dest="enabled", action="store_false", help="Disable ZWSP anti-ping")
        cmd.set_defaults(enabled=None)
        self.commands.register(cmd, self.cmd_zwsp)

        cmd = CommandParser(prog="NOTICERELAY", description="enable or disable relaying of Matrix notices to IRC")
        cmd.add_argument("--enable", dest="enabled", action="store_true", help="Enable notice relay")
        cmd.add_argument("--disable", dest="enabled", action="store_false", help="Disable notice relay")
        cmd.set_defaults(enabled=None)
        self.commands.register(cmd, self.cmd_noticerelay)

        cmd = CommandParser(prog="TOPIC", description="show or set channel topic and configure sync mode")
        cmd.add_argument("--sync", choices=["off", "irc", "matrix", "any"], help="Topic sync targets, defaults to off")
        cmd.add_argument("text", nargs="*", help="topic text if setting")
        self.commands.register(cmd, self.cmd_topic)

        cmd = CommandParser(prog="RELAYTAG", description="set RELAYMSG tag if supported by server")
        cmd.add_argument("tag", nargs="?", help="new tag")
        self.commands.register(cmd, self.cmd_relaytag)

        self.mx_register("m.room.topic", self._on_mx_room_topic)

    def is_valid(self) -> bool:
        # we are valid as long as the appservice is in the room
        if not self.in_room(self.serv.user_id):
            return False

        return True

    @staticmethod
    async def create(network: "NetworkRoom", id: str, channel: str, key: str) -> "ChannelRoom":
        logging.debug(f"PlumbedRoom.create(network='{network.name}', id='{id}', channel='{channel}', key='{key}'")

        network.send_notice(f"Joining room {id} to initiate plumb...")
        try:
            room_id = await network.az.intent.join_room(id)
        except MatrixRequestError as e:
            network.send_notice(f"Failed to join room: {str(e)}")
            return

        network.send_notice(f"Joined room {room_id}, refreshing member state...")
        await network.az.intent.get_room_members(room_id)
        network.send_notice(f"Got state for room {room_id}, plumbing...")

        joined = await network.az.state_store.get_member_profiles(room_id, (Membership.JOIN,))
        banned = await network.az.state_store.get_members(room_id, (Membership.BAN,))

        room = PlumbedRoom(room_id, network.user_id, network.serv, joined, banned)
        room.name = channel.lower()
        room.key = key
        room.network = network
        room.network_id = network.id
        room.network_name = network.name

        # stamp global member sync setting at room creation time
        room.member_sync = network.serv.config["member_sync"]

        room.max_lines = network.serv.config["max_lines"]
        room.use_pastebin = network.serv.config["use_pastebin"]

        for user_id, member in joined.items():
            if member.displayname is not None:
                room.displaynames[user_id] = member.displayname

        network.serv.register_room(room)
        network.rooms[room.name] = room
        await room.save()

        network.send_notice(f"Plumbed {room_id} to {channel}, to unplumb just kick me out.")
        return room

    def from_config(self, config: dict) -> None:
        super().from_config(config)

        if "use_displaynames" in config:
            self.use_displaynames = config["use_displaynames"]

        if "use_disambiguation" in config:
            self.use_disambiguation = config["use_disambiguation"]

        if "use_zwsp" in config:
            self.use_zwsp = config["use_zwsp"]

        if "allow_notice" in config:
            self.allow_notice = config["allow_notice"]

        if "topic_sync" in config:
            self.topic_sync = config["topic_sync"]

        if "relaytag" in config:
            self.relaytag = config["relaytag"]

    def to_config(self) -> dict:
        return {
            **(super().to_config()),
            "use_displaynames": self.use_displaynames,
            "use_disambiguation": self.use_disambiguation,
            "use_zwsp": self.use_zwsp,
            "allow_notice": self.allow_notice,
            "topic_sync": self.topic_sync,
            "relaytag": self.relaytag,
        }

    # topic updates from channel state replies are ignored because formatting changes
    def set_topic(self, topic: str, user_id: Optional[str] = None) -> None:
        pass

    def on_topic(self, conn, event) -> None:
        self.send_notice("{} changed the topic".format(event.source.nick))
        if conn.real_nickname != event.source.nick and self.topic_sync in ["matrix", "any"]:
            (plain, formatted) = parse_irc_formatting(event.arguments[0])
            super().set_topic(plain)

    @connected
    async def _on_mx_room_topic(self, event) -> None:
        if event.sender != self.serv.user_id and self.topic_sync in ["irc", "any"]:
            topic = re.sub(r"[\r\n]", " ", event.content.topic)
            self.network.conn.topic(self.name, topic)

    @connected
    async def on_mx_message(self, event) -> None:
        sender = str(event.sender)
        (name, server) = sender.split(":", 1)

        # ignore self messages
        if sender == self.serv.user_id:
            return

        # prevent re-sending federated messages back
        if name.startswith("@" + self.serv.puppet_prefix) and server == self.serv.server_name:
            return

        # add ZWSP to sender to avoid pinging on IRC
        if self.use_zwsp:
            sender = f"{name[:2]}\u200B{name[2:]}:{server[:1]}\u200B{server[1:]}"

        if self.use_displaynames and event.sender in self.displaynames:
            sender_displayname = self.displaynames[event.sender]

            # ensure displayname is unique
            if self.use_disambiguation:
                for user_id, displayname in self.displaynames.items():
                    if user_id != event.sender and displayname == sender_displayname:
                        sender_displayname += f" ({sender})"
                        break

            # add ZWSP if displayname matches something on IRC
            if self.use_zwsp and len(sender_displayname) > 1:
                sender_displayname = f"{sender_displayname[:1]}\u200B{sender_displayname[1:]}"

            sender = sender_displayname

        # limit plumbed sender max length to 100 characters
        sender = sender[:100]

        if event.content.msgtype.is_media:
            # process media event like it was a text message
            media_event = MessageEvent(
                sender=event.sender,
                type=None,
                room_id=None,
                event_id=None,
                timestamp=None,
                content=TextMessageEventContent(body=self.serv.mxc_to_url(event.content.url, event.content.body)),
            )
            await self.relay_message(media_event, self.network.conn.privmsg, sender)

            self.react(event.event_id, "\U0001F517")  # link
            self.media.append([event.event_id, event.content.url])
            await self.save()
        elif event.content.msgtype == MessageType.EMOTE:
            await self.relay_message(event, self.network.conn.action, sender)
        elif event.content.msgtype == MessageType.TEXT:
            await self.relay_message(event, self.network.conn.privmsg, sender)
        elif event.content.msgtype == MessageType.NOTICE and self.allow_notice:
            await self.relay_message(event, self.network.conn.notice, sender)

        await self.az.intent.send_receipt(event.room_id, event.event_id)

    async def relay_message(self, event, func, sender):
        prefix = f"{sender} " if str(event.content.msgtype) == "m.emote" else f"<{sender}> "

        # if we have relaymsg cap and it's not a notice, add more abstraction
        if "draft/relaymsg" in self.network.caps_enabled and event.content.msgtype != MessageType.NOTICE:
            func = send_relaymsg(self, func, sender)
            prefix = None

        await self._send_message(event, func, prefix)

    @connected
    async def on_mx_ban(self, user_id) -> None:
        nick = self.serv.nick_from_irc_user_id(self.network.name, user_id)
        if nick is None:
            return

        # best effort kick and ban
        self.network.conn.mode(self.name, f"+b {nick}!*@*")
        self.network.conn.kick(self.name, nick, "You have been banned on Matrix")

    @connected
    async def on_mx_unban(self, user_id) -> None:
        nick = self.serv.nick_from_irc_user_id(self.network.name, user_id)
        if nick is None:
            return

        # best effort unban
        self.network.conn.mode(self.name, f"-b {nick}!*@*")

    @connected
    async def on_mx_leave(self, user_id) -> None:
        nick = self.serv.nick_from_irc_user_id(self.network.name, user_id)
        if nick is None:
            return

        # best effort kick
        if self.is_on_channel(nick):
            self.network.conn.kick(self.name, nick, "You have been kicked on Matrix")

    def pills(self):
        ret = super().pills()

        # remove the bot from pills as it may cause confusion
        nick = self.network.conn.real_nickname.lower()
        if nick in ret:
            del ret[nick]

        return ret

    async def cmd_displaynames(self, args) -> None:
        if args.enabled is not None:
            self.use_displaynames = args.enabled
            await self.save()

        self.send_notice(f"Displaynames are {'enabled' if self.use_displaynames else 'disabled'}")

    async def cmd_disambiguation(self, args) -> None:
        if args.enabled is not None:
            self.use_disambiguation = args.enabled
            await self.save()

        self.send_notice(f"Dismabiguation is {'enabled' if self.use_disambiguation else 'disabled'}")

    async def cmd_zwsp(self, args) -> None:
        if args.enabled is not None:
            self.use_zwsp = args.enabled
            await self.save()

        self.send_notice(f"Zero-Width-Space anti-ping is {'enabled' if self.use_zwsp else 'disabled'}")

    async def cmd_noticerelay(self, args) -> None:
        if args.enabled is not None:
            self.allow_notice = args.enabled
            await self.save()

        self.send_notice(f"Notice relay is {'enabled' if self.allow_notice else 'disabled'}")

    async def cmd_topic(self, args) -> None:
        if args.sync is None:
            self.network.conn.topic(self.name, " ".join(args.text))
            return

        self.topic_sync = args.sync if args.sync != "off" else None
        self.send_notice(f"Topic sync is {self.topic_sync if self.topic_sync else 'off'}")
        await self.save()

    async def cmd_relaytag(self, args) -> None:
        if args.tag:
            self.relaytag = sanitize_irc_nick(args.tag)
            await self.save()

        self.send_notice(f"Relay tag is '{self.relaytag}'")

    def on_mode(self, conn, event) -> None:
        super().on_mode(conn, event)

        # when we get ops (or half-ops) get current ban list to see if we need to ban someone that has been banned on matrix
        modes = list(event.arguments)
        for sign, key, value in parse_channel_modes(" ".join(modes)):
            if sign == "+" and key in ["o", "h"] and value == self.network.conn.real_nickname:
                self.network.conn.mode(self.name, "+b")

    def on_endofbanlist(self, conn, event) -> None:
        masks = [ban[0].lower() for ban in self.bans_buffer]
        super().on_endofbanlist(conn, event)

        # add any nick bans that are missing from IRC
        for user_id in self.bans:
            nick = self.serv.nick_from_irc_user_id(self.network.name, user_id)
            if nick is None:
                continue

            mask = f"{nick}!*@*"
            if mask not in masks:
                self.network.conn.mode(self.name, f"+b {mask}")
            if self.is_on_channel(nick):
                self.network.conn.kick(self.name, nick)
