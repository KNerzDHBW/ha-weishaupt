"""Base entity for WEM Web Interface."""

from __future__ import annotations

from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import WemCoordinator


class WemBaseEntity(Entity):
    """Base class shared by sensor, number, and select entities."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator: WemCoordinator, stack: str, param_id: str) -> None:
        self._coordinator = coordinator
        self._stack = stack
        self._param_id = param_id
        info = coordinator.get_parameter(stack, param_id)
        self._attr_name = info.name if info else param_id
        self._attr_unique_id = (
            f"wem_{coordinator.ip_address}_{coordinator.make_key(stack, param_id)}"
        )
        # Group entities by the IP of the device
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.ip_address)},
            "name": f"WEM {coordinator.ip_address}",
            "manufacturer": "WEM",
            "model": "Web Interface",
            "configuration_url": coordinator.base_url,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        self._coordinator.register_update_callback(
            self._stack, self._param_id, self._handle_update
        )

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.unregister_update_callback(
            self._stack, self._param_id, self._handle_update
        )

    def _handle_update(self) -> None:
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Common properties
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        info = self._coordinator.get_parameter(self._stack, self._param_id)
        return info is not None and not info.discovery_failed
