import asyncio
import ipaddress
import logging
import re
import socket

from heisenbridge.network_room import NetworkRoom


class Identd:
    async def handle(self, reader, writer):
        try:
            data = await asyncio.wait_for(reader.readuntil(b"\r\n"), 10)
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

                """
                This is a hack to workaround the issue where asyncio create_connection has not returned before
                identd is already requested.

                Proper fix would be to use our own sock that has been pre-bound but that's quite a bit of work
                for very little gain.
                """
                await asyncio.sleep(0.1)

                for room in self.serv.find_rooms(NetworkRoom):
                    if not room.conn or not room.conn.connected:
                        continue

                    _remote_addr, remote_port, *_ = room.conn.transport.get_extra_info("peername") or ("", "")
                    local_addr, local_port, *_ = room.conn.transport.get_extra_info("sockname") or ("", "")
                    remote_addr = ipaddress.ip_address(_remote_addr)

                    if isinstance(remote_addr, ipaddress.IPv4Address):
                        remote_addr = ipaddress.ip_address("::ffff:" + _remote_addr)

                    if remote_addr == req_addr and remote_port == dst_port and local_port == src_port:
                        response = f"{src_port}, {dst_port} : USERID : UNIX : {room.get_ident()}\r\n"
                        break

                logging.debug(f"Responding with: {response}")
                writer.write(response.encode())
                await writer.drain()
        except Exception:
            logging.debug("Identd request threw exception, ignored")
        finally:
            writer.close()

    async def start_listening(self, serv, port):
        self.serv = serv

        # XXX: this only works if dual stack is enabled which usually is
        if socket.has_ipv6:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            sock.bind(("::", port))
            self.server = await asyncio.start_server(self.handle, sock=sock, loop=asyncio.get_event_loop(), limit=128)
        else:
            self.server = await asyncio.start_server(self.handle, "0.0.0.0", port, limit=128)
