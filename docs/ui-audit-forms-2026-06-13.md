# UI Audit — Forms, Inputs, Selects, Toggles (2026-06-13)

*Track A component audit spawned by META:ui (the admin UI/UX design-system coordinator).*
*Scope: every form control in the admin SPA (`packages/admin/evolve_admin/web/index.html` + `static/js/**`) against [docs/style-guide.md](style-guide.md) §6 (radius), §9.2 (forms), §10.1 (control type), §10.2 (input widths), and the §14 P0 #3 / Phase 2 width-rollout.*

**This is an audit-only document. It changes no code.** It produces a ranked findings table and a dispatchable bite plan for fix-chips.

---

## Executive summary

**The form surface is ~85–90% width-compliant.** Phase 2a/2b did the heavy lifting: in `index.html` alone, 87 controls carry an `.input-w-*` class and 47 carry an explicit inline width — only ~15 remain implicitly full-width. Across the whole SPA, **28 form controls still lack any explicit width** and inherit the global `input, select, textarea { width: 100% }` rule. The remaining debt is concentrated, not scattered: 14 textareas, 5 selects in `ai-optimization.js`, 2 in `bot-detail.js`, and a handful of one-offs.

**Is Phase 2c (scoping-down / removing the global width rule) finishable now? — No, but it's one bite away.** The global rule is still load-bearing for ~28 controls that have no explicit width of their own (the chat composers, the JSON/config editors, and several modal note fields all rely on it to be full-width; the route-tier selects and the heal-config numeric rely on it and look *wrong* because of it). Removing the rule today would collapse the composers and editors to intrinsic width. **Sequence: do the explicit-width sweep (Bite A) first, then scope the global rule (Bite B).** The §14 Phase-2c note that says "the remaining inputs are either single-input modal fields opting into `width:100%` inline … or auto-width filter chips" is **inaccurate** — it undercounts the real remainder (see Finding W1).

### Top 5 issues

1. **`ai-optimization.js` route-tier & compact selects render full-width with short labels (5 selects).** `ai-route-{maintenance,background,ambiguous}Tier`, `ai-compact-mode`, `ai-compact-tier` carry ad-hoc inline styling, no width → a 4-option "Auto / Fast / Standard / Power" picker stretches the whole column. Exactly the §10.2 anti-pattern. **P1.**
2. **Heal-config numeric input is full-width (`index.html:12418`).** The `_row()` helper emits `<input type="number">` inside `.form-field` (which has no width cap) → a small integer field spans the modal. The textbook "Duration: 30 dropdown 800px wide" case. **P1.**
3. **Phase 2c is blocked by ~14 textareas + ~12 selects/inputs with no explicit width.** Until these carry `.input-w-text` / `.input-w-full` / `.input-w-sm` / `.input-w-auto`, the global `width:100%` cannot be removed without regressing them. **P1 (gating).**
4. **Form-control radius drift: 30× `border-radius:5px` + several `4px` instead of the canonical 6px (§6).** Heavily concentrated in `ai-optimization.js` (11) and `cost-measures.js` (`.cm-other-select`, `.cm-cell-custom-input`, `cm-runaway-threshold` all 4px). Inputs/selects should be 6px. **P2.**
5. **`.form-select` / `.input` utilities still don't exist (§9.2 says "New work should add one").** The same ~10-line inline style block (`background:var(--bg2);border:1px solid var(--border);…border-radius:5px`) is copy-pasted onto every select in `ai-optimization.js`, `bot-detail.js`, `onboard-modal.js`, `plugins.js`, `skills.js`, `users.js`, `cost-measures.js`. This is the root cause of findings 1 & 4 and the durable fix for both. **DESIGN-FORK (high leverage).**

### Method & caveats

- Robust multi-line tag parser (the `tools/ui-style-lint` single-line regex finds **0** in `index.html` because nearly every control there is a multi-line tag or JS template literal — it materially under-reports; do not trust it for width coverage).
- "Width control" = an `.input-w-*` class, an inline `width:`/`max-width:`/`flex:` style, a width-exempt `type` (checkbox/radio/hidden/file/button/etc.), or a project class that sets width in CSS (verified by hand for `.cm-*`).
- Three lint matches were comment false-positives (`index.html:8624`, `home.js:142`, `home.js:266` — all prose `<input>`/`<select>` inside code comments) and are excluded.
- `cost-measures.js:962` (`.cm-caps-chip-custom-input`, width:72px) and `cost-measures.js:1871` (`.cm-cell-custom-input`, max-width:90px) are width-capped by their project class → excluded from the width findings (kept under radius).

---

## Ranked findings

Severity: **P0** operator-visible + easy · **P1** worth a focused fix · **P2** hygiene. Classification: **CLEAR-FIX** (mechanical, no judgment) · **DESIGN-FORK** (needs a decision).

| # | Page / feature | File:line | Rule / gap | Sev | Recommended fix | Class |
|---|---|---|---|---|---|---|
| **W1** | AI Optimization — route-tier selects | [ai-optimization.js:2183](packages/admin/evolve_admin/web/static/js/pages/ai-optimization.js#L2183), [:2193](packages/admin/evolve_admin/web/static/js/pages/ai-optimization.js#L2193), [:2203](packages/admin/evolve_admin/web/static/js/pages/ai-optimization.js#L2203) | §10.2 full-width select, short labels; §9.2 ad-hoc inline style | P1 | `.input-w-md` (160) or `.input-w-auto`; ultimately `.form-select` | CLEAR-FIX |
| **W2** | AI Optimization — compact-mode selects | [ai-optimization.js:2664](packages/admin/evolve_admin/web/static/js/pages/ai-optimization.js#L2664), [:2674](packages/admin/evolve_admin/web/static/js/pages/ai-optimization.js#L2674) | §10.2 full-width select, short labels | P1 | `.input-w-md` / `.input-w-auto` | CLEAR-FIX |
| **W3** | Heal config — numeric override row | [index.html:12418](packages/admin/evolve_admin/web/index.html#L12418) | §10.2 full-width numeric (`.form-field` has no cap) | P1 | `.input-w-sm` (80) | CLEAR-FIX |
| **W4** | Bot detail — handover expiry/audience | [bot-detail.js:185](packages/admin/evolve_admin/web/static/js/pages/bot-detail.js#L185), [:195](packages/admin/evolve_admin/web/static/js/pages/bot-detail.js#L195) | §10.2 full-width-in-grid-cell select | P2 | expires→`.input-w-md`/`-auto`; audience→`.input-w-lg` (long labels) | CLEAR-FIX |
| **W5** | Onboard / plugins / skills / users — inline mini-selects | [onboard-modal.js:298](packages/admin/evolve_admin/web/static/js/pages/onboard-modal.js#L298), [plugins.js:604](packages/admin/evolve_admin/web/static/js/pages/plugins.js#L604), [skills.js:675](packages/admin/evolve_admin/web/static/js/pages/skills.js#L675), [users.js:1460](packages/admin/evolve_admin/web/static/js/pages/users.js#L1460) | §10.2 full-width select | P2 | `.input-w-auto` / `.input-w-md` | CLEAR-FIX |
| **W6** | Cost measures — "other source" select | [cost-measures.js:1842](packages/admin/evolve_admin/web/static/js/pages/cost-measures.js#L1842) | §9.2 `.cm-other-select` is `max-width:100%` (uncapped) + radius 4px | P2 | cap width via `.input-w-md`; radius 4→6px | CLEAR-FIX |
| **T1** | Modal note/description textareas | [index.html:1243](packages/admin/evolve_admin/web/index.html#L1243) (ncap-desc), [:1588](packages/admin/evolve_admin/web/index.html#L1588) (forge-approval-notes), [:1599](packages/admin/evolve_admin/web/index.html#L1599) (forge-reject-reason), [:6500](packages/admin/evolve_admin/web/index.html#L6500) (fb-note), [:7097](packages/admin/evolve_admin/web/index.html#L7097) (wiz-description), [create-app-wizard.js:129](packages/admin/evolve_admin/web/static/js/pages/create-app-wizard.js#L129), [:260](packages/admin/evolve_admin/web/static/js/pages/create-app-wizard.js#L260) | §9.2 textarea → 600px band, none set it | P2 | `.input-w-text` (600) | CLEAR-FIX |
| **T2** | Chat composers (own their row) | [index.html:476](packages/admin/evolve_admin/web/index.html#L476) (home-prompt), [:6169](packages/admin/evolve_admin/web/index.html#L6169) (help-prompt), [:6530](packages/admin/evolve_admin/web/index.html#L6530) (feedback-prompt), [:7717](packages/admin/evolve_admin/web/index.html#L7717) (evo-drawer-input) | §9.2 full-width legit, but **implicit** — depends on global rule | P1 (gating) | explicit `.input-w-full` so Bite B is safe | CLEAR-FIX |
| **T3** | Body editors (own their row) | [index.html:1307](packages/admin/evolve_admin/web/index.html#L1307) (gallery-import-json), [:3454](packages/admin/evolve_admin/web/index.html#L3454) (hook-baseline-fields), [:3484](packages/admin/evolve_admin/web/index.html#L3484) (plugin-config-fields) | §9.2 full-width legit, but **implicit** | P1 (gating) | explicit `.input-w-full` | CLEAR-FIX |
| **I1** | Delete-bot confirmation input | [index.html:814](packages/admin/evolve_admin/web/index.html#L814) | §10.2 full-width text in narrow modal | P2 | `.input-w-md` (160) | CLEAR-FIX |
| **R1** | Form-control radius drift | 30× `border-radius:5px` (mostly [ai-optimization.js](packages/admin/evolve_admin/web/static/js/pages/ai-optimization.js), [plugins.js:604](packages/admin/evolve_admin/web/static/js/pages/plugins.js#L604), [skills.js:675](packages/admin/evolve_admin/web/static/js/pages/skills.js#L675)) + 4px in [cost-measures.js:1952/1966](packages/admin/evolve_admin/web/static/js/pages/cost-measures.js#L1952), identity inputs [index.html:2537–2545](packages/admin/evolve_admin/web/index.html#L2537) | §6 form controls = 6px | P2 | sweep form-control radii → 6px (verify each is a control, not decorative) | CLEAR-FIX |
| **G1** | Global width rule, stale doc ref | rule at [base.css:1130](packages/admin/evolve_admin/web/static/css/base.css#L1130); guide cites `:1001` at [style-guide.md:370/670/821](style-guide.md) | §14 P0 #3 accuracy | P2 | scope/remove rule (Bite B); fix the 3 stale `:1001` refs → `:1130` | CLEAR-FIX |
| **D1** | Ad-hoc inline-styled selects everywhere | ai-optimization, bot-detail, onboard-modal, plugins, skills, users, cost-measures | §9.2 "add a `.form-select` / `.input` utility" — neither exists | P1 | promote `.form-select` + `.input`, migrate callsites | DESIGN-FORK |
| **D2** | 46 raw `<input type=checkbox>` not `.toggle` | across `index.html` + JS (only 1 carries a class) | §9.2 "don't restyle a checkbox ad hoc / use `.toggle` for boolean settings" | P2 | audit which are boolean *settings* (→`.toggle`) vs multi-select/consent (stay) | DESIGN-FORK |
| **D3** | Route-tier select vs segmented buttons | [ai-optimization.js:2183–2203](packages/admin/evolve_admin/web/static/js/pages/ai-optimization.js#L2183) | §10.1 — mirrors the home tier selector, which uses segmented buttons + a mobile `<select>` | P2 | recommend: keep as `<select>` (4 opts, dense config row) + cap width — don't import the home segmented treatment here | DESIGN-FORK |

---

## Proposed bite plan

Bites are scoped to **not collide on the same file region** and ordered by dependency. Bites A→B are the path to closing Phase 2c; they should land in sequence (A then B). C is independent.

### Bite A — "explicit-width sweep on the remaining 28 controls" *(Phase 2c precondition · CLEAR-FIX)*
Add the correct `.input-w-*` class to every control in findings **W1–W6, T1–T3, I1**. Mapping is mechanical (data-shape table, §9.2): numerics→`-sm`, short selects→`-md`/`-auto`, long-label selects→`-lg`, textareas→`-text`, composers/editors that own the row→`-full` (explicit, so they survive Bite B). **Files:** `index.html` + `ai-optimization.js`, `bot-detail.js`, `onboard-modal.js`, `plugins.js`, `skills.js`, `users.js`, `cost-measures.js`, `create-app-wizard.js`. ~28 edits, no logic change. **This is the largest and most valuable bite** and the gate for everything Phase 2c.

### Bite B — "scope-down the global `width:100%` rule" *(Phase 2c · CLEAR-FIX · depends on A)*
After Bite A makes every full-width control explicit, change [base.css:1130](packages/admin/evolve_admin/web/static/css/base.css#L1130) `input, select, textarea { … width: 100% … }` to **drop `width:100%`** (let intrinsic width + the `.input-w-*` utilities drive) or scope it to `.input-w-full`. Then fix the three stale `[…:1001]` doc refs (**G1**) and tick §14 Phase 2c ✅. **Must not land before A** or the composers/editors regress to intrinsic width. Single-file CSS + doc edit; small, but high-blast-radius — verify both themes + phone/tablet/laptop, and click every composer.

### Bite C — "form-control radius normalization" *(independent · CLEAR-FIX)*
Sweep finding **R1**: `border-radius:5px`/`4px` on inputs & selects → `6px` (§6). Concentrated in `ai-optimization.js` and `cost-measures.js`; eyeball each `5px` hit to confirm it's a form control vs a decorative chip before flipping. No overlap with A/B's regions except `cost-measures.js` `.cm-other-select` (W6 touches its width, C touches its radius — coordinate or fold W6 into C for that one file). Independent otherwise.

### DESIGN-FORK items (decide before dispatching)

- **D1 — promote `.form-select` + `.input` utilities (RECOMMEND: do it).** This is the durable fix that *subsumes Bites A & C* for the inline-styled selects — one utility class replaces the copy-pasted 10-line style block, fixing width, radius, and theme-token drift at once. Bigger (touches ~12 callsites across 7 files) so it's a fork vs the quick Bite-A class-add. **Recommendation:** add the two utilities now (small, additive, mirrors the `.input-w-auto` filter pattern), then migrate the ad-hoc selects to them *as* Bite A rather than just bolting on a width class. If schedule-constrained, Bite A's width-class add is an acceptable interim that doesn't block D1 later.
- **D2 — checkbox→`.toggle` audit (RECOMMEND: targeted, not blanket).** 46 raw checkboxes, but most are legitimate (multi-select lists, consent forms). Only boolean *settings* switches should become `.toggle`. Needs a per-callsite pass; not a mechanical sweep. Low priority.
- **D3 — route-tier control type (RECOMMEND: keep `<select>`).** 4 options in a dense config row → `<select>` is correct per §10.1; just cap its width (Bite A / D1). Do **not** import the home-page segmented-button + mobile-select treatment — that's a hero control, these are config rows.

---

## Phase 2c verdict (for META:ui)

> **Not safe today; safe immediately after Bite A.** The global `input, select, textarea { width:100% }` rule (now at base.css:**1130**, not :1001 as the guide says) is still load-bearing for ~28 controls — 14 textareas (incl. all four chat composers and the JSON/config editors), the five `ai-optimization.js` tier selects, the two handover selects, and the heal-config numeric. Run Bite A to give every one of them an explicit `.input-w-*` class, then Bite B can drop/scope the rule cleanly. The §14 Phase-2c note ("remaining inputs are just opt-in modal fields / auto-width chips") understates the remainder and should be corrected when Bite B lands.
