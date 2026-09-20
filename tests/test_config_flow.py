"""Tests for the RoboAlarms config flow.

The connection test is patched here (the real one is exercised against a
fake panel in test_aiopanel.py): these tests pin the flow's behaviour -
create after a successful test, recover from cannot_connect, and a
discovered panel updating an existing entry instead of duplicating (HAI-004).
"""

from ipaddress import ip_address
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.roboalarms.aiopanel import CannotConnect, PanelInfo, UnsupportedVersion
from custom_components.roboalarms.const import DOMAIN

INFO = PanelInfo(
    panel_id="0a1b2c", name="Home", model="crowpanel-p4", fw="0.6.0", api=1, paired=False
)

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


def _patch_validate(**kwargs):
    return patch(
        "custom_components.roboalarms.config_flow._async_validate_connection",
        AsyncMock(**kwargs),
    )


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """Manual entry: the form, a successful test, the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    with _patch_validate(return_value=INFO):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.50", CONF_PORT: 6054}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["data"] == {CONF_HOST: "192.168.1.50", CONF_PORT: 6054}
    assert result["result"].unique_id == "0a1b2c"


async def test_user_flow_recovers_from_cannot_connect(hass: HomeAssistant) -> None:
    """A failed test shows the form again; the flow then recovers."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with _patch_validate(side_effect=CannotConnect("down")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.50", CONF_PORT: 6054}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    with _patch_validate(return_value=INFO):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.50", CONF_PORT: 6054}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_unsupported_version(hass: HomeAssistant) -> None:
    """A panel speaking another protocol version gets its own message."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with _patch_validate(side_effect=UnsupportedVersion("api 2")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.50", CONF_PORT: 6054}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unsupported"}


async def test_user_flow_duplicate_updates_address(hass: HomeAssistant) -> None:
    """Adding a panel that already has an entry updates it instead."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="0a1b2c", data={CONF_HOST: "192.168.1.7", CONF_PORT: 6054}
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with _patch_validate(return_value=INFO):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.50", CONF_PORT: 6054}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.168.1.50"


async def test_zeroconf_flow_creates_entry(hass: HomeAssistant) -> None:
    """A discovered panel: confirm, test, entry with the discovered address."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=DISCOVERY
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"

    with _patch_validate(return_value=INFO):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["data"] == {CONF_HOST: "192.168.1.50", CONF_PORT: 6054}
    assert result["result"].unique_id == "0a1b2c"


async def test_zeroconf_cannot_connect_shows_form_again(hass: HomeAssistant) -> None:
    """A discovered panel that then doesn't answer keeps the confirm step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=DISCOVERY
    )
    with _patch_validate(side_effect=CannotConnect("down")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["errors"] == {"base": "cannot_connect"}


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
