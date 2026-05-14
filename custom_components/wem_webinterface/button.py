"""Button entities for WEM Web Interface integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WemCoordinator
from .entity_base import WemEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button entities."""
    coordinator: WemCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    
    # Register callback to add buttons when new parameters are discovered
    def _on_new_params(stack: str, params: list) -> None:
        buttons = []
        for param in params:
            # Refresh button for each parameter
            buttons.append(WemParameterRefreshButton(coordinator, stack, param.param_id))
            # Re-discover button for each parameter
            buttons.append(WemParameterRediscoverButton(coordinator, stack, param.param_id))
        if buttons:
            async_add_entities(buttons)
    
    coordinator.register_new_param_callback(_on_new_params)


class WemParameterRefreshButton(WemEntity, ButtonEntity):
    """Button to refresh a single parameter value."""

    entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: WemCoordinator, stack: str, param_id: str) -> None:
        """Initialize the button."""
        super().__init__(coordinator, stack, param_id)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{stack}_{param_id}_refresh"
        self._attr_name = None  # Will be set by update_from_coordinator

    @property
    def name(self) -> str | None:
        """Return the name of the button."""
        info = self.coordinator.get_parameter(self._stack, self._param_id)
        if not info:
            return None
        return f"{info.name} (Refresh)"

    async def async_press(self) -> None:
        """Press the button to refresh the parameter value."""
        _LOGGER.debug("Refreshing parameter %s from stack %s", self._param_id, self._stack[:40])
        try:
            await self.coordinator._poll_stack(self._stack)
            _LOGGER.info("Successfully refreshed parameter %s", self._param_id)
        except Exception as exc:
            _LOGGER.error("Failed to refresh parameter %s: %s", self._param_id, exc)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        info = self.coordinator.get_parameter(self._stack, self._param_id)
        return info is not None and not self.coordinator.discovery_failed


class WemParameterRediscoverButton(WemEntity, ButtonEntity):
    """Button to re-discover a single parameter (refresh metadata, range, type, etc.)."""

    entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: WemCoordinator, stack: str, param_id: str) -> None:
        """Initialize the button."""
        super().__init__(coordinator, stack, param_id)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{stack}_{param_id}_rediscover"
        self._attr_name = None  # Will be set by update_from_coordinator

    @property
    def name(self) -> str | None:
        """Return the name of the button."""
        info = self.coordinator.get_parameter(self._stack, self._param_id)
        if not info:
            return None
        return f"{info.name} (Re-discover)"

    async def async_press(self) -> None:
        """Press the button to re-discover the parameter."""
        _LOGGER.debug("Re-discovering parameter %s from stack %s", self._param_id, self._stack[:40])
        try:
            await self.coordinator.async_rediscover_stack(self._stack)
            _LOGGER.info("Successfully re-discovered parameter %s", self._param_id)
        except Exception as exc:
            _LOGGER.error("Failed to re-discover parameter %s: %s", self._param_id, exc)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        info = self.coordinator.get_parameter(self._stack, self._param_id)
        return info is not None and not self.coordinator.discovery_failed
