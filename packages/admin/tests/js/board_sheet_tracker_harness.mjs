// Board detail-sheet Tracker rendering harness — D-TM2/5/8, proven against
// the real board.html.
//
// The board page is one plain-JS file with no framework and no build step
// (D-MB3), so there is no module to import and no JS unit runner in this
// package — same answer as board_drag_harness.mjs: evaluate the REAL page
// script in a mock browser scope and assert the invariants from here. Run
// directly (`node board_sheet_tracker_harness.mjs`) or via
// tests/test_board_sheet_tracker.py.
//
// What it proves:
//   1. The detail sheet renders outcome, an "other" owner's wait, the next
//      touch (time + action label), due, and the goal link — the fields
//      the build brief names by name.
//   2. The last three touches render, most recent first, oldest dropped.
//   3. A card with none of these fields renders no Tracking/touches group
//      at all (additive: an old card's sheet is unchanged).
//   4. LOCALE. The sheet's date text comes from the injected `Date`'s own
//      toLocaleString/toLocaleDateString — proven by booting the SAME
//      fixture card under two different `Date` subclasses (stand-ins for
//      two browser locales) and checking the rendered text changes to
//      match each one, never a hardcoded format.

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

const html = readFileSync(PAGE, 'utf8');
const script = html.match(/<script>\n([\s\S]*?)\n<\/script>/);
if (!script) { console.log('FAIL could not find the page script'); process.exit(1); }

const STATIC_IDS = [...html.matchAll(/\sid="([a-z0-9-]+)"/g)].map((m) => m[1]);

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
    getBoundingClientRect() {
      return node.rect || { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
    },
    cloneNode() {
      const copy = makeNode(node.tagName);
      copy.className = node.className; copy.textContent = node.textContent;
      copy.attrs = { ...node.attrs }; copy.id = node.id;
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

// Every text node under `node`, flattened and space-joined — how the
// harness reads what a viewer would see, since the mock DOM has no
// innerText/textContent aggregation of its own.
function visibleText(node) {
  return walk(node).map((n) => n.textContent).filter(Boolean).join(' ');
}

function makeScope({ dateClass } = {}) {
  const byId = {};
  for (const id of STATIC_IDS) { byId[id] = makeNode('div'); byId[id].setAttribute('id', id); }
  const body = makeNode('body');
  const head = makeNode('head');
  const timers = [];
  const docListeners = {};
  const posted = [];
  let fetchBoard = { cards: [] };

  const document = {
    body, head,
    createElement: (tag) => makeNode(tag),
    getElementById: (id) => {
      if (byId[id]) return byId[id];
      for (const n of walk(byId['lanes'] || makeNode('div'))) if (n.id === id) return n;
      return null;
    },
    querySelectorAll: () => [],
    addEventListener: (type, fn) => { (docListeners[type] ||= []).push(fn); },
    hidden: false,
  };

  const scope = {
    document, console, navigator: {},
    location: { pathname: '/board/dom', search: '' },
    history: { replaceState() {} },
    URLSearchParams,
    JSON, Object, Array, Math, String, Number, Error, Promise, RegExp,
    Date: dateClass || Date,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (h) => { if (timers[h - 1]) timers[h - 1].cancelled = true; },
    setInterval: () => 0,
    requestAnimationFrame: () => 1,
    cancelAnimationFrame: () => {},
    matchMedia: (q) => ({ matches: false, media: q }),
    innerHeight: 800,
    scrollBy: () => {},
    fetch: (path, opts) => {
      opts = opts || {};
      if ((opts.method || 'GET') === 'GET') {
        return Promise.resolve({
          ok: true, status: 200, headers: { get: () => '"etag"' },
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
    scope, byId, body, posted,
    setBoard(b) { fetchBoard = b; },
    // The page keeps `openMoveSheet` private inside its own IIFE (same as
    // every other page function) — a tile's own onclick is what the drag
    // harness already uses to reach it, so this does the same instead of
    // reaching for a function that was never exported.
    tile(id) {
      return walk(byId['lanes']).find((n) => n.attrs['data-card-id'] === id);
    },
    openSheet(id) { this.tile(id).onclick(); },
  };
}

async function boot(opts = {}) {
  const h = makeScope(opts);
  h.setBoard(opts.board || { cards: [] });
  vm.createContext(h.scope);
  vm.runInContext(script[1], h.scope);
  await new Promise((r) => setImmediate(r));
  return h;
}

// A fixture card carrying every D-TM2/5/8 field the sheet is asked to
// render: outcome, an "other" wait, a resolved pace, a due date, three
// touches (only the last three of which should show, newest first), and a
// goal link resolved against a second card in the same board.
const GOAL_CARD = { id: 'goal0000', title: 'Plan the Vegas trip', cluster: 'travel', lane: 'later' };
const FIXTURE = {
  id: 'card0001', human_id: 'OP-0042', title: 'Chase the venue deposit',
  cluster: 'admin', lane: 'today', owner: 'other',
  waiting_on: { who: 'Alex at the venue', since: '2026-09-20T00:00:00Z', source: 'email' },
  outcome: 'deposit paid and confirmed',
  next_touch: '2026-09-25T18:00:00Z', touch_action: 'remind',
  due: '2026-09-30',
  belong_to: 'goal0000',
  touches: [
    { at: '2026-09-01T09:00:00Z', action: 'remind', result: 'no_change', actor: 'bot' },
    { at: '2026-09-10T09:00:00Z', action: 'remind', result: 'no_change', actor: 'bot' },
    { at: '2026-09-18T09:00:00Z', action: 'check_source', result: 'still_waiting', actor: 'bot' },
    { at: '2026-09-24T09:00:00Z', action: 'remind', result: '', actor: 'bot' },
  ],
};

const PLAIN_CARD = { id: 'plain0001', title: 'Ordinary card', cluster: 'admin', lane: 'inbox', owner: 'me' };

// ── 1-3: field presence, given a real Date (no locale claim either way) ──
{
  const h = await boot({ board: { cards: [FIXTURE, GOAL_CARD, PLAIN_CARD] } });
  h.openSheet(FIXTURE.id);
  const sheetText = visibleText(h.byId['detail-body']);

  check('1. outcome renders', sheetText.includes('deposit paid and confirmed'));
  check('1. an "other" owner shows who it is waiting on', sheetText.includes('Alex at the venue'));
  check('1. next touch shows the action label', sheetText.includes('remind'));
  check('1. the goal link resolves to the GOAL\'s title, not its bare id',
    sheetText.includes('Plan the Vegas trip') && !sheetText.includes('goal0000'));
  check('1. the human id appears in the sheet title', h.byId['move-title'].textContent.includes('OP-0042'));

  // touches[0] ("no_change") is the ONE dropped by slice(-3) — if it were
  // still rendered, "no_change" would match twice (touches[0] and [1])
  // instead of once, alongside the one "still_waiting" from touches[2].
  check('2. only the last three touches render (the oldest, index 0, drops)',
    (sheetText.match(/no_change/g) || []).length === 1 &&
    (sheetText.match(/still_waiting/g) || []).length === 1,
    sheetText);
  const touchGroup = walk(h.byId['detail-body']).find(
    (n) => n.children.some((c) => c.textContent === 'Recent touches'));
  const touchRows = touchGroup ? touchGroup.children.slice(1).map((c) => c.textContent) : [];
  // Newest (2026-09-24, blank result) first, then -18 (still_waiting), then
  // -10 (no_change) — the reverse of touches[]' append order.
  check('2. the most recent touch renders first',
    touchRows.length === 3 &&
    !/no_change|still_waiting/.test(touchRows[0]) &&
    touchRows[1].includes('still_waiting') &&
    touchRows[2].includes('no_change'),
    touchRows);

  h.openSheet(PLAIN_CARD.id);
  const plainText = visibleText(h.byId['detail-body']);
  check('3. a card with no Tracker fields shows no Tracking/touches group',
    !plainText.includes('Tracking') && !plainText.includes('Recent touches'), plainText);
}

// ── 4: locale — the SAME fixture, two different Date subclasses ─────────
{
  function makeFakeDate(tag) {
    return class FakeDate extends Date {
      toLocaleString() { return `LOCALE[${tag}]:` + this.toISOString(); }
      toLocaleDateString() { return `LOCALE[${tag}]:` + this.toISOString().slice(0, 10); }
    };
  }
  const en = await boot({ dateClass: makeFakeDate('en'), board: { cards: [FIXTURE, GOAL_CARD] } });
  en.openSheet(FIXTURE.id);
  const enText = visibleText(en.byId['detail-body']);

  const de = await boot({ dateClass: makeFakeDate('de'), board: { cards: [FIXTURE, GOAL_CARD] } });
  de.openSheet(FIXTURE.id);
  const deText = visibleText(de.byId['detail-body']);

  check('4. locale A\'s next-touch/due text carries its own tag', enText.includes('LOCALE[en]'), enText);
  check('4. locale B\'s next-touch/due text carries its own tag', deText.includes('LOCALE[de]'), deText);
  check('4. the two locales render DIFFERENT text for the same card',
    enText !== deText && !enText.includes('LOCALE[de]') && !deText.includes('LOCALE[en]'));
}

console.log(failures === 0
  ? '\nall board sheet tracker invariants hold'
  : `\n${failures} board sheet tracker invariant(s) failed`);
process.exit(failures === 0 ? 0 : 1);
