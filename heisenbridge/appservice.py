from abc import ABC
from abc import abstractmethod
from typing import List

from heisenbridge.matrix import Matrix
from heisenbridge.matrix import MatrixNotFound


class Room:
    pass


class AppService(ABC):
    api: Matrix
    user_id: str
    server_name: str
    config: dict

    async def load(self):
        try:
            self.config.update(await self.api.get_user_account_data(self.user_id, "irc"))
        except MatrixNotFound:
            await self.save()

    async def save(self):
        await self.api.put_user_account_data(self.user_id, "irc", self.config)

    async def create_room(self, name: str, topic: str, invite: List[str]) -> str:
        resp = await self.api.post_room_create(
            {
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
        )

        return resp["room_id"]

    @abstractmethod
    def register_room(self, room: Room):
        pass

    @abstractmethod
    def find_rooms(self, type=None, user_id: str = None) -> List[Room]:
        pass
