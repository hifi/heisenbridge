import asyncio

from heisenbridge.command_parse import CommandManager
from heisenbridge.command_parse import CommandParser
from heisenbridge.command_parse import CommandParserError
from heisenbridge.matrix import MatrixError
from heisenbridge.network_room import NetworkRoom
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
        if event["content"]["msgtype"] != "m.text" or event["user_id"] == self.serv.user_id:
            return True

        try:
            return await self.commands.trigger(event["content"]["body"])
        except CommandParserError as e:
            return self.send_notice(str(e))

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
            self.send_notice(f"\t{server['address']}:{server['port']} {with_tls}")

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
            {"address": address, "port": args.port, "tls": args.tls, "tls_insecure": args.tls_insecure}
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

        self.send_notice(f"I have {len(users)} known users:")
        for user in users:
            ncontrol = len(self.serv.find_rooms("ControlRoom", user))

            self.send_notice(f"\t{user} ({ncontrol} open control rooms):")

            for network in self.serv.find_rooms("NetworkRoom", user):
                connected = "not connected"
                channels = "not on any channel"
                privates = "not in any DMs"

                if network.conn and network.conn.connected:
                    connected = f"connected as {network.conn.real_nickname}"

                nchannels = 0
                nprivates = 0

                for room in network.rooms.values():
                    if type(room).__name__ == "PrivateRoom":
                        nprivates += 1
                    if type(room).__name__ == "ChannelRoom":
                        nchannels += 1

                if nprivates > 0:
                    privates = f"in {nprivates} DMs"

                if nchannels > 0:
                    channels = f"on {nchannels} channels"

                self.send_notice(f"\t\t{network.name}, {connected}, {channels}, {privates}")

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
                room.cmd_disconnect()

        self.send_notice(f"Leaving all {len(rooms)} rooms {args.user} was in...")

        # then just forget everything
        for room in rooms:
            self.serv.unregister_room(room.id)

            try:
                await self.serv.api.post_room_leave(room.id)
            except MatrixError:
                pass
            try:
                await self.serv.api.post_room_forget(room.id)
            except MatrixError:
                pass

        self.send_notice(f"Done, I have forgotten about {args.user}")

    async def cmd_open(self, args):
        networks = self.networks()
        name = args.name.lower()

        if name not in networks:
            return self.send_notice("Network does not exist")

        network = networks[name]

        for room in self.serv.find_rooms(NetworkRoom, self.user_id):
            if room.name == network["name"]:
                if self.user_id not in room.members:
                    self.send_notice(f"Inviting back to {room.name}")
                    await self.serv.api.post_room_invite(room.id, self.user_id)
                else:
                    self.send_notice(f"You are already in {room.name}")
                return

        self.send_notice(f"You have been invited to {network['name']}")
        await NetworkRoom.create(self.serv, network["name"], self.user_id)

    async def cmd_quit(self, args):
        rooms = self.serv.find_rooms(None, self.user_id)

        # disconnect each network room in first pass
        for room in rooms:
            if type(room) == NetworkRoom and room.conn and room.conn.connected:
                self.send_notice(f"Disconnecting from {room.name}...")
                room.cmd_disconnect()

        self.send_notice("Closing all channels and private messages...")

        # then just forget everything
        for room in rooms:
            if room.id == self.id:
                continue

            self.serv.unregister_room(room.id)

            try:
                await self.serv.api.post_room_leave(room.id)
            except MatrixError:
                pass
            try:
                await self.serv.api.post_room_forget(room.id)
            except MatrixError:
                pass

        self.send_notice("Goodbye!")
        await asyncio.sleep(1)
        raise RoomInvalidError("Leaving")
