# PWA Phase 0 §4.3 — Responsive Design Sub-Spec

**Date:** 2026-05-18
**Status:** Draft, pending review
**Parent:** [spec-pwa-2026-05-18.md](spec-pwa-2026-05-18.md) §4.3
**Owner:** Evolve admin web UI

---

## 1. Why this sub-spec

The parent PWA spec calls for a responsive design audit in Phase 0. That work is large enough (1–2 weeks) and has enough cross-cutting design decisions that it deserves its own scoping pass before implementation. This sub-spec captures the current state of the admin UI, the decisions that need to be made, and a phased delivery plan.

---

## 2. Current state (from audit, 2026-05-18)

The admin UI is a **single-page application**: one HTML file (`packages/admin/evolve_admin/web/index.html`), 27 tabs/pages controlled by anchor-style routing (`#page-home`, `#page-cost`, etc.). All markup, CSS, and inline JS live in that one file.

### CSS stack
- **No framework.** Custom CSS in `<style>` blocks, CSS custom properties for theming (light/dark).
- **Hand-rolled grid utilities** (`.grid`, `.grid-2`, `.grid-3`, `.grid-4`, `.grid-6`) using fixed `repeat(N, 1fr)` — they do **not** reflow on small viewports.
- **Sidebar:** fixed 220px width with `flex-shrink: 0`. No mobile collapse.
- **Main content:** `flex: 1; padding: 28px 32px` — padding does not scale down.

### Existing breakpoints (the whole set)
- `@media (max-width: 720px)` — host-health strip stacks vertically (Monitoring only).
- `@media (max-width: 980px)` — Home page goes from 2-col to stacked (Home only).
- `@media (min-width: 1280px)` — evo drawer docks at desktop width (Home only).

**That's it.** Three media queries across 27 pages. Only the Home page has meaningful responsive behavior. The other 26 are desktop-only by design.

### Page inventory and rough complexity

The 8 spec-listed primary pages plus 19 secondary pages — 27 total. From heaviest responsive lift to lightest:

| Page | Heaviest issue | Lift |
|---|---|---|
| **Maintenance** | 11 inner subtabs, 6 wide tables (gateway, cron, infra, OC version, logs, recovery) | Heavy |
| **Cost** | Per-bot tile selector + 3–4 wide tables (by model/channel/source/user), drill-down interactions | Heavy |
| **Apps** | Manifest table + test-results table + forge-jobs table + subtab bar | Heavy |
| **Analytics** | 5 subtabs, dashboard grid, several tables | Medium |
| **Reports/Alerts** | Subscriptions tab + message log + 3-level nesting | Medium |
| **Settings** | Sidebar list + right-pane forms, modal-heavy | Medium |
| **Bots (Overview)** | Bot status list + stat blocks, single column-heavy table | Medium |
| **AI Optimization** | 2 tables, subtabs, proposal list | Medium |
| **Security, Capabilities, Forge, Skills, Gallery, etc.** | Form + list patterns | Light |
| **Home** | Already responsive (only one) | None — already done |
| **Monitoring** | Host-health strip already has a 720px rule | Light |
| **Feedback, Help, Errors** | Simple forms / tabs | Light |

### Reusable components — fix once, win everywhere

Six primitives drive most of the responsive failures. Fixing each one once fixes the symptom across every page that uses it.

| Component | Defined at | Used in | Today | Needed |
|---|---|---|---|---|
| `<table>` row pattern | inline, ~6 instantiations | Maintenance, Cost, Apps, Analytics, Reports, errors | Fixed-width cols, no wrapping, no sticky header, hover-only row hi | Card-stack on phone, sticky-header + horizontal scroll on tablet |
| Subtab system (`.subtab`, `.subtab-inner`) | `index.html:293–321` | Apps, Maintenance, Analytics, Reports, Settings, AI Opt | Flexbox; overflows horizontally on phone with no scroll affordance | Horizontal scroll with fade indicator, or wrap with collapse |
| Card grids (`.grid-2/3/4`) | `index.html:74–77` | Most pages | Fixed `repeat(N, 1fr)` — no reflow | `auto-fill, minmax(...)` |
| Form rows (`.form-row`) | `index.html:277` | Settings, modals | **Already responsive** ✓ | — |
| Modal/drawer | `index.html:602–604, 575–578` | 30+ modals | `max-width: 95vw` fallback | **Mostly OK** ✓ minor polish |
| Help tooltips (`.help-btn .tip`) | `index.html:625–629` | Throughout | `:hover` reveal — touch users can't see them | Click-to-open + `:focus-within` |

And one non-component but cross-cutting issue:

| Issue | Today | Needed |
|---|---|---|
| **Sidebar** | Fixed 220px, always visible | Collapse below ~980px to a hamburger + drawer (a real nav redesign, not just CSS) |

---

## 3. Key decisions

### 3.1 Breakpoint set

Recommend adopting **four standardized breakpoints**:

| Name | Width | Target | Behavior |
|---|---|---|---|
| `phone` | <480px | Phone portrait | Single column, hamburger nav, card-stack tables, tighter padding |
| `tablet` | 480–768px | Phone landscape, small tablet | 2-col where space allows, horizontal-scroll tables with sticky col |
| `laptop` | 768–1280px | Tablet landscape, small laptop | Sidebar visible but collapsible, full tables |
| `desktop` | ≥1280px | Laptop, desktop | As today — sidebar pinned, evo drawer docks |

Keeps the spirit of today's 720/980/1280 set (none change drastically) but adds the **phone-portrait** breakpoint that's missing and renames to industry-standard names. A small CSS-custom-property convention (`--bp-phone`, `--bp-tablet`, ...) keeps them grep-able.

### 3.2 Sidebar on mobile

The 220px fixed sidebar is the single biggest mobile-usability blocker. Options:

| Option | UX | Implementation cost |
|---|---|---|
| **Hamburger → drawer** | Standard, well-understood, fits 27-page nav | Medium — needs a toggle, animation, focus management |
| **Bottom-tab nav** | Mobile-native feel | High — 27 pages don't fit; would force IA redesign |
| **Top horizontal tabs** | Works for short page lists | Doesn't scale to 27 items |
| **Hide sidebar; rely on browser back/forward** | Cheapest | Hostile UX |

**Recommend hamburger → slide-in drawer.** Below 980px, sidebar hides behind a hamburger button in the top bar. Tapping opens a full-height slide-in drawer with the same nav items. Drawer closes on item-tap, outside-tap, or Escape. Above 980px, sidebar visible as today.

### 3.3 Tables on mobile

Three viable patterns:

| Pattern | Phone (<480px) | Tablet (480–768px) | Notes |
|---|---|---|---|
| **A: Card-stack on phone, horizontal-scroll on tablet** | Each row → labeled card | Sticky first col + horizontal scroll | Most flexible; new component needed |
| **B: Horizontal scroll everywhere** | Same | Same | Cheapest; awkward on phone for wide tables |
| **C: Hide non-essential columns on phone** | Show 2–3 essential cols, link to detail page | All cols | Forces "essential cols" decision per table |

**Recommend Pattern A.** Build a `<ResponsiveTable>` primitive that supports declared columns + a phone-card renderer per row. Bigger investment up front, but the Maintenance/Cost/Apps pages all benefit.

### 3.4 Tooltips on touch

Today's `.help-btn:hover .tip` is invisible on touch. Fix at the primitive: change the rule to `.help-btn:hover .tip, .help-btn:focus-within .tip, .help-btn[data-open="true"] .tip { display: block }` plus a one-line JS handler that toggles `data-open` on tap. Single change at `index.html:625–629` covers every help button in the UI.

### 3.5 Design pass first, or iterate?

Recommend **iterate, with one exception.**

- **Foundational components** (responsive table, tooltip touch-fix, grid `auto-fill`, subtab overflow) — just build them. They have one obvious right answer.
- **Sidebar mobile drawer** — wants a design pass. It's the most-used surface; getting hamburger placement, drawer animation, and the close-affordance wrong is the kind of mistake users notice on every page-load. Worth 30 min sketching before coding.

### 3.6 PR shape

Recommend small, sequential PRs:

1. Breakpoint custom properties + `auto-fill` grid utilities (foundation).
2. Responsive table primitive (used by 4 heavy pages).
3. Tooltip touch fix (one-line primitive, all pages benefit).
4. Subtab overflow handling (one-line primitive, several pages benefit).
5. Sidebar hamburger + drawer (its own PR — high-visibility surface).
6. Per-page sweeps (one PR per heavy-lift page; Maintenance, Cost, Apps).
7. Quick-win sweep (one PR for the long-tail pages).

Per memory: Pod-Admin's flow is design-sync → ship → use → retrospect. Small PRs with a real device check at each step beat one monolithic responsive-pass PR.

---

## 4. Phased delivery

| Phase | Work | Estimate |
|---|---|---|
| **4.3.a Foundation** | Breakpoint custom properties, `auto-fill` grids, responsive table primitive, tooltip touch-fix, subtab overflow | **3–4 days** |
| **4.3.b Sidebar** | Hamburger + drawer + nav focus management; design sketch first | **2 days** |
| **4.3.c Heavy-lift pages** | Maintenance, Cost, Apps — one PR per page, applying the new primitives | **4–5 days** |
| **4.3.d Long-tail sweep** | Bots, Analytics, Reports, Settings, Security, AI Opt, Capabilities, etc. (20+ pages, mostly grids + forms) | **3–4 days** |
| **4.3.e Cross-device verification** | Run the §4.4 matrix on a real iPhone, Android phone, iPad. Bugfix sweep. | **1–2 days** |

**Total: 2–3 weeks.** Matches the parent spec's estimate.

Phases are sequential except 4.3.b (sidebar) which can run in parallel with 4.3.c after 4.3.a lands.

---

## 5. Open questions

1. **Sidebar drawer animation and placement.** Hamburger top-left (Material) vs. top-right (iOS)? Slide-in from left, right, or top? Recommendation: hamburger top-left, drawer slides from left — matches Material/Plex/Tailscale conventions and most evolutions of web nav. Sketch first.

2. **Card-stack rendering for tables.** When a table row becomes a card on phone, which column becomes the card "title" (top, bold)? Recommend: an explicit `cardTitleColumn` prop on the responsive-table component; defaults to the first column. Per-page override available.

3. **Tooltip click-to-open dismiss.** Tap outside to dismiss, or tap-again-to-toggle? Recommend tap-outside-dismiss + Escape; click-to-toggle gets confusing across nested tooltips.

4. **Maintenance's 11 inner subtabs.** Even with subtab overflow, 11 is a lot on phone. Worth flagging as an IA decision separate from responsive: should some of those tabs collapse into an "Advanced" submenu? Recommend: in scope for 4.3.c, since reducing tab count is part of making Maintenance phone-usable.

5. **Subtab overflow style.** Horizontal scroll with edge-fade, or wrap to multiple rows? Recommend horizontal scroll for navigation tabs (preserves tab order), wrap for filter chips.

6. **Density toggle.** Some users (looking at Maintenance) may prefer a compact-density mode even on phone. Worth a v2 add — out of scope for 4.3 unless it falls out for free.

7. **Tab-key navigation on the drawer.** Standard focus-trap-while-open or just let focus flow? Recommend focus-trap with Escape-to-close; that's the accessibility baseline.

8. **What happens when an operator changes layout via dev-tools mid-session?** Don't engineer for this; React-style re-renders are not the model. Accept that radical resize may require reload.

---

## 6. Out of scope

- **Design system overhaul.** Custom CSS stays. No migration to Tailwind / Radix / etc. as part of this work.
- **Dark/light theming changes.** The existing custom-property theme keeps working.
- **Replacing tables with something fancier** (data grids, virtual scrolling). Tables stay tables; they just get the responsive primitive.
- **Page-IA redesign** beyond the Maintenance subtab consolidation flagged in §5.4.
- **Animation polish.** Functional responsive first. Easing curves and microinteractions in a follow-on if anyone cares.
- **Accessibility audit.** Touched (focus-trap, focus-within on tooltips) but not the focus of this work. A11y has its own spec when it gets one.

---

## 7. Acceptance criteria

For Phase 0 §4.3 to be "done":

- Every primary page (the 8 spec-listed plus the 19 secondary) renders at 360px width without horizontal scroll on the page itself (tables may scroll internally).
- All tap targets ≥44px on touch devices.
- No `:hover`-only affordances that reveal information; every hover-revealed element has a touch path.
- Sidebar collapses to hamburger below 980px; nav still reaches every page.
- Tables on phone render as card-stacks or scroll horizontally with a visible scroll indicator.
- The §4.4 manual matrix passes on the user's real iPhone, Android phone, and iPad.
- No regressions: every existing desktop view still works at ≥1280px.

---

## 8. Why this shape

- **Foundational primitives first** — fixing 6 reusable components fixes most of 27 pages before anyone touches a page-specific layout. High leverage.
- **Hamburger sidebar is its own PR** — it's the surface a user touches on every interaction; deserves design attention disproportionate to its line count.
- **Heavy-lift pages get dedicated passes** — Maintenance, Cost, Apps each have enough complexity that bundling them with the long-tail sweep would hide regressions.
- **Real-device verification at the end** — Playwright + WebKit catches structural bugs; real iOS Safari catches the iOS-specific things (e.g. `100vh` includes Safari's bottom bar) that emulators miss.
- **Matches Pod-Admin's design-sync-ship-use-retrospect workflow** — small, reviewable PRs with a device check at each step.
