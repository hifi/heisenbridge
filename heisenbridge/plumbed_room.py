import logging
from typing import Optional

from heisenbridge.channel_room import ChannelRoom
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
            join_rules = await network.serv.api.get_room_state_event(id, "m.room.join_rules")
        except MatrixError as e:
            network.send_notice(f"Failed to join room: {str(e)}")
            return

        room = PlumbedRoom(resp["room_id"], network.user_id, network.serv, [network.serv.user_id])
        room.name = channel.lower()
        room.key = key
        room.network = network
        room.network_name = network.name
        room.need_invite = join_rules["join_rule"] != "public"

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
                (name, server) = member.split(":")

                if name.startswith("@" + self.serv.puppet_prefix) and server == self.serv.server_name:
                    try:
                        await self.serv.api.post_room_leave(self.id, member)
                    except Exception:
                        logging.exception("Removing puppet on relaybot leave failed")

        await super()._on_mx_room_member(event)

    def send_notice(
        self,
        text: str,
        user_id: Optional[str] = None,
        formatted=None,
        fallback_html: Optional[str] = None,
        forward=True,
    ):
        if user_id is not None or forward is False:
            super().send_notice(text=text, user_id=user_id, formatted=formatted, fallback_html=fallback_html)
            return

        self.network.send_notice(
            text=f"{self.name}: {text}", user_id=user_id, formatted=formatted, fallback_html=fallback_html
        )

    # don't try to set room topic when we're plumbed, just show it
    def set_topic(self, topic: str, user_id: Optional[str] = None) -> None:
        self.send_notice(f"New topic is: '{topic}'")

    async def on_mx_message(self, event) -> None:
        if self.network is None or self.network.conn is None or not self.network.conn.connected:
            return

        (name, server) = event["user_id"].split(":")

        # prevent re-sending federated messages back
        if name.startswith("@" + self.serv.puppet_prefix) and server == self.serv.server_name:
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
                return

            messages = []

            for line in body.split("\n"):
                if line == "":
                    continue

                messages += split_long(
                    self.network.conn.real_nickname,
                    self.network.conn.user,
                    self.network.real_host,
                    self.name,
                    f"<{event['user_id']}> {line}",
                )

            for i, message in enumerate(messages):
                if i == 4:
                    self.send_notice("Message was truncated to four lines for IRC.", forward=False)
                    self.network.conn.privmsg(self.name, "... (message truncated)")
                    return
                self.network.conn.privmsg(self.name, message)
