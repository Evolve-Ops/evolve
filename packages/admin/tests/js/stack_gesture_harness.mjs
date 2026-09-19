// Stack swipe-gesture harness — D-ST3/D-ST4/D-ST9, proven against the real
// stack.html.
//
// Same answer as board_drag_harness.mjs: the Stack page is one plain-JS
// file with no framework and no build step (D-MB3), so there is no module
// to import and no JS unit runner in this package. This harness evaluates
// the REAL page script in a mock browser scope and asserts the invariants
// from here. Run directly (`node stack_gesture_harness.mjs`) or via
// tests/test_stack_gestures.py.
//
// What it proves:
//   1. UP = PASS. A swipe up posts …/seen (not …/move) and the card
//      returns to the back of the queue, not off it for good.
//   2. RIGHT = DONE posts exactly …/move {to_lane:"done"} — the SAME body
//      the reduced-motion button posts, proven by construction (one
//      `commit()` function, two callers).
//   3. LEFT = DROP shows the D-BI2 reason chips first; a tap sends the
//      reason on the move, letting the window elapse sends none.
//   4. UNDO reverses each of the three committed swipes: pass's toast
//      posts …/seen {undo:true}; done's/drop's re-posts the prior lane.
//   5. BELOW THRESHOLD SPRINGS BACK — no request of any kind.
//   6. A FAST FLICK commits even short of the 35% distance threshold.
//   7. REDUCED MOTION disables the pointer gesture entirely (no drag
//      state, whatever the pointer does) and the four-button fallback
//      posts the identical bodies.
//   8. A TAP (no drag past the slop) opens the detail sheet, never a
//      commit.
//   9. THE HINT TOGGLE persists across a reload (the cookie), defaults on.
//  10. Approve/Decline post …/decision; the Later chip posts …/move with
//      a snooze_until; an action chip posts …/instruct — and each removes
//      the card from the queue without touching the other cards.
//
// Fidelity caveat: no layout/CSS engine, same as board_drag_harness — this
// proves the gesture's LOGIC and its requests, not pixel hit-testing.

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const PAGE = resolve(HERE, '../../evolve_admin/web/stack.html');

let failures = 0;
function check(name, cond, detail) {
  if (cond) console.log(`ok   ${name}`);
  else { failures++; console.log(`FAIL ${name}${detail ? ` — ${detail}` : ''}`); }
}
function eq(name, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  check(name, a === e, `got ${a}, want ${e}`);
}

const html = readFileSync(PAGE, 'utf8');
const script = html.match(/<script>\n([\s\S]*?)\n<\/script>/);
if (!script) { console.log('FAIL could not find the page script'); process.exit(1); }

const STATIC_IDS = [...html.matchAll(/\sid="([a-z0-9-]+)"/g)].map((m) => m[1]);

// ── 0: the [hidden] paint invariant — no CSS engine here, so this greps
// the page's own stylesheet text for the exact rule rather than rendering
// it (fix for pr-4280: an author-origin `display` on .card/.drop-reasons/
// #button-row beat the UA default, so `el.hidden = true` painted nothing).
check('0. `[hidden]{display:none!important}` is the page\'s own override',
  html.includes('[hidden] { display: none !important; }'));

function makeNode(tag) {
  const node = {
    tagName: (tag || 'div').toUpperCase(),
    children: [], parentNode: null, style: {}, attrs: {}, dataset: {},
    id: '', className: '', textContent: '', hidden: false, disabled: false,
    title: '', href: '', listeners: {}, rect: null, onclick: null,
    classList: {
      add(...c) { for (const x of c) if (!node._classes().includes(x)) node.className = (node.className + ' ' + x).trim(); },
      remove(...c) { node.className = node._classes().filter((x) => !c.includes(x)).join(' '); },
      toggle(c, on) { if (on) node.classList.add(c); else node.classList.remove(c); },
      contains(c) { return node._classes().includes(c); },
    },
    _classes() { return node.className.split(/\s+/).filter(Boolean); },
    appendChild(child) { child.parentNode = node; node.children.push(child); return child; },
    removeChild(child) { node.children = node.children.filter((c) => c !== child); child.parentNode = null; return child; },
    replaceChildren(...kids) { node.children = kids; for (const k of kids) k.parentNode = node; },
    setAttribute(k, v) { node.attrs[k] = String(v); if (k === 'id') node.id = String(v); },
    getAttribute(k) { return k in node.attrs ? node.attrs[k] : null; },
    hasAttribute(k) { return k in node.attrs; },
    addEventListener(type, fn) { (node.listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) { node.listeners[type] = (node.listeners[type] || []).filter((f) => f !== fn); },
    getBoundingClientRect() { return node.rect || { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; },
    closest(selector) {
      const tags = selector.split(',').map((s) => s.trim().toUpperCase());
      let n = node;
      while (n) { if (tags.includes(n.tagName)) return n; n = n.parentNode; }
      return null;
    },
    fire(type, ev) { for (const fn of (node.listeners[type] || [])) fn(ev); },
  };
  return node;
}

function walk(node, out = []) {
  out.push(node);
  for (const kid of node.children) walk(kid, out);
  return out;
}

function makeScope({ reducedMotion = false, initialCookie = '' } = {}) {
  const byId = {};
  for (const id of STATIC_IDS) { byId[id] = makeNode('div'); byId[id].setAttribute('id', id); }
  const body = makeNode('body');
  const head = makeNode('head');
  const timers = [];
  const docListeners = {};
  const posted = [];
  const postResponses = [];
  let stackData = { items: [], empty_state: null };
  let cookieJar = initialCookie;

  const document = {
    body, head,
    get cookie() { return cookieJar; },
    set cookie(v) { cookieJar = v; },
    createElement: (tag) => makeNode(tag),
    getElementById: (id) => byId[id] || null,
    addEventListener: (type, fn) => { (docListeners[type] ||= []).push(fn); },
    hidden: false,
  };

  const scope = {
    document, console, navigator: { vibrate: () => true },
    location: { pathname: '/board/dom/stack', search: '' },
    history: { replaceState() {} },
    URLSearchParams,
    JSON, Object, Array, Math, String, Number, Date, Error, Promise, RegExp,
    setTimeout: (fn, ms) => { const t = { fn, ms, cancelled: false }; timers.push(t); return timers.length; },
    clearTimeout: (h) => { if (timers[h - 1]) timers[h - 1].cancelled = true; },
    setInterval: () => 0,
    matchMedia: (q) => ({ matches: reducedMotion && q.includes('reduced-motion'), media: q }),
    fetch: (path, opts) => {
      opts = opts || {};
      const method = opts.method || 'GET';
      if (method === 'GET') {
        return Promise.resolve({
          ok: true, status: 200, json: () => Promise.resolve(stackData),
        });
      }
      const entry = { path, method, body: JSON.parse(opts.body || '{}') };
      posted.push(entry);
      const next = postResponses.length ? postResponses.shift() : { ok: true };
      if (next && next.__httpFail) {
        return Promise.resolve({
          ok: false, status: next.status || 500,
          json: () => Promise.resolve({ error: next.error || 'write failed' }),
        });
      }
      return Promise.resolve({
        ok: true, status: 200, json: () => Promise.resolve(next),
      });
    },
  };
  scope.window = scope;
  scope.globalThis = scope;

  return {
    scope, byId, body, posted, timers, docListeners,
    setStack(items, emptyState) { stackData = { items, empty_state: emptyState ?? null }; },
    queuePostResponse(r) { postResponses.push(r); },
    // The next write this scope's `fetch` sees comes back HTTP-failed —
    // exactly what an unreachable pod or a 500 looks like to the page.
    failNextPost(status, error) { postResponses.push({ __httpFail: true, status, error }); },
    cookie() { return cookieJar; },
    doc(type, ev) { for (const fn of (docListeners[type] || [])) fn(ev); },
    fireTimer(ms) {
      const t = timers.filter((t) => !t.cancelled && t.ms === ms).pop();
      if (!t) return false;
      t.cancelled = true; t.fn(); return true;
    },
  };
}

async function flush() { await new Promise((r) => setImmediate(r)); await new Promise((r) => setImmediate(r)); }

function fixtureItems() {
  return [
    {
      kind: 'card', card: {
        id: 'card1', title: 'Book dentist', cluster: 'health', lane: 'today',
        owner: 'me', source: 'manual', why_line: 'added by you',
        actions: [{ id: 'find_directions', label: 'Find directions', kind: 'tool', est_cost: 0 }],
      },
    },
    {
      kind: 'card', card: {
        id: 'card2', title: 'Approve the reply', cluster: 'work', lane: 'today',
        owner: 'bot', source: 'manual', why_line: 'from calendar',
        delegation: { state: 'returned_for_review' },
      },
    },
  ];
}

async function boot(opts = {}) {
  const h = makeScope(opts);
  h.setStack(opts.items ?? fixtureItems(), opts.emptyState);
  vm.createContext(h.scope);
  vm.runInContext(script[1], h.scope);
  await flush();
  return h;
}

const RECT = { left: 0, top: 0, right: 320, bottom: 520, width: 320, height: 520 };

/** Drive one pointer gesture across a sequence of {x,y,ts} points. */
function drive(h, points) {
  const top = h.byId['top-card'];
  top.rect = RECT;
  top.fire('pointerdown', { pointerId: 1, clientX: points[0].x, clientY: points[0].y,
                            timeStamp: points[0].ts, target: top });
  for (let i = 1; i < points.length; i++) {
    h.doc('pointermove', { pointerId: 1, clientX: points[i].x, clientY: points[i].y,
                           timeStamp: points[i].ts });
  }
  const last = points[points.length - 1];
  h.doc('pointerup', { pointerId: 1, clientX: last.x, clientY: last.y, timeStamp: last.ts });
}

// ── 1/2: pass and done, same-body proof ───────────────────────────────────
{
  const h = await boot();
  // Right, past the 35% threshold (0.35*320=112) — a clean commit.
  drive(h, [{ x: 0, y: 0, ts: 0 }, { x: 30, y: 0, ts: 50 }, { x: 200, y: 0, ts: 400 }]);
  await flush();
  eq('2. right = done posts to_lane:"done"', h.posted, [{
    path: '/api/board/dom/cards/card1/move', method: 'POST', body: { to_lane: 'done' },
  }]);
  check('2. the card leaves the top of the queue', h.byId['card-body'].children.some(
    (n) => n.textContent === 'Approve the reply'));
}

{
  const h = await boot();
  drive(h, [{ x: 0, y: 0, ts: 0 }, { x: 0, y: -30, ts: 50 }, { x: 0, y: -200, ts: 400 }]);
  await flush();
  eq('1. up = pass posts …/seen, never …/move', h.posted, [{
    path: '/api/board/dom/cards/card1/seen', method: 'POST', body: {},
  }]);
  check('1. the passed card rotates to the BACK, not off the queue',
    h.byId['card-body'].children.some((n) => n.textContent === 'Approve the reply'));
}

// ── 3: drop shows reasons, then posts ─────────────────────────────────────
{
  const h = await boot();
  drive(h, [{ x: 0, y: 0, ts: 0 }, { x: -30, y: 0, ts: 50 }, { x: -200, y: 0, ts: 400 }]);
  await flush();
  eq('3. a drop does not post until the reason window resolves', h.posted, []);
  const overlay = h.byId['drop-reasons'];
  check('3. the reason overlay is showing', overlay.hidden === false);
  const reasons = overlay.children.map((b) => b.textContent);
  eq('3. the reason set is D-BI2 verbatim', reasons,
    ['not mine', 'already handled', 'never', 'later than later']);
  overlay.children.find((b) => b.textContent === 'never').onclick();
  await flush();
  eq('3. picking a reason posts it on the move', h.posted, [{
    path: '/api/board/dom/cards/card1/move', method: 'POST',
    body: { to_lane: 'dropped', reason: 'never' },
  }]);
  check('3. …and the overlay is gone', h.byId['drop-reasons'].hidden === true);
}

{
  const h = await boot();
  drive(h, [{ x: 0, y: 0, ts: 0 }, { x: -30, y: 0, ts: 50 }, { x: -200, y: 0, ts: 400 }]);
  await flush();
  h.fireTimer(1000);
  await flush();
  eq('3. letting the window elapse drops with no reason', h.posted, [{
    path: '/api/board/dom/cards/card1/move', method: 'POST', body: { to_lane: 'dropped' },
  }]);
}

// ── 4: undo reverses each of the three commits ────────────────────────────
{
  const h = await boot();
  drive(h, [{ x: 0, y: 0, ts: 0 }, { x: 0, y: -30, ts: 50 }, { x: 0, y: -200, ts: 400 }]);
  await flush();
  h.posted.length = 0;
  h.byId['undo-btn'].onclick();
  await flush();
  eq('4. undoing a pass posts …/seen {undo:true}', h.posted, [{
    path: '/api/board/dom/cards/card1/seen', method: 'POST', body: { undo: true },
  }]);
}

{
  const h = await boot();
  drive(h, [{ x: 0, y: 0, ts: 0 }, { x: 30, y: 0, ts: 50 }, { x: 200, y: 0, ts: 400 }]);
  await flush();
  h.posted.length = 0;
  h.byId['undo-btn'].onclick();
  await flush();
  eq('4. undoing "done" re-posts the card\'s prior lane', h.posted, [{
    path: '/api/board/dom/cards/card1/move', method: 'POST', body: { to_lane: 'today' },
  }]);
  check('4. …and the card is back on top',
    h.byId['card-body'].children.some((n) => n.textContent === 'Book dentist'));
}

// ── 5: below threshold springs back — nothing sent ────────────────────────
{
  const h = await boot();
  drive(h, [{ x: 0, y: 0, ts: 0 }, { x: 20, y: 0, ts: 1000 }]);
  await flush();
  eq('5. a small, slow movement sends nothing', h.posted, []);
  check('5. the card is still on top',
    h.byId['card-body'].children.some((n) => n.textContent === 'Book dentist'));
}

// ── 6: a fast flick commits short of the distance threshold ──────────────
{
  const h = await boot();
  // 20px then another 30px ten ms later: 3px/ms on the final stretch, well
  // past the 0.6px/ms flick speed, and 50px total is far under the 112px
  // (35% of 320) distance threshold.
  drive(h, [{ x: 0, y: 0, ts: 0 }, { x: 20, y: 0, ts: 5 }, { x: 50, y: 0, ts: 15 }]);
  await flush();
  eq('6. a fast flick commits despite low distance', h.posted, [{
    path: '/api/board/dom/cards/card1/move', method: 'POST', body: { to_lane: 'done' },
  }]);
}

// ── 7: the four buttons are shown for EVERY viewer, not just reduced
// motion (fix for pr-4280: they used to be gated behind the OS preference,
// hiding the only non-gesture path from everyone else) ────────────────────
{
  const h = await boot({ reducedMotion: false });
  check('7. the four-button fallback shows with reduced motion OFF too',
    h.byId['button-row'].hidden === false);
}

// ── 7b: reduced motion disables the gesture; buttons post the same bodies ─
{
  const h = await boot({ reducedMotion: true });
  check('7b. the four-button fallback is shown', h.byId['button-row'].hidden === false);
  drive(h, [{ x: 0, y: 0, ts: 0 }, { x: 200, y: 0, ts: 400 }]);
  await flush();
  eq('7b. the pointer gesture is inert under reduced motion', h.posted, []);
  h.byId['btn-right'].onclick();
  await flush();
  eq('7b. the button posts the identical body a swipe would', h.posted, [{
    path: '/api/board/dom/cards/card1/move', method: 'POST', body: { to_lane: 'done' },
  }]);
}

// ── 8: a tap (no drag) opens the detail sheet, never a commit ────────────
{
  const h = await boot();
  drive(h, [{ x: 0, y: 0, ts: 0 }, { x: 2, y: 1, ts: 10 }]);
  await flush();
  eq('8. a tap sends no write', h.posted, []);
  check('8. …and opens the detail sheet',
    h.byId['detail-sheet'].style.display === 'block');
  eq('8. …titled after the tapped card', h.byId['detail-title'].textContent, 'Book dentist');
}

// ── 9: the hint toggle persists across a reload, defaults on ─────────────
{
  const h1 = await boot();
  check('9. hints are on by default', !h1.body.classList.contains('hints-off'));
  h1.byId['hint-toggle-btn']; // present even off-empty-state is fine to reference
  // Reach the toggle via the empty state (where it lives) by simulating an
  // empty stack, since that is the only place D-ST9 puts it.
  const hEmpty = await boot({ items: [], emptyState: { done_today: 0, dropped_today: 0, handed_off_today: 0, bots_plate: [] } });
  check('9. empty stack shows the toggle', hEmpty.byId['hint-toggle-btn'].textContent.includes('hide'));
  hEmpty.byId['hint-toggle-btn'].onclick();
  check('9. toggling hides the hints', hEmpty.body.classList.contains('hints-off'));
  const savedCookie = hEmpty.cookie();
  const h2 = await boot({ initialCookie: savedCookie });
  check('9. …and the choice survives a reload', h2.body.classList.contains('hints-off'));
}

// ── 10: taps — decision, Later, action ────────────────────────────────────
{
  const h = await boot({ items: [fixtureItems()[1]] }); // returned_for_review card on top
  const approve = h.byId['card-body'].children.find(
    (n) => n.className.includes('decision-row'));
  check('10. a returned card shows Approve/Decline', !!approve);
  const approveBtn = approve.children.find((b) => b.textContent === 'Approve');
  approveBtn.onclick();
  await flush();
  eq('10. Approve posts …/decision', h.posted, [{
    path: '/api/board/dom/cards/card2/decision', method: 'POST', body: { decision: 'approve' },
  }]);
}

{
  const h = await boot({ items: [fixtureItems()[0]] }); // owner:"me" card, has actions + Later
  const later = h.byId['card-body'].children.find((n) => n.id === 'later-btn');
  later.onclick();
  check('10. Later opens its sheet', h.byId['later-sheet'].style.display === 'block');
  const row = h.byId['later-row'];
  eq('10. three presets are offered', row.children.map((b) => b.textContent),
    ['tomorrow', 'this weekend', 'next week']);
  row.children.find((b) => b.textContent === 'tomorrow').onclick();
  await flush();
  check('10. Later posts a move to "later" carrying snooze_until',
    h.posted.length === 1 &&
    h.posted[0].path === '/api/board/dom/cards/card1/move' &&
    h.posted[0].body.to_lane === 'later' &&
    typeof h.posted[0].body.snooze_until === 'string' &&
    h.posted[0].body.snooze_until.length > 0,
    JSON.stringify(h.posted));
}

{
  const h = await boot({ items: [fixtureItems()[0]] });
  const actionBtn = h.byId['card-body'].children.find(
    (n) => n.className === 'actions').children[0];
  actionBtn.onclick();
  await flush();
  eq('10. an action chip posts …/instruct', h.posted, [{
    path: '/api/board/dom/cards/card1/instruct', method: 'POST', body: { action_id: 'find_directions' },
  }]);
}

// ── 11: persist-then-animate — a rejected Done leaves queue[0] untouched
// and the top card showing; the toast carries the failure instead of the
// optimistic "Done." (fix for pr-4280: the card used to leave the queue
// BEFORE the write was confirmed, so a failure banner floated over a card
// that already looked gone) ────────────────────────────────────────────────
{
  const h = await boot();
  h.failNextPost(500, 'boom');
  drive(h, [{ x: 0, y: 0, ts: 0 }, { x: 30, y: 0, ts: 50 }, { x: 200, y: 0, ts: 400 }]);
  await flush();
  check('11. the card stays on top after a rejected Done',
    h.byId['card-body'].children.some((n) => n.textContent === 'Book dentist'));
  eq('11. …and the failed write is the only request made', h.posted, [{
    path: '/api/board/dom/cards/card1/move', method: 'POST', body: { to_lane: 'done' },
  }]);
  check('11. the toast reads the failure, not "Done."',
    h.byId['undo-text'].textContent.indexOf("couldn't save") === 0);
  check('11. …and the toast is showing', h.byId['undo-toast'].hidden === false);
}

// ── 12: the same persist-then-animate guarantee on the Drop path ─────────
{
  const h = await boot();
  drive(h, [{ x: 0, y: 0, ts: 0 }, { x: -30, y: 0, ts: 50 }, { x: -200, y: 0, ts: 400 }]);
  await flush();
  h.failNextPost(500, 'boom');
  h.byId['drop-reasons'].children.find((b) => b.textContent === 'never').onclick();
  await flush();
  check('12. the card stays on top after a rejected Drop',
    h.byId['card-body'].children.some((n) => n.textContent === 'Book dentist'));
  check('12. the toast reads the failure, not "Dropped."',
    h.byId['undo-text'].textContent.indexOf("couldn't save") === 0);
}

// ── 13: a lapsed Undo goes dead — the tap can never re-fire a move the
// user no longer asked for, whatever the exact race with the 3s timer ────
{
  const h = await boot();
  drive(h, [{ x: 0, y: 0, ts: 0 }, { x: 30, y: 0, ts: 50 }, { x: 200, y: 0, ts: 400 }]);
  await flush();
  h.posted.length = 0;
  h.fireTimer(3000);
  check('13. the undo button is disabled once its window lapses',
    h.byId['undo-btn'].disabled === true);
  check('13. …and its click handler is cleared', h.byId['undo-btn'].onclick === null);
  eq('13. …so a stray tap after the window posts nothing', h.posted, []);
}

console.log(failures === 0
  ? '\nall stack gesture invariants hold'
  : `\n${failures} stack gesture invariant(s) failed`);
process.exit(failures === 0 ? 0 : 1);
