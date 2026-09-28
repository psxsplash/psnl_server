"""The live picture the spectator web view renders, assembled from server state.

This is the whole read model. It holds almost nothing of its own, because almost
everything it needs already exists somewhere authoritative:

    where everyone is standing   psnl_server.rooms.Member.avatar (client-authored)
    who is an impostor, alive,
    vented, how many tasks,
    the meeting clock, sabotage  games.psxsus.match.Match
    who is in which room, owner,
    which ship                   PSXSUSGame

Reading those directly is what makes the view honest: there is no second copy of
the game state to drift out of agreement with the first.

Four things genuinely are NOT recoverable that way, and they are exactly what
this module stores:

1. **Where a body is lying.** The server only ever knows one position per slot,
   and after a kill that slot's console is reporting its GHOST. So the victim's
   last live position is captured at the moment of death - which is where they
   fell, accurate to one snapshot.
2. **Who called the meeting, and over whose body.** `Match` emits it and keeps
   nothing.
3. **The vote tally and the ejection.** Same: emitted, then discarded.
4. **How the last round ended.** `_end_match` drops the Match, so without this
   the results screen the players are still looking at has already vanished from
   the spectator's view.

All four arrive through ONE seam: `Match.emit` is the only way a match speaks,
so `observe()` taps it and decodes what it needs. Nothing in `match.py` knows
this module exists, and nothing had to be added to it.
"""

from __future__ import annotations

import json
import logging
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from .. import protocol as P
from ..match import FPS, Match, Sabotage
from .stats import GameResult, Stats

logger = logging.getLogger(__name__)

MAPS_DIR = Path(__file__).resolve().parent / "static" / "maps"

#: fp12: the console sends `psyqo::FixedPoint::value`, and one world pixel is
#: 4096 of them (networkmanager.cpp buildAndSendSnapshot; the console's own Lua
#: divides by the same number in `toPx`). Getting this wrong does not look like a
#: scale bug - it puts every avatar four thousand tiles off the map.
FP12_ONE = 4096.0

#: The per-slot avatar tint, copied from `COLOURS` in `Assets/Lua/scene_game.lua`
#: and indexed the same way (`((slot - 1) % #COLOURS) + 1`). It is duplicated
#: rather than derived because the Lua is on a disc and this is on a server;
#: `tests/psxsus/test_spectator.py` asserts the two lists are identical, which is
#: the only way to keep a copy honest.
COLOURS = [
    (255, 40, 40), (40, 80, 255), (40, 200, 40), (255, 130, 200),
    (255, 150, 40), (240, 240, 60), (60, 60, 60), (240, 240, 240),
    (140, 60, 220), (60, 220, 220),
]

#: How many events a room keeps for the spectator's feed.
EVENT_LOG = 40

#: How long a finished round stays visible after the Match is dropped. The
#: console holds its results screen until the owner presses on, so a spectator
#: whose view emptied the instant the last kill landed would never see who won.
RESULT_LINGER = 90.0


def colour_for(slot: int) -> tuple[int, int, int]:
    return COLOURS[(slot - 1) % len(COLOURS)]


@cache
def map_titles() -> dict[int, str]:
    """Ship names, read from the generated map files rather than restated.

    `MAP_NAMES` already exists in the console's Lua and in three merged copies
    of it; a fifth here, in a language neither of those can check, is how a
    rename ends up showing the wrong ship on one screen. `make_web.py` writes
    the title into each map, so the browser is told rather than assuming.
    """
    titles: dict[int, str] = {}
    for path in sorted(MAPS_DIR.glob("map*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            titles[int(data["id"])] = str(data["title"])
        except Exception:
            logger.warning("spectator: %s is not a readable map", path, exc_info=True)
    if not titles:
        logger.warning("spectator: no maps in %s - run PSXSUS/Art/make_web.py", MAPS_DIR)
    return titles


@dataclass(slots=True)
class RoomWatch:
    """The per-room extras that no authoritative object keeps."""

    started_at: float = field(default_factory=time.time)
    #: slot -> (x, z) in world pixels, where that player was killed.
    bodies: dict[int, tuple[float, float]] = field(default_factory=dict)
    #: The meeting in progress, if any: reason/by/body from MEETING_START.
    meeting: dict | None = None
    #: The last resolved vote, kept until the next meeting starts.
    tally: dict[int, int] = field(default_factory=dict)
    ejected: dict | None = None
    #: The finished round, kept for RESULT_LINGER so the results screen the
    #: players are looking at is also visible here.
    over: dict | None = None
    over_at: float = 0.0
    events: deque = field(default_factory=lambda: deque(maxlen=EVENT_LOG))
    #: Bumped on every event, so the live feed can tell whether the log is worth
    #: resending. See `app.SpectatorWeb._stream_room`.
    seq: int = 0

    def event(self, what: str, **fields) -> None:
        """One line for the spectator's feed.

        The parameter is `what`, not `kind`, because a sabotage event carries a
        sabotage KIND of its own - and `event("sabotage", kind=...)` is a
        TypeError for "multiple values for argument 'kind'", which `observe`
        catches and logs. The feed simply lost every sabotage.
        """
        self.seq += 1
        self.events.appendleft({"t": time.time(), "kind": what, **fields})


class Spectator:
    """Read model + the taps that feed it. Owned by `PSXSUSGame`."""

    def __init__(self, game, stats_path=None) -> None:
        self.game = game
        self.stats = Stats(stats_path)
        self._watch: dict[int, RoomWatch] = {}

    # --- taps -----------------------------------------------------------

    def match_started(self, room, match: Match) -> None:
        """A round has begun. Called AFTER `Match.start`, which is what assigns
        the roles - see the note at the call site."""
        watch = RoomWatch()
        self._watch[room.room_id] = watch
        watch.event("start",
                    players=len(match.players),
                    impostors=sum(1 for p in match.players.values() if p.impostor),
                    map=self.game._maps.get(room.room_id, 0))

    def returned_to_lobby(self, room_id: int) -> None:
        """The room went back to the lobby, so stop showing the result.

        A finished round lingers for RESULT_LINGER so that a spectator sees the
        same results screen the players do. But the owner can dismiss it early,
        and then the room really is back in the lobby - without this the room
        list went on saying "Round over" for the rest of the ninety seconds.
        """
        watch = self._watch.get(room_id)
        if watch is not None:
            watch.over = None
            watch.over_at = 0.0
            watch.bodies.clear()
            watch.meeting = None
            watch.ejected = None

    def observe(self, room, slot, payload: bytes) -> None:
        """Everything the Match says, as it says it.

        Wrapped around `emit` in `PSXSUSGame._begin_game`. Broadcasts (slot None)
        are the interesting ones; per-slot messages are a player's private view
        (their role, their tasks, their cooldowns) and are read from the Match
        directly instead, where they are not scattered across ten copies.

        Never raises. A spectator page is not worth taking a round down for, so a
        payload this cannot parse is dropped exactly the way the console drops
        one it does not recognise.
        """
        watch = self._watch.get(room.room_id)
        if watch is None or slot is not None or not payload:
            return
        try:
            self._observe(room, watch, payload)
        except Exception:
            logger.exception("spectator: could not read a %#x from room %d",
                             payload[0], room.room_id)

    def _observe(self, room, watch: RoomWatch, payload: bytes) -> None:
        subtype = payload[0]

        if subtype == P.S2C.PLAYER_DIED and len(payload) >= 2:
            victim = payload[1]
            member = room.members.get(victim)
            if member is not None:
                # The LAST LIVE POSITION is the body. From the next snapshot on,
                # that same slot is reporting a ghost walking away from it.
                watch.bodies[victim] = (
                    member.avatar.px / FP12_ONE, member.avatar.pz / FP12_ONE
                )
            watch.event("died", slot=victim)

        elif subtype == P.S2C.MEETING_START and len(payload) >= 4:
            reason, by_slot, body_slot = payload[1], payload[2], payload[3]
            watch.meeting = {"reason": reason, "by": by_slot, "body": body_slot}
            watch.tally = {}
            watch.ejected = None
            # A meeting collects every body on the ship, so none of them are on
            # the floor any more - the console does the same when it teleports
            # everyone to the table.
            watch.bodies.clear()
            watch.event("meeting", reason=reason, by=by_slot, body=body_slot)

        elif subtype == P.S2C.VOTE_TALLY and len(payload) >= 2:
            count = payload[1]
            tally = {}
            for i in range(count):
                off = 2 + i * 2
                if off + 2 > len(payload):
                    break
                tally[payload[off]] = payload[off + 1]
            watch.tally = tally
            watch.event("tally", tally=tally)

        elif subtype == P.S2C.EJECTED and len(payload) >= 3:
            watch.ejected = {"slot": payload[1], "wasImpostor": bool(payload[2])}
            watch.event("ejected", slot=payload[1], wasImpostor=bool(payload[2]))

        elif subtype == P.S2C.MEETING_END:
            watch.meeting = None
            watch.event("meetingEnd")

        elif subtype == P.S2C.SABOTAGE_START and len(payload) >= 4:
            kind, frames = payload[1], struct.unpack_from("<H", payload, 2)[0]
            watch.event("sabotage", sabotage=int(Sabotage(kind)),
                        name=Sabotage(kind).name, seconds=frames // FPS)

        elif subtype == P.S2C.SABOTAGE_END and len(payload) >= 2:
            watch.event("sabotageEnd", sabotage=payload[1],
                        name=Sabotage(payload[1]).name)

        elif subtype == P.S2C.PLAYER_VENT and len(payload) >= 3:
            watch.event("vent", slot=payload[1], vented=bool(payload[2]))

        elif subtype == P.S2C.GAME_OVER and len(payload) >= 3:
            watch.over = {"winner": payload[1], "reason": payload[2]}
            watch.over_at = time.time()
            watch.event("over", winner=payload[1], reason=payload[2])

    def match_ended(self, room_id: int, match: Match) -> None:
        """Called from `_end_match`, while the Match is still in hand.

        Only a round with a WINNER is counted. A match dropped because everyone
        left never finished, and recording it would move the impostor win rate
        by an event that had nothing to do with either side playing well."""
        watch = self._watch.get(room_id)
        if match.winner is None:
            return
        room = self.game._room(room_id)
        self.stats.record(GameResult(
            ended_at=time.time(),
            map_id=self.game._maps.get(room_id, 0),
            # `match.players` shrinks as people disconnect, so this is who was
            # there at the END. That is the honest number for "how big was this
            # game" - a ten-player round that four people quit was not a
            # ten-player round.
            players=len(match.players),
            impostors=sum(1 for p in match.players.values() if p.impostor),
            winner=int(match.winner),
            reason=int(match.over_reason or 0),
            duration=round(time.time() - (watch.started_at if watch else time.time()), 1),
            room_name=room.name if room is not None else "",
        ))

    def room_closed(self, room_id: int) -> None:
        self._watch.pop(room_id, None)

    # --- reporting ------------------------------------------------------

    def rooms(self) -> list[dict]:
        """Every PUBLIC room, for the front page.

        Private rooms are absent on purpose and that is the opt-out: a room made
        private is one whose players have said they do not want strangers, and
        spectating is a stranger. Join codes are never reported for the same
        reason - a public list of live codes is a one-click way to walk into
        somebody's game.
        """
        server = self.game.server
        if server is None:
            return []
        out = []
        for room in server.rooms.public_rooms():
            if not room.members:
                continue
            match = self.game._matches.get(room.room_id)
            watch = self._watch.get(room.room_id)
            map_id = self.game._maps.get(room.room_id, 0)
            out.append({
                "id": room.room_id,
                "name": room.name,
                "players": room.player_count,
                "map": map_id,
                "mapName": map_titles().get(map_id, f"MAP {map_id + 1}"),
                "inProgress": room.in_progress,
                "phase": self._phase_name(match, watch),
                "countdown": self._countdown_seconds(room.room_id),
                "elapsed": round(time.time() - watch.started_at, 1) if watch and match else 0,
                "roster": [
                    {"slot": m.slot, "name": m.name, "colour": colour_for(m.slot)}
                    for m in sorted(room.members.values(), key=lambda m: m.slot)
                ],
            })
        out.sort(key=lambda r: (not r["inProgress"], -r["players"], r["id"]))
        return out

    def room_state(self, room_id: int) -> dict | None:
        """One room, in full, for the live view."""
        server = self.game.server
        if server is None:
            return None
        room = server.rooms.get(room_id)
        if room is None or room.private or not room.members:
            return None

        match = self.game._matches.get(room_id)
        watch = self._watch.get(room_id)
        owner = self.game._owners.get(room_id, 0)

        players = []
        for member in sorted(room.members.values(), key=lambda m: m.slot):
            slot = member.slot
            p = match.players.get(slot) if match else None
            players.append({
                "slot": slot,
                "name": member.name,
                "colour": colour_for(slot),
                "owner": slot == owner,
                # Positions in WORLD PIXELS, the unit the tilemap is in, so the
                # browser places an avatar with the same arithmetic the console
                # does (`x / 16` is the tile).
                "x": round(member.avatar.px / FP12_ONE, 2),
                "z": round(member.avatar.pz / FP12_ONE, 2),
                "alive": True if p is None else p.alive,
                # A spectator is omniscient - seeing who the impostors are is the
                # entire point of the view, and the reason the fair-play warning
                # below exists.
                "impostor": bool(p.impostor) if p else False,
                "vented": bool(p.vented) if p else False,
                "tasks": len(p.tasks) if p else 0,
                "tasksDone": p.tasks_done if p else 0,
                "voted": bool(p.has_voted) if p else False,
                "votedFor": (p.vote if p and p.has_voted else None),
                "meetingsLeft": p.meetings_left if p else 0,
                "killCooldown": round(p.kill_cooldown / FPS, 1) if p else 0,
            })

        map_id = self.game._maps.get(room_id, 0)
        state = {
            "id": room.room_id,
            "name": room.name,
            "map": map_id,
            "mapName": map_titles().get(map_id, f"MAP {map_id + 1}"),
            "inProgress": room.in_progress,
            "phase": self._phase_name(match, watch),
            "countdown": self._countdown_seconds(room_id),
            "players": players,
            "bodies": [
                {"slot": s, "x": round(x, 2), "z": round(z, 2), "colour": colour_for(s)}
                for s, (x, z) in (watch.bodies.items() if watch else ())
            ],
            "events": list(watch.events) if watch else [],
            "eventSeq": watch.seq if watch else 0,
            "now": time.time(),
        }

        if match is not None:
            state["match"] = {
                "elapsed": round(time.time() - watch.started_at, 1) if watch else 0,
                "tasksDone": match.done_tasks(),
                "tasksTotal": match.total_tasks(),
                "impostors": sum(1 for p in match.players.values() if p.impostor),
                "impostorsAlive": len(match.alive_impostors),
                "crewAlive": len(match.alive_crew),
                "sabotage": (
                    {
                        "kind": int(match.sabotage),
                        "name": Sabotage(match.sabotage).name,
                        # Only the critical ones run a doomsday clock; the others
                        # report 0, which is not "expired" but "no timer".
                        "seconds": max(0, match.sabotage_timer) // FPS,
                    }
                    if match.sabotage is not None else None
                ),
                "sabotageCooldown": max(0, match.sabotage_cooldown) // FPS,
            }
            if match.phase == match.phase.MEETING and watch and watch.meeting:
                state["meeting"] = {
                    **watch.meeting,
                    "votingOpen": match.voting_open,
                    "seconds": max(0, int(match.meeting_timer)) // FPS,
                    "tally": watch.tally,
                    "ejected": watch.ejected,
                }
            elif watch and watch.ejected and match.result_timer > 0:
                # The result is on screen: the meeting is over but nobody has
                # been released yet, which the console shows and so should this.
                state["result"] = {"tally": watch.tally, "ejected": watch.ejected}

        if watch and watch.over and time.time() - watch.over_at < RESULT_LINGER:
            state["over"] = watch.over

        return state

    def stats_snapshot(self) -> dict:
        server = self.game.server
        live = {
            "connected": len(server.sessions) if server else 0,
            "rooms": sum(1 for r in (server.rooms.public_rooms() if server else []) if r.members),
            "inGame": sum(1 for r in (server.rooms.all_rooms() if server else []) if r.in_progress),
            "playing": sum(
                r.player_count for r in (server.rooms.all_rooms() if server else [])
                if r.in_progress
            ),
        }
        return {
            "live": live,
            # Names for the enums the numbers are keyed by, so the page never
            # holds a private copy of an ordering the server owns.
            "reasonNames": {int(r): r.name for r in P.OverReason},
            "mapNames": map_titles(),
            **self.stats.snapshot(),
        }

    # --- fair play ------------------------------------------------------

    def fair_play(self, ip: str) -> dict:
        """Is this spectator sitting at a console that is currently in a room?

        Spectating shows who the impostors are. Doing that from the machine you
        are playing on is cheating, and doing it from the same house is close
        enough that saying nothing would be the wrong choice - so the page warns
        rather than blocks.

        WARN, DO NOT BLOCK, and the reason is in how a PlayStation reaches this
        server: real hardware connects through the bridge process on somebody's
        PC, so a console's peer address IS a PC's address, and it is routinely
        the same PC someone else in the house browses from. Behind one NAT every
        player and every spectator shares an address. Blocking on that evidence
        would lock honest people out of their own spectator page; a warning
        costs nothing and is right about the case that matters.
        """
        server = self.game.server
        if not ip or server is None:
            return {"warn": False, "rooms": [], "sessions": 0}
        rooms, sessions = [], 0
        for session in server.sessions:
            peer = getattr(session, "peer", None)
            if not peer or peer[0] != ip:
                continue
            sessions += 1
            room = getattr(session, "room", None)
            if room is not None:
                rooms.append(room.room_id)
        return {"warn": bool(rooms), "rooms": sorted(set(rooms)), "sessions": sessions}

    # --- helpers --------------------------------------------------------

    def _phase_name(self, match: Match | None, watch: RoomWatch | None) -> str:
        if match is None:
            if watch and watch.over and time.time() - watch.over_at < RESULT_LINGER:
                return "over"
            return "lobby"
        if match.phase == match.phase.MEETING:
            return "meeting"
        if match.phase == match.phase.OVER:
            return "over"
        return "playing"

    def _countdown_seconds(self, room_id: int) -> int:
        frames = self.game._countdowns.get(room_id)
        return int(frames // FPS) + 1 if frames else 0
