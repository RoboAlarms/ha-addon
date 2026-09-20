"""The AlarmSystem integration.

A local-push integration for the AlarmSystem touchscreen alarm panel:
mDNS discovery, pairing confirmed on the panel's screen, then a mutual-TLS
connection carrying state, events and commands. No cloud, no MQTT broker.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

# Platforms are added here as they are built: alarm_control_panel,
# binary_sensor, sensor, switch, button, event, update.
PLATFORMS: list[Platform] = []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an AlarmSystem panel from a config entry."""
    # The connection and push coordinator arrive with the protocol client
    # (aioalarmsystem); until it exists the config flow cannot create an
    # entry, so this is never reached in practice.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
