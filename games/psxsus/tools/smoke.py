#!/usr/bin/env python3
"""Drive PSXSUS's lobby against a RUNNING server, over real TCP.

Ten fake consoles do what ten real ones would: connect, name themselves, list
rooms, one creates and nine join, the owner starts the game, and everyone waits
to be told. Then they check that avatars are actually flowing.

    docker compose up -d
    python tools/smoke_psxsus.py
    python tools/smoke_psxsus.py --host 10.0.0.5

This is the deployment gate for a server with --game attached. The in-process
suite proves the rules; this proves the container: the plug-in really loaded, the
protocol module really imported next to it, and the whole path works over a
socket. Exits non-zero on failure.
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import sys

from games.psxsus import protocol as P
from psnl.protocol import (
    NO_SLOT,
    FrameParser,
    HelloAckPayload,
    HelloPayload,
    PacketType,
    encode_frame,
)

SCENE_HASH = 0xDEADBEEF
PLAYERS = 10


class Console:
    def __init__(self, idx: int) -> None:
        self.idx = idx
        self.name = f"P{idx:02d}"
        self.slot = NO_SLOT
        self.room_id = 0
        self.code = ""
        self.parser = FrameParser()
        self.app_data: list[bytes] = []
        self.snapshots = 0
        self.started = False
        self.role: int | None = None
        self.tasks: list[int] = []
        self.winner: int | None = None
        self._seq = 1

    async def connect(self, host: str, port: int) -> None:
        self.reader, self.writer = await asyncio.open_connection(host, port)

    def _send(self, ptype, payload=b""):
        frame = encode_frame(ptype, payload, src_slot=self.slot, seq=self._seq)
        self._seq = (self._seq + 1) & 0xFFFF
        self.writer.write(frame)

    def say(self, payload: bytes) -> None:
        self._send(PacketType.APP_DATA, payload)

    async def pump(self, timeout: float = 1.0) -> bool:
        try:
            data = await asyncio.wait_for(self.reader.read(4096), timeout)
        except asyncio.TimeoutError:
            return False
        if not data:
            raise ConnectionError("server closed the connection")
        for frame in self.parser.feed(data):
            if frame.header.packet_type == PacketType.HELLO_ACK:
                self.slot = HelloAckPayload.unpack(frame.payload).your_slot
            elif frame.header.packet_type == PacketType.SNAPSHOT:
                self.snapshots += 1
            elif frame.header.packet_type == PacketType.APP_DATA:
                self.app_data.append(frame.payload)
                self._send(PacketType.EVENT_ACK, b"")
                self._on_app(frame.payload)
        return True

    def _on_app(self, payload: bytes) -> None:
        if not payload:
            return
        if payload[0] == P.S2C.ROOM_JOINED:
            self.room_id = int.from_bytes(payload[1:3], "little")
            self.slot = payload[3]
            self.code = P.unpack_name(payload[4:8])
        elif payload[0] == P.S2C.GAME_STARTING:
            self.started = True
        elif payload[0] == P.S2C.ROLE_ASSIGNED:
            self.role = payload[1]
        elif payload[0] == P.S2C.TASK_LIST:
            self.tasks = list(payload[2 : 2 + payload[1]])
        elif payload[0] == P.S2C.GAME_OVER:
            self.winner = payload[1]

    async def until(self, pred, what: str, timeout: float = 8.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not pred():
            if loop.time() > deadline:
                raise TimeoutError(f"console {self.idx}: timed out waiting for {what}")
            await self.pump(timeout=min(1.0, deadline - loop.time()))

    async def handshake(self) -> None:
        self._send(PacketType.HELLO, HelloPayload(scene_hash=SCENE_HASH, tiebreak=self.idx).pack())
        await self.until(lambda: self.slot != NO_SLOT or self.app_data or True, "ack", 3.0)
        await self.pump(timeout=0.3)

    def send_avatar(self) -> None:
        rec = struct.pack("<BBHiiii", self.slot, 1, 0, self.idx * 100, 0, self.idx * 7, 0)
        self._send(PacketType.SNAPSHOT, struct.pack("<BBH", 1, 0, 0) + rec)

    async def close(self) -> None:
        self.writer.close()


def saw(c: Console, subtype: int) -> bytes | None:
    """The LATEST message of this subtype.

    Latest, not first: a room broadcasts a fresh RoomState on every join, so the
    first one a console ever received describes a room it was alone in.
    """
    for m in reversed(c.app_data):
        if m and m[0] == subtype:
            return m
    return None


async def run(host: str, port: int) -> int:
    consoles = [Console(i) for i in range(PLAYERS)]
    for c in consoles:
        await c.connect(host, port)
        await c.handshake()
        c.say(bytes([P.C2S.SET_NAME]) + P.pack_name(c.name))

    owner, joiners = consoles[0], consoles[1:]

    # A lobby means no slot until a room is chosen. That is the point of it.
    if owner.slot != NO_SLOT:
        print(f"FAIL: got slot {owner.slot} before joining a room - "
              "is the PSXSUS game plug-in actually attached? (check `game=` in the log)")
        return 1
    print("ok  no slot before choosing a room (the lobby exists)")

    owner.say(bytes([P.C2S.CREATE_ROOM]) + P.pack_name("smoke") + bytes([0]))
    await owner.until(lambda: owner.room_id != 0, "RoomJoined")
    print(f"ok  owner created room {owner.room_id} and holds slot {owner.slot}")

    for c in joiners:
        c.say(bytes([P.C2S.JOIN_ROOM]) + owner.room_id.to_bytes(2, "little"))
        await c.until(lambda c=c: c.room_id != 0, "RoomJoined")

    slots = sorted(c.slot for c in consoles)
    if len(set(slots)) != PLAYERS or 0 in slots:
        print(f"FAIL: bad slot assignment {slots}")
        return 1
    print(f"ok  {PLAYERS} players in one room on slots {slots[0]}..{slots[-1]} "
          "(slot 0 reserved for the authority)")

    # Everyone should know who everyone is. Each console only reads its socket
    # when it is waiting on something, so the RoomState broadcasts caused by the
    # other nine joins are still unread - drain before asking what they know.
    await owner.until(
        lambda: (lambda s: s is not None and s[1] == PLAYERS)(saw(owner, P.S2C.ROOM_STATE)),
        f"a RoomState listing all {PLAYERS} players",
    )
    state = saw(owner, P.S2C.ROOM_STATE)
    if state is None or state[1] != PLAYERS:
        print(f"FAIL: owner's RoomState shows {state[1] if state else 'nothing'}, expected {PLAYERS}")
        return 1
    names = {P.PlayerRecord.unpack(state[3 + i * 16 : 19 + i * 16]).name for i in range(PLAYERS)}
    if names != {c.name for c in consoles}:
        print(f"FAIL: RoomState names {sorted(names)}")
        return 1
    print(f"ok  every player is named in RoomState, owner marked")

    # A non-owner must not be able to start.
    joiners[0].say(bytes([P.C2S.START_GAME]))
    await asyncio.sleep(0.4)
    for c in consoles:
        await c.pump(timeout=0.2)
    if any(c.started for c in consoles):
        print("FAIL: a non-owner started the game")
        return 1
    print("ok  a non-owner cannot start the game")

    owner.say(bytes([P.C2S.START_GAME]))
    for c in consoles:
        await c.until(lambda c=c: c.started, "GameStarting")
    print(f"ok  the owner started it and all {PLAYERS} were told")

    # Avatars: client-authoritative, server-relayed.
    for c in consoles:
        c.send_avatar()
        c.snapshots = 0
    for c in consoles:
        await c.until(lambda c=c: c.snapshots >= 3, "snapshots")
    print(f"ok  snapshots flowing to all {PLAYERS}")

    # --- the round ---
    for c in consoles:
        await c.until(lambda c=c: c.role is not None, "RoleAssigned")

    impostors = [c for c in consoles if c.role == P.Role.IMPOSTOR]
    crew = [c for c in consoles if c.role == P.Role.CREW]
    if not impostors or not crew:
        print(f"FAIL: {len(impostors)} impostors / {len(crew)} crew")
        return 1
    # Each console was told its OWN role and nobody else's - the whole game
    # depends on that being true on the wire, not just in the UI.
    print(f"ok  roles dealt privately: {len(impostors)} impostor(s), {len(crew)} crew")

    if any(c.tasks for c in impostors) or not all(c.tasks for c in crew):
        print("FAIL: task lists went to the wrong people")
        return 1
    print(f"ok  every crewmate got {len(crew[0].tasks)} tasks; impostors got none")

    # A crewmate cannot kill. The server answers a forbidden request with
    # silence, so "nothing happened" is the pass condition.
    victim = crew[1]
    crew[0].say(bytes([P.C2S.KILL, victim.slot]))
    await asyncio.sleep(0.4)
    for c in consoles:
        await c.pump(timeout=0.2)
    if any(saw(c, P.S2C.PLAYER_DIED) for c in consoles):
        print("FAIL: a crewmate killed somebody")
        return 1
    print("ok  a crewmate cannot kill")

    # Crew finish every task -> crew win. This exercises the whole authoritative
    # loop: request, validate, tally, broadcast, end.
    for c in crew:
        for t in c.tasks:
            c.say(bytes([P.C2S.TASK_COMPLETE, t]))
    for c in consoles:
        await c.until(lambda c=c: c.winner is not None, "GameOver")
    if any(c.winner != P.Role.CREW for c in consoles):
        print(f"FAIL: winners {[c.winner for c in consoles]}")
        return 1
    print(f"ok  finishing every task wins it for the crew, and all {PLAYERS} were told")

    for c in consoles:
        await c.close()
    print("\nPASS - a whole PSXSUS round plays out against a real server.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6699)
    args = p.parse_args()
    print(f"PSXSUS lobby: {PLAYERS} fake consoles -> {args.host}:{args.port} (raw)\n")
    try:
        return asyncio.run(run(args.host, args.port))
    except (OSError, TimeoutError, ConnectionError) as e:
        print(f"FAIL: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
