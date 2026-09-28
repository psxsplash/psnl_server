# The PSXSUS spectator

A read-only web view of every **public** room on the server: a front page with
the room list and the server's stats, and a live overhead view of a chosen room
that draws the ship and its players from the same map data and the same sprite
sheet the PlayStation does.

It is part of the **game module**, not the relay. `psnl_server` still knows
nothing about impostors, and a different game built on psxsplash gets no
spectator it did not write. The only thing added to the plug-in seam was a
generic pair of lifecycle hooks (`Game.on_start` / `Game.on_stop`), because
`attach()` runs before there is an event loop to start a listener on.

---

## Running it

Off unless you ask for it. One environment variable turns it on:

```sh
PSXSUS_WEB_PORT=8089 python -m psnl_server --game games.psxsus.game:PSXSUSGame
```

`docker compose up` already sets it; the page is then on <http://localhost:8089/>.

| Variable | Default | What it does |
|---|---|---|
| `PSXSUS_WEB_PORT` | *(unset - off)* | Port to listen on. `0` picks a free one. |
| `PSXSUS_WEB_HOST` | `0.0.0.0` | Interface to bind. |
| `PSXSUS_WEB_STATS` | *(unset)* | Where to keep the win/loss history. Without it the numbers are right but forgotten on restart. |
| `PSXSUS_WEB_TRUST_PROXY` | *(unset)* | Comma-separated addresses whose `X-Real-IP` / `X-Forwarded-For` are believed. Required behind a reverse proxy, or the fair-play warning stops working. |

**Why the default is off.** This view hands out the impostors' identities to
anybody who can reach the port. That is fine when you meant it and never
acceptable as a side effect of attaching the game plug-in. Failing to bind is
also not fatal: the server logs it and keeps relaying, because a spectator page
must never cost ten players their round.

---

## What it shows, and what it will not

Spectators are **omniscient** - that is the point. Roles, positions, vents,
task progress, who voted for whom, and the whole sabotage clock.

Three things are deliberately withheld:

- **Private rooms.** Never listed, and `/api/room/<id>` answers 404 for one even
  if you know the number. Making a room private is how players say they do not
  want strangers, and a spectator is a stranger.
- **Join codes.** Never sent, for any room. A public list of live codes is a
  one-click way to walk into somebody's game.
- **Anything about a console except its game state.** No addresses, no session
  detail. The one place a peer address is read at all is the fair-play check
  below, and it is compared, never reported.

### The fair-play warning

If the browser asking for a page comes from an address the server also has a
console connected from, and that console is in a room, the page says so in a
banner.

It **warns and does not block**, because of how a PlayStation reaches this
server. Real hardware connects through the bridge process on somebody's PC, so
a console's peer address *is* a PC's address - routinely the same PC somebody
else in the house browses from. Behind one NAT every player and every spectator
shares an address. Blocking on that evidence would lock honest people out of
their own spectator page. A warning costs nothing and is right about the case
that matters.

---

## Behind a reverse proxy

`deploy/hestia/` has HestiaCP nginx templates and a walkthrough. Two things
matter whatever proxy is in front:

**Set `PSXSUS_WEB_TRUST_PROXY`.** Every request now arrives from the proxy, so
the fair-play check compares the proxy's address against the consoles' and can
never match — the warning silently stops appearing. Naming the proxy makes
`X-Real-IP` count. It is not automatic on purpose: `X-Forwarded-For` is an
ordinary request header, so believing it from anyone would let a spectator
decide whether the banner applies to them.

**Do not buffer the streams.** SSE needs `proxy_buffering off`, `gzip off` and
`proxy_http_version 1.1` with `Connection ""`. A buffer holds the response until
it has enough to forward, so the page shows nothing and then jumps —
indistinguishable from a frozen server. The app sends `X-Accel-Buffering: no`,
which nginx honours, but a caching layer added in front of it will not care.

---

## Endpoints

| Path | What it answers |
|---|---|
| `/` | The page. Deep links are hash routes (`#/room/3`), so no rewrite is needed. |
| `/api/rooms` | Every public room, with its roster. |
| `/api/room/<id>` | One room, in full. |
| `/api/stats` | Aggregate results plus live counters. |
| `/api/fairplay` | Just the check above, for this client. |
| `/api/stream/rooms` | The front page, live (1Hz). |
| `/api/stream/room/<id>` | One room, live (15Hz). |
| `/healthz` | `{"ok": true}`. |

The live feeds are **Server-Sent Events**, not WebSockets. Nothing ever flows
from the browser to the server here, and SSE over a plain GET needs no upgrade
handshake, no frame masking and no close-code state machine - and the browser
reconnects by itself. The whole transport is `httpd.Stream`.

There are no runtime dependencies. `psnl` declares none, and a spectator page is
not worth making aiohttp a requirement of running a PlayStation relay, so
`httpd.py` is `asyncio.start_server` and about two hundred lines. GET and HEAD
only, one request per connection, no bodies.

---

## How the view is built

Almost nothing is stored twice. The read model (`spectator.py`) assembles each
frame from whatever is already authoritative:

| What | Where it really lives |
|---|---|
| Where everyone is standing | `psnl_server.rooms.Member.avatar` (client-authored) |
| Roles, aliveness, vents, tasks, meeting clock, sabotage | `games.psxsus.match.Match` |
| Who is in which room, who owns it, which ship | `PSXSUSGame` |

Four things genuinely are not recoverable that way, and they are the only state
this module keeps:

1. **Where a body is lying.** The server knows one position per slot, and after
   a kill that slot's console is reporting its *ghost*. The victim's last live
   position is captured at the moment of death - which is where they fell,
   accurate to one snapshot. (`scene_game.lua` does exactly the same thing, and
   for exactly the same reason.)
2. **Who called a meeting, and over whose body.**
3. **The vote tally and the ejection.**
4. **How the last round ended** - `_end_match` drops the Match, so without this
   the results screen the players are still looking at would already have
   vanished here.

All four arrive through **one seam**: `Match.emit` is a match's only way to
speak, so `Spectator.observe` taps it in `PSXSUSGame._begin_game`. Nothing in
`match.py` knows anybody is watching, and nothing had to be added to it.

### Rendering 1:1

The browser is not given a description of the ship; it is given the ship.
`PSXSUS/Art/make_web.py` reads the very `MapBuilder` objects `make_maps.py`
builds the Unity tilemaps from and writes `static/maps/mapN.json`, then copies
`tileset.png` and `crew.png` straight out of `Assets/Art`. Change a wall and
re-run the pipeline, and the disc and the page move together.

Everything the renderer has to agree with the console about is cited in
`static/app.js` next to the code that depends on it:

- **Positions are fp12.** The console sends `psyqo::FixedPoint::value`; one
  world pixel is 4096 of them. The Lua divides by the same number in `toPx`.
- **Tints are `texel * colour / 128`**, so 128 is the identity and the crew art
  is drawn at 128 grey for that reason. One pass over the pixels per colour, at
  load.
- **Sprite offsets.** Avatars are bound at `(-8, -12)`; a corpse is placed at
  the raw position with no offset. That asymmetry is real - the body genuinely
  is drawn down-right of where the player stood - and it is reproduced rather
  than tidied up.
- **Walk cycles come from movement**, dominant axis, no yaw, held for 133ms
  after the last movement seen. That is what `player_control.lua`'s
  `animateRemotes` does with the positions snapshots give it, and the server
  gives the browser the same thing.
- **Animation ranges** are `CREW_ANIMS` from `make_sheets.py`.

`tests/psxsus/test_spectator.py` checks the copies that cannot import each
other - the per-slot colour table exists in Lua, Python and JavaScript, and the
test fails if any of the three drifts.

A vented player is drawn faintly and ringed rather than hidden. In the game
they are invisible, which is the whole point of a vent; a spectator is not a
player, and "hiding in a vent" is exactly the sort of thing this view exists to
show.

---

## Regenerating the assets

`static/maps/` and `static/art/` are **generated**. Do not hand-edit them.

```sh
python PSXSUS/Art/make_web.py
```

It is the last step of the content pipeline and it reads the previous ones'
output, so run it after `make_art.py` / `make_sheets.py` / `make_maps.py`. See
`PSXSUS/CONTENT_PIPELINE.md`.

They also have to ship in the wheel - the container installs one - which is
what `[tool.setuptools.package-data]` in `server/pyproject.toml` is for.
`test_every_static_file_is_declared_as_package_data` fails if a new asset
directory is not covered, because the symptom otherwise is a server that works
from a checkout and answers 404 for every asset in production.
