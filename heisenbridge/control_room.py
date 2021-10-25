import asyncio
import re
from argparse import Namespace
from urllib.parse import urlparse

from mautrix.errors import MatrixRequestError

from heisenbridge import __version__
from heisenbridge.command_parse import CommandManager
from heisenbridge.command_parse import CommandParser
from heisenbridge.command_parse import CommandParserError
from heisenbridge.network_room import NetworkRoom
from heisenbridge.parser import IRCMatrixParser
from heisenbridge.room import Room
from heisenbridge.room import RoomInvalidError


class ControlRoom(Room):
    commands: CommandManager

    def init(self):
        self.commands = CommandManager()

        cmd = CommandParser(prog="NETWORKS", description="list available networks")
        self.commands.register(cmd, self.cmd_networks)

        cmd = CommandParser(prog="SERVERS", description="list servers for a network")
        cmd.add_argument("network", help="network name (see NETWORKS)")
        self.commands.register(cmd, self.cmd_servers)

        cmd = CommandParser(prog="OPEN", description="open network for connecting")
        cmd.add_argument("name", help="network name (see NETWORKS)")
        cmd.add_argument("--new", action="store_true", help="force open a new network connection")
        self.commands.register(cmd, self.cmd_open)

        cmd = CommandParser(
            prog="QUIT",
            description="disconnect from all networks",
            epilog=(
                "For quickly leaving all networks and removing configurations in a single command.\n"
                "\n"
                "Additionally this will close current DM session with the bridge.\n"
            ),
        )
        self.commands.register(cmd, self.cmd_quit)

        if self.serv.is_admin(self.user_id):
            cmd = CommandParser(prog="MASKS", description="list allow masks")
            self.commands.register(cmd, self.cmd_masks)

            cmd = CommandParser(
                prog="ADDMASK",
                description="add new allow mask",
                epilog=(
                    "For anyone else than the owner to use this bridge they need to be allowed to talk with the bridge bot.\n"
                    "This is accomplished by adding an allow mask that determines their permission level when using the bridge.\n"
                    "\n"
                    "Only admins can manage networks, normal users can just connect.\n"
                ),
            )
            cmd.add_argument("mask", help="Matrix ID mask (eg: @friend:contoso.com or *:contoso.com)")
            cmd.add_argument("--admin", help="Admin level access", action="store_true")
            self.commands.register(cmd, self.cmd_addmask)

            cmd = CommandParser(
                prog="DELMASK",
                description="delete allow mask",
                epilog=(
                    "Note: Removing a mask only prevents starting a new DM with the bridge bot. Use FORGET for ending existing"
                    " sessions."
                ),
            )
            cmd.add_argument("mask", help="Matrix ID mask (eg: @friend:contoso.com or *:contoso.com)")
            self.commands.register(cmd, self.cmd_delmask)

            cmd = CommandParser(prog="ADDNETWORK", description="add new network")
            cmd.add_argument("name", help="network name")
            self.commands.register(cmd, self.cmd_addnetwork)

            cmd = CommandParser(prog="DELNETWORK", description="delete network")
            cmd.add_argument("name", help="network name")
            self.commands.register(cmd, self.cmd_delnetwork)

            cmd = CommandParser(prog="ADDSERVER", description="add server to a network")
            cmd.add_argument("network", help="network name")
            cmd.add_argument("address", help="server address")
            cmd.add_argument("port", nargs="?", type=int, help="server port", default=6667)
            cmd.add_argument("--tls", action="store_true", help="use TLS encryption", default=False)
            cmd.add_argument(
                "--tls-insecure",
                action="store_true",
                help="ignore TLS verification errors (hostname, self-signed, expired)",
                default=False,
            )
            cmd.add_argument("--proxy", help="use a SOCKS proxy (socks5://...)", default=None)
            self.commands.register(cmd, self.cmd_addserver)

            cmd = CommandParser(prog="DELSERVER", description="delete server from a network")
            cmd.add_argument("network", help="network name")
            cmd.add_argument("address", help="server address")
            cmd.add_argument("port", nargs="?", type=int, help="server port", default=6667)
            self.commands.register(cmd, self.cmd_delserver)

            cmd = CommandParser(prog="STATUS", description="list active users")
            self.commands.register(cmd, self.cmd_status)

            cmd = CommandParser(
                prog="FORGET",
                description="remove all connections and configuration of a user",
                epilog=(
                    "Kills all connections of this user, removes all user set configuration and makes the bridge leave all rooms"
                    " where this user is in.\n"
                    "If the user still has an allow mask they can DM the bridge again to reconfigure and reconnect.\n"
                    "\n"
                    "This is meant as a way to kick users after removing an allow mask or resetting a user after losing access to"
                    " existing account/rooms for any reason.\n"
                ),
            )
            cmd.add_argument("user", help="Matrix ID (eg: @ex-friend:contoso.com)")
            self.commands.register(cmd, self.cmd_forget)

            cmd = CommandParser(prog="DISPLAYNAME", description="change bridge displayname")
            cmd.add_argument("displayname", help="new bridge displayname")
            self.commands.register(cmd, self.cmd_displayname)

            cmd = CommandParser(prog="AVATAR", description="change bridge avatar")
            cmd.add_argument("url", help="new avatar URL (mxc:// format)")
            self.commands.register(cmd, self.cmd_avatar)

            cmd = CommandParser(
                prog="IDENT",
                description="configure ident replies",
                epilog="Note: MXID here is case sensitive, see subcommand help with IDENTCFG SET -h",
            )
            subcmd = cmd.add_subparsers(help="commands", dest="cmd")
            subcmd.add_parser("list", help="list custom idents (default)")
            cmd_set = subcmd.add_parser("set", help="set custom ident")
            cmd_set.add_argument("mxid", help="mxid of the user")
            cmd_set.add_argument("ident", help="custom ident for the user")
            cmd_remove = subcmd.add_parser("remove", help="remove custom ident")
            cmd_remove.add_argument("mxid", help="mxid of the user")
            self.commands.register(cmd, self.cmd_ident)

            cmd = CommandParser(
                prog="SYNC",
                description="set default IRC member sync mode",
                epilog="Note: Users can override this per room.",
            )
            group = cmd.add_mutually_exclusive_group()
            group.add_argument("--lazy", help="set lazy sync, members are added when they talk", action="store_true")
            group.add_argument(
                "--half", help="set half sync, members are added when they join or talk (default)", action="store_true"
            )
            group.add_argument("--full", help="set full sync, members are fully synchronized", action="store_true")
            self.commands.register(cmd, self.cmd_sync)

            cmd = CommandParser(prog="MEDIAURL", description="configure media URL for links")
            cmd.add_argument("url", nargs="?", help="new URL override")
            cmd.add_argument("--remove", help="remove URL override (will retry auto-detection)", action="store_true")
            self.commands.register(cmd, self.cmd_media_url)

            cmd = CommandParser(prog="VERSION", description="show bridge version")
            self.commands.register(cmd, self.cmd_version)

        self.mx_register("m.room.message", self.on_mx_message)

    def is_valid(self) -> bool:
        if self.user_id is None:
            return False

        if len(self.members) != 2:
            return False

        return True

    async def show_help(self):
        self.send_notice_html(
            f"<b>Howdy, stranger!</b> You have been granted access to the IRC bridge of <b>{self.serv.server_name}</b>."
        )

        try:
            return await self.commands.trigger("HELP")
        except CommandParserError as e:
            return self.send_notice(str(e))

    async def on_mx_message(self, event) -> bool:
        if str(event.content.msgtype) != "m.text" or event.sender == self.serv.user_id:
            return

        # ignore edits
        if event.content.get_edit():
            return

        try:
            if event.content.formatted_body:
                lines = str(IRCMatrixParser.parse(event.content.formatted_body)).split("\n")
            else:
                lines = event.content.body.split("\n")

            command = lines.pop(0)
            tail = "\n".join(lines) if len(lines) > 0 else None

            await self.commands.trigger(command, tail)
        except CommandParserError as e:
            self.send_notice(str(e))

    def networks(self):
        networks = {}

        for network, config in self.serv.config["networks"].items():
            config["name"] = network
            networks[network.lower()] = config

        return networks

    async def cmd_masks(self, args):
        msg = "Configured masks:\n"

        for mask, value in self.serv.config["allow"].items():
            msg += "\t{} -> {}\n".format(mask, value)

        self.send_notice(msg)

    async def cmd_addmask(self, args):
        masks = self.serv.config["allow"]

        if args.mask in masks:
            return self.send_notice("Mask already exists")

        masks[args.mask] = "admin" if args.admin else "user"
        await self.serv.save()

        self.send_notice("Mask added.")

    async def cmd_delmask(self, args):
        masks = self.serv.config["allow"]

        if args.mask not in masks:
            return self.send_notice("Mask does not exist")

        del masks[args.mask]
        await self.serv.save()

        self.send_notice("Mask removed.")

    async def cmd_networks(self, args):
        networks = self.serv.config["networks"]

        self.send_notice("Configured networks:")

        for network, data in networks.items():
            self.send_notice(f"\t{network} ({len(data['servers'])} servers)")

    async def cmd_addnetwork(self, args):
        networks = self.networks()

        if args.name.lower() in networks:
            return self.send_notice("Network already exists")

        self.serv.config["networks"][args.name] = {"servers": []}
        await self.serv.save()

        self.send_notice("Network added.")

    async def cmd_delnetwork(self, args):
        networks = self.networks()

        if args.name.lower() not in networks:
            return self.send_notice("Network does not exist")

        # FIXME: check if anyone is currently connected

        # FIXME: if no one is currently connected, leave from all network related rooms

        del self.serv.config["networks"][args.name]
        await self.serv.save()

        return self.send_notice("Network removed.")

    async def cmd_servers(self, args):
        networks = self.networks()

        if args.network.lower() not in networks:
            return self.send_notice("Network does not exist")

        network = networks[args.network.lower()]

        self.send_notice(f"Configured servers for {network['name']}:")

        for server in network["servers"]:
            with_tls = ""
            if server["tls"]:
                if "tls_insecure" in server and server["tls_insecure"]:
                    with_tls = "with insecure TLS"
                else:
                    with_tls = "with TLS"
            proxy = (
                f" through {server['proxy']}"
                if "proxy" in server and server["proxy"] is not None and len(server["proxy"]) > 0
                else ""
            )
            self.send_notice(f"\t{server['address']}:{server['port']} {with_tls}{proxy}")

    async def cmd_addserver(self, args):
        networks = self.networks()

        if args.network.lower() not in networks:
            return self.send_notice("Network does not exist")

        network = networks[args.network.lower()]
        address = args.address.lower()

        for server in network["servers"]:
            if server["address"] == address and server["port"] == args.port:
                return self.send_notice("This server already exists.")

        self.serv.config["networks"][network["name"]]["servers"].append(
            {
                "address": address,
                "port": args.port,
                "tls": args.tls,
                "tls_insecure": args.tls_insecure,
                "proxy": args.proxy,
            }
        )
        await self.serv.save()

        self.send_notice("Server added.")

    async def cmd_delserver(self, args):
        networks = self.networks()

        if args.network.lower() not in networks:
            return self.send_notice("Network does not exist")

        network = networks[args.network.lower()]
        address = args.address.lower()

        to_pop = -1
        for i, server in enumerate(network["servers"]):
            if server["address"] == address and server["port"] == args.port:
                to_pop = i
                break

        if to_pop == -1:
            return self.send_notice("No such server.")

        self.serv.config["networks"][network["name"]]["servers"].pop(to_pop)
        await self.serv.save()

        self.send_notice("Server deleted.")

    async def cmd_status(self, args):
        users = set()

        for room in self.serv.find_rooms():
            users.add(room.user_id)

        users = list(users)
        users.sort()

        self.send_notice(f"I have {len(users)} known users:")
        for user in users:
            ncontrol = len(self.serv.find_rooms("ControlRoom", user))

            self.send_notice(f"\t{user} ({ncontrol} open control rooms):")

            for network in self.serv.find_rooms("NetworkRoom", user):
                connected = "not connected"
                channels = "not in channels"
                privates = "not in PMs"
                plumbs = "not in plumbs"

                if network.conn and network.conn.connected:
                    user = network.real_user if network.real_user[0] != "?" else "?"
                    host = network.real_host if network.real_host[0] != "?" else "?"
                    connected = f"connected as {network.conn.real_nickname}!{user}@{host}"

                nchannels = 0
                nprivates = 0
                nplumbs = 0

                for room in network.rooms.values():
                    if type(room).__name__ == "PrivateRoom":
                        nprivates += 1
                    if type(room).__name__ == "ChannelRoom":
                        nchannels += 1
                    if type(room).__name__ == "PlumbedRoom":
                        nplumbs += 1

                if nprivates > 0:
                    privates = f"in {nprivates} PMs"

                if nchannels > 0:
                    channels = f"in {nchannels} channels"

                if nplumbs > 0:
                    plumbs = f"in {nplumbs} plumbs"

                self.send_notice(f"\t\t{network.name}, {connected}, {channels}, {privates}, {plumbs}")

    async def cmd_forget(self, args):
        if args.user == self.user_id:
            return self.send_notice("I can't forget you, silly!")

        rooms = self.serv.find_rooms(None, args.user)

        if len(rooms) == 0:
            return self.send_notice("No such user. See STATUS for list of users.")

        # disconnect each network room in first pass
        for room in rooms:
            if type(room) == NetworkRoom and room.conn and room.conn.connected:
                self.send_notice(f"Disconnecting {args.user} from {room.name}...")
                await room.cmd_disconnect(Namespace())

        self.send_notice(f"Leaving all {len(rooms)} rooms {args.user} was in...")

        # then just forget everything
        for room in rooms:
            self.serv.unregister_room(room.id)

            try:
                await self.az.intent.leave_room(room.id)
            except MatrixRequestError:
                pass
            try:
                await self.az.intent.forget_room(room.id)
            except MatrixRequestError:
                pass

        self.send_notice(f"Done, I have forgotten about {args.user}")

    async def cmd_displayname(self, args):
        try:
            await self.az.intent.set_displayname(args.displayname)
        except MatrixRequestError as e:
            self.send_notice(f"Failed to set displayname: {str(e)}")

    async def cmd_avatar(self, args):
        try:
            await self.az.intent.set_avatar_url(args.url)
        except MatrixRequestError as e:
            self.send_notice(f"Failed to set avatar: {str(e)}")

    async def cmd_ident(self, args):
        idents = self.serv.config["idents"]

        if args.cmd == "list" or args.cmd is None:
            self.send_notice("Configured custom idents:")
            for mxid, ident in idents.items():
                self.send_notice(f"\t{mxid} -> {ident}")
        elif args.cmd == "set":
            if not re.match(r"^[a-z][-a-z0-9]*$", args.ident):
                self.send_notice(f"Invalid ident string: {args.ident}")
                self.send_notice("Must be lowercase, start with a letter, can contain dashes, letters and numbers.")
            else:
                idents[args.mxid] = args.ident
                self.send_notice(f"Set custom ident for {args.mxid} to {args.ident}")
                await self.serv.save()
        elif args.cmd == "remove":
            if args.mxid in idents:
                del idents[args.mxid]
                self.send_notice(f"Removed custom ident for {args.mxid}")
                await self.serv.save()
            else:
                self.send_notice(f"No custom ident for {args.mxid}")

    async def cmd_sync(self, args):
        if args.lazy:
            self.serv.config["member_sync"] = "lazy"
            await self.serv.save()
        elif args.half:
            self.serv.config["member_sync"] = "half"
            await self.serv.save()
        elif args.full:
            self.serv.config["member_sync"] = "full"
            await self.serv.save()

        self.send_notice(f"Member sync is set to {self.serv.config['member_sync']}")

    async def cmd_media_url(self, args):
        if args.remove:
            self.serv.config["media_url"] = None
            await self.serv.save()
            self.serv.endpoint = await self.serv.detect_public_endpoint()
        elif args.url:
            parsed = urlparse(args.url)
            if parsed.scheme in ["http", "https"] and not parsed.params and not parsed.query and not parsed.fragment:
                self.serv.config["media_url"] = args.url
                await self.serv.save()
                self.serv.endpoint = args.url
            else:
                self.send_notice(f"Invalid media URL format: {args.url}")
                return

        self.send_notice(f"Media URL override is set to {self.serv.config['media_url']}")
        self.send_notice(f"Current active media URL: {self.serv.endpoint}")

    async def cmd_open(self, args):
        networks = self.networks()
        name = args.name.lower()

        if name not in networks:
            return self.send_notice("Network does not exist")

        network = networks[name]

        found = 0
        for room in self.serv.find_rooms(NetworkRoom, self.user_id):
            if room.name == network["name"]:
                found += 1

                if not args.new:
                    if self.user_id not in room.members:
                        self.send_notice(f"Inviting back to {room.name} ({room.id})")
                        await self.az.intent.invite_user(room.id, self.user_id)
                    else:
                        self.send_notice(f"You are already in {room.name} ({room.id})")

        # if we found at least one network room, no need to create unless forced
        if found > 0 and not args.new:
            return

        name = network["name"] if found == 0 else f"{network['name']} {found + 1}"

        self.send_notice(f"You have been invited to {name}")
        await NetworkRoom.create(self.serv, network["name"], self.user_id, name)

    async def cmd_quit(self, args):
        rooms = self.serv.find_rooms(None, self.user_id)

        # disconnect each network room in first pass
        for room in rooms:
            if type(room) == NetworkRoom and room.conn and room.conn.connected:
                self.send_notice(f"Disconnecting from {room.name}...")
                await room.cmd_disconnect(Namespace())

        self.send_notice("Closing all channels and private messages...")

        # then just forget everything
        for room in rooms:
            if room.id == self.id:
                continue

            self.serv.unregister_room(room.id)

            try:
                await self.az.intent.leave_room(room.id)
            except MatrixRequestError:
                pass
            try:
                await self.az.intent.forget_room(room.id)
            except MatrixRequestError:
                pass

        self.send_notice("Goodbye!")
        await asyncio.sleep(1)
        raise RoomInvalidError("Leaving")

    async def cmd_version(self, args):
        self.send_notice(f"heisenbridge v{__version__}")
