"""Unit tests for DiscordClient against a stub Discord API server."""

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
    / "discord_client.py"
)
spec = importlib.util.spec_from_file_location("discord_client", MODULE_PATH)
discord_client = importlib.util.module_from_spec(spec)
sys.modules["discord_client"] = discord_client
spec.loader.exec_module(discord_client)

DiscordClient = discord_client.DiscordClient
DiscordError = discord_client.DiscordError

TOKEN = "good-token"
GUILD = "900"
CHANNEL = "555"
FORUM = "557"
FORUM_TAGS = [
    {"id": "t1", "name": "画像", "emoji_name": "🖼️", "emoji_id": None,
     "moderated": False},
    {"id": "t2", "name": "文書・記事", "emoji_name": "📝", "emoji_id": None,
     "moderated": False},
    {"id": "t3", "name": "アプリ", "emoji_name": "📱", "emoji_id": None,
     "moderated": False},
]


def make_message(mid, author_id, content, attachments=None):
    return {
        "id": str(mid),
        "channel_id": CHANNEL,
        "guild_id": GUILD,
        "author": {"id": author_id, "username": f"user{author_id}",
                   "global_name": None, "bot": False},
        "content": content,
        "timestamp": "2026-11-13T00:00:00+00:00",
        "edited_timestamp": None,
        "attachments": attachments or [],
        "embeds": [],
        "mentions": [],
        "pinned": False,
        "referenced_message": None,
    }


RAW_MESSAGES = [
    make_message(100, "111", "first"),
    make_message(101, "222", "family note"),
    make_message(102, "111", "look at this", attachments=[{
        "id": "a1", "filename": "trip.png",
        "url": "https://cdn.discordapp.com/attachments/x/trip.png",
        "proxy_url": "https://media.discordapp.net/attachments/x/trip.png",
        "content_type": "image/png", "size": 12345,
        "width": 800, "height": 600,
    }]),
    make_message(103, "111", "and a movie", attachments=[{
        "id": "a2", "filename": "clip.mp4",
        "url": "https://cdn.discordapp.com/attachments/x/clip.mp4",
        "proxy_url": None, "content_type": "video/mp4", "size": 99999,
        "width": 1280, "height": 720,
    }]),
]


class StubDiscord:
    def __init__(self):
        self.messages = [dict(m) for m in RAW_MESSAGES]
        self.events = []
        self.threads = []
        self.channel_store = {
            CHANNEL: {"id": CHANNEL, "name": "chat", "type": 0,
                      "parent_id": None, "topic": None,
                      "available_tags": []},
            "556": {"id": "556", "name": "trips", "type": 0,
                    "parent_id": None, "topic": "travel",
                    "available_tags": []},
            FORUM: {"id": FORUM, "name": "ai-created", "type": 15,
                    "parent_id": None, "topic": "works",
                    "available_tags": [dict(t) for t in FORUM_TAGS]},
        }
        self.next_channel_id = 600
        self.next_tag_id = 100
        self.last_send_body = None
        self.last_forum_body = None
        self.last_forum_upload = None
        self.next_id = 200
        self.next_thread_id = 800
        app = web.Application()
        base = "/api/v10"
        app.router.add_get(base + "/users/@me", self.me)
        app.router.add_get(base + "/users/@me/guilds", self.guilds)
        app.router.add_get(base + "/guilds/{gid}/channels", self.channels)
        app.router.add_get(base + "/channels/{cid}/messages", self.get_messages)
        app.router.add_post(base + "/channels/{cid}/messages", self.post_message)
        app.router.add_get(
            base + "/channels/{cid}/messages/{mid}", self.get_one_message
        )
        app.router.add_get(
            base + "/channels/{cid}/polls/{mid}/answers/{aid}",
            self.poll_voters,
        )
        app.router.add_post(
            base + "/channels/{cid}/polls/{mid}/expire", self.poll_expire
        )
        app.router.add_get(base + "/guilds/{gid}/scheduled-events", self.list_events)
        app.router.add_post(base + "/guilds/{gid}/scheduled-events", self.create_event)
        app.router.add_get(base + "/channels/{cid}", self.get_channel)
        app.router.add_post(
            base + "/guilds/{gid}/channels", self.create_channel
        )
        app.router.add_patch(base + "/channels/{cid}", self.patch_channel)
        app.router.add_post(base + "/channels/{cid}/threads", self.create_thread)
        app.router.add_get(
            base + "/guilds/{gid}/threads/active", self.active_threads
        )
        app.router.add_get(
            base + "/channels/{cid}/threads/archived/public",
            self.archived_threads,
        )
        self.app = app

    def _check(self, request):
        if request.headers.get("Authorization") != f"Bot {TOKEN}":
            raise web.HTTPUnauthorized(
                text='{"message": "401: Unauthorized", "code": 0}',
                content_type="application/json",
            )

    async def me(self, request):
        self._check(request)
        return web.json_response({"id": "42", "username": "bridge-bot", "bot": True})

    async def guilds(self, request):
        self._check(request)
        return web.json_response([{"id": GUILD, "name": "Family"}])

    async def channels(self, request):
        self._check(request)
        return web.json_response(list(self.channel_store.values()))

    async def get_channel(self, request):
        self._check(request)
        cid = request.match_info["cid"]
        channel = self.channel_store.get(cid)
        if channel is None:
            return web.json_response(
                {"message": "Unknown Channel"}, status=404
            )
        return web.json_response(channel)

    def _assign_tag_ids(self, tags):
        assigned = []
        for tag in tags or []:
            item = dict(tag)
            if not item.get("id"):
                self.next_tag_id += 1
                item["id"] = f"new{self.next_tag_id}"
            item.setdefault("emoji_name", None)
            item.setdefault("emoji_id", None)
            item.setdefault("moderated", False)
            assigned.append(item)
        return assigned

    async def create_channel(self, request):
        self._check(request)
        body = await request.json()
        if not body.get("name"):
            return web.json_response({"message": "name required"}, status=400)
        self.next_channel_id += 1
        channel = {
            "id": str(self.next_channel_id),
            "name": body["name"],
            "type": body.get("type", 0),
            "parent_id": body.get("parent_id"),
            "topic": body.get("topic"),
            "guild_id": request.match_info["gid"],
            "available_tags": self._assign_tag_ids(
                body.get("available_tags")
            ),
        }
        self.channel_store[channel["id"]] = channel
        self.last_create_body = body
        return web.json_response(channel, status=201)

    async def patch_channel(self, request):
        self._check(request)
        cid = request.match_info["cid"]
        channel = self.channel_store.get(cid)
        if channel is None:
            return web.json_response(
                {"message": "Unknown Channel"}, status=404
            )
        body = await request.json()
        self.last_patch_body = body
        for key in ("name", "topic", "parent_id", "position"):
            if key in body:
                channel[key] = body[key]
        if "available_tags" in body:
            channel["available_tags"] = self._assign_tag_ids(
                body["available_tags"]
            )
        return web.json_response(channel)

    async def create_thread(self, request):
        self._check(request)
        cid = request.match_info["cid"]
        if (request.content_type or "").startswith("multipart/form-data"):
            import json as _json

            form = await request.post()
            body = _json.loads(form["payload_json"])
            upload = form["files[0]"]
            self.last_forum_upload = {
                "payload": body,
                "filename": upload.filename,
                "data": upload.file.read(),
            }
        else:
            body = await request.json()
        self.last_forum_body = body
        if not body.get("name"):
            return web.json_response({"message": "name required"}, status=400)
        self.next_thread_id += 1
        thread = {
            "id": str(self.next_thread_id),
            "name": body["name"],
            "parent_id": cid,
            "guild_id": GUILD,
            "owner_id": "42",
            "applied_tags": body.get("applied_tags") or [],
            "message_count": 1,
            "member_count": 1,
            "thread_metadata": {"archived": False, "locked": False},
        }
        self.threads.append(thread)
        return web.json_response(thread, status=201)

    async def active_threads(self, request):
        self._check(request)
        active = [
            t for t in self.threads
            if not (t.get("thread_metadata") or {}).get("archived")
        ]
        return web.json_response(
            {"threads": active, "members": [], "has_more": False}
        )

    async def archived_threads(self, request):
        self._check(request)
        archived = [
            t for t in self.threads
            if (t.get("thread_metadata") or {}).get("archived")
        ]
        return web.json_response(
            {"threads": archived, "members": [], "has_more": False}
        )

    async def get_messages(self, request):
        self._check(request)
        items = self.messages
        after = request.query.get("after")
        before = request.query.get("before")
        limit = int(request.query.get("limit", 50))
        if after:
            items = [m for m in items if int(m["id"]) > int(after)]
        if before:
            items = [m for m in items if int(m["id"]) < int(before)]
        page = items[-limit:]  # newest `limit`
        return web.json_response(list(reversed(page)))  # Discord: newest first

    async def post_message(self, request):
        self._check(request)
        if (request.content_type or "").startswith("multipart/form-data"):
            import json as _json

            form = await request.post()
            body = _json.loads(form["payload_json"])
            upload = form["files[0]"]
            self.last_upload = {
                "payload": body,
                "filename": upload.filename,
                "content_type": upload.content_type,
                "data": upload.file.read(),
            }
            for att in body.get("attachments") or []:
                att.setdefault(
                    "url",
                    "https://cdn.discordapp.com/attachments/x/" + att["filename"],
                )
                att.setdefault("content_type", upload.content_type)
                att.setdefault("size", len(self.last_upload["data"]))
        else:
            body = await request.json()
        self.last_send_body = body
        self.next_id += 1
        message = make_message(self.next_id, "42", body.get("content", ""))
        if body.get("poll"):
            poll = body["poll"]
            answers = []
            counts = []
            for index, ans in enumerate(poll.get("answers") or []):
                answers.append({
                    "answer_id": index + 1,
                    "poll_media": ans.get("poll_media") or {},
                })
                counts.append({
                    "id": index + 1, "count": 0, "me_voted": False,
                })
            message["poll"] = {
                "question": poll.get("question"),
                "answers": answers,
                "expiry": "2026-11-14T00:00:00+00:00",
                "allow_multiselect": poll.get("allow_multiselect", False),
                "layout_type": poll.get("layout_type", 1),
                "results": {
                    "is_finalized": False, "answer_counts": counts,
                },
            }
        if body.get("attachments"):
            message["attachments"] = body["attachments"]
        message["author"] = {"id": "42", "username": "bridge-bot",
                             "global_name": None, "bot": True}
        if body.get("message_reference"):
            message["referenced_message"] = {
                "id": body["message_reference"]["message_id"]
            }
        self.messages.append(message)
        return web.json_response(message, status=201)

    async def get_one_message(self, request):
        self._check(request)
        mid = request.match_info["mid"]
        for message in self.messages:
            if message["id"] == mid:
                return web.json_response(message)
        return web.json_response({"message": "Unknown Message"}, status=404)

    async def poll_voters(self, request):
        self._check(request)
        if request.match_info["aid"] == "1":
            users = [
                {"id": "111", "username": "user111", "global_name": None},
                {"id": "222", "username": "user222", "global_name": "Fam"},
            ]
        else:
            users = []
        return web.json_response({"users": users})

    async def poll_expire(self, request):
        self._check(request)
        mid = request.match_info["mid"]
        for message in self.messages:
            if message["id"] == mid and message.get("poll"):
                counts = message["poll"]["results"]["answer_counts"]
                if counts:
                    counts[0]["count"] = 2
                message["poll"]["results"]["is_finalized"] = True
                return web.json_response(message)
        return web.json_response({"message": "Unknown Poll"}, status=404)

    async def list_events(self, request):
        self._check(request)
        return web.json_response(self.events)

    async def create_event(self, request):
        self._check(request)
        body = await request.json()
        metadata = body.get("entity_metadata") or {}
        if body.get("entity_type") == 3 and not metadata.get("location"):
            return web.json_response(
                {"message": "entity_metadata.location is required"}, status=400
            )
        event = {
            "id": str(700 + len(self.events)),
            "guild_id": request.match_info["gid"],
            "name": body["name"],
            "description": body.get("description"),
            "scheduled_start_time": body["scheduled_start_time"],
            "scheduled_end_time": body.get("scheduled_end_time"),
            "entity_metadata": metadata,
            "status": 1,
            "entity_type": body.get("entity_type"),
        }
        self.events.append(event)
        return web.json_response(event, status=201)


@pytest.fixture()
async def stub():
    server = StubDiscord()
    async with TestClient(TestServer(server.app)) as client:
        base = str(client.make_url("/api/v10"))
        yield server, client.session, base


def make_client(session, base, author_id="111", token=TOKEN):
    return DiscordClient(session, token, author_id=author_id, api_base=base)


@pytest.mark.asyncio
async def test_identity_and_structure(stub):
    _, session, base = stub
    client = make_client(session, base)
    assert (await client.me())["username"] == "bridge-bot"
    assert (await client.guilds())[0]["name"] == "Family"
    channels = await client.channels(GUILD)
    assert [c["name"] for c in channels] == ["chat", "trips", "ai-created"]


@pytest.mark.asyncio
async def test_messages_keep_attachments_and_order(stub):
    _, session, base = stub
    client = make_client(session, base)
    messages = await client.messages(CHANNEL)
    assert [m["id"] for m in messages] == ["100", "101", "102", "103"]
    image = messages[2]["attachments"][0]
    assert image["content_type"] == "image/png"
    assert image["width"] == 800
    video = messages[3]["attachments"][0]
    assert video["content_type"] == "video/mp4"


@pytest.mark.asyncio
async def test_new_messages_and_author_filter(stub):
    _, session, base = stub
    client = make_client(session, base)
    fresh = await client.new_messages(CHANNEL, after_id="100")
    assert [m["id"] for m in fresh] == ["101", "102", "103"]
    mine = client.filter_author(fresh)
    assert [m["id"] for m in mine] == ["102", "103"]


@pytest.mark.asyncio
async def test_send_with_reply_reference(stub):
    server, session, base = stub
    client = make_client(session, base)
    sent = await client.send(CHANNEL, "got it", in_reply_to="102")
    assert sent["reply_to"] == "102"
    assert server.last_send_body["message_reference"] == {"message_id": "102"}


@pytest.mark.asyncio
async def test_send_file_multipart(stub):
    server, session, base = stub
    client = make_client(session, base)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    sent = await client.send_file(
        CHANNEL, "art.png", png, content="made this", content_type="image/png"
    )
    assert server.last_upload["filename"] == "art.png"
    assert server.last_upload["data"] == png
    assert server.last_upload["payload"]["content"] == "made this"
    assert sent["attachments"][0]["filename"] == "art.png"
    assert sent["attachments"][0]["content_type"] == "image/png"


@pytest.mark.asyncio
async def test_send_file_validation(stub):
    _, session, base = stub
    client = make_client(session, base)
    with pytest.raises(DiscordError):
        await client.send_file(CHANNEL, "empty.png", b"")
    with pytest.raises(DiscordError) as excinfo:
        await client.send_file(
            CHANNEL, "big.bin", b"0" * (discord_client.MAX_FILE_BYTES + 1)
        )
    assert excinfo.value.status == 413


@pytest.mark.asyncio
async def test_create_event_external_defaults_end(stub):
    server, session, base = stub
    client = make_client(session, base)
    event = await client.create_event(
        GUILD, "Family trip to the lake",
        "2026-12-20T09:00:00+09:00", location="Lake House",
        description="Bring food",
    )
    assert event["location"] == "Lake House"
    assert event["scheduled_end_time"] == "2026-12-20T01:00:00+00:00" or \
        event["scheduled_end_time"].startswith("2026-12-20T10:00")
    assert server.events[0]["entity_type"] == 3
    listed = await client.list_events(GUILD)
    assert [e["name"] for e in listed] == ["Family trip to the lake"]


@pytest.mark.asyncio
async def test_create_event_requires_location(stub):
    _, session, base = stub
    client = make_client(session, base)
    with pytest.raises(DiscordError):
        await client.create_event(
            GUILD, "No place", "2026-12-20T09:00:00+09:00"
        )


@pytest.mark.asyncio
async def test_forum_tags_and_resolve(stub):
    _, session, base = stub
    client = make_client(session, base)
    channel = await client.channel(FORUM)
    assert channel["type"] == 15
    tags = await client.forum_tags(FORUM)
    assert [t["name"] for t in tags] == ["画像", "文書・記事", "アプリ"]
    assert client.resolve_tag_ids(channel, ["画像", "t3"]) == ["t1", "t3"]
    with pytest.raises(DiscordError) as excinfo:
        client.resolve_tag_ids(channel, ["存在しない"])
    assert excinfo.value.status == 400
    assert "画像" in str(excinfo.value)


@pytest.mark.asyncio
async def test_create_forum_post_and_list(stub):
    server, session, base = stub
    client = make_client(session, base)
    post = await client.create_forum_post(
        FORUM, "家づくり記録 #1", content="本文", applied_tags=["t1"]
    )
    assert post["name"] == "家づくり記録 #1"
    assert post["parent_id"] == FORUM
    assert post["applied_tags"] == ["t1"]
    assert server.last_forum_body["message"] == {"content": "本文"}
    posts = await client.list_forum_posts(GUILD, FORUM)
    assert [p["id"] for p in posts] == [post["id"]]


@pytest.mark.asyncio
async def test_create_forum_post_with_file(stub):
    server, session, base = stub
    client = make_client(session, base)
    png = b"\x89PNG" + b"1" * 50
    post = await client.create_forum_post(
        FORUM, "アイキャッチ", content="見てね", applied_tags=["t1"],
        filename="eye.png", data=png, content_type="image/png",
    )
    assert server.last_forum_upload["filename"] == "eye.png"
    assert server.last_forum_upload["data"] == png
    payload = server.last_forum_upload["payload"]
    assert payload["name"] == "アイキャッチ"
    assert payload["applied_tags"] == ["t1"]
    assert payload["message"]["attachments"][0]["filename"] == "eye.png"
    assert post["applied_tags"] == ["t1"]


@pytest.mark.asyncio
async def test_create_forum_post_validation(stub):
    _, session, base = stub
    client = make_client(session, base)
    with pytest.raises(DiscordError):
        await client.create_forum_post(FORUM, "", content="x")
    with pytest.raises(DiscordError):
        await client.create_forum_post(FORUM, "題だけ")
    with pytest.raises(DiscordError):
        await client.create_forum_post(
            FORUM, "多すぎ", content="x",
            applied_tags=["1", "2", "3", "4", "5", "6"],
        )
    with pytest.raises(DiscordError) as excinfo:
        await client.create_forum_post(
            FORUM, "大きすぎ", filename="big.bin",
            data=b"0" * (discord_client.MAX_FILE_BYTES + 1),
        )
    assert excinfo.value.status == 413


@pytest.mark.asyncio
async def test_create_forum_channel_with_tags(stub):
    server, session, base = stub
    client = make_client(session, base)
    channel = await client.create_channel(
        GUILD, "ai-created-new", type=15, parent_id="cat1",
        topic="works", available_tags=["画像", {"name": "アプリ",
                                               "emoji_name": "📱"}],
    )
    assert channel["type"] == 15
    assert channel["parent_id"] == "cat1"
    assert [t["name"] for t in channel["available_tags"]] == ["画像", "アプリ"]
    assert all(t["id"] for t in channel["available_tags"])
    assert server.last_create_body["type"] == 15


@pytest.mark.asyncio
async def test_create_channel_validation(stub):
    _, session, base = stub
    client = make_client(session, base)
    with pytest.raises(DiscordError):
        await client.create_channel(GUILD, "")
    with pytest.raises(DiscordError):
        await client.create_channel(GUILD, "x", type=99)
    with pytest.raises(DiscordError):
        await client.create_channel(
            GUILD, "x", type=0, available_tags=["画像"]
        )
    with pytest.raises(DiscordError):
        await client.create_channel(
            GUILD, "x", type=15,
            available_tags=[str(i) for i in range(21)],
        )


@pytest.mark.asyncio
async def test_edit_channel_rename_and_topic(stub):
    server, session, base = stub
    client = make_client(session, base)
    channel = await client.edit_channel(
        CHANNEL, name="ai-created-archive", topic=""
    )
    assert channel["name"] == "ai-created-archive"
    assert server.last_patch_body == {
        "name": "ai-created-archive", "topic": "",
    }
    with pytest.raises(DiscordError):
        await client.edit_channel(CHANNEL)


@pytest.mark.asyncio
async def test_set_forum_tags_preserves_existing_ids(stub):
    server, session, base = stub
    client = make_client(session, base)
    channel = await client.set_forum_tags(FORUM, ["画像", "動画"])
    tags = {t["name"]: t for t in channel["available_tags"]}
    assert tags["画像"]["id"] == "t1"  # existing tag kept its id
    assert tags["画像"]["emoji_name"] == "🖼️"
    assert tags["動画"]["id"].startswith("new")  # new tag got an id
    assert "文書・記事" not in tags  # unlisted tags are removed
    with pytest.raises(DiscordError):
        await client.set_forum_tags(CHANNEL, ["画像"])


@pytest.mark.asyncio
async def test_poll_lifecycle(stub):
    server, session, base = stub
    client = make_client(session, base)
    sent = await client.send_poll(
        CHANNEL, "次の旅行先は？",
        ["海", {"text": "山", "emoji_name": "⛰️"}],
        duration_hours=48, allow_multiselect=True,
    )
    poll = sent["poll"]
    assert poll["question"] == "次の旅行先は？"
    assert [a["text"] for a in poll["answers"]] == ["海", "山"]
    assert poll["answers"][1]["emoji_name"] == "⛰️"
    assert poll["allow_multiselect"] is True
    assert server.last_send_body["poll"]["duration"] == 48
    assert poll["results"]["total_votes"] == 0

    results = await client.poll_results(CHANNEL, sent["id"])
    assert results["poll"]["results"]["is_finalized"] is False

    voters = await client.poll_voters(CHANNEL, sent["id"], 1)
    assert [u["username"] for u in voters] == ["user111", "user222"]
    assert await client.poll_voters(CHANNEL, sent["id"], 2) == []

    final = await client.expire_poll(CHANNEL, sent["id"])
    assert final["poll"]["results"]["is_finalized"] is True
    assert final["poll"]["results"]["total_votes"] == 2


@pytest.mark.asyncio
async def test_poll_validation(stub):
    _, session, base = stub
    client = make_client(session, base)
    with pytest.raises(DiscordError):
        await client.send_poll(CHANNEL, "", ["a"])
    with pytest.raises(DiscordError):
        await client.send_poll(CHANNEL, "q" * 301, ["a"])
    with pytest.raises(DiscordError):
        await client.send_poll(CHANNEL, "q", [])
    with pytest.raises(DiscordError):
        await client.send_poll(CHANNEL, "q", ["a"] * 11)
    with pytest.raises(DiscordError):
        await client.send_poll(CHANNEL, "q", ["a" * 56])
    with pytest.raises(DiscordError):
        await client.send_poll(CHANNEL, "q", ["a"], duration_hours=0)
    with pytest.raises(DiscordError):
        await client.send_poll(CHANNEL, "q", ["a"], duration_hours=769)
    with pytest.raises(DiscordError) as excinfo:
        await client.poll_results(CHANNEL, "100")
    assert excinfo.value.status == 400


@pytest.mark.asyncio
async def test_wrong_token_rejected(stub):
    _, session, base = stub
    client = make_client(session, base, token="bad")
    with pytest.raises(DiscordError) as excinfo:
        await client.me()
    assert excinfo.value.status == 401
