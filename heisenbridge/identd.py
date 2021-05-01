import asyncio
import logging
import re

from heisenbridge.appservice import AppService
from heisenbridge.network_room import NetworkRoom

class Identd():
    async def handle(self, reader, writer):
        try:
            data = await reader.read(128)
            query = data.decode()
            req_addr, req_port, *_ = writer.get_extra_info("peername")

            m = re.match(r"^(\d+)\s*,\s*(\d+)", query)
            if m:
                src_port = int(m.group(1))
                dst_port = int(m.group(2))

                response = f"{src_port}, {dst_port} : ERROR : NO-USER\r\n"

                logging.debug(f"Remote {req_addr} wants to know who is {src_port} connected to {dst_port}")

                for room in self.serv.find_rooms(NetworkRoom):
                    if not room.conn or not room.conn.connected:
                        continue

                    remote_addr, remote_port, *_ = room.conn.transport.get_extra_info("peername") or ("", "")
                    local_addr, local_port, *_ = room.conn.transport.get_extra_info("sockname") or ("", "")

                    if remote_addr == req_addr and remote_port == dst_port and local_port == src_port:
                        username = room.get_username()
                        if username is not None:
                            response = f"{src_port}, {dst_port} : USERID : UNIX : {username}\r\n"
                        break

                logging.debug(f"Responding with: {response}")
                writer.write(response.encode())
                await writer.drain()
        except Exception:
            logging.exception("Identd request failed.")
        finally:
            writer.close()

    async def start_listening(self, listen_address):
        self.server = await asyncio.start_server(self.handle, listen_address, 113)

    async def run(self, serv):
        self.serv = serv

        async with self.server:
            await self.server.serve_forever()
