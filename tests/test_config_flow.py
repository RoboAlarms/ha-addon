"""Tests for the RoboAlarms config flow.

The client is patched here (it is exercised for real against a fake panel in
test_aiopanel.py): these tests pin the flow's journey - test before configure
(HAI-004), the pairing steps with the code shown while the panel waits for
Allow (HAI-003), the entry's contents, and a discovered panel updating an
existing entry instead of duplicating.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from ipaddress import ip_address
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.roboalarms.aiopanel import CannotConnect, PairingFailed, PanelInfo
from custom_components.roboalarms.const import (
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_PANEL_FP,
    DOMAIN,
)

INFO = PanelInfo(
    panel_id="0a1b2c", name="Home", model="crowpanel-p4", fw="0.6.0", api=1, paired=False
)
PANEL_FP = bytes(range(0x60, 0x80))

DISCOVERY = ZeroconfServiceInfo(
    ip_address=ip_address("192.168.1.50"),
    ip_addresses=[ip_address("192.168.1.50")],
    hostname="roboalarms-0a1b2c.local.",
    name="Home._roboalarms._tcp.local.",
    port=6054,
    properties={
        "id": "0a1b2c",
        "name": "Home",
        "model": "crowpanel-p4",
        "fw": "0.6.0",
        "api": "1",
        "paired": "0",
    },
    type="_roboalarms._tcp.local.",
)


def _mock_client() -> tuple[MagicMock, asyncio.Event]:
    """A client whose pair_wait blocks (like a person walking to the panel)
    until the returned event is set - so the flow really shows its progress."""
    client = MagicMock()
    client.connect = AsyncMock(return_value=INFO)
    client.info = INFO
    client.close = AsyncMock()
    client.pair_begin = AsyncMock(return_value="588460")
    allow = asyncio.Event()

    async def _wait() -> bytes:
        await allow.wait()
        result = client.pair_outcome
        if isinstance(result, Exception):
            raise result
        return result

    client.pair_outcome = PANEL_FP
    client.pair_wait = AsyncMock(side_effect=_wait)
    return client, allow


@contextmanager
def _patched(client: MagicMock):
    with (
        patch(
            "custom_components.roboalarms.config_flow.PanelClient", return_value=client
        ) as client_cls,
        patch(
            "custom_components.roboalarms.config_flow.generate_client_identity",
            return_value=("KEY-PEM", "CERT-PEM"),
        ),
        patch("custom_components.roboalarms.config_flow.client_ssl_context", return_value=None),
        patch(
            "custom_components.roboalarms.config_flow.cert_der_from_pem",
            return_value=b"CERT-DER",
        ),
    ):
        yield client_cls


async def _drive_to_pair(hass: HomeAssistant) -> str:
    """Manual entry up to the pair instruction step; returns the flow id."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.168.1.50", CONF_PORT: 6054}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"
    return result["flow_id"]


async def test_full_flow_pairs_and_creates_entry(hass: HomeAssistant) -> None:
    """Manual entry, pairing with the code shown, entry with the pinned panel."""
    client, allow = _mock_client()
    with _patched(client):
        flow_id = await _drive_to_pair(hass)
        result = await hass.config_entries.flow.async_configure(flow_id, {})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["step_id"] == "pair_wait"
        assert result["description_placeholders"] == {"code": "588460"}
        allow.set()  # the person at the panel taps Allow
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["data"] == {
        CONF_HOST: "192.168.1.50",
        CONF_PORT: 6054,
        CONF_PANEL_FP: PANEL_FP.hex(),
        CONF_CLIENT_KEY: "KEY-PEM",
        CONF_CLIENT_CERT: "CERT-PEM",
    }
    assert result["result"].unique_id == "0a1b2c"


async def test_zeroconf_flow_reaches_pairing(hass: HomeAssistant) -> None:
    """A discovered panel: confirm, test, then the pairing instruction."""
    client, _allow = _mock_client()
    with _patched(client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=DISCOVERY
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"


async def test_pair_window_closed_shows_error_then_recovers(hass: HomeAssistant) -> None:
    """Pairing not open on the panel: an error on the pair step, then it works."""
    client, _allow = _mock_client()
    client.pair_begin.side_effect = [PairingFailed("not_pairing"), "588460"]
    with _patched(client):
        flow_id = await _drive_to_pair(hass)
        result = await hass.config_entries.flow.async_configure(flow_id, {})
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "not_pairing"}
        result = await hass.config_entries.flow.async_configure(flow_id, {})
        assert result["type"] is FlowResultType.SHOW_PROGRESS


async def test_pair_rejected_aborts(hass: HomeAssistant) -> None:
    """The person at the panel rejects the code: the flow ends with the reason."""
    client, allow = _mock_client()
    client.pair_outcome = PairingFailed("rejected")
    with _patched(client):
        flow_id = await _drive_to_pair(hass)
        result = await hass.config_entries.flow.async_configure(flow_id, {})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        allow.set()
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pair_rejected"


async def test_user_flow_recovers_from_cannot_connect(hass: HomeAssistant) -> None:
    """A failed connection test shows the form again; the flow then recovers."""
    client, _allow = _mock_client()
    client.connect.side_effect = [CannotConnect("down"), INFO]
    with _patched(client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.50", CONF_PORT: 6054}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "cannot_connect"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.50", CONF_PORT: 6054}
        )
        assert result["step_id"] == "pair"


async def test_user_flow_duplicate_updates_address(hass: HomeAssistant) -> None:
    """Adding a panel that already has an entry updates it instead."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="0a1b2c", data={CONF_HOST: "192.168.1.7", CONF_PORT: 6054}
    )
    entry.add_to_hass(hass)
    client, _allow = _mock_client()
    with _patched(client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.50", CONF_PORT: 6054}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.168.1.50"


async def test_zeroconf_updates_existing_entry(hass: HomeAssistant) -> None:
    """Discovery of a known panel updates its address, no duplicate (HAI-004)."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="0a1b2c", data={CONF_HOST: "192.168.1.7", CONF_PORT: 6054}
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=DISCOVERY
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.168.1.50"


async def test_zeroconf_without_panel_id_aborts(hass: HomeAssistant) -> None:
    """A service without the panel id TXT key is not ours."""
    anonymous = ZeroconfServiceInfo(
        ip_address=ip_address("192.168.1.51"),
        ip_addresses=[ip_address("192.168.1.51")],
        hostname="mystery.local.",
        name="Mystery._roboalarms._tcp.local.",
        port=6054,
        properties={},
        type="_roboalarms._tcp.local.",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=anonymous
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"
