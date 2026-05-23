"""Constants for the WEM webinterface integration."""

DOMAIN = "wem_webinterface"

CONF_BASE_URL = "base_url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_WAIT_SECONDS = "wait_seconds"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DISABLED_MENUS = "disabled_menus"
CONF_DISABLED_SUBMENUS = "disabled_submenus"
CONF_KNOWN_MENUS = "known_menus"
CONF_KNOWN_SUBMENUS = "known_submenus"

DEFAULT_BASE_URL = "http://heizung.home"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = ""
DEFAULT_WAIT_SECONDS = 2.0
DEFAULT_SCAN_INTERVAL = 30

DEFAULT_MAX_HTTP_RETRIES = 4
DEFAULT_HTTP_TIMEOUT = 20
DEFAULT_LOGIN_ROUNDS = 5
DEFAULT_MAX_WRITE_RETRIES = 3

STORAGE_VERSION = 1
STORAGE_KEY = "wem_webinterface_state"

PLATFORMS = ["sensor", "number", "select", "text"]
