"""Constants for the Discord Bridge integration."""

DOMAIN = "discord_bridge"

# Bridge add-on (queue) mode
CONF_BRIDGE_URL = "bridge_url"
CONF_QUEUE_TOKEN = "queue_token"

# Direct Discord API mode
CONF_DISCORD_TOKEN = "discord_bot_token"
CONF_GUILD_ID = "guild_id"
CONF_CHANNEL_IDS = "channel_ids"

# Shared
CONF_AUTHOR_ID = "author_id"
CONF_REQUIRE_ADMIN = "require_admin"

DEFAULT_BRIDGE_URL = "http://127.0.0.1:8765"
DEFAULT_REQUIRE_ADMIN = True

STORAGE_KEY = f"{DOMAIN}_cursors"
STORAGE_VERSION = 1
