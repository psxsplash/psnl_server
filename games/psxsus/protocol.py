"""PSXSUS room protocol - the server's half.

The console's half is `PSXSUS/Lua/psxsus_protocol.lua`. The two must agree
byte-for-byte; `PSXSUS/Protocol/ROOM_PROTOCOL.md` is the specification, and
`server/tests/test_psxsus.py` enforces the agreement.

This rides the engine's opaque AppData channel, so nothing here appears in
netprotocol.hh - see ROOM_PROTOCOL.md for why the engine deliberately knows
nothing about rooms.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

# One reliable packet is 256 bytes (net::c_maxEventPayload) and the channel is
# stop-and-wait, so a reply that does not fit one packet costs a whole extra
# round trip (~33ms). Every payload below is sized against this.
MAX_PAYLOAD = 256

NAME_LEN = 12
CODE_LEN = 4

#: The join-code alphabet. No O/0 and no I/1: these codes get read aloud across a
#: room, and "is that an oh or a zero" is the whole reason to drop them. The
#: console's code-entry UI cycles through exactly this string, so it must match
#: what the server mints - `test_psxsus_protocol.py` asserts that it does.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ROOM_RECORD = 16
PLAYER_RECORD = 16

# 4-byte header + 15 * 16 = 244 <= 256. This is where NAME_LEN comes from.
ROOMS_PER_PAGE = 15
assert 4 + ROOMS_PER_PAGE * ROOM_RECORD <= MAX_PAYLOAD


class C2S(IntEnum):
    SET_NAME = 0x01
    LIST_ROOMS = 0x02
    CREATE_ROOM = 0x03
    JOIN_ROOM = 0x04
    JOIN_BY_CODE = 0x05
    LEAVE_ROOM = 0x06
    START_GAME = 0x07
    TASK_COMPLETE = 0x10
    REPORT_BODY = 0x11
    VOTE = 0x12
    KILL = 0x13
    SABOTAGE = 0x14
    FIX_SABOTAGE = 0x15
    USE_VENT = 0x16
    CALL_MEETING = 0x17
    #: Entering/leaving a vent, as opposed to USE_VENT's hop between them. This one
    #: IS relayed to the room: a player inside a vent is invisible to everyone, and
    #: the other consoles cannot hide a sprite they were never told about.
    VENT_STATE = 0x18
    #: "Tell me the roster." Sent by the GAME scene once its link is up.
    #:
    #: The roster is only broadcast when it CHANGES, and it does not change when
    #: a round starts - everyone was already in the room. But loading the game
    #: scene wipes the console's Lua state, so the new scene comes up knowing
    #: nobody: no names for the vote screen, and no living players for the kill
    #: lookup to find. Asking is what refills it.
    REQUEST_STATE = 0x19
    #: "We are done looking at the results - take the room back to the lobby."
    #:
    #: Sent by the ROOM'S OWNER from the game-over screen, and relayed to
    #: everyone as RETURN_LOBBY. It exists because the alternative is each
    #: console pressing its own way out, which means ten players trickle back to
    #: the lobby over ten seconds and the host cannot start the next round until
    #: the slowest of them has noticed the game is over. One person decides, and
    #: the room moves together - the same reasoning as MEETING_END.
    RETURN_TO_LOBBY = 0x1A
    #: "Put this room on a different ship." Owner only, lobby only.
    #:
    #: The map used to be fixed at creation, so an owner who picked wrong - or a
    #: room that just wanted a change of scene between rounds - had to be torn
    #: down and rebuilt, which costs everyone their slot and the join code. The
    #: server clamps the value and refuses the request outright while a round is
    #: running: changing the map under a match in progress would leave every
    #: console on a ship the server is no longer simulating.
    SET_MAP = 0x1B


class S2C(IntEnum):
    ROOM_LIST = 0x81
    ROOM_JOINED = 0x82
    ROOM_REJECTED = 0x83
    ROOM_STATE = 0x84
    GAME_STARTING = 0x85
    ROLE_ASSIGNED = 0x86
    TASK_LIST = 0x87
    TASK_PROGRESS = 0x88
    MEETING_START = 0x89
    VOTE_TALLY = 0x8A
    EJECTED = 0x8B
    SABOTAGE_START = 0x8C
    SABOTAGE_END = 0x8D
    PLAYER_DIED = 0x8E
    GAME_OVER = 0x8F
    PLAYER_VENT = 0x90
    #: Periodic resync of the running sabotage countdown. The server owns the
    #: timer; without this the console had to integrate its own copy, and the two
    #: drifted apart in opposite directions with no correction.
    SABOTAGE_TICK = 0x91
    #: The meeting is over and play resumes. Sent after the result has been on
    #: screen long enough to read, so every console leaves the vote at the same
    #: moment instead of whenever its player happens to press a button.
    MEETING_END = 0x92
    #: Everybody back to the lobby, now. The answer to RETURN_TO_LOBBY.
    RETURN_LOBBY = 0x93
    #: The meeting clock, once a second: is voting open yet, and how long is
    #: left. The server owns it for the same reason it owns the sabotage timer.
    MEETING_TICK = 0x94
    #: This player's own cooldowns, once a second while the round is running.
    #:
    #: Addressed to ONE player, not broadcast: a kill cooldown is personal, and
    #: telling the whole room when the impostor can next kill would hand the crew
    #: the one piece of information the impostor is trying to hide.
    #:
    #: Playtesters asked for this directly - "where there's timeouts like with
    #: killing and emergency meetings a visible timer should be seen". Before it,
    #: the console knew nothing about either: pressing KILL early looked
    #: identical to pressing it out of range, and identical to a dropped packet.
    COOLDOWN_TICK = 0x95
    #: Who your fellow impostors are. SENT ONLY TO IMPOSTORS.
    #:
    #: A crewmate is never sent this message, which is the whole security model:
    #: a modified client cannot render what it was never given, so the marker the
    #: console draws over a fellow impostor is safe in a way that a "draw this
    #: only if my own role is IMPOSTOR" flag would not be. It is the same reason
    #: `Match.start` sends each player only their OWN role.
    #:
    #: Includes the recipient's own slot. Filtering "everyone but me" is one
    #: comparison on the console and makes this message identical for every
    #: impostor, so the server serialises it once.
    IMPOSTOR_LIST = 0x97
    #: This room is on a different ship now.
    #:
    #: Broadcast to the whole room when the owner changes it, and sent to a
    #: joining console right after ROOM_JOINED. That second use is not
    #: decoration: ROOM_JOINED carries no map, so a console that joined by CODE
    #: had no way to learn one - the room was very likely absent from its room
    #: list (private rooms never appear there), so the client-side fallback of
    #: reading the map off the cached list entry found nothing and the room
    #: screen named the wrong ship until the round actually started.
    MAP_CHANGED = 0x96


class Reject(IntEnum):
    FULL = 1
    NOT_FOUND = 2
    IN_PROGRESS = 3
    SCENE_MISMATCH = 4
    TOO_FEW_PLAYERS = 5


class Role(IntEnum):
    CREW = 0
    IMPOSTOR = 1


ROOM_FLAG_PRIVATE = 0x01
ROOM_FLAG_IN_PROGRESS = 0x02

# Bits 2-3 of a room's flags byte carry which map it will be played on.
#
# Packed into the spare bits rather than given a field of its own because a
# ROOM_RECORD is exactly 16 bytes and fifteen of them fit in one 244-byte packet
# -- one round trip for a whole page of rooms. Widening the record to 17 would
# drop that to fourteen and cost a second round trip on a link that has already
# been the bottleneck of this project twice.
ROOM_MAP_SHIFT = 2
ROOM_MAP_MASK = 0x03
MAP_COUNT = 3


def pack_room_flags(private: bool, in_progress: bool, map_id: int) -> int:
    """The flags byte for a room record."""
    return ((ROOM_FLAG_PRIVATE if private else 0)
            | (ROOM_FLAG_IN_PROGRESS if in_progress else 0)
            | ((map_id & ROOM_MAP_MASK) << ROOM_MAP_SHIFT))

PLAYER_FLAG_ALIVE = 0x01
PLAYER_FLAG_OWNER = 0x02

VOTE_SKIP = 0xFF
NOBODY = 0xFF

# MeetingStart reasons.
MEETING_REPORT = 0
MEETING_BUTTON = 1


class SabotageKind(IntEnum):
    LIGHTS = 0
    COMMS = 1
    REACTOR = 2
    OXYGEN = 3


def pack_name(name: str, length: int = NAME_LEN) -> bytes:
    """Fixed-width, NUL-padded. Truncation is deliberate and silent.

    The console reads a fixed-size field; a longer name would shift every
    following byte, so the limit is enforced here rather than trusted.
    """
    raw = name.encode("ascii", errors="replace")[:length]
    return raw + b"\x00" * (length - len(raw))


def unpack_name(raw: bytes) -> str:
    return raw.split(b"\x00")[0].decode("ascii", errors="replace")


@dataclass(frozen=True, slots=True)
class RoomRecord:
    room_id: int
    player_count: int
    flags: int
    name: str

    def pack(self) -> bytes:
        out = struct.pack("<HBB", self.room_id, self.player_count, self.flags) + pack_name(self.name)
        assert len(out) == ROOM_RECORD
        return out

    @classmethod
    def unpack(cls, raw: bytes) -> "RoomRecord":
        room_id, player_count, flags = struct.unpack_from("<HBB", raw, 0)
        return cls(room_id, player_count, flags, unpack_name(raw[4:16]))

    @property
    def map_id(self) -> int:
        return (self.flags >> ROOM_MAP_SHIFT) & ROOM_MAP_MASK


@dataclass(frozen=True, slots=True)
class PlayerRecord:
    slot: int
    flags: int
    name: str
    colour: int = 0

    def pack(self) -> bytes:
        out = struct.pack("<BB", self.slot, self.flags) + pack_name(self.name) + struct.pack(
            "<BB", self.colour, 0
        )
        assert len(out) == PLAYER_RECORD
        return out

    @classmethod
    def unpack(cls, raw: bytes) -> "PlayerRecord":
        slot, flags = struct.unpack_from("<BB", raw, 0)
        colour = raw[14]
        return cls(slot, flags, unpack_name(raw[2:14]), colour)


# --- server -> client encoders -------------------------------------------


def room_list(rooms: list[RoomRecord], page: int = 0, page_count: int = 1) -> bytes:
    if len(rooms) > ROOMS_PER_PAGE:
        raise ValueError(f"{len(rooms)} rooms exceeds one page ({ROOMS_PER_PAGE})")
    out = struct.pack("<BBBB", S2C.ROOM_LIST, page, page_count, len(rooms))
    out += b"".join(r.pack() for r in rooms)
    assert len(out) <= MAX_PAYLOAD
    return out


def room_joined(room_id: int, your_slot: int, code: str = "") -> bytes:
    return struct.pack("<BHB", S2C.ROOM_JOINED, room_id, your_slot) + pack_name(code, CODE_LEN)


def room_rejected(reason: Reject) -> bytes:
    return struct.pack("<BB", S2C.ROOM_REJECTED, reason)


def room_state(players: list[PlayerRecord], owner_slot: int) -> bytes:
    out = struct.pack("<BBB", S2C.ROOM_STATE, len(players), owner_slot)
    out += b"".join(p.pack() for p in players)
    if len(out) > MAX_PAYLOAD:
        raise ValueError("room state exceeds one packet")
    return out


def game_starting(countdown_frames: int, map_id: int = 0) -> bytes:
    """The round is beginning, on THIS map.

    The map id is appended rather than inserted: byte 1 has always been the
    countdown, and a console built before maps existed reads that byte and
    ignores the rest. The map has to be on the wire at all because every console
    in the room must load the same scene, and the only thing both ends agree on
    is what the server said -- a client that picked its own would put two
    players on two different ships with no error anywhere.
    """
    return struct.pack("<BBB", S2C.GAME_STARTING, min(countdown_frames, 255),
                       map_id & ROOM_MAP_MASK)


def map_changed(map_id: int) -> bytes:
    """This room is on `map_id` now.

    Masked with ROOM_MAP_MASK for the same reason the room-list flags are: the
    map travels as two bits there, so a value that does not survive the mask is
    one the browser could never show back.
    """
    return struct.pack("<BB", S2C.MAP_CHANGED, map_id & ROOM_MAP_MASK)


def role_assigned(role: Role, impostor_count: int) -> bytes:
    return struct.pack("<BBB", S2C.ROLE_ASSIGNED, role, impostor_count)


def task_list(task_ids: list[int]) -> bytes:
    return struct.pack("<BB", S2C.TASK_LIST, len(task_ids)) + bytes(task_ids)


def task_progress(done: int, total: int) -> bytes:
    return struct.pack("<BBB", S2C.TASK_PROGRESS, done, total)


def meeting_start(reason: int, by_slot: int, body_slot: int) -> bytes:
    return struct.pack("<BBBB", S2C.MEETING_START, reason, by_slot, body_slot)


def vote_tally(tally: dict[int, int], votes: dict[int, int] | None = None) -> bytes:
    """The result of a vote: the counts, and then WHO VOTED FOR WHOM.

    The per-voter section is APPENDED after the counts rather than replacing
    them, which is the same backward-compatible shape `game_starting` and
    `game_over` use: a console built before this existed reads the count byte,
    consumes that many pairs and never looks further. A console built after it
    reads a zero length past the end of an old message (`u8` answers 0 there)
    and simply shows no voter faces.

    Counts are not derivable from the detail on the console's side without
    trusting it to do the arithmetic on a message it may have received
    truncated, and the detail is what the results screen needs to draw a face
    beside each candidate. Both is 21 extra bytes at ten players.

    `votes` maps VOTER slot -> the slot they voted for, or VOTE_SKIP. Only
    players who actually voted appear.
    """
    out = struct.pack("<BB", S2C.VOTE_TALLY, len(tally))
    for slot, n in sorted(tally.items()):
        out += struct.pack("<BB", slot, n)
    votes = votes or {}
    out += struct.pack("<B", len(votes))
    for voter, target in sorted(votes.items()):
        out += struct.pack("<BB", voter, target)
    if len(out) > MAX_PAYLOAD:
        raise ValueError("vote tally exceeds one packet")
    return out


def impostor_list(slots: list[int]) -> bytes:
    """The impostor roster. Only ever sent TO an impostor - see S2C.IMPOSTOR_LIST."""
    return struct.pack("<BB", S2C.IMPOSTOR_LIST, len(slots)) + bytes(sorted(slots))


def ejected(slot: int, was_impostor: bool) -> bytes:
    return struct.pack("<BBB", S2C.EJECTED, slot, int(was_impostor))


def sabotage_start(kind: int, timer_frames: int) -> bytes:
    return struct.pack("<BBH", S2C.SABOTAGE_START, kind, timer_frames)


def sabotage_end(kind: int) -> bytes:
    return struct.pack("<BB", S2C.SABOTAGE_END, kind)


def sabotage_tick(timer_frames: int) -> bytes:
    return struct.pack("<BH", S2C.SABOTAGE_TICK, max(0, min(timer_frames, 0xFFFF)))


def meeting_end() -> bytes:
    return struct.pack("<B", S2C.MEETING_END)


def player_died(slot: int) -> bytes:
    return struct.pack("<BB", S2C.PLAYER_DIED, slot)


#: Why a round ended. The winner alone does not explain an ending - "IMPOSTORS
#: WIN" is the same two words whether the reactor blew up, the last two
#: crewmates were murdered, or the crew voted out their own last hope, and a
#: player who cannot tell those apart cannot learn anything from losing.
#:
#: Appended AFTER the winner byte, so a console built before this existed reads
#: the same winner it always did and ignores a byte it does not know about.
class OverReason(IntEnum):
    TASKS = 0       # the crew finished every task
    EJECTED = 1     # the last impostor was voted out
    KILLED = 2      # the impostors reached parity by killing
    SABOTAGE = 3    # a doomsday sabotage ran out of time
    LEFT = 4        # the last impostor disconnected


def game_over(winner: Role, reason: int = OverReason.KILLED) -> bytes:
    return struct.pack("<BBB", S2C.GAME_OVER, winner, reason)


def meeting_tick(voting_open: bool, frames_left: int) -> bytes:
    return struct.pack("<BBH", S2C.MEETING_TICK, 1 if voting_open else 0,
                       min(0xFFFF, max(0, frames_left)))


def cooldown_tick(kill_seconds: int, meeting_seconds: int, meetings_left: int) -> bytes:
    """One player's own clocks, in whole SECONDS.

    Seconds rather than frames because that is what gets displayed, and a byte
    each is enough: everything here is capped well under 255, and this goes to
    every impostor once a second for the length of a round.
    """
    return struct.pack("<BBBB", S2C.COOLDOWN_TICK,
                       min(255, max(0, kill_seconds)),
                       min(255, max(0, meeting_seconds)),
                       min(255, max(0, meetings_left)))


def return_lobby() -> bytes:
    return struct.pack("<B", S2C.RETURN_LOBBY)


def player_vent(slot: int, vented: bool) -> bytes:
    """A player entered (1) or left (0) a vent. Broadcast, because everyone else
    has to hide or restore that avatar."""
    return struct.pack("<BBB", S2C.PLAYER_VENT, slot, 1 if vented else 0)


# --- client -> server decoding -------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    subtype: int
    body: bytes

    def u8(self, index: int = 0) -> int:
        return self.body[index] if index < len(self.body) else 0

    def name(self, offset: int = 0, length: int = NAME_LEN) -> str:
        return unpack_name(self.body[offset : offset + length])

    def u16(self, offset: int = 0) -> int:
        if offset + 2 > len(self.body):
            return 0
        return struct.unpack_from("<H", self.body, offset)[0]


def decode(payload: bytes) -> Message | None:
    """Never raises: this parses bytes from a game console over a serial cable.

    A malformed message is dropped, not fatal - the same posture the framing
    layer takes toward line noise.
    """
    if not payload:
        return None
    return Message(subtype=payload[0], body=payload[1:])
