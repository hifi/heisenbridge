import asyncio
import logging
from datetime import datetime
from typing import List

from heisenbridge.command_parse import CommandParser
from heisenbridge.private_room import PrivateRoom


class NetworkRoom:
    pass


class ChannelRoom(PrivateRoom):
    key: str
    names_buffer: List[str]
    bans_buffer: List[str]

    def init(self) -> None:
        super().init()

        self.key = None

        cmd = CommandParser(prog="MODES", description="fetch current channel modes")
        self.commands.register(cmd, self.cmd_modes)

        cmd = CommandParser(prog="NAMES", description="resynchronize channel members")
        self.commands.register(cmd, self.cmd_names)

        cmd = CommandParser(prog="BANS", description="show channel ban list")
        self.commands.register(cmd, self.cmd_bans)

        self.names_buffer = []
        self.bans_buffer = []

    def from_config(self, config: dict) -> None:
        if "name" not in config:
            raise Exception("No name key in config for ChatRoom")

        if "network" not in config:
            raise Exception("No network key in config for ChatRoom")

        self.name = config["name"]
        self.network_name = config["network"]

        if "key" in config:
            self.key = config["key"]

    def to_config(self) -> dict:
        return {"name": self.name, "network": self.network_name, "key": self.key}

    @staticmethod
    async def create(network: NetworkRoom, name: str) -> "ChannelRoom":
        logging.debug(f"ChannelRoom.create(network='{network.name}', name='{name}'")

        # handle !room names properly
        visible_name = name
        if visible_name.startswith("!"):
            visible_name = "!" + visible_name[6:]

        room_id = await network.serv.create_room(
            f"{visible_name} ({network.name})",
            "",
            [network.user_id],
        )
        room = ChannelRoom(room_id, network.user_id, network.serv, [network.serv.user_id])
        room.name = name.lower()
        room.network = network
        room.network_name = network.name
        await room.save()
        network.serv.register_room(room)
        return room

    def is_valid(self) -> bool:
        if not self.in_room(self.user_id):
            return False

        return super().is_valid()

    async def cleanup(self) -> None:
        if self.network:
            if self.network.conn and self.network.conn.connected:
                self.network.conn.part(self.name)
            if self.name in self.network.rooms:
                del self.network.rooms[self.name]

    async def cmd_modes(self, args) -> None:
        self.network.conn.mode(self.name, "")

    async def cmd_names(self, args) -> None:
        self.network.conn.names(self.name)

    async def cmd_bans(self, args) -> None:
        self.network.conn.mode(self.name, "+b")

    async def on_pubmsg(self, conn, event):
        await self.on_privmsg(conn, event)

    async def on_pubnotice(self, conn, event):
        await self.on_privnotice(conn, event)

    async def on_namreply(self, conn, event) -> None:
        self.names_buffer.extend(event.arguments[2].split())

    async def _add_puppet(self, nick):
        irc_user_id = await self.serv.ensure_irc_user_id(self.network.name, nick)
        await self.serv.api.post_room_invite(self.id, irc_user_id)
        await self.serv.api.post_room_join(self.id, irc_user_id)

        if irc_user_id not in self.members:
            self.members.append(irc_user_id)

    async def _remove_puppet(self, user_id):
        if user_id == self.serv.user_id or user_id == self.user_id:
            return

        await self.serv.api.post_room_leave(self.id, user_id)

        if user_id in self.members:
            self.members.remove(user_id)

    async def on_endofnames(self, conn, event) -> None:
        to_remove = list(self.members)
        to_add = []
        names = list(self.names_buffer)
        self.names_buffer = []

        for nick in names:
            nick = self.serv.strip_nick(nick)

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

        # create a big bunch of invites, aiohttp will have some limits in-place
        for (irc_user_id, nick) in to_add:
            # to prevent multiple NAMES commands to overlap, add to list immediately
            if irc_user_id not in self.members:
                self.members.append(irc_user_id)

            asyncio.ensure_future(self._add_puppet(nick))

        # create a big bunch of leaves
        for irc_user_id in to_remove:
            # to prevent multiple NAMES commands to overlap, add to list immediately
            if irc_user_id in self.members:
                self.members.append(irc_user_id)

            asyncio.ensure_future(self._remove_puppet(irc_user_id))

    async def on_join(self, conn, event) -> None:
        # we don't need to sync ourself
        if conn.real_nickname == event.source.nick:
            self.send_notice("Joined channel.")
            # sync channel modes/key on join
            self.network.conn.mode(self.name, "")
            return

        # convert to mx id, check if we already have them
        irc_user_id = self.serv.irc_user_id(self.network_name, event.source.nick)
        if irc_user_id in self.members:
            return

        # append before ensuring so we don't do it twice
        self.members.append(irc_user_id)

        # ensure, append, invite and join
        await self._add_puppet(event.source.nick)

    async def on_quit(self, conn, event) -> None:
        await self.on_part(conn, event)

    async def on_part(self, conn, event) -> None:
        # we don't need to sync ourself
        if conn.real_nickname == event.source.nick:
            return

        irc_user_id = self.serv.irc_user_id(self.network_name, event.source.nick)

        if irc_user_id not in self.members:
            return

        await self._remove_puppet(irc_user_id)

    async def update_key(self, modes):
        # update channel key
        if modes[0].startswith("-") and modes[0].find("k") > -1:
            if self.key is not None:
                self.key = None
                await self.save()
        elif modes[0].startswith("+"):
            key_pos = modes[0].find("k")
            if key_pos > -1:
                key = modes[key_pos]
                if self.key != key:
                    self.key = key
                    await self.save()

    async def on_mode(self, conn, event) -> None:
        modes = list(event.arguments)

        self.send_notice("{} set modes {}".format(event.source.nick, " ".join(modes)))
        await self.update_key(modes)

    async def on_notopic(self, conn, event) -> None:
        await self.serv.api.put_room_send_state(self.id, "m.room.topic", "", {"topic": ""})

    async def on_currenttopic(self, conn, event) -> None:
        await self.serv.api.put_room_send_state(self.id, "m.room.topic", "", {"topic": event.arguments[1]})

    async def on_topic(self, conn, event) -> None:
        self.send_notice("{} changed the topic".format(event.source.nick))
        await self.serv.api.put_room_send_state(self.id, "m.room.topic", "", {"topic": event.arguments[0]})

    async def on_kick(self, conn, event) -> None:
        target_user_id = await self.serv.ensure_irc_user_id(self.network.name, event.arguments[0])
        await self.serv.api.post_room_kick(
            self.id, target_user_id, f"Kicked by {event.source.nick}: {event.arguments[1]}"
        )

        if target_user_id in self.members:
            self.members.remove(target_user_id)

    async def on_banlist(self, conn, event) -> None:
        parts = list(event.arguments)
        parts.pop(0)
        logging.info(parts)
        self.bans_buffer.append(parts)

    async def on_endofbanlist(self, conn, event) -> None:
        bans = self.bans_buffer
        self.bans_buffer = []

        self.send_notice("Current channel bans:")
        for ban in bans:
            bantime = datetime.utcfromtimestamp(int(ban[2])).strftime("%c %Z")
            self.send_notice(f"\t{ban[0]} set by {ban[1]} at {bantime}")

    async def on_channelmodeis(self, conn, event) -> None:
        modes = list(event.arguments)
        modes.pop(0)

        self.send_notice(f"Current channel modes: {' '.join(modes)}")
        await self.update_key(modes)

    async def on_channelcreate(self, conn, event) -> None:
        created = datetime.utcfromtimestamp(int(event.arguments[1])).strftime("%c %Z")
        self.send_notice(f"Channel was created at {created}")
