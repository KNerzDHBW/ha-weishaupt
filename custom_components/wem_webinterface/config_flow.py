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


def _format_scan_summary(result: Dict[str, Any]) -> str:
    """Render compact scan details for config flow summary screens."""
    details = result.get("details") or []
    if not details:
        return "No detailed menu information available."

    lines: list[str] = []
    for item in details[:80]:
        stack = str(item.get("stack", ""))
        menu = str(item.get("menu", "")).strip() or "(unknown)"
        status = str(item.get("status", "unknown"))
        parsed = int(item.get("parsed_params", 0) or 0)
        nested = int(item.get("found_nested", 0) or 0)

        if menu.startswith("("):
            label = stack[:42] + ("..." if len(stack) > 42 else "")
        else:
            label = menu[:60]

        lines.append(f"{status.upper():<7} parsed={parsed:<3} nested={nested:<3} menu={label}")
    if len(details) > 80:
        lines.append(f"... and {len(details) - 80} more")
    return "\n".join(lines)


class WemConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._pending_user_input: Optional[Dict[str, Any]] = None
        self._autoscan_task: Optional[Any] = None
        self._autoscan_result: Optional[Dict[str, Any]] = None
        self._autoscan_error: Optional[Exception] = None
        self._autoscan_progress: Dict[str, Any] = {
            "root_total": 0,
            "root_done": 0,
            "root_current_index": 0,
            "root_current_stack": "",
            "root_current_menu": "",
            "processed": 0,
        }

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        errors: Dict[str, str] = {}

        if user_input is not None:
            self._pending_user_input = dict(user_input)
            self._autoscan_result = None
            self._autoscan_error = None
            self._autoscan_progress = {
                "root_total": 0,
                "root_done": 0,
                "root_current_index": 0,
                "root_current_stack": "",
                "root_current_menu": "",
                "processed": 0,
            }

            try:
                self._autoscan_task = self.hass.async_create_task(
                    self._run_initial_autoscan(self._pending_user_input)
                )
            except Exception as exc:
                _LOGGER.error("Failed to start automatic initialization scan: %s", exc)
                errors["base"] = "init_scan_failed"
            else:
                return await self.async_step_autoscan()

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
            errors=errors,
            description_placeholders={
                "entries_help": (
                    "One stack per line. Example:\n"
                    "330000010000000000800070CF010002000301,330026000000000000800070CF020003000401\n"
                    "060000010000000000800070CF010011000301"
                )
            },
        )

    async def async_step_autoscan(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Show progress while automatic initialization scan runs."""
        if self._autoscan_task is None:
            return await self.async_step_user()

        if not self._autoscan_task.done():
            return self.async_show_progress(
                step_id="autoscan",
                progress_action="initial_autoscan",
                description_placeholders=self._autoscan_description_placeholders(),
                progress_task=self._autoscan_task,
            )

        try:
            self._autoscan_result = self._autoscan_task.result()
            return self.async_show_progress_done(next_step_id="autoscan_summary")
        except Exception as exc:
            self._autoscan_error = exc
            _LOGGER.error("Automatic initialization scan failed: %s", exc)
            return self.async_show_progress_done(next_step_id="autoscan_failed")

    async def async_step_autoscan_failed(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Handle autoscan failure by allowing setup without autoscan result."""
        if user_input is not None and self._pending_user_input is not None:
            return self.async_create_entry(
                title=f"WEM {self._pending_user_input[CONF_IP_ADDRESS]}",
                data=self._pending_user_input,
            )

        return self.async_show_form(
            step_id="autoscan_failed",
            data_schema=vol.Schema({}),
            errors={"base": "init_scan_failed"},
        )

    async def async_step_autoscan_summary(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Show autoscan summary and finalize config entry."""
        if self._pending_user_input is None:
            return await self.async_step_user()

        if user_input is not None:
            return self.async_create_entry(
                title=f"WEM {self._pending_user_input[CONF_IP_ADDRESS]}",
                data=self._pending_user_input,
            )

        return self.async_show_form(
            step_id="autoscan_summary",
            data_schema=vol.Schema({}),
            description_placeholders={
                "summary": _format_scan_summary(self._autoscan_result or {}),
            },
        )

    def _autoscan_description_placeholders(self) -> Dict[str, str]:
        """Return placeholders for the progress text shown during autoscan."""
        total = int(self._autoscan_progress.get("root_total", 0) or 0)
        done = int(self._autoscan_progress.get("root_done", 0) or 0)
        current_index = int(self._autoscan_progress.get("root_current_index", 0) or 0)
        current_menu = str(self._autoscan_progress.get("root_current_menu", "") or "").strip()
        current_stack = str(self._autoscan_progress.get("root_current_stack", "") or "").strip()
        processed = int(self._autoscan_progress.get("processed", 0) or 0)

        if current_menu:
            label = current_menu[:70]
        elif current_stack:
            label = current_stack[:42] + ("..." if len(current_stack) > 42 else "")
        else:
            label = "(waiting for first menu)"

        return {
            "root_done": str(done),
            "root_total": str(max(total, 1)),
            "root_current_index": str(current_index if current_index > 0 else 1),
            "root_current_label": label,
            "processed": str(processed),
        }

    async def _on_autoscan_progress(self, progress: Dict[str, Any]) -> None:
        """Receive live scan progress and push it to the frontend."""
        self._autoscan_progress.update(progress)

        total = int(self._autoscan_progress.get("root_total", 0) or 0)
        done = int(self._autoscan_progress.get("root_done", 0) or 0)
        current_index = int(self._autoscan_progress.get("root_current_index", 0) or 0)

        ratio = 0.0
        if total > 0:
            in_flight = 1 if current_index > 0 and done < total else 0
            ratio = min(1.0, (done + in_flight * 0.5) / total)

        self.async_update_progress(ratio)
        self.async_notify_flow_changed()

    async def _run_initial_autoscan(self, user_input: Dict[str, Any]) -> Dict[str, Any]:
        """Run one automatic full scan during initial setup."""
        temp_coordinator = WemCoordinator(
            ip_address=user_input[CONF_IP_ADDRESS],
            username=user_input[CONF_USERNAME],
            password=user_input[CONF_PASSWORD],
            entries=_parse_entries(user_input.get(CONF_ENTRIES, "")),
            cycle_interval=DEFAULT_CYCLE_INTERVAL,
            retry_interval=DEFAULT_RETRY_INTERVAL,
            max_retries=DEFAULT_MAX_RETRIES,
            hass=None,
            config_entry=None,
        )

        try:
            await temp_coordinator._create_session()
            await temp_coordinator._check_ip_reachability()
            await temp_coordinator._check_web_port_reachability()
            await temp_coordinator._login()
            scan_result = await temp_coordinator.async_initialize_entries(
                scan_interval_seconds=5,
                max_entries=500,
                progress_callback=self._on_autoscan_progress,
            )
            user_input[CONF_ENTRIES] = "\n".join(temp_coordinator.entries)
            return scan_result
        finally:
            await temp_coordinator.async_teardown()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return WemOptionsFlow(config_entry)


class WemOptionsFlow(config_entries.OptionsFlow):
    """Handle options (cycle interval, retry settings)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry
        self._pending_options_input: Optional[Dict[str, Any]] = None
        self._manual_scan_task: Optional[Any] = None
        self._manual_scan_result: Optional[Dict[str, Any]] = None
        self._manual_scan_error: Optional[Exception] = None

    async def async_step_init(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        errors: Dict[str, str] = {}

        if user_input is not None:
            run_manual_scan = bool(user_input.get(CONF_INIT_SCAN_NOW, False))
            entries_raw = user_input.get(CONF_ENTRIES, "")

            if not entries_raw.strip() and not run_manual_scan:
                errors[CONF_ENTRIES] = "entries_empty"
            else:
                if run_manual_scan:
                    self._pending_options_input = dict(user_input)
                    self._manual_scan_result = None
                    self._manual_scan_error = None
                    try:
                        self._manual_scan_task = self.hass.async_create_task(
                            self._run_initialization_scan(self._pending_options_input)
                        )
                    except Exception as exc:
                        _LOGGER.error("Initialization scan from options failed to start: %s", exc)
                        errors["base"] = "init_scan_failed"
                    else:
                        return await self.async_step_manual_scan_progress()
                else:
                    # The one-click action should not remain enabled in saved options.
                    user_input[CONF_INIT_SCAN_NOW] = False
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

    async def async_step_manual_scan_progress(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Show progress while manual initialization scan runs from options."""
        if self._manual_scan_task is None:
            return await self.async_step_init()

        if not self._manual_scan_task.done():
            return self.async_show_progress(
                step_id="manual_scan_progress",
                progress_action="manual_init_scan",
                progress_task=self._manual_scan_task,
            )

        try:
            self._manual_scan_result = self._manual_scan_task.result()
            return self.async_show_progress_done(next_step_id="manual_scan_summary")
        except Exception as exc:
            self._manual_scan_error = exc
            _LOGGER.error("Initialization scan from options failed: %s", exc)
            return self.async_show_progress_done(next_step_id="manual_scan_failed")

    async def async_step_manual_scan_summary(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Show manual scan summary and persist options on confirmation."""
        if self._pending_options_input is None or self._manual_scan_result is None:
            return await self.async_step_init()

        if user_input is not None:
            data = dict(self._pending_options_input)
            data[CONF_ENTRIES] = "\n".join(self._manual_scan_result.get("entries", []))
            data[CONF_INIT_SCAN_NOW] = False
            return self.async_create_entry(title="", data=data)

        return self.async_show_form(
            step_id="manual_scan_summary",
            data_schema=vol.Schema({}),
            description_placeholders={
                "summary": _format_scan_summary(self._manual_scan_result.get("scan_result", {})),
            },
        )

    async def async_step_manual_scan_failed(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Show manual scan failure and allow continuing without discovered entries."""
        if user_input is not None and self._pending_options_input is not None:
            data = dict(self._pending_options_input)
            data[CONF_INIT_SCAN_NOW] = False
            return self.async_create_entry(title="", data=data)

        return self.async_show_form(
            step_id="manual_scan_failed",
            data_schema=vol.Schema({}),
            errors={"base": "init_scan_failed"},
        )

    async def _run_initialization_scan(self, user_input: Dict[str, Any]) -> Dict[str, Any]:
        """Run full recursive scan once and return entries plus scan details."""
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
            scan_result = await temp_coordinator.async_initialize_entries(
                scan_interval_seconds=interval,
                max_entries=max_entries,
            )
            return {
                "entries": temp_coordinator.entries,
                "scan_result": scan_result,
            }
        finally:
            await temp_coordinator.async_teardown()
