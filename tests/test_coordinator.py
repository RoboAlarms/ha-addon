"""Regression tests for the coordinator (codex-review HA01, HA02, HA03, HA04,
HA05, HA07, HA11, HA12).

These turn the diagnostic probes in codex-review/ha-addon/evidence/test_review.py
into assertions of the desired, fixed behaviour: the panel's pre-snapshot watch
list is honoured, controllable entities are followed without being shared as
zones, a slow device action never delays the alarm, an event only reaches its
own panel, a torn link takes the entities with it and recovers only on a fresh
snapshot, a cancelled bootstrap closes its socket, and sends are bounded and
coalesced.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
from test_init import (
    HALL,
    HALL_ATTRS,
    LIGHT,
    PANEL_HELLO,
    PARTITION,
    PORCH,
    PORCH_ATTRS,
    SNAPSHOT,
    ZONE,
    StatePanel,
    _house,
    _setup,
)

from custom_components.roboalarms.const import (
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_CONTROL_ENTITIES,
    CONF_PANEL_FP,
    CONF_SHARE_ENTITIES,
    DOMAIN,
)
from custom_components.roboalarms.coordinator import _CALLS_MAX, RoboAlarmsCoordinator

pytestmark = pytest.mark.usefixtures("socket_enabled")

ALARM = "alarm_control_panel.home"
ZONE_SENSOR = "binary_sensor.front_door"


async def _settle() -> None:
    """Everything here travels a real socket: give it a moment."""
    await asyncio.sleep(0.15)


async def _eventually(check: Callable[[], bool], what: str, limit: float = 10.0) -> None:
    """Wait for something a reconnect cycle has to get around to."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(what)


async def _state_is(hass: HomeAssistant, entity_id: str, want: str) -> None:
    await _eventually(
        lambda: (s := hass.states.get(entity_id)) is not None and s.state == want,
        f"{entity_id} never became {want}",
    )


class ReconnectPanel(StatePanel):
    """A fake panel whose connection the test can drop, and which can be told
    to withhold its watch list or its snapshot from a given connection on.

    `connections` counts the paired connections it has served; `snapshot_on`
    and `watch_on` are the connection numbers that still get those messages
    (None = all of them), and `before_snapshot` is sent where the snapshot
    would have gone.
    """

    def __init__(self) -> None:
        super().__init__()
        self.connections = 0
        self.snapshot_on: set[int] | None = None
        self.watch_on: set[int] | None = None
        self.before_snapshot: list[dict[str, Any]] = []

    async def _send(self, writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
        if msg is PANEL_HELLO:
            self.connections += 1
        if msg.get("t") == "watch" and self.watch_on is not None:
            if self.connections not in self.watch_on:
                return
        if msg is SNAPSHOT:
            for extra in self.before_snapshot:
                await super()._send(writer, extra)
            if self.snapshot_on is not None and self.connections not in self.snapshot_on:
                return
        await super()._send(writer, msg)

    def drop(self) -> None:
        """Break the connection the way a Wi-Fi blip would."""
        assert self._writer is not None
        self._writer.close()


class SilentCommandPanel(StatePanel):
    """Takes commands and never answers them."""

    async def _send(self, writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
        if msg.get("t") == "result":
            return
        await super()._send(writer, msg)


class OtherPanel(StatePanel):
    """A second, distinctly identified fake panel."""

    async def _send(self, writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
        if msg is PANEL_HELLO:
            msg = {**msg, "id": "112233", "name": "Other"}
        await super()._send(writer, msg)


async def _setup_other(hass: HomeAssistant, panel: OtherPanel) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="112233",
        title="Other",
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: panel.port,
            CONF_PANEL_FP: "00" * 32,
            CONF_CLIENT_KEY: "KEY-PEM",
            CONF_CLIENT_CERT: "CERT-PEM",
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.roboalarms.coordinator.client_ssl_context", return_value=None):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _fast_link() -> Any:
    """Reconnect in test time rather than in panel time.

    The plain-socket TLS context has to stay patched past setup as well: the
    reconnections happen while the test is still running.
    """
    return patch.multiple(
        "custom_components.roboalarms.coordinator",
        _BACKOFF_MIN_S=0.05,
        _BACKOFF_MAX_S=0.2,
        _FIRST_SNAPSHOT_S=0.3,
        client_ssl_context=MagicMock(return_value=None),
    )


# ---- HA01: the watch list the panel sends before its snapshot -------------------------


async def test_a_watch_before_the_first_snapshot_is_honoured(hass: HomeAssistant) -> None:
    """The firmware sends its watch list before the first snapshot
    (ha_link.c push_state). The bootstrap used to read messages until the
    snapshot and discard everything else, so a configured sensor fed the
    panel nothing at all after setup (HA01)."""
    _house(hass)
    async with StatePanel() as panel:
        panel.watch = [PORCH]
        entry = await _setup(hass, panel, {CONF_SHARE_ENTITIES: [PORCH]})

        assert entry.runtime_data._watched == [PORCH]
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


async def test_a_pre_snapshot_watch_still_obeys_the_options(hass: HomeAssistant) -> None:
    """Arriving early buys nothing: an entity the options don't share stays
    private, exactly as it does for a watch arriving later."""
    _house(hass)
    async with StatePanel() as panel:
        panel.watch = [HALL, PORCH]
        entry = await _setup(hass, panel, {CONF_SHARE_ENTITIES: [PORCH]})

        assert entry.runtime_data._watched == [PORCH]
        await panel.wait_for("state")
        hass.states.async_set(HALL, STATE_ON, HALL_ATTRS)
        await _settle()
        assert [msg["id"] for msg in panel.got("state")] == [PORCH]


async def test_the_watch_is_honoured_again_after_a_reload(hass: HomeAssistant) -> None:
    """A reload is a fresh connection, and the panel repeats its list on it."""
    _house(hass)
    async with StatePanel() as panel:
        panel.watch = [PORCH]
        entry = await _setup(hass, panel, {CONF_SHARE_ENTITIES: [PORCH]})
        await panel.wait_for("state")

        with patch(
            "custom_components.roboalarms.coordinator.client_ssl_context", return_value=None
        ):
            await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()

        assert entry.runtime_data._watched == [PORCH]
        await panel.wait_for("state", 2)


async def test_every_watched_entity_is_pushed_again_after_a_reconnect(
    hass: HomeAssistant,
) -> None:
    """The panel supervises the whole link: when it comes back, every sensor
    bound to it must report within its grace (120 s) or the zone goes to
    CHECK (Features/22, ZSRC-015). So a reconnection re-pushes state, it does
    not only resume sending changes."""
    _house(hass)
    async with ReconnectPanel() as panel:
        panel.watch = [PORCH]
        with _fast_link():
            await _setup(hass, panel, {CONF_SHARE_ENTITIES: [PORCH]})
            await panel.wait_for("state")
            assert len(panel.got("state")) == 1

            panel.drop()
            await _eventually(
                lambda: len(panel.got("state")) >= 2, "no fresh state after the reconnect"
            )
            assert panel.got("state")[-1] == {
                "t": "state",
                "id": PORCH,
                "state": STATE_OFF,
                "avail": True,
            }


async def test_a_silent_reconnect_still_re_pushes_the_watched_state(
    hass: HomeAssistant,
) -> None:
    """Even when the panel does not repeat its watch list on the new
    connection, the entities it was watching report again - otherwise its
    zones go to CHECK two minutes after every reconnection."""
    _house(hass)
    async with ReconnectPanel() as panel:
        panel.watch = [PORCH]
        with _fast_link():
            entry = await _setup(hass, panel, {CONF_SHARE_ENTITIES: [PORCH]})
            await panel.wait_for("state")
            panel.watch_on = {1}  # the reconnection gets no watch message

            panel.drop()
            await _eventually(
                lambda: len(panel.got("state")) >= 2, "no fresh state after the silent reconnect"
            )
            assert entry.runtime_data._watched == [PORCH]
            assert panel.got("state")[-1]["id"] == PORCH


# ---- HA02: the entities the panel may act on ------------------------------------------


async def test_a_controllable_entity_is_followed_without_being_shared(
    hass: HomeAssistant,
) -> None:
    """A light the panel may switch reports its state without anyone having
    to share it as an alarm sensor as well (HA02). It is still not something
    a zone may bind to."""
    hass.states.async_set(LIGHT, STATE_OFF)
    async with StatePanel() as panel:
        entry = await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LIGHT]})

        assert await panel.wait_for("state") == {
            "t": "state",
            "id": LIGHT,
            "state": STATE_OFF,
            "avail": True,
        }

        hass.states.async_set(LIGHT, STATE_ON)
        assert (await panel.wait_for("state", 2))["state"] == STATE_ON

        panel.push({"t": "watch", "ids": [LIGHT]})
        await _settle()
        assert entry.runtime_data._watched == []  # controllable is not bindable


async def test_a_controllable_entity_that_goes_away_says_so(hass: HomeAssistant) -> None:
    """Unavailable is reported as such, so the panel's tile greys out rather
    than showing the last thing it happened to see."""
    hass.states.async_set(LIGHT, STATE_ON)
    async with StatePanel() as panel:
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LIGHT]})
        await panel.wait_for("state")

        hass.states.async_set(LIGHT, STATE_UNAVAILABLE)
        assert await panel.wait_for("state", 2) == {
            "t": "state",
            "id": LIGHT,
            "state": "",
            "avail": False,
        }


async def test_an_entity_in_both_lists_is_followed_once(hass: HomeAssistant) -> None:
    """Shared and controllable is one subscription and one stream of updates,
    not two of each."""
    _house(hass)
    async with StatePanel() as panel:
        panel.watch = [PORCH]
        await _setup(hass, panel, {CONF_SHARE_ENTITIES: [PORCH], CONF_CONTROL_ENTITIES: [PORCH]})
        await panel.wait_for("state")
        await _settle()
        assert len(panel.got("state")) == 1

        hass.states.async_set(PORCH, STATE_ON, PORCH_ATTRS)
        await _settle()
        assert len(panel.got("state")) == 2


async def test_revoking_control_stops_the_updates(hass: HomeAssistant) -> None:
    """Taking an entity out of the options stops it being followed at once,
    without waiting for a reconnect."""
    hass.states.async_set(LIGHT, STATE_OFF)
    async with StatePanel() as panel:
        entry = await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LIGHT]})
        await panel.wait_for("state")

        hass.config_entries.async_update_entry(entry, options={})
        await hass.async_block_till_done()
        seen = len(panel.got("state"))

        hass.states.async_set(LIGHT, STATE_ON)
        await _settle()
        assert len(panel.got("state")) == seen


# ---- HA03: a device action never delays the alarm -------------------------------------


async def test_a_slow_device_action_does_not_delay_the_alarm(hass: HomeAssistant) -> None:
    """The receive loop used to await each service call with blocking=True, so
    a device that takes its time held up alarm state, command results, pings
    and disconnect detection behind it (HA03)."""
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(call: Any) -> None:
        started.set()
        await release.wait()

    hass.services.async_register("light", "turn_on", slow)
    hass.states.async_set(LIGHT, STATE_OFF)
    async with StatePanel() as panel:
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LIGHT]})
        await panel.wait_for("catalog")

        panel.push({"t": "call", "id": "c1", "entity": LIGHT, "action": "turn_on"})
        await asyncio.wait_for(started.wait(), 5)

        panel.push({"t": "delta", "seq": 8, "partitions": [{**PARTITION, "ha_state": "triggered"}]})
        # the alarm arrives while the lamp is still thinking about it
        await _state_is(hass, ALARM, "triggered")
        assert not release.is_set()

        release.set()
        assert (await panel.wait_for("call_result"))["ok"] is True


async def test_a_device_action_that_never_finishes_is_answered(hass: HomeAssistant) -> None:
    """Every accepted call gets exactly one answer, deadline included, rather
    than leaving the panel's Devices screen to time it out by itself."""
    release = asyncio.Event()

    async def never(call: Any) -> None:
        await release.wait()

    hass.services.async_register("light", "turn_on", never)
    hass.states.async_set(LIGHT, STATE_OFF)
    async with StatePanel() as panel:
        with patch("custom_components.roboalarms.coordinator._CALL_TIMEOUT_S", 0.1):
            await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LIGHT]})
            await panel.wait_for("catalog")

            panel.push({"t": "call", "id": "t1", "entity": LIGHT, "action": "turn_on"})
            assert await panel.wait_for("call_result") == {
                "t": "call_result",
                "id": "t1",
                "ok": False,
                "error": "timeout",
            }
        release.set()


async def test_a_repeated_call_id_is_answered_again_without_acting_twice(
    hass: HomeAssistant,
) -> None:
    """A call id that has already been answered is answered the same way
    again: repeating a lock or a cover whose outcome is uncertain is worse
    than repeating the answer."""
    calls = async_mock_service(hass, "light", "turn_on")
    hass.states.async_set(LIGHT, STATE_OFF)
    async with StatePanel() as panel:
        await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LIGHT]})
        await panel.wait_for("catalog")

        panel.push({"t": "call", "id": "c1", "entity": LIGHT, "action": "turn_on"})
        first = await panel.wait_for("call_result")
        panel.push({"t": "call", "id": "c1", "entity": LIGHT, "action": "turn_on"})
        second = await panel.wait_for("call_result", 2)

        assert first == second == {"t": "call_result", "id": "c1", "ok": True, "error": ""}
        assert len(calls) == 1


async def test_more_device_actions_than_the_limit_are_refused_not_queued(
    hass: HomeAssistant,
) -> None:
    """The backlog is bounded, and what does not fit is told so rather than
    queued out of sight."""
    release = asyncio.Event()

    async def never(call: Any) -> None:
        await release.wait()

    hass.services.async_register("light", "turn_on", never)
    hass.states.async_set(LIGHT, STATE_OFF)
    async with StatePanel() as panel:
        entry = await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LIGHT]})
        await panel.wait_for("catalog")

        for n in range(_CALLS_MAX + 2):
            panel.push({"t": "call", "id": f"c{n}", "entity": LIGHT, "action": "turn_on"})

        refused = [await panel.wait_for("call_result", n) for n in (1, 2)]
        assert [r["error"] for r in refused] == ["failed", "failed"]
        assert len(entry.runtime_data._calls) == _CALLS_MAX

        release.set()
        await _eventually(
            lambda: len(panel.got("call_result")) == _CALLS_MAX + 2, "not every call was answered"
        )


async def test_an_action_revoked_while_it_queued_is_refused(hass: HomeAssistant) -> None:
    """The options are read when the action runs, not when it arrived."""
    calls = async_mock_service(hass, "light", "turn_on")
    hass.states.async_set(LIGHT, STATE_OFF)
    async with StatePanel() as panel:
        entry = await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LIGHT]})
        await panel.wait_for("catalog")

        hass.config_entries.async_update_entry(entry, options={})
        await hass.async_block_till_done()

        panel.push({"t": "call", "id": "c9", "entity": LIGHT, "action": "turn_on"})
        assert (await panel.wait_for("call_result"))["error"] == "not_allowed"
        assert calls == []


async def test_unload_leaves_no_device_action_behind(hass: HomeAssistant) -> None:
    """A hung action must not outlive the entry it belongs to."""
    release = asyncio.Event()

    async def never(call: Any) -> None:
        await release.wait()

    hass.services.async_register("light", "turn_on", never)
    hass.states.async_set(LIGHT, STATE_OFF)
    async with StatePanel() as panel:
        entry = await _setup(hass, panel, {CONF_CONTROL_ENTITIES: [LIGHT]})
        await panel.wait_for("catalog")
        coordinator = entry.runtime_data

        panel.push({"t": "call", "id": "c1", "entity": LIGHT, "action": "turn_on"})
        await _eventually(lambda: bool(coordinator._calls), "the call never started")
        tasks = list(coordinator._calls.values())

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        assert coordinator._calls == {}
        assert all(task.done() for task in tasks)
        assert coordinator._task is None
        release.set()


# ---- HA04: an event belongs to the panel that sent it ---------------------------------


async def test_an_event_reaches_only_its_own_panels_entity(hass: HomeAssistant) -> None:
    """Both panels listen to the same `roboalarms_event` bus event, so the
    coordinator has to say which entry it fired for - from its own config
    entry, never from a field off the wire (HA04)."""
    async with StatePanel() as first, OtherPanel() as second:
        home = await _setup(hass, first)
        other = await _setup_other(hass, second)

        # a peer-supplied entry_id must not be able to steer the routing
        first.push(
            {
                "t": "event",
                "seq": 12,
                "event_type": "alarm",
                "partition": 1,
                "silent": False,
                "entry_id": other.entry_id,
            }
        )
        await _eventually(
            lambda: (
                (s := hass.states.get("event.home_event")) is not None
                and s.attributes.get("event_type") == "alarm"
            ),
            "the panel's own event never reached its event entity",
        )
        assert hass.states.get("event.other_event").attributes.get("event_type") is None
        assert "entry_id" not in hass.states.get("event.home_event").attributes
        assert home.entry_id != other.entry_id


async def test_a_silent_event_is_routed_the_same_way(hass: HomeAssistant) -> None:
    """Duress rides the same path and still changes no visible alarm state
    (HAI-008)."""
    async with StatePanel() as first, OtherPanel() as second:
        await _setup(hass, first)
        await _setup_other(hass, second)

        first.push(
            {"t": "event", "seq": 13, "event_type": "duress", "partition": 1, "silent": True}
        )
        await _eventually(
            lambda: (
                (s := hass.states.get("event.home_event")) is not None
                and s.attributes.get("event_type") == "duress"
            ),
            "the duress event never reached its own event entity",
        )
        assert hass.states.get("event.other_event").attributes.get("event_type") is None
        assert hass.states.get(ALARM).state == "disarmed"


# ---- HA05 / HA12: a torn link takes the entities with it ------------------------------


async def test_a_malformed_delta_takes_the_link_down_with_the_entities(
    hass: HomeAssistant,
) -> None:
    """The listener used to die on a parse error while last_update_success
    stayed true, so Home Assistant showed a healthy alarm nothing was
    maintaining (HA05)."""
    async with StatePanel() as panel:
        with patch("custom_components.roboalarms.coordinator._BACKOFF_MIN_S", 30.0):
            entry = await _setup(hass, panel)
            coordinator = entry.runtime_data

            panel.push({"t": "delta", "seq": 8, "zones": [{"name": "no number at all"}]})
            await _state_is(hass, ALARM, STATE_UNAVAILABLE)

            assert not coordinator.last_update_success
            assert hass.states.get(ZONE_SENSOR).state == STATE_UNAVAILABLE
            assert coordinator._task is not None and not coordinator._task.done()
            assert coordinator._client is None  # the socket went with it


async def test_a_delta_after_a_link_failure_does_not_restore_the_old_state(
    hass: HomeAssistant,
) -> None:
    """A new connection starts from an empty state, so a bare delta cannot
    make the entities available again on the previous session's partition
    (HA05's other half, HA12). Only a snapshot of its own does."""
    async with ReconnectPanel() as panel:
        with _fast_link():
            entry = await _setup(hass, panel)
            coordinator = entry.runtime_data

            panel.snapshot_on = {1}  # from the reconnection on: a delta and nothing else
            panel.before_snapshot = [{"t": "delta", "seq": 9, "zones": [{**ZONE, "open": True}]}]
            panel.drop()

            await _state_is(hass, ALARM, STATE_UNAVAILABLE)
            await asyncio.sleep(1.0)  # several reconnect attempts, each delta-first
            assert panel.connections >= 2
            assert not coordinator.last_update_success
            assert hass.states.get(ALARM).state == STATE_UNAVAILABLE
            assert hass.states.get(ZONE_SENSOR).state == STATE_UNAVAILABLE

            panel.snapshot_on = None
            panel.before_snapshot = []
            await _state_is(hass, ALARM, "disarmed")
            assert hass.states.get(ZONE_SENSOR).state == STATE_OFF


async def test_a_peer_that_only_answers_does_not_pass_for_a_snapshot(
    hass: HomeAssistant,
) -> None:
    """The resynchronisation deadline is wall-clock and nothing on the wire
    extends it: a peer that keeps talking but never resynchronises stays
    unavailable (HA12)."""
    async with ReconnectPanel() as panel:
        with _fast_link():
            entry = await _setup(hass, panel)
            coordinator = entry.runtime_data
            panel.snapshot_on = {1}
            panel.drop()

            async def chatter() -> None:
                while True:
                    panel.push({"t": "pong"})
                    await asyncio.sleep(0.05)

            noise = asyncio.create_task(chatter())
            try:
                await _state_is(hass, ALARM, STATE_UNAVAILABLE)
                await asyncio.sleep(1.5)
                assert not coordinator.last_update_success
                assert hass.states.get(ALARM).state == STATE_UNAVAILABLE
            finally:
                noise.cancel()


async def test_a_reconnect_reconciles_a_removed_partition(hass: HomeAssistant) -> None:
    """The new session's snapshot replaces the old one whole, so a partition
    the panel no longer has goes unavailable instead of lingering."""
    async with ReconnectPanel() as panel:
        with _fast_link():
            entry = await _setup(hass, panel)
            panel.push(
                {
                    "t": "snapshot",
                    "seq": 8,
                    "partitions": [PARTITION, {**PARTITION, "partition": 2, "name": "Garage"}],
                    "zones": [ZONE],
                }
            )
            await _eventually(
                lambda: 2 in (entry.runtime_data.data.partitions or {}),
                "the second partition never arrived",
            )

            panel.drop()  # the panel comes back with its original single partition
            await _eventually(
                lambda: 2 not in (entry.runtime_data.data.partitions or {}),
                "the removed partition survived the reconnect",
            )
            assert panel.connections >= 2
            await _state_is(hass, ALARM, "disarmed")


# ---- HA07: the bootstrap owns its socket until the task exists ------------------------


async def test_a_cancelled_bootstrap_closes_the_connection(hass: HomeAssistant) -> None:
    """Cancelling setup while the first snapshot is still on its way used to
    abandon a connected client: it was closed for TimeoutError and LinkError
    only, and self._client had not been assigned, so shutdown could not find
    it either. The panel serves one connection at a time, so that slot stayed
    occupied (HA07)."""
    entry = MockConfigEntry(domain=DOMAIN, title="Home", data={})
    entry.add_to_hass(hass)
    coordinator = RoboAlarmsCoordinator(hass, entry)
    entered = asyncio.Event()

    async def recv(*args: Any, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client = MagicMock()
    client.recv = AsyncMock(side_effect=recv)
    client.close = AsyncMock()

    with patch.object(coordinator, "_async_connect", return_value=client):
        task = asyncio.create_task(coordinator.async_start())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert client.close.await_count == 1
    assert coordinator._client is None
    assert coordinator._task is None
    await coordinator.async_shutdown()


async def test_shutdown_waits_for_the_task_and_answers_the_commands(
    hass: HomeAssistant,
) -> None:
    """Unload stops the link task, and a command still waiting for an answer
    is told rather than left hanging on a socket that is gone."""
    async with SilentCommandPanel() as panel:
        entry = await _setup(hass, panel)
        coordinator = entry.runtime_data
        link = coordinator._task
        assert link is not None

        pending = asyncio.create_task(coordinator.async_command("disarm", code="123456"))
        await _eventually(lambda: bool(coordinator._pending), "the command never went out")

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        assert link.done()
        assert coordinator._pending == {}
        assert coordinator._client is None
        with pytest.raises(HomeAssistantError):
            await asyncio.wait_for(pending, 5)


# ---- HA11: bounded, coalesced sending -------------------------------------------------


class _BlockingClient:
    """A panel client whose sends can be held open at will."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.started = 0
        self.closed = 0
        self.gate: asyncio.Event | None = None

    async def send(self, msg: dict[str, Any], timeout: float | None = None) -> None:
        self.started += 1
        if self.gate is not None:
            await self.gate.wait()
        self.sent.append(msg)

    async def close(self, timeout: float | None = None) -> None:
        self.closed += 1


def _offline_coordinator(hass: HomeAssistant, **options: Any) -> RoboAlarmsCoordinator:
    """A coordinator with a fake client, for the send paths alone."""
    entry = MockConfigEntry(domain=DOMAIN, title="Home", data={}, options=options)
    entry.add_to_hass(hass)
    return RoboAlarmsCoordinator(hass, entry)


async def test_the_command_deadline_covers_the_send(hass: HomeAssistant) -> None:
    """The deadline used to start only after send() returned, so a panel that
    stopped reading held a command open indefinitely (HA11)."""
    coordinator = _offline_coordinator(hass)
    client = _BlockingClient()
    client.gate = asyncio.Event()
    coordinator._client = client  # type: ignore[assignment]

    with patch("custom_components.roboalarms.coordinator._COMMAND_TIMEOUT_S", 0.05):
        with pytest.raises(HomeAssistantError):
            await asyncio.wait_for(coordinator.async_command("disarm", code="123456"), 5)

    assert coordinator._pending == {}
    client.gate.set()
    await coordinator.async_shutdown()


async def test_a_burst_of_changes_coalesces_to_the_newest_state(hass: HomeAssistant) -> None:
    """Every state change used to await its own send, so a peer that stopped
    reading collected one pending send per change (HA11). Now one entity owes
    at most one message, and what goes out when the link moves again is its
    current state, not a backlog of superseded ones."""
    hass.states.async_set(PORCH, STATE_OFF, PORCH_ATTRS)
    coordinator = _offline_coordinator(hass, **{CONF_SHARE_ENTITIES: [PORCH]})
    client = _BlockingClient()
    coordinator._client = client  # type: ignore[assignment]

    await coordinator._async_watch([PORCH])
    assert client.started == 1  # the state that answers the watch

    client.gate = asyncio.Event()  # from here the panel stops reading
    for n in range(200):
        hass.states.async_set(PORCH, STATE_ON if n % 2 else STATE_OFF, PORCH_ATTRS)
        await asyncio.sleep(0)
    await _settle()

    assert client.started == 2  # one send in flight, not two hundred
    assert len(coordinator._outbox) <= 1

    client.gate.set()
    await _settle()
    assert coordinator._outbox == {}
    assert client.sent[-1]["state"] == hass.states.get(PORCH).state
    await coordinator.async_shutdown()
    assert client.closed >= 1
