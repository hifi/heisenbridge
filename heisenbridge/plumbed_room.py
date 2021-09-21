import logging
from typing import Optional

from heisenbridge.channel_room import ChannelRoom
from heisenbridge.matrix import MatrixError


class NetworkRoom:
    pass


class PlumbedRoom(ChannelRoom):
    need_invite = False
    max_lines = 5
    use_pastebin = True
    use_displaynames = False
    use_disambiguation = True
    use_zwsp = False
    allow_notice = False

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
            join_rules = await network.serv.api.get_room_state_event(resp["room_id"], "m.room.join_rules")
            joined_members = (await network.serv.api.get_room_joined_members(resp["room_id"]))["joined"]
        except MatrixError as e:
            network.send_notice(f"Failed to join room: {str(e)}")
            return

        room = PlumbedRoom(resp["room_id"], network.user_id, network.serv, [network.serv.user_id])
        room.name = channel.lower()
        room.key = key
        room.network = network
        room.network_name = network.name
        room.need_invite = join_rules["join_rule"] != "public"

        # stamp global member sync setting at room creation time
        room.member_sync = network.serv.config["member_sync"]

        for user_id, data in joined_members.items():
            if user_id not in room.members:
                room.members.append(user_id)
            if "display_name" in data and data["display_name"] is not None:
                room.displaynames[user_id] = str(data["display_name"])

        network.serv.register_room(room)
        network.rooms[room.name] = room
        await room.save()

        network.send_notice(f"Plumbed {resp['room_id']} to {channel}, to unplumb just kick me out.")
        return room

    def from_config(self, config: dict) -> None:
        super().from_config(config)

        if "max_lines" in config:
            self.max_lines = config["max_lines"]

        if "use_pastebin" in config:
            self.use_pastebin = config["use_pastebin"]

        if "use_displaynames" in config:
            self.use_displaynames = config["use_displaynames"]

        if "use_disambiguation" in config:
            self.use_disambiguation = config["use_disambiguation"]

        if "use_zwsp" in config:
            self.use_zwsp = config["use_zwsp"]

        if "allow_notice" in config:
            self.allow_notice = config["allow_notice"]

    def to_config(self) -> dict:
        return {
            **(super().to_config()),
            "max_lines": self.max_lines,
            "use_pastebin": self.use_pastebin,
            "use_displaynames": self.use_displaynames,
            "use_disambiguation": self.use_disambiguation,
            "use_zwsp": self.use_zwsp,
            "allow_notice": self.allow_notice,
        }

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

    def send_notice_html(self, text: str, user_id: Optional[str] = None, forward=True) -> None:
        if user_id is not None or forward is False:
            super().send_notice_html(text=text, user_id=user_id)
            return

        self.network.send_notice_html(text=f"{self.name}: {text}")

    # don't try to set room topic when we're plumbed, just show it
    def set_topic(self, topic: str, user_id: Optional[str] = None) -> None:
        self.send_notice(f"New topic is: '{topic}'")

    async def on_mx_message(self, event) -> None:
        if self.network is None or self.network.conn is None or not self.network.conn.connected:
            return

        sender = event["sender"]
        (name, server) = sender.split(":")

        # ignore self messages
        if sender == self.serv.user_id:
            return

        # prevent re-sending federated messages back
        if name.startswith("@" + self.serv.puppet_prefix) and server == self.serv.server_name:
            return

        # add ZWSP to sender to avoid pinging on IRC
        if self.use_zwsp:
            sender = f"{name[:2]}\u200B{name[2:]}:{server[:1]}\u200B{server[1:]}"

        if self.use_displaynames and event["sender"] in self.displaynames:
            sender_displayname = self.displaynames[event["sender"]]

            # ensure displayname is unique
            if self.use_disambiguation:
                for user_id, displayname in self.displaynames.items():
                    if user_id != event["sender"] and displayname == sender_displayname:
                        sender_displayname += f" ({sender})"
                        break

            # add ZWSP if displayname matches something on IRC
            if self.use_zwsp and len(sender_displayname) > 1:
                sender_displayname = f"{sender_displayname[:1]}\u200B{sender_displayname[1:]}"

            sender = sender_displayname

        # limit plumbed sender max length to 100 characters
        sender = sender[:100]

        if event["content"]["msgtype"] in ["m.image", "m.file", "m.audio", "m.video"]:

            # process media event like it was a text message
            media_event = {"content": {"body": self.serv.mxc_to_url(event["content"]["url"], event["content"]["body"])}}
            messages = self._process_event_content(media_event, prefix=f"<{sender}> ")
            self.network.conn.privmsg(self.name, messages[0])

            self.react(event["event_id"], "\U0001F517")  # link
            self.media.append([event["event_id"], event["content"]["url"]])
            await self.save()
        elif event["content"]["msgtype"] == "m.emote":
            await self._send_message(event, self.network.conn.action, prefix=f"{sender} ")
        elif event["content"]["msgtype"] == "m.text":
            await self._send_message(event, self.network.conn.privmsg, prefix=f"<{sender}> ")
        elif event["content"]["msgtype"] == "m.notice" and self.allow_notice:
            await self._send_message(event, self.network.conn.notice, prefix=f"<{sender}> ")

    def pills(self):
        ret = super().pills()

        # remove the bot from pills as it may cause confusion
        nick = self.network.conn.real_nickname.lower()
        if nick in ret:
            del ret[nick]

        return ret
