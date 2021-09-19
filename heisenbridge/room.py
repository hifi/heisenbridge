import asyncio
import logging
import re
from abc import ABC
from collections import defaultdict
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional

from heisenbridge.appservice import AppService
from heisenbridge.event_queue import EventQueue
from heisenbridge.matrix import MatrixForbidden


class RoomInvalidError(Exception):
    pass


class Room(ABC):
    id: str
    user_id: str
    serv: AppService
    members: List[str]
    lazy_members: Dict[str, str]
    displaynames: Dict[str, str]
    need_invite: bool = True

    _mx_handlers: Dict[str, List[Callable[[dict], bool]]]
    _queue: EventQueue

    def __init__(self, id: str, user_id: str, serv: AppService, members: List[str]):
        self.id = id
        self.user_id = user_id
        self.serv = serv
        self.members = members
        self.lazy_members = {}
        self.displaynames = {}
        self.last_messages = defaultdict(str)

        self._mx_handlers = {}
        self._queue = EventQueue(self._flush_events)

        # start event queue
        if self.id:
            self._queue.start()

        # we track room members
        self.mx_register("m.room.member", self._on_mx_room_member)
        self.mx_register("m.room.join_rules", self._on_mx_room_join_rules)

        self.init()

    def from_config(self, config: dict) -> None:
        pass

    def init(self) -> None:
        pass

    def is_valid(self) -> bool:
        return True

    def cleanup(self):
        self._queue.stop()

    def to_config(self) -> dict:
        return {}

    async def save(self) -> None:
        config = self.to_config()
        config["type"] = type(self).__name__
        config["user_id"] = self.user_id
        await self.serv.api.put_room_account_data(self.serv.user_id, self.id, "irc", config)

    def mx_register(self, type: str, func: Callable[[dict], bool]) -> None:
        if type not in self._mx_handlers:
            self._mx_handlers[type] = []

        self._mx_handlers[type].append(func)

    async def on_mx_event(self, event: dict) -> None:
        handlers = self._mx_handlers.get(event["type"], [self._on_mx_unhandled_event])

        for handler in handlers:
            await handler(event)

    def in_room(self, user_id):
        return user_id in self.members

    async def _on_mx_unhandled_event(self, event: dict) -> None:
        pass

    async def _on_mx_room_join_rules(self, event: dict) -> None:
        self.need_invite = event["content"]["join_rule"] != "public"
        logging.debug("Room invite rule updated to " + str(self.need_invite))
        await self.save()

    async def _on_mx_room_member(self, event: dict) -> None:
        if event["content"]["membership"] == "leave" and event["state_key"] in self.members:
            self.members.remove(event["state_key"])
            if event["state_key"] in self.displaynames:
                del self.displaynames[event["state_key"]]
            if event["state_key"] in self.last_messages:
                del self.last_messages[event["state_key"]]

            if not self.is_valid():
                raise RoomInvalidError(
                    f"Room {self.id} ended up invalid after membership change, returning false from event handler."
                )

        if event["content"]["membership"] == "join":
            if event["state_key"] not in self.members:
                self.members.append(event["state_key"])

            if "displayname" in event["content"] and event["content"]["displayname"] is not None:
                self.displaynames[event["state_key"]] = str(event["content"]["displayname"])
            elif event["state_key"] in self.displaynames:
                del self.displaynames[event["state_key"]]

    async def _join(self, user_id, nick=None):
        if not self.serv.synapse_admin or not self.serv.is_local(self.id):

            if self.need_invite:
                await self.serv.api.post_room_invite(self.id, user_id)

            for i in range(0, 10):
                try:
                    await self.serv.api.post_room_join(self.id, user_id)
                    break
                except MatrixForbidden:
                    logging.warning("Puppet joining a room was forbidden, retrying")
                    await asyncio.sleep(i)
        else:
            await self.serv.api.post_synapse_admin_room_join(self.id, user_id)

        self.members.append(user_id)
        if nick is not None:
            self.displaynames[user_id] = nick

        if user_id in self.lazy_members:
            del self.lazy_members[user_id]

    async def _flush_events(self, events):
        for event in events:
            try:
                if event["type"] == "_join":
                    if event["user_id"] not in self.members:
                        if event["lazy"]:
                            self.lazy_members[event["user_id"]] = event["nick"]
                        else:
                            await self._join(event["user_id"], event["nick"])
                elif event["type"] == "_leave":
                    if event["user_id"] in self.lazy_members:
                        del self.lazy_members[event["user_id"]]

                    if event["user_id"] in self.members:
                        if event["reason"] is not None:
                            await self.serv.api.post_room_kick(
                                self.id, event["user_id"], user_id=event["user_id"], reason=event["reason"]
                            )
                        else:
                            await self.serv.api.post_room_leave(self.id, event["user_id"])
                        self.members.remove(event["user_id"])
                        if event["user_id"] in self.displaynames:
                            del self.displaynames[event["user_id"]]
                elif event["type"] == "_rename":
                    old_irc_user_id = self.serv.irc_user_id(self.network.name, event["old_nick"])
                    new_irc_user_id = self.serv.irc_user_id(self.network.name, event["new_nick"])

                    # if we are lazy loading and this user has never spoken, update that
                    if old_irc_user_id in self.lazy_members:
                        del self.lazy_members[old_irc_user_id]
                        self.lazy_members[new_irc_user_id] = event["new_nick"]
                        continue

                    # this event is created for all rooms, skip if irrelevant
                    if old_irc_user_id not in self.members:
                        continue

                    # check if we can just update the displayname
                    if old_irc_user_id != new_irc_user_id:
                        # ensure we have the new puppet
                        await self.serv.ensure_irc_user_id(self.network.name, event["new_nick"])

                        # old puppet away
                        await self.serv.api.post_room_kick(
                            self.id,
                            old_irc_user_id,
                            user_id=old_irc_user_id,
                            reason=f"Changing nick to {event['new_nick']}",
                        )
                        self.members.remove(old_irc_user_id)
                        if old_irc_user_id in self.displaynames:
                            del self.displaynames[old_irc_user_id]

                        # new puppet in
                        if new_irc_user_id not in self.members:
                            await self._join(new_irc_user_id, event["new_nick"])

                elif event["type"] == "_kick":
                    if event["user_id"] in self.members:
                        await self.serv.api.post_room_kick(self.id, event["user_id"], event["reason"])
                        self.members.remove(event["user_id"])
                        if event["user_id"] in self.displaynames:
                            del self.displaynames[event["user_id"]]
                elif event["type"] == "_ensure_irc_user_id":
                    await self.serv.ensure_irc_user_id(event["network"], event["nick"])
                elif "state_key" in event:
                    await self.serv.api.put_room_send_state(
                        self.id, event["type"], event["state_key"], event["content"], event["user_id"]
                    )
                else:
                    # invite puppet *now* if we are lazy loading and it should be here
                    if event["user_id"] in self.lazy_members and event["user_id"] not in self.members:
                        await self.serv.ensure_irc_user_id(self.network.name, self.lazy_members[event["user_id"]])
                        await self._join(event["user_id"], self.lazy_members[event["user_id"]])

                    # if we get an event from unknown user (outside room for some reason) we may have a fallback
                    if event["user_id"] is not None and event["user_id"] not in self.members:
                        if "fallback_html" in event and event["fallback_html"] is not None:
                            fallback_html = event["fallback_html"]
                        else:
                            fallback_html = (
                                f"{event['user_id']} sent {event['type']} but is not in the room, this is a bug."
                            )

                        # create fallback event
                        event["content"] = {
                            "msgtype": "m.notice",
                            "body": re.sub("<[^<]+?>", "", event["fallback_html"]),
                            "format": "org.matrix.custom.html",
                            "formatted_body": fallback_html,
                        }

                        # unpuppet
                        event["user_id"] = None
                    await self.serv.api.put_room_send_event(self.id, event["type"], event["content"], event["user_id"])
            except Exception:
                logging.exception("Queued event failed")

    # send message to mx user (may be puppeted)
    def send_message(
        self, text: str, user_id: Optional[str] = None, formatted=None, fallback_html: Optional[str] = None
    ) -> None:
        if formatted:
            event = {
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "format": "org.matrix.custom.html",
                    "body": text,
                    "formatted_body": formatted,
                },
                "user_id": user_id,
                "fallback_html": fallback_html,
            }
        else:
            event = {
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": text,
                },
                "user_id": user_id,
                "fallback_html": fallback_html,
            }

        self._queue.enqueue(event)

    # send emote to mx user (may be puppeted)
    def send_emote(self, text: str, user_id: Optional[str] = None, fallback_html: Optional[str] = None) -> None:
        event = {
            "type": "m.room.message",
            "content": {
                "msgtype": "m.emote",
                "body": text,
            },
            "user_id": user_id,
            "fallback_html": fallback_html,
        }

        self._queue.enqueue(event)

    # send notice to mx user (may be puppeted)
    def send_notice(
        self, text: str, user_id: Optional[str] = None, formatted=None, fallback_html: Optional[str] = None
    ) -> None:
        if formatted:
            event = {
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.notice",
                    "format": "org.matrix.custom.html",
                    "body": text,
                    "formatted_body": formatted,
                },
                "user_id": user_id,
                "fallback_html": fallback_html,
            }
        else:
            event = {
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.notice",
                    "body": text,
                },
                "user_id": user_id,
                "fallback_html": fallback_html,
            }

        self._queue.enqueue(event)

    # send notice to mx user (may be puppeted)
    def send_notice_html(self, text: str, user_id: Optional[str] = None) -> None:
        event = {
            "type": "m.room.message",
            "content": {
                "msgtype": "m.notice",
                "body": re.sub("<[^<]+?>", "", text),
                "format": "org.matrix.custom.html",
                "formatted_body": text,
            },
            "user_id": user_id,
        }

        self._queue.enqueue(event)

    def react(self, event_id: str, text: str) -> None:
        event = {
            "type": "m.reaction",
            "content": {
                "m.relates_to": {
                    "event_id": event_id,
                    "key": text,
                    "rel_type": "m.annotation",
                }
            },
            "user_id": None,
        }

        self._queue.enqueue(event)

    def set_topic(self, topic: str, user_id: Optional[str] = None) -> None:
        event = {
            "type": "m.room.topic",
            "content": {
                "topic": topic,
            },
            "state_key": "",
            "user_id": user_id,
        }

        self._queue.enqueue(event)

    def join(self, user_id: str, nick=None, lazy=False) -> None:
        event = {
            "type": "_join",
            "content": {},
            "user_id": user_id,
            "nick": nick,
            "lazy": lazy,
        }

        self._queue.enqueue(event)

    def leave(self, user_id: str, reason: Optional[str] = None) -> None:
        event = {
            "type": "_leave",
            "content": {},
            "reason": reason,
            "user_id": user_id,
        }

        self._queue.enqueue(event)

    def rename(self, old_nick: str, new_nick: str) -> None:
        event = {
            "type": "_rename",
            "content": {},
            "old_nick": old_nick,
            "new_nick": new_nick,
        }

        self._queue.enqueue(event)

    def kick(self, user_id: str, reason: str) -> None:
        event = {
            "type": "_kick",
            "content": {},
            "reason": reason,
            "user_id": user_id,
        }

        self._queue.enqueue(event)

    def ensure_irc_user_id(self, network, nick):
        event = {
            "type": "_ensure_irc_user_id",
            "content": {},
            "network": network,
            "nick": nick,
            "user_id": None,
        }

        self._queue.enqueue(event)
