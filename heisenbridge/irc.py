import asyncio
import base64
import collections
import logging

from irc.client import ServerConnectionError
from irc.client_aio import AioConnection
from irc.client_aio import AioReactor
from irc.client_aio import IrcProtocol
from irc.connection import AioFactory


class MultiQueue:
    def __init__(self):
        self._prios = []
        self._ques = {}

    def __len__(self):
        return sum([len(q) for q in self._ques.values()])

    def append(self, item):
        prio, value, tag = item

        if prio not in self._prios:
            self._prios.append(prio)
            self._prios.sort()
            self._ques[prio] = collections.deque()

        self._ques[prio].append(item)

    def get(self):
        for prio in self._prios:
            que = self._ques[prio]
            if len(que) > 0:
                return que.popleft()

        raise IndexError("Get called when all queues empty")

    def filter(self, func) -> int:
        filtered = 0

        for que in self._ques.values():
            tmp = que.copy()
            olen = len(que)
            que.clear()
            que.extend(filter(func, tmp))
            filtered += olen - len(que)

        return filtered


# asyncio.PriorityQueue does not preserve order within priority level
class OrderedPriorityQueue(asyncio.Queue):
    def _init(self, maxsize):
        self._queue = MultiQueue()

    def _get(self):
        return self._queue.get()

    def _put(self, item):
        self._queue.append(item)

    def remove_tag(self, tag) -> int:
        return self._queue.filter(lambda x: x == tag)


class HeisenProtocol(IrcProtocol):
    ping_timeout = 300

    def connection_made(self, *args, **kwargs):
        super().connection_made(*args, **kwargs)

        # start aliveness check
        self._timer = self.loop.call_later(60, self._are_we_still_alive)
        self._last_data = self.loop.time()

    def connection_lost(self, exc):
        super().connection_lost(exc)
        self._timer.cancel()

    def data_received(self, *args, **kwargs):
        super().data_received(*args, **kwargs)
        self._last_data = self.loop.time()

    def _are_we_still_alive(self):
        if not self.connection or not hasattr(self.connection, "connected") or not self.connection.connected:
            return

        # no
        if self.loop.time() - self._last_data >= self.ping_timeout:
            logging.debug("Disconnecting due to no data received from server.")
            self.connection.disconnect("No data received.")
            return

        # re-schedule aliveness check
        self._timer = self.loop.call_later(self.ping_timeout / 3, self._are_we_still_alive)

        # yes
        if self.loop.time() - self._last_data < self.ping_timeout / 3:
            return

        # perhaps, ask the server
        logging.debug("Aliveness check failed, sending PING")
        self.connection.send_items("PING", self.connection.real_server_name)


class HeisenConnection(AioConnection):
    protocol_class = HeisenProtocol

    def __init__(self, reactor):
        super().__init__(reactor)
        self._queue = OrderedPriorityQueue()

    async def expect(self, events, timeout=30):
        events = events if not isinstance(events, str) and not isinstance(events, int) else [events]
        waitable = asyncio.Event()
        result = None

        def expected(connection, event):
            nonlocal result, waitable
            result = (connection, event)
            waitable.set()
            return "NO MORE"

        for event in events:
            self.add_global_handler(event, expected, -100)

        try:
            await asyncio.wait_for(waitable.wait(), timeout)
            return result
        finally:
            for event in events:
                self.remove_global_handler(event, expected)

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
        self.real_nickname = ""
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
        self._task = asyncio.ensure_future(self._run())
        self.reactor._on_connect(self.protocol, self.transport)
        return self

    async def register(self):
        # SASL stuff
        if self.sasl_username is not None and self.sasl_password is not None:
            self.cap("REQ", "sasl")

            try:
                (connection, event) = await self.expect("cap")
                if not event.arguments or event.arguments[0] != "ACK":
                    raise ServerConnectionError("SASL requested but not supported by server.")

                self.send_items("AUTHENTICATE PLAIN")

                (connection, event) = await self.expect("authenticate")
                if event.target != "+":
                    raise ServerConnectionError("SASL AUTHENTICATE was rejected.")

                sasl = f"{self.sasl_username}\0{self.sasl_username}\0{self.sasl_password}"
                self.send_items("AUTHENTICATE", base64.b64encode(sasl.encode("utf8")).decode("utf8"))
                (connection, event) = await self.expect(["903", "904", "908"])
                if event.type != "903":
                    raise ServerConnectionError(event.arguments[0])

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
                (priority, string, tag) = await self._queue.get()

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

    def send_raw(self, string, priority=0, tag=None):
        self._queue.put_nowait((priority, string, tag))

    def send_items(self, *items):
        priority = 0
        tag = None
        if items[0] == "NOTICE":
            # queue CTCP replies even lower than notices
            if len(items) > 2 and len(items[2]) > 1 and items[2][1] == "\001":
                priority = 3
            else:
                priority = 2
        if items[0] == "PRIVMSG":
            priority = 1
        elif items[0] == "PONG":
            priority = -1

        # tag with target to dequeue with filter
        if tag is None and items[0] in ["NOTICE", "PRIVMSG", "MODE", "JOIN", "PART", "KICK"]:
            tag = items[1].lower()

        self.send_raw(" ".join(filter(None, items)), priority, tag)

    def remove_tag(self, tag) -> int:
        return self._queue.remove_tag(tag)


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
