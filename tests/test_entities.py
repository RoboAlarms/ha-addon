"""Regression tests for the entity platforms: event, binary_sensor,
alarm_control_panel, switch and button (codex-review HA04, HA08, HA09, HA13).

These turn the diagnostic probes in codex-review/ha-addon/evidence/test_review.py
(test_review_events_cross_panel_boundaries, test_review_zone_metadata_is_stale_after_snapshot,
test_review_new_partition_does_not_gain_entities, test_review_two_turn_on_calls_send_two_toggles)
into assertions of the desired, fixed behaviour.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_init import PARTITION, SNAPSHOT, ZONE, StatePanel, _setup

from custom_components.roboalarms.const import (
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_PANEL_FP,
    DOMAIN,
)
from custom_components.roboalarms.switch import _CONFIRM_TIMEOUT_S

pytestmark = pytest.mark.usefixtures("socket_enabled")


async def settle() -> None:
    """Real sockets and confirmation waits both need a moment."""
    await asyncio.sleep(0.15)


def _entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)


class OtherPanel(StatePanel):
    """A second, distinctly identified fake panel."""

    async def _send(self, writer, msg):
        if msg is not None and msg.get("t") == "hello":
            msg = {**msg, "id": "112233", "name": "Other"}
        await super()._send(writer, msg)


async def _setup_second(hass: HomeAssistant, panel: OtherPanel) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="112233",
        title="Other",
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: panel.port,
            CONF_PANEL_FP: "00" * 32,
            CONF_CLIENT_KEY: "KEY",
            CONF_CLIENT_CERT: "CERT",
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.roboalarms.coordinator.client_ssl_context", return_value=None):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


# ---- HA04: an event entity only reacts to its own panel's events ----------------------
#
# Cross-workstream contract (see reports/HA-E.md): the coordinator (workstream HA-C,
# coordinator.py) stamps the entry_id of its own config entry onto every event it fires
# on the shared "roboalarms_event" bus event, derived locally - never from a peer/wire
# field. This entity (event.py) drops anything not stamped with its own coordinator's
# entry_id. These tests fire the bus event directly with that stamp already applied, so
# they exercise event.py's half of the contract without depending on coordinator.py's
# own (concurrently developed) producer-side change.


async def test_event_entity_ignores_other_panels_entry_id(hass: HomeAssistant) -> None:
    """Two panels, one event stamped for the first: only its entity updates
    (HA04; event.py:53-69 previously filtered only on event_type)."""
    async with StatePanel() as first, OtherPanel() as second:
        first_entry = await _setup(hass, first)
        second_entry = await _setup_second(hass, second)

        hass.bus.async_fire(
            f"{DOMAIN}_event",
            {
                "event_type": "alarm",
                "partition": 1,
                "silent": False,
                "entry_id": first_entry.entry_id,
            },
        )
        await settle()
        home = hass.states.get("event.home_event")
        other = hass.states.get("event.other_event")
        assert home is not None and home.attributes.get("event_type") == "alarm"
        assert other is not None and other.attributes.get("event_type") is None
        await hass.config_entries.async_unload(first_entry.entry_id)
        await hass.config_entries.async_unload(second_entry.entry_id)


async def test_event_entity_accepts_its_own_entry_id(hass: HomeAssistant) -> None:
    """The normal case still works, silent events included - routing is
    independent of HAI-008's "never changes alarm state" rule."""
    async with StatePanel() as panel:
        entry = await _setup(hass, panel)
        hass.bus.async_fire(
            f"{DOMAIN}_event",
            {
                "event_type": "duress",
                "partition": 1,
                "silent": True,
                "entry_id": entry.entry_id,
            },
        )
        await settle()
        state = hass.states.get("event.home_event")
        assert state is not None
        assert state.attributes.get("event_type") == "duress"
        assert state.attributes.get("silent") is True
        await hass.config_entries.async_unload(entry.entry_id)


# ---- HA08: zone metadata (name, device class) reconciles on new snapshots -------------


async def test_zone_rename_and_reclassify_updates_live(hass: HomeAssistant) -> None:
    """A snapshot renaming zone 1 and changing its class updates the
    existing device and entity - not just the coordinator (HA08,
    binary_sensor.py:51-58/75-83). The unique_id and entity_id stay put, so
    history and automation references survive the rename."""
    async with StatePanel() as panel:
        entry = await _setup(hass, panel)
        before = hass.states.get("binary_sensor.front_door")
        assert before is not None
        assert before.attributes["device_class"] == "door"

        panel.push(
            {
                **SNAPSHOT,
                "seq": 8,
                "zones": [{**ZONE, "name": "Kitchen Smoke", "device_class": "smoke"}],
            }
        )
        await settle()

        after = hass.states.get("binary_sensor.front_door")
        assert after is not None
        assert after.attributes["friendly_name"] == "Kitchen Smoke"
        assert after.attributes["device_class"] == "smoke"

        reg_entry = er.async_get(hass).async_get("binary_sensor.front_door")
        assert reg_entry is not None
        assert reg_entry.unique_id == "0a1b2c_z1"
        await hass.config_entries.async_unload(entry.entry_id)


async def test_zone_rename_preserves_user_name_and_area(hass: HomeAssistant) -> None:
    """A person's own device name and area assignment survive a panel-side
    rename (HA08 acceptance: "user customizations survive")."""
    async with StatePanel() as panel:
        entry = await _setup(hass, panel)
        entity_registry = er.async_get(hass)
        device_registry = dr.async_get(hass)
        reg_entry = entity_registry.async_get("binary_sensor.front_door")
        assert reg_entry is not None and reg_entry.device_id is not None
        area = ar.async_get(hass).async_get_or_create("upstairs")
        device_registry.async_update_device(
            reg_entry.device_id, area_id=area.id, name_by_user="My Front Door"
        )

        panel.push(
            {
                **SNAPSHOT,
                "seq": 8,
                "zones": [{**ZONE, "name": "Kitchen Smoke", "device_class": "smoke"}],
            }
        )
        await settle()

        device = device_registry.async_get(reg_entry.device_id)
        assert device is not None
        assert device.name_by_user == "My Front Door"
        assert device.area_id == area.id
        assert device.name == "Kitchen Smoke"  # the panel's reported name still updates

        state = hass.states.get("binary_sensor.front_door")
        assert state is not None
        assert state.attributes["friendly_name"] == "My Front Door"  # user override wins
        await hass.config_entries.async_unload(entry.entry_id)


# ---- HA09: a partition added after the first snapshot gets its controls too -----------


async def test_new_partition_gains_alarm_switch_and_button(hass: HomeAssistant) -> None:
    """Enabling partition 2 at runtime creates its alarm_control_panel,
    chime switch and exit-restart button without a reload (HA09;
    alarm_control_panel.py:34-43, switch.py:21-30, button.py:13-22 used to
    create entities only from the first snapshot)."""
    async with StatePanel() as panel:
        entry = await _setup(hass, panel)
        panel.push(
            {
                **SNAPSHOT,
                "seq": 8,
                "partitions": [PARTITION, {**PARTITION, "partition": 2, "name": "Garage"}],
            }
        )
        await settle()

        alarm_id = _entity_id(hass, "alarm_control_panel", "0a1b2c_p2")
        switch_id = _entity_id(hass, "switch", "0a1b2c_p2_chime")
        button_id = _entity_id(hass, "button", "0a1b2c_p2_exit_restart")
        assert alarm_id is not None and hass.states.get(alarm_id) is not None
        assert switch_id is not None and hass.states.get(switch_id) is not None
        assert button_id is not None and hass.states.get(button_id) is not None
        assert hass.states.get(alarm_id).state != STATE_UNAVAILABLE
        await hass.config_entries.async_unload(entry.entry_id)


async def test_removed_partition_is_unavailable_and_refuses_commands(
    hass: HomeAssistant,
) -> None:
    """A partition that later disappears from the snapshot goes unavailable
    on all three of its entities and cannot be commanded (HA09 acceptance:
    "removed partitions cannot issue commands and do not appear
    available"). Home Assistant's own service-call framework drops
    unavailable entities from a target before invoking them, so the
    `available` guard alone is what stops the command."""
    async with StatePanel() as panel:
        entry = await _setup(hass, panel)
        panel.push(
            {
                **SNAPSHOT,
                "seq": 8,
                "partitions": [PARTITION, {**PARTITION, "partition": 2, "name": "Garage"}],
            }
        )
        await settle()
        alarm_id = _entity_id(hass, "alarm_control_panel", "0a1b2c_p2")
        switch_id = _entity_id(hass, "switch", "0a1b2c_p2_chime")
        button_id = _entity_id(hass, "button", "0a1b2c_p2_exit_restart")
        assert hass.states.get(alarm_id).state != STATE_UNAVAILABLE

        panel.push({**SNAPSHOT, "seq": 9, "partitions": [PARTITION]})
        await settle()

        assert hass.states.get(alarm_id).state == STATE_UNAVAILABLE
        assert hass.states.get(switch_id).state == STATE_UNAVAILABLE
        assert hass.states.get(button_id).state == STATE_UNAVAILABLE

        commands_before = len(panel.commands)
        await hass.services.async_call(
            "alarm_control_panel",
            "alarm_disarm",
            {"entity_id": alarm_id, "code": "123456"},
            blocking=True,
        )
        assert len(panel.commands) == commands_before
        await hass.config_entries.async_unload(entry.entry_id)


# ---- HA13: concurrent chime toggles no longer race back to the same state -------------


class ChimeConfirmingPanel(StatePanel):
    """Like a real panel: once a chime command's "ok" result is sent, also
    pushes the delta the engine's toggle would cause (protocol.md - the
    result carries no state, only a later delta does)."""

    def __init__(self) -> None:
        super().__init__()
        self._chime = False

    async def _send(self, writer, msg):
        await super()._send(writer, msg)
        if msg is not None and msg.get("t") == "result" and msg.get("action") == "chime":
            self._chime = not self._chime
            self.push({"t": "delta", "seq": 9, "partitions": [{**PARTITION, "chime": self._chime}]})


async def test_concurrent_turn_on_sends_exactly_one_toggle(hass: HomeAssistant) -> None:
    """Two concurrent switch.turn_on calls used to both read "off" and both
    send the toggle, leaving the chime off again (HA13, switch.py _set,
    60-63). Only one toggle should go out, and the switch ends on."""
    async with ChimeConfirmingPanel() as panel:
        entry = await _setup(hass, panel)
        await asyncio.gather(
            *[
                hass.services.async_call(
                    "switch", "turn_on", {"entity_id": "switch.home_chime"}, blocking=True
                )
                for _ in range(2)
            ]
        )
        assert [m["action"] for m in panel.commands] == ["chime"]
        assert hass.states.get("switch.home_chime").state == STATE_ON
        await hass.config_entries.async_unload(entry.entry_id)


async def test_concurrent_turn_off_calls_send_nothing(hass: HomeAssistant) -> None:
    """Off/Off concurrently while already off: neither call should toggle
    a switch that is already at the target."""
    async with ChimeConfirmingPanel() as panel:
        entry = await _setup(hass, panel)
        await asyncio.gather(
            *[
                hass.services.async_call(
                    "switch", "turn_off", {"entity_id": "switch.home_chime"}, blocking=True
                )
                for _ in range(2)
            ]
        )
        assert panel.commands == []
        assert hass.states.get("switch.home_chime").state == STATE_OFF
        await hass.config_entries.async_unload(entry.entry_id)


async def test_on_then_off_confirms_each_before_the_next(hass: HomeAssistant) -> None:
    """On, then Off, back to back: two real toggles, ending off. If the
    second call did not wait for the first's delta, it would still see
    "off" cached and skip sending, leaving the chime stuck on."""
    async with ChimeConfirmingPanel() as panel:
        entry = await _setup(hass, panel)
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.home_chime"}, blocking=True
        )
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.home_chime"}, blocking=True
        )
        assert [m["action"] for m in panel.commands] == ["chime", "chime"]
        assert hass.states.get("switch.home_chime").state == STATE_OFF
        await hass.config_entries.async_unload(entry.entry_id)


async def test_external_change_is_seen_before_the_next_request(hass: HomeAssistant) -> None:
    """A keypad chime toggle this switch never asked for still updates
    `is_on`, and a subsequent request acts on the panel's real state, not
    on what the switch last asked for (HA13 acceptance: "external keypad
    changes")."""
    async with StatePanel() as panel:
        entry = await _setup(hass, panel)
        panel.push({"t": "delta", "seq": 9, "partitions": [{**PARTITION, "chime": True}]})
        await settle()
        assert hass.states.get("switch.home_chime").state == STATE_ON

        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.home_chime"}, blocking=True
        )
        assert panel.commands == []  # already on: a no-op, not a second toggle
        await hass.config_entries.async_unload(entry.entry_id)


async def test_turn_on_completes_without_a_confirming_delta(hass: HomeAssistant) -> None:
    """No delta ever confirms the toggle (an older panel, or one dropped on
    the wire): the call still completes rather than hanging forever."""
    async with StatePanel() as panel:
        entry = await _setup(hass, panel)
        await asyncio.wait_for(
            hass.services.async_call(
                "switch", "turn_on", {"entity_id": "switch.home_chime"}, blocking=True
            ),
            timeout=_CONFIRM_TIMEOUT_S + 3,
        )
        assert panel.commands[-1]["action"] == "chime"
        await hass.config_entries.async_unload(entry.entry_id)
