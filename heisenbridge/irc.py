import asyncio
import logging

from irc.client_aio import AioConnection
from irc.client_aio import AioReactor
from irc.client_aio import IrcProtocol


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
