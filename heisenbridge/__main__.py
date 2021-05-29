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

import aiohttp
import yaml
from aiohttp import ClientSession
from aiohttp import web

from heisenbridge.appservice import AppService
from heisenbridge.channel_room import ChannelRoom
from heisenbridge.control_room import ControlRoom
from heisenbridge.identd import Identd
from heisenbridge.matrix import Matrix
from heisenbridge.matrix import MatrixError
from heisenbridge.matrix import MatrixForbidden
from heisenbridge.matrix import MatrixUserInUse
from heisenbridge.network_room import NetworkRoom
from heisenbridge.plumbed_room import PlumbedRoom
from heisenbridge.private_room import PrivateRoom
from heisenbridge.room import Room
from heisenbridge.room import RoomInvalidError


class BridgeAppService(AppService):
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

    def irc_user_id(self, network, nick, at=True, server=True):
        nick, mode = self.strip_nick(nick)

        ret = re.sub(
            r"[^0-9a-z\-\.=\_/]",
            lambda m: "=" + m.group(0).encode("utf-8").hex(),
            f"{self.puppet_prefix}{network}_{nick}".lower(),
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
                await self.api.put_user_displayname(user_id, displayname)
            except MatrixError as e:
                logging.warning(f"Failed to set displayname '{displayname}' for user_id '{user_id}', got '{e}'")

            self._users[user_id] = displayname

    def is_user_cached(self, user_id):
        return user_id in self._users

    async def ensure_irc_user_id(self, network, nick):
        user_id = self.irc_user_id(network, nick)

        # if we've seen this user before, we can skip registering
        if not self.is_user_cached(user_id):
            try:
                await self.api.post_user_register(
                    {
                        "type": "m.login.application_service",
                        "username": self.irc_user_id(network, nick, False, False),
                    }
                )
            except MatrixUserInUse:
                pass

        # always ensure the displayname is up-to-date
        await self.cache_user(user_id, nick)

        return user_id

    async def _on_mx_event(self, event):
        # keep user cache up-to-date
        if "sender" in event:
            await self.cache_user(event["sender"], None)

        if "room_id" in event and event["room_id"] in self._rooms:
            try:
                room = self._rooms[event["room_id"]]
                await room.on_mx_event(event)
            except RoomInvalidError:
                logging.info(f"Event handler for {event['type']} threw RoomInvalidError, leaving and cleaning up.")
                self.unregister_room(room.id)
                room.cleanup()

                try:
                    await self.api.post_room_leave(room.id)
                except MatrixError:
                    pass
                try:
                    await self.api.post_room_forget(room.id)
                except MatrixError:
                    pass
            except Exception:
                logging.exception("Ignoring exception from room handler. This should be fixed.")
        elif (
            event["type"] == "m.room.member"
            and event["sender"] != self.user_id
            and event["content"]["membership"] == "invite"
        ):
            if "is_direct" not in event["content"] or event["content"]["is_direct"] is not True:
                logging.debug("Got an invite to non-direct room, ignoring")
                return

            logging.info(f"Got an invite from {event['sender']}")

            # only respond to an invite
            if event["room_id"] in self._rooms:
                logging.debug("Control room already open, uhh")
                return

            # set owner if we have none and the user is from the same HS
            if self.config.get("owner", None) is None and event["sender"].endswith(":" + self.server_name):
                logging.info(f"We have an owner now, let us rejoice, {event['sender']}!")
                self.config["owner"] = event["sender"]
                await self.save()

            if not self.is_user(event["sender"]):
                logging.info(f"Non-whitelisted user {event['sender']} tried to invite us, ignoring.")
                return

            logging.info(f"Whitelisted user {event['sender']} invited us, going to accept.")

            # accept invite sequence
            try:
                room = ControlRoom(id=event["room_id"], user_id=event["sender"], serv=self, members=[event["sender"]])
                await room.save()
                self.register_room(room)

                # sometimes federated rooms take a while to join
                for i in range(6):
                    try:
                        await self.api.post_room_join(room.id)
                        break
                    except MatrixForbidden:
                        logging.debug("Responding to invite failed, retrying")
                        await asyncio.sleep((i + 1) * 5)

                # show help on open
                await room.show_help()
            except Exception:
                if event["room_id"] in self._rooms:
                    del self._rooms[event["room_id"]]
                logging.exception("Failed to create control room.")
        else:
            pass
            # print(json.dumps(event, indent=4, sort_keys=True))

    async def _transaction(self, req):
        body = await req.json()

        for event in body["events"]:
            asyncio.ensure_future(self._on_mx_event(event))

        return web.json_response({})

    async def detect_public_endpoint(self):
        async with ClientSession() as session:
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
            return self.api.url

    def mxc_to_url(self, mxc):
        mxc = urllib.parse.urlparse(mxc)
        return "{}/_matrix/media/r0/download/{}{}".format(self.endpoint, mxc.netloc, mxc.path)

    async def reset(self, config_file, homeserver_url):
        with open(config_file) as f:
            registration = yaml.safe_load(f)

        self.api = Matrix(homeserver_url, registration["as_token"])

        whoami = await self.api.get_user_whoami()
        self.user_id = whoami["user_id"]
        print("We are " + whoami["user_id"])

        resp = await self.api.get_user_joined_rooms()
        print(f"Leaving from {len(resp['joined_rooms'])} rooms...")

        for room_id in resp["joined_rooms"]:
            print(f"Leaving from {room_id}...")
            await self.api.post_room_leave(room_id)

            try:
                await self.api.post_room_forget(room_id)
                print("  ...and it's gone")
            except MatrixError:
                print("  ...but couldn't forget, that's fine")
                pass

        print("Resetting configuration...")
        self.config = {}
        await self.save()

        print("All done!")

    def load_reg(self, config_file):
        with open(config_file) as f:
            self.registration = yaml.safe_load(f)

    async def run(self, listen_address, listen_port, homeserver_url, owner):

        app = aiohttp.web.Application()
        app.router.add_put("/transactions/{id}", self._transaction)
        app.router.add_put("/_matrix/app/v1/transactions/{id}", self._transaction)

        if (
            "namespaces" not in self.registration
            or "users" not in self.registration["namespaces"]
            or len(self.registration["namespaces"]["users"]) != 1
        ):
            print("A single user namespace is required for puppets in the registration file.")
            sys.exit(1)

        user_namespace = self.registration["namespaces"]["users"][0]
        if "exclusive" not in user_namespace or not user_namespace["exclusive"]:
            print("User namespace must be exclusive.")
            sys.exit(1)

        m = re.match(r"^@([^.]+)\.\*$", user_namespace["regex"])
        if not m:
            print("User namespace regex must be a prefix like '@irc_.*' and not contain anything else.")
            sys.exit(1)

        self.puppet_prefix = m.group(1)

        self.api = Matrix(homeserver_url, self.registration["as_token"])

        try:
            await self.api.post_user_register(
                {
                    "type": "m.login.application_service",
                    "username": self.registration["sender_localpart"],
                }
            )
            logging.debug("Appservice user registration succeeded.")
        except MatrixUserInUse:
            logging.debug("Appservice user is already registered.")
        except Exception:
            logging.exception("Unexpected failure when registering appservice user.")

        whoami = await self.api.get_user_whoami()
        logging.info("We are " + whoami["user_id"])

        self._rooms = {}
        self._users = {}
        self.user_id = whoami["user_id"]
        self.server_name = self.user_id.split(":")[1]
        self.config = {"networks": {}, "owner": None, "allow": {}}
        logging.debug(f"Default config: {self.config}")
        self.synapse_admin = False

        try:
            is_admin = await self.api.get_synapse_admin_users_admin(self.user_id)
            self.synapse_admin = is_admin["admin"]
        except MatrixForbidden:
            logging.warning(
                f"We ({self.user_id}) are not a server admin, inviting puppets manually is required which is slightly slower."
            )
        except Exception:
            logging.info("Seems we are not connected to Synapse, inviting puppets is required.")

        # figure out where we are publicly for MXC conversions
        self.endpoint = await self.detect_public_endpoint()
        print("Homeserver is publicly available at " + self.endpoint)

        # load config from HS
        await self.load()

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

        resp = await self.api.get_user_joined_rooms()
        logging.debug(f"Appservice rooms: {resp['joined_rooms']}")

        # room types and their init order, network must be before chat and group
        room_types = [ControlRoom, NetworkRoom, PrivateRoom, ChannelRoom, PlumbedRoom]

        room_type_map = {}
        for room_type in room_types:
            room_type_map[room_type.__name__] = room_type

        # import all rooms
        for room_id in resp["joined_rooms"]:
            try:
                config = await self.api.get_room_account_data(self.user_id, room_id, "irc")

                if "type" not in config or "user_id" not in config:
                    raise Exception("Invalid config")

                cls = room_type_map.get(config["type"])
                if not cls:
                    raise Exception("Unknown room type")

                members = list((await self.api.get_room_joined_members(room_id))["joined"].keys())

                # add to cache immediately but without known displayname
                for user_id in members:
                    await self.cache_user(user_id, None)

                room = cls(id=room_id, user_id=config["user_id"], serv=self, members=members)
                room.from_config(config)

                # only add valid rooms to event handler
                if room.is_valid():
                    self._rooms[room_id] = room
                else:
                    room.cleanup()
                    raise Exception("Room validation failed after init")
            except Exception:
                logging.exception(f"Failed to reconfigure room {room_id} during init, leaving.")

                self.unregister_room(room_id)

                try:
                    await self.api.post_room_leave(room_id)
                except MatrixError:
                    pass
                try:
                    await self.api.post_room_forget(room_id)
                except MatrixError:
                    pass

        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, listen_address, listen_port)
        await site.start()

        logging.info("Connecting network rooms...")

        # connect network rooms one by one, this may take a while
        wait = 1
        for room in self._rooms.values():
            if type(room) == NetworkRoom and room.connected:

                def sync_connect(room):
                    asyncio.ensure_future(room.connect())

                asyncio.get_event_loop().call_later(wait, sync_connect, room)
                wait += 1

        logging.info("Init done, bridge is now running!")

        await asyncio.Event().wait()


def main():
    parser = argparse.ArgumentParser(
        prog=os.path.basename(sys.executable) + " -m " + __package__,
        description="a Matrix IRC bridge",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-v", "--verbose", help="logging verbosity level: once is info, twice is debug", action="count", default=0
    )
    parser.add_argument(
        "-c",
        "--config",
        help="registration YAML file path, must be writable if generating",
        required=True,
    )
    parser.add_argument("-l", "--listen-address", help="bridge listen address", default="127.0.0.1")
    parser.add_argument("-p", "--listen-port", help="bridge listen port", type=int, default="9898")
    parser.add_argument("-u", "--uid", help="user id to run as", default=None)
    parser.add_argument("-g", "--gid", help="group id to run as", default=None)
    parser.add_argument(
        "-i",
        "--identd",
        action="store_true",
        help="enable identd on TCP port 113, requires root",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="generate registration YAML for Matrix homeserver",
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="reset ALL bridge configuration from homeserver and exit",
        default=argparse.SUPPRESS,
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

    if "generate" in args:
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

        with open(args.config, "w") as f:
            yaml.dump(registration, f, sort_keys=False)

        print(f"Registration file generated and saved to {args.config}")
    elif "reset" in args:
        service = BridgeAppService()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(service.reset(args.config, args.homeserver))
        loop.close()
    else:
        loop = asyncio.get_event_loop()
        service = BridgeAppService()
        identd = None

        service.load_reg(args.config)

        if args.identd:
            identd = Identd()
            loop.run_until_complete(identd.start_listening(service))

        if os.getuid() == 0:
            if args.gid:
                gid = grp.getgrnam(args.gid).gr_gid
                os.setgid(gid)
                os.setgroups([])

            if args.uid:
                uid = pwd.getpwnam(args.uid).pw_uid
                os.setuid(uid)

        os.umask(0o077)

        if identd:
            loop.create_task(identd.run())

        loop.run_until_complete(service.run(args.listen_address, args.listen_port, args.homeserver, args.owner))
        loop.close()


if __name__ == "__main__":
    main()
