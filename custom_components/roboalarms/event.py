"""The panel's events as an event entity, for automations (HAI-005).

Every reportable event the panel pushes lands here with its fields as
attributes. Silent events (duress, silent panic) arrive marked silent and
never change any alarm state (HAI-008): whether an automation reacts to
them is the user's decision.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import RoboAlarmsConfigEntry, RoboAlarmsCoordinator
from .entity import RoboAlarmsEntity

# alarm_strings.c alarm_event_name(): an external contract of the panel (add, never rename).
EVENT_TYPES = [
    "system_start", "armed", "arm_failed", "disarmed", "auto_stay", "exit_restart",
    "entry_delay", "alarm", "alarm_restore", "exit_error", "recent_closing", "alarm_aborted",
    "alarm_canceled", "alarm_silenced", "bell_timeout", "swinger_shutdown", "memory_cleared",
    "bypass", "unbypass", "trouble", "trouble_restore", "troubles_acked", "duress",
    "invalid_code", "lockout", "chime_on", "chime_off", "walk_test_start", "walk_test_end",
    "walk_test_zone", "monitor_fault", "monitor_restore", "user_changed", "user_deleted",
    "config_changed", "user_updated", "program_enter", "program_exit", "factory_reset",
    "firmware_update",
]  # fmt: skip


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoboAlarmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One event entity for the panel."""
    async_add_entities([RoboAlarmsEvent(entry.runtime_data)])


class RoboAlarmsEvent(RoboAlarmsEntity, EventEntity):
    """Everything the panel reports, as it happens."""

    _attr_translation_key = "panel_event"
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator: RoboAlarmsCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.panel_id}_event"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hass.bus.async_listen(f"{DOMAIN}_event", self._handle_panel_event)
        )

    @callback
    def _handle_panel_event(self, event: Any) -> None:
        msg: dict[str, Any] = dict(event.data)
        event_type = str(msg.pop("event_type", ""))
        if event_type not in EVENT_TYPES:
            return  # a newer panel: an unknown event rides along, never crashes
        msg.pop("t", None)
        self._trigger_event(event_type, msg)
        self.async_write_ha_state()
