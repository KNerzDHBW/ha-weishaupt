"""WEM Web Interface – Home Assistant integration setup."""

from __future__ import annotations

import logging
import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
import voluptuous as vol
import homeassistant.helpers.config_validation as cv
import aiohttp

from .const import CONF_ENTRIES, DOMAIN, PLATFORMS
from .coordinator import WemCoordinator, _parse_entries

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up WEM Web Interface from a config entry."""
    coordinator = WemCoordinator.from_config_entry(hass, entry)

    try:
        await coordinator.async_setup()
    except PermissionError as exc:
        raise ConfigEntryAuthFailed(str(exc)) from exc
    except (ConnectionError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # Tell Home Assistant to retry setup later instead of marking the
        # integration as permanently failed.
        raise ConfigEntryNotReady(str(exc)) from exc
    except Exception as exc:
        _LOGGER.error("Failed to set up WEM coordinator: %s", exc)
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # --- Register service: wem_webinterface.rediscover ---
    async def handle_rediscover(call: ServiceCall) -> None:
        stack = call.data.get("stack")
        if stack:
            await coordinator.async_rediscover_stack(stack)
        else:
            # Re-discover everything
            for s in coordinator.entries:
                await coordinator.async_rediscover_stack(s)

    if not hass.services.has_service(DOMAIN, "rediscover"):
        hass.services.async_register(
            DOMAIN,
            "rediscover",
            handle_rediscover,
            schema=vol.Schema({vol.Optional("stack"): cv.string}),
        )

    # --- Register service: wem_webinterface.initialize_scan ---
    async def handle_initialize_scan(call: ServiceCall) -> None:
        interval = int(call.data.get("scan_interval", 10))
        max_entries = int(call.data.get("max_entries", 500))
        result = await coordinator.async_initialize_entries(
            scan_interval_seconds=interval,
            max_entries=max_entries,
        )
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_ENTRIES: "\n".join(coordinator.entries),
            },
        )
        _LOGGER.info(
            "Initialization scan finished: processed=%d new_entries=%d failed=%d total_entries=%d",
            result["processed"],
            result["new_entries"],
            result["failed"],
            result["total_entries"],
        )

    if not hass.services.has_service(DOMAIN, "initialize_scan"):
        hass.services.async_register(
            DOMAIN,
            "initialize_scan",
            handle_initialize_scan,
            schema=vol.Schema(
                {
                    vol.Optional("scan_interval", default=10): vol.All(
                        vol.Coerce(int), vol.Range(min=5, max=300)
                    ),
                    vol.Optional("max_entries", default=500): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=5000)
                    ),
                }
            ),
        )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: WemCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_teardown()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unloaded
