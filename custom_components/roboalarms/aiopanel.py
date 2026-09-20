"""Protocol client for the RoboAlarms Panel local link, version 1.

The protocol is specified in the panel firmware repository (Features/23,
protocol.md); the panel's C implementation and this file share the same
golden vectors in their test suites so the two cannot drift.

Self-contained on purpose: this module is the seed of the planned
``aioroboalarms`` PyPI package (so the integration can move toward Home
Assistant core later) and must not import anything from Home Assistant.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from ssl import SSLContext
from typing import Any

API_VERSION = 1
DEFAULT_PORT = 6054
FRAME_MAX = 65536
CLIENT_INFO = "aioroboalarms 0.1.0"

_PAIR_CONTEXT = b"roboalarms-pair-v1"
_CONNECT_TIMEOUT = 10.0


class LinkError(Exception):
    """Base error for the panel link."""


class CannotConnect(LinkError):
    """The panel could not be reached."""


class InvalidMessage(LinkError):
    """The peer sent something that is not a valid link message."""


class UnsupportedVersion(LinkError):
    """The panel speaks a protocol version this client does not."""


@dataclass
class PanelInfo:
    """What the panel's hello reports."""

    panel_id: str
    name: str
    model: str
    fw: str
    api: int
    paired: bool


def pair_commit(nonce: bytes) -> str:
    """The commitment sent in pair_start: hex SHA-256 of the client's nonce."""
    return hashlib.sha256(nonce).hexdigest()


def pair_code(nonce_ha: bytes, nonce_panel: bytes, fp_ha: bytes, fp_panel: bytes) -> str:
    """The 6-digit code both screens show (protocol.md, "Code derivation")."""
    digest = hashlib.sha256(_PAIR_CONTEXT + nonce_ha + nonce_panel + fp_ha + fp_panel).digest()
    return str(int.from_bytes(digest[:4], "big") % 1_000_000).zfill(6)


def encode_frame(message: dict[str, Any]) -> bytes:
    """One frame: a 4-byte big-endian length, then the JSON payload."""
    payload = json.dumps(message, separators=(",", ":")).encode()
    if not 1 <= len(payload) <= FRAME_MAX:
        raise InvalidMessage(f"frame payload of {len(payload)} bytes")
    return len(payload).to_bytes(4, "big") + payload


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read one frame; anything that is not a JSON object is InvalidMessage."""
    try:
        header = await reader.readexactly(4)
        length = int.from_bytes(header, "big")
        if not 1 <= length <= FRAME_MAX:
            raise InvalidMessage(f"frame length {length}")
        payload = await reader.readexactly(length)
    except (asyncio.IncompleteReadError, ConnectionError) as err:
        raise CannotConnect("connection closed") from err
    try:
        message = json.loads(payload)
    except ValueError as err:
        raise InvalidMessage("frame is not JSON") from err
    if not isinstance(message, dict) or not isinstance(message.get("t"), str):
        raise InvalidMessage("frame is not a message object")
    return message


class PanelClient:
    """One connection to a panel: connect, hello, then typed messages."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, ssl: SSLContext | None = None) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.info: PanelInfo | None = None

    async def connect(self, timeout: float = _CONNECT_TIMEOUT) -> PanelInfo:
        """Open the connection, exchange hellos, return the panel's identity."""
        try:
            async with asyncio.timeout(timeout):
                self._reader, self._writer = await asyncio.open_connection(
                    self._host, self._port, ssl=self._ssl
                )
                await self.send({"t": "hello", "api": API_VERSION, "client": CLIENT_INFO})
                hello = await self.recv()
        except (OSError, TimeoutError) as err:
            raise CannotConnect(f"cannot connect to {self._host}:{self._port}") from err
        if hello.get("t") != "hello":
            raise InvalidMessage(f"expected the panel's hello, got {hello.get('t')!r}")
        api = hello.get("api")
        if not isinstance(api, int) or api < 1:
            raise InvalidMessage("the panel's hello has no usable api version")
        if api != API_VERSION:
            raise UnsupportedVersion(f"panel speaks api {api}, this client api {API_VERSION}")
        panel_id = hello.get("id")
        if not isinstance(panel_id, str) or not panel_id:
            raise InvalidMessage("the panel's hello has no panel id")
        self.info = PanelInfo(
            panel_id=panel_id.lower(),
            name=str(hello.get("name") or ""),
            model=str(hello.get("model") or ""),
            fw=str(hello.get("fw") or ""),
            api=api,
            paired=bool(hello.get("paired", False)),
        )
        return self.info

    async def send(self, message: dict[str, Any]) -> None:
        """Send one message."""
        if self._writer is None:
            raise CannotConnect("not connected")
        try:
            self._writer.write(encode_frame(message))
            await self._writer.drain()
        except (OSError, ConnectionError) as err:
            raise CannotConnect("connection lost") from err

    async def recv(self, timeout: float = _CONNECT_TIMEOUT) -> dict[str, Any]:
        """Receive one message."""
        if self._reader is None:
            raise CannotConnect("not connected")
        try:
            async with asyncio.timeout(timeout):
                return await read_frame(self._reader)
        except TimeoutError as err:
            raise CannotConnect("the panel stopped answering") from err

    async def close(self) -> None:
        """Close the connection; safe to call twice or before connecting."""
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass
