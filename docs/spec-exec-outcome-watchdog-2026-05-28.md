# Exec-outcome watchdog — design spec

**Status:** Draft. Pre-implementation; awaiting design approval before any code lands.

**Date:** 2026-05-28.

**Origin:** Two cases from May 2026 show the same shape:

1. **Security-Bot approval timeout (2026-05-28).** Security-Bot tried to run `openclaw doctor --non-interactive` to follow up on the cost cleanup. OC asked the operator to approve via the 30-min exec-approval flow. The operator didn't see the request. The command silently timed out. Security-Bot surfaced "Command did not run: approval timed out" in chat. *Evolve produced no Signal.*

2. **Team-Bot-A protein-tracker (2026-05-24).** Team-Bot-A's protein-tracker app dispatched `python3 ops/tools/unified_task_system.py`. OC denied (exec policy `deny`, empty allowlist). Team-Bot-A had already composed an optimistic reply ("Added to the management meeting agenda — refund queued"). The user saw the contradiction in Slack. *Evolve produced no Signal.*

Both are instances of a class: **the bot expected an action to succeed; the system blocked it; nobody on the Evolve side noticed.**

**Adjacent:**

- [docs/spec-smarter-generators-2026-05-28.md](spec-smarter-generators-2026-05-28.md) — the investigate-before-propose architecture this spec extends. Same toolkit, same proposal shape, new producer + new investigating generator.
- [project_oc_2026_5_18_exec_deny_migration](../memory/project_oc_2026_5_18_exec_deny_migration.md) — the exec-deny migration that produced the team-bot-a case. Spec status: Phase A shipped 2026-05-25.
- [project_oc_exec_preflight_runtime_notes](../memory/project_oc_exec_preflight_runtime_notes.md) — OC v5.26 blocks `python`/`node` + complex syntax even with exec=full. Filed openclaw#87371.
- [project_cost_alerting_blackout_2026_05_20](../memory/project_cost_alerting_blackout_2026_05_20.md) — the 30-min approval timeout incident; OC#85841 closed as "already implemented" in v5.20.
- [feedback_evolve_bot_llm_visibility](../memory/feedback_evolve_bot_llm_visibility.md) — the "causal opacity at constraint moments is the real bug" rule; this spec ships the operator-side half (the bot-side `(pod note: …)` channel is separate).
- [feedback_distinguish_tooling_failure_from_findings](../memory/feedback_distinguish_tooling_failure_from_findings.md) — tri-state status: "we don't know" must be first-class. Applies directly to exec-outcome attribution.

---

## Problem

Today, an operator can only learn that a bot's exec failed by:

1. The operator messages the bot, gets a confused reply, and chases it down.
2. The operator notices the bot is quiet on a workflow that should be moving.
3. The bot itself surfaces the issue in chat (as Security-Bot did).

All three depend on the operator being in the loop on the *specific* failure, in the *specific* moment. They scale to zero bots and break at one.

What Evolve produces for these cases today:

- `cost_watchdog` sees the exec attempt because it bills against the bot — but it sees a *successful* turn (cost was incurred for the LLM call that *requested* the exec; the denial is in the tool_result content, not the cost record).
- `audit.py` checks static config (exec policy current state) but doesn't watch outcomes.
- `tier3_audit` / `security_warden` watch session-level health but not exec-event outcomes.
- The annotation pipeline *already* counts `tool_error_count` and `tool_retry_count` per turn (`{shared_dir}/annotations/<bot>/<date>.jsonl`, `turn_annotation.struggle_features`). **Nobody reads them.**

The data sits there. We just don't surface it.

The team-bot-a case has a second wrinkle: the bot composed an optimistic reply *before* the exec failed. From the user's perspective the visible artifact is the contradiction — but Evolve's signal would fire on the upstream cause (denied exec), and the proposal explains the contradiction the user saw.

---

## Principle

**Watch the gap between bot intent and execution outcome. Attribute the cause. Surface to the operator at the right cadence with the right framing.**

Three sub-principles, mirroring the smarter-generators spec:

1. **Detection is cheap; the data exists.** `turn_annotation` records carry `struggle_features` per turn. A producer that sweeps annotation JSONL and emits Signals on `tool_error_count > 0` patterns is pure Python, sub-second, runs daily. LLM enters only at Phase 3-ish, for diagnosing exec content when rule-based attribution deadlocks.

2. **One Signal per failure mode, one Proposal per investigated bot.** The same shape as `bloat_investigator`: multiple cooperating Signals, one Investigation Proposal per bot carrying a `root_cause_attribution` block. Operator sees the cause, not a wall of repeated denials.

3. **Distinguish the four failure modes**, because the right response differs:
   - **Approval timeout** → operator-routing problem ("you didn't see this"). Action: surface, optionally propose a different approval cadence.
   - **Exec denied** → policy problem. Action: ConfigPatch proposal to extend allowlist, via existing L2 applier.
   - **Preflight blocked** (OC v5.26+ blocking `python`/pipes) → bot needs to rephrase or use a different tool. Action: Investigation pointing at the upstream issue.
   - **MCP unreachable** → integration problem. Action: integration-probe-style re-check + repair proposal.

---

## Inventory: what we already have

| Surface | Where | Notes |
|---|---|---|
| Per-turn struggle features | `{shared_dir}/annotations/<bot>/<date>.jsonl` (`turn_annotation.struggle_features`) | Counts `tool_error_count`, `tool_retry_count`, `restart_markers`, `clarification_loops`, `tokens_per_progress`. Already populated. |
| Session-level rollup | Same files, `session_summary` records | `correction_detected`, `outcome`, `tier_confidence` |
| Static exec policy | `/Users/<bot>/.openclaw/exec-approvals.json` | Per-bot allowlist; read by `audit.py` and `security_warden` |
| Tool call detail | OC `sessions.sqlite` (per-bot), `turn_detail.py` extracts pairs | Full tool_use/tool_result content available; not currently aggregated |
| Approval timeout signal | Nowhere structured | OC's chat-side message is the only visible artifact; the underlying timeout isn't logged to a watchable file |
| Investigation toolkit | `investigation/` (just shipped) | `correlated_signals`, `recent_config_changes`, `config_intent`, `peer_baseline`, `proposal_history` all directly applicable |

The annotation layer is the cheap path. The OC session detail is the expensive path (deeper attribution) — and we already have `turn_detail.py` that knows how to parse it.

---

## Failure modes and signatures

Four modes, ordered by detection cost (cheapest first):

### Mode 1 — Tool-error burst (cheapest)

`turn_annotation.struggle_features.tool_error_count > 0` over a session window.

Signature: bot called tools and got error replies. Doesn't distinguish denial vs runtime error vs MCP failure, but it's the cheap pre-filter — every other mode below presents as elevated tool_error_count first.

### Mode 2 — Exec-policy denial

Mode 1 + tool_result content matches OC's denial shape (e.g. content includes "denied", "exec-policy", "not approved"). Detection: scan recent session tool_result blocks for the denial signature; producer reads from `turn_detail.extract_tool_pairs`.

When detected: cross-reference the bot's exec-approvals.json. If the blocked command pattern isn't in the allowlist *and* the manifest declares it (`INSTALLED_APPS.md` mentions it), the gap is fixable — propose extending allowlist.

### Mode 3 — Approval timeout

Mode 1 + tool_result indicates "approval timed out" / "approval pending" with no resolution turn. OC's 30-min TTL means this presents as the assistant turn completing without the tool call resolving — the next turn typically retries or surfaces the failure in prose.

When detected: this is an operator-routing problem, not a config problem. Propose: surface to operator at higher cadence (notifications, evo DM) and optionally suggest a faster-approval channel.

### Mode 4 — Preflight block

Mode 1 + tool_result matches OC v5.26+ preflight signatures (`python` / `node` / pipes / `&&` / `>` / `-c`). The bot's exec policy is `full` but OC's preflight still blocks.

When detected: the bot needs to rephrase (wrap in a script the allowlist permits) or the operator needs to upgrade OC. Propose: Investigation referencing openclaw#87371 + suggested rephrasings.

### Mode 5 (deferred) — Optimistic-reply contradiction

The team-bot-a case: the bot composed a reply *before* the exec failed; the visible artifact is the contradiction. Detection requires diffing the assistant's stated outcome against the tool's actual outcome — content-level inspection. Deferred to a follow-on spec; the upstream cause (one of modes 1-4) is already surfaced by this spec, so the contradiction gets context even without dedicated detection.

---

## Architecture

New producer + new investigating generator, paralleling the cost_watchdog + bloat_investigator pair.

### New producer: `exec_outcome_watchdog`

Lives at `packages/analyzer/exec_outcome_watchdog.py`. Reads `turn_annotation` records from `{shared_dir}/annotations/<bot>/<date>.jsonl` for the trailing window. Detectors:

| Detector | Fires when | Signal type |
|---|---|---|
| `detect_tool_error_burst` | rolling 7d tool_error_count/session above baseline | `tool_error_burst` |
| `detect_exec_denied` | tool_result content matches denial signature | `exec_denied` |
| `detect_approval_timeout` | tool_use without resolution + assistant turn references "approval timed out" | `approval_timeout` |
| `detect_preflight_block` | tool_result content matches OC preflight signature | `preflight_block` |

Each detector produces Signals in the same shape as `cost_watchdog` — `signature`, `producer`, `type`, `severity`, `details` with `vector="exec_outcome"`, `what_it_means`, `fix_steps`.

Where the detector needs tool_result *content* (modes 2-4), it pulls from session detail via `turn_detail.extract_tool_pairs` — the existing extractor handles both inline and separate tool_use/tool_result block shapes.

### New generator: `exec_outcome_investigator`

Lives at `packages/analyzer/generators/exec_outcome_investigator/`. Mirrors `bloat_investigator`:

- `charter.yaml` — declares cost dimension, allowlist [Investigation, ConfigPatch], cadence daily, resolves_when_silent
- `observe.py` — consumes `tool_error_burst`, `exec_denied`, `approval_timeout`, `preflight_block` Signals on the bot; gathers correlated evidence; runs attribution rules; emits one Proposal per investigated bot
- `attribution.py` — named rules, ambiguous fallback

Attribution rules (first match wins):

1. **`exec_denied_allowlist_gap`** — `exec_denied` fires + the blocked command appears in the bot's `INSTALLED_APPS.md` manifest + isn't in `exec-approvals.json`. Confidence: high. Action: `ConfigPatch` extending allowlist via the existing L2 applier (`UpdatePermissionConfig`).
2. **`approval_timeout_operator_missed`** — `approval_timeout` fires + recent operator activity in admin UI is quiet. Confidence: medium. Action: Investigation surfaced to operator + suggestion to enable a faster approval channel.
3. **`preflight_block_known`** — `preflight_block` fires with a known OC v5.26 signature. Confidence: high. Action: Investigation referencing openclaw#87371 + the canonical rephrasing template.
4. **`tool_error_burst_unclassified`** — `tool_error_burst` fires alone (modes 2/3/4 didn't latch). Confidence: low. Action: Investigation listing the failing tools + retry counts + asking the operator to inspect.
5. **`ambiguous`** — fallback. Always emits an Investigation Proposal with whatever evidence was gathered.

Toolkit reuse: `correlated_signals` for cross-signal evidence; `recent_config_changes` to see if exec-approvals.json was recently edited (operator action that might explain a new denial); `config_intent` to suppress on deliberate `tools.exec.security = deny` choices; `proposal_history` + `operator_already_declined` to suppress re-emission of the same denied-and-dismissed proposal.

### Why a separate generator instead of extending bloat_investigator

Different evidence shapes (cost cache writes vs tool errors). Different action kinds (workspace rotation vs allowlist extension). Different attribution rules. The toolkit is shared; the generator boundary is the contract between "envelope-growth cluster of cost" and "exec-outcome cluster of failure." Two specialized generators are easier to calibrate than one polyfunctional one.

---

## Phased plan

### Phase 1 — Mode 1 (cheap baseline)

- `detect_tool_error_burst` reading annotation JSONL — pure aggregation over data we already produce.
- Wire into a new `run_for_bot` entry point in `exec_outcome_watchdog.py`.
- New Signal type `tool_error_burst`.
- Backtest: scan the last 30 days of every bot's annotations; report which bots would fire. Calibrate threshold from real distribution.

Smallest possible PR. End-to-end producer + Signal lands; no generator yet.

### Phase 2 — Modes 2-4 + the investigator generator

- Add `detect_exec_denied`, `detect_approval_timeout`, `detect_preflight_block` — each reads tool_result content via `turn_detail.extract_tool_pairs`.
- New Signal types `exec_denied`, `approval_timeout`, `preflight_block`.
- `exec_outcome_investigator` generator: charter, observe, attribution rules 1-4.
- Register in `_CONTEXT_FACTORIES`.
- Operator UI: the `renderProposalRootCauseAttribution` block from the smarter-generators PR already handles this generator's output unchanged — the block is generator-agnostic.

### Phase 3 — Calibration + ConfigPatch wiring

- Connect rule 1 (`exec_denied_allowlist_gap`) to the existing `UpdatePermissionConfig` L2 applier so the operator can one-click approve the allowlist extension.
- Wire the calibration loop deferred from the smarter-generators spec: track which attribution rules fire correctly, demote ones that consistently mis-attribute.
- Add Mode 5 (`optimistic-reply contradiction`) as a follow-on if the team-bot-a-case pattern recurs.

### Phase 4 — Operator-routing improvements

The approval-timeout case (Mode 3) exposes that today's approval surface (Telegram in evo's case, OC's terminal otherwise) has no cadence-tuning. If `approval_timeout_operator_missed` fires repeatedly on the same bot, the system should escalate to a different channel rather than re-suggest the same one. This is a wider design question — separate spec.

---

## Acceptance criteria

A second Security-Bot-shape exec-outcome event (e.g. another bot tries `openclaw doctor` and the approval times out) must:

1. Fire `tool_error_burst` from `exec_outcome_watchdog` (Phase 1).
2. Fire `approval_timeout` once Phase 2 ships (specific mode).
3. `exec_outcome_investigator` produces one Proposal per affected bot with `cause_key=approval_timeout_operator_missed`, carrying the failing tool name and the assistant's pre-failure reply as evidence.
4. The Proposal renders in the admin UI with the attribution block (no UI changes required — Phase 4 of the prior spec already covers this).

A second team-bot-a-shape exec-denial event must:

1. Fire `tool_error_burst` + `exec_denied`.
2. The investigator's `exec_denied_allowlist_gap` rule fires.
3. Proposal carries a one-click ConfigPatch to extend the bot's allowlist with the blocked command.
4. Cross-references `INSTALLED_APPS.md` (manifest declared the capability) so the operator sees the gap is mechanical.

Backtest on the actual team-bot-a 2026-05-24 case: the producer should fire on that bot's annotation history; the investigator's rule 1 should hit.

---

## Open questions

- **Is OC's "approval timed out" surfaced as structured tool_result content, or only as a chat message?** If the latter, Mode 3 detection requires us to parse assistant prose, which is fragile. Worth filing an upstream issue (mirror of openclaw#85841) asking for a structured event.
- **For Mode 2, what's the exact denial signature in tool_result content?** Need to sample real denials from the team-bot-a case before threshold-setting. Quick win: grep recent annotations for "denied" / "exec-policy" once Phase 1 lands.
- **Should the `exec_outcome_watchdog` run hourly or daily?** Approval timeouts are 30-min-bounded — a daily run can be 24h late. Trade-off: hourly is more responsive but burns more CPU on sweeping annotations. Probably hourly with a 2-hour cooldown per `(bot, mode)`.
- **How does this interact with the safety nets shipped 2026-05-23?** The L1 cost breaker can trip on bots with runaway exec errors (each retry costs LLM tokens); we shouldn't fire `tool_error_burst` *and* `daily_cap_usd` proposals on the same bot in the same hour. The smarter-generators spec's suppression-by-breaker already handles this; just need to add `tool_error_burst` to `_SUPPRESSIBLE_TYPES_TO_CATEGORY` mapped to `"automation"`.

---

## Sequence for the user

Phase 1 first — one producer, one Signal type, backtestable across 30 days of annotations on the current pod. The output is data — we see which bots would have fired, calibrate the threshold from reality, ship. Phase 2 is the architectural payoff (the four-mode attribution + Investigation Proposal with named cause). Phase 3 is the autonomous-fix path (one-click allowlist extension via existing L2). Phase 4 only earns its slot if approval-timeout recurrence shows up in the data after Phase 2.
