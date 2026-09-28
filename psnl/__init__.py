"""psnl - the PSNL serial network protocol, shared by the central server and the
Python bridge.

The psxsplash engine (C++/MIPS) is the reference implementation of this wire
format. This package exists so the server and the bridge share exactly one
implementation of framing and CRC, rather than each growing its own and drifting
apart.

Conformance against the engine is enforced by replaying a corpus the engine
itself generates - see ``tests/test_conformance.py``.
"""

from .protocol import (
    FLAG_REBIND,
    HOST_SLOT,
    MAGIC,
    MAX_EVENT_PAYLOAD,
    MAX_PAYLOAD,
    NO_SLOT,
    PROTO_VERSION,
    SYNC_BYTE,
    Frame,
    FrameParser,
    HelloAckPayload,
    HelloPayload,
    PacketHeader,
    PacketType,
    PingPayload,
    ProtocolError,
    crc16,
    encode_frame,
)

__all__ = [
    "FLAG_REBIND",
    "HOST_SLOT",
    "MAGIC",
    "MAX_EVENT_PAYLOAD",
    "MAX_PAYLOAD",
    "NO_SLOT",
    "PROTO_VERSION",
    "SYNC_BYTE",
    "Frame",
    "FrameParser",
    "HelloAckPayload",
    "HelloPayload",
    "PacketHeader",
    "PacketType",
    "PingPayload",
    "ProtocolError",
    "crc16",
    "encode_frame",
]

__version__ = "0.1.0"
