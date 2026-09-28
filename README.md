# PSNL

Networked multiplayer for [psxsplash](https://github.com/psxsplash/psxsplash)
games on the PlayStation 1. Connects consoles, real and emulated, into rooms of
up to **10 players**.

This repository is **the server, the bridge, and the wire format they share**.
It is game-agnostic: fork it, drop your rules in `games/`, and ship.

```
Real PlayStation --serial--> psnl-bridge --raw------> :6699 -+
                                                             +-> psnl-server
PCSX-Redux ------------------SIO1 client, protobuf--> :6700 -+
```

## Quick start

```sh
pip install -e ".[dev]"
psnl-server --game games.psxsus.game:PSXSUSGame
```

or the whole thing in a container:

```sh
docker compose up --build
```

With no `--game` it is a pure relay, which is a useful thing to be on its own:
two consoles sharing a scene need nothing more.

## Writing your own game

The server knows about frames, slots, rooms and snapshots. It knows nothing about
tasks, votes or scores, and it must not learn - baking one game's rules into the
relay hands every later game a model it never asked for.

So a game is a plug-in. Start from `examples/minimal_game.py`, a working shared
scoreboard in about forty lines:

```python
from psnl_server.game import Game

class ScoreGame(Game):
    name = "score"

    def on_app_data(self, session, payload: bytes) -> bool:
        if not payload or payload[0] != SCORE:
            return False          # not ours; the relay passes it to the room
        self._scores[session.slot] = self._scores.get(session.slot, 0) + 1
        self._broadcast(session.room)
        return True
```

```sh
psnl-server --game examples.minimal_game:ScoreGame
```

Then lay yours out the same way as the worked example:

```
games/yourgame/
    __init__.py
    protocol.py   the bytes your console and your rules agree on
    game.py       subclass Game, override the hooks you need
    tests/
```

Every hook on `Game` is optional and has a working default - see
`psnl_server/game.py`. The ones that matter most:

| Hook | For |
|---|---|
| `on_app_data` | your messages. Return True to consume, False to relay |
| `auto_join` | set False if you have a lobby and want to own room membership |
| `on_connect` / `on_disconnect` | a console arrived or vanished |
| `tick` | anything on a clock: timers, countdowns, phases |
| `replicates` | whether a room gets avatar snapshots at all |
| `on_reliable_dropped` | a message the link gave up on, so you can regenerate it |

**`games/psxsus/` is the worked example.** It is a real shipping game rather than
a toy, which is the point: it exercises every hook, including the awkward ones.

### Testing your game without hardware

`psnl_server.testing.FakeConsole` speaks the byte-exact wire the engine emits,
checked against a corpus the engine itself generates. A ten-player room can be
proven with nothing plugged in:

```python
from psnl_server.testing import FakeConsole

console = FakeConsole()
await console.connect("127.0.0.1", port)
await console.handshake()
console.send_app_data(bytes([MY_OPCODE]))
reply = await console.wait_for_app_data(MY_REPLY)
```

## Two ports, deliberately

| Port | Wire | Who |
|---|---|---|
| 6699 | raw | the bridge (a serial cable carries raw bytes), Redux-as-SIO1-server |
| 6700 | protobuf | Redux's SIO1 **client**, which cannot speak raw |

Redux's client throws on Raw, and a player pointing Redux at a remote server must
use client mode, so protobuf is not a preference on that path - it is the only
wire available. Sniffing the first byte (0xA5 = raw sync) would work in practice
but is ambiguous in principle; two ports cost nothing and cannot be wrong.

## The bridge

Real hardware speaks raw serial, not TCP. `psnl-bridge` is the byte pump between
them, and it deliberately does not parse PSNL: understanding the protocol would
mean a second implementation to keep in step with the console.

```sh
pip install -e ".[gui]"     # or ".[bridge]" for headless
psnl-bridge                 # pick a COM port and a server
```

## Architecture

**Star.** The server is slot 0 and the authority; every console is a client. Slot
0 has no avatar, so ten players occupy slots 1..10 - the engine's `c_maxSlots` is
11, not 10.

**It relays continuous state and owns discrete state.** It does not simulate.
Reimplementing PlayStation collision and navigation in Python would mean two
physics engines that have to agree forever, and every divergence would be a desync
bug with no natural home.

| State | Authority |
|---|---|
| Avatar transforms | **client**; the server validates only that a console claims its own slot |
| Game rules (roles, scores, phases) | **server**, via your plug-in |
| World objects | **host**, and the server is slot 0, so the engine's ObjectState path works unmodified |

Accepted consequence: a modified client can teleport. Server-side speed clamping
is the hardening step, and it belongs in a game plug-in rather than here.

| Module | Role |
|---|---|
| `psnl/` | the wire format: framing, CRC, packet types. Shared with the bridge |
| `psnl_server/transport/framing.py` | raw and protobuf adapters; everything above sees clean PSNL bytes |
| `psnl_server/session.py` | per-connection handshake, slot assignment, rebind, dispatch, pacing |
| `psnl_server/rooms.py` | membership, slot allocation, per-scene snapshot groups |
| `psnl_server/server.py` | listeners and the fan-out tick |
| `psnl_server/game.py` | the plug-in seam |
| `psnl_bridge/` | serial to TCP, with end-to-end backpressure |

## Tests

```sh
pytest -q
```

Runs the server's own suite and every game's. A game that breaks the server, or a
server change that breaks a game, fails the same run.

Two of them check agreement with the C++ engine using a corpus the engine
generates. They need a psxsplash checkout, found via `$PSXSPLASH` or assumed to be
a sibling directory; without one they skip with instructions rather than fail. See
`tests/engine_corpus.py` - the only place this repo looks outside itself.

## Is a deployment actually working?

```sh
python tools/smoke_10_consoles.py            # or --host <ip> for a remote box
```

Ten fake consoles handshake over real TCP and wait for snapshots. Unlike the
container healthcheck, which only proves the port accepts a connection, this
catches a listener that is up but not speaking PSNL, and it exits non-zero so it
works as a deployment gate.
