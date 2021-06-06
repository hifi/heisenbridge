import asyncio
import base64
import logging

from irc.client import ServerConnectionError
from irc.client_aio import AioConnection
from irc.client_aio import AioReactor
from irc.client_aio import IrcProtocol
from irc.connection import AioFactory


class HeisenProtocol(IrcProtocol):
    ping_timeout = 300

    def connection_made(self, transport):
        super().connection_made(transport)

        # start aliveness check
        self.loop.call_later(60, self._are_we_still_alive)
        self.last_data = self.loop.time()

    def data_received(self, data):
        super().data_received(data)
        self.last_data = self.loop.time()

    def _are_we_still_alive(self):
        # cancel if we don't have a connection
        if not self.connection or not hasattr(self.connection, "connected") or not self.connection.connected:
            logging.debug("Aliveness check has no connection, aborting.")
            return

        # no
        if self.loop.time() - self.last_data >= self.ping_timeout:
            logging.debug("Disconnecting due to no data received from server.")
            self.connection.disconnect("No data received.")
            return

        # re-schedule aliveness check
        self.loop.call_later(self.ping_timeout / 3, self._are_we_still_alive)

        # yes
        if self.loop.time() - self.last_data < self.ping_timeout / 3:
            return

        # perhaps, ask the server
        logging.debug("Aliveness check failed, sending PING")
        self.connection.send_raw("PING " + self.connection.real_server_name)


class HeisenConnection(AioConnection):
    protocol_class = HeisenProtocol

    def __init__(self, reactor):
        super().__init__(reactor)
        self._queue = asyncio.Queue()
        self._task = asyncio.ensure_future(self._run())

        self._cap_event = asyncio.Event()
        self._cap_sasl = False
        self._authenticate_event = asyncio.Event()
        self._authenticate_cont = False
        self._authreply_event = asyncio.Event()
        self._authreply_error = None

        self.add_global_handler("cap", self._on_cap)
        self.add_global_handler("authenticate", self._on_authenticate)
        self.add_global_handler("903", self._on_auth_ok)
        self.add_global_handler("904", self._on_auth_fail)
        self.add_global_handler("908", self._on_auth_fail)

    def _on_cap(self, connection, event):
        if event.arguments and event.arguments[0] == "ACK":
            self._cap_sasl = True

        self._cap_event.set()

    def _on_authenticate(self, connection, event):
        self._authenticate_cont = event.target == "+"
        self._authenticate_event.set()

    def _on_auth_ok(self, connection, event):
        self._authreply_event.set()

    def _on_auth_fail(self, connection, event):
        self._authreply_error = event.arguments[0]
        self._authreply_event.set()

    async def connect(
        self,
        server,
        port,
        nickname,
        password=None,
        username=None,
        ircname=None,
        connect_factory=AioFactory(),
        sasl_username=None,
        sasl_password=None,
    ):
        if self.connected:
            self.disconnect("Changing servers")

        self.buffer = self.buffer_class()
        self.handlers = {}
        self.real_server_name = ""
        self.real_nickname = nickname
        self.server = server
        self.port = port
        self.server_address = (server, port)
        self.nickname = nickname
        self.username = username or nickname
        self.ircname = ircname or nickname
        self.password = password
        self.sasl_username = sasl_username
        self.sasl_password = sasl_password
        self.connect_factory = connect_factory

        protocol_instance = self.protocol_class(self, self.reactor.loop)
        connection = self.connect_factory(protocol_instance, self.server_address)
        transport, protocol = await connection

        self.transport = transport
        self.protocol = protocol

        self.connected = True
        self.reactor._on_connect(self.protocol, self.transport)
        return self

    async def register(self):
        # SASL stuff
        if self.sasl_username is not None and self.sasl_password is not None:
            self.cap("REQ", "sasl")

            try:
                await asyncio.wait_for(self._cap_event.wait(), 30)

                if not self._cap_sasl:
                    raise ServerConnectionError("SASL requested but not supported.")

                self.send_raw("AUTHENTICATE PLAIN")
                await asyncio.wait_for(self._authenticate_event.wait(), 30)
                if not self._authenticate_cont:
                    raise ServerConnectionError("AUTHENTICATE was rejected.")

                sasl = f"{self.sasl_username}\0{self.sasl_username}\0{self.sasl_password}"
                self.send_raw("AUTHENTICATE " + base64.b64encode(sasl.encode("utf8")).decode("utf8"))
                await asyncio.wait_for(self._authreply_event.wait(), 30)

                if self._authreply_error is not None:
                    raise ServerConnectionError(self._authreply_error)
            except asyncio.TimeoutError:
                raise ServerConnectionError("SASL authentication timed out.")

            self.cap("END")

        # Log on...
        if self.password:
            self.pass_(self.password)
        self.nick(self.nickname)
        self.user(self.username, self.ircname)

    def close(self):
        logging.debug("Canceling IRC event queue")
        self._task.cancel()
        super().close()

    async def _run(self):
        loop = asyncio.get_event_loop()
        last = loop.time()
        penalty = 0

        while True:
            try:
                string = await self._queue.get()

                diff = int(loop.time() - last)

                # zero int diff means we are going too fast
                if diff == 0:
                    penalty += 1
                else:
                    penalty -= diff
                    if penalty < 0:
                        penalty = 0

                super().send_raw(string)

                # sleep is based on message length
                sleep_time = max(len(string.encode()) / 512 * 6, 1.5)

                if penalty > 5 or sleep_time > 1.5:
                    await asyncio.sleep(sleep_time)

                # this needs to be reset if we slept
                last = loop.time()
            except asyncio.CancelledError:
                break
            except Exception:
                logging.exception("Failed to flush IRC queue")

            self._queue.task_done()

        logging.debug("IRC event queue ended")

    def send_raw(self, string):
        self._queue.put_nowait(string)


class HeisenReactor(AioReactor):
    connection_class = HeisenConnection

    def _handle_event(self, connection, event):
        with self.mutex:
            matching_handlers = sorted(self.handlers.get("all_events", []) + self.handlers.get(event.type, []))

            if len(matching_handlers) == 0 and event.type != "all_raw_messages" and event.type != "pong":
                matching_handlers += self.handlers.get("unhandled_events", [])

            for handler in matching_handlers:
                result = handler.callback(connection, event)
                if result == "NO MORE":
                    return
