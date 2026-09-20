"""Config flow for the AlarmSystem integration.

Covers discovery (zeroconf) and manual entry. The connection is always
tested before an entry is created (HAI-004); a discovered panel whose entry
already exists gets its host updated instead of a duplicate.

The connection test is a stub until the protocol client (aioalarmsystem)
exists: the panel side of the API is built first, so for now every attempt
reports "cannot connect". Reauth and reconfigure follow with the client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import DEFAULT_PORT, DOMAIN, ZC_PROP_ID, ZC_PROP_NAME

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
    }
)


@dataclass
class PanelInfo:
    """What the panel's hello reports during the connection test."""

    panel_id: str
    name: str


async def _async_validate_connection(hass: HomeAssistant, host: str, port: int) -> PanelInfo:
    """Connect to the panel and read its hello.

    TODO(M12): wire up aioalarmsystem once the panel's TLS API exists.
    """
    raise CannotConnect


class AlarmSystemConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for an AlarmSystem panel."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._host: str | None = None
        self._port: int = DEFAULT_PORT
        self._name: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle manual entry of the panel's address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await _async_validate_connection(
                    self.hass, user_input[CONF_HOST], user_input[CONF_PORT]
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(info.panel_id, raise_on_progress=False)
                self._abort_if_unique_id_configured(updates=dict(user_input))
                return self.async_create_entry(title=info.name, data=user_input)
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
            try:
                info = await _async_validate_connection(self.hass, self._host, self._port)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=info.name,
                    data={CONF_HOST: self._host, CONF_PORT: self._port},
                )
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"name": self._name or "", "host": self._host or ""},
            errors=errors,
        )


class CannotConnect(HomeAssistantError):
    """The panel could not be reached or refused the connection."""
