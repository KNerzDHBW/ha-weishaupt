"""Entity base classes for WEM integration."""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WemCoordinator


class WemPointEntity(CoordinatorEntity[WemCoordinator]):
    """Base entity for one WEM point."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: WemCoordinator, point_id: str) -> None:
        super().__init__(coordinator)
        self._point_id = point_id

    @property
    def point(self):
        return self.coordinator.points[self._point_id]

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.entry.entry_id}_{self._point_id}"

    @property
    def name(self) -> str:
        return self.point.full_name

    @property
    def available(self) -> bool:
        return self.coordinator.is_point_enabled(self.point)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "menu": self.point.menu,
            "submenu": self.point.submenu,
            "source_stack": self.point.source_stack,
            "writable": self.point.writable,
            "last_read": self.coordinator.last_read.get(self._point_id),
        }

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.coordinator.entry.entry_id)},
            "name": "WEM Webinterface",
            "manufacturer": "Weishaupt",
            "model": "WEM",
            "configuration_url": self.coordinator.base_url,
        }
