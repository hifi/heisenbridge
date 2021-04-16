from typing import List
from abc import ABC, abstractmethod

from heisenbridge.room import Room
from heisenbridge.matrix import Matrix, MatrixNotFound

class AppService(ABC):
    api: Matrix
    user_id: str
    server_name: str
    config: dict

    async def load(self):
        try:
            self.config.update(await self.api.get_user_account_data(self.user_id, 'irc'))
        except MatrixNotFound:
            await self.save()

    async def save(self):
        await self.api.put_user_account_data(self.user_id, 'irc', self.config)

    async def create_room(self, name: str, topic: str, invite: List[str]) -> str:
        resp = await self.api.post_room_create({
            'visibility': 'private',
            'name': name,
            'topic': topic,
            'invite': invite,
            'is_direct': False,
            'power_level_content_override': {
                'users_default': 0,
                'invite': 100,
                'kick': 100,
                'redact': 100,
                'ban': 100,
            },
        })

        return resp['room_id']

    @abstractmethod
    def register_room(self, room: Room):
        pass

    @abstractmethod
    def find_rooms(self, type, user_id: str = None) -> List[Room]:
        pass
