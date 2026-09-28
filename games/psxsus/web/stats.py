"""Aggregate results across rounds, for the spectator front page.

Deliberately a plain object with no server, socket or asyncio in it - the same
posture `match.py` takes - so the whole thing is testable by calling `record()`
in a loop.

WHAT IS AND IS NOT COUNTED. Only a round that actually FINISHED is recorded: a
match whose players all wandered off, or one dropped when the process restarts,
never had a winner and would drag the impostor win rate toward whichever side
happens to lose when a lobby empties. `Match.winner is None` is exactly that
test, and it is the caller's job to make it (see `PSXSUSGame._end_match`).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: How many finished rounds the front page can show. A scrolling history is not
#: the point of the page and every entry is held in memory forever otherwise.
RECENT_GAMES = 25

#: Written back to disk at most this often, however many rounds end. Rounds take
#: minutes, so this never actually throttles anything - it is here so that a
#: pathological loop (a room ending rounds instantly) cannot turn the stats file
#: into a write amplifier on the server's disk.
SAVE_INTERVAL = 10.0


@dataclass(slots=True)
class GameResult:
    """One finished round, as the front page lists it."""

    ended_at: float
    map_id: int
    players: int
    impostors: int
    #: 0 crew, 1 impostors - `match.Winner`.
    winner: int
    #: `protocol.OverReason`.
    reason: int
    duration: float
    room_name: str = ""


class Stats:
    """Everything the spectator front page reports, and nothing else.

    Persistence is OPTIONAL and best-effort. A spectator page is not a system of
    record: if the file is missing, unreadable or written by an older build, the
    honest response is to start counting again rather than to refuse to serve
    the page - so every failure here is logged and swallowed.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else None
        self.games = 0
        self.crew_wins = 0
        self.impostor_wins = 0
        self.total_players = 0
        self.total_duration = 0.0
        self.by_reason: Counter[int] = Counter()
        self.by_map: Counter[int] = Counter()
        #: map_id -> impostor wins on it. Paired with `by_map` this is the only
        #: per-map number worth showing: whether a ship favours the impostors.
        self.impostor_wins_by_map: Counter[int] = Counter()
        self.recent: deque[GameResult] = deque(maxlen=RECENT_GAMES)
        self.started_at = time.time()
        self._last_save = 0.0
        if self.path:
            self._load()

    # --- recording ------------------------------------------------------

    def record(self, result: GameResult) -> None:
        self.games += 1
        if result.winner:
            self.impostor_wins += 1
            self.impostor_wins_by_map[result.map_id] += 1
        else:
            self.crew_wins += 1
        self.total_players += result.players
        self.total_duration += result.duration
        self.by_reason[result.reason] += 1
        self.by_map[result.map_id] += 1
        self.recent.appendleft(result)
        self.save()

    # --- reporting ------------------------------------------------------

    def snapshot(self) -> dict:
        games = self.games or 1  # only ever a divisor
        return {
            "games": self.games,
            "crewWins": self.crew_wins,
            "impostorWins": self.impostor_wins,
            # Rounded to a tenth of a percent. A win rate quoted to eight
            # decimal places over nine games is false precision, and the front
            # page renders whatever it is handed.
            "impostorWinRate": round(100.0 * self.impostor_wins / games, 1),
            "crewWinRate": round(100.0 * self.crew_wins / games, 1),
            "avgPlayers": round(self.total_players / games, 1),
            "avgDuration": round(self.total_duration / games, 1),
            "byReason": {str(k): v for k, v in sorted(self.by_reason.items())},
            "byMap": [
                {
                    "map": map_id,
                    "games": count,
                    "impostorWins": self.impostor_wins_by_map.get(map_id, 0),
                    "impostorWinRate": round(
                        100.0 * self.impostor_wins_by_map.get(map_id, 0) / count, 1
                    ),
                }
                for map_id, count in sorted(self.by_map.items())
            ],
            "recent": [asdict(r) for r in self.recent],
            "since": self.started_at,
        }

    # --- persistence ----------------------------------------------------

    def save(self, force: bool = False) -> None:
        if self.path is None:
            return
        now = time.monotonic()
        if not force and now - self._last_save < SAVE_INTERVAL:
            return
        self._last_save = now
        payload = {
            "version": 1,
            "games": self.games,
            "crew_wins": self.crew_wins,
            "impostor_wins": self.impostor_wins,
            "total_players": self.total_players,
            "total_duration": self.total_duration,
            "by_reason": dict(self.by_reason),
            "by_map": dict(self.by_map),
            "impostor_wins_by_map": dict(self.impostor_wins_by_map),
            "recent": [asdict(r) for r in self.recent],
            "started_at": self.started_at,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename, because the alternative is a truncated stats
            # file whenever the container is stopped mid-write - and that file
            # is then unreadable on every subsequent boot, not just this one.
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.path)
        except Exception:
            logger.warning("spectator: could not save stats to %s", self.path, exc_info=True)

    def _load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("spectator: stats at %s are unreadable, starting fresh", self.path)
            return
        if data.get("version") != 1:
            logger.info("spectator: stats at %s are version %s, starting fresh",
                        self.path, data.get("version"))
            return
        self.games = int(data.get("games", 0))
        self.crew_wins = int(data.get("crew_wins", 0))
        self.impostor_wins = int(data.get("impostor_wins", 0))
        self.total_players = int(data.get("total_players", 0))
        self.total_duration = float(data.get("total_duration", 0.0))
        # JSON object keys are strings; these are counters keyed by small ints.
        self.by_reason = Counter({int(k): int(v) for k, v in data.get("by_reason", {}).items()})
        self.by_map = Counter({int(k): int(v) for k, v in data.get("by_map", {}).items()})
        self.impostor_wins_by_map = Counter(
            {int(k): int(v) for k, v in data.get("impostor_wins_by_map", {}).items()}
        )
        self.recent = deque(
            (GameResult(**r) for r in data.get("recent", [])), maxlen=RECENT_GAMES
        )
        self.started_at = float(data.get("started_at", time.time()))
        logger.info("spectator: loaded %d past rounds from %s", self.games, self.path)
