"""The chime as a switch, per partition (HAI-005).

The panel's chime command is a toggle checked by the engine's own rules:
only while disarmed, and with the installer's "chime needs a code" option
(its default) the panel refuses a codeless switch - the refusal shows as
the error, codes are never stored here.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import RoboAlarmsConfigEntry, RoboAlarmsCoordinator
from .entity import RoboAlarmsEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoboAlarmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One chime switch per partition in the first snapshot."""
    coordinator = entry.runtime_data
    assert coordinator.data is not None and coordinator.data.partitions is not None
    async_add_entities(RoboAlarmsChime(coordinator, p) for p in coordinator.data.partitions)


class RoboAlarmsChime(RoboAlarmsEntity, SwitchEntity):
    """A beep at the panel when a door or window opens, while disarmed."""

    _attr_translation_key = "chime"

    def __init__(self, coordinator: RoboAlarmsCoordinator, partition: int) -> None:
        super().__init__(coordinator)
        self._partition = partition
        self._attr_unique_id = f"{self.panel_id}_p{partition}_chime"
        if partition != 1:
            self._attr_translation_placeholders = {"partition": f" {partition}"}
        else:
            self._attr_translation_placeholders = {"partition": ""}

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None or data.partitions is None:
            return None
        part = data.partitions.get(self._partition)
        return part.chime if part is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, on: bool) -> None:
        if self.is_on == on:
            return  # the panel's command is a toggle: don't flip it the wrong way
        await self.coordinator.async_command("chime", partition=self._partition)
