"""The panel's own Z-Wave outputs as switches, lights and garage covers (protocol.md, API 1
addition; Features/23, BL-043): the list the panel sends, the commands it takes, what it refuses.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from test_init import StatePanel, _setup

from custom_components.roboalarms import async_remove_config_entry_device
from custom_components.roboalarms.aiopanel import InvalidMessage, PanelState
from custom_components.roboalarms.const import DOMAIN
from custom_components.roboalarms.light import brightness_to_level, level_to_brightness

pytestmark = pytest.mark.usefixtures("socket_enabled")

RELAY = {"key": "zw_c0ffee01_3_1_0", "kind": "switch", "name": "Pump", "level": 0, "online": True}
DIMMER = {
    "key": "zw_c0ffee01_4_1_0",
    "kind": "light",
    "name": "Porch light",
    "level": 40,
    "online": True,
}
GARAGE = {
    "key": "zw_c0ffee01_6_1_1",
    "kind": "garage",
    "name": "Garage opener",
    "level": None,
    "online": True,
    "state": "closed",
}


async def settle() -> None:
    await asyncio.sleep(0.15)


def device(hass: HomeAssistant, entry, identity: str) -> dr.DeviceEntry:
    """The entry's device with this identifier (identifiers are unique per entry only)."""
    found = [
        d
        for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        if (DOMAIN, identity) in d.identifiers
    ]
    assert len(found) == 1, identity
    return found[0]


def devices(*items: dict) -> dict:
    return {"t": "zwave_devices", "devices": list(items)}


async def _started(hass: HomeAssistant, panel: StatePanel):
    panel.zwave = [RELAY, DIMMER, GARAGE]
    entry = await _setup(hass, panel)
    await settle()
    return entry


async def test_each_output_becomes_its_entity(hass: HomeAssistant) -> None:
    """A switch, a dimmer with its brightness, and a garage door that only closes, each its own
    device under the panel."""
    async with StatePanel() as panel:
        entry = await _started(hass, panel)
        relay = hass.states.get("switch.pump")
        assert relay is not None and relay.state == STATE_OFF
        light = hass.states.get("light.porch_light")
        assert light is not None and light.state == STATE_ON
        assert light.attributes["brightness"] == level_to_brightness(40) == 102
        garage = hass.states.get("cover.garage_opener")
        assert garage is not None and garage.state == "closed"
        assert garage.attributes["device_class"] == "garage"
        assert garage.attributes["supported_features"] == 2  # CoverEntityFeature.CLOSE only
        dimmer = device(hass, entry, "0a1b2c_zw_c0ffee01_4_1_0")
        assert dimmer.name == "Porch light"
        assert dimmer.via_device_id == device(hass, entry, "0a1b2c").id
        await hass.config_entries.async_unload(entry.entry_id)


async def test_commands_carry_the_level(hass: HomeAssistant) -> None:
    """On is 100 and off 0 for a switch; a light's brightness as a level, and back to its last
    level when turned on without one; a garage closes with 0."""
    async with StatePanel() as panel:
        entry = await _started(hass, panel)
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.pump"}, blocking=True
        )
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": "light.porch_light", "brightness": 128}, blocking=True
        )
        await hass.services.async_call(
            "light", "turn_off", {"entity_id": "light.porch_light"}, blocking=True
        )
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": "light.porch_light"}, blocking=True
        )
        await hass.services.async_call(
            "cover", "close_cover", {"entity_id": "cover.garage_opener"}, blocking=True
        )
        sent = [(m["key"], m["level"]) for m in panel.zwave_sets]
        assert sent == [
            ("zw_c0ffee01_3_1_0", 100),
            ("zw_c0ffee01_4_1_0", 50),
            ("zw_c0ffee01_4_1_0", 0),
            ("zw_c0ffee01_4_1_0", 40),
            ("zw_c0ffee01_6_1_1", 0),
        ]
        assert all(m.get("id") for m in panel.zwave_sets)
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_garage_never_opens_from_here(hass: HomeAssistant) -> None:
    """Opening takes a user's code at the panel (HCTL-005): no open action, nothing sent."""
    async with StatePanel() as panel:
        entry = await _started(hass, panel)
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "cover", "open_cover", {"entity_id": "cover.garage_opener"}, blocking=True
            )
        assert panel.zwave_sets == []
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_refusal_is_an_error(hass: HomeAssistant) -> None:
    """not_taken (busy, the radio away) or not_allowed reach the person as an error."""
    async with StatePanel() as panel:
        entry = await _started(hass, panel)
        panel.zwave_error = "not_taken"
        with pytest.raises(HomeAssistantError, match="not taken"):
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": "switch.pump"}, blocking=True
            )
        await hass.config_entries.async_unload(entry.entry_id)


async def test_the_state_follows_the_panels_list(hass: HomeAssistant) -> None:
    """An accepted command changes nothing here until the panel's list says so."""
    async with StatePanel() as panel:
        entry = await _started(hass, panel)
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.pump"}, blocking=True
        )
        assert hass.states.get("switch.pump").state == STATE_OFF
        panel.push(devices({**RELAY, "level": 100}, DIMMER, GARAGE))
        await settle()
        assert hass.states.get("switch.pump").state == STATE_ON
        panel.push(devices(RELAY, DIMMER, {**GARAGE, "state": "open"}))
        await settle()
        assert hass.states.get("cover.garage_opener").state == "open"
        panel.push(devices(RELAY, DIMMER, {**GARAGE, "state": "unknown"}))
        await settle()
        assert hass.states.get("cover.garage_opener").state == "unknown"
        await hass.config_entries.async_unload(entry.entry_id)


async def test_offline_or_missing_is_unavailable_never_removed(hass: HomeAssistant) -> None:
    """Offline shows unavailable; a device missing from the list (the panel's radio starting)
    too, and it comes back as it was."""
    async with StatePanel() as panel:
        entry = await _started(hass, panel)
        panel.push(devices({**RELAY, "online": False}, DIMMER, GARAGE))
        await settle()
        assert hass.states.get("switch.pump").state == STATE_UNAVAILABLE
        panel.push(devices())
        await settle()
        for entity_id in ("switch.pump", "light.porch_light", "cover.garage_opener"):
            assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
        panel.push(devices(RELAY, DIMMER, GARAGE))
        await settle()
        assert hass.states.get("light.porch_light").state == STATE_ON
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_device_listed_later_is_added(hass: HomeAssistant) -> None:
    async with StatePanel() as panel:
        panel.zwave = [RELAY]
        entry = await _setup(hass, panel)
        await settle()
        assert hass.states.get("light.porch_light") is None
        panel.push(devices(RELAY, DIMMER))
        await settle()
        assert hass.states.get("light.porch_light").state == STATE_ON
        await hass.config_entries.async_unload(entry.entry_id)


async def test_only_an_unlisted_device_can_be_deleted(hass: HomeAssistant) -> None:
    async with StatePanel() as panel:
        entry = await _started(hass, panel)
        dimmer = device(hass, entry, "0a1b2c_zw_c0ffee01_4_1_0")
        panel_device = device(hass, entry, "0a1b2c")
        assert not await async_remove_config_entry_device(hass, entry, dimmer)
        assert not await async_remove_config_entry_device(hass, entry, panel_device)
        panel.push(devices(RELAY, GARAGE))
        await settle()
        assert await async_remove_config_entry_device(hass, entry, dimmer)
        assert not await async_remove_config_entry_device(hass, entry, panel_device)
        await hass.config_entries.async_unload(entry.entry_id)


def test_the_list_is_checked_whole() -> None:
    """Unknown kinds are left out, an unknown garage word reads unknown, a bad device or too many
    of them refuse the whole message and leave the state as it was."""
    state = PanelState()
    state.apply_zwave(devices(DIMMER, {**RELAY, "kind": "thermostat"}, {**GARAGE, "state": "ajar"}))
    assert sorted(state.zwave) == ["zw_c0ffee01_4_1_0", "zw_c0ffee01_6_1_1"]
    assert state.zwave["zw_c0ffee01_6_1_1"].state == "unknown"
    before = dict(state.zwave)
    for bad in (
        devices({**DIMMER, "level": 101}),
        devices({**DIMMER, "key": ""}),
        devices({**DIMMER, "online": "yes"}),
        devices(*[{**RELAY, "key": f"zw_{i}"} for i in range(65)]),
        {"t": "zwave_devices", "devices": {"not": "a list"}},
    ):
        with pytest.raises(InvalidMessage):
            state.apply_zwave(bad)
        assert state.zwave == before


def test_the_panels_own_golden_message() -> None:
    """The exact list the firmware's test_link.c (test_zwave_devices_golden) writes: an online
    dimmer at 40 %, an offline garage door whose level is unknown and whose sensor says closed."""
    golden = json.loads(
        '{"t":"zwave_devices","devices":[{"key":"zw_c0ffee01_4_1_0","kind":"light",'
        '"name":"Porch light","level":40,"online":true},{"key":"zw_c0ffee01_6_1_1",'
        '"kind":"garage","name":"Garage door","level":null,"online":false,'
        '"state":"closed"}]}'
    )
    state = PanelState()
    state.apply_zwave(golden)
    light = state.zwave["zw_c0ffee01_4_1_0"]
    assert (light.kind, light.name, light.level, light.online, light.state) == (
        "light",
        "Porch light",
        40,
        True,
        "",
    )
    garage = state.zwave["zw_c0ffee01_6_1_1"]
    assert (garage.kind, garage.level, garage.online, garage.state) == (
        "garage",
        None,
        False,
        "closed",
    )


def test_brightness_and_level() -> None:
    assert brightness_to_level(255) == 100
    assert brightness_to_level(1) == 1  # a light turned on is never sent 0
    assert level_to_brightness(100) == 255
