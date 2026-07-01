# Cross-browser test matrix — Evolve admin UI

Phase 0 §4.4 of [spec-pwa-2026-05-18.md](../spec-pwa-2026-05-18.md). Establishes
the baseline for what gets checked on which browser before the PWA work in
Phase 1+ begins.

The matrix has two halves. **Automation** is what `tests/browser/` runs in
CI — fast, headless, and limited to the surfaces a real browser can reach
on a CI runner. **Manual** is what an operator (or whoever is verifying a
PWA-touching PR) actually clicks through on their own devices — install
prompts, share sheets, touch behaviour, and the iOS/iPadOS/Android trio
that has no automation path.

A passing automation run does **not** count as cross-browser verified.
For PRs that touch the admin UI shell, manifest, service worker, install
UX, or any responsive behaviour, the manual checklist below is required
in addition.

---

## 1. Browser × surface matrix

| Browser | Surface | Automated | Manual |
|---|---|---|---|
| Chrome / Chromium (desktop) | Primary install target on Mac, Windows, Linux | yes (Chromium engine via Playwright) | install prompt, drag-and-drop into chat, paste-from-clipboard |
| Safari (macOS) | Mac install fallback | yes (WebKit engine via Playwright) | install prompt (Add to Dock — different UX from Chrome), keyboard shortcut sanity, font rendering |
| Firefox (desktop) | Compatibility, no PWA install | yes (Firefox engine via Playwright) | renders + functions; install affordances correctly absent (no broken "Install" button) |
| Safari (iOS / iPadOS) | iPhone + iPad install target | **no** — real device only | Add to Home Screen via Share sheet, app icon, standalone window, push (post-install, post-permission) |
| Chrome (Android) | Android install — best mobile experience | **no** — real device only | install banner, share sheet, push, tablet layout |
| Edge (Windows) | Chromium parity | optional — covered by Chromium engine | only run if a Windows-specific report comes in |

**Playwright WebKit ≠ Safari iOS.** WebKit on a Mac/Linux CI runner uses
the same engine but not the same OS-level chrome — install prompts, share
sheets, and push permission UI all differ on iOS itself. Use WebKit
automation to catch rendering and JS-engine regressions; rely on the
manual iOS pass for anything user-facing in the install/notify path.

---

## 2. Local dev — running the server for testing

The admin server is a Flask app. To exercise it for browser testing:

```bash
# from repo root, using a throwaway network.json (the one in
# tests/browser/fixtures/ works for smoke purposes)
pip install -e packages/admin
python -m evolve_admin.web.run \
    --host 127.0.0.1 \
    --port 5050 \
    --network tests/browser/fixtures/network.json
```

Then point your browser at `http://127.0.0.1:5050/`.

The server binds to 127.0.0.1 only. To reach it from a phone on the same
Tailnet, run it on the mini and visit the mini's tailnet hostname instead
(this is what Phase 0.1 makes HTTPS).

For the automated suite see `tests/browser/README.md`.

---

## 3. Manual per-page checklist

Run through this on each browser × surface row marked **Manual** above
when the change being verified touches the admin UI shell, page
templates, or anything the operator interacts with.

Each box is "does the page do what it claims with no console errors". A
deeper test plan lives per-feature; this is the cross-browser cover.

### Home (`#page-home`) — landing/chat surface

- [ ] Page renders without JS console errors (open dev tools first).
- [ ] Pod-state ribbon (top tiles) populates within ~5s.
- [ ] Chat input accepts text and sends.
- [ ] On phone: chat input is reachable without horizontal scroll; send button is tappable (≥44px target).
- [ ] On Safari macOS: copy-to-clipboard buttons (e.g. evo response copy) work — Safari is the historical hotspot for clipboard permission UX.

### Dashboard (`#page-overview`)

- [ ] Cards render with real data (sys-loop, bot status, recent activity).
- [ ] Click-through links navigate to the destination page and highlight the right nav item.
- [ ] On phone: cards stack rather than overflow horizontally.

### Plugins (`#page-integrations-keys`)

- [ ] Plugin grid renders, badges legible.
- [ ] "?" help tooltips (`.help-btn`) — **note: these are hover-reveal today**, see [browser-compat-audit.md](browser-compat-audit.md). On touchscreens they're unreachable until that's fixed; skip the check and log it as expected.

### Usage (`#page-cost`)

- [ ] Charts render (no blank canvases).
- [ ] Per-bot tiles render.
- [ ] Date-range / unit toggles work.

### Maintenance (`#page-maintenance`)

- [ ] Default subtab loads; subtab switching works.
- [ ] Health-scan button reachable; firing it returns a result (or visible error).
- [ ] On phone: subtab row is scrollable or wraps; doesn't get truncated.

### Reports (`#page-reports`)

- [ ] Latest report renders.
- [ ] Refresh button works.

### Security (`#page-security`)

- [ ] Page renders.
- [ ] Subtabs (if present) reachable.

### Skills (`#page-skills`)

- [ ] Catalog renders.
- [ ] Filters/search input works.

### Apps (`#page-apps`)

- [ ] Per-bot app list renders.
- [ ] Add/edit affordances reachable.

### Alerts (`#page-alerts` — note: surfaced under Maintenance in nav today)

- [ ] Active / History / Subscriptions tabs all switchable.
- [ ] Subscriptions toggles persist after refresh.

### Recommendations (`#page-self-improvement`)

- [ ] Proposal list renders.
- [ ] Approve / dismiss actions reachable.

### AI Optimization (`#page-ai-optimization`)

- [ ] Page renders.
- [ ] Model-routing controls reachable.

### Cost Optimization (`#page-cost-measures`)

- [ ] Page renders.

### Getting Started (`#page-getting-started`)

- [ ] Page renders.
- [ ] Setup links reachable.

### Settings (`#page-settings`)

- [ ] Subtabs reachable.
- [ ] Form inputs full-width on phone.

### Errors (`#page-errors`)

- [ ] Error log renders.
- [ ] Copy-error-detail buttons work (clipboard).

### Feedback (`#page-feedback`)

- [ ] Form renders, inputs accept text.

### Help (`#page-help`)

- [ ] Help content renders.

### Setup wizard (when reached via web; not always present)

- [ ] Step navigation works (next/back).
- [ ] On phone: form inputs full-width; no modal that doesn't fit.

---

## 4. Manual cross-cutting checks

Run once per browser × surface, not per-page:

### Desktop (any of Chrome / Safari macOS / Firefox)

- [ ] Open dev tools, navigate through five pages. No uncaught exceptions, no failed network requests beyond expected 404s from empty deployments.
- [ ] Copy-to-clipboard works at least once (e.g. on the Errors page).
- [ ] Drag a screenshot file onto the chat drawer drop zone (Phase 1 §5.4 wires this; until then, expect no drop affordance).
- [ ] Cmd+V / Ctrl+V into the chat input (Phase 1 §5.4 same).

### iOS Safari / Chrome Android (real device)

- [ ] Tap targets feel ≥44px / 48dp — no missed taps on nav items, buttons, tiles.
- [ ] No horizontal scrollbar on portrait phone (≤390px).
- [ ] Tap-to-expand affordances reachable for anything that hovered-to-reveal on desktop.
- [ ] Pinch-zoom works (no `user-scalable=no` blocking).
- [ ] Address bar collapse on scroll doesn't clip content.

### iPad (real device)

- [ ] Landscape and portrait both usable.
- [ ] Split-view rendering doesn't break layout below ~640px effective width.

### Install (post-Phase 1, until then skip)

- Chrome desktop: address-bar Install icon appears; install creates standalone window with own dock icon.
- Safari macOS: File → Add to Dock visible; install creates standalone window.
- iOS Safari: Share → Add to Home Screen creates standalone-mode icon; opening from icon hides Safari chrome.
- Chrome Android: install banner appears; install creates home-screen icon and standalone window.
- Firefox desktop: no install option offered (expected); no broken-looking placeholder.

---

## 5. What to file when something breaks

- **Render bug only on one engine** (WebKit-only, Firefox-only, etc.) → log a Signal via the Alerts page or open a `browser-compat-<engine>-<page>` ticket; capture engine version and page URL.
- **Console error** → include the full stack and the network request immediately preceding, since most are async fetches.
- **Touch-unreachable affordance** → note file:line if obvious, otherwise the page and the element label. The [browser-compat-audit.md](browser-compat-audit.md) is the inventory.

Do **not** patch around it in tests. The Phase 0 spec is explicit: build
Evolve, don't patch the test pod. If the audit grows, the matrix doesn't.
