"""The push coordinator: one connection to the panel, state in, commands out.

The panel pushes a snapshot right after the paired hello and deltas as
things change (HAI-006); the coordinator folds them into a PanelState and
notifies the entities. Commands travel the same connection and their
results are matched back by id (HAI-007). The link keeps itself alive with
pings after 30 quiet seconds and gives up at 90 (protocol.md), then
reconnects with backoff while the entities show unavailable.

The other direction is HAI-009: the entities the options allow are sent to
the panel as a paged catalog, the panel answers with the ones its zones are
bound to, and their states are pushed as they change. Nothing outside the
options list ever leaves Home Assistant.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
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
from .const import (
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_PANEL_FP,
    CONF_SHARE_ENTITIES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_FIRST_SNAPSHOT_S = 15.0
_QUIET_PING_S = 30.0
_QUIET_LIMIT = 3  # three quiet ping rounds (90 s) and the link is dead
_BACKOFF_MIN_S = 5.0
_BACKOFF_MAX_S = 60.0
_COMMAND_TIMEOUT_S = 15.0
_CATALOG_PAGE = 20  # entities per catalog message; a full catalog can't fit one frame

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
        self._watched: list[str] = []
        self._panel_watch: list[str] = []  # as the panel asked, before the allow-list
        self._unwatch: Callable[[], None] | None = None

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
        await self._async_send_catalog()
        self.config_entry.async_on_unload(
            self.config_entry.add_update_listener(self._async_entry_updated)
        )
        self._task = self.config_entry.async_create_background_task(
            self.hass, self._async_run(), name=f"{DOMAIN} link"
        )

    async def async_shutdown(self) -> None:
        """Unload: stop the task and close the connection."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._async_drop_watch()
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
                    await self._async_send_catalog()
                await self._async_listen(self._client)
            except asyncio.CancelledError:
                raise
            except WrongPanel as err:
                _LOGGER.error("%s: %s", self.name, err)
                self.async_set_update_error(UpdateFailed(str(err)))
                self._async_drop_watch()
                # a factory-reset panel: the person must pair again (HAI-004)
                self.config_entry.async_start_reauth(self.hass)
                return
            except (CannotConnect, InvalidMessage, UnsupportedVersion, TimeoutError) as err:
                self.async_set_update_error(UpdateFailed(str(err)))
                # The panel asks again for what it watches on the next connection.
                self._async_drop_watch()
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
            elif t == "watch":
                await self._async_watch(msg.get("ids"))

    # ---- what the panel may see (HAI-009) -------------------------------------------

    @property
    def _allowed(self) -> list[str]:
        """The entities the options share with the panel, in the order chosen."""
        shared = self.config_entry.options.get(CONF_SHARE_ENTITIES) or []
        return [entity_id for entity_id in shared if isinstance(entity_id, str)]

    def _area_name(self, entity_id: str) -> str:
        """Where the entity is, by its own area or the one its device sits in."""
        registry_entry = er.async_get(self.hass).async_get(entity_id)
        if registry_entry is None:
            return ""
        area_id = registry_entry.area_id
        if area_id is None and registry_entry.device_id is not None:
            device = dr.async_get(self.hass).async_get(registry_entry.device_id)
            area_id = device.area_id if device is not None else None
        if area_id is None:
            return ""
        area = ar.async_get(self.hass).async_get_area(area_id)
        return area.name if area is not None else ""

    def _catalog_entity(self, entity_id: str) -> dict[str, Any]:
        """One catalog row, with the keys protocol.md names for it."""
        state = self.hass.states.get(entity_id)
        known = state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        return {
            "id": entity_id,
            "name": state.name if state is not None else entity_id,
            "area": self._area_name(entity_id),
            "domain": entity_id.partition(".")[0],
            "class": str(state.attributes.get("device_class") or "") if state is not None else "",
            "state": state.state if known and state is not None else "",
        }

    async def _async_send_catalog(self) -> None:
        """Send the allowed entities as pages; page 0 replaces what the panel had.

        A dropped link is not worth reporting here: the listening loop sees it
        too, and the catalog goes out again on the next connection.
        """
        client = self._client
        if client is None:
            return
        allowed = self._allowed
        pages = [allowed[i : i + _CATALOG_PAGE] for i in range(0, len(allowed), _CATALOG_PAGE)]
        for page, entity_ids in enumerate(pages or [[]]):
            try:
                # "page" and "more" before "entities": the panel's parser reads
                # them in this order (protocol.md), and json.dumps keeps it.
                await client.send(
                    {
                        "t": "catalog",
                        "page": page,
                        "more": page < len(pages) - 1,
                        "entities": [self._catalog_entity(e) for e in entity_ids],
                    }
                )
            except LinkError as err:
                _LOGGER.debug("%s: the catalog could not be sent: %s", self.name, err)
                return

    async def _async_watch(self, ids: Any) -> None:
        """The panel's watch list: the allowed part of it, the rest ignored."""
        allowed = set(self._allowed)
        asked = [
            entity_id
            for entity_id in (ids if isinstance(ids, list) else [])
            if isinstance(entity_id, str)
        ]
        self._panel_watch = asked  # kept whole: options may allow more of it later
        watched = [entity_id for entity_id in asked if entity_id in allowed]
        self._async_drop_watch()
        self._watched = watched
        if watched:
            self._unwatch = async_track_state_change_event(
                self.hass, watched, self._async_state_changed
            )
        for entity_id in watched:
            await self._async_send_state(entity_id)

    def _async_drop_watch(self) -> None:
        """Stop following the watched entities (link gone, or a new watch list)."""
        if self._unwatch is not None:
            self._unwatch()
            self._unwatch = None
        self._watched = []

    async def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """A watched entity moved: the panel hears about it at once (HAI-006)."""
        await self._async_send_state(event.data["entity_id"])

    async def _async_send_state(self, entity_id: str) -> None:
        """One state message. Unknown, unavailable or gone is avail false: the
        panel's zone shows CHECK after its grace time rather than "closed"."""
        client = self._client
        if client is None:
            return
        state = self.hass.states.get(entity_id)
        avail = state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        try:
            await client.send(
                {
                    "t": "state",
                    "id": entity_id,
                    "state": state.state if avail and state is not None else "",
                    "avail": avail,
                }
            )
        except LinkError as err:
            _LOGGER.debug("%s: %s could not be sent: %s", self.name, entity_id, err)

    async def _async_entry_updated(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """The options changed: a fresh catalog, and the panel's own watch list is
        filtered again - what is no longer allowed stops, what is newly allowed
        starts without waiting for a reconnect."""
        await self._async_send_catalog()
        await self._async_watch(list(self._panel_watch))

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
