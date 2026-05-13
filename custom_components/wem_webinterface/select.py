"""Select platform – writable string-list WEM parameters."""

from __future__ import annotations

import logging
from typing import List, Optional

from homeassistant.components.select import SelectEntity
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
        WemSelect(coordinator, p.stack, p.param_id)
        for p in coordinator.get_all_parameters()
        if p.param_type == "select"
    ]
    async_add_entities(entities)

    async def _on_new_params(stack, params):
        new_entities = [
            WemSelect(coordinator, stack, p.param_id)
            for p in params
            if p.param_type == "select"
        ]
        if new_entities:
            async_add_entities(new_entities)

    coordinator.register_new_param_callback(_on_new_params)


class WemSelect(WemBaseEntity, SelectEntity):
    """A writable string-list parameter on the WEM device."""

    @property
    def current_option(self) -> Optional[str]:
        info = self._coordinator.get_parameter(self._stack, self._param_id)
        return str(info.current_value) if (info and info.current_value is not None) else None

    @property
    def options(self) -> List[str]:
        info = self._coordinator.get_parameter(self._stack, self._param_id)
        return list(info.options) if (info and info.options) else []

    async def async_select_option(self, option: str) -> None:
        await self._coordinator.request_write(self._stack, self._param_id, option)
