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
        self._task = None
        self._timeout = 3600

    def start(self):
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run(self):
        while True:
            try:
                task = await self._chain.get()
            except asyncio.CancelledError:
                logging.debug("EventQueue was cancelled.")
                return

            try:
                await asyncio.wait_for(task, timeout=self._timeout)
            except asyncio.CancelledError:
                logging.debug("EventQueue task was cancelled.")
                return
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

        # always cancel timer when we enqueue
        if self._timer:
            self._timer.cancel()

        # stamp start time when we queue first event, always append event
        if len(self._events) == 0:
            self._start = now
            self._events.append(event)
        else:
            # lets see if we can merge the event
            prev = self._events[-1]

            prev_formatted = "format" in prev["content"]
            cur_formatted = "format" in event["content"]

            # calculate content length if we need to flush anyway to stay within max event size
            prev_len = 0
            if "content" in prev:
                if "body" in prev["content"]:
                    prev_len += len(prev["content"]["body"])
                if "formatted_body" in prev["content"]:
                    prev_len += len(prev["content"]["formatted_body"])

            if (
                prev["type"] == event["type"]
                and prev["type"][0] != "_"
                and prev["user_id"] == event["user_id"]
                and "msgtype" in prev["content"]
                and prev["content"]["msgtype"] == event["content"]["msgtype"]
                and prev_formatted == cur_formatted
                and prev_len < 64_000  # a single IRC event can't overflow with this
            ):
                prev["content"]["body"] += "\n" + event["content"]["body"]
                if cur_formatted:
                    prev["content"]["formatted_body"] += "<br>" + event["content"]["formatted_body"]
            else:
                # can't merge, force flush but enqueue the next event
                self._flush()
                self._start = now
                self._events.append(event)

        # if we have bumped ourself for a full second, flush now
        if now >= self._start + 1.0:
            self._flush()
        else:
            self._timer = self._loop.call_later(0.1, self._flush)
