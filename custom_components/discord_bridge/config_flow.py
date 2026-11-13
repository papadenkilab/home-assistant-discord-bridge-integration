"""Config flow for the Discord Bridge integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .bridge_client import BridgeError, DiscordBridgeClient
from .const import (
    CONF_AUTHOR_ID,
    CONF_BRIDGE_URL,
    CONF_CHANNEL_IDS,
    CONF_DISCORD_TOKEN,
    CONF_GUILD_ID,
    CONF_QUEUE_TOKEN,
    CONF_REQUIRE_ADMIN,
    DEFAULT_BRIDGE_URL,
    DEFAULT_REQUIRE_ADMIN,
    DOMAIN,
)
from .discord_client import DiscordClient, DiscordError

_LOGGER = logging.getLogger(__name__)


def _schema(user_input: dict[str, Any] | None = None) -> vol.Schema:
    values = user_input or {}
    return vol.Schema(
        {
            vol.Optional(
                CONF_BRIDGE_URL,
                default=values.get(CONF_BRIDGE_URL, DEFAULT_BRIDGE_URL),
            ): str,
            vol.Optional(
                CONF_QUEUE_TOKEN, default=values.get(CONF_QUEUE_TOKEN, "")
            ): str,
            vol.Optional(
                CONF_DISCORD_TOKEN, default=values.get(CONF_DISCORD_TOKEN, "")
            ): str,
            vol.Optional(CONF_GUILD_ID, default=values.get(CONF_GUILD_ID, "")): str,
            vol.Optional(
                CONF_CHANNEL_IDS, default=values.get(CONF_CHANNEL_IDS, "")
            ): str,
            vol.Optional(
                CONF_AUTHOR_ID, default=values.get(CONF_AUTHOR_ID, "")
            ): str,
            vol.Optional(
                CONF_REQUIRE_ADMIN,
                default=values.get(CONF_REQUIRE_ADMIN, DEFAULT_REQUIRE_ADMIN),
            ): bool,
        }
    )


class DiscordBridgeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Discord Bridge."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if self.hass.config_entries.async_entries(DOMAIN):
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            queue_token = (user_input.get(CONF_QUEUE_TOKEN) or "").strip()
            discord_token = (user_input.get(CONF_DISCORD_TOKEN) or "").strip()

            if not queue_token and not discord_token:
                errors["base"] = "no_mode"

            if queue_token and not errors:
                bridge = DiscordBridgeClient(
                    session=session,
                    base_url=user_input.get(CONF_BRIDGE_URL) or DEFAULT_BRIDGE_URL,
                    queue_token=queue_token,
                )
                try:
                    await bridge.pending_raw()
                except BridgeError as err:
                    errors["base"] = (
                        "invalid_auth" if err.status in (401, 403) else "cannot_connect"
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Unexpected error validating bridge")
                    errors["base"] = "unknown"

            if discord_token and not errors:
                discord = DiscordClient(session=session, bot_token=discord_token)
                try:
                    await discord.me()
                except DiscordError as err:
                    errors["base"] = (
                        "invalid_auth_discord"
                        if err.status in (401, 403)
                        else "cannot_connect_discord"
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Unexpected error validating Discord token")
                    errors["base"] = "unknown"

            if not errors:
                data = {
                    CONF_BRIDGE_URL: (
                        user_input.get(CONF_BRIDGE_URL) or DEFAULT_BRIDGE_URL
                    ).rstrip("/"),
                    CONF_QUEUE_TOKEN: queue_token,
                    CONF_DISCORD_TOKEN: discord_token,
                    CONF_GUILD_ID: (user_input.get(CONF_GUILD_ID) or "").strip(),
                    CONF_CHANNEL_IDS: (user_input.get(CONF_CHANNEL_IDS) or "").strip(),
                    CONF_AUTHOR_ID: (user_input.get(CONF_AUTHOR_ID) or "").strip(),
                    CONF_REQUIRE_ADMIN: user_input.get(
                        CONF_REQUIRE_ADMIN, DEFAULT_REQUIRE_ADMIN
                    ),
                }
                return self.async_create_entry(title="Discord Bridge", data=data)

        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input), errors=errors
        )
