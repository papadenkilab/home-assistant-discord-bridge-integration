"""Unit tests for DiscordBridgeClient against a stub bridge server.

The stub mimics the real add-on's semantics:
- GET /pending returns everything until acked (reading does not delete)
- POST /ack marks ids done
- POST /send dedupes on dedupe_id
- all of the above require the X-Queue-Token header
"""

import importlib.util
import pathlib
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "discord_bridge"
    / "bridge_client.py"
)
spec = importlib.util.spec_from_file_location("bridge_client", MODULE_PATH)
bridge_client = importlib.util.module_from_spec(spec)
sys.modules["bridge_client"] = bridge_client
spec.loader.exec_module(bridge_client)

DiscordBridgeClient = bridge_client.DiscordBridgeClient
BridgeError = bridge_client.BridgeError

TOKEN = "test-token"
MESSAGES = [
    {"id": 1, "discord_message_id": "d1", "channel_id": "c1",
     "author_id": "111", "content": "mine one", "created_at": "t1"},
    {"id": 2, "discord_message_id": "d2", "channel_id": "c1",
     "author_id": "222", "content": "family one", "created_at": "t2"},
    {"id": 3, "discord_message_id": "d3", "channel_id": "c2",
     "author_id": "111", "content": "mine two", "created_at": "t3"},
]


class StubBridge:
    def __init__(self):
        self.messages = [dict(m) for m in MESSAGES]
        self.done = set()
        self.sent = {}
        self.app = web.Application()
        self.app.router.add_get("/health", self.health)
        self.app.router.add_get("/pending", self.pending)
        self.app.router.add_post("/ack", self.ack)
        self.app.router.add_post("/send", self.send)

    def _check(self, request):
        if request.headers.get("X-Queue-Token") != TOKEN:
            raise web.HTTPUnauthorized(text='{"error": "unauthorized"}')

    async def health(self, request):
        return web.json_response({"status": "ok", "discord_connected": True})

    async def pending(self, request):
        self._check(request)
        items = [m for m in self.messages if m["id"] not in self.done]
        return web.json_response({"messages": items})

    async def ack(self, request):
        self._check(request)
        body = await request.json()
        count = 0
        for queue_id in body.get("ids", []):
            if queue_id not in self.done and any(
                m["id"] == queue_id for m in self.messages
            ):
                self.done.add(queue_id)
                count += 1
        return web.json_response({"acked": count})

    async def send(self, request):
        self._check(request)
        body = await request.json()
        if not all(body.get(k) for k in ("channel_id", "content", "dedupe_id")):
            return web.json_response(
                {"error": "channel_id, content and dedupe_id are required"},
                status=400,
            )
        if body["dedupe_id"] in self.sent:
            return web.json_response(
                {"sent_message_id": self.sent[body["dedupe_id"]],
                 "status": "sent", "deduped": True}
            )
        message_id = f"s{len(self.sent) + 1}"
        self.sent[body["dedupe_id"]] = message_id
        return web.json_response({"sent_message_id": message_id, "status": "sent"})


@pytest.fixture()
async def stub():
    bridge = StubBridge()
    async with TestClient(TestServer(bridge.app)) as client:
        yield bridge, client.session, str(client.make_url("/")).rstrip("/")


@pytest.mark.asyncio
async def test_pending_filters_to_configured_author(stub):
    bridge, session, base_url = stub
    client = DiscordBridgeClient(session, base_url, TOKEN, author_id="111")
    messages = await client.pending()
    assert [m["id"] for m in messages] == [1, 3]
    # Reading must not consume anything on the bridge.
    assert bridge.done == set()


@pytest.mark.asyncio
async def test_pending_unfiltered_sees_everything(stub):
    _, session, base_url = stub
    client = DiscordBridgeClient(session, base_url, TOKEN)
    messages = await client.pending()
    assert [m["id"] for m in messages] == [1, 2, 3]


@pytest.mark.asyncio
async def test_ack_refuses_other_authors_ids(stub):
    bridge, session, base_url = stub
    client = DiscordBridgeClient(session, base_url, TOKEN, author_id="111")
    result = await client.ack([1, 2, 999])
    assert result == {"acked": 1, "skipped_ids": [2, 999]}
    # The family member's message must still be pending afterwards.
    remaining = await client.pending_raw()
    assert [m["id"] for m in remaining] == [2, 3]
    assert bridge.done == {1}


@pytest.mark.asyncio
async def test_wrong_token_is_invalid_auth(stub):
    _, session, base_url = stub
    client = DiscordBridgeClient(session, base_url, "wrong", author_id="111")
    with pytest.raises(BridgeError) as excinfo:
        await client.pending()
    assert excinfo.value.status == 401


@pytest.mark.asyncio
async def test_send_passes_through_and_dedupes(stub):
    _, session, base_url = stub
    client = DiscordBridgeClient(session, base_url, TOKEN, author_id="111")
    first = await client.send("c1", "hello", dedupe_id="reply-d1")
    second = await client.send("c1", "hello", dedupe_id="reply-d1")
    assert first["sent_message_id"] == second["sent_message_id"]
    assert second.get("deduped") is True


@pytest.mark.asyncio
async def test_send_requires_dedupe_id(stub):
    _, session, base_url = stub
    client = DiscordBridgeClient(session, base_url, TOKEN)
    with pytest.raises(BridgeError):
        await client.send("c1", "hello", dedupe_id="")
