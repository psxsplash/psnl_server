"""Game plug-ins.

The server knows about frames, slots, rooms and snapshots. It knows nothing about
tasks, votes or scores, and it must not learn: baking one game's rules into the
relay would hand every later game a model it never asked for.

So a game lives here, in its own subpackage, and the server loads it by module
spec at startup:

    psnl-server --game games.psxsus.game:PSXSUSGame

To add yours, copy the shape of `psxsus/`:

    games/yourgame/
        __init__.py
        protocol.py   the bytes your console and your rules agree on
        game.py       subclass psnl_server.game.Game, override the hooks you need
        tests/

`examples/minimal_game.py` is the smallest thing that works, if the PSXSUS
example is more than you want to read first.
"""
