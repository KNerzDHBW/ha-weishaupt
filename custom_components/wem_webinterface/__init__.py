"""WEM Web Interface – Home Assistant integration setup."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
import voluptuous as vol
import homeassistant.helpers.config_validation as cv

from .const import CONF_ENTRIES, DOMAIN, PLATFORMS
from .coordinator import WemCoordinator, _parse_entries

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up WEM Web Interface from a config entry."""
    coordinator = WemCoordinator.from_config_entry(hass, entry)

    try:
        await coordinator.async_setup()
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
