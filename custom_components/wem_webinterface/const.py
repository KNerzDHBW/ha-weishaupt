"""Constants for WEM Web Interface integration."""

DOMAIN = "wem_webinterface"

CONF_IP_ADDRESS = "ip_address"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_ENTRIES = "entries"
CONF_CYCLE_INTERVAL = "cycle_interval"
CONF_RETRY_INTERVAL = "retry_interval"
CONF_MAX_RETRIES = "max_retries"
CONF_INIT_SCAN_NOW = "init_scan_now"
CONF_INIT_SCAN_INTERVAL = "init_scan_interval"
CONF_INIT_SCAN_MAX_ENTRIES = "init_scan_max_entries"

DEFAULT_CYCLE_INTERVAL = 20    # seconds between polls
DEFAULT_RETRY_INTERVAL = 5     # seconds before retry on incomplete page
DEFAULT_MAX_RETRIES = 3        # max retries for incomplete pages
DEFAULT_MAX_WRITE_RETRIES = 3  # max retries for write verification
DEFAULT_INIT_SCAN_INTERVAL = 5
DEFAULT_INIT_SCAN_MAX_ENTRIES = 500

PLATFORMS = ["sensor", "number", "select"]
