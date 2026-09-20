"""Diagnostics download (HAI-011): panel info and link state, no keys."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_CLIENT_CERT, CONF_CLIENT_KEY, CONF_PANEL_FP
from .coordinator import RoboAlarmsConfigEntry

_REDACT = {CONF_CLIENT_KEY, CONF_CLIENT_CERT, CONF_PANEL_FP}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RoboAlarmsConfigEntry
) -> dict[str, Any]:
    """The entry, the panel's hello and the last pushed state."""
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "entry": async_redact_data(dict(entry.data), _REDACT),
        "hello": asdict(coordinator.info) if coordinator.info is not None else None,
        "link_up": coordinator.last_update_success,
        "state": {
            "seq": data.seq,
            "partitions": [p.raw for p in (data.partitions or {}).values()],
            "zones": [z.raw for z in (data.zones or {}).values()],
            "troubles": data.troubles,
        }
        if data is not None
        else None,
    }
