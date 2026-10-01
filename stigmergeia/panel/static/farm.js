/* The formicarium. Folds /api/events into a colony and draws it as a glass ant farm.

   Layers, bottom to top:
     sand   (WebGL)  the material: strata, grain and veins, tunnels carved out of it with rim-lit
                     walls, and light scattered through the sand from inside (records, fresh work).
                     It samples three CPU-drawn textures: the strata column, the dig mask (where the
                     tunnels are, lined in their digger's colour) and the light map.
     farm   (2D)     what reads as marks: chambers, borrowed-code arcs, dead ends, the ruler, the sky
                     with the opening round, the ants and their labels.
   Without WebGL the sand layer falls back to plain 2D compositing of the same textures.

   Live and replay are one view: the scrubber's right end is "now"; a finished run simply stops
   growing. Every motion is a measurement arriving: the render loop redraws the sand only when
   time or the data moved. */
(function () {
'use strict';
const $ = id => document.getElementById(id);
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const C = {};
for (const k of ['void', 'glass', 'hair', 'ink', 'ink-dim', 'ink-ghost', 'sand-1', 'sand-2', 'sand-3', 'trail', 'record',
  'dead', 'claude', 'codex', 'fake', 'cat-1', 'cat-2', 'cat-3', 'cat-4', 'cat-5', 'cat-6', 'cat-7', 'measure']) C[k] = css('--' + k);
const rgb = h => { const n = parseInt(h.replace('#', ''), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; };
const rgba = (h, a) => { const [r, g, b] = rgb(h); return `rgba(${r},${g},${b},${a})`; };
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmt = s => { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s % 60).padStart(2, '0'); };
const hash = n => { const x = Math.sin(n * 9301.17 + 49297.3) * 233280; return x - Math.floor(x); };
const LAB = new Set(['score', 'submit', 'run', 'wait', 'confirm', 'jobs']);
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
const PROF = /[?&]prof\b/.test(location.search) ? (window.__prof = {}) : null;  // ?prof: per-layer frame timings in window.__prof

// ------------------------------------------------------------------ the fold
const S = {
  meta: null, seq: 0, agents: [], backend: {},
  posts: [], byId: new Map(), byAgent: {}, gates: [], board: [], round: [],
  acts: {}, jobs: {}, ends: {}, captions: [],
  live: {}, spent: null, boardError: null,
  serverNow: 0, polledPerf: 0, lastOkPerf: 0, everOk: false, pollFailed: false,
  version: 0,  // bumps whenever the fold changes, so cached layers know to redraw
};
const lineage = { memo: new Map() };

function fold(ev) {
  ev.t = +ev.t;
  switch (ev.k) {
    case 'post':
      if (S.byId.has(ev.id)) return;
      S.posts.push(ev); S.byId.set(ev.id, ev); S.board.push(ev);
      if (ev.type === 'WARN') S.captions.push({ t: ev.t, who: ev.who, what: 'dead end', text: warnLine(ev.text), col: C.dead });
      else S.captions.push({ t: ev.t, who: ev.who, what: 'on the board · ' + ev.type.toLowerCase(), text: postLine(ev.text), col: null });
      break;
    case 'gate':
      if (S.byId.has(ev.id)) return;
      ev.gate = true; S.gates.push(ev); S.byId.set(ev.id, ev); S.board.push(ev);
      S.captions.push({ t: ev.t, who: 'gate', what: ev.record ? 'record' : 'verdict', col: ev.record ? C.record : C['ink-dim'],
        text: `${ev.agent}'s ${ev.policy}: ${num(ev.score)}${ev.record ? ', a new record' : ev.confirmed ? ', confirmed' : ev.confirmed === false ? ', not confirmed' : ', one batch'}` });
      break;
    case 'round': S.round.push(ev); break;
    case 'act': (S.acts[ev.agent] ||= []).push(ev); break;
    case 'say':
      (S.acts[ev.agent] ||= []).push({ ...ev, tool: 'say' });
      S.captions.push({ t: ev.t, who: ev.agent, what: 'thinking aloud', text: ev.text.split('\n').find(l => l.trim()) || '', col: null });
      break;
    case 'job': (S.jobs[ev.agent] ||= []).push(ev); break;
    case 'end': S.ends[ev.agent] = ev; break;
    case 'error':
      S.captions.push({ t: ev.t, who: ev.agent, what: 'harness error', text: ev.text, col: C.dead });
      break;
  }
}
// the line of a post worth reading aloud: skip the "a00: answer to the opening round" header
const postLine = t => { const ls = (t || '').split('\n').map(l => l.trim()).filter(Boolean);
  return ((ls.length > 1 && /^a\d+:/.test(ls[0]) ? ls[1] : ls[0]) || '').slice(0, 260); };
const num = x => (x == null ? '—' : (+x).toFixed(2));
const warnLine = t => { const m = /Result:\s*(.*)/.exec(t || ''); return (m ? m[1] : (t || '').split('\n')[0]).slice(0, 220); };

function afterFold() {
  S.board.sort((a, b) => a.id - b.id);
  S.posts.sort((a, b) => a.t - b.t || a.id - b.id);
  S.byAgent = {}; for (const p of S.posts) (S.byAgent[p.who] ||= []).push(p);  // per-agent, in time order
  S.gates.sort((a, b) => a.t - b.t || a.id - b.id);
  S.captions.sort((a, b) => a.t - b.t);
  for (const a of Object.keys(S.acts)) S.acts[a].sort((p, q) => p.t - q.t);
  S.round.sort((a, b) => a.t - b.t);
  lineage.memo.clear();
  S.version++;
}

// a post's column: the agent whose line of work its derives-from chain roots in
function rootOf(p) {
  if (!p) return null;
  if (lineage.memo.has(p.id)) return lineage.memo.get(p.id);
  lineage.memo.set(p.id, p.who || p.agent);  // cycle guard
  const d = (p.refs || []).find(r => r[0] === 'derives-from');
  const parent = d ? S.byId.get(d[1]) : null;
  const r = parent ? rootOf(parent) : (p.who || p.agent);
  lineage.memo.set(p.id, S.agents.includes(r) ? r : (p.who || p.agent));
  return lineage.memo.get(p.id);
}
const parentOf = p => { const d = (p.refs || []).find(r => r[0] === 'derives-from'); return d ? S.byId.get(d[1]) : null; };
// every parent: a post deriving from two lines is a merge, and each line gets its own thread
const parentsOf = p => (p.refs || []).filter(r => r[0] === 'derives-from').map(r => S.byId.get(r[1])).filter(Boolean);

// ------------------------------------------------------------------ the clock
let T = 0, follow = true, playing = false, speed = 20, lastFrame = 0;
const T0 = () => S.meta?.started || Math.min(...S.posts.map(p => p.t), ...S.round.map(r => r.t), Date.now() / 1000);
const ended = () => !!S.meta?.ended;
function Tnow() {
  if (ended()) {
    const last = Math.max(S.meta.ended || 0, ...S.board.map(p => p.t));
    return last;
  }
  return S.serverNow + (performance.now() - S.polledPerf) / 1000;
}

// ------------------------------------------------------------------ geometry
const glassEl = $('glass'), farm = $('farm');
let sandCv = $('sand');
const fctx = farm.getContext('2d');
let W = 800, H = 520, DPR = 1, G = {};
function layout() {
  const r = glassEl.getBoundingClientRect();
  W = Math.max(320, Math.round(r.width)); H = Math.max(240, Math.round(r.height));
  DPR = Math.min(2, window.devicePixelRatio || 1);
  farm.width = Math.round(W * DPR); farm.height = Math.round(H * DPR);
  sandCv.width = W; sandCv.height = H;  // the sand is soft: 1x pixels; the marks above keep full density
  noiseDirty = true;
  for (const c of [dig, light]) { c.width = W; c.height = H; }
  ground.width = 4; ground.height = H;
  const SKY = Math.max(64, Math.min(110, H * 0.15));
  G = { SKY, LEFT: 58, RIGHT: W - 16, TOP: SKY + 14, BOT: H - 24 };
  G.FRONT = G.TOP + (G.BOT - G.TOP) * 0.86;
  dirtySand = true;
}
const colW = () => (G.RIGHT - G.LEFT) / Math.max(1, S.agents.length);
const colX = a => G.LEFT + (S.agents.indexOf(a) + 0.5) * colW();
const lane = a => ((S.agents.indexOf(a) % 5) - 2) * colW() * 0.09;
// Compaction: the digging front (now) sits at a fixed depth and the surface is always the start
// of the run. The last RECENT seconds keep full height; older time compacts on a log curve, so a
// run of any length fits the glass and the ruler says where it squeezed.
const RECENT = 180;
const phi = a => (a <= RECENT ? a : RECENT + RECENT * Math.log(1 + (a - RECENT) / RECENT));
function yOf(t) {
  const A = Math.max(1, T - T0()), a = Math.max(0, Math.min(A, T - t));
  return G.FRONT - (G.FRONT - G.TOP) * phi(a) / phi(A);
}
const ptOf = p => [colX(rootOf(p)) + lane(p.who) + (hash(p.id) - 0.5) * colW() * 0.14, yOf(p.t)];
const colorOf = a => C[S.backend[a]] || C.ink;

// ------------------------------------------------------------------ state at T
// posts up to T, in time order: a binary search on the sorted list, not a scan
const visiblePosts = () => { const out = S.posts.slice(0, lastAt(S.posts, T) + 1); return out.filter(p => p.who in S.byAgent && S.agents.includes(p.who)); };
function lastAt(arr, t) { let lo = 0, hi = arr.length; while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m].t <= t) lo = m + 1; else hi = m; } return lo - 1; }
function statusAt(a) {
  const end = S.ends[a];
  if (end && end.t <= T) return ['ended', end.why];
  if (follow && !ended() && S.live[a]) {
    const s = S.live[a].status;
    if (s === 'not started') return ['asleep', ''];
    if (s === 'idle') return ['waiting', 'waiting on the board'];
    if (s === 'sleeping') return ['sleeping', 'sleeping'];
    if (s === 'blocked') return ['lab', 'blocked in the lab'];
    if (s === 'waiting on the lab') return ['lab', 'waiting on its jobs'];
    if (s === 'quiet') return ['quiet', 'quiet'];
    if (s === 'waiting on tool' && /mcp__lab__/.test(S.live[a].detail || '')) return ['lab', 'in the lab'];
    if (S.live[a].jobs > 0) return ['working', `${S.live[a].jobs} in the lab`];
    return ['working', ''];
  }
  const acts = S.acts[a] || [], i = lastAt(acts, T);
  if (i < 0) return ['asleep', ''];
  const l = acts[i], next = acts[i + 1];
  if (T - l.t > 600) return ['quiet', 'quiet'];
  if (LAB.has(l.tool) && (next ? next.t > T : T - l.t < 300) && T - l.t > 1.5) return ['lab', 'in the lab'];
  return ['working', ''];
}
function bestAt() {
  const hib = S.meta?.higher_is_better !== false;
  let best = null;
  for (const g of S.gates) {
    if (g.t > T || g.score == null) continue;
    if (g.record) { best = g; continue; }
  }
  if (best) return best;
  for (const g of S.gates) {  // no record flags on this board: the best confirmed score
    if (g.t > T || g.score == null || g.confirmed === false) continue;
    if (!best || (hib ? g.score > best.score : g.score < best.score)) best = g;
  }
  return best;
}
function trailOf(a, vis) {
  const pts = [[colX(a) + lane(a), G.TOP - 2, null]], mine = S.byAgent[a] || [], n = lastAt(mine, T);
  for (let i = 0; i <= n; i++) { const p = mine[i], [x, y] = ptOf(p); pts.push([x, y, p]); }
  return pts;
}
function chamberAt(g, vis) {
  const mine = S.byAgent[g.agent] || [], anchor = mine[lastAt(mine, Math.min(g.t, T))] || null;
  const x = anchor ? colX(rootOf(anchor)) + lane(g.agent) : colX(g.agent) + lane(g.agent);
  return [x + (g.id % 2 ? 14 : -14), yOf(g.t) + 3];
}

// ------------------------------------------------------------------ CPU textures
const ground = document.createElement('canvas'), dig = document.createElement('canvas'), light = document.createElement('canvas');
const gctx = ground.getContext('2d'), dctx = dig.getContext('2d'), lctx = light.getContext('2d');
let dirtySand = true;

// strata column: r = band shade, g = below the surface, b = 255 undug (below now) / 128 a minute boundary.
// Alpha stays opaque: a canvas premultiplies alpha, so data packed there would zero the other channels.
function paintGround() {
  const img = gctx.createImageData(4, H), d = img.data, t0 = T0();
  const bandAt = y => { // which minute band a row belongs to: invert yOf by bisection
    let lo = t0, hi = T;
    for (let i = 0; i < 26; i++) { const m = (lo + hi) / 2; if (yOf(m) < y) lo = m; else hi = m; }
    return (lo - t0) / 60;
  };
  for (let y = 0; y < H; y++) {
    const below = y >= G.TOP - 6, undug = y > G.FRONT;
    let shade = 0, edge = 0;
    if (below && !undug && T > t0) {
      const b = bandAt(y); shade = Math.floor(b) % 2 ? 0 : 1;
      const frac = b - Math.floor(b); edge = frac < 0.02 || frac > 0.98 ? 1 : 0;
    }
    for (let x = 0; x < 4; x++) { const o = (y * 4 + x) * 4; d[o] = shade * 255; d[o + 1] = below ? 255 : 0; d[o + 2] = undug ? 255 : edge ? 128 : 0; d[o + 3] = 255; }
  }
  gctx.putImageData(img, 0, 0);
}

function brush(ctx, pts, seed, width, passes, jitter) {
  // a hand-cut tunnel: several offset strokes of varying width instead of one clean vector line
  for (let k = 0; k < passes; k++) {
    ctx.lineWidth = width * (0.75 + 0.5 * hash(seed + k * 13.1));
    ctx.beginPath();
    pts.forEach(([x, y, p], i) => {
      const j = p ? p.id : i;
      const ox = (hash(j * 3.7 + k + seed) - 0.5) * jitter, oy = (hash(j * 5.3 + k * 2 + seed) - 0.5) * jitter * 0.6;
      if (i === 0) { ctx.moveTo(x + ox, y + oy); return; }
      const [x0, y0] = pts[i - 1], my = (y0 + y + oy) / 2;
      ctx.bezierCurveTo(x0 + ox * 0.5, my, x + ox, my, x + ox, y + oy);
    });
    ctx.stroke();
  }
}

function paintDig(vis, trails) {
  dctx.clearRect(0, 0, W, H);
  dctx.lineCap = 'round'; dctx.lineJoin = 'round';
  const w = Math.max(5, Math.min(11, colW() * 0.16));
  for (const [a, pts] of trails) {
    if (pts.length < 2) continue;
    dctx.strokeStyle = colorOf(a);
    brush(dctx, pts, S.agents.indexOf(a) * 31, w, 4, w * 0.45);
  }
  // chambers: cavities off the tunnel, lined gold for a record
  for (const g of S.gates) {
    if (g.t > T) continue;
    const [cx, cy] = chamberAt(g, vis);
    dctx.fillStyle = g.record ? C.record : colorOf(g.agent);
    dctx.beginPath(); dctx.ellipse(cx, cy, 12, 8, 0, 0, 7); dctx.fill();
  }
  // dead ends: the tunnel segment leading to a WARN is backfilled, the sand returns
  dctx.globalCompositeOperation = 'destination-out';
  for (const p of vis) {
    if (p.type !== 'WARN') continue;
    const pts = trails.get(p.who); if (!pts) continue;
    const i = pts.findIndex(q => q[2] && q[2].id === p.id); if (i < 1) continue;
    dctx.globalAlpha = 0.72; dctx.lineWidth = w * 1.4;
    brush(dctx, [pts[i - 1], pts[i]], p.id, w * 1.3, 2, w * 0.3);
  }
  dctx.globalAlpha = 1; dctx.globalCompositeOperation = 'source-over';
}

function paintLight(vis, trails) {
  lctx.clearRect(0, 0, W, H);
  lctx.globalCompositeOperation = 'lighter';
  const glow = (x, y, r, col, a) => {
    const g = lctx.createRadialGradient(x, y, 0, x, y, r);
    g.addColorStop(0, rgba(col, a)); g.addColorStop(1, rgba(col, 0));
    lctx.fillStyle = g; lctx.fillRect(x - r, y - r, 2 * r, 2 * r);
  };
  for (const g of S.gates) {  // records light the sand around them, for good
    if (g.t > T || !g.record) continue;
    const [x, y] = chamberAt(g, vis); glow(x, y, 70, C.record, 0.32);
  }
  for (let i = vis.length - 1; i >= 0 && T - vis[i].t <= 45; i--) {  // fresh work glows in its author's colour, then cools
    const p = vis[i], age = T - p.t;
    const [x, y] = ptOf(p); glow(x, y, 46, colorOf(p.who), 0.38 * Math.exp(-age / 12));
    const par = parentOf(p);  // and borrowed code flares amber where it lands
    if (par) glow(x, y, 60, C.trail, 0.5 * Math.exp(-age / 15));
  }
  for (const [a, pts] of trails) {  // an agent in the lab carries a lit grain
    if (statusAt(a)[0] !== 'lab') continue;
    const lp = pts[pts.length - 1]; glow(lp[0], Math.max(lp[1], G.FRONT - 2), 26, C.ink, 0.22);
  }
  lctx.globalCompositeOperation = 'source-over';
}

// veins: value-noise fbm baked into a small tiling texture once, sampled by the shader
let noiseDirty = true;
const noiseCv = document.createElement('canvas');
function bakeNoise() {
  const w = 256, h = 256; noiseCv.width = w; noiseCv.height = h;
  const ctx = noiseCv.getContext('2d'), img = ctx.createImageData(w, h), d = img.data;
  const lat = (x, y, s) => hash(((x % s + s) % s) * 7.31 + ((y % s + s) % s) * 131.7 + s);
  const vn = (x, y, s) => { const xi = Math.floor(x), yi = Math.floor(y), fx = x - xi, fy = y - yi, ux = fx * fx * (3 - 2 * fx), uy = fy * fy * (3 - 2 * fy);
    const a = lat(xi, yi, s), b = lat(xi + 1, yi, s), c = lat(xi, yi + 1, s), e = lat(xi + 1, yi + 1, s);
    return a + (b - a) * ux + (c - a) * uy + (a - b - c + e) * ux * uy; };
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    let v = 0, amp = 0.5, sx = 3, sy = 12;  // stretched across: veins lie along the strata
    for (let o = 0; o < 5; o++) { v += amp * vn(x / w * sx, y / h * sy, o === 0 ? 3 : sx); sx *= 2; sy *= 2; amp *= 0.5; }
    const o4 = (y * w + x) * 4; d[o4] = d[o4 + 1] = d[o4 + 2] = Math.round(v * 255); d[o4 + 3] = 255;
  }
  ctx.putImageData(img, 0, 0);
}

// ------------------------------------------------------------------ the sand (WebGL)
const VS = `attribute vec2 p; void main(){ gl_Position = vec4(p, 0., 1.); }`;
const FS = `
precision highp float;
uniform sampler2D uGround, uDig, uLight, uNoise;
uniform vec2 uRes; uniform float uTime, uDpr;
uniform vec3 uS1, uS2, uS3, uGlass, uVoid, uGold;
uniform vec3 uBest; // x, y (css px), strength
float h(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float n(vec2 p){ vec2 i = floor(p), f = fract(p); f = f*f*(3.-2.*f);
  return mix(mix(h(i), h(i+vec2(1,0)), f.x), mix(h(i+vec2(0,1)), h(i+vec2(1,1)), f.x), f.y); }
void main(){
  vec2 px = vec2(gl_FragCoord.x, uRes.y - gl_FragCoord.y) / uDpr;   // css px, y down
  vec2 css = uRes / uDpr;
  vec2 uv = px / css;
  vec4 st = texture2D(uGround, vec2(.5, uv.y));
  float undug = step(.75, st.b), seam = step(.25, st.b) * (1. - undug);
  vec3 col;
  if (st.g < .5) {
    // above the surface: the glass, faintly lit from the sand below
    col = uGlass + vec3(.03,.022,.015) * smoothstep(.0, 1., uv.y * 6.);
  } else {
    float grain = h(floor(px));
    // veins: baked once into uNoise (fbm is too dear per pixel per frame); bands shift the lookup
    float vein = texture2D(uNoise, fract(uv * vec2(1., 1.) + vec2(0., st.r * .37))).r;
    vec3 sand = mix(uS2, uS1, st.r);
    sand = mix(sand, uS3, undug);
    sand *= .78 + .32 * vein + .22 * (grain - .5);
    sand += vec3(.035,.025,.015) * seam;       // the line between two minutes
    vec2 e = vec2(1.5) / css;
    vec4 d = texture2D(uDig, uv);
    float dx = texture2D(uDig, uv + vec2(e.x, 0.)).a - texture2D(uDig, uv - vec2(e.x, 0.)).a;
    float dy = texture2D(uDig, uv + vec2(0., e.y)).a - texture2D(uDig, uv - vec2(0., e.y)).a;
    float rim = clamp(length(vec2(dx, dy)) * 1.6, 0., 1.);
    float lit = clamp(.55 + .6 * (-dy * 2.), .2, 1.2);     // walls catch light from above
    vec3 lining = d.a > .01 ? d.rgb : vec3(0.);
    vec3 tunnel = uVoid + lining * (.10 + .05 * h(floor(px * .3)));
    col = mix(sand, tunnel, smoothstep(.15, .85, d.a));
    col += lining * rim * .55 * lit;
    // light scattered through the sand from inside: soft, and it catches on single grains
    vec4 Lt = texture2D(uLight, uv);
    vec3 L = Lt.rgb * Lt.a;   // the upload un-premultiplies; weight the colour by its coverage again
    float b = length(px - uBest.xy);
    L += uGold * uBest.z * exp(-b * b / 2600.);
    col += L * (.8 - .45 * d.a) + L * step(.982, grain) * 1.2;
  }
  // the glass: a vignette and a faint cold reflection
  float v = smoothstep(1.25, .35, length((uv - .5) * vec2(1.2, 1.)));
  col *= .72 + .28 * v;
  gl_FragColor = vec4(col, 1.);
}`;
let gl = null, prog = null, tex = {};
function initGL() {
  if (/[?&]flat\b/.test(location.search)) return false;  // ?flat: the 2D sand, for slow or software GL
  try {
    gl = sandCv.getContext('webgl', { antialias: false, premultipliedAlpha: false });
    if (!gl) return false;
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    const renderer = dbg ? String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)) : '';
    if (/swiftshader|llvmpipe|software/i.test(renderer) && !/[?&]gl\b/.test(location.search)) {
      console.info('formicarium: software GL (' + renderer + '): flat sand; ?gl forces the shader');
      gl = null; sandCv.replaceWith(sandCv = sandCv.cloneNode()); return false;
    }
    const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s)); return s; };
    prog = gl.createProgram();
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, VS)); gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    gl.useProgram(prog);
    const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(prog, 'p'); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    ['uGround', 'uDig', 'uLight', 'uNoise'].forEach((name, i) => {
      const t = gl.createTexture(); gl.activeTexture(gl.TEXTURE0 + i); gl.bindTexture(gl.TEXTURE_2D, t);
      for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR],
        [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, k, v);
      gl.uniform1i(gl.getUniformLocation(prog, name), i); tex[name] = t;
    });
    const v3 = (u, h) => gl.uniform3fv(gl.getUniformLocation(prog, u), rgb(h).map(x => x / 255));
    v3('uS1', C['sand-1']); v3('uS2', C['sand-2']); v3('uS3', C['sand-3']); v3('uGlass', C.glass); v3('uVoid', C.void); v3('uGold', C.record);
    return true;
  } catch (e) { console.warn('formicarium: no WebGL sand, drawing it flat', e); gl = null; return false; }
}
function drawSand(best, now) {
  if (gl) {
    gl.viewport(0, 0, sandCv.width, sandCv.height);
    if (noiseDirty) { bakeNoise(); gl.activeTexture(gl.TEXTURE3); gl.bindTexture(gl.TEXTURE_2D, tex.uNoise);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, noiseCv);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.REPEAT); noiseDirty = false; }
    [['uGround', ground], ['uDig', dig], ['uLight', light]].forEach(([name, cv], i) => {
      gl.activeTexture(gl.TEXTURE0 + i); gl.bindTexture(gl.TEXTURE_2D, tex[name]);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, cv);
    });
    gl.uniform2f(gl.getUniformLocation(prog, 'uRes'), sandCv.width, sandCv.height);
    gl.uniform1f(gl.getUniformLocation(prog, 'uDpr'), sandCv.width / W);
    gl.uniform1f(gl.getUniformLocation(prog, 'uTime'), now / 1000);
    gl.uniform3f(gl.getUniformLocation(prog, 'uBest'), best ? best[0] : -999, best ? best[1] : -999, best ? best[2] : 0);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    return;
  }
  // flat fallback: the same textures, composited plainly
  const c = sandCv.getContext('2d'); c.setTransform(sandCv.width / W, 0, 0, sandCv.height / H, 0, 0);
  c.fillStyle = C.glass; c.fillRect(0, 0, W, H);
  c.imageSmoothingEnabled = false;
  const img = gctx.getImageData(0, 0, 1, H).data;
  for (let y = 0; y < H; y++) {
    const o = y * 16; if (!img[o + 1]) continue;
    c.fillStyle = img[o + 2] > 200 ? C['sand-3'] : img[o] ? C['sand-1'] : C['sand-2']; c.fillRect(0, y, W, 1);
  }
  c.globalAlpha = 0.85; c.drawImage(dig, 0, 0, W, H); c.globalAlpha = 1;
  c.globalCompositeOperation = 'lighter'; c.drawImage(light, 0, 0, W, H); c.globalCompositeOperation = 'source-over';
}

// ------------------------------------------------------------------ spoil: grains kicked up as work lands
// When time moves forward across a post, the ant that wrote it throws up a little sand; a record throws
// gold. Only forward motion through an event spawns grains, so a seek or a scrub never sprays.
let grains = [], spoilT = null;
function spoil(vis, now) {
  if (reduced) return;
  if (spoilT != null && T > spoilT && T - spoilT < 30) {
    for (let i = vis.length - 1; i >= 0 && vis[i].t > spoilT; i--) { const p = vis[i];
      const [x, y] = ptOf(p);
      for (let i = 0; i < 16; i++) grains.push({ x, y, vx: (Math.random() - 0.5) * 70, vy: -30 - Math.random() * 70, born: now, life: 900 + Math.random() * 700,
        col: p.type === 'WARN' ? C.dead : Math.random() < 0.3 ? colorOf(p.who) : C.ink, r: 0.8 + Math.random() * 1.1 });
    }
    for (const g of S.gates) if (g.record && g.t > spoilT && g.t <= T) {
      const [x, y] = chamberAt(g, vis);
      for (let i = 0; i < 40; i++) { const a = Math.random() * 7, v = 20 + Math.random() * 90;
        grains.push({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v - 40, born: now, life: 1400 + Math.random() * 1200, col: C.record, r: 0.8 + Math.random() * 1.4, spark: true }); }
    }
  }
  spoilT = T;
}
function drawGrains(now, dt) {
  if (!grains.length) return;
  const c = fctx;
  grains = grains.filter(g => now - g.born < g.life);
  for (const g of grains) {
    g.vy += (g.spark ? 40 : 260) * dt; g.vx *= 1 - 1.5 * dt; g.x += g.vx * dt; g.y += g.vy * dt;
    const k = 1 - (now - g.born) / g.life;
    c.globalAlpha = Math.max(0, k) * (g.spark ? 1 : 0.8); c.fillStyle = g.col;
    c.beginPath(); c.arc(g.x, g.y, g.r, 0, 7); c.fill();
  }
  c.globalAlpha = 1;
}

// ------------------------------------------------------------------ the marks (2D overlay)
let hits = [];
function drawAnt(x, y, ang, col, moving, now, carry) {
  const c = fctx; c.save(); c.translate(x, y); c.rotate(ang);
  c.strokeStyle = col; c.lineWidth = 1;
  const w = moving ? Math.sin(now / 60) * 2.2 : 0;
  for (const s of [-1, 1]) for (const k of [-4, 0, 4]) { c.beginPath(); c.moveTo(k, 0); c.lineTo(k + (k === 0 ? w : -w) * s * 0.8, 6 * s); c.stroke(); }
  c.fillStyle = col; c.beginPath(); c.ellipse(-7, 0, 4.6, 3.2, 0, 0, 7); c.ellipse(0, 0, 2.6, 2, 0, 0, 7); c.ellipse(6, 0, 3, 2.6, 0, 0, 7); c.fill();
  c.beginPath(); c.moveTo(8, -1); c.lineTo(13, -4); c.moveTo(8, 1); c.lineTo(13, 4); c.stroke();
  if (carry) { c.fillStyle = C.ink; c.beginPath(); c.arc(11, 0, 2.2, 0, 7); c.fill(); }
  c.restore();
}

function drawRuler() {
  const c = fctx, t0 = T0(), mins = Math.ceil((T - t0) / 60) + 1;
  c.font = '600 10px "Barlow Condensed", sans-serif'; c.textAlign = 'right'; c.textBaseline = 'middle';
  let lastY = -1e9, skipped = 0;
  const step = mins > 240 ? 60 : mins > 90 ? 10 : mins > 30 ? 5 : 1;
  for (let m = 0; m < mins; m += 1) {
    const t = t0 + m * 60; if (t > T) break; const y = yOf(t);
    c.fillStyle = C['ink-ghost']; c.fillRect(G.LEFT - 34, y, m % step ? 3 : 6, 1);
    if (m % step === 0 && y - lastY >= 13) { c.fillText(m >= 60 ? fmt(m * 60).replace(/:00$/, 'h') : m + "'", G.LEFT - 38, y); lastY = y; } else if (m % step === 0) skipped++;
  }
  if (skipped) { c.save(); c.translate(G.LEFT - 48, G.TOP + (yOf(T - RECENT) - G.TOP) / 2); c.rotate(-Math.PI / 2); c.textAlign = 'center';
    c.fillStyle = C['ink-ghost']; c.fillText('compacted', 0, 0); c.restore(); }
  c.textAlign = 'left'; c.fillStyle = C['ink-dim']; c.fillText(follow && !ended() ? 'now' : fmt(T - t0), G.LEFT - 30, G.FRONT - 9);
  c.strokeStyle = 'rgba(239,230,216,.12)'; c.setLineDash([3, 6]); c.beginPath(); c.moveTo(G.LEFT - 30, G.FRONT); c.lineTo(G.RIGHT, G.FRONT); c.stroke(); c.setLineDash([]);
  if (colW() >= 26) {
    c.textAlign = 'center'; c.font = '600 11px "Barlow Condensed", sans-serif';
    S.agents.forEach(a => { c.fillStyle = C['ink-ghost']; c.fillText(a.toUpperCase(), colX(a), H - 10); });
  }
}

function drawSky(now) {
  const c = fctx, rs = S.round; if (!rs.length) return;
  const rev = rs.find(e => e.event === 'revealed');
  const jarX = W / 2, jarY = G.SKY * 0.45, jw = Math.min(70, 20 + S.agents.length * 3), jh = G.SKY * 0.55;
  c.strokeStyle = C['ink-ghost']; c.lineWidth = 1.2; c.beginPath();
  c.moveTo(jarX - jw / 2 - 4, jarY - jh / 2); c.lineTo(jarX - jw / 2, jarY + jh / 2); c.lineTo(jarX + jw / 2, jarY + jh / 2); c.lineTo(jarX + jw / 2 + 4, jarY - jh / 2); c.stroke();
  const open = rev && rev.t <= T;
  if (!open) { c.beginPath(); c.moveTo(jarX - jw / 2 - 7, jarY - jh / 2); c.lineTo(jarX + jw / 2 + 7, jarY - jh / 2); c.stroke(); }
  c.font = '600 10px "Barlow Condensed", sans-serif'; c.fillStyle = C['ink-ghost']; c.textAlign = 'center'; c.textBaseline = 'alphabetic';
  c.fillText(open ? 'OPENED' : 'SEALED', jarX, jarY + jh / 2 + 12);
  const answered = rs.filter(e => e.event === 'answered').sort((p, q) => p.t - q.t).map(e => e.agent);
  const per = Math.max(1, Math.floor((jw - 8) / 9));
  S.agents.forEach((a, i) => {
    const s = rs.find(e => e.event === 'sealed' && e.agent === a), an = rs.find(e => e.event === 'answered' && e.agent === a);
    if (!s || s.t > T) return;
    let x, y, wing = true;
    const inJar = [jarX - jw / 2 + 6 + (i % per) * 9, jarY + jh / 2 - 6 - Math.floor(i / per) * 8];
    const out = [colX(a), G.SKY * (0.25 + 0.3 * (i % 2))];
    if (!open) { [x, y] = inJar; wing = false; }
    else if (!an || an.t > T) { const k = Math.min(1, (T - rev.t) / 4); x = inJar[0] + (out[0] - inJar[0]) * k; y = inJar[1] + (out[1] - inJar[1]) * k; }
    else { const k = Math.min(1, (T - an.t) / 2); x = out[0]; y = out[1] + (G.TOP - 4 - out[1]) * k; wing = k < 1; }
    const fl = wing && !reduced ? Math.sin(now / 70 + i) * 4 : 0;
    c.fillStyle = colorOf(a);
    if (wing) { c.globalAlpha = 0.45; c.beginPath(); c.ellipse(x - 4, y - 2, 6, 2.5 + fl * 0.3, -0.5, 0, 7); c.ellipse(x + 4, y - 2, 6, 2.5 - fl * 0.3, 0.5, 0, 7); c.fill(); c.globalAlpha = 1; }
    c.beginPath(); c.arc(x, y, S.agents.length > 30 ? 2 : 3.2, 0, 7); c.fill();
    if (an && an.t <= T && colW() >= 18) {
      c.fillStyle = C['ink-dim']; c.font = '500 10px "JetBrains Mono", monospace';
      c.fillText('#' + (answered.indexOf(a) + 1), x, Math.min(y, G.TOP - 10) - 8);
    }
  });
}

function drawMarks(vis, trails, now) {
  const c = fctx; c.setTransform(DPR, 0, 0, DPR, 0, 0); c.clearRect(0, 0, W, H);
  hits = [];
  drawRuler(); drawSky(now);
  // borrowed code: amber arcs breaking into another column, bright when fresh
  for (const p of vis) for (const par of parentsOf(p)) {
    if (par.t > T) continue;
    const [x0, y0] = par.gate ? chamberAt(par, vis) : ptOf(par), [x1, y1] = ptOf(p), age = T - p.t;
    c.strokeStyle = C.trail; c.globalAlpha = Math.max(0.4, Math.exp(-age / 40)); c.lineWidth = 1.4 + 2 * Math.exp(-age / 10);
    c.beginPath(); c.moveTo(x0, y0); c.bezierCurveTo(x0, y0 + (y1 - y0) * 0.6, x1, y1 - (y1 - y0) * 0.6, x1, y1); c.stroke();
  }
  c.globalAlpha = 1;
  // dead ends: hatched backfill, the last few with their revive-if pinned to the glass
  const warns = vis.filter(p => p.type === 'WARN');
  for (const w of warns) {
    const pts = trails.get(w.who); if (!pts) continue; const i = pts.findIndex(q => q[2] && q[2].id === w.id); if (i < 1) continue;
    const [x0, y0] = pts[i - 1], [x1, y1] = pts[i];
    c.save(); c.beginPath(); c.moveTo(x0, y0); c.bezierCurveTo(x0, (y0 + y1) / 2, x1, (y0 + y1) / 2, x1, y1);
    c.setLineDash([2, 3]); c.strokeStyle = C.dead; c.lineWidth = 4; c.stroke(); c.restore();
  }
  c.font = 'italic 12px Newsreader, serif'; c.textAlign = 'left'; c.textBaseline = 'middle';
  for (const w of warns.slice(-3)) {
    const [x, y] = ptOf(w), m = /Revive if:\s*(.*)/i.exec(w.text || '');
    const tag = m ? ('revive if ' + m[1]).slice(0, 52) : 'dead end';
    c.fillStyle = C.dead; c.fillRect(x + 10, y - 1, 3, 3); c.fillStyle = C['ink-dim']; c.fillText(tag, x + 16, y + 1);
  }
  // posts: a small mark where each sits, so the glass can be read point by point
  for (const p of vis) {
    const [x, y] = ptOf(p);
    c.fillStyle = p.type === 'WARN' ? C.dead : rgba(colorOf(p.who), 0.9);
    c.beginPath(); c.arc(x, y, p.type === 'PROPOSAL' ? 2.6 : 2, 0, 7); c.fill();
    hits.push({ x, y, r: 7, item: p });
  }
  // chambers, filled to their score
  const sc = S.gates.filter(g => g.score != null).map(g => +g.score);
  const sMin = Math.min(...sc), sMax = Math.max(...sc), span = Math.max(1e-6, sMax - sMin);
  const hib = S.meta?.higher_is_better !== false, best = bestAt();
  for (const g of S.gates) {
    if (g.t > T || g.score == null) continue;
    const [cx, cy] = chamberAt(g, vis), rx = 10, ry = 6.5;
    let k = (g.score - sMin) / span; if (!hib) k = 1 - k; k = 0.08 + 0.92 * Math.max(0, Math.min(1, k));
    const col = g.record ? C.record : colorOf(g.agent);
    hits.push({ x: cx, y: cy, r: 12, item: g });
    if (g.confirmed !== true && !g.record) {  // never confirmed: hollow
      c.strokeStyle = col; c.globalAlpha = 0.55; c.lineWidth = 1; c.beginPath(); c.ellipse(cx, cy, rx, ry, 0, 0, 7); c.stroke(); c.globalAlpha = 1; continue;
    }
    c.save(); c.beginPath(); c.ellipse(cx, cy, rx, ry, 0, 0, 7); c.clip();
    c.fillStyle = col; c.globalAlpha = g.record ? 0.95 : 0.6; c.fillRect(cx - rx, cy + ry - 2 * ry * k, 2 * rx, 2 * ry * k); c.restore(); c.globalAlpha = 1;
    if (g.record) {
      c.strokeStyle = C.record; c.lineWidth = 1.4; c.globalAlpha = g === best ? 0.9 : 0.5; c.beginPath(); c.ellipse(cx, cy, rx + 2, ry + 2, 0, 0, 7); c.stroke(); c.globalAlpha = 1;
      if (g === best || colW() > 60) { c.fillStyle = C.record; c.font = '800 15px "Noto Serif Display", serif'; c.textAlign = 'left'; c.fillText(num(g.score), cx + rx + 5, cy + 1); }
    }
  }
  // focus ring for a post picked on the teletype
  if (focusId != null) {
    const p = S.byId.get(focusId);
    if (p && p.t <= T) { const [x, y] = p.gate ? chamberAt(p, vis) : ptOf(p);
      c.strokeStyle = C.ink; c.lineWidth = 1; c.beginPath(); c.arc(x, y, 14 + 2 * Math.sin(now / 200), 0, 7); c.stroke(); }
  }
  // the ants: where each agent is now; labels laid out after, so two ants in one lineage never overprint
  const showLabels = colW() >= 40, labels = [];
  // a crowd at the front: ants digging in one lineage fan out across its column, so a herd reads
  // as a crowd of distinct ants rather than one smudge
  const crowd = new Map();
  for (const [a, pts] of trails) {
    const st = statusAt(a)[0]; if (st === 'asleep' || st === 'quiet' || st === 'ended') continue;
    const lp = pts[pts.length - 1], key = lp[2] ? rootOf(lp[2]) : a;
    (crowd.get(key) || crowd.set(key, []).get(key)).push(a);
  }
  const fan = new Map();
  for (const [key, ants] of crowd) {
    if (ants.length < 2) continue;
    const spread = Math.min(colW() * 0.8, ants.length * 16);
    ants.sort((p, q) => S.agents.indexOf(p) - S.agents.indexOf(q))
      .forEach((a, i) => fan.set(a, colX(key) + (i - (ants.length - 1) / 2) * spread / Math.max(1, ants.length - 1)));
  }
  for (const [a, pts] of trails) {
    const [st, why] = statusAt(a); if (st === 'asleep') continue;
    let lp = pts[pts.length - 1]; const pv = pts[pts.length - 2] || [lp[0], lp[1] - 10];
    const still = st === 'quiet' || st === 'ended';
    if (fan.has(a)) lp = [fan.get(a), lp[1], lp[2]];
    const y = still ? lp[1] : Math.max(lp[1], G.FRONT - 2);
    const wander = still || reduced ? 0 : Math.sin(now / 1300 + S.agents.indexOf(a) * 1.7) * colW() * 0.08;
    const ang = still ? Math.atan2(lp[1] - pv[1], lp[0] - pv[0]) : Math.PI / 2 + Math.sin(now / 900 + S.agents.indexOf(a)) * 0.3;
    const col = still ? C['ink-ghost'] : colorOf(a);
    drawAnt(lp[0] + wander, y, ang, col, st === 'working' && !reduced, now, st === 'lab');
    hits.push({ x: lp[0] + wander, y, r: 12, item: { ant: a, st, why } });
    if (showLabels) labels.push({ x: lp[0] + 10, y: y - 10, txt: a + (why && colW() >= 150 ? ' · ' + why : ''), col: still ? C['ink-ghost'] : C.ink });
  }
  c.font = '600 11px "Barlow Condensed", sans-serif'; c.textAlign = 'left'; c.textBaseline = 'alphabetic';
  const placed = [];
  for (const l of labels.sort((p, q) => p.x - q.x)) {
    const w = c.measureText(l.txt).width;
    let y = l.y;  // step up until the label clears every label already placed
    for (let k = 0; k < 12 && placed.some(q => l.x < q.x + q.w + 4 && q.x < l.x + w + 4 && Math.abs(q.y - y) < 12); k++) y = l.y - 12 * Math.ceil((k + 1) / 2) * (k % 2 ? -1 : 1) + (k % 2 ? 0 : 0);
    placed.push({ x: l.x, y, w });
    c.fillStyle = l.col; c.fillText(l.txt, l.x, y);
  }
}

// ------------------------------------------------------------------ teletype, captions, hud
const tty = $('tty'); let ttyCount = -1, focusId = null;
function ttyLine(p, fresh) {
  const f = fresh ? ' fresh' : '', hl = p.id === focusId ? ' hl' : '';
  if (p.gate) {
    const kind = p.record ? '<span class="stamp">record</span>' : p.confirmed ? '<span class="stamp faint">confirmed</span>' : p.confirmed === false ? '<span class="stamp faint">not confirmed</span>' : '<span class="stamp faint">one batch</span>';
    return `<div class="line stampline${f}${hl}" data-id="${p.id}"><span>#${p.id} GATE ${esc(p.agent)} ${esc(p.policy)} <b>${num(p.score)}</b></span>${kind}</div>`;
  }
  const byKind = new Map(); for (const [k, id] of p.refs || []) (byKind.get(k) || byKind.set(k, []).get(k)).push('#' + id);
  const edges = [...byKind].map(([k, ids]) => `<div class="edge">&nbsp;&nbsp;↳ ${esc(k)} ${ids.join(' ')}</div>`).join('');
  return `<div class="line${f}${hl}" data-id="${p.id}"><span class="hd${p.type === 'WARN' ? ' warn' : ''}">#${p.id} ${esc(p.who)} ${esc(p.type)}</span> ${esc((p.text || '').slice(0, 260))}${edges}</div>`;
}
function drawTty(force) {
  const vis = S.board.filter(p => p.t <= T);
  if (!force && vis.length === ttyCount) return;
  const atBottom = tty.scrollHeight - tty.scrollTop - tty.clientHeight < 40;
  if (!force && vis.length > ttyCount && ttyCount >= 0 && vis.slice(0, ttyCount).every((p, i) => tty.children[i]?.dataset.id == p.id)) {
    tty.insertAdjacentHTML('beforeend', vis.slice(ttyCount).map(p => ttyLine(p, true)).join(''));
  } else {
    tty.innerHTML = vis.map(p => ttyLine(p, false)).join('') || '<div class="eof">the paper is blank: nothing on the board yet</div>';
  }
  ttyCount = vis.length;
  if (atBottom || force) tty.scrollTop = tty.scrollHeight;
}
tty.addEventListener('click', e => {
  const l = e.target.closest('.line'); if (!l) return;
  focusId = +l.dataset.id === focusId ? null : +l.dataset.id;
  tty.querySelectorAll('.hl').forEach(x => x.classList.remove('hl'));
  if (focusId != null) l.classList.add('hl');
});

const capEl = $('caption');
function caption() {
  let l = null; for (let i = S.captions.length - 1; i >= 0; i--) if (S.captions[i].t <= T) { l = S.captions[i]; break; }
  if (!l) { capEl.innerHTML = '<em>the glass is still empty</em>'; return; }
  const col = l.col || colorOf(l.who);
  capEl.innerHTML = `<span class="who" style="color:${col}">${esc(l.who)} · ${esc(l.what)}</span>${esc(l.text.slice(0, 280))}`;
}

function hud() {
  const b = bestAt(), t0 = T0();
  $('best').textContent = b ? num(b.score) : '—';
  $('clock').textContent = fmt(T - t0);
  $('clockcap').textContent = ended() ? (follow ? 'run time · ended' : 'replay') : follow ? 'run time · live' : 'replay';
  const vis = visiblePosts();
  const alive = new Set(vis.filter(p => p.t > T - 600 && p.type !== 'PROPOSAL').map(rootOf));
  $('width').textContent = alive.size ? `${alive.size}/${S.agents.length}` : '—';
  $('borrow').textContent = vis.filter(p => parentOf(p)).length;
  $('dead').textContent = vis.filter(p => p.type === 'WARN').length;
  $('spent').textContent = S.spent == null ? '—' : '$' + S.spent.toFixed(2);
  const fr = $('fresh').parentElement, age = (performance.now() - S.lastOkPerf) / 1000;
  fr.classList.toggle('stale', !ended() && age >= 12 && age < 30); fr.classList.toggle('dead', !ended() && (age >= 30 || S.pollFailed));
  $('fresh').textContent = !S.everOk ? '—' : ended() ? 'ended' : 'T+' + Math.floor(age);
  fr.querySelector('.cap').textContent = S.boardError ? 'the board did not answer' : S.pollFailed ? 'the panel did not answer' : 'since the panel answered';
  const ab = $('absent');
  ab.hidden = S.everOk && S.agents.length > 0;
  if (!ab.hidden) ab.textContent = S.pollFailed ? 'the panel did not answer; still trying' : 'waiting for the first word from the run';
}

// ------------------------------------------------------------------ instruments (drawers)
function svgEl(svg) { const r = svg.getBoundingClientRect(); return [Math.max(300, r.width), r.height || 220]; }
function drawStair() {
  const svg = $('stair'); if (!$('drawer-stair').open) return;
  const [w, h] = svgEl(svg); svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  const gs = S.gates.filter(g => g.score != null), t0 = T0(), t1 = Math.max(Tnow(), t0 + 60);
  if (!gs.length) { svg.innerHTML = `<text x="${w / 2}" y="${h / 2}" text-anchor="middle">no held-out score yet</text>`; return; }
  const sc = gs.map(g => +g.score), lo = Math.min(...sc), hi = Math.max(...sc), pad = (hi - lo) * 0.08 || 1;
  const X = t => 40 + (w - 56) * (t - t0) / (t1 - t0), Y = s => h - 22 - (h - 36) * (s - lo + pad) / (hi - lo + 2 * pad);
  let out = '';
  for (let k = 0; k <= 4; k++) { const s = lo - pad + (hi - lo + 2 * pad) * k / 4; out += `<line x1="40" x2="${w - 16}" y1="${Y(s)}" y2="${Y(s)}" stroke="${C.hair}"/><text x="34" y="${Y(s) + 3}" text-anchor="end">${s.toFixed(1)}</text>`; }
  const recs = gs.filter(g => g.record);
  if (recs.length) { let d = `M${X(recs[0].t)},${Y(recs[0].score)}`; for (const r of recs.slice(1)) d += ` H${X(r.t)} V${Y(r.score)}`; d += ` H${X(t1)}`;
    out += `<path d="${d}" fill="none" stroke="${C.record}" stroke-width="1.6"/>`; }
  for (const g of gs) { const col = colorOf(g.agent), solid = g.confirmed === true || g.record;
    out += `<circle cx="${X(g.t)}" cy="${Y(g.score)}" r="${g.record ? 4 : 2.8}" fill="${solid ? col : 'none'}" stroke="${col}" opacity="${g.t <= T ? 0.9 : 0.2}"><title>#${g.id} ${esc(g.agent)} ${esc(g.policy)} ${num(g.score)}</title></circle>`; }
  out += `<line x1="${X(T)}" x2="${X(T)}" y1="8" y2="${h - 18}" stroke="${C['ink-dim']}" stroke-dasharray="2 4"/>`;
  out += `<text x="40" y="${h - 4}">${fmt(0)}</text><text x="${w - 16}" y="${h - 4}" text-anchor="end">${fmt(t1 - t0)}</text>`;
  svg.innerHTML = out;
}
function drawRoundline() {
  const svg = $('roundline'); if (!$('drawer-round').open) return;
  const [w] = svgEl(svg), rs = S.round, n = S.agents.length, rowH = Math.max(6, Math.min(16, 180 / Math.max(1, n))), h = 30 + n * rowH;
  svg.style.height = h + 'px'; svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  if (!rs.length) { svg.innerHTML = `<text x="${w / 2}" y="20" text-anchor="middle">this run had no opening round</text>`; return; }
  const t0 = T0(), t1 = Math.max(...rs.map(r => r.t)) + 10, X = t => 60 + (w - 76) * (t - t0) / (t1 - t0);
  const rev = rs.find(e => e.event === 'revealed');
  let out = rev ? `<line x1="${X(rev.t)}" x2="${X(rev.t)}" y1="6" y2="${h - 16}" stroke="${C['ink-dim']}"/><text x="${X(rev.t) + 4}" y="12">revealed${rev.why ? ' · ' + esc(rev.why) : ''}</text>` : '';
  const answered = rs.filter(e => e.event === 'answered').sort((p, q) => p.t - q.t).map(e => e.agent);
  S.agents.forEach((a, i) => {
    const y = 24 + i * rowH, s = rs.find(e => e.event === 'sealed' && e.agent === a), an = rs.find(e => e.event === 'answered' && e.agent === a), col = colorOf(a);
    out += `<text x="52" y="${y + 3}" text-anchor="end">${esc(a)}</text>`;
    if (s) out += `<rect x="${X(s.t) - 3}" y="${y - 3}" width="6" height="6" fill="none" stroke="${col}"><title>sealed at ${fmt(s.t - t0)}</title></rect>`;
    if (s && an) out += `<line x1="${X(s.t)}" x2="${X(an.t)}" y1="${y}" y2="${y}" stroke="${col}" stroke-opacity=".3"/>`;
    if (an) out += `<circle cx="${X(an.t)}" cy="${y}" r="3.5" fill="${col}"><title>answered #${answered.indexOf(a) + 1}${an.late ? ' (late)' : ''}</title></circle><text x="${X(an.t) + 7}" y="${y + 3}">#${answered.indexOf(a) + 1}</text>`;
  });
  svg.innerHTML = out;
}
// genealogy: one lane per agent, derives-from edges as arcs; the current record's ancestry in gold
function lineOf(g) {
  const out = new Set(); if (!g) return out;
  let cur = null; for (const p of S.posts) if (p.who === g.agent && p.t <= g.t + 120 && p.t >= g.t - 1 && (p.text || '').includes('#' + g.id)) { cur = p; break; }
  const stack = cur ? [cur] : [];  // the whole ancestry: every parent of every merge
  while (stack.length) { const q = stack.pop(); if (out.has(q.id)) continue; out.add(q.id); stack.push(...parentsOf(q)); }
  return out;
}
function drawGene() {
  const svg = $('gene'); if (!$('drawer-gene').open) return;
  const n = S.agents.length, rowH = Math.max(8, Math.min(20, 200 / Math.max(1, n))), h = 30 + n * rowH, [w] = svgEl(svg);
  svg.style.height = h + 'px'; svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  const t0 = T0(), t1 = Math.max(Tnow(), t0 + 60), X = t => 50 + (w - 66) * (t - t0) / (t1 - t0), Y = a => 14 + S.agents.indexOf(a) * rowH;
  const lin = lineOf(bestAt());
  let out = '';
  S.agents.forEach(a => { out += `<line x1="50" x2="${w - 16}" y1="${Y(a)}" y2="${Y(a)}" stroke="${C.hair}"/><text x="44" y="${Y(a) + 3}" text-anchor="end" style="fill:${colorOf(a)}">${esc(a)}</text>`; });
  for (const p of S.posts) for (const par of parentsOf(p)) {
    if (p.t > T || !S.agents.includes(p.who)) continue;
    const pa = par.who || par.agent; if (!S.agents.includes(pa)) continue;
    const x1 = X(par.t), y1 = Y(pa), x2 = X(p.t), y2 = Y(p.who), hot = lin.has(p.id), mx = (x1 + x2) / 2;
    out += `<path d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}" fill="none" stroke="${hot ? C.record : C.trail}" stroke-width="${hot ? 2 : 1}" opacity="${hot ? 1 : 0.6}"/>`;
  }
  for (const p of S.posts) {
    if (p.t > T || !S.agents.includes(p.who)) continue;
    const col = lin.has(p.id) ? C.record : p.type === 'WARN' ? C.dead : colorOf(p.who);
    out += `<circle cx="${X(p.t)}" cy="${Y(p.who)}" r="${lin.has(p.id) ? 3.6 : 2.2}" fill="${col}"><title>#${p.id} ${esc(p.who)} ${esc(p.type)}</title></circle>`;
  }
  out += `<text x="50" y="${h - 4}">${fmt(0)}</text><text x="${w - 16}" y="${h - 4}" text-anchor="end">${fmt(t1 - t0)}</text>`;
  svg.innerHTML = out;
}
// herding: the number of distinct lineages with a non-proposal post in the trailing window
const HERD_WIN = 600;
function drawHerd() {
  const svg = $('herd'); if (!$('drawer-herd').open) return;
  const [w, h] = svgEl(svg); svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  const t0 = T0(), t1 = Math.max(Tnow(), t0 + 60), n = Math.max(1, S.agents.length);
  const X = t => 40 + (w - 56) * (t - t0) / (t1 - t0), Y = v => h - 22 - (h - 36) * v / n;
  const work = S.posts.filter(p => p.type !== 'PROPOSAL' && S.agents.includes(p.who));
  const step = Math.max(10, (t1 - t0) / 240), pts = [];
  for (let t = t0; t <= Math.min(T, t1) + 0.1; t += step) pts.push([t, new Set(work.filter(p => p.t > t - HERD_WIN && p.t <= t).map(rootOf)).size]);
  let out = '';
  for (const v of [0, Math.round(n / 2), n]) out += `<line x1="40" x2="${w - 16}" y1="${Y(v)}" y2="${Y(v)}" stroke="${C.hair}"/><text x="34" y="${Y(v) + 3}" text-anchor="end">${v}</text>`;
  if (pts.length > 1) {
    const d = pts.map((p, i) => (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1)).join('');
    out += `<path d="${d}L${X(pts[pts.length - 1][0])},${Y(0)}L${X(pts[0][0])},${Y(0)}Z" fill="${C.measure}" opacity=".10"/><path d="${d}" fill="none" stroke="${C.measure}" stroke-width="1.6"/>`;
    const l = pts[pts.length - 1]; out += `<circle cx="${X(l[0])}" cy="${Y(l[1])}" r="3.5" fill="${C.measure}"/><text x="${X(l[0]) - 6}" y="${Y(l[1]) - 8}" text-anchor="end">${l[1]} of ${n} lines alive (last ${HERD_WIN / 60} min)</text>`;
  }
  out += `<text x="40" y="${h - 4}">${fmt(0)}</text><text x="${w - 16}" y="${h - 4}" text-anchor="end">${fmt(t1 - t0)}</text>`;
  svg.innerHTML = out;
}
// specialisation: each agent's calls by kind, as one stacked bar
const CATS = [['experiment', ['score', 'submit', 'run', 'confirm'], 'cat-1'], ['shell', ['bash', 'write', 'edit', 'read', 'grep', 'glob'], 'cat-2'],
  ['post', ['korax_post', 'korax_dm', 'post', 'dm'], 'cat-3'], ['read the board', ['korax_search', 'korax_read', 'korax_onboard', 'korax_ack', 'korax_fetch', 'search', 'fetch'], 'cat-4'],
  ['wait', ['wait', 'jobs'], 'cat-5'], ['round', ['propose', 'answer'], 'cat-6'], ['thinking aloud', ['say'], 'cat-7']];
function drawSpec() {
  const svg = $('spec'); if (!$('drawer-spec').open) return;
  const n = S.agents.length, rowH = Math.max(8, Math.min(18, 200 / Math.max(1, n))), h = 40 + n * rowH, [w] = svgEl(svg);
  svg.style.height = h + 'px'; svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  const bw = w - 66; let out = '';
  S.agents.forEach((a, i) => {
    const ev = (S.acts[a] || []).filter(e => e.t <= T), tot = ev.length || 1, y = 8 + i * rowH; let x = 50;
    out += `<text x="44" y="${y + rowH / 2 + 2}" text-anchor="end" style="fill:${colorOf(a)}">${esc(a)}</text>`;
    for (const [name, tools, col] of CATS) {
      const k = ev.filter(e => tools.includes(e.tool)).length, ww = bw * k / tot;
      if (ww > 0) out += `<rect x="${x}" y="${y}" width="${ww}" height="${rowH - 3}" fill="${C[col]}" opacity=".85"><title>${esc(a)}: ${k} ${name}</title></rect>`;
      x += ww;
    }
  });
  let lx = 50; for (const [name, , col] of CATS) { out += `<rect x="${lx}" y="${h - 14}" width="8" height="8" fill="${C[col]}"/><text x="${lx + 11}" y="${h - 7}">${name}</text>`; lx += name.length * 6 + 26; }
  svg.innerHTML = out;
}
const drawers = [['drawer-stair', drawStair], ['drawer-gene', drawGene], ['drawer-herd', drawHerd], ['drawer-spec', drawSpec], ['drawer-round', drawRoundline]];
const drawDrawers = () => drawers.forEach(([, f]) => f());
drawers.forEach(([id, f]) => $(id).addEventListener('toggle', f));

// ------------------------------------------------------------------ hover cards
const card = $('card');
farm.addEventListener('mousemove', e => {
  const r = farm.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  let best = null, bd = 1e9;
  for (const h of hits) { const d = Math.hypot(h.x - x, h.y - y); if (d < h.r && d < bd) { best = h; bd = d; } }
  if (!best) { card.hidden = true; return; }
  const it = best.item;
  if (it.ant) card.innerHTML = `<div class="hd" style="color:${colorOf(it.ant)}">${esc(it.ant)} · ${esc(S.backend[it.ant] || '')}</div><div class="tx">${esc(it.why || it.st)}</div>`;
  else if (it.gate) card.innerHTML = `<div class="hd" style="color:${it.record ? C.record : colorOf(it.agent)}">gate · ${it.record ? 'record' : 'verdict'}</div><div class="id">#${it.id} · ${fmt(it.t - T0())}</div><div class="tx">${esc(it.agent)}'s ${esc(it.policy)}: ${num(it.score)}${it.ci95 ? ` (95% ${num(it.ci95[0])}–${num(it.ci95[1])})` : ''}\n${it.confirmed === true ? 'confirmed by a second batch' : it.confirmed === false ? 'not confirmed' : 'one batch'}</div>`;
  else { const par = parentOf(it);
    card.innerHTML = `<div class="hd" style="color:${it.type === 'WARN' ? C.dead : colorOf(it.who)}">${esc(it.who)} · ${esc(it.type)}</div><div class="id">#${it.id} · ${fmt(it.t - T0())}${par ? ` · derives from #${par.id} (${esc(par.who || par.agent)})` : ''}</div><div class="tx">${esc((it.text || '').slice(0, 700))}</div>`; }
  card.hidden = false;
  const cw = card.offsetWidth, ch = card.offsetHeight;
  card.style.left = Math.min(r.width - cw - 8, x + 14) + 'px'; card.style.top = Math.min(r.height - ch - 8, y + 14) + 'px';
});
farm.addEventListener('mouseleave', () => { card.hidden = true; });
farm.addEventListener('click', () => {
  const vis = [...hits].reverse().find(h => !card.hidden && h.item.id != null);
  if (!vis) return; focusId = vis.item.id;
  const el = tty.querySelector(`[data-id="${focusId}"]`); tty.querySelectorAll('.hl').forEach(x => x.classList.remove('hl'));
  if (el) { el.classList.add('hl'); el.scrollIntoView({ block: 'center', behavior: reduced ? 'auto' : 'smooth' }); }
});

// ------------------------------------------------------------------ scrubber and keys
const slider = $('t'), playB = $('play'), speedB = $('speed'), liveB = $('live');
const span = () => Math.max(1, Tnow() - T0());
function setFollow(on) {
  follow = on && !ended(); if (follow) { playing = false; playB.textContent = 'play'; }
  liveB.classList.toggle('on', follow); liveB.disabled = ended(); liveB.textContent = ended() ? 'ended' : 'live';
}
function seek(t) { spoilT = null; T = Math.max(T0(), Math.min(Tnow(), t)); setFollow(false); ttyCount = -1; dirtySand = true; uiTick(true); drawDrawers(); }
slider.addEventListener('input', () => seek(T0() + span() * slider.value / 1000));
function toggle() {
  if (follow) setFollow(false);
  playing = !playing; if (playing && T >= Tnow() - 0.5) { T = T0(); ttyCount = -1; }
  playB.textContent = playing ? 'pause' : 'play'; lastFrame = performance.now();
}
playB.addEventListener('click', toggle);
speedB.addEventListener('click', () => { speed = speed === 20 ? 60 : speed === 60 ? 200 : speed === 200 ? 5 : 20; speedB.textContent = '×' + speed; });
liveB.addEventListener('click', () => { setFollow(!follow); ttyCount = -1; });
const sheet = $('sheet');
$('keys').addEventListener('click', () => { sheet.hidden = !sheet.hidden; });
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' && e.key !== ' ') return;
  if (e.target.tagName === 'SUMMARY' && e.key === ' ') return;
  if (e.key === ' ') { e.preventDefault(); toggle(); }
  else if (e.key === 'ArrowRight') seek(T + 15); else if (e.key === 'ArrowLeft') seek(T - 15);
  else if (e.key === 'l' || e.key === 'L') { setFollow(true); ttyCount = -1; }
  else if (e.key === '?') sheet.hidden = !sheet.hidden; else if (e.key === 'Escape') { sheet.hidden = true; focusId = null; }
});
function ticks() {
  const s = span(), t0 = T0();
  $('ticks').innerHTML = S.gates.filter(g => g.record).map(g => `<i style="left:${(100 * (g.t - t0) / s).toFixed(2)}%"></i>`).join('');
}

// ------------------------------------------------------------------ polling
async function poll() {
  let wait = 2000;
  try {
    const r = await fetch(`/api/events?after=${S.seq}`, { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json(); if (d.error) throw new Error(d.error);
    const first = !S.meta;
    S.meta = d.meta;
    if (first) {
      S.agents = d.meta.agents.map(a => a.name);
      for (const a of d.meta.agents) S.backend[a.name] = a.backend;
      const kinds = [...new Set(Object.values(S.backend))];
      $('legend-backends').innerHTML = kinds.map(k => `<span><i class="sw" style="background:${C[k] || C.ink}"></i>${k === 'fake' ? 'scripted' : k}</span>`).join('');
      $('sub').textContent = `formicarium · ${d.meta.task} · ${S.agents.length} agents · ${d.meta.run}`;
      document.title = `${d.meta.run} · formicarium`;
    }
    for (const ev of d.events) fold(ev);
    if (d.events.length) afterFold();
    S.seq = d.seq; S.live = d.agents || {}; S.spent = d.spent_total; S.boardError = d.board_error;
    S.serverNow = d.now; S.polledPerf = performance.now(); S.lastOkPerf = S.polledPerf; S.everOk = true; S.pollFailed = false;
    if (first) { T = Tnow(); setFollow(!ended()); ttyCount = -1; }
    if (d.events.length) { dirtySand = true; ticks(); drawDrawers(); }
    if (ended() && !d.events.length) wait = 15000;
  } catch (e) {
    S.pollFailed = true; wait = 4000;
  }
  setTimeout(poll, wait);
}

// ------------------------------------------------------------------ the loop
let lastUi = 0, lastSandT = -1, lastSandV = -1, lastSandPerf = 0;
function uiTick(force) { hud(); caption(); drawTty(force); if (!follow) slider.value = Math.round(1000 * (T - T0()) / span()); else slider.value = 1000; }
function frame(now) {
  if (S.meta) {
    if (follow) T = Tnow();
    else if (playing) { T = Math.min(Tnow(), T + (now - lastFrame) / 1000 * speed); if (T >= Tnow()) { playing = false; playB.textContent = 'play'; if (!ended()) setFollow(true); } }
    lastFrame = now;
    const P = PROF ? (k, t) => { PROF[k] = (PROF[k] || 0) + performance.now() - t; } : () => {}; let t_ = performance.now();
    const vis = visiblePosts(), trails = new Map(S.agents.map(a => [a, trailOf(a, vis)])); P('fold', t_);
    // the sand redraws when time moved (at most 8×/s while following) or the data changed
    const moved = Math.abs(T - lastSandT) > 0.01, due = now - lastSandPerf > (playing ? 0 : 125);
    if (dirtySand || lastSandV !== S.version || (moved && due)) {
      t_ = performance.now(); paintGround(); P('ground', t_); t_ = performance.now(); paintDig(vis, trails); P('dig', t_); t_ = performance.now(); paintLight(vis, trails); P('light', t_);
      lastSandT = T; lastSandV = S.version; lastSandPerf = now; dirtySand = false;
    }
    const b = bestAt(); let bestPt = null;
    if (b && b.record) { const [x, y] = chamberAt(b, vis); bestPt = [x, y, reduced ? 0.25 : 0.22 + 0.14 * Math.sin(now / 420)]; }
    t_ = performance.now(); drawSand(bestPt, now); P('sand', t_);
    t_ = performance.now(); drawMarks(vis, trails, now); P('marks', t_);
    spoil(vis, now); drawGrains(now, Math.min(0.05, (now - (frame.last || now)) / 1000)); frame.last = now;
    if (PROF) PROF.frames = (PROF.frames || 0) + 1;
    if (now - lastUi > 200) { t_ = performance.now(); uiTick(false); P('ui', t_); lastUi = now; if (playing) drawDrawers(); }
  } else if (now - lastUi > 500) { hud(); lastUi = now; }
  requestAnimationFrame(frame);
}

layout(); initGL();
new ResizeObserver(() => layout()).observe(glassEl);
poll(); requestAnimationFrame(frame);
})();
