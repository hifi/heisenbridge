import asyncio

from irc.client_aio import AioConnection
from irc.client_aio import AioReactor


class HeisenConnection(AioConnection):
    def __init__(self, reactor):
        super().__init__(reactor)
        self._queue = asyncio.Queue()
        self._task = asyncio.ensure_future(self._run())

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

                if penalty > 5:
                    await asyncio.sleep(1.5)

                super().send_raw(string)

                # this needs to be reset if we slept
                last = loop.time()
            except asyncio.CancelledError:
                return
            finally:
                self._queue.task_done()

    def send_raw(self, string):
        self._queue.put_nowait(string)


class HeisenReactor(AioReactor):
    connection_class = HeisenConnection
