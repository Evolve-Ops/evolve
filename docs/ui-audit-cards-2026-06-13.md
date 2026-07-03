# UI Audit — Cards & Panels (2026-06-13)

**Spawned by META:ui (Track A).** Audit-only, read-only against the shared web files.
Surface scope: every card/panel in the admin SPA — `.card`, `.stat-block`,
`.insights-panel`, `.bot-row`, `.cost-bot-tile` / `.cm-bot-tile`, `.botcfg-card`,
`.explainer-banner`, and ad-hoc `<div class="card" style="…">` / raw-div panels.
Measured against [`docs/style-guide.md`](style-guide.md) §5, §7, §9.3, §9.11, §10.4,
§10.6, §10.7.

---

## Executive summary

**The card surface is structurally healthy but leaks at the edges.** The base
primitives (`.card`, `.stat-block`, `.insights-panel`, `.bot-row`, the two bot-tiles)
are token-driven, on the spacing scale, and Phase 7 already normalized their paddings.
Hover shadows are correctly confined to interactive tiles, and Phase 3 cleared inline
`rgba(0,0,0,…)` shadows from `base.css`. So the *defined* card system is ~90% compliant.

The drift is almost entirely in **inline-styled cards** (~158 `<div class="card" style=…>`
across `index.html` + page JS) and **ad-hoc accent-stripe panels**. The good news: most
inline styles are benign (`margin-bottom:14px` — the canonical stack margin). The genuine
violations cluster into a handful of repeatable patterns, which makes them bite-able.

**Overall compliance estimate: ~85% by surface count.** The non-compliant ~15% is
concentrated, not diffuse — five patterns account for nearly all of it.

### Top 5 issues

1. **Duplicate global `.card` rule** — `base.css:2567` redefines `.card` at
   `border-radius: 12px`, overriding the canonical `10px` at `base.css:433`. Because it
   wins the cascade, **every card in the app renders at 12px**, contradicting §6 (cards =
   10px; 12px is the *drawer* radius). One-line fix, globally visible. *(Touches the
   shared `base.css` — B1's file; sequence after B1.)*
2. **20 accent-stripe cards with hardcoded hex `border-left` colors** that don't theme
   (§2/§3). e.g. `border-left:3px solid #7fc8ff` / `#ff4d4d` / `#eb4`. Tuned for dark bg,
   no light pair, and `#7fc8ff` isn't even the canonical blue. Densest in
   `self-improvement.js` (11). The correct pattern already exists — `.explainer-banner`
   (`base.css:1323`) does `border-left: 3px solid var(--accent)`.
3. **Content-width caps essentially absent** — only **3** `max-width:64ch` declarations
   in the entire web tree, against many long-paragraph cards (the 4 Reports
   `explainer-banner`s run 246–591 chars; the "How it works" cards are full-width prose).
   At desktop these wrap to unreadable 1000px+ line lengths (§10.6). This is §14 #17,
   still open.
4. **Bad token + hardcoded fallback** — `arbiter-rate-banner` (index.html:1831) uses
   `background:var(--panel-hi, #222)`. `--panel-hi` is not a defined token, so it always
   falls through to the literal `#222`, which stays dark in light theme (§2). One-line fix.
5. **Empty/loading-state gaps** (§9.11) — `recovery.js` and `home.js` render
   nothing-states with **zero** `.empty`/`.empty-state-card` primitives; `reports.js` has
   7 empty states but **0** loading/skeleton states; and ~8 empty strings ("No
   applications discovered yet", "No metrics yet.", "No jobs found…") carry **no
   next-action verb**, which §9.11 explicitly calls failure.

---

## Findings (ranked)

Severity: **P0** visible-and-easy · **P1** focused-PR · **P2** hygiene.
Classification: **CLEAR-FIX** (one answer per the guide) · **DESIGN-FORK** (needs a call).

| # | Surface | file:line | Rule / gap | Sev | Class | Fix |
|---|---------|-----------|------------|-----|-------|-----|
| 1 | All cards | `base.css:2567` (vs `:433`) | Duplicate global `.card` at radius **12px** overrides canonical **10px** (§6). 12px is drawer radius. | P1 | CLEAR-FIX | Delete the radius from the `:2567` "UPDATED COMMON" copy (or merge the two `.card` rules); leave the `:433` canonical 10px as the single source. **Shared file — sequence after B1.** |
| 2 | Accent-stripe panels (self-improvement) | `self-improvement.js` :1032 `#ff8c42`, :1436/:1445 `#ff5050`, :1451 `#ffb83c`, :1696/:2336/:2619/:3116/:3460 `#7fc8ff`, :2973/:3145 `#ff4d4d` | Hardcoded hex `border-left` — no theme pair, off-token (§2/§3). | P1 | CLEAR-FIX | Map to semantic tokens: red→`var(--red)`, amber→`var(--yellow)`, orange→`var(--orange)`, blue→`var(--blue)`. |
| 3 | Accent-stripe panels (backup/config/cost) | `backup.js` :124 `#eb4`, :176/:217/:983 `#ff4757`, :192 `#ffa502`; `bot-detail.js:203` `#f0b020`; `pod-config.js:752` `#eb4`; `cost-measures.js:134` / `alerts-extended.js:61` `#7fc8ff` | Same hardcoded-hex stripe pattern (§2/§3). | P1 | CLEAR-FIX | Same token mapping as #2. Consider extracting a `.stripe-card.is-{warn,crit,info}` helper so the next one can't freelance a hex. |
| 4 | Reports + wizard explainer cards | `index.html` :4313/:4403/:4522/:4566 (`explainer-banner`), :1432/:1465/:1750 ("How it works") | Long paragraph prose (246–591 ch) with no `max-width:64ch` (§10.6, §14 #17). | P2 | CLEAR-FIX | Add `max-width:64ch` to `.explainer-banner` and the explainer card body in `base.css`. **Shared files.** |
| 5 | Arbiter rate banner | `index.html:1831` | `background:var(--panel-hi, #222)` — undefined token → literal `#222`, no light pair (§2). | P1 | CLEAR-FIX | Replace with `var(--bg3)` (the intended "raised panel" surface). **Shared file.** |
| 6 | Recovery page | `recovery.js` (0 empty-state primitives) | Nothing-states rendered without `.empty`/`.empty-state-card` (§9.11). | P1 | CLEAR-FIX | Wrap empty renders in `.empty` / `.empty-state-card`; add a next-action verb. |
| 7 | Home page | `home.js` (0 empty-state primitives) | Same as #6. | P1 | CLEAR-FIX | Same. |
| 8 | Reports loading states | `reports.js` (7 empty, 0 loading) | Cards show empty/populated but no `.loading`/`.skeleton` during fetch (§9.11). | P2 | DESIGN-FORK | Either add skeletons or confirm fetches are fast enough that a flash isn't worth it — coordinator call. |
| 9 | Verbless empty states | `apps.js:539` "No applications discovered yet", `forge.js:377` "No metrics yet.", `maintenance.js:206` "No jobs found…", `users.js:1335` "No users approved yet.", `apps.js:2924` "No tracked files…" | Empty state with no next-action verb (§9.11 rule). | P2 | CLEAR-FIX | Append a verb/link ("…— discover apps →", "Run the forge →"). |
| 10 | Ad-hoc modals as raw `.card`-ish divs | `apps.js:1824/1892/1918` `<div style="background:var(--bg2);border-radius:12px;padding:18px 20px;…">` | Hand-rolled panel instead of `.modal`; radius 12px (drawer) + off-scale `20px` padding (§5/§9.6). | P2 | CLEAR-FIX | Convert to `.modal`/`.modal-wide`. (Modal-scope overlap — coordinate with the modals audit if one runs.) |
| 11 | Off-scale padding `18px 20px` | `index.html:1432/1465/1750`, `self-improvement.js:1693`, `apps.js:1824/1892/1918` | `20px` is off the 4/6/8/10/12/14/18/22 scale (§5). | P2 | CLEAR-FIX | Round to `18px 22px` or `18px`. |
| 12 | Spacing-rhythm mix (JS card stacks) | page JS: `margin-bottom` values 6/8/10/12/14/16 intermixed | Sibling cards at mixed margins read as broken layout (§10.7). | P2 | DESIGN-FORK | Audit per-stack; many are sub-elements not true card siblings. Needs eyes-on per page, not a blind sweep. |
| 13 | `.explainer-banner` radius | `base.css:1323` `border-radius: 4px` | 4px is badge radius; an info banner should be 6px (alert) or 10px (card) per §6. | P2 | DESIGN-FORK | Minor; bump to 6px if treated as an alert. Low priority. **Shared file.** |
| 14 | `<h2>` semantic adoption | broad: 157 `.card-title` vs 82 `<h2>` in `index.html` | §14 #16 (still open) — styled-span/`.card-title` section titles that should be `<h2>` for outline order (§4 rule 3). | P2 | DESIGN-FORK | Page-by-page conversion; not card-specific enough for one bite. Track in §14 #16. |

### Not findings (verified compliant)

- **Hover shadows** — all `:hover { box-shadow }` rules (`base.css:555/577/2405/2613`) are
  on genuinely interactive surfaces (`cost-bot-tile`, `cm-bot-tile`, `pod-node`,
  `evo-fab`) and use `var(--shadow-hover)`. §7 rule satisfied.
- **Inline rgba(0,0,0) shadows in base.css** — none remain (Phase 3 swept them).
- **Base card paddings** — `.card` 18, `.stat-block` 16, `.insights-panel` 16,
  `.bot-row` 12×14, bot-tiles 10–12 all on-scale (Phase 7).
- **Double `.btn-primary` per card** — no card-region instance found.
- **Most inline margins** — `margin-bottom:14px` (88 in HTML, the dominant value) is the
  canonical stack margin; not a violation.

---

## Collapsibility candidates (feed B2 backlog)

Large always-shown explainer/reference cards a returning operator would want collapsed.
**Excluded per the brief:** the 3 "How it works" cards (Recommendations/Forge/app-wizard)
B1 is already handling. Remaining candidates:

| Surface | file:line | Why collapse |
|---------|-----------|--------------|
| Reports → Subscriptions explainer | `index.html:4313` (531 ch) | Dense onboarding prose, shown every visit. |
| Reports → explainer #2 | `index.html:4403` (246 ch) | Same. |
| Reports → explainer #3 | `index.html:4522` (591 ch) | Longest banner; pure reference. |
| Reports → explainer #4 | `index.html:4566` (377 ch) | Same. |
| Arbiter "How it works" | `index.html:1750` | Reference text above the proposal list. |
| Identity / pod-context cards | `index.html:2100/2517/2553` | Settings reference an operator reads once. |
| Diagnostics report card | `index.html:2712` (`diag-report-card`) | Deep-dive panel, rarely needed open. |

Recommendation: once B1's collapsible-card primitive lands, the 4 Reports
`explainer-banner`s are the highest-value first adopters (most text, most repeat visits).

---

## Proposed bite plan

All fix-bites must **rebase on `main` after B1's collapsible-card PR lands** to avoid
colliding on `index.html` / `base.css` / page JS. Grouped so no two bites touch the same
file region.

### CLEAR-FIX bites

**Bite C1 — accent-stripe → tokens (self-improvement).** *(~30 min, JS only)*
Replace the 11 hardcoded `border-left:3px solid #hex` in `self-improvement.js` with
semantic tokens (red/amber/orange/blue per finding #2). Self-contained to one file.

**Bite C2 — accent-stripe → tokens (the rest) + helper.** *(~30 min, JS only)*
The 9 stripes in `backup.js`, `bot-detail.js`, `pod-config.js`, `cost-measures.js`,
`alerts-extended.js` (findings #3). While here, add a `.stripe-card.is-{warn,crit,info}`
helper in `base.css` so future stripes can't freelance hex. *(Touches `base.css` — last in
sequence, after B1.)*

**Bite C3 — shared-file one-liners + content caps.** *(~30 min, `base.css`/`index.html`)*
Bundle the small shared-file fixes that all touch `base.css`/`index.html`: dedupe the
`.card` radius (finding #1), fix `--panel-hi`→`var(--bg3)` (#5), add `max-width:64ch` to
`.explainer-banner` + explainer card bodies (#4), round `18px 20px`→`18px 22px` in the 3
HTML explainer cards (#11). **Must be sequenced strictly after B1 merges** — heaviest
overlap with B1's working set.

**Bite C4 — empty/loading states.** *(~30 min, JS only)*
Add `.empty`/`.empty-state-card` to `recovery.js` and `home.js` (#6, #7); add next-action
verbs to the ~5 verbless empties in `apps.js`/`forge.js`/`maintenance.js`/`users.js` (#9).

### DESIGN-FORK items (need a coordinator/operator call)

- **#8 Reports loading states** — add skeletons, or accept the flash if fetches are sub-
  100ms? Recommend: **add skeletons** — Reports fetches hit the signal store and can lag.
- **#10/#11 ad-hoc apps.js modals** — fold into a modals-audit bite rather than cards;
  recommend deferring to whoever owns the modal surface.
- **#12 spacing-rhythm mix** — needs eyes-on per page (many "cards" are sub-elements).
  Recommend: **skip the blind sweep**, fold into per-page work as pages get touched.
- **#13 explainer-banner radius** — recommend **leave at 4px** unless the banner gets
  reclassified as an alert; cosmetic.
- **#14 `<h2>` adoption** — recommend tracking under §14 #16 as its own cross-page bite,
  not a card bite.

---

*Audit produced read-only; no shared web files were modified. Fixes are backlog items for
dispatched chips, sequenced after B1's collapsible-card primitive lands.*
