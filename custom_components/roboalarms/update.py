"""Firmware release status; installation is authorized on the panel."""

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import RoboAlarmsEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([RoboAlarmsUpdate(entry.runtime_data)])


class RoboAlarmsUpdate(RoboAlarmsEntity, UpdateEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_translation_key = "firmware"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.panel_id}_firmware"

    def _status(self):
        data = self.coordinator.data
        return (data.status or {}).get("update", {}) if data is not None else {}

    @property
    def installed_version(self):
        return self._status().get("installed") or self.coordinator.info.fw or None

    @property
    def latest_version(self):
        return self._status().get("latest") or None

    @property
    def in_progress(self):
        return self._status().get("in_progress", False)
