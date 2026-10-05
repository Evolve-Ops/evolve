// board_text.js harness — the Board/Stack shared renderer for `when` and
// bot-written text (brief: board-sheet-renders-time-text-and-price-is-warmed).
//
// Runs the REAL board_text.js in a vm scope whose Intl is pinned to a fixed
// locale AND a fixed time zone — a locale alone does not decide the rendered
// hour, and the phone travels while the pod does not. Run directly or via
// tests/test_board_text.py.
//
// What it proves (D-CS7 — every control a known-good AND a known-bad case):
//   1. `when` renders in the viewer's zone and locale (two locales x two zones).
//   2. Same-day range / different days / bare date / no-offset / foreign-offset.
//   3. Unparseable or end-before-start → "time unknown", raw on hover, no throw.
//   4. Bot text: **bold** → <strong>, list items one per line with the marker
//      kept, "<script>" stays text, only http(s) becomes an anchor and its
//      text IS its href; javascript: stays literal.
//   5. No innerHTML anywhere in board_text.js, board.html or stack.html.

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB = resolve(HERE, '../../evolve_admin/web');
const SRC = readFileSync(resolve(WEB, 'board_text.js'), 'utf8');

let failures = 0;
function check(name, cond, detail) {
  if (cond) console.log(`ok   ${name}`);
  else { failures++; console.log(`FAIL ${name}${detail ? ` — ${detail}` : ''}`); }
}

function node(tag) {
  const n = { tagName: tag.toUpperCase(), children: [], textContent: '', className: '', attrs: {},
    appendChild(c) { n.children.push(c); return c; } };
  return n;
}
const doc = { createElement: node };
function text(n) { return n.tagName === 'BR' ? '\n' : (n.textContent || '') + n.children.map(text).join(''); }
function all(n, out = []) { out.push(n); n.children.forEach((c) => all(c, out)); return out; }

function load(locale, timeZone) {
  const RealDTF = Intl.DateTimeFormat;
  const PinnedIntl = { DateTimeFormat: function (loc, opts) {
    return new RealDTF(loc || locale, Object.assign({ timeZone }, opts || {}));
  } };
  const scope = { Intl: PinnedIntl, JSON, String, Date };
  scope.window = scope;
  vm.createContext(scope);
  vm.runInContext(SRC, scope);
  return scope.BoardText;
}

const FIXTURE = { start: '2026-09-20T14:00:00Z', end: '2026-09-20T14:30:00Z' };

// ── 1. two locales x two zones — the PR body's table ──
const table = [
  ['en-US', 'America/Los_Angeles', 'Sun, Sep 20, 7:00 – 7:30 AM'],
  ['en-US', 'Europe/Berlin', 'Sun, Sep 20, 4:00 – 4:30 PM'],
  // de-DE's range suffix ("Uhr") varies by ICU build; the hour and the
  // weekday are the claim, so those are what is pinned.
  ['de-DE', 'America/Los_Angeles', /^So\., 20\. Sept\., 07:00\s?–\s?07:30/],
  ['de-DE', 'Europe/Berlin', /^So\., 20\. Sept\., 16:00\s?–\s?16:30/],
];
for (const [loc, tz, want] of table) {
  const got = load(loc, tz).formatWhen(FIXTURE);
  console.log(`     ${loc} ${tz}: ${got.text}`);
  const norm = got.text.replace(/\s/g, ' ');
  check(`fixture when in ${loc} / ${tz}`, got.known &&
    (want instanceof RegExp ? want.test(norm) : norm === want.replace(/\s/g, ' ')), got.text);
}

// ── 2. shapes ──
{
  const B = load('en-US', 'America/Los_Angeles');
  const t = (v) => B.formatWhen(v).text;
  check('bare ISO string start only', t('2026-09-20T14:00:00Z') === 'Sun, Sep 20, 7:00 AM', t('2026-09-20T14:00:00Z'));
  check('different days show both dates',
    t({ start: '2026-09-20T14:00:00Z', end: '2026-09-22T01:00:00Z' }) === 'Sun, Sep 20, 7:00 AM – Mon, Sep 21, 6:00 PM',
    t({ start: '2026-09-20T14:00:00Z', end: '2026-09-22T01:00:00Z' }));
  check('bare date is the date only, never shifted', t('2026-09-20') === 'Sun, Sep 20', t('2026-09-20'));
  check('no offset says so', t('2026-09-20T09:00:00') === 'Sun, Sep 20, 9:00 AM (time zone not stated)', t('2026-09-20T09:00:00'));
  const ny = t({ start: '2026-09-20T10:00:00-04:00', end: '2026-09-20T10:30:00-04:00' });
  check('a when written in another zone says it is converted', ny.replace(/\s/g, ' ') === 'Sun, Sep 20, 7:00 – 7:30 AM your time (PDT)', ny);
  const own = t('2026-09-20T07:00:00-07:00');
  check('a when already in the viewer zone carries no label', own === 'Sun, Sep 20, 7:00 AM', own);
}

// ── 3. known-bad: unknown, never raw, never a throw ──
{
  const B = load('en-US', 'America/Los_Angeles');
  for (const bad of ['next Tuesday-ish', '2026-02-31', { start: 'soon' }, { start: '2026-09-20T14:00:00Z', end: 'later' },
                     { start: '2026-09-20T14:30:00Z', end: '2026-09-20T14:00:00Z' }, null, 42]) {
    let got;
    try { got = B.formatWhen(bad); } catch (e) { got = { text: `THREW ${e}` }; }
    check(`unknown: ${JSON.stringify(bad)}`, got.text === 'time unknown' && got.known === false, got.text);
  }
  const span = B.whenNode(doc, { start: '2026-09-20T14:30:00Z', end: '2026-09-20T14:00:00Z' });
  check('unknown keeps the raw value on hover', span.textContent === 'time unknown' && span.title.includes('14:30'), span.title);
}

// ── 4. bot text ──
{
  const B = load('en-US', 'UTC');
  const box = B.renderBotText(doc, '**Recommended:** option B\n1. call first\n2. then book\n- bring ID', 'note');
  const strong = all(box).filter((n) => n.tagName === 'STRONG');
  check('**bold** becomes a <strong> built by the DOM API', strong.length === 1 && text(strong[0]) === 'Recommended:');
  check('list items one per line, markers kept',
    text(box) === 'Recommended: option B\n1. call first\n2. then book\n- bring ID', JSON.stringify(text(box)));
  const xss = B.renderBotText(doc, '<script>alert(1)</script> <b>x</b>');
  check('<script> renders as text', text(xss) === '<script>alert(1)</script> <b>x</b>' &&
    all(xss).every((n) => ['DIV', 'SPAN'].includes(n.tagName)));
  const js = B.renderBotText(doc, 'click javascript:alert(1) or [here](javascript:alert(1)) or data:text/html,x');
  check('javascript:/data: render as text, no anchor', all(js).every((n) => n.tagName !== 'A') &&
    text(js) === 'click javascript:alert(1) or [here](javascript:alert(1)) or data:text/html,x');
  const ok = B.renderBotText(doc, 'see [the menu](https://example.com/menu?x=1).');
  const anchors = all(ok).filter((n) => n.tagName === 'A');
  check('https becomes an anchor whose text equals its href', anchors.length === 1 &&
    anchors[0].href === 'https://example.com/menu?x=1' && anchors[0].textContent === anchors[0].href &&
    anchors[0].rel === 'noopener noreferrer' && anchors[0].target === '_blank', JSON.stringify(anchors[0]));
  check('the bot-supplied label is left as plain text', text(ok) === 'see [the menu](https://example.com/menu?x=1).');
}

// ── 5. no innerHTML with bot text anywhere ──
for (const f of ['board_text.js', 'board.html', 'stack.html']) {
  check(`no innerHTML in ${f}`, !readFileSync(resolve(WEB, f), 'utf8').includes('innerHTML'));
}
for (const f of ['board.html', 'stack.html']) {
  const src = readFileSync(resolve(WEB, f), 'utf8');
  check(`${f} loads the shared renderer before its own script`,
    src.indexOf('<script src="/board/board-text.js"></script>') < src.indexOf('<script>\n'));
  check(`${f} renders when through BoardText only`, src.includes('BoardText.formatWhen') || src.includes('BoardText.whenNode'));
}

if (failures) { console.log(`\n${failures} failure(s)`); process.exit(1); }
console.log('\nall board_text checks passed');
