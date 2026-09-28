/* PSXSUS spectator.
 *
 * Two views behind a hash router, and one renderer that has to agree with a
 * PlayStation. Everywhere that agreement is load-bearing, the console's own
 * source is cited - if you change one, change the other.
 */

// ------------------------------------------------------------------ helpers

const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2).toLowerCase(), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return node;
}

const rgb = (c) => `rgb(${c[0]},${c[1]},${c[2]})`;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const pct = (n, d) => (d ? Math.round((100 * n) / d) : 0);

function duration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

const conn = $('#conn');
const setConn = (state) => { conn.dataset.state = state; conn.textContent = state; };

/** One SSE subscription that survives view changes by being closed on them. */
function subscribe(url, onMessage, onNamed = {}) {
  const source = new EventSource(url);
  source.onopen = () => setConn('live');
  source.onerror = () => setConn('lost');
  source.onmessage = (e) => {
    setConn('live');
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    onMessage(data);
  };
  for (const [name, fn] of Object.entries(onNamed)) source.addEventListener(name, fn);
  return source;
}

// --------------------------------------------------------------- fair play

/* Shown whenever the server recognises this browser's address as one a console
 * is connected from. It warns and never blocks - see Spectator.fair_play for
 * why the evidence does not support blocking. */
function renderFairPlay(fair) {
  const box = $('#fairplay');
  if (!fair || !fair.warn) { box.hidden = true; box.replaceChildren(); return; }
  const where = fair.rooms.length === 1
    ? `room ${fair.rooms[0]}`
    : `rooms ${fair.rooms.join(', ')}`;
  box.hidden = false;
  box.replaceChildren(
    el('span', { class: 'icon', 'aria-hidden': 'true' }, '⚠'),
    el('div', {},
      el('strong', {}, 'Fair play. '),
      `Someone at your network address is playing right now (${where}). ` +
      'This page shows who the impostors are, where everyone is standing and ' +
      'what every vote was. Watching a game you or someone next to you is in ' +
      'takes it away from the other players.',
    ),
  );
}

// ------------------------------------------------------------------- home

function statTile(label, value, sub) {
  return el('div', { class: 'card tile' },
    el('div', { class: 'label' }, label),
    el('div', { class: 'value' }, value),
    sub ? el('div', { class: 'sub' }, sub) : null,
  );
}

/* Share of wins. A stacked pair, so it carries BOTH required forms of
 * secondary encoding: a 2px gap between fills (in app.css) and a direct text
 * label on each side that also serves as the legend. Never colour alone. */
function winMeter(stats) {
  const total = stats.crewWins + stats.impostorWins;
  const track = el('div', { class: total ? 'meter-track' : 'meter-track empty' });
  if (total) {
    track.append(
      el('i', { class: 'meter-fill crew', style: `flex:${stats.crewWins}` }),
      el('i', { class: 'meter-fill impostor', style: `flex:${stats.impostorWins}` }),
    );
  }
  return el('section', { class: 'card meter' },
    el('h2', {}, 'Who wins'),
    track,
    el('div', { class: 'meter-keys' },
      el('div', { class: 'meter-key' },
        el('span', { class: 'swatch crew' }),
        'Crewmates',
        el('b', {}, ` ${stats.crewWinRate}%`),
        el('span', {}, ` (${stats.crewWins})`),
      ),
      el('div', { class: 'meter-key' },
        el('span', { class: 'swatch impostor' }),
        'Impostors',
        el('b', {}, ` ${stats.impostorWinRate}%`),
        el('span', {}, ` (${stats.impostorWins})`),
      ),
    ),
  );
}

function mapTable(stats) {
  if (!stats.byMap.length) return el('div', { class: 'empty' }, 'No finished rounds yet.');
  const rows = stats.byMap.map((m) => {
    const crewWins = m.games - m.impostorWins;
    return el('tr', {},
      el('td', { class: 'name' }, stats.mapNames[m.map] || `MAP ${m.map + 1}`),
      el('td', {}, String(m.games)),
      el('td', {}, `${m.impostorWinRate}%`),
      el('td', {},
        el('div', {
          class: 'minibar',
          title: `${crewWins} crew / ${m.impostorWins} impostor`,
        },
          el('i', { class: 'crew', style: `flex:${crewWins || 0.001}` }),
          el('i', { class: 'impostor', style: `flex:${m.impostorWins || 0.001}` }),
        ),
      ),
    );
  });
  return el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, 'Ship'), el('th', {}, 'Rounds'),
      el('th', {}, 'Impostor wins'), el('th', {}, 'Split'))),
    el('tbody', {}, rows),
  );
}

const REASON_TEXT = {
  TASKS: 'all tasks done', EJECTED: 'last impostor ejected',
  KILLED: 'crew wiped out', SABOTAGE: 'sabotage ran out', LEFT: 'last impostor left',
};

function recentTable(stats) {
  if (!stats.recent.length) return el('div', { class: 'empty' }, 'No finished rounds yet.');
  const rows = stats.recent.map((g) => {
    const name = stats.reasonNames[g.reason] || '';
    return el('tr', {},
      el('td', {}, ago(g.ended_at)),
      el('td', { class: 'name' }, stats.mapNames[g.map_id] || `MAP ${g.map_id + 1}`),
      el('td', {}, `${g.players}p`),
      el('td', {}, duration(g.duration)),
      el('td', {},
        el('span', { class: `chip ${g.winner ? 'impostor' : 'crew'}` },
          g.winner ? 'IMPOSTOR' : 'CREW'),
        el('span', { style: 'color:var(--ink-3);margin-left:8px' },
          REASON_TEXT[name] || name.toLowerCase()),
      ),
    );
  });
  return el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, 'When'), el('th', {}, 'Ship'), el('th', {}, 'Size'),
      el('th', {}, 'Length'), el('th', {}, 'Result'))),
    el('tbody', {}, rows),
  );
}

const PHASE_TEXT = {
  lobby: 'In lobby', playing: 'Playing', meeting: 'Meeting', over: 'Round over',
};

function roomCard(room) {
  return el('a', {
    class: 'card room-card',
    href: `#/room/${room.id}`,
    'aria-label': `Watch ${room.name || `room ${room.id}`}`,
  },
    el('div', { class: 'room-head' },
      el('span', { class: 'rname' }, room.name || `Room ${room.id}`),
      el('span', { class: 'rmap' }, room.mapName),
      el('span', { class: `rstate state-${room.phase}` },
        room.countdown ? `Starting in ${room.countdown}s`
          : room.phase === 'playing' ? `Playing · ${duration(room.elapsed)}`
            : PHASE_TEXT[room.phase]),
    ),
    el('div', { class: 'crew-strip' },
      room.roster.map((p) => el('span', { class: 'pip' },
        el('span', { class: 'dot', style: `background:${rgb(p.colour)}` }),
        p.name || `Slot ${p.slot}`)),
      el('span', { class: 'pip' }, `${room.players}/10`),
    ),
    // A whole card being a link is not obvious until you happen to hover it,
    // so it says so.
    el('div', { class: 'room-go' }, 'Watch ', el('span', { 'aria-hidden': 'true' }, '→')),
  );
}

function renderHome(view) {
  view.replaceChildren(el('div', { class: 'loading' }, 'Loading…'));
  let built = false;
  let nodes = {};

  const source = subscribe('/api/stream/rooms', (data) => {
    const stats = data.stats;
    renderFairPlay(data.fairPlay);

    $('#live-strip').replaceChildren(
      el('span', {}, 'Online ', el('b', {}, String(stats.live.connected))),
      el('span', {}, 'Rooms ', el('b', {}, String(stats.live.rooms))),
      el('span', {}, 'In game ', el('b', {}, String(stats.live.playing))),
    );

    if (!built) {
      built = true;
      nodes = {
        tiles: el('div', { class: 'tiles' }),
        meter: el('div'),
        maps: el('section', { class: 'card section' }, el('h2', {}, 'By ship')),
        recent: el('section', { class: 'card section' }, el('h2', {}, 'Recent rounds')),
        rooms: el('div', { class: 'rooms' }),
      };
      view.replaceChildren(
        el('div', { class: 'home' },
          el('div', {},
            el('h2', {}, 'Live rooms'),
            nodes.rooms,
          ),
          el('div', {},
            nodes.tiles, nodes.meter, nodes.maps, nodes.recent,
          ),
        ),
      );
    }

    nodes.tiles.replaceChildren(
      statTile('Rounds played', String(stats.games)),
      statTile('Impostor wins', `${stats.impostorWinRate}%`,
        `${stats.impostorWins} of ${stats.games || 0}`),
      statTile('Avg lobby', String(stats.avgPlayers), 'players per round'),
      statTile('Avg length', duration(stats.avgDuration)),
    );
    nodes.meter.replaceChildren(winMeter(stats));
    nodes.maps.replaceChildren(el('h2', {}, 'By ship'), mapTable(stats));
    nodes.recent.replaceChildren(el('h2', {}, 'Recent rounds'), recentTable(stats));

    // SPREAD, because replaceChildren takes nodes as separate arguments: handed
    // an array it stringifies it, and the room list rendered as the text
    // "[object HTMLAnchorElement]" with nothing to click.
    nodes.rooms.replaceChildren(
      ...(data.rooms.length
        ? data.rooms.map(roomCard)
        : [el('div', { class: 'card section empty' },
          'Nobody is playing right now. Public rooms appear here the moment ' +
          'somebody hosts one.')]),
    );
  });

  return () => source.close();
}

// -------------------------------------------------------------- renderer

/* THE SPRITE SHEET CONTRACT.
 *
 * (first cell, cell count, frames per cell) for each animation, copied from
 * CREW_ANIMS in PSXSUS/Art/make_sheets.py, which is generated from the CREW
 * table make_art.py draws. Durations are in 60Hz vsync frames because that is
 * what the console counts. */
const ANIM = {
  idle: [0, 2, 30], walk_down: [4, 4, 6], walk_up: [8, 4, 6],
  walk_left: [12, 4, 6], walk_right: [16, 4, 6], dead: [20, 1, 1],
  ghost_idle: [24, 2, 30], ghost: [28, 4, 8],
};
const CELL = 16;        // one sprite cell, in world pixels
const CREW_COLS = 8;    // crew.png is 128x128 of 16x16 cells
const TILE_COLS = 16;   // tileset.png is 256x128 of 16x16 cells

/* Per-slot avatar tints. The same list, in the same order, as COLOURS in
 * Assets/Lua/scene_game.lua and in web/spectator.py - the server sends each
 * player's colour with every state, but the tinted sheets have to be built
 * before any state has arrived. tests/psxsus/test_spectator.py checks that the
 * Lua and the Python agree; this is the third copy and it is checked there too. */
const COLOURS = [
  [255, 40, 40], [40, 80, 255], [40, 200, 40], [255, 130, 200],
  [255, 150, 40], [240, 240, 60], [60, 60, 60], [240, 240, 240],
  [140, 60, 220], [60, 220, 220],
];

/* Why a round ended, by name - protocol.OverReason's own order. The stats feed
 * sends these names, but a room feed carries only the number. */
const OVER_REASONS = ['TASKS', 'EJECTED', 'KILLED', 'SABOTAGE', 'LEFT'];

/* Bound sprite offsets, from scene_game.lua:
 *   avatars  Sprite.BindToActor(id, actor, 0, -8, -12)
 *   bodies   Sprite.SetPos(id, bx, bz)          (no offset - the corpse really
 *                                                is drawn down-right of where
 *                                                the player was standing) */
const AVATAR_OFF_X = -8, AVATAR_OFF_Z = -12;

/* How long a remote avatar keeps walking after the last movement seen.
 * player_control.lua holds it for WALK_HOLD_UPDATES (4) updates of a 30Hz
 * loop; the same 133ms here, so the walk cycle neither strobes nor trails. */
const WALK_HOLD_MS = 133;

/* How fast a drawn position catches up to the one the server reported. The
 * feed is 15Hz and the eye is not, so without this every avatar steps. */
const INTERP_MS = 90;

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`could not load ${src}`));
    img.src = src;
  });
}

/* The PlayStation's tint, exactly: `final = texel * colour / 128`, so 128 is
 * the identity and the crew art is drawn at 128 grey for that reason (see
 * make_art.py). Canvas has no blend mode that does this, so it is one pass over
 * the pixels, once per colour, at load. */
function tintSheet(img, colour) {
  const c = document.createElement('canvas');
  c.width = img.width; c.height = img.height;
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  const image = ctx.getImageData(0, 0, c.width, c.height);
  const p = image.data;
  const [r, g, b] = colour;
  for (let i = 0; i < p.length; i += 4) {
    if (p[i + 3] === 0) continue;
    p[i] = Math.min(255, (p[i] * r) / 128);
    p[i + 1] = Math.min(255, (p[i + 1] * g) / 128);
    p[i + 2] = Math.min(255, (p[i + 2] * b) / 128);
  }
  ctx.putImageData(image, 0, 0);
  return c;
}

/** The whole ship, drawn once into an offscreen canvas. */
function prerenderMap(map, tileset) {
  const T = map.tile;
  const c = document.createElement('canvas');
  c.width = map.w * T; c.height = map.h * T;
  const ctx = c.getContext('2d');
  ctx.imageSmoothingEnabled = false;
  for (let y = 0; y < map.h; y++) {
    for (let x = 0; x < map.w; x++) {
      const t = map.tiles[y * map.w + x];
      if (t < 0) continue;    // empty cell: this is what puts the ship in space
      ctx.drawImage(tileset, (t % TILE_COLS) * T, Math.floor(t / TILE_COLS) * T,
        T, T, x * T, y * T, T, T);
    }
  }
  return c;
}

class MapView {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.map = null;
    this.mapCanvas = null;
    this.sheets = [];         // one tinted crew sheet per slot colour
    this.state = null;
    this.showAvatars = false; // only once a round is actually running
    this.tracks = new Map();  // slot -> drawn position + walk cycle
    this.view = { cx: 0, cy: 0, zoom: 2, fit: 2 };
    this.follow = null;
    this.show = { names: true, rooms: true, roles: true };
    this.last = performance.now();
    this._drag = null;
    this._bind();
  }

  async load(mapId, colours) {
    const [map, tileset, crew] = await Promise.all([
      fetch(`/maps/map${mapId}.json`).then((r) => r.json()),
      loadImage('/art/tileset.png'),
      loadImage('/art/crew.png'),
    ]);
    this.map = map;
    this.mapCanvas = prerenderMap(map, tileset);
    this.sheets = colours.map((c) => tintSheet(crew, c));
    this.fit();
  }

  // --- camera -------------------------------------------------------

  size() {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(this.canvas.clientWidth * dpr));
    const h = Math.max(1, Math.round(this.canvas.clientHeight * dpr));
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w; this.canvas.height = h;
    }
    return [w, h, dpr];
  }

  fit() {
    if (!this.map) return;
    const [w, h, dpr] = this.size();
    const world = [this.map.w * this.map.tile, this.map.h * this.map.tile];
    // A little margin, so the hull is not flush against the frame.
    const z = Math.min(w / world[0], h / world[1]) * 0.94;
    this.view.fit = z / dpr;
    this.view.zoom = z / dpr;
    this.view.cx = world[0] / 2;
    this.view.cy = world[1] / 2;
    this.follow = null;
  }

  toWorld(sx, sy) {
    const [w, h, dpr] = this.size();
    const z = this.view.zoom * dpr;
    return { x: (sx * dpr - w / 2) / z + this.view.cx, y: (sy * dpr - h / 2) / z + this.view.cy };
  }

  zoomBy(factor, atX, atY) {
    const before = this.toWorld(atX, atY);
    this.view.zoom = clamp(this.view.zoom * factor, this.view.fit * 0.75, 10);
    const after = this.toWorld(atX, atY);
    this.view.cx += before.x - after.x;
    this.view.cy += before.y - after.y;
  }

  _bind() {
    const c = this.canvas;
    c.addEventListener('wheel', (e) => {
      e.preventDefault();
      const r = c.getBoundingClientRect();
      this.follow = null;
      this.zoomBy(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });

    c.addEventListener('pointerdown', (e) => {
      c.setPointerCapture(e.pointerId);
      this._drag = { x: e.clientX, y: e.clientY, moved: false };
      c.classList.add('dragging');
    });
    c.addEventListener('pointermove', (e) => {
      if (!this._drag) return;
      const dx = e.clientX - this._drag.x, dy = e.clientY - this._drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) this._drag.moved = true;
      this._drag.x = e.clientX; this._drag.y = e.clientY;
      this.follow = null;    // taking the wheel turns off follow
      this.view.cx -= dx / this.view.zoom;
      this.view.cy -= dy / this.view.zoom;
    });
    const end = (e) => {
      if (this._drag) c.releasePointerCapture?.(e.pointerId);
      this._drag = null;
      c.classList.remove('dragging');
    };
    c.addEventListener('pointerup', end);
    c.addEventListener('pointercancel', end);
    c.addEventListener('dblclick', () => this.fit());
  }

  // --- state --------------------------------------------------------

  update(state) {
    this.state = state;
    this.showAvatars = Boolean(state.inProgress);
    if (!this.showAvatars) {
      // Drop the walk cycles too, so returning to the map after a round does
      // not start everybody mid-stride from where they died.
      this.tracks.clear();
      return;
    }
    const now = performance.now();
    for (const p of state.players) {
      let t = this.tracks.get(p.slot);
      if (!t) {
        t = { x: p.x, z: p.z, tx: p.x, tz: p.z, dir: 'walk_down', until: 0, since: now };
        this.tracks.set(p.slot, t);
      }
      // The direction is read from the movement the SERVER reported, which is
      // exactly what player_control.lua's animateRemotes does with the
      // positions snapshots give it - dominant axis, no yaw, because a top-down
      // game has a heading and not a rotation.
      const dx = p.x - t.tx, dz = p.z - t.tz;
      if (dx !== 0 || dz !== 0) {
        if (dx < 0) t.dir = 'walk_left';
        else if (dx > 0) t.dir = 'walk_right';
        else if (dz < 0) t.dir = 'walk_up';
        else t.dir = 'walk_down';
        t.until = now + WALK_HOLD_MS;
      }
      t.tx = p.x; t.tz = p.z;
    }
    for (const slot of [...this.tracks.keys()]) {
      if (!state.players.some((p) => p.slot === slot)) this.tracks.delete(slot);
    }
  }

  // --- drawing ------------------------------------------------------

  frame() {
    const now = performance.now();
    const dt = Math.min(100, now - this.last);
    this.last = now;
    if (!this.map) return;

    const [w, h, dpr] = this.size();
    const ctx = this.ctx;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, w, h);

    // Catch each drawn position up to the reported one.
    const k = 1 - Math.exp(-dt / INTERP_MS);
    for (const t of this.tracks.values()) {
      t.x += (t.tx - t.x) * k;
      t.z += (t.tz - t.z) * k;
    }

    if (this.follow !== null) {
      const t = this.tracks.get(this.follow);
      if (t) { this.view.cx = t.x; this.view.cy = t.z; }
    }

    const z = this.view.zoom * dpr;
    ctx.translate(w / 2, h / 2);
    ctx.scale(z, z);
    ctx.translate(-this.view.cx, -this.view.cy);

    ctx.drawImage(this.mapCanvas, 0, 0);

    if (this.show.rooms) this.drawRoomLabels(ctx, z);
    // NOT while the room is in its lobby. Nobody has an avatar there - the
    // lobby scene maps no actors, so every console is still reporting the zero
    // it spawned with, and drawing that piles ten crewmates into the top-left
    // corner of a ship none of them is standing on.
    if (this.state && this.showAvatars) {
      this.drawBodies(ctx);
      this.drawPlayers(ctx, now, z);
    }
  }

  drawRoomLabels(ctx, z) {
    const T = this.map.tile;
    // Screen-constant type: a label that scaled with the map would be a smear
    // at fit zoom and a billboard at 8x.
    ctx.save();
    ctx.font = `600 ${11 / z}px system-ui, sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = 'rgba(232,237,245,0.34)';
    for (const [name, [x, y, w, h]] of Object.entries(this.map.rooms)) {
      ctx.fillText(name.replace(/_/g, ' ').toUpperCase(),
        (x + w / 2) * T, (y + h / 2) * T);
    }
    ctx.restore();
  }

  drawBodies(ctx) {
    for (const body of this.state.bodies) {
      const sheet = this.sheets[(body.slot - 1) % this.sheets.length];
      const [first] = ANIM.dead;
      // No offset: the console's corpse sprite is placed at the raw position
      // (scene_game.lua), unlike a bound avatar.
      this.blit(ctx, sheet, first, body.x, body.z, 1);
    }
  }

  drawPlayers(ctx, now, z) {
    for (const p of this.state.players) {
      const t = this.tracks.get(p.slot);
      if (!t) continue;
      const sheet = this.sheets[(p.slot - 1) % this.sheets.length];
      const walking = now < t.until;
      // GameAnimFor(slot, walkAnim): the dead get the ghost silhouette, and
      // there is one ghost walk cycle rather than four.
      const anim = p.alive ? (walking ? t.dir : 'idle') : (walking ? 'ghost' : 'ghost_idle');
      const [first, count, per] = ANIM[anim];
      const frame = first + Math.floor((now * 0.06) / per) % count;

      // A vented player is invisible in the game. A spectator is not a player,
      // so they are drawn - faintly, and ringed, because "hiding in a vent" is
      // information the view exists to show.
      const alpha = p.vented ? 0.35 : (p.alive ? 1 : 0.55);

      if (this.show.roles && p.impostor && p.alive) {
        ctx.save();
        ctx.strokeStyle = 'rgba(230,103,103,0.9)';
        ctx.lineWidth = 1.5 / z;
        ctx.beginPath();
        ctx.ellipse(t.x, t.z + 2, 9, 5, 0, 0, Math.PI * 2);
        ctx.stroke();
        ctx.restore();
      }
      if (p.vented) {
        ctx.save();
        ctx.strokeStyle = 'rgba(201,133,0,0.8)';
        ctx.lineWidth = 1 / z;
        ctx.setLineDash([2 / z, 2 / z]);
        ctx.beginPath();
        ctx.ellipse(t.x, t.z + 2, 10, 6, 0, 0, Math.PI * 2);
        ctx.stroke();
        ctx.restore();
      }

      this.blit(ctx, sheet, frame, t.x + AVATAR_OFF_X, t.z + AVATAR_OFF_Z, alpha);

      if (this.show.names && z > 1.1) {
        ctx.save();
        ctx.font = `600 ${9 / z}px system-ui, sans-serif`;
        ctx.textAlign = 'center';
        ctx.lineWidth = 3 / z;
        ctx.strokeStyle = 'rgba(0,0,0,0.85)';
        ctx.fillStyle = p.alive ? '#e8edf5' : '#9daabd';
        const label = p.name || `SLOT ${p.slot}`;
        ctx.strokeText(label, t.x, t.z - 14);
        ctx.fillText(label, t.x, t.z - 14);
        ctx.restore();
      }
    }
  }

  /* One sprite cell, at an INTEGER world position - the console's sprite system
   * places sprites at whole pixels (scene_game.lua's `toPx` floors on the way
   * in), so rounding here is what keeps the pixel grid from shimmering. */
  blit(ctx, sheet, frame, x, y, alpha) {
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.drawImage(sheet,
      (frame % CREW_COLS) * CELL, Math.floor(frame / CREW_COLS) * CELL, CELL, CELL,
      Math.round(x), Math.round(y), CELL, CELL);
    ctx.restore();
  }
}

// ------------------------------------------------------------- room view

const SABOTAGE_TEXT = {
  LIGHTS: 'Lights out', COMMS: 'Comms down', REACTOR: 'Reactor meltdown', OXYGEN: 'Oxygen depleted',
};

function eventText(ev, byName) {
  const who = (slot) => byName(slot) || `slot ${slot}`;
  switch (ev.kind) {
    case 'start': return `Round begins · ${ev.players} players, ${ev.impostors} impostor(s)`;
    case 'died': return `${who(ev.slot)} was killed`;
    case 'meeting': return ev.reason === 0
      ? `${who(ev.by)} reported ${who(ev.body)}'s body`
      : `${who(ev.by)} called an emergency meeting`;
    case 'tally': return 'Votes are in';
    case 'ejected': return ev.slot === 255
      ? 'Nobody was ejected'
      : `${who(ev.slot)} was ejected — ${ev.wasImpostor ? 'an impostor' : 'not an impostor'}`;
    case 'meetingEnd': return 'Back to the ship';
    // `ev.name` is the sabotage's name; `ev.kind` is the EVENT's kind. The
    // server sends both because the two words collide otherwise.
    case 'sabotage': return `${SABOTAGE_TEXT[ev.name] || 'Sabotage'} — sabotaged`;
    case 'sabotageEnd': return `${SABOTAGE_TEXT[ev.name] || 'Sabotage'} — repaired`;
    case 'vent': return `${who(ev.slot)} ${ev.vented ? 'went into' : 'came out of'} a vent`;
    case 'over': return `${ev.winner ? 'Impostors' : 'Crewmates'} win`;
    default: return ev.kind;
  }
}

/* What the stage shows between rounds. There is nothing to draw on the map -
 * nobody has an avatar until a round starts - so the ship sits behind this as a
 * backdrop and the roster is the content, which is also what the players
 * themselves are looking at on their room screen. */
function lobbyPanel(state) {
  return el('div', { class: 'vote-modal lobby-modal' },
    el('div', { class: 'vote-box' },
      el('h3', {}, state.countdown ? `Starting in ${state.countdown}` : 'In the lobby'),
      el('div', { class: 'why' },
        `${state.mapName} · ${state.players.length} of 10 ` +
        (state.countdown ? '· everyone is loading in'
          : state.players.length < 4
            ? '· needs 4 to start'
            : '· waiting for the host')),
      state.players.map((p) => el('div', { class: 'vote-row' },
        el('span', { class: 'dot', style: `background:${rgb(p.colour)}` }),
        el('span', { class: 'who' }, p.name || `Slot ${p.slot}`),
        p.owner ? el('span', { class: 'status' }, 'host') : null)),
    ),
  );
}

function voteModal(state, byName) {
  const meeting = state.meeting;
  if (!meeting) return null;
  const alive = state.players.filter((p) => p.alive);
  const rows = alive.map((p) => {
    const votes = meeting.tally[p.slot] || 0;
    return el('div', { class: 'vote-row' },
      el('span', { class: 'dot', style: `background:${rgb(p.colour)}` }),
      el('span', { class: 'who' }, p.name || `Slot ${p.slot}`),
      // Who each player voted for, which the players themselves never see. A
      // spectator does: that is the point of the view, and the reason the
      // fair-play banner exists.
      p.votedFor !== null && p.votedFor !== undefined
        ? el('span', { class: 'status' },
          p.votedFor === 255 ? 'skipped' : `→ ${byName(p.votedFor) || p.votedFor}`)
        : el('span', { class: 'status' }, meeting.votingOpen ? 'thinking' : 'discussing'),
      votes ? el('span', { class: 'tally' }, String(votes)) : null,
    );
  });

  return el('div', { class: 'vote-modal' },
    el('div', { class: 'vote-box' },
      el('h3', {}, meeting.votingOpen ? 'Voting' : 'Discussion'),
      el('div', { class: 'why' },
        (meeting.reason === 0
          ? `${byName(meeting.by) || 'Someone'} reported `
            + (byName(meeting.body) ? `${byName(meeting.body)}'s body` : 'a body')
          : `${byName(meeting.by) || 'Someone'} called an emergency meeting`)
        + ` · ${meeting.seconds}s`),
      rows,
      meeting.ejected
        ? el('div', { class: 'vote-result' },
          meeting.ejected.slot === 255
            ? 'Nobody was ejected.'
            : `${byName(meeting.ejected.slot)} was ejected — `
            + (meeting.ejected.wasImpostor ? 'an impostor.' : 'not an impostor.'))
        : null,
    ),
  );
}

function renderRoom(view, roomId) {
  const canvas = el('canvas');
  const stage = el('div', { class: 'stage' }, canvas);
  const bar = el('div', { class: 'stage-bar' });
  const tools = el('div', { class: 'tools' });
  const overlay = el('div');
  stage.append(bar, tools, overlay);

  const side = el('div', { class: 'side' });
  const title = el('h2', {}, `Room ${roomId}`);

  view.replaceChildren(
    el('a', { class: 'back', href: '#/' }, '← All rooms'),
    el('div', { class: 'roomview' },
      el('div', {}, title, stage),
      side),
  );

  const map = new MapView(canvas);
  let loadedMap = null;
  let loading = null;
  let running = true;
  let latest = null;
  let lastEvents = [];

  const toggle = (label, key) => {
    const b = el('button', { type: 'button', 'aria-pressed': String(map.show[key]) },
      label);
    b.addEventListener('click', () => {
      map.show[key] = !map.show[key];
      b.setAttribute('aria-pressed', String(map.show[key]));
    });
    return b;
  };
  /* Fullscreen.
   *
   * Two things have to happen together and both are easy to miss. The stage
   * element is what goes fullscreen (not the canvas: the badges, the tool bar
   * and the vote modal are its children and would be left behind on the page),
   * and the canvas has to be re-fitted afterwards - its backing store is sized
   * from clientWidth, which does not change until the browser has finished the
   * transition, so fitting on the click gives a map sized for the old box. */
  const fullBtn = el('button', { type: 'button', title: 'Fullscreen (F)' }, 'Fullscreen');
  const isFull = () => document.fullscreenElement === stage;
  const toggleFull = async () => {
    try {
      if (isFull()) await document.exitFullscreen();
      else await stage.requestFullscreen();
    } catch {
      /* Refused (no user gesture, or an iframe without allowfullscreen).
         Nothing is broken; the map is still there in the page. */
    }
  };
  fullBtn.addEventListener('click', toggleFull);
  const onFullChange = () => {
    fullBtn.textContent = isFull() ? 'Exit' : 'Fullscreen';
    fullBtn.setAttribute('aria-pressed', String(isFull()));
    // After the transition, not during it.
    requestAnimationFrame(() => requestAnimationFrame(() => map.fit()));
  };
  document.addEventListener('fullscreenchange', onFullChange);

  // F for fullscreen; Escape is the browser's own and needs nothing from us.
  const onKey = (e) => {
    if (e.key === 'f' || e.key === 'F') {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      toggleFull();
    }
  };
  document.addEventListener('keydown', onKey);

  tools.append(
    el('button', { type: 'button', onclick: () => map.zoomBy(1.3, canvas.clientWidth / 2, canvas.clientHeight / 2) }, '+'),
    el('button', { type: 'button', onclick: () => map.zoomBy(1 / 1.3, canvas.clientWidth / 2, canvas.clientHeight / 2) }, '−'),
    el('button', { type: 'button', onclick: () => map.fit() }, 'Fit'),
    toggle('Names', 'names'),
    toggle('Rooms', 'rooms'),
    toggle('Roles', 'roles'),
    fullBtn,
  );

  const loop = () => {
    if (!running) return;
    map.frame();
    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);

  const byName = (slot) => {
    const p = latest && latest.players.find((q) => q.slot === slot);
    return p ? (p.name || `Slot ${slot}`) : null;
  };

  const source = subscribe(`/api/stream/room/${roomId}`, async (state) => {
    latest = state;
    renderFairPlay(state.fairPlay);
    title.textContent = `${state.name || `Room ${state.id}`} · ${state.mapName}`;

    // Guarded by a promise, not just the id: state arrives at 15Hz and a map
    // takes longer than 66ms to fetch and pre-render, so an unguarded check
    // starts the same load several times over.
    if (loadedMap !== state.map && !loading) {
      loadedMap = state.map;
      loading = map.load(state.map, COLOURS).finally(() => { loading = null; });
    }
    if (loading) await loading;
    map.update(state);

    // --- the bar over the map
    const chips = [el('span', { class: 'badge' }, PHASE_TEXT[state.phase])];
    if (state.countdown) chips.push(el('span', { class: 'badge alert' }, `Starting in ${state.countdown}s`));
    if (state.match) {
      chips.push(el('span', { class: 'badge' },
        `Tasks ${state.match.tasksDone}/${state.match.tasksTotal}`));
      chips.push(el('span', { class: 'badge' },
        `${state.match.crewAlive} crew · ${state.match.impostorsAlive} impostor`));
      chips.push(el('span', { class: 'badge' }, duration(state.match.elapsed)));
      if (state.match.sabotage) {
        const s = state.match.sabotage;
        chips.push(el('span', { class: s.seconds ? 'badge crit' : 'badge alert' },
          SABOTAGE_TEXT[s.name] + (s.seconds ? ` ${s.seconds}s` : '')));
      }
    }
    bar.replaceChildren(...chips);

    // --- overlays
    const parts = [];
    const modal = state.inProgress ? voteModal(state, byName) : lobbyPanel(state);
    if (modal) parts.push(modal);
    else if (state.result) {
      // The meeting is over but nobody has been released yet: the consoles are
      // holding the result on screen, so this holds it too.
      parts.push(el('div', { class: 'vote-modal' },
        el('div', { class: 'vote-box' },
          el('h3', {}, 'Result'),
          el('div', { class: 'vote-result' },
            state.result.ejected.slot === 255
              ? 'Nobody was ejected.'
              : `${byName(state.result.ejected.slot)} was ejected — `
              + (state.result.ejected.wasImpostor ? 'an impostor.' : 'not an impostor.')))));
    }
    if (state.over) {
      parts.push(el('div', { class: `over-banner ${state.over.winner ? 'impostor' : 'crew'}` },
        state.over.winner ? 'Impostors win' : 'Crewmates win',
        el('small', {}, REASON_TEXT[latestReason(state)] || '')));
    }
    overlay.replaceChildren(...parts);

    // --- the sidebar
    const rows = state.players.map((p) => {
      const row = el('button', {
        class: `prow${p.alive ? '' : ' dead'}`,
        type: 'button',
        'aria-pressed': String(map.follow === p.slot),
      },
        el('span', { class: 'dot', style: `background:${rgb(p.colour)}` }),
        el('span', { class: 'pname' }, p.name || `Slot ${p.slot}`),
        p.impostor ? el('span', { class: 'ptag imp' }, 'IMPOSTOR') : null,
        p.vented ? el('span', { class: 'ptag vent' }, 'VENT') : null,
        !p.impostor && p.tasks
          ? el('span', { class: 'ptasks' }, `${p.tasksDone}/${p.tasks}`)
          : null,
      );
      row.addEventListener('click', () => {
        map.follow = map.follow === p.slot ? null : p.slot;
        if (map.follow !== null) map.view.zoom = Math.max(map.view.zoom, 3);
      });
      return row;
    });

    const feed = (state.events || lastEvents).map((ev) => el('div', { class: 'feed-row' },
      el('time', {}, new Date(ev.t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })),
      el('span', {}, eventText(ev, byName))));
    if (state.events) lastEvents = state.events;

    side.replaceChildren(
      el('section', { class: 'card section' },
        el('h2', {}, `Players · ${state.players.length}`),
        state.match
          ? el('div', {},
            el('div', { class: 'taskbar' },
              el('i', { style: `width:${pct(state.match.tasksDone, state.match.tasksTotal)}%` })),
            el('div', { class: 'sub', style: 'font-size:12px;color:var(--ink-3);margin:6px 0 10px' },
              `Crew tasks ${pct(state.match.tasksDone, state.match.tasksTotal)}% complete`))
          : null,
        el('div', { class: 'plist' }, rows)),
      el('section', { class: 'card section' },
        el('h2', {}, 'What happened'),
        feed.length ? el('div', { class: 'feed' }, feed)
          : el('div', { class: 'empty' }, 'Nothing yet.')),
    );
  }, {
    gone: () => {
      view.replaceChildren(
        el('a', { class: 'back', href: '#/' }, '← All rooms'),
        el('div', { class: 'card section empty' },
          'That room is gone - everyone left, or it was made private.'),
      );
    },
  });

  return () => {
    running = false;
    source.close();
    document.removeEventListener('fullscreenchange', onFullChange);
    document.removeEventListener('keydown', onKey);
    // Navigating away while fullscreen would otherwise leave the browser
    // showing a detached element.
    if (isFull()) document.exitFullscreen().catch(() => {});
  };
}

const latestReason = (state) => OVER_REASONS[state.over?.reason] || '';

// ---------------------------------------------------------------- router

let teardown = null;

function route() {
  if (teardown) { teardown(); teardown = null; }
  const view = $('#view');
  const hash = location.hash.replace(/^#/, '');
  const room = hash.match(/^\/room\/(\d+)$/);
  setConn('connecting');
  // The strip is the front page's server-wide counters; a room view has its own
  // badges over the map and would otherwise leave stale numbers up there.
  $('#live-strip').replaceChildren();
  if (room) {
    document.title = `Room ${room[1]} · PSXSUS Spectator`;
    teardown = renderRoom(view, Number(room[1]));
  } else {
    document.title = 'PSXSUS Spectator';
    teardown = renderHome(view);
  }
}

window.addEventListener('hashchange', route);
route();
