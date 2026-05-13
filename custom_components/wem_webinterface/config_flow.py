"""Config flow for WEM Web Interface."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_CYCLE_INTERVAL,
    CONF_ENTRIES,
    CONF_INIT_SCAN_INTERVAL,
    CONF_INIT_SCAN_MAX_ENTRIES,
    CONF_INIT_SCAN_NOW,
    CONF_IP_ADDRESS,
    CONF_MAX_RETRIES,
    CONF_PASSWORD,
    CONF_RETRY_INTERVAL,
    CONF_USERNAME,
    DEFAULT_CYCLE_INTERVAL,
    DEFAULT_INIT_SCAN_INTERVAL,
    DEFAULT_INIT_SCAN_MAX_ENTRIES,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_INTERVAL,
    DOMAIN,
)
from .coordinator import WemCoordinator, _parse_entries

_LOGGER = logging.getLogger(__name__)


def _safe_int(value: Any, default: int) -> int:
    """Best-effort int conversion for persisted options values."""
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


class WemConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        if user_input is not None:
            return self.async_create_entry(
                title=f"WEM {user_input[CONF_IP_ADDRESS]}",
                data=user_input,
            )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_IP_ADDRESS, default="192.168.179.36"): str,
                vol.Required(CONF_USERNAME, default="admin"): str,
                vol.Required(CONF_PASSWORD): str,
                # One stack entry per line (each line may contain comma-separated IDs)
                vol.Optional(CONF_ENTRIES, default=""): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors={},
            description_placeholders={
                "entries_help": (
                    "One stack per line. Example:\n"
                    "330000010000000000800070CF010002000301,330026000000000000800070CF020003000401\n"
                    "060000010000000000800070CF010011000301"
                )
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return WemOptionsFlow(config_entry)


class WemOptionsFlow(config_entries.OptionsFlow):
    """Handle options (cycle interval, retry settings)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        errors: Dict[str, str] = {}

        if user_input is not None:
            entries_raw = user_input.get(CONF_ENTRIES, "")
            if not entries_raw.strip():
                errors[CONF_ENTRIES] = "entries_empty"
            else:
                if user_input.get(CONF_INIT_SCAN_NOW, False):
                    try:
                        discovered_entries = await self._run_initialization_scan(user_input)
                        user_input[CONF_ENTRIES] = "\n".join(discovered_entries)
                    except Exception as exc:
                        _LOGGER.error("Initialization scan from options failed: %s", exc)
                        errors["base"] = "init_scan_failed"

                # The one-click action should not remain enabled in saved options.
                user_input[CONF_INIT_SCAN_NOW] = False

            if not errors:
                return self.async_create_entry(title="", data=user_input)

        opts = self.config_entry.options
        data = self.config_entry.data
        entries_default = str(opts.get(CONF_ENTRIES, data.get(CONF_ENTRIES, "")) or "")
        options_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CYCLE_INTERVAL,
                    default=_safe_int(
                        opts.get(CONF_CYCLE_INTERVAL, DEFAULT_CYCLE_INTERVAL),
                        DEFAULT_CYCLE_INTERVAL,
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=3600)),
                vol.Optional(
                    CONF_RETRY_INTERVAL,
                    default=_safe_int(
                        opts.get(CONF_RETRY_INTERVAL, DEFAULT_RETRY_INTERVAL),
                        DEFAULT_RETRY_INTERVAL,
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
                vol.Optional(
                    CONF_MAX_RETRIES,
                    default=_safe_int(
                        opts.get(CONF_MAX_RETRIES, DEFAULT_MAX_RETRIES),
                        DEFAULT_MAX_RETRIES,
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
                vol.Optional(
                    CONF_ENTRIES,
                    default=entries_default,
                ): str,
                vol.Optional(
                    CONF_INIT_SCAN_NOW,
                    default=False,
                ): bool,
                vol.Optional(
                    CONF_INIT_SCAN_INTERVAL,
                    default=_safe_int(
                        opts.get(CONF_INIT_SCAN_INTERVAL, DEFAULT_INIT_SCAN_INTERVAL),
                        DEFAULT_INIT_SCAN_INTERVAL,
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=300)),
                vol.Optional(
                    CONF_INIT_SCAN_MAX_ENTRIES,
                    default=_safe_int(
                        opts.get(CONF_INIT_SCAN_MAX_ENTRIES, DEFAULT_INIT_SCAN_MAX_ENTRIES),
                        DEFAULT_INIT_SCAN_MAX_ENTRIES,
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=5000)),
            }
        )

        return self.async_show_form(step_id="init", data_schema=options_schema, errors=errors)

    async def _run_initialization_scan(self, user_input: Dict[str, Any]) -> list[str]:
        """Run full recursive scan once and return updated entries list."""
        data = self.config_entry.data
        opts = self.config_entry.options

        temp_coordinator = WemCoordinator(
            ip_address=data[CONF_IP_ADDRESS],
            username=data[CONF_USERNAME],
            password=data[CONF_PASSWORD],
            entries=_parse_entries(user_input.get(CONF_ENTRIES, "")),
            cycle_interval=_safe_int(
                user_input.get(CONF_CYCLE_INTERVAL, opts.get(CONF_CYCLE_INTERVAL, DEFAULT_CYCLE_INTERVAL)),
                DEFAULT_CYCLE_INTERVAL,
            ),
            retry_interval=_safe_int(
                user_input.get(CONF_RETRY_INTERVAL, opts.get(CONF_RETRY_INTERVAL, DEFAULT_RETRY_INTERVAL)),
                DEFAULT_RETRY_INTERVAL,
            ),
            max_retries=_safe_int(
                user_input.get(CONF_MAX_RETRIES, opts.get(CONF_MAX_RETRIES, DEFAULT_MAX_RETRIES)),
                DEFAULT_MAX_RETRIES,
            ),
            hass=None,
            config_entry=self.config_entry,
        )

        interval = _safe_int(user_input.get(CONF_INIT_SCAN_INTERVAL), DEFAULT_INIT_SCAN_INTERVAL)
        max_entries = _safe_int(
            user_input.get(CONF_INIT_SCAN_MAX_ENTRIES),
            DEFAULT_INIT_SCAN_MAX_ENTRIES,
        )

        try:
            await temp_coordinator._create_session()
            await temp_coordinator._check_ip_reachability()
            await temp_coordinator._check_web_port_reachability()
            await temp_coordinator._login()
            await temp_coordinator.async_initialize_entries(
                scan_interval_seconds=interval,
                max_entries=max_entries,
            )
            return temp_coordinator.entries
        finally:
            await temp_coordinator.async_teardown()
