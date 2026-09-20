"""Config flow for the RoboAlarms Panel integration.

Covers discovery (zeroconf) and manual entry. The connection is always
tested before an entry is created (HAI-004); a discovered panel whose entry
already exists gets its host updated instead of a duplicate.

The connection test speaks the real link protocol (aiopanel.py): it will
succeed as soon as the panel firmware's TLS API exists. The pairing step
(HAI-003), reauth and reconfigure land together with that firmware.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .aiopanel import (
    CannotConnect,
    InvalidMessage,
    PanelClient,
    PanelInfo,
    UnsupportedVersion,
)
from .const import DEFAULT_PORT, DOMAIN, ZC_PROP_ID, ZC_PROP_NAME

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
    }
)


async def _async_validate_connection(hass: HomeAssistant, host: str, port: int) -> PanelInfo:
    """Connect to the panel and read its hello."""
    client = PanelClient(host, port)
    try:
        return await client.connect()
    finally:
        await client.close()


class RoboAlarmsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for a RoboAlarms Panel."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._host: str | None = None
        self._port: int = DEFAULT_PORT
        self._name: str | None = None

    async def _async_try_create(
        self, host: str, port: int, errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        """Test the connection and create the entry; None = show the form again."""
        try:
            info = await _async_validate_connection(self.hass, host, port)
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidMessage:
            errors["base"] = "cannot_connect"
        except UnsupportedVersion:
            errors["base"] = "unsupported"
        else:
            await self.async_set_unique_id(info.panel_id, raise_on_progress=False)
            self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
            return self.async_create_entry(
                title=info.name or f"Panel {info.panel_id}",
                data={CONF_HOST: host, CONF_PORT: port},
            )
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle manual entry of the panel's address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_try_create(
                user_input[CONF_HOST], user_input[CONF_PORT], errors
            )
            if result is not None:
                return result
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """Handle a panel found by mDNS."""
        panel_id = discovery_info.properties.get(ZC_PROP_ID, "").lower()
        if not panel_id:
            return self.async_abort(reason="unknown")

        self._host = discovery_info.host
        self._port = discovery_info.port or DEFAULT_PORT
        self._name = discovery_info.properties.get(ZC_PROP_NAME) or f"Panel {panel_id}"

        await self.async_set_unique_id(panel_id)
        # A known panel that moved to a new address is updated, not duplicated.
        self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: self._port})

        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a discovered panel."""
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._host is not None
            result = await self._async_try_create(self._host, self._port, errors)
            if result is not None:
                return result
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"name": self._name or "", "host": self._host or ""},
            errors=errors,
        )
