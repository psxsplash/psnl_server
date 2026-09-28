"""The spectator web service: routes, live feeds, and the one place it is
configured.

Off unless asked for. `PSXSUS_WEB_PORT` is what turns it on, and the default is
"no", for two independent reasons:

* It serves the impostors' identities to anybody who can reach the port. That is
  the entire feature and it is fine when you meant it; it must never happen
  because somebody attached the game plug-in.
* The test suite starts a real PSXSUS server per test. A plug-in that binds a
  fixed port on attach would collide with itself the moment two tests overlap,
  and would do it in a fork's CI too.

`docker-compose.yml` sets the variable, which is where "I want to run this"
belongs.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .httpd import HttpServer, Request, Response
from .spectator import Spectator

logger = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"

#: How often a room's live state is pushed. The console reports its avatar at
#: 30Hz and the server fans out at 30Hz, so nothing new exists above that;
#: 15Hz halves the bytes for a difference no eye finds, because the page
#: interpolates between frames anyway (the same trick the console uses on
#: remote avatars).
ROOM_HZ = 15.0

#: The room list changes when somebody joins, leaves or starts - human-scale
#: events. Once a second is plenty and keeps an idle lobby page nearly free.
LIST_HZ = 1.0

#: Neither stream is ever quiet - both push on a fixed clock - so there is no
#: keepalive here. The one thing a proxy could still do is BUFFER, which is what
#: `X-Accel-Buffering: no` in `httpd._stream` is for.


def config_from_env() -> dict | None:
    """The spectator's settings, or None when it is switched off."""
    raw = os.environ.get("PSXSUS_WEB_PORT", "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        logger.warning("PSXSUS_WEB_PORT=%r is not a number; spectator disabled", raw)
        return None
    if port < 0:
        return None
    return {
        "host": os.environ.get("PSXSUS_WEB_HOST", "0.0.0.0"),
        "port": port,
        # Optional. Without it the numbers are correct but forgotten on restart,
        # which is the right default for a service with no volume mounted.
        "stats_path": os.environ.get("PSXSUS_WEB_STATS") or None,
        "trusted_proxies": parse_trusted_proxies(
            os.environ.get("PSXSUS_WEB_TRUST_PROXY", "")),
    }


def parse_trusted_proxies(raw: str) -> frozenset[str]:
    """Addresses whose `X-Real-IP` / `X-Forwarded-For` we believe.

    Comma-separated, and EMPTY BY DEFAULT. Behind a reverse proxy every request
    arrives from the proxy, so without this the fair-play check compares the
    proxy's address against the consoles' and can never match - the warning
    silently stops working, which is the worst way for a safeguard to fail.

    Set it to the proxy's address (`127.0.0.1` for nginx on the same host) and
    the check sees real spectators again. It is not "on when a header is
    present", because then any client could set the header and choose whether to
    be warned.
    """
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


class SpectatorWeb:
    """Owns the HTTP server and the read model. One per PSXSUSGame."""

    def __init__(self, game, *, host: str = "0.0.0.0", port: int = 8088,
                 stats_path=None, trusted_proxies: frozenset[str] | None = None) -> None:
        self.spectator = Spectator(game, stats_path=stats_path)
        self.http = HttpServer(host, port, static_dir=STATIC,
                               trusted_proxies=trusted_proxies)
        self._register()

    @property
    def port(self) -> int:
        return self.http.port

    def _register(self) -> None:
        r = self.http.route
        r(r"/healthz", self._healthz)
        r(r"/api/stats", self._stats)
        r(r"/api/rooms", self._rooms)
        r(r"/api/room/(\d+)", self._room)
        r(r"/api/fairplay", self._fairplay)
        r(r"/api/stream/rooms", self._stream_rooms, stream=True)
        r(r"/api/stream/room/(\d+)", self._stream_room, stream=True)
        # Everything else falls through to the static handler, which serves the
        # single page. Deep links (#/room/3) are fragments, so the browser never
        # asks the server for them and no catch-all rewrite is needed.

    async def start(self) -> None:
        await self.http.start()

    async def stop(self) -> None:
        await self.http.stop()
        # Force a final write: the periodic one is rate-limited, so without this
        # the last few rounds before a restart are the ones that get lost.
        self.spectator.stats.save(force=True)

    # --- one-shot endpoints ---------------------------------------------

    def _healthz(self, request: Request) -> Response:
        return Response.json({"ok": True})

    def _stats(self, request: Request) -> Response:
        return Response.json({
            **self.spectator.stats_snapshot(),
            "fairPlay": self.spectator.fair_play(request.client_ip),
        })

    def _rooms(self, request: Request) -> Response:
        return Response.json({
            "rooms": self.spectator.rooms(),
            "fairPlay": self.spectator.fair_play(request.client_ip),
        })

    def _room(self, request: Request) -> Response:
        state = self.spectator.room_state(int(request.match.group(1)))
        if state is None:
            return Response.json({"error": "no such public room"}, status=404)
        state["fairPlay"] = self.spectator.fair_play(request.client_ip)
        return Response.json(state)

    def _fairplay(self, request: Request) -> Response:
        return Response.json(self.spectator.fair_play(request.client_ip))

    # --- live feeds ------------------------------------------------------

    async def _stream_rooms(self, request: Request, stream) -> None:
        fair = self.spectator.fair_play(request.client_ip)
        while not stream.closed:
            ok = await stream.send({
                "rooms": self.spectator.rooms(),
                "stats": self.spectator.stats_snapshot(),
                "fairPlay": fair,
            })
            if not ok:
                return
            await asyncio.sleep(1.0 / LIST_HZ)

    async def _stream_room(self, request: Request, stream) -> None:
        room_id = int(request.match.group(1))
        fair = self.spectator.fair_play(request.client_ip)
        interval = 1.0 / ROOM_HZ
        #: The event log is the largest thing in the payload and it changes a few
        #: times a round, not fifteen times a second. Sending it only when the
        #: room's event counter moves takes about a third off the wire for a full
        #: game; the page keeps whatever it last received.
        sent_seq = -1
        while not stream.closed:
            state = self.spectator.room_state(room_id)
            if state is None:
                # The room is gone (everyone left, or it went private). Say so
                # rather than dropping the connection: the browser would treat a
                # silent close as a network blip and reconnect forever.
                await stream.send({"gone": True}, event="gone")
                return
            if state["eventSeq"] == sent_seq:
                state.pop("events", None)
            else:
                sent_seq = state["eventSeq"]
            state["fairPlay"] = fair
            if not await stream.send(state):
                return
            await asyncio.sleep(interval)
