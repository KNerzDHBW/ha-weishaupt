"""Sensor entities for WEM integration."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
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

    entities: list[SensorEntity] = [
        WemReadOnlySensor(coordinator, point_id)
        for point_id, point in coordinator.points.items()
        if not point.writable and coordinator.is_point_enabled(point)
    ]
    entities.extend(
        [
            WemLoggedInSensor(coordinator),
            WemStateSensor(coordinator),
            WemLastRefreshSensor(coordinator),
            WemSetupPhaseSensor(coordinator),
            WemLastUpdateSensor(coordinator),
            WemStackRecoveriesSensor(coordinator),
            WemConsecutiveFailuresSensor(coordinator),
        ]
    )
    async_add_entities(entities)

    known: set[str] = {entity._point_id for entity in entities if isinstance(entity, WemReadOnlySensor)}

    def _add_new_points() -> None:
        new_entities: list[SensorEntity] = []
        for point_id, point in coordinator.points.items():
            if point.writable or point_id in known:
                continue
            if not coordinator.is_point_enabled(point):
                continue
            known.add(point_id)
            new_entities.append(WemReadOnlySensor(coordinator, point_id))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.register_point_listener(_add_new_points))


class WemReadOnlySensor(WemPointEntity, SensorEntity):
    """Sensor for read-only WEM values."""

    @property
    def native_value(self):
        return self.point.value

    @property
    def native_unit_of_measurement(self):
        return self.point.unit or None


class WemLoggedInSensor(SensorEntity):
    """Status sensor: logged in."""

    _attr_has_entity_name = True
    _attr_name = "Logged in"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WemCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_logged_in"

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        return self.coordinator.logged_in

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))


class WemStateSensor(SensorEntity):
    """Status sensor: summarized runtime state."""

    _attr_has_entity_name = True
    _attr_name = "State"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WemCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_state"

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        return self.coordinator.state_text

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "logged_in": self.coordinator.logged_in,
            "is_updating": self.coordinator.is_updating,
            "consecutive_failures": self.coordinator.consecutive_failures,
            "last_error": self.coordinator.last_error,
            "last_update_page": self.coordinator.last_update_page,
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))


class WemLastRefreshSensor(SensorEntity):
    """Status sensor: timestamp of last successful refresh."""

    _attr_has_entity_name = True
    _attr_name = "Last Refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WemCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_last_refresh"

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        return self.coordinator.last_refresh or "unknown"

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "last_update_page": self.coordinator.last_update_page,
            "logged_in": self.coordinator.logged_in,
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))


class WemLastUpdateSensor(SensorEntity):
    """Status sensor: last read page."""

    _attr_has_entity_name = True
    _attr_name = "Last Update"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WemCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_last_update"

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        return self.coordinator.last_update_page

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))


class WemSetupPhaseSensor(SensorEntity):
    """Status sensor: current setup phase."""

    _attr_has_entity_name = True
    _attr_name = "Setup Phase"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WemCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_setup_phase"

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        return self.coordinator.setup_phase

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))


class WemConsecutiveFailuresSensor(SensorEntity):
    """Status sensor: consecutive failures."""

    _attr_has_entity_name = True
    _attr_name = "Consecutive Failures"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WemCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_consecutive_failures"

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        return self.coordinator.consecutive_failures

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))


class WemStackRecoveriesSensor(SensorEntity):
    """Status sensor: total successful stack recoveries."""

    _attr_has_entity_name = True
    _attr_name = "Stack Recoveries"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WemCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_stack_recoveries"

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        return self.coordinator.stack_recoveries

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))
