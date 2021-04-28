import asyncio
import logging

"""
Buffering event queue with merging of events.
"""


class EventQueue:
    def __init__(self, callback):
        self._callback = callback
        self._events = []
        self._loop = asyncio.get_event_loop()
        self._timer = None
        self._start = 0
        self._chain = asyncio.Queue()
        self._task = asyncio.ensure_future(self._run())
        self._timeout = 30

    def __del__(self):
        self._task.cancel()

    async def _run(self):
        while True:
            try:
                task = await self._chain.get()
            except asyncio.CancelledError:
                return

            try:
                await asyncio.wait_for(task, timeout=self._timeout)
            except asyncio.TimeoutError:
                logging.warning("EventQueue task timed out.")
            finally:
                self._chain.task_done()

    def _flush(self):
        events = self._events

        self._timer = None
        self._events = []

        self._chain.put_nowait(self._callback(events))

    def enqueue(self, event):
        now = self._loop.time()

        # stamp start time when we queue first event, always append event
        if len(self._events) == 0:
            self._start = now
            self._events.append(event)
        else:
            # lets see if we can merge the event
            prev = self._events[-1]

            prev_formatted = "format" in prev["content"]
            cur_formatted = "format" in event["content"]

            if (
                prev["type"] == event["type"]
                and prev["user_id"] == event["user_id"]
                and prev["content"]["msgtype"] == event["content"]["msgtype"]
                and prev_formatted == cur_formatted
            ):
                prev["content"]["body"] += "\n" + event["content"]["body"]
                if cur_formatted:
                    prev["content"]["formatted_body"] += "<br>" + event["content"]["formatted_body"]
            else:
                # can't merge, force flush
                self._start = 0
                self._events.append(event)

        # always cancel timer when we enqueue
        if self._timer and not self._timer.cancelled():
            self._timer.cancel()

        # if we have bumped ourself for a full second, flush now
        if now >= self._start + 1.0:
            self._flush()
        else:
            self._timer = self._loop.call_later(0.1, self._flush)
