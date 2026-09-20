"""The push coordinator: one connection to the panel, state in, commands out.

The panel pushes a snapshot right after the paired hello and deltas as
things change (HAI-006); the coordinator folds them into a PanelState and
notifies the entities. Commands travel the same connection and their
results are matched back by id (HAI-007). The link keeps itself alive with
pings after 30 quiet seconds and gives up at 90 (protocol.md), then
reconnects with backoff while the entities show unavailable.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aiopanel import (
    CannotConnect,
    InvalidMessage,
    LinkError,
    PanelClient,
    PanelInfo,
    PanelState,
    UnsupportedVersion,
    client_ssl_context,
)
from .const import CONF_CLIENT_CERT, CONF_CLIENT_KEY, CONF_PANEL_FP, DOMAIN

_LOGGER = logging.getLogger(__name__)

_FIRST_SNAPSHOT_S = 15.0
_QUIET_PING_S = 30.0
_QUIET_LIMIT = 3  # three quiet ping rounds (90 s) and the link is dead
_BACKOFF_MIN_S = 5.0
_BACKOFF_MAX_S = 60.0
_COMMAND_TIMEOUT_S = 15.0

type RoboAlarmsConfigEntry = ConfigEntry[RoboAlarmsCoordinator]


class WrongPanel(LinkError):
    """The certificate on the other end isn't the pinned one."""


class RoboAlarmsCoordinator(DataUpdateCoordinator[PanelState]):
    """Owns the connection; entities subscribe through CoordinatorEntity."""

    config_entry: RoboAlarmsConfigEntry

    def __init__(self, hass: HomeAssistant, entry: RoboAlarmsConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {entry.title}")
        self.info: PanelInfo | None = None
        self._client: PanelClient | None = None
        self._ssl = None
        self._task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    # ---- connecting ---------------------------------------------------------------

    async def _async_connect(self) -> PanelClient:
        """Connect with this entry's identity and check the pinned fingerprint."""
        entry = self.config_entry
        if self._ssl is None:
            self._ssl = await self.hass.async_add_executor_job(
                client_ssl_context, entry.data[CONF_CLIENT_KEY], entry.data[CONF_CLIENT_CERT]
            )
        client = PanelClient(entry.data[CONF_HOST], entry.data[CONF_PORT], ssl=self._ssl)
        try:
            self.info = await client.connect()
            der = client.panel_cert_der
            if der is not None and hashlib.sha256(der).hexdigest() != entry.data[CONF_PANEL_FP]:
                # Not the panel this entry paired with (a factory reset, or an impostor).
                raise WrongPanel("the panel's certificate isn't the pinned one")
        except BaseException:
            await client.close()
            raise
        return client

    async def async_start(self) -> None:
        """First connection and first snapshot, then the listening task.

        Raises toward setup so a dead panel becomes ConfigEntryNotReady there.
        """
        client = await self._async_connect()
        state = PanelState()
        try:
            async with asyncio.timeout(_FIRST_SNAPSHOT_S):
                while not state.ready:
                    msg = await client.recv()
                    if msg.get("t") == "snapshot":
                        state.apply(msg)
        except (TimeoutError, LinkError):
            await client.close()
            raise CannotConnect("the panel never sent its snapshot") from None
        self._client = client
        self.async_set_updated_data(state)
        self._task = self.config_entry.async_create_background_task(
            self.hass, self._async_run(), name=f"{DOMAIN} link"
        )

    async def async_shutdown(self) -> None:
        """Unload: stop the task and close the connection."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._client is not None:
            await self._client.close()
            self._client = None
        await super().async_shutdown()

    # ---- the listening loop -------------------------------------------------------

    async def _async_run(self) -> None:
        backoff = _BACKOFF_MIN_S
        while True:
            try:
                if self._client is None:
                    self._client = await self._async_connect()
                    # data continues from the fresh snapshot the panel sends
                await self._async_listen(self._client)
            except asyncio.CancelledError:
                raise
            except WrongPanel as err:
                _LOGGER.error("%s: %s", self.name, err)
                self.async_set_update_error(UpdateFailed(str(err)))
                # a factory-reset panel: the person must pair again (HAI-004)
                self.config_entry.async_start_reauth(self.hass)
                return
            except (CannotConnect, InvalidMessage, UnsupportedVersion, TimeoutError) as err:
                self.async_set_update_error(UpdateFailed(str(err)))
                if self._client is not None:
                    await self._client.close()
                    self._client = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(CannotConnect("the link dropped"))
                self._pending.clear()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)
            else:
                backoff = _BACKOFF_MIN_S

    async def _async_listen(self, client: PanelClient) -> None:
        state = self.data if self.data is not None and self.data.ready else PanelState()
        quiet = 0
        while True:
            try:
                msg = await client.recv(timeout=_QUIET_PING_S)
            except TimeoutError:
                quiet += 1
                if quiet >= _QUIET_LIMIT:
                    raise CannotConnect("the panel went quiet") from None
                await client.send({"t": "ping"})
                continue
            quiet = 0
            t = msg.get("t")
            if t in ("snapshot", "delta"):
                state.apply(msg)
                self.async_set_updated_data(state)
            elif t == "event":
                self.hass.bus.async_fire(f"{DOMAIN}_event", msg)
            elif t == "result":
                fut = self._pending.pop(str(msg.get("id") or ""), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)

    # ---- commands (HAI-007) ---------------------------------------------------------

    async def async_command(self, action: str, **fields: Any) -> dict[str, Any]:
        """Send a command and wait for the panel's answer to it."""
        client = self._client
        if client is None:
            raise HomeAssistantError("The panel is not connected")
        cmd_id = uuid4().hex[:8]
        fut: asyncio.Future[dict[str, Any]] = self.hass.loop.create_future()
        self._pending[cmd_id] = fut
        try:
            await client.send({"t": "command", "id": cmd_id, "action": action, **fields})
            async with asyncio.timeout(_COMMAND_TIMEOUT_S):
                result = await fut
        except (TimeoutError, LinkError) as err:
            raise HomeAssistantError("The panel did not answer the command") from err
        finally:
            self._pending.pop(cmd_id, None)
        verdict = str(result.get("result") or "")
        if verdict != "ok":
            raise HomeAssistantError(f"The panel refused it: {verdict.replace('_', ' ')}")
        return result
