"""Shared entity plumbing: the panel device, availability and per-partition
reconciliation (HAI-005), and the panel's own Z-Wave outputs (protocol.md, API 1
addition)."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .aiopanel import ZwaveDevice
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
            name=info.name or "RoboAlarms",
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


def zwave_device_identifier(panel_id: str, key: str) -> str:
    """The device registry identifier of one of the panel's Z-Wave outputs."""
    return f"{panel_id}_{key}"


class RoboAlarmsZwaveEntity(RoboAlarmsEntity):
    """One of the panel's own Z-Wave outputs: its own device under the panel, named as the
    panel names it, available while the panel lists it as online.

    A device missing from the list is unavailable, never removed by itself: the panel sends an
    empty list while its Z-Wave radio starts or is away (zwave_service_ha_devices), and a removal
    then would drop the person's area and names. One that is really gone is deleted from its
    device page (async_remove_config_entry_device).
    """

    _attr_name = None  # the device carries the name

    def __init__(self, coordinator: RoboAlarmsCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        identity = zwave_device_identifier(self.panel_id, key)
        self._attr_unique_id = identity
        device = self._device()
        assert device is not None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identity)},
            name=device.name or key,
            manufacturer="RoboAlarms",
            model={"switch": "Z-Wave switch", "light": "Z-Wave dimmer"}.get(
                device.kind, "Z-Wave garage door"
            ),
            via_device=(DOMAIN, self.panel_id),
        )

    def _device(self) -> ZwaveDevice | None:
        data = self.coordinator.data
        if data is None or data.zwave is None:
            return None
        return data.zwave.get(self._key)

    @property
    def available(self) -> bool:
        device = self._device()
        return super().available and device is not None and device.online


def async_add_zwave_entities(
    coordinator: RoboAlarmsCoordinator,
    entry: RoboAlarmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    kind: str,
    factory: Callable[[RoboAlarmsCoordinator, str], RoboAlarmsZwaveEntity],
) -> None:
    """Add an entity for each of the panel's Z-Wave outputs of this kind, and for any it lists
    later (RoboAlarmsZwaveEntity says why nothing is removed here)."""
    known: set[str] = set()

    @callback
    def _sync() -> None:
        data = coordinator.data
        if data is None or data.zwave is None:
            return
        new = [k for k, d in data.zwave.items() if d.kind == kind and k not in known]
        if new:
            known.update(new)
            async_add_entities(factory(coordinator, k) for k in new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))
