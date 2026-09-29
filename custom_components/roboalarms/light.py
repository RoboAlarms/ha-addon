"""The panel's Z-Wave dimmers as lights (protocol.md, API 1 addition).

A dimmer's level is 0..100 on the panel and brightness 0..255 here. Turned on without a
brightness it goes back to the last level it had, or full when that isn't known. Its state comes
back from the panel's list, not from the answer to the command.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import RoboAlarmsConfigEntry, RoboAlarmsCoordinator
from .entity import RoboAlarmsZwaveEntity, async_add_zwave_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoboAlarmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """The panel's Z-Wave dimmers, including ones it lists later."""
    async_add_zwave_entities(
        entry.runtime_data, entry, async_add_entities, "light", RoboAlarmsZwaveLight
    )


def level_to_brightness(level: int) -> int:
    return round(level * 255 / 100)


def brightness_to_level(brightness: int) -> int:
    """At least 1: a brightness Home Assistant calls on never turns the light off."""
    return max(1, min(100, round(brightness * 100 / 255)))


class RoboAlarmsZwaveLight(RoboAlarmsZwaveEntity, LightEntity):
    """A Z-Wave dimmer the panel runs."""

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, coordinator: RoboAlarmsCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._last_level: int | None = None
        self._remember()

    @callback
    def _remember(self) -> None:
        device = self._device()
        if device is not None and device.level:
            self._last_level = device.level

    @callback
    def _handle_coordinator_update(self) -> None:
        self._remember()
        super()._handle_coordinator_update()

    @property
    def is_on(self) -> bool | None:
        device = self._device()
        if device is None or device.level is None:
            return None
        return device.level > 0

    @property
    def brightness(self) -> int | None:
        device = self._device()
        if device is None or device.level is None:
            return None
        return level_to_brightness(device.level)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_BRIGHTNESS in kwargs:
            level = brightness_to_level(kwargs[ATTR_BRIGHTNESS])
        else:
            level = self._last_level or 100
        await self.coordinator.async_zwave_set(self._key, level)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_zwave_set(self._key, 0)
