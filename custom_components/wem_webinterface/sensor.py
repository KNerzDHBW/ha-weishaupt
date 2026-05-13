"""Sensor platform – read-only WEM parameters."""

from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WemCoordinator
from .entity_base import WemBaseEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WemCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        WemSensor(coordinator, p.stack, p.param_id)
        for p in coordinator.get_all_parameters()
        if p.param_type == "readonly"
    ]
    async_add_entities(entities)

    # Also handle parameters discovered later (re-discovery)
    async def _on_new_params(stack, params):
        new_entities = [
            WemSensor(coordinator, stack, p.param_id)
            for p in params
            if p.param_type == "readonly"
        ]
        if new_entities:
            async_add_entities(new_entities)

    coordinator.register_new_param_callback(_on_new_params)


class WemSensor(WemBaseEntity, SensorEntity):
    """A read-only measurement from the WEM device."""

    @property
    def native_value(self) -> Any:
        info = self._coordinator.get_parameter(self._stack, self._param_id)
        return info.current_value if info else None

    @property
    def native_unit_of_measurement(self) -> Optional[str]:
        info = self._coordinator.get_parameter(self._stack, self._param_id)
        return (info.unit or None) if info else None

    @property
    def state_class(self) -> Optional[str]:
        info = self._coordinator.get_parameter(self._stack, self._param_id)
        if info and isinstance(info.current_value, (int, float)):
            return "measurement"
        return None
