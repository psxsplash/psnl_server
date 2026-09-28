# psnl

The PSNL serial network protocol for psxsplash, in Python. Shared by the central
server and the Python bridge.

## Why this exists

The bridge must be Python (it talks to a real PlayStation over a serial cable),
and the server needs the identical wire format. Implementing the protocol twice -
once per component, or once per language - would guarantee that framing and CRC
bugs get fixed in one place and not the other. This package is the single
implementation both import.

The **C++ engine is the reference implementation**. This package conforms to it,
not the other way round.

## Install

```sh
cd packages/psnl
python -m pip install -e ".[dev]"
```

## Use

```python
from psnl import FrameParser, PacketType, encode_frame, HelloAckPayload

# Encode: one on-wire frame, [sync][header 8B][payload][crc16 LE]
ack = HelloAckPayload(your_slot=3, scene_hash=0xDEADBEEF, player_count=7)
data = encode_frame(PacketType.HELLO_ACK, ack.pack(), src_slot=0, seq=1)

# Decode: feed arbitrary chunks (TCP and serial both deliver fragments),
# get whole validated frames back.
parser = FrameParser()
for frame in parser.feed(incoming_bytes):
    print(frame.type, frame.src_slot, frame.payload)
```

`FrameParser` handles corruption the same way the console does - drop the frame,
hunt for the next sync byte - so a noisy line degrades throughput without ever
breaking the parser. It carries the same known caveat as the console: a `0xA5`
inside garbage is indistinguishable from a frame start and swallows the following
frame before the CRC forces a resync. That costs one frame, by design; snapshots
are unreliable anyway.

## Conformance

The tests replay a corpus that the **engine itself generates**
(`psxsplash/tests/host/gen_vectors.cpp`), in both directions:

```
decode:  parse(vector.bin)      == manifest spec
encode:  encode(manifest spec)  == vector.bin
```

Both directions matter. Decode-only would let the server accept the console's
frames while emitting subtly different ones; encode-only would let it emit
correct frames it cannot itself parse.

```sh
python -m pytest tests -q
```

If the corpus is missing, generate it first:

```sh
cd ../../psxsplash/tests/host && make vectors     # or .\build_msvc.ps1 -Vectors
```

**A conformance failure means the console and the server disagree about the
protocol.** Fix the code, never the expected bytes.

## Protocol summary

Mirrors `psxsplash/src/netprotocol.hh`:

- Frame: `[sync 0xA5][PacketHeader 8B][payload][crc16 LE]`, CRC-16/CCITT-FALSE
  over header + payload, **not** the sync byte.
- `PacketHeader{u8 type, u8 src_slot, u8 flags, u8 reserved, u16 seq, u16 length}`
- Magic `0x4C4E5350` = `"PSNL"` little-endian. Proto version 1. Max payload 512
  (256 on the reliable channel).
- Slot-addressed from day one: slot 0 is the host/authority, `0xFF` = unassigned.
  A two-console link uses slots {0,1}; the central server fans out to N.
