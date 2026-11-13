"""Async client for the Discord REST API (direct connection mode).

No Home Assistant imports, only aiohttp, so it can be unit tested with a
stub Discord API server. Only REST is used, never a Gateway connection,
so sharing the bot token with the Discord Bridge add-on (which owns the
Gateway session) does not cause a double login.

Discord REST recap (API v10):
  GET  /users/@me
  GET  /users/@me/guilds
  GET  /guilds/{guild_id}/channels
  GET  /channels/{channel_id}/messages?limit=&after=&before=
       -> array, newest first, full message objects (attachments included)
  POST /channels/{channel_id}/messages {content, message_reference?}
       With a file: multipart/form-data with payload_json + files[0]
       (Discord's bot upload limit is 25 MB).
  GET  /guilds/{guild_id}/scheduled-events
  POST /guilds/{guild_id}/scheduled-events
       {name, privacy_level: 2, scheduled_start_time,
        scheduled_end_time, entity_type, entity_metadata, description}
       External events (entity_type 3) require a location and an end time
       and the bot needs the Manage Events permission.
  GET  /channels/{channel_id}
       -> channel object; forum channels (type 15) carry available_tags
  POST /channels/{forum_channel_id}/threads
       {name, message: {content, attachments?}, applied_tags: [tag_id]}
       Creates a forum post (a thread with a starter message). With a
       file it is multipart/form-data, payload_json + files[0], exactly
       like a message upload. Tags are referenced by id (max 5 per post).
       An existing text channel cannot be converted to a forum; the
       forum itself must be created as a new channel in Discord.
  GET  /guilds/{guild_id}/threads/active
       -> {threads: [...]}; forum posts are the threads whose
          parent_id is the forum channel.
  GET  /channels/{forum_channel_id}/threads/archived/public
       -> {threads: [...], has_more: bool}
  POST /guilds/{guild_id}/channels
       {name, type, parent_id?, topic?, available_tags?}
       Creates a channel (type 15 creates a forum). Requires the
       Manage Channels permission; Discord returns 403 without it.
  PATCH /channels/{channel_id}
       {name?, topic?, parent_id?, position?, available_tags?, ...}
       Modifies a channel (rename, retopic, move, retag a forum).
       Also requires Manage Channels.
  POST /channels/{channel_id}/messages with a ``poll`` object
       {question: {text}, answers: [{poll_media: {text, emoji?}}],
        duration (hours, 1-768, default 24), allow_multiselect,
        layout_type: 1}
       Creates a poll. Question <= 300 chars, 1-10 answers of <= 55
       chars. A poll cannot share a message with a file attachment,
       and it cannot be edited afterwards. Needs the Send Polls
       permission in Discord.
  GET  /channels/{channel_id}/messages/{message_id}
       -> the message; its poll carries results (answer_counts,
          is_finalized) once votes come in / after expiry.
  GET  /channels/{channel_id}/polls/{message_id}/answers/{answer_id}
       -> {users: [...]} who voted for that answer.
  POST /channels/{channel_id}/polls/{message_id}/expire
       Ends the bot's own poll early and returns the message.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

API_BASE = "https://discord.com/api/v10"
MAX_FILE_BYTES = 25 * 1024 * 1024  # Discord bot upload limit


class DiscordError(Exception):
    """Error from the Discord API.

    ``status`` is the HTTP status, or None when Discord was unreachable.
    ``retry_after`` is set on 429 responses.
    """

    def __init__(
        self, message: str, status: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def normalize_message(raw: dict[str, Any]) -> dict[str, Any]:
    """Trim a raw Discord message object to the fields consumers need."""
    author = raw.get("author") or {}
    attachments = []
    for att in raw.get("attachments") or []:
        attachments.append(
            {
                "id": att.get("id"),
                "filename": att.get("filename"),
                "url": att.get("url"),
                "proxy_url": att.get("proxy_url"),
                "content_type": att.get("content_type"),
                "size": att.get("size"),
                "width": att.get("width"),
                "height": att.get("height"),
            }
        )
    stickers = []
    for sticker in raw.get("stickers") or raw.get("sticker_items") or []:
        stickers.append(
            {
                "id": sticker.get("id"),
                "name": sticker.get("name"),
                "format_type": sticker.get("format_type"),
            }
        )
    referenced = raw.get("referenced_message") or {}
    return {
        "id": raw.get("id"),
        "channel_id": raw.get("channel_id"),
        "guild_id": raw.get("guild_id"),
        "author_id": author.get("id"),
        "author_name": author.get("username"),
        "author_global_name": author.get("global_name"),
        "author_bot": author.get("bot", False),
        "content": raw.get("content", ""),
        "timestamp": raw.get("timestamp"),
        "edited_timestamp": raw.get("edited_timestamp"),
        "attachments": attachments,
        "embeds": raw.get("embeds") or [],
        "stickers": stickers,
        "mention_ids": [m.get("id") for m in raw.get("mentions") or []],
        "reply_to": referenced.get("id"),
        "pinned": raw.get("pinned", False),
        "poll": normalize_poll(raw.get("poll")),
    }


def normalize_poll(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Trim a poll object, including live/final results when present."""
    if not raw:
        return None
    question = raw.get("question") or {}
    answers = []
    for ans in raw.get("answers") or []:
        media = ans.get("poll_media") or {}
        emoji = media.get("emoji") or {}
        answers.append(
            {
                "answer_id": ans.get("answer_id"),
                "text": media.get("text"),
                "emoji_name": emoji.get("name"),
                "emoji_id": emoji.get("id"),
            }
        )
    results = raw.get("results")
    normalized_results = None
    if results is not None:
        counts = results.get("answer_counts") or []
        normalized_results = {
            "is_finalized": results.get("is_finalized", False),
            "total_votes": sum(int(c.get("count") or 0) for c in counts),
            "answer_counts": [
                {
                    "answer_id": c.get("id"),
                    "count": c.get("count"),
                    "me_voted": c.get("me_voted", False),
                }
                for c in counts
            ],
        }
    return {
        "question": question.get("text"),
        "answers": answers,
        "expiry": raw.get("expiry"),
        "allow_multiselect": raw.get("allow_multiselect", False),
        "layout_type": raw.get("layout_type"),
        "results": normalized_results,
    }


def normalize_tag(raw: dict[str, Any]) -> dict[str, Any]:
    """Trim a forum tag object."""
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "emoji_name": raw.get("emoji_name"),
        "emoji_id": raw.get("emoji_id"),
        "moderated": raw.get("moderated", False),
    }


def normalize_channel(raw: dict[str, Any]) -> dict[str, Any]:
    """Trim a channel object; forum channels include their tags."""
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "type": raw.get("type"),
        "parent_id": raw.get("parent_id"),
        "topic": raw.get("topic"),
        "available_tags": [
            normalize_tag(t) for t in raw.get("available_tags") or []
        ],
    }


def normalize_thread(raw: dict[str, Any]) -> dict[str, Any]:
    """Trim a thread object (a forum post is a thread)."""
    metadata = raw.get("thread_metadata") or {}
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "parent_id": raw.get("parent_id"),
        "guild_id": raw.get("guild_id"),
        "owner_id": raw.get("owner_id"),
        "applied_tags": raw.get("applied_tags") or [],
        "message_count": raw.get("message_count"),
        "member_count": raw.get("member_count"),
        "archived": metadata.get("archived", raw.get("archived", False)),
        "locked": metadata.get("locked", raw.get("locked", False)),
    }


def normalize_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Trim a raw Discord scheduled event object."""
    metadata = raw.get("entity_metadata") or {}
    return {
        "id": raw.get("id"),
        "guild_id": raw.get("guild_id"),
        "name": raw.get("name"),
        "description": raw.get("description"),
        "scheduled_start_time": raw.get("scheduled_start_time"),
        "scheduled_end_time": raw.get("scheduled_end_time"),
        "location": metadata.get("location"),
        "status": raw.get("status"),
        "entity_type": raw.get("entity_type"),
    }


class DiscordClient:
    """REST client bound to one bot and (optionally) one author filter."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        bot_token: str,
        author_id: str | None = None,
        api_base: str = API_BASE,
    ) -> None:
        self._session = session
        self._token = bot_token
        self._author_id = str(author_id) if author_id else None
        self._base = api_base.rstrip("/")

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self._token}"}

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: Any = None,
    ) -> Any:
        try:
            async with self._session.request(
                method,
                f"{self._base}{path}",
                headers=self._headers,
                params=params,
                json=json,
                data=data,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                try:
                    payload = await resp.json(content_type=None)
                except Exception:
                    payload = None
                if resp.status >= 400:
                    message = (
                        payload.get("message", resp.reason)
                        if isinstance(payload, dict)
                        else resp.reason
                    )
                    retry_after = (
                        payload.get("retry_after")
                        if isinstance(payload, dict)
                        else None
                    )
                    raise DiscordError(
                        f"discord returned {resp.status}: {message}",
                        status=resp.status,
                        retry_after=retry_after,
                    )
                return payload
        except DiscordError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise DiscordError(f"cannot reach discord: {err}", status=None) from err

    # -- identity / structure ---------------------------------------------

    async def me(self) -> dict[str, Any]:
        payload = await self._request("GET", "/users/@me")
        return {
            "id": payload.get("id"),
            "username": payload.get("username"),
            "bot": payload.get("bot", True),
        }

    async def guilds(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/users/@me/guilds")
        return [
            {"id": g.get("id"), "name": g.get("name")}
            for g in payload or []
        ]

    async def channels(self, guild_id: str) -> list[dict[str, Any]]:
        payload = await self._request("GET", f"/guilds/{guild_id}/channels")
        return [normalize_channel(c) for c in payload or []]

    async def channel(self, channel_id: str) -> dict[str, Any]:
        """One channel; for a forum this includes its available_tags."""
        payload = await self._request("GET", f"/channels/{channel_id}")
        return normalize_channel(payload or {})

    # Channel types a caller may create (text, voice, category,
    # announcement, stage, forum, media).
    CREATABLE_TYPES = (0, 2, 4, 5, 13, 15, 16)

    @staticmethod
    def _tag_for_write(tag: Any, keep_id: bool = False) -> dict[str, Any]:
        """Normalise one tag for a create/modify request."""
        if isinstance(tag, str):
            return {"name": tag}
        if not isinstance(tag, dict) or not tag.get("name"):
            raise DiscordError(
                "each tag needs at least a name", status=400
            )
        item: dict[str, Any] = {"name": str(tag["name"])}
        if keep_id and tag.get("id"):
            item["id"] = str(tag["id"])
        if tag.get("emoji_name"):
            item["emoji_name"] = tag["emoji_name"]
        if tag.get("emoji_id"):
            item["emoji_id"] = tag["emoji_id"]
        if "moderated" in tag:
            item["moderated"] = bool(tag["moderated"])
        return item

    async def create_channel(
        self,
        guild_id: str,
        name: str,
        type: int = 0,
        parent_id: str | None = None,
        topic: str | None = None,
        available_tags: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Create a channel in the guild (type 15 creates a forum).

        Needs the bot's Manage Channels permission in Discord; without
        it Discord answers 403 and that error is passed through.
        """
        if not name:
            raise DiscordError("name is required", status=400)
        if int(type) not in self.CREATABLE_TYPES:
            raise DiscordError(
                f"channel type {type} cannot be created here",
                status=400,
            )
        tags: list[dict[str, Any]] | None = None
        if available_tags is not None:
            if int(type) != 15:
                raise DiscordError(
                    "available_tags only apply to forum channels (type 15)",
                    status=400,
                )
            if len(available_tags) > 20:
                raise DiscordError(
                    "a forum can have at most 20 tags", status=400
                )
            tags = [self._tag_for_write(t) for t in available_tags]
        body: dict[str, Any] = {"name": name, "type": int(type)}
        if parent_id:
            body["parent_id"] = str(parent_id)
        if topic:
            body["topic"] = topic
        if tags is not None:
            body["available_tags"] = tags
        payload = await self._request(
            "POST", f"/guilds/{guild_id}/channels", json=body
        )
        return normalize_channel(payload or {})

    async def edit_channel(
        self,
        channel_id: str,
        name: str | None = None,
        topic: str | None = None,
        parent_id: str | None = None,
        position: int | None = None,
        available_tags: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Modify a channel: rename, topic, parent, position, tags.

        Only the fields given are sent. For forum tags, pass the full
        desired list; entries that keep their ``id`` stay the same tag,
        entries without one are created (see set_forum_tags for the
        name-based convenience version).
        """
        body: dict[str, Any] = {}
        if name is not None:
            if not name:
                raise DiscordError("name cannot be empty", status=400)
            body["name"] = name
        if topic is not None:
            body["topic"] = topic
        if parent_id is not None:
            body["parent_id"] = str(parent_id)
        if position is not None:
            body["position"] = int(position)
        if available_tags is not None:
            if len(available_tags) > 20:
                raise DiscordError(
                    "a forum can have at most 20 tags", status=400
                )
            body["available_tags"] = [
                self._tag_for_write(t, keep_id=True)
                for t in available_tags
            ]
        if not body:
            raise DiscordError(
                "nothing to change: give at least one field", status=400
            )
        payload = await self._request(
            "PATCH", f"/channels/{channel_id}", json=body
        )
        return normalize_channel(payload or {})

    async def set_forum_tags(
        self, channel_id: str, tags: list[Any]
    ) -> dict[str, Any]:
        """Replace a forum's tags by name, preserving existing tags.

        A requested tag whose name already exists keeps its id and
        emoji, so posts that already use it are unaffected; new names
        become new tags, and tags not requested are removed.
        """
        channel = await self.channel(channel_id)
        if channel.get("type") != 15:
            raise DiscordError(
                "channel is not a forum channel "
                f"(type {channel.get('type')})",
                status=400,
            )
        existing = {
            str(t.get("name")): t
            for t in channel.get("available_tags") or []
        }
        merged: list[dict[str, Any]] = []
        for tag in tags:
            name = tag if isinstance(tag, str) else (tag or {}).get("name")
            if name in existing:
                merged.append(existing[str(name)])
            else:
                merged.append(tag)
        return await self.edit_channel(
            channel_id, available_tags=merged
        )

    async def forum_tags(self, channel_id: str) -> list[dict[str, Any]]:
        channel = await self.channel(channel_id)
        return channel.get("available_tags") or []

    @staticmethod
    def resolve_tag_ids(
        channel: dict[str, Any], tags: list[str]
    ) -> list[str]:
        """Resolve tag names (or ids) to tag ids for one forum channel.

        Raises DiscordError (400) naming the unknown tags, so callers do
        not silently post without the intended categorisation.
        """
        available = channel.get("available_tags") or []
        by_name = {str(t.get("name")): str(t.get("id")) for t in available}
        by_id = {str(t.get("id")): str(t.get("id")) for t in available}
        resolved: list[str] = []
        unknown: list[str] = []
        for tag in tags:
            key = str(tag)
            if key in by_id:
                resolved.append(by_id[key])
            elif key in by_name:
                resolved.append(by_name[key])
            else:
                unknown.append(key)
        if unknown:
            names = ", ".join(sorted(by_name)) or "(none)"
            raise DiscordError(
                f"unknown forum tag(s): {', '.join(unknown)}; "
                f"available tags: {names}",
                status=400,
            )
        return resolved

    # -- messages -----------------------------------------------------------

    async def messages(
        self,
        channel_id: str,
        limit: int = 50,
        after: str | None = None,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        """One page of channel history, oldest first."""
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
        if after:
            params["after"] = str(after)
        if before:
            params["before"] = str(before)
        payload = await self._request(
            "GET", f"/channels/{channel_id}/messages", params=params
        )
        items = [normalize_message(m) for m in payload or []]
        items.reverse()  # Discord returns newest first
        return items

    async def new_messages(
        self, channel_id: str, after_id: str | None, max_total: int = 300
    ) -> list[dict[str, Any]]:
        """All messages after ``after_id`` (up to max_total), oldest first."""
        collected: list[dict[str, Any]] = []
        cursor = after_id
        while len(collected) < max_total:
            batch = await self.messages(
                channel_id,
                limit=min(100, max_total - len(collected)),
                after=cursor,
            )
            if not batch:
                break
            collected.extend(batch)
            cursor = batch[-1]["id"]
            if len(batch) < 100:
                break
        return collected

    def filter_author(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not self._author_id:
            return list(messages)
        return [m for m in messages if str(m.get("author_id")) == self._author_id]

    async def send(
        self,
        channel_id: str,
        content: str,
        in_reply_to: str | None = None,
    ) -> dict[str, Any]:
        if not content:
            raise DiscordError("content is required", status=400)
        body: dict[str, Any] = {"content": content}
        if in_reply_to:
            body["message_reference"] = {"message_id": str(in_reply_to)}
        payload = await self._request(
            "POST", f"/channels/{channel_id}/messages", json=body
        )
        return normalize_message(payload)

    async def send_file(
        self,
        channel_id: str,
        filename: str,
        data: bytes,
        content: str | None = None,
        content_type: str | None = None,
        in_reply_to: str | None = None,
    ) -> dict[str, Any]:
        """Post a message with one file attached (image, video, document).

        Discord takes file uploads as multipart/form-data: a
        ``payload_json`` part describing the message plus ``files[0]``.
        """
        if not filename:
            raise DiscordError("filename is required", status=400)
        if not data:
            raise DiscordError("file data is empty", status=400)
        if len(data) > MAX_FILE_BYTES:
            raise DiscordError(
                f"file exceeds the Discord limit of {MAX_FILE_BYTES} bytes",
                status=413,
            )
        payload: dict[str, Any] = {
            "attachments": [{"id": 0, "filename": filename}]
        }
        if content:
            payload["content"] = content
        if in_reply_to:
            payload["message_reference"] = {"message_id": str(in_reply_to)}
        form = aiohttp.FormData()
        form.add_field(
            "payload_json", json.dumps(payload), content_type="application/json"
        )
        form.add_field(
            "files[0]",
            data,
            filename=filename,
            content_type=content_type or "application/octet-stream",
        )
        result = await self._request(
            "POST", f"/channels/{channel_id}/messages", data=form
        )
        return normalize_message(result)

    # -- forum posts --------------------------------------------------------

    async def create_forum_post(
        self,
        channel_id: str,
        title: str,
        content: str | None = None,
        applied_tags: list[str] | None = None,
        filename: str | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Create one forum post (thread) in a forum channel.

        ``applied_tags`` holds tag *ids* (see resolve_tag_ids); Discord
        allows at most 5 per post. A file can be attached to the starter
        message, uploaded the same multipart way as a message file.
        """
        if not title:
            raise DiscordError("title is required", status=400)
        tags = [str(t) for t in applied_tags or []]
        if len(tags) > 5:
            raise DiscordError(
                "a forum post can have at most 5 tags", status=400
            )
        if data is not None:
            if not filename:
                raise DiscordError("filename is required", status=400)
            if not data:
                raise DiscordError("file data is empty", status=400)
            if len(data) > MAX_FILE_BYTES:
                raise DiscordError(
                    f"file exceeds the Discord limit of {MAX_FILE_BYTES} bytes",
                    status=413,
                )
            message: dict[str, Any] = {
                "attachments": [{"id": 0, "filename": filename}]
            }
            if content:
                message["content"] = content
            payload = {
                "name": title,
                "message": message,
                "applied_tags": tags,
            }
            form = aiohttp.FormData()
            form.add_field(
                "payload_json",
                json.dumps(payload),
                content_type="application/json",
            )
            form.add_field(
                "files[0]",
                data,
                filename=filename,
                content_type=content_type or "application/octet-stream",
            )
            result = await self._request(
                "POST", f"/channels/{channel_id}/threads", data=form
            )
            return normalize_thread(result or {})
        if not content:
            raise DiscordError(
                "content is required when no file is attached", status=400
            )
        body: dict[str, Any] = {
            "name": title,
            "message": {"content": content},
            "applied_tags": tags,
        }
        result = await self._request(
            "POST", f"/channels/{channel_id}/threads", json=body
        )
        return normalize_thread(result or {})

    async def list_forum_posts(
        self, guild_id: str, channel_id: str
    ) -> list[dict[str, Any]]:
        """Active plus publicly archived posts of one forum channel."""
        collected: dict[str, dict[str, Any]] = {}
        payload = await self._request(
            "GET", f"/guilds/{guild_id}/threads/active"
        )
        for thread in (payload or {}).get("threads") or []:
            if str(thread.get("parent_id")) == str(channel_id):
                item = normalize_thread(thread)
                collected[str(item["id"])] = item
        try:
            archived = await self._request(
                "GET", f"/channels/{channel_id}/threads/archived/public"
            )
        except DiscordError:
            archived = None
        for thread in (archived or {}).get("threads") or []:
            item = normalize_thread(thread)
            collected.setdefault(str(item["id"]), item)
        return sorted(
            collected.values(), key=lambda t: int(t["id"] or 0)
        )

    # -- polls --------------------------------------------------------------

    async def send_poll(
        self,
        channel_id: str,
        question: str,
        answers: list[Any],
        duration_hours: int = 24,
        allow_multiselect: bool = False,
        content: str | None = None,
    ) -> dict[str, Any]:
        """Post a poll in a text channel.

        ``answers`` items are plain strings or dicts
        ``{"text": ..., "emoji_name": ...}`` (a custom emoji id can be
        given as ``emoji_id`` instead). Discord limits: question
        <= 300 chars, 1-10 answers of <= 55 chars, duration 1-768
        hours. The bot needs the Send Polls permission.
        """
        if not question:
            raise DiscordError("question is required", status=400)
        if len(question) > 300:
            raise DiscordError(
                "question exceeds the Discord limit of 300 characters",
                status=400,
            )
        if not answers or len(answers) > 10:
            raise DiscordError(
                "a poll needs between 1 and 10 answers", status=400
            )
        try:
            duration = int(duration_hours)
        except (TypeError, ValueError):
            raise DiscordError(
                "duration_hours must be a number", status=400
            ) from None
        if duration < 1 or duration > 768:
            raise DiscordError(
                "duration_hours must be between 1 and 768", status=400
            )
        built_answers = []
        for answer in answers:
            if isinstance(answer, str):
                text, emoji_name, emoji_id = answer, None, None
            elif isinstance(answer, dict):
                text = answer.get("text")
                emoji_name = answer.get("emoji_name")
                emoji_id = answer.get("emoji_id")
            else:
                raise DiscordError(
                    "each answer must be a string or an object",
                    status=400,
                )
            if not text:
                raise DiscordError("each answer needs text", status=400)
            if len(str(text)) > 55:
                raise DiscordError(
                    "an answer exceeds the Discord limit of 55 characters",
                    status=400,
                )
            media: dict[str, Any] = {"text": str(text)}
            if emoji_id:
                media["emoji"] = {"id": str(emoji_id)}
            elif emoji_name:
                media["emoji"] = {"name": str(emoji_name)}
            built_answers.append({"poll_media": media})
        poll: dict[str, Any] = {
            "question": {"text": question},
            "answers": built_answers,
            "duration": duration,
            "allow_multiselect": bool(allow_multiselect),
            "layout_type": 1,
        }
        body: dict[str, Any] = {"poll": poll}
        if content:
            body["content"] = content
        payload = await self._request(
            "POST", f"/channels/{channel_id}/messages", json=body
        )
        return normalize_message(payload)

    async def get_message(
        self, channel_id: str, message_id: str
    ) -> dict[str, Any]:
        payload = await self._request(
            "GET", f"/channels/{channel_id}/messages/{message_id}"
        )
        return normalize_message(payload or {})

    async def poll_results(
        self, channel_id: str, message_id: str
    ) -> dict[str, Any]:
        """Current results of a poll (counts per answer, finalized?)."""
        message = await self.get_message(channel_id, message_id)
        if not message.get("poll"):
            raise DiscordError(
                "message is not a poll", status=400
            )
        return {
            "message_id": message["id"],
            "channel_id": message["channel_id"],
            "poll": message["poll"],
        }

    async def poll_voters(
        self,
        channel_id: str,
        message_id: str,
        answer_id: int | str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Users who voted for one answer of a poll."""
        params = {"limit": max(1, min(int(limit), 100))}
        payload = await self._request(
            "GET",
            f"/channels/{channel_id}/polls/{message_id}"
            f"/answers/{answer_id}",
            params=params,
        )
        return [
            {
                "id": u.get("id"),
                "username": u.get("username"),
                "global_name": u.get("global_name"),
            }
            for u in (payload or {}).get("users") or []
        ]

    async def expire_poll(
        self, channel_id: str, message_id: str
    ) -> dict[str, Any]:
        """End the bot's own poll now and return the final message."""
        payload = await self._request(
            "POST",
            f"/channels/{channel_id}/polls/{message_id}/expire",
        )
        return normalize_message(payload or {})

    # -- scheduled events ---------------------------------------------------

    async def list_events(self, guild_id: str) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET", f"/guilds/{guild_id}/scheduled-events"
        )
        return [normalize_event(e) for e in payload or []]

    async def create_event(
        self,
        guild_id: str,
        name: str,
        scheduled_start_time: str,
        scheduled_end_time: str | None = None,
        description: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        """Create an external scheduled event (for trips and outings).

        External events require a location and an end time in Discord.
        When no end time is given, one hour after the start is used.
        A location is mandatory here because these events represent
        real-world plans; without one Discord rejects the request.
        """
        if not name or not scheduled_start_time:
            raise DiscordError(
                "name and scheduled_start_time are required", status=400
            )
        if not location:
            raise DiscordError(
                "location is required for external events", status=400
            )
        if not scheduled_end_time:
            start = datetime.fromisoformat(
                scheduled_start_time.replace("Z", "+00:00")
            )
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            scheduled_end_time = (start + timedelta(hours=1)).isoformat()
        body: dict[str, Any] = {
            "name": name,
            "privacy_level": 2,
            "scheduled_start_time": scheduled_start_time,
            "scheduled_end_time": scheduled_end_time,
            "entity_type": 3,
            "entity_metadata": {"location": location},
        }
        if description:
            body["description"] = description
        payload = await self._request(
            "POST", f"/guilds/{guild_id}/scheduled-events", json=body
        )
        return normalize_event(payload)
