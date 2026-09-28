#!/usr/bin/env python3
"""Drive 10 fake consoles against a RUNNING server in RELAY mode, over real TCP.

The pytest suite exercises the server in-process, which proves the logic but not
the deployment: not the wheels the image built, not the entrypoint it runs, not
the ports it publishes. This talks to whatever is actually listening.

    docker compose up -d
    python tools/smoke_10_consoles.py                 # defaults to 127.0.0.1:6699
    python tools/smoke_10_consoles.py --host 10.0.0.5 --port 6699

Exits non-zero on failure, so it works as a deployment gate.

RELAY MODE ONLY. This expects the server to drop a console into a room the
moment it says Hello, which is what happens with no --game attached. A game with
a lobby (PSXSUS) deliberately does NOT auto-join - the player has not chosen a
room yet - so every console here would sit at slot 255 with no snapshots, and
that is correct behaviour rather than a fault. Use tools/smoke_psxsus.py for
that server.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Runnable straight from a checkout, without installing first: a deployment gate
# you have to `pip install` before you can use is a gate people skip. This reaches
# the repo root, never above it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psnl.protocol import (
    HOST_SLOT,
    NO_SLOT,
    FrameParser,
    HelloAckPayload,
    HelloPayload,
    PacketType,
    encode_frame,
)

SCENE_HASH = 0xDEADBEEF
SNAPSHOTS_WANTED = 5


async def fake_console(idx: int, host: str, port: int, results: dict) -> None:
    reader, writer = await asyncio.open_connection(host, port)
    parser = FrameParser()
    try:
        hello = HelloPayload(scene_hash=SCENE_HASH, tiebreak=1000 + idx)
        writer.write(encode_frame(PacketType.HELLO, hello.pack()))
        await writer.drain()

        slot = None
        snapshots = 0
        payload_len = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0

        while loop.time() < deadline:
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if not data:
                break
            for frame in parser.feed(data):
                if frame.header.packet_type == PacketType.HELLO_ACK:
                    slot = HelloAckPayload.unpack(frame.payload).your_slot
                elif frame.header.packet_type == PacketType.SNAPSHOT:
                    snapshots += 1
                    payload_len = len(frame.payload)
                    if snapshots >= SNAPSHOTS_WANTED and slot is not None:
                        results[idx] = (slot, snapshots, payload_len)
                        return
        results[idx] = (slot, snapshots, payload_len)
    finally:
        writer.close()


async def run(host: str, port: int) -> int:
    results: dict[int, tuple] = {}
    await asyncio.gather(*(fake_console(i, host, port, results) for i in range(10)))

    slots = sorted(s for s, _, _ in results.values() if s is not None and s != NO_SLOT)
    ok = True

    if not slots:
        # The single most likely explanation, and it is not a broken server.
        print(
            "FAIL: no console was given a slot.\n"
            "      If this server has a game plug-in with a lobby attached "
            "(look for `game=` in its startup log),\n"
            "      that is CORRECT: consoles pick a room before they get a slot. "
            "Use tools/smoke_psxsus.py instead."
        )
        return 1

    if len(slots) != 10:
        print(f"FAIL: {len(slots)}/10 consoles were given a slot")
        ok = False
    if len(set(slots)) != len(slots):
        print(f"FAIL: duplicate slots handed out: {slots}")
        ok = False
    # Slot 0 is the server: the authority, and avatar-less. Handing it to a
    # console would mean two things believe they are the authority.
    if HOST_SLOT in slots:
        print("FAIL: a console was given the authority slot 0")
        ok = False

    for i, (slot, snaps, plen) in sorted(results.items()):
        status = "ok " if snaps >= SNAPSHOTS_WANTED else "BAD"
        print(f"  {status} console {i}: slot={slot} snapshots={snaps} payload={plen}B")
        if snaps < SNAPSHOTS_WANTED:
            ok = False

    print()
    if ok:
        print(f"PASS - slots {slots[0]}..{slots[-1]}, all receiving snapshots.")
        return 0
    print("FAIL")
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6699, help="raw port (6699); Redux uses 6700")
    args = p.parse_args()

    print(f"10 fake consoles -> {args.host}:{args.port} (raw)")
    try:
        return asyncio.run(run(args.host, args.port))
    except OSError as e:
        print(f"FAIL: cannot reach {args.host}:{args.port} - is the server up? ({e})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
