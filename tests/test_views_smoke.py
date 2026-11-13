"""Smoke test for the Home Assistant views with stubbed HA modules.

Real Home Assistant is not installed here (it needs a newer Python), so
this test stubs the small HA surface the integration imports and then
drives the real view code against the stub bridge from
test_bridge_client: an admin gets only their own messages, a non-admin
is rejected, and ack still refuses other authors' ids.
"""

import json
import pathlib
import sys
import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Import the stub bridge app first (its module loads bridge_client).
from test_bridge_client import StubBridge, TOKEN  # noqa: E402


def _install_ha_stubs() -> None:
    ha = types.ModuleType("homeassistant")
    ha.__path__ = []

    config_entries = types.ModuleType("homeassistant.config_entries")

    class ConfigEntry:  # noqa: D401 - stub
        pass

    class ConfigFlow:
        def __init_subclass__(cls, domain=None, **kwargs):
            super().__init_subclass__()

    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigFlow = ConfigFlow
    config_entries.ConfigFlowResult = dict

    core = types.ModuleType("homeassistant.core")

    class HomeAssistant:
        pass

    core.HomeAssistant = HomeAssistant

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []

    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: hass.session

    http = types.ModuleType("homeassistant.helpers.http")

    class HomeAssistantView:
        requires_auth = True
        url = None
        name = None

        def json(self, result, status_code=200):
            return web.json_response(result, status=status_code)

    http.HomeAssistantView = HomeAssistantView

    storage = types.ModuleType("homeassistant.helpers.storage")

    class Store:
        def __init__(self, hass, version, key):
            self._data = None

        async def async_load(self):
            return self._data

        async def async_save(self, data):
            self._data = data

    storage.Store = Store

    ha.config_entries = config_entries
    ha.core = core
    sys.modules.update(
        {
            "homeassistant": ha,
            "homeassistant.config_entries": config_entries,
            "homeassistant.core": core,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.aiohttp_client": aiohttp_client,
            "homeassistant.helpers.http": http,
            "homeassistant.helpers.storage": storage,
        }
    )


_install_ha_stubs()

from custom_components.discord_bridge import (  # noqa: E402
    AckView,
    PendingView,
    DOMAIN,
)
from custom_components.discord_bridge.bridge_client import (  # noqa: E402
    DiscordBridgeClient,
)


class FakeUser:
    def __init__(self, is_admin):
        self.is_admin = is_admin


class FakeRequest:
    def __init__(self, hass, user, body=None, form=None, content_type=None,
                 query=None):
        self.app = {"hass": hass}
        self.query = query or {}
        self._user = user
        self._body = body
        self._form = form
        self.content_type = content_type

    async def post(self):
        return self._form

    def get(self, key, default=None):
        if key == "hass_user":
            return self._user
        return default

    async def json(self):
        return self._body


class FakeHass:
    def __init__(self, client):
        self.data = {DOMAIN: {"bridge_client": client, "require_admin": True}}
        self.session = None


@pytest.mark.asyncio
async def test_pending_view_filters_and_admin_gate():
    bridge = StubBridge()
    async with TestClient(TestServer(bridge.app)) as http_client:
        base_url = str(http_client.make_url("/")).rstrip("/")
        client = DiscordBridgeClient(
            http_client.session, base_url, TOKEN, author_id="111"
        )
        hass = FakeHass(client)

        admin = FakeUser(is_admin=True)
        response = await PendingView().get(FakeRequest(hass, admin))
        payload = json.loads(response.text)
        assert response.status == 200
        assert [m["id"] for m in payload["messages"]] == [1, 3]
        assert payload["count"] == 2

        non_admin = FakeUser(is_admin=False)
        response = await PendingView().get(FakeRequest(hass, non_admin))
        assert response.status == 403


@pytest.mark.asyncio
async def test_ack_view_refuses_family_ids():
    bridge = StubBridge()
    async with TestClient(TestServer(bridge.app)) as http_client:
        base_url = str(http_client.make_url("/")).rstrip("/")
        client = DiscordBridgeClient(
            http_client.session, base_url, TOKEN, author_id="111"
        )
        hass = FakeHass(client)
        request = FakeRequest(hass, FakeUser(is_admin=True), body={"ids": [2, 3]})
        response = await AckView().post(request)
        payload = json.loads(response.text)
        assert payload == {"acked": 1, "skipped_ids": [2]}
        assert bridge.done == {3}


# ---------------------------------------------------------------- direct

from test_discord_client import StubDiscord, CHANNEL, FORUM, GUILD  # noqa: E402
from custom_components.discord_bridge import (  # noqa: E402
    DirectChannelView,
    DirectChannelsView,
    DirectEventsView,
    DirectForumPostsView,
    DirectForumView,
    DirectMarkReadView,
    DirectNewView,
    DirectPollExpireView,
    DirectPollView,
    DirectPollVotersView,
)
from custom_components.discord_bridge.discord_client import (  # noqa: E402
    DiscordClient,
)


class FakeStore:
    def __init__(self):
        self.saved = None

    async def async_save(self, data):
        self.saved = dict(data)


class FakeDirectHass:
    def __init__(self, client):
        self.store = FakeStore()
        self.data = {
            DOMAIN: {
                "discord_client": client,
                "require_admin": True,
                "guild_id": GUILD,
                "channel_ids": [CHANNEL],
                "cursors": {},
                "store": self.store,
            }
        }
        self.session = None


@pytest.mark.asyncio
async def test_direct_new_filters_author_and_mark_read_advances():
    server = StubDiscord()
    async with TestClient(TestServer(server.app)) as http_client:
        base = str(http_client.make_url("/api/v10"))
        client = DiscordClient(
            http_client.session, "good-token", author_id="111", api_base=base
        )
        hass = FakeDirectHass(client)
        admin = FakeUser(is_admin=True)

        response = await DirectNewView().get(FakeRequest(hass, admin))
        payload = json.loads(response.text)
        assert [m["id"] for m in payload["messages"]] == ["100", "102", "103"]
        assert payload["latest"] == {CHANNEL: "103"}

        mark = FakeRequest(
            hass, admin, body={"channel_id": CHANNEL, "message_id": "102"}
        )
        response = await DirectMarkReadView().post(mark)
        assert json.loads(response.text)["cursor"] == "102"
        assert hass.store.saved == {CHANNEL: "102"}

        response = await DirectNewView().get(FakeRequest(hass, admin))
        payload = json.loads(response.text)
        assert [m["id"] for m in payload["messages"]] == ["103"]


@pytest.mark.asyncio
async def test_direct_events_create_and_list():
    server = StubDiscord()
    async with TestClient(TestServer(server.app)) as http_client:
        base = str(http_client.make_url("/api/v10"))
        client = DiscordClient(
            http_client.session, "good-token", author_id="111", api_base=base
        )
        hass = FakeDirectHass(client)
        admin = FakeUser(is_admin=True)
        body = {
            "name": "Family trip",
            "scheduled_start_time": "2026-12-20T09:00:00+09:00",
            "scheduled_end_time": "2026-12-22T18:00:00+09:00",
            "location": "Lake House",
        }
        response = await DirectEventsView().post(
            FakeRequest(hass, admin, body=body)
        )
        assert response.status == 200
        assert json.loads(response.text)["name"] == "Family trip"

        response = await DirectEventsView().get(FakeRequest(hass, admin))
        payload = json.loads(response.text)
        assert payload["count"] == 1


@pytest.mark.asyncio
async def test_direct_forum_post_resolves_tag_names_and_lists():
    server = StubDiscord()
    async with TestClient(TestServer(server.app)) as http_client:
        base = str(http_client.make_url("/api/v10"))
        client = DiscordClient(
            http_client.session, "good-token", author_id="111", api_base=base
        )
        hass = FakeDirectHass(client)
        admin = FakeUser(is_admin=True)

        response = await DirectForumView().get(
            FakeRequest(hass, admin, query={"channel_id": FORUM})
        )
        payload = json.loads(response.text)
        assert payload["type"] == 15
        assert [t["name"] for t in payload["available_tags"]][0] == "画像"

        body = {
            "channel_id": FORUM,
            "title": "新しい画像",
            "content": "作りました",
            "tags": ["画像"],
        }
        response = await DirectForumPostsView().post(
            FakeRequest(hass, admin, body=body)
        )
        assert response.status == 200
        payload = json.loads(response.text)
        assert payload["applied_tags"] == ["t1"]

        response = await DirectForumPostsView().get(
            FakeRequest(
                hass, admin,
                query={"channel_id": FORUM, "guild_id": GUILD},
            )
        )
        payload = json.loads(response.text)
        assert payload["count"] == 1
        assert payload["posts"][0]["name"] == "新しい画像"

        body["tags"] = ["無いタグ"]
        response = await DirectForumPostsView().post(
            FakeRequest(hass, admin, body=body)
        )
        assert response.status == 400

        body = {"channel_id": CHANNEL, "title": "x", "content": "y"}
        response = await DirectForumPostsView().post(
            FakeRequest(hass, admin, body=body)
        )
        assert response.status == 400
        assert "not a forum" in json.loads(response.text)["error"]


@pytest.mark.asyncio
async def test_direct_forum_post_with_file_via_multipart():
    import io

    server = StubDiscord()
    async with TestClient(TestServer(server.app)) as http_client:
        base = str(http_client.make_url("/api/v10"))
        client = DiscordClient(
            http_client.session, "good-token", author_id="111", api_base=base
        )
        hass = FakeDirectHass(client)
        admin = FakeUser(is_admin=True)

        class FakeUpload:
            filename = "art.png"
            content_type = "image/png"

            def __init__(self):
                self.file = io.BytesIO(b"png-bytes")

        form = {
            "channel_id": FORUM,
            "title": "ファイル付き投稿",
            "content": "添付あり",
            "tags": "画像",
            "file": FakeUpload(),
        }
        request = FakeRequest(
            hass, admin, form=form, content_type="multipart/form-data"
        )
        response = await DirectForumPostsView().post(request)
        assert response.status == 200
        payload = json.loads(response.text)
        assert payload["applied_tags"] == ["t1"]
        assert server.last_forum_upload["data"] == b"png-bytes"


@pytest.mark.asyncio
async def test_direct_create_and_edit_channel_views():
    server = StubDiscord()
    async with TestClient(TestServer(server.app)) as http_client:
        base = str(http_client.make_url("/api/v10"))
        client = DiscordClient(
            http_client.session, "good-token", author_id="111", api_base=base
        )
        hass = FakeDirectHass(client)
        admin = FakeUser(is_admin=True)

        body = {
            "name": "ai-created",
            "type": 15,
            "topic": "works",
            "available_tags": ["画像", "アプリ"],
        }
        response = await DirectChannelsView().post(
            FakeRequest(hass, admin, body=body)
        )
        assert response.status == 200
        created = json.loads(response.text)
        assert created["type"] == 15
        assert [t["name"] for t in created["available_tags"]] == [
            "画像", "アプリ",
        ]

        rename = {"channel_id": CHANNEL, "name": "ai-created-archive"}
        response = await DirectChannelView().post(
            FakeRequest(hass, admin, body=rename)
        )
        assert response.status == 200
        assert json.loads(response.text)["name"] == "ai-created-archive"

        retag = {"channel_id": created["id"], "tags": ["画像", "動画"]}
        response = await DirectChannelView().patch(
            FakeRequest(hass, admin, body=retag)
        )
        assert response.status == 200
        payload = json.loads(response.text)
        names = [t["name"] for t in payload["available_tags"]]
        assert names == ["画像", "動画"]
        kept = [t for t in payload["available_tags"] if t["name"] == "画像"]
        assert kept[0]["id"] == created["available_tags"][0]["id"]


@pytest.mark.asyncio
async def test_direct_edit_channel_view_sets_thread_tags():
    server = StubDiscord()
    async with TestClient(TestServer(server.app)) as http_client:
        base = str(http_client.make_url("/api/v10"))
        client = DiscordClient(
            http_client.session, "good-token", author_id="111", api_base=base
        )
        hass = FakeDirectHass(client)
        admin = FakeUser(is_admin=True)

        body = {
            "channel_id": FORUM,
            "title": "作品",
            "content": "本文",
            "tags": ["画像"],
        }
        response = await DirectForumPostsView().post(
            FakeRequest(hass, admin, body=body)
        )
        assert response.status == 200
        post = json.loads(response.text)
        assert post["applied_tags"] == ["t1"]

        retag = {
            "channel_id": post["id"],
            "applied_tags": ["アプリ", "画像"],
        }
        response = await DirectChannelView().patch(
            FakeRequest(hass, admin, body=retag)
        )
        assert response.status == 200
        payload = json.loads(response.text)
        assert payload["applied_tags"] == ["t3", "t1"]

        bad = {"channel_id": post["id"], "applied_tags": ["無いタグ"]}
        response = await DirectChannelView().patch(
            FakeRequest(hass, admin, body=bad)
        )
        assert response.status == 400


@pytest.mark.asyncio
async def test_direct_poll_lifecycle_views():
    server = StubDiscord()
    async with TestClient(TestServer(server.app)) as http_client:
        base = str(http_client.make_url("/api/v10"))
        client = DiscordClient(
            http_client.session, "good-token", author_id="111", api_base=base
        )
        hass = FakeDirectHass(client)
        admin = FakeUser(is_admin=True)

        body = {
            "channel_id": CHANNEL,
            "question": "どちらがいい？",
            "answers": ["A案", "B案"],
            "duration_hours": 12,
        }
        response = await DirectPollView().post(
            FakeRequest(hass, admin, body=body)
        )
        assert response.status == 200
        created = json.loads(response.text)
        message_id = created["id"]
        assert created["poll"]["question"] == "どちらがいい？"

        query = {"channel_id": CHANNEL, "message_id": message_id}
        response = await DirectPollView().get(
            FakeRequest(hass, admin, query=query)
        )
        payload = json.loads(response.text)
        assert payload["poll"]["results"]["total_votes"] == 0

        voters_query = dict(query, answer_id="1")
        response = await DirectPollVotersView().get(
            FakeRequest(hass, admin, query=voters_query)
        )
        payload = json.loads(response.text)
        assert payload["count"] == 2

        response = await DirectPollExpireView().post(
            FakeRequest(hass, admin, body=query)
        )
        assert response.status == 200
        payload = json.loads(response.text)
        assert payload["poll"]["results"]["is_finalized"] is True
        assert payload["poll"]["results"]["total_votes"] == 2

        bad = {"channel_id": CHANNEL, "question": "x", "answers": "A,B"}
        response = await DirectPollView().post(
            FakeRequest(hass, admin, body=bad)
        )
        assert response.status == 400


@pytest.mark.asyncio
async def test_direct_send_file_via_multipart_form():
    import io

    server = StubDiscord()
    async with TestClient(TestServer(server.app)) as http_client:
        base = str(http_client.make_url("/api/v10"))
        client = DiscordClient(
            http_client.session, "good-token", author_id="111", api_base=base
        )
        hass = FakeDirectHass(client)
        admin = FakeUser(is_admin=True)

        class FakeUpload:
            filename = "photo.jpg"
            content_type = "image/jpeg"

            def __init__(self):
                self.file = io.BytesIO(b"jpeg-bytes")

        form = {"channel_id": CHANNEL, "content": "look", "file": FakeUpload()}
        request = FakeRequest(
            hass, admin, form=form, content_type="multipart/form-data"
        )
        from custom_components.discord_bridge import DirectSendView

        response = await DirectSendView().post(request)
        assert response.status == 200
        payload = json.loads(response.text)
        assert payload["attachments"][0]["filename"] == "photo.jpg"
        assert server.last_upload["data"] == b"jpeg-bytes"
