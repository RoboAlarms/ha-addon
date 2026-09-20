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
