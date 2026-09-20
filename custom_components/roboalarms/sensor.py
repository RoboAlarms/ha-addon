"""Diagnostic telemetry pushed by the panel; absent readings stay unknown."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, UnitOfTime
from homeassistant.helpers.entity import EntityCategory

from .entity import RoboAlarmsEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(RoboAlarmsSensor(entry.runtime_data, key) for key in ("uptime_s", "rssi"))


class RoboAlarmsSensor(RoboAlarmsEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, key):
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{self.panel_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_class = (
            SensorDeviceClass.DURATION if key == "uptime_s" else SensorDeviceClass.SIGNAL_STRENGTH
        )
        self._attr_native_unit_of_measurement = (
            UnitOfTime.SECONDS if key == "uptime_s" else SIGNAL_STRENGTH_DECIBELS_MILLIWATT
        )
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        data = self.coordinator.data
        return (data.status or {}).get(self._key) if data is not None else None
