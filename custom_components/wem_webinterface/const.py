"""Constants for WEM Web Interface integration."""

DOMAIN = "wem_webinterface"

CONF_IP_ADDRESS = "ip_address"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_ENTRIES = "entries"
CONF_CYCLE_INTERVAL = "cycle_interval"
CONF_RETRY_INTERVAL = "retry_interval"
CONF_MAX_RETRIES = "max_retries"

DEFAULT_CYCLE_INTERVAL = 20    # seconds between polls
DEFAULT_RETRY_INTERVAL = 5     # seconds before retry on incomplete page
DEFAULT_MAX_RETRIES = 3        # max retries for incomplete pages
DEFAULT_MAX_WRITE_RETRIES = 3  # max retries for write verification

PLATFORMS = ["sensor", "number", "select"]
