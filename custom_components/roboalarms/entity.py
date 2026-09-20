"""Shared entity plumbing: the panel device and availability."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RoboAlarmsCoordinator


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
