"""PSXSUS's server-side rules, plugged into the game-agnostic PSNL server.

    python -m psnl_server --game PSXSUS/Server/psxsus_game.py:PSXSUSGame

This file owns the LOBBY: who is in which room, who owns a room, and when a game
starts. Once a round begins, `psxsus_match.Match` owns the rules and this becomes
the wiring between it and the sockets.

It does NOT own where anyone is standing - see ROOM_PROTOCOL.md on the authority
split, which is the one design decision here worth reading before changing
anything.

It lives outside `psnl_server` on purpose. Rooms with join codes and an impostor
count are PSXSUS's problem; the next game built on psxsplash gets to have a
different lobby, or none.
"""

from __future__ import annotations

import logging
import time

#: Shortest gap between two regenerations of the roster for the SAME console.
#:
#: A console that just failed to receive the largest message in the game across
#: eight retransmissions has a link that cannot carry it right now. Rebuilding it
#: immediately does not help that console and takes wire from everyone else; the
#: honest response is to wait for the link to drain. Long enough to be sure the
#: previous attempt has finished dying, short enough that a console recovering
#: from a hiccup still gets its roster well inside a round.
ROSTER_REGEN_COOLDOWN = 5.0
from psnl_server.game import Game
from psnl_server.rooms import MAX_NAME_LEN, MAX_PLAYERS, RoomFullError, SceneMismatchError

# One definition of these bytes, shared with the tests and with the Lua half the
# console runs. `protocol.py` is the contract; nothing else may restate it.
from . import protocol as P
from .match import FPS, Match
from .web import SpectatorWeb, config_from_env as web_config_from_env

logger = logging.getLogger(__name__)

# Frames at 60Hz. Long enough to read "starting", short enough not to annoy.
START_COUNTDOWN_FRAMES = 180

#: A round needs enough people to be a game at all: one impostor plus enough crew
#: that the impostor is not instantly at parity (which would end the round on the
#: first kill). Among Us uses 4 for the same reason.
MIN_PLAYERS_TO_START = 4


class PSXSUSGame(Game):
    name = "psxsus"

    # The lobby is the whole point: a console that has just connected has not
    # chosen a room yet, so dropping it into one would put it in a game before
    # the player ever saw the list.
    auto_join = False

    def __init__(self) -> None:
        self.server = None
        # slot is meaningless before a room is joined, so sessions are keyed by
        # identity, not slot.
        self._names: dict[int, str] = {}
        self._owners: dict[int, int] = {}      # room_id -> owner slot
        # room_id -> which map the round will be played on. Kept HERE and not on
        # psnl_server's Room for the same reason ownership is: the PSNL server is
        # game-agnostic, and "which of PSXSUS's three ships" is not something a
        # relay should know about. Same lifecycle as _owners.
        self._maps: dict[int, int] = {}
        self._countdowns: dict[int, int] = {}  # room_id -> frames remaining
        self._matches: dict[int, Match] = {}   # room_id -> the round in progress
        #: Rooms whose roster changed this tick. Broadcasting per join sends one
        #: ROOM_STATE per joiner, and on a stop-and-wait reliable channel a burst
        #: of those can overflow the queue and be dropped - which is exactly how
        #: two players joining in the same tick end up invisible to each other.
        #: Coalescing to one broadcast per tick sends the FINAL roster once.
        self._roster_dirty: set[int] = set()
        #: id(session) -> when its roster was last regenerated after a delivery
        #: failure. See on_reliable_dropped: without this, a bad link regenerates
        #: the biggest message in the game forever.
        self._roster_regen_at: dict[int, float] = {}
        #: The spectator web view, or None when it is switched off - which is the
        #: default, because it shows every impostor to anyone who can reach the
        #: port. See web/app.py. None here is load-bearing: every call site below
        #: is guarded, so a server without it does exactly what it did before
        #: this existed, at exactly the same cost.
        self.web = None

    # --- lifecycle ------------------------------------------------------

    async def on_start(self) -> None:
        config = web_config_from_env()
        if config is None:
            return
        self.web = SpectatorWeb(self, **config)
        try:
            await self.web.start()
        except OSError:
            # A port that is taken is the common case (two servers on one box),
            # and it is not worth refusing to run a game over. Drop the view.
            logger.exception("psxsus: spectator web view could not start")
            self.web = None

    async def on_stop(self) -> None:
        if self.web is not None:
            await self.web.stop()
            self.web = None

    def on_connect(self, session) -> None:
        self._names.setdefault(id(session), "")

    def on_disconnect(self, session) -> None:
        # These two are SESSION state, not room state, so they are dropped here and
        # NOT in _release_room: leaving a room is not leaving the server.
        #
        # `_h_leave_room` used to call this whole method, which wiped the player's
        # name on the way back to the room list. The console sends SetName once, at
        # boot, so the name never came back - every room they joined afterwards
        # listed them as blank, in the lobby and in the game. That was "usernames
        # disappearing" and the "never coming back" half of the leave bug.
        self._names.pop(id(session), None)
        # Keyed by id(), which CPython reuses once the object is freed. Dropping it
        # here keeps a new session from inheriting a departed one's cooldown, and
        # keeps the dict from growing for the life of the process.
        self._roster_regen_at.pop(id(session), None)
        self._release_room(session)

    def _release_room(self, session) -> None:
        """Room bookkeeping for a session that is leaving its room, whether it is
        going back to the room list or going away entirely.

        Everything here is keyed by ROOM, so it is correct for both. Anything keyed
        by SESSION belongs in `on_disconnect` instead - see the note there.
        """
        room = session.room
        if room is None:
            return
        leaving_slot = session.slot
        # session.close() removes the member; do the bookkeeping that depends on
        # the room still existing first.
        # A player vanishing can end a round - the last impostor rage-quitting is
        # a crew win, and it must not leave everyone waiting for a vote that can
        # never complete.
        match = self._matches.get(room.room_id)
        if match is not None:
            match.remove(leaving_slot)
            if match.phase == match.phase.OVER:
                self._end_match(room.room_id)

        remaining = [s for s in room.members if s != leaving_slot]
        if self._owners.get(room.room_id) == leaving_slot and remaining:
            # The owner left mid-lobby. Hand the room to whoever is left rather
            # than stranding everyone in a room that can never be started.
            self._owners[room.room_id] = min(remaining)
            logger.info("room %d: owner left, slot %d now owns it",
                        room.room_id, self._owners[room.room_id])
        if not remaining:
            self._owners.pop(room.room_id, None)
            self._maps.pop(room.room_id, None)
            self._countdowns.pop(room.room_id, None)
            self._matches.pop(room.room_id, None)
            self._roster_dirty.discard(room.room_id)
            if self.web:
                self.web.spectator.room_closed(room.room_id)
        else:
            # Everyone still here has to see that they left.
            self._mark_roster_dirty(room)

    def tick(self, dt_seconds: float = 1.0 / 30.0) -> None:
        # Countdowns are in 60Hz frames (the wire carries them that way), but how
        # many elapsed is measured, not assumed. The old hardcoded "-2 per tick"
        # meant every overrun of the server loop silently gave the countdown free
        # time back.
        elapsed_frames = dt_seconds * FPS

        for room_id, frames in list(self._countdowns.items()):
            room = self._room(room_id)
            if room is None or not room.members:
                self._countdowns.pop(room_id, None)
                continue
            frames -= elapsed_frames
            if frames > 0:
                self._countdowns[room_id] = frames
                continue
            self._countdowns.pop(room_id, None)
            # RE-CHECK, because the count is only enforced when the countdown
            # STARTS and people leave during it. A room that pressed start with
            # four and lost two began a round with two: one impostor and one crew
            # is already parity, so the round was over before anybody moved. That
            # is the "leaving a lobby prematurely screws up the game" report.
            if room.player_count < MIN_PLAYERS_TO_START:
                logger.info("room %d: start aborted, %d player(s) < %d after the "
                            "countdown", room_id, room.player_count,
                            MIN_PLAYERS_TO_START)
                # Everyone, not just the owner: every console is showing a
                # countdown and needs to be let out of it.
                payload = P.room_rejected(P.Reject.TOO_FEW_PLAYERS)
                for m in room.members.values():
                    m.session.send_app_data(payload)
                self._mark_roster_dirty(room)
                continue
            self._begin_game(room)

        # Match clocks: kill cooldowns, the vote timer, the sabotage doomsday.
        for room_id, match in list(self._matches.items()):
            room = self._room(room_id)
            if room is None or not room.members:
                self._matches.pop(room_id, None)
                continue
            match.tick(dt_seconds)
            if match.phase == match.phase.OVER:
                self._end_match(room_id)

        # One roster broadcast per room per tick, carrying whatever the roster
        # ended up as after every join/leave/rename this tick.
        self._flush_roster_broadcasts()

    # --- dispatch -------------------------------------------------------

    def on_app_data(self, session, payload: bytes) -> bool:
        msg = P.decode(payload)
        if msg is None:
            return True  # empty AppData: consumed, not relayed as noise

        handler = _HANDLERS.get(msg.subtype)
        if handler is None:
            # Not ours - let the engine's default relay carry it to the room.
            # Gameplay subtypes (0x10+) land here until M9 implements them.
            return False
        try:
            handler(self, session, msg)
        except Exception:
            logger.exception("psxsus: handler for subtype %#x failed", msg.subtype)
        return True

    # --- helpers --------------------------------------------------------

    def _room(self, room_id: int):
        return self.server.rooms.get(room_id) if self.server else None

    def _name_of(self, session) -> str:
        return self._names.get(id(session), "")

    def _reject(self, session, reason: P.Reject) -> None:
        session.send_app_data(P.room_rejected(reason))

    def _mark_roster_dirty(self, room) -> None:
        """Queue a roster broadcast for the end of this tick instead of sending one
        immediately. See `_roster_dirty`."""
        if room is not None:
            self._roster_dirty.add(room.room_id)

    def _flush_roster_broadcasts(self) -> None:
        for room_id in list(self._roster_dirty):
            room = self._room(room_id)
            if room is not None and room.members:
                self._broadcast_room_state(room)
        self._roster_dirty.clear()

    def _room_state_payload(self, room, owner: int) -> bytes:
        """The roster as bytes.

        Aliveness comes from the MATCH when a round is running, not from a blanket
        ALIVE flag. A late roster that marked everyone alive would resurrect the
        dead on every console that received it - ghosts would reappear as valid
        kill targets and vote candidates. Outside a round there is no match, and
        everyone is alive by definition.
        """
        match = self._matches.get(room.room_id)
        players = []
        for m in sorted(room.members.values(), key=lambda m: m.slot):
            alive = True if match is None else match.is_alive(m.slot)
            flags = (P.PLAYER_FLAG_ALIVE if alive else 0) | (
                P.PLAYER_FLAG_OWNER if m.slot == owner else 0
            )
            players.append(P.PlayerRecord(
                slot=m.slot,
                flags=flags,
                name=m.name,
                colour=m.slot,  # one colour per slot; distinct by construction
            ))
        return P.room_state(players, owner)

    def on_reliable_dropped(self, session, payload: bytes) -> None:
        """Regenerate anything the console cannot do without - ONCE, and only for
        the console that lost it.

        The roster is the one message whose loss is unrecoverable from the
        console's side: it carries the player list, and a console that never
        receives it shows an empty room with nothing to retry against.

        BUT REGENERATION IS AN AMPLIFIER, and this is where it ran away. It used to
        call `_mark_roster_dirty(room)`, which broadcasts the roster to EVERY
        MEMBER - so one struggling console turned a single failed delivery into N
        fresh copies of the largest message in the game, aimed at a wire that had
        just proved it could not carry one. There was also no cooldown, so the
        cycle repeated for as long as the link stayed bad: eight retransmissions,
        give up, regenerate, eight more, forever.

        MEASURED before this fix: 66.2 KB pushed to a console whose entire return
        path had managed 6.7 KB in the same window - about twice what the cable can
        physically carry, which is congestion collapse, not a slow link.

        So: only the session that actually lost it, and not more often than
        ROSTER_REGEN_COOLDOWN. The other members already have a correct roster;
        nothing about this event is news to them.
        """
        if not payload or payload[0] != P.S2C.ROOM_STATE:
            return
        room = session.room
        if room is None or not room.members:
            return

        now = time.monotonic()
        last = self._roster_regen_at.get(id(session), 0.0)
        if now - last < ROSTER_REGEN_COOLDOWN:
            logger.info(
                "room %d: roster to %s dropped again within %.1fs, not regenerating "
                "(the link cannot carry it; piling on makes that worse)",
                room.room_id, session.peer, ROSTER_REGEN_COOLDOWN,
            )
            return
        self._roster_regen_at[id(session)] = now

        logger.info("room %d: roster to %s was dropped, resending to it alone",
                    room.room_id, session.peer)
        owner = self._owners.get(room.room_id, 0)
        session.send_app_data(self._room_state_payload(room, owner))

    def replicates(self, room) -> bool:
        """Only a room that is actually playing gets avatar snapshots.

        In the lobby nobody has an avatar yet - the scene draws a text roster and
        maps no remote actors, so every record we send is discarded on arrival. On
        a 115200 serial console that traffic is not merely wasted, it is actively
        harmful: it consumed over half the link and starved the room roster, which
        is the largest reliable message in the game and the one thing the lobby
        cannot proceed without. Consoles sat at "Room - Code:" with an empty player
        list because the wire was full of positions of players standing still.
        """
        return room.in_progress

    def _broadcast_room_state(self, room) -> None:
        owner = self._owners.get(room.room_id, 0)
        payload = self._room_state_payload(room, owner)
        for m in room.members.values():
            m.session.send_app_data(payload)

    def _begin_game(self, room) -> None:
        room.in_progress = True
        logger.info("room %d: game starting with %d players", room.room_id, room.player_count)

        # `emit` is the Match's only way to speak, which is what keeps every rule
        # in psxsus_match.py testable without a socket in sight.
        #
        # It is also the ONE place the spectator view taps. A few things a
        # spectator needs are announced and then forgotten by the Match - who
        # called a meeting, the vote tally, where a body fell - and listening
        # here catches all of them without `match.py` growing a notion that
        # anybody is watching. See web/spectator.py.
        observe = self.web.spectator.observe if self.web else None

        def emit(slot, payload, _room=room):
            if observe is not None:
                observe(_room, slot, payload)
            if slot is None:
                for m in _room.members.values():
                    m.session.send_app_data(payload)
                return
            member = _room.members.get(slot)
            if member is not None:
                member.session.send_app_data(payload)

        match = Match(sorted(room.members), emit)
        self._matches[room.room_id] = match
        match.start()
        if self.web:
            # AFTER start(), which is what assigns the roles - told before it,
            # the spectator's opening line reported every round as having zero
            # impostors. Nothing start() emits is anything `observe` records
            # (roles and task lists are per-slot and read from the Match
            # directly), so there is nothing to miss by waiting.
            self.web.spectator.match_started(room, match)

    def _end_match(self, room_id: int) -> None:
        match = self._matches.pop(room_id, None)
        # While the Match is still in hand: it is the only thing that knows who
        # won and how, and this is the last moment it exists. Only a round that
        # actually reached a winner is counted - see Spectator.match_ended.
        if match is not None and self.web:
            self.web.spectator.match_ended(room_id, match)
        room = self._room(room_id)
        if room is not None:
            # Back to the lobby: the room becomes joinable and startable again.
            room.in_progress = False
            self._mark_roster_dirty(room)

    # --- handlers -------------------------------------------------------

    def _h_set_name(self, session, msg) -> None:
        name = msg.name(0)[:MAX_NAME_LEN]
        self._names[id(session)] = name
        if session.member is not None:
            session.member.name = name
            self._mark_roster_dirty(session.room)

    def _h_request_state(self, session, msg) -> None:
        """Send the roster to one session that asked for it.

        The game scene asks as soon as its link is up. It has to: the roster is
        only broadcast when it CHANGES, and starting a round changes nothing -
        the same people are in the same room. But `Scene.Load` wipes the
        console's Lua state, so the game scene comes up with an empty player
        table. Without this the vote screen offers only SKIP and names nobody,
        and an impostor can never kill, because the kill lookup skips every slot
        it does not know to be alive.

        Answered to the ASKER only, not broadcast: one console reloading a scene
        is not news to the other nine, and this arrives during the scene swap
        when the reliable queue is at its busiest.
        """
        room = session.room
        if room is None:
            return
        owner = self._owners.get(room.room_id, 0)
        session.send_app_data(self._room_state_payload(room, owner))

        # The roster is not the only thing a scene load wipes.
        #
        # `Scene.Load` clears the console's Lua state, so a console that reloads
        # mid-round comes up not knowing its own role either - and an impostor
        # comes up not knowing who its partners are, which is unrecoverable from
        # the client's side because both messages are sent once, at match start.
        # Answered only to the ASKER, and the impostor roster only if the asker
        # really is one, so this leaks nothing that `Match.start` did not already
        # send that same console.
        match = self._matches.get(room.room_id)
        if match is None:
            return
        me = match.players.get(session.slot)
        if me is None:
            return
        impostors = sorted(s for s, p in match.players.items() if p.impostor)
        session.send_app_data(P.role_assigned(
            P.Role.IMPOSTOR if me.impostor else P.Role.CREW, len(impostors)))
        if me.impostor and impostors:
            session.send_app_data(P.impostor_list(impostors))

    def _h_list_rooms(self, session, msg) -> None:
        """One page of the room browser.

        === Ordering, and why it is not by room id =========================

        On a private server with four rooms the order does not matter. On a
        PUBLIC one it is the whole feature: a list sorted by creation order puts
        the oldest rooms first, and the oldest rooms on a busy server are the
        ones nobody has joined - so page one is a list of empty rooms and every
        real game is buried on page nine. Nobody pages to nine. They host a new
        room, which lands at the END of the list, and the server fills up with
        one-player rooms that can never start.

        So: FULLEST FIRST, and full rooms dropped entirely. A room needs four
        players to start and holds ten, so the most useful thing to show anyone
        is the room closest to being playable. Room id breaks ties, which keeps
        the order stable while people join and leave - a list that reshuffles
        under the cursor is a list you cannot press X on.

        Empty rooms are dropped too. They exist only in the gap between a
        creator disconnecting and `destroy_if_empty` running, and one on the
        front page is a room that vanishes the moment it is chosen.
        """
        page = msg.u8(0)
        # ...and only rooms this console could actually GET INTO.
        #
        # `Room.add` refuses a joiner whose scene_hash disagrees with the room's,
        # and answers SCENE_MISMATCH - which the lobby renders as "THEY ARE ON
        # ANOTHER VERSION". That is true when it is true, and badly misleading
        # when it is not: a room whose round has just ended is NOT in progress
        # any more (the Match hits OVER and `_end_match` clears the flag) but its
        # members are still standing on the results screen, so the room's
        # identity is still the GAME scene. It was therefore listed, and every
        # attempt to join it was told the host was on a different version of the
        # game. Playtesters read that as "hosting requires resetting the lobby
        # for people to join after a game ends", which is exactly what it looked
        # like.
        #
        # Comparing against the ASKER's own hash keeps the server scene-agnostic:
        # it still holds no registry of what any scene is, only the rule that a
        # room you cannot join is not worth showing you. It reappears by itself
        # the moment the whole room finishes loading the lobby again (see
        # Room.rebind), with nothing for the host to reset.
        mine = getattr(session, "scene_hash", 0)
        rooms = [r for r in self.server.rooms.public_rooms()
                 if not r.in_progress and 0 < r.player_count < MAX_PLAYERS
                 and (r.scene_hash is None or r.scene_hash == mine)]
        rooms.sort(key=lambda r: (-r.player_count, r.room_id))

        # Stop-and-wait makes every extra packet a whole round trip, so a page is
        # sized to fill exactly one: 4 + 15*16 = 244 <= 256.
        per = P.ROOMS_PER_PAGE
        page_count = max(1, (len(rooms) + per - 1) // per)
        # Clamped to 255, because `page_count` is one byte on the wire. A server
        # with 3,826 rooms would otherwise report page count 0 and the console
        # would decide there was nothing to show.
        page_count = min(page_count, 255)
        page = min(page, page_count - 1)
        window = rooms[page * per : page * per + per]

        records = [
            P.RoomRecord(
                room_id=r.room_id,
                player_count=r.player_count,
                flags=P.pack_room_flags(r.private, r.in_progress,
                                        self._maps.get(r.room_id, 0)),
                name=r.name,
            )
            for r in window
        ]
        session.send_app_data(P.room_list(records, page=page, page_count=page_count))

    def _h_create_room(self, session, msg) -> None:
        name = msg.name(0)
        private = bool(msg.u8(P.NAME_LEN) & P.ROOM_FLAG_PRIVATE)
        # The map byte is optional on the wire: a console built before maps
        # existed sends a 14-byte CreateRoom and `u8` answers 0 past the end,
        # which is the first map. Clamped, because a client is not trusted to
        # send an id this build has a scene for.
        map_id = msg.u8(P.NAME_LEN + 1) % P.MAP_COUNT
        try:
            room = self.server.rooms.create(name=name or "room", private=private)
        except RoomFullError:
            self._reject(session, P.Reject.FULL)
            return
        self._maps[room.room_id] = map_id
        # The creator owns it. Ownership is what gates StartGame.
        if self._admit(session, room):
            self._owners[room.room_id] = session.slot

    def _h_join_room(self, session, msg) -> None:
        room = self.server.rooms.get(msg.u16(0))
        if room is None:
            self._reject(session, P.Reject.NOT_FOUND)
            return
        self._admit(session, room)

    def _h_join_by_code(self, session, msg) -> None:
        room = self.server.rooms.by_join_code(msg.name(0, P.CODE_LEN))
        if room is None:
            self._reject(session, P.Reject.NOT_FOUND)
            return
        self._admit(session, room)

    def _admit(self, session, room) -> bool:
        if room.in_progress:
            self._reject(session, P.Reject.IN_PROGRESS)
            return False
        if room.player_count >= MAX_PLAYERS:
            self._reject(session, P.Reject.FULL)
            return False
        try:
            session.join_room(room, name=self._name_of(session))
        except RoomFullError:
            self._reject(session, P.Reject.FULL)
            return False
        except SceneMismatchError:
            # The joiner's splashpack disagrees with the room's. Rejecting is
            # the whole point: letting them in would desync silently.
            self._reject(session, P.Reject.SCENE_MISMATCH)
            return False

        # A room whose owner never registered (joined before anyone owned it)
        # would be unstartable, so the first person in owns it.
        self._owners.setdefault(room.room_id, session.slot)
        session.send_app_data(P.room_joined(room.room_id, session.slot, room.join_code))
        # Which ship this room is on. ROOM_JOINED has no room for it and adding
        # one would change a message every console already parses, so it rides
        # its own. A console that joined BY CODE has no other way to find out:
        # private rooms never appear in a room list, so the client-side trick of
        # reading the map off the cached list entry finds nothing and the room
        # screen names the wrong ship right up until the round starts.
        session.send_app_data(P.map_changed(self._maps.get(room.room_id, 0)))
        # Coalesced: if three people join in the same tick, everyone gets ONE
        # roster containing all three rather than three racing broadcasts.
        self._mark_roster_dirty(room)
        return True

    def _h_leave_room(self, session, msg) -> None:
        room = session.room
        if room is None:
            return
        # _release_room, NOT on_disconnect: the same ownership handover and match
        # bookkeeping, WITHOUT dropping the session's name. This console is going
        # back to the room list, not going away, and it will not send its name
        # again - the console sends SetName once at boot.
        self._release_room(session)
        session.leave_room()
        if room.members:
            self._mark_roster_dirty(room)

    def _h_start_game(self, session, msg) -> None:
        room = session.room
        if room is None or room.in_progress:
            return
        # Server-authoritative: the client asks, it does not decide.
        if self._owners.get(room.room_id) != session.slot:
            logger.info("room %d: slot %d tried to start but does not own it",
                        room.room_id, session.slot)
            return
        if room.room_id in self._countdowns:
            return  # already starting; a second press must not restart it
        # Too few players is not a game: with fewer than four, the single impostor
        # is at parity after one kill and the round ends immediately. Refused here
        # rather than started and instantly won.
        if room.player_count < MIN_PLAYERS_TO_START:
            logger.info("room %d: start refused, %d player(s) < %d",
                        room.room_id, room.player_count, MIN_PLAYERS_TO_START)
            session.send_app_data(P.room_rejected(P.Reject.TOO_FEW_PLAYERS))
            return

        self._countdowns[room.room_id] = START_COUNTDOWN_FRAMES
        # The map is decided ONCE, when the room is created, and echoed here.
        # Everybody in the room gets the same byte in the same message, which is
        # what makes "we are all on the same ship" true by construction rather
        # than by every console independently remembering what it picked.
        payload = P.game_starting(min(START_COUNTDOWN_FRAMES, 255),
                                  self._maps.get(room.room_id, 0))
        for m in room.members.values():
            m.session.send_app_data(payload)

    def _h_return_to_lobby(self, session, msg) -> None:
        """The owner is done with the results screen. Take the whole room back.

        Server-authoritative, like StartGame and for the same reason: without it
        every console decides for itself when the round is over, players trickle
        back to the lobby one at a time, and the host cannot start the next round
        until the slowest of them has pressed a button. This moves the room
        together.

        The MATCH is dropped here rather than left to expire. `_end_match` had
        only ever been reached from a rule that ended the round, so a match whose
        players had all wandered back to the lobby stayed in `self._matches`
        forever - and the room with it, because `replicates()` reads
        `in_progress`.
        """
        room = session.room
        if room is None:
            return
        if self._owners.get(room.room_id) != session.slot:
            return
        self._end_match(room.room_id)
        if self.web:
            # RETURN_LOBBY does not travel through Match.emit, so the spectator
            # cannot observe it - and without being told, it keeps showing the
            # results screen for a room that is back in the lobby.
            self.web.spectator.returned_to_lobby(room.room_id)
        payload = P.return_lobby()
        for m in room.members.values():
            m.session.send_app_data(payload)

    def _h_set_map(self, session, msg) -> None:
        """Put this room on a different ship. Owner only, and not mid-round.

        The map used to be decided once, at creation, and never again - so an
        owner who picked the wrong ship, or a room that simply wanted a change
        between rounds, had to be destroyed and rebuilt. That costs everyone in
        it their slot and invalidates a join code people have already read out
        across a room, which is a heavy price for a two-bit field.

        Refused outright while a round is running rather than queued for later.
        The map is what every console in the room loaded as a SCENE; changing it
        under a live match would leave ten consoles simulating a ship the server
        is not.
        """
        room = session.room
        if room is None:
            return
        if self._owners.get(room.room_id) != session.slot:
            return
        if room.in_progress:
            return
        map_id = msg.u8(0) % P.MAP_COUNT
        if self._maps.get(room.room_id) == map_id:
            return
        self._maps[room.room_id] = map_id
        # Everyone, including the owner who asked. The owner's console does not
        # apply the change locally off its own button press: the server is what
        # decides, and a client that painted the new map before being told would
        # show a different ship from everybody else the moment a request was
        # refused for a reason it could not see.
        payload = P.map_changed(map_id)
        for m in room.members.values():
            m.session.send_app_data(payload)

    # --- gameplay -------------------------------------------------------
    #
    # Every one of these is a REQUEST. The Match decides, and silently declines
    # anything a legitimate client would never ask for - there is no reply that
    # says "no", because a modified client learns something from every "no" it
    # can provoke. An impostor probing "can I kill this person" would otherwise
    # get a free radar.

    def _match_of(self, session) -> Match | None:
        if session.room is None:
            return None
        return self._matches.get(session.room.room_id)

    def _after(self, session) -> None:
        """Reap a round the action just ended."""
        match = self._match_of(session)
        if match is not None and match.phase == match.phase.OVER:
            self._end_match(session.room.room_id)

    def _h_task_complete(self, session, msg) -> None:
        m = self._match_of(session)
        if m:
            m.task_complete(session.slot, msg.u8(0))
            self._after(session)

    def _h_kill(self, session, msg) -> None:
        m = self._match_of(session)
        if m:
            m.kill(session.slot, msg.u8(0))
            self._after(session)

    def _h_report_body(self, session, msg) -> None:
        m = self._match_of(session)
        if m:
            m.report_body(session.slot, msg.u8(0))

    def _h_call_meeting(self, session, msg) -> None:
        m = self._match_of(session)
        if m:
            m.call_meeting(session.slot)

    def _h_vote(self, session, msg) -> None:
        m = self._match_of(session)
        if m:
            m.vote(session.slot, msg.u8(0))
            self._after(session)

    def _h_sabotage(self, session, msg) -> None:
        m = self._match_of(session)
        if m:
            m.start_sabotage(session.slot, msg.u8(0))

    def _h_fix_sabotage(self, session, msg) -> None:
        m = self._match_of(session)
        if m:
            m.fix_sabotage(session.slot, msg.u8(0))

    def _h_vent_state(self, session, msg) -> None:
        m = self._match_of(session)
        if m:
            m.set_vented(session.slot, msg.u8(0) != 0)

    def _h_use_vent(self, session, msg) -> None:
        # Nothing is broadcast either way: a vent the whole room could see is a
        # vent no impostor would ever use. The console moves itself; this only
        # rules on whether it was allowed to.
        m = self._match_of(session)
        if m:
            m.use_vent(session.slot)


_HANDLERS = {
    P.C2S.SET_NAME: PSXSUSGame._h_set_name,
    P.C2S.LIST_ROOMS: PSXSUSGame._h_list_rooms,
    P.C2S.CREATE_ROOM: PSXSUSGame._h_create_room,
    P.C2S.JOIN_ROOM: PSXSUSGame._h_join_room,
    P.C2S.JOIN_BY_CODE: PSXSUSGame._h_join_by_code,
    P.C2S.LEAVE_ROOM: PSXSUSGame._h_leave_room,
    P.C2S.START_GAME: PSXSUSGame._h_start_game,
    P.C2S.TASK_COMPLETE: PSXSUSGame._h_task_complete,
    P.C2S.REPORT_BODY: PSXSUSGame._h_report_body,
    P.C2S.VOTE: PSXSUSGame._h_vote,
    P.C2S.KILL: PSXSUSGame._h_kill,
    P.C2S.SABOTAGE: PSXSUSGame._h_sabotage,
    P.C2S.FIX_SABOTAGE: PSXSUSGame._h_fix_sabotage,
    P.C2S.USE_VENT: PSXSUSGame._h_use_vent,
    P.C2S.CALL_MEETING: PSXSUSGame._h_call_meeting,
    P.C2S.VENT_STATE: PSXSUSGame._h_vent_state,
    P.C2S.REQUEST_STATE: PSXSUSGame._h_request_state,
    P.C2S.RETURN_TO_LOBBY: PSXSUSGame._h_return_to_lobby,
    P.C2S.SET_MAP: PSXSUSGame._h_set_map,
}
