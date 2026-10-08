# launchd scope policy — system-scope by default

**TL;DR**: New Evolve infra services default to **system-scope LaunchDaemons** under `/Library/LaunchDaemons/`. Adding a per-user LaunchAgent (kind="agent" in `CORE_INFRA_DAEMON_KINDS`) requires a written justification in this document and a matching entry in `JUSTIFIED_AGENTS`.

## Why this policy exists

Evolve pods run **headless**. The Mac mini reference deployment has:

- An admin user (e.g. `pod-admin`) that only ever logs in via SSH → produces a `Background` launchd session, **no Aqua (graphical) session**, no `gui/<uid>` domain.
- A service user (`evolve`) that runs under launchd → `Background` session.
- A console user (one of the bot accounts) that is the only user with an active Aqua session (and rarely matches the user whose home would host an admin LaunchAgent).

macOS LaunchAgents in the `gui/<uid>` domain can only be bootstrapped when that user has an active Aqua session. Attempting `launchctl bootstrap gui/<uid> /path/to.plist` against a Background-only user returns **error 125 "Domain does not support specified action"**. The plist sits on disk forever; the infra audit emits `daemon_not_loaded` every run; the operator dismisses; the loop repeats.

That happened to `com.evolve.mcp-bridge` for ~6 weeks before being caught. See [evolve#1821](https://github.com/evolve-ops/evolve/pull/1821) and the daemon's pre-conversion comment at `packages/admin/evolve_admin/applications/infra_audit.py:79`.

## The rule

**Default: system-scope LaunchDaemon.** All new infra services in `CORE_INFRA_DAEMON_KINDS` must be `kind="system"` unless this document grants an explicit exception.

A new entry to `JUSTIFIED_AGENTS` (in `packages/admin/evolve_admin/applications/_scope_policy.py`) requires a corresponding section in this document covering:

1. **Why a LaunchDaemon won't work.** Specific technical reason — not "feels per-user." Examples that would qualify:
   - Service must access the user's keychain (system daemons can't unlock user keychains).
   - Service must render UI in the user's Aqua session (e.g. a status menu item).
   - Service must consume a per-user file format that's only readable while that user is logged in.
2. **Which user account holds the Aqua session.** Concretely name it. If it's an admin account that runs only via SSH, the answer is "no one" — the policy reject applies.
3. **Fallback behavior on headless pods.** When `gui/<uid>` doesn't exist, what should the service do? Skip silently? Surface a `not_supported_on_headless` Signal? The answer must be in the JUSTIFIED_AGENTS entry's `headless_fallback` field.

If you can't answer all three, the answer is "convert to LaunchDaemon."

## Whitelist

> *(When a future agent is justified, add a `### <label>` heading here mirroring the JUSTIFIED_AGENTS entry: why a LaunchDaemon won't work, which user holds Aqua, headless fallback. Both the doc and the code constant must change in the same PR — `test_launchd_scope_policy.py` enforces parity.)*

### com.evolve.mcp-bridge

**Transitional grandfathering — remove with [evolve#1821].** The mcp-bridge service is being converted from a per-user LaunchAgent (`com.evolve.mcp-bridge` under `~/Library/LaunchAgents/`) to a system-scope LaunchDaemon (`ai.evolve.evolve.mcp-bridge` under `/Library/LaunchDaemons/`) in [evolve#1821](https://github.com/evolve-ops/evolve/pull/1821). Until that PR merges and reaches main, the kind="agent" entry in `CORE_INFRA_DAEMON_KINDS` would trip `test_every_agent_has_a_justification` because it has no justification. This grandfathered entry exists solely so this guardrail PR can land before #1821 without shipping a deliberately-failing test.

- **Why a LaunchDaemon won't work**: there is no legitimate technical reason. This is exactly the case the policy rejects.
- **Aqua session user**: no Aqua session exists. That's the bug.
- **Headless fallback**: `convert_to_daemon` — the actual fix shipping in #1821.
- **Introduced by**: pending, [evolve#1821]; this entry must be removed by that PR's diff before merge (or by a follow-up sweeper if landing order goes the other way).

A follow-up commit (in #1821 or a sweep PR) removes both this section and the `_scope_policy.JUSTIFIED_AGENTS["com.evolve.mcp-bridge"]` entry once the kind="agent" entry is gone from `CORE_INFRA_DAEMON_KINDS`. After that, this Whitelist should be empty.

## Cross-references

- **PR 1** — mcp-bridge LaunchAgent → LaunchDaemon conversion: [evolve#1821](https://github.com/evolve-ops/evolve/pull/1821)
- **PR 2** — feasibility-checked audit + lineage-aware proposals: [evolve#1826](https://github.com/evolve-ops/evolve/pull/1826)
- **Related feedback patterns**:
  - `feedback_distinguish_tooling_failure_from_findings.md` — same class (audit emits a finding that's actually a tooling/feasibility issue rather than a real defect)
  - `feedback_diagnosis_must_survive_live_inspection.md` — verify the suggested fix against live system state before emitting
  - `feedback_bot_id_not_account_name.md` — sibling class (path derivation that assumes per-context identity)

## How the guardrails work

| File | Role |
|------|------|
| `docs/policy/launchd-scope.md` (this file) | Human-readable policy, why+how |
| `packages/admin/evolve_admin/applications/_scope_policy.py` — `JUSTIFIED_AGENTS` | Machine-readable whitelist; structured justification fields |
| `packages/admin/tests/test_launchd_scope_policy.py` | Invariant tests that fail CI if either side drifts |
| `packages/admin/evolve_admin/applications/infra_audit.py` — load-time check | Warns at import if any kind="agent" entry isn't justified (defense-in-depth for runtime) |

A new agent therefore requires **four** changes in the same PR: doc section, JUSTIFIED_AGENTS entry, the agent's own plist/install code, and the infra_audit kind dict. Forgetting any of them causes the invariant test to fail.
