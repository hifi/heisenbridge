import asyncio
import json

from heisenbridge.websocket import AppserviceWebsocket


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_str(self, data):
        self.sent.append(json.loads(data))


def make():
    events = []

    async def callback(event):
        events.append(event)

    return AppserviceWebsocket("wss://example.invalid", "token", callback), events


def test_message_without_status_is_ignored(caplog):
    ws, events = make()
    fake = FakeWS()
    request = {"command": "http_proxy", "id": 1, "data": {"headers": {"Authorization": ["Bearer secret"]}}}
    asyncio.run(ws.handle_message(fake, request))
    assert fake.sent == []
    assert events == []
    assert "http_proxy" in caplog.text
    assert "secret" not in caplog.text


def test_transaction_is_dispatched_and_acknowledged():
    ws, events = make()
    fake = FakeWS()
    event = {
        "type": "m.room.message",
        "room_id": "!room:example.invalid",
        "event_id": "$event",
        "sender": "@user:example.invalid",
        "origin_server_ts": 0,
        "content": {"msgtype": "m.text", "body": "hi"},
    }
    asyncio.run(
        ws.handle_message(fake, {"status": "ok", "command": "transaction", "txn_id": "1", "id": 7, "events": [event]})
    )
    assert len(events) == 1
    assert fake.sent == [{"command": "response", "id": 7, "data": {}}]
