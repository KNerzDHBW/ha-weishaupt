"""Number entities for WEM integration."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity
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
        WemNumberEntity(coordinator, point_id)
        for point_id, point in coordinator.points.items()
        if point.writable and point.kind == "number" and coordinator.is_point_enabled(point)
    ]
    async_add_entities(entities)

    known = {entity._point_id for entity in entities}

    def _add_new_points() -> None:
        new_entities = []
        for point_id, point in coordinator.points.items():
            if point_id in known:
                continue
            if not point.writable or point.kind != "number":
                continue
            if not coordinator.is_point_enabled(point):
                continue
            known.add(point_id)
            new_entities.append(WemNumberEntity(coordinator, point_id))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.register_point_listener(_add_new_points))


class WemNumberEntity(WemPointEntity, NumberEntity):
    """Writable numeric WEM point."""

    def _current_numeric_value(self) -> float | None:
        value = self.point.value
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @property
    def native_value(self):
        current = self._current_numeric_value()
        return 0.0 if current is None else current

    @property
    def native_unit_of_measurement(self):
        return self.point.unit or None

    @property
    def native_min_value(self):
        minimum = self.point.min_value
        if minimum is None:
            return -1000000

        current = self._current_numeric_value()
        if current is not None and minimum > current:
            return -1000000
        return minimum

    @property
    def native_max_value(self):
        maximum = self.point.max_value
        if maximum is None:
            return 1000000

        current = self._current_numeric_value()
        if current is not None and maximum < current:
            return 1000000
        return maximum

    @property
    def native_step(self):
        return self.point.step if self.point.step is not None else 1.0

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_point_value(self._point_id, value)
