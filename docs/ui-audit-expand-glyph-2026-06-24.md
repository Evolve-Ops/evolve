# UI audit — raw expand/collapse triangle glyphs (§9.13 class sweep)

*2026-06-24 · META:ui · audit + canonicalization + lint-coverage gap close*

## Why this audit exists

An operator reported the Chat-page bot-tile expand/collapse triangle was too
small, too dim, and a raw Unicode glyph — it should be the canonical
`.expand-icon` SVG chevron (style-guide §9.13). That was fixed in
[#3244](https://github.com/evolve-ops/evolve/pull/3244) (bot tile) and
[#3246](https://github.com/evolve-ops/evolve/pull/3246) (the "Smaller stuff"
toggle). This audit asks the follow-on question: **is that a one-off, or a
class?** — and closes the **lint gap** that let the bot-tile caret ship clean.

The answer: the bot tile was *almost* the whole class. The admin SPA already
adopts `.expand-icon` at **92 sites**. A full sweep of `static/js/**` +
`index.html` for triangle glyphs (`▲ △ ▴ ▵ ▶ ▷ ▸ ▹ ▼ ▽ ▾ ▿ ◀ ◁ …`) finds
**73 raw-glyph instances across 71 lines** — but the §9.13 classification rule
(convert only genuine **expand/collapse affordances**; KEEP menu carets, CTA
go-arrows, status/trend badges, sort arrows) lands almost all of them in the
KEEP column. Only **one** genuine hand-rolled collapse caret remained:
`settings.js`'s "Advanced" `<details>` disclosure.

So the product-code change is tiny (1 site). The durable win is **deliverable
#3: closing the lint blind spot** so the *next* hand-rolled caret can't ship
silently.

---

## Executive summary

| Disposition | Sites (lines) | What |
|---|---|---|
| **CONVERT** → `.expand-icon` | **1** | `settings.js:215` — the Advanced `<details>/<summary>` disclosure, a raw 0.72rem `▶` in `.module-advanced-arrow` |
| **KEEP** — menu / dropdown / popover caret | 10 | `Snooze ▾`, `Dismiss ▾`, `Bulk Snooze ▾`, the Maintenance `More ▾` trigger, the score-breakdown `▾` popover |
| **KEEP** — CTA go-arrow (`▸`) | 10 | `Continue ▸`, `Verify ▸`, `Go ▸`, `Open Setup wizard ▸`, `Set up ▸` |
| **KEEP** — `▶` play/action button or external-link arrow | 24 | `▶ Set up`, `▶ Dispatch`, `▶ Unpause`, `▶ Resume`, `… scope ▶` (external doc links) |
| **KEEP** — trend / status badge (`▲`/`▼`) | 14 | `▲ new`, `▼ 12% vs prior`, `▼ orphan`, `▼ 3 error(s)` |
| **KEEP** — sort arrow | 1 | `monitoring.js:182` `arrow = up ? '▲' : '▼'` (table sort direction) |
| **KEEP** — comment (not rendered) | 11 | doc lines describing migrated glyphs or the `More ▾` cluster |
| **Total** | **71 lines / 73 glyphs** | |

**Convert count: 1. Keep count: 70 lines (72 glyphs).** No genuine
expand/collapse affordance is left as Unicode; no menu/CTA/sort/badge is
wrongly converted.

---

## CONVERT (1)

### `settings.js:215` — Advanced module disclosure

```html
<!-- before -->
<summary class="module-advanced-summary"><span class="module-advanced-arrow">▶</span> Advanced</summary>
<!-- after -->
<summary class="module-advanced-summary"><span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span> Advanced</summary>
```

This is a genuine native `<details>/<summary>` content disclosure (it shows/
hides the Advanced module list). The `▶` was a bare text glyph inheriting the
summary's `0.72rem` font — i.e. an ~8px triangle, exactly the legibility
problem §9.13 exists to fix (`.module-advanced-arrow` had *no* sizing rule of
its own, despite a comment claiming it "inherits sizing from `.expand-icon`").

**Target / mechanics:**
- `.expand-icon` is a fixed **14×14px** SVG chevron, `color: var(--text2)`,
  so it no longer shrinks with the inherited font.
- Rotation on open needs **no new CSS** — the base utility's
  `details[open] > summary .expand-icon` rule (`base.css:281`) already rotates
  any `.expand-icon` inside an open `<details>`. The native disclosure marker
  is suppressed by the existing `.module-advanced-summary::-webkit-details-marker`
  rule plus `summary:has(.expand-icon)`.
- The now-dead `.module-advanced-arrow` CSS hook (`base.css`, its rotation
  rule + comment) is **removed**; a `:hover` rule
  (`.module-advanced-summary:hover .expand-icon { color: var(--text) }`) is
  added so the chevron brightens as an interactive control.
- No behaviour change: same toggle, same default-collapsed state. Verified in
  both themes (the chevron uses `var(--text2)`/`var(--text)` tokens, parity for
  free).

This site is also a `<summary>`, so the existing `non-canonical-collapse`
lint rule already counted it (grandfathered in `collapse-canonical-baseline.txt`).
Converting it lets us **ratchet that baseline down** (see below). The
block-tier `unicode-expand-triangle` rule, by contrast, never caught it — its
glyph set is only `▸▾▼`, and `▶` (U+25B6) is not in it.

---

## Judgment calls (KEEP, with reasoning)

Two sites toggle a region but are deliberately **not** converted. Both are
documented here per the "when ambiguous, prefer KEEP" rule.

### `onboard-modal.js:316` — "Use a different account" override toggle → **KEEP**

```js
onclick="onboardToggleOverride(...)">${st.override_open ? '[hide override]' : '[▶ Use a different account]'}</a>
```

It *does* show/hide an inline region (the override-credential inputs), so it's
disclosure-shaped. But it is a **label-swap text link**, not a caret control:
the entire label changes (`[▶ Use a different account]` → `[hide override]`)
and the glyph **disappears** in the open state. `.expand-icon` is a *persistent
rotating chevron*; this is the legitimate, different "show more / show less"
text pattern. Converting it would mean restructuring the asymmetric-label logic
and inventing an open-state glyph — a redesign, not a mechanical
canonicalization. Outside the scope of "canonicalize hand-rolled **carets**."

### `self-improvement.js:2942` — score-breakdown `▾` → **KEEP**

```js
rank #${rank || '?'} · score ${score} ▾
  <span ... class="score-popover" style="display:none;position:absolute;bottom:100%;...">
```

The `▾` triggers a **floating popover** (`.score-popover`, absolutely
positioned `bottom:100%` — it floats *above* the chip), not an inline accordion.
That is the §9.13 "menu/dropdown — select-like trigger" KEEP category: a caret
that drops a positioned panel. The caret does not rotate, and a 90° rotation
would point *away* from the upward popover. This matches how the existing
block-tier rule already treats it (`score-chip` / `score-popover` are in its
skip-list).

---

## KEEP — full enumeration by category

**Menu / dropdown / popover caret (10):** `index.html` 1842, 4523, 4531, 4611,
2649 (`<span class="caret"> ▾</span>` Maintenance "More" trigger);
`alerts.js` 363, 377, 721, 735; `self-improvement.js` 2942 (judgment call).
*Why KEEP:* §9.13 explicitly excludes select-like triggers that drop a menu/
popover.

**CTA go-arrow `▸` (10):** `index.html` 360, 431, 5904, 5915, 11080, 11226,
11354, 11450; `bot-detail.js` 392, 395. *Why KEEP:* forward "Continue / Verify
/ Go / Open …" affordances, not collapses.

**`▶` play/action button & external-link arrow (24):** `index.html` 3724,
4112, 4113, 4115, 7019, 7026; `apps.js` 798, 2998; `credentials.js` 291, 339,
352, 389, 603, 733; `evo-drawer.js` 850; `forge.js` 134, 495, 506;
`onboard-modal.js` 112, 113, 124, 316 (judgment call), 474; `recovery.js` 489.
*Why KEEP:* `▶ Set up / Dispatch / Unpause / Resume` are play-style action
buttons; the `… ▶` links are external-doc go-arrows. None show/hide a region.

**Trend / status badge `▲`/`▼` (14):** `apps.js` 487, 1946 (`▼ orphan`);
`cost-measures.js` 316, 320, 322; `cost.js` 190, 195, 196;
`model-catalog.js` 317 (`▼ N error(s)`); `overview.js` 407, 412, 413;
`self-improvement.js` 3960, 3961. *Why KEEP:* directional status indicators
(`.pod-trend-up`/`-down`, status badges), §9.13 KEEP.

**Sort arrow (1):** `monitoring.js:182` `const arrow = up ? '▲' : '▼'` —
table-header sort direction. *Why KEEP:* §9.13 sort-arrow exclusion.

**Comments, not rendered (11):** `index.html` 2635, 7963, 8829, 10782;
`subtabs.js` 11, 45, 79, 112, 131; `credentials.js` 227; `home.js` 2388
(documents the migrated `show ▾ / hide ▴` glyphs). *Why KEEP:* `//` / `/* */`
/ `<!-- -->` lines don't render; the lint already skips them.

---

## Deliverable #3 — closing the lint coverage gap

### The gap

Two lint rules touch expand glyphs, and **both** missed the bot-tile caret:

1. **`unicode-expand-triangle`** (block-tier) — regex `[▸▾▼]` with a context
   skip-list. Two blind spots:
   - **Glyph set** — only `▸▾▼`. An up-caret `▴`/`▵` (used by show/hide
     toggles) or a `▶`/`▲` is never matched.
   - **`caret` skip** — the skip-list contains the literal substring `caret`,
     so *any* line with a `caret`-named class is excluded — including a genuine
     collapse caret like `<span class="…-tile-caret">▾</span>`.
2. **`non-canonical-collapse`** (warn, baseline-gated) — only scans
   `<summary>…</summary>` spans. A hand-rolled caret on a `<span>`/`<button>`/
   `<div>` (the bot-tile shape) is invisible to it.

So a hand-rolled caret on a non-`<summary>` element whose class contains
`caret` (or that uses `▴`/`▶`) sails through clean. That's how #3244 shipped a
too-small raw triangle with `ui-style-lint` exiting 0.

### The fix — a `handrolled-collapse-caret` sibling rule

A new warn-tier, shrink-only-baseline rule (mirrors `inline-hex` exactly).
`_line_has_handrolled_caret(line)` fires when **all** hold:

- the line carries a raw triangle glyph from the **broad** set
  `▴▵▾▿▸▹◂◃▲△▽▶◀▷◁►◄` (a superset of the block rule's `▸▾▼`); **and**
- the element's `class="…"` value names a caret/collapse control —
  `caret | collapse | disclosure | chevron | accordion | arrow | \bexpand\b`;
  **and**
- it is **not** already `.expand-icon` (canonical); **and**
- it carries **no** menu signal (`aria-haspopup`, `role="menu"`, `menuitem`,
  `dropdown`, `popover`).

Requiring a caret/collapse **class** is what keeps it conservative: plain CTA
buttons (`▶ Set up`), trend badges (`▲`), and bare `arrow = up ? '▲'`
assignments carry no such class, so they never match. Menu carets that *do*
use a `.caret` class (the Maintenance "More ▾" trigger) are excluded by the
haspopup signal.

**Behaviour:**
- **Per-finding:** warn-tier (prints, never blocks a normal commit) — same as
  `inline-hex`, because the class heuristic can have edge cases.
- **`--strict` (CI):** a per-file no-growth gate against
  `tools/handrolled-caret-baseline.txt`. The list may only ratchet **down**.
- **Seed baseline:** after converting `settings.js:215`, the rule matches
  **zero** current sites — the rule is precise enough that there is nothing
  legit to absorb, so the seeded baseline is **empty (0 entries)**. Any
  net-new hand-rolled caret therefore **blocks** under `--strict`.

It would have caught the #3244 bot tile: `home-rail-tile-caret` contains
`caret`, the glyph was `▾`/`▴`, no menu signal, not yet `.expand-icon` → flagged.
A 12-case unit test
(`packages/admin/tests/test_ui_style_lint_handrolled_caret.py`) pins both the
gap-closure cases and the KEEP categories.

### Lint before / after

| | before | after |
|---|---|---|
| `handrolled-collapse-caret` rule | *did not exist* | new warn-tier + shrink-only gate, **0 sites / baseline 0** |
| `unicode-expand-triangle` (block) | unchanged | unchanged (no FP risk taken on a block rule) |
| `non-canonical-collapse` (`<summary>`) | baseline **11** (stale; reality 5) | converted `settings.js` → reality **5**, baseline ratcheted **11 → 5** |
| `ui-style-lint --strict` on changed files | — | **0 blocking** |

The `non-canonical-collapse` baseline drop (11 → 5) is: **−1** from this PR's
`settings.js` conversion, **−5** from `index.html` summaries canonicalized by
prior PRs (#3244/#3246-era) but never ratcheted. Tightening a shrink-only gate
is risk-free and locks in the paydown.

---

## Outcome

- **1** genuine hand-rolled collapse caret canonicalized (`settings.js`
  Advanced disclosure) → `.expand-icon`, both themes verified.
- **70** sites correctly KEPT (menu/CTA/trend/sort/comment) per the §9.13
  exclusion list, **2** documented judgment calls.
- The **lint blind spot is closed**: a new `handrolled-collapse-caret` rule +
  empty shrink-only baseline + unit test means the *next* bot-tile-style caret
  cannot ship green.
