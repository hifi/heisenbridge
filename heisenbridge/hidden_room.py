import logging

from heisenbridge.appservice import AppService
from heisenbridge.room import Room


class HiddenRoom(Room):
    @staticmethod
    async def create(serv: AppService) -> "HiddenRoom":
        logging.debug("HiddenRoom.create(serv)")
        room_id = await serv.create_room("heisenbridge-hidden-room", "Invite-Sink for Heisenbridge", [])
        room = HiddenRoom(
            room_id,
            None,
            serv,
            [serv.user_id],
            [],
        )
        await room.save()
        serv.register_room(room)
        return room

    def is_valid(self) -> bool:
        # Hidden Room usage has been explicitly disabled by user
        if not self.serv.config.get("use_hidden_room", True):
            return False

        # Server already has a (different) hidden room
        if self.serv.hidden_room and self.serv.hidden_room is not self:
            return False

        return True

    async def post_init(self) -> None:
        # Those can be huge lists, but are entirely unused. Free up some memory.
        self.members = []
        self.displaynames = {}
