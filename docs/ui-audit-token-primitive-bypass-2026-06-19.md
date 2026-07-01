# UI audit — inline token/primitive bypass + lint-coverage gaps

*2026-06-19 · META:ui · audit + paydown plan (no product-code change)*

## Why this audit exists

Two operator-reported bugs — a dark-blue unreadable link (bare `<a>` inheriting
the browser default `~#0000EE`) and a Remove button drawn with raw-hex orange
instead of the `.btn-warning` primitive — both fixed in
[#3035](https://github.com/evolve-ops/evolve/pull/3035) — were not one-offs. They are
the visible tip of a **systemic bypass**: the admin SPA has a real design system
(`docs/style-guide.md` is law, tokens live in `base.css`, `tools/ui-style-lint`
gates commits) but a large amount of UI is built in JS template strings with
**inline `style=` attributes that hardcode values the token/primitive system
already owns**, and the lint does not currently catch the bypass.

Every finding below is one of two kinds:

- **(A) Style not fully applied** — an inline override that *should* reference an
  existing token or adopt an existing primitive. The system already covers it;
  the callsite went around it.
- **(B) No style ever captured** — a scenario the design system has no token,
  primitive, or lint rule for. The fix is to *extend the system* (add a token
  pair / primitive / lint rule), then converge callsites onto it.

The headline result: **the bypass is almost entirely class A.** Inline hex is not
"data-viz that legitimately needs raw color" — it is overwhelmingly muted-grey
body text and semantic status color that map cleanly onto existing tokens. There
are essentially **zero legitimate data-viz one-offs** in inline styles. So this is
a convergence + lint-coverage problem, not a "the system is missing colors"
problem.

---

## Executive summary

| Class | What | Count (measured) | A / B split |
|---|---|---|---|
| 1 | Raw hex in inline `style=` | **227 attrs / 57 distinct hexes** (169 bare + 58 `var(--x,#hex)` fallbacks) — admin SPA only | **~96% A** (tokenizable), ~1% B (urgency-scale palette), 58 fallbacks = cosmetic-only |
| 2 | Inline `font-size` off the documented §4 table | **~3.7k inline declarations**; de-facto scale of ~11 rungs | mostly a **doc gap (B)** — lint already blesses the heavy rungs; ~55 true outliers are A |
| 3 | Bare links / click-targets without color or class | **26 `javascript:` + 98 `href=` no-class** | A — covered by #3035's global `a{}` rule; ~remainder need a `.link` utility |
| 4 | Hand-rolled buttons | **50 `<button onclick>` with no `.btn`** + **197 `<div>/<span> onclick>`** | A (adopt `.btn-*`) + a11y B (non-button click targets) |

**The single biggest number in the whole audit:** of ~169 bare inline hexes,
**96 (~57%) are grey muted-text** (`#888`×60, `#999`, `#aaa`, `#bbb`, `#666`,
`#777`, `#ccc`, `#333`…) that should be `var(--text2)` / `var(--text3)`. One
mechanical sweep of the grey family clears more than half of class 1.

**Hotspot:** `static/js/pages/self-improvement.js` alone holds **131 of the 169**
bare hexes (`#888`×50, `#7fc8ff`×21, grey family ×~30). It is not a chart page —
it is the same muted-text + semantic-status set as everywhere else, just denser.
Paying down that one file clears ~78% of class 1.

**Why the lint missed all of this:** `ui-style-lint` checks CSS-file shadows,
off-scale fonts, expand triangles, and input widths — but has **no rule for raw
hex inside an inline `style=` attribute**, even though §2/§3 make tokens
mandatory. That single gap is why 227 inline hexes accumulated under a green gate.
Deliverable 2 of this audit closes it (warn-tier + shrink-only baseline).

---

## Class 1 — Raw hex in inline `style=`

### Frequency (bare, excluding `var(--x,#hex)` fallbacks)

```
 60 #888      35 #7fc8ff   12 #fff     10 #ff8c42  10 #bbb
  8 #eb4       7 #aaa       7 #999      6 #ffa502   6 #ff4757
  6 #c53030    6 #666       6 #333      5 #000      4 #7fff9e
  4 #3dd984    3 #fca5a5    3 #f5b400   3 #ccc      3 #777   …(57 distinct)
```

### Per-file hotspots (bare hex count)

| File | Bare hexes |
|---|---|
| `static/js/pages/self-improvement.js` | **131** |
| `static/js/pages/cost-measures.js` | 22 |
| `static/js/pages/backup.js` | 21 |
| `static/js/pages/users.js` | 15 |
| `index.html` | 12 |
| `static/js/pages/pod-config.js` | 8 |
| `inbox.js` / `alerts.js` | 4 each |
| (remainder) | ≤2 each |

### Categorization (A / B / legit) with the token mapping

Every frequent hex family maps to an **existing** token — this is the table a
paydown bite executes against.

| Family (hexes) | Count | Class | Maps to | Notes |
|---|---|---|---|---|
| **Grey muted text** `#888 #999 #aaa #bbb #666 #777 #ccc #333 #444 #64748b #94a3b8 #8896a5` | **96** | **A** | `var(--text2)` (labels/secondary) or `var(--text3)` (hints/disabled) | The dominant class. Pick `--text2` for ≥`#888`, `--text3` for darker `#666`/`#333`. Hardcoded greys are also a **theme-parity bug** — they don't lighten in light theme. |
| **Info/link blue** `#7fc8ff #aac8e0 #3742fa #6366f1 #5a67d8 #2a4a5e #e2e8f0` | 40 | **A** | `var(--blue)` / `var(--cyan)` (`#4CC9F0`); `#6366f1`/`#5a67d8` indigo → `var(--accent)` (off-brand purple drift) | `#7fc8ff` is overwhelmingly a "jump to…" link color — see class 3; these want the link treatment, not a chip. |
| **Warning orange** `#ff8c42 #ffa502 #f97316 #ffb347 #ffb060 #ffb83c #f59e0b #ff8c4255` | 22 | **A** | `.btn-warning` (buttons) or a new `--warning-orange` token (text/badges) | `.btn-warning` itself hardcodes `color:#ff8c42` ([base.css:875](../packages/admin/evolve_admin/web/static/css/base.css#L875)) — **the primitive has no token to point at.** See class-1 §B. |
| **Amber/yellow** `#eb4 #f5b400 #fbbf24 #ffd966 #d4a72c #d6a82c` | 9 | **A** | `var(--yellow)` | Status text on `⚠` lines. |
| **Red/error** `#c53030 #ff4757 #ff5050 #f87171 #fca5a5 #f85149 #ff4d4d #dc2626 #e54 #ff8c8c #e57373` | 25 | **A** | `var(--red)` (text), `var(--red)` fill + `var(--on-accent)` text | Error messages + destructive CTAs. `#dc2626` on a CTA fill also appears as `background:var(--red)` next to a literal twin — converge. |
| **Green/success** `#7fff9e #3dd984 #5fd17a #2ed573 #22c55e #3fb950` | 10 | **A** | `var(--green)` | "configured" / "active" chips. |
| **White on fill** `#fff` | 12 | **A** (mild) | `var(--on-accent)` | Always `color:#fff` on a `var(--red)`/`#dc2626` fill. Reads in both themes by luck (same as panic button), but should use the `--on-accent` token introduced for exactly this. |
| **Brand purple** `#b47fff` | 2 | **A** | `var(--accent)` / `var(--purple)` | Badge text; lighter than canonical, drift. |
| **Dark-bg surfaces** `#000 #0d0d12 #1c1c1c #1a1a1a #181818 #1e293b` | 9 | **A** | `var(--bg)` / `var(--bg2)` | e.g. the fatal-error full-screen at [index.html:13800](../packages/admin/evolve_admin/web/index.html#L13800) hardcodes `background:#0d0d12` → `var(--bg)`. |

Representative callsites:
- Grey text: [cost-measures.js:131](../packages/admin/evolve_admin/web/static/js/pages/cost-measures.js#L131) `color:#888`; [self-improvement.js:1068](../packages/admin/evolve_admin/web/static/js/pages/self-improvement.js#L1068) uppercase label `color:#888`.
- Link blue: [backup.js:121](../packages/admin/evolve_admin/web/static/js/pages/backup.js#L121) `color:#7fc8ff;text-decoration:none`.
- Error CTA twin: [overview.js:1427](../packages/admin/evolve_admin/web/static/js/pages/overview.js#L1427) `background:var(--red);color:#fff` vs [cost-measures.js:1990](../packages/admin/evolve_admin/web/static/js/pages/cost-measures.js#L1990) `background:#dc2626;color:#fff` — same widget, two color idioms.

### Class 1 — the (B) cases (system genuinely lacks a token)

1. **Warning-orange has no token.** `.btn-warning` and ~22 inline callsites all
   spell `#ff8c42` literally because there is no `--warning-orange` /
   `--orange-warn` token. `--orange` exists (`#fb923c`) but is reserved for
   forge/security badges (§3) and is a *different* hue. **Recommend:** add a
   `--btn-warning-fg` (dark `#ff8c42` / light `#b45309`, matching the existing
   light override at [base.css:3742](../packages/admin/evolve_admin/web/static/css/base.css#L3742))
   token pair, point `.btn-warning` at it, then the inline callsites can
   reference it too.
2. **Urgency palette is a semantic scale, not ad-hoc.** `self-improvement.js`
   defines a `URGENCY_COLORS` map ([self-improvement.js:56](../packages/admin/evolve_admin/web/static/js/pages/self-improvement.js#L56))
   keyed by urgency level. This is the one place a small **B** token set is
   justified — a 3–4 step urgency ramp — *if* it can't simply reuse
   green→yellow→orange→red semantic tokens (it can, and should: urgency *is*
   status). Recommend collapsing it onto the semantic tokens rather than minting
   new ones.

### Class 1 — legit / low-priority (do NOT flag for paydown)

- **`var(--x, #hex)` fallbacks (58 occurrences).** e.g. `background:var(--bg2,#1c1c1c)`.
  The token *is* referenced; the hex is a defensive fallback. Cosmetic only —
  drop the fallback opportunistically, never block on it. The Deliverable-2 lint
  rule **excludes** these by design (the hex is inside a `var()` call).
- **No genuine chart/sparkline one-offs found** — a sweep of inline hex inside
  `chart`/`canvas`/`gradient` contexts returned a single match. The design
  system loses nothing by mandating tokens inline.

---

## Class 2 — inline `font-size` vs the documented scale

### The actual inline scale (rem, by frequency)

```
710 0.78   483 0.72   461 0.82   276 0.85   260 0.7    203 0.8
185 0.74   160 0.75    86 0.76    40 0.68    38 0.65   25 1
25 0.95    22 0.62     19 0.73    17 1.1     14 0.9     9 0.67
 …  (singletons: 0.77×1, 0.94×1, 1.0×1)
```

### Reconciliation — this is a DOC gap, not 3.7k violations

The §4 table documents `0.7 / 0.75 / 0.8 / 0.85 / …` but **omits** `0.72 / 0.74 /
0.76 / 0.78 / 0.82`, which together account for **~1,900 callsites** and which
**`ui-style-lint` already treats as canonical** (its `OFF_SCALE_FONT` "good" list
at [tools/ui-style-lint:62](../tools/ui-style-lint#L62) explicitly blesses
`0.72/0.74/0.78/0.82`). So the lint and the codebase already agree on a finer
scale than the *prose* table admits. Treating 3.7k declarations as violations
would be wrong.

**Note on lint mechanics:** `OFF_SCALE_FONT` is a narrow *denylist* — it flags
only `0.83/0.84/0.86/0.88/0.55/0.64/0.81/0.87`. Everything else (including the
outliers `0.77/0.73/0.67/0.62`) **passes today**. So "off the §4 table" ≠ "caught
by lint." The heavy rungs pass because they're not on the denylist, not because
they're an allowlisted scale.

**Recommendation (a DECISION for the coordinator/operator, presented — not
imposed):**

> **(a) Codify the de-facto scale.** Update style-guide §4 to document the rungs
> that are in heavy use and pass the current denylist: `0.62 · 0.65 · 0.68 ·
> 0.7 · 0.72 · 0.74 · 0.75 · 0.76 · 0.78 · 0.8 · 0.82 · 0.85 · 0.9 · 0.95 · 1 ·
> 1.1 · 1.3 · 1.4 · 2`. This makes the table descriptive of reality. Optionally
> extend `OFF_SCALE_FONT` to *add* the singleton outliers (`0.77/0.73/0.67`) to
> the denylist so they're caught going forward — but only after the existing
> callsites are rounded (B7), else it blocks legacy code.

> **(b) Consolidate only the unambiguous outliers** (round-to-nearest, ~55
> callsites, all class A):
>
> | Outlier | Count | → Round to | Confidence |
> |---|---|---|---|
> | `0.77rem` | 1 | `0.78` | certain |
> | `0.73rem` | 19 | `0.74` | high |
> | `0.67rem` | 9 | `0.68` | high |
> | `0.94rem` | 1 | `0.95` | certain |
> | `1.0rem` | 1 | `1rem` (normalize literal) | certain |
> | `0.62rem` | 22 | **decision** — codify as the micro rung *or* round to `0.65` | judgment |
> | `0.66rem` | 4 | leave (already lint-blessed) | n/a |

`0.62` (×22) is the only real judgment call: it sits between the `0.58` nav-caption
rung and `0.65`. Either bless it as the in-content micro rung or consolidate to
`0.65`. Flagging it here so the decision is explicit, not silent.

**Net:** class 2 is mostly **B (doc convergence)** + a ~55-callsite **A** cleanup.
Do *not* mass-rewrite the 3.7k; that would be churn against a scale everyone
already uses.

---

## Class 3 — bare links / non-anchor click targets

### Counts

- `href="javascript:…"` links: **26**
- `<a href=…>` with no `class=`: **98**

### #3035 already fixes the color for all of them

[#3035](https://github.com/evolve-ops/evolve/pull/3035) adds a global tokenized rule
to `base.css`:

```css
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
```

Element-selector specificity `(0,0,1)` means every class-styled anchor
(`.btn-*`, `.nav-item`, `.sib-cta`, `.hb-link`, `.pod-switcher-row`) is
unaffected — so all 124 bare anchors get a readable, theme-correct color in one
stroke, and it also repairs the latent `.cm-fix-link` (no color def). **This
class is effectively closed for color.** Confirmed by reading the merged-branch
diff at commit `a307787`.

### What still needs a *different* treatment

1. **Inline `color:#7fc8ff` links (~35).** These bare anchors *also* hardcode a
   blue that now fights the global `--accent` rule (inline beats element
   selector). They should **drop the inline color** and inherit `a{}` — i.e.
   they are class 1 *and* class 3. A paydown bite removes `color:#7fc8ff;
   text-decoration:none` and lets the global rule win.
2. **Non-anchor click targets (`<div>/<span> onclick>`, 197 — see class 4).**
   The `a{}` rule does nothing for these. Where the target is semantically a
   *link* (navigates / reveals), introduce a **`.link` utility** (`color:
   var(--accent); cursor:pointer; text-decoration:none; :hover underline`) so a
   `<span>` link reads the same as an `<a>` without abusing `<button>` styling.

### Lint recommendation

Add a **warn-tier** `bare-link-color` rule: an `<a …>` whose tag carries neither
a `class=` nor an inline `color:` *and* whose `href` is not `javascript:` (those
are click-handlers, color comes from `a{}`). Warn-only because the global `a{}`
rule means most are now fine — the warning is a nudge for the rare anchor that
needs a utility class, not a block.

---

## Class 4 — hand-rolled buttons

### Counts

- `<button onclick=…>` with **no** `class="…btn…"`: **50**
- `<div>` / `<span>` with `onclick=` (button behavior, wrong element): **197**

### Cluster → primitive mapping

The 50 classless `<button>`s fall into recognizable clusters; each should adopt a
canonical variant from §9.1:

| Cluster (representative) | Adopt |
|---|---|
| Inline "Remove / Drop / Revoke" (users.js — *already migrated in #3035*) | `.btn.btn-sm.btn-warning` |
| Destructive "Delete / Disable / Stop" | `.btn.btn-sm.btn-danger` |
| Confirm / Approve | `.btn.btn-sm.btn-green` |
| "Copy" / secondary inline actions ([pod-config.js:1005](../packages/admin/evolve_admin/web/static/js/pages/pod-config.js#L1005) `style="…background:var(--red);color:#fff"`) | `.btn.btn-sm.btn-ghost` (or `.btn-danger` if it IS destructive — copy is not) |
| Primary CTA per surface (error-recovery CTAs in overview.js / cost-measures.js) | `.btn.btn-primary` (+ drop the inline `background:var(--red);color:#fff`) |

Several classless buttons additionally carry **inline `background:`/`color:` hex**
(class 1 overlap) — adopting the primitive removes the inline color for free.

### a11y finding — 197 `<div>/<span> onclick>`

Per §9.1 rule 3 ("don't restyle a `<span>` as a button — accessibility and
keyboard nav depend on it"), a click-handler on a non-interactive element is not
keyboard-focusable and not announced as a control. The 197 occurrences are a
real a11y debt. Two legitimate sub-cases:
- **Links** (navigate/reveal) → real `<a>` or the `.link` utility from class 3.
- **Buttons** (mutate/act) → real `<button class="btn …">`, or at minimum
  `role="button"` + `tabindex="0"` + an Enter/Space key handler.

### Lint recommendation

Add a **warn-tier** `hand-rolled-button` rule, two heuristics:
1. `<button …>` whose tag lacks `class="…btn…"`.
2. `<div`/`<span` whose tag carries `onclick=` and lacks `role="button"`.
Warn-only (high false-positive risk on `<span onclick>` used as a genuine toggle
cell); the value is the running count + audit list, not a hard gate.

---

## New design-system coverage

### New tokens to add to `base.css`

| Token | Dark | Light | Why |
|---|---|---|---|
| `--btn-warning-fg` | `#ff8c42` | `#b45309` | Class 1 §B-1. Removes the literal from `.btn-warning` + ~22 inline callsites. Light value already exists at [base.css:3742](../packages/admin/evolve_admin/web/static/css/base.css#L3742). |
| *(none else required)* | | | Every other inline hex maps to an **existing** token. The system is not short on colors — callsites just bypassed it. |

### New primitives

| Primitive | Spec | Why |
|---|---|---|
| `.link` | `color:var(--accent); cursor:pointer; text-decoration:none;` + `:hover{text-decoration:underline}` | Class 3/4. A link-styled non-anchor (`<span>` jump target) without abusing `<a>` or `.btn`. Mirrors the `a{}` rule from #3035 so anchors and span-links read identically. |

### New `tools/ui-style-lint` rules (concrete, actionable specs)

These are written as drop-in additions mirroring the existing rule structure
(`OFF_SCALE_FONT`, `INLINE_DARK_SHADOW`, severity tiers, baseline gate).

1. **`inline-hex` — raw hex inside an inline `style=` (warn, baseline-gated).**
   *(Shipped in this PR — see Deliverable 2.)*
   - Regex: match `style="…#RGB|#RRGGBB|#RRGGBBAA…"`, **excluding** hex inside a
     `var(--x, #hex)` fallback (the token is referenced) and inside SVG
     attribute values.
   - Severity: **warn**, gated by a **shrink-only count baseline**
     (`tools/inline-hex-baseline.txt`, same shape as
     `collapse-canonical-baseline.txt`) so existing 227 don't block, only net
     new ones do; `--strict` enforces the per-file no-growth cap.
   - Rationale: §2/§3 make tokens mandatory; this is the gap that let 227 hexes
     accumulate green.

   - *Count note:* the lint baseline reports **197 inline-hex sites** — a
     per-*line* count over `LINTED_PREFIXES` (which also includes
     `docs/gitpages/index.html`), whereas the audit's **169** is a per-*attribute*
     count over the admin SPA only. Both are correct for what they measure; the
     baseline's job is a no-growth tripwire, not a headline metric.

2. **`bare-link-color` (warn).** `<a …>` with no `class=` and no inline
   `color:` and `href` ≠ `javascript:`. Nudge toward a utility class for the
   rare anchor the global `a{}` rule doesn't cover. Warn-only.

3. **`hand-rolled-button` (warn).** `<button>` lacking `class="…btn…"`, OR
   `<div`/`<span` with `onclick=` and no `role="button"`. Surfaces the count +
   list for class-4 paydown. Warn-only (false positives on toggle cells).

4. **§4 doc reconciliation + optional denylist extension.** Update §4's table
   to the de-facto rung list. The existing `OFF_SCALE_FONT` denylist does **not**
   currently catch the outliers (`0.77/0.73/0.67/0.62` all pass) — so rounding
   them (B7) is a manual cleanup, after which the three clear ones
   (`0.77/0.73/0.67`) can be *added* to the denylist to prevent regressions.
   `0.62` needs the explicit codify-vs-consolidate decision first.

---

## Proposed paydown plan (≈30-min bites, shrink-only where it fits)

Ordered by leverage. Each bite is independently shippable, doc-only-adjacent, and
verifiable by re-running `ui-style-lint --strict`.

| # | Bite | Effort | Clears |
|---|---|---|---|
| **B0** | *(this PR)* land `inline-hex` warn rule + `inline-hex-baseline.txt` seeded at current count | done | future-proofs class 1 |
| **B1** | **Grey-family sweep in `self-improvement.js`** — `#888/#999/#aaa/#bbb/#666/#333` → `var(--text2)`/`var(--text3)`. One file, mechanical. | 30m | ~80 hexes (47% of class 1) + the file's theme-parity bugs |
| **B2** | Grey-family sweep across remaining files (cost-measures, backup, users, index.html) | 30m | ~16 hexes |
| **B3** | Add `--btn-warning-fg` token pair; point `.btn-warning` at it; sweep the 22 warning-orange inline callsites | 30m | class 1 §B-1 + 22 hexes |
| **B4** | Semantic-status sweep — `#7fff9e/#3dd984→--green`, `#eb4/#f5b400→--yellow`, `#c53030/#ff4757→--red` | 30m | ~44 hexes |
| **B5** | Link-blue sweep — drop inline `color:#7fc8ff;text-decoration:none` on bare anchors, let `a{}` win; add `.link` utility; convert `#6366f1/#5a67d8→--accent` | 30m | ~40 hexes + class 3 residue |
| **B6** | Dark-bg + white-on-fill — `#0d0d12/#000→--bg`, `#fff` on red fills → `--on-accent` | 20m | ~21 hexes |
| **B7** | §4 doc reconciliation (operator picks codify vs consolidate) + round the 5 singleton outliers + `0.62` decision | 30m | class 2 |
| **B8** | Classless `<button>` → `.btn-*` adoption (50 sites, clustered per the class-4 table) | 30m | class 4 buttons |
| **B9** | `bare-link-color` + `hand-rolled-button` warn rules + their baselines | 30m | locks classes 3/4 |
| **B10** | a11y: `<div>/<span> onclick>` → real `<button>`/`.link`/`role=button` (large; split per-page) | multi-bite | 197 a11y sites |

After **B1+B2** the inline-hex baseline can ratchet down by ~96 in one move
(`ui-style-lint --update-…-baseline`), the same shrink-only pattern as
`collapse-canonical-baseline.txt`.

---

## Appendix — measurement commands (reproducible)

```bash
cd packages/admin/evolve_admin/web
# class 1 frequency
grep -rEoh 'style="[^"]*#[0-9a-fA-F]{3,8}[^"]*"' static/js index.html \
  | grep -oE '#[0-9a-fA-F]{3,8}' | sort | uniq -c | sort -rn
# class 2 font scale
grep -rEoh 'font-size:\s*[0-9.]+rem' static/js index.html \
  | grep -oE '[0-9.]+rem' | sort | uniq -c | sort -rn
# class 3 bare links
grep -rEoh 'href="javascript:' static/js index.html | wc -l
grep -rEoh '<a [^>]*href="[^"]*"[^>]*>' static/js index.html | grep -v 'class=' | wc -l
# class 4 buttons
grep -rEoh '<button [^>]*onclick=[^>]*>' static/js index.html | grep -v 'class="[^"]*btn' | wc -l
grep -rEoh '<(div|span)[^>]*onclick=[^>]*>' static/js index.html | wc -l
```
