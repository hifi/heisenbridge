import asyncio
import html
import logging
from typing import Dict
from typing import List
from typing import Optional

from irc.modes import parse_channel_modes

from heisenbridge.command_parse import CommandParser
from heisenbridge.private_room import parse_irc_formatting
from heisenbridge.private_room import PrivateRoom
from heisenbridge.private_room import unix_to_local


class NetworkRoom:
    pass


class ChannelRoom(PrivateRoom):
    key: Optional[str]
    member_sync: str
    autocmd: str
    names_buffer: List[str]
    bans_buffer: List[str]
    on_channel: List[str]

    def init(self) -> None:
        super().init()

        self.key = None
        self.autocmd = None

        # for migration the class default is full
        self.member_sync = "full"

        cmd = CommandParser(
            prog="AUTOCMD",
            description="run commands on join",
            epilog=(
                "Works _exactly_ like network AUTOCMD and runs in the network context."
                " You can use this to login to bots or other services after joining a channel."
            ),
        )
        cmd.add_argument("command", nargs="*", help="commands separated with ';'")
        cmd.add_argument("--remove", action="store_true", help="remove stored command")
        self.commands.register(cmd, self.cmd_autocmd)

        cmd = CommandParser(
            prog="SYNC",
            description="override IRC member sync type for this room",
            epilog="Note: To force full sync after setting to full, use the NAMES command",
        )
        group = cmd.add_mutually_exclusive_group()
        group.add_argument("--lazy", help="set lazy sync, members are added when they talk", action="store_true")
        group.add_argument(
            "--half", help="set half sync, members are added when they join or talk", action="store_true"
        )
        group.add_argument("--full", help="set full sync, members are fully synchronized", action="store_true")
        group.add_argument(
            "--off",
            help="disable member sync completely, the bridge will relay all messages, may be useful during spam attacks",
            action="store_true",
        )
        self.commands.register(cmd, self.cmd_sync)

        cmd = CommandParser(
            prog="MODE",
            description="send MODE command",
            epilog=(
                "Can be used to change channel modes, ban lists or invoke/manage custom lists.\n"
                "It is very network specific what modes or lists are supported, please see their documentation"
                " for comprehensive help.\n"
                "\n"
                "Note: Some common modes and lists may have a command, see HELP.\n"
            ),
        )
        cmd.add_argument("args", nargs="*", help="MODE command arguments")
        self.commands.register(cmd, self.cmd_mode)

        cmd = CommandParser(
            prog="NAMES",
            description="list channel members",
            epilog=(
                "Sends a NAMES command to server.\n"
                "\n"
                "This can be used to see what IRC permissions users currently have on this channel.\n"
                "\n"
                "Note: In addition this will resynchronize the Matrix room members list and may cause joins/leaves"
                " if it has fallen out of sync.\n"
            ),
        )
        self.commands.register(cmd, self.cmd_names)

        # plumbs have a slightly adjusted version
        if type(self) == ChannelRoom:
            cmd = CommandParser(prog="TOPIC", description="show or set channel topic")
            cmd.add_argument("text", nargs="*", help="topic text if setting")
            self.commands.register(cmd, self.cmd_topic)

        cmd = CommandParser(prog="BANS", description="show channel ban list")
        self.commands.register(cmd, self.cmd_bans)

        cmd = CommandParser(prog="OP", description="op someone")
        cmd.add_argument("nick", help="nick to target")
        self.commands.register(cmd, self.cmd_op)

        cmd = CommandParser(prog="DEOP", description="deop someone")
        cmd.add_argument("nick", help="nick to target")
        self.commands.register(cmd, self.cmd_deop)

        cmd = CommandParser(prog="VOICE", description="voice someone")
        cmd.add_argument("nick", help="nick to target")
        self.commands.register(cmd, self.cmd_voice)

        cmd = CommandParser(prog="DEVOICE", description="devoice someone")
        cmd.add_argument("nick", help="nick to target")
        self.commands.register(cmd, self.cmd_devoice)

        cmd = CommandParser(prog="KICK", description="kick someone")
        cmd.add_argument("nick", help="nick to target")
        cmd.add_argument("reason", nargs="*", help="reason")
        self.commands.register(cmd, self.cmd_kick)

        cmd = CommandParser(prog="KB", description="kick and ban someone")
        cmd.add_argument("nick", help="nick to target")
        cmd.add_argument("reason", nargs="*", help="reason")
        self.commands.register(cmd, self.cmd_kb)

        cmd = CommandParser(prog="JOIN", description="join this channel if not on it")
        self.commands.register(cmd, self.cmd_join)

        cmd = CommandParser(prog="PART", description="leave this channel temporarily")
        self.commands.register(cmd, self.cmd_part)

        cmd = CommandParser(
            prog="STOP",
            description="immediately clear all queued IRC events like long messages",
            epilog="Use this to stop accidental long pastes, also known as STAHP!",
        )
        self.commands.register(cmd, self.cmd_stop, ["STOP!", "STAHP", "STAHP!"])

        self.names_buffer = []
        self.bans_buffer = []
        self.on_channel = []

    def from_config(self, config: dict) -> None:
        super().from_config(config)

        if "key" in config:
            self.key = config["key"]

        if "member_sync" in config:
            self.member_sync = config["member_sync"]

        if "autocmd" in config:
            self.autocmd = config["autocmd"]

    def to_config(self) -> dict:
        return {**(super().to_config()), "key": self.key, "member_sync": self.member_sync, "autocmd": self.autocmd}

    @staticmethod
    def create(network: NetworkRoom, name: str) -> "ChannelRoom":
        logging.debug(f"ChannelRoom.create(network='{network.name}', name='{name}'")

        room = ChannelRoom(None, network.user_id, network.serv, [network.serv.user_id, network.user_id], [])
        room.name = name.lower()
        room.network = network
        room.network_id = network.id
        room.network_name = network.name

        # fetch stored channel key if used for join command
        if room.name in network.keys:
            room.key = network.keys[room.name]
            del network.keys[room.name]

        # stamp global member sync setting at room creation time
        room.member_sync = network.serv.config["member_sync"]

        asyncio.ensure_future(room._create_mx(name))
        return room

    async def _create_mx(self, name):
        # handle !room names properly
        visible_name = name
        if visible_name.startswith("!"):
            visible_name = "!" + visible_name[6:]

        self.id = await self.network.serv.create_room(
            f"{visible_name} ({self.network.name})",
            "",
            [self.network.user_id],
        )
        self.serv.register_room(self)
        await self.save()
        # start event queue now that we have an id
        self._queue.start()

    def is_valid(self) -> bool:
        if not self.in_room(self.user_id):
            return False

        return super().is_valid()

    def cleanup(self) -> None:
        if self.network:
            if self.network.conn and self.network.conn.connected:
                self.network.conn.part(self.name)

        super().cleanup()

    async def cmd_autocmd(self, args) -> None:
        autocmd = " ".join(args.command)

        if args.remove:
            self.autocmd = None
            await self.save()
            self.send_notice("Autocmd removed.", forward=args._forward)
            return

        if autocmd == "":
            self.send_notice(f"Configured autocmd: {self.autocmd if self.autocmd else ''}", forward=args._forward)
            return

        self.autocmd = autocmd
        await self.save()
        self.send_notice(f"Autocmd set to {self.autocmd}", forward=args._forward)

    async def cmd_sync(self, args):
        if args.lazy:
            self.member_sync = "lazy"
            await self.save()
        elif args.half:
            self.member_sync = "half"
            await self.save()
        elif args.full:
            self.member_sync = "full"
            await self.save()
        elif args.off:
            self.member_sync = "off"
            # prevent anyone already in lazy list to be invited
            self.lazy_members = {}
            await self.save()

        self.send_notice(f"Member sync is set to {self.member_sync}", forward=args._forward)

    async def cmd_mode(self, args) -> None:
        self.network.conn.mode(self.name, " ".join(args.args))

    async def cmd_modes(self, args) -> None:
        self.network.conn.mode(self.name, "")

    async def cmd_names(self, args) -> None:
        self.network.conn.names(self.name)

    async def cmd_bans(self, args) -> None:
        self.network.conn.mode(self.name, "+b")

    async def cmd_op(self, args) -> None:
        self.network.conn.mode(self.name, f"+o {args.nick}")

    async def cmd_deop(self, args) -> None:
        self.network.conn.mode(self.name, f"-o {args.nick}")

    async def cmd_voice(self, args) -> None:
        self.network.conn.mode(self.name, f"+v {args.nick}")

    async def cmd_devoice(self, args) -> None:
        self.network.conn.mode(self.name, f"-v {args.nick}")

    async def cmd_topic(self, args) -> None:
        self.network.conn.topic(self.name, " ".join(args.text))

    async def cmd_kick(self, args) -> None:
        self.network.conn.kick(self.name, args.nick, " ".join(args.reason))

    async def cmd_kb(self, args) -> None:
        self.network.kickban(self.name, args.nick, " ".join(args.reason))

    async def cmd_join(self, args) -> None:
        self.network.conn.join(self.name, self.key)

    async def cmd_part(self, args) -> None:
        self.network.conn.part(self.name)

    async def cmd_stop(self, args) -> None:
        filtered = self.network.conn.remove_tag(self.name)
        self.send_notice(f"{filtered} messages removed from queue.")

    def on_pubmsg(self, conn, event):
        self.on_privmsg(conn, event)

    def on_pubnotice(self, conn, event):
        self.on_privnotice(conn, event)

    def on_namreply(self, conn, event) -> None:
        self.names_buffer.extend(event.arguments[2].split())

    def _add_puppet(self, nick):
        irc_user_id = self.serv.irc_user_id(self.network.name, nick)

        self.ensure_irc_user_id(self.network.name, nick)
        self.join(irc_user_id, nick)

    def _remove_puppet(self, user_id, reason=None):
        if user_id == self.serv.user_id or user_id == self.user_id:
            return

        self.leave(user_id, reason)

    def on_endofnames(self, conn, event) -> None:
        to_remove = []
        to_add = []
        names = list(self.names_buffer)
        self.names_buffer = []
        modes: Dict[str, List[str]] = {}
        others = []
        on_channel = []

        # build to_remove list from our own puppets
        for member in self.members:
            (name, server) = member.split(":")

            if name.startswith("@" + self.serv.puppet_prefix) and server == self.serv.server_name:
                to_remove.append(member)

        for nick in names:
            nick, mode = self.serv.strip_nick(nick)
            on_channel.append(nick.lower())

            if mode:
                if mode not in modes:
                    modes[mode] = []

                if nick == conn.real_nickname:
                    modes[mode].append(nick + " (you)")
                else:
                    modes[mode].append(nick)
            else:
                if nick == conn.real_nickname:
                    others.append(nick + " (you)")
                else:
                    others.append(nick)

            # ignore us
            if nick == conn.real_nickname:
                continue

            # convert to mx id, check if we already have them
            irc_user_id = self.serv.irc_user_id(self.network.name, nick)

            # make sure this user is not removed from room
            if irc_user_id in to_remove:
                to_remove.remove(irc_user_id)
                continue

            # if this user is not in room, add to invite list
            if not self.in_room(irc_user_id):
                to_add.append((irc_user_id, nick))

        # never remove us or appservice
        if self.serv.user_id in to_remove:
            to_remove.remove(self.serv.user_id)
        if self.user_id in to_remove:
            to_remove.remove(self.user_id)

        self.send_notice(
            "Synchronizing members:"
            + f" got {len(names)} from server,"
            + f" {len(self.members)} in room,"
            + f" {len(to_add)} will be invited and {len(to_remove)} removed."
        )

        # known common mode names
        modenames = {
            "~": "owner",
            "&": "admin",
            "@": "op",
            "%": "half-op",
            "+": "voice",
        }

        # show modes from top to bottom
        for mode, name in modenames.items():
            if mode in modes:
                nicks = sorted(modes[mode], key=str.casefold)
                self.send_notice(f"Users with {name} ({mode}): {', '.join(nicks)}")
                del modes[mode]

        # show unknown modes
        for mode, nicks in modes.items():
            nicks = sorted(nicks, key=str.casefold)
            self.send_notice(f"Users with '{mode}': {', '.join(nicks)}")

        # show everyone else
        if len(others) > 0:
            others = sorted(others, key=str.casefold)
            self.send_notice(f"Users: {', '.join(others)}")

        # always reset lazy list because it can be toggled on-the-fly
        self.lazy_members = {}

        if self.member_sync == "full":
            for (irc_user_id, nick) in to_add:
                self._add_puppet(nick)
        else:
            self.send_notice(f"Member sync is set to {self.member_sync}, skipping invites.")
            if self.member_sync != "off":
                for (irc_user_id, nick) in to_add:
                    self.lazy_members[irc_user_id] = nick

        for irc_user_id in to_remove:
            self._remove_puppet(irc_user_id)

        # trust the names reply is always up-to-date
        self.on_channel = on_channel

    def is_on_channel(self, nick):
        return nick.lower() in self.on_channel

    def channel_join(self, nick):
        nick = nick.lower()
        if nick not in self.on_channel:
            self.on_channel.append(nick)

    def channel_leave(self, nick):
        nick = nick.lower()
        if nick in self.on_channel:
            self.on_channel.remove(nick)

    def on_join(self, conn, event) -> None:
        self.channel_join(event.source.nick)

        # we don't need to sync ourself
        if conn.real_nickname == event.source.nick:
            self.send_notice(f"Joined {event.target} as {event.source.nick} ({event.source.userhost})")

            # sync channel modes/key on join
            self.network.conn.mode(self.name, "")

            # send autocmd if we have one
            if self.autocmd:

                async def autocmd(self):
                    self.send_notice("Executing channel autocmd.")
                    try:
                        await self.network.commands.trigger(
                            self.autocmd, allowed=["RAW", "MSG", "NICKSERV", "NS", "CHANSERV", "CS", "WAIT"]
                        )
                    except Exception as e:
                        self.send_notice(f"Channel autocmd failed: {str(e)}")

                asyncio.ensure_future(autocmd(self))

            return

        # ensure, append, invite and join
        if self.member_sync == "full" or self.member_sync == "half":
            self._add_puppet(event.source.nick)
        elif self.member_sync != "off":
            irc_user_id = self.serv.irc_user_id(self.network.name, event.source.nick)
            self.join(irc_user_id, event.source.nick, lazy=True)

    def on_part(self, conn, event) -> None:
        self.channel_leave(event.source.nick)

        # we don't need to sync ourself
        if conn.real_nickname == event.source.nick:
            # immediately dequeue all future events
            conn.remove_tag(event.target.lower())

            self.send_notice_html(
                f"You left the channel. To rejoin, type <b>JOIN {event.target}</b> in the <b>{self.network.name}</b> network room."
            )
            self.send_notice_html("If you want to permanently leave you need to leave this room.")
            return

        irc_user_id = self.serv.irc_user_id(self.network.name, event.source.nick)
        self._remove_puppet(irc_user_id, event.arguments[0] if len(event.arguments) else None)

    def on_quit(self, conn, event) -> None:
        self.channel_leave(event.source.nick)
        irc_user_id = self.serv.irc_user_id(self.network.name, event.source.nick)
        self._remove_puppet(irc_user_id, f"Quit: {event.arguments[0]}")

    def update_key(self, modes):
        for sign, key, value in parse_channel_modes(" ".join(modes)):
            # update channel key
            if key == "k":
                value = None if sign == "-" else value
                if value != self.key:
                    self.key = value
                    if self.id is not None:
                        asyncio.ensure_future(self.save())

    def on_badchannelkey(self, conn, event) -> None:
        self.send_notice(event.arguments[1] if len(event.arguments) > 1 else "Incorrect channel key, join failed.")
        self.send_notice_html(
            f"Use <b>JOIN {html.escape(event.arguments[0])} &lt;key&gt;</b> in the network room to rejoin this channel."
        )

    def on_chanoprivsneeded(self, conn, event) -> None:
        self.send_notice(event.arguments[1] if len(event.arguments) > 1 else "You're not operator.")

    def on_cannotsendtochan(self, conn, event) -> None:
        self.send_notice(event.arguments[1] if len(event.arguments) > 1 else "Cannot send to channel.")

    def on_mode(self, conn, event) -> None:
        modes = list(event.arguments)

        self.send_notice("{} set modes {}".format(event.source.nick, " ".join(modes)))
        self.update_key(modes)

    def on_notopic(self, conn, event) -> None:
        self.send_notice(event.arguments[1] if len(event.arguments) > 1 else "No topic is set.")
        self.set_topic("")

    def on_currenttopic(self, conn, event) -> None:
        (plain, formatted) = parse_irc_formatting(event.arguments[1])
        self.send_notice(f"Topic is '{plain}'")
        self.set_topic(plain)

    def on_topicinfo(self, conn, event) -> None:
        settime = unix_to_local(event.arguments[2]) if len(event.arguments) > 2 else "?"
        (plain, formatted) = parse_irc_formatting(event.arguments[1])
        self.send_notice(f"Topic set by {plain} at {settime}")

    def on_topic(self, conn, event) -> None:
        self.send_notice("{} changed the topic".format(event.source.nick))
        (plain, formatted) = parse_irc_formatting(event.arguments[0])
        self.set_topic(plain)

    def on_kick(self, conn, event) -> None:
        self.channel_leave(event.arguments[0])

        reason = (": " + event.arguments[1]) if len(event.arguments) > 1 and len(event.arguments[1]) > 0 else ""

        if event.arguments[0] == conn.real_nickname:
            # immediately dequeue all future events
            conn.remove_tag(event.target.lower())

            self.send_notice_html(f"You were kicked from the channel by <b>{event.source.nick}</b>{reason}")
            if self.network.rejoin_kick:
                self.send_notice("Rejoin on kick is enabled, trying to join back immediately...")
                conn.join(event.target)
            else:
                self.send_notice_html(
                    f"To rejoin the channel, type <b>JOIN {event.target}</b> in the <b>{self.network.name}</b> network room."
                )
        else:
            target_user_id = self.serv.irc_user_id(self.network.name, event.arguments[0])
            self.kick(target_user_id, f"Kicked by {event.source.nick}{reason}")

    def on_banlist(self, conn, event) -> None:
        parts = list(event.arguments)
        parts.pop(0)
        self.bans_buffer.append(parts)

    def on_endofbanlist(self, conn, event) -> None:
        bans = self.bans_buffer
        self.bans_buffer = []

        self.send_notice("Current channel bans:")
        for ban in bans:
            strban = f"\t{ban[0]}"

            # all other argumenta are optional
            if len(ban) > 1:
                strban += f" set by {ban[1]}"
            if len(ban) > 2:
                strban += f" at {unix_to_local(ban[2])}"

            self.send_notice(strban)

    def on_channelmodeis(self, conn, event) -> None:
        modes = list(event.arguments)
        modes.pop(0)

        self.send_notice(f"Current channel modes: {' '.join(modes)}")

    def on_channelcreate(self, conn, event) -> None:
        created = unix_to_local(event.arguments[1])
        self.send_notice(f"Channel was created at {created}")

    def on_328(self, conn, event) -> None:
        (plain, formatted) = parse_irc_formatting(event.arguments[1])
        self.send_notice(f"URL for {event.arguments[0]}: {plain}")
