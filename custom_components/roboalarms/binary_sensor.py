"""Every zone as a binary sensor, its own device under the panel (HAI-005).

Zones added on the panel appear without a restart; a zone that disappears
from the panel's snapshot goes unavailable (stale-device cleanup follows
with diagnostics and repairs).
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
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
        new = [z for z in data.zones if z not in known]
        if new:
            known.update(new)
            async_add_entities(RoboAlarmsZone(coordinator, z) for z in new)

    _sync_zones()
    entry.async_on_unload(coordinator.async_add_listener(_sync_zones))


class RoboAlarmsZone(RoboAlarmsEntity, BinarySensorEntity):
    """One zone: on = open / faulted."""

    _attr_name = None  # the zone's device carries the name

    def __init__(self, coordinator: RoboAlarmsCoordinator, zone: int) -> None:
        super().__init__(coordinator)
        self._zone = zone
        self._attr_unique_id = f"{self.panel_id}_z{zone}"
        state = self._state()
        assert state is not None
        self._attr_device_class = _DEVICE_CLASS.get(state.device_class)
        # each zone is its own device so it can live in a room (Features/23)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self.panel_id}_z{zone}")},
            name=state.name or f"Zone {zone}",
            manufacturer="RoboAlarms",
            model="Zone",
            via_device=(DOMAIN, self.panel_id),
        )

    def _state(self) -> ZoneState | None:
        data = self.coordinator.data
        if data is None or data.zones is None:
            return None
        return data.zones.get(self._zone)

    @property
    def available(self) -> bool:
        return super().available and self._state() is not None

    @property
    def is_on(self) -> bool | None:
        state = self._state()
        return state.open if state is not None else None

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
