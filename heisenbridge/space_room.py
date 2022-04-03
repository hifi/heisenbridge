import asyncio
import logging
from typing import List

from mautrix.api import Method
from mautrix.api import Path
from mautrix.types import SpaceChildStateEventContent
from mautrix.types.event.type import EventType

from heisenbridge.room import Room


class NetworkRoom:
    pass


class SpaceRoom(Room):
    # pending rooms to attach during space creation
    pending: List[str]

    def init(self) -> None:
        super().init()

        self.pending = []

    def is_valid(self) -> bool:
        # we need to know our network
        if self.network_id is None:
            return False

        # we are valid as long as our user is in the room
        if not self.in_room(self.user_id):
            return False

        return True

    @staticmethod
    def create(network: "NetworkRoom", initial_rooms: List[str]) -> "SpaceRoom":
        logging.debug(f"SpaceRoom.create(network='{network.id}' ({network.name}))")

        room = SpaceRoom(
            None,
            network.user_id,
            network.serv,
            [network.user_id, network.serv.user_id],
            [],
        )
        room.name = network.name
        room.network = network  # only used in create_finalize
        room.network_id = network.id
        room.pending += initial_rooms
        return room

    async def create_finalize(self) -> None:
        resp = await self.az.intent.api.request(
            Method.POST,
            Path.v3.createRoom,
            {
                "creation_content": {
                    "type": "m.space",
                },
                "visibility": "private",
                "name": self.network.name,
                "topic": f"Network space for {self.network.name}",
                "invite": [self.network.user_id],
                "is_direct": False,
                "initial_state": [
                    {
                        "type": "m.space.child",
                        "state_key": self.network.id,
                        "content": {"via": [self.network.serv.server_name]},
                    }
                ],
                "power_level_content_override": {
                    "events_default": 100,
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
            },
        )

        self.id = resp["room_id"]
        self.serv.register_room(self)
        await self.save()

        # attach all pending rooms
        rooms = self.pending
        self.pending = []

        for room_id in rooms:
            await self.attach(room_id)

    def from_config(self, config: dict) -> None:
        super().from_config(config)

        if "network_id" in config:
            self.network_id = config["network_id"]

    def to_config(self) -> dict:
        return {
            **(super().to_config()),
            "network_id": self.network_id,
        }

    def cleanup(self) -> None:
        try:
            network = self.serv._rooms[self.network_id]

            if network.space == self:
                network.space = None
                network.space_id = None
                asyncio.ensure_future(network.save())
                logging.debug(f"Space {self.id} cleaned up from network {network.id}")
            else:
                logging.debug(f"Space room cleaned up as a duplicate for network {network.id}, probably fine.")
        except KeyError:
            logging.debug(f"Space room cleaned up with missing network {self.network_id}, probably fine.")

        super().cleanup()

    async def attach(self, room_id) -> None:
        # if we are attached between space request and creation just add to pending list
        if self.id is None:
            logging.debug(f"Queuing room {room_id} attachment to pending space.")
            self.pending.append(room_id)
            return

        logging.debug(f"Attaching room {room_id} to space {self.id}.")
        await self.az.intent.send_state_event(
            self.id,
            EventType.SPACE_CHILD,
            state_key=room_id,
            content=SpaceChildStateEventContent(via=[self.serv.server_name]),
        )

    async def detach(self, room_id) -> None:
        if self.id is not None:
            logging.debug(f"Detaching room {room_id} from space {self.id}.")
            await self.az.intent.send_state_event(
                self.id, EventType.SPACE_CHILD, state_key=room_id, content=SpaceChildStateEventContent()
            )
        elif room_id in self.pending:
            logging.debug(f"Removing {room_id} from space {self.id} pending queue.")
            self.pending.remove(room_id)

    async def post_init(self) -> None:
        try:
            network = self.serv._rooms[self.network_id]
            if network.space is not None:
                logging.warn(
                    f"Network room {network.id} already has space {network.space.id} but I'm {self.id}, we are dangling."
                )
                return

            network.space = self
            logging.debug(f"Space {self.id} attached to network {network.id}")
        except KeyError:
            logging.debug(f"Network room {self.network_id} was not found for space {self.id}, we are dangling.")
            self.network_id = None
