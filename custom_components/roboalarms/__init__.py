"""The RoboAlarms Panel integration.

A local-push integration for the RoboAlarms Panel: mDNS discovery, pairing
confirmed on the panel's screen, then a mutual-TLS connection carrying
state, events and commands. No cloud, no MQTT broker.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry

from .aiopanel import CannotConnect, InvalidMessage, UnsupportedVersion
from .coordinator import RoboAlarmsConfigEntry, RoboAlarmsCoordinator, WrongPanel
from .entity import panel_device_info
from .services import async_register_services

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.COVER,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.UPDATE,
]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register alarm actions independently of device availability."""
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: RoboAlarmsConfigEntry) -> bool:
    """Connect to the panel and set up its entities."""
    coordinator = RoboAlarmsCoordinator(hass, entry)
    try:
        await coordinator.async_start()
    except WrongPanel as err:
        # The panel was factory-reset or replaced: pairing again is the only way
        # out. ConfigEntryAuthFailed is what starts HA's reauth flow from setup,
        # the same recovery a mismatch found on a running connection gets
        # (coordinator.py's reconnect loop) - never a silent, endless setup retry.
        raise ConfigEntryAuthFailed(str(err)) from err
    except (CannotConnect, InvalidMessage, UnsupportedVersion, TimeoutError) as err:
        raise ConfigEntryNotReady(str(err)) from err
    entry.runtime_data = coordinator
    # The panel's device first: its zones and Z-Wave outputs name it as their parent by its
    # registry id (entity.parent_of).
    assert coordinator.info is not None
    coordinator.panel_device_id = (
        dr.async_get(hass)
        .async_get_or_create(config_entry_id=entry.entry_id, **panel_device_info(coordinator.info))
        .id
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RoboAlarmsConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_shutdown()
    return ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: RoboAlarmsConfigEntry, device: DeviceEntry
) -> bool:
    """A person may delete one of the panel's Z-Wave devices the panel no longer lists.

    Those aren't removed by themselves (entity.py, RoboAlarmsZwaveEntity: the list is empty while
    the panel's radio starts). The panel itself, its zones and a Z-Wave device still listed stay.
    """
    from .const import DOMAIN
    from .entity import zwave_device_identifier

    coordinator = getattr(entry, "runtime_data", None)
    data = coordinator.data if coordinator is not None else None
    if coordinator is None or coordinator.info is None or data is None or data.zwave is None:
        return False
    listed = {zwave_device_identifier(coordinator.info.panel_id, key) for key in data.zwave}
    prefix = zwave_device_identifier(coordinator.info.panel_id, "zw_")
    for domain, identity in device.identifiers:
        if domain == DOMAIN and identity.startswith(prefix):
            return identity not in listed
    return False


async def async_remove_entry(hass: HomeAssistant, entry: RoboAlarmsConfigEntry) -> None:
    """Remove a compatibility repair when its panel entry is deleted."""
    from homeassistant.helpers import issue_registry as ir

    from .const import DOMAIN

    ir.async_delete_issue(hass, DOMAIN, f"unsupported_protocol_{entry.entry_id}")
