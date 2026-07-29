# Browser-compat audit — Evolve admin UI

Phase 0 §4.4 of `spec-pwa-2026-05-18.md`.

This is an **inventory**, not a fix list. The point is to know what's
likely to bite when the admin UI starts being driven from Safari (macOS),
Safari (iOS), Firefox, and touchscreens — and to feed §4.1 (HTTPS+CSP)
and §4.3 (responsive design) with a concrete list of things to consider.

Nothing in here gets fixed by the PR introducing this file. The hover
items in particular are intentional debt: fixing them is the §4.3
responsive design pass, not this baseline.

**Scope of audit.** All admin-UI frontend lives in a single file —
`packages/admin/evolve_admin/web/index.html` (~34k lines, inline CSS +
inline JS). There is no other CSS or JS bundle for the admin web. The
audit grepped that one file. SPA blueprint route templates (e.g.
`server.py:505` injects a `<script>` for the investigation deep-link)
are noted where they affect what loads in the browser.

---

## 1. Headline findings (top-five for §4.3 / §4.1)

1. **External CDN: `https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js`** loaded at `index.html:12`. Will break under a strict CSP and is a hard offline failure for the service worker's offline-shell story. Phase 0.1 should either move Chart.js to a bundled copy under `/static/` or add `script-src https://cdn.jsdelivr.net` to the CSP (preferring the former; offline-shell can only cache same-origin trivially).
2. **One hover-reveal affordance**: `.help-btn:hover .tip { display: block; }` at `index.html:627–628`. Touch users can never see the help-tip. There are **39 `.help-btn` instances** in the page, so this is the dominant unreachable affordance on touchscreens.
3. **253 `title="…"` attributes** on buttons/icons. The browser's native title tooltip only fires on hover. Touch users don't get the explanatory text. Not breaking anything, but every one of those is invisible on phone/tablet — worth a tap-to-show pattern when the tooltip is load-bearing.
4. **Six `100vh` usages**, including the main app layout (`index.html:35–36`). iOS Safari includes the URL-bar height in `100vh`, so content gets clipped behind the bottom bar when the bar is visible. Phase 0.1 / §4.3 should switch to `100dvh` (dynamic viewport) with a `100vh` fallback for older browsers (`min-height: 100vh; min-height: 100dvh;`).
5. **No `@supports` queries anywhere**. The CSS assumes evergreen browsers — fine in practice today, but means there is no graceful path for any feature that ever needs a fallback. Worth keeping in mind once we start writing PWA-specific CSS (maskable-icon padding, install-prompt theming).

---

## 2. CSS

### Modern CSS in use (low-risk on evergreen browsers)

- `flex` / `grid` layouts everywhere — universally supported.
- CSS custom properties (`var(--bg)`, etc.) — universally supported.
- `@media (max-width: …)` breakpoints at 720px, 980px, 900px, 600px, 1280px — supported.
- `@media (prefers-reduced-motion: reduce)` at `index.html:1289` — supported.
- ~129 `gap:` / `position: sticky` / etc. uses — supported across Chrome/Safari/Firefox shipped releases.

### Modern CSS NOT in use (audited and absent — good)

- `:has(…)` selector — none found. (Firefox shipped this default in 2023; this is no longer a real risk but worth confirming as we write new code.)
- `aspect-ratio` — none found.
- `backdrop-filter` — none found. (Firefox shipped default in 103; Safari shipped with `-webkit-` prefix.)
- `subgrid` — none found.
- `@container` queries — none found.
- `color-mix(…)`, `color(display-p3 …)` — none found.
- `dvh` / `svh` / `lvh` viewport units — **none found**, but see headline #4: `100vh` is in use and would benefit from `dvh`.
- Vendor prefixes (`-webkit-`, `-moz-`, `-ms-`) — none found outside the font-family `-apple-system, BlinkMacSystemFont` value at `index.html:35`, which is the standard system-font cascade and is fine.

### Hover-only reveals (touch-unreachable)

| Selector | File:line | What's revealed | Severity |
|---|---|---|---|
| `.help-btn:hover .tip` | `index.html:627-628` | The help "?" tooltip text (39 instances on the page) | Medium — content is genuinely informative on cards like skills/recommendations |
| `[title="…"]` (native browser tooltip) | 253 instances across `index.html` | Button/icon labels | Low to medium — many duplicate visible text; some (e.g. theme toggle ◑) are the only label |

That's the lot. No other `:hover` rule changes visibility/display — the
remaining ~50 `:hover` rules only adjust colour, border, opacity (0.82 →
0.88 etc.) and are decorative.

**Recommendation for §4.3:** convert `.help-btn` to a tap-to-toggle
pattern (click toggles `.tip` visibility; document-level click outside
closes). The 253 `title="…"` attributes are a follow-on cleanup —
visible text labels exist on most buttons already, but a sweep should
identify which ones are *only* labelled via `title=`.

### Viewport meta

```html
<meta name="viewport" content="width=device-width, initial-scale=1.0">
```

(`index.html:5`)

Good — no `user-scalable=no` or `maximum-scale`, so pinch-zoom works on
mobile. Don't regress this.

---

## 3. JavaScript

### Modern APIs in use

- `fetch(…)` — universal.
- `navigator.clipboard.writeText(…)` — 8 calls. Requires **secure context** (HTTPS or localhost), so works in dev (localhost), works on the mini once Phase 0.1 ships HTTPS, but currently fails silently on Safari over plain HTTP from a phone over Tailscale (until Phase 0.1).
  - Fallback already present: `document.execCommand('copy')` at `index.html:20480`. This is deprecated and removal-scheduled by browser vendors, but still works in Chromium/Safari/Firefox today. Worth flagging as something to revisit when Phase 0.1 makes clipboard unconditionally available.
- `localStorage.setItem / getItem` — supported everywhere; Safari has stricter eviction in private-browsing mode but the admin UI isn't expected to run in private mode.
- `window.open(url, '_blank', 'noopener')` — universal; popup blockers may interfere on Safari when the open isn't directly user-triggered (one case at `index.html:18720` for OAuth — already user-triggered so fine).

### Modern APIs NOT in use (audited and absent)

- `structuredClone` — none.
- `Array.prototype.at(-1)` — none.
- `Object.hasOwn(…)` — none.
- `globalThis` — none.
- `crypto.randomUUID()` — none.
- `crypto.subtle` — none.
- `navigator.share` — none (Web Share API; iOS Safari only supports limited subset).
- `ResizeObserver` / `IntersectionObserver` — none.
- `requestIdleCallback` — none.
- `Array.prototype.toReversed` / `toSorted` (ES2023) — none.
- Top-level `await` — none (admin UI has no ESM modules; everything is a single inline `<script>` block).
- Dynamic `import(…)` — none.
- Web Workers — none.
- `EventSource` (SSE) / `WebSocket` — none.

### Mouse-only event handlers (touch-unfriendly)

Audited for `onmouseenter` / `onmouseleave` / `onmouseover` / `onmouseout` /
`onmousedown` / `onmouseup` / `onmousemove` and `addEventListener('mouse…')`
/ `addEventListener('pointer…')`. **Zero matches.**

That means there's no JS that fires only on mouse — all interaction is
`onclick=` (touch-equivalent) or hover-reveal CSS (covered above). Good
news for tablet/phone interaction baseline.

### XSS surface (informational, not cross-browser per se)

`index.html` contains **687 `innerHTML` / `innerText` writes**. Most are
template fragments with `escHtml(…)` wrappers, but the count is large
enough that any Phase 0.1 CSP work should plan around `'unsafe-inline'`
remaining in `script-src` for the inline `<script>` blocks; tightening
that is its own project, not a §4.4 deliverable.

---

## 4. Inline scripts and CSP impact (for §4.1)

- The admin UI is a **single HTML file with inline `<style>` and inline
  `<script>`** (no bundler, no external app JS, no ESM). A strict CSP
  (`script-src 'self'`) would block it outright.
- One external script tag at `index.html:12` (`cdn.jsdelivr.net/.../chart.umd.min.js`).
- One server-injected inline `<script>` at `packages/admin/evolve_admin/web/server.py:513` for the investigation deep-link landing — uses `markupsafe.escape` on the input, so XSS-safe, but inline-script-policy bound.
- One inline `onerror=` HTML attribute at `index.html:11780` (image fallback) — also inline-script-policy bound.

Implication for Phase 0.1: the realistic CSP is `script-src 'self'
'unsafe-inline' https://cdn.jsdelivr.net;` (or move Chart.js
self-hosted). Tightening past that requires extracting all inline JS,
which is a separate project.

---

## 5. iOS Safari–specific snags

- **`100vh` clips behind the URL bar.** Six usages including the main
  layout at `index.html:35–36`. See headline #4.
- **`overscroll-behavior` not used.** None found. Bounce-scroll at page edges is iOS default; acceptable.
- **`-webkit-overflow-scrolling: touch` not used.** Was needed pre-iOS 13; no longer required.
- **PWA install hint absent.** The page has no `apple-mobile-web-app-capable` or `apple-mobile-web-app-status-bar-style` meta tags. That's expected — Phase 1 §5.1 adds them. Not a current bug; flagging so we don't think it's "missing" before Phase 1.

---

## 6. Firefox-specific snags

- **`:has(…)` parity** — not relevant, not used.
- **`backdrop-filter` parity** — not relevant, not used.
- No `-moz-` prefixed properties were ever needed in the audit and none are present.
- Firefox doesn't honour `beforeinstallprompt` — Phase 1's install UX needs to feature-detect rather than assume the event will fire. Not a current bug; flagging for Phase 1.

---

## 7. Chart.js dependency

- Version pinned to `4.4.0` at `index.html:12` via CDN.
- Chart.js 4.x supports Chrome 70+, Safari 13+, Firefox 68+ — well below any browser likely to hit the admin UI. No compatibility concern.
- The CDN dependency itself is the only real risk (see headline #1).

---

## 8. Items intentionally not in scope

- Fixing any of the above — that is §4.3 (responsive) and §4.1 (HTTPS+CSP) work, not §4.4 (baseline).
- Lighthouse / accessibility audit — separate concern; the spec defers to "the Plex test" as the design constraint, not a numeric audit.
- Network-resilience / offline behaviour — Phase 1 service worker.
- Performance budgets / TTI measurements — defer to Phase 1 once the install path is wired.

---

## 9. How to re-run this audit

Grep recipes for next time (paths from repo root):

```bash
# Modern CSS hotspots
grep -nE ":has\(|aspect-ratio|backdrop-filter|subgrid|@container|color-mix|\bdvh\b|\bsvh\b" \
    packages/admin/evolve_admin/web/index.html

# Vendor prefixes
grep -nE "-webkit-|-moz-|-ms-" packages/admin/evolve_admin/web/index.html \
    | grep -v "BlinkMacSystemFont\|apple-system"

# Hover-reveal CSS
grep -nB1 -A1 ":hover" packages/admin/evolve_admin/web/index.html \
    | grep -E "display:|visibility:|opacity:\s*[01]"

# Mouse-only handlers
grep -nE "onmouse|addEventListener\(['\"](mouse|pointer)" \
    packages/admin/evolve_admin/web/index.html

# Modern JS APIs
grep -nE "structuredClone|globalThis|Array\.prototype\.at|Object\.hasOwn|crypto\.randomUUID|navigator\.share|ResizeObserver|IntersectionObserver|requestIdleCallback" \
    packages/admin/evolve_admin/web/index.html

# External scripts (CSP surface)
grep -nE "<script src=|<link.*href=\"https?:" packages/admin/evolve_admin/web/index.html
```
