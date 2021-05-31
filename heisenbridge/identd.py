import asyncio
import ipaddress
import logging
import re
import socket

from heisenbridge.network_room import NetworkRoom


class Identd:
    async def handle(self, reader, writer):
        try:
            data = await reader.read(128)
            query = data.decode()

            m = re.match(r"^(\d+)\s*,\s*(\d+)", query)
            if m:
                _req_addr, req_port, *_ = writer.get_extra_info("peername")
                req_addr = ipaddress.ip_address(_req_addr)

                if isinstance(req_addr, ipaddress.IPv4Address):
                    req_addr = ipaddress.ip_address("::ffff:" + _req_addr)

                src_port = int(m.group(1))
                dst_port = int(m.group(2))

                response = f"{src_port}, {dst_port} : ERROR : NO-USER\r\n"

                logging.debug(f"Remote {req_addr} wants to know who is {src_port} connected to {dst_port}")

                for room in self.serv.find_rooms(NetworkRoom):
                    if not room.conn or not room.conn.connected:
                        continue

                    _remote_addr, remote_port, *_ = room.conn.transport.get_extra_info("peername") or ("", "")
                    local_addr, local_port, *_ = room.conn.transport.get_extra_info("sockname") or ("", "")
                    remote_addr = ipaddress.ip_address(_remote_addr)

                    if isinstance(remote_addr, ipaddress.IPv4Address):
                        remote_addr = ipaddress.ip_address("::ffff:" + _remote_addr)

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

    async def start_listening(self, serv, port):
        self.serv = serv

        # XXX: this only works if dual stack is enabled which usually is
        if socket.has_ipv6:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            sock.bind(("::", port))
            self.server = await asyncio.start_server(self.handle, sock=sock, loop=asyncio.get_event_loop())
        else:
            self.server = await asyncio.start_server(self.handle, "0.0.0.0", port)

    async def run(self):
        async with self.server:
            await self.server.serve_forever()
