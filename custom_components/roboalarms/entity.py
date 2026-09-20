"""Shared entity plumbing: the panel device, availability and per-partition
reconciliation (HAI-005)."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RoboAlarmsConfigEntry, RoboAlarmsCoordinator


class RoboAlarmsEntity(CoordinatorEntity[RoboAlarmsCoordinator]):
    """An entity of the panel itself (zones carry their own devices)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RoboAlarmsCoordinator) -> None:
        super().__init__(coordinator)
        info = coordinator.info
        assert info is not None
        self.panel_id = info.panel_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, info.panel_id)},
            name=info.name or "RoboAlarms Panel",
            manufacturer="RoboAlarms",
            model=info.model or None,
            sw_version=info.fw or None,
        )


class RoboAlarmsPartitionEntity(RoboAlarmsEntity):
    """An entity that exists for exactly as long as its partition does.

    alarm_control_panel, switch (chime) and button (exit restart) are each
    one-per-partition; this is the identity both share (HA09): unavailable,
    and therefore (Home Assistant drops unavailable entities before calling
    a service) unable to issue commands, once the panel no longer reports
    that partition. A partition that comes back reuses the same entity.
    """

    def __init__(self, coordinator: RoboAlarmsCoordinator, partition: int) -> None:
        super().__init__(coordinator)
        self._partition = partition

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return (
            super().available
            and data is not None
            and data.partitions is not None
            and self._partition in data.partitions
        )


def async_add_partition_entities(
    coordinator: RoboAlarmsCoordinator,
    entry: RoboAlarmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[RoboAlarmsCoordinator, int], RoboAlarmsPartitionEntity],
) -> None:
    """Add one entity per partition, and any the panel gains later (HA09).

    binary_sensor.py already does this for zones; alarm_control_panel,
    switch and button share this helper rather than each creating entities
    only from the first snapshot, so a partition enabled at runtime gets
    its controls without a reload. A partition that stops being reported is
    left registered - it goes unavailable through
    RoboAlarmsPartitionEntity.available. Zone devices are removed separately
    by binary_sensor.py when an authoritative snapshot removes them.
    """
    known: set[int] = set()

    @callback
    def _sync() -> None:
        data = coordinator.data
        if data is None or data.partitions is None:
            return
        new = [p for p in data.partitions if p not in known]
        if new:
            known.update(new)
            async_add_entities(factory(coordinator, p) for p in new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))
