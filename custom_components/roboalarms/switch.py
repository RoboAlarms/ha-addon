"""The chime as a switch, per partition (HAI-005).

The panel's chime command is a toggle checked by the engine's own rules:
only while disarmed, and with the installer's "chime needs a code" option
(its default) the panel refuses a codeless switch - the refusal shows as
the error, codes are never stored here.

The toggle carries no target state, and its "ok" result only means the
panel accepted it - the actual new value follows separately, as a delta
(protocol.md). Two requests for the same partition are therefore
serialized, and each one waits for that delta to confirm the panel's real
state before the next is allowed to read `is_on` (HA13): without that, two
`turn_on` calls issued close together can both see the old value and both
toggle, leaving the chime back where it started.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import RoboAlarmsConfigEntry, RoboAlarmsCoordinator
from .entity import RoboAlarmsPartitionEntity, async_add_partition_entities

_LOGGER = logging.getLogger(__name__)

# How long to wait for the panel's delta confirming a toggle before letting
# a queued request through anyway (HA13). Generous for real network jitter;
# still bounded so the switch can never hang forever on a missed delta.
_CONFIRM_TIMEOUT_S = 2.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoboAlarmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One chime switch per partition, including ones the panel adds later (HA09)."""
    coordinator = entry.runtime_data
    async_add_partition_entities(coordinator, entry, async_add_entities, RoboAlarmsChime)


class RoboAlarmsChime(RoboAlarmsPartitionEntity, SwitchEntity):
    """A beep at the panel when a door or window opens, while disarmed."""

    _attr_translation_key = "chime"

    def __init__(self, coordinator: RoboAlarmsCoordinator, partition: int) -> None:
        super().__init__(coordinator, partition)
        self._attr_unique_id = f"{self.panel_id}_p{partition}_chime"
        if partition != 1:
            self._attr_translation_placeholders = {"partition": f" {partition}"}
        else:
            self._attr_translation_placeholders = {"partition": ""}
        self._lock = asyncio.Lock()

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
        # One in-flight request at a time per partition: a second call
        # queues here instead of reading `is_on` while the first is still
        # waiting on the panel (HA13).
        async with self._lock:
            if self.is_on == on:
                return  # the panel's command is a toggle: don't flip it the wrong way
            await self.coordinator.async_command("chime", partition=self._partition)
            await self._await_confirmed(on)

    async def _await_confirmed(self, on: bool) -> None:
        """Wait until the coordinator's own data agrees with `on`.

        The command's "ok" result only means the panel accepted the toggle;
        the new value arrives on its own as a delta. Until that delta is
        folded into `coordinator.data`, `is_on` still reads the old value -
        a queued second request must not evaluate against that stale read,
        so this runs (holding the lock) before `_set` returns. A delta that
        never comes gives up after a while rather than blocking the switch.
        """
        if self.is_on == on:
            return
        confirmed = asyncio.Event()

        @callback
        def _check() -> None:
            if self.is_on == on:
                confirmed.set()

        remove = self.coordinator.async_add_listener(_check)
        try:
            async with asyncio.timeout(_CONFIRM_TIMEOUT_S):
                await confirmed.wait()
        except TimeoutError:
            _LOGGER.debug(
                "%s: no confirmed chime state within %ss of the toggle",
                self.entity_id,
                _CONFIRM_TIMEOUT_S,
            )
        finally:
            remove()
