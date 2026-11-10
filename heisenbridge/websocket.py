import asyncio
import json
import logging

import aiohttp
from mautrix.types.event import Event


class AppserviceWebsocket:
    def __init__(self, url, token, callback):
        self.url = url + "/_matrix/client/unstable/fi.mau.as_sync"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "X-Mautrix-Websocket-Version": "3",
        }
        self.callback = callback

    async def start(self):
        asyncio.create_task(self._loop())

    async def handle_message(self, ws, data):
        # Not every message carries "status" (only transactions do), so a plain
        # data["status"] lookup raised KeyError and dropped the connection.
        if data.get("status") == "ok" and data.get("command") == "transaction":
            logging.debug(f"Websocket transaction {data['txn_id']}")
            for event in data["events"]:
                try:
                    await self.callback(Event.deserialize(event))
                except Exception as e:
                    logging.error(e)

            await ws.send_str(
                json.dumps(
                    {
                        "command": "response",
                        "id": data["id"],
                        "data": {},
                    }
                )
            )
        else:
            # Only the command and id: requests such as http_proxy carry the
            # caller's Authorization header in their data.
            logging.warning("Unhandled WS command: %s (id %s)", data.get("command"), data.get("id"))

    async def _loop(self):
        while True:
            try:
                logging.info(f"Connecting to {self.url}...")

                async with aiohttp.ClientSession(headers=self.headers) as sess:
                    async with sess.ws_connect(self.url) as ws:
                        logging.info("Websocket connected.")

                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                logging.debug("Unhandled WS message: %s", msg)
                                continue

                            await self.handle_message(ws, msg.json())

                logging.info("Websocket disconnected.")
            except asyncio.CancelledError:
                logging.info("Websocket was cancelled.")
                return
            except Exception as e:
                logging.error(e)

                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    return
