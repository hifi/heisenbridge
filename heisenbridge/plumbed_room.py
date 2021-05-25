import logging
import re

from heisenbridge.channel_room import ChannelRoom
from heisenbridge.command_parse import CommandParserError
from heisenbridge.matrix import MatrixError
from heisenbridge.private_room import split_long


class NetworkRoom:
    pass


class PlumbedRoom(ChannelRoom):
    need_invite = False

    def is_valid(self) -> bool:
        # we are valid as long as the appservice is in the room
        if not self.in_room(self.serv.user_id):
            return False

        return True

    @staticmethod
    async def create(network: "NetworkRoom", id: str, channel: str, key: str) -> "ChannelRoom":
        logging.debug(f"PlumbedRoom.create(network='{network.name}', id='{id}', channel='{channel}', key='{key}'")

        try:
            resp = await network.serv.api.post_room_join_alias(id)
        except MatrixError as e:
            network.send_notice(f"Failed to join room: {str(e)}")
            return

        room = PlumbedRoom(resp["room_id"], network.user_id, network.serv, [network.serv.user_id])
        room.name = channel.lower()
        room.key = key
        room.network = network
        room.network_name = network.name

        network.serv.register_room(room)
        network.rooms[room.name] = room
        await room.save()

        network.send_notice(f"Plumbed {resp['room_id']} to {channel}, to unplumb just kick me out.")
        return room

    async def _on_mx_room_member(self, event: dict) -> None:
        # if we are leaving the room, make all puppets leave
        if event["content"]["membership"] == "leave" and event["state_key"] == self.serv.user_id:

            # stop event queue immediately
            self._queue.stop()

            for member in self.members:
                if member.startswith("@" + self.serv.puppet_prefix):
                    await self.serv.api.post_room_leave(self.id, member)

        await super()._on_mx_room_member(event)

    async def on_mx_message(self, event) -> None:
        if self.network is None or self.network.conn is None or not self.network.conn.connected:
            return

        # prevent re-sending federated messages back
        if event["user_id"].startswith("@" + self.serv.puppet_prefix):
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
            self.network.conn.action(self.name, "{} {}".format(event["user_id"], body))
        elif event["content"]["msgtype"] == "m.image":
            self.network.conn.privmsg(
                self.name, "<{}> {}".format(event["user_id"], self.serv.mxc_to_url(event["content"]["url"]))
            )
        elif event["content"]["msgtype"] == "m.text":
            if "m.new_content" in event["content"]:
                self.send_notice("Editing messages is not supported on IRC, edited text was NOT sent.")
                return

            # allow commanding the appservice in rooms
            match = re.match(r"^\s*([^:,\s]+)[\s:,]*(.+)$", body)
            if match and match.group(1).lower() == "heisenbridge":
                if event["user_id"] != self.user_id:
                    self.send_notice("I only obey {self.user_id}.")
                    return
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
                    self.network.conn.privmsg(self.name, "<{}> {}".format(event["user_id"], body))
