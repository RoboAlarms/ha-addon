"""The panel's Z-Wave garage doors as covers that only close (protocol.md, API 1 addition).

A garage door is a relay and a tilt sensor on the panel. It never opens from Home Assistant:
opening takes a user's code at the panel (Features/24 HCTL-005), so the cover offers close
alone, and the panel refuses an open anyway (not_allowed). Whether it is open is its sensor's
word; the panel never reports "opening" (a tilt sensor can't see a door on its way).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import CoverDeviceClass, CoverEntity, CoverEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import RoboAlarmsConfigEntry
from .entity import RoboAlarmsZwaveEntity, async_add_zwave_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoboAlarmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """The panel's Z-Wave garage doors, including ones it lists later."""
    async_add_zwave_entities(
        entry.runtime_data, entry, async_add_entities, "garage", RoboAlarmsZwaveGarage
    )


class RoboAlarmsZwaveGarage(RoboAlarmsZwaveEntity, CoverEntity):
    """A garage door the panel runs: closes from here, opens only at the panel."""

    _attr_device_class = CoverDeviceClass.GARAGE
    _attr_supported_features = CoverEntityFeature.CLOSE

    @property
    def is_closed(self) -> bool | None:
        device = self._device()
        if device is None or device.state not in ("open", "closed"):
            return None
        return device.state == "closed"

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self.coordinator.async_zwave_set(self._key, 0)
