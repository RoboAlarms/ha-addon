"""Every zone as a binary sensor, its own device under the panel (HAI-005).

Zones added on the panel appear without a restart; a zone that disappears
from the panel's snapshot is removed from the entity and device registries.
A zone's name and device class are read
live from the latest snapshot rather than captured once (HA08): renaming a
zone or reclassifying it on the panel updates the existing device and
entity here too, without a restart, while any area assignment or name the
person set in Home Assistant stays put.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .aiopanel import ZoneState
from .const import DOMAIN
from .coordinator import RoboAlarmsConfigEntry, RoboAlarmsCoordinator
from .entity import RoboAlarmsEntity

_DEVICE_CLASS = {
    "door": BinarySensorDeviceClass.DOOR,
    "window": BinarySensorDeviceClass.WINDOW,
    "garage_door": BinarySensorDeviceClass.GARAGE_DOOR,
    "motion": BinarySensorDeviceClass.MOTION,
    "smoke": BinarySensorDeviceClass.SMOKE,
    "co": BinarySensorDeviceClass.CO,
    "gas": BinarySensorDeviceClass.GAS,
    "heat": BinarySensorDeviceClass.HEAT,
    "moisture": BinarySensorDeviceClass.MOISTURE,
    "water": BinarySensorDeviceClass.MOISTURE,
    "vibration": BinarySensorDeviceClass.VIBRATION,
    "tamper": BinarySensorDeviceClass.TAMPER,
    "safety": BinarySensorDeviceClass.SAFETY,
    "sound": BinarySensorDeviceClass.SOUND,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoboAlarmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Zones from the first snapshot, and new ones as the panel gains them."""
    coordinator = entry.runtime_data
    known: set[int] = set()

    @callback
    def _sync_zones() -> None:
        data = coordinator.data
        if data is None or data.zones is None:
            return
        removed = known - set(data.zones)
        if removed:
            registry = er.async_get(hass)
            devices = dr.async_get(hass)
            for zone in removed:
                identity = f"{coordinator.info.panel_id}_z{zone}"
                device = next(
                    (
                        d
                        for d in dr.async_entries_for_config_entry(devices, entry.entry_id)
                        if (DOMAIN, identity) in d.identifiers
                    ),
                    None,
                )
                for entity in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
                    if entity.unique_id == identity or entity.unique_id.startswith(identity + "_"):
                        registry.async_remove(entity.entity_id)
                if device is not None:
                    devices.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
            known.difference_update(removed)
        new = [z for z in data.zones if z not in known]
        if new:
            known.update(new)
            async_add_entities(
                entity
                for z in new
                for entity in (
                    RoboAlarmsZone(coordinator, z),
                    *(RoboAlarmsZoneDiagnostic(coordinator, z, key) for key in _ZONE_DIAGNOSTICS),
                )
            )

    async_add_entities(RoboAlarmsPanelDiagnostic(coordinator, key) for key in _PANEL_DIAGNOSTICS)
    _sync_zones()
    entry.async_on_unload(coordinator.async_add_listener(_sync_zones))


class RoboAlarmsZone(RoboAlarmsEntity, BinarySensorEntity):
    """One zone: on = open / faulted."""

    def __init__(self, coordinator: RoboAlarmsCoordinator, zone: int) -> None:
        super().__init__(coordinator)
        self._attr_name = None  # the zone device carries the name
        self._zone = zone
        self._attr_unique_id = f"{self.panel_id}_z{zone}"
        state = self._state()
        assert state is not None
        self._device_identifiers = {(DOMAIN, f"{self.panel_id}_z{zone}")}
        self._device_name = state.name or f"Zone {zone}"
        # each zone is its own device so it can live in a room (Features/23)
        self._attr_device_info = DeviceInfo(
            identifiers=self._device_identifiers,
            name=self._device_name,
            manufacturer="RoboAlarms",
            model="Zone",
            via_device=(DOMAIN, self.panel_id),
        )

    def _state(self) -> ZoneState | None:
        data = self.coordinator.data
        if data is None or data.zones is None:
            return None
        return data.zones.get(self._zone)

    @callback
    def _handle_coordinator_update(self) -> None:
        """A fresh snapshot or delta: keep the device's name current (HA08)."""
        self._async_reconcile_device()
        super()._handle_coordinator_update()

    @callback
    def _async_reconcile_device(self) -> None:
        """Push a renamed zone's name to its device entry.

        Only the name is registry state; device_class is read live by the
        property below. `name_by_user` and the area are untouched fields
        (UNDEFINED, not passed) so a person's own choices survive.
        """
        state = self._state()
        if state is None:
            return
        name = state.name or f"Zone {self._zone}"
        if name == self._device_name:
            return
        self._device_name = name
        dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.coordinator.config_entry.entry_id,
            identifiers=self._device_identifiers,
            name=name,
        )

    @property
    def available(self) -> bool:
        return super().available and self._state() is not None

    @property
    def is_on(self) -> bool | None:
        state = self._state()
        return state.open if state is not None else None

    @property
    def device_class(self) -> BinarySensorDeviceClass | None:
        """Read live (not captured once) so a reclassification on the panel
        shows up here without a restart (HA08)."""
        state = self._state()
        return _DEVICE_CLASS.get(state.device_class) if state is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, bool] | None:
        state = self._state()
        if state is None:
            return None
        return {
            "bypassed": state.bypassed,
            "alarm": state.alarm,
            "trouble": state.trouble,
            "tamper": state.tamper,
            "low_battery": state.low_battery,
            "supervision": state.supervision,
        }


_ZONE_DIAGNOSTICS = {
    "tamper": BinarySensorDeviceClass.TAMPER,
    "low_battery": BinarySensorDeviceClass.BATTERY,
    "supervision": BinarySensorDeviceClass.PROBLEM,
}
_PANEL_DIAGNOSTICS = {
    "trouble": BinarySensorDeviceClass.PROBLEM,
    "ac_power": BinarySensorDeviceClass.POWER,
    "installer_mode": None,
}


class RoboAlarmsZoneDiagnostic(RoboAlarmsZone):
    """A separate diagnostic entity on the existing zone device."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, zone: int, key: str) -> None:
        super().__init__(coordinator, zone)
        self._key = key
        self._attr_unique_id += f"_{key}"
        self._attr_translation_key = key
        del self._attr_name

    @property
    def device_class(self):
        return _ZONE_DIAGNOSTICS[self._key]

    @property
    def is_on(self) -> bool | None:
        state = self._state()
        return getattr(state, self._key) if state is not None else None

    @property
    def extra_state_attributes(self):
        return None


class RoboAlarmsPanelDiagnostic(RoboAlarmsEntity, BinarySensorEntity):
    """Panel trouble, supply and programming state from pushed snapshots."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{self.panel_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_class = _PANEL_DIAGNOSTICS[key]

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        if self._key == "installer_mode":
            return any(p.raw.get("installer_mode", False) for p in (data.partitions or {}).values())
        if data.troubles is None:
            return None
        if self._key == "trouble":
            return bool(data.troubles.get("count", 0))
        system = data.troubles.get("system")
        return "ac_loss" not in system if isinstance(system, list) else None
