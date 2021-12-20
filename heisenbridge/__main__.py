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
from mautrix.errors import MatrixRequestError
from mautrix.errors import MForbidden
from mautrix.types import Membership
from mautrix.util.config import yaml

from heisenbridge import __version__
from heisenbridge.appservice import AppService
from heisenbridge.channel_room import ChannelRoom
from heisenbridge.control_room import ControlRoom
from heisenbridge.identd import Identd
from heisenbridge.network_room import NetworkRoom
from heisenbridge.plumbed_room import PlumbedRoom
from heisenbridge.private_room import PrivateRoom
from heisenbridge.room import Room
from heisenbridge.room import RoomInvalidError


class MemoryBridgeStateStore(ASStateStore, MemoryStateStore):
    def __init__(self) -> None:
        ASStateStore.__init__(self)
        MemoryStateStore.__init__(self)


class BridgeAppService(AppService):
    az: MauService
    _api: HTTPAPI
    _rooms: Dict[str, Room]
    _users: Dict[str, str]

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
        m = re.match(r"^([~&@%\+]?)(.+)$", nick)
        if m:
            return (m.group(2), (m.group(1) if len(m.group(1)) > 0 else None))
        else:
            raise TypeError(f"Input nick is not valid: '{nick}'")

    def nick_from_irc_user_id(self, network, user_id):
        (name, server) = user_id.split(":")

        if server != self.server_name:
            return None

        prefix = "@" + re.sub(
            r"[^0-9a-z\-\.=\_/]",
            lambda m: "=" + m.group(0).encode("utf-8").hex(),
            f"{self.puppet_prefix}{network}_".lower(),
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
            if not event.content.is_direct:
                logging.debug("Got an invite to non-direct room, ignoring")
                return

            logging.info(f"Got an invite from {event.sender}")

            # only respond to an invite
            if event.room_id in self._rooms:
                logging.debug("Control room already open, uhh")
                return

            # handle invites against puppets
            if event.state_key != self.user_id:
                logging.info(f"Whitelisted user {event.sender} invited {event.state_key}, going to reject.")

                try:
                    await self.az.intent.user(event.state_key).kick_user(
                        event.room_id,
                        event.state_key,
                        "Inviting puppets is not supported",
                    )
                except Exception:
                    logging.exception("Failed to reject invitation.")

                return

            # set owner if we have none and the user is from the same HS
            if self.config.get("owner", None) is None and event.sender.endswith(":" + self.server_name):
                logging.info(f"We have an owner now, let us rejoice, {event.sender}!")
                self.config["owner"] = event.sender
                await self.save()

            if not self.is_user(event.sender):
                logging.info(f"Non-whitelisted user {event.sender} tried to invite us, ignoring.")
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
        whoami = await api.request(Method.GET, Path.account.whoami)
        self.user_id = whoami["user_id"]
        self.server_name = self.user_id.split(":")[1]
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
            (name, server) = member.split(":")

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
        asyncio.get_event_loop().call_later(60, self._keepalive)

    async def run(self, listen_address, listen_port, homeserver_url, owner, safe_mode):

        if "sender_localpart" not in self.registration:
            print("Missing sender_localpart from registration file.")
            sys.exit(1)

        if "namespaces" not in self.registration or "users" not in self.registration["namespaces"]:
            print("User namespaces missing from registration file.")
            sys.exit(1)

        # remove self namespace if exists
        self_ns = f"@{self.registration['sender_localpart']}:.*"
        ns_users = [x for x in self.registration["namespaces"]["users"] if x["regex"] != self_ns]

        if len(ns_users) != 1:
            print("A single user namespace is required for puppets in the registration file.")
            sys.exit(1)

        if "exclusive" not in ns_users[0] or not ns_users[0]["exclusive"]:
            print("User namespace must be exclusive.")
            sys.exit(1)

        m = re.match(r"^@(.+)([\_/])\.\*$", ns_users[0]["regex"])
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

        # mautrix migration requires us to call whoami manually at this point
        self.api = HTTPAPI(base_url=homeserver_url, token=self.registration["as_token"])
        whoami = await self.api.request(Method.GET, Path.account.whoami)

        logging.info("We are " + whoami["user_id"])

        self.user_id = whoami["user_id"]
        self.server_name = self.user_id.split(":")[1]

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
            return

        try:
            await self.az.intent.ensure_registered()
            logging.debug("Appservice user exists at least now.")
        except Exception:
            logging.exception("Unexpected failure when registering appservice user.")

        self._rooms = {}
        self._users = {}
        self.config = {
            "networks": {},
            "owner": None,
            "allow": {},
            "idents": {},
            "member_sync": "half",
            "media_url": None,
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

        # use configured media_url for endpoint if we have it
        if self.config["media_url"]:
            self.endpoint = self.config["media_url"]
        else:
            self.endpoint = await self.detect_public_endpoint()

        print("Homeserver is publicly available at " + self.endpoint, flush=True)

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

        # honor command line owner
        if owner is not None and self.config["owner"] != owner:
            logging.info(f"Overriding loaded owner with '{owner}'")
            self.config["owner"] = owner
            await self.save()

        print("Fetching joined rooms...", flush=True)

        joined_rooms = await self.az.intent.get_joined_rooms()
        logging.debug(f"Appservice rooms: {joined_rooms}")

        print(f"Bridge is in {len(joined_rooms)} rooms, initializing them...", flush=True)

        Room.init_class(self.az)

        # room types and their init order, network must be before chat and group
        room_types = [ControlRoom, NetworkRoom, PrivateRoom, ChannelRoom, PlumbedRoom]

        room_type_map = {}
        for room_type in room_types:
            room_type_map[room_type.__name__] = room_type

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
            except Exception:
                logging.exception(f"Failed to reconfigure room {room_id} during init, leaving.")

                # regardless of same mode, we ignore this room
                self.unregister_room(room_id)

                if safe_mode:
                    print("Safe mode enabled, not leaving room.", flush=True)
                else:
                    await self.leave_room(room_id, joined.keys())

        print("All valid rooms initialized, connecting network rooms...", flush=True)

        # connect network rooms one by one, this may take a while
        wait = 1
        for room in self._rooms.values():
            if type(room) == NetworkRoom and room.connected:

                def sync_connect(room):
                    asyncio.ensure_future(room.connect())

                asyncio.get_event_loop().call_later(wait, sync_connect, room)
                wait += 1

        print(f"Init done with {wait-1} networks connecting, bridge is now running!", flush=True)

        await asyncio.Event().wait()


def main():
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
    parser.add_argument("-l", "--listen-address", help="bridge listen address", default="127.0.0.1")
    parser.add_argument("-p", "--listen-port", help="bridge listen port", type=int, default="9898")
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
            "url": "http://{}:{}".format(args.listen_address, args.listen_port),
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

        with open(args.config, "w") as f:
            yaml.dump(registration, f)

        print(f"Registration file generated and saved to {args.config}")
    elif "reset" in args:
        service = BridgeAppService()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(service.reset(args.config, args.homeserver))
        loop.close()
    elif "version" in args:
        print(__version__)
    else:
        loop = asyncio.get_event_loop()
        service = BridgeAppService()
        identd = None

        service.load_reg(args.config)

        if args.identd:
            identd = Identd()
            loop.run_until_complete(identd.start_listening(service, args.identd_port))

        if os.getuid() == 0:
            if args.gid:
                gid = grp.getgrnam(args.gid).gr_gid
                os.setgid(gid)
                os.setgroups([])

            if args.uid:
                uid = pwd.getpwnam(args.uid).pw_uid
                os.setuid(uid)

        os.umask(0o077)

        loop.run_until_complete(
            service.run(args.listen_address, args.listen_port, args.homeserver, args.owner, args.safe_mode)
        )
        loop.close()


if __name__ == "__main__":
    main()
