"""Config flow for WEM webinterface integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import selector

from .const import (
    CONF_BASE_URL,
    CONF_DISABLED_MENUS,
    CONF_DISABLED_SUBMENUS,
    CONF_KNOWN_MENUS,
    CONF_KNOWN_SUBMENUS,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    CONF_WAIT_SECONDS,
    DEFAULT_BASE_URL,
    DEFAULT_PASSWORD,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USERNAME,
    DEFAULT_WAIT_SECONDS,
    DOMAIN,
)
from .parser import WemPoint, WemWebClient, discover_structure, fetch_root_menus

_LOGGER = logging.getLogger(__name__)

SELECTED_MENUS = "selected_menus"
STATUS_TEXT = "status_text"
BOOTSTRAP_CACHE_KEY = "_bootstrap_cache"


def _tail_lines(text: str, max_lines: int = 24) -> str:
    """Return newest status lines first so latest updates are visible at the top."""
    lines = text.splitlines()
    header = "Latest first (newest at top):"
    if not lines:
        return header

    newest_first = list(reversed(lines[-max_lines:]))
    body = "\n".join(newest_first)
    if len(lines) <= max_lines:
        return f"{header}\n{body}"
    return f"{header}\n{body}\n..."


def _normalize_base_url(value: Any) -> str:
    """Normalize user-provided base URL for stable login/setup usage."""
    base_url = str(value or "").strip()
    if not base_url:
        return DEFAULT_BASE_URL
    if "://" not in base_url:
        base_url = f"http://{base_url}"
    return base_url.rstrip("/")


def _serialize_points_for_bootstrap(points: dict[str, WemPoint]) -> dict[str, dict[str, Any]]:
    serialized: dict[str, dict[str, Any]] = {}
    for point_id, point in points.items():
        write_spec = None
        if point.write_spec is not None:
            write_spec = {
                "action_url": point.write_spec.action_url,
                "hidden_fields": dict(point.write_spec.hidden_fields),
                "value_field": point.write_spec.value_field,
                "scaling_factor": point.write_spec.scaling_factor,
                "select_value_map": dict(point.write_spec.select_value_map),
            }

        serialized[point_id] = {
            "point_id": point.point_id,
            "menu": point.menu,
            "submenu": point.submenu,
            "name": point.name,
            "source_stack": point.source_stack,
            "value": point.value,
            "unit": point.unit,
            "writable": point.writable,
            "kind": point.kind,
            "editor_stack": point.editor_stack,
            "options": list(point.options),
            "min_value": point.min_value,
            "max_value": point.max_value,
            "step": point.step,
            "write_spec": write_spec,
        }

    return serialized


def _menu_options(entry: config_entries.ConfigEntry) -> dict[str, str]:
    menus = list(entry.options.get(CONF_KNOWN_MENUS, []))
    return {menu: menu for menu in menus}


def _submenu_options(entry: config_entries.ConfigEntry) -> dict[str, str]:
    submenus = list(entry.options.get(CONF_KNOWN_SUBMENUS, []))
    return {item: item for item in submenus}


class WemConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle WEM config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._pending_user_input: dict[str, Any] | None = None
        self._temp_client: WemWebClient | None = None

        self._login_task: asyncio.Task | None = None
        self._login_status: str = "Logging in..."
        self._login_lines: list[str] = []

        self._root_menus: list[tuple[str, str]] = []
        self._selected_menus: set[str] = set()

        self._scan_task: asyncio.Task | None = None
        self._scan_tree: dict[str, dict[str, bool]] = {}
        self._scan_progress_lines: str = "Waiting..."
        self._scan_finalize_total: int = 0
        self._scan_finalize_done: int = 0
        self._scan_finalize_item: str = ""
        self._scan_result: tuple[dict, list, list, list] | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input[CONF_BASE_URL] = _normalize_base_url(user_input.get(CONF_BASE_URL))
            await self.async_set_unique_id(user_input[CONF_BASE_URL])
            self._abort_if_unique_id_configured()
            self._pending_user_input = dict(user_input)
            return await self.async_step_login_progress()

        schema = vol.Schema(
            {
                vol.Required(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
                vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
                vol.Required(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
                vol.Required(CONF_WAIT_SECONDS, default=DEFAULT_WAIT_SECONDS): vol.All(
                    vol.Coerce(float), vol.Range(min=0.0, max=60.0)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_login_progress(self, user_input: dict[str, Any] | None = None):
        if self._pending_user_input is None:
            return await self.async_step_user()

        if self._login_task is None:
            self._login_status = "Logging in..."
            self._login_lines = ["Logging in..."]
            self._login_task = self.hass.async_create_task(self._async_login_and_load_menus())

        if not self._login_task.done():
            status_text = _tail_lines("\n".join(self._login_lines), max_lines=16) or self._login_status
            return self.async_show_form(
                step_id="login_progress",
                data_schema=vol.Schema(
                    {
                        vol.Optional(STATUS_TEXT, default=status_text): selector.TextSelector(
                            selector.TextSelectorConfig(multiline=True)
                        )
                    }
                ),
                description_placeholders={
                    "status": self._login_status,
                    "status_lines": status_text,
                },
            )

        try:
            self._login_task.result()
        except Exception as err:
            _LOGGER.error("Login phase failed: %s", err)
            await self._async_cleanup_temp_client()
            self._login_task = None
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONF_BASE_URL,
                            default=self._pending_user_input.get(CONF_BASE_URL, DEFAULT_BASE_URL),
                        ): str,
                        vol.Required(
                            CONF_USERNAME,
                            default=self._pending_user_input.get(CONF_USERNAME, DEFAULT_USERNAME),
                        ): str,
                        vol.Required(
                            CONF_PASSWORD,
                            default=self._pending_user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD),
                        ): str,
                        vol.Required(
                            CONF_WAIT_SECONDS,
                            default=self._pending_user_input.get(CONF_WAIT_SECONDS, DEFAULT_WAIT_SECONDS),
                        ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=60.0)),
                    }
                ),
                errors={"base": "login_failed"},
            )

        return await self.async_step_menu_select()

    async def _async_login_and_load_menus(self) -> None:
        if self._pending_user_input is None:
            raise RuntimeError("No user input available")

        await self._async_cleanup_temp_client()

        self._temp_client = WemWebClient(
            base_url=str(self._pending_user_input[CONF_BASE_URL]),
            username=str(self._pending_user_input[CONF_USERNAME]),
            password=str(self._pending_user_input[CONF_PASSWORD]),
            wait_seconds=float(self._pending_user_input[CONF_WAIT_SECONDS]),
        )
        await self._temp_client.async_open()

        self._login_status = "Trying login..."
        self._login_lines.append("Trying login...")
        self.async_update_progress(0.2)

        async def _on_login_progress(event: dict[str, Any]) -> None:
            event_type = str(event.get("event") or "")
            if event_type == "login_try":
                attempt = event.get("try")
                maximum = event.get("max")
                line = f"Trying login. Try {attempt}/{maximum}"
            elif event_type == "login_post":
                line = "Sending login request..."
            elif event_type == "login_verify":
                line = "Verifying session via settings_export.html..."
            elif event_type == "login_post_failed":
                line = "Login POST failed, trying next target..."
            elif event_type == "login_ok":
                line = "Login successful."
            elif event_type == "login_failed":
                line = "Login failed after maximum retries."
            else:
                return

            self._login_status = line
            self._login_lines.append(line)

        await self._temp_client.login(progress_callback=_on_login_progress)

        self._login_status = "Login successful. Reading main menus..."
        self._login_lines.append("Reading main menus...")
        self.async_update_progress(0.7)

        self._root_menus = await fetch_root_menus(self._temp_client)
        if not self._root_menus:
            raise RuntimeError("No menu entries found after login")

        self._selected_menus = {name for name, _ in self._root_menus}
        self._login_status = "Main menus loaded."
        self.async_update_progress(1.0)

    async def async_step_menu_select(self, user_input: dict[str, Any] | None = None):
        if not self._root_menus:
            return await self.async_step_user()

        errors: dict[str, str] = {}

        if user_input is not None:
            selected = user_input.get(SELECTED_MENUS, [])
            if isinstance(selected, dict):
                selected_names = {name for name, checked in selected.items() if checked}
            else:
                selected_names = set(selected)
            if not selected_names:
                errors["base"] = "no_menu_selected"
            else:
                self._selected_menus = selected_names
                self._pending_user_input[CONF_SCAN_INTERVAL] = int(
                    user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                )
                return await self.async_step_scan_progress()

        menu_names = [name for name, _ in self._root_menus]
        menu_select = {name: name for name in menu_names}

        schema = vol.Schema(
            {
                vol.Required(SELECTED_MENUS, default=menu_names): cv.multi_select(menu_select),
                vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
                    vol.Coerce(int), vol.Range(min=5, max=3600)
                ),
            }
        )

        return self.async_show_form(step_id="menu_select", data_schema=schema, errors=errors)

    async def async_step_scan_progress(self, user_input: dict[str, Any] | None = None):
        if self._pending_user_input is None or self._temp_client is None:
            return await self.async_step_user()

        if self._scan_task is None:
            self._scan_task = self.hass.async_create_task(self._async_scan_selected_menus())

        if not self._scan_task.done():
            status_text = _tail_lines(self._scan_progress_lines, max_lines=24) or "Starting initialization..."
            return self.async_show_form(
                step_id="scan_progress",
                data_schema=vol.Schema(
                    {
                        vol.Optional(STATUS_TEXT, default=status_text): selector.TextSelector(
                            selector.TextSelectorConfig(multiline=True)
                        )
                    }
                ),
                description_placeholders={"status_lines": status_text},
            )

        try:
            self._scan_result = self._scan_task.result()
        except Exception as err:
            _LOGGER.error("Initialization scan failed: %s", err)
            await self._async_cleanup_temp_client()
            return self.async_show_form(
                step_id="menu_select",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            SELECTED_MENUS,
                            default=[name for name, _ in self._root_menus],
                        ): cv.multi_select({name: name for name, _ in self._root_menus}),
                        vol.Required(
                            CONF_SCAN_INTERVAL,
                            default=self._pending_user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                        ): vol.All(vol.Coerce(int), vol.Range(min=5, max=3600)),
                    }
                ),
                errors={"base": "scan_failed"},
            )

        return await self.async_step_scan_summary()

    async def _async_scan_selected_menus(self):
        if self._temp_client is None:
            raise RuntimeError("Temporary client is missing")

        self._scan_tree = {}
        self._scan_progress_lines = "Starting initialization..."
        self._scan_finalize_total = 0
        self._scan_finalize_done = 0
        self._scan_finalize_item = ""
        self.async_update_progress(0.0)

        async def _on_progress(event: dict[str, Any]) -> None:
            event_type = event.get("event")
            menu = str(event.get("menu") or "")

            if event_type == "menu":
                submenus = [str(name) for name in event.get("submenus") or []]
                if submenus:
                    self._scan_tree[menu] = {name: False for name in submenus}
                else:
                    self._scan_tree[menu] = {"(menu page)": False}
            elif event_type == "submenu_done":
                submenu = str(event.get("submenu") or "")
                key = submenu if submenu else "(menu page)"
                self._scan_tree.setdefault(menu, {})
                self._scan_tree[menu][key] = True
            elif event_type == "finalize_start":
                self._scan_finalize_total = int(event.get("total") or 0)
                self._scan_finalize_done = 0
                self._scan_finalize_item = ""
            elif event_type == "finalize_step":
                self._scan_finalize_done = int(event.get("done") or 0)
                self._scan_finalize_item = str(event.get("item") or "")
            elif event_type == "finalize_done":
                self._scan_finalize_done = self._scan_finalize_total

            self._scan_progress_lines = self._render_scan_lines()

            menu_total = sum(len(items) for items in self._scan_tree.values())
            menu_done = sum(1 for items in self._scan_tree.values() for ok in items.values() if ok)
            menu_ratio = (menu_done / menu_total) if menu_total else 0.0

            # Reserve the final 20% for writable editor inspection phase.
            if self._scan_finalize_total > 0:
                finalize_ratio = self._scan_finalize_done / max(1, self._scan_finalize_total)
            else:
                finalize_ratio = 0.0

            ratio = min(1.0, 0.8 * menu_ratio + 0.2 * finalize_ratio)
            self.async_update_progress(ratio)

        result = await discover_structure(
            self._temp_client,
            selected_menus=self._selected_menus,
            progress_callback=_on_progress,
        )
        self._scan_progress_lines = self._render_scan_lines()
        self.async_update_progress(1.0)
        return result

    def _render_scan_lines(self) -> str:
        lines: list[str] = []
        for menu, subitems in self._scan_tree.items():
            lines.append(f"- {menu}")
            for name, done in subitems.items():
                mark = "[x]" if done else "[ ]"
                lines.append(f"  {mark} {name}")
        if self._scan_finalize_total > 0:
            lines.append("")
            line = f"Finalizing writable items: {self._scan_finalize_done}/{self._scan_finalize_total}"
            if self._scan_finalize_item:
                line = f"{line}: {self._scan_finalize_item}"
            lines.append(line)
        if not lines:
            return "Starting initialization..."
        return "\n".join(lines)

    async def async_step_scan_summary(self, user_input: dict[str, Any] | None = None):
        if self._pending_user_input is None or self._scan_result is None:
            return await self.async_step_user()

        if user_input is not None:
            points, pages, known_menus, known_submenus = self._scan_result
            disabled_menus = [menu for menu in known_menus if menu not in self._selected_menus]
            disabled_menu_set = set(disabled_menus)
            disabled_submenus = [
                submenu_key
                for submenu_key in known_submenus
                if submenu_key.split("|", 1)[0] in disabled_menu_set
            ]

            bootstrap_state = {
                "values": {point_id: point.value for point_id, point in points.items()},
                "points": _serialize_points_for_bootstrap(points),
                "page_targets": [list(row) for row in pages],
                "logged_in": False,
                "last_update_page": "setup: bootstrap from config flow",
                "consecutive_failures": 0,
            }
            domain_data = self.hass.data.setdefault(DOMAIN, {})
            bootstrap_cache = domain_data.setdefault(BOOTSTRAP_CACHE_KEY, {})
            bootstrap_cache[str(self._pending_user_input[CONF_BASE_URL])] = bootstrap_state

            options = {
                CONF_KNOWN_MENUS: list(known_menus),
                CONF_KNOWN_SUBMENUS: list(known_submenus),
                CONF_DISABLED_MENUS: disabled_menus,
                CONF_DISABLED_SUBMENUS: disabled_submenus,
            }
            await self._async_cleanup_temp_client()
            return self.async_create_entry(
                title="WEM Webinterface",
                data={
                    CONF_BASE_URL: self._pending_user_input[CONF_BASE_URL],
                    CONF_USERNAME: self._pending_user_input[CONF_USERNAME],
                    CONF_PASSWORD: self._pending_user_input[CONF_PASSWORD],
                    CONF_WAIT_SECONDS: self._pending_user_input[CONF_WAIT_SECONDS],
                    CONF_SCAN_INTERVAL: self._pending_user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                },
                options=options,
            )

        points, _pages, known_menus, known_submenus = self._scan_result
        summary = self._render_scan_lines()
        summary += (
            f"\n\nDetected points: {len(points)}"
            f"\nMain menus: {len(known_menus)}"
            f"\nSubmenus: {len(known_submenus)}"
        )

        return self.async_show_form(
            step_id="scan_summary",
            data_schema=vol.Schema({}),
            description_placeholders={"summary": summary},
        )

    async def _async_cleanup_temp_client(self) -> None:
        if self._temp_client is not None:
            await self._temp_client.async_close()
            self._temp_client = None

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return WemOptionsFlow(config_entry)


class WemOptionsFlow(config_entries.OptionsFlow):
    """Handle WEM options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self._config_entry.options
        data = self._config_entry.data

        menu_opts = _menu_options(self._config_entry)
        submenu_opts = _submenu_options(self._config_entry)

        schema_dict: dict[Any, Any] = {
            vol.Required(
                CONF_BASE_URL,
                default=options.get(CONF_BASE_URL, data.get(CONF_BASE_URL, DEFAULT_BASE_URL)),
            ): str,
            vol.Required(
                CONF_USERNAME,
                default=options.get(CONF_USERNAME, data.get(CONF_USERNAME, DEFAULT_USERNAME)),
            ): str,
            vol.Required(
                CONF_PASSWORD,
                default=options.get(CONF_PASSWORD, data.get(CONF_PASSWORD, DEFAULT_PASSWORD)),
            ): str,
            vol.Required(
                CONF_WAIT_SECONDS,
                default=options.get(CONF_WAIT_SECONDS, data.get(CONF_WAIT_SECONDS, DEFAULT_WAIT_SECONDS)),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=60.0)),
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=options.get(CONF_SCAN_INTERVAL, data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
            ): vol.All(vol.Coerce(int), vol.Range(min=5, max=3600)),
            vol.Optional(
                CONF_DISABLED_MENUS,
                default=options.get(CONF_DISABLED_MENUS, []),
            ): cv.multi_select(menu_opts),
            vol.Optional(
                CONF_DISABLED_SUBMENUS,
                default=options.get(CONF_DISABLED_SUBMENUS, []),
            ): cv.multi_select(submenu_opts),
        }

        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema_dict))
