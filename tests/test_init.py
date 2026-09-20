"""The integration against a live (fake) panel: snapshot in, entities up,
commands out and answered, deltas moving states (HAI-005..007)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.roboalarms import aiopanel
from custom_components.roboalarms.const import (
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_CONTROL_ENTITIES,
    CONF_PANEL_FP,
    CONF_SHARE_ENTITIES,
    DOMAIN,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")

PANEL_HELLO = {
    "t": "hello",
    "api": 1,
    "id": "0a1b2c",
    "name": "Home",
    "model": "crowpanel-p4",
    "fw": "0.6.0",
    "paired": True,
}

PARTITION = {
    "partition": 1,
    "name": "Home",
    "state": "disarmed",
    "ha_state": "disarmed",
    "ready": True,
    "chime": False,
    "triggered": False,
}

ZONE = {
    "zone": 1,
    "name": "Front Door",
    "type": "entry_exit_1",
    "device_class": "door",
    "partition": 1,
    "open": False,
    "bypassed": False,
    "alarm": False,
    "trouble": False,
    "tamper": False,
    "low_battery": False,
    "supervision": False,
    "not_ready": False,
}

SNAPSHOT = {
    "t": "snapshot",
    "seq": 7,
    "partitions": [PARTITION],
    "zones": [ZONE],
    "troubles": {"count": 0},
}


class StatePanel:
    """A fake paired panel: hello, snapshot, then commands answered with 'ok'.

    Everything the integration sends lands in `received` (pings excepted), so a
    test can look at the catalog and the state messages it is supposed to get.
    """

    def __init__(self) -> None:
        self.server: asyncio.Server | None = None
        self.commands: list[dict[str, Any]] = []
        self.received: list[dict[str, Any]] = []
        self.command_result = "ok"
        self._push: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> StatePanel:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.server is not None
        self.server.close()
        if self._writer is not None:
            # Python 3.12+: wait_closed() waits for every handler, and the
            # handler waits on this connection - close it or deadlock.
            self._writer.close()
        await self.server.wait_closed()

    @property
    def port(self) -> int:
        assert self.server is not None
        return int(self.server.sockets[0].getsockname()[1])

    def push(self, msg: dict[str, Any]) -> None:
        """Queue a message for the connected client (a delta, an event)."""
        self._push.put_nowait(msg)

    def got(self, kind: str) -> list[dict[str, Any]]:
        """Every message of this type the integration has sent so far."""
        return [msg for msg in self.received if msg.get("t") == kind]

    async def wait_for(self, kind: str, count: int = 1) -> dict[str, Any]:
        """Wait for the count-th message of a type and return it (it travels a
        real socket, so it takes a moment)."""
        for _ in range(100):
            got = self.got(kind)
            if len(got) >= count:
                return got[count - 1]
            await asyncio.sleep(0.02)
        raise AssertionError(f"the integration never sent {count} {kind} message(s)")

    async def _send(self, writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
        writer.write(aiopanel.encode_frame(msg))
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        try:
            header = await reader.readexactly(4)
            await reader.readexactly(int.from_bytes(header, "big"))  # the client's hello
            await self._send(writer, PANEL_HELLO)
            await self._send(writer, SNAPSHOT)
            read = asyncio.ensure_future(reader.readexactly(4))
            pushed = asyncio.ensure_future(self._push.get())
            while True:
                done, _ = await asyncio.wait({read, pushed}, return_when=asyncio.FIRST_COMPLETED)
                if pushed in done:
                    await self._send(writer, pushed.result())
                    pushed = asyncio.ensure_future(self._push.get())
                if read in done:
                    header = read.result()
                    payload = await reader.readexactly(int.from_bytes(header, "big"))
                    msg = json.loads(payload)
                    if msg.get("t") != "ping":
                        self.received.append(msg)
                    if msg.get("t") == "command":
                        self.commands.append(msg)
                        await self._send(
                            writer,
                            {
                                "t": "result",
                                "id": msg.get("id"),
                                "action": msg.get("action"),
                                "result": self.command_result,
                            },
                        )
                    elif msg.get("t") == "ping":
                        await self._send(writer, {"t": "pong"})
                    read = asyncio.ensure_future(reader.readexactly(4))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            for task in (read, pushed):
                try:
                    task.cancel()
                except NameError:
                    pass
            writer.close()


async def _setup(
    hass: HomeAssistant, panel: StatePanel, options: dict[str, Any] | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="0a1b2c",
        title="Home",
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: panel.port,
            CONF_PANEL_FP: "00" * 32,  # unchecked on a plain test connection
            CONF_CLIENT_KEY: "KEY-PEM",
            CONF_CLIENT_CERT: "CERT-PEM",
        },
    )
    entry.add_to_hass(hass)
    if options is not None:
        hass.config_entries.async_update_entry(entry, options=options)
    with patch("custom_components.roboalarms.coordinator.client_ssl_context", return_value=None):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_snapshot_becomes_entities(hass: HomeAssistant) -> None:
    """The first snapshot turns into the partition and its zone."""
    async with StatePanel() as panel:
        await _setup(hass, panel)
        alarm = hass.states.get("alarm_control_panel.home")
        assert alarm is not None
        assert alarm.state == "disarmed"
        zone = hass.states.get("binary_sensor.front_door")
        assert zone is not None
        assert zone.state == STATE_OFF
        assert zone.attributes["device_class"] == "door"


async def test_arm_away_roundtrip(hass: HomeAssistant) -> None:
    """Arming sends the command with the code; the delta moves the state."""
    async with StatePanel() as panel:
        await _setup(hass, panel)
        await hass.services.async_call(
            "alarm_control_panel",
            "alarm_arm_away",
            {"entity_id": "alarm_control_panel.home", "code": "246810"},
            blocking=True,
        )
        assert panel.commands == [
            {
                "t": "command",
                "id": panel.commands[0]["id"],
                "action": "arm",
                "level": "away",
                "partition": 1,
                "code": "246810",
            }
        ]
        panel.push(
            {
                "t": "delta",
                "seq": 8,
                "partitions": [{**PARTITION, "state": "exit_delay", "ha_state": "arming"}],
            }
        )
        await _wait_for_state(hass, "alarm_control_panel.home", "arming")


async def test_refused_command_raises(hass: HomeAssistant) -> None:
    """A wrong code comes back as the panel's own words, not success."""
    async with StatePanel() as panel:
        panel.command_result = "invalid_code"
        await _setup(hass, panel)
        with pytest.raises(HomeAssistantError, match="invalid code"):
            await hass.services.async_call(
                "alarm_control_panel",
                "alarm_disarm",
                {"entity_id": "alarm_control_panel.home", "code": "000000"},
                blocking=True,
            )


async def test_zone_added_without_restart(hass: HomeAssistant) -> None:
    """A zone the panel gains later becomes an entity by itself (HAI-005)."""
    async with StatePanel() as panel:
        await _setup(hass, panel)
        panel.push(
            {
                "t": "delta",
                "seq": 9,
                "zones": [
                    {
                        **ZONE,
                        "zone": 2,
                        "name": "Garage Door",
                        "device_class": "garage_door",
                        "open": True,
                    }
                ],
            }
        )
        await _wait_for_state(hass, "binary_sensor.garage_door", STATE_ON)


async def test_chime_button_and_event(hass: HomeAssistant) -> None:
    """The chime switch toggles, the button restarts the exit delay, the event
    entity carries what the panel reports."""
    async with StatePanel() as panel:
        await _setup(hass, panel)

        chime = hass.states.get("switch.home_chime")
        assert chime is not None and chime.state == STATE_OFF
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.home_chime"}, blocking=True
        )
        assert panel.commands[-1]["action"] == "chime"
        # turning it "off" while it is already off sends nothing (the command toggles)
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.home_chime"}, blocking=True
        )
        assert len(panel.commands) == 1

        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.home_restart_exit_delay"},
            blocking=True,
        )
        assert panel.commands[-1] == {
            "t": "command",
            "id": panel.commands[-1]["id"],
            "action": "exit_restart",
            "partition": 1,
        }

        panel.push(
            {
                "t": "event",
                "seq": 12,
                "event_type": "chime_on",
                "partition": 1,
                "user": 2,
                "silent": False,
            }
        )
        for _ in range(50):
            await asyncio.sleep(0.02)
            ev = hass.states.get("event.home_event")
            if ev is not None and ev.attributes.get("event_type") == "chime_on":
                break
        else:
            raise AssertionError("the panel's event never reached the event entity")
        assert ev.attributes["partition"] == 1


# ---- what the panel may see of Home Assistant (HAI-009) --------------------------------

PORCH = "binary_sensor.porch"
HALL = "binary_sensor.hall"

PORCH_ATTRS = {"device_class": "door", "friendly_name": "Porch Door"}
HALL_ATTRS = {"device_class": "motion", "friendly_name": "Hall Motion"}


def _house(hass: HomeAssistant) -> None:
    """Two binary sensors of this Home Assistant; the tests share only one."""
    hass.states.async_set(PORCH, STATE_OFF, PORCH_ATTRS)
    hass.states.async_set(HALL, STATE_OFF, HALL_ATTRS)


async def test_catalog_holds_only_the_shared_entities(hass: HomeAssistant) -> None:
    """The panel is offered what the options picked, in the shape protocol.md
    describes, and nothing else."""
    async with StatePanel() as panel:
        _house(hass)
        await _setup(hass, panel, {CONF_SHARE_ENTITIES: [PORCH]})
        catalog = await panel.wait_for("catalog")
        # the panel's parser reads page and more before the entities
        assert list(catalog) == ["t", "page", "more", "entities"]
        assert catalog["page"] == 0
        assert catalog["more"] is False
        assert catalog["entities"] == [
            {
                "id": PORCH,
                "name": "Porch Door",
                "area": "",
                "domain": "binary_sensor",
                "class": "door",
                "state": STATE_OFF,
                "zones": True,
                "control": False,
            }
        ]


async def test_watch_answers_with_a_state_and_keeps_pushing(hass: HomeAssistant) -> None:
    """A watched entity arrives at once and again whenever it moves."""
    async with StatePanel() as panel:
        _house(hass)
        await _setup(hass, panel, {CONF_SHARE_ENTITIES: [PORCH]})
        await panel.wait_for("catalog")

        panel.push({"t": "watch", "ids": [PORCH]})
        assert await panel.wait_for("state") == {
            "t": "state",
            "id": PORCH,
            "state": STATE_OFF,
            "avail": True,
        }

        hass.states.async_set(PORCH, STATE_ON, PORCH_ATTRS)
        assert await panel.wait_for("state", 2) == {
            "t": "state",
            "id": PORCH,
            "state": STATE_ON,
            "avail": True,
        }


async def test_watch_outside_the_options_is_ignored(hass: HomeAssistant) -> None:
    """An entity the options don't share is never reported, asked for or not."""
    async with StatePanel() as panel:
        _house(hass)
        await _setup(hass, panel, {CONF_SHARE_ENTITIES: [PORCH]})
        await panel.wait_for("catalog")

        panel.push({"t": "watch", "ids": [HALL]})
        # the shared one that follows proves the first watch was handled
        panel.push({"t": "watch", "ids": [PORCH]})
        await panel.wait_for("state")

        hass.states.async_set(HALL, STATE_ON, HALL_ATTRS)
        await hass.async_block_till_done()
        await asyncio.sleep(0.1)
        assert [msg["id"] for msg in panel.got("state")] == [PORCH]


async def test_unavailable_entity_is_reported_unavailable(hass: HomeAssistant) -> None:
    """Nothing to say about it: no state text and avail false, so the zone can
    go to CHECK rather than look closed."""
    async with StatePanel() as panel:
        hass.states.async_set(PORCH, STATE_UNAVAILABLE, PORCH_ATTRS)
        await _setup(hass, panel, {CONF_SHARE_ENTITIES: [PORCH]})
        catalog = await panel.wait_for("catalog")
        assert catalog["entities"][0]["state"] == ""

        panel.push({"t": "watch", "ids": [PORCH]})
        assert await panel.wait_for("state") == {
            "t": "state",
            "id": PORCH,
            "state": "",
            "avail": False,
        }


async def test_changed_options_resend_the_catalog(hass: HomeAssistant) -> None:
    """Sharing one entity less: a fresh page 0, and it stops being watched."""
    async with StatePanel() as panel:
        _house(hass)
        entry = await _setup(hass, panel, {CONF_SHARE_ENTITIES: [PORCH, HALL]})
        first = await panel.wait_for("catalog")
        assert [e["id"] for e in first["entities"]] == [PORCH, HALL]

        panel.push({"t": "watch", "ids": [PORCH, HALL]})
        await panel.wait_for("state", 2)

        hass.config_entries.async_update_entry(entry, options={CONF_SHARE_ENTITIES: [PORCH]})
        await hass.async_block_till_done()
        second = await panel.wait_for("catalog", 2)
        assert second["page"] == 0
        assert [e["id"] for e in second["entities"]] == [PORCH]

        seen = len(panel.got("state"))
        hass.states.async_set(HALL, STATE_ON, HALL_ATTRS)
        await hass.async_block_till_done()
        await asyncio.sleep(0.1)
        assert all(msg["id"] == PORCH for msg in panel.got("state")[seen:])


# ---- what the panel may do: its Devices screen (Features/24) ---------------------------

LIGHT = "light.porch"
LAMP = "light.lamp"
LOCK = "lock.front_door"
THERMOSTAT = "climate.hall"


async def _call(
    hass: HomeAssistant, panel: StatePanel, count: int = 1, **fields: Any
) -> dict[str, Any]:
    """Ask the panel's way for something and wait for the one answer to it."""
    panel.push({"t": "call", **fields})
    result = await panel.wait_for("call_result", count)
    await hass.async_block_till_done()
    return result


async def test_call_controls_an_allowed_light(hass: HomeAssistant) -> None:
    """A light the options allow is turned on with the brightness asked for."""
    async with StatePanel() as panel:
        hass.states.async_set(LIGHT, STATE_OFF)
        calls = async_mock_service(hass, "light", "turn_on")
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LIGHT]})
        await panel.wait_for("catalog")

        result = await _call(hass, panel, id="c1", entity=LIGHT, action="turn_on", value=60)
        assert result == {"t": "call_result", "id": "c1", "ok": True, "error": ""}
        assert len(calls) == 1
        assert calls[0].data["entity_id"] == LIGHT
        assert calls[0].data["brightness_pct"] == 60


async def test_call_outside_the_options_is_refused(hass: HomeAssistant) -> None:
    """An entity nobody allowed is never touched, whatever the panel asks."""
    async with StatePanel() as panel:
        hass.states.async_set(LIGHT, STATE_OFF)
        hass.states.async_set(LAMP, STATE_OFF)
        calls = async_mock_service(hass, "light", "turn_on")
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LIGHT]})
        await panel.wait_for("catalog")

        result = await _call(hass, panel, id="c2", entity=LAMP, action="turn_on")
        assert result == {"t": "call_result", "id": "c2", "ok": False, "error": "not_allowed"}
        assert calls == []


async def test_call_with_an_action_the_domain_lacks_is_refused(hass: HomeAssistant) -> None:
    """A lock has no toggle in Features/24's table: refused, never guessed."""
    async with StatePanel() as panel:
        hass.states.async_set(LOCK, "locked")
        calls = async_mock_service(hass, "lock", "unlock")
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LOCK]})
        await panel.wait_for("catalog")

        result = await _call(hass, panel, id="c3", entity=LOCK, action="toggle")
        assert result == {"t": "call_result", "id": "c3", "ok": False, "error": "not_allowed"}
        assert calls == []


async def test_call_on_an_unavailable_entity_says_so(hass: HomeAssistant) -> None:
    """Nothing to act on: the panel is told rather than left waiting."""
    async with StatePanel() as panel:
        hass.states.async_set(LIGHT, STATE_UNAVAILABLE)
        calls = async_mock_service(hass, "light", "turn_on")
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LIGHT]})
        await panel.wait_for("catalog")

        result = await _call(hass, panel, id="c4", entity=LIGHT, action="turn_on")
        assert result == {"t": "call_result", "id": "c4", "ok": False, "error": "unavailable"}
        assert calls == []


async def test_call_sets_a_temperature_from_hundredths(hass: HomeAssistant) -> None:
    """The panel counts in hundredths of a degree; Home Assistant in degrees."""
    async with StatePanel() as panel:
        hass.states.async_set(THERMOSTAT, "heat")
        calls = async_mock_service(hass, "climate", "set_temperature")
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [THERMOSTAT]})
        await panel.wait_for("catalog")

        result = await _call(
            hass, panel, id="c5", entity=THERMOSTAT, action="set_temperature", value=2150
        )
        assert result["ok"] is True
        assert len(calls) == 1
        assert calls[0].data["temperature"] == 21.5


async def test_catalog_marks_zones_and_control(hass: HomeAssistant) -> None:
    """One entity shared, one controllable, one both: each row says which."""
    async with StatePanel() as panel:
        _house(hass)
        hass.states.async_set(LIGHT, STATE_OFF)
        await _setup(
            hass,
            panel,
            {CONF_SHARE_ENTITIES: [PORCH, HALL], CONF_CONTROL_ENTITIES: [HALL, LIGHT]},
        )
        catalog = await panel.wait_for("catalog")
        assert [(e["id"], e["zones"], e["control"]) for e in catalog["entities"]] == [
            (PORCH, True, False),
            (HALL, True, True),
            (LIGHT, False, True),
        ]


async def _wait_for_state(hass: HomeAssistant, entity_id: str, want: str) -> None:
    """The push arrives over a real socket: give it a moment."""
    for _ in range(50):
        await asyncio.sleep(0.02)
        state = hass.states.get(entity_id)
        if state is not None and state.state == want:
            return
    raise AssertionError(f"{entity_id} never became {want}")
