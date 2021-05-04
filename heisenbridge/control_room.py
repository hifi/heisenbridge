from heisenbridge.command_parse import CommandManager
from heisenbridge.command_parse import CommandParser
from heisenbridge.command_parse import CommandParserError
from heisenbridge.network_room import NetworkRoom
from heisenbridge.room import Room


class ControlRoom(Room):
    commands: CommandManager

    def __init__(self) -> None:
        self.commands = CommandManager()

        cmd = CommandParser(prog="NETWORKS", description="List networks")
        self.commands.register(cmd, self.cmd_networks)

        cmd = CommandParser(prog="SERVERS", description="List servers")
        cmd.add_argument("network", help="network name")
        self.commands.register(cmd, self.cmd_servers)

        cmd = CommandParser(prog="OPEN", description="Open network room to connect")
        cmd.add_argument("name", help="network name")
        self.commands.register(cmd, self.cmd_open)

        if self.serv.is_admin(self.user_id):
            cmd = CommandParser(prog="MASKS", description="List allow masks")
            self.commands.register(cmd, self.cmd_masks)

            cmd = CommandParser(prog="ADDMASK", description="Add allow mask")
            cmd.add_argument("mask", help="Matrix ID mask (eg: @friend:contoso.com or *:contoso.com)")
            cmd.add_argument("--admin", help="Admin level access", action="store_true")
            self.commands.register(cmd, self.cmd_addmask)

            cmd = CommandParser(prog="DELMASK", description="Remove allow mask")
            cmd.add_argument("mask", help="Matrix ID mask (eg: @friend:contoso.com or *:contoso.com)")
            self.commands.register(cmd, self.cmd_delmask)

            cmd = CommandParser(prog="ADDNETWORK", description="Add network")
            cmd.add_argument("name", help="network name")
            self.commands.register(cmd, self.cmd_addnetwork)

            cmd = CommandParser(prog="DELNETWORK", description="Delete network")
            cmd.add_argument("name", help="network name")
            self.commands.register(cmd, self.cmd_delnetwork)

            cmd = CommandParser(prog="ADDSERVER", description="Add server to network")
            cmd.add_argument("network", help="network name")
            cmd.add_argument("address", help="server address")
            cmd.add_argument("port", nargs="?", type=int, help="server port", default=6667)
            cmd.add_argument("--tls", action="store_true", help="use TLS encryption", default=False)
            self.commands.register(cmd, self.cmd_addserver)

            cmd = CommandParser(prog="DELSERVER", description="Delete server from network")
            cmd.add_argument("network", help="network name")
            cmd.add_argument("address", help="server address")
            cmd.add_argument("port", nargs="?", type=int, help="server port", default=6667)
            self.commands.register(cmd, self.cmd_delserver)

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
            self.send_notice(f"\t{server['address']}:{server['port']} {'with TLS' if server['tls'] else ''}")

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
            {"address": address, "port": args.port, "tls": args.tls}
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
