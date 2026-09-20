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


class FakePanel:
    """A minimal panel: answers the client's hello, then follows a script."""

    def __init__(self, hello: dict[str, Any] | None = None, raw: bytes | None = None) -> None:
        self.hello = PANEL_HELLO if hello is None else hello
        self.raw = raw  # sent instead of a proper hello frame
        self.client_hello: dict[str, Any] | None = None
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

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = await reader.readexactly(4)
            payload = await reader.readexactly(int.from_bytes(header, "big"))
            self.client_hello = json.loads(payload)
            writer.write(self.raw if self.raw is not None else aiopanel.encode_frame(self.hello))
            await writer.drain()
            await reader.read(1)  # hold the connection until the client leaves
        except (asyncio.IncompleteReadError, ConnectionError):
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
