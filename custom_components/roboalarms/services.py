"""Alarm actions with explicit partition targeting and panel-side authorization."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

BASE = {
    vol.Required("entry_id"): str,
    vol.Required("code"): vol.All(str, vol.Match(r"^[0-9]{4,8}$")),
    vol.Optional("partition", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=8)),
}
SCHEMAS = {
    "bypass_zone": vol.Schema(
        {**BASE, vol.Required("zone"): vol.All(vol.Coerce(int), vol.Range(min=1, max=128))}
    ),
    "unbypass_zone": vol.Schema(
        {**BASE, vol.Required("zone"): vol.All(vol.Coerce(int), vol.Range(min=1, max=128))}
    ),
    "arm": vol.Schema(
        {
            **BASE,
            vol.Required("level"): vol.In(["stay", "away", "night"]),
            vol.Optional("silent_exit", default=False): bool,
            vol.Optional("no_entry_delay", default=False): bool,
        }
    ),
    "panic": vol.Schema({**BASE, vol.Required("panic"): vol.In(["police", "fire", "medical"])}),
}


def async_register_services(hass: HomeAssistant) -> None:
    """Register even when no panel is loaded so automations remain editable."""

    async def handle(call: ServiceCall) -> None:
        entry = hass.config_entries.async_get_entry(call.data["entry_id"])
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise HomeAssistantError("The selected panel is not connected")
        coordinator = entry.runtime_data
        fields = dict(call.data)
        fields.pop("entry_id")
        data = coordinator.data
        if data is None or fields["partition"] not in (data.partitions or {}):
            raise HomeAssistantError("The selected partition is not available")
        action = {"bypass_zone": "bypass", "unbypass_zone": "unbypass"}.get(
            call.service, call.service
        )
        if action == "arm":
            fields["flags"] = [key for key in ("silent_exit", "no_entry_delay") if fields.pop(key)]
        await coordinator.async_command(action, **fields)

    for name, schema in SCHEMAS.items():
        hass.services.async_register(DOMAIN, name, handle, schema=schema)
