"""Tests for the RoboAlarms config flow.

The connection test is still a stub (the panel's TLS API is being built
first), so entry creation cannot be tested yet: these tests pin down the
flow's shape - forms, the cannot_connect error, and a discovered panel
updating an existing entry's address instead of duplicating it (HAI-004).
"""

from ipaddress import ip_address

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.roboalarms.const import DOMAIN

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


async def test_user_flow_shows_form(hass: HomeAssistant) -> None:
    """The manual flow starts with the host form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_user_flow_reports_cannot_connect(hass: HomeAssistant) -> None:
    """Until the protocol client exists, a connection attempt fails cleanly."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.168.1.50", CONF_PORT: 6054}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_zeroconf_flow_asks_for_confirmation(hass: HomeAssistant) -> None:
    """A discovered panel shows the confirm step with its name."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=DISCOVERY
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_zeroconf_updates_existing_entry(hass: HomeAssistant) -> None:
    """Discovery of a known panel updates its address, no duplicate (HAI-004)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="0a1b2c",
        data={CONF_HOST: "192.168.1.7", CONF_PORT: 6054},
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
