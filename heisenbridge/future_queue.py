import logging
from asyncio import CancelledError
from asyncio import ensure_future
from asyncio import Event
from asyncio import Queue
from asyncio import TimeoutError
from asyncio import wait_for

"""
Ordered Future execution queue. Do not ever recurse or it will deadlock.
"""


class FutureQueue:
    _queue: Queue

    def __init__(self, timeout=None):
        self._queue = Queue()
        self._timeout = timeout
        self._task = ensure_future(self._run())

    def __del__(self):
        self._task.cancel()

    async def _run(self):
        while True:
            try:
                (start, completed) = await self._queue.get()
            except CancelledError:
                return

            # allow execution
            start.set()

            # wait for completion
            await completed.wait()

            self._queue.task_done()

    async def schedule(self, obj):
        start = Event()
        completed = Event()

        # push execution request to queue
        self._queue.put_nowait((start, completed))

        # wait until we are dequeued
        await start.wait()

        # run our job
        try:
            ret = await wait_for(obj, timeout=self._timeout)
        except TimeoutError:
            logging.warning("FutureQueue task timed out and will be cancelled.")
            raise CancelledError("FutureQueue task was cancelled because it timed out")
        finally:
            completed.set()

        return ret
