"""The seam between the PSNL server and a game's server-side rules.

PSNL knows about frames, slots, rooms and snapshots. It does not know what a
task, a vote or an impostor is, and it must not learn: baking one game's lobby
into the server is the same mistake as baking `CreateRoom` into `netprotocol.hh`.
The next game built on psxsplash would inherit a lobby model it never asked for.

So a game is a plug-in. It receives the opaque AppData bytes the engine already
refuses to interpret, and it may own room membership if it has a lobby of its
own. Everything below has a working default, so a server with no game attached is
exactly the relay it was before this file existed.

PSXSUS's implementation lives in `PSXSUS/Server/psxsus_game.py` - outside this
package, deliberately.
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


class Game:
    """Base class for a game's server-side rules. Every hook is optional."""

    #: When False, the server does NOT drop a console into a room on Hello, and
    #: the game owns membership instead. A game with a lobby wants False: the
    #: player has not chosen a room yet, and auto-joining would put them in one
    #: before they ever saw the list.
    auto_join: bool = True

    #: Human-readable, for the startup log.
    name: str = "none"

    def attach(self, server) -> None:
        """Called once, before listening. `server` is the PSNLServer."""
        self.server = server

    async def on_start(self) -> None:
        """The server is listening. Start anything of your own that needs a loop.

        `attach` cannot do this job: it runs in `PSNLServer.__init__`, which is
        ordinary synchronous code and may have no running event loop at all. A
        plug-in that wants a background task, a timer or a listener of its own
        (PSXSUS starts its spectator web view here) needs a hook that runs inside
        the loop, and this is it.

        Kept deliberately generic. The relay learns that a game may have things
        to start and stop - not what any of them are.
        """

    async def on_stop(self) -> None:
        """The server is shutting down. Undo `on_start`.

        Called even if `on_start` failed, so it must tolerate being asked to stop
        something that never started.
        """

    def on_connect(self, session) -> None:
        """A console completed its handshake."""

    def on_app_data(self, session, payload: bytes) -> bool:
        """Handle one AppData message.

        Return True if the game consumed it. Returning False relays the bytes to
        the rest of the room, which is the engine's default behaviour.
        """
        return False

    def on_disconnect(self, session) -> None:
        """A console went away - cleanly or otherwise."""

    def on_reliable_dropped(self, session, payload: bytes) -> None:
        """A reliable message was abandoned after exhausting its retransmits.

        The default is to do nothing: the relay has no idea what the bytes meant.
        A game that has messages it cannot afford to lose should override this and
        regenerate them from current state - which beats resending, because by the
        time retries are exhausted the original payload is seconds old.
        """

    def replicates(self, room) -> bool:
        """Whether this room's avatars should be broadcast to its members.

        Default True: the plain relay replicates every room, which is what it has
        always done and what a game with no lobby expects.

        A game with a lobby should return False for rooms that are not playing.
        Snapshots are the single largest consumer of a 115200 serial link - at ten
        players, 215 bytes at 30Hz is 56% of it - and in a lobby every one of those
        bytes is a position that has never changed and that the console discards on
        arrival. Worse, it is spent starving the reliable channel the lobby depends
        on, so the room roster gets shredded and the player sits on an empty room
        screen while the wire is busy. See PSXSUSGame.replicates.
        """
        return True

    def tick(self, dt_seconds: float = 1.0 / 30.0) -> None:
        """Called once per snapshot tick, with the REAL time since the last one.

        Take the argument seriously: the tick loop is scheduled on a wall clock
        but it can and does overrun, and a plug-in that assumes a fixed tick
        length silently loses that time from every timer it owns.
        """


def load_game(spec: str) -> Game:
    """Load a game plug-in from a `module:attr` or `path/to/file.py:attr` spec.

    Accepting a filesystem path is what lets PSXSUS live in the game's own folder
    rather than being installed into the server's package - the two ship and
    version independently.
    """
    if ":" not in spec:
        raise ValueError(f"game spec {spec!r} must be 'module:attr' or 'path.py:attr'")
    module_part, _, attr = spec.rpartition(":")

    if module_part.endswith(".py"):
        path = Path(module_part).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"no such game module: {path}")
        # Its siblings (the shared protocol module) must be importable too.
        sys.path.insert(0, str(path.parent))
        module = importlib.import_module(path.stem)
    else:
        module = importlib.import_module(module_part)

    obj = getattr(module, attr)
    game = obj() if isinstance(obj, type) else obj
    if not isinstance(game, Game):
        raise TypeError(f"{spec} is not a psnl_server.game.Game")
    return game
