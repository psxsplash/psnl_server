"""A PlayStation, as far as the server can tell.

Shipped as part of the package rather than kept in the test folder, because a
game plug-in needs it as much as the server does: it is how you test your own
rules without a console, an emulator or a serial cable on the desk.

    from psnl_server.testing import FakeConsole

    console = FakeConsole()
    await console.connect("127.0.0.1", port)
    await console.handshake()
    console.send_app_data(b"...")
    reply = await console.wait_for_app_data(MY_OPCODE)

It speaks the byte-exact wire the real engine emits, checked against the
engine-generated corpus in tests/test_psnl_conformance.py, which is what lets a
ten-player star topology be proven with nothing plugged in.
"""

from __future__ import annotations

import asyncio
import struct

from psnl import (
    NO_SLOT,
    Frame,
    FrameParser,
    HelloAckPayload,
    HelloPayload,
    PacketType,
    PingPayload,
    encode_frame,
)

from psnl_server.rooms import (
    COMPACT_AVATAR_SIZE,
    COMPACT_ORIGIN_BYTES,
    SNAP_FORMAT_COMPACT,
)
from psnl_server.transport.framing import ProtobufAdapter, RawAdapter

SNAP_FLAG_ACTIVE = 0x01


class FakeConsole:
    def __init__(self, scene_hash: int = 0xDEADBEEF, *, protobuf: bool = False) -> None:
        self.scene_hash = scene_hash
        self.adapter = ProtobufAdapter() if protobuf else RawAdapter()
        self.parser = FrameParser()
        self.slot = NO_SLOT
        self.host_slot = 0
        self.player_count = 0
        self.connected = False
        self.snapshots: list[Frame] = []
        self.events: list[tuple[int, int]] = []
        self.app_data: list[bytes] = []
        self.pongs: list[PingPayload] = []
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._tx_seq = 1

    async def connect(self, host: str, port: int) -> None:
        self.reader, self.writer = await asyncio.open_connection(host, port)

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass

    def _send(self, packet_type: PacketType, payload: bytes = b"", *, flags: int = 0, seq: int | None = None) -> None:
        assert self.writer is not None
        if seq is None:
            seq = self._tx_seq
            self._tx_seq = (self._tx_seq + 1) & 0xFFFF
        frame = encode_frame(packet_type, payload, src_slot=self.slot, seq=seq, flags=flags)
        self.writer.write(self.adapter.encode(frame))

    async def _pump(self, timeout: float = 2.0) -> None:
        """Read once and dispatch whatever whole frames arrived."""
        assert self.reader is not None
        data = await asyncio.wait_for(self.reader.read(4096), timeout)
        if not data:
            raise ConnectionError("server closed the connection")
        for frame in self.parser.feed(self.adapter.decode(data)):
            self._dispatch(frame)

    def _dispatch(self, frame: Frame) -> None:
        if frame.type == PacketType.HELLO_ACK:
            ack = HelloAckPayload.unpack(frame.payload)
            self.slot = ack.your_slot
            self.host_slot = ack.host_slot
            self.player_count = ack.player_count
            self.connected = True
        elif frame.type == PacketType.SNAPSHOT:
            self.snapshots.append(frame)
        elif frame.type == PacketType.EVENT:
            if len(frame.payload) >= 8:
                self.events.append(struct.unpack_from("<ii", frame.payload, 0))
            self._send(PacketType.EVENT_ACK, b"", seq=frame.seq)
        elif frame.type == PacketType.APP_DATA:
            self.app_data.append(frame.payload)
            self._send(PacketType.EVENT_ACK, b"", seq=frame.seq)
        elif frame.type == PacketType.PONG:
            self.pongs.append(PingPayload.unpack(frame.payload))

    def send_ping(self, echo: int, goodput: int = 0, rtt_millis: int = 0) -> None:
        """Report what this console measured, and ask for a Pong back.

        The console is the only party that can observe the far end of the link -
        the server's own view stops at its socket buffer - so this is how the
        server learns anything true about it.
        """
        self._send(
            PacketType.PING,
            PingPayload(echo=echo, goodput=goodput, rtt_millis=rtt_millis).pack(),
        )

    async def handshake(self, timeout: float = 2.0, *, rebind: bool = False) -> None:
        hello = HelloPayload(scene_hash=self.scene_hash, tiebreak=0x1234)
        flags = 0x01 if rebind else 0  # FLAG_REBIND
        # Restart the reliable sequence, because the real console does:
        # Net.Connect() calls NetLink::reset(), which sets m_txEventSeq back to 1.
        # A fixture that kept counting could never reproduce the server treating
        # the first post-handover packet as a duplicate of the last pre-handover
        # one - which is a bug this suite is supposed to be able to catch.
        self._tx_seq = 1
        self._send(PacketType.HELLO, hello.pack(), flags=flags)

        deadline = asyncio.get_running_loop().time() + timeout
        was = self.slot
        self.connected = False
        while not self.connected:
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("no HelloAck")
            await self._pump(timeout=timeout)
        if rebind and was != NO_SLOT:
            assert self.slot == was, f"rebind changed slot {was} -> {self.slot}"

    def send_avatar(self, px: int, py: int, pz: int, yaw: int) -> None:
        """Report our own avatar, exactly as buildAndSendSnapshot does."""
        record = struct.pack("<BBHiiii", self.slot, SNAP_FLAG_ACTIVE, 0, px, py, pz, yaw)
        payload = struct.pack("<BBH", 1, 0, 0) + record
        self._send(PacketType.SNAPSHOT, payload)

    def send_event(self, event_id: int, arg: int) -> None:
        self._send(PacketType.EVENT, struct.pack("<ii", event_id, arg))

    def send_app_data(self, payload: bytes) -> None:
        """The game's own channel - exactly what Lua's Net.SendData emits."""
        self._send(PacketType.APP_DATA, payload)

    async def settle(self, seconds: float = 0.2) -> None:
        """Read for `seconds`, discarding whatever is already in flight.

        Reliable server->console delivery is stop-and-wait with a queue behind
        it, so messages the test does not care about can still be arriving from
        earlier steps. Without draining them first, a `wait_for_app_data` marked
        "after now" can match a BACKLOGGED older message and assert against stale
        contents - which shows up as a test that passes most of the time.

        Deliberately time-boxed rather than "read until quiet": a joined console
        receives snapshots continuously, so the link is never actually quiet and
        a wait-for-silence loop would hang forever.
        """
        assert self.reader is not None
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            try:
                await self._pump(timeout=remaining)
            except (TimeoutError, asyncio.TimeoutError):
                return

    def mark(self) -> int:
        """Current AppData position, for `wait_for_app_data(since=...)`.

        Take one BEFORE the action you are testing. Without it a wait scans from
        the beginning and can match a message the previous step produced - which
        makes the test pass or fail for reasons that have nothing to do with what
        it is checking.
        """
        return len(self.app_data)

    async def wait_for_app_data(
        self, subtype: int | None = None, timeout: float = 2.0, since: int = 0
    ) -> bytes:
        """Wait for an AppData message, optionally of a given subtype.

        Filtering by subtype matters because one action can provoke several
        replies (RoomJoined then RoomState), and a test that grabs whichever
        arrives first is a test that passes for the wrong reason.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        seen = since
        while True:
            while seen < len(self.app_data):
                msg = self.app_data[seen]
                seen += 1
                if subtype is None or (msg and msg[0] == subtype):
                    return msg
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"no AppData{'' if subtype is None else f' subtype {subtype:#x}'} arrived"
                )
            await self._pump(timeout=remaining)

    async def wait_for_snapshot(self, timeout: float = 2.0) -> Frame:
        before = len(self.snapshots)
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.snapshots) == before:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("no snapshot arrived")
            await self._pump(timeout=remaining)
        return self.snapshots[-1]

    @staticmethod
    def parse_snapshot(frame: Frame) -> dict[int, tuple[int, int, int, int]]:
        """-> {slot: (px, py, pz, yaw)}

        Understands both snapshot formats, mirroring the console decoder in
        `networkmanager.cpp` (applyPendingSnapshot). A test parser that only knew
        the legacy layout would fail the moment the server compacted anything -
        which is what it just did, and correctly so.

        Positions from a compact snapshot are quantised, so callers comparing
        exact coordinates should either send values that are multiples of the
        shift or assert with a tolerance.
        """
        payload = frame.payload
        count = payload[0]
        fmt = payload[2]
        shift = payload[3]
        out: dict[int, tuple[int, int, int, int]] = {}
        off = 4

        if fmt == SNAP_FORMAT_COMPACT:
            ox, oy, oz = struct.unpack_from("<iii", payload, off)
            off += COMPACT_ORIGIN_BYTES
            for _ in range(count):
                slot, _flags, dx, dy, dz, yaw = struct.unpack_from("<BBHHHH", payload, off)
                off += COMPACT_AVATAR_SIZE
                out[slot] = (ox + (dx << shift), oy + (dy << shift), oz + (dz << shift), yaw)
            return out

        for _ in range(count):
            slot, _flags, _pad, px, py, pz, yaw = struct.unpack_from("<BBHiiii", payload, off)
            off += 20
            out[slot] = (px, py, pz, yaw)
        return out
