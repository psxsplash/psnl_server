"""The smallest useful game plug-in: a shared scoreboard.

Run it:

    psnl-server --game examples.minimal_game:ScoreGame

Then from Lua on any console in the scene:

    Net.SendData(string.char(1))          -- SCORE: give me a point
    function onNetData(payload)           -- TOTALS: everyone's score
        local n = string.byte(payload, 2)
        for i = 1, n do
            local slot  = string.byte(payload, 1 + i * 2)
            local score = string.byte(payload, 2 + i * 2)
        end
    end

That is the whole shape of a networked psxsplash game. Everything PSXSUS does -
rooms, roles, votes, sabotage - is more of exactly this and nothing else.

THREE THINGS WORTH COPYING FROM HERE
------------------------------------

**Opcode-first payloads.** The first byte says what the message is. The engine
never looks inside an AppData payload, so the format is entirely yours, but you
will want to dispatch on something and a leading byte costs nothing.

**Return True to consume.** `on_app_data` returning False relays the bytes to the
rest of the room untouched, which is the engine's default and is genuinely useful
- a chat message needs no server logic at all. Return True once you have handled
it yourself.

**Slots, not connections.** A player is a slot number. Slot 0 is the server and
has no avatar, so human players start at 1. Keying anything by session object
instead will surprise you the first time somebody reconnects.
"""

from __future__ import annotations

import struct

from psnl_server.game import Game

#: Client -> server.
SCORE = 1
#: Server -> client.
TOTALS = 0x80


class ScoreGame(Game):
    #: True (the default) drops a console into a room as soon as it says hello,
    #: which is what you want without a lobby. A game with a room browser sets
    #: this False and calls room membership itself - see games/psxsus/game.py.
    auto_join = True

    name = "score"

    def __init__(self) -> None:
        self._scores: dict[int, int] = {}

    def on_connect(self, session) -> None:
        self._scores.setdefault(session.slot, 0)
        self._broadcast(session.room)

    def on_disconnect(self, session) -> None:
        self._scores.pop(session.slot, None)
        self._broadcast(session.room)

    def on_app_data(self, session, payload: bytes) -> bool:
        if not payload or payload[0] != SCORE:
            return False  # not ours: let the relay pass it to the room
        self._scores[session.slot] = self._scores.get(session.slot, 0) + 1
        self._broadcast(session.room)
        return True

    def _broadcast(self, room) -> None:
        if room is None:
            return
        # Serialise ONCE and hand the same bytes to everyone. At ten players on a
        # 57600 serial link the difference is not academic.
        body = bytes([TOTALS, len(self._scores)])
        for slot, score in sorted(self._scores.items()):
            body += struct.pack("<BB", slot, min(score, 255))
        for member in room.members.values():
            # send_app_data is RELIABLE: retransmitted until the console
            # acknowledges. Use it for anything a player would notice going
            # missing. Position updates go on the unreliable path instead,
            # because a lost one is replaced by the next in 33ms.
            member.session.send_app_data(body)
