import asyncio
import re
from abc import ABC
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional

from heisenbridge.appservice import AppService


class RoomInvalidError(Exception):
    pass


class Room(ABC):
    id: str
    user_id: str
    serv: AppService
    members: List[str]

    _mx_handlers: Dict[str, List[Callable[[dict], bool]]]
    _notice_buf: List[str]
    _notice_task: Any

    def __init__(self, id: str, user_id: str, serv: AppService, members: List[str]):
        self.id = id
        self.user_id = user_id
        self.serv = serv
        self.members = members

        self._mx_handlers = {}
        self._notice_buf = []
        self._notice_task = None

        # we track room members
        self.mx_register("m.room.member", self._on_mx_room_member)

        self.init()

    def from_config(self, config: dict) -> None:
        pass

    def init(self) -> None:
        pass

    def is_valid(self) -> bool:
        return True

    async def cleanup(self):
        pass

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

    async def _on_mx_room_member(self, event: dict) -> None:
        if event["content"]["membership"] == "leave" and event["user_id"] in self.members:
            self.members.remove(event["user_id"])

            if not self.is_valid():
                raise RoomInvalidError(
                    f"Room {self.id} ended up invalid after membership change, returning false from event handler."
                )

        if event["content"]["membership"] == "join" and event["user_id"] not in self.members:
            self.members.append(event["user_id"])

    # send message to mx user (may be puppeted)
    async def send_message(self, text: str, user_id: Optional[str] = None) -> None:
        await self.serv.api.put_room_send_event(self.id, "m.room.message", {"msgtype": "m.text", "body": text}, user_id)

    # send emote to mx user (may be puppeted)
    async def send_emote(self, text: str, user_id: Optional[str] = None) -> None:
        await self.serv.api.put_room_send_event(
            self.id, "m.room.message", {"msgtype": "m.emote", "body": text}, user_id
        )

    async def flush_notices(self) -> None:
        await asyncio.sleep(0.2)
        text = "\n".join(self._notice_buf)
        self._notice_buf = []
        self._notice_task = None
        await self.serv.api.put_room_send_event(self.id, "m.room.message", {"msgtype": "m.notice", "body": text})

    # send notice to mx user (may be puppeted)
    async def send_notice(self, text: str, user_id: Optional[str] = None) -> None:
        # buffer only non-puppeted notices
        if user_id is None:
            self._notice_buf.append(text)

            # start task if it doesn't exist
            if self._notice_task is None:
                self._notice_task = asyncio.ensure_future(self.flush_notices())

            return

        await self.serv.api.put_room_send_event(
            self.id, "m.room.message", {"msgtype": "m.notice", "body": text}, user_id
        )

    # send notice to mx user (may be puppeted)
    async def send_notice_html(self, text: str, user_id: Optional[str] = None) -> None:

        await self.serv.api.put_room_send_event(
            self.id,
            "m.room.message",
            {
                "msgtype": "m.notice",
                "format": "org.matrix.custom.html",
                "formatted_body": text,
                "body": re.sub("<[^<]+?>", "", text),
            },
            user_id,
        )
