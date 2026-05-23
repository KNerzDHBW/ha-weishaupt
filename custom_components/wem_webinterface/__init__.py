"""WEM webinterface integration setup."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er

from .const import CONF_BASE_URL, DOMAIN, PLATFORMS
from .coordinator import WemCoordinator

BOOTSTRAP_CACHE_KEY = "_bootstrap_cache"
SERVICE_CLEANUP_DUPLICATES = "cleanup_duplicates"

_LOGGER = logging.getLogger(__name__)


def _normalized_entity_name(entry: er.RegistryEntry) -> str:
    name = entry.original_name or getattr(entry, "name", None) or entry.entity_id
    return " ".join(str(name).strip().lower().split())


def _entry_score(hass: HomeAssistant, entry: er.RegistryEntry) -> tuple[int, int, int, int]:
    state = hass.states.get(entry.entity_id)
    state_value = state.state if state is not None else None
    is_available = int(state_value not in (None, "unavailable", "unknown"))
    has_state = int(state is not None)
    not_disabled = int(entry.disabled_by is None)
    not_unknown = int(state_value != "unknown")
    return (is_available, has_state, not_disabled, not_unknown)


async def _async_cleanup_duplicates_service(hass: HomeAssistant, _call: ServiceCall) -> None:
    """Remove duplicate entities for WEM points and keep the most useful one."""
    registry = er.async_get(hass)
    removed = 0
    groups = 0

    for config_entry in hass.config_entries.async_entries(DOMAIN):
        entries = [
            entry
            for entry in er.async_entries_for_config_entry(registry, config_entry.entry_id)
            if entry.platform == DOMAIN and entry.domain in PLATFORMS
        ]

        grouped: dict[tuple[str, str], list[er.RegistryEntry]] = {}
        for entry in entries:
            key = (entry.domain, _normalized_entity_name(entry))
            grouped.setdefault(key, []).append(entry)

        for siblings in grouped.values():
            if len(siblings) < 2:
                continue

            groups += 1
            keeper = max(
                siblings,
                key=lambda item: (_entry_score(hass, item), -len(item.entity_id), item.entity_id),
            )

            for candidate in siblings:
                if candidate.entity_id == keeper.entity_id:
                    continue
                registry.async_remove(candidate.entity_id)
                removed += 1

    _LOGGER.info("WEM cleanup_duplicates removed %s entities across %s duplicate groups", removed, groups)


def _async_register_services(hass: HomeAssistant) -> None:
    if not hass.services.has_service(DOMAIN, SERVICE_CLEANUP_DUPLICATES):
        async def _handler(call: ServiceCall) -> None:
            await _async_cleanup_duplicates_service(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEANUP_DUPLICATES,
            _handler,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up WEM from a config entry."""
    _async_register_services(hass)

    domain_data = hass.data.setdefault(DOMAIN, {})
    bootstrap_cache = domain_data.get(BOOTSTRAP_CACHE_KEY, {})
    bootstrap_state = None
    if isinstance(bootstrap_cache, dict):
        bootstrap_state = bootstrap_cache.pop(str(entry.data.get(CONF_BASE_URL, "")), None)

    coordinator = WemCoordinator(hass, entry, bootstrap_state=bootstrap_state)

    try:
        await coordinator.async_initialize()
    except PermissionError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:
        raise ConfigEntryNotReady(str(err)) from err

    domain_data[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when config/option values change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload WEM config entry."""
    coordinator: WemCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_shutdown()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
