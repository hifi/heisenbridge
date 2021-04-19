import logging
from typing import List

from heisenbridge.private_room import PrivateRoom


class NetworkRoom:
    pass


class ChannelRoom(PrivateRoom):
    names_buffer: List[str]

    def init(self) -> None:
        super().init()

        self.names_buffer = []

        self.irc_register("353", self.on_irc_names)
        self.irc_register("366", self.on_irc_end_of_names)
        self.irc_register("JOIN", self.on_irc_join)
        self.irc_register("PART", self.on_irc_leave)
        self.irc_register("MODE", self.on_irc_mode)
        self.irc_register("TOPIC", self.on_irc_topic)
        self.irc_register("331", self.on_irc_reply_notopic)
        self.irc_register("332", self.on_irc_reply_topic)

    @staticmethod
    async def create(network: NetworkRoom, name: str) -> "ChannelRoom":
        logging.debug(f"ChannelRoom.create(network='{network.name}', name='{name}'")
        room_id = await network.serv.create_room(
            f"{name} ({network.name})",
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
                self.network.conn.send("PART {}".format(self.name))
            if self.name in self.network.rooms:
                del self.network.rooms[self.name]

    async def on_server_message(self, message) -> None:
        parameters = list(message.parameters)
        parameters.pop(0)
        await self.send_notice(" ".join(parameters))

    async def on_irc_names(self, event) -> None:
        self.names_buffer.extend(event.parameters[3].split())

    async def on_irc_end_of_names(self, event) -> None:
        to_remove = list(self.members)
        names = list(self.names_buffer)
        self.names_buffer = []

        for nick in names:
            nick = self.serv.strip_nick(nick)

            if self.network.nick == nick:
                continue

            # convert to mx id, check if we already have them
            irc_user_id = await self.serv.ensure_irc_user_id(self.network.name, nick)

            # make sure this user is not removed from room
            if irc_user_id in to_remove:
                to_remove.remove(irc_user_id)
                continue

            # if this user is not in room, invite and join
            if not self.in_room(irc_user_id):
                await self.serv.api.post_room_invite(self.id, irc_user_id)
                await self.serv.api.post_room_join(self.id, irc_user_id)

        # never remove us or appservice
        if self.serv.user_id in to_remove:
            to_remove.remove(self.serv.user_id)
        if self.user_id in to_remove:
            to_remove.remove(self.user_id)

        for user_id in to_remove:
            await self.serv.api.post_room_leave(self.id, user_id)
            self.members.remove(user_id)

    async def on_irc_join(self, event) -> None:
        # we don't need to sync ourself
        if self.network.nick == event.prefix.nick:
            await self.send_notice("Joined channel.")
            return

        # convert to mx id, check if we already have them
        irc_user_id = self.serv.irc_user_id(self.network_name, event.prefix.nick)
        if irc_user_id in self.members:
            return

        # append before ensuring so we don't do it twice
        self.members.append(irc_user_id)

        # ensure, append, invite and join
        irc_user_id = await self.serv.ensure_irc_user_id(self.network_name, event.prefix.nick)
        await self.serv.api.post_room_invite(self.id, irc_user_id)
        await self.serv.api.post_room_join(self.id, irc_user_id)

    async def on_irc_leave(self, event) -> None:
        # we don't need to sync ourself
        if self.network.nick == event.prefix.nick:
            return

        irc_user_id = self.serv.irc_user_id(self.network_name, event.prefix.nick)

        if irc_user_id not in self.members:
            return

        self.members.remove(irc_user_id)

        await self.serv.api.post_room_leave(self.id, irc_user_id)

    async def on_irc_mode(self, event) -> None:
        modes = list(event.parameters)
        modes.pop(0)

        await self.send_notice("{} set modes {}".format(event.prefix.nick, " ".join(modes)))

    async def on_irc_reply_notopic(self, event) -> None:
        await self.serv.api.put_room_send_state(self.id, "m.room.topic", "", {"topic": ""})

    async def on_irc_reply_topic(self, event) -> None:
        await self.serv.api.put_room_send_state(self.id, "m.room.topic", "", {"topic": event.parameters[2]})

    async def on_irc_topic(self, event) -> None:
        await self.send_notice("{} changed the topic".format(event.prefix.nick))
        await self.serv.api.put_room_send_state(self.id, "m.room.topic", "", {"topic": event.parameters[1]})
