"""Number platform – writable numeric WEM parameters."""

from __future__ import annotations

import logging
from typing import Optional

from homeassistant.components.number import NumberEntity, NumberMode
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
        WemNumber(coordinator, p.stack, p.param_id)
        for p in coordinator.get_all_parameters()
        if p.param_type == "number"
    ]
    async_add_entities(entities)

    async def _on_new_params(stack, params):
        new_entities = [
            WemNumber(coordinator, stack, p.param_id)
            for p in params
            if p.param_type == "number"
        ]
        if new_entities:
            async_add_entities(new_entities)

    coordinator.register_new_param_callback(_on_new_params)


class WemNumber(WemBaseEntity, NumberEntity):
    """A writable numeric parameter on the WEM device."""

    _attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> Optional[float]:
        info = self._coordinator.get_parameter(self._stack, self._param_id)
        if info and info.current_value is not None:
            try:
                return float(info.current_value)
            except (ValueError, TypeError):
                return None
        return None

    @property
    def native_min_value(self) -> float:
        info = self._coordinator.get_parameter(self._stack, self._param_id)
        return info.min_value if (info and info.min_value is not None) else 0.0

    @property
    def native_max_value(self) -> float:
        info = self._coordinator.get_parameter(self._stack, self._param_id)
        return info.max_value if (info and info.max_value is not None) else 100.0

    @property
    def native_step(self) -> float:
        info = self._coordinator.get_parameter(self._stack, self._param_id)
        return info.step if (info and info.step is not None) else 1.0

    @property
    def native_unit_of_measurement(self) -> Optional[str]:
        info = self._coordinator.get_parameter(self._stack, self._param_id)
        return (info.unit or None) if info else None

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.request_write(self._stack, self._param_id, value)
