import asyncio
import logging

from irc.client_aio import AioConnection
from irc.client_aio import AioReactor


class HeisenConnection(AioConnection):
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

            if len(matching_handlers) == 0 and event.type != "all_raw_messages":
                matching_handlers += self.handlers.get("unhandled_events", [])

            for handler in matching_handlers:
                result = handler.callback(connection, event)
                if result == "NO MORE":
                    return
