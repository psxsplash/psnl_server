"""PSXSUS: social deduction on the PlayStation 1, as a worked example.

This is a real shipping game, not a toy, which is the point of it being here: it
exercises every hook `psnl_server.game.Game` offers, including the awkward ones
(owning room membership, refusing a join, regenerating a message the link
dropped).

    protocol.py   the wire format, shared verbatim with the console's Lua
    game.py       lobby, rooms, ownership, starting a round
    match.py      roles, tasks, sabotage, meetings, voting, win conditions
    web/          an unauthenticated spectator view of every public room

`web/` is worth a second look if you are writing your own game, because it is
the example of building something substantial on the plug-in seam WITHOUT
touching the relay: it reads live state through the ordinary public objects and
taps `Match.emit` for the rest, and the only thing it needed from `psnl_server`
was a generic `Game.on_start` hook. See SPECTATOR.md.
"""
