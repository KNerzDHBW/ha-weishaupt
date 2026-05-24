"""Coordinator for WEM webinterface integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

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
    DEFAULT_MAX_WRITE_RETRIES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USERNAME,
    DEFAULT_WAIT_SECONDS,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .parser import (
    WriteSpec,
    WemPoint,
    WemWebClient,
    discover_structure,
    looks_like_menu_echo_page,
    normalize_for_compare,
    parse_points_from_page,
    resolve_menu_submenu_stack,
)

_LOGGER = logging.getLogger(__name__)

PointListener = Callable[[], None]


class WemCoordinator(DataUpdateCoordinator[dict[str, WemPoint]]):
    """Coordinate one WEM device poll/update lifecycle."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        bootstrap_state: dict[str, Any] | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry

        scan_interval = int(self._opt(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{entry.entry_id}",
            update_interval=timedelta(seconds=scan_interval),
        )

        self.base_url: str = self._resolve_base_url()
        self.username: str = str(self._opt(CONF_USERNAME, DEFAULT_USERNAME))
        self.password: str = str(self._opt(CONF_PASSWORD, ""))
        self.wait_seconds: float = float(self._opt(CONF_WAIT_SECONDS, DEFAULT_WAIT_SECONDS))

        self.client = WemWebClient(
            base_url=self.base_url,
            username=self.username,
            password=self.password,
            wait_seconds=self.wait_seconds,
        )

        self.points: dict[str, WemPoint] = {}
        self.page_targets: list[tuple[str, str, str]] = []

        self.logged_in: bool = False
        self.is_updating: bool = False
        self.last_refresh: str | None = None
        self.last_error: str = ""
        self.last_update_page: str = "startup"
        self.setup_phase: str = "idle"
        self.stack_recoveries: int = 0
        self.consecutive_failures: int = 0

        self._point_listeners: list[PointListener] = []
        self._lock = asyncio.Lock()
        self._storage = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry.entry_id}")
        self._last_values: dict[str, Any] = {}
        self._bootstrap_state = bootstrap_state

    @staticmethod
    def _normalize_label(value: str) -> str:
        return re.sub(r"\s+", " ", (value or "")).strip().lower()

    def _logical_key(self, menu: str, submenu: str, name: str) -> tuple[str, str, str]:
        return (
            self._normalize_label(menu),
            self._normalize_label(submenu),
            self._normalize_label(name),
        )

    @staticmethod
    def _write_signature(point: WemPoint) -> tuple[str, str] | None:
        """Stable writable signature independent from transient point IDs."""
        if point.write_spec is None:
            return None
        action_url = str(point.write_spec.action_url or "").split("?", 1)[0].rstrip("/").lower()
        value_field = str(point.write_spec.value_field or "").strip().lower()
        if not action_url or not value_field:
            return None
        return (action_url, value_field)

    def _find_compatible_existing_point_id(
        self,
        merged: dict[str, WemPoint],
        discovered_point: WemPoint,
    ) -> str | None:
        """Find existing point when submenu/stack context changed between scans."""
        wanted_menu = self._normalize_label(discovered_point.menu)
        wanted_submenu = self._normalize_label(discovered_point.submenu)
        wanted_name = self._normalize_label(discovered_point.name)
        wanted_sig = self._write_signature(discovered_point)

        best_id: str | None = None
        best_score = -1

        for point_id, point in merged.items():
            if self._normalize_label(point.menu) != wanted_menu:
                continue
            if self._normalize_label(point.name) != wanted_name:
                continue

            score = 0
            point_submenu = self._normalize_label(point.submenu)
            if point_submenu == wanted_submenu:
                score += 4
            elif not point_submenu or not wanted_submenu:
                score += 2
            else:
                continue

            point_sig = self._write_signature(point)
            if wanted_sig and point_sig:
                if wanted_sig != point_sig:
                    continue
                score += 3
            elif wanted_sig or point_sig:
                score += 1

            if score > best_score:
                best_score = score
                best_id = point_id

        return best_id

    def _set_setup_phase(self, phase: str) -> None:
        """Track setup phase for diagnostics and UI visibility."""
        if phase == self.setup_phase:
            return
        self.setup_phase = phase
        self.last_update_page = f"setup: {phase}"
        self.async_update_listeners()

    @staticmethod
    def _is_unknown_runtime_value(value: Any) -> bool:
        """Return True for placeholder/empty values that should not replace known state."""
        if value is None:
            return True
        if isinstance(value, str):
            norm = value.strip().lower()
            return norm in {"", "unknown", "n/a", "none", "nan"}
        return False

    @staticmethod
    def _is_numeric_select_point(point: WemPoint) -> bool:
        """Return True if select options are all numeric-like labels."""
        if point.write_spec is None or not point.write_spec.select_value_map:
            return False
        option_labels = list(point.write_spec.select_value_map.keys())
        if not option_labels:
            return False

        for label in option_labels:
            text = str(label or "").strip()
            match = re.search(r"[+-]?[0-9]+(?:[.,][0-9]+)?", text)
            if match is None:
                return False
            try:
                float(match.group(0).replace(",", "."))
            except (TypeError, ValueError):
                return False

        return True

    @staticmethod
    def _resolve_stable_kind(existing: WemPoint, incoming: WemPoint) -> str:
        """Prefer stable writable kinds over transient text-only parses."""
        existing_kind = str(existing.kind or "").strip().lower()
        incoming_kind = str(incoming.kind or "").strip().lower()

        if incoming_kind == "select":
            if existing_kind == "number" and WemCoordinator._is_numeric_select_point(existing):
                return "number"
            return "select"
        if incoming_kind == "number":
            return "number"

        # Parsed pages often expose writable entries as generic text links.
        # Keep previously classified writable types unless explicitly reclassified.
        if incoming_kind == "text" and existing_kind in {"select", "number"}:
            return existing_kind

        if incoming_kind:
            return incoming_kind
        return existing_kind or "text"

    def _opt(self, key: str, default: Any) -> Any:
        if key in self.entry.options:
            return self.entry.options[key]
        return self.entry.data.get(key, default)

    def _resolve_base_url(self) -> str:
        """Resolve base URL from options/data including legacy ip_address entries."""
        raw_base_url = str(self._opt(CONF_BASE_URL, "") or "").strip()
        if not raw_base_url:
            legacy_ip = str(self.entry.data.get("ip_address", "") or "").strip()
            if legacy_ip:
                raw_base_url = f"http://{legacy_ip}"
            else:
                raw_base_url = DEFAULT_BASE_URL

        if "://" not in raw_base_url:
            raw_base_url = f"http://{raw_base_url}"

        return raw_base_url.rstrip("/")

    @property
    def known_menus(self) -> list[str]:
        return list(self.entry.options.get(CONF_KNOWN_MENUS, []))

    @property
    def known_submenus(self) -> list[str]:
        return list(self.entry.options.get(CONF_KNOWN_SUBMENUS, []))

    @property
    def disabled_menus(self) -> set[str]:
        return set(self.entry.options.get(CONF_DISABLED_MENUS, []))

    @property
    def disabled_submenus(self) -> set[str]:
        return set(self.entry.options.get(CONF_DISABLED_SUBMENUS, []))

    def is_point_enabled(self, point: WemPoint) -> bool:
        """Return whether a point is enabled by menu/submenu options."""
        if point.menu in self.disabled_menus:
            return False
        if point.submenu and f"{point.menu}|{point.submenu}" in self.disabled_submenus:
            return False
        return True

    async def async_initialize(self) -> None:
        """Initialize coordinator and run first discovery."""
        self._set_setup_phase("loading cached state")
        _LOGGER.info("WEM setup: loading cached state")
        state = await self._storage.async_load() or {}

        if not self._has_cached_structure(state) and self._has_cached_structure(self._bootstrap_state):
            _LOGGER.info("WEM setup: applying bootstrap state from config flow")
            state = dict(self._bootstrap_state or {})

        self._last_values = dict(state.get("values", {}))
        self.stack_recoveries = int(state.get("stack_recoveries", 0) or 0)
        self.last_refresh = str(state.get("last_refresh") or "") or None
        self.last_error = str(state.get("last_error") or "")

        restored_from_cache = self._restore_cached_structure(state)

        self._set_setup_phase("opening HTTP session")
        _LOGGER.info("WEM setup: opening HTTP session")
        await self.client.async_open()

        if restored_from_cache:
            self.logged_in = False
            self._set_setup_phase("restored cached structure")
            _LOGGER.info(
                "WEM setup: restored cached structure (%s points, %s pages)",
                len(self.points),
                len(self.page_targets),
            )
            await self._save_state()
            self.async_set_updated_data(self.points)
            self._set_setup_phase("ready")
            _LOGGER.info("WEM setup: ready (using cached data)")
            return

        self._set_setup_phase("logging in")
        _LOGGER.info("WEM setup: logging in")
        await self.client.login()
        self.logged_in = True

        self._set_setup_phase("discovering menus and points")
        _LOGGER.info("WEM setup: discovering menus and points")
        await self._discover_once()

        self._set_setup_phase("running first data refresh")
        _LOGGER.info("WEM setup: running first data refresh")
        await self.async_refresh()

        self._set_setup_phase("ready")
        _LOGGER.info("WEM setup: ready")

    async def async_shutdown(self) -> None:
        """Close resources."""
        await self.client.async_close()

    def register_point_listener(self, callback: PointListener) -> Callable[[], None]:
        """Register callback for newly discovered points."""
        self._point_listeners.append(callback)

        def _unsub() -> None:
            if callback in self._point_listeners:
                self._point_listeners.remove(callback)

        return _unsub

    async def _discover_once(self) -> None:
        """Run discovery and apply startup defaults."""
        old_ids = set(self.points)
        finalize_total = 0

        async def _on_discovery_progress(event: dict[str, Any]) -> None:
            nonlocal finalize_total
            event_type = str(event.get("event") or "")
            if event_type == "menu":
                menu = str(event.get("menu") or "")
                self._set_setup_phase(f"discovering menu: {menu}")
                return
            if event_type == "submenu_done":
                menu = str(event.get("menu") or "")
                submenu = str(event.get("submenu") or "")
                label = submenu if submenu else "(menu page)"
                self._set_setup_phase(f"parsed submenu: {menu} / {label}")
                return
            if event_type == "finalize_start":
                finalize_total = int(event.get("total") or 0)
                self._set_setup_phase(f"finalizing writable items: 0/{finalize_total}")
                return
            if event_type == "finalize_step":
                done = int(event.get("done") or 0)
                item = str(event.get("item") or "")
                progress = f"{done}/{finalize_total}" if finalize_total > 0 else str(done)
                if item:
                    self._set_setup_phase(f"finalizing writable items: {progress}: {item}")
                else:
                    self._set_setup_phase(f"finalizing writable items: {progress}")
                return
            if event_type == "finalize_done":
                self._set_setup_phase("finalizing writable items: done")

        points, pages, known_menus, known_submenus = await discover_structure(
            self.client,
            progress_callback=_on_discovery_progress,
        )

        self.points = self._merge_discovered_points(points)
        self.page_targets = self._merge_page_targets(pages)

        for point in self.points.values():
            if point.point_id in self._last_values:
                point.value = self._last_values[point.point_id]
            elif point.kind == "number":
                if point.min_value is not None:
                    point.value = point.min_value
                elif isinstance(point.value, (int, float)):
                    pass
                else:
                    point.value = 0.0
            elif point.kind == "select" and point.options:
                point.value = point.value if point.value in point.options else point.options[0]
            else:
                point.value = point.value if point.value not in (None, "") else "unknown"

        if known_menus or known_submenus:
            new_options = dict(self.entry.options)
            merged_menus = sorted(
                set(self.entry.options.get(CONF_KNOWN_MENUS, [])) | set(known_menus)
            )
            merged_submenus = sorted(
                set(self.entry.options.get(CONF_KNOWN_SUBMENUS, [])) | set(known_submenus)
            )
            new_options[CONF_KNOWN_MENUS] = merged_menus
            new_options[CONF_KNOWN_SUBMENUS] = merged_submenus
            self.hass.config_entries.async_update_entry(self.entry, options=new_options)

        if set(self.points) != old_ids:
            for callback in list(self._point_listeners):
                callback()

    def _restore_cached_structure(self, state: dict[str, Any]) -> bool:
        """Restore discovered points/pages from storage, if available."""
        raw_points = state.get("points")
        raw_pages = state.get("page_targets")
        if not isinstance(raw_points, dict):
            return False

        restored_points: dict[str, WemPoint] = {}
        for point_id, payload in raw_points.items():
            if not isinstance(payload, dict):
                continue
            point = self._deserialize_point(str(point_id), payload)
            if point is None:
                continue
            restored_points[point.point_id] = point

        restored_pages: list[tuple[str, str, str]] = []
        if isinstance(raw_pages, list):
            for row in raw_pages:
                if not isinstance(row, (list, tuple)) or len(row) != 3:
                    continue
                menu, submenu, stack = row
                restored_pages.append((str(menu), str(submenu), str(stack)))

        if not restored_points:
            return False

        self.points = restored_points
        self.points = self._deduplicate_points_by_logical_key(self.points)
        self.page_targets = restored_pages
        return True

    def _merge_discovered_points(self, discovered: dict[str, WemPoint]) -> dict[str, WemPoint]:
        """Merge newly discovered points into existing ones without removing any point."""
        merged = dict(self.points)
        logical_index: dict[tuple[str, str, str], str] = {
            self._logical_key(point.menu, point.submenu, point.name): point_id
            for point_id, point in merged.items()
        }

        for discovered_id, discovered_point in discovered.items():
            existing = merged.get(discovered_id)
            if existing is None:
                logical_key = self._logical_key(
                    discovered_point.menu,
                    discovered_point.submenu,
                    discovered_point.name,
                )
                existing_id = logical_index.get(logical_key)
                if existing_id is None:
                    existing_id = self._find_compatible_existing_point_id(merged, discovered_point)
                if existing_id is None:
                    merged[discovered_id] = discovered_point
                    logical_index[logical_key] = discovered_id
                    continue
                existing = merged[existing_id]

            existing.source_stack = discovered_point.source_stack or existing.source_stack
            if not self._is_unknown_runtime_value(discovered_point.value):
                existing.value = discovered_point.value
            existing.unit = discovered_point.unit
            existing.writable = discovered_point.writable
            existing.kind = self._resolve_stable_kind(existing, discovered_point)
            existing.editor_stack = discovered_point.editor_stack or existing.editor_stack
            if discovered_point.options:
                existing.options = discovered_point.options
            existing.min_value = (
                discovered_point.min_value
                if discovered_point.min_value is not None
                else existing.min_value
            )
            existing.max_value = (
                discovered_point.max_value
                if discovered_point.max_value is not None
                else existing.max_value
            )
            existing.step = discovered_point.step if discovered_point.step is not None else existing.step
            existing.write_spec = discovered_point.write_spec or existing.write_spec

        return self._deduplicate_points_by_logical_key(merged)

    def _deduplicate_points_by_logical_key(self, points: dict[str, WemPoint]) -> dict[str, WemPoint]:
        """Keep one canonical point per logical key to avoid duplicate entities."""
        result: dict[str, WemPoint] = {}
        by_key: dict[tuple[str, str, str], str] = {}

        for point_id, point in points.items():
            logical = self._logical_key(point.menu, point.submenu, point.name)
            existing_id = by_key.get(logical)
            if existing_id is None:
                by_key[logical] = point_id
                result[point_id] = point
                continue

            existing = result[existing_id]
            if existing.value in (None, "", "unknown") and point.value not in (None, "", "unknown"):
                existing.value = point.value
            if not existing.unit and point.unit:
                existing.unit = point.unit
            existing.writable = existing.writable or point.writable
            if point.kind and existing.kind in ("", "sensor"):
                existing.kind = point.kind
            existing.editor_stack = existing.editor_stack or point.editor_stack
            if not existing.options and point.options:
                existing.options = point.options
            existing.min_value = existing.min_value if existing.min_value is not None else point.min_value
            existing.max_value = existing.max_value if existing.max_value is not None else point.max_value
            existing.step = existing.step if existing.step is not None else point.step
            existing.write_spec = existing.write_spec or point.write_spec

        return result

    def _merge_page_targets(
        self,
        discovered_pages: list[tuple[str, str, str]],
    ) -> list[tuple[str, str, str]]:
        """Merge page targets without dropping existing entries."""
        existing = {(menu, submenu): stack for menu, submenu, stack in self.page_targets}
        for menu, submenu, stack in discovered_pages:
            existing[(menu, submenu)] = stack
        return [(menu, submenu, stack) for (menu, submenu), stack in sorted(existing.items())]

    def _has_cached_structure(self, state: dict[str, Any] | None) -> bool:
        if not isinstance(state, dict):
            return False
        return isinstance(state.get("points"), dict) and isinstance(state.get("page_targets"), list)

    def _serialize_point(self, point: WemPoint) -> dict[str, Any]:
        write_spec = None
        if point.write_spec is not None:
            write_spec = {
                "action_url": point.write_spec.action_url,
                "hidden_fields": dict(point.write_spec.hidden_fields),
                "value_field": point.write_spec.value_field,
                "scaling_factor": point.write_spec.scaling_factor,
                "select_value_map": dict(point.write_spec.select_value_map),
            }

        return {
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

    def _deserialize_point(self, point_id: str, payload: dict[str, Any]) -> WemPoint | None:
        source_stack = str(payload.get("source_stack") or "")
        menu = str(payload.get("menu") or "")
        name = str(payload.get("name") or "")
        if not source_stack or not menu or not name:
            return None

        write_spec_data = payload.get("write_spec")
        write_spec: WriteSpec | None = None
        if isinstance(write_spec_data, dict):
            action_url = str(write_spec_data.get("action_url") or "")
            value_field = str(write_spec_data.get("value_field") or "")
            hidden_fields = write_spec_data.get("hidden_fields") or {}
            scaling_factor = float(write_spec_data.get("scaling_factor") or 1.0)
            select_value_map_raw = write_spec_data.get("select_value_map") or {}
            select_value_map = (
                {str(k): str(v) for k, v in select_value_map_raw.items()}
                if isinstance(select_value_map_raw, dict)
                else {}
            )
            if action_url and value_field and isinstance(hidden_fields, dict):
                write_spec = WriteSpec(
                    action_url=action_url,
                    hidden_fields={str(k): str(v) for k, v in hidden_fields.items()},
                    value_field=value_field,
                    scaling_factor=scaling_factor,
                    select_value_map=select_value_map,
                )

        point = WemPoint(
            point_id=point_id,
            menu=menu,
            submenu=str(payload.get("submenu") or ""),
            name=name,
            source_stack=source_stack,
            value=payload.get("value"),
            unit=str(payload.get("unit") or ""),
            writable=bool(payload.get("writable")),
            kind=str(payload.get("kind") or ("text" if payload.get("writable") else "sensor")),
            editor_stack=str(payload.get("editor_stack") or "") or None,
            options=[str(item) for item in payload.get("options") or []],
            min_value=payload.get("min_value"),
            max_value=payload.get("max_value"),
            step=payload.get("step"),
            write_spec=write_spec,
        )

        if point.point_id in self._last_values:
            point.value = self._last_values[point.point_id]

        return point

    async def _async_update_data(self) -> dict[str, WemPoint]:
        """Update all enabled pages and points."""
        async with self._lock:
            self.is_updating = True
            self.async_update_listeners()
            try:
                if not self.logged_in:
                    await self.client.login()
                    self.logged_in = True

                for menu, submenu, stack in self.page_targets:
                    if menu in self.disabled_menus:
                        continue
                    if submenu and f"{menu}|{submenu}" in self.disabled_submenus:
                        continue

                    await self._refresh_page(menu, submenu, stack)

                self.consecutive_failures = 0
                self.last_error = ""
                self.last_refresh = datetime.now(timezone.utc).isoformat()
                await self._save_state()
                return self.points
            except Exception as err:
                self.consecutive_failures += 1
                self.logged_in = False
                self.last_error = str(err)
                _LOGGER.warning("WEM update failed (%s): %s", self.consecutive_failures, err)
                await self._save_state()
                return self.points
            finally:
                self.is_updating = False
                self.async_update_listeners()

    @property
    def state_text(self) -> str:
        """Human-readable runtime state."""
        if self.is_updating:
            mode = "updating"
        elif self.consecutive_failures > 0:
            mode = "error"
        elif self.logged_in:
            mode = "ok"
        else:
            mode = "disconnected"

        login_state = "ok" if self.logged_in else "not_logged_in"
        return f"{mode}; login={login_state}; failures={self.consecutive_failures}"

    async def _refresh_page(self, menu: str, submenu: str, stack: str) -> None:
        active_stack = stack
        try:
            html = await self.client.fetch_stack(active_stack)
        except Exception as err:
            recovered_stack = await self._recover_stack_for_target(menu, submenu, active_stack, str(err))
            if recovered_stack is None:
                raise
            active_stack = recovered_stack
            html = await self.client.fetch_stack(active_stack)

        if looks_like_menu_echo_page(html, menu, submenu):
            for _ in range(2):
                html = await self.client.fetch_stack_reloaded(active_stack)
                if not looks_like_menu_echo_page(html, menu, submenu):
                    break

        if looks_like_menu_echo_page(html, menu, submenu):
            recovered_stack = await self._recover_stack_for_target(menu, submenu, active_stack, "menu echo page")
            if recovered_stack is not None and recovered_stack != active_stack:
                active_stack = recovered_stack
                html = await self.client.fetch_stack(active_stack)

        self.last_update_page = f"{menu} / {submenu or '-'}"

        parsed = parse_points_from_page(
            html=html,
            menu=menu,
            submenu=submenu,
            source_stack=active_stack,
        )

        if not parsed:
            recovered_stack = await self._recover_stack_for_target(
                menu,
                submenu,
                active_stack,
                "no points parsed (likely menu/submenu fallback)",
            )
            if recovered_stack is not None and recovered_stack != active_stack:
                active_stack = recovered_stack
                html = await self.client.fetch_stack(active_stack)
                parsed = parse_points_from_page(
                    html=html,
                    menu=menu,
                    submenu=submenu,
                    source_stack=active_stack,
                )

        for parsed_point in parsed:
            existing = self.points.get(parsed_point.point_id)
            if existing is None:
                existing_key = self._find_point_id_by_logical_name(menu, submenu, parsed_point.name)
                if existing_key is None:
                    self.points[parsed_point.point_id] = parsed_point
                    continue

                existing = self.points[existing_key]
                existing.source_stack = active_stack

            if not self._is_unknown_runtime_value(parsed_point.value):
                existing.value = parsed_point.value
            existing.unit = parsed_point.unit
            existing.writable = parsed_point.writable
            existing.kind = self._resolve_stable_kind(existing, parsed_point)
            existing.editor_stack = parsed_point.editor_stack or existing.editor_stack
            if parsed_point.options:
                existing.options = parsed_point.options
            existing.min_value = parsed_point.min_value if parsed_point.min_value is not None else existing.min_value
            existing.max_value = parsed_point.max_value if parsed_point.max_value is not None else existing.max_value
            existing.step = parsed_point.step if parsed_point.step is not None else existing.step
            existing.write_spec = parsed_point.write_spec or existing.write_spec

    def _find_point_id_by_logical_name(self, menu: str, submenu: str, name: str) -> str | None:
        wanted = self._logical_key(menu, submenu, name)
        for point_id, point in self.points.items():
            if self._logical_key(point.menu, point.submenu, point.name) == wanted:
                return point_id

        # Fallback: some pages intermittently omit submenu context; keep ID stable anyway.
        wanted_menu = self._normalize_label(menu)
        wanted_name = self._normalize_label(name)
        for point_id, point in self.points.items():
            if (
                self._normalize_label(point.menu) == wanted_menu
                and self._normalize_label(point.name) == wanted_name
            ):
                return point_id
        return None

    async def _recover_stack_for_target(
        self,
        menu: str,
        submenu: str,
        old_stack: str,
        reason: str,
    ) -> str | None:
        _LOGGER.warning(
            "WEM refresh issue for %s / %s on stack %s: %s. Trying stack recovery by labels.",
            menu,
            submenu or "-",
            old_stack,
            reason,
        )

        new_stack = await resolve_menu_submenu_stack(self.client, menu_name=menu, submenu_name=submenu)
        if not new_stack:
            _LOGGER.warning("WEM stack recovery failed for %s / %s: no current stack found", menu, submenu or "-")
            return None

        if new_stack == old_stack:
            _LOGGER.warning(
                "WEM stack recovery found same stack for %s / %s (%s)",
                menu,
                submenu or "-",
                old_stack,
            )
            return None

        self.page_targets = [
            (m, s, new_stack if (m == menu and s == submenu and st == old_stack) else st)
            for m, s, st in self.page_targets
        ]

        for point in self.points.values():
            if point.menu == menu and point.submenu == submenu and point.source_stack == old_stack:
                point.source_stack = new_stack

        self.stack_recoveries += 1
        await self._save_state()
        _LOGGER.info(
            "WEM stack recovery updated %s / %s: %s -> %s (total recoveries=%s)",
            menu,
            submenu or "-",
            old_stack,
            new_stack,
            self.stack_recoveries,
        )
        return new_stack

    async def async_set_point_value(self, point_id: str, new_value: Any) -> None:
        """Set value for one writable point and verify by reading back."""
        point = self.points.get(point_id)
        if point is None:
            raise HomeAssistantError(f"Unknown point: {point_id}")
        if not point.writable:
            raise HomeAssistantError(f"Point is read-only: {point.full_name}")

        async with self._lock:
            for _attempt in range(DEFAULT_MAX_WRITE_RETRIES):
                if not self.logged_in:
                    try:
                        await self.client.login()
                    except Exception:
                        # Session/cookie state can become stale between long update cycles.
                        await self.client.async_close()
                        await self.client.async_open()
                        await self.client.login()
                    self.logged_in = True

                await self.client.write_point(point, new_value)
                await self._refresh_page(point.menu, point.submenu, point.source_stack)

                updated = self.points.get(point_id)
                if updated is None:
                    continue
                if normalize_for_compare(updated.value) == normalize_for_compare(new_value):
                    await self._save_state()
                    self.async_set_updated_data(self.points)
                    return

            self.consecutive_failures += 1
            await self._save_state()
            raise HomeAssistantError(
                f"Write verification failed for {point.full_name}: expected {new_value}, got {self.points.get(point_id).value if self.points.get(point_id) else 'n/a'}"
            )

    async def _save_state(self) -> None:
        values = {point_id: point.value for point_id, point in self.points.items()}
        points = {point_id: self._serialize_point(point) for point_id, point in self.points.items()}
        await self._storage.async_save(
            {
                "values": values,
                "points": points,
                "page_targets": [list(row) for row in self.page_targets],
                "logged_in": self.logged_in,
                "last_refresh": self.last_refresh,
                "last_update_page": self.last_update_page,
                "last_error": self.last_error,
                "stack_recoveries": self.stack_recoveries,
                "consecutive_failures": self.consecutive_failures,
            }
        )
