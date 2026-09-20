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
import secrets
import ssl as ssl_mod
import tempfile
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
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


class PairingFailed(LinkError):
    """The panel answered a pairing attempt with a failure result."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"pairing failed: {reason}")
        self.reason = reason


@dataclass
class PanelInfo:
    """What the panel's hello reports."""

    panel_id: str
    name: str
    model: str
    fw: str
    api: int
    paired: bool


@dataclass
class PartitionState:
    """One partition, as the panel's partition-status object reports it."""

    partition: int
    name: str
    state: str
    ha_state: str  # disarmed/arming/armed_home/armed_away/armed_night/pending/triggered
    ready: bool
    chime: bool
    triggered: bool
    raw: dict[str, Any]

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> PartitionState:
        return cls(
            partition=int(d["partition"]),
            name=str(d.get("name") or ""),
            state=str(d.get("state") or ""),
            ha_state=str(d.get("ha_state") or "disarmed"),
            ready=bool(d.get("ready", False)),
            chime=bool(d.get("chime", False)),
            triggered=bool(d.get("triggered", False)),
            raw=d,
        )


@dataclass
class ZoneState:
    """One zone, as the panel's zone-state object reports it."""

    zone: int
    name: str
    type: str
    device_class: str
    partition: int
    open: bool
    bypassed: bool
    alarm: bool
    trouble: bool
    tamper: bool
    low_battery: bool
    supervision: bool
    raw: dict[str, Any]

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ZoneState:
        return cls(
            zone=int(d["zone"]),
            name=str(d.get("name") or ""),
            type=str(d.get("type") or ""),
            device_class=str(d.get("device_class") or ""),
            partition=int(d.get("partition") or 1),
            open=bool(d.get("open", False)),
            bypassed=bool(d.get("bypassed", False)),
            alarm=bool(d.get("alarm", False)),
            trouble=bool(d.get("trouble", False)),
            tamper=bool(d.get("tamper", False)),
            low_battery=bool(d.get("low_battery", False)),
            supervision=bool(d.get("supervision", False)),
            raw=d,
        )


@dataclass
class PanelState:
    """Everything the panel has pushed so far (snapshot, then deltas)."""

    seq: int = 0
    partitions: dict[int, PartitionState] | None = None
    zones: dict[int, ZoneState] | None = None
    troubles: dict[str, Any] | None = None

    def apply(self, msg: dict[str, Any]) -> None:
        """Fold a snapshot or delta into this state.

        A snapshot replaces everything (zones removed on the panel disappear);
        a delta only touches what it carries.
        """
        kind = msg.get("t")
        if kind == "snapshot":
            self.partitions = {}
            self.zones = {}
            self.troubles = None
        elif self.partitions is None or self.zones is None:
            raise InvalidMessage("a delta arrived before any snapshot")
        for d in msg.get("partitions") or []:
            p = PartitionState.from_json(d)
            self.partitions[p.partition] = p
        for d in msg.get("zones") or []:
            z = ZoneState.from_json(d)
            self.zones[z.zone] = z
        if "troubles" in msg:
            self.troubles = msg["troubles"]
        self.seq = int(msg.get("seq") or self.seq)

    @property
    def ready(self) -> bool:
        """A snapshot has arrived: the state means something."""
        return self.partitions is not None


def pair_commit(nonce: bytes) -> str:
    """The commitment sent in pair_start: hex SHA-256 of the client's nonce."""
    return hashlib.sha256(nonce).hexdigest()


def pair_code(nonce_ha: bytes, nonce_panel: bytes, fp_ha: bytes, fp_panel: bytes) -> str:
    """The 6-digit code both screens show (protocol.md, "Code derivation")."""
    digest = hashlib.sha256(_PAIR_CONTEXT + nonce_ha + nonce_panel + fp_ha + fp_panel).digest()
    return str(int.from_bytes(digest[:4], "big") % 1_000_000).zfill(6)


def generate_client_identity() -> tuple[str, str]:
    """A fresh P-256 key and self-signed certificate (PEM): this client's identity.

    Generated once per config entry and kept in it; the panel pins its SHA-256
    fingerprint at pairing, so losing it means pairing again.
    """
    from datetime import datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "aioroboalarms")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365 * 50))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return key_pem, cert_pem


def cert_der_from_pem(cert_pem: str) -> bytes:
    """The certificate's DER bytes (what fingerprints are computed over)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    return x509.load_pem_x509_certificate(cert_pem.encode()).public_bytes(
        serialization.Encoding.DER
    )


def client_ssl_context(key_pem: str, cert_pem: str) -> SSLContext:
    """A TLS context presenting the client identity; the panel is checked by its
    pinned fingerprint after the handshake, not by a certificate authority."""
    ctx = ssl_mod.SSLContext(ssl_mod.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl_mod.CERT_NONE
    with tempfile.TemporaryDirectory() as tmp:
        cert_file = Path(tmp) / "client.pem"
        cert_file.write_text(cert_pem + key_pem)
        ctx.load_cert_chain(cert_file)
    return ctx


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
        self._pair_nonce = b""
        self._pair_fp_panel = b""
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
        """Receive one message. TimeoutError means only that nothing arrived:
        a quiet link is for the caller to judge (keepalives, protocol.md)."""
        if self._reader is None:
            raise CannotConnect("not connected")
        async with asyncio.timeout(timeout):
            return await read_frame(self._reader)

    @property
    def panel_cert_der(self) -> bytes | None:
        """The panel certificate as seen on the wire (None on a plain connection)."""
        if self._writer is None:
            return None
        ssl_object = self._writer.get_extra_info("ssl_object")
        if ssl_object is None:
            return None
        return ssl_object.getpeercert(binary_form=True)

    async def pair_begin(self, client_cert_der: bytes, panel_cert_der: bytes | None = None) -> str:
        """Start pairing on an open connection and return the 6-digit code.

        The commitment goes out before the panel's nonce arrives (protocol.md,
        "Pairing"), so neither side can steer the code. panel_cert_der defaults
        to the certificate of this TLS connection.
        """
        if panel_cert_der is None:
            panel_cert_der = self.panel_cert_der
        if panel_cert_der is None:
            raise InvalidMessage("no panel certificate to derive the pairing code from")
        self._pair_nonce = secrets.token_bytes(32)
        await self.send({"t": "pair_start", "commit": pair_commit(self._pair_nonce)})
        msg = await self.recv(timeout=30)
        if msg.get("t") == "pair_result":
            raise PairingFailed(str(msg.get("result") or "unknown"))
        if msg.get("t") != "pair_nonce":
            raise InvalidMessage(f"expected pair_nonce, got {msg.get('t')!r}")
        nonce_hex = msg.get("nonce")
        if not isinstance(nonce_hex, str):
            raise InvalidMessage("pair_nonce without a nonce")
        try:
            nonce_panel = bytes.fromhex(nonce_hex)
        except ValueError as err:
            raise InvalidMessage("pair_nonce isn't hex") from err
        if len(nonce_panel) != 32:
            raise InvalidMessage("pair_nonce has the wrong length")
        await self.send({"t": "pair_reveal", "nonce": self._pair_nonce.hex()})
        fp_ha = hashlib.sha256(client_cert_der).digest()
        self._pair_fp_panel = hashlib.sha256(panel_cert_der).digest()
        return pair_code(self._pair_nonce, nonce_panel, fp_ha, self._pair_fp_panel)

    async def pair_wait(self, timeout: float = 130.0) -> bytes:
        """Wait for the person at the panel; returns the panel fingerprint to pin."""
        try:
            msg = await self.recv(timeout=timeout)
        except TimeoutError as err:
            raise PairingFailed("timeout") from err
        if msg.get("t") != "pair_result":
            raise InvalidMessage(f"expected pair_result, got {msg.get('t')!r}")
        result = str(msg.get("result") or "unknown")
        if result != "paired":
            raise PairingFailed(result)
        return self._pair_fp_panel

    async def close(self) -> None:
        """Close the connection; safe to call twice or before connecting."""
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass
