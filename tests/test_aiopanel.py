"""Tests for the link client (aiopanel.py) against a fake panel.

The pairing golden vectors are shared verbatim with the panel firmware's
host tests (protocol.md, "Golden vectors"): if either side drifts from the
spec, one of the two suites fails.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from custom_components.roboalarms import aiopanel

pytestmark = pytest.mark.usefixtures("socket_enabled")

NONCE_HA = bytes(range(0x00, 0x20))
NONCE_PANEL = bytes(range(0x20, 0x40))
FP_HA = bytes(range(0x40, 0x60))
FP_PANEL = bytes(range(0x60, 0x80))

PANEL_HELLO = {
    "t": "hello",
    "api": 1,
    "id": "0a1b2c",
    "name": "Home",
    "model": "crowpanel-p4",
    "fw": "0.6.0",
    "paired": False,
}


def test_pair_code_vectors() -> None:
    """The golden vectors from protocol.md, same values as test_link.c."""
    assert aiopanel.pair_code(NONCE_HA, NONCE_PANEL, FP_HA, FP_PANEL) == "588460"
    zero = bytes(32)
    assert aiopanel.pair_code(zero, zero, zero, zero) == "064511"
    assert aiopanel.pair_code(NONCE_PANEL, NONCE_HA, FP_HA, FP_PANEL) == "195600"


def test_pair_commit_matches_c_suite() -> None:
    """The commitment for vector 1's nonce, as asserted in test_link.c."""
    assert (
        aiopanel.pair_commit(NONCE_HA)
        == "630dcd2966c4336691125448bbb25b4ff412a49c732db2c8abc1b8581bd710dd"
    )


def test_frame_roundtrip() -> None:
    """A frame is a big-endian length and compact JSON."""
    frame = aiopanel.encode_frame({"t": "ping"})
    assert frame == b"\x00\x00\x00\x0c" + b'{"t":"ping"}'
    with pytest.raises(aiopanel.InvalidMessage):
        aiopanel.encode_frame({"t": "x" * aiopanel.FRAME_MAX})


CLIENT_DER = b"a pretend client certificate"
PANEL_DER = b"a pretend panel certificate"


class FakePanel:
    """A minimal panel: answers the client's hello, then follows a script.

    With pair="paired" (or another pair_result) it speaks the pairing exchange
    like the firmware's proto_pair: nonce out, commitment checked, its own code
    computed the same way, then the scripted result.
    """

    def __init__(
        self,
        hello: dict[str, Any] | None = None,
        raw: bytes | None = None,
        pair: str | None = None,
    ) -> None:
        self.hello = PANEL_HELLO if hello is None else hello
        self.raw = raw  # sent instead of a proper hello frame
        self.pair = pair  # the pair_result to send after a full exchange
        self.client_hello: dict[str, Any] | None = None
        self.code: str | None = None  # what the panel's screen would show
        self.commit_ok: bool | None = None
        self.server: asyncio.Server | None = None

    async def __aenter__(self) -> FakePanel:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    @property
    def port(self) -> int:
        assert self.server is not None
        return int(self.server.sockets[0].getsockname()[1])

    async def _read(self, reader: asyncio.StreamReader) -> dict[str, Any]:
        header = await reader.readexactly(4)
        payload = await reader.readexactly(int.from_bytes(header, "big"))
        return json.loads(payload)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            self.client_hello = await self._read(reader)
            writer.write(self.raw if self.raw is not None else aiopanel.encode_frame(self.hello))
            await writer.drain()
            if self.pair is not None:
                start = await self._read(reader)
                if self.pair == "not_pairing":
                    writer.write(
                        aiopanel.encode_frame({"t": "pair_result", "result": "not_pairing"})
                    )
                    await writer.drain()
                    return
                writer.write(aiopanel.encode_frame({"t": "pair_nonce", "nonce": NONCE_PANEL.hex()}))
                await writer.drain()
                reveal = await self._read(reader)
                nonce_ha = bytes.fromhex(reveal["nonce"])
                self.commit_ok = aiopanel.pair_commit(nonce_ha) == start["commit"]
                self.code = aiopanel.pair_code(
                    nonce_ha,
                    NONCE_PANEL,
                    aiopanel.hashlib.sha256(CLIENT_DER).digest(),
                    aiopanel.hashlib.sha256(PANEL_DER).digest(),
                )
                writer.write(aiopanel.encode_frame({"t": "pair_result", "result": self.pair}))
                await writer.drain()
            await reader.read(1)  # hold the connection until the client leaves
        except (asyncio.IncompleteReadError, ConnectionError, KeyError, ValueError):
            pass
        finally:
            writer.close()


async def test_connect_and_hello() -> None:
    """The client introduces itself and reads the panel's identity."""
    async with FakePanel() as panel:
        client = aiopanel.PanelClient("127.0.0.1", panel.port)
        try:
            info = await client.connect()
        finally:
            await client.close()
    assert panel.client_hello == {
        "t": "hello",
        "api": aiopanel.API_VERSION,
        "client": aiopanel.CLIENT_INFO,
    }
    assert info == aiopanel.PanelInfo(
        panel_id="0a1b2c", name="Home", model="crowpanel-p4", fw="0.6.0", api=1, paired=False
    )


async def test_connect_refused() -> None:
    """A closed port is CannotConnect."""
    async with FakePanel() as panel:
        port = panel.port
    client = aiopanel.PanelClient("127.0.0.1", port)
    with pytest.raises(aiopanel.CannotConnect):
        await client.connect()
    await client.close()


async def test_unsupported_api() -> None:
    """A panel speaking another api version is refused with its own error."""
    async with FakePanel(hello={**PANEL_HELLO, "api": 2}) as panel:
        client = aiopanel.PanelClient("127.0.0.1", panel.port)
        with pytest.raises(aiopanel.UnsupportedVersion):
            await client.connect()
        await client.close()


@pytest.mark.parametrize(
    "hello",
    [
        {**PANEL_HELLO, "api": "one"},
        {k: v for k, v in PANEL_HELLO.items() if k != "api"},
        {k: v for k, v in PANEL_HELLO.items() if k != "id"},
        {"t": "pong"},
    ],
)
async def test_broken_hello(hello: dict[str, Any]) -> None:
    """A hello without a usable api or id, or the wrong message, is invalid."""
    async with FakePanel(hello=hello) as panel:
        client = aiopanel.PanelClient("127.0.0.1", panel.port)
        with pytest.raises(aiopanel.InvalidMessage):
            await client.connect()
        await client.close()


@pytest.mark.parametrize(
    "raw",
    [
        b"\x00\x00\x00\x09not JSON!",
        b"\x00\x00\x00\x044444",  # a JSON number, not an object
        (aiopanel.FRAME_MAX + 1).to_bytes(4, "big"),  # oversized frame announced
        b"\x00\x00\x00\x00",  # empty frame
    ],
)
async def test_broken_frames(raw: bytes) -> None:
    """Garbage instead of a hello frame is invalid, never a crash."""
    async with FakePanel(raw=raw) as panel:
        client = aiopanel.PanelClient("127.0.0.1", panel.port)
        with pytest.raises(aiopanel.InvalidMessage):
            await client.connect()
        await client.close()


async def test_connection_lost_midframe() -> None:
    """A panel that dies mid-frame is CannotConnect."""
    async with FakePanel(raw=b'\x00\x00\x00\x20{"t":') as panel:
        client = aiopanel.PanelClient("127.0.0.1", panel.port)
        with pytest.raises(aiopanel.CannotConnect):
            await client.connect()
        await client.close()


async def test_pairing_happy_path() -> None:
    """Both sides derive the same code and the pinned fingerprint comes back."""
    async with FakePanel(pair="paired") as panel:
        client = aiopanel.PanelClient("127.0.0.1", panel.port)
        try:
            await client.connect()
            code = await client.pair_begin(CLIENT_DER, panel_cert_der=PANEL_DER)
            fp = await client.pair_wait()
        finally:
            await client.close()
    assert panel.commit_ok is True  # the commitment went out before the panel's nonce
    assert panel.code == code  # what the panel's screen shows matches ours (HAI-003)
    assert fp == aiopanel.hashlib.sha256(PANEL_DER).digest()


async def test_pairing_rejected() -> None:
    """The person at the panel says no: PairingFailed with the panel's reason."""
    async with FakePanel(pair="rejected") as panel:
        client = aiopanel.PanelClient("127.0.0.1", panel.port)
        try:
            await client.connect()
            await client.pair_begin(CLIENT_DER, panel_cert_der=PANEL_DER)
            with pytest.raises(aiopanel.PairingFailed) as err:
                await client.pair_wait()
            assert err.value.reason == "rejected"
        finally:
            await client.close()


async def test_pairing_window_closed() -> None:
    """pair_start against a closed window fails right away with the reason."""
    async with FakePanel(pair="not_pairing") as panel:
        client = aiopanel.PanelClient("127.0.0.1", panel.port)
        try:
            await client.connect()
            with pytest.raises(aiopanel.PairingFailed) as err:
                await client.pair_begin(CLIENT_DER, panel_cert_der=PANEL_DER)
            assert err.value.reason == "not_pairing"
        finally:
            await client.close()


def test_client_identity_roundtrip() -> None:
    """The generated identity loads as a certificate and yields stable DER."""
    key_pem, cert_pem = aiopanel.generate_client_identity()
    assert "PRIVATE KEY" in key_pem
    der = aiopanel.cert_der_from_pem(cert_pem)
    assert der == aiopanel.cert_der_from_pem(cert_pem)
    ctx = aiopanel.client_ssl_context(key_pem, cert_pem)
    assert ctx.check_hostname is False


# ---- framing across timeouts (HA10) ------------------------------------------------


def _reading(stream: asyncio.StreamReader) -> aiopanel.PanelClient:
    """A client that receives from this stream, without a socket under it."""
    client = aiopanel.PanelClient("unused")
    client._reader = stream
    return client


# What the firmware actually writes: proto_partition_status and proto_zone_state
# (alarm_proto/src/messages.c). Fuller than the client reads, with the nulls
# pj_kstr writes where the panel has no value, so the tests below prove the
# checks do not refuse a real panel.
PANEL_PARTITION = {
    "partition": 1,
    "name": "Home",
    "state": "disarmed",
    "ha_state": "disarmed",
    "level": None,
    "no_entry_delay": False,
    "silent_exit": False,
    "ready": True,
    "ready_levels": ["away", "stay"],
    "delay_remaining_s": 0,
    "triggered": False,
    "exit_error": False,
    "chime": False,
    "walk_test": False,
    "installer_mode": False,
    "armed_by": None,
    "memory": [],
    "memory_canceled": False,
    "faults": 0,
    "bypassed": 0,  # a count here, a boolean on a zone: both are read as given
    "alarms": 0,
    "troubles": 0,
    "trouble_beeps": False,
    "sounder": "off",
    "ts": None,
}

PANEL_ZONE = {
    "zone": 1,
    "name": "Front Door",
    "type": "entry_exit_1",
    "device_class": None,  # the panel writes null for "no device class"
    "partition": 1,
    "open": False,
    "bypassed": False,
    "alarm": False,
    "trouble": False,
    "tamper": False,
    "low_battery": False,
    "supervision": False,
    "not_ready": False,
}

PANEL_SNAPSHOT = {
    "t": "snapshot",
    "seq": 7,
    "partitions": [PANEL_PARTITION],
    "zones": [PANEL_ZONE],
    "troubles": {"zones": [], "system": [], "devices": []},
}

# A full panel's snapshot (128 zones) and a small frame behind it: the two must
# come out whole however the bytes are chopped up.
BIG_SNAPSHOT = {
    **PANEL_SNAPSHOT,
    "zones": [{**PANEL_ZONE, "zone": z, "name": f"Zone {z}"} for z in range(1, 129)],
}
STREAM = aiopanel.encode_frame(BIG_SNAPSHOT) + aiopanel.encode_frame({"t": "pong"})
FIRST = len(aiopanel.encode_frame(BIG_SNAPSHOT))


async def test_timeout_before_a_frame_is_silence() -> None:
    """Nothing consumed: TimeoutError, and the next frame reads whole."""
    stream = asyncio.StreamReader()
    client = _reading(stream)
    with pytest.raises(TimeoutError):
        await client.recv(timeout=0.01)
    stream.feed_data(aiopanel.encode_frame({"t": "pong"}))
    assert await client.recv(timeout=0.5) == {"t": "pong"}


async def test_a_timeout_halfway_through_a_frame_keeps_the_message() -> None:
    """The header is not lost when the timeout strikes mid-payload (HA10)."""
    stream = asyncio.StreamReader()
    client = _reading(stream)
    frame = aiopanel.encode_frame({"t": "pong"})
    stream.feed_data(frame[:6])
    asyncio.get_running_loop().call_later(0.05, stream.feed_data, frame[6:])
    assert await client.recv(timeout=0.01) == {"t": "pong"}


async def test_a_frame_that_never_finishes_is_a_dead_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence with a frame half consumed is a torn link, not a quiet one."""
    monkeypatch.setattr(aiopanel, "_FRAME_STALL", 0.05)
    stream = asyncio.StreamReader()
    client = _reading(stream)
    stream.feed_data(aiopanel.encode_frame({"t": "pong"})[:6])
    with pytest.raises(aiopanel.CannotConnect):
        await client.recv(timeout=0.01)


@pytest.mark.parametrize(
    "cut",
    [0, 1, 3, 4, 5, 2000, FIRST - 1, FIRST, FIRST + 1, FIRST + 3, FIRST + 4, FIRST + 8],
)
async def test_a_timeout_at_any_cut_keeps_the_boundary(cut: int) -> None:
    """Fragmentation before, during and after a header, in a big snapshot and
    in the frame behind it: every message arrives once, at its own boundary."""
    stream = asyncio.StreamReader()
    client = _reading(stream)
    stream.feed_data(STREAM[:cut])

    async def feed_the_rest() -> None:
        await asyncio.sleep(0.05)
        stream.feed_data(STREAM[cut:])

    feeder = asyncio.create_task(feed_the_rest())
    got: list[dict[str, Any]] = []
    for _ in range(40):
        if len(got) == 2:
            break
        try:
            got.append(await client.recv(timeout=0.01))
        except TimeoutError:
            pass  # nothing had been consumed: harmless silence
    await feeder
    assert got == [BIG_SNAPSHOT, {"t": "pong"}]
    with pytest.raises(TimeoutError):
        await client.recv(timeout=0.01)  # and nothing was delivered twice


# ---- sending against a peer that stops reading (HA11) ------------------------------


class BlockedWriter:
    """A transport that accepts bytes and never finishes flushing them."""

    def __init__(self) -> None:
        self.queued = 0
        self.release = asyncio.Event()

    def write(self, data: bytes) -> None:
        self.queued += len(data)

    async def drain(self) -> None:
        await self.release.wait()


def _writing(writer: object) -> aiopanel.PanelClient:
    """A client that sends into this writer, without a socket under it."""
    client = aiopanel.PanelClient("unused")
    client._writer = writer  # type: ignore[assignment]
    return client


async def test_a_send_gives_up_on_a_peer_that_stops_reading() -> None:
    """send has a deadline of its own: it cannot hang for ever (HA11)."""
    writer = BlockedWriter()
    client = _writing(writer)
    with pytest.raises(aiopanel.CannotConnect):
        await client.send({"t": "ping"}, timeout=0.05)
    writer.release.set()


async def test_many_sends_against_a_blocked_peer_stay_bounded() -> None:
    """200 state messages at once: all finish, the queue and the bytes handed
    to the stuck transport stay bounded, and nothing is left pending."""
    writer = BlockedWriter()
    client = _writing(writer)
    messages = [
        {"t": "state", "id": f"binary_sensor.z{i}", "state": "on", "avail": True}
        for i in range(200)
    ]
    results = await asyncio.gather(
        *[asyncio.create_task(client.send(m, timeout=0.05)) for m in messages],
        return_exceptions=True,
    )
    assert len(results) == 200
    assert all(isinstance(r, aiopanel.CannotConnect) for r in results)
    refused = [r for r in results if isinstance(r, aiopanel.SendOverflow)]
    assert len(refused) == 200 - aiopanel.SEND_QUEUE_MAX  # the rest never queued
    # Only the one frame that reached the transport before it stalled.
    assert writer.queued == len(aiopanel.encode_frame(messages[0]))
    assert client._waiting == 0
    writer.release.set()


async def test_a_link_that_gave_up_sending_stops_receiving_too() -> None:
    """The listener finds the link dead and reconnects, rather than reading on
    over a connection it can no longer answer (HA11)."""
    writer = BlockedWriter()
    client = _writing(writer)
    stream = asyncio.StreamReader()
    client._reader = stream
    stream.feed_data(aiopanel.encode_frame({"t": "pong"}))
    with pytest.raises(aiopanel.CannotConnect):
        await client.send({"t": "ping"}, timeout=0.05)
    with pytest.raises(aiopanel.CannotConnect):
        await client.recv(timeout=0.5)
    writer.release.set()


async def test_sends_keep_their_order() -> None:
    """One frame at a time, in the order the callers asked for."""

    class CountingWriter:
        def __init__(self) -> None:
            self.frames: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.frames.append(data)

        async def drain(self) -> None:
            await asyncio.sleep(0)

    writer = CountingWriter()
    client = _writing(writer)
    await asyncio.gather(*[client.send({"t": "ping", "n": n}) for n in range(20)])
    assert [json.loads(f[4:])["n"] for f in writer.frames] == list(range(20))


async def test_close_aborts_a_transport_that_will_not_flush() -> None:
    """Shutdown is bounded too: a transport that cannot flush is aborted."""

    class StuckTransport:
        def __init__(self) -> None:
            self.aborted = False

        def abort(self) -> None:
            self.aborted = True

    class StuckWriter:
        def __init__(self) -> None:
            self.transport = StuckTransport()
            self.closed = False

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            await asyncio.Event().wait()

    writer = StuckWriter()
    client = _writing(writer)
    await client.close(timeout=0.02)
    assert writer.closed
    assert writer.transport.aborted


# ---- state messages the protocol does not allow (HA05) -----------------------------


def test_the_panels_own_objects_are_accepted() -> None:
    """The shapes the firmware writes, nulls and all, go in unchanged."""
    state = aiopanel.PanelState()
    state.apply(PANEL_SNAPSHOT)
    assert state.ready
    assert state.seq == 7
    assert state.partitions is not None and state.partitions[1].ha_state == "disarmed"
    assert state.zones is not None
    assert state.zones[1].device_class == ""  # null means "none", not a failure
    assert state.zones[1].partition == 1


BAD_STATE_MESSAGES = [
    {"t": "delta", "zones": [{"name": "no zone number"}]},
    {"t": "delta", "zones": [{"zone": "1"}]},
    {"t": "delta", "zones": [{"zone": True}]},
    {"t": "delta", "zones": [{"zone": 0}]},
    {"t": "delta", "zones": [{"zone": aiopanel.ZONE_MAX + 1}]},
    {"t": "delta", "zones": [{"zone": 1, "open": 1}]},
    {"t": "delta", "zones": [{"zone": 1, "name": 7}]},
    {"t": "delta", "zones": [{"zone": 1, "partition": aiopanel.PARTITION_MAX + 1}]},
    {"t": "delta", "zones": ["not an object"]},
    {"t": "delta", "zones": "not an array"},
    {"t": "delta", "partitions": [{"name": "no partition number"}]},
    {"t": "delta", "partitions": [{"partition": 0}]},
    {"t": "delta", "partitions": [{"partition": 1, "ready": "yes"}]},
    {"t": "delta", "partitions": [{"partition": 1, "ha_state": 3}]},
    {"t": "delta", "partitions": {"partition": 1}},
    {"t": "delta", "seq": "8"},
    {"t": "delta", "seq": -1},
    {"t": "delta", "troubles": []},
]


@pytest.mark.parametrize("msg", BAD_STATE_MESSAGES)
def test_a_malformed_state_message_is_invalid_and_changes_nothing(
    msg: dict[str, Any],
) -> None:
    """Missing, wrongly typed and out-of-range fields are InvalidMessage - not
    a KeyError out of a listener - and the state stays exactly as it was."""
    state = aiopanel.PanelState()
    state.apply(PANEL_SNAPSHOT)
    before = (state.seq, dict(state.zones or {}), dict(state.partitions or {}), state.troubles)
    with pytest.raises(aiopanel.InvalidMessage):
        state.apply(msg)
    assert (state.seq, state.zones, state.partitions, state.troubles) == before


def test_a_rejected_delta_applies_none_of_it() -> None:
    """The good zone in front of the bad one is not folded in either (HA05)."""
    state = aiopanel.PanelState()
    state.apply(PANEL_SNAPSHOT)
    with pytest.raises(aiopanel.InvalidMessage):
        state.apply(
            {
                "t": "delta",
                "seq": 9,
                "zones": [{**PANEL_ZONE, "open": True}, {"name": "no zone number"}],
            }
        )
    assert state.zones is not None and state.zones[1].open is False
    assert state.seq == 7


def test_an_invalid_first_snapshot_leaves_the_state_unready() -> None:
    """Nothing to show and nothing half-built: the caller has to reconnect."""
    state = aiopanel.PanelState()
    with pytest.raises(aiopanel.InvalidMessage):
        state.apply({"t": "snapshot", "seq": 1, "zones": [{"name": "no zone number"}]})
    assert not state.ready
    assert state.zones is None


def test_a_delta_before_a_snapshot_is_invalid() -> None:
    """A delta means nothing without the snapshot it changes."""
    state = aiopanel.PanelState()
    with pytest.raises(aiopanel.InvalidMessage):
        state.apply({"t": "delta", "zones": [PANEL_ZONE]})
    assert not state.ready


def test_zones_cannot_grow_past_the_bound() -> None:
    """Zone numbers are checked against the protocol's range, so a peer cannot
    grow this state message after message - and the refusal is visible."""
    state = aiopanel.PanelState()
    state.apply({**PANEL_SNAPSHOT, "zones": []})
    for zone in range(1, 6):  # accumulation across deltas is by zone number
        state.apply({"t": "delta", "zones": [{**PANEL_ZONE, "zone": zone}]})
    assert state.zones is not None and len(state.zones) == 5
    state.apply(
        {
            "t": "delta",
            "zones": [{**PANEL_ZONE, "zone": z} for z in range(1, aiopanel.ZONE_MAX + 1)],
        }
    )
    assert len(state.zones) == aiopanel.ZONE_MAX
    with pytest.raises(aiopanel.InvalidMessage):
        state.apply({"t": "delta", "zones": [{**PANEL_ZONE, "zone": aiopanel.ZONE_MAX + 1}]})
    assert len(state.zones) == aiopanel.ZONE_MAX
