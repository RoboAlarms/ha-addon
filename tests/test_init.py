"""The integration against a live (fake) panel: snapshot in, entities up,
commands out and answered, deltas moving states (HAI-005..007)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.roboalarms import aiopanel
from custom_components.roboalarms.const import (
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_PANEL_FP,
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
    """A fake paired panel: hello, snapshot, then commands answered with 'ok'."""

    def __init__(self) -> None:
        self.server: asyncio.Server | None = None
        self.commands: list[dict[str, Any]] = []
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


async def _setup(hass: HomeAssistant, panel: StatePanel) -> MockConfigEntry:
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


async def _wait_for_state(hass: HomeAssistant, entity_id: str, want: str) -> None:
    """The push arrives over a real socket: give it a moment."""
    for _ in range(50):
        await asyncio.sleep(0.02)
        state = hass.states.get(entity_id)
        if state is not None and state.state == want:
            return
    raise AssertionError(f"{entity_id} never became {want}")
