"""Async client for the Discord Bridge add-on HTTP API.

This module intentionally has no Home Assistant imports so the queue
filtering / ack-guard logic can be unit tested outside Home Assistant.
It only relies on aiohttp, which Home Assistant itself provides.

Bridge API recap (from the add-on's bridge.py):
  GET  /health         no auth
  GET  /pending        header X-Queue-Token, returns {"messages": [...]}
  POST /ack            header X-Queue-Token, body {"ids": [queue_id, ...]}
  POST /send           header X-Queue-Token, body {"channel_id", "content",
                       "dedupe_id", "in_reply_to" (optional)}
Reading /pending does not remove anything; only /ack marks messages done.
"""

from __future__ import annotations

from typing import Any

import aiohttp


class BridgeError(Exception):
    """Error talking to the bridge.

    ``status`` is the HTTP status returned by the bridge, or None when the
    bridge could not be reached at all.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DiscordBridgeClient:
    """Client bound to one consumer.

    When ``author_id`` is set, the client only ever *sees* that Discord
    user's messages, and ``ack`` refuses to acknowledge anyone else's
    messages. This is what makes sharing one bridge instance between
    several assistants safe: the guard is enforced here, server-side
    (inside Home Assistant), not by the caller's discipline.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        queue_token: str,
        author_id: str | None = None,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._token = queue_token
        self._author_id = str(author_id) if author_id else None

    # -- helpers ---------------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Queue-Token": self._token}

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            async with self._session.request(
                method,
                f"{self._base_url}{path}",
                timeout=aiohttp.ClientTimeout(total=15),
                **kwargs,
            ) as resp:
                try:
                    payload = await resp.json(content_type=None)
                except Exception:
                    payload = {"raw": await resp.text()}
                if resp.status >= 400:
                    message = (
                        payload.get("error", resp.reason)
                        if isinstance(payload, dict)
                        else resp.reason
                    )
                    raise BridgeError(
                        f"bridge returned {resp.status}: {message}",
                        status=resp.status,
                    )
                return payload if isinstance(payload, dict) else {"data": payload}
        except BridgeError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BridgeError(f"cannot reach bridge: {err}", status=None) from err

    # -- filtering -------------------------------------------------------

    def filter_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep only this consumer's messages (all of them if unfiltered)."""
        if not self._author_id:
            return list(messages)
        return [
            msg
            for msg in messages
            if str(msg.get("author_id")) == self._author_id
        ]

    # -- bridge endpoints -------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """GET /health on the bridge (no token needed bridge-side)."""
        return await self._request("GET", "/health")

    async def pending_raw(self) -> list[dict[str, Any]]:
        """All queued messages, unfiltered."""
        payload = await self._request("GET", "/pending", headers=self._headers)
        messages = payload.get("messages", [])
        return messages if isinstance(messages, list) else []

    async def pending(self) -> list[dict[str, Any]]:
        """Queued messages for this consumer only."""
        return self.filter_messages(await self.pending_raw())

    async def ack(self, ids: list[int]) -> dict[str, Any]:
        """Acknowledge queue ids, refusing ids that are not ours.

        The bridge itself lets any token holder ack any id, so we first
        read the queue and intersect the requested ids with the ids this
        consumer is allowed to see. Ids that are unknown, already done,
        or belong to someone else are reported as ``skipped_ids`` and are
        never forwarded to the bridge.
        """
        requested: list[int] = []
        for value in ids:
            try:
                requested.append(int(value))
            except (TypeError, ValueError):
                continue
        visible = await self.pending()
        allowed = {int(msg["id"]) for msg in visible if "id" in msg}
        own = [queue_id for queue_id in requested if queue_id in allowed]
        skipped = [queue_id for queue_id in requested if queue_id not in allowed]
        acked = 0
        if own:
            payload = await self._request(
                "POST", "/ack", headers=self._headers, json={"ids": own}
            )
            acked = int(payload.get("acked", 0))
        return {"acked": acked, "skipped_ids": skipped}

    async def send(
        self,
        channel_id: str,
        content: str,
        dedupe_id: str,
        in_reply_to: str | None = None,
    ) -> dict[str, Any]:
        """POST /send on the bridge.

        ``dedupe_id`` is required by the bridge and is what stops the
        same reply being posted twice; callers should derive it from the
        message being answered (e.g. its discord_message_id).
        """
        if not channel_id or not content or not dedupe_id:
            raise BridgeError(
                "channel_id, content and dedupe_id are required", status=400
            )
        payload: dict[str, Any] = {
            "channel_id": str(channel_id),
            "content": content,
            "dedupe_id": str(dedupe_id),
        }
        if in_reply_to:
            payload["in_reply_to"] = str(in_reply_to)
        return await self._request(
            "POST", "/send", headers=self._headers, json=payload
        )
