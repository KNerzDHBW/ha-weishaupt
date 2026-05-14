"""Text entities for WEM Web Interface integration."""

from __future__ import annotations

import logging

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WemCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up text entities."""
    coordinator: WemCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([WemAddStackText(coordinator)])


class WemAddStackText(TextEntity):
    """Text entity to add a new stack."""

    entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:plus-circle"

    def __init__(self, coordinator: WemCoordinator) -> None:
        """Initialize the text entity."""
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_add_stack"
        self._attr_name = "Add Stack"
        self._attr_native_value = ""

    @property
    def available(self) -> bool:
        """Return True if coordinator is ready."""
        return True

    async def async_set_value(self, value: str) -> None:
        """Handle text value change."""
        if not value or not value.strip():
            # Empty value, just clear it
            self._attr_native_value = ""
            self.async_write_ha_state()
            return

        stack = value.strip()
        _LOGGER.info("Adding and discovering new stack: %s", stack[:50])
        
        try:
            # Rediscover the new stack
            await self.coordinator.async_rediscover_stack(stack)
            _LOGGER.info("Successfully discovered new stack: %s", stack[:50])
            
            # Clear the input field after successful discovery
            self._attr_native_value = ""
            self.async_write_ha_state()
            
        except Exception as exc:
            _LOGGER.error("Failed to discover new stack %s: %s", stack[:50], exc)
            # Keep the value so user can see what failed
            self._attr_native_value = value
            self.async_write_ha_state()
