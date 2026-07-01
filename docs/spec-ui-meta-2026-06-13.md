# META:ui — Admin UI / UX design system (mission + scope + invariants + backlog)

**Status:** seed (2026-06-13). Design source of truth for the `ui` META coordinator.
This is a *coordinator charter*, not a feature spec — it points to the durable design
corpus (the style guide), fixes the ownership boundary against the page-content
aspects, and carries the live backlog + in-flight ledger pointer.

**Aspect:** `ui` — the **cross-cutting design system and UX quality** of the Evolve
admin app across all three surfaces it renders on: **browser, desktop app, and
mobile**. The premise (operator, 2026-06-13): good UI/UX is not a nice-to-have — it
is the line between an app being usable and not. This aspect owns the *scaffolding*
(primitives, principles, lint) so that every new component clears a high bar, and
drives the audit/sweep work that raises existing surfaces to it.

---

## 1. Mission

Own the shared design system end to end so every surface is consistent, legible,
themable, responsive, and accessible:

- **Design tokens & primitives** — `base.css` token set (color/type/spacing/radius/
  shadow), the component vocabulary (buttons, forms, cards, tables, badges, modals,
  drawers, empty/loading/error states, expand/collapse, nav).
- **The style guide** — [docs/style-guide.md](style-guide.md) is the law; keep it
  authoritative and ahead of the CSS (extend the doc *before* extending the CSS).
- **The lint** — `tools/ui-style-lint` (hybrid-severity; `--strict` in CI) is the
  enforcement seam. New rules get codified here as patterns harden.
- **Cross-cutting UX patterns** — collapsibility, theme parity (dark + light),
  responsive behavior (phone/tablet/laptop/desktop), touch targets, the help/
  instruction-text and infographic conventions, accessibility (semantic headings,
  keyboard, aria).
- **Instruction-text & infographic quality** — the *standard and mechanism* for all
  "How it works" cards, explainer banners, `help-btn` tooltips, and diagrams:
  clear, succinct, accurate, and necessary/helpful (operator's four tests).
- **The §14 rollout backlog** — drive the style-guide's known-issues list to done.

## 2. Ownership boundary (settled 2026-06-13)

Every page-content aspect already carries a *"style-guide + both-theme check on SPA
surfaces"* invariant — `ui` is the aspect those defer **to**. The split is
**presentation vs. content**:

| Concern | Owner | Note |
|---|---|---|
| Design tokens, `base.css` primitives, component vocabulary | **ui** | the shared library every page consumes |
| Style guide doc + `ui-style-lint` rules | **ui** | the law and its enforcement |
| Cross-cutting UX patterns (collapse, theme, responsive, a11y, touch) | **ui** | reusable mechanisms, not page content |
| Instruction-text/infographic *standard + mechanism* | **ui** | the quality bar, the collapsible-explainer/`help-btn` patterns, the editorial sweep |
| §14 design-system rollout (Phases 2c/5-deferred/etc.) | **ui** | finishing the existing backlog |
| Page-specific **content truth** (is *this* copy accurate?) | → owning page aspect | review-and-route: `ui` flags clarity/succinctness/collapsibility; copy-*accuracy* fixes pair with the page's aspect (reports/rsi/apps/skills/model-tiers/user-value/multi-pod). Same model as reports↔rsi. |
| What goes *on* a given page (which cards, which data) | → owning page aspect | `ui` owns *how* a card looks/behaves, not *whether* the page should have it |
| Marketing site (`docs/gitpages/index.html`) | **ui** | same token system; keep in sync per style-guide §13 (its light-theme parity pass is still owed) |

Litmus: if the change is a **shared primitive, a principle, a lint rule, or a
presentation/behavior pattern** → `ui`. If it's **the truth of a specific page's
copy or which data a page shows** → the page aspect (review & route).

## 3. Inherited design corpus (read on bootstrap)

- [docs/style-guide.md](style-guide.md) — **the spec.** §1-13 the system, §14 the
  prioritized known-issues backlog + rollout status, §15 conventions for new work.
  The "five highest-violation rules" live in [CLAUDE.md](../CLAUDE.md) admin-UI
  section — keep those in working memory.
- Admin SPA markup: `packages/admin/evolve_admin/web/index.html`; JS modules:
  `packages/admin/evolve_admin/web/static/js/`; design-system CSS:
  `packages/admin/evolve_admin/web/static/css/base.css`.
- Lint: `tools/ui-style-lint` (pre-commit hook + CI `--strict`).

## 4. Key invariants / guardrails

1. **The style guide is the law.** Every visual change complies; if nothing fits,
   extend the doc *before* the CSS (§15). No new hex / font-size / radius / spacing
   off the documented scales.
2. **Both themes are first-class.** Toggle dark↔light and verify before any visual
   PR — there is no CI gate for theme parity. Tokens theme for free; hardcoded
   values don't.
3. **Tokens, not literals.** Colors/shadows go through `var(--*)` token pairs.
   Semantic color (green/yellow/red) means status, never decoration.
4. **Data-shape input widths.** Every new `<input>`/`<select>` gets an explicit
   `.input-w-*` class; the global `width:100%` is a safety net, not the design.
5. **Reuse the primitive over hand-rolling.** A hand-rolled variant of something the
   system already has (a bespoke collapse, a parallel "primary" button, a fifth
   badge) is drift. Add to the shared primitive; migrate callsites opportunistically.
6. **Accessibility is not optional.** Semantic headings (`<h2>` not styled spans),
   keyboard-operable controls (`<button>`/`<details>`/`<summary>`, not clickable
   `<div>`s where it matters), `aria-hidden` on decorative glyphs, 44×44 touch
   targets on mobile.
7. **Lint clean.** `tools/ui-style-lint <changed-files>` passes (block-severity) and
   `--strict` warnings are addressed for new code.
8. **Deploy:** admin-only, **canary-gated** (`pod.release.mode=canary`): merge lands
   a candidate → `release promote`/soak → admin-ui kickstart. SPA assets are static;
   no migration. Verify in the live admin UI (Gate 2) after promote.

## 5. The component-audit program (the standing work)

Two parallel tracks, driven as a series of small bites:

### Track A — Component & pattern audit (consistency + usability gaps)
Sweep the SPA component-by-component to (a) calibrate against the style guide and
(b) surface usability gaps. Priority surfaces to audit (initial list, expand as we
learn): collapse/expand affordances, cards, forms/inputs, modals/drawers, badges,
tables, empty/loading/error states, navigation/tabs, the home dashboard tiles, the
help/Getting-Started surfaces. Each audit bite yields findings → fixes (often a
primitive + a callsite sweep).

**Audit finding 2026-06-13 (collapse/expand):** 27+ collapse points exist; only
~52% use the canonical `.expand-icon` (rest mix Unicode `▴ ▾`, text "Details/Hide"
buttons, or bare `<summary>`). **No reusable collapsible-card primitive exists** —
every collapsible card is hand-rolled with bespoke state CSS. **No collapse state
persists across visits** (home report/host collapse reset on reload). 3 always-shown
top-level "How it works" explainer cards (Recommendations, Forge, app-wizard) plus 4
Reports `explainer-banner`s are never collapsible.

### Track B — Instruction-text & infographic quality (operator's four tests)
Editorial sweep of every "How it works" card, explainer banner, `help-btn` tooltip,
and infographic against: **clear · succinct · accurate · necessary/helpful.** Where
a surface fails *clarity/succinctness/necessity* → `ui` fixes (rewrite/collapse/cut).
Where it fails *accuracy* (content truth) → route to the owning page aspect. The
collapsibility work (Track A) is the mechanism half of "necessary" — content a user
needs once should be present but collapsible, not permanently in the way.

Inventory of instruction surfaces captured 2026-06-13 (see in-flight memory):
~3 always-shown "How it works" cards, ~5 `<details>` explainers, ~10 JS-toggled
explainers, 4 `explainer-banner`s, 50+ `help-btn` tooltips, 9 Getting-Started cards.
Known content smells to chase: emoji used as functional step-icons in the
Recommendations pipeline (§9.12 violation); duplicated explanations of the same
concept across Getting-Started + page-level cards (accuracy-drift risk).

## 6. Backlog (live — newest decisions first)

### SHIPPED — bouts 1-2 (2026-06-14)
- **B1 ✅ #2878** — `.collapsible-card` primitive (`<details>` + `.expand-icon` +
  `static/js/core/collapsible.js`, localStorage-persisted by id, default-expanded +
  remember-collapse; style-guide §9.14). Applied to the 3 How-it-works cards.
- **Track A audits ✅ #2876/#2877** — cards + forms findings docs (the fix backlogs).
- **Card fixes ✅ #2879** — fixed the dup `.card` radius bug (every card was 12px not
  10px) + undefined `--panel-hi`→`var(--bg3)` + `.card-prose` 64ch caps (§10.6).
- **Stripes ✅ #2881** — 16 accent-stripe sites → semantic tokens (light-mode fix).
- **Empty/loading ✅ #2880** — §9.11 states in recovery/home.
- **Phase 2c ✅ #2884** — §14 P0 #3 DONE: capped 30 controls, removed global
  `width:100%` by folding it into the `.input-w-*` utilities.
- **Track B (instruction text) ✅** — audit #2888 (verdict: good overall);
  **B3a #2891** emoji→SVG step-icons (§9.12); **B3b+c #2892** Getting-Started dedup +
  de-stale + user-facing vocab standardized on "coaches".

### Remaining backlog
- **B2 — Collapse-affordance canonicalization.** Migrate the ~13 non-canonical
  collapse points (Unicode `▴ ▾`, text Details/Hide) to `.expand-icon` + the
  primitive where they're cards. Add a `ui-style-lint` rule flagging Unicode
  expand-glyphs outside menu-dropdown contexts (extends §9.13's block rule).
- **B3d — Tooltip & broad-glyph sweep** (Track B follow-on): the 75 `help-btn`
  tooltips + in-content glyph→SVG, sized but not yet dispatched.
- **C2 — accent-stripe sweep** in backup/bot-detail/pod-config/cost/alerts (9 sites)
  + a `.stripe-card` helper.
- **Routing (other aspects):** tier0/1/2/3 tooltip vocab → model-tiers; alert-channel
  copy → reports; raw `snake_case` coach-ID chips + 7-day-check-in nuance → rsi.
- **From style-guide §14:** Phase 5 deferred items, P1 #8 (primary-button dedup),
  P2 #16 (`<h2>` adoption), P2 #17 (content caps), marketing-site light-theme (§13).

## 7. In-flight ledger

**Authoritative live ledger is in memory `[[project_ui_meta_2026_06_13]]`** — read it
on bootstrap. As of 2026-06-14 (bouts 1-2 closed): **in-flight EMPTY** — all 11 PRs
merged; remaining work is the §6 backlog above; **Gate-2 deploy pending** (canary
promote + admin-ui kickstart to take bouts 1-2 live).
