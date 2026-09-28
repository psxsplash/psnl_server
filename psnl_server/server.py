"""The PSNL central server.

Listens on two ports because the two client kinds speak different wires and
guessing is unnecessary:

    6699  raw       the Python bridge (real hardware), and Redux-as-SIO1-server
    6700  protobuf  Redux's SIO1 client, which cannot speak raw

Auto-detecting the wire from the first byte (0xA5 = raw sync) works in practice
but is ambiguous in principle - a protobuf length prefix could legitimately be
0xA5. Two ports cost nothing and cannot be wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .game import Game
from .rooms import RoomManager, encode_snapshot_frame
from .session import Session
from .transport.framing import ProtobufAdapter, RawAdapter

logger = logging.getLogger(__name__)

# The console sends a snapshot every 2 frames (c_snapshotIntervalFrames) at 60Hz.
# Matching that cadence keeps the wire full without ever queueing faster than the
# console can drain - at 115200 the 215-byte fan-out already occupies ~56% of the
# link, so sending faster would only build latency.
SNAPSHOT_HZ = 30.0

#: How long a peer may say nothing before it is treated as gone.
#:
#: This is the fix for stale rooms, and it is needed because a reset console does
#: NOT close its socket: real hardware reaches this server through a bridge
#: process, and resetting the PlayStation leaves the bridge - and therefore the
#: TCP connection - perfectly alive. Without a timeout the server keeps a member
#: that no longer exists; reset every console in a room and the room never
#: empties, so it is never destroyed, and it stays listed and joinable forever.
#: Players then join a lobby full of people who are not there.
#:
#: Twenty seconds is generous on purpose. A live console sends a snapshot every
#: two frames while replicating and a Ping on its own clock, so the honest
#: threshold is well under a second - but a console mid-scene-load is genuinely
#: silent while it reads the disc, and dropping somebody during the handover from
#: the lobby to the map would be a far worse bug than the one being fixed.
PEER_TIMEOUT_SECONDS = 20.0


class PSNLServer:
    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        raw_port: int = 6699,
        protobuf_port: int = 6700,
        snapshot_hz: float = SNAPSHOT_HZ,
        game: Game | None = None,
        peer_timeout: float = PEER_TIMEOUT_SECONDS,
    ) -> None:
        self.host = host
        self.raw_port = raw_port
        self.protobuf_port = protobuf_port
        self.snapshot_interval = 1.0 / snapshot_hz
        #: Upper bound on the elapsed time a single tick may report. Without it a
        #: debugger pause or a suspended process delivers one huge dt that expires
        #: every game timer simultaneously.
        self.max_tick_seconds = 1.0
        #: Zero or negative disables the stale-peer reaper, which the tests use
        #: to hold a silent fixture open on purpose.
        self.peer_timeout = peer_timeout
        self.rooms = RoomManager()
        self.sessions: set[Session] = set()
        self._servers: list[asyncio.AbstractServer] = []
        self._tick_task: asyncio.Task | None = None
        # No game attached => the plain relay this server has always been.
        self.game = game or Game()
        self.game.attach(self)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, protobuf: bool) -> None:
        adapter = ProtobufAdapter() if protobuf else RawAdapter()
        session = Session(
            writer, adapter, self.rooms,
            auto_join=self.game.auto_join,
            on_connect=self.game.on_connect,
        )
        session.on_app_data = self.game.on_app_data
        session.on_reliable_dropped = self.game.on_reliable_dropped
        self.sessions.add(session)
        logger.info("connection from %s (%s)", session.peer, adapter.name)
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                session.feed(data)
                # Backpressure: if a console stops draining, stop queueing for it
                # rather than growing the write buffer without bound.
                with contextlib.suppress(Exception):
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            logger.exception("session error for %s", session.peer)
        finally:
            logger.info("disconnect %s (slot %d)", session.peer, session.slot)
            # Before close(), so the game can still see which room it was in.
            with contextlib.suppress(Exception):
                self.game.on_disconnect(session)
            session.close()
            self.sessions.discard(session)
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _tick(self) -> None:
        """Fan out one snapshot per scene group per room, per tick.

        Each group serializes exactly once and the identical bytes go to every
        member of it: the console discards its own slot on receipt
        (networkmanager.cpp:268), so no per-recipient variant is needed.

        Normally a room is a single group. It splits only while a scene handover
        is in flight, so that players who have finished loading see each other
        instead of waiting for the slowest console.
        """
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        last_tick = loop.time()
        while True:
            next_at += self.snapshot_interval
            delay = next_at - loop.time()
            if delay < 0:
                # We overran the budget; resync rather than spiral into catch-up.
                next_at = loop.time()
                delay = 0
            await asyncio.sleep(delay)

            # REAL elapsed time, not an assumed tick length.
            #
            # Dropping the missed *snapshots* on an overrun is right - nobody
            # wants stale positions replayed. Dropping the missed *time* is not:
            # it silently stole seconds from every game clock (sabotage doomsday,
            # kill cooldown, vote timer), always in the same direction, and the
            # error accumulated across a whole match. A 200ms hiccup used to mean
            # the reactor got 200ms of free life.
            #
            # Clamped so that a debugger pause or a suspended process cannot
            # deliver one enormous dt that instantly expires every timer at once.
            now = loop.time()
            elapsed = now - last_tick
            last_tick = now
            if elapsed > self.max_tick_seconds:
                elapsed = self.max_tick_seconds

            # Game timers (countdowns, sabotage) run on the snapshot tick rather
            # than a second timer: one clock is easier to reason about, and a
            # game rule that throws must never kill the fan-out.
            try:
                self.game.tick(elapsed)
            except Exception:
                logger.exception("game tick failed")

            # Retransmit anything the consoles have not acknowledged. Server ->
            # console AppData used to be fire-and-forget, so one corrupted frame
            # lost a message forever - and the largest message on the wire is the
            # room roster, which is exactly the one whose loss is unrecoverable.
            for session in list(self.sessions):
                session.service_reliable(now)

            self._reap_stale_peers(now)

            for room in self.rooms.all_rooms():
                if not room.members:
                    continue
                # Let the game say whether this room's avatars are worth the wire.
                # A room that has not started has no avatars to replicate: every
                # record is the zero the player spawned with, and the lobby scene
                # discards them all (it maps no remote actors). Sending them anyway
                # cost 6.4KB/s at ten players - 56% of a 115200 serial link - and it
                # was spent starving the ONE message the lobby actually waits for,
                # the room roster. A console could sit at "Room - Code:" forever
                # while the wire was saturated with positions of players standing
                # still in a menu.
                #
                # This asks the plug-in rather than testing `room.in_progress`
                # directly: in_progress is set only by games that have a lobby, so
                # reading it here would silently stop the plain relay from
                # replicating anything at all.
                if not self.game.replicates(room):
                    continue
                for members in room.scene_groups().values():
                    frame = encode_snapshot_frame(room, members)
                    for member in members:
                        # Pacing is per session, not per room: one console on a slow
                        # serial link must not make everyone else stutter, and a
                        # snapshot is the one thing that is free to drop.
                        if member.session.wants_snapshot():
                            member.session.send_raw(frame)

    def _reap_stale_peers(self, now: float) -> None:
        """Drop peers that have gone silent, and take their room membership with
        them.

        The connection handler already does this teardown when a socket closes -
        but a reset console does not close its socket, because the socket belongs
        to the bridge process on the PC and the bridge is still running. See
        `Session.is_stale`. Without this the room keeps ghosts: reset every
        console in a lobby and it stays listed and joinable with a full roster of
        players who are not there, forever, because it never empties and
        therefore is never destroyed.

        Ordered exactly like the handler's `finally` block: the game is told
        first, while the session still knows which room it was in, and only then
        is the session closed.
        """
        if self.peer_timeout <= 0:
            return
        for session in list(self.sessions):
            if not session.is_stale(now, self.peer_timeout):
                continue
            logger.info("dropping silent peer %s (slot %d): nothing for %.0fs",
                        session.peer, session.slot, self.peer_timeout)
            with contextlib.suppress(Exception):
                self.game.on_disconnect(session)
            session.close()
            self.sessions.discard(session)

    async def start(self) -> None:
        self._servers = [
            await asyncio.start_server(
                lambda r, w: self._handle(r, w, protobuf=False), self.host, self.raw_port
            ),
            await asyncio.start_server(
                lambda r, w: self._handle(r, w, protobuf=True), self.host, self.protobuf_port
            ),
        ]
        self._tick_task = asyncio.create_task(self._tick())
        logger.info(
            "listening: raw=%s:%d protobuf=%s:%d (snapshot %.0fHz, game=%s)",
            self.host, self.raw_port, self.host, self.protobuf_port,
            1.0 / self.snapshot_interval, self.game.name,
        )
        # The plug-in's own listeners and tasks, now that there is a loop to
        # start them on. A game that cannot start is not a reason to refuse to
        # relay: PSXSUS's spectator page failing to bind its port must not take
        # ten players' round down with it.
        try:
            await self.game.on_start()
        except Exception:
            logger.exception("game %s failed to start; serving without it", self.game.name)

    async def stop(self) -> None:
        # First, so the game can still flush anything it wants to persist while
        # the rooms it describes are still populated.
        with contextlib.suppress(Exception):
            await self.game.on_stop()

        if self._tick_task:
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task

        # Close the sessions BEFORE awaiting wait_closed(). Since Python 3.12,
        # Server.wait_closed() waits for every client handler to finish, and each
        # handler is parked in `await reader.read()` until its socket drops. Doing
        # this in the other order deadlocks whenever a client is still attached.
        for session in list(self.sessions):
            session.close()
        self.sessions.clear()

        for server in self._servers:
            server.close()
        for server in self._servers:
            # Even with the sockets closed, a wedged handler must not hang
            # shutdown for good - time out and move on.
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)
        self._servers.clear()

    async def serve_forever(self) -> None:
        await self.start()
        try:
            await asyncio.gather(*(s.serve_forever() for s in self._servers))
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()
