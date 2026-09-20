"""Config flow for the RoboAlarms Panel integration.

Discovery (zeroconf) or manual entry, then the connection is tested before
anything is created (HAI-004), then pairing (HAI-003): the flow starts the
commitment exchange, both screens show the same 6-digit code, and the entry
is created when the person at the panel taps Allow. The entry keeps this
client's key and certificate and the panel's pinned fingerprint.

A discovered panel whose entry already exists gets its host updated instead
of a duplicate. Reauth and reconfigure land next.

The options flow is the other half of HAI-009: Home Assistant decides which
entities the panel may see, and nothing outside that list is ever sent.
"""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .aiopanel import (
    CannotConnect,
    InvalidMessage,
    PairingFailed,
    PanelClient,
    UnsupportedVersion,
    cert_der_from_pem,
    client_ssl_context,
    generate_client_identity,
)
from .const import (
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_PANEL_FP,
    CONF_SHARE_ENTITIES,
    DEFAULT_PORT,
    DOMAIN,
    ZC_PROP_ID,
    ZC_PROP_NAME,
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
    }
)

_PAIR_ERRORS = {"not_pairing", "busy", "timeout"}


class RoboAlarmsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for a RoboAlarms Panel."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> RoboAlarmsOptionsFlow:
        """Options: which entities the panel may use as zones (HAI-009)."""
        return RoboAlarmsOptionsFlow()

    def __init__(self) -> None:
        """Initialize the flow."""
        self._host: str | None = None
        self._port: int = DEFAULT_PORT
        self._name: str | None = None
        self._key_pem: str | None = None
        self._cert_pem: str | None = None
        self._ssl = None
        self._client: PanelClient | None = None
        self._code = ""
        self._panel_fp = b""
        self._fail = "cannot_connect"
        self._pair_task: asyncio.Task | None = None

    # ---- plumbing ----------------------------------------------------------------

    async def _async_ensure_identity(self) -> None:
        """This flow's client identity: one key pair for validate, pair and entry."""
        if self._key_pem is None:
            self._key_pem, self._cert_pem = await self.hass.async_add_executor_job(
                generate_client_identity
            )
            self._ssl = await self.hass.async_add_executor_job(
                client_ssl_context, self._key_pem, self._cert_pem
            )

    async def _async_connect(self) -> PanelClient:
        """Open a connection and exchange hellos (the test-before-configure)."""
        await self._async_ensure_identity()
        assert self._host is not None
        client = PanelClient(self._host, self._port, ssl=self._ssl)
        try:
            await client.connect()
        except Exception:
            await client.close()
            raise
        return client

    async def _async_close_client(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _async_validate(self, errors: dict[str, str]) -> bool:
        """Test the connection; on success the flow's unique id and name are set."""
        try:
            client = await self._async_connect()
        except CannotConnect:
            errors["base"] = "cannot_connect"
            return False
        except InvalidMessage:
            errors["base"] = "cannot_connect"
            return False
        except UnsupportedVersion:
            errors["base"] = "unsupported"
            return False
        info = client.info
        await client.close()
        assert info is not None
        await self.async_set_unique_id(info.panel_id, raise_on_progress=False)
        self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: self._port})
        self._name = info.name or f"Panel {info.panel_id}"
        return True

    # ---- entry points ------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle manual entry of the panel's address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST]
            self._port = user_input[CONF_PORT]
            if await self._async_validate(errors):
                return await self.async_step_pair()
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
            if await self._async_validate(errors):
                return await self.async_step_pair()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"name": self._name or "", "host": self._host or ""},
            errors=errors,
        )

    # ---- reauth: the panel was factory-reset or replaced (HAI-004) ----------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """The pinned certificate no longer matches: pair again."""
        entry = self._get_reauth_entry()
        self._host = entry.data[CONF_HOST]
        self._port = entry.data[CONF_PORT]
        self._name = entry.title
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Check it is the same panel, then walk the normal pairing steps."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                client = await self._async_connect()
            except (CannotConnect, InvalidMessage, TimeoutError):
                errors["base"] = "cannot_connect"
            except UnsupportedVersion:
                errors["base"] = "unsupported"
            else:
                info = client.info
                await client.close()
                assert info is not None
                if info.panel_id != self._get_reauth_entry().unique_id:
                    return self.async_abort(reason="wrong_device")
                return await self.async_step_pair()
        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={"name": self._name or ""},
            errors=errors,
        )

    # ---- pairing (HAI-003) -------------------------------------------------------

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask the user to open pairing on the panel, then start the exchange."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._client = await self._async_connect()
                assert self._cert_pem is not None
                cert_der = await self.hass.async_add_executor_job(cert_der_from_pem, self._cert_pem)
                self._code = await self._client.pair_begin(cert_der)
            except PairingFailed as err:
                await self._async_close_client()
                errors["base"] = "not_pairing" if err.reason in _PAIR_ERRORS else "cannot_connect"
            except (CannotConnect, InvalidMessage, TimeoutError):
                await self._async_close_client()
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_pair_wait()
        return self.async_show_form(
            step_id="pair",
            description_placeholders={"name": self._name or ""},
            errors=errors,
        )

    async def async_step_pair_wait(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Both screens show the code; wait for Allow on the panel."""
        assert self._client is not None
        if self._pair_task is None:
            self._pair_task = self.hass.async_create_task(self._client.pair_wait())
        if not self._pair_task.done():
            return self.async_show_progress(
                step_id="pair_wait",
                progress_action="wait_allow",
                progress_task=self._pair_task,
                description_placeholders={"code": self._code},
            )
        try:
            self._panel_fp = self._pair_task.result()
        except PairingFailed as err:
            self._fail = f"pair_{err.reason}" if err.reason != "unknown" else "cannot_connect"
            return self.async_show_progress_done(next_step_id="pair_failed")
        except (CannotConnect, InvalidMessage, TimeoutError):
            self._fail = "cannot_connect"
            return self.async_show_progress_done(next_step_id="pair_failed")
        finally:
            self._pair_task = None
        return self.async_show_progress_done(next_step_id="pair_done")

    async def async_step_pair_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Paired: pin the panel; create the entry, or repair the old one."""
        await self._async_close_client()
        assert self._key_pem is not None and self._cert_pem is not None
        data = {
            CONF_HOST: self._host,
            CONF_PORT: self._port,
            CONF_PANEL_FP: self._panel_fp.hex(),
            CONF_CLIENT_KEY: self._key_pem,
            CONF_CLIENT_CERT: self._cert_pem,
        }
        if self.source == config_entries.SOURCE_REAUTH:
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=data)
        return self.async_create_entry(title=self._name or "RoboAlarms Panel", data=data)

    async def async_step_pair_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The panel said no (or the window closed): end the flow with the reason."""
        await self._async_close_client()
        return self.async_abort(reason=self._fail)


class RoboAlarmsOptionsFlow(OptionsFlow):
    """What the panel may see of this Home Assistant (HAI-009).

    One list, empty by default: the panel is told about these entities and
    nothing else, and a watch for anything outside it is ignored.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick the entities the panel may use as zones."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SHARE_ENTITIES,
                    default=list(self.config_entry.options.get(CONF_SHARE_ENTITIES, [])),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["binary_sensor"], multiple=True)
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
