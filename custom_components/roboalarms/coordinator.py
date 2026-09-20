"""The push coordinator: one connection to the panel, state in, commands out.

The panel pushes a snapshot right after the paired hello and deltas as
things change (HAI-006); the coordinator folds them into a PanelState and
notifies the entities. Commands travel the same connection and their
results are matched back by id (HAI-007). The link keeps itself alive with
pings after 30 quiet seconds and gives up at 90 (protocol.md), then
reconnects with backoff while the entities show unavailable.

Every connection is a generation of its own. It starts from an empty
PanelState and publishes nothing until that connection has sent a snapshot
of its own, so a delta can never be folded into the previous session's
baseline (HA12). The same dispatcher reads the messages during that
resynchronisation and afterwards, because the panel sends its watch list
*before* its first snapshot (ha_link.c, push_state) and throwing it away
leaves every sensor it binds silent (HA01).

The other direction is HAI-009: the entities the options allow are sent to
the panel as a paged catalog, the panel answers with the ones its zones are
bound to, and their states are pushed as they change - on every connection,
because the panel demands fresh state from every sensor after a link comes
back and shows CHECK otherwise (Features/22, ZSRC-015). The catalog also
says which of them the panel's Devices screen may act on (Features/24); a
`call` for one of those runs beside the receive loop, never in it, so a
slow lamp cannot delay an alarm (HA03). Nothing outside the options lists
ever leaves Home Assistant or is acted on.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Iterable
from contextlib import suppress
from functools import partial
from typing import Any, NamedTuple
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
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
    CONF_CONTROL_ENTITIES,
    CONF_PANEL_FP,
    CONF_SHARE_ENTITIES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_FIRST_SNAPSHOT_S = 15.0  # every connection's own resynchronisation deadline
_QUIET_PING_S = 30.0
_QUIET_LIMIT = 3  # three quiet ping rounds (90 s) and the link is dead
_BACKOFF_MIN_S = 5.0
_BACKOFF_MAX_S = 60.0
_COMMAND_TIMEOUT_S = 15.0
_CATALOG_PAGE = 20  # entities per catalog message; a full catalog can't fit one frame
# One `call` from the panel's Devices screen gets this long. Shorter than the
# panel's own HC_ACT_TIMEOUT_MS (30 s, home_control.h), so it hears a real
# answer rather than timing the action out by itself.
_CALL_TIMEOUT_S = 25.0
_CALLS_MAX = 8  # device actions running at once; the rest are refused, not queued
_CALL_STOP_S = 5.0  # how long unload waits for cancelled calls to let go
_ANSWERED_MAX = 64  # call ids remembered per connection, so a repeat isn't re-run

type RoboAlarmsConfigEntry = ConfigEntry[RoboAlarmsCoordinator]


class _Call(NamedTuple):
    """One row of Features/24's "Actions per domain": what a panel action does."""

    service: str
    field: str = ""  # the service data key the argument fills ("" = no argument)
    source: str = ""  # where it comes from: "value", "hundredths" or "mode"
    required: bool = True  # False: the argument may be left out


# What the panel may ask for, per domain. Nothing outside this table is ever
# executed and nothing in it is guessed at: alarm_control_panel and siren are
# absent on purpose (Features/24 - no alarm loops through the Devices screen).
_CALLS: dict[str, dict[str, _Call]] = {
    "light": {
        "turn_on": _Call("turn_on", "brightness_pct", "value", required=False),
        "turn_off": _Call("turn_off"),
        "toggle": _Call("toggle"),
    },
    "switch": {
        "turn_on": _Call("turn_on"),
        "turn_off": _Call("turn_off"),
        "toggle": _Call("toggle"),
    },
    "input_boolean": {
        "turn_on": _Call("turn_on"),
        "turn_off": _Call("turn_off"),
        "toggle": _Call("toggle"),
    },
    "fan": {
        "turn_on": _Call("turn_on"),
        "turn_off": _Call("turn_off"),
        "set_percentage": _Call("set_percentage", "percentage", "value"),
    },
    "climate": {
        "set_hvac_mode": _Call("set_hvac_mode", "hvac_mode", "mode"),
        "set_temperature": _Call("set_temperature", "temperature", "hundredths"),
        "set_preset_mode": _Call("set_preset_mode", "preset_mode", "mode"),
    },
    "cover": {
        "open": _Call("open_cover"),
        "close": _Call("close_cover"),
        "stop": _Call("stop_cover"),
        "set_position": _Call("set_cover_position", "position", "value"),
    },
    "lock": {
        "lock": _Call("lock"),
        "unlock": _Call("unlock"),
    },
    "valve": {
        "open": _Call("open_valve"),
        "close": _Call("close_valve"),
    },
    "scene": {"turn_on": _Call("turn_on")},
    "script": {"turn_on": _Call("turn_on")},
}


def _call_data(wanted: _Call, msg: dict[str, Any]) -> dict[str, Any] | None:
    """The service data for one call, or None when its argument is missing or
    of the wrong kind - the panel hears not_allowed rather than a guess."""
    if not wanted.field:
        return {}
    if wanted.source == "mode":
        mode = msg.get("mode")
        if isinstance(mode, str) and mode:
            return {wanted.field: mode}
        return None if wanted.required else {}
    value = msg.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None if wanted.required else {}
    if wanted.source == "hundredths":
        # temperatures travel as hundredths of a degree (protocol.md)
        return {wanted.field: value / 100}
    return {wanted.field: value}


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
        self._watch_seen = False  # the panel repeated its list on this connection
        self._unwatch: Callable[[], None] | None = None
        self._outbox: dict[str, None] = {}  # entities whose newest state is owed
        self._draining = False
        self._calls: dict[str, asyncio.Task] = {}  # device actions running right now
        self._answered: dict[str, str] = {}  # call id -> the one answer it was given

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
            if entry.unique_id and self.info.panel_id != entry.unique_id:
                raise WrongPanel("the address belongs to another panel")
            if not self.info.paired:
                raise WrongPanel("the panel has been unpaired; pair again")
            der = client.panel_cert_der
            if der is not None and hashlib.sha256(der).hexdigest() != entry.data[CONF_PANEL_FP]:
                # Not the panel this entry paired with (a factory reset, or an impostor).
                raise WrongPanel("the panel's certificate isn't the pinned one")
        except UnsupportedVersion:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"unsupported_protocol_{entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="unsupported_protocol",
                translation_placeholders={"name": entry.title},
            )
            await client.close()
            raise
        except BaseException:
            await client.close()
            raise
        ir.async_delete_issue(self.hass, DOMAIN, f"unsupported_protocol_{entry.entry_id}")
        return client

    async def async_start(self) -> None:
        """First connection and first snapshot, then the listening task.

        Raises toward setup so a dead panel becomes ConfigEntryNotReady there.
        The connection is owned here until the listening task exists; anything
        that ends this method before then - an error, or a cancellation while
        the first snapshot is still on its way - closes it rather than leaving
        the panel's single connection slot occupied (HA07).
        """
        client = await self._async_connect()
        self._client = client
        started = False
        try:
            state = await self._async_resync(client)
            self.async_set_updated_data(state)
            await self._async_after_connect()
            self.config_entry.async_on_unload(
                self.config_entry.add_update_listener(self._async_entry_updated)
            )
            self._task = self.config_entry.async_create_background_task(
                self.hass, self._async_run(state), name=f"{DOMAIN} link"
            )
            started = True
        finally:
            if not started:
                await self._async_end_session()

    async def async_shutdown(self) -> None:
        """Unload: stop the task, wait for it, and let go of everything."""
        task, self._task = self._task, None
        calls = list(self._calls.values())  # before the task's own cleanup clears them
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._async_end_session()
        pending = [call for call in calls if not call.done()]
        if pending:
            # they were cancelled above; give them a bounded moment to unwind
            await asyncio.wait(pending, timeout=_CALL_STOP_S)
        await super().async_shutdown()

    async def _async_end_session(self) -> None:
        """Let go of everything one connection owned.

        Run on every link failure and at unload, so nothing outlives its
        generation: the subscription stops, the device actions are cancelled,
        the commands waiting for an answer are told, and the socket is closed.
        The next connection therefore starts from an empty state (HA05, HA12).
        """
        self._async_drop_watch()
        self._answered.clear()
        for call in self._calls.values():
            call.cancel()
        self._calls.clear()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CannotConnect("the link dropped"))
        self._pending.clear()
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    async def _async_link_failed(self, err: LinkError) -> None:
        """A send that gave up means this connection is finished (HA11).

        Closing it here is what turns a wedged link into a reconnect: the
        listening loop's next recv fails and the loop below takes over, rather
        than carrying on over a link that can no longer be answered.
        """
        _LOGGER.debug("%s: the link failed while sending: %s", self.name, err)
        client = self._client
        if client is not None:
            await client.close()

    # ---- the listening loop -------------------------------------------------------

    async def _async_run(self, state: PanelState) -> None:
        """Follow the panel, reconnecting for as long as the entry is loaded."""
        try:
            await self._async_link(state)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - a dead receiver must never look healthy
            # Not a link error: a bug here would otherwise leave the entities
            # available on state nothing is maintaining any more (HA05).
            _LOGGER.exception("%s: the panel link stopped unexpectedly", self.name)
            self.async_set_update_error(UpdateFailed(f"the panel link stopped: {err}"))
        finally:
            await self._async_end_session()

    async def _async_link(self, state: PanelState | None) -> None:
        backoff = _BACKOFF_MIN_S
        while True:
            try:
                client = self._client
                if client is None:
                    client = self._client = await self._async_connect()
                    state = None  # a new connection, a new generation
                if state is None:
                    # Nothing is published until this connection has sent a
                    # snapshot of its own (HA12); the watch list the panel
                    # sends first is handled on the way (HA01).
                    state = await self._async_resync(client)
                    self.async_set_updated_data(state)
                    await self._async_after_connect()
                    backoff = _BACKOFF_MIN_S
                await self._async_listen(client, state)
            except asyncio.CancelledError:
                raise
            except WrongPanel as err:
                _LOGGER.error("%s: %s", self.name, err)
                self.async_set_update_error(UpdateFailed(str(err)))
                await self._async_end_session()
                # a factory-reset panel: the person must pair again (HAI-004)
                self.config_entry.async_start_reauth(self.hass)
                return
            except (CannotConnect, InvalidMessage, UnsupportedVersion, TimeoutError) as err:
                self.async_set_update_error(UpdateFailed(str(err)))
                await self._async_end_session()
                state = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)

    async def _async_resync(self, client: PanelClient) -> PanelState:
        """Read until this connection has given a snapshot of its own.

        Bounded by a wall-clock deadline that nothing on the wire extends, so
        a peer that answers pings for ever but never resynchronises is a
        failed connection rather than a silent one (HA12). Everything else the
        panel sends first is dispatched as usual - the firmware sends its
        watch list before the snapshot, and dropping it would leave every
        sensor bound to a zone silent until the options changed (HA01).
        """
        state = PanelState()
        self._watch_seen = False
        try:
            async with asyncio.timeout(_FIRST_SNAPSHOT_S):
                while not state.ready:
                    msg = await client.recv(timeout=_FIRST_SNAPSHOT_S)
                    await self._async_handle(state, msg)
        except TimeoutError:
            raise CannotConnect("the panel never sent its snapshot") from None
        return state

    async def _async_listen(self, client: PanelClient, state: PanelState) -> None:
        quiet = 0
        while True:
            try:
                msg = await client.recv(timeout=_QUIET_PING_S)
            except TimeoutError:
                quiet += 1
                if quiet >= _QUIET_LIMIT:
                    raise CannotConnect("the panel went quiet") from None
                await client.send({"t": "ping"}, timeout=_QUIET_PING_S)
                continue
            quiet = 0
            if await self._async_handle(state, msg):
                self.async_set_updated_data(state)

    async def _async_handle(self, state: PanelState, msg: dict[str, Any]) -> bool:
        """One message from the panel. True when it moved the panel's state.

        The same dispatcher serves the resynchronisation and the connection
        afterwards, so message types cannot be silently dropped depending on
        when they arrive (HA01).
        """
        t = msg.get("t")
        if t in ("snapshot", "delta"):
            state.apply(msg)
            return True
        if t == "status":
            state.apply_status(msg)
            return state.ready
        if t == "event":
            # Which panel this came from is this coordinator's own entry, never
            # a field off the wire: one panel's alarm must not reach another
            # panel's event entity or its automations (HA04). The bus event
            # itself stays global so "any panel" automations keep working.
            self.hass.bus.async_fire(
                f"{DOMAIN}_event", {**msg, "entry_id": self.config_entry.entry_id}
            )
        elif t == "result":
            fut = self._pending.pop(str(msg.get("id") or ""), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif t == "watch":
            self._watch_seen = True
            await self._async_watch(msg.get("ids"))
        elif t == "call":
            self._async_start_call(msg)
        return False

    # ---- what the panel may see (HAI-009) -------------------------------------------

    def _option_list(self, key: str) -> list[str]:
        """One of the two options lists, in the order chosen."""
        chosen = self.config_entry.options.get(key) or []
        registry = er.async_get(self.hass)
        return [
            entity_id
            for entity_id in chosen
            if isinstance(entity_id, str)
            and ((entity := registry.async_get(entity_id)) is None or entity.platform != DOMAIN)
        ]

    @property
    def _allowed(self) -> list[str]:
        """The entities the options share with the panel as zones."""
        return self._option_list(CONF_SHARE_ENTITIES)

    @property
    def _controllable(self) -> list[str]:
        """The entities the options let the panel act on (Features/24)."""
        return self._option_list(CONF_CONTROL_ENTITIES)

    @property
    def _catalogued(self) -> list[str]:
        """Everything the catalog carries: shared first, then control-only ones."""
        catalogued = list(self._allowed)
        catalogued += [e for e in self._controllable if e not in catalogued]
        return catalogued

    @property
    def _subscribed(self) -> list[str]:
        """Everything this link follows in Home Assistant.

        The sensors the panel watches for its zones, plus every entity it may
        act on - a light or a lock the panel can operate has to show what it
        is doing, without anyone having to share it as an alarm sensor as
        well (HA02). An entity in both lists appears once, so it gets one
        subscription and one stream of updates.
        """
        following = list(self._watched)
        following += [e for e in self._controllable if e not in following]
        return following

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

    def _catalog_entity(
        self, entity_id: str, *, zones: bool = True, control: bool = False
    ) -> dict[str, Any]:
        """One catalog row, with the keys protocol.md names for it.

        "zones" and "control" come last: the panel's parser only needs page and
        more before the entities, and older panels ignore what they don't know.
        """
        state = self.hass.states.get(entity_id)
        known = state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        return {
            "id": entity_id,
            "name": state.name if state is not None else entity_id,
            "area": self._area_name(entity_id),
            "domain": entity_id.partition(".")[0],
            "class": str(state.attributes.get("device_class") or "") if state is not None else "",
            "state": state.state if known and state is not None else "",
            "zones": zones,
            "control": control,
        }

    async def _async_send_catalog(self) -> None:
        """Send the allowed entities as pages; page 0 replaces what the panel had."""
        client = self._client
        if client is None:
            return
        catalogued = self._catalogued
        shared = set(self._allowed)
        controllable = set(self._controllable)
        pages = [
            catalogued[i : i + _CATALOG_PAGE] for i in range(0, len(catalogued), _CATALOG_PAGE)
        ]
        for page, entity_ids in enumerate(pages or [[]]):
            try:
                # "page" and "more" before "entities": the panel's parser reads
                # them in this order (protocol.md), and json.dumps keeps it.
                await client.send(
                    {
                        "t": "catalog",
                        "page": page,
                        "more": page < len(pages) - 1,
                        "entities": [
                            self._catalog_entity(e, zones=e in shared, control=e in controllable)
                            for e in entity_ids
                        ],
                    }
                )
            except CannotConnect as err:
                await self._async_link_failed(err)
                return
            except LinkError as err:
                _LOGGER.debug("%s: the catalog could not be sent: %s", self.name, err)
                return

    async def _async_after_connect(self) -> None:
        """Everything a fresh connection is owed, once its snapshot is in.

        The panel supervises a source's whole link: when it comes back, every
        sensor bound to it has to report again within its grace (120 s for
        Home Assistant) or its zone goes to CHECK (Features/22, ZSRC-015). So
        the current state of every followed entity goes out on *every*
        connection, not only the first - including a reconnection where the
        panel's watch list has not changed and it did not repeat it.
        """
        await self._async_send_catalog()
        if not self._watch_seen and self._panel_watch:
            await self._async_watch(list(self._panel_watch))
        await self._async_push_control()

    async def _async_watch(self, ids: Any) -> None:
        """The panel's watch list: the allowed part of it, the rest ignored."""
        allowed = set(self._allowed)
        asked = [
            entity_id
            for entity_id in (ids if isinstance(ids, list) else [])
            if isinstance(entity_id, str)
        ]
        self._panel_watch = asked  # kept whole: options may allow more of it later
        self._watched = [entity_id for entity_id in asked if entity_id in allowed]
        self._async_resubscribe()
        await self._async_push_states(self._watched)

    async def _async_push_control(self) -> None:
        """The current state of every controllable entity the watch list does
        not already cover - one subscription, one update stream (HA02)."""
        watched = set(self._watched)
        await self._async_push_states([e for e in self._controllable if e not in watched])

    async def _async_push_states(self, entity_ids: Iterable[str]) -> None:
        """Tell the panel where each of these entities stands right now."""
        for entity_id in entity_ids:
            await self._async_send_state(entity_id)

    @callback
    def _async_resubscribe(self) -> None:
        """One state-change subscription covering everything this link follows.

        Rebuilt whenever the watch list or the options change, so an entity
        the options no longer allow stops being followed at once.
        """
        if self._unwatch is not None:
            self._unwatch()
            self._unwatch = None
        following = self._subscribed
        stale = set(self._outbox) - set(following)
        for entity_id in stale:
            del self._outbox[entity_id]
        if following:
            self._unwatch = async_track_state_change_event(
                self.hass, following, self._async_state_changed
            )

    @callback
    def _async_drop_watch(self) -> None:
        """Stop following anything (the link is gone)."""
        if self._unwatch is not None:
            self._unwatch()
            self._unwatch = None
        self._watched = []
        self._outbox.clear()

    async def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """A followed entity moved: the panel hears about it at once (HAI-006).

        While one send is in flight the others wait here as one entry per
        entity, so a burst cannot grow into a queue of encoded payloads
        (HA11). Nothing is lost to a cap: every entity that changed still has
        its current state sent, and the send below reads that state when it
        gets there, so what goes out is the newest rather than a backlog of
        superseded ones.
        """
        self._outbox[event.data["entity_id"]] = None
        if self._draining:
            return
        self._draining = True
        try:
            while self._outbox:
                entity_id = next(iter(self._outbox))  # oldest first
                del self._outbox[entity_id]
                await self._async_send_state(entity_id)
        finally:
            self._draining = False

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
        except CannotConnect as err:
            # Not one unlucky message: the link is finished (HA11). Close it so
            # the state goes out again on the connection that replaces it.
            await self._async_link_failed(err)
        except LinkError as err:
            _LOGGER.debug("%s: %s could not be sent: %s", self.name, entity_id, err)

    async def _async_entry_updated(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """The options changed: a fresh catalog, and the panel's own watch list is
        filtered again - what is no longer allowed stops, what is newly allowed
        starts without waiting for a reconnect."""
        await self._async_send_catalog()
        await self._async_watch(list(self._panel_watch))
        await self._async_push_control()

    # ---- what the panel may do: its Devices screen (Features/24) ---------------------

    @callback
    def _async_start_call(self, msg: dict[str, Any]) -> None:
        """Take one `call` from the panel and run it beside the receive loop.

        The alarm's own traffic must never wait behind a device action (HA03),
        so the service call gets its own task and its own deadline. Each
        accepted id is answered exactly once: an id already running is left to
        the task running it, and an id already answered is given the same
        answer again rather than acted on twice - repeating a lock or a cover
        whose outcome is uncertain is worse than repeating the answer.
        """
        call_id = msg.get("id")
        if call_id is None or call_id == "":
            # Nothing to answer to: a result without an id means nothing there.
            return
        key = str(call_id)
        if key in self._calls:
            return  # still running; its own task sends the one answer
        if key in self._answered:
            self._async_answer_later(call_id, self._answered[key])
            return
        if len(self._calls) >= _CALLS_MAX:
            _LOGGER.debug("%s: %s device actions already running", self.name, _CALLS_MAX)
            self._remember(key, "failed")
            self._async_answer_later(call_id, "failed")
            return
        task = self.config_entry.async_create_background_task(
            self.hass, self._async_call(key, msg), name=f"{DOMAIN} call {key}"
        )
        self._calls[key] = task
        task.add_done_callback(partial(self._async_call_done, key))

    @callback
    def _async_call_done(self, key: str, task: asyncio.Task) -> None:
        """One device action finished: stop tracking exactly that task."""
        if self._calls.get(key) is task:
            del self._calls[key]

    @callback
    def _async_answer_later(self, call_id: Any, error: str) -> None:
        """Answer a call without making the receive loop wait for the send."""
        self.config_entry.async_create_background_task(
            self.hass, self._async_call_result(call_id, error), name=f"{DOMAIN} call answer"
        )

    def _remember(self, key: str, error: str) -> None:
        """The answer an id was given, for as long as this connection lasts."""
        self._answered[key] = error
        while len(self._answered) > _ANSWERED_MAX:
            self._answered.pop(next(iter(self._answered)))

    async def _async_call(self, key: str, msg: dict[str, Any]) -> None:
        """Run one device action and answer it exactly once."""
        try:
            error = await self._async_act(msg)
        except asyncio.CancelledError:
            # Unload, or the link went: no answer can reach the panel anyway,
            # and it fails the action itself after its own 30 s (HCTL-012).
            raise
        except Exception:  # noqa: BLE001 - the panel gets one word, whatever broke
            _LOGGER.exception("%s: a device action from the panel failed", self.name)
            error = "failed"
        self._remember(key, error)
        await self._async_call_result(msg.get("id"), error)

    async def _async_act(self, msg: dict[str, Any]) -> str:
        """Carry out one `call`; the empty string means it worked.

        The entity must be in the control options and the action must be one
        Features/24 lists for that entity's domain; anything else is answered
        not_allowed rather than guessed at. Both are checked here, when the
        action actually runs, so an entity the options stopped allowing while
        this was queued is refused rather than acted on. The user code an
        unlock or an open needs is checked on the panel (HCTL-005) and never
        crosses this link.
        """
        entity_id = msg.get("entity")
        action = msg.get("action")
        if not isinstance(entity_id, str) or not isinstance(action, str):
            return "not_allowed"
        domain = entity_id.partition(".")[0]
        wanted = _CALLS.get(domain, {}).get(action)
        if entity_id not in self._controllable or wanted is None:
            return "not_allowed"
        data = _call_data(wanted, msg)
        if data is None:
            return "not_allowed"
        state = self.hass.states.get(entity_id)
        if state is None or state.state == STATE_UNAVAILABLE:
            return "unavailable"
        try:
            async with asyncio.timeout(_CALL_TIMEOUT_S):
                await self.hass.services.async_call(
                    domain, wanted.service, {"entity_id": entity_id, **data}, blocking=True
                )
        except TimeoutError:
            _LOGGER.debug("%s: %s %s did not finish in time", self.name, entity_id, action)
            return "timeout"
        except Exception as err:  # noqa: BLE001 - the panel hears one word
            _LOGGER.debug("%s: %s %s failed: %s", self.name, entity_id, action, err)
            return "failed"
        return ""

    async def _async_call_result(self, call_id: Any, error: str) -> None:
        """The one answer a call gets; the id comes back as it was sent."""
        client = self._client
        if client is None:
            return
        try:
            await client.send({"t": "call_result", "id": call_id, "ok": not error, "error": error})
        except CannotConnect as err:
            await self._async_link_failed(err)
        except LinkError as err:
            _LOGGER.debug("%s: the call result could not be sent: %s", self.name, err)

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
            # The send is inside the deadline, not before it: a panel that
            # stopped reading must not hold a command open past it (HA11).
            async with asyncio.timeout(_COMMAND_TIMEOUT_S):
                await client.send(
                    {"t": "command", "id": cmd_id, "action": action, **fields},
                    timeout=_COMMAND_TIMEOUT_S,
                )
                result = await fut
        except CannotConnect as err:
            await self._async_link_failed(err)
            raise HomeAssistantError("The panel did not answer the command") from err
        except (TimeoutError, LinkError) as err:
            raise HomeAssistantError("The panel did not answer the command") from err
        finally:
            self._pending.pop(cmd_id, None)
        verdict = str(result.get("result") or "")
        if verdict != "ok":
            raise HomeAssistantError(f"The panel refused it: {verdict.replace('_', ' ')}")
        return result
