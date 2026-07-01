# Config-Intent System — design spec

**Status:** Draft for discussion · 2026-05-21
**Author:** pod-admin + claude (design dialogue)
**Motivating memory:** [feedback_generators_consider_intent](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_generators_consider_intent.md) (the locked design)
**Adjacent memory:** [project_l1_l2_applier_architecture](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_l1_l2_applier_architecture.md) (the write seam this hooks into)
**Motivating incident:** PR #1430 — `auth_drift_filler` proposing `Restore <bot> tools.exec.security → 'deny'` for six bots whose `full` / `allowlist` values were deliberate (codex plugin, security warden, etc.). Recurring noise every sweep, forever.

---

## 1. Problem + principles

### 1.1 The failure mode

`auth_drift_filler` keeps proposing `Restore <bot> tools.exec.security → 'deny'` for every bot whose value has drifted from `deny`. On 2026-05-21 there were six bots in this state. Five had `full`; one had `allowlist`. The generator emits one proposal per drifted bot, every sweep, forever — because its rule is **"if drifted, propose revert"** with no consideration of *whether the drift is intentional*.

Evo (in the drawer) reasoned correctly about this in the same transcript: *"team-bot-a has codex plugin enabled — codex typically needs exec. Restoring deny may break it."* Evo had access to:

- the current `openclaw.json` contents
- the baseline
- the plugin list
- the operator's earlier configuration choices

The generator had access to the same data, used none of it, and produced a noisy proposal — and would have produced exactly the same proposal next sweep, and the sweep after that.

### 1.2 The user moment that anchors the design

From the 2026-05-21 design transcript:

> **Operator:** "If we're checking against a baseline that doesn't match reality, we're going to keep firing forever. Should we have an inspector that decides whether a drift is intentional before we emit a proposal?"
>
> **Pod-Admin:** "Yes — we must try to understand the context and be smart. We don't want a system that recommends fighting upstream choices the operator already made. If they installed codex, they consented to exec=full as a consequence. The system should surface the trade-off, not undermine the upstream choice."

Lock-in: the operator's *upstream choice* is the operative consent. Everything downstream from that choice is downstream from operator-granted authority, and the system's job is to **audit, flag, and warn — not to recommend reverts**.

### 1.3 Principles

These four are non-negotiable and shape every concrete decision in this spec:

1. **Trust but verify.** When the operator makes an upstream choice (install a plugin, enable a tool, accept a proposal), the resulting config state is *not drift*. It's a consequence of consent. The system surfaces the trade-off, never undermines the choice.

2. **Inference-first UX.** Default is *not* "prompt operator for a reason." Default is **infer + record silently when confidence is high**. Most operators don't know what `tools.exec.security` means. Asking them is noise.

3. **Narrow high-signal over elaborate noise** (from [feedback_rsi_design_approach](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_rsi_design_approach.md)). Context-free "optimum-vs-current" generators violate this once *any* deliberate deviation exists. Without intent, every such generator devolves into recurring nuisance.

4. **Cheap by default** (from [feedback_rsi_low_cost_preference](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_rsi_low_cost_preference.md)). The intent-check itself is pure Python (file read + comparison). The LLM inference layer is one haiku call per config-write — budget well under $0.001 per write. Cost-monitoring infra that itself burns budget defeats the point.

### 1.4 Why this affects more than `auth_drift_filler`

The same context-free pattern is latent in every "optimum-vs-current" generator:

| Generator | Field it watches | Failure mode once any intentional deviation exists |
|---|---|---|
| `auth_drift_filler` | `tools.exec.security`, etc. | Proposes revert for plugin-driven `full` (codex, etc.) |
| `cron_caps_filler` | `payload.maxTurns`, `payload.maxBudgetUsd` | Would propose capping a deliberately-uncapped cron |
| `cache_ttl_tuner` (planned) | TTL fields | Would propose raising TTL when operator deliberately set short TTL |
| Future "optimum-vs-current" generators | (any) | Same pattern, by construction |

This isn't a single-generator bug. It's a **generator-design rule**: generators must reason from *context*, not just compare against a static optimum. The config-intent system makes that reasoning structured, repeatable, and cheap.

---

## 2. Data schemas

The system stores intents in **two synchronized forms**: a sidecar file (canonical, full record) and an inline annotation in `network.json` (compact, operator-visible when reading the config). Plus a third internal log for generator hygiene.

### 2.1 Sidecar — `{shared_dir}/config_intents/<bot_id>.json`

Canonical store. One file per bot. Owned by the `evolve` user (no sudo/`/tmp` staging needed — `{shared_dir}` already has the right ACL; see [CLAUDE.md "Arbiter on-disk layout"](../CLAUDE.md)).

```json
{
  "bot_id": "team-bot-a",
  "schema_version": 1,
  "intents": [
    {
      "id": "intent-9a4f2c",
      "field_path": "tools.exec.security",
      "value": "full",
      "reason": "codex plugin requires exec",
      "depends_on": {"plugin": "codex"},
      "set_at": "2026-05-14T10:00:00Z",
      "set_by": "inferred:high",
      "set_by_detail": "codex plugin installed 2 minutes before this field changed (2026-05-14T09:58:11Z)",
      "audit_history": [
        {"event": "set", "at": "2026-05-14T10:00:00Z", "actor": "inference_layer"},
        {"event": "audited", "at": "2026-05-18T12:00:00Z", "result": "intent_valid", "checked_by": "auth_drift_filler"}
      ]
    },
    {
      "id": "intent-b71e08",
      "field_path": "tools.web.fetch.enabled",
      "value": true,
      "reason": "morning briefing pulls calendar invites by URL",
      "depends_on": null,
      "set_at": "2026-05-10T08:30:00Z",
      "set_by": "pod_admin (admin UI)",
      "set_by_detail": "operator entered reason in Save popover",
      "audit_history": [
        {"event": "set", "at": "2026-05-10T08:30:00Z", "actor": "pod_admin"}
      ]
    }
  ]
}
```

**Field definitions:**

| Field | Type | Required | Notes |
|---|---|---|---|
| `bot_id` | string | yes | Matches `network.json::bots.<id>` (logical name, not necessarily macOS account). |
| `schema_version` | int | yes | `1` for this revision. |
| `intents[].id` | string | yes | `intent-<6-char>` (sufficient for per-bot uniqueness). Created at first-write. |
| `intents[].field_path` | string | yes | Dotpath into `openclaw.json` (or other config surface — see §2.4 extensibility). |
| `intents[].value` | any JSON | yes | The non-baseline value that is intentional. Stored as the literal expected to be present in config. |
| `intents[].reason` | string | yes | Natural-language explanation. Operator-readable. |
| `intents[].depends_on` | object \| null | optional, default null | If the intent is coupled to another resource (plugin, integration, etc.), name it. See §2.3. |
| `intents[].set_at` | ISO-8601 UTC string | yes | When the intent was first recorded. |
| `intents[].set_by` | string | yes | Taxonomy below in §2.2. |
| `intents[].set_by_detail` | string | optional | Free-text detail (script name, popover note, inferred-reasoning trace). |
| `intents[].audit_history` | list[object] | yes (≥1 entry) | Append-only event log. Each entry has `event`, `at`, plus event-specific fields. |

**`audit_history[].event` values:** `set`, `updated`, `audited`, `stale_flagged`, `revoked`. Each entry's other fields are event-specific (e.g., `audited` carries `result` and `checked_by`; `updated` carries `from_value` and `to_value`).

### 2.2 The `set_by` taxonomy (8 categories)

Locked from the design memory. Every intent has exactly one `set_by` value:

| Value | When | Reason auto-recorded? |
|---|---|---|
| `pod_admin (admin UI)` | Operator clicked Save in admin UI with a non-baseline value | Inference attempts first; popover only if inference confidence is low |
| `pod_admin (CLI)` | Operator ran `evolve-admin` command that touched config | Yes — `reason = "set via CLI: <command name>"` plus any `--reason` arg |
| `applier:<proposal_id>` | Applied via proposal pipeline (any L1/L2 applier) | Yes — inherits the proposal's `problem` / `admin_surface_summary` |
| `migration:<id>` | A migration script wrote the value (e.g., OC 2026.5.18 `exec_deny_migration`) | Yes — `reason = "set by migration <id>"` plus script-provided detail |
| `plugin_side_effect:<plugin_id>` | Plugin install/enable code path mutated the field as a documented consequence | Yes — the plugin → field dep map (§6) provides the reason text |
| `inferred:high` / `inferred:medium` | LLM-deduction layer reasoned about a write that arrived without explicit `set_by` | Yes — model-provided reason; confidence determines whether it's silent or prompts follow-up |
| `inferred:low` | Same, but confidence too low to auto-record | Recorded as a *queued* intent only; UI prompts operator at next admin-UI visit |
| `drift` | Reserved value — the *absence* of an intent for a non-baseline value is what generators interpret as "actual drift". Never literally stored (drift means no record). | n/a |

> **Note:** `drift` is the implicit *seventh* taxonomy entry; the design memory calls it out as one of the eight categories for symmetry. In storage we only ever materialize the other seven — drift is "no entry exists."

### 2.3 Plugin-coupled intents (`depends_on.plugin`)

When `depends_on.plugin` is set, the intent is *contingent on that plugin remaining enabled*. The `intent_still_valid()` helper (§4) checks the bot's plugin list at audit time. If the plugin is no longer enabled, the intent auto-flags stale and the generator emits an **audit-only signal** (not a proposal):

> *"You set `tools.exec.security=full` to enable the `codex` plugin; `codex` is no longer installed on `team-bot-a`. Want to restore the baseline?"*

This is once-per-stale-transition. Layer-2 generator memory (§2.5) prevents re-emission.

Future extensions of `depends_on`:

```json
"depends_on": {"plugin": "codex"}
"depends_on": {"integration": "morning_briefing"}
"depends_on": {"sibling_intent": "intent-b71e08"}
"depends_on": {"baseline_override": true}  // operator explicitly opted out of the baseline opinion for this field
```

For v1 we ship `plugin` only. The schema is `{<kind>: <value>}` (single key) so adding kinds doesn't break old readers.

### 2.4 Inline annotation in `network.json`

A compact summary lives in `network.json::bots.<bot_id>.config_intents` so operators reading the config see at a glance that fields have recorded intent:

```json
"bots": {
  "team-bot-a": {
    "role": "member",
    "user": "team-bot-a",
    "...": "...",
    "config_intents": [
      {"field": "tools.exec.security", "value": "full", "reason_id": "intent-9a4f2c"},
      {"field": "tools.web.fetch.enabled", "value": true,   "reason_id": "intent-b71e08"}
    ]
  }
}
```

Just enough to be visible when reading `network.json`. The `reason_id` references the sidecar record for the full details.

**Why both?**

- **Sidecar is authoritative.** Full history, append-only audit, generator-queried at sweep time.
- **Inline is a visibility tool.** Operators reading `network.json` (or staring at the admin UI's network tab) see intent flags inline without a second file lookup. Useful in incident response, git diffs of `network.json`, etc.
- **They are kept in sync by `evolve_admin.config.intent.set_intent()`** (the only legitimate writer; see §3 + §2.7).

### 2.5 Generator-internal "already-informed" log

Separate file: `{shared_dir}/config_intents/_generator_memory.jsonl`. Append-only.

```jsonl
{"at": "2026-05-21T12:00:00Z", "generator_id": "auth_drift_filler", "signature": "team-bot-a:tools.exec.security:audit_only:intent-9a4f2c", "event": "audit_signal_emitted"}
{"at": "2026-05-22T12:00:00Z", "generator_id": "auth_drift_filler", "signature": "team-bot-a:tools.exec.security:audit_only:intent-9a4f2c", "event": "audit_signal_suppressed_already_emitted"}
```

Purpose: prevent a generator from re-emitting the same audit-only signal every sweep. Not operator-visible.

**Signature shape:** `<bot_id>:<field_path>:<reason_category>:<intent_id>` where `reason_category ∈ {audit_only, stale_flagged}`. A signature is dedup'd against the last 90 days of log entries before emission. If the intent changes (new `intent_id`), the signature differs and the generator emits afresh.

Why JSONL and not JSON? Append-only without rewrites is cheaper and survives partial writes. Retention is 1 year (mirrors signal log retention in CLAUDE.md "Signal store" section).

### 2.6 File layout under `{shared_dir}`

```
{shared_dir}/
├── config_intents/
│   ├── team-bot-a.json                    ← per-bot sidecar (§2.1)
│   ├── admin-bot.json
│   ├── team-bot-b.json
│   ├── ...
│   └── _generator_memory.jsonl     ← internal dedup log (§2.5)
```

All paths under `{shared_dir}` are owned by the `evolve` user. No sudo, no `/tmp` staging — see CLAUDE.md "Arbiter on-disk layout" for the precedent.

### 2.7 Sync invariants

The sidecar and inline annotation must remain consistent. The invariant is enforced by routing *all* writes through a single helper:

**New module:** `packages/admin/evolve_admin/config/intent.py` (or `packages/analyzer/permissions/intent.py` — see §10 open question).

```python
# packages/admin/evolve_admin/config/intent.py
def set_intent(
    bot_id: str,
    field_path: str,
    value: Any,
    *,
    reason: str,
    set_by: str,
    set_by_detail: str | None = None,
    depends_on: dict | None = None,
    shared_dir: Path | None = None,
    network_path: Path | None = None,
) -> str:
    """Record (or update) an intent. Writes sidecar + inline atomically.

    Returns the intent_id.
    """

def get_intent(bot_id: str, field_path: str, *, shared_dir: Path | None = None) -> dict | None:
    """Return the active intent for (bot, field), or None."""

def list_intents(bot_id: str, *, shared_dir: Path | None = None) -> list[dict]:
    """All active intents for a bot."""

def revoke_intent(bot_id: str, intent_id: str, *, shared_dir: Path | None = None, actor: str) -> None:
    """Mark an intent as revoked. Appends to audit_history; removes inline annotation."""

def intent_still_valid(intent: dict, *, shared_dir: Path | None = None) -> bool:
    """For plugin-coupled intents, verify the plugin is still enabled."""
```

**Atomicity:**

1. Read sidecar.
2. Apply mutation in-memory.
3. Temp-file + rename inside `{shared_dir}/config_intents/` for the sidecar.
4. Read `network.json` via `evolve_admin.config.load_network`, mutate `bots.<bot_id>.config_intents`, write via `evolve_admin.config.save_network` (existing /tmp + sudo seam).
5. If step 4 fails after step 3 succeeded, append a `desync_warning` entry to `_generator_memory.jsonl` — next reconciliation pass (§8.6) self-heals from sidecar (authoritative).

---

## 3. Write-path integration — `record_intent_on_set`

For each code path that mutates a config field, this section names the specific file + function that calls `set_intent()`. **The sidecar is updated; not the config file itself** — `set_intent()` is purely a metadata write.

| `set_by` | Code path (file:function) | Trigger | Reason source |
|---|---|---|---|
| `pod_admin (admin UI)` | [packages/admin/evolve_admin/web/server.py:19551](../packages/admin/evolve_admin/web/server.py) — `api_permissions_config_update` (and siblings `_approval_update`, `_cron_upsert`, etc.) → wraps existing `_operator_create_apply` (line 19102) | Before returning, calls `set_intent()` for each changed field whose post-write value ≠ baseline. | Inference layer first (§5); fall back to operator-supplied note via Save popover. |
| `pod_admin (CLI)` | [packages/admin/evolve_admin/cli.py](../packages/admin/evolve_admin/cli.py) Click commands that mutate config (any `evolve-admin permission set …`-style command — currently bundled into wizard flows) | After successful write, calls `set_intent()`. | `reason = f"set via CLI: {ctx.command.name}"` plus any `--reason` arg. |
| `applier:<proposal_id>` | [packages/analyzer/arbiter/appliers/permissions.py:137](../packages/analyzer/arbiter/appliers/permissions.py) — `UpdatePermissionConfigApplier.apply` (and the four sibling appliers in the same file) | At end of `apply()` after `write_openclaw_fields` succeeds, for each `path` in `fields` whose new value ≠ baseline. | Inherits `proposal.problem` (truncated to 200 chars). The applier already has access to the proposal — wire `proposal_id` through `apply()` signature so the reason can reference it. |
| `migration:<id>` | Migration scripts. For OC-side migrations (e.g., `exec_deny_migration` in OC 2026.5.18) this is currently external; for Evolve-side, the canonical seam is whatever script writes `openclaw.json`. **Spec deliverable (Phase 1):** introduce a `record_migration_intent()` convenience that migration scripts call as part of their write. | Migration script runs, mutates field. | Script provides `id`, `reason`, optional `depends_on`. |
| `plugin_side_effect:<plugin_id>` | Plugin install/enable code paths (located at [packages/admin/evolve_admin/deploy.py](../packages/admin/evolve_admin/deploy.py) `_setup_oc_for_bot` and the OC-side plugin-enable hook). Each plugin's install routine **must explicitly invoke** `set_intent()` for any non-baseline field state it leaves behind. | Plugin enable completes. | Map lookup against the plugin → field dependency map (§6). |
| `inferred:<confidence>` | New module: `packages/analyzer/permissions/intent_inference.py` (Phase 3 deliverable). Invoked from `set_intent()` itself when called without an explicit `reason`. | Any write that doesn't have a known reason. | Single haiku call; output structure in §5. |
| `drift` | (No code path — `drift` is the absence of an intent.) | n/a | n/a |

**One write helper, many callers.** The contract is: *every code path that writes a permission-config field calls `set_intent()` exactly once*, either with an explicit `set_by` + `reason`, or with `set_by="inferred:auto"` to delegate to the inference layer.

### 3.1 What `set_intent()` actually does, end-to-end

1. Validates `field_path` is well-formed (member of `_ALLOWED_FIELDS` for permission surfaces; extensible to other surfaces in v2).
2. If `set_by="inferred:auto"` and Phase 3 is live → invoke `intent_inference.infer()` (§5). Replace `set_by` with the returned `inferred:high|medium|low`.
3. If `set_by="inferred:low"` → mark intent as `queued` (separate field); UI surface (§7) prompts operator to confirm.
4. Otherwise: write the intent to sidecar via temp-file + rename.
5. Update the inline annotation in `network.json` via `evolve_admin.config.save_network`.
6. Return `intent_id`.

### 3.2 sudoers + ACL implications

No new sudoers grants needed for sidecar writes — `{shared_dir}/config_intents/` is under evolve ownership and writable directly. **However,** Phase 1 must add the new subdirectory to the wizard's `set_evolve_read_acl` equivalent and ensure the directory exists at first run. The canonical bootstrap seam is [packages/admin/evolve_admin/setup_wizard.py:_render_evolve_sudoers](../packages/admin/evolve_admin/setup_wizard.py); add a one-time `mkdir -p` for `{shared_dir}/config_intents/` to the post-install step.

If a future expansion routes intent records into per-bot `.openclaw/workspace/evolve/` (we shouldn't — but if), then standard /tmp + sudo /bin/cp pattern applies; the writer module to use is [permissions/writer.py:_bot_owned_write](../packages/analyzer/permissions/writer.py).

---

## 4. Read-path / generator integration — `investigate_before_propose`

This section defines the generator-side seam. The current `auth_drift_filler` pattern:

```python
# packages/analyzer/generators/auth_drift_filler/signal_proposals.py — CURRENT
def make_drift_proposals(signal) -> list[Proposal]:
    for field, diff in diffs.items():
        if diff.expected != diff.observed:
            emit_proposal(...)
```

The new pattern:

```python
# packages/analyzer/generators/auth_drift_filler/signal_proposals.py — TARGET
from evolve_admin.config.intent import get_intent, intent_still_valid
from analyzer.generators._intent_memory import already_emitted, record_emission

def make_drift_proposals(signal) -> list[Proposal]:
    out = []
    for field, diff in diffs.items():
        if diff.expected == diff.observed:
            continue

        intent = get_intent(bot_id=signal.bot_id, field_path=field)

        if intent is None:
            # No intent → actual drift. Emit proposal as before.
            out.append(_make_revert_proposal(signal, field, diff))
            continue

        if intent_still_valid(intent):
            # Deliberate deviation. Don't propose. Optionally emit an
            # audit-only signal IF the generator hasn't already told the
            # operator about this trade-off recently.
            sig = f"{signal.bot_id}:{field}:audit_only:{intent['id']}"
            if not already_emitted(generator_id="auth_drift_filler", signature=sig):
                _emit_audit_only_signal(signal, field, intent)
                record_emission(generator_id="auth_drift_filler", signature=sig)
            continue

        # Intent exists but is stale (e.g., depends_on.plugin no longer enabled).
        sig = f"{signal.bot_id}:{field}:stale_flagged:{intent['id']}"
        if not already_emitted(generator_id="auth_drift_filler", signature=sig):
            _emit_intent_stale_proposal(signal, field, intent)
            record_emission(generator_id="auth_drift_filler", signature=sig)

    return out
```

### 4.1 The helpers it depends on (new — Phase 1)

| Helper | Where | Returns |
|---|---|---|
| `get_intent(bot_id, field_path)` | `evolve_admin/config/intent.py` | `dict \| None` — the active intent record for that pair, or None if none exists |
| `intent_still_valid(intent)` | `evolve_admin/config/intent.py` | `bool` — for plugin-coupled intents, checks the bot's plugin list; for non-coupled, returns `True` (intents don't expire on time alone) |
| `already_emitted(generator_id, signature)` | `analyzer/generators/_intent_memory.py` | `bool` — checks the JSONL dedup log; True if same signature emitted in last 90 days |
| `record_emission(generator_id, signature)` | `analyzer/generators/_intent_memory.py` | Appends to `_generator_memory.jsonl` |

### 4.2 `intent_still_valid()` — concrete definition

```python
def intent_still_valid(intent: dict, *, shared_dir: Path | None = None) -> bool:
    """An intent is valid unless its preconditions are no longer met.

    Plugin-coupled: check the bot's plugin list (via existing
    permissions.inventory or network.json bots.<id>.plugins).
    Non-coupled: always valid — intents don't expire on time alone.
    """
    deps = intent.get("depends_on") or {}
    if "plugin" in deps:
        plugin_id = deps["plugin"]
        return _plugin_is_enabled_for_bot(intent["bot_id"], plugin_id)
    # Future: integration, sibling_intent, baseline_override
    return True
```

The `_plugin_is_enabled_for_bot()` helper reuses the existing OC-side plugin discovery — see [packages/analyzer/permissions/inventory.py](../packages/analyzer/permissions/inventory.py) for the equivalent lookup pattern (plugin list comes from the bot's `openclaw.json::plugins` block or `network.json::bots.<id>.plugins` depending on which surface it lives on; the inventory module knows).

### 4.3 Audit-only signals — proposal vs signal distinction

When an intent matches, the generator does **not** emit a `Proposal`. It either emits nothing, or emits a low-severity **audit-only Signal** to the signal store (existing `signals.store.observe()` seam from CLAUDE.md "Signal store" section).

Audit-only signals appear under **Security → Intentional Deviations** (new subtab, §7.3) where the operator can review and revoke. They do **not** appear in Recommendations (which is for actionable proposals only).

This preserves the principle: *visibility is good, pestering is not*.

---

## 5. LLM inference layer (Phase 3)

The single haiku call invoked when a config write arrives without explicit intent. Lives at `packages/analyzer/permissions/intent_inference.py`. Called from `set_intent()` when `set_by="inferred:auto"`.

### 5.1 Model + cost

- **Model:** `claude-haiku-4-5-20251001` (cheapest current Haiku per CLAUDE.md model id list).
- **Tokens:** ~500 input, ~150 output → ~$0.0005 per write at current Haiku pricing.
- **Frequency:** Bounded by config-write rate (operator clicks / proposal applies). Realistic upper bound: a few dozen per day per pod. Annual cost ceiling: under $5.

### 5.2 Inputs (the prompt context)

| Input | Source |
|---|---|
| The diff: `{bot_id, field_path, old_value, new_value}` | The caller |
| Last 10 minutes of operator activity on this bot | New helper that reads from the existing audit/event log (see `audit_state.py` and `watchdog/<date>.jsonl`) — filtered by `bot_id` |
| Currently-enabled plugins on this bot | `network.json::bots.<id>.plugins` + `openclaw.json::plugins` block |
| Plugin → field dependency map for matched plugins | §6 |
| Existing intents on this bot (for pattern continuity) | `list_intents(bot_id)` |

### 5.3 Output schema

```json
{
  "reason": "codex plugin requires exec — install timestamp 2026-05-14T09:58:11Z preceded this write by 2 minutes",
  "depends_on": {"plugin": "codex"},
  "confidence": "high",
  "set_by": "inferred:high"
}
```

### 5.4 Confidence policy

| Confidence | Behavior |
|---|---|
| `high` | Record automatically. No operator prompt. The model has a clearly-implicated upstream cause (plugin install N minutes before, applier rationale that matches, etc.). |
| `medium` | Record, but queue for next-admin-UI-visit follow-up prompt. Operator can edit the recorded reason; default reason stands if they dismiss. |
| `low` | Don't auto-record. Surface a non-blocking "we noticed this change but couldn't deduce why — quick note?" prompt at next admin-UI visit. Stored as `intent.queued=true` in the meantime so generators treat it the same as "intent present" for noise-suppression. |

### 5.5 Failure modes

- **Model returns malformed JSON** → fall back to `set_by="inferred:low"`, `reason="(inference failed — please review)"`, schedule operator follow-up.
- **Model unreachable** → same fallback; do not block the config write.
- **Model returns a reason that contradicts known facts** (e.g., references a plugin that isn't installed) → fall back to `inferred:low` after a single contradiction check against the plugin list.

### 5.6 Where the call lives

Inside `set_intent()` synchronously. **Not** in the admin server (would block Save). **Not** in a separate daemon (would lag and create reconciliation pain).

If latency becomes a concern (a single Haiku call is ~500ms-1s; tolerable for a Save click), the fallback is to make `set_intent()` write the bare intent immediately with `set_by="inferred:auto"` and have a background sweep upgrade it to `inferred:high|medium|low` — but **this is not v1**. v1 is synchronous. Decision deferred to operational data.

---

## 6. Plugin → field dependency map

Required by both §5 (inference inputs) and §3 (`plugin_side_effect:` intent recording).

**Location:** `packages/analyzer/permissions/plugin_field_deps.yaml`

**Structure:**

```yaml
# packages/analyzer/permissions/plugin_field_deps.yaml
codex:
  required_fields:
    - field: tools.exec.security
      values: [allowlist, full]
      rationale: codex executes generated code locally; requires exec
    - field: tools.fs.workspaceOnly
      values: [false]
      rationale: codex needs to touch files outside workspace by design
slack:
  required_fields:
    - field: plugins.slack.enabled
      values: [true]
      rationale: enabled state is the plugin definition
brave:
  required_fields:
    - field: tools.web.search.enabled
      values: [true]
      rationale: brave is a web-search provider
```

**Format choice:** YAML (matches the existing `charter.yaml` convention in `packages/analyzer/generators/*/charter.yaml`).

**Maintenance burden:** Centralized at Evolve, not per-plugin metadata. Reasoning:

- OC plugins do not currently emit field-dependency metadata in a machine-readable form.
- The dep map is small (~10-20 plugins on a typical pod, each with 1-3 fields).
- Centralized keeps the data adjacent to the inference + intent code that uses it.
- When OC upstream adds plugin-metadata schemas that expose this, we can switch to discovery (open question §10).

**Lifecycle:** Updated by hand when a new plugin is adopted by the pod. PR-reviewed. Adding an entry has no runtime risk (the worst case is missing data — inference falls to lower confidence).

### 6.1 Reverse-lookup helper

```python
# packages/analyzer/permissions/plugin_field_deps.py
def plugins_implying(field_path: str, value: Any) -> list[str]:
    """Return plugin_ids whose required_fields include this (field, value)."""

def fields_implied_by(plugin_id: str) -> list[tuple[str, list[Any], str]]:
    """Return [(field_path, allowed_values, rationale)] for a plugin."""
```

These feed §5 (inference context) and §7.3 (operator-visible "this intent is because of plugin X" badge).

---

## 7. Operator UX (three surfaces)

### 7.1 Surface A — Admin UI Save, high-confidence inference (silent)

Operator changes `tools.exec.security` from `deny` to `full` via the Permissions panel. They click Save.

1. Existing flow: `POST /api/permissions/config` → `_operator_create_apply` → applier → write succeeds.
2. New flow (Phase 1): the route calls `set_intent(bot_id, field, value, set_by="pod_admin (admin UI)", reason=<inferred>)`.
3. Inference (Phase 3): a single Haiku call deduces `reason="codex plugin requires exec"` with `confidence=high`.
4. Operator sees the existing Save toast: *"Permission config updated."* Nothing more.

The recorded intent is visible later under Security → Intentional Deviations (§7.3) if they want to review.

### 7.2 Surface B — Admin UI Save, low-confidence inference (popover)

Same setup, but inference returns `confidence=low` (e.g., no recent plugin install, no applier context).

After the Save toast, a non-blocking popover appears in the bottom-right:

```
┌──────────────────────────────────────────────────────┐
│  Heads up — we recorded this change, but couldn't    │
│  figure out why.                                     │
│                                                       │
│  team-bot-a · tools.exec.security: deny → full              │
│                                                       │
│  Want to add a quick note? (optional)                │
│  ┌────────────────────────────────────────────────┐ │
│  │                                                │ │
│  └────────────────────────────────────────────────┘ │
│                                                       │
│              [ Skip ]   [ Save note ]                │
└──────────────────────────────────────────────────────┘
```

- Skip → intent stays recorded with `reason="(unspecified)"`. Generators still treat it as intent-present (no drift proposal).
- Save note → intent updated with operator-supplied text; `set_by="pod_admin (admin UI)"`.

Pattern reference: matches the existing toast + dismissible popover pattern used elsewhere in the admin UI (see Recommendations row actions).

### 7.3 Surface C — Security → Intentional Deviations (new subtab)

Sibling to Security → Posture / Security → Approvals (existing). Lists every active intent across the pod:

```
SECURITY · INTENTIONAL DEVIATIONS

┌─ team-bot-a ───────────────────────────────────────────────────────────┐
│ tools.exec.security = "full"          ▼ codex plugin requires exec │
│   recorded 2026-05-14 by inference (high confidence)              │
│   linked to plugin: codex                                          │
│   [ View history ]  [ Revoke intent ]                             │
├─────────────────────────────────────────────────────────────────┤
│ tools.web.fetch.enabled = true        ▼ morning briefing needs URL fetch │
│   recorded 2026-05-10 by pod_admin (admin UI)                     │
│   [ View history ]  [ Revoke intent ]                             │
└─────────────────────────────────────────────────────────────────┘

┌─ admin-bot ─────────────────────────────────────────────────────────┐
│ ...                                                              │
└─────────────────────────────────────────────────────────────────┘
```

**Actions per row:**

- **View history** — expands `audit_history` chronologically.
- **Revoke intent** — confirms, then calls `revoke_intent()`. The config field is **not** auto-reverted; revoking just removes the intent record. The next sweep will then see the field as actual drift, and `auth_drift_filler` will (legitimately) emit a revert proposal — which the operator can accept or reject normally.

Pattern reference: matches the Alerts page's per-signal row pattern (existing implementation in `routes_alerts.py`).

### 7.4 What is *not* on these surfaces

- No per-write blocking prompts. Saves complete; intent recording is a side effect.
- No "approve this intent" gate. The intent record is automatic; only the *config write itself* is operator-authorized (already handled by existing UI).
- No global "review all intents" required step. Intents accumulate; operators inspect when curious.

This matches the principle: trust but verify, inference-first, no per-fact approval queues (precedent: [feedback_profile_passive_growth](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_profile_passive_growth.md)).

---

## 8. Edge cases

### 8.1 Intent set, but the value is later reverted to baseline

Operator (or applier, or migration) returns the field to its baseline value. The intent now references a non-existent deviation.

**Behavior:** auto-archive the intent. Move it from `intents[]` to a new `intents_archive[]` list in the same sidecar file, with an `audit_history` entry `{"event": "auto_archived", "at": "...", "reason": "value returned to baseline"}`. Inline annotation is removed. The history is preserved for audit but doesn't affect generator behavior.

**Why archive vs delete:** preserves audit history. Cheap (the entries are small).

### 8.2 Two intents for the same (bot, field) with different values

Operator changes their mind: codex is removed and they want `exec=full` for a *different* reason now (say, a custom local script).

**Behavior:** last-write-wins. `set_intent()` for an existing `(bot_id, field_path)` *updates* the existing record in place — same `id`, mutated `value` / `reason` / `set_by`, plus an appended `audit_history` entry `{"event": "updated", "at": "...", "from_value": ..., "to_value": ..., "actor": ...}`. The history captures the change; the active record reflects the current truth.

### 8.3 Plugin in `depends_on.plugin` is renamed or removed

Intent references a plugin id that no longer exists in the plugin → field dep map (or in the bot's plugin list).

**Behavior:** `intent_still_valid()` returns `False`. The generator emits a one-time `intent_stale_flagged` signal: *"You set `team-bot-a::tools.exec.security=full` to enable `codex`; `codex` is no longer present. Restore the baseline?"* This is an actionable proposal (not audit-only) because the upstream cause is gone.

Generator memory ensures it fires once per stale transition, not every sweep.

### 8.4 Migration sets a field non-baseline; operator later wants to restore

Common case: OC 2026.5.18 migration writes `exec_security=deny`. Six months later operator wants to restore the *Evolve* baseline (which says `deny` too, in this hypothetical, but the migration's record is still there).

**Behavior:** operator clicks "Revoke intent" in Surface C. Intent is removed. Field value is *not* auto-changed by revocation — the operator can change it separately if they want, via the existing Permissions UI.

If the operator instead clicks an `auth_drift_filler` proposal that says "restore baseline" *while* the migration intent is still active: the proposal won't be emitted in the first place (intent suppresses it). To restore, the operator first revokes the intent, then the next sweep emits the proposal, then they accept it. **Two steps, deliberate.**

This is the right shape because migration-recorded intents are *contextual claims about why a value is set* — discarding the claim is a separate, deliberate act from changing the value.

### 8.5 Inference is wrong

Operator notices the recorded reason doesn't match reality ("codex plugin requires exec" — but they removed codex months ago and `exec=full` is now because of something else).

**Behavior:** Surface C "View history" → "Edit reason" inline editor. Updates the `reason` field, appends an `audit_history` entry `{"event": "updated", "at": "...", "from_reason": ..., "to_reason": ..., "actor": "pod_admin"}`. Confidence is reset to `set_by="pod_admin (manual correction)"`.

### 8.6 Sidecar and inline annotation get out of sync

E.g., someone hand-edits `network.json` without going through `set_intent()`. Or a sidecar write completes but the inline update fails.

**Reconciliation policy:** **Sidecar is authoritative**. Inline annotation is a *derived summary*. A reconciliation pass — triggered (a) at admin server startup, (b) before each generator sweep, (c) on any `save_network()` call — reads sidecar, rebuilds `bots.<id>.config_intents` from it, and writes if different.

Discrepancies are logged but not flagged to the operator — they self-heal silently. (If we eventually surface discrepancies, that's a separate decision; v1 is silent.)

### 8.7 The "value" of an intent isn't a scalar

E.g., `commands.native` is a *list*. Intent value is a list. Comparison for `intent_value == current_value` uses deep equality. Sidecar stores the value as-is (JSON serialization handles all primitive / dict / list shapes).

For partial-list overlap (operator added one item; intent only mentions two of three) — out of scope for v1. v1 treats values as whole units. If this matters in practice, it surfaces in Phase 5 and the intent schema gains a `value_mode: "exact" | "contains"` field.

---

## 9. Phased implementation plan

### Phase 1 — Foundations

**Goal:** the data + helpers exist. No generator wiring. Tests at the helper level.

**Deliverables:**

| File | New / Changed | Purpose |
|---|---|---|
| `packages/admin/evolve_admin/config/intent.py` | **new** | `set_intent`, `get_intent`, `list_intents`, `revoke_intent`, `intent_still_valid` (without plugin-coupled logic — Phase 5), plus sidecar I/O |
| `packages/admin/evolve_admin/config/__init__.py` | **new** (or extend existing) | Re-export the intent module |
| `packages/analyzer/permissions/plugin_field_deps.yaml` | **new** | The dep map (initial entries: codex, slack, brave, github, telegram, discord, home_assistant, notion, linear) |
| `packages/analyzer/permissions/plugin_field_deps.py` | **new** | `plugins_implying`, `fields_implied_by` |
| `packages/analyzer/generators/_intent_memory.py` | **new** | `already_emitted`, `record_emission` for the JSONL dedup log |
| `packages/admin/evolve_admin/setup_wizard.py` | changed | Ensure `{shared_dir}/config_intents/` is created during wizard run |
| `tests/admin/test_config_intent.py` | **new** | Unit tests for the helpers (round-trip, atomicity, sync invariant) |
| `tests/analyzer/permissions/test_plugin_field_deps.py` | **new** | Tests for the dep map loader |
| `tests/analyzer/generators/test_intent_memory.py` | **new** | Dedup-log tests |

**Test surface (silent-failure checklist per [feedback_two_pass_review_workflow](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_two_pass_review_workflow.md)):**

- Sidecar write with no shared_dir directory → must auto-mkdir, not silently no-op.
- Sidecar write race (two writers concurrent) → atomic rename guarantees one wins; second sees the first's record and updates.
- Inline annotation update failing after sidecar succeeds → reconciliation self-heals.
- `get_intent` on a bot with no sidecar → returns None cleanly, no exception.
- Schema-version mismatch → readable error, no data loss.

**Dependencies:** none. Phase 1 stands alone.

**Scope estimate:** ~500-700 LOC source + ~400-600 LOC tests. ~6-8 agent-hours.

### Phase 2 — Wire `auth_drift_filler`

**Goal:** prove the abstraction. The generator stops emitting noise.

**Deliverables:**

| File | Changed / New | Purpose |
|---|---|---|
| `packages/analyzer/generators/auth_drift_filler/signal_proposals.py` | changed | Insert `investigate_before_propose` step (§4) |
| `packages/analyzer/arbiter/appliers/permissions.py` | changed | `UpdatePermissionConfigApplier.apply` calls `set_intent` for each changed field whose post-write value ≠ baseline. Wires `proposal_id` through `apply()` signature. |
| `packages/admin/evolve_admin/web/server.py` | changed | `api_permissions_config_update` (and the four sibling routes) also call `set_intent` post-write with `set_by="pod_admin (admin UI)"` and a placeholder reason (since Phase 3 inference isn't live yet) |
| `tests/analyzer/generators/test_auth_drift_filler_intent.py` | **new** | Cases: (a) intent present + valid → no proposal, (b) intent present + stale → one stale_flagged proposal, (c) no intent → revert proposal as before, (d) audit-only signal not re-emitted on second sweep |

**Test surface (silent-failure checklist):**

- Generator runs with no `_generator_memory.jsonl` file → first emission goes through, log gets created.
- Intent file exists but is malformed JSON → generator treats as "no intent" and emits proposal (fail-open to the existing behavior, log a warning).
- Audit-only signal emission failure → does *not* prevent subsequent valid proposals.

**Dependencies:** Phase 1 helpers must exist and be tested.

**Scope estimate:** ~300-400 LOC source + ~400 LOC tests. ~4-5 agent-hours.

### Phase 3 — LLM inference layer

**Goal:** silent recording when the system can deduce why.

**Deliverables:**

| File | New / Changed | Purpose |
|---|---|---|
| `packages/analyzer/permissions/intent_inference.py` | **new** | The Haiku call wrapper; input assembly; output parsing; confidence policy |
| `packages/admin/evolve_admin/config/intent.py` | changed | `set_intent` invokes inference when `set_by="inferred:auto"` |
| `packages/analyzer/permissions/intent_recent_context.py` | **new** | Helper to assemble "last 10 minutes of operator activity" from `watchdog/` and `audit_state` |
| `tests/permissions/test_intent_inference.py` | **new** | Snapshot tests with fake Haiku responses; confidence-policy branching; failure-mode fallback |

**Test surface:**

- Model returns malformed JSON → falls back to `inferred:low`, intent still recorded.
- Model unreachable → falls back, write does not block.
- Recent-activity window is empty → inference still runs, returns `inferred:low` or `inferred:medium` based on plugin context alone.

**Dependencies:** Phases 1 + 2. Also depends on the haiku-class model being available — already used elsewhere in Evolve, no new auth.

**Scope estimate:** ~400-500 LOC source + ~300 LOC tests + prompt-engineering iteration. ~5-7 agent-hours.

### Phase 4 — Operator-visible audit UI

**Goal:** Surface C exists. Operators can browse and revoke.

**Deliverables:**

| File | New / Changed | Purpose |
|---|---|---|
| `packages/admin/evolve_admin/web/server.py` | changed | New routes: `GET /api/intents`, `GET /api/intents/<bot_id>`, `POST /api/intents/<intent_id>/revoke`, `POST /api/intents/<intent_id>/edit_reason` |
| `packages/admin/evolve_admin/web/templates/security_intents.html` (or React equivalent) | **new** | The Surface C view |
| `packages/admin/evolve_admin/web/templates/_intent_popover.html` | **new** | The Surface B popover partial |
| Existing Save toast routes | changed | Trigger Surface B popover when post-Save intent is `inferred:low` |
| `tests/admin/test_intent_routes.py` | **new** | API contract + revoke flow tests |

**Test surface:**

- Revoking an intent removes the inline annotation atomically.
- Editing a reason appends `audit_history` correctly.
- Cross-bot listing doesn't leak — only intents visible to the operator's authorized bot scope.

**Dependencies:** Phases 1 + 2 + 3.

**Scope estimate:** ~600-800 LOC source (mostly UI) + ~300 LOC tests. ~6-8 agent-hours.

### Phase 5 — Wire other generators

**Goal:** generalize. `cron_caps_filler`, `cache_ttl_tuner`, others gain the same investigate-before-propose step.

**Deliverables:**

| File | Changed | Purpose |
|---|---|---|
| `packages/analyzer/generators/cron_caps_filler/signal_proposals.py` | changed | Same pattern as auth_drift_filler |
| `packages/analyzer/generators/cache_ttl_tuner/signal_proposals.py` (if exists) | changed | Same |
| Each generator's tests | changed | Same coverage as Phase 2's test set |
| Possibly: extension of `_ALLOWED_FIELDS` in the applier whitelist + `plugin_field_deps.yaml` entries for fields specific to these generators | changed | As needed per generator |

**Dependencies:** Phases 1-3 (Phase 4 is independent).

**Scope estimate:** ~200 LOC per generator. ~2-3 agent-hours per generator. Batch as one PR or one-per-generator at author's discretion.

### MVP

Phase 1 + Phase 2 ship together as the MVP. The system stops noise from `auth_drift_filler` even before inference is live — admin-UI Saves and applier writes already produce explicit intents during Phase 2. Phase 3 (inference) turns the operator-prompt frequency down further. Phase 4 (UI) is a quality-of-life add. Phase 5 generalizes.

---

## 10. Open questions for review

These don't block the spec from landing. They're decisions to make during Phase 1 implementation, in the PRs themselves.

### 10.1 Sidecar format: single JSON per bot, or JSONL append-only?

**Current spec:** single JSON per bot with `intents[]` array + `audit_history[]` per intent.

**Alternative:** JSONL where each line is an event (`set`, `updated`, `revoked`). The current intent state is derived by replaying.

**Trade-off:** JSON is simpler to read + atomic-rename-safe. JSONL is append-only, more resilient to partial writes, but every read replays the log. With ~10-50 intents per bot, JSON wins on simplicity. Recommendation: stick with JSON for v1; switch to JSONL only if we see contention.

### 10.2 Plugin → field dep map: hand-curated, or derived?

**Current spec:** hand-curated YAML at `packages/analyzer/permissions/plugin_field_deps.yaml`.

**Alternative:** parsed from each plugin's manifest at install time, cached.

**Status:** OC plugins don't currently expose this in manifest metadata. Switching to discovery requires upstream OC work or a per-plugin annotation file in Evolve. Recommendation: hand-curated for v1; revisit if [project_openclaw_admin_coverage_roadmap](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_openclaw_admin_coverage_roadmap.md) lands a manifest-extension upstream.

### 10.3 Where the LLM inference call lives

**Current spec:** synchronous inside `set_intent()`, called from the admin server (Save handler) and from the applier.

**Alternative:** a small helper daemon that drains a queue, so `set_intent()` returns immediately.

**Trade-off:** synchronous adds ~500-1000ms to Save latency but avoids reconciliation complexity. Daemon adds infra surface area for a feature whose latency target ("invisible to operator") is already met by synchronous. Recommendation: synchronous for v1; revisit only if measured latency causes UX complaints.

### 10.4 Reconciliation policy when sidecar and inline disagree

**Current spec:** sidecar wins; inline rebuilt silently from sidecar on next reconciliation pass.

**Alternative:** flag a `desync_warning` Signal in the signal store for operator visibility.

**Trade-off:** silent self-heal is operator-friendly but hides write-path bugs. Flagging is noisier but catches `set_intent()` bypass attempts (e.g., someone hand-edits `network.json::bots.<id>.config_intents`). Recommendation: silent for v1, with a counter logged to `_generator_memory.jsonl` so future ops can tell whether it's a real problem.

### 10.5 Module placement: `evolve_admin.config.intent` vs `analyzer.permissions.intent`

**Current spec:** `evolve_admin.config.intent` because the helpers are admin-side (UI + applier integration).

**Alternative:** `analyzer.permissions.intent` because the *generators* are the most frequent reader.

**Trade-off:** admin-side aligns with where writes happen; analyzer-side aligns with where reads happen. The cross-package dependency exists either way. Recommendation: admin-side, with a thin re-export in `analyzer.permissions` so generators can `from analyzer.permissions.intent import get_intent` and not need to know the truth lives in `evolve_admin`.

### 10.6 `set_by` for the case where an operator changes a value in the admin UI back to a *deliberately non-baseline* value after a migration set it differently

E.g., migration set `exec=deny`, operator now sets `exec=full` because they just installed codex. Should the migration intent be archived first, then a new `pod_admin (admin UI)` intent recorded?

**Current spec:** yes — `set_intent()` finds the existing `(bot, field)` intent and updates it in place. Migration record's `audit_history` preserves the prior set_by. The current record always reflects the current truth.

**Alternative:** keep both intents, mark the migration one as superseded.

**Trade-off:** in-place update is simpler. Multiple intents per (bot, field) opens layering questions ("which one wins?") that the schema currently dodges. Recommendation: in-place update for v1.

### 10.7 `set_by="inferred:auto"` placeholder during Phase 2

Phase 2 lands before Phase 3. What `set_by` does the applier use for proposals it applies?

**Current spec:** `applier:<proposal_id>` always — Phase 2's applier hook is explicit, never delegates to inference. Phase 3 introduces `inferred:*` only for the admin-UI Save path's non-proposal direct writes.

**Trade-off:** this means admin-UI Saves during Phase 2 record `set_by="pod_admin (admin UI)"` with `reason="(unspecified — Phase 3 inference not yet live)"`. Generators still see *an intent exists*, so the noise problem is solved. Inference upgrades the reason later. Recommendation: ship Phase 2 with the placeholder reason; document the upgrade path.

---

## Appendix A — End-to-end example

The team-bot-a / codex / `tools.exec.security` story, end to end, after the full system ships:

1. **2026-05-14T09:58:11Z** — Operator installs `codex` plugin on team-bot-a via admin UI. Plugin enable code in `evolve_admin/deploy.py` writes `tools.exec.security=full` to `team-bot-a/.openclaw/openclaw.json` via `permissions/writer.py::write_openclaw_fields`, then calls `set_intent(bot_id="team-bot-a", field_path="tools.exec.security", value="full", set_by="plugin_side_effect:codex", reason="codex plugin requires exec — installed via admin UI", depends_on={"plugin": "codex"})`.
2. Sidecar `{shared_dir}/config_intents/team-bot-a.json` now has the intent. `network.json::bots.team-bot-a.config_intents` has the inline annotation.
3. **2026-05-14T12:00:00Z** — `permission_monitor` sweeps. It detects `tools.exec.security=full` differs from baseline (`deny`). Emits `perm_config_drift` signal as before.
4. `auth_drift_filler` consumes the signal. Calls `get_intent("team-bot-a", "tools.exec.security")` → returns the intent. Calls `intent_still_valid(intent)` → checks plugin list, finds `codex`, returns True.
5. Generator checks `_generator_memory.jsonl` for signature `team-bot-a:tools.exec.security:audit_only:intent-9a4f2c`. Not present. Emits one audit-only signal to Signal store. Appends emission record. Does **not** emit a Proposal.
6. **2026-05-15 through 2026-08-12** — Every sweep, same flow. Step 5's dedup check finds the prior emission. **No further audit signals, no proposals.** Quiet.
7. **2026-08-12** — Operator removes codex plugin. Plugin disable code does *not* automatically clear `exec=full` (the value is now genuinely a deliberate-or-orphaned config; deciding which is the operator's call).
8. **2026-08-12T12:00:00Z** — Next sweep. `auth_drift_filler` calls `intent_still_valid(intent)` → plugin no longer enabled → returns False. Generator emits a one-time `stale_flagged` proposal: *"You set team-bot-a::tools.exec.security=full to enable codex; codex is no longer installed. Restore the baseline?"*
9. Operator clicks "Apply" on the proposal. `UpdatePermissionConfigApplier` writes `exec=deny`. Calls `set_intent` post-write; finds post-write value matches baseline; auto-archives the intent (§8.1). Inline annotation removed.
10. **2026-08-13** — sweep finds no drift, no intent, no signal. Steady state restored.

Total noise: one signal emission in May, one stale_flagged proposal in August, zero in between. Compared to today: ~90 redundant proposals over the same window.

---

## Appendix B — Cross-references

- [feedback_generators_consider_intent](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_generators_consider_intent.md) — the design memory (lock-ins this spec materializes)
- [project_l1_l2_applier_architecture](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_l1_l2_applier_architecture.md) — the L2 applier seam used in §3
- [feedback_rsi_design_approach](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_rsi_design_approach.md) — narrow high-signal principle
- [feedback_rsi_low_cost_preference](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_rsi_low_cost_preference.md) — cost ceiling
- [feedback_two_pass_review_workflow](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_two_pass_review_workflow.md) — silent-failure test discipline applied in each phase
- [feedback_profile_passive_growth](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_profile_passive_growth.md) — precedent for "passive harvest, no per-fact approval"
- [packages/analyzer/generators/auth_drift_filler/signal_proposals.py](../packages/analyzer/generators/auth_drift_filler/signal_proposals.py) — Phase 2 integration target
- [packages/analyzer/arbiter/appliers/permissions.py:119](../packages/analyzer/arbiter/appliers/permissions.py) — `UpdatePermissionConfigApplier.apply`, the write seam for Phase 1
- [packages/analyzer/permissions/writer.py:83](../packages/analyzer/permissions/writer.py) — `write_openclaw_fields`, the canonical write helper
- [packages/admin/evolve_admin/web/server.py:19102](../packages/admin/evolve_admin/web/server.py) — `_operator_create_apply`, the admin-UI Save seam
- [packages/admin/evolve_admin/setup_wizard.py:_render_evolve_sudoers](../packages/admin/evolve_admin/setup_wizard.py) — sudoers context (no new grants needed for v1)
- [docs/spec-rsi-architecture-2026-04-17.md](spec-rsi-architecture-2026-04-17.md) — broader RSI design this fits inside
