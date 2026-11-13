"""Discord Bridge integration.

Two ways to reach Discord, both exposed through Home Assistant's own
authenticated REST API (no new port is opened):

Bridge (queue) mode - via the local Discord Bridge add-on:
  GET  /api/discord_bridge/health
  GET  /api/discord_bridge/pending   (filtered to the configured author)
  POST /api/discord_bridge/ack       (refuses other authors' message ids)
  POST /api/discord_bridge/send

Direct mode - this integration talks to the Discord REST API itself,
using the same bot token the add-on uses (the token stays in Home
Assistant). Reading history is non-destructive, so it never competes
with the add-on's queue or another assistant:
  GET  /api/discord_bridge/direct/health
  GET  /api/discord_bridge/direct/channels?guild_id=
  GET  /api/discord_bridge/direct/messages?channel_id=&limit=&after=&before=
  GET  /api/discord_bridge/direct/new
       (configured channels, filtered to the configured author,
        messages since the stored cursor; does not advance the cursor)
  POST /api/discord_bridge/direct/mark_read {channel_id, message_id}
  POST /api/discord_bridge/direct/send {channel_id, content, in_reply_to?}
  GET  /api/discord_bridge/direct/events?guild_id=
  POST /api/discord_bridge/direct/events
       {name, scheduled_start_time, scheduled_end_time?,
        description?, location, guild_id?}
  POST /api/discord_bridge/direct/channels
       {guild_id?, name, type?, parent_id?, topic?,
        available_tags?: [tag name or {name, emoji_name}]}
       Creates a channel (type 15 = forum). Needs the bot's Manage
       Channels permission in Discord; Discord's 403 is passed through.
  GET  /api/discord_bridge/direct/channel?channel_id=
  POST /api/discord_bridge/direct/channel
       {channel_id, name?, topic?, parent_id?, position?,
        tags?: [names] | available_tags?: [...]}
       Modifies a channel (also available as PATCH). ``tags`` replaces
       a forum's tags by name, preserving existing tag ids; use
       ``available_tags`` for the raw Discord form.
  POST /api/discord_bridge/direct/poll
       {channel_id, question, answers: [text | {text, emoji_name}],
        duration_hours?, allow_multiselect?, content?}
       Creates a poll (needs the bot's Send Polls permission).
  GET  /api/discord_bridge/direct/poll?channel_id=&message_id=
       Current results of that poll.
  GET  /api/discord_bridge/direct/poll/voters?channel_id=&message_id=
       &answer_id=&limit=
       Users who voted for that answer.
  POST /api/discord_bridge/direct/poll/expire {channel_id, message_id}
       Ends the poll early.
  GET  /api/discord_bridge/direct/forum?channel_id=
       (forum channel info, including its available_tags)
  GET  /api/discord_bridge/direct/forum/posts?channel_id=&guild_id=
       (active + archived forum posts of that forum)
  POST /api/discord_bridge/direct/forum/posts
       {channel_id, title, content?, tags?: [tag name or id]}
       Creates one forum post. ``tags`` are resolved against the
       forum's available_tags by name; unknown names are a 400.
       A file can be attached with multipart/form-data instead:
       fields channel_id, title, content (optional), tags
       (comma-separated names) and one file part named ``file``.

All views require Home Assistant authentication. By default they also
require an admin user (require_admin option).
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.http import HomeAssistantView
from homeassistant.helpers.storage import Store

from .bridge_client import BridgeError, DiscordBridgeClient
from .const import (
    CONF_AUTHOR_ID,
    CONF_BRIDGE_URL,
    CONF_CHANNEL_IDS,
    CONF_DISCORD_TOKEN,
    CONF_GUILD_ID,
    CONF_QUEUE_TOKEN,
    CONF_REQUIRE_ADMIN,
    DEFAULT_REQUIRE_ADMIN,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .discord_client import DiscordClient, DiscordError

_LOGGER = logging.getLogger(__name__)


def _data(hass: HomeAssistant) -> dict[str, Any]:
    return hass.data.get(DOMAIN, {})


class _BaseView(HomeAssistantView):
    """Base view: HA-authenticated, optionally admin-only."""

    requires_auth = True

    def _authorize(self, request: web.Request) -> web.Response | None:
        data = _data(request.app["hass"])
        if not data.get("bridge_client") and not data.get("discord_client"):
            return self.json(
                {"error": "discord_bridge is not configured"}, status_code=503
            )
        if data.get("require_admin", DEFAULT_REQUIRE_ADMIN):
            user = request.get("hass_user")
            if user is None or not user.is_admin:
                return self.json(
                    {"error": "admin user required"}, status_code=403
                )
        return None

    def _bridge(self, request: web.Request) -> DiscordBridgeClient | web.Response:
        client = _data(request.app["hass"]).get("bridge_client")
        if client is None:
            return self.json(
                {"error": "bridge mode is not configured"}, status_code=503
            )
        return client

    def _discord(self, request: web.Request) -> DiscordClient | web.Response:
        client = _data(request.app["hass"]).get("discord_client")
        if client is None:
            return self.json(
                {"error": "direct mode is not configured"}, status_code=503
            )
        return client

    @staticmethod
    def _error_response(err: BridgeError | DiscordError) -> web.Response:
        status = err.status if err.status and 400 <= err.status < 500 else 502
        body: dict[str, Any] = {"error": str(err)}
        retry_after = getattr(err, "retry_after", None)
        if retry_after is not None:
            body["retry_after"] = retry_after
            status = 429
        return web.json_response(body, status=status)


# ---------------------------------------------------------------- bridge


class HealthView(_BaseView):
    url = "/api/discord_bridge/health"
    name = "api:discord_bridge:health"

    async def get(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._bridge(request)
        if isinstance(client, web.Response):
            return client
        try:
            return self.json(await client.health())
        except BridgeError as err:
            return self._error_response(err)


class PendingView(_BaseView):
    url = "/api/discord_bridge/pending"
    name = "api:discord_bridge:pending"

    async def get(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._bridge(request)
        if isinstance(client, web.Response):
            return client
        try:
            messages = await client.pending()
        except BridgeError as err:
            return self._error_response(err)
        return self.json({"messages": messages, "count": len(messages)})


class AckView(_BaseView):
    url = "/api/discord_bridge/ack"
    name = "api:discord_bridge:ack"

    async def post(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._bridge(request)
        if isinstance(client, web.Response):
            return client
        try:
            body: dict[str, Any] = await request.json()
        except ValueError:
            return self.json({"error": "invalid JSON body"}, status_code=400)
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids:
            return self.json(
                {"error": "ids (non-empty list) is required"}, status_code=400
            )
        try:
            return self.json(await client.ack(ids))
        except BridgeError as err:
            return self._error_response(err)


class SendView(_BaseView):
    url = "/api/discord_bridge/send"
    name = "api:discord_bridge:send"

    async def post(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._bridge(request)
        if isinstance(client, web.Response):
            return client
        try:
            body: dict[str, Any] = await request.json()
        except ValueError:
            return self.json({"error": "invalid JSON body"}, status_code=400)
        try:
            result = await client.send(
                channel_id=body.get("channel_id", ""),
                content=body.get("content", ""),
                dedupe_id=body.get("dedupe_id", ""),
                in_reply_to=body.get("in_reply_to"),
            )
        except BridgeError as err:
            return self._error_response(err)
        return self.json(result)


# ---------------------------------------------------------------- direct


class DirectHealthView(_BaseView):
    url = "/api/discord_bridge/direct/health"
    name = "api:discord_bridge:direct:health"

    async def get(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        try:
            me = await client.me()
            guilds = await client.guilds()
        except DiscordError as err:
            return self._error_response(err)
        return self.json({"bot": me, "guilds": guilds})


class DirectChannelsView(_BaseView):
    url = "/api/discord_bridge/direct/channels"
    name = "api:discord_bridge:direct:channels"

    def _guild_id(self, request: web.Request, body: dict | None = None):
        if body and body.get("guild_id"):
            return str(body["guild_id"])
        return request.query.get("guild_id") or _data(
            request.app["hass"]
        ).get("guild_id")

    async def get(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        guild_id = self._guild_id(request)
        if not guild_id:
            return self.json(
                {"error": "guild_id is required"}, status_code=400
            )
        try:
            channels = await client.channels(str(guild_id))
        except DiscordError as err:
            return self._error_response(err)
        return self.json({"channels": channels})

    async def post(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        try:
            body: dict[str, Any] = await request.json()
        except ValueError:
            return self.json({"error": "invalid JSON body"}, status_code=400)
        guild_id = self._guild_id(request, body)
        if not guild_id:
            return self.json(
                {"error": "guild_id is required"}, status_code=400
            )
        if not body.get("name"):
            return self.json({"error": "name is required"}, status_code=400)
        try:
            channel_type = int(body.get("type", 0))
        except (TypeError, ValueError):
            return self.json(
                {"error": "type must be a number"}, status_code=400
            )
        try:
            channel = await client.create_channel(
                guild_id=str(guild_id),
                name=str(body["name"]),
                type=channel_type,
                parent_id=body.get("parent_id"),
                topic=body.get("topic"),
                available_tags=body.get("available_tags"),
            )
        except DiscordError as err:
            return self._error_response(err)
        return self.json(channel)


class DirectChannelView(_BaseView):
    url = "/api/discord_bridge/direct/channel"
    name = "api:discord_bridge:direct:channel"

    async def get(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        channel_id = request.query.get("channel_id")
        if not channel_id:
            return self.json(
                {"error": "channel_id is required"}, status_code=400
            )
        try:
            channel = await client.channel(str(channel_id))
        except DiscordError as err:
            return self._error_response(err)
        return self.json(channel)

    async def _edit(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        try:
            body: dict[str, Any] = await request.json()
        except ValueError:
            return self.json({"error": "invalid JSON body"}, status_code=400)
        channel_id = body.get("channel_id")
        if not channel_id:
            return self.json(
                {"error": "channel_id is required"}, status_code=400
            )
        try:
            if body.get("tags") is not None and (
                body.get("name") is not None
                or body.get("topic") is not None
                or body.get("parent_id") is not None
                or body.get("position") is not None
            ):
                # Apply the other edits first, then the tag replacement,
                # so the response reflects the final state.
                await client.edit_channel(
                    str(channel_id),
                    name=body.get("name"),
                    topic=body.get("topic"),
                    parent_id=body.get("parent_id"),
                    position=body.get("position"),
                )
                channel = await client.set_forum_tags(
                    str(channel_id), body["tags"]
                )
            elif body.get("tags") is not None:
                channel = await client.set_forum_tags(
                    str(channel_id), body["tags"]
                )
            else:
                channel = await client.edit_channel(
                    str(channel_id),
                    name=body.get("name"),
                    topic=body.get("topic"),
                    parent_id=body.get("parent_id"),
                    position=body.get("position"),
                    available_tags=body.get("available_tags"),
                )
        except DiscordError as err:
            return self._error_response(err)
        return self.json(channel)

    async def post(self, request: web.Request) -> web.Response:
        return await self._edit(request)

    async def patch(self, request: web.Request) -> web.Response:
        return await self._edit(request)


class DirectMessagesView(_BaseView):
    url = "/api/discord_bridge/direct/messages"
    name = "api:discord_bridge:direct:messages"

    async def get(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        channel_id = request.query.get("channel_id")
        if not channel_id:
            return self.json(
                {"error": "channel_id is required"}, status_code=400
            )
        try:
            limit = int(request.query.get("limit", 50))
        except ValueError:
            return self.json({"error": "limit must be a number"}, status_code=400)
        try:
            messages = await client.messages(
                channel_id,
                limit=limit,
                after=request.query.get("after"),
                before=request.query.get("before"),
            )
        except DiscordError as err:
            return self._error_response(err)
        return self.json({"messages": messages, "count": len(messages)})


class DirectNewView(_BaseView):
    url = "/api/discord_bridge/direct/new"
    name = "api:discord_bridge:direct:new"

    async def get(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        data = _data(request.app["hass"])
        channel_ids: list[str] = data.get("channel_ids") or []
        if not channel_ids:
            return self.json(
                {"error": "no watch channels configured"}, status_code=400
            )
        cursors: dict[str, str] = data.get("cursors") or {}
        collected: list[dict[str, Any]] = []
        latest: dict[str, str | None] = {}
        try:
            for channel_id in channel_ids:
                fresh = await client.new_messages(
                    channel_id, cursors.get(channel_id)
                )
                latest[channel_id] = fresh[-1]["id"] if fresh else cursors.get(
                    channel_id
                )
                collected.extend(client.filter_author(fresh))
        except DiscordError as err:
            return self._error_response(err)
        collected.sort(key=lambda m: int(m["id"]))
        return self.json(
            {"messages": collected, "count": len(collected), "latest": latest}
        )


class DirectMarkReadView(_BaseView):
    url = "/api/discord_bridge/direct/mark_read"
    name = "api:discord_bridge:direct:mark_read"

    async def post(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        try:
            body: dict[str, Any] = await request.json()
        except ValueError:
            return self.json({"error": "invalid JSON body"}, status_code=400)
        channel_id = body.get("channel_id")
        message_id = body.get("message_id")
        if not channel_id or not message_id:
            return self.json(
                {"error": "channel_id and message_id are required"},
                status_code=400,
            )
        data = _data(request.app["hass"])
        cursors: dict[str, str] = data.setdefault("cursors", {})
        current = cursors.get(str(channel_id))
        if current is None or int(str(message_id)) > int(current):
            cursors[str(channel_id)] = str(message_id)
        store: Store | None = data.get("store")
        if store is not None:
            await store.async_save(cursors)
        return self.json(
            {"channel_id": str(channel_id), "cursor": cursors[str(channel_id)]}
        )


class DirectSendView(_BaseView):
    url = "/api/discord_bridge/direct/send"
    name = "api:discord_bridge:direct:send"

    async def post(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        if (request.content_type or "").startswith("multipart/form-data"):
            return await self._post_file(request, client)
        try:
            body: dict[str, Any] = await request.json()
        except ValueError:
            return self.json({"error": "invalid JSON body"}, status_code=400)
        if not body.get("channel_id") or not body.get("content"):
            return self.json(
                {"error": "channel_id and content are required"},
                status_code=400,
            )
        try:
            result = await client.send(
                channel_id=str(body["channel_id"]),
                content=body["content"],
                in_reply_to=body.get("in_reply_to"),
            )
        except DiscordError as err:
            return self._error_response(err)
        return self.json(result)

    async def _post_file(self, request: web.Request, client) -> web.Response:
        """Handle a multipart upload: fields channel_id, content,
        in_reply_to and one file part named ``file``."""
        try:
            form = await request.post()
        except ValueError:
            return self.json({"error": "invalid form data"}, status_code=400)
        channel_id = form.get("channel_id")
        upload = form.get("file")
        if not channel_id or upload is None or not hasattr(upload, "file"):
            return self.json(
                {"error": "channel_id and a file part are required"},
                status_code=400,
            )
        data = upload.file.read()
        try:
            result = await client.send_file(
                channel_id=str(channel_id),
                filename=upload.filename or "file",
                data=data,
                content=form.get("content") or None,
                content_type=upload.content_type,
                in_reply_to=form.get("in_reply_to") or None,
            )
        except DiscordError as err:
            return self._error_response(err)
        return self.json(result)


class DirectEventsView(_BaseView):
    url = "/api/discord_bridge/direct/events"
    name = "api:discord_bridge:direct:events"

    def _guild_id(self, request: web.Request, body: dict | None = None) -> str | None:
        if body and body.get("guild_id"):
            return str(body["guild_id"])
        return request.query.get("guild_id") or _data(request.app["hass"]).get(
            "guild_id"
        )

    async def get(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        guild_id = self._guild_id(request)
        if not guild_id:
            return self.json(
                {"error": "guild_id is required"}, status_code=400
            )
        try:
            events = await client.list_events(str(guild_id))
        except DiscordError as err:
            return self._error_response(err)
        return self.json({"events": events, "count": len(events)})

    async def post(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        try:
            body: dict[str, Any] = await request.json()
        except ValueError:
            return self.json({"error": "invalid JSON body"}, status_code=400)
        guild_id = self._guild_id(request, body)
        if not guild_id:
            return self.json(
                {"error": "guild_id is required"}, status_code=400
            )
        try:
            event = await client.create_event(
                guild_id=str(guild_id),
                name=body.get("name", ""),
                scheduled_start_time=body.get("scheduled_start_time", ""),
                scheduled_end_time=body.get("scheduled_end_time"),
                description=body.get("description"),
                location=body.get("location"),
            )
        except DiscordError as err:
            return self._error_response(err)
        return self.json(event)


class DirectPollView(_BaseView):
    url = "/api/discord_bridge/direct/poll"
    name = "api:discord_bridge:direct:poll"

    async def get(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        channel_id = request.query.get("channel_id")
        message_id = request.query.get("message_id")
        if not channel_id or not message_id:
            return self.json(
                {"error": "channel_id and message_id are required"},
                status_code=400,
            )
        try:
            result = await client.poll_results(
                str(channel_id), str(message_id)
            )
        except DiscordError as err:
            return self._error_response(err)
        return self.json(result)

    async def post(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        try:
            body: dict[str, Any] = await request.json()
        except ValueError:
            return self.json({"error": "invalid JSON body"}, status_code=400)
        if not body.get("channel_id") or not body.get("question"):
            return self.json(
                {"error": "channel_id and question are required"},
                status_code=400,
            )
        if not isinstance(body.get("answers"), list):
            return self.json(
                {"error": "answers (list) is required"}, status_code=400
            )
        try:
            result = await client.send_poll(
                channel_id=str(body["channel_id"]),
                question=str(body["question"]),
                answers=body["answers"],
                duration_hours=body.get("duration_hours", 24),
                allow_multiselect=bool(body.get("allow_multiselect")),
                content=body.get("content"),
            )
        except DiscordError as err:
            return self._error_response(err)
        return self.json(result)


class DirectPollVotersView(_BaseView):
    url = "/api/discord_bridge/direct/poll/voters"
    name = "api:discord_bridge:direct:poll:voters"

    async def get(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        channel_id = request.query.get("channel_id")
        message_id = request.query.get("message_id")
        answer_id = request.query.get("answer_id")
        if not channel_id or not message_id or answer_id is None:
            return self.json(
                {
                    "error": "channel_id, message_id and answer_id "
                    "are required"
                },
                status_code=400,
            )
        try:
            limit = int(request.query.get("limit", 100))
        except ValueError:
            return self.json(
                {"error": "limit must be a number"}, status_code=400
            )
        try:
            users = await client.poll_voters(
                str(channel_id), str(message_id), answer_id, limit=limit
            )
        except DiscordError as err:
            return self._error_response(err)
        return self.json({"users": users, "count": len(users)})


class DirectPollExpireView(_BaseView):
    url = "/api/discord_bridge/direct/poll/expire"
    name = "api:discord_bridge:direct:poll:expire"

    async def post(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        try:
            body: dict[str, Any] = await request.json()
        except ValueError:
            return self.json({"error": "invalid JSON body"}, status_code=400)
        if not body.get("channel_id") or not body.get("message_id"):
            return self.json(
                {"error": "channel_id and message_id are required"},
                status_code=400,
            )
        try:
            result = await client.expire_poll(
                str(body["channel_id"]), str(body["message_id"])
            )
        except DiscordError as err:
            return self._error_response(err)
        return self.json(result)


class DirectForumView(_BaseView):
    url = "/api/discord_bridge/direct/forum"
    name = "api:discord_bridge:direct:forum"

    async def get(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        channel_id = request.query.get("channel_id")
        if not channel_id:
            return self.json(
                {"error": "channel_id is required"}, status_code=400
            )
        try:
            channel = await client.channel(str(channel_id))
        except DiscordError as err:
            return self._error_response(err)
        return self.json(channel)


class DirectForumPostsView(_BaseView):
    url = "/api/discord_bridge/direct/forum/posts"
    name = "api:discord_bridge:direct:forum:posts"

    async def get(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        channel_id = request.query.get("channel_id")
        if not channel_id:
            return self.json(
                {"error": "channel_id is required"}, status_code=400
            )
        guild_id = request.query.get("guild_id") or _data(
            request.app["hass"]
        ).get("guild_id")
        if not guild_id:
            return self.json(
                {"error": "guild_id is required"}, status_code=400
            )
        try:
            posts = await client.list_forum_posts(
                str(guild_id), str(channel_id)
            )
        except DiscordError as err:
            return self._error_response(err)
        return self.json({"posts": posts, "count": len(posts)})

    async def post(self, request: web.Request) -> web.Response:
        if (error := self._authorize(request)) is not None:
            return error
        client = self._discord(request)
        if isinstance(client, web.Response):
            return client
        upload = None
        if (request.content_type or "").startswith("multipart/form-data"):
            try:
                form = await request.post()
            except ValueError:
                return self.json(
                    {"error": "invalid form data"}, status_code=400
                )
            raw_tags = form.get("tags") or ""
            tags = [
                part.strip()
                for part in str(raw_tags).split(",")
                if part.strip()
            ]
            body: dict[str, Any] = {
                "channel_id": form.get("channel_id"),
                "title": form.get("title"),
                "content": form.get("content") or None,
                "tags": tags,
            }
            upload = form.get("file")
            if upload is not None and not hasattr(upload, "file"):
                upload = None
        else:
            try:
                body = await request.json()
            except ValueError:
                return self.json(
                    {"error": "invalid JSON body"}, status_code=400
                )
        channel_id = body.get("channel_id")
        title = body.get("title")
        if not channel_id or not title:
            return self.json(
                {"error": "channel_id and title are required"},
                status_code=400,
            )
        tags = body.get("tags") or []
        if not isinstance(tags, list):
            return self.json(
                {"error": "tags must be a list of tag names or ids"},
                status_code=400,
            )
        try:
            channel = await client.channel(str(channel_id))
            if channel.get("type") != 15:
                return self.json(
                    {
                        "error": "channel is not a forum channel "
                        f"(type {channel.get('type')})"
                    },
                    status_code=400,
                )
            tag_ids = client.resolve_tag_ids(channel, [str(t) for t in tags])
            if upload is not None:
                result = await client.create_forum_post(
                    channel_id=str(channel_id),
                    title=str(title),
                    content=body.get("content"),
                    applied_tags=tag_ids,
                    filename=upload.filename or "file",
                    data=upload.file.read(),
                    content_type=upload.content_type,
                )
            else:
                result = await client.create_forum_post(
                    channel_id=str(channel_id),
                    title=str(title),
                    content=body.get("content"),
                    applied_tags=tag_ids,
                )
        except DiscordError as err:
            return self._error_response(err)
        return self.json(result)


_VIEWS = (
    HealthView,
    PendingView,
    AckView,
    SendView,
    DirectHealthView,
    DirectChannelsView,
    DirectChannelView,
    DirectMessagesView,
    DirectNewView,
    DirectMarkReadView,
    DirectSendView,
    DirectEventsView,
    DirectPollView,
    DirectPollVotersView,
    DirectPollExpireView,
    DirectForumView,
    DirectForumPostsView,
)


def _parse_channel_ids(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Discord Bridge from a config entry (both modes optional)."""
    session = async_get_clientsession(hass)
    data = entry.data
    domain_data = hass.data.setdefault(DOMAIN, {})

    if data.get(CONF_BRIDGE_URL) and data.get(CONF_QUEUE_TOKEN):
        domain_data["bridge_client"] = DiscordBridgeClient(
            session=session,
            base_url=data[CONF_BRIDGE_URL],
            queue_token=data[CONF_QUEUE_TOKEN],
            author_id=data.get(CONF_AUTHOR_ID) or None,
        )
    else:
        domain_data.pop("bridge_client", None)

    if data.get(CONF_DISCORD_TOKEN):
        domain_data["discord_client"] = DiscordClient(
            session=session,
            bot_token=data[CONF_DISCORD_TOKEN],
            author_id=data.get(CONF_AUTHOR_ID) or None,
        )
    else:
        domain_data.pop("discord_client", None)

    domain_data["require_admin"] = data.get(
        CONF_REQUIRE_ADMIN, DEFAULT_REQUIRE_ADMIN
    )
    domain_data["guild_id"] = data.get(CONF_GUILD_ID) or None
    domain_data["channel_ids"] = _parse_channel_ids(data.get(CONF_CHANNEL_IDS))

    store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    domain_data["store"] = store
    stored = await store.async_load()
    domain_data["cursors"] = stored if isinstance(stored, dict) else {}

    if not domain_data.get("views_registered"):
        for view in _VIEWS:
            hass.http.register_view(view())
        domain_data["views_registered"] = True

    if not domain_data.get("bridge_client") and not domain_data.get(
        "discord_client"
    ):
        _LOGGER.error(
            "Discord Bridge integration has neither bridge settings nor a "
            "Discord bot token; all endpoints will return 503"
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry (views stay registered but return 503)."""
    domain_data = hass.data.get(DOMAIN, {})
    for key in ("bridge_client", "discord_client", "guild_id", "channel_ids"):
        domain_data.pop(key, None)
    return True
