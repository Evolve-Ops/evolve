# Evolve — Style Guide

*Last updated: 2026-06-08*

Authoritative reference for Evolve's visual design system. When adding or restyling a component, find the relevant section here first; if nothing fits, extend this doc before extending the CSS. The admin UI markup lives at [packages/admin/evolve_admin/web/index.html](../packages/admin/evolve_admin/web/index.html); the design-system CSS lives at [packages/admin/evolve_admin/web/static/css/base.css](../packages/admin/evolve_admin/web/static/css/base.css) (extracted from index.html in evolve-ops/evolve#2418). New tokens and component rules go in `base.css`. Keep the admin UI and marketing site (`docs/gitpages/index.html`) in sync from this doc.

## Contents

1. [Logo](#1-logo)
2. [Theme parity (dark + light)](#2-theme-parity-dark--light)
3. [Color tokens](#3-color-tokens)
4. [Typography](#4-typography)
5. [Spacing](#5-spacing)
6. [Border radius](#6-border-radius)
7. [Shadows & elevation](#7-shadows--elevation)
8. [Layer color coding](#8-layer-color-coding)
9. [Components](#9-components)
10. [UI principles](#10-ui-principles)
11. [Layout & responsive](#11-layout--responsive)
12. [Animation](#12-animation)
13. [Marketing site](#13-marketing-site)
14. [Known issues & prioritized recommendations](#14-known-issues--prioritized-recommendations)
15. [Conventions for new work](#15-conventions-for-new-work)

---

## 1. Logo

### Primary mark
- `artwork/evolve_mobius_text_white_tight.svg` — Möbius mark + "EVOLVE" wordmark, white. Use on dark backgrounds.
- `artwork/evolve_mobius_text_black_tight.svg` — same composition, black. Use on white/light backgrounds.

### Mark only (no wordmark)
- `artwork/evolve_mobius_white.svg` — for dark backgrounds
- `artwork/evolve_mobius_black.svg` — for light backgrounds
- `artwork/evolve_mobius_gray.svg` — neutral contexts

### Rules
- Always use the SVG files directly — never recreate the mark.
- Minimum display height: 20px.
- Do not apply additional color fills, drop shadows, or stretch.
- Dark UI / marketing: white variants. Light UI: black variants.

---

## 2. Theme parity (dark + light)

**Both themes are first-class.** Dark is the default and historically what the admin UI was designed against, but light theme ships with every release and the operator toggles it from the sidebar footer ([static/css/base.css:112](../packages/admin/evolve_admin/web/static/css/base.css#L112)). Every rule in this guide must work in both themes. If you can't satisfy that with tokens alone, document the light-mode counterpart inline next to the dark one — never just pick one and ship.

### The mechanism

Themes are driven by a `data-theme` attribute on `<html>`. The CSS defines two token blocks at [static/css/base.css:20-56](../packages/admin/evolve_admin/web/static/css/base.css#L20):

```css
:root, [data-theme="dark"] { /* dark token values */ }
[data-theme="light"]       { /* light token values */ }
```

Every component rule should reference tokens (`var(--bg2)`, `var(--text)`, `var(--accent)`, etc.) so it themes automatically. The token system is the parity contract.

### Where parity breaks today

The token system covers surfaces / text / accent correctly. The drift lives in **rules that hardcoded a value** assuming dark mode and never got a light-mode pair:

- **Shadows.** 54+ occurrences of `rgba(0,0,0, 0.x)` shadow alphas tuned for dark backgrounds. On light backgrounds the same shadow reads as a smudge — too dark, wrong contrast. Light theme needs softer shadow alphas (typically `rgba(0,0,0, 0.04-0.10)` and a slight blue tint) or a different elevation strategy (relying on `border` only).
- **Code / pre blocks.** Hardcoded `background: #0a0c0f; color: #a8b5c8` for code blocks (see §9.9) — looks like a black hole inside a white card in light mode. Needs `background: #f6f8fa; color: #24292f` or equivalent in light.
- **Warning banner text.** `color: #c8a85a` (a beige tuned for dark bg) becomes low-contrast on light bg. Needs `var(--yellow)` for the dark text role in light mode, plus a bumped saturation.
- **Panic button.** Hardcodes `#dc2626 / #b91c1c / #ef4444` ([static/css/base.css:628](../packages/admin/evolve_admin/web/static/css/base.css#L628)) which happen to read OK on both themes by luck, but the disabled-state `#6b7280 / #4b5563` is wrong on light. Should reference `var(--red)` + a `var(--red-dark)` pair.
- **Inline status colors** — `#eb4`, `#ff8c42`, `#ff9f43`, `#4ec9b0`, `#d6a82c`, `#c53030` scattered through `.home-session-counter`, `.evo-drawer-counter`, `.cm-grade-D`, `.terminal-header-meta`, and inline error styles ([base.css:1961, 2673, 480, 3106](../packages/admin/evolve_admin/web/static/css/base.css#L1961) plus [index.html:9273](../packages/admin/evolve_admin/web/index.html#L9273) for the inline-error markup) all assume dark. Each needs a light-mode equivalent or refactor to tokens.
- **Tinted overlays.** `rgba(74,222,128,0.15)`-style tints (using the dark theme's hex) stay the same in light mode, so a green badge that's vivid in dark becomes washed out / barely visible against a white card. The fix: define overlay tints as token pairs too (`--green-tint-bg`, `--green-tint-border`) rather than hardcoded rgba.

The audit (2026-06-08) found **only one `[data-theme="light"]` override block in the entire CSS** — the token redefinition at [base.css:38](../packages/admin/evolve_admin/web/static/css/base.css#L38). Every other parity issue is invisible until someone toggles the theme and finds it.

### Rules for theme parity

1. **No hex codes in component CSS.** If you need a color, it's a token. If the token doesn't exist, add it as a pair (dark + light) in §3 first.
2. **Test the toggle.** Before opening any visual PR, toggle the theme button. If anything looks wrong, fix it in the same PR — there is no CI gate for theme parity.
3. **Shadows go through `var(--shadow-*)` tokens.** Available as `--shadow-popover`, `--shadow-modal`, `--shadow-hover`, `--shadow-hero`, `--shadow-drawer` ([static/css/base.css:32-55](../packages/admin/evolve_admin/web/static/css/base.css#L32)) — each defined as a dark/light pair, so referencing the token gives you both-theme parity for free. The Phase 3 sweep (see §14 P0 #1) replaces existing inline `rgba(0,0,0,…)` shadows with these.
4. **Code blocks, terminals, banners, and any other hand-tuned surface need explicit light-mode treatment.** See the per-component rules in §9 — they now carry both-theme values.
5. **Tinted overlays (`rgba(<r,g,b>, alpha)`) drift across themes.** A `rgba(255,107,107,0.10)` red-tint that pops on dark bg disappears on light bg. Either reach for stronger alpha (~0.18) in light, or define a token pair.
6. **Status glows (`box-shadow: 0 0 6px var(--green)`) read very differently in light mode** — what was a soft glow becomes a fuzzy outline. Halve the spread (`0 0 3px`) in light, or drop the glow entirely and rely on the dot itself.
7. **Charts (chart.js) take colors via JS, not CSS.** When you add a chart, read the theme from `document.documentElement.dataset.theme` and pass theme-appropriate colors to the chart config. Don't hardcode `#fff` or `#000`.
8. **Images and SVG.** SVG with `fill="currentColor"` themes for free. SVG with hardcoded `fill="#fff"` (or PNG with baked-in colors) does not — use the logo `--logo-filter` pattern at [static/css/base.css:26](../packages/admin/evolve_admin/web/static/css/base.css#L26) as the reference (`invert(1)` in light theme).

### Practical light-mode opposites cheat-sheet

When you must hand-craft a light value, these are the working substitutes:

| Dark theme idiom | Light theme equivalent |
|---|---|
| Shadow `0 4px 12px rgba(0,0,0,0.30)` (popover) | `0 4px 12px rgba(15,23,42,0.08)` |
| Shadow `0 8px 24px rgba(0,0,0,0.40)` (modal) | `0 8px 24px rgba(15,23,42,0.12)` |
| Shadow `0 2px 10px rgba(0,0,0,0.35)` (card hover) | `0 2px 10px rgba(15,23,42,0.06)` |
| Shadow `0 24px 80px rgba(0,0,0,0.50)` (hero) | `0 24px 80px rgba(15,23,42,0.15)` |
| Code block `bg: #0a0c0f; color: #a8b5c8` | `bg: #f6f8fa; color: #24292f` |
| Status glow `0 0 6px var(--green)` | `0 0 3px var(--green)` |
| Banner text `#c8a85a` | `var(--yellow)` (the light-theme value is already deeper) |
| Disabled button `#6b7280 / #4b5563` | `var(--text3) / var(--border)` |

When in doubt: **token references work in both themes; hex codes don't.**

---

## 3. Color tokens

Tokens are defined at [static/css/base.css:20-56](../packages/admin/evolve_admin/web/static/css/base.css#L20). Both themes ship every token — **never hardcode a hex in a new rule**. Extend the token set instead.

### Dark theme (primary)

| Token | Hex | Use |
|---|---|---|
| `--bg` | `#0B0D10` | Page background |
| `--bg2` | `#12161B` | Cards, panels, sidebar, modal body |
| `--bg3` | `#1A2026` | Hover surface, row-expand, inset |
| `--bg4` | `#222830` | Progress-bar track, deepest inset |
| `--border` | `#1E2530` | 1px dividers, card outlines |
| `--text` | `#E6EDF3` | Primary text |
| `--text2` | `#8B949E` | Labels, secondary text |
| `--text3` | `#5C6773` | Hints, captions, placeholder, disabled |

### Light theme

| Token | Hex |
|---|---|
| `--bg` | `#F4F6F9` |
| `--bg2` | `#FFFFFF` |
| `--bg3` | `#EEF1F6` |
| `--bg4` | `#E2E6EE` |
| `--border` | `#D0D7E3` |
| `--text` | `#0D1117` |
| `--text2` | `#4A5568` |
| `--text3` | `#8896A5` |

### Accent + semantic colors

| Token | Dark | Light | Role |
|---|---|---|---|
| `--accent` | `#7C5CFF` | `#7C5CFF` | **Brand purple.** Primary CTA, active nav, focus ring, Intelligence layer |
| `--green` | `#3DDC84` | `#16A34A` | **Semantic: success / healthy.** Also Operations layer |
| `--yellow` | `#F0B429` | `#C07D00` | **Semantic: warning / degraded.** Reserve for warnings only |
| `--red` | `#FF6B6B` | `#DC2626` | **Semantic: error / critical / destructive** |
| `--blue` `--cyan` | `#4CC9F0` | `#0284C7` | Info, primary-tier badge |
| `--orange` | `#fb923c` | `#EA6C00` | Secondary warning, forge/security-bot badges |
| `--teal` | `#2dd4bf` | `#0D9488` | Autonomous states, teal highlights |
| `--purple` | `#7C5CFF` | `#7C5CFF` | Brand-accent alias of `--accent` |

### Tinted overlay alphas

When you need a colored background tint, reach for an existing alpha rung rather than freelancing:

| Use | Pattern |
|---|---|
| Accent ambient bg | `rgba(124,92,255,0.07-0.10)` |
| Accent active/border | `rgba(124,92,255,0.22-0.30)` |
| Green/red/yellow status bg | `rgba(<r,g,b>,0.10-0.15)` |
| Green/red/yellow status border | `rgba(<r,g,b>,0.30-0.45)` |
| Warning banner bg | `rgba(245,166,35,0.07)` |
| Warning banner border | `rgba(245,166,35,0.25)` |

### Rules for color use

1. **`green / yellow / red` are state colors.** Only use them when the thing IS healthy / warning / critical. Don't decorate categories with them. (`.badge-feature` at [static/css/base.css:571](../packages/admin/evolve_admin/web/static/css/base.css#L571) and `.cost-bot-tile-chip` at [static/css/base.css:457](../packages/admin/evolve_admin/web/static/css/base.css#L457) currently abuse yellow — see §14.)
2. **`--accent` is the only brand color.** One primary CTA per surface. Don't reach for purple to "make it stand out" — use weight/size/spacing instead.
3. **`--orange` and `--teal` are reserves.** Don't introduce a new role for them without checking this doc.
4. **No new hex values in component CSS.** If you need a new shade, add the token here in dark + light and reference it.

---

## 4. Typography

Family loaded at [static/css/base.css:1](../packages/admin/evolve_admin/web/static/css/base.css#L1):

```css
font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
```

Monospace (code, terminals, pre):

```css
font-family: 'SF Mono', 'Fira Mono', 'Consolas', monospace;
```

### Type scale

Body is 14px ([static/css/base.css:62](../packages/admin/evolve_admin/web/static/css/base.css#L62)). All new type goes through these rungs — anything off-scale needs a comment explaining why.

**Canonical rem scale (descriptive of in-use reality).** These are the `rem` rungs in heavy use across the admin SPA and blessed by `tools/ui-style-lint` (i.e. *not* on its `OFF_SCALE_FONT` denylist):

```
0.62 · 0.65 · 0.68 · 0.7 · 0.72 · 0.74 · 0.75 · 0.76 · 0.78 · 0.8 · 0.82 · 0.85 · 0.9 · 0.95 · 1 · 1.1 · 1.3 · 1.4 · 2
```

`0.58rem` is the sidebar/nav micro-caption rung (used only there). `0.62rem` is the **in-content micro rung** — blessed, *not* an error — sitting just below `0.65`. The off-scale values the lint *blocks* are `0.55 / 0.64 / 0.81 / 0.83 / 0.84 / 0.86 / 0.87 / 0.88`; the singleton outliers `0.67 / 0.73 / 0.77` still pass today and are slated for a round-to-nearest cleanup before being added to the denylist.

The role→size table below maps the common roles onto those rungs:

| Role | Size | Weight | Letter-spacing | Example |
|---|---|---|---|---|
| Marketing hero h1 | `clamp(2rem, 5vw, 3.2rem)` | 800 | -0.02em | marketing only |
| Marketing section h2 | `clamp(1.6rem, 3vw, 2.2rem)` | 700 | -0.02em | marketing only |
| Stat / display number | `2rem` (up to `3.5rem`) | 700–800 | -0.02em | `.stat-value` |
| Sub-stat number | `1.4rem` | 700 | — | `.bot-score-num` |
| Admin h1 (page title) | `1.3rem` | 600 | — | [static/css/base.css:326](../packages/admin/evolve_admin/web/static/css/base.css#L326) |
| Nav brand | `1.1rem` | 700 | — | |
| Admin h2 (section title) | `1rem` | 600 | — | [static/css/base.css:327](../packages/admin/evolve_admin/web/static/css/base.css#L327) |
| Body | `0.85rem` | 400 | — | Default in panels |
| Subtitle / caption | `0.83rem` | 400 | — | `.subtitle` |
| Card subtitle / help | `0.84rem` | 400 | — | `.card-subtitle` |
| Small / secondary | `0.75-0.8rem` | 400/600 | — | `.bot-meta`, `.btn-sm` |
| Tiny (status chips) | `0.68rem` | 600 | — | `.badge`, `.sb-badge` |
| Micro uppercase (in-content) | `0.7rem` | 600 | `0.06em` | `.card-title` |
| Micro uppercase (sidebar/nav) | `0.58rem` | 600 | `0.1em` | `.nav-section` |
| Code | `0.82rem` | 400 | — | pre/code blocks |

### Line heights

- Headings: 1.15–1.25
- Body: 1.6
- Secondary text: 1.55
- Code blocks: 1.65

### Typography rules

1. **Two uppercase-label sizes, not three.** `0.7rem + 0.06em tracking` for in-content micro-labels (card titles, section captions). `0.58rem + 0.1em` for sidebar/nav only. Don't introduce a third (today `.botcfg-section-header` at 0.78rem is the outlier).
2. **Don't use `0.83 / 0.84 / 0.86 / 0.88rem`** — these are off the canonical scale (and lint-blocked); round to `0.85rem` (body) or `0.75rem` (small). (`0.82rem` *is* canonical — it's the code-block rung.)
3. **Semantic headings, not styled spans.** `<h2>` for section titles — outline order matters for accessibility and future-you grepping for "where's that section."
4. **Numeric displays max at weight 700.** Everything else maxes at 600 (admin UI). Marketing hero is allowed 800.

---

## 5. Spacing

The codebase has converged on **4 / 6 / 8 / 10 / 12 / 14 / 18 / 22 px**. Treat these as the rungs.

| Token | Value | Use |
|---|---|---|
| xs | 4px | Label↔value stack, badge padding |
| sm | 6px | Tight icon↔text, inline groups |
| md | 8px | Default flex/grid gap, list-item spacing |
| lg | 10px | Inter-section row gaps |
| xl | 12px | Card→card inside a stack, button↔button |
| 2xl | 14px | Grid gap, card stack margin |
| 3xl | 16–18px | Card interior padding |
| 4xl | 22px | Page-section header top margin |
| 5xl | 24–32px | Marketing section padding (horizontal) |
| 6xl | 48–80px | Marketing section padding (vertical) |

### Rules

1. **`padding: 18px` is the standard `.card` interior.** `16px` is acceptable for denser tiles (`.stat-block`, `.insights-panel`). Don't go below 14px unless the tile is < 200px wide.
2. **Don't introduce 7px, 9px, 11px, 13px, 15px, 17px.** Round to the nearest rung.
3. **Card stacks use `margin-top: 14px`** between siblings ([static/css/base.css:333](../packages/admin/evolve_admin/web/static/css/base.css#L333)).
4. **Section→section headers use `margin-top: 22px`** ([static/css/base.css:346](../packages/admin/evolve_admin/web/static/css/base.css#L346)).

---

## 6. Border radius

| Value | Use |
|---|---|
| 3px | Micro chips, score bars, key tags |
| 4px | Standard badges, inline pills |
| 6px | **Form controls** (inputs, selects, buttons, inline alerts) |
| 8px | List rows, modals, stat blocks, toasts |
| 10px | **Cards, panels, tiles** — the default surface |
| 12px | Drawers (when promoted to a class) |
| 20px / 100px | Pill badges, toggle slider |
| 50% | Status dots, avatar circles |

Pick by component family — don't freelance a new radius.

---

## 7. Shadows & elevation

Elevation flows through `bg → bg2 → bg3 → bg4` first, `border` second, `box-shadow` last. Shadows are reserved for floating/overlay surfaces.

**Shadows are theme-sensitive.** Dark-theme shadows use `rgba(0,0,0, 0.3-0.5)` — the same shadow on a light card reads as a smudge. Light theme uses softer alphas (~0.06-0.15) with a slight cool tint (`rgba(15,23,42, …)`). Always pair them:

| Level | Dark theme | Light theme | Use |
|---|---|---|---|
| 0 — page | `var(--bg)` | `var(--bg)` | Page background |
| 1 — surface | `var(--bg2)` + `1px solid var(--border)` | same | Cards, panels, sidebar, tiles |
| 2 — hover / inset | `var(--bg3)` | same | Hovered rows, expanded body |
| 3 — popover | bg2 + border + `0 4px 12px rgba(0,0,0,0.30)` | bg2 + border + `0 4px 12px rgba(15,23,42,0.08)` | Dropdown menu, tooltip |
| 4 — modal | bg2 + border + `0 8px 24-32px rgba(0,0,0,0.35-0.45)` | bg2 + border + `0 8px 24-32px rgba(15,23,42,0.10-0.15)` | Modal dialog, evo drawer |
| 5 — hero | `0 24px 80px rgba(0,0,0,0.50)` | `0 24px 80px rgba(15,23,42,0.15)` | Marketing hero screenshot only |
| Card hover | `0 2px 10px rgba(0,0,0,0.35)` | `0 2px 10px rgba(15,23,42,0.06)` | Interactive tiles on `:hover` |
| Focus ring | `0 0 0 2px var(--accent)` | same | Active form control, focused button |
| Status glow (green) | `0 0 6px var(--green)` | `0 0 3px var(--green)` | Live/healthy dot — light theme halves spread to avoid a fuzzy halo |
| Status glow (red) | `0 0 6px var(--red)` | `0 0 3px var(--red)` | Critical/down dot |
| Status glow (accent) | `0 0 6px var(--accent)` | `0 0 3px var(--accent)` | Active/selected indicator |

When you write a new shadowed component, write both rules:

```css
.popover {
  background: var(--bg2);
  border: 1px solid var(--border);
  box-shadow: 0 4px 12px rgba(0,0,0,0.30);
}
[data-theme="light"] .popover {
  box-shadow: 0 4px 12px rgba(15,23,42,0.08);
}
```

**Token shortcut.** Use the shadow tokens (`var(--shadow-popover)`, `var(--shadow-modal)`, `var(--shadow-hover)`, `var(--shadow-hero)`, `var(--shadow-drawer)`) instead of hand-writing the rgba alpha — they're defined as dark/light pairs at [static/css/base.css:32-55](../packages/admin/evolve_admin/web/static/css/base.css#L32) and theme automatically.

**Rule.** Don't apply hover shadows to non-interactive cards. Interactive tiles use the pattern at [static/css/base.css:452](../packages/admin/evolve_admin/web/static/css/base.css#L452) (thin purple ring + soft shadow on `:hover`) — verify it in both themes.

---

## 8. Layer color coding

Two operational layers carry consistent color treatment across the UI and marketing site:

| Layer | Color | Token |
|---|---|---|
| Operations | Green | `--green` / `#3DDC84` |
| Intelligence | Purple | `--accent` / `#7C5CFF` |

Apply the layer color to: section labels, pillar card layer tags, nav section headings, status badges that distinguish layer.

---

## 9. Components

### 9.1 Buttons

Canonical classes at [static/css/base.css:613-640](../packages/admin/evolve_admin/web/static/css/base.css#L613).

| Class | Use |
|---|---|
| `.btn` | Base — don't use alone, always pair with a variant |
| `.btn-primary` | The single primary action on a surface |
| `.btn-ghost` | Secondary action, dismissals |
| `.btn-green` | Approve / confirm-positive |
| `.btn-warning` | Reversible-but-noisy action (Disconnect, Detach). Same visual weight as `.btn-danger` / `.btn-green` so adjacent action clusters read as a row. Added in #2415. |
| `.btn-danger` | Destructive (delete, disable) |
| `.btn-panic` | Emergency stop — only for the literal panic button |
| `.btn-resume` | Pairs with `.btn-panic` |
| `.btn-sm` | Size modifier — stacks with any color variant |

**Primary**

```css
background: var(--accent); color: #fff;
padding: 7px 14px; border-radius: 6px;
font-size: 0.8rem; font-weight: 600;
/* hover: opacity 0.88 */
```

**Ghost / secondary**

```css
background: var(--bg2); color: var(--text);
border: 1px solid var(--border);
padding: 7px 14px; border-radius: 6px;
/* hover: border-color var(--accent) */
```

**Danger**

```css
background: rgba(255,107,107,0.10); color: var(--red);
border: 1px solid rgba(255,107,107,0.30);
/* hover: background rgba(255,107,107,0.20) */
```

**Button rules**

1. **One primary per surface.** Two `.btn-primary` in the same panel → demote one to `.btn-ghost`.
2. **Buttons are for actions** (do this thing); **links are for navigation** (go to this URL). If clicking it changes the URL and that's the whole job, use a link.
3. **Don't restyle `<span>` as a button.** If it acts like a button it's a `<button>` — accessibility and keyboard navigation depend on it.
4. **Minimum touch target 44 × 44px on mobile.** Default `.btn` padding (7×14) is below that — wrap in `min-height: 44px` or bump padding inside `@media (max-width: 480px)`.
5. **`.err-action-btn.primary` ([static/css/base.css:1503](../packages/admin/evolve_admin/web/static/css/base.css#L1503)) is a parallel implementation of "primary"** that fights `.btn-primary`. Deprecate (see §14).

### 9.1a Links

A global rule colors every bare anchor so it never inherits the unreadable browser-default blue:

```css
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
```

Element-selector specificity `(0,0,1)` means any anchor styled by a class that sets its own color (`.btn-*`, `.nav-item`, `.sib-cta`, `.hb-link`, `.pod-switcher-row`) is unaffected.

For a **non-anchor click target that behaves like a link** (a `<span>`/`<div>` that navigates or reveals), use the `.link` utility instead of hardcoding `color:` inline or abusing `.btn`:

```css
.link { color: var(--accent); cursor: pointer; text-decoration: none; }
.link:hover { text-decoration: underline; }
```

It mirrors the `a{}` rule exactly, so a span-link reads identically to an anchor. Per §9.1 rule 2/3: if it navigates it's a link (`<a>` or `.link`); if it acts it's a `<button>`. A genuinely interactive non-anchor still needs `role` / `tabindex` for keyboard access — `.link` provides styling, not semantics.

### 9.2 Forms

There is no global `.input` / `.select` / `.textarea` class today. New work should add one. As of **Phase 2c** the form rule at [static/css/base.css:1158](../packages/admin/evolve_admin/web/static/css/base.css#L1158) no longer sets `width: 100%` — that property now lives on the `.input-w-*` utilities ([static/css/base.css:1170](../packages/admin/evolve_admin/web/static/css/base.css#L1170)). **Every new `<input>` / `<select>` / `<textarea>` must carry a width utility** (below); a control with none falls back to its intrinsic width rather than spanning the page.

**Input width bands.** Width follows data shape, not the surrounding column:

| Field shape | Max width |
|---|---|
| Checkbox, single-digit numeric | `48px` |
| Numeric ≤ 4 digits, code, port, abbreviation | `80px` |
| Short string (slug, label) | `160px` |
| Standard input (name, title) | `320px` |
| Long string (URL, single-line description) | `480px` |
| Textarea (multiline) | `600px` |
| Full-width — only when | the input semantically owns the row (search bar, single-input form, body editor) |

Apply via a utility set (`.input-w-sm` / `-md` / `-lg`) or inline `style="max-width:…"`. **Never let a numeric "Duration: 30" dropdown stretch 800px wide.**

**Selects.** A `<select>` should never span the full content width unless the option labels are long AND the list is ≥ 20 options. The filter pattern at [static/css/base.css:409](../packages/admin/evolve_admin/web/static/css/base.css#L409) (`width: auto; min-width: 130px`) is the right default — promote it to a `.form-select` utility.

**Labels.**
- Labels go above input, in `--text2`, 0.75rem.
- Required marker is `*` in `--accent`, not red. Red is for errors.

**Toggles.** Use `.toggle` ([static/css/base.css:1023](../packages/admin/evolve_admin/web/static/css/base.css#L1023)) for boolean settings. Don't restyle a checkbox ad hoc.

### 9.3 Cards & panels

| Class | Use |
|---|---|
| `.card` | General page-section container (padding 18) |
| `.stat-block` | Single metric: big number + label + sub (padding 16) |
| `.insights-panel` | Bulleted findings, recommendations (padding 16) |
| `.bot-row` | Horizontal list entry with status + meta + actions |
| `.cost-bot-tile` / `.cm-bot-tile` | Per-bot grid tile (interactive, padding 10-12) |
| `.botcfg-card` | Bot-config section (inherits `.card`) |

Base pattern:

```css
background: var(--bg2);
border: 1px solid var(--border);
border-radius: 10px;
padding: 18px;
/* hover (interactive only): border-color: rgba(124,92,255,0.40) */
```

**Card rules.**
1. Each card has *either* a `.card-title` (uppercase micro-label) *or* a real `<h2>` — not both at the same hierarchy level.
2. Padding hierarchy: 18 (full card) → 16 (stat block, insights, dense card) → 10–12 (small tile). Nothing in between.
3. Don't add hover shadow to non-interactive cards.
4. `.card + .card { margin-top: 14px }` is a **flow** stacking margin. Inside a grid or flex container the `gap` already does that job, and the margin drops every card but the first — under `align-items: stretch` that reads as cards of *different heights*, not as a shifted card. Grid/flex card containers reset it (`.grid > .card + .card`, `.pi-grid > .card + .card`); a new card container that isn't `.grid` must do the same. Note the reset has to be written on the *container*: a `0,1,0` rule on the child (`.pi-module { margin-top: 0 }`) loses to the `0,2,0` of `.card + .card` and never fires.

### 9.4 Tables

Use `.resp-table-wrap > .resp-table` ([static/css/base.css:756-882](../packages/admin/evolve_admin/web/static/css/base.css#L756)). It handles three viewport modes:

- **Desktop (≥ 768px):** standard table.
- **Tablet (481-767px):** horizontal scroll, first column sticky with a right-edge fade.
- **Phone (< 480px):** card-stack mode — each row becomes a vertical block, cells render `label: value` driven by `data-label`.

**Table rules.**
1. `data-label="…"` on every `<td>` is **required** for card-stack mode to render labels.
2. Use `<td class="resp-table-fullspan" colspan="…">` for expansion rows (skips the label/value layout).
3. Long-string cells get `max-width: 240px; overflow: hidden; text-overflow: ellipsis`. URLs and stack traces should never blow the column width.
4. Use `.resp-table-dense` to force card-stack at < 768px (instead of < 480px) for space-constrained tables.
5. No sticky headers today. If you add one, put it on `.resp-table-wrap thead th` so all tables get it.

### 9.5 Badges, chips, status indicators

**Five primitives exist today** (`.badge`, `.sb-badge`, `.cm-chip`, `.gtag-chip`, `.recovery-pill`) doing roughly the same job with inconsistent padding/radius. Going forward, use this matrix:

| Use | Class | Notes |
|---|---|---|
| Status label (ok/warn/crit) | `.badge.badge-ok` / `.badge-warn` / `.badge-crit` | Authoritative |
| Category label (non-semantic) | `.badge.badge-feature` (neutral variant) | Don't use yellow for "category" |
| Status dot | `.dot.dot-green/red/yellow/gray` | 8×8 circle, glow on `.dot-green` |
| Numeric pill (counts) | `.nav-badge` shape — red bg, small min-width | |
| Tag chip (gallery) | `.gtag-chip` | Canonical / suite / freeform variants |
| Recovery state | `.recovery-pill.ok/.bad/.dim` | Will fold into `.badge` over time |

Base badge:

```css
padding: 2px 7px;
border-radius: 4px;
font-size: 0.68rem;
font-weight: 600;
```

Variants (background / color / border):

| Variant | bg | text | border |
|---|---|---|---|
| OK / healthy | `rgba(61,220,132,0.12)` | `#3DDC84` | `rgba(61,220,132,0.30)` |
| Warning | `rgba(240,180,41,0.12)` | `#F0B429` | `rgba(240,180,41,0.30)` |
| Critical | `rgba(255,107,107,0.12)` | `#FF6B6B` | `rgba(255,107,107,0.30)` |
| Accent / feature | `rgba(124,92,255,0.12)` | `#7C5CFF` | `rgba(124,92,255,0.30)` |
| Alpha / status | `rgba(245,166,35,0.12)` | `#F5A623` | `rgba(245,166,35,0.30)` |

**Status dot:**

```css
width: 8px; height: 8px; border-radius: 50%;
/* .dot-green: background var(--green); box-shadow: 0 0 6px var(--green) */
/* .dot-red:   background var(--red) */
/* .dot-gray:  background var(--text3) */
/* .dot-yellow: background var(--yellow) */
```

### 9.6 Modals & drawers

| Pattern | Class | Max width |
|---|---|---|
| Confirmation | `.modal` | 380px |
| Standard modal | `.modal` | 520px (phone: 95vw) |
| Wide modal (form / table) | `.modal.modal-wide` | 720px |
| Side drawer (new work) | `.drawer.drawer-right` | docks on desktop ≥ 1280px with `.docked` modifier |
| Side drawer (legacy ID-based) | `#evo-drawer`, `#turndetail-drawer`, `#errlog-drawer`, `#cm-rec-suggestions-drawer` | each carries its own width, z-index, and show/hide pattern — migrate opportunistically per the playbook below |

**Modal rules.**
1. **Three widths, not five.** Collapse today's 520 / 560 / 680 / uncapped variants ([base.css:720, 1551, 2919](../packages/admin/evolve_admin/web/static/css/base.css#L1551)) into the table above.
2. Every modal closes via **all three**: X button in top-right, overlay click, `Esc` key.
3. Don't open a modal for an action that fits in a popover. One input + one button = inline reveal, not modal.
4. Modal title is `<h2>` at `1rem / 600`.
5. **Never use native `confirm()` / `alert()`.** They are silently suppressed in the desktop Evolve Pods app (Tauri v2 + wry webview has no script-dialog delegate), so a destructive action gated behind `if (!confirm(msg)) return;` becomes a silent no-op and an error surfaced only via `alert()` vanishes — and native dialogs are unthemed besides. Use the global helpers in [core/dom-utils.js](../packages/admin/evolve_admin/web/static/js/core/dom-utils.js): `await confirmModal(opts)` (async, returns a `Promise<boolean>`; pass `{body, title, confirmLabel, danger}` — `danger:true` for delete/revoke/reset) replaces `confirm()`, and `toast(msg, 'err'|'ok')` replaces `alert()`. `ui-style-lint` flags new native calls (warn-tier `native-dialog`).

**Drawer rules.**
1. Side drawers use `12px` border-radius on the inner-edge corners, full-bleed on the outer edge.
2. Drawer shadow: `var(--shadow-drawer)` — themes automatically per §7.
3. On phone/tablet, drawer is fullscreen overlay. On desktop ≥ 1280px, drawer docks alongside main content (`.main` gets right padding via the `.docked` modifier on `.drawer-right`).

**Drawer migration playbook** (for the legacy ID-based drawers above):

When you touch one of the legacy drawers for an unrelated reason — adding content, fixing a bug, restyling the header — consider migrating it as part of that PR. The path:

1. **HTML:** add `class="drawer drawer-right"` next to the existing `id="…-drawer"`. Keep the ID so existing `getElementById` calls still target it.
2. **CSS:** swap `width: <NNN>px` to a custom modifier (`.drawer-wide { width: 640px }`) or fall back to the default 380. Drop the inline `position: fixed`, `border-left`, `z-index`, `box-shadow` declarations — `.drawer-right` provides them.
3. **Show/hide:** if the legacy drawer used `display: none` ↔ `display: flex`, switch to the `.drawer-right.open` transform pattern (drawer slides in instead of pop-appearing). UX is nicer; verify no JS depends on `display: none` to hide children.
4. **JS:** `getElementById('…-drawer').classList.add('open')` keeps working (the class state is the same).
5. **Test:** open the drawer in dark + light, on phone + desktop, and confirm the close handlers still fire.

The four legacy drawers each have specific quirks (`#turndetail-drawer` is 640px wide, `#errlog-drawer` is a corner pop-up, `#cm-rec-suggestions-drawer` is a bottom sheet, `#evo-drawer` is 420px with a complex header/composer/thread layout) — none MUST migrate today, but each one can land cleanly when its turn comes.

### 9.7 Warning / alpha banner

**Dark theme:**

```css
.banner-warn {
  background: rgba(245,166,35,0.07);
  border: 1px solid rgba(245,166,35,0.25);
  border-radius: 10px;
  padding: 1.25rem 1.5rem;
  color: #c8a85a;
}
.banner-warn strong { color: var(--yellow); }
```

**Light theme override:**

```css
[data-theme="light"] .banner-warn {
  background: rgba(192,125,0,0.06);
  border-color: rgba(192,125,0,0.30);
  color: var(--yellow);            /* token already deeper in light theme */
}
[data-theme="light"] .banner-warn strong { color: #8a5a00; }
```

The dark-theme beige `#c8a85a` becomes low-contrast on a white background. Use the deeper light-theme `--yellow` value, and a darker amber for strong text.

Reserve banners for product-stage callouts ("Alpha", "Beta", "Experimental"). Don't use the warning banner for transient alerts — those are `.alert.alert-warn` or `#toast.warn`.

### 9.8 Section labels (uppercase caps)

Two flavors:

```css
/* In-content section label */
font-size: 0.7rem; font-weight: 600;
letter-spacing: 0.06em; text-transform: uppercase;
color: var(--text2);    /* or var(--accent) for Intelligence, var(--green) for Ops */

/* Sidebar / nav section caption */
font-size: 0.58rem; font-weight: 600;
letter-spacing: 0.10em; text-transform: uppercase;
color: var(--text3);
```

### 9.9 Code / pre blocks

**Dark theme:**

```css
pre, .code-block {
  background: #0a0c0f;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1.25rem 1.5rem;
  font-family: 'SF Mono', 'Fira Mono', monospace;
  font-size: 0.82rem;
  line-height: 1.65;
  color: #a8b5c8;
  overflow-x: auto;
}
```

**Light theme override:**

```css
[data-theme="light"] pre,
[data-theme="light"] .code-block {
  background: #f6f8fa;
  color: #24292f;
}
```

The dark code-block surface (`#0a0c0f`) is darker than the page bg so it reads as "inset". On light theme the equivalent is a light-gray slab (`#f6f8fa`) one shade darker than the white card. GitHub's light-theme code colors (`#f6f8fa` bg, `#24292f` text) are the field-tested reference.

When syntax highlighting kicks in, swap the highlighter theme on theme toggle (Prism / Highlight.js both ship light + dark CSS).

### 9.10 Navigation

- **Sidebar** — `.sidebar`, 220px wide, sticky above 980px ([static/css/base.css:100](../packages/admin/evolve_admin/web/static/css/base.css#L100)). Below 980px, collapses behind a hamburger and slides as a drawer.
- **Nav item** — `.nav-item`. Active state = accent color + 2px left border + `rgba(124,92,255,0.07)` background tint.
- **Tabs** — `.subtabs > .subtab` with overflow-scrolling and `.subtab-more-cluster` for overflow items. Don't reinvent.
- **Pagination** — `.sb-pager` (prev/next + page counter, 0.8rem). Bump button padding to hit the 32px target.
- **Breadcrumbs** — not in the system today. If we need one, it lives between the topbar and the first card on a sub-page.

### 9.11 Empty, loading, error states

| State | Class | Pattern |
|---|---|---|
| Empty inline | `.empty` | text3, centered, 20px padding |
| Empty full panel | `.empty-state-card` | dashed border, 32px padding, centered |
| Loading | `.loading` + `.spinner` | 16×16 accent spinner + label, inline-flex |
| Inline alert | `.alert.alert-{ok,warn,error}` | tinted bg + matching border + 0.82rem text |
| Toast (transient) | `#toast` | Fixed bottom-right, 24px inset, slides up on `.show` |
| Skeleton loader | `.skeleton` | bg3 with `border-radius: inherit` + slow pulse; honors `prefers-reduced-motion` |

**Rule.** Empty states always carry one verb (button or link) explaining the next action. "No bots yet" alone is failure — "No bots yet. Add one →" is right.

### 9.12 Icons

Today the sidebar mixes emoji (🗨), Unicode block symbols (▦ ▥ ⊕), and other glyphs ([index.html:3241](../packages/admin/evolve_admin/web/index.html#L3241)). **Pick one system and migrate.**

Standard:
- Inline SVG, sized `16×16` (or `14×14` in dense rows).
- `fill="currentColor"` so they inherit text color and theme toggle works for free.
- Same icon for the same concept across surfaces.

Don't use emoji as functional icons (they render differently per OS, don't theme, can't be sized consistently). Emoji is fine in user-generated content and friendly banners only.

### 9.13 Expand/collapse affordance

The canonical "this row/section can be expanded" indicator is the `.expand-icon` utility — a 14×14 SVG chevron with `stroke="currentColor"` so it inherits the surrounding text color and themes for free. The icon rotates 90° (chevron-right → chevron-down) when the section opens.

Replaces an earlier ad-hoc mix of Unicode glyphs (▸ ▾ ▲ ▼ ▶ ⟩) rendered at 0.7rem — the Unicode triangles came out ≤8px wide on most surfaces and operators couldn't read direction at a glance.

**Standard markup:**

```html
<span class="expand-icon" aria-hidden="true">
  <svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
</span>
```

**Wiring patterns:**

| Pattern | How the icon rotates |
|---|---|
| `<details><summary>` | Automatic — `details[open] > summary .expand-icon` rotates 90° |
| Click-toggled JS state | Add/remove `.is-open` class on the `.expand-icon` element |
| Parent state class (e.g. `.home-tile-host.collapsed`) | Custom CSS rule like `.home-tile-host:not(.collapsed) .home-host-caret { transform: rotate(90deg); }` |

**Default state convention:** the SVG paints as chevron-right (closed). Sections that *default to expanded* (e.g. the host tile, the current-report card) keep the icon rotated via the parent-state CSS rule above.

**Rules.**
1. **Use `.expand-icon` for expand/collapse only** — sections that open more content. NOT for menu dropdown indicators (the `▾` on a "More" or "Snooze all" button is a separate convention; leave those as Unicode).
2. **Don't use it for direction-of-trend indicators** (`▲ +5%` / `▼ −5%` on cost tiles) — those convey direction, not collapse state.
3. **Hide the native `<details>` disclosure marker** when you add `.expand-icon` to a `<summary>` — base.css already handles this via `summary:has(.expand-icon)`, so you don't need to add `list-style: none` inline.
4. **`aria-hidden="true"` on the wrapper span** — the icon is decorative; the summary text carries the accessible label.

### 9.14 Collapsible card

A **`.collapsible-card`** is a `.card` whose body folds away behind its header. Built on a native `<details>`/`<summary>` plus the §9.13 `.expand-icon` chevron — so keyboard toggle (Space/Enter on the header), focus, and the chevron rotation all come for free. The header (chevron + title) stays visible when collapsed; only the body hides.

Use it for **always-shown explainer / help / reference cards that a returning operator wants out of the way** — "How it works" pipeline cards, onboarding hints, reference legends. The explainer is valuable to a new user and clutter to an experienced one; collapsible-with-memory serves both. **Not** for transient content (a card that's only shown contextually, a results panel, anything whose visibility is already driven by state) — that's not what the persistence is for.

**Markup:**

```html
<details class="card collapsible-card" data-collapse-id="how-it-works-recommendations" open>
  <summary class="collapsible-card-head">
    <span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
    <span class="card-title">How it works</span>
  </summary>
  <div class="collapsible-card-body">
    …existing card contents…
  </div>
</details>
```

- `.card` keeps the surface (bg/border/radius); `.collapsible-card` (qualified `details.collapsible-card` in CSS so it also beats the `.card` padding rule inside the ≤480px block) hands interior padding to the head and body so the collapsed state stays a tidy padded bar.
- The title is a `<span class="card-title">` inside the `<summary>` so it stays visible when collapsed.
- The header is a ≥44px touch target on mobile (18px vertical padding + the icon/title row).

**Persistence contract (the point of the primitive).** Every collapsible card carries a stable **`data-collapse-id`**. `static/js/core/collapsible.js` wires each one on load and writes the open/closed choice to `localStorage` under **`evolve.collapse.<data-collapse-id>`** on the native `toggle` event:

- **Default = expanded.** Render the `<details>` with the `open` attribute. New users see the explainer.
- **Either default is supported, and storage is authoritative for the user's explicit choice.** The helper *removes* `open` when storage says `"closed"` and *adds* `open` when storage says `"open"`; absence leaves the markup default. So a default-expanded card stays collapsed across visits only if the user collapsed it, and a **default-collapsed** card (render WITHOUT `open`) stays open across visits only if the user opened it. The Overview "Updates" detail region (`#ov-updates-detail`) uses the default-collapsed variant — toggled from a summary-stat cell, body-only when collapsed, so it costs zero rows in steady state.
- IDs are independent — collapsing one doesn't touch another.
- The wiring is `addEventListener('toggle')` (no inline `onclick`, no window-export needed) and idempotent — `window.wireCollapsibleCards(root)` is exposed for re-running the pass after a dynamic re-render, though static markup needs no call. Storage access is wrapped so a blocked `localStorage` (Safari private mode) degrades to non-persistent rather than throwing.

Cross-reference §9.13 for the chevron. The persistence convention here is the SPA's only "remember my view choice" mechanism — reuse it rather than inventing a parallel one.

---

## 10. UI principles

### 10.1 Buttons vs. dropdowns vs. tabs

| Pattern | Use when |
|---|---|
| **Button row (segmented)** | 2–3 mutually exclusive options, short labels, all values equally common |
| **Dropdown (`<select>`)** | 4+ options, OR variable-length list, OR one option is overwhelmingly common (default) and the rest are rare |
| **Toggle switch** | Exactly 2 values and one of them is "off / disabled" |
| **Radio group** | ≤ 4 options AND you want all visible AND each needs explanatory text |
| **Tab strip** (`.subtabs`) | Switching between **views of the same content**, not picking an input value |

A button row of 4+ siblings (e.g. the home tier selector at [index.html:3549-3566](../packages/admin/evolve_admin/web/index.html#L3549)) becomes a 4-wide button wall on mobile. Switch to `<select>` below 480px, or just `<select>` everywhere if labels are long.

### 10.2 Form input widths

> *"Do not have drop downs that span a page when it only needs to be a certain width."*

There is no longer a global `width: 100%` on form controls (removed in **Phase 2c**; the form rule is now at [static/css/base.css:1158](../packages/admin/evolve_admin/web/static/css/base.css#L1158)) — that property is carried by the `.input-w-*` utilities instead, so width follows **data shape**, not the column the input happens to sit in. See §9.2 for the width band table. Default to narrow; widen only when the data actually needs it.

### 10.3 Color semantics

- `green` = success / healthy. `yellow` = warning / degraded. `red` = error / destructive / critical.
- **Don't decorate with semantic color.** A category label that happens to be styled yellow ([static/css/base.css:571](../packages/admin/evolve_admin/web/static/css/base.css#L571), [static/css/base.css:457](../packages/admin/evolve_admin/web/static/css/base.css#L457)) trains operators to ignore the actual warnings.
- `accent` (purple) is for the brand and the single primary action — not for "draw attention" generally. Use weight, size, and spacing to draw attention; reserve color for status.

### 10.4 One primary action per surface

Per page, per card, per modal: one `.btn-primary`. If you have two, demote the secondary one to `.btn-ghost`. If both are equally important, the page hierarchy is wrong — pick.

### 10.5 Touch targets

Anything clickable on mobile must hit at least **44 × 44px**. The top-bar controls already do this ([static/css/base.css:265](../packages/admin/evolve_admin/web/static/css/base.css#L265)). The default `.btn` (7px×14px padding ≈ 30px tall) and `.nav-item` (9px×16px ≈ 34px tall) don't. Bump them inside `@media (max-width: 480px)`.

### 10.6 Content readability

Long-form paragraph text inside a card should cap at `max-width: 64ch` (≈ 600px). At desktop widths a card description spanning 1400px is unreadable. Use the `.card-prose` utility ([static/css/base.css](../packages/admin/evolve_admin/web/static/css/base.css), near `.card-subtitle`) on the prose/text element — not on a card that also holds a diagram, table, or button row (those need the full width). For a single-purpose prose banner (e.g. `.explainer-banner`) the banner *is* the text element, so the class goes on the banner itself.

Similarly: table cells holding long strings (URLs, descriptions, stack traces) cap at `max-width: 240px` with ellipsis overflow. Reveal full content on hover (tooltip) or row-expand.

### 10.7 Spacing rhythm

Within a page, the gap between siblings should be **consistent for siblings of the same type**.

- Cards in a stack: 14px between each (don't mix 14 / 18 / 22).
- Section groups (group of cards → group of cards): 22px between groups.
- Form rows within a card: 10–12px.
- Items in a list-row component: 8px.

Mixed rhythm (some cards at 14, others at 18) reads as "broken layout" even when nothing is.

### 10.8 Theme-test everything

Both themes are first-class — dark and light ship in every release and operators toggle between them. **Before opening any visual PR, click the theme button in the sidebar footer and verify the change in both modes.** There is no CI gate for theme parity — silent breakage stays broken until someone toggles and notices.

Typical light-theme regressions to look for:
- A surface that was `var(--bg2)` (themes correctly) sitting next to a manual `#0a0c0f` background (stays dark in light mode → looks like a hole).
- A shadow tuned with `rgba(0,0,0,0.4)` (correct on dark bg, smudge on light bg).
- A green/red/yellow tinted overlay at `rgba(…,0.10)` alpha that pops on dark but disappears on light. Bump to ~0.18 or define a token pair.
- White text (`#fff`) on a colored chip that becomes invisible when the chip background flips lighter in light theme.
- A status glow `box-shadow: 0 0 6px var(--green)` that becomes a fuzzy halo on white — halve the spread in light theme.

See §2 for the full theme-parity rules and the dark-↔-light cheat-sheet.

### 10.9 Responsive-test everything

Three viewports: phone (< 480), tablet (481–767), laptop+ (≥ 768). Chrome DevTools device emulation hits all three quickly. The sidebar drawer transitions at 980 — also test 850 and 1100.

### 10.10 No new tokens without a doc update

If you reach for a new color, a new font size, a new radius, a new spacing value — pause. Either an existing token fits, or you're introducing system drift. If it's truly new, add it to this doc in the same PR.

### 10.11 Match dedicated tools to dedicated UIs

When a control deserves its own visual treatment (panic button, primary CTA on the marketing site, the evo drawer), give it dedicated styling and document it here. When it doesn't, reuse the existing class — the strongest signal of "this is just another card" is that it looks like every other card.

---

## 11. Layout & responsive

### 11.1 Breakpoints

Declared at [static/css/base.css:2-19](../packages/admin/evolve_admin/web/static/css/base.css#L2). These are the canonical break points — don't add 600px / 900px / 1024px ad hoc.

| Name | Width | Behavior |
|---|---|---|
| Phone | < 480px | Single-column. Card-stack tables. Tighter padding. Sidebar drawer. |
| Tablet | 480-768px | Tables horizontal-scroll with sticky first column. Sidebar drawer. |
| Sidebar dock cutoff | 980px | Above: sidebar sticky. Below: hamburger drawer. |
| Laptop | 768-1280px | Full tables. Sidebar pinned. |
| Desktop | ≥ 1280px | Evo drawer docks alongside main. Full-width chrome. |

Note: the sidebar drawer cutoff (980px) is **different** from the laptop breakpoint (768px). This is intentional — at 850px the sidebar would crowd the main content. Don't "unify" these without rethinking the whole responsive system.

### 11.2 Page padding

- Desktop `.main`: `14px 32px` ([static/css/base.css:321](../packages/admin/evolve_admin/web/static/css/base.css#L321)).
- Phone `.main`: `18px 14px` ([static/css/base.css:312](../packages/admin/evolve_admin/web/static/css/base.css#L312)).

New pages live inside `.main` and inherit these.

### 11.3 Content widths

- Sidebar (desktop): 220px fixed.
- Max content (marketing): 1100px.
- Max content (marketing sections): 1000px.
- Max content (marketing hero / text-heavy blocks): 800px.
- Max content (paragraph text in admin cards): 64ch (~600px) — apply via the `.card-prose` utility.
- Max content (long-string table cells): 240px with ellipsis.

### 11.4 Horizontal overflow

The `html, body { overflow-x: hidden; width: 100vw }` rule at [static/css/base.css:79](../packages/admin/evolve_admin/web/static/css/base.css#L79) is a safety net for iOS Safari, **not the design**. If your component genuinely needs horizontal scroll (wide table, code block, chart), wrap *it* in `overflow-x: auto`. Don't rely on the global rule to clip your bug.

**Grid tracks won't shrink past an item's transferred minimum.** `1fr` is `minmax(auto, 1fr)`, and `auto` resolves to the item's automatic minimum size. On a box with `aspect-ratio`, a `min-height` is *transferred through the ratio into a minimum width* — so `.pi-day { aspect-ratio: 1; min-height: 10px }` gave every strip cell a 10px width floor, and `repeat(28, 1fr)` overflowed its card instead of shrinking. If a grid item must shrink below its content or ratio minimum, write `minmax(0, 1fr)` on the track **and** `min-width: 0` on the item; for a mark that has to survive a narrow column, a fixed `height` is usually a better fit than `aspect-ratio` + `min-height`.

---

## 12. Animation

| Use | Value |
|---|---|
| Default UI transition | `0.15s ease` |
| Hover color change | `0.12s ease` |
| Expand / collapse | `0.3s ease` |
| Slow pulse | `0.6s ease-in-out` |
| Spinner (loading) | `1s linear infinite` |

Named keyframes used:
- `pulse-dot` — breathing glow on status indicators
- `pod-pulse` — ring pulse on active pod nodes
- `edge-flow` — animated connection lines
- `pulse-warn` — warning state pulse

**Rule.** No transition longer than 300ms on functional UI. Marketing animations (hero, scroll-reveal) can be slower.

---

## 13. Marketing site

The marketing page (`docs/gitpages/index.html`) uses the same color system as the admin UI with these additions:

- Hero `h1` uses fluid type: `clamp(2rem, 5vw, 3.2rem)`.
- Screenshot wrapper carries a stronger shadow: `0 24px 80px rgba(0,0,0,0.5)`.
- Nav is sticky with `backdrop-filter: blur(8px)` and semi-transparent bg.
- Pill badges use `border-radius: 100px`.
- The "Alpha" badge uses the warning color scheme (amber/orange).

**Brand-purple alignment** (reconciled in Phase 5): the marketing site previously used `--accent: #7c6af7` (lighter); now matches the admin's canonical `#7C5CFF`. The accompanying `rgba(124, 106, 247, …)` inline tints were also swept to `rgba(124, 92, 255, …)`. One commented-out reference to the old hex remains in the gitpages CSS as a history note.

---

## 14. Known issues & prioritized recommendations

These are the biggest existing inconsistencies the audit (2026-06-08) surfaced. Tackle top-down.

### Rollout status

- ✅ **Phase 1 — primitives** (landed 2026-06-08). Shadow token pairs, input-width utilities (`.input-w-xs/-sm/-md/-lg/-xl/-text/-auto/-full`), `.badge-sm`, `.badge-neutral`, `.modal-narrow`, `.modal-wide`, `.drawer / .drawer-right`, `.skeleton`. Pure additions, no callsites touched.
- 🟡 Phase 2 — form-width sweep. **2a ✅:** 5 canary dropdowns (breaker, analytics, capability filters). **2b ✅:** broader sweep — ~48 selects + numeric inputs across arbiter, history, observations, config, recovery, hook, plugin, MCP, reports, self-improvement, backup, cost, users. **2c:** scope-down or remove the global `width: 100%` once the remaining handful (single-input modals already opting into `width:100%` inline) are explicit.
- ✅ Phase 3 — light-theme parity sweep. Migrated 15 inline `rgba(0,0,0,…)` shadows to `var(--shadow-*)` tokens; 4 custom-offset shadows (`.sidebar` mobile drawer, `#errlog-drawer`, `.home-rail.mobile-open`, `#evo-fab`) carry hand-tuned `[data-theme="light"]` overrides at the bottom of base.css; 11 hex-color overrides for `.btn-warning` / `.btn-panic:disabled` / `.cm-grade-D` / `.home-session-counter` / `.evo-drawer-counter` / `.home-msg-warn` / `.terminal-header-meta` so they read against white surfaces.
- ✅ Phase 4 — sidebar SVG icon migration. 22 Lucide-style inline SVGs sized 16×16, `stroke="currentColor" fill="none"`. New `.nav-icon` CSS rule in base.css sets stroke-width:2 + round joins/caps. Icons inherit the parent `.nav-item` text color so they theme for free with both dark and light. Replaces the previous emoji/Unicode-block/glyph mix (🗨 / ▦ / ⊕ / ⌘ / ⛨ / etc.) that rendered inconsistently across OSes.
- 🟡 Phase 5 — small consolidation PRs. **✅:** `.badge-feature` yellow → neutral, modal width family collapsed to 380/520/720 (`.pod-rollback-modal` 560→520, `#rel-detail-box` 680→720), mobile 44px min-height for `.btn` + `.nav-item`, marketing-site accent purple `#7c6af7`→`#7C5CFF` plus 10 rgba sweeps. **Deferred:** home tier/model multi-button rows → `<select>` below 480px (needs JS wiring beyond CSS).
- ✅ Phase 6 — badge primitive unification. `.sb-badge` family (5 dead-code rules) removed; `.cm-chip` callsite migrated to `.badge.badge-sm.badge-{crit,warn,ok}` and its CSS removed; 21 `.recovery-pill` callsites in recovery.js + alerts-extended.js migrated to `.badge.badge-sm.badge-{ok,crit,neutral}` and its CSS removed. Net: 22 callsites converted to the canonical `.badge` shape, 13 CSS rules eliminated.
- ✅ Phase 7 — typography + spacing hygiene. `.botcfg-section-header` collapsed from off-scale 0.78rem/0.08em to the canonical in-content uppercase-label size 0.7rem/0.06em. `.cm-bot-tile` and `.cost-bot-tile` asymmetric paddings (`11px 13px 10px`, `10px 12px 9px`) normalized to the spacing scale (12px, `10px 12px`). 14 off-scale font-sizes in base.css + 66 in JS/HTML templates rounded to the canonical scale (0.83/0.84/0.86/0.88 → 0.85; 0.55 → 0.58; 0.64 → 0.65). `.cm-bot-tile`'s off-scale 7px gap also bumped to canonical 8px.
- ✅ Phase 8 — drawer migration (documentation-only). Phase 1 added the canonical `.drawer / .drawer-right` primitive with header/body/footer subclasses, transform-animated show/hide, dock modifier at ≥1280px, and full-bleed phone fallback. Four legacy ID-based drawers exist (`#evo-drawer`, `#turndetail-drawer`, `#errlog-drawer`, `#cm-rec-suggestions-drawer`) — each with its own quirks (different widths, z-indexes, show/hide patterns, animation styles) that would each require small UX-changing migrations. The pragmatic call: **new drawers use `.drawer / .drawer-right`; legacy drawers stay until they're touched for unrelated reasons, then migrate as part of that work.** Migration playbook documented in §9.6.

### P0 — Visible to operators, easy to fix

1. **Light-theme parity sweep — ✅ Phase 3 landed.** Phase 1 added the `var(--shadow-*)` token pairs; Phase 3 swept the callsites. Net: 15 inline `rgba(0,0,0,…)` shadows replaced with token references (hover tiles, pop-rollback modal, popovers, drawers, terminal modals); 4 custom-offset shadows carry hand-tuned `[data-theme="light"]` overrides at the bottom of `base.css` (`.sidebar` mobile drawer, `#errlog-drawer`, `.home-rail.mobile-open`, `#evo-fab`); 11 hex-color overrides for orange/yellow warning states (`.btn-warning`, `.cm-grade-D`, `.home-session-counter.near-cap/.at-cap`, `.evo-drawer-counter.near-cap/.at-cap`, `.home-msg-warn`), `.btn-panic:disabled`, and terminal `is-connecting`/`is-connected` status dots. The `#0a0c0f` code-block bg and `#c8a85a` banner text referenced in the original audit did not appear in admin CSS (they were marketing-site holdouts) — marketing site (`docs/gitpages/index.html`) still needs its own parity pass.
2. **Sidebar icon system — ✅ Phase 4 landed.** 22 inline Lucide-style SVGs (`viewBox="0 0 24 24"`, sized 16×16, `stroke="currentColor" fill="none"`) replaced the previous emoji + Unicode-block + glyph mix. New `.nav-icon` CSS utility lives in base.css next to `.nav-item` and sets stroke-width plus round line joins. Icons theme via `currentColor` inheritance from the parent nav-item.
3. **Form input widths span the page. ✅ Phase 2c landed.** The form rule at [static/css/base.css:1158](../packages/admin/evolve_admin/web/static/css/base.css#L1158) no longer sets a global `width: 100%`; that property is now carried by the `.input-w-*` utilities ([static/css/base.css:1170](../packages/admin/evolve_admin/web/static/css/base.css#L1170)) so each control fills only to its own data-shape cap, and a control with **no** width utility falls back to intrinsic width instead of spanning. **Phase 1 ✅** added the `.input-w-*` utility set. **Phase 2a ✅** capped 5 canary dropdowns. **Phase 2b ✅** capped ~48 more selects + numeric inputs across arbiter, history, observations, config heal/classifier/security, recovery, hook, plugin install, MCP install, reports schedule, self-improvement onboarding, backup privacy, cost / usage compact mode, and users primary-channel selectors. **Phase 2c ✅** swept the final 28 uncapped controls (route-tier + compact selects, handover selects, heal numeric, modal note/description textareas, the four chat composers + JSON/config body editors, inline mini-selects, delete-bot confirm) and removed the global `width:100%`, folding it into the width utilities. The earlier note that the remainder was "just opt-in modal fields / auto-width chips" undercounted it — there were 14 textareas (incl. all four composers + the JSON/config editors), the five `ai-optimization.js` tier selects, the two handover selects, and the heal numeric.
4. **Semantic color misuse — yellow as category. ✅ Phase 5 landed.** `.badge-feature` migrated from `rgba(251,191,36,0.15)` + `var(--yellow)` to the neutral `var(--bg3)` + `var(--text2)` + border treatment (it's defined but had no callsites, so future code reaching for "feature" no longer accidentally signals "warning"). `.cost-bot-tile-chip` audit follow-up: the chip is actually semantically yellow — it renders for `cost spike` only ([cost.js:N — search "cost-bot-tile-chip"]) — so the yellow is correct and stays. The original audit conflated decorative vs semantic yellow on that one.
5. **Modal max-width family. ✅ Phase 5 landed.** Collapsed `.pod-rollback-modal` 560→520 (canonical default) and `#rel-detail-box` 680→720 (canonical wide). Now every modal in the admin UI lands on the 380/520/720 family from §9.6.
6. **Marketing-site accent purple drift. ✅ Phase 5 landed.** Gitpages `--accent` flipped from `#7c6af7` to `#7C5CFF` plus 10 inline `rgba(124, 106, 247, …)` tints swept to `rgba(124, 92, 255, …)`. Marketing and admin now use the same brand purple.

### P1 — Worth a focused PR

7. **Badge primitive unification. ✅ Phase 6 landed.** Phase 1 added `.badge-sm` and `.badge-neutral`; Phase 6 migrated the 22 callsites and deleted 13 CSS rules. `.cost-bot-tile-chip` stays — it's actually semantically yellow (renders only for "cost spike"), not a generic chip. `.badge` is now the canonical status-badge primitive across the SPA.
8. **Primary-button duplication.** `.btn-primary` (solid fill, large) and `.err-action-btn.primary` (outline, small) both mean "primary". **Fix:** make `.err-action-btn` extend `.btn.btn-sm` and `.btn-primary` instead of redefining the visual.
9. **Touch targets below 44px on mobile. ✅ Phase 5 landed.** Added `@media (max-width: 480px) { .btn, .nav-item { min-height: 44px; } .btn.btn-sm { min-height: 0; } }` next to the `.btn` definition. `.btn-sm` opts out (it's used inside dense rows where 44px would dominate); everything else now hits the Apple HIG minimum on phone widths.
10. **Multi-button mobile rows. ✅ Phase 5 deferred → landed.** Home authority (3 buttons: Ask / Ask big / Auto) and model-tier (4 buttons: Auto / Fast / Standard / Power) rows now carry a `<select id="home-tier-mobile">` and `<select id="home-model-mobile">` sibling. CSS hides the desktop button row on phone widths and reveals the select. `_homeRestoreTier()` / `_homeRestoreModelTier()` sync the select's value with the active button; `_homeFetchTierConfig()` disables the Power option when `dailyCap === 0` (mirrors the desktop button-hide).
11. **Drawer-class promotion. ✅ Phase 8 landed (documentation-only).** The `.drawer / .drawer-right` primitive ships in Phase 1 and is the path for new drawers. The four legacy ID-based drawers (`#evo-drawer`, `#turndetail-drawer`, `#errlog-drawer`, `#cm-rec-suggestions-drawer`) each have UX-affecting quirks that make a forced migration riskier than the payoff. Migration playbook in §9.6 covers the conversion when one of them next gets touched.

### P2 — Hygiene sweep

12. **Uppercase-label size collapse. ✅ Phase 7 landed.** `.botcfg-section-header` now uses the canonical 0.7rem + 0.06em — matches `.card-title` and the rest of the in-content uppercase labels. Sidebar/nav stays at 0.58rem (its own role per §3).
13. **Off-scale font sizes. ✅ Phase 7 landed.** Swept 80 callsites across base.css + JS page modules: rare `0.83 / 0.84 / 0.86 / 0.88rem` → `0.85rem`; `0.55rem` → `0.58rem`; `0.64rem` → `0.65rem`. The major rungs (0.82 / 0.78 / 0.72 with 30+ callsites each) stayed — too widespread to sweep without risk; they're acceptable spacing-scale members in practice.
14. **Card padding hierarchy enforcement. ✅ Phase 7 landed.** `.cm-bot-tile` normalized from `11px 13px 10px` + 7px gap → symmetric `12px` + 8px gap. `.cost-bot-tile` normalized from `10px 12px 9px` → `10px 12px`. Both are on the canonical spacing scale now.
15. **Skeleton loader pattern.** Define a `.skeleton` class now (bg3 + same radius as replaced content) so the first feature to need one doesn't invent a new pattern.
16. **`<h2>` semantic adoption.** Audit pages for styled-span "section titles" and convert to `<h2>`. Outline order matters for accessibility.
17. **Content width caps on long descriptions.** 🟡 *Partial (2026-06-13).* Added the `.card-prose` (`max-width:64ch`) utility and applied it to the 4 Reports explainer banners (the worst offenders, 246–591ch). **Still open:** the "How it works" collapsible cards (Recommendations / Forge / app-wizard) — their bodies mix prose with `.si-pipeline` step diagrams and badge rows, so a clean cap needs per-prose-element application rather than a body-level cap; and long-string table cells per §10.6.

---

## 15. Conventions for new work

When adding a component or page, in order:

1. **Read this doc.** If your component fits a pattern, use it.
2. **No new hex values.** Extend tokens in §3 and reference the variable.
3. **No new font sizes outside §4.**
4. **No new border-radius outside §6.**
5. **Width-cap form fields by data type (§9.2 / §10.2).** Default `width: 100%` is almost always wrong.
6. **One primary action per surface (§10.4).**
7. **Theme-toggle test (§10.8) — verify both dark AND light.**
8. **Responsive-test phone / tablet / laptop (§10.9).**
9. **Update this doc** in the same PR if you added a reusable component. Undocumented patterns get reinvented.
