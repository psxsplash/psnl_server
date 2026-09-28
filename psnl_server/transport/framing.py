"""Socket framing adapters.

A PSNL frame is `[sync][header][payload][crc]`, and the console's SIO1 driver
neither knows nor cares how those bytes reached the serial port. But *how* they
sit on a TCP socket depends entirely on who is on the other end:

    RawAdapter       the socket bytes ARE the serial bytes. Used by the Python
                     bridge (a serial cable carries raw bytes and nothing else),
                     and by PCSX-Redux when it acts as the SIO1 *server*.

    ProtobufAdapter  PCSX-Redux's SIO1 *client* wraps every serial byte in a
                     length-prefixed protobuf message. This is not optional:
                     `SIO1Client::startClient` throws
                     "Client doesn't currently support raw mode"
                     (pcsx-redux/src/core/sio1-server.cc:97-103), and the throw
                     is uncaught at the call site (gui.cc:2301). Since a player
                     pointing Redux at a remote server *must* use client mode,
                     Protobuf is the only wire available for that path.

Both adapters expose the same interface, so everything above them sees a clean
PSNL byte stream and never learns which one it is talking to.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

__all__ = ["FramingAdapter", "RawAdapter", "ProtobufAdapter", "MAX_PROTOBUF_PAYLOAD"]


class FramingAdapter(ABC):
    """Translates between the socket's byte stream and clean PSNL bytes."""

    name: str

    @abstractmethod
    def decode(self, data: bytes) -> bytes:
        """Socket bytes -> PSNL bytes. Buffers partial input internally."""

    @abstractmethod
    def encode(self, data: bytes) -> bytes:
        """PSNL bytes -> socket bytes."""


class RawAdapter(FramingAdapter):
    """Identity. The socket carries serial bytes verbatim."""

    name = "raw"

    def decode(self, data: bytes) -> bytes:
        return data

    def encode(self, data: bytes) -> bytes:
        return data


# --- minimal protobuf codec ----------------------------------------------
#
# Redux's SIOPayload is a three-message schema (sio1.h:46-53):
#
#     DataTransfer { bytes data = 1; }
#     FlowControl  { bool dxr = 1; bool xts = 2; }
#     SIOPayload   { DataTransfer data_transfer = 1; FlowControl flow_control = 2; }
#
# Hand-rolling it is ~40 lines and keeps `protobuf` out of the dependency tree
# for a schema that is three fields and can never change without Redux changing
# with it. It also matches the project's existing hand-rolled-binary convention.


def _put_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _get_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Returns (value, new_pos). Raises IndexError if truncated."""
    result = 0
    shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _tag(field: int, wire_type: int) -> bytes:
    return _put_varint((field << 3) | wire_type)


_WIRE_VARINT = 0
_WIRE_LEN = 2

# Redux prefixes each message with a *single* length byte
# (`m_fifo->write<uint8_t>(message.size())`, sio1.cc:41), so a serialized
# SIOPayload can never exceed 255 bytes. Backing out the worst-case protobuf
# overhead for a 2-byte-varint length (tag+len for SIOPayload.data_transfer, then
# tag+len for DataTransfer.data = 6 bytes) leaves 249; 245 keeps a safety margin
# and still carries a whole 215-byte snapshot in one message.
MAX_PROTOBUF_PAYLOAD = 245


def encode_sio_payload(data: bytes) -> bytes:
    """One SIOPayload carrying a DataTransfer, with Redux's 1-byte length prefix.

    Redux's own send path emits one byte per message, but its receive path
    `pushSlice`s the entire DataTransfer payload in one go
    (sio1.cc:98-111) - so we may batch many bytes per message and a 215-byte
    snapshot costs one message rather than 215.
    """
    if len(data) > MAX_PROTOBUF_PAYLOAD:
        raise ValueError(f"{len(data)} bytes exceeds MAX_PROTOBUF_PAYLOAD ({MAX_PROTOBUF_PAYLOAD})")

    # DataTransfer { bytes data = 1 }
    inner = _tag(1, _WIRE_LEN) + _put_varint(len(data)) + data
    # SIOPayload { DataTransfer data_transfer = 1 }
    #
    # flow_control is omitted rather than sent empty: Redux distinguishes the two
    # message kinds by `hasData()` on the bytes field (sio1.cc:199), and an
    # all-default FlowControl submessage would serialize to nothing useful while
    # costing bytes.
    payload = _tag(1, _WIRE_LEN) + _put_varint(len(inner)) + inner
    return bytes([len(payload)]) + payload


def encode_flow_control(dxr: bool, xts: bool) -> bytes:
    """One SIOPayload carrying FlowControl (DTR/RTS levels)."""
    inner = b""
    if dxr:
        inner += _tag(1, _WIRE_VARINT) + _put_varint(1)
    if xts:
        inner += _tag(2, _WIRE_VARINT) + _put_varint(1)
    payload = _tag(2, _WIRE_LEN) + _put_varint(len(inner)) + inner
    return bytes([len(payload)]) + payload


def decode_sio_payload(payload: bytes) -> bytes:
    """Extract DataTransfer.data from a serialized SIOPayload.

    Returns b"" for a flow-control-only message. Unknown fields are skipped
    rather than rejected, so a future Redux that adds one still interoperates.
    """
    pos = 0
    out = b""
    end = len(payload)
    while pos < end:
        key, pos = _get_varint(payload, pos)
        field, wire = key >> 3, key & 0x07
        if wire == _WIRE_LEN:
            length, pos = _get_varint(payload, pos)
            chunk = payload[pos : pos + length]
            pos += length
            if field == 1:  # data_transfer
                inner_pos = 0
                while inner_pos < len(chunk):
                    ikey, inner_pos = _get_varint(chunk, inner_pos)
                    ifield, iwire = ikey >> 3, ikey & 0x07
                    if iwire == _WIRE_LEN:
                        ilen, inner_pos = _get_varint(chunk, inner_pos)
                        if ifield == 1:  # data
                            out += chunk[inner_pos : inner_pos + ilen]
                        inner_pos += ilen
                    elif iwire == _WIRE_VARINT:
                        _, inner_pos = _get_varint(chunk, inner_pos)
                    else:
                        break
            # field 2 (flow_control) carries no PSNL bytes; ignore it.
        elif wire == _WIRE_VARINT:
            _, pos = _get_varint(payload, pos)
        else:
            break  # unknown wire type: stop rather than misparse
    return out


class ProtobufAdapter(FramingAdapter):
    """Redux's SIO1 client wire: `[u8 len][SIOPayload]`, repeated.

    Mirrors Redux's own READ_SIZE -> READ_MESSAGE state machine
    (sio1.h:113-127), but without its failure mode: Redux kills the session on a
    deserialize error, whereas we skip the message and keep going. A server that
    hangs up on one malformed packet is a server that drops players for a line
    glitch.
    """

    name = "protobuf"

    def __init__(self) -> None:
        self._buf = bytearray()
        self.decode_errors = 0

    def decode(self, data: bytes) -> bytes:
        self._buf.extend(data)
        out = bytearray()
        while True:
            if not self._buf:
                break
            size = self._buf[0]
            if len(self._buf) < 1 + size:
                break  # incomplete message; wait for more
            message = bytes(self._buf[1 : 1 + size])
            del self._buf[: 1 + size]
            try:
                out += decode_sio_payload(message)
            except (IndexError, ValueError):
                self.decode_errors += 1  # skip it; do not drop the connection
        return bytes(out)

    def encode(self, data: bytes) -> bytes:
        out = bytearray()
        for i in range(0, len(data), MAX_PROTOBUF_PAYLOAD):
            out += encode_sio_payload(data[i : i + MAX_PROTOBUF_PAYLOAD])
        return bytes(out)
