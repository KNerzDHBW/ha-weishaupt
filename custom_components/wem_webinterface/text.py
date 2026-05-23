"""Text entities for WEM integration."""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WemCoordinator
from .entity_base import WemPointEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WemCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        WemTextEntity(coordinator, point_id)
        for point_id, point in coordinator.points.items()
        if point.writable and point.kind == "text" and coordinator.is_point_enabled(point)
    ]
    async_add_entities(entities)

    known = {entity._point_id for entity in entities}

    def _add_new_points() -> None:
        new_entities = []
        for point_id, point in coordinator.points.items():
            if point_id in known:
                continue
            if not point.writable or point.kind != "text":
                continue
            if not coordinator.is_point_enabled(point):
                continue
            known.add(point_id)
            new_entities.append(WemTextEntity(coordinator, point_id))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.register_point_listener(_add_new_points))


class WemTextEntity(WemPointEntity, TextEntity):
    """Writable text WEM point."""

    @property
    def native_value(self) -> str:
        return str(self.point.value)

    async def async_set_value(self, value: str) -> None:
        await self.coordinator.async_set_point_value(self._point_id, value)
