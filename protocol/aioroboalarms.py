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
SEND_QUEUE_MAX = 32  # sends allowed to wait for the transport at the same time

# A panel of api 1 has 4 partitions and 128 zones (the firmware's alarm_types.h).
# These are the numbers this client is willing to hold: room for a larger panel,
# but bounded, so a peer cannot grow a PanelState without limit (HA05).
PARTITION_MAX = 64
ZONE_MAX = 1024

_PAIR_CONTEXT = b"roboalarms-pair-v1"
_CONNECT_TIMEOUT = 10.0
_SEND_TIMEOUT = 10.0  # one send's whole budget, the transport included
_CLOSE_TIMEOUT = 5.0  # a transport that will not flush is aborted after this
_FRAME_STALL = 10.0  # once a frame has started, the rest of it must arrive within this
_SEQ_MAX = (1 << 64) - 1


class LinkError(Exception):
    """Base error for the panel link."""


class CannotConnect(LinkError):
    """The panel could not be reached."""


class SendOverflow(CannotConnect):
    """Too many sends are already waiting: the link is not keeping up.

    A CannotConnect on purpose - the link is unusable until it is reconnected,
    and nothing was written, so the caller knows this message did not go out.
    """


class InvalidMessage(LinkError):
    """The peer sent something that is not a valid link message."""


class UnsupportedVersion(LinkError):
    """The panel speaks a protocol version this client does not."""


class PairingFailed(LinkError):
    """The panel answered a pairing attempt with a failure result."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"pairing failed: {reason}")
        self.reason = reason


def _obj(value: Any, what: str) -> dict[str, Any]:
    """The value as a JSON object, or InvalidMessage."""
    if not isinstance(value, dict):
        raise InvalidMessage(f"{what} is not an object")
    return value


def _array(msg: dict[str, Any], key: str) -> list[Any]:
    """An optional array field; absent or null means an empty one."""
    value = msg.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidMessage(f"{key} is not an array")
    return value


def _text(d: dict[str, Any], key: str, what: str) -> str:
    """An optional string field. The panel writes null where it has no value
    (a zone with no device class, say), so null and absent both mean ""."""
    value = d.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidMessage(f"{what}: {key} is not a string")
    return value


def _flag(d: dict[str, Any], key: str, what: str) -> bool:
    """An optional boolean field; absent or null means false."""
    value = d.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise InvalidMessage(f"{what}: {key} is not a boolean")
    return value


def _number(d: dict[str, Any], key: str, what: str, *, low: int, high: int, default: int) -> int:
    """An optional whole-number field, refused when it is out of range."""
    value = d.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidMessage(f"{what}: {key} is not a whole number")
    if not low <= value <= high:
        raise InvalidMessage(f"{what}: {key} is out of range ({value})")
    return value


def _required_number(d: dict[str, Any], key: str, what: str, *, low: int, high: int) -> int:
    """The 1-based number that identifies a partition or a zone."""
    if d.get(key) is None:
        raise InvalidMessage(f"{what} has no {key}")
    return _number(d, key, what, low=low, high=high, default=low)


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
        """One partition-status object, or InvalidMessage.

        Keys the panel does not know about are ignored (protocol.md), but a key
        this client reads has to carry what the protocol says it does: anything
        else is a protocol error, not something to guess at (HA05).
        """
        d = _obj(d, "a partition")
        partition = _required_number(d, "partition", "a partition", low=1, high=PARTITION_MAX)
        what = f"partition {partition}"
        return cls(
            partition=partition,
            name=_text(d, "name", what),
            state=_text(d, "state", what),
            ha_state=_text(d, "ha_state", what) or "disarmed",
            ready=_flag(d, "ready", what),
            chime=_flag(d, "chime", what),
            triggered=_flag(d, "triggered", what),
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
        """One zone-state object, or InvalidMessage (see PartitionState)."""
        d = _obj(d, "a zone")
        zone = _required_number(d, "zone", "a zone", low=1, high=ZONE_MAX)
        what = f"zone {zone}"
        return cls(
            zone=zone,
            name=_text(d, "name", what),
            type=_text(d, "type", what),
            device_class=_text(d, "device_class", what),
            # 0 means "no partition" on the panel; here it reads as the first.
            partition=_number(d, "partition", what, low=0, high=PARTITION_MAX, default=0) or 1,
            open=_flag(d, "open", what),
            bypassed=_flag(d, "bypassed", what),
            alarm=_flag(d, "alarm", what),
            trouble=_flag(d, "trouble", what),
            tamper=_flag(d, "tamper", what),
            low_battery=_flag(d, "low_battery", what),
            supervision=_flag(d, "supervision", what),
            raw=d,
        )


@dataclass
class PanelState:
    """Everything the panel has pushed so far (snapshot, then deltas)."""

    seq: int = 0
    status: dict[str, Any] | None = None
    partitions: dict[int, PartitionState] | None = None
    zones: dict[int, ZoneState] | None = None
    troubles: dict[str, Any] | None = None

    def apply(self, msg: dict[str, Any]) -> None:
        """Fold a snapshot or delta into this state.

        A snapshot replaces everything (zones removed on the panel disappear);
        a delta only touches what it carries.

        The whole message is read and the new state built before anything here
        changes, so a message the protocol does not allow raises InvalidMessage
        and leaves this state exactly as it was - no half-applied delta (HA05).
        Zone and partition numbers are bounded (ZONE_MAX, PARTITION_MAX), so a
        peer cannot grow this state message after message.
        """
        _obj(msg, "a state message")
        if msg.get("t") == "snapshot":
            partitions: dict[int, PartitionState] = {}
            zones: dict[int, ZoneState] = {}
            troubles: dict[str, Any] | None = None
        elif self.partitions is None or self.zones is None:
            raise InvalidMessage("a delta arrived before any snapshot")
        else:
            partitions = dict(self.partitions)
            zones = dict(self.zones)
            troubles = self.troubles
        for d in _array(msg, "partitions"):
            p = PartitionState.from_json(d)
            partitions[p.partition] = p
        for d in _array(msg, "zones"):
            z = ZoneState.from_json(d)
            zones[z.zone] = z
        if "troubles" in msg:
            reported = msg["troubles"]
            troubles = None if reported is None else _obj(reported, "troubles")
        seq = _number(msg, "seq", "a state message", low=0, high=_SEQ_MAX, default=self.seq)
        # Nothing above touched self: from here the whole message is good.
        self.partitions = partitions
        self.zones = zones
        self.troubles = troubles
        self.seq = seq or self.seq  # seq 0 means "no counter", as it always did

    def apply_status(self, msg: dict[str, Any]) -> None:
        """Validate optional diagnostic status atomically; never invent missing readings."""
        status = {}
        if "uptime_s" in msg:
            status["uptime_s"] = _required_number(msg, "uptime_s", "status", low=0, high=2**53)
        if "rssi" in msg:
            status["rssi"] = (
                None
                if msg["rssi"] is None
                else _required_number(msg, "rssi", "status", low=-127, high=0)
            )
        if "update" in msg:
            update = _obj(msg["update"], "update status")
            status["update"] = {
                "installed": _text(update, "installed", "update status"),
                "latest": _text(update, "latest", "update status"),
                "available": _flag(update, "available", "update status"),
                "in_progress": _flag(update, "in_progress", "update status"),
                "progress": _number(
                    update, "progress", "update status", low=-1, high=100, default=-1
                ),
            }
        self.status = status

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


def _frame_length(header: bytes) -> int:
    """The payload length a frame header announces (1..FRAME_MAX)."""
    length = int.from_bytes(header, "big")
    if not 1 <= length <= FRAME_MAX:
        raise InvalidMessage(f"frame length {length}")
    return length


class FrameReader:
    """Frames off one stream, resumable when a read is cancelled part way.

    Everything consumed lives here rather than in a local variable, so a read
    that a timeout cancels halfway can be picked up where it stopped. Reading
    the header into a local and losing it to a cancelled body read is what
    made the next read take the payload for a length (HA10).
    """

    def __init__(self, stream: asyncio.StreamReader) -> None:
        self.stream = stream
        self._buf = bytearray()
        self._length: int | None = None

    @property
    def in_progress(self) -> bool:
        """Part of a frame is already in: silence now is a torn frame, not a
        quiet link."""
        return self._length is not None or bool(self._buf)

    async def _exactly(self, count: int) -> bytes:
        """`count` bytes, keeping what arrives across a cancellation."""
        while len(self._buf) < count:
            try:
                chunk = await self.stream.read(count - len(self._buf))
            except (asyncio.IncompleteReadError, ConnectionError) as err:
                raise CannotConnect("connection closed") from err
            if not chunk:
                raise CannotConnect("connection closed")
            self._buf += chunk  # no await between here and the read above
        data = bytes(self._buf[:count])
        del self._buf[:count]
        return data

    async def read(self) -> dict[str, Any]:
        """The next frame; anything that is not a JSON object is InvalidMessage."""
        length = self._length
        if length is None:
            length = _frame_length(await self._exactly(4))
            self._length = length
        payload = await self._exactly(length)
        self._length = None
        try:
            message = json.loads(payload)
        except ValueError as err:
            raise InvalidMessage("frame is not JSON") from err
        if not isinstance(message, dict) or not isinstance(message.get("t"), str):
            raise InvalidMessage("frame is not a message object")
        return message


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read one frame. Use a FrameReader to survive a cancelled read."""
    return await FrameReader(reader).read()


class PanelClient:
    """One connection to a panel: connect, hello, then typed messages."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, ssl: SSLContext | None = None) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._frames: FrameReader | None = None
        self._send_lock = asyncio.Lock()
        self._waiting = 0  # sends queued behind the lock right now
        self._broken = False  # a send gave up: this connection is finished
        self._pair_nonce = b""
        self._pair_fp_panel = b""
        self.info: PanelInfo | None = None

    async def connect(self, timeout: float = _CONNECT_TIMEOUT) -> PanelInfo:
        """Open the connection, exchange hellos, return the panel's identity."""
        try:
            async with asyncio.timeout(timeout):
                self._broken = False
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

    async def send(self, message: dict[str, Any], timeout: float = _SEND_TIMEOUT) -> None:
        """Send one message, within a deadline and behind a bounded queue.

        Only one frame is handed to the transport at a time, so a peer that
        stops reading cannot have callers pile encoded payloads into it; a
        caller that waits longer than `timeout`, and a caller that arrives
        when SEND_QUEUE_MAX are already waiting, is told the link is gone
        instead of hanging on it (HA11). The first send to give up finishes
        the connection, so the ones behind it raise rather than write another
        frame into a transport that is not moving.

        Nothing is dropped quietly: a send either reaches the transport or
        raises. A send that raises before writing did not go out at all; a
        send that gives up while the transport is flushing is uncertain, and
        either way the caller has to treat the link as gone and reconnect.
        """
        writer = self._writer
        if writer is None:
            raise CannotConnect("not connected")
        if self._broken:
            raise CannotConnect("the link gave up on an earlier send")
        frame = encode_frame(message)
        if self._waiting >= SEND_QUEUE_MAX:
            raise SendOverflow(f"{SEND_QUEUE_MAX} sends are already waiting for the panel")
        self._waiting += 1
        try:
            async with asyncio.timeout(timeout), self._send_lock:
                if self._writer is not writer or self._broken:
                    raise CannotConnect("the link is gone")
                # write() puts the whole frame in at once, so a cancelled
                # drain() below can never leave half a frame on the wire.
                writer.write(frame)
                await writer.drain()
        except TimeoutError as err:
            self._broken = True
            raise CannotConnect("the panel stopped reading") from err
        except (OSError, ConnectionError) as err:
            self._broken = True
            raise CannotConnect("connection lost") from err
        finally:
            self._waiting -= 1

    @property
    def _frame_reader(self) -> FrameReader | None:
        """This connection's parser, kept between calls so a cancelled read
        resumes instead of losing its place in the frame."""
        reader = self._reader
        if reader is None:
            return None
        if self._frames is None or self._frames.stream is not reader:
            self._frames = FrameReader(reader)
        return self._frames

    async def recv(self, timeout: float = _CONNECT_TIMEOUT) -> dict[str, Any]:
        """Receive one message.

        TimeoutError means only that nothing arrived: a quiet link is for the
        caller to judge (keepalives, protocol.md). Once part of a frame has
        been consumed the silence is no longer harmless, so the rest of that
        frame gets a bounded stall window and a frame that never finishes is
        a dead link rather than silence (HA10). Either way the next message is
        never read at the wrong boundary, and a cancelled recv keeps its place.

        A connection whose sending gave up is finished in both directions, so
        the listener reconnects instead of reading on over a link it can no
        longer answer (HA11).
        """
        frames = self._frame_reader
        if frames is None:
            raise CannotConnect("not connected")
        if self._broken:
            raise CannotConnect("the link gave up on an earlier send")
        try:
            async with asyncio.timeout(timeout):
                return await frames.read()
        except TimeoutError:
            if not frames.in_progress:
                raise
        try:
            async with asyncio.timeout(_FRAME_STALL):
                return await frames.read()
        except TimeoutError as err:
            raise CannotConnect("a frame stopped halfway") from err

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

    async def close(self, timeout: float = _CLOSE_TIMEOUT) -> None:
        """Close the connection; safe to call twice or before connecting.

        Bounded: a transport that cannot flush because the peer stopped
        reading is aborted rather than waited on for as long as it likes
        (HA11). Sends still queued see the writer change and give up.
        """
        writer, self._writer, self._reader, self._frames = self._writer, None, None, None
        if writer is None:
            return
        writer.close()
        try:
            async with asyncio.timeout(timeout):
                await writer.wait_closed()
        except TimeoutError:
            writer.transport.abort()
        except (OSError, ConnectionError):
            pass
