"""The RoboAlarms Panel integration.

A local-push integration for the RoboAlarms Panel: mDNS discovery, pairing
confirmed on the panel's screen, then a mutual-TLS connection carrying
state, events and commands. No cloud, no MQTT broker.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .aiopanel import CannotConnect, InvalidMessage, UnsupportedVersion
from .coordinator import RoboAlarmsConfigEntry, RoboAlarmsCoordinator, WrongPanel

PLATFORMS: list[Platform] = [Platform.ALARM_CONTROL_PANEL, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: RoboAlarmsConfigEntry) -> bool:
    """Connect to the panel and set up its entities."""
    coordinator = RoboAlarmsCoordinator(hass, entry)
    try:
        await coordinator.async_start()
    except WrongPanel as err:
        # The panel was factory-reset or replaced: pairing again is the only way out.
        raise ConfigEntryNotReady(str(err)) from err
    except (CannotConnect, InvalidMessage, UnsupportedVersion, TimeoutError) as err:
        raise ConfigEntryNotReady(str(err)) from err
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RoboAlarmsConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_shutdown()
    return ok
