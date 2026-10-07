"""The integration's bench findings (panel repo BL-173) and a device's values over the
link (panel repo BL-171), against the fake panel of test_init.py."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import async_mock_service
from test_init import (  # noqa: F401 - the fixture is used by name
    StatePanel,
    _call,
    _setup,
    light_integration_first,
)

from custom_components.roboalarms.const import CONF_CONTROL_ENTITIES, CONF_SHARE_ENTITIES, DOMAIN
from custom_components.roboalarms.event import EVENT_TYPES

pytestmark = pytest.mark.usefixtures("socket_enabled")

ALARM = "alarm_control_panel.home"


async def _wait_until(check: Any, what: str) -> None:
    """The panel's messages travel a real socket: give them a moment."""
    for _ in range(100):
        await asyncio.sleep(0.02)
        if check():
            return
    raise AssertionError(what)


# ---- keepalive -------------------------------------------------------------------------


async def test_answers_the_panels_ping(hass: HomeAssistant) -> None:
    """HAI-006, protocol.md: ping and pong go both ways - the panel's ping gets a pong."""
    async with StatePanel() as panel:
        await _setup(hass, panel)
        assert panel.got("pong") == []
        panel.push({"t": "ping"})
        assert await panel.wait_for("pong") == {"t": "pong"}
        panel.push({"t": "ping"})
        await panel.wait_for("pong", 2)
        assert panel.connections == 1


# ---- who armed or disarmed -------------------------------------------------------------


def _event(event_type: str, **fields: Any) -> dict[str, Any]:
    """A panel event as docs/features/10 shapes it (user 0 travels as null)."""
    return {
        "t": "event",
        "seq": 40,
        "event_type": event_type,
        "partition": 1,
        "user": None,
        "user_name": None,
        "source": "keypad",
        "source_id": 1,
        "silent": False,
        "replayed": False,
        **fields,
    }


def _changed_by(hass: HomeAssistant) -> Any:
    state = hass.states.get(ALARM)
    assert state is not None
    return state.attributes.get("changed_by")


async def test_changed_by_follows_arm_and_disarm_events(hass: HomeAssistant) -> None:
    """HAI-005: changed_by is the user the panel's armed/disarmed event names - the
    name, else the number - for this partition only; a silent event never shows
    (HAI-008); a disarm by nobody in particular clears it."""
    async with StatePanel() as panel:
        await _setup(hass, panel)
        assert _changed_by(hass) is None

        panel.push(_event("armed", user=3, user_name="Alex", level="away"))
        await _wait_until(lambda: _changed_by(hass) == "Alex", "armed never set changed_by")

        # another partition's arm, a chime and a silent disarm leave it alone ...
        panel.push(_event("armed", partition=2, user=4, user_name="Pat"))
        panel.push(_event("chime_on", user=4, user_name="Pat"))
        panel.push(_event("disarmed", user=5, user_name="Sam", silent=True))
        # ... and a disarm by user 2, who has no name, gives the number
        panel.push(_event("disarmed", user=2))
        await _wait_until(lambda: _changed_by(hass) == "2", "disarmed never set changed_by")

        panel.push(_event("auto_stay"))
        await _wait_until(lambda: _changed_by(hass) is None, "auto_stay kept the old user")


async def test_changed_by_ignores_another_panels_events(hass: HomeAssistant) -> None:
    """HA04: one panel's events never reach another panel's entities."""
    async with StatePanel() as panel:
        entry = await _setup(hass, panel)
        hass.bus.async_fire(
            f"{DOMAIN}_event",
            {**_event("armed", user=3, user_name="Alex"), "entry_id": "another-entry"},
        )
        await hass.async_block_till_done()
        assert _changed_by(hass) is None
        hass.bus.async_fire(
            f"{DOMAIN}_event",
            {**_event("armed", user=3, user_name="Alex"), "entry_id": entry.entry_id},
        )
        await hass.async_block_till_done()
        assert _changed_by(hass) == "Alex"


# ---- a code the panel won't take -------------------------------------------------------

REFUSALS = {
    "invalid_code": "The panel did not accept that code",
    "not_authorized": "That code is not allowed to do this on the panel",
    "code_required": "The panel needs a user code for this",
    "locked_out": "The panel is not taking codes for now after too many wrong ones. "
    "Wait a few minutes, then try again",
}


@pytest.mark.parametrize(("result", "words"), list(REFUSALS.items()))
@pytest.mark.parametrize("service", ["alarm_disarm", "alarm_arm_away"])
async def test_a_refused_code_is_a_validation_error(
    hass: HomeAssistant, service: str, result: str, words: str
) -> None:
    """HAI-007: a wrong or refused code is the person's mistake, said in their
    language (strings.json "exceptions"), not a failure of the integration."""
    async with StatePanel() as panel:
        panel.command_result = result
        await _setup(hass, panel)
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(
                "alarm_control_panel",
                service,
                {"entity_id": ALARM, "code": "000000"},
                blocking=True,
            )
        assert err.value.translation_domain == DOMAIN
        assert err.value.translation_key == result
        assert str(err.value) == words


async def test_other_refusals_stay_as_they_were(hass: HomeAssistant) -> None:
    """HAI-007: a refusal that isn't about the code keeps the panel's own words."""
    async with StatePanel() as panel:
        panel.command_result = "not_ready"
        await _setup(hass, panel)
        with pytest.raises(HomeAssistantError, match="not ready") as err:
            await hass.services.async_call(
                "alarm_control_panel",
                "alarm_arm_away",
                {"entity_id": ALARM, "code": "246810"},
                blocking=True,
            )
        assert not isinstance(err.value, ServiceValidationError)


# ---- the services take the panel's codes: 4 to 10 digits -------------------------------


async def test_services_take_codes_of_4_to_10_digits(hass: HomeAssistant) -> None:
    """HAI-007: the panel's user codes are 4-10 digits; the actions take every one
    of them and nothing longer, shorter or not a number."""
    async with StatePanel() as panel:
        entry = await _setup(hass, panel)
        for code in ("2468", "1234567890"):
            await hass.services.async_call(
                DOMAIN,
                "bypass_zone",
                {"entry_id": entry.entry_id, "code": code, "zone": 1},
                blocking=True,
            )
            assert panel.commands[-1]["code"] == code
        sent = len(panel.commands)
        for code in ("12345678901", "246", "12a4"):
            with pytest.raises(vol.Invalid):
                await hass.services.async_call(
                    DOMAIN,
                    "bypass_zone",
                    {"entry_id": entry.entry_id, "code": code, "zone": 1},
                    blocking=True,
                )
        assert len(panel.commands) == sent


# ---- the event entity knows every event the panel sends --------------------------------

# alarm_event_name() in the panel repo's components/core/alarm_core/src/strings.c, in order
PANEL_EVENTS = [
    "system_start", "armed", "arm_failed", "disarmed", "auto_stay", "exit_restart",
    "entry_delay", "alarm", "alarm_restore", "exit_error", "recent_closing", "alarm_aborted",
    "alarm_canceled", "alarm_silenced", "bell_timeout", "swinger_shutdown", "memory_cleared",
    "bypass", "unbypass", "trouble", "trouble_restore", "troubles_acked", "duress",
    "invalid_code", "lockout", "chime_on", "chime_off", "walk_test_start", "walk_test_end",
    "walk_test_zone", "monitor_fault", "monitor_restore", "user_changed", "user_deleted",
    "config_changed", "user_updated", "program_enter", "program_exit", "factory_reset",
    "firmware_update", "zwave_device_added", "zwave_device_added_insecure",
    "zwave_device_removed", "zwave_device_left", "zwave_network_reset", "zwave_radio_restarted",
    "zwave_device_software", "zwave_garage", "zwave_jamming_reported", "backup_made",
    "backup_restored", "walk_test_extend", "walk_test_warning", "clock_set", "zigbee_added",
    "zigbee_removed", "zigbee_forgotten", "zigbee_left", "zigbee_replaced", "zigbee_join_refused",
    "zigbee_channel", "zigbee_network_reset", "zigbee_radio_restarted", "automation_failed",
    "arming_put_off",
]  # fmt: skip


def test_event_types_cover_the_panels_list() -> None:
    """HAI-005: every event the panel sends on the link can reach the event entity."""
    assert EVENT_TYPES == PANEL_EVENTS
    assert len(set(EVENT_TYPES)) == len(EVENT_TYPES)


async def test_a_newer_event_reaches_the_event_entity(hass: HomeAssistant) -> None:
    """HAI-005: one of the types added since v0.1.0 is no longer dropped."""
    async with StatePanel() as panel:
        await _setup(hass, panel)
        panel.push(_event("zwave_device_added", partition=0))

        def arrived() -> bool:
            ev = hass.states.get("event.home_event")
            return ev is not None and ev.attributes.get("event_type") == "zwave_device_added"

        await _wait_until(arrived, "the event never reached the event entity")


# ---- a controllable device's values (BL-171) -------------------------------------------

LIGHT = "light.porch"
DIM = "light.desk"  # an on/off light: no brightness
FAN = "fan.bedroom"
COVER = "cover.garage"
VALVE = "valve.water"
THERMOSTAT = "climate.hall"
SENSOR = "binary_sensor.porch"

THERMOSTAT_ATTRS = {
    "temperature": 21.5,
    "current_temperature": 19.75,
    "hvac_modes": ["off", "heat", "cool", "heat_cool"],
    "friendly_name": "Hall",
}


def _devices(hass: HomeAssistant) -> None:
    hass.states.async_set(LIGHT, STATE_ON, {"brightness": 128, "friendly_name": "Porch"})
    hass.states.async_set(DIM, STATE_ON, {"brightness": None, "friendly_name": "Desk"})
    hass.states.async_set(FAN, STATE_ON, {"percentage": 33, "friendly_name": "Bedroom"})
    hass.states.async_set(COVER, "open", {"current_position": 40, "friendly_name": "Garage"})
    hass.states.async_set(VALVE, "open", {"current_position": 100, "friendly_name": "Water"})
    hass.states.async_set(THERMOSTAT, "heat", THERMOSTAT_ATTRS)
    hass.states.async_set(SENSOR, STATE_OFF, {"device_class": "door", "brightness": 128})


VALUES = ("level", "setpoint", "temp", "modes")


def _values(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in VALUES if key in row}


async def test_catalog_carries_a_devices_values(hass: HomeAssistant) -> None:
    """HCTL-002, HAI-009: a controllable entity's catalog row carries its level,
    setpoint, temperature and modes, each only when known; a zone-only one none."""
    async with StatePanel() as panel:
        _devices(hass)
        await _setup(
            hass,
            panel,
            {
                CONF_SHARE_ENTITIES: [SENSOR],
                CONF_CONTROL_ENTITIES: [LIGHT, DIM, FAN, COVER, VALVE, THERMOSTAT],
            },
        )
        catalog = await panel.wait_for("catalog")
        rows = {row["id"]: row for row in catalog["entities"]}
        assert {entity: _values(row) for entity, row in rows.items()} == {
            SENSOR: {},
            LIGHT: {"level": 50},  # 128 of 255
            DIM: {},
            FAN: {"level": 33},
            COVER: {"level": 40},
            VALVE: {"level": 100},
            THERMOSTAT: {
                "setpoint": 2150,
                "temp": 1975,
                "modes": ["off", "heat", "cool", "heat_cool"],
            },
        }
        # the original keys keep their place; the values come after them
        assert list(rows[THERMOSTAT])[:8] == [
            "id", "name", "area", "domain", "class", "state", "zones", "control",
        ]  # fmt: skip

        # the connection's states say the same
        await panel.wait_for("state", 6)
        states = {msg["id"]: msg for msg in panel.got("state")}
        assert states[LIGHT] == {
            "t": "state",
            "id": LIGHT,
            "state": STATE_ON,
            "avail": True,
            "level": 50,
        }
        assert _values(states[THERMOSTAT]) == _values(rows[THERMOSTAT])


async def test_an_off_light_that_dims_says_0(hass: HomeAssistant) -> None:
    """HCTL-004: a light that is off but can dim sends level 0, so the panel keeps
    its Brightness slider after a reconnect; an off on/off light sends none."""
    async with StatePanel() as panel:
        hass.states.async_set(
            LIGHT, STATE_OFF, {"brightness": None, "supported_color_modes": ["brightness"]}
        )
        hass.states.async_set(DIM, STATE_OFF, {"supported_color_modes": ["onoff"]})
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LIGHT, DIM]})
        catalog = await panel.wait_for("catalog")
        rows = {row["id"]: _values(row) for row in catalog["entities"]}
        assert rows == {LIGHT: {"level": 0}, DIM: {}}
        await panel.wait_for("state", 2)
        states = {msg["id"]: _values(msg) for msg in panel.got("state")}
        assert states == {LIGHT: {"level": 0}, DIM: {}}


async def test_values_absent_when_not_known(hass: HomeAssistant) -> None:
    """HCTL-002: nothing is guessed - an off light, a thermostat holding a range
    (no single target), a fan's speed out of range and an unavailable cover send
    no level, setpoint, temperature or modes."""
    async with StatePanel() as panel:
        hass.states.async_set(LIGHT, STATE_OFF, {"brightness": None})
        hass.states.async_set(
            THERMOSTAT,
            "heat_cool",
            {"temperature": None, "current_temperature": None, "hvac_modes": None},
        )
        hass.states.async_set(FAN, STATE_ON, {"percentage": 150})
        hass.states.async_set(COVER, STATE_UNAVAILABLE, {"current_position": 40})
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LIGHT, THERMOSTAT, FAN, COVER]})
        catalog = await panel.wait_for("catalog")
        assert [_values(row) for row in catalog["entities"]] == [{}, {}, {}, {}]
        await panel.wait_for("state", 4)
        assert [_values(msg) for msg in panel.got("state")] == [{}, {}, {}, {}]


async def test_a_brightness_change_alone_sends_a_state(hass: HomeAssistant) -> None:
    """HAI-009, HCTL-002: an attribute the panel gets moves without the state
    word - the panel hears the new level; one it doesn't get sends nothing."""
    async with StatePanel() as panel:
        hass.states.async_set(LIGHT, STATE_ON, {"brightness": 128, "friendly_name": "Porch"})
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LIGHT]})
        first = await panel.wait_for("state")
        assert first["level"] == 50

        hass.states.async_set(LIGHT, STATE_ON, {"brightness": 255, "friendly_name": "Porch"})
        second = await panel.wait_for("state", 2)
        assert second == {"t": "state", "id": LIGHT, "state": STATE_ON, "avail": True, "level": 100}

        # a name, an icon: nothing the panel gets has changed
        hass.states.async_set(
            LIGHT, STATE_ON, {"brightness": 255, "friendly_name": "Porch Light", "icon": "mdi:x"}
        )
        await hass.async_block_till_done()
        await asyncio.sleep(0.1)
        assert len(panel.got("state")) == 2

        # and the state word still goes out as it always did
        hass.states.async_set(LIGHT, STATE_OFF, {"friendly_name": "Porch Light"})
        third = await panel.wait_for("state", 3)
        assert third == {"t": "state", "id": LIGHT, "state": STATE_OFF, "avail": True}


async def test_a_thermostat_setpoint_change_sends_a_state(hass: HomeAssistant) -> None:
    """HCTL-002: a new target or measured temperature reaches the panel."""
    async with StatePanel() as panel:
        hass.states.async_set(THERMOSTAT, "heat", THERMOSTAT_ATTRS)
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [THERMOSTAT]})
        await panel.wait_for("state")
        hass.states.async_set(THERMOSTAT, "heat", {**THERMOSTAT_ATTRS, "temperature": 22})
        second = await panel.wait_for("state", 2)
        assert second["setpoint"] == 2200
        assert second["temp"] == 1975


async def test_an_options_change_sends_every_state_again(hass: HomeAssistant) -> None:
    """ZSRC-015, HAI-009: what was already said is no reason to stay quiet when the
    options change (or a connection starts): even an unchanged state goes out."""
    async with StatePanel() as panel:
        hass.states.async_set(LIGHT, STATE_ON, {"brightness": 128})
        entry = await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LIGHT]})
        await panel.wait_for("state")
        hass.config_entries.async_update_entry(
            entry, options={CONF_CONTROL_ENTITIES: [LIGHT], CONF_SHARE_ENTITIES: []}
        )
        await hass.async_block_till_done()
        again = await panel.wait_for("state", 2)
        assert again["level"] == 50


# ---- what a device can do, what a thermostat is doing, since when (BL-171, BL-174) ----

SHED = "cover.shed"  # shared as a zone only: never controllable
SCENE = "scene.good_night"
RAN_AT = "2026-10-06T21:05:12.345678+00:00"
RAN_AT_S = 1791320712


def _by_id(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The newest message per entity."""
    return {msg["id"]: msg for msg in messages}


async def test_supports_from_home_assistants_flags(hass: HomeAssistant) -> None:
    """HCTL-002, HCTL-004: what a cover, a fan or a light can do beyond its domain's
    own actions goes out as "supports", on the catalog row and the state; an empty
    list says it can do none of them. A valve's is empty whatever it can do."""
    entities = {
        COVER: ("closed", {"supported_features": 12}),  # STOP | SET_POSITION
        "cover.gate": ("closed", {"supported_features": 3}),  # OPEN | CLOSE
        VALVE: ("open", {"supported_features": 15}),  # OPEN, CLOSE, SET_POSITION, STOP
        FAN: (STATE_ON, {"supported_features": 1}),  # SET_SPEED
        "fan.attic": (STATE_ON, {"supported_features": 8 | 16 | 32}),  # PRESET, ON, OFF
        DIM: (STATE_ON, {"supported_color_modes": ["onoff"]}),
        LIGHT: (STATE_ON, {"supported_color_modes": ["hs", "color_temp"]}),
    }
    expected = {
        COVER: ["stop", "set_position"],
        "cover.gate": [],
        VALVE: [],  # the panel may only open and close a valve
        FAN: ["set_percentage"],
        "fan.attic": [],
        DIM: [],
        LIGHT: ["brightness"],
    }
    async with StatePanel() as panel:
        for entity_id, (word, attrs) in entities.items():
            hass.states.async_set(entity_id, word, attrs)
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: list(entities)})
        catalog = await panel.wait_for("catalog")
        rows = _by_id(catalog["entities"])
        assert {entity: row.get("supports") for entity, row in rows.items()} == expected
        await panel.wait_for("state", len(entities))
        states = _by_id(panel.got("state"))
        assert {entity: msg.get("supports") for entity, msg in states.items()} == expected
        # the original keys keep their place; what it can do comes after them
        assert list(rows[COVER])[:8] == [
            "id", "name", "area", "domain", "class", "state", "zones", "control",
        ]  # fmt: skip


async def test_supports_left_out_when_not_known(hass: HomeAssistant) -> None:
    """HCTL-002: nothing is guessed - a light without supported_color_modes, a fan
    without supported_features (or a bool or a word there), a switch, a lock and a
    thermostat send no "supports", and the panel offers what their kind has; an
    entity shared as a zone only never carries it."""
    controlled = {
        LIGHT: (STATE_ON, {"brightness": 128}),
        FAN: (STATE_ON, {"percentage": 33}),
        "fan.attic": (STATE_ON, {"supported_features": True}),
        "fan.loft": (STATE_ON, {"supported_features": "1"}),
        "switch.pump": (STATE_ON, {"supported_features": 1}),
        "lock.front": ("locked", {"supported_features": 1}),
        THERMOSTAT: ("heat", {**THERMOSTAT_ATTRS, "supported_features": 1}),
    }
    async with StatePanel() as panel:
        for entity_id, (word, attrs) in controlled.items():
            hass.states.async_set(entity_id, word, attrs)
        hass.states.async_set(SHED, "closed", {"supported_features": 12})
        panel.watch = [SHED]  # a zone is bound to it: its state goes out too
        await _setup(
            hass,
            panel,
            {CONF_SHARE_ENTITIES: [SHED], CONF_CONTROL_ENTITIES: list(controlled)},
        )
        catalog = await panel.wait_for("catalog")
        assert [row["id"] for row in catalog["entities"] if "supports" in row] == []
        assert [row["id"] for row in catalog["entities"] if "since" in row] == []
        await panel.wait_for("state", len(controlled) + 1)
        assert [msg["id"] for msg in panel.got("state") if "supports" in msg] == []


async def test_a_valve_supports_nothing_more(hass: HomeAssistant) -> None:
    """HCTL-002, HCTL-004 (BL-173): the panel may only open and close a valve, so its
    "supports" is always an explicit empty list - with every feature flag, none, a
    malformed or absent supported_features, and while unavailable - on the catalog
    row and the state. Its position still goes out as "level"."""
    valves = {
        "valve.all": ("open", {"supported_features": 15, "current_position": 60}),
        "valve.stop": ("open", {"supported_features": 8}),  # STOP
        "valve.plain": ("closed", {"supported_features": 3}),  # OPEN | CLOSE
        "valve.none": ("closed", {}),
        "valve.bool": ("open", {"supported_features": True}),
        "valve.word": ("open", {"supported_features": "12"}),
        "valve.gone": (STATE_UNAVAILABLE, {"supported_features": 15}),
        "valve.lost": (STATE_UNAVAILABLE, {}),
    }
    async with StatePanel() as panel:
        for entity_id, (word, attrs) in valves.items():
            hass.states.async_set(entity_id, word, attrs)
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: list(valves)})
        catalog = await panel.wait_for("catalog")
        rows = _by_id(catalog["entities"])
        assert {entity: row.get("supports") for entity, row in rows.items()} == {
            entity: [] for entity in valves
        }
        assert rows["valve.all"]["level"] == 60
        await panel.wait_for("state", len(valves))
        states = _by_id(panel.got("state"))
        assert {entity: msg.get("supports") for entity, msg in states.items()} == {
            entity: [] for entity in valves
        }
        assert states["valve.all"]["level"] == 60
        assert states["valve.gone"]["avail"] is False
        assert "since" in states["valve.gone"]


async def test_a_valve_opens_and_closes_only(hass: HomeAssistant) -> None:
    """Features/24, BL-173: a valve the options allow opens and closes; stop and
    set_position are refused not_allowed without calling any service."""
    async with StatePanel() as panel:
        hass.states.async_set(VALVE, "closed", {"supported_features": 15})
        services = {
            name: async_mock_service(hass, "valve", name)
            for name in ("open_valve", "close_valve", "stop_valve", "set_valve_position")
        }
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [VALVE]})
        await panel.wait_for("catalog")

        result = await _call(hass, panel, id="v1", entity=VALVE, action="open")
        assert result == {"t": "call_result", "id": "v1", "ok": True, "error": ""}
        result = await _call(hass, panel, 2, id="v2", entity=VALVE, action="close")
        assert result == {"t": "call_result", "id": "v2", "ok": True, "error": ""}
        result = await _call(hass, panel, 3, id="v3", entity=VALVE, action="stop")
        assert result == {"t": "call_result", "id": "v3", "ok": False, "error": "not_allowed"}
        result = await _call(hass, panel, 4, id="v4", entity=VALVE, action="set_position", value=50)
        assert result == {"t": "call_result", "id": "v4", "ok": False, "error": "not_allowed"}
        assert [call.data["entity_id"] for call in services["open_valve"]] == [VALVE]
        assert [call.data["entity_id"] for call in services["close_valve"]] == [VALVE]
        assert services["stop_valve"] == []
        assert services["set_valve_position"] == []


async def test_a_features_change_alone_sends_a_state(hass: HomeAssistant) -> None:
    """HAI-009, HCTL-002: Home Assistant changing what an entity can do, with its
    state word the same, sends one state with the new list."""
    async with StatePanel() as panel:
        hass.states.async_set(COVER, "closed", {"supported_features": 12})
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [COVER]})
        first = await panel.wait_for("state")
        assert first["supports"] == ["stop", "set_position"]

        hass.states.async_set(COVER, "closed", {"supported_features": 4})
        second = await panel.wait_for("state", 2)
        assert second == {
            "t": "state",
            "id": COVER,
            "state": "closed",
            "avail": True,
            "supports": ["set_position"],
        }
        await hass.async_block_till_done()
        await asyncio.sleep(0.1)
        assert len(panel.got("state")) == 2


async def test_supports_and_since_while_unavailable(hass: HomeAssistant) -> None:
    """HCTL-006, HCTL-002: an unavailable cover's state says what it can do (a greyed
    tile keeps its shape) and since when it has been unavailable; its catalog row
    says what it can do but never since when (a catalog row can't say
    unavailable). A zone-only entity's unavailable state stays as it was."""
    async with StatePanel() as panel:
        hass.states.async_set(COVER, STATE_UNAVAILABLE, {"supported_features": 12})
        hass.states.async_set(SENSOR, STATE_UNAVAILABLE, {"device_class": "door"})
        down = int(hass.states.get(COVER).last_changed.timestamp())
        panel.watch = [SENSOR]
        await _setup(hass, panel, {CONF_SHARE_ENTITIES: [SENSOR], CONF_CONTROL_ENTITIES: [COVER]})
        catalog = await panel.wait_for("catalog")
        row = _by_id(catalog["entities"])[COVER]
        assert row["state"] == ""
        assert row["supports"] == ["stop", "set_position"]
        assert "since" not in row
        await panel.wait_for("state", 2)
        states = _by_id(panel.got("state"))
        assert states[COVER] == {
            "t": "state",
            "id": COVER,
            "state": "",
            "avail": False,
            "supports": ["stop", "set_position"],
            "since": down,
        }
        assert states[SENSOR] == {"t": "state", "id": SENSOR, "state": "", "avail": False}


async def test_hvac_action_crosses(hass: HomeAssistant) -> None:
    """HCTL-002: what a thermostat is doing goes out as "hvac_action" while it is
    available; a change of it alone sends a state; unavailable or not a word, none."""
    async with StatePanel() as panel:
        hass.states.async_set(THERMOSTAT, "heat", {**THERMOSTAT_ATTRS, "hvac_action": "idle"})
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [THERMOSTAT]})
        catalog = await panel.wait_for("catalog")
        assert catalog["entities"][0]["hvac_action"] == "idle"
        first = await panel.wait_for("state")
        assert first["hvac_action"] == "idle"
        assert "supports" not in first

        hass.states.async_set(THERMOSTAT, "heat", {**THERMOSTAT_ATTRS, "hvac_action": "heating"})
        second = await panel.wait_for("state", 2)
        assert second["hvac_action"] == "heating"
        assert second["setpoint"] == first["setpoint"]

        hass.states.async_set(THERMOSTAT, "heat", {**THERMOSTAT_ATTRS, "hvac_action": 3})
        third = await panel.wait_for("state", 3)
        assert "hvac_action" not in third

        hass.states.async_set(THERMOSTAT, STATE_UNAVAILABLE, {"hvac_action": "heating"})
        fourth = await panel.wait_for("state", 4)
        assert fourth["avail"] is False
        assert "hvac_action" not in fourth


async def test_a_scene_says_when_it_ran(hass: HomeAssistant) -> None:
    """HCTL-006: a scene that has run says when, on its catalog row and its state
    (its state word is that time); one that hasn't run says nothing; an unavailable
    one's catalog row says nothing and its state says since when; an available
    light says no time."""
    async with StatePanel() as panel:
        hass.states.async_set(SCENE, RAN_AT, {"friendly_name": "Good Night"})
        hass.states.async_set("scene.movie", STATE_UNKNOWN, {"friendly_name": "Movie"})
        hass.states.async_set("scene.away", STATE_UNAVAILABLE, {"friendly_name": "Away"})
        hass.states.async_set(LIGHT, STATE_ON, {"brightness": 128})
        down = int(hass.states.get("scene.away").last_changed.timestamp())
        await _setup(
            hass,
            panel,
            {CONF_CONTROL_ENTITIES: [SCENE, "scene.movie", "scene.away", LIGHT]},
        )
        catalog = await panel.wait_for("catalog")
        rows = _by_id(catalog["entities"])
        assert {entity: row.get("since") for entity, row in rows.items()} == {
            SCENE: RAN_AT_S,
            "scene.movie": None,
            "scene.away": None,
            LIGHT: None,
        }
        await panel.wait_for("state", 4)
        states = _by_id(panel.got("state"))
        assert {entity: msg.get("since") for entity, msg in states.items()} == {
            SCENE: RAN_AT_S,
            "scene.movie": None,
            "scene.away": down,
            LIGHT: None,
        }
        assert states[SCENE] == {
            "t": "state",
            "id": SCENE,
            "state": RAN_AT,
            "avail": True,
            "since": RAN_AT_S,
        }


async def test_a_scene_with_a_bad_time_says_none(hass: HomeAssistant) -> None:
    """HCTL-006: a scene's state is free text (the REST API, the developer tools);
    one shaped like a date that isn't one (Home Assistant's parser raises on it)
    sends no "since", on the catalog row or a state, and the link keeps working."""
    async with StatePanel() as panel:
        hass.states.async_set(SCENE, "2026-02-30T10:00:00+00:00", {"friendly_name": "Good Night"})
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [SCENE]})
        catalog = await panel.wait_for("catalog")
        assert "since" not in catalog["entities"][0]
        first = await panel.wait_for("state")
        assert first["avail"] is True
        assert "since" not in first

        hass.states.async_set(SCENE, "2026-13-45 10:00:00", {"friendly_name": "Good Night"})
        second = await panel.wait_for("state", 2)
        assert second["state"] == "2026-13-45 10:00:00"
        assert "since" not in second

        hass.states.async_set(SCENE, RAN_AT, {"friendly_name": "Good Night"})
        third = await panel.wait_for("state", 3)
        assert third["since"] == RAN_AT_S
