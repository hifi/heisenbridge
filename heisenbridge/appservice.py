import logging
from abc import ABC
from abc import abstractmethod
from typing import List

from mautrix.api import Method
from mautrix.api import Path
from mautrix.errors import MNotFound


class Room:
    pass


class AppService(ABC):
    user_id: str
    server_name: str
    config: dict
    hidden_room: Room

    async def load(self):
        try:
            self.config.update(await self.az.intent.get_account_data("irc"))
        except MNotFound:
            await self.save()

    async def save(self):
        await self.az.intent.set_account_data("irc", self.config)

    async def create_room(self, name: str, topic: str, invite: List[str], restricted: str = None) -> str:
        req = {
            "visibility": "private",
            "name": name,
            "topic": topic,
            "invite": invite,
            "is_direct": False,
            "power_level_content_override": {
                "users_default": 0,
                "invite": 100,
                "kick": 100,
                "redact": 100,
                "ban": 100,
                "events": {
                    "m.room.name": 0,
                    "m.room.avatar": 0,  # these work as long as rooms are private
                },
            },
        }

        if restricted is not None:
            resp = await self.az.intent.api.request(Method.GET, Path.v3.capabilities)
            try:
                def_ver = resp["capabilities"]["m.room_versions"]["default"]
            except KeyError:
                logging.debug("Unexpected capabilities reply")
                def_ver = None

            # If room version is in range of 1..8, request v9
            if def_ver in [str(v) for v in range(1, 9)]:
                req["room_version"] = "9"

            req["initial_state"] = [
                {
                    "type": "m.room.join_rules",
                    "state_key": "",
                    "content": {
                        "join_rule": "restricted",
                        "allow": [{"type": "m.room_membership", "room_id": restricted}],
                    },
                }
            ]

        resp = await self.az.intent.api.request(Method.POST, Path.v3.createRoom, req)

        return resp["room_id"]

    @abstractmethod
    def register_room(self, room: Room):
        pass

    @abstractmethod
    def find_rooms(self, type=None, user_id: str = None) -> List[Room]:
        pass
