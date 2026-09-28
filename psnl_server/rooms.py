"""Rooms - membership, slot assignment, and per-room snapshot fan-out.

Authority model: the server RELAYS continuous state and OWNS discrete state.

That split is not a compromise, it is the whole design. Simulating movement
server-side would mean reimplementing the PlayStation's collision and navigation
in Python and keeping the two in agreement forever; every divergence would be a
desync bug with no natural home. So:

  - Avatar transforms are client-authoritative. Each console sends its own avatar
    (keyed by slot) and the server fans the set out. This is exactly what
    NetworkManager::buildAndSendSnapshot already does.
  - Discrete game state (roles, tasks, votes, kills, sabotage timers) is
    server-authoritative and travels over the reliable channel.
  - World objects are host-authoritative, and the server IS slot 0 - so the
    console's existing ObjectState path works unmodified.

The console needs no new code to be a client of this: `onHelloAck`
(networkmanager.cpp:178-194) already reads whatever slot it is handed.
"""

from __future__ import annotations

import logging
import secrets
import struct
import time
from dataclasses import dataclass, field

from psnl import HOST_SLOT, PacketType, encode_frame

logger = logging.getLogger(__name__)

# Mirrors NetworkManager::c_maxSlots. Slot 0 is the server (the authority, which
# has no avatar), so the player population is slots 1..10 - hence 11 slots for
# 10 players, not 10.
MAX_SLOTS = 11
FIRST_PLAYER_SLOT = 1
MAX_PLAYERS = MAX_SLOTS - FIRST_PLAYER_SLOT  # 10

# Snapshot record layout, from networkmanager.cpp:10-13:
#   [u8 avatarCount][u8 objectCount][u16 reserved]
#   avatar record (20B): [u8 slot][u8 flags][u16 pad][i32 px][i32 py][i32 pz][i32 yaw]
RECORD_SIZE = 20
SNAP_FLAG_ACTIVE = 0x01

# Snapshot formats. Mirrors `netprotocol.hh`; read that for the full rationale.
#
# LEGACY is the 20-byte absolute record this protocol has always emitted. COMPACT
# stores an absolute origin once per snapshot and every position as a 16-bit
# offset from it, shifted right by a per-snapshot amount the sender chooses to fit
# the actual spread. Ten bytes a record instead of twenty.
#
# The format is SELF-DESCRIBING - origin and shift travel in the packet - so there
# is nothing to negotiate and no agreed state that can be lost. That is deliberate:
# a negotiated precision profile is exactly the kind of thing that silently fails
# on a bad link, and this session has already watched a Ping/Pong exchange fail to
# complete on a saturated console. A format depending on such an exchange would
# have decoded every position wrongly rather than merely failing to measure.
SNAP_FORMAT_LEGACY = 0
SNAP_FORMAT_COMPACT = 1

COMPACT_ORIGIN_BYTES = 12
COMPACT_AVATAR_SIZE = 10

# Compact pays for itself from two avatars up: 12 + 10N < 20N whenever N >= 2.
# Below that it would make the packet bigger, so the sender stays on legacy.
COMPACT_MIN_AVATARS = 2

# Largest offset a 16-bit field can carry, and the largest shift worth trying.
# If even shift 15 cannot span the spread, the sender falls back to legacy rather
# than emit something lossy enough to be wrong - the failure mode is a bigger
# packet, never a teleporting player.
COMPACT_MAX_OFFSET = 0xFFFF
COMPACT_MAX_SHIFT = 15

# One full turn in 4.12 fixed point: round(2*pi * 4096). Mirrors c_fp12TwoPi.
FP12_TWO_PI = 25736


def normalize_fp12_angle(yaw: int) -> int:
    """Wrap an fp12 angle into [0, FP12_TWO_PI).

    Normalising rather than clamping is what makes 16 bits safe for yaw. Clamping
    would be wrong for an angle - a game that lets rotation accumulate would have
    its players stick at the clamp - and truncating to 16 bits would wrap at 1.27
    turns, which is not a rotation.
    """
    return yaw % FP12_TWO_PI


def choose_compact_shift(spread: int) -> int | None:
    """Smallest shift for which `spread >> shift` fits in 16 bits, or None.

    None means the positions are too far apart to compact at any precision worth
    having, and the caller should emit legacy instead.
    """
    for shift in range(COMPACT_MAX_SHIFT + 1):
        if (spread >> shift) <= COMPACT_MAX_OFFSET:
            return shift
    return None

# Names ride the reliable channel (256B), not the UI text buffer (63 chars), so
# the payload is the binding constraint: 15 rooms x 16B = 244B keeps a full room
# list inside a single reliable packet, which matters because the reliable
# channel is stop-and-wait.
MAX_NAME_LEN = 12


@dataclass(slots=True)
class Avatar:
    """The latest transform a client reported for itself."""

    px: int = 0
    py: int = 0
    pz: int = 0
    yaw: int = 0
    flags: int = SNAP_FLAG_ACTIVE
    updated_at: float = 0.0

    def pack(self, slot: int) -> bytes:
        return struct.pack(
            "<BBHiiii", slot, self.flags, 0, self.px, self.py, self.pz, self.yaw
        )

    def pack_compact(self, slot: int, origin: tuple[int, int, int], shift: int) -> bytes:
        """Ten bytes instead of twenty: 16-bit offsets from a shared origin.

        Mirrors the decoder in `networkmanager.cpp` (applyPendingSnapshot) and the
        format described in `netprotocol.hh`.
        """
        ox, oy, oz = origin
        return struct.pack(
            "<BBHHHH",
            slot,
            self.flags,
            (self.px - ox) >> shift,
            (self.py - oy) >> shift,
            (self.pz - oz) >> shift,
            normalize_fp12_angle(self.yaw),
        )


@dataclass(slots=True)
class Member:
    slot: int
    session: object  # Session; untyped to avoid a circular import
    name: str = ""
    avatar: Avatar = field(default_factory=Avatar)
    scene_hash: int = 0


class RoomFullError(Exception):
    pass


class SceneMismatchError(Exception):
    """The joiner's splashpack disagrees with the room's."""


@dataclass(slots=True)
class Room:
    room_id: int
    name: str
    private: bool = False
    join_code: str = ""
    members: dict[int, Member] = field(default_factory=dict)
    # The room's scene identity. Set by the first joiner and thereafter enforced.
    #
    # The server deliberately treats this as OPAQUE and keeps no registry of
    # known scenes: a registry is a config file that drifts from the content the
    # moment anyone edits a map. Agreement *within* a room is the only property
    # that actually matters, and it needs no server-side knowledge at all.
    scene_hash: int | None = None
    in_progress: bool = False
    created_at: float = field(default_factory=time.monotonic)

    @property
    def player_count(self) -> int:
        return len(self.members)

    @property
    def is_full(self) -> bool:
        return self.player_count >= MAX_SLOTS - FIRST_PLAYER_SLOT

    def _free_slot(self) -> int:
        used = set(self.members)
        for slot in range(FIRST_PLAYER_SLOT, MAX_SLOTS):
            if slot not in used:
                return slot
        raise RoomFullError(f"room {self.room_id} is full")

    def add(self, session: object, scene_hash: int, name: str = "") -> Member:
        if self.scene_hash is None:
            self.scene_hash = scene_hash
        elif scene_hash != self.scene_hash:
            raise SceneMismatchError(
                f"joiner scene_hash={scene_hash:#010x} != room {self.scene_hash:#010x}"
            )
        slot = self._free_slot()
        member = Member(slot=slot, session=session, name=name[:MAX_NAME_LEN], scene_hash=scene_hash)
        self.members[slot] = member
        logger.info("room %d: slot %d joined (%d players)", self.room_id, slot, self.player_count)
        return member

    def remove(self, slot: int) -> None:
        if self.members.pop(slot, None) is not None:
            logger.info("room %d: slot %d left (%d players)", self.room_id, slot, self.player_count)
        if not self.members:
            # Reset identity so a recycled empty room can host a different scene.
            self.scene_hash = None
            self.in_progress = False
            return

        # A DEPARTURE CAN COMPLETE A SCENE MOVE, and forgetting that stranded rooms
        # permanently.
        #
        # `rebind` only adopts a new scene once EVERY member has arrived there, so
        # while one console is still on the old scene the room keeps the old
        # identity. If that console then LEAVES rather than finishing the move, the
        # condition that was waiting on it can never be met again: the remaining
        # members are all on the new scene and none of them will reload it, so
        # nothing calls rebind and the room's identity stays on a scene that nobody
        # is in.
        #
        # The room is then invisible and unjoinable, because both gates compare
        # against its scene_hash: `_h_list_rooms` drops rooms whose hash is not the
        # asker's, and `add` answers SCENE_MISMATCH. That is the "leaving a lobby
        # prematurely makes rooms stop showing up" bug, and it survived the round -
        # the room stayed hidden for the rest of its life.
        #
        # Same rule as rebind, re-evaluated against who is actually left.
        hashes = {m.scene_hash for m in self.members.values()}
        if len(hashes) == 1:
            arrived = hashes.pop()
            if arrived != self.scene_hash:
                self.scene_hash = arrived
                logger.info("room %d: last straggler left, room is now scene %#010x",
                            self.room_id, arrived)

    def scene_groups(self) -> dict[int, list[Member]]:
        """Members grouped by the scene each is actually in.

        Normally there is exactly one group. During a scene handover there are
        two, because consoles finish loading at different times - the room is
        mid-flight from the lobby scene to the game scene and some players have
        arrived while others are still on the loading screen.
        """
        groups: dict[int, list[Member]] = {}
        for member in self.members.values():
            groups.setdefault(member.scene_hash, []).append(member)
        return groups

    def build_snapshot(self, members: list[Member]) -> bytes:
        """One snapshot for one scene group.

        A single buffer is broadcast verbatim to every member of the group - no
        per-recipient variant - because the console already ignores its own slot
        on receipt (`if (slot == m_localSlot) continue;`, networkmanager.cpp:268).
        That turns fan-out into one serialization plus N writes.
        """
        ordered = sorted(members, key=lambda m: m.slot)

        compact = self._build_compact(ordered)
        if compact is not None:
            return compact

        avatars = b"".join(m.avatar.pack(m.slot) for m in ordered)
        header = struct.pack("<BBBB", len(ordered), 0, SNAP_FORMAT_LEGACY, 0)
        return header + avatars

    @staticmethod
    def _build_compact(ordered: list[Member]) -> bytes | None:
        """The compact snapshot, or None when legacy would be smaller or safer.

        This is the single biggest saving available on the wire: at ten players
        it is 116 bytes against 204. That matters twice over on real hardware -
        less wire time, and 43% less pressure on an 8-byte RX FIFO that has been
        measured overrunning.
        """
        if len(ordered) < COMPACT_MIN_AVATARS:
            return None

        xs = [m.avatar.px for m in ordered]
        ys = [m.avatar.py for m in ordered]
        zs = [m.avatar.pz for m in ordered]
        origin = (min(xs), min(ys), min(zs))
        spread = max(max(xs) - origin[0], max(ys) - origin[1], max(zs) - origin[2])

        shift = choose_compact_shift(spread)
        if shift is None:
            return None  # too far apart to compact; legacy is the honest answer

        header = struct.pack("<BBBB", len(ordered), 0, SNAP_FORMAT_COMPACT, shift)
        body = struct.pack("<iii", *origin)
        records = b"".join(m.avatar.pack_compact(m.slot, origin, shift) for m in ordered)
        return header + body + records

    def rebind(self, member: Member, scene_hash: int) -> None:
        """A member finished loading a new scene, keeping its slot.

        Always accepted: the member already holds a slot and is merely reporting
        where it now is. The room's own identity only follows once EVERY member
        has arrived - so a single console rebinding to a bogus hash cannot drag
        the room with it, it just stops sharing a snapshot group with anyone.
        """
        member.scene_hash = scene_hash
        if all(m.scene_hash == scene_hash for m in self.members.values()):
            self.scene_hash = scene_hash
            logger.info("room %d: whole room moved to scene %#010x", self.room_id, scene_hash)


class RoomManager:
    def __init__(self, max_rooms: int = 256) -> None:
        self._rooms: dict[int, Room] = {}
        self._next_id = 1
        self._max_rooms = max_rooms

    def create(self, name: str, private: bool = False) -> Room:
        if len(self._rooms) >= self._max_rooms:
            raise RoomFullError("server is at its room limit")
        room_id = self._next_id
        self._next_id += 1
        # EVERY room gets a code, not just private ones. A private room needs it to
        # be reachable at all, but a public room wants it too: reading four letters
        # to the person next to you is how people actually join each other, and a
        # public room with no code can only be found by scrolling the list.
        # Unambiguous alphabet: no O/0 or I/1, because these get read aloud.
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        existing = {r.join_code for r in self._rooms.values()}
        for _ in range(16):
            join_code = "".join(secrets.choice(alphabet) for _ in range(4))
            if join_code not in existing:
                break
        room = Room(room_id=room_id, name=name[:MAX_NAME_LEN], private=private, join_code=join_code)
        self._rooms[room_id] = room
        logger.info("created room %d %r private=%s", room_id, room.name, private)
        return room

    def get(self, room_id: int) -> Room | None:
        return self._rooms.get(room_id)

    def by_join_code(self, code: str) -> Room | None:
        code = code.upper()
        for room in self._rooms.values():
            if room.join_code and room.join_code == code:
                return room
        return None

    def public_rooms(self) -> list[Room]:
        return [r for r in self._rooms.values() if not r.private]

    def all_rooms(self) -> list[Room]:
        return list(self._rooms.values())

    def destroy_if_empty(self, room: Room) -> None:
        if room.player_count == 0 and room.room_id in self._rooms:
            del self._rooms[room.room_id]
            logger.info("destroyed empty room %d", room.room_id)


def encode_snapshot_frame(room: Room, members: list[Member]) -> bytes:
    """The exact bytes the server puts on one scene group's wire this tick."""
    return encode_frame(
        PacketType.SNAPSHOT, room.build_snapshot(members), src_slot=HOST_SLOT, seq=0
    )
