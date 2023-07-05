import argparse
import asyncio
import grp
import logging
import os
import pwd
import random
import re
import string
import sys
import urllib
from fnmatch import fnmatch
from typing import Dict
from typing import List
from typing import Tuple

from mautrix.api import HTTPAPI
from mautrix.api import Method
from mautrix.api import Path
from mautrix.api import SynapseAdminPath
from mautrix.appservice import AppService as MauService
from mautrix.appservice.state_store import ASStateStore
from mautrix.client.state_store.memory import MemoryStateStore
from mautrix.errors import MatrixConnectionError
from mautrix.errors import MatrixRequestError
from mautrix.errors import MForbidden
from mautrix.errors import MUserInUse
from mautrix.types import EventType
from mautrix.types import JoinRule
from mautrix.types import Membership
from mautrix.util.bridge_state import BridgeState
from mautrix.util.bridge_state import BridgeStateEvent
from mautrix.util.config import yaml

from heisenbridge import __version__
from heisenbridge.appservice import AppService
from heisenbridge.channel_room import ChannelRoom
from heisenbridge.control_room import ControlRoom
from heisenbridge.hidden_room import HiddenRoom
from heisenbridge.identd import Identd
from heisenbridge.network_room import NetworkRoom
from heisenbridge.plumbed_room import PlumbedRoom
from heisenbridge.private_room import PrivateRoom
from heisenbridge.room import Room
from heisenbridge.room import RoomInvalidError
from heisenbridge.space_room import SpaceRoom
from heisenbridge.websocket import AppserviceWebsocket


class MemoryBridgeStateStore(ASStateStore, MemoryStateStore):
    def __init__(self) -> None:
        ASStateStore.__init__(self)
        MemoryStateStore.__init__(self)


class BridgeAppService(AppService):
    az: MauService
    _api: HTTPAPI
    _rooms: Dict[str, Room]
    _users: Dict[str, str]

    async def push_bridge_state(
        self,
        state_event: BridgeStateEvent,
        error=None,
        message=None,
        ttl=None,
        remote_id=None,
    ) -> None:

        if "heisenbridge" not in self.registration or "status_endpoint" not in self.registration["heisenbridge"]:
            return

        state = BridgeState(
            state_event=state_event,
            error=error,
            message=message,
            ttl=ttl,
            remote_id=remote_id,
        )

        logging.debug(f"Updating bridge state {state}")

        await state.send(self.registration["heisenbridge"]["status_endpoint"], self.az.as_token, log=logging)

    def register_room(self, room: Room):
        self._rooms[room.id] = room

    def unregister_room(self, room_id):
        if room_id in self._rooms:
            del self._rooms[room_id]

    # this is mostly used by network rooms at init, it's a bit slow
    def find_rooms(self, rtype=None, user_id=None) -> List[Room]:
        ret = []

        if rtype is not None and type(rtype) != str:
            rtype = rtype.__name__

        for room in self._rooms.values():
            if (rtype is None or room.__class__.__name__ == rtype) and (user_id is None or room.user_id == user_id):
                ret.append(room)

        return ret

    def is_admin(self, user_id: str):
        if user_id == self.config["owner"]:
            return True

        for mask, value in self.config["allow"].items():
            if fnmatch(user_id, mask) and value == "admin":
                return True

        return False

    def is_user(self, user_id: str):
        if self.is_admin(user_id):
            return True

        for mask in self.config["allow"].keys():
            if fnmatch(user_id, mask):
                return True

        return False

    def is_local(self, mxid: str):
        return mxid.endswith(":" + self.server_name)

    def strip_nick(self, nick: str) -> Tuple[str, str]:
        m = re.match(r"^([~&@%\+!]?)(.+)$", nick)
        if m:
            return (m.group(2), (m.group(1) if len(m.group(1)) > 0 else None))
        else:
            raise TypeError(f"Input nick is not valid: '{nick}'")

    def split_irc_user_id(self, user_id):
        (name, server) = user_id.split(":", 1)

        network = None
        nick = None

        if server != self.server_name:
            return None, None

        if not name.startswith("@" + self.puppet_prefix):
            return None, None

        network_nick = name[len(self.puppet_prefix) + 1 :]

        m = re.match(r"([^" + self.puppet_separator + r"]+).(.+)$", network_nick)

        if m:
            network = re.sub(r"=([0-9a-z]{2})", lambda m: bytes.fromhex(m.group(1)).decode("utf-8"), m.group(1)).lower()
            nick = re.sub(r"=([0-9a-z]{2})", lambda m: bytes.fromhex(m.group(1)).decode("utf-8"), m.group(2)).lower()

        return network, nick

    def nick_from_irc_user_id(self, network, user_id):
        (name, server) = user_id.split(":", 1)

        if server != self.server_name:
            return None

        prefix = "@" + re.sub(
            r"[^0-9a-z\-\.=\_/]",
            lambda m: "=" + m.group(0).encode("utf-8").hex(),
            f"{self.puppet_prefix}{network}{self.puppet_separator}".lower(),
        )

        if not name.startswith(prefix):
            return None

        nick = name[len(prefix) :]
        nick = re.sub(r"=([0-9a-z]{2})", lambda m: bytes.fromhex(m.group(1)).decode("utf-8"), nick)

        return nick

    def irc_user_id(self, network, nick, at=True, server=True):
        nick, mode = self.strip_nick(nick)

        ret = re.sub(
            r"[^0-9a-z\-\.=\_/]",
            lambda m: "=" + m.group(0).encode("utf-8").hex(),
            f"{self.puppet_prefix}{network}{self.puppet_separator}{nick}".lower(),
        )

        if at:
            ret = "@" + ret

        if server:
            ret += ":" + self.server_name

        return ret

    async def cache_user(self, user_id, displayname):
        # start by caching that the user_id exists without a displayname
        if user_id not in self._users:
            self._users[user_id] = None

        # if the cached displayname is incorrect
        if displayname and self._users[user_id] != displayname:
            try:
                await self.az.intent.user(user_id).set_displayname(displayname)
                self._users[user_id] = displayname
            except MatrixRequestError as e:
                logging.warning(f"Failed to set displayname '{displayname}' for user_id '{user_id}', got '{e}'")

    def is_user_cached(self, user_id, displayname=None):
        return user_id in self._users and (displayname is None or self._users[user_id] == displayname)

    async def ensure_irc_user_id(self, network, nick, update_cache=True):
        user_id = self.irc_user_id(network, nick)

        # if we've seen this user before, we can skip registering
        if not self.is_user_cached(user_id):
            await self.az.intent.user(self.irc_user_id(network, nick)).ensure_registered()

        # always ensure the displayname is up-to-date
        if update_cache:
            await self.cache_user(user_id, nick)

        return user_id

    async def _on_mx_event(self, event):

        if event.room_id and event.room_id in self._rooms:
            try:
                room = self._rooms[event.room_id]
                await room.on_mx_event(event)
            except RoomInvalidError:
                logging.info(f"Event handler for {event.type} threw RoomInvalidError, leaving and cleaning up.")
                self.unregister_room(room.id)
                room.cleanup()

                await self.leave_room(room.id, room.members)
            except Exception:
                logging.exception("Ignoring exception from room handler. This should be fixed.")
        elif (
            str(event.type) == "m.room.member"
            and event.sender != self.user_id
            and event.content.membership == Membership.INVITE
        ):
            # set owner if we have none and the user is from the same HS
            if self.config.get("owner", None) is None and event.sender.endswith(":" + self.server_name):
                logging.info(f"We have an owner now, let us rejoice, {event.sender}!")
                self.config["owner"] = event.sender
                await self.save()

            if not self.is_user(event.sender):
                logging.info(f"Non-whitelisted user {event.sender} tried to invite us, ignoring.")
                return
            else:
                logging.info(f"Got an invite from {event.sender}")

            if not event.content.is_direct:
                logging.debug("Got an invite to non-direct room, ignoring")
                return

            # only respond to invites unknown new rooms
            if event.room_id in self._rooms:
                logging.debug("Got an invite to room we're already in, ignoring")
                return

            # handle invites against puppets
            if event.state_key != self.user_id:
                logging.info(f"Whitelisted user {event.sender} invited {event.state_key}, going to reject.")

                try:
                    await self.az.intent.user(event.state_key).kick_user(
                        event.room_id,
                        event.state_key,
                        "Will invite YOU instead",
                    )
                except Exception:
                    logging.exception("Failed to reject invitation.")

                (network, nick) = self.split_irc_user_id(event.state_key)

                if network is not None and nick is not None:
                    for room in self.find_rooms(NetworkRoom, event.sender):
                        if room.name.lower() == network.lower():
                            logging.debug(
                                "Found matching network room ({network}) for {event.sender}, emulating query command for {nick}"
                            )
                            await room.cmd_query(argparse.Namespace(nick=nick, message=[]))
                            break

                return

            logging.info(f"Whitelisted user {event.sender} invited us, going to accept.")

            # accept invite sequence
            try:
                room = ControlRoom(id=event.room_id, user_id=event.sender, serv=self, members=[event.sender], bans=[])
                await room.save()
                self.register_room(room)

                await self.az.intent.join_room(room.id)

                # show help on open
                await room.show_help()
            except Exception:
                if event.room_id in self._rooms:
                    del self._rooms[event.room_id]
                logging.exception("Failed to create control room.")
        else:
            pass
            # print(json.dumps(event, indent=4, sort_keys=True))

    async def detect_public_endpoint(self):
        async with self.api.session as session:
            # first try https well-known
            try:
                resp = await session.request(
                    "GET",
                    "https://{}/.well-known/matrix/client".format(self.server_name),
                )
                data = await resp.json(content_type=None)
                return data["m.homeserver"]["base_url"]
            except Exception:
                logging.debug("Did not find .well-known for HS")

            # try https directly
            try:
                resp = await session.request("GET", "https://{}/_matrix/client/versions".format(self.server_name))
                await resp.json(content_type=None)
                return "https://{}".format(self.server_name)
            except Exception:
                logging.debug("Could not use direct connection to HS")

            # give up
            logging.warning("Using internal URL for homeserver, media links are likely broken!")
            return str(self.api.base_url)

    def mxc_to_url(self, mxc, filename=None):
        mxc = urllib.parse.urlparse(mxc)

        if filename is None:
            filename = ""
        else:
            filename = "/" + urllib.parse.quote(filename)

        return "{}/_matrix/media/r0/download/{}{}{}".format(self.endpoint, mxc.netloc, mxc.path, filename)

    async def reset(self, config_file, homeserver_url):
        with open(config_file) as f:
            registration = yaml.load(f)

        api = HTTPAPI(base_url=homeserver_url, token=registration["as_token"])
        whoami = await api.request(Method.GET, Path.v3.account.whoami)
        self.user_id = whoami["user_id"]
        self.server_name = self.user_id.split(":", 1)[1]
        print("We are " + whoami["user_id"])

        self.az = MauService(
            id=registration["id"],
            domain=self.server_name,
            server=homeserver_url,
            as_token=registration["as_token"],
            hs_token=registration["hs_token"],
            bot_localpart=registration["sender_localpart"],
            state_store=MemoryBridgeStateStore(),
        )

        try:
            await self.az.start(host="127.0.0.1", port=None)
        except Exception:
            logging.exception("Failed to listen.")
            return

        joined_rooms = await self.az.intent.get_joined_rooms()
        print(f"Leaving from {len(joined_rooms)} rooms...")

        for room_id in joined_rooms:
            print(f"Leaving from {room_id}...")
            await self.leave_room(room_id, None)

        print("Resetting configuration...")
        self.config = {}
        await self.save()

        print("All done!")

    def load_reg(self, config_file):
        with open(config_file) as f:
            self.registration = yaml.load(f)

    async def leave_room(self, room_id, members):
        members = members if members else []

        for member in members:
            (name, server) = member.split(":", 1)

            if name.startswith("@" + self.puppet_prefix) and server == self.server_name:
                try:
                    await self.az.intent.user(member).leave_room(room_id)
                except Exception:
                    logging.exception("Removing puppet on leave failed")

        try:
            await self.az.intent.leave_room(room_id)
        except MatrixRequestError:
            pass
        try:
            await self.az.intent.forget_room(room_id)
        except MatrixRequestError:
            pass

    def _keepalive(self):
        async def put_presence():
            try:
                await self.az.intent.set_presence(self.user_id)
            except Exception:
                pass

        asyncio.ensure_future(put_presence())
        asyncio.get_running_loop().call_later(60, self._keepalive)

    async def ensure_hidden_room(self):
        use_hidden_room = self.config.get("use_hidden_room", False)

        if not self.hidden_room and use_hidden_room:
            try:
                resp = await self.az.intent.api.request(Method.GET, Path.v3.capabilities)
                if resp["capabilities"]["m.room_versions"]["available"].get("9", None) == "stable":
                    self.hidden_room = await HiddenRoom.create(self)
                else:
                    m = "No stable version 9 rooms available, hidden room disabled."
                    logging.info(m)
                    raise Exception(m)
            except KeyError:
                m = "Unexpected capabilities response from server."
                logging.debug(m)
                raise Exception(m)
        elif self.hidden_room and not use_hidden_room:
            joined = await self.az.state_store.get_member_profiles(self.hidden_room.id, (Membership.JOIN,))

            self.unregister_room(self.hidden_room.id)
            await self.leave_room(self.hidden_room.id, joined.keys())
            self.hidden_room = None

            for room in self._rooms.values():
                if room.hidden_room_id:
                    # Re-Run post init if room has a hidden room set
                    await room.post_init()

        return use_hidden_room

    async def run(self, listen_address, listen_port, homeserver_url, owner, safe_mode):

        if "sender_localpart" not in self.registration:
            print("Missing sender_localpart from registration file.")
            sys.exit(1)

        if "namespaces" not in self.registration or "users" not in self.registration["namespaces"]:
            print("User namespaces missing from registration file.")
            sys.exit(1)

        # remove self namespace if exists
        ns_users = [
            x
            for x in self.registration["namespaces"]["users"]
            if x["regex"].split(":")[0] != f"@{self.registration['sender_localpart']}"
        ]

        if len(ns_users) != 1:
            print("A single user namespace is required for puppets in the registration file.")
            sys.exit(1)

        if "exclusive" not in ns_users[0] or not ns_users[0]["exclusive"]:
            print("User namespace must be exclusive.")
            sys.exit(1)

        m = re.match(r"^@(.+)([\_/])\.[\*\+]:?", ns_users[0]["regex"])
        if not m:
            print(
                "User namespace regex must be an exact prefix like '@irc_.*' that includes the separator character (_ or /)."
            )
            sys.exit(1)

        self.puppet_separator = m.group(2)
        self.puppet_prefix = m.group(1) + self.puppet_separator

        print(f"Heisenbridge v{__version__}", flush=True)
        if safe_mode:
            print("Safe mode is enabled.", flush=True)

        url = urllib.parse.urlparse(homeserver_url)
        ws = None
        if url.scheme in ["ws", "wss"]:
            print("Using websockets to receive transactions. Listening is still enabled.")
            ws = AppserviceWebsocket(homeserver_url, self.registration["as_token"], self._on_mx_event)
            homeserver_url = url._replace(scheme=("https" if url.scheme == "wss" else "http")).geturl()
            print(f"Connecting to HS at {homeserver_url}")

        self.api = HTTPAPI(base_url=homeserver_url, token=self.registration["as_token"])

        # conduit requires that the appservice user is registered before whoami
        wait = 0
        while True:
            try:
                await self.api.request(
                    Method.POST,
                    Path.v3.register,
                    {
                        "type": "m.login.application_service",
                        "username": self.registration["sender_localpart"],
                    },
                )
                logging.debug("Appservice user registration succeeded.")
                break
            except MUserInUse:
                logging.debug("Appservice user is already registered.")
                break
            except MatrixConnectionError as e:
                if wait < 30:
                    wait += 5
                logging.warning(f"Failed to connect to HS: {e}, retrying in {wait} seconds...")
                await asyncio.sleep(wait)
            except Exception:
                logging.exception("Unexpected failure when registering appservice user.")
                sys.exit(1)

        # mautrix migration requires us to call whoami manually at this point
        whoami = await self.api.request(Method.GET, Path.v3.account.whoami)

        logging.info("We are " + whoami["user_id"])

        self.user_id = whoami["user_id"]
        self.server_name = self.user_id.split(":", 1)[1]

        self.az = MauService(
            id=self.registration["id"],
            domain=self.server_name,
            server=homeserver_url,
            as_token=self.registration["as_token"],
            hs_token=self.registration["hs_token"],
            bot_localpart=self.registration["sender_localpart"],
            state_store=MemoryBridgeStateStore(),
        )
        self.az.matrix_event_handler(self._on_mx_event)

        try:
            await self.az.start(host=listen_address, port=listen_port)
        except Exception:
            logging.exception("Failed to listen.")
            sys.exit(1)

        try:
            await self.az.intent.ensure_registered()
            logging.debug("Appservice user exists at least now.")
        except Exception:
            logging.exception("Unexpected failure when registering appservice user.")
            sys.exit(1)

        if "heisenbridge" in self.registration and "displayname" in self.registration["heisenbridge"]:
            try:
                logging.debug(
                    f"Overriding displayname from registration file to {self.registration['heisenbridge']['displayname']}"
                )
                await self.az.intent.set_displayname(self.registration["heisenbridge"]["displayname"])
            except MatrixRequestError as e:
                logging.warning(f"Failed to set displayname: {str(e)}")

        self._rooms = {}
        self._users = {}
        self.config = {
            "networks": {},
            "owner": None,
            "allow": {},
            "idents": {},
            "member_sync": "half",
            "max_lines": 0,
            "use_pastebin": False,
            "media_url": None,
            "namespace": self.puppet_prefix,
        }
        logging.debug(f"Default config: {self.config}")
        self.synapse_admin = False

        try:
            is_admin = await self.api.request(Method.GET, SynapseAdminPath.v1.users[self.user_id].admin)
            self.synapse_admin = is_admin["admin"]
        except MForbidden:
            logging.info(f"We ({self.user_id}) are not a server admin, inviting puppets is required.")
        except Exception:
            logging.info("Seems we are not connected to Synapse, inviting puppets is required.")

        # load config from HS
        await self.load()

        async def _resolve_media_endpoint():
            endpoint = await self.detect_public_endpoint()

            # only rewrite it if it wasn't changed
            if self.endpoint == str(self.api.base_url):
                self.endpoint = endpoint

            print("Homeserver is publicly available at " + self.endpoint, flush=True)

        # use configured media_url for endpoint if we have it
        if "heisenbridge" in self.registration and "media_url" in self.registration["heisenbridge"]:
            logging.debug(
                f"Overriding media URL from regirstation file to {self.registration['heisenbridge']['media_url']}"
            )
            self.endpoint = self.registration["heisenbridge"]["media_url"]
        elif self.config["media_url"]:
            self.endpoint = self.config["media_url"]
        else:
            print("Trying to detect homeserver public endpoint, this might take a while...", flush=True)
            self.endpoint = str(self.api.base_url)
            asyncio.ensure_future(_resolve_media_endpoint())

        logging.info("Starting presence loop")
        self._keepalive()

        # do a little migration for servers, remove this later
        for network in self.config["networks"].values():
            new_servers = []

            for server in network["servers"]:
                if isinstance(server, str):
                    new_servers.append({"address": server, "port": 6667, "tls": False})

            if len(new_servers) > 0:
                logging.debug("Migrating servers from old to new config format")
                network["servers"] = new_servers

        logging.debug(f"Merged configuration from HS: {self.config}")

        # prevent starting bridge with changed namespace
        if self.config["namespace"] != self.puppet_prefix:
            logging.error(
                f"Previously used namespace '{self.config['namespace']}' does not match current '{self.puppet_prefix}'."
            )
            sys.exit(1)

        # honor command line owner
        if owner is not None and self.config["owner"] != owner:
            logging.info(f"Overriding loaded owner with '{owner}'")
            self.config["owner"] = owner

        # always ensure our merged and migrated configuration is up-to-date
        await self.save()

        print("Fetching joined rooms...", flush=True)

        joined_rooms = await self.az.intent.get_joined_rooms()
        logging.debug(f"Appservice rooms: {joined_rooms}")

        print(f"Bridge is in {len(joined_rooms)} rooms, initializing them...", flush=True)

        Room.init_class(self.az)
        self.hidden_room = None

        # room types and their init order, network must be before chat and group
        room_types = [HiddenRoom, ControlRoom, NetworkRoom, PrivateRoom, ChannelRoom, PlumbedRoom, SpaceRoom]

        room_type_map = {}
        for room_type in room_types:
            room_type.init_class(self.az)
            room_type_map[room_type.__name__] = room_type

        # we always auto-open control room for owner
        owner_control_open = False

        # import all rooms
        for room_id in joined_rooms:
            joined = {}

            try:
                config = await self.az.intent.get_account_data("irc", room_id)

                if "type" not in config or "user_id" not in config:
                    raise Exception("Invalid config")

                cls = room_type_map.get(config["type"])
                if not cls:
                    raise Exception("Unknown room type")

                # refresh room members state
                await self.az.intent.get_room_members(room_id)

                joined = await self.az.state_store.get_member_profiles(room_id, (Membership.JOIN,))
                banned = await self.az.state_store.get_members(room_id, (Membership.BAN,))

                room = cls(id=room_id, user_id=config["user_id"], serv=self, members=joined.keys(), bans=banned)
                room.from_config(config)

                join_rules = await self.az.intent.get_state_event(room_id, EventType.ROOM_JOIN_RULES)
                if join_rules.join_rule == JoinRule.RESTRICTED and join_rules.allow:
                    room.hidden_room_id = join_rules.allow[0].room_id

                # add to room displayname
                for user_id, member in joined.items():
                    if member.displayname is not None:
                        room.displaynames[user_id] = member.displayname
                    # add to global puppet cache if it's a puppet
                    if user_id.startswith("@" + self.puppet_prefix) and self.is_local(user_id):
                        self._users[user_id] = member.displayname

                # only add valid rooms to event handler
                if room.is_valid():
                    self._rooms[room_id] = room
                else:
                    room.cleanup()
                    raise Exception("Room validation failed after init")

                if cls is HiddenRoom:
                    self.hidden_room = room

                if cls == ControlRoom and room.user_id == self.config["owner"]:
                    owner_control_open = True
            except Exception:
                logging.exception(f"Failed to reconfigure room {room_id} during init, leaving.")

                # regardless of same mode, we ignore this room
                self.unregister_room(room_id)

                if safe_mode:
                    print("Safe mode enabled, not leaving room.", flush=True)
                else:
                    await self.leave_room(room_id, joined.keys())

        try:
            await self.ensure_hidden_room()
        except Exception as e:
            logging.debug(f"Failed setting up hidden room: {e}")

        print("All valid rooms initialized, connecting network rooms...", flush=True)

        wait = 1
        for room in list(self._rooms.values()):
            await room.post_init()

            # check again if we're still valid
            if not room.is_valid():
                logging.debug(f"Room {room.id} failed validation after post init, leaving.")

                self.unregister_room(room.id)

                if not safe_mode:
                    await self.leave_room(room.id, room.members)

                continue

            # connect network rooms one by one, this may take a while
            if type(room) == NetworkRoom and room.connected:

                def sync_connect(room):
                    asyncio.ensure_future(room.connect())

                asyncio.get_running_loop().call_later(wait, sync_connect, room)
                wait += 1

        print(f"Init done with {wait-1} networks connecting, bridge is now running!", flush=True)

        await self.push_bridge_state(BridgeStateEvent.UNCONFIGURED)

        # late start WS to avoid getting transactions too early
        if ws:
            await ws.start()

        if self.config["owner"] and not owner_control_open:
            print(f"Opening control room for owner {self.config['owner']}")
            try:
                room_id = await self.az.intent.create_room(invitees=[self.config["owner"]])

                room = ControlRoom(
                    id=room_id, user_id=self.config["owner"], serv=self, members=[self.config["owner"]], bans=[]
                )
                await room.save()
                self.register_room(room)

                await self.az.intent.join_room(room.id)

                # show help on open
                await room.show_help()
            except Exception:
                print("Failed to create control room, huh")

        await asyncio.Event().wait()


async def async_main():
    parser = argparse.ArgumentParser(
        prog=os.path.basename(sys.executable) + " -m " + __package__,
        description=f"a bouncer-style Matrix IRC bridge (v{__version__})",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-v", "--verbose", help="logging verbosity level: once is info, twice is debug", action="count", default=0
    )
    req = parser.add_mutually_exclusive_group(required=True)
    req.add_argument(
        "-c",
        "--config",
        help="registration YAML file path, must be writable if generating",
    )
    req.add_argument(
        "--version",
        action="store_true",
        help="show bridge version",
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-l",
        "--listen-address",
        help="bridge listen address (default: as specified in url in config, 127.0.0.1 otherwise)",
    )
    parser.add_argument(
        "-p",
        "--listen-port",
        help="bridge listen port (default: as specified in url in config, 9898 otherwise)",
        type=int,
    )
    parser.add_argument("-u", "--uid", help="user id to run as", default=None)
    parser.add_argument("-g", "--gid", help="group id to run as", default=None)
    parser.add_argument("-i", "--identd", action="store_true", help="enable identd service")
    parser.add_argument("--identd-port", type=int, default="113", help="identd listen port")
    parser.add_argument(
        "--generate",
        action="store_true",
        help="generate registration YAML for Matrix homeserver (Synapse)",
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--generate-compat",
        action="store_true",
        help="generate registration YAML for Matrix homeserver (Dendrite and Conduit)",
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="reset ALL bridge configuration from homeserver and exit",
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--safe-mode",
        action="store_true",
        help="prevent appservice from leaving invalid rooms on startup (for debugging)",
    )
    parser.add_argument(
        "-o",
        "--owner",
        help="set owner MXID (eg: @user:homeserver) or first talking local user will claim the bridge",
        default=None,
    )
    parser.add_argument(
        "homeserver",
        nargs="?",
        help="URL of Matrix homeserver",
        default="http://localhost:8008",
    )

    args = parser.parse_args()

    logging_level = logging.WARNING
    if args.verbose > 0:
        logging_level = logging.INFO
        if args.verbose > 1:
            logging_level = logging.DEBUG

    logging.basicConfig(stream=sys.stdout, level=logging_level)

    if "generate" in args or "generate_compat" in args:
        letters = string.ascii_letters + string.digits

        registration = {
            "id": "heisenbridge",
            "url": "http://{}:{}".format(args.listen_address or "127.0.0.1", args.listen_port or 9898),
            "as_token": "".join(random.choice(letters) for i in range(64)),
            "hs_token": "".join(random.choice(letters) for i in range(64)),
            "rate_limited": False,
            "sender_localpart": "heisenbridge",
            "namespaces": {
                "users": [{"regex": "@irc_.*", "exclusive": True}],
                "aliases": [],
                "rooms": [],
            },
        }

        if "generate_compat" in args:
            registration["namespaces"]["users"].append({"regex": "@heisenbridge:.*", "exclusive": True})

        if os.path.isfile(args.config):
            print("Registration file already exists, not overwriting.")
            sys.exit(1)

        if args.config == "-":
            yaml.dump(registration, sys.stdout)
        else:
            with open(args.config, "w") as f:
                yaml.dump(registration, f)

            print(f"Registration file generated and saved to {args.config}")
    elif "reset" in args:
        service = BridgeAppService()
        await service.reset(args.config, args.homeserver)
    elif "version" in args:
        print(__version__)
    else:
        service = BridgeAppService()
        identd = None

        service.load_reg(args.config)

        if args.identd:
            identd = Identd()
            await identd.start_listening(service, args.identd_port)

        if os.getuid() == 0:
            if args.gid:
                gid = grp.getgrnam(args.gid).gr_gid
                os.setgid(gid)
                os.setgroups([])

            if args.uid:
                uid = pwd.getpwnam(args.uid).pw_uid
                os.setuid(uid)

        os.umask(0o077)

        listen_address = args.listen_address
        listen_port = args.listen_port

        if not listen_address:
            try:
                url = urllib.parse.urlparse(service.registration["url"])
                listen_address = url.hostname
            except Exception:
                listen_address = "127.0.0.1"

        if not listen_port:
            try:
                url = urllib.parse.urlparse(service.registration["url"])
                listen_port = url.port
            except Exception:
                listen_port = 9898

        await service.run(listen_address, listen_port, args.homeserver, args.owner, args.safe_mode)


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
