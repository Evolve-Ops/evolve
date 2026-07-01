# Spec — Model Economics page (model-tiers Phase 13)

**Status:** drafted 2026-06-13 (design-sync with operator). v1 scope approved.
**Aspect:** `META:model-tiers`. Parent spec: `docs/spec-model-rungs-and-roles-2026-06-09.md` (this is Phase 13 / Addendum 11 of that arc).
**Deploy:** admin-only, canary-gated (same as the rest of model-tiers).

---

## Problem

The **Usage / Cost** page is **bot-centric** — "what is each bot spending/doing." An operator
tuning model choice needs the **transpose**: a **model-centric, pod-normalized** view — "what
does each *model* cost across the whole pod, and how do models compare across tiers and
providers" — so they can answer *"is the model behind Standard overpriced versus alternatives
in its band?"* and *"where is our spend actually going?"*

This is a **synthesis & presentation** phase, not new instrumentation: the data-inventory
(2026-06-13) found almost every cut already exists in `usage_analytics`, and two are already
rendered on the Cost page. v1 assembles existing pod-wide payloads into a model-centric
comparative surface.

## Decisions (operator, 2026-06-13)

- **v1 = cost economics only.** Performance (latency / success / error / struggle) shipped as
  **v2** — no longer deferred (the span-reader exists and coverage is ~49%; see §"v2 —
  performance"). Cost stays over ALL turns; perf is reported over the span-covered subset with
  an explicit coverage %.
- **Headline metric = cost per turn**, per model — the default sort and the most prominent
  number. **Effective $/1k tokens** (incl. cache) is kept as the rigorous secondary comparator.
- **Placement:** a **pod-wide "Models" lens off AI Optimization** (AI-Opt is "optimize model
  choice"; Cost is "account for spend" — same data, different intent), reusing the exact
  `by_model` payload the Cost page consumes, cross-linked from Cost. Not a new top-level page;
  not a per-bot subtab (AI-Opt's subtabs are bot-centric — this is the POD/pod-wide lens).

## Data sources (all existing — REUSE, do not re-derive)

| Need | Source | File |
|---|---|---|
| Per-model cost/turns/tokens/$/1k, pod-wide | `usage_analytics.compute_summary().by_model` (`calls`=turns, `cost`, `usd_per_1k_{input,output,blended}`, `billed_tokens`, `low_confidence`); `load_turns(bot_id=None)` pools all bots | `packages/analyzer/usage_analytics.py:465-834` |
| Human vs non-human per model | `by_model_by_audience` (human/non_human `{calls,cost}`) via `trigger_kind` | `usage_analytics.py:738-756` |
| List (advertised) price | `lookup_price` over `model-pricing.json` (LiteLLM + models.dev) | `packages/analyzer/model_pricing.py:437-475` |
| Band / tier placement | `model_cost_bands.resolve_band` / `observed_band` | `packages/analyzer/model_cost_bands.py:294-466` |
| Model identity (the model that actually ran = gateway-reported, not self-report) | turn record `model`/`provider` (from `llm_output` hook) | `packages/plugin/src/observer/TurnObserver.ts:1402-1409, 5175-5191` |
| Serving route (NOT a frozen-cap file) | `routes_analytics.py` `register_analytics_routes()` | `packages/admin/evolve_admin/web/routes_analytics.py:123, 1976` |

**Small NEW aggregations v1 adds** (cheap, over data already loaded):
- **bot-count per model** (distinct `instance` per model) — "how many bots use this model."
- **recency per model** (max `ts` per model) — "last used."
- Both are a re-aggregation of the turns `usage_analytics` already loads; add to the `by_model`
  rows (or a sibling map) rather than a second load.

## v1 page design — model leaderboard

A **sortable table, one row per model used pod-wide**, default-sorted by **$/turn desc**:

| Column | Source | Notes |
|---|---|---|
| Model | turn `model`/`provider` | provider-colored chip via the existing `_aiProvider*` presentation map — **no provider literals in logic** |
| Tier / role served | `resolve_band` placement | which band/role this model sits in |
| **$/turn** | `by_model.cost ÷ by_model.calls` | **headline / default sort.** Labeled **per-turn** (≠ per-LLM-call) |
| Effective $/1k (blended) | `by_model.usd_per_1k_blended` | labeled **"incl. cache cost"** (the #2797 honesty contract — do NOT re-litigate as a clean per-token rate); in/out available on expand |
| List $/1k | `lookup_price` | advertised; "—" if pricing cache miss |
| Eff-vs-list delta | computed | surfaces cache savings (eff < list) or overage; "—" when list unknown |
| Total spend | `by_model.cost` | |
| Share of pod spend | `cost ÷ Σcost` | |
| Volume (turns) | `by_model.calls` | |
| Billed tokens | `by_model.billed_tokens` | |
| **Confidence** | `by_model.low_confidence` + sample size | badge: low-sample → "insufficient data"; **never hide low-sample rows, mark them** |
| Human % | `by_model_by_audience` | share of turns that are human-initiated |

- **Group / filter by tier and by provider** — the core "compare within a band" interaction.
- **Configured-but-unused models shown distinctly** (volume 0 → "no usage yet"): the operator
  sees the full catalog, not just the used subset (ties into the credential/dormant work —
  a configured model with zero usage is itself a signal).
- A compact **human-vs-non-human** per-model strip (reuse the Cost page's Model×Audience shape).
- **Pod-normalized**: all bots pooled (`load_turns(bot_id=None)`); this lens is pod-wide by
  construction, not per-bot.

## v1.5 — interactive redesign (model-centric, 2026-06-14)

Operator review of the live v1 leaderboard. v1.5 keeps the data layer but reworks identity + presentation. Decisions (operator design-sync; two forks confirmed):

### 1. One row per model identity (collapse sub-series)
v1 keyed rows on the raw `by_model` key, so a single model split into multiple rows: separate cost series (notably the `:unexpected_billing` suffix variant) and the configured-vs-`off-catalog` distinction, each re-deriving the band from its own observed cost (→ the same model shown as both "premium" and "high"). FIX: collapse to **one row per `(provider, bare model_id)`**. Sum `cost`/`calls`/`billed_tokens` across all sub-series; recompute `$/turn = Σcost ÷ Σcalls` and `usd_per_1k_*` from the summed legs; derive **one band from pricing** (`resolve_band` on the identity, NOT per-observed-series). `configured` / `off-catalog` collapse from a row key to a **badge + filter**. Dedup the configured-but-unused enumeration against the merged identity set. Different versions stay distinct (`opus-4-8` ≠ `opus-4-7`; `gpt-4o` ≠ `gpt-4.1`).

### 2. Tier = pricing cost-band, not bot role
The model's tier label is the pricing-anchored cost-band (`model_cost_bands` → low/med/high/premium), decoupled from any bot's role assignment. Subsumes the per-series band split in #1.

### 3. Bars primary + table toggle (Bite B)
Default view = horizontal bar chart, one bar per model, sorted desc by the selected metric (default `$/turn`). Metric-switcher buttons swap the bar metric: $/turn · Eff. cost/1k · spend · share · turns. Secondary stats render under/in each bar. A toggle flips to the v1 dense multi-stat table (kept). Low-confidence rows (`low_confidence` / tiny sample) are visually de-emphasized in the bars (reduced opacity), never hidden (confidence badge stays).

### 4. Rename column "EFF $/1k" → "Eff. cost/1k" (Bite B)
"EFF" reads as *effectiveness*; the metric is effective **cost** incl. cache. Rename; keep the incl-cache tooltip (#2797 contract). There is no effectiveness/quality metric in v1.x — that's v2 perf-from-spans.

### 5. Filter chips — Reports/Alerts pattern (Bite B UI; Bite A data)
Multi-select within a facet: **provider** (all / per-provider) · **band** (all / low/med/high/premium) · **bot** (all / per-bot) · **audience** (all / human / auto).
- provider & band: free (per-row fields).
- **per-bot:** new additive compute — preserve the bot dimension (a `(bot × model)` matrix, or re-run the pooled assembly with a `bot_id` filter); the source supports `load_turns(bot_id=…)`. Pick the cheapest correct approach.
- **audience:** re-split $/turn, spend, share, turns from `by_model_by_audience` `{calls,cost}`. **Eff. cost/1k is unavailable per-audience** (no per-audience token volume) → the UI greys it when an audience filter is active.

### 6. Aggregate-by-tier rollup — BOTH axes
A summary strip showing blended cost per grouping, toggleable between: **by cost-band** (low/med/high/premium — market view) and **by role** (fast/standard/power/max/judge — what each configured slot costs pod-wide; role→rung→models from `merge_model_catalog().roles`). Blended `$/turn = Σcost ÷ Σcalls` over each group's members; include member count.

### Turns / LLM-calls
Turns stay user-facing turns with an honest tooltip (sub-calls fold in). True per-LLM-call counts ride v2 (same cascade-span source as performance).

### Carries v1 invariants
No provider literals in logic (presentation map); style-guide + both-theme; serve from `routes_analytics.py` (avoid the frozen no-growth files); reuse `by_model`/`by_model_by_audience`/pricing/bands — new compute limited to merge-by-identity, the `(bot × model)` matrix, the two rollups, and the audience re-split.

## Invariants / guardrails

1. **Honest units.** `$/turn` is per-turn, not per-LLM-call (sub-calls fold into the parent
   turn; the per-call plugin path is mostly silent on the live pod). Effective $/1k is
   incl-cache (#2788/#2797). Label both; do not overstate.
2. **Confidence-gated.** Never present a low-sample number as authoritative — surface the
   `low_confidence` gate (10k-token min sample) as a badge.
3. **Reuse, don't re-derive.** Consume `by_model` / `by_model_by_audience` / pricing / bands.
   The only new compute is bot-count + recency.
4. **No provider literals in logic** (provider colors/labels via the presentation map).
5. **Style-guide + both-theme** on all SPA work; serve from `routes_analytics.py` — avoid the
   five frozen no-growth files (`cli.py, deploy.py, routes_admin.py, routes_admin_config.py,
   routes_admin_shared.py, server.py`).
6. **Pod-wide only** in v1 (no per-bot model economics — that's the Cost page's job).

## v2 — performance (model-centric, from cascade spans)

The performance dimension (latency / success / error / struggle per model) exists **exactly
once**, on the cascade turn-**spans** (`CascadeTelemetry.buildSpan` → `{model, provider,
total_cost, start/end_time, cascade.success, error_info, cascade.struggle.score}`, per turn).
**No longer deferred** — the span-reader already exists
(`observability.session_rollup.iter_turn_spans`, also used by `audit_runner._load_spans` and
the live `routes_cascade._iter_recent_spans`) and EACCES is already solved there. What was
missing is a **model-grouped** rollup, which v2 adds. Coverage is **partial** (~49% of turns
carry a span on the live pod, ~1,952 / 3,996 over 30d; per-model coverage varies) — that is
EXPECTED and is **reported, never hidden**.

**The asymmetry is the whole point:** cost reads evolve-readable turn records (ALL turns); perf
reads the gateway-owned spans (a SUBSET). The payload + UI keep that legible — a perf figure
(subset) is never conflated with the all-turns cost figure.

### Metrics (per model identity, over the span-covered subset)
- **Headline effectiveness = struggle score** — avg `cascade.struggle.score` (100% present on
  spans; **lower = smoother**). `struggle_avg`.
- **success_rate** (+ **success_n**) — `cascade.success` true ÷ spans where the field is
  **present** (~60% coverage). The denominator is the present-subset, **never all spans** —
  over-all-spans would understate the rate. `None`/`success_n=0` when absent everywhere.
- **error_rate** — `error_info` non-null ÷ spans.
- **latency p50 + p95** — turn-duration (end−start) percentiles, linear-interpolated, in **ms**.
  This is **whole-turn wall-clock** (incl. tool calls + waits), **NOT pure inference time** —
  label honestly. Speed axis only.

### Identity join (shared with cost #2889)
Perf rows join cost rows by the **same `(provider_lower, bare)` identity** the cost layer uses,
so a model the gateway logged both qualified (`anthropic/claude-…`) and bare (`claude-…`)
collapses onto one perf row that lines up with its one cost row. The resolver is **shared, not
reimplemented** — `model_economics.resolve_bare_to_provider` (the public #2889 entrypoint) +
`_identity` / `_split_model_key` — and a span's identity is resolved by the **identical**
function and map the cost layer uses. Spans also carry a clean top-level `provider` field, but
it is **deliberately NOT used for identity**: the cost layer never sees it (it resolves a bare
cost key only via the shared map), so honoring a span-only provider would key the perf row onto
an identity the cost row never lands on and **orphan** the perf data. Matching cost's resolution
exactly is the binding contract (the clean provider agrees with the map on the live pod anyway —
every bare twin has a qualified sibling that seeds it). **No provider literals in logic.**

### Coverage gate (mirrors the cost `low_confidence` pattern)
- Pod-level **`perf_coverage_pct`** (total spans ÷ pod total turns) in the header.
- Per-model **`coverage_pct`** = `span_count ÷ total_turns` (total_turns from the cost rows'
  per-identity turn counts — i.e. `usage_analytics.by_model` grouped by the same identity). A
  model with spans but **no** cost-turns reports `coverage_pct = None` (no denominator), never
  a fabricated value.
- Below a **min sample (< 20 spans/model)** → emit the perf fields but flag **`low_coverage =
  true`** so the UI shows "insufficient span data". The model is **never hidden**.

### Presentation (Bite B)
Perf metrics join the leaderboard as additional **metric-switcher** options (bars) **and**
**table columns**, each with a **per-model span-coverage badge** — never conflated with the
all-turns cost figures. Models with no spans show "no span data" (the route sets `row.perf =
null`); models with `low_coverage` show the badge over their (thin) figures.

> **Bite B shipped** (PR — model-economics.js / index.html / base.css). The metric switcher
> grew a separate **Perf (span subset)** group — Struggle (headline, `lower = smoother`),
> Latency p50, Latency p95, Success, Error rate — rendered as **single-segment** bars (only
> cost/turns stack human/auto). Perf bars carry an honesty caption with the metric's direction
> (so a long bar is never misread as "good") and the span-subset/all-audience framing. The table
> grew **primary** columns Perf cov. · Struggle · Lat p50 · Lat p95 · Success · Errors (primary,
> not `data-secondary` — the ME table has no diagnostic-column toggle, so a secondary perf
> column would be permanently hidden). Per-model coverage rides the `_mePerfBadge` ("no span
> data" / "insufficient span data" / N spans + coverage_pct) in the bar meta and the "Perf cov."
> cell. Pod-level `perf_coverage_pct` + `perf_total_spans` surface as a badge by the leaderboard
> header. Under a bot/audience filter the perf block stays **all-audience** and is labeled so
> (caption + view-note + a "(all-aud)" marker on the Perf cov. header) while only cost recasts.
> Latency is labeled whole-turn wall-clock (not pure inference). Both themes verified.

**Bite-B handling note — audience filter:** perf is NOT audience-split (spans have no audience
leg), so under an `?audience=human|auto` filter the `row.perf` block keeps its **all-audience**
figures while the cost fields (`turns`/`$/turn`) recast to the audience subset. That is by
design (perf is a distinct span-subset block), but Bite B must not imply the perf numbers are
audience-scoped — label them as all-audience, or grey/hide perf when an audience facet is
active. Data-layer-side this is a non-issue; it is purely a UI-copy concern.

### Bite A — data layer (this PR)
- **`packages/analyzer/model_performance.py`** — `assemble_model_performance(spans,
  total_turns_by_identity, *, total_turns=None, bare_to_provider=None, min_spans=20)` →
  `{by_identity: {<provider/bare>: {span_count, latency_p50_ms, latency_p95_ms, struggle_avg,
  success_rate, success_n, error_rate, coverage_pct, low_coverage}}, perf_coverage_pct,
  total_spans, total_turns}`. Pure read-and-aggregate; reads spans via `iter_turn_spans`.
- **Route wiring** (`routes_analytics.py::api_analytics_model_economics`, not frozen): after
  `assemble_model_economics`, `_attach_perf_block` loads spans for the window and **merges a
  `perf` block onto each economics row by identity** (`row["perf"] = {…}` or `None`) + a sibling
  `performance` map (keyed by identity, so perf-only models are reachable) + `perf_coverage_pct`
  in the summary. **Additive** — all v1.5 fields intact; **fail-safe** — any span-store hiccup
  degrades perf to null and the cost payload stays whole (the v1.5 UI must not break).

**Weaker alternatives, NOT used:** engagement-as-quality (observation tuples carry `engagement`
+ `session_id` but **no model field**, so model attribution is transitive-through-session and
lossy on multi-model sessions).

## v1.1 (optional) — $/session per model

Derivable: `session_id` on every record; `session_economics.py` aggregates per-session cost.
Needs a new **`(session × model)`** rollup. **Honest caveat:** a session can span models
(cascade re-tiers mid-session), so `(session × model)` is the truthful unit, not "$/session."
Deferred from v1 to keep it tight.

## Phasing / bites

- **v1 (this phase):** the cost leaderboard above — reuse payloads + bot-count/recency add +
  the new pod-wide Models lens + serving endpoint. ~1–2 bites (data/endpoint; SPA view).
- **v2 — Bite A (data, this PR):** span-reader perf rollup (`model_performance.py`) + coverage
  reporting + additive `perf` block on the economics payload. **Bite B (UI):** perf metric-
  switcher + table columns + per-model coverage badge (consumes the `perf` block).
- **v1.1 (opt):** `(session × model)` economics.

## Open caveats (carry into build + UI copy)
- spans EACCES (v2 coverage);
- `$/turn` granularity (≠ per-call);
- multi-model sessions (v1.1 unit).

## Usage page — unified composition card (model-tiers, 2026-06-15)

Adjacent surface (the **Usage** page's Spend sub-tab, not the Model Economics
page), folded in here because it shares the provider-color vocabulary and the
turns-vs-cost metric concept. The old layout stacked three blocks vertically: a
"Usage Summary" card whose tall **By provider** table dominated the fold, a
separate "Activity Composition (by trigger)" card (compact bar + legend), and a
standalone "Stack by: Turns | Cost" strip that drove the two timeline charts.
The provider table was the vertical-space problem.

**End state — one combined card.** The provider table and the whole composition
card collapse into a single card that renders the same compact bar + legend,
switchable by **two segmented toggles in its header**:

- **Dimension** — `Trigger | Provider`. Trigger reuses the existing 4-bucket
  roll-up (`human / heartbeat / cron / background` via `usageCompBucketOf`);
  Provider builds segments from `billing.by_provider` (`{provider: {calls,
  cost}}`). Default = **Provider** (operator-approved).
- **Metric** — `Turns | Cost`. This **replaces** the standalone "Stack by"
  strip: one metric concept now governs the whole Spend view. The metric drives
  the composition bar widths, the bold `%`, and the legend sort **and** restacks
  the two timelines below (they already read `window._usageUnit`). Promoting the
  metric toggle into the card header is the point — `setUsageUnit` re-renders the
  timelines *and* the composition bar from the one cached payload.

A single one-line totals strip (`Total turns: N · Total cost: $X`) sits at the
top of the card body; the alert banners (no-data / unexpected-billing /
cron-majority) are a separate concern and still render into `#usage-alert`.

**Metric / sort semantics.** Segment width, the bold `%`, and legend row order
all = share of the **active** metric (turns → `calls/Σcalls`; cost →
`cost/Σcost`), sorted DESC by that metric. (The old provider table was unsorted
insertion order — now fixed.) Persisted to `localStorage` under
`evolveUsageCompDim` (dimension) and the existing `evolveUsageUnit` (metric).

**Color sources (no provider literals in logic).** Trigger segments keep
`USAGE_COMP_BUCKET_COLORS` (token vars). Provider segments reuse the shared
`--ai-provider-*` token vocabulary via the `.ai-provider-<slug>` classes (which
set `--provider-color`) — resolved through `_aiProviderColorClass` /
`_aiProviderSlug` from `ai-optimization.js` (loaded on every page, alphabetically
before `cost.js`, so reliably in scope at render time). The provider allow-list
is **not** copied into `cost.js`.

**Provider-has-no-sessions asymmetry.** Trigger buckets carry
`{calls, sessions, cost}` but provider rows carry only `{calls, cost}` — there is
no per-provider session count. Each legend row always shows turns and cost; the
`· N sessions` segment is emitted **only when `sessions` is defined**, so the
shared cell renderer tolerates the missing field for provider rows.

## Addendum — used-but-low-volume legibility (model-tiers, 2026-06-15)

Operator report: *"Opus is missing, but it's an Anthropic model that's been
used."* The page was **technically correct but conflated two ideas** — *"this
model was used" (a fact)* vs. *"its $/turn figure is statistically authoritative"
(a confidence qualifier)* — and the second silently erased the first.

**Mechanism (fully traced).** The leaderboard filters out `low_confidence` rows
**by default** (`if (!_meShowLowConf) rows = rows.filter(r => !r.low_confidence)`).
A row is `low_confidence` when billed tokens < `MODEL_COST_MIN_TOKENS` (10k). On
this pod the **power rung** (opus-class) gets little traffic — most turns are
haiku/sonnet — so it sits under 10k and is gated out. The header *"Models in use:
N"* counts every row, but the bars/table showed only the confident subset: an
N-vs-M gap with no explanation. The provider/band facets counted the gated rows
while the leaderboard didn't render them, and a band/role whose members are ALL
low-volume rendered **"0 models · 0 turns"** (the opus-class IS the `high` band,
hence the empty "High" rollup card). A "Show insufficient data" checkbox existed
but you had to know to look.

**STEP-0 finding.** Confirmed Opus is **present-but-gated**, never **dropped**:
the identity-resolution path (#2889) can at worst split a model into two rows, and
only the sentinel skip-list (`delivery-mirror` / `unknown`) drops rows — opus is
neither. A sub-10k qualified opus row lands in `rows` with `low_confidence: true`
(provider resolved, band `high`). So this is a **presentation** fix, not a
threshold or data fix.

**Decision (operator).** Keep the clean confident-only default ranking; make the
hidden-but-used models **explicitly legible**, never silently absent. The 10k gate
is **unchanged**, and low-confidence samples are **never** folded into any blended
$/turn.

### A. Leaderboard — "+N below the confidence line" affordance
An **always-visible** line at the bottom of the **bars** list AND the **table**
whenever ≥1 used model is low-confidence:

> `+N used models below the 10k-token confidence threshold — <first ~3 names>, and K more — show`

- shows the **count** and **names** the models (first ~3 + "and K more") so a used
  model — e.g. the low-volume power-rung model — is **visible by name even while
  collapsed**;
- clicking **expands in place** to reveal those rows, **dimmed** (`.is-lowconf` /
  `.me-lowconf-tr`) + the existing "insufficient data" badge, then flips to
  "hide";
- the **confident ranking stays clean** — low-conf rows render as a **separate
  group below the affordance**, sorted among themselves, so they never jump the
  $/turn sort. Bar widths scale to the **confident** max, so revealing the
  low-conf rows does NOT reflow the confident bars (a low-conf bar exceeding the
  confident max caps at the full track, dimmed + badged, value still shown);
- backed by the same `_meShowLowConf` reveal state the table uses (bars + table
  stay consistent). The affordance **replaces** the somewhat-hidden global "Show
  insufficient data" checkbox (minimal coherent UI). Uses the `.expand-icon` SVG
  chevron (style-guide §9.13), not a unicode triangle.

### B. Headline reconcile
The "Models in use" stat now annotates the used-vs-confident split (a `· M
confident` suffix + a tooltip: *"N used · M with confident cost data · K
low-volume"*) so the count reconciles with the +N affordance instead of reading as
an unexplained gap. The count itself is unchanged (N = all used is correct).

### C. Rollup cards — count used members even when low-volume
`_blended` now returns a 5-tuple `(Σspend, Σturns, blended $/turn, member_count,
used_count)` — `used_count` = ALL members incl. low-conf; `member_count` =
confident only (the blend denominator). Threaded through `_band_rollup` and
`_role_rollup` into the payload (both `_blended` callers updated). When confident
turns == 0 but `used_count` > 0 the card renders **"N models · insufficient
data"** (cost dimmed `—`) instead of "0 models · 0 turns". The $/turn blend stays
**confident-only** — this is a count/label change, not a math change (the always-on
low-confidence blend exclusion that fixed the HIGH-below-MEDIUM distortion is
preserved).

**Invariants preserved:** the 10k `MODEL_COST_MIN_TOKENS` gate is unchanged;
low-confidence rows are never folded into any blended $/turn (rollup or headline);
no provider literals in logic; style-guide + both-theme (dark + light verified).

## Usage page — By Channel / By User legibility (model-tiers, 2026-06-17)

Adjacent surface (the **Usage** page's `by_channel` / `by_user` tables in
`pages/cost.js`), folded here because it shares the read-layer + the
no-provider-literals discipline. The tables showed raw platform ids: By
Channel shattered one Slack channel across many `:thread:<ts>` rows and mixed
in `unknown`/`heartbeat` system volume; By User keyed rows `{channel}:{user_id}`
so the top rows were `unknown:?` / `heartbeat:?` (system traffic, **not**
people). Operator forks (all → Recommended): channel grouping = **roll threads
into the parent** (expandable); system volume = **relabel into named
categories** + keep OUT of By User; name resolution = **background cache**
(render reads cache only, never a live API call). **3-bite plan**, all REUSE —
no new platform-API capability.

### Bite 1 — SHIPPED #2985 (model-tiers)
Data layer (`usage_analytics.compute_summary`): By User is built from the
**human** source bucket only, emitting structured rows `{platform, user_id,
instance, calls}` keyed by `(platform, user_id)` (dominant bot tracked for
display + cache-only resolution). By Channel rolls `<channel>:thread:<ts>` up
to the parent conversation with a `threads:[{thread_ts,calls,cost}]` child
list, and buckets no-conversation turns (sentinel channels `""`/`unknown`/
`heartbeat`) into **named system categories** (Heartbeat / Scheduled / Forge
builds / Subagents / Evo / System), flagged `system:true`. New provider-neutral
shape helpers `_infer_platform` / `_split_thread` / `_is_system_channel` /
`_system_category` (turn records carry no `platform` field — inferred from id
shape; backlog: stamp it at the gateway `TurnObserver`). Route
(`/api/analytics/usage` → `_enrich_usage_names`) attaches a **cache-only**
`display_name` + `name_source` + categorized fallback label via
`roster_resolver.resolve_display_name`, and `"DM · <name>"` to Slack DMs when
the cache already carries the participant. cost.js renders names primary + raw
id dim secondary (header `User / Channel` → `User`); By Channel rows are
expandable to their threads via the `.expand-icon` affordance (window-exported
`toggleUsageChanThreads`) with a distinct "System & scheduled" group.

**Row shapes (the contract Bite 3 binds to):**
- `by_user`: `{platform, user_id, instance, calls, display_name|null, name_source, label}`
- `by_channel` real: `{channel, calls, cost, system:false, threads:[{thread_ts,calls,cost}], platform, is_dm, label}`
- `by_channel` system: `{channel:<category>, calls, cost, system:true, category, threads:[], platform:"system", label:<category>}`

### Bite 2 — conversation-name resolver + cache + sweep (`users` aspect)
Deposited to the `users` backlog 2026-06-17. Builds the cache-only
conversation-name source (Slack public/private channel names, group titles)
the render path reads — the live populate path is a background sweep, not the
render. **Gate for Bite 3.**

### Bite 3 — By Channel consumes the conversation cache (model-tiers, queued)
Pure consumer + presentation (ui-co-owned copy): replaces remaining raw
channel ids with resolved names (Slack `#name`, group title), binding to Bite
1's `by_channel` row shape (enriches the non-DM `label`) + Bite 2's cache key.
Gated on Bite 2 merged.

**Invariants:** no live Slack/Telegram API call from the render path (the
operator's background-cache decision); no provider literals in routing logic
(shape-based `_infer_platform`; provider strings only in display labels with a
generic fallback); the CLI `_print_usage`/`_resolve_user` path is untouched (it
builds its own local `by_user`/`by_channel`); style-guide + both-theme (dark +
light verified).

---

## Addendum — uncredentialed-catalog honesty (model-tiers, 2026-06-25)

**Status:** approved (operator design-sync 2026-06-25). Treatment = **relabel +
separate**, role-slot view INCLUDED. Canary-gated, admin-only.

### Problem

The "CONFIGURED, NOT USED" list and the "CONFIGURED, UNUSED: N" headline stat
enumerate every model in every rung of the **pod-effective** catalog
(`_configured_catalog_models` over `_pod_catalog` = code-defaults < pod-overrides,
`packages/analyzer/model_economics.py`). Because the default rungs ship
Google/OpenAI/xAI models, a pod holding ONLY an Anthropic key still lists those
models — and they render with status **"no usage yet"**, which reads as
"available, just idle" when they in fact **cannot run** (no credentials).
Confirmed live on the VPS pod: only the Anthropic models have usage; the
default-rung Google/OpenAI/xAI models show "no usage yet" with no key behind them.
The headline conflates *unusable* with *idle*.

### Primitive (reused, not reinvented)

`model_discovery.discover_credentialed_providers(bot_users) -> set[str]` already
returns the pod-wide set of providers any bot holds an `api_key` for, derived
from auth-profiles — **no provider literals**. The route already has the member
list; it computes the set and threads it in.

### Treatment

1. **Thread the credentialed set into `assemble_model_economics`** via a new
   `credentialed_providers: set[str] | None = None` kwarg. The route derives it
   from `discover_credentialed_providers(_oc_members())` (the broader member list
   — includes the evolve bot whose turns also appear in usage — so a key held
   only by evolve is not missed and we never *false-flag* a credentialed model),
   wrapped in try/except → `None` on any failure.

2. **Flag uncredentialed configured-but-unused rows.** For each unused model,
   `_provider_uncredentialed(provider, credentialed)` decides: provider absent
   from the (lowercased) credentialed set ⇒ `credentialed:False` +
   `status:"no_credentials"`; else `credentialed:True` + `status:"no_usage"`.

3. **Split the headline.** `unused_uncredentialed` (count of `credentialed:False`
   unused rows) rides in the payload; the JS counts only credentialed-unused in
   the "Configured, unused" stat and surfaces the uncredentialed count as a muted
   "· N no key" annotation. The stat stops conflating unusable with idle.

4. **Relabel + separate (JS).** Uncredentialed rows get a muted **"no credentials"**
   badge (neutral token badge, provider-aware title — "No {provider} API key on
   this pod — this model can't run until you add one"), sort to the bottom under a
   muted **"Not available — no credentials"** sub-heading row, and dim via the
   existing `.me-lowconf-tr`. They are **kept** (not dropped) so the "add a key to
   unlock" affordance survives. Credentialed-but-idle rows keep "no usage yet".

5. **Role-slot view.** `_role_rollup` flags a slot `uncredentialed:True` when its
   rung members are ALL uncredentialed; the role-slot card then reads "no
   credentials" (unfilled) instead of a populated slot with no traffic.

### Invariants (enforced)

- **Fail-open.** `credentialed_providers is None` ⇒ behave exactly as before — no
  flagging, no blanking, `unused_uncredentialed == 0`, no slot uncredentialed.
  A transient auth-profiles read miss must never false-flag the whole catalog.
  (Mirrors `model_catalog.py`'s None-path contract.) The set rides in the payload
  as `credentialed_providers` (sorted list or `None`) so `filter_economics`
  re-applies the same role-slot filter when it re-derives the rollups.
- **Bare provider ("") treated as credentialed/unknown** — never force-flagged
  (bare ids are almost always Anthropic twins).
- **No provider literals in viability logic** — pure data-derived set membership;
  provider strings appear only in DISPLAY labels, with a generic fallback when the
  provider is "".
- **Style:** new badge uses token vars (no new hex, no off-scale font);
  `tools/ui-style-lint` clean; both themes verified.

### Tests (`packages/analyzer/tests/test_model_economics.py`)

- uncredentialed provider model flagged `credentialed:False`/`status:no_credentials`;
- credentialed-but-unused stays `credentialed:True`/`status:no_usage`;
- `credentialed_providers=None` ⇒ nothing flagged (fail-open), payload
  `credentialed_providers` is `None`, no role slot uncredentialed;
- bare-provider unused model not flagged even with a present credentialed set;
- role rollup: an all-uncredentialed rung's slot reads `uncredentialed:True`,
  a credentialed slot reads `False`.

Data-derived fixtures; no provider literals in assertions beyond the fixtures'
own placeholder provider names.
