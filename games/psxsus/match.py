"""One PSXSUS match: roles, tasks, meetings, votes, kills, sabotage, vents.

Everything here is server-authoritative, and that is the entire point. A client
never decides it is an impostor, never decides a vote result, never decides it
killed someone, and never decides the game is over. It asks; this answers.

The one thing this does NOT own is where anybody is standing. Avatar transforms
stay client-authoritative and are merely relayed, because reimplementing the
PlayStation's collision and navigation in Python would mean two physics engines
that must agree forever. See ROOM_PROTOCOL.md.

Deliberately free of asyncio, sockets and psnl_server: a Match is a state machine
you poke with `tick()` and method calls, which is why `test_psxsus_match.py` can
play whole games in a few microseconds with no network at all.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from enum import IntEnum

logger = logging.getLogger(__name__)

# --- tuning ---------------------------------------------------------------
# Frames at 60Hz, because that is the unit the console thinks in.

FPS = 60

#: Impostors by player count. Below 4 players the game is pointless but is
#: allowed to run rather than being a special case.
#:
#: The 3-impostor threshold was 9, which is one player early: the usual split is
#: 1 at 4-6, 2 at 7-9, 3 at 10+. Three impostors in a nine-player lobby leaves
#: six crew, and the impostors win at parity - so they needed only two kills
#: each to take the round before anyone had finished a task.
def impostor_count(players: int) -> int:
    if players >= 10:
        return 3
    if players >= 7:
        return 2
    return 1


TASK_POOL = list(range(1, 13))     # 12 authored task stations
#: Six of the twelve stations each. Four made the crew's win condition land
#: sooner than any social play could develop - the round was decided by walking
#: speed rather than by anybody working anything out.
TASKS_PER_PLAYER = 6

# PACING. All of these came back from the first playtest as far too fast: an
# eight-player lobby could be over in about twenty seconds, with the impostors
# simply killing everyone before the crew had spread out.
#
# The arithmetic of why: impostors win at PARITY, so eight players (2 impostors,
# 6 crew) needs four kills. At the old 10s opening delay and 25s cooldown, two
# impostors have four kills banked by t=35s, and nothing the crew does in that
# window can prevent it. The cooldown is the round's real clock, so it is the
# number that had to move.
KILL_COOLDOWN_FRAMES = 45 * FPS
#: No kill in the opening seconds, so a round does not begin with a body at
#: everyone's feet before the crew have left the spawn.
FIRST_KILL_DELAY_FRAMES = 25 * FPS
#: A meeting is TWO phases, and it always was in Among Us: you argue, and then
#: you vote. Rolling them into one window meant the fastest player decided the
#: round - the first four votes ended it before anyone had said anything, which
#: is the opposite of a social deduction game.
DISCUSS_FRAMES = 25 * FPS
VOTE_FRAMES = 25 * FPS
#: Both are counted down in ONE timer, so `meeting_timer <= VOTE_FRAMES` is
#: "voting is open". One clock cannot drift against itself.
MEETING_FRAMES = DISCUSS_FRAMES + VOTE_FRAMES
SABOTAGE_FRAMES = 45 * FPS

#: Emergency meetings. Body reports are never limited - only the button is, or one
#: player could stall a round indefinitely by mashing it.
EMERGENCY_MEETINGS_PER_PLAYER = 1
MEETING_COOLDOWN_FRAMES = 15 * FPS

#: How long after a sabotage ENDS before another may be started.
#:
#: Without this the only rule was "one at a time", so an impostor could re-trigger
#: the instant a crewmate finished the repair. Lights went out, someone walked to
#: the panel and fixed them, and they went straight back out - reported from play as
#: "Lights Out keeps flickering". It is not a lights bug: LIGHTS is simply the
#: cheapest sabotage to spam, because it has no doomsday timer to run down and no
#: travel for the impostor, so it surfaced there first.
#:
#: 30s is the repair walk plus enough of the round to be worth the walk. Shorter and
#: the crew are doing nothing but repairs; much longer and sabotage stops being a
#: tool. It is deliberately independent of KILL_COOLDOWN_FRAMES: sabotage is map
#: pressure, and tying it to the kill clock would let one impostor's kill silence
#: the other's sabotage.
SABOTAGE_COOLDOWN_FRAMES = 30 * FPS

#: How long the vote RESULT stays on screen before play resumes. The server
#: decides, so every console leaves the meeting at the same moment - a player who
#: had to dismiss it themselves rejoined a round already in progress, and one who
#: never pressed anything was stuck at the table for the rest of the game.
RESULT_FRAMES = 5 * FPS

#: How often the running sabotage countdown is resynced to the consoles. The
#: server owns that clock; the console only smooths between these.
SABOTAGE_TICK_FRAMES = FPS      # once a second
#: Same rate for the per-player kill/meeting cooldowns. One 4-byte message per
#: living player per second is nothing next to the roster, and a cooldown that
#: only updates every other second reads as a stuck clock.
COOLDOWN_TICK_FRAMES = FPS

#: How long a reactor "hold" counts for after the last ping from that player. The
#: console pings while a player stands at a panel, so this only has to outlast a
#: couple of dropped packets.
REACTOR_HOLD_FRAMES = 30

#: Sabotage kinds. Only CRITICAL kinds run a doomsday timer; the others are
#: nuisances that a crewmate clears by walking to a station.
class Sabotage(IntEnum):
    LIGHTS = 0
    COMMS = 1
    REACTOR = 2
    OXYGEN = 3


CRITICAL_SABOTAGES = {Sabotage.REACTOR, Sabotage.OXYGEN}


class Winner(IntEnum):
    CREW = 0
    IMPOSTORS = 1


class Phase(IntEnum):
    PLAYING = 0
    MEETING = 1
    OVER = 2


@dataclass(slots=True)
class Player:
    slot: int
    impostor: bool = False
    alive: bool = True
    tasks: list[int] = field(default_factory=list)
    done: set[int] = field(default_factory=set)
    kill_cooldown: int = 0
    vote: int | None = None      # slot voted for, or VOTE_SKIP
    has_voted: bool = False
    meetings_left: int = EMERGENCY_MEETINGS_PER_PLAYER
    vented: bool = False

    @property
    def tasks_done(self) -> int:
        return len(self.done)


class Match:
    """A single round. Create it when the countdown ends; drop it on game over.

    `emit` is how it speaks: `emit(slot, payload)` sends one player one AppData
    message, and `emit(None, payload)` sends everyone. Injecting it keeps this
    class free of the server and makes every rule below directly testable.
    """

    def __init__(self, slots: list[int], emit, rng: random.Random | None = None) -> None:
        self.emit = emit
        self.rng = rng or random.Random()
        self.phase = Phase.PLAYING
        self.players: dict[int, Player] = {s: Player(slot=s) for s in slots}

        self.meeting_timer = 0
        self._meeting_tick_accum = 0.0
        self._pending_meeting_tick = False
        self.sabotage: Sabotage | None = None
        self.sabotage_timer = 0
        #: Frames until another sabotage may be started. Set when one ENDS, however
        #: it ends - repaired, or cleared by a meeting. See
        #: SABOTAGE_COOLDOWN_FRAMES.
        self.sabotage_cooldown = 0
        self.winner: Winner | None = None
        #: Why the round ended, once it has. See P.OverReason.
        self.over_reason: int | None = None
        self._first_kill_delay = FIRST_KILL_DELAY_FRAMES
        # The emergency button is cold at the start of a round, so nobody can open
        # with a meeting before anything has happened.
        self.meeting_cooldown = MEETING_COOLDOWN_FRAMES
        #: slot -> frames remaining while that player counts as holding a reactor
        #: panel. Two distinct live holders is what fixes a reactor.
        self.reactor_holders: dict[int, int] = {}
        #: Sub-frame remainder carried between ticks, so real time is never lost
        #: to integer truncation. See tick().
        self._frame_accum: float = 0.0
        #: Frames left showing a vote result before MEETING_END is broadcast.
        self.result_timer: int = 0
        self._sabotage_tick_accum: int = 0
        #: Forced to fire on the first tick, so an impostor sees a real number
        #: the moment the round begins rather than a blank for up to a second.
        self._cooldown_tick_accum: int = COOLDOWN_TICK_FRAMES

    def _emit_cooldowns(self) -> None:
        from . import protocol as P

        for slot, p in self.players.items():
            if not p.alive:
                continue
            # A crewmate has no kill clock, and sending them one would be
            # telling them something only an impostor is supposed to know.
            kill = p.kill_cooldown if p.impostor else 0
            if p.impostor and self._first_kill_delay > kill:
                kill = self._first_kill_delay
            self.emit(slot, P.cooldown_tick(
                (kill + FPS - 1) // FPS,
                (self.meeting_cooldown + FPS - 1) // FPS,
                p.meetings_left))

    # --- queries --------------------------------------------------------

    @property
    def alive(self) -> list[Player]:
        return [p for p in self.players.values() if p.alive]

    def is_alive(self, slot: int) -> bool:
        """Whether one slot is still living. A slot the match never knew (someone
        who joined after it started) counts as alive: they are a spectator, not a
        corpse, and reporting them dead would put a body on every screen."""
        p = self.players.get(slot)
        return True if p is None else p.alive

    @property
    def alive_impostors(self) -> list[Player]:
        return [p for p in self.alive if p.impostor]

    @property
    def alive_crew(self) -> list[Player]:
        return [p for p in self.alive if not p.impostor]

    def total_tasks(self) -> int:
        # Ghosts keep finishing tasks, and their work still counts - that is what
        # gives a murdered crewmate something to do besides spectate.
        return sum(len(p.tasks) for p in self.players.values() if not p.impostor)

    def done_tasks(self) -> int:
        return sum(p.tasks_done for p in self.players.values() if not p.impostor)

    # --- setup ----------------------------------------------------------

    def start(self) -> None:
        from . import protocol as P

        slots = list(self.players)
        n_imp = min(impostor_count(len(slots)), max(0, len(slots) - 1))
        impostors = set(self.rng.sample(slots, n_imp)) if n_imp else set()

        for slot, p in self.players.items():
            p.impostor = slot in impostors
            if p.impostor:
                p.kill_cooldown = FIRST_KILL_DELAY_FRAMES
            else:
                p.tasks = self.rng.sample(TASK_POOL, min(TASKS_PER_PLAYER, len(TASK_POOL)))

        # Each player is told ONLY its own role. A client that could see everyone's
        # roles is a client that can cheat, so the crew's copy never contains the
        # impostors' identities in any form.
        for slot, p in self.players.items():
            role = P.Role.IMPOSTOR if p.impostor else P.Role.CREW
            self.emit(slot, P.role_assigned(role, n_imp))
            if p.tasks:
                self.emit(slot, P.task_list(p.tasks))

        # ...but the impostors are told about EACH OTHER, and only they are.
        #
        # Two impostors who cannot recognise each other spend the round afraid of
        # the one person they are safe with - they avoid each other, refuse to
        # vouch, and occasionally walk one another into a vote. The console draws
        # a marker over a fellow impostor, and the reason that marker cannot leak
        # is this loop: a crewmate is never sent the message at all, so there is
        # nothing for a modified client to reveal. A "role == IMPOSTOR" check on
        # a broadcast would have been one edited byte away from showing everyone.
        roster = sorted(impostors)
        if roster:
            payload = P.impostor_list(roster)
            for slot in roster:
                self.emit(slot, payload)

        self._broadcast_progress()
        logger.info("match: %d players, %d impostor(s)", len(slots), n_imp)

    # --- per-frame ------------------------------------------------------

    def tick(self, dt_seconds: float = 1.0 / 30.0) -> None:
        """Advance every match clock by REAL elapsed time.

        Durations stay in 60Hz frames because that is the unit the wire carries
        (`SabotageStart` ships a u16 of frames) and the unit the console renders,
        but what drives them is now the wall clock rather than an assumed tick
        length. The server used to hardcode "2 frames per tick"; when the loop
        overran - a GC pause, a burst of I/O - it dropped the missed ticks and
        those frames simply never happened. The error was silent, one-directional
        and cumulative: over a match, impostors got measurably shorter kill
        cooldowns and the crew got extra real seconds to fix a reactor.

        The fractional remainder is carried, so no time is lost to rounding
        either - 1/30s at 60fps is exactly 2 frames, but nothing depends on that.
        """
        from . import protocol as P

        if self.phase == Phase.OVER:
            return

        self._frame_accum += dt_seconds * FPS
        frames = int(self._frame_accum)
        self._frame_accum -= frames
        if frames <= 0:
            return

        for p in self.players.values():
            if p.kill_cooldown > 0:
                p.kill_cooldown = max(0, p.kill_cooldown - frames)
        if self._first_kill_delay > 0:
            self._first_kill_delay = max(0, self._first_kill_delay - frames)
        if self.meeting_cooldown > 0:
            self.meeting_cooldown = max(0, self.meeting_cooldown - frames)
        # Runs during a meeting too, on purpose: the clock the crew care about is
        # how long the lights stay on, and freezing it would hand the impostors a
        # fresh sabotage the moment play resumed.
        if self.sabotage_cooldown > 0:
            self.sabotage_cooldown = max(0, self.sabotage_cooldown - frames)

        # Tell each player their OWN clocks, once a second.
        #
        # The console cannot work these out for itself - it is never told the
        # cooldown rules and must not be, since a client that knows when the
        # impostor may next kill is a client that can be read. So without this
        # the game simply refused a press and said nothing, and a kill that was
        # too early looked exactly like a kill that was out of range, which
        # looked exactly like a dropped packet.
        self._cooldown_tick_accum += frames
        if self._cooldown_tick_accum >= COOLDOWN_TICK_FRAMES:
            self._cooldown_tick_accum = 0
            self._emit_cooldowns()

        # Reactor panel holds decay, so a player who walked away (or dropped off
        # the link) stops counting almost immediately.
        for slot in list(self.reactor_holders):
            self.reactor_holders[slot] -= frames
            if self.reactor_holders[slot] <= 0:
                del self.reactor_holders[slot]

        # Play resumes when the SERVER says so, not when each player dismisses
        # the result themselves.
        if self.result_timer > 0:
            self.result_timer = max(0, self.result_timer - frames)
            if self.result_timer == 0:
                self.emit(None, P.meeting_end())

        # The doomsday clock PAUSES for a meeting.
        #
        # It used to run through one, on the reasoning that pausing would make a
        # body report a free way to stall a reactor. The arithmetic says the
        # opposite is what actually happens: a meeting is MEETING_FRAMES (50s)
        # and a critical sabotage is SABOTAGE_FRAMES (45s), so a body reported
        # at ANY point during a reactor expires the clock before anyone is
        # released to go and fix it. Not a stall - an automatic impostor win,
        # with no counterplay available to anybody, which is what playtesters
        # ran into.
        #
        # Pausing is not exploitable in the way the old comment feared. The
        # emergency button is already refused during a critical sabotage (see
        # call_meeting), so the only door into a meeting is a body report, and
        # that costs a body: it cannot be repeated at will. The clock resumes
        # with exactly the time that was left on it.
        if (self.sabotage is not None
                and self.sabotage in CRITICAL_SABOTAGES
                and self.phase != Phase.MEETING):
            self.sabotage_timer -= frames
            # Resync the consoles' displayed countdown. Without this each console
            # integrated its own copy and they drifted apart with no correction -
            # in opposite directions, since the console's dt is clamped after a
            # hitch while the server's used to drop whole ticks.
            self._sabotage_tick_accum += frames
            if self._sabotage_tick_accum >= SABOTAGE_TICK_FRAMES:
                self._sabotage_tick_accum = 0
                self.emit(None, P.sabotage_tick(max(0, self.sabotage_timer)))
            if self.sabotage_timer <= 0:
                # Nobody fixed it. This is the impostors' other win condition and
                # the only one that ignores how many people are left alive.
                self._finish(Winner.IMPOSTORS, P.OverReason.SABOTAGE)
                return

        if self.phase == Phase.MEETING:
            self.meeting_timer -= frames
            # The console cannot run this clock itself for the same reason it
            # cannot run the sabotage one: its dt is clamped after a hitch, so
            # two independently integrated timers drift apart in opposite
            # directions. The server owns it and says so once a second.
            self._meeting_tick_accum += frames
            if self._pending_meeting_tick:
                self._pending_meeting_tick = False
                self._meeting_tick_accum = 0.0
                self.emit(None, P.meeting_tick(self.voting_open,
                                               max(0, int(self.meeting_timer))))
            elif self._meeting_tick_accum >= SABOTAGE_TICK_FRAMES:
                self._meeting_tick_accum = 0.0
                self.emit(None, P.meeting_tick(self.voting_open,
                                               max(0, int(self.meeting_timer))))
            if self.meeting_timer <= 0:
                self._resolve_votes()
            return

    # --- player actions -------------------------------------------------

    def task_complete(self, slot: int, task_id: int) -> None:
        from . import protocol as P

        p = self.players.get(slot)
        if p is None or self.phase != Phase.PLAYING:
            return
        # An impostor "doing" a task is faking it: it must never count. Nor may a
        # task be claimed twice, or one station could win the game alone.
        if p.impostor or task_id not in p.tasks or task_id in p.done:
            return
        p.done.add(task_id)
        self._broadcast_progress()
        if self.done_tasks() >= self.total_tasks() and self.total_tasks() > 0:
            self._finish(Winner.CREW, P.OverReason.TASKS)

    def kill(self, slot: int, target_slot: int) -> bool:
        from . import protocol as P

        killer = self.players.get(slot)
        victim = self.players.get(target_slot)
        if self.phase != Phase.PLAYING:
            return False
        if killer is None or victim is None:
            return False
        if not killer.impostor or not killer.alive:
            return False
        if not victim.alive or victim.impostor or victim.slot == killer.slot:
            return False
        if killer.kill_cooldown > 0 or self._first_kill_delay > 0:
            return False

        victim.alive = False
        killer.kill_cooldown = KILL_COOLDOWN_FRAMES
        # A corpse is not in a vent. Without this the victim would stay hidden on
        # every other console forever.
        if victim.vented:
            victim.vented = False
            self.emit(None, P.player_vent(target_slot, False))

        # Told to everyone, because the console has to draw the body somewhere and
        # a body nobody can see cannot be reported. This does leak the MOMENT of a
        # death, which real Among Us does not - an accepted simplification, and
        # the reason it is written down here rather than discovered later.
        self.emit(None, P.player_died(target_slot))
        self._check_win()
        return True

    def report_body(self, slot: int, body_slot: int) -> None:
        from . import protocol as P

        reporter = self.players.get(slot)
        body = self.players.get(body_slot)
        if self.phase != Phase.PLAYING or reporter is None or not reporter.alive:
            return
        if body is None or body.alive:
            return
        self._start_meeting(P.MEETING_REPORT, slot, body_slot)

    def call_meeting(self, slot: int) -> None:
        from . import protocol as P

        caller = self.players.get(slot)
        if self.phase != Phase.PLAYING or caller is None or not caller.alive:
            return
        # The button is dead while the ship is dying: otherwise a crewmate could
        # call a meeting to run out a reactor, or an impostor could call one to
        # stop anybody reaching the panels.
        if self.sabotage in CRITICAL_SABOTAGES:
            return
        if self.meeting_cooldown > 0:
            return
        if caller.meetings_left <= 0:
            return
        caller.meetings_left -= 1
        self._start_meeting(P.MEETING_BUTTON, slot, P.NOBODY)

    def vote(self, slot: int, target: int) -> None:
        from . import protocol as P

        p = self.players.get(slot)
        if self.phase != Phase.MEETING or p is None or not p.alive or p.has_voted:
            return
        # Not during the debate. Otherwise the players who talk least decide the
        # round, and everyone else is voting on an argument that has not happened.
        if not self.voting_open:
            return
        # The dead do not vote, and nobody votes twice - both would be trivial to
        # forge from a modified client, so neither is checked client-side only.
        if target != P.VOTE_SKIP:
            t = self.players.get(target)
            if t is None or not t.alive:
                return
        p.vote = target
        p.has_voted = True
        # Only once voting is actually open, and only if there IS anybody alive
        # to have voted.
        #
        # `all()` over an empty sequence is TRUE, so a meeting in which nobody
        # can vote yet resolves the instant this is evaluated - which is exactly
        # how the debate half ended the whole meeting and the voting half never
        # happened. Both halves of this condition are load-bearing.
        if self.voting_open and self.alive and all(a.has_voted for a in self.alive):
            self._resolve_votes()

    def start_sabotage(self, slot: int, kind: int) -> bool:
        from . import protocol as P

        p = self.players.get(slot)
        if self.phase != Phase.PLAYING or p is None or not p.impostor or not p.alive:
            return False
        if self.sabotage is not None:
            return False
        # Refused, not queued: a sabotage that fires by itself later is one the
        # impostor did not choose the moment for, which is the whole skill of it.
        if self.sabotage_cooldown > 0:
            return False
        try:
            kind = Sabotage(kind)
        except ValueError:
            return False

        self.sabotage = kind
        self.sabotage_timer = SABOTAGE_FRAMES if kind in CRITICAL_SABOTAGES else 0
        self.emit(None, P.sabotage_start(int(kind), self.sabotage_timer))
        return True

    def fix_sabotage(self, slot: int, kind: int) -> None:
        from . import protocol as P

        p = self.players.get(slot)
        if p is None or not p.alive or self.sabotage is None:
            return
        if int(self.sabotage) != kind:
            return

        # The reactor takes TWO people at once - that is the whole point of it.
        # The console pings this while a player stands at a panel; two distinct
        # live holders clear it, one holder just keeps their own hold alive.
        if self.sabotage == Sabotage.REACTOR:
            self.reactor_holders[slot] = REACTOR_HOLD_FRAMES
            if len(self.reactor_holders) < 2:
                return
            self.reactor_holders.clear()

        ended, self.sabotage = self.sabotage, None
        self.sabotage_timer = 0
        # The crew have earned a stretch of quiet. Without this an impostor could
        # re-trigger the instant the repair completed - see
        # SABOTAGE_COOLDOWN_FRAMES.
        self.sabotage_cooldown = SABOTAGE_COOLDOWN_FRAMES
        self.reactor_holders.clear()
        self.emit(None, P.sabotage_end(int(ended)))

    def use_vent(self, slot: int) -> bool:
        """Vents are movement, so the console moves itself; this only rules on
        whether it was allowed to. The hop itself is not broadcast: a vent everyone
        could see would be a vent no impostor would use."""
        p = self.players.get(slot)
        return (
            self.phase == Phase.PLAYING
            and p is not None
            and p.impostor
            and p.alive
        )

    def set_vented(self, slot: int, vented: bool) -> bool:
        """Enter or leave a vent. Unlike the hop, this IS broadcast - a player
        inside a vent is invisible to everyone, which is the whole point, and the
        other consoles cannot hide a sprite they were never told about."""
        from . import protocol as P

        p = self.players.get(slot)
        if p is None or self.phase != Phase.PLAYING:
            return False
        if vented and (not p.impostor or not p.alive):
            return False
        if p.vented == vented:
            return False
        p.vented = vented
        self.emit(None, P.player_vent(slot, vented))
        return True

    def remove(self, slot: int) -> None:
        """A player disconnected. Their absence can end the game."""
        from . import protocol as P

        p = self.players.pop(slot, None)
        if p is None or self.phase == Phase.OVER:
            return
        # Same guard as `vote()`: a disconnect during the debate must not end a
        # meeting nobody has been able to vote in. See the note there on `all()`
        # over an empty sequence.
        if (self.phase == Phase.MEETING and self.voting_open and self.alive
                and all(a.has_voted for a in self.alive)):
            self._resolve_votes()
            return
        # A crew win by disconnection is not an ejection, and saying so is the
        # difference between "we got them" and "they rage quit".
        self._check_win(crew_reason=P.OverReason.LEFT)

    # --- internals ------------------------------------------------------

    def _broadcast_progress(self) -> None:
        from . import protocol as P

        self.emit(None, P.task_progress(self.done_tasks(), self.total_tasks()))

    def _clear_all_vented(self) -> None:
        """Nobody stays invisible across a meeting, a death or the end of a round.
        Broadcast each one so the other consoles put the sprite back."""
        from . import protocol as P

        for p in self.players.values():
            if p.vented:
                p.vented = False
                self.emit(None, P.player_vent(p.slot, False))

    def _start_meeting(self, reason: int, by_slot: int, body_slot: int) -> None:
        from . import protocol as P

        self.phase = Phase.MEETING
        self.meeting_timer = MEETING_FRAMES
        self._meeting_tick_accum = 0.0
        self._pending_meeting_tick = True
        for p in self.players.values():
            p.vote = None
            p.has_voted = False
        # Everyone is summoned to the table, so nobody stays hidden in a vent -
        # otherwise an impostor could sit out the meeting invisible.
        self._clear_all_vented()
        self.reactor_holders.clear()
        # A meeting resets kill cooldowns, or an impostor could walk out of one
        # and immediately kill again.
        for p in self.players.values():
            if p.impostor:
                p.kill_cooldown = KILL_COOLDOWN_FRAMES
        self.emit(None, P.meeting_start(reason, by_slot, body_slot))

    def _resolve_votes(self) -> None:
        from . import protocol as P

        tally: dict[int, int] = {}
        #: Voter -> who they voted for, SKIP included. The counts alone cannot
        #: say who cast them, and "who voted with whom" is most of what a table
        #: argues about on the next body - so the results screen shows a face
        #: beside every candidate. Nothing secret is in here: the vote is public
        #: the moment it resolves, exactly as it is in the game this copies.
        votes: dict[int, int] = {}
        for p in self.alive:
            if p.has_voted and p.vote is not None:
                votes[p.slot] = p.vote
                if p.vote != P.VOTE_SKIP:
                    tally[p.vote] = tally.get(p.vote, 0) + 1
        self.emit(None, P.vote_tally(tally, votes))

        ejected = P.NOBODY
        if tally:
            top = max(tally.values())
            leaders = [s for s, v in tally.items() if v == top]
            # A tie ejects nobody. Ejecting an arbitrary one of them would be a
            # coin flip deciding the round.
            if len(leaders) == 1:
                ejected = leaders[0]

        was_impostor = False
        if ejected != P.NOBODY:
            victim = self.players.get(ejected)
            if victim is not None:
                victim.alive = False
                was_impostor = victim.impostor

        self.emit(None, P.ejected(ejected, was_impostor))
        self.phase = Phase.PLAYING
        self.meeting_timer = 0
        # Hold the result on screen, then tell everyone to go back to the map.
        self.result_timer = RESULT_FRAMES
        # The button goes cold again after every meeting.
        self.meeting_cooldown = MEETING_COOLDOWN_FRAMES
        self._check_win()

    @property
    def voting_open(self) -> bool:
        """Whether the meeting has moved from arguing to voting."""
        return self.phase == Phase.MEETING and self.meeting_timer <= VOTE_FRAMES

    def _check_win(self, crew_reason: int | None = None) -> None:
        from . import protocol as P

        if self.phase == Phase.OVER:
            return
        if not self.alive_impostors:
            # How the last impostor stopped being a threat is the whole story of
            # the round: voted out is a crew that read each other correctly,
            # disconnected is nobody's victory at all. The caller knows which,
            # and it is the only thing that does.
            self._finish(Winner.CREW,
                         P.OverReason.EJECTED if crew_reason is None else crew_reason)
        elif len(self.alive_impostors) >= len(self.alive_crew):
            # Parity is a win: at 1v1 the impostor cannot be voted out and cannot
            # be stopped, so playing it out is theatre.
            self._finish(Winner.IMPOSTORS, P.OverReason.KILLED)

    def _finish(self, winner: Winner, reason: int) -> None:
        from . import protocol as P

        if self.phase == Phase.OVER:
            return
        # Put everyone back on screen before the round ends, or a vented impostor
        # would still be invisible in the lobby that follows.
        self._clear_all_vented()
        self.phase = Phase.OVER
        self.winner = winner
        self.over_reason = reason
        self.emit(None, P.game_over(int(winner), reason))
        logger.info("match over: %s win (%s)", winner.name.lower(),
                    P.OverReason(reason).name.lower())
