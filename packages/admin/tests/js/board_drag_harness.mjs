// Board drag-gesture harness — D-BI1, proven against the real board.html.
//
// The board page is one plain-JS file with no framework and no build step
// (D-MB3), so there is no module to import and no JS unit runner in this
// package. Same answer as sw_fetch_harness.mjs / ordered_bot_ids_harness.mjs:
// evaluate the REAL page script in a mock browser scope and assert the
// invariants from here. Run directly (`node board_drag_harness.mjs`) or via
// tests/test_board_drag.py.
//
// What it proves:
//
//   1. SAME BODY AS THE SHEET. A press-and-hold drag onto a lane header
//      posts exactly the request the move sheet's button posts — same path,
//      same method, same JSON. The page has ONE move path; this is the proof
//      that the gesture did not grow a second one that can drift.
//   2. LETTING GO OVER NOTHING IS FREE. A drag released over neither a lane
//      header nor the Bot zone sends no request at all.
//   3. THE BOT ZONE CHANGES WHO, NOT WHEN. A drop on the zone posts
//      …/assign {owner:"bot"} — never …/move.
//   4. A HOLD THAT MOVES FIRST IS A SCROLL. Movement past the slop before
//      the 400 ms hold elapses cancels the gesture; a later release sends
//      nothing and lifts no card.
//   5. EDGE AUTO-SCROLL. Dragging into the band at the top of the viewport
//      scrolls the page (the answer to §6 Q1 — no lane picker in this chip).
//   6. REDUCED MOTION DISABLES THE LIFT, NOT THE GESTURE. With
//      prefers-reduced-motion the lifted clone carries no transform, and the
//      same drop still posts the same move.
//   7. DROPPING ASKS FIRST. A drag onto the Dropped header opens the reason
//      sheet instead of posting; picking a reason posts it, and "Just drop
//      it" posts the move with no reason.
//   8. OWNER IS MARKED BY A PATTERN. A bot-owned tile carries the stripe
//      class, not merely a colour, and says "bot" in words.
//   9. THE PLATFORM'S OWN LONG-PRESS DOES NOT WIN. A held card suppresses
//      contextmenu (Android's long-press menu arrives mid-gesture), and the
//      page leaves contextmenu alone when nothing is held.
//  10. A POLL DURING A TAP IS DEFERRED, NOT DROPPED. A refresh that lands
//      inside the ~400ms hold of an ordinary tap is applied when the finger
//      lifts, not discarded until the next poll 30s later.
//
// Fidelity caveats (what a green run does NOT prove): this mock DOM has no
// layout, so element rects are supplied by the harness rather than measured —
// it proves the gesture's LOGIC and its requests, not that a lane header is
// physically hittable on a phone. It has no CSS engine, so `touch-action`,
// the stripe pattern and the lift shadow are asserted as the class/style the
// page sets, not as rendered pixels. The phone check is the operator's.

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const PAGE = resolve(HERE, '../../evolve_admin/web/board.html');

let failures = 0;
function check(name, cond, detail) {
  if (cond) console.log(`ok   ${name}`);
  else { failures++; console.log(`FAIL ${name}${detail ? ` — ${detail}` : ''}`); }
}
function eq(name, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  check(name, a === e, `got ${a}, want ${e}`);
}

// ── the page's script, extracted verbatim ────────────────────────────────
const html = readFileSync(PAGE, 'utf8');
const script = html.match(/<script>\n([\s\S]*?)\n<\/script>/);
if (!script) { console.log('FAIL could not find the page script'); process.exit(1); }

// Every element id the static markup declares — the mock document serves
// these from getElementById, so the page finds exactly what a browser would.
const STATIC_IDS = [...html.matchAll(/\sid="([a-z0-9-]+)"/g)].map((m) => m[1]);

// ── mock DOM ─────────────────────────────────────────────────────────────
function makeNode(tag) {
  const node = {
    tagName: (tag || 'div').toUpperCase(),
    children: [], parentNode: null, style: {}, attrs: {}, dataset: {},
    id: '', className: '', textContent: '', hidden: false, tabIndex: -1,
    listeners: {}, rect: null, onclick: null, onkeydown: null,
    classList: {
      add(...c) { for (const x of c) if (!node._classes().includes(x)) node.className = (node.className + ' ' + x).trim(); },
      remove(...c) { node.className = node._classes().filter((x) => !c.includes(x)).join(' '); },
      contains(c) { return node._classes().includes(c); },
    },
    _classes() { return node.className.split(/\s+/).filter(Boolean); },
    appendChild(child) { child.parentNode = node; node.children.push(child); return child; },
    removeChild(child) {
      node.children = node.children.filter((c) => c !== child);
      child.parentNode = null; return child;
    },
    replaceChildren(...kids) { node.children = kids; for (const k of kids) k.parentNode = node; },
    setAttribute(k, v) { node.attrs[k] = String(v); if (k === 'id') node.id = String(v); },
    getAttribute(k) { return k in node.attrs ? node.attrs[k] : null; },
    hasAttribute(k) { return k in node.attrs; },
    addEventListener(type, fn) { (node.listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) {
      node.listeners[type] = (node.listeners[type] || []).filter((f) => f !== fn);
    },
    // No layout engine: the harness pins a rect where it needs a hit test,
    // and anything unpinned is a zero-size box nothing can land on.
    getBoundingClientRect() {
      return node.rect || { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
    },
    cloneNode() {
      const copy = makeNode(node.tagName);
      copy.className = node.className;
      copy.textContent = node.textContent;
      copy.attrs = { ...node.attrs };
      copy.id = node.id;
      return copy;
    },
    fire(type, ev) { for (const fn of node.listeners[type] || []) fn(ev); },
  };
  return node;
}

function walk(node, out = []) {
  out.push(node);
  for (const kid of node.children) walk(kid, out);
  return out;
}

function makeScope({ reducedMotion = false } = {}) {
  const byId = {};
  for (const id of STATIC_IDS) { byId[id] = makeNode('div'); byId[id].setAttribute('id', id); }
  const body = makeNode('body');
  const head = makeNode('head');
  const timers = [];
  const docListeners = {};
  const posted = [];
  const scrolls = [];
  let fetchBoard = { cards: [] };

  const document = {
    body, head,
    createElement: (tag) => makeNode(tag),
    getElementById: (id) => {
      if (byId[id]) return byId[id];
      // Ids the page itself mints (bot-zone, bot-plate) live in the tree.
      for (const n of walk(byId['lanes'] || makeNode('div'))) {
        if (n.id === id) return n;
      }
      return null;
    },
    querySelectorAll: (sel) => {
      const attr = sel.replace(/[[\]]/g, '');
      const roots = [byId['lanes'], body].filter(Boolean);
      const hits = [];
      for (const r of roots) for (const n of walk(r)) if (n.hasAttribute(attr)) hits.push(n);
      return hits;
    },
    addEventListener: (type, fn) => { (docListeners[type] ||= []).push(fn); },
    hidden: false,
  };

  const scope = {
    document,
    console,
    navigator: {},
    location: { pathname: '/board/dom', search: '' },
    history: { replaceState() {} },
    URLSearchParams,
    JSON, Object, Array, Math, String, Number, Date, Error, Promise, RegExp,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (h) => { if (timers[h - 1]) timers[h - 1].cancelled = true; },
    setInterval: () => 0,
    requestAnimationFrame: () => 1,
    cancelAnimationFrame: () => {},
    matchMedia: (q) => ({
      matches: reducedMotion && q.includes('reduced-motion'), media: q,
    }),
    innerHeight: 800,
    scrollBy: (x, y) => { scrolls.push([x, y]); },
    matchedDropReasons: null,
    fetch: (path, opts) => {
      opts = opts || {};
      if ((opts.method || 'GET') === 'GET') {
        return Promise.resolve({
          ok: true, status: 200,
          headers: { get: () => '"etag"' },
          json: () => Promise.resolve(fetchBoard),
        });
      }
      posted.push({ path, method: opts.method, body: JSON.parse(opts.body) });
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
    },
  };
  scope.window = scope;
  scope.globalThis = scope;

  return {
    scope, byId, body, posted, scrolls, timers, docListeners,
    setBoard(b) { fetchBoard = b; },
    // A contextmenu event as the page's document listener sees it.
    contextmenu() {
      let prevented = false;
      for (const fn of docListeners['contextmenu'] || []) {
        fn({ preventDefault: () => { prevented = true; } });
      }
      return prevented;
    },
    // The page's own `hold` timer is the only setTimeout it schedules with a
    // delay; firing it is what "the user held for 400 ms" means here.
    fireHoldTimer() {
      const t = timers.filter((t) => !t.cancelled && t.ms === 400).pop();
      if (!t) return false;
      t.cancelled = true; t.fn(); return true;
    },
    doc(type, ev) { for (const fn of docListeners[type] || []) fn(ev); },
    tiles() {
      return walk(byId['lanes']).filter((n) => n.hasAttribute('data-card-id'));
    },
    // Re-looked-up every time: each refresh re-renders the board, so a tile
    // held across a write is a node no longer in the tree.
    tile(id) {
      return walk(byId['lanes']).find((n) => n.attrs['data-card-id'] === id);
    },
    laneHead(label) {
      return walk(byId['lanes']).find(
        (n) => n.className.includes('lane-head') &&
               n.children.some((c) => c.textContent === label));
    },
    zone() { return walk(byId['lanes']).find((n) => n.id === 'bot-zone'); },
    // The 30s poll, as the page runs it: the visibilitychange listener calls
    // the page's own refresh(), so firing that is a faithful stand-in for the
    // interval the harness does not run.
    refresh() { for (const fn of docListeners['visibilitychange'] || []) fn(); },
  };
}

const RECT = (x, y) => ({ left: x - 40, right: x + 40, top: y - 12, bottom: y + 12, width: 80, height: 24 });

async function boot(opts = {}) {
  const h = makeScope(opts);
  h.setBoard(opts.board || {
    cards: [
      { id: 'aaaa1111', title: 'Book dentist', cluster: 'health', lane: 'inbox', owner: 'me' },
      { id: 'bbbb2222', title: 'Draft reply', cluster: 'work', lane: 'today', owner: 'bot',
        delegation: { state: 'offered' } },
    ],
  });
  vm.createContext(h.scope);
  vm.runInContext(script[1], h.scope);
  await new Promise((r) => setImmediate(r));   // let refresh()'s promise settle
  return h;
}

/** One complete press-and-hold drag from a tile to a point. */
function dragTo(h, tile, point, { hold = true } = {}) {
  const start = { left: 0, top: 0, right: 200, bottom: 60, width: 200, height: 60 };
  tile.rect = start;
  tile.fire('pointerdown', { button: 0, pointerId: 1, clientX: 20, clientY: 30 });
  if (hold) h.fireHoldTimer();
  h.doc('pointermove', { clientX: point.x, clientY: point.y });
  h.doc('pointerup', { clientX: point.x, clientY: point.y });
}

// ── 1/2/3/8: the three drop outcomes, and owner marking ──────────────────
{
  const h = await boot();
  const botTile = h.tile('bbbb2222');

  check('8. bot-owned tile carries the stripe PATTERN, not just a colour',
    botTile.className.includes('owner-stripe') && botTile.className.includes('owner-bot'),
    botTile.className);
  check('8. bot-owned tile says "bot" in words too',
    walk(botTile).some((n) => n.textContent === 'bot'));

  // The sheet's Today button — the body every other path must match.
  h.tile('aaaa1111').onclick();
  const moveRow = h.byId['move-row'];
  moveRow.children.find((b) => b.textContent === 'Today').onclick();
  await new Promise((r) => setImmediate(r));
  const fromSheet = h.posted.pop();

  const today = h.laneHead('Today');
  today.rect = RECT(150, 400);
  dragTo(h, h.tile('aaaa1111'), { x: 150, y: 400 });
  await new Promise((r) => setImmediate(r));
  const fromDrag = h.posted.pop();
  eq('1. a drag posts exactly what the sheet posts', fromDrag, fromSheet);
  eq('1. …and that body is the move it claims to be', fromDrag, {
    path: '/api/board/dom/cards/aaaa1111/move', method: 'POST',
    body: { to_lane: 'today' },
  });

  // Over neither a lane header nor the zone.
  h.posted.length = 0;
  dragTo(h, h.tile('aaaa1111'), { x: 600, y: 700 });
  await new Promise((r) => setImmediate(r));
  eq('2. releasing over nothing sends nothing', h.posted, []);

  // The Bot drop zone: a WHO change.
  const zone = h.zone();
  zone.rect = RECT(150, 120);
  dragTo(h, h.tile('aaaa1111'), { x: 150, y: 120 });
  await new Promise((r) => setImmediate(r));
  eq('3. a drop on the Bot zone posts assign, not move', h.posted, [{
    path: '/api/board/dom/cards/aaaa1111/assign', method: 'POST',
    body: { owner: 'bot' },
  }]);

  // 4. a hold that moved first was a scroll.
  h.posted.length = 0;
  let inboxTile = h.tile('aaaa1111');
  inboxTile.rect = { left: 0, top: 0, right: 200, bottom: 60, width: 200, height: 60 };
  inboxTile.fire('pointerdown', { button: 0, pointerId: 1, clientX: 20, clientY: 30 });
  h.doc('pointermove', { clientX: 20, clientY: 200 });   // past the slop
  h.fireHoldTimer();                                     // fires into a cancelled hold
  h.doc('pointermove', { clientX: 150, clientY: 400 });
  h.doc('pointerup', { clientX: 150, clientY: 400 });
  await new Promise((r) => setImmediate(r));
  eq('4. moving before the hold elapses cancels the gesture', h.posted, []);

  // 5. edge auto-scroll.
  h.scrolls.length = 0;
  inboxTile = h.tile('aaaa1111');
  inboxTile.rect = { left: 0, top: 0, right: 200, bottom: 60, width: 200, height: 60 };
  inboxTile.fire('pointerdown', { button: 0, pointerId: 1, clientX: 20, clientY: 30 });
  h.fireHoldTimer();
  h.doc('pointermove', { clientX: 150, clientY: 20 });    // inside the top band
  check('5. dragging into the top edge band scrolls the page',
    h.scrolls.length > 0 && h.scrolls[0][1] < 0, JSON.stringify(h.scrolls));
  h.doc('pointerup', { clientX: 150, clientY: 20 });

  // 7. dropping asks first.
  h.posted.length = 0;
  const droppedHead = h.laneHead('Dropped');
  check('7. the Dropped header exists as a target even while the lane is empty',
    !!droppedHead);
  const droppedSec = droppedHead.parentNode;
  check('7. …and that empty lane is hidden when nothing is being dragged',
    droppedSec.hidden === true);
  droppedHead.rect = RECT(150, 600);
  {
    // The reveal is the mechanism: a lane you cannot see is a lane you
    // cannot drop into, and Dropped is empty on every board's first day.
    const t = h.tile('aaaa1111');
    t.rect = { left: 0, top: 0, right: 200, bottom: 60, width: 200, height: 60 };
    t.fire('pointerdown', { button: 0, pointerId: 1, clientX: 20, clientY: 30 });
    h.fireHoldTimer();
    check('7. holding a card reveals the hidden empty lanes',
      droppedSec.hidden === false);
    const lifted = h.body.children.find((n) => n.className.includes('drag-ghost'));
    check('6. without reduced motion the clone DOES carry the lift transform',
      !!lifted && lifted.style.transform === 'scale(1.03)',
      lifted && lifted.style.transform);
    h.doc('pointerup', { clientX: 900, clientY: 900 });
    check('7. …and they are hidden again once the card is let go',
      droppedSec.hidden === true);
  }
  dragTo(h, h.tile('aaaa1111'), { x: 150, y: 600 });
  await new Promise((r) => setImmediate(r));
  eq('7. a drop does not post until the reason sheet is answered', h.posted, []);
  const reasons = h.byId['drop-row'].children.map((b) => b.textContent);
  eq('7. the reason set is D-BI2 verbatim', reasons,
    ['not mine', 'already handled', 'never', 'later than later']);
  h.byId['drop-row'].children.find((b) => b.textContent === 'never').onclick();
  await new Promise((r) => setImmediate(r));
  eq('7. picking a reason posts it on the move', h.posted, [{
    path: '/api/board/dom/cards/aaaa1111/move', method: 'POST',
    body: { to_lane: 'dropped', reason: 'never' },
  }]);

  h.posted.length = 0;
  h.laneHead('Dropped').rect = RECT(150, 600);
  dragTo(h, h.tile('aaaa1111'), { x: 150, y: 600 });
  await new Promise((r) => setImmediate(r));
  h.byId['drop-go'].onclick();
  await new Promise((r) => setImmediate(r));
  eq('7. "Just drop it" posts the move with no reason', h.posted, [{
    path: '/api/board/dom/cards/aaaa1111/move', method: 'POST',
    body: { to_lane: 'dropped' },
  }]);
}

// ── 9: the platform's own long-press must not win the gesture ────────────
{
  // The CSS half, asserted against the SOURCE — this harness has no CSS
  // engine, and the properties are the whole fix, so "we can't render it" is
  // not a reason to leave them unpinned. Both spellings of user-select are
  // required (Safari still wants the prefix) and the callout property is
  // Safari-only; dropping any one of them puts the selection loupe back.
  const cardRule = html.slice(html.indexOf('  .card {'),
                              html.indexOf('  .card:active'));
  for (const prop of ['-webkit-user-select: none', 'user-select: none',
                      '-webkit-touch-callout: none']) {
    check(`9. .card declares ${prop}`, cardRule.includes(prop));
  }

  const h = await boot();
  check('9. contextmenu is left alone when nothing is held',
    h.contextmenu() === false);

  const tile = h.tile('aaaa1111');
  tile.rect = { left: 0, top: 0, right: 200, bottom: 60, width: 200, height: 60 };
  tile.fire('pointerdown', { button: 0, pointerId: 1, clientX: 20, clientY: 30 });
  check('9. …and suppressed during the hold, before the card even lifts',
    h.contextmenu() === true);
  h.fireHoldTimer();
  check('9. …and while the card is held', h.contextmenu() === true);
  h.doc('pointerup', { clientX: 900, clientY: 900 });
  check('9. …and left alone again once the card is let go',
    h.contextmenu() === false);
}

// ── 10: a poll landing inside a TAP is deferred, not dropped ─────────────
{
  const h = await boot();
  const before = h.tiles().length;
  const tile = h.tile('aaaa1111');
  tile.rect = { left: 0, top: 0, right: 200, bottom: 60, width: 200, height: 60 };

  // Press and hold — but never long enough to lift the card: a plain tap.
  tile.fire('pointerdown', { button: 0, pointerId: 1, clientX: 20, clientY: 30 });
  // The 30s poll lands mid-tap with a card the page has not seen.
  h.setBoard({
    cards: [
      { id: 'aaaa1111', title: 'Book dentist', cluster: 'health', lane: 'inbox', owner: 'me' },
      { id: 'bbbb2222', title: 'Draft reply', cluster: 'work', lane: 'today', owner: 'bot',
        delegation: { state: 'offered' } },
      { id: 'cccc3333', title: 'Arrived mid-tap', cluster: 'home', lane: 'today', owner: 'me' },
    ],
  });
  h.refresh();
  await new Promise((r) => setImmediate(r));
  check('10. a poll during the hold is NOT applied under the finger',
    h.tiles().length === before, `${h.tiles().length} vs ${before}`);

  h.doc('pointerup', { clientX: 20, clientY: 30 });
  check('10. …and IS applied the moment the tap ends',
    !!h.tile('cccc3333'));
}

// ── 6: reduced motion ────────────────────────────────────────────────────
{
  const h = await boot({ reducedMotion: true });
  const tile = h.tile('aaaa1111');
  tile.rect = { left: 0, top: 0, right: 200, bottom: 60, width: 200, height: 60 };
  tile.fire('pointerdown', { button: 0, pointerId: 1, clientX: 20, clientY: 30 });
  h.fireHoldTimer();
  const ghost = h.body.children.find((n) => n.className.includes('drag-ghost'));
  check('6. reduced motion still lifts a clone (the gesture is not disabled)', !!ghost);
  check('6. …and that clone carries no lift transform',
    !!ghost && ghost.style.transform === undefined, ghost && ghost.style.transform);
  const today = h.laneHead('Today');
  today.rect = RECT(150, 400);
  h.doc('pointermove', { clientX: 150, clientY: 400 });
  h.doc('pointerup', { clientX: 150, clientY: 400 });
  await new Promise((r) => setImmediate(r));
  eq('6. …and the same drop still posts the same move', h.posted, [{
    path: '/api/board/dom/cards/aaaa1111/move', method: 'POST',
    body: { to_lane: 'today' },
  }]);
  check('6. the lifted clone is removed on release',
    !h.body.children.some((n) => n.className.includes('drag-ghost')));
}

console.log(failures === 0
  ? '\nall board drag invariants hold'
  : `\n${failures} board drag invariant(s) failed`);
process.exit(failures === 0 ? 0 : 1);
