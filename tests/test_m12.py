"""M12 action, diagnostic and recovery acceptance tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_init import PARTITION, ZONE, StatePanel, _setup, _wait_for_state

from custom_components.roboalarms.aiopanel import UnsupportedVersion
from custom_components.roboalarms.const import DOMAIN
from custom_components.roboalarms.coordinator import RoboAlarmsCoordinator

pytestmark = pytest.mark.usefixtures("socket_enabled")


@pytest.mark.parametrize(
    ("service", "fields", "action"),
    [
        ("bypass_zone", {"zone": 1}, "bypass"),
        ("unbypass_zone", {"zone": 1}, "unbypass"),
        ("arm", {"level": "away", "silent_exit": True, "no_entry_delay": True}, "arm"),
        ("panic", {"panic": "police"}, "panic"),
    ],
)
async def test_actions(hass: HomeAssistant, service, fields, action):
    async with StatePanel() as panel:
        entry = await _setup(hass, panel)
        await hass.services.async_call(
            DOMAIN, service, {"entry_id": entry.entry_id, "code": "2468", **fields}, blocking=True
        )
        command = panel.commands[-1]
        assert command["action"] == action
        assert command["code"] == "2468"
        if action == "arm":
            assert command["flags"] == ["silent_exit", "no_entry_delay"]
        panel.command_result = "not_allowed"
        with pytest.raises(HomeAssistantError, match="not allowed"):
            await hass.services.async_call(
                DOMAIN,
                service,
                {"entry_id": entry.entry_id, "code": "2468", **fields},
                blocking=True,
            )


async def test_diagnostic_push(hass: HomeAssistant):
    async with StatePanel() as panel:
        await _setup(hass, panel)
        panel.push(
            {
                "t": "delta",
                "zones": [{**ZONE, "tamper": True, "low_battery": True, "supervision": True}],
                "partitions": [{**PARTITION, "installer_mode": True}],
                "troubles": {"count": 1, "system": ["ac_loss"]},
            }
        )
        registry = er.async_get(hass)
        for suffix in [
            "z1_tamper",
            "z1_low_battery",
            "z1_supervision",
            "trouble",
            "installer_mode",
        ]:
            entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, f"0a1b2c_{suffix}")
            assert entity_id
            await _wait_for_state(hass, entity_id, "on")
        entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, "0a1b2c_ac_power")
        await _wait_for_state(hass, entity_id, "off")


async def test_unsupported_protocol_repair_recovers(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test panel",
        data={
            "host": "panel.example",
            "port": 6054,
            "client_key": "key",
            "client_cert": "cert",
            "panel_fp": "00" * 32,
        },
    )
    entry.add_to_hass(hass)
    coordinator = RoboAlarmsCoordinator(hass, entry)
    client = MagicMock(
        connect=AsyncMock(side_effect=UnsupportedVersion("api mismatch")),
        close=AsyncMock(),
        panel_cert_der=None,
    )
    with (
        patch("custom_components.roboalarms.coordinator.client_ssl_context", return_value=None),
        patch("custom_components.roboalarms.coordinator.PanelClient", return_value=client),
    ):
        with pytest.raises(UnsupportedVersion):
            await coordinator._async_connect()
        issue_id = f"unsupported_protocol_{entry.entry_id}"
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
        client.connect.side_effect = None
        await coordinator._async_connect()
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_telemetry_and_update(hass: HomeAssistant):
    async with StatePanel() as panel:
        await _setup(hass, panel)
        panel.push(
            {
                "t": "status",
                "uptime_s": 125,
                "rssi": -54,
                "update": {
                    "installed": "0.6.0",
                    "latest": "0.7.0",
                    "available": True,
                    "in_progress": False,
                    "progress": -1,
                },
            }
        )
        registry = er.async_get(hass)
        await _wait_for_state(
            hass, registry.async_get_entity_id("sensor", DOMAIN, "0a1b2c_uptime_s"), "125"
        )
        await _wait_for_state(
            hass, registry.async_get_entity_id("sensor", DOMAIN, "0a1b2c_rssi"), "-54"
        )
        await _wait_for_state(
            hass, registry.async_get_entity_id("update", DOMAIN, "0a1b2c_firmware"), "on"
        )


async def test_zone_removed_then_readded(hass: HomeAssistant):
    import asyncio

    from homeassistant.helpers import device_registry as dr
    from test_init import SNAPSHOT

    async with StatePanel() as panel:
        entry = await _setup(hass, panel)
        registry = er.async_get(hass)
        panel.push({**SNAPSHOT, "zones": []})
        await asyncio.sleep(0.2)
        await hass.async_block_till_done()
        assert registry.async_get_entity_id("binary_sensor", DOMAIN, "0a1b2c_z1") is None
        assert not any(
            (DOMAIN, "0a1b2c_z1") in d.identifiers
            for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        )
        panel.push(SNAPSHOT)
        await asyncio.sleep(0.2)
        await hass.async_block_till_done()
        entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, "0a1b2c_z1")
        assert entity_id
        await _wait_for_state(hass, entity_id, "off")
