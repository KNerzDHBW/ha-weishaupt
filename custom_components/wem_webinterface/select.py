"""Select entities for WEM integration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
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
        WemSelectEntity(coordinator, point_id)
        for point_id, point in coordinator.points.items()
        if point.writable and point.kind == "select" and coordinator.is_point_enabled(point)
    ]
    async_add_entities(entities)

    known = {entity._point_id for entity in entities}

    def _add_new_points() -> None:
        new_entities = []
        for point_id, point in coordinator.points.items():
            if point_id in known:
                continue
            if not point.writable or point.kind != "select":
                continue
            if not coordinator.is_point_enabled(point):
                continue
            known.add(point_id)
            new_entities.append(WemSelectEntity(coordinator, point_id))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.register_point_listener(_add_new_points))


class WemSelectEntity(WemPointEntity, SelectEntity):
    """Writable selection WEM point."""

    @property
    def options(self) -> list[str]:
        return list(self.point.options)

    @property
    def current_option(self):
        value = str(self.point.value)
        if value in self.point.options:
            return value
        if self.point.options:
            return self.point.options[0]
        return "unknown"

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_point_value(self._point_id, option)
