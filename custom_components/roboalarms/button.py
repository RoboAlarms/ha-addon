"""Restart the exit delay, per partition (HAI-005): more time to leave."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import RoboAlarmsConfigEntry, RoboAlarmsCoordinator
from .entity import RoboAlarmsPartitionEntity, async_add_partition_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoboAlarmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One exit-restart button per partition, including ones the panel adds later (HA09)."""
    coordinator = entry.runtime_data
    async_add_partition_entities(coordinator, entry, async_add_entities, RoboAlarmsExitRestart)


class RoboAlarmsExitRestart(RoboAlarmsPartitionEntity, ButtonEntity):
    """Start the exit time over while it is running."""

    _attr_translation_key = "exit_restart"

    def __init__(self, coordinator: RoboAlarmsCoordinator, partition: int) -> None:
        super().__init__(coordinator, partition)
        self._attr_unique_id = f"{self.panel_id}_p{partition}_exit_restart"
        if partition != 1:
            self._attr_translation_placeholders = {"partition": f" {partition}"}
        else:
            self._attr_translation_placeholders = {"partition": ""}

    async def async_press(self) -> None:
        await self.coordinator.async_command("exit_restart", partition=self._partition)
