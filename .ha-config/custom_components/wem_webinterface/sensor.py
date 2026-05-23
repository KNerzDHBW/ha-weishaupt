"""Sensor platform – read-only WEM parameters."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
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
    entities.append(WemLastSuccessfulReadSensor(coordinator))
    entities.append(WemLastSuccessfulSensorNameSensor(coordinator))
    entities.append(WemConsecutiveReadFailuresSensor(coordinator))
    entities.append(WemLastReadErrorSensor(coordinator))
    entities.append(WemStatusSensor(coordinator))
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


class WemLastSuccessfulReadSensor(SensorEntity):
    """Diagnostic sensor tracking the last successful read across all parameters."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Last Successful Read"
    _attr_icon = "mdi:clock-check-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: WemCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"wem_{coordinator.ip_address}_last_successful_read"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.ip_address)},
            "name": f"WEM {coordinator.ip_address}",
            "manufacturer": "WEM",
            "model": "Web Interface",
            "configuration_url": coordinator.base_url,
        }

    async def async_added_to_hass(self) -> None:
        self._coordinator.register_status_callback(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.unregister_status_callback(self._handle_update)

    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> Optional[datetime]:
        return self._coordinator.last_successful_read

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"sensor_name": self._coordinator.last_successful_sensor_name}


class WemLastSuccessfulSensorNameSensor(SensorEntity):
    """Diagnostic sensor naming the last parameter updated successfully."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Last Successful Sensor"
    _attr_icon = "mdi:form-textbox"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WemCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"wem_{coordinator.ip_address}_last_successful_sensor"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.ip_address)},
            "name": f"WEM {coordinator.ip_address}",
            "manufacturer": "WEM",
            "model": "Web Interface",
            "configuration_url": coordinator.base_url,
        }

    async def async_added_to_hass(self) -> None:
        self._coordinator.register_status_callback(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.unregister_status_callback(self._handle_update)

    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> Optional[str]:
        return self._coordinator.last_successful_sensor_name


class WemConsecutiveReadFailuresSensor(SensorEntity):
    """Diagnostic sensor counting consecutive failed read attempts."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Consecutive Read Failures"
    _attr_icon = "mdi:counter"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WemCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"wem_{coordinator.ip_address}_consecutive_read_failures"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.ip_address)},
            "name": f"WEM {coordinator.ip_address}",
            "manufacturer": "WEM",
            "model": "Web Interface",
            "configuration_url": coordinator.base_url,
        }

    async def async_added_to_hass(self) -> None:
        self._coordinator.register_status_callback(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.unregister_status_callback(self._handle_update)

    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> int:
        return self._coordinator.consecutive_read_failures


class WemLastReadErrorSensor(SensorEntity):
    """Diagnostic sensor exposing the last read error."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Last Read Error"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WemCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"wem_{coordinator.ip_address}_last_read_error"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.ip_address)},
            "name": f"WEM {coordinator.ip_address}",
            "manufacturer": "WEM",
            "model": "Web Interface",
            "configuration_url": coordinator.base_url,
        }

    async def async_added_to_hass(self) -> None:
        self._coordinator.register_status_callback(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.unregister_status_callback(self._handle_update)

    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> Optional[str]:
        return self._coordinator.last_read_error


class WemStatusSensor(SensorEntity):
    """Diagnostic sensor exposing the coordinator runtime status."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Status"
    _attr_icon = "mdi:state-machine"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WemCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"wem_{coordinator.ip_address}_status"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.ip_address)},
            "name": f"WEM {coordinator.ip_address}",
            "manufacturer": "WEM",
            "model": "Web Interface",
            "configuration_url": coordinator.base_url,
        }

    async def async_added_to_hass(self) -> None:
        self._coordinator.register_status_callback(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.unregister_status_callback(self._handle_update)

    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        return self._coordinator.status_value
