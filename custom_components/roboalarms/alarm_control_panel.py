"""One alarm panel entity per partition (HAI-005, HAI-007).

The panel computes the Home Assistant state itself (`ha_state` in every
partition-status object, the same rule as its MQTT payloads): silent and
duress alarms never show here (HAI-008). Commands carry the code typed in
Home Assistant to the panel, which checks it like any keypad's.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import RoboAlarmsConfigEntry, RoboAlarmsCoordinator
from .entity import RoboAlarmsPartitionEntity, async_add_partition_entities

# The panel's events that arm or disarm a partition: each says who did it.
_CHANGES = frozenset({"armed", "disarmed", "auto_stay"})

_HA_STATE = {
    "disarmed": AlarmControlPanelState.DISARMED,
    "arming": AlarmControlPanelState.ARMING,
    "armed_home": AlarmControlPanelState.ARMED_HOME,
    "armed_away": AlarmControlPanelState.ARMED_AWAY,
    "armed_night": AlarmControlPanelState.ARMED_NIGHT,
    "pending": AlarmControlPanelState.PENDING,
    "triggered": AlarmControlPanelState.TRIGGERED,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoboAlarmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One entity per partition, including ones the panel adds later (HA09)."""
    coordinator = entry.runtime_data
    async_add_partition_entities(coordinator, entry, async_add_entities, RoboAlarmsPartition)


class RoboAlarmsPartition(RoboAlarmsPartitionEntity, AlarmControlPanelEntity):
    """A partition as Home Assistant's alarm panel."""

    _attr_name = None  # the device (partition 1: the panel) carries the name
    _attr_code_format = CodeFormat.NUMBER
    _attr_code_arm_required = True
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )

    def __init__(self, coordinator: RoboAlarmsCoordinator, partition: int) -> None:
        super().__init__(coordinator, partition)
        self._attr_unique_id = f"{self.panel_id}_p{partition}"
        if partition != 1:
            self._attr_name = f"Partition {partition}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(f"{DOMAIN}_event", self._handle_event))

    @callback
    def _handle_event(self, event: Any) -> None:
        """changed_by: who armed or disarmed this partition, as the panel's
        event names them - the user's name, else their number, else nobody
        (a quick arm, a schedule). Only this panel's events count (HA04), and
        a silent one never shows here (HAI-008)."""
        msg = event.data
        if msg.get("entry_id") != self.coordinator.config_entry.entry_id:
            return
        if msg.get("event_type") not in _CHANGES or msg.get("partition") != self._partition:
            return
        if msg.get("silent") is True:
            return
        name = msg.get("user_name")
        user = msg.get("user")
        if isinstance(name, str) and name:
            changed_by: str | None = name
        elif isinstance(user, int) and not isinstance(user, bool) and user > 0:
            changed_by = str(user)
        else:
            changed_by = None
        self._attr_changed_by = changed_by
        self.async_write_ha_state()

    @property
    @callback
    def alarm_state(self) -> AlarmControlPanelState | None:
        data = self.coordinator.data
        if data is None or data.partitions is None:
            return None
        part = data.partitions.get(self._partition)
        if part is None:
            return None
        return _HA_STATE.get(part.ha_state)

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self.coordinator.async_command("disarm", partition=self._partition, code=code)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self._arm("stay", code)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._arm("away", code)

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        await self._arm("night", code)

    async def _arm(self, level: str, code: str | None) -> None:
        await self.coordinator.async_command(
            "arm", level=level, partition=self._partition, code=code
        )
