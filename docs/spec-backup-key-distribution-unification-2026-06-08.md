# Backup SSH-key distribution — model unification

**Status:** Implementation-ready refinement of the 2026-06-07 spec.

**Date:** 2026-06-08.

**Relationship to prior spec:** Supersedes [docs/spec-backup-key-distribution-unification-2026-06-07.md](./spec-backup-key-distribution-unification-2026-06-07.md) on three implementation specifics, agrees on the structural direction (subtract Writer A; shared is canonical):

1. **Override-preservation removed.** The 06-07 spec preserved a per-bot override path (drop a different priv/pub pair in by hand → distribute leaves it alone). This spec rejects that capability — single canonical writer, four-clause invariant, no exceptions. Rationale: the override path keeps two mental models alive in the codebase forever, and the operator's actual preference recorded 2026-06-07 was "leave existing per-bot deploy keys on GitHub in place" (cleanup, not perpetuation).
2. **Sentinel field changed.** 06-07 used `network.json::backup.sharedKeyRegistered` as an operator-confirmation gate before migration. This spec uses `backup.key_mode = "shared"` as a post-migration completion sentinel — the operator action (Distribute Key click) IS the gate, the sentinel records completion rather than intent.
3. **Per-repo deploy-key registration via API, not operator copy-paste.** 06-07 assumed the operator would manually paste the shared pubkey on their GitHub user account. This spec registers the pubkey per-repo via the GitHub API automatically (subsuming PR #2357's in-flight automation). Operator-account-level SSH key registration was a misleading dead end — per-repo is the standard backup pattern.

**Origin:** The 2026-06-07 incident showed that two independent writers can both land bytes at `/Users/<bot>/.ssh/evolve-backup-<bot>{,.pub}` with no shared invariant — the per-bot writer in `deploy.py` and the shared writer in `server.py`. An operator who ran Distribute Key after configuring a per-bot pod ended up with a private key matching the shared model but pubkeys (on disk and on GitHub) belonging to the per-bot model, and backups failed silently for two days.

**Adjacent:**

- [docs/spec-backup-and-data-classification-2026-05-28.md](./spec-backup-and-data-classification-2026-05-28.md) — the broader backup architecture. This spec refines its assumed key-distribution layer; it does not contradict any of that spec's resolved decisions.
- The GitHub-credentials-three-purposes model (private memory note) — backup is the "per-bot self-backup" purpose; this spec narrows how the credential is shaped.
- [docs/PLACEHOLDER_NAMING.md](./PLACEHOLDER_NAMING.md) — naming conventions used throughout this document.
- PR #2353 (merged) — diagnostic classifier in [packages/analyzer/backup_diagnostics.py](../packages/analyzer/backup_diagnostics.py). The new cause IDs (`ssh_key_mismatch`, `deploy_key_unrecognized`) are the symptoms the unified model must make unreachable.
- PR #2357 (open, will land before this spec's Phase 2) — auto-registers the shared pubkey on each bot's backup repo via the GitHub API and keeps the `{shared_dir}/pubkeys/<bot>.pub` mirror in sync. This spec assumes that PR has landed and extends its direction.
- [packages/admin/evolve_admin/deploy.py:6230](../packages/admin/evolve_admin/deploy.py) — current per-bot writer entry point (`_ensure_backup_ssh_key`, helper `_distribute_backup_ssh_key`).
- [packages/admin/evolve_admin/web/server.py:9060](../packages/admin/evolve_admin/web/server.py) — current shared writer (`api_security_backup_distribute_key`).
- [packages/admin/evolve_admin/web/server.py:16983](../packages/admin/evolve_admin/web/server.py) — `_ensure_deploy_key` helper, currently scoped inside `_register_admin_routes`.

---

## Problem

Two writers, one filesystem target, no shared invariant.

**Writer A — per-bot model** (`deploy.py::_ensure_backup_ssh_key` + `_distribute_backup_ssh_key`).
Generates a unique Ed25519 keypair per bot at `/Users/evolve/.ssh/evolve-backup-<bot>{,.pub}`, then stages it into the bot's `~/.ssh/evolve-backup-<bot>{,.pub}`. Runs on every `sudo evolve-admin deploy <bot>` and during fresh setup. The pubkey is registered as a per-repo deploy key on GitHub manually by the operator (or, on this branch, via the onboarding wizard's `_ensure_deploy_key` path).

**Writer B — shared model** (`server.py::api_security_backup_distribute_key`).
Generates ONE Ed25519 keypair at `/Users/evolve/.ssh/evolve-backup-shared{,.pub}`, then copies that same pair into every backup-configured bot's `~/.ssh/evolve-backup-<bot>{,.pub}`. Also mirrors the shared pubkey to `{shared_dir}/pubkeys/<bot>.pub` so the admin UI can read it without sudo. With PR #2357 it ALSO registers the shared pubkey as a per-repo deploy key on every bot's backup repo via the GitHub API.

**The two writers share a destination.** Both write to `/Users/<bot>/.ssh/evolve-backup-<bot>{,.pub}`. The last writer wins, and there is no enforcement that the bytes on the bot's disk agree with the bytes registered on GitHub.

**Reachable bad states** (all observed on the reference pod 2026-06-07):

1. **Mixed pod** — some bots distributed with the shared key, others still on their per-bot key. The pod has no manifest of which bot is in which mode, so reconciling requires hand-comparing files.
2. **Pair desync** — the private key on a bot's disk is the shared key, but the `.pub` next to it is a per-bot key (or vice versa). SSH refuses to load the private key because `identity_sign` rejects pair mismatch. Diagnostic: `backup_diagnostics::ssh_key_mismatch`.
3. **GitHub desync** — the shared pubkey is on the bot's disk but NOT registered as a deploy key on the bot's backup repo (because Distribute Key was run before PR #2357 added the GitHub-registration step, or because registration silently failed). Diagnostic: `backup_diagnostics::deploy_key_unrecognized`.
4. **Re-deploy clobber** — operator runs Distribute Key (shared mode), then `sudo evolve-admin deploy <bot>` regenerates a per-bot key, distributes it, and silently undoes the shared model for that one bot. Diagnostic eventually surfaces, but only after 3 consecutive backup failures fire `backup_failing` — by which time it has been 2–3 days.

The 2026-06-07 incident hit (3) then (2) in sequence — Distribute Key fixed the on-disk pair, but the new shared pubkey wasn't on GitHub. After PR #2357 fixed (3), the pod still has (1) and (4) reachable; this spec closes both.

---

## Principle

**One canonical model: shared private key, per-repo deploy-key registration, one reconciler.**

- One Ed25519 keypair lives at `/Users/evolve/.ssh/evolve-backup-shared{,.pub}` — the **canonical source**.
- Every backup-configured bot's `~/.ssh/evolve-backup-<bot>{,.pub}` is a byte-identical copy of the canonical source.
- The canonical pubkey is registered as a deploy key on **every** backup repo. Per-repo registrations remain GitHub's authorization surface — they can be surgically revoked.
- A single reconciler (`backup_keys.reconcile_pod`) is the only path that writes to those filenames. Both `deploy_bot` and the Distribute Key endpoint call into it.
- The reconciler is **idempotent and self-healing**: it can be run any time and will converge a partially-aligned pod toward the invariant without operator intervention.

This model is chosen over per-bot because:

| Axis | Shared | Per-bot |
|---|---|---|
| Operator UX | One ssh-keygen, one distribute, N per-repo registrations (automated) | N per-bot ssh-keygens, N distributes, N per-repo registrations |
| Rotation | One file to rotate, atomic for all bots | N rotations, N distributes, N re-registrations |
| Diagnostic surface | Match against ONE source-of-truth file | Match against N source-of-truth files |
| Revocation surgery | Delete deploy key on one repo → that bot stops backing up | Same |
| Compromise blast radius (mini stolen) | Every backup repo compromised | Every backup repo compromised (attacker has all bot homes) |
| Compromise blast radius (one bot user only) | Every backup repo compromised | Only that bot's repo compromised |

The last row is the only meaningful security difference. Realistic threat model: the mini is one host; compromise of one bot user almost always implies broader compromise (shared launchd, shared sudoers grants, shared `/Users/evolve/` reads). The per-bot isolation benefit is small enough that the operator-UX and incident-debugging wins dominate. The "incident debugging" weight is high — yesterday's incident was a five-hour debug because diagnosis required inspecting six different pairs of files against six different remote registrations.

The shared-key intent is already documented in [packages/admin/evolve_admin/web/server.py:9087](../packages/admin/evolve_admin/web/server.py) ("the operator adds the returned pubkey ONCE … the same key authenticates as that user against every repo"). This spec finishes the journey that comment started, and updates the comment to reflect the per-repo-registration mechanism (not user-account registration) that PR #2357 introduced.

### Org-account vs personal-account split

Independent of key model. The deploy-key registration step calls `/repos/<owner>/<name>/keys` regardless of whether `<owner>` is a user or an org. The PAT used for the call must have admin access to the repo (already the constraint for `_ensure_deploy_key` today). No change to org/personal handling — the unification is orthogonal to that axis. The spec assumes the existing org/personal detection in `_onboard_one_github_bot` (POST `/orgs/<login>/repos` vs `/user/repos`) is correct and reuses it.

---

## Invariant

For every bot in `network.json::bots` with a non-empty `backupRepoUrl`, **all four** of the following must hold:

1. **Bot private matches source.** `read_bytes(/Users/<bot_user>/.ssh/evolve-backup-<bot>) == read_bytes(/Users/evolve/.ssh/evolve-backup-shared)`.
2. **Bot pub matches source.** `read_bytes(/Users/<bot_user>/.ssh/evolve-backup-<bot>.pub) == read_bytes(/Users/evolve/.ssh/evolve-backup-shared.pub)`.
3. **Mirror matches source.** `read_text({shared_dir}/pubkeys/<bot>.pub).strip() == read_text(/Users/evolve/.ssh/evolve-backup-shared.pub).strip()`.
4. **GitHub authorizes source.** Parsing `backupRepoUrl` to `<owner>/<repo>`, the canonical pubkey blob appears in the result of `GET /repos/<owner>/<repo>/keys`.

The reconciler verifies all four, classifies each bot as `aligned` or `drifted`, and self-heals drifts in clauses 1–3 by copying from the canonical source. Clause 4 is healed by calling `_ensure_deploy_key` (moved to module scope; see §"Code-level consequences"). If the canonical source itself is missing, the reconciler refuses to make any change and emits a `pod_backup_key_missing` Signal — generating a new shared credential is an explicit operator action, never auto-recovery.

**Mode field for forward safety.** `network.json::backup.key_mode` records `"shared"` once the post-migration reconciler succeeds on every backup-configured bot. Absence means "pre-migration legacy state, mode unknown" — the reconciler treats this as `drifted` and offers the migration flow. Presence means "shared is the asserted mode; any drift is a bug to repair, not a mode question." This sentinel is the difference between "the pod has not yet been migrated" and "the pod was migrated and has drifted since."

**What the invariant explicitly forbids:**

- Different bytes in `/Users/<bot>/.ssh/evolve-backup-<bot>` for any two bots in the same pod.
- A `.pub` file on the bot's disk that doesn't byte-match its sibling private (closes the 2026-06-07 incident's first failure shape).
- A canonical pubkey absent from a configured backup repo's deploy keys (closes the 2026-06-07 incident's second failure shape).
- The legacy per-bot staging files `/Users/evolve/.ssh/evolve-backup-<bot>{,.pub}` existing in steady state.

---

## Migration

Three distinct pod shapes today (across the reference deployment plus pre-launch installs):

- **A. All shared.** Every backup-configured bot has the shared private+pub on disk and the shared pubkey registered on its repo. `network.json::backup.key_mode` may or may not be set.
- **B. All per-bot.** Every backup-configured bot has a unique private+pub on disk and a unique pubkey registered on its repo. `/Users/evolve/.ssh/evolve-backup-shared` does not exist.
- **C. Mixed.** Some bots in shape A, some in shape B. Today's reference pod is here.

### Discovery

The reconciler runs first in **read-only discovery mode** when no `key_mode` sentinel is set. For each backup-configured bot:

```
status = "aligned"   if all four invariant clauses hold
       = "shared_disk_only" if 1–3 hold but 4 fails (pubkey not registered)
       = "legacy_per_bot"   if 1–3 use a unique-per-bot key and 4 holds for that unique key
       = "drifted"  in any other case
```

The Backup → Cloud subtab renders this matrix above the Distribute Key button. If all rows are `aligned`, the UI shows "Shared mode active, all bots aligned" and offers a "Reverify" button (re-runs discovery). Otherwise it offers a "Migrate to shared mode" button.

### Migrate-to-shared flow

Triggered by the operator clicking "Migrate to shared mode." Single button, confirmation modal explains the steps.

1. **Ensure canonical source exists.** If `/Users/evolve/.ssh/evolve-backup-shared` is missing, generate it. Modal explicitly names this as "creating a new pod-wide backup key" — the operator confirms.
2. **Register canonical pubkey on every backup repo.** For each bot with a `backupRepoUrl`, call `ensure_deploy_key_registered(token, login, repo, shared_pubkey, bot_id)`. **Existing per-bot deploy keys on those repos are left in place** — they remain valid and continue to authorize the unique-per-bot keys still on disk. This is the operator preference recorded yesterday ("Leave them in place"): the rationale is that revocation can be done later, deliberately, with the operator's eyes open. A failed migration must not strand a working credential.
3. **Distribute shared pair to every bot.** Copy the canonical private+pub into every backup-configured bot's `~/.ssh/evolve-backup-<bot>{,.pub}`. Same `/tmp` staging + `sudo /bin/cp` path the current shared writer uses. Update `{shared_dir}/pubkeys/<bot>.pub` to the shared pubkey.
4. **Re-verify.** Run discovery again. Every backup-configured bot must now be `aligned`. If any are not, the migration leaves the pod in a "partial" state (see §"Failure modes") — the sentinel is NOT written, the UI shows which bots remain unmigrated, and the operator can retry.
5. **Write sentinel.** Only if step 4 reports every bot `aligned`: write `network.json::backup.key_mode = "shared"`.
6. **Surface cleanup affordance.** A separate, optional banner offers "Remove redundant per-bot deploy keys" — lists each bot's old per-bot deploy key by GitHub title (typically `evolve-<bot>`), shows the date of registration, and lets the operator delete them one-by-one. No bulk button on the first release; this is a one-time cleanup and bulk-delete on GitHub feels too sharp.

### Rollback

Migration is mostly add-only on GitHub (we register a new deploy key; we don't remove the old one). On disk, the bot's `~/.ssh/evolve-backup-<bot>` private+pub gets overwritten with the shared bytes. Restoring per-bot is possible because `/Users/evolve/.ssh/evolve-backup-<bot>` still exists (steady-state cleanup is deferred to a separate, later sweep — see §"Code-level consequences"). For the duration of the first release after migration, "rollback to per-bot" is `cp /Users/evolve/.ssh/evolve-backup-<bot>{,.pub} /Users/<bot>/.ssh/`. After that sweep removes the staging copies, rollback would require regenerating per-bot keys from scratch — by that point the model is committed.

---

## Code-level consequences

Drift in the per-bot writer is the structural root of the incident. The unification deletes that writer.

| Component | Action | Notes |
|---|---|---|
| `deploy.py::_ensure_backup_ssh_key` | **DELETE** | Replaced by a call into the unified writer. |
| `deploy.py::_distribute_backup_ssh_key` | **DELETE** | Same. The `/tmp` staging logic moves into the new module. |
| `deploy.py::deploy_bot` SSH-key step (around line 6230) | **REPLACE** with `backup_keys.ensure_bot_in_sync(bot_id, bot_user)` | The new helper enforces invariant clauses 1–3 for one bot. If the canonical source is missing, it logs a warning and returns without touching the bot's disk; deploy continues (backup is opt-in). Clause 4 — deploy-key registration — is NOT enforced here, because deploy may run without GitHub PAT context; the periodic reconciler catches it. |
| `server.py::api_security_backup_distribute_key` | **REFACTOR** to a thin wrapper | Becomes `reconcile_pod(force_distribute=True)` plus the response shaping. The body of the endpoint is ~5 lines once the work moves to `backup_keys.py`. The legacy URL alias `/api/security/backup-distribute-key` is preserved. |
| `server.py::_ensure_deploy_key` (line 16983, scoped inside `_register_admin_routes`) | **MOVE** to `backup_keys.ensure_deploy_key_registered` at module scope | Drag the `_github_api` helper with it (or duplicate-free: move both to a shared `github_api.py` if `_github_api` is otherwise nested). Both the onboarding flow and the reconciler call the new module-scope helper. |
| `server.py::_bot_pubkey` (line 16963) | **RENAME and SIMPLIFY** to `backup_keys.read_canonical_pubkey()` (no `bot_id` arg) | Reads `/Users/evolve/.ssh/evolve-backup-shared.pub` directly. The onboarding wizard's call sites pass the same value to every bot. |
| `analyzer/backup.py::generate_ssh_deploy_key` | **DELETE** | No more per-bot generation. The two consumers (`/api/backup/cloud/keys/generate` and `_bot_pubkey`) migrate to the shared source. |
| `analyzer/backup.py::ssh_key_path` | **KEEP** | The bot-side daemon still reads `~/.ssh/evolve-backup-<bot>` — the bot daemon is agnostic to which model produced that file. Comment updated to point at this spec. |
| `/api/backup/cloud/keys/generate` endpoint (server.py:9037) | **REPURPOSE** to `/api/backup/cloud/keys/ensure-shared` | The body `{botId}` is ignored; the endpoint ensures `/Users/evolve/.ssh/evolve-backup-shared` exists and returns its pubkey. Legacy callers that pass a botId get the same shared pubkey back — fine, since that's now the universal answer. Or DELETE entirely if no live caller remains. (Confirm during implementation.) |
| `{shared_dir}/pubkeys/<bot>.pub` mirror | **KEEP for one release**, then collapse to `{shared_dir}/pubkeys/shared.pub` | The N-copy mirror is dead weight once the model is shared, but keeping it preserves the existing `/api/security/backup-config` response shape on the first release. A follow-on PR collapses to a single file plus a compat shim in the config endpoint. |
| `/Users/evolve/.ssh/evolve-backup-<bot>{,.pub}` legacy staging files | **REMOVE during migration**, then never recreated | Migration step 3 leaves them in place; a separate "Sweep legacy staging files" affordance in the Cloud subtab (offered after `key_mode = "shared"` is written) deletes them after operator confirmation. This is the "after that sweep removes the staging copies, rollback would require regenerating per-bot keys from scratch" line in §"Rollback". |
| `network.json::backup.key_mode` | **NEW** field, `"shared"` or absent | Written only after migration step 4 passes. Treated by the reconciler as authoritative. |
| `network.json::bots.<id>.backupRepoUrl` | **KEEP unchanged** | Per-bot remote URL is unrelated to key model. |

### New module: `packages/admin/evolve_admin/backup_keys.py`

Contains the shared writer plus the reconciler. Roughly:

```
SHARED_SOURCE_PRIV = Path("/Users/evolve/.ssh/evolve-backup-shared")
SHARED_SOURCE_PUB  = SHARED_SOURCE_PRIV.with_suffix(".pub")

def ensure_shared_source_generated() -> Result            # idempotent ssh-keygen
def read_canonical_pubkey() -> str | None
def ensure_bot_in_sync(bot_id, bot_user) -> BotSyncResult  # invariant clauses 1–3
def ensure_deploy_key_registered(token, login, repo, pubkey, bot_id) -> RegResult  # clause 4
def discover_pod(network) -> dict[bot_id, "aligned"|"shared_disk_only"|"legacy_per_bot"|"drifted"]
def reconcile_pod(network, *, force_distribute: bool, github_token: str | None) -> ReconcileReport
```

`deploy_bot` calls `ensure_bot_in_sync`. The Distribute Key endpoint calls `reconcile_pod(force_distribute=True)`. A new daily monitor calls `reconcile_pod(force_distribute=False, github_token=pat)` and emits a `backup_key_drift` Signal if any bot is not aligned.

### Tests

The invariant must be machine-checked, not merely asserted by code review.

- **Unit:** `ensure_bot_in_sync` against a tmp-path filesystem: drift each of clauses 1–3 individually, assert the reconciler detects and fixes. Drift two at once, assert both are fixed in one pass.
- **Unit:** `discover_pod` over a synthetic pod with one bot in each of `aligned` / `shared_disk_only` / `legacy_per_bot` / `drifted`. Assert classification matches.
- **Unit:** `reconcile_pod(force_distribute=True)` on a "legacy_per_bot" pod (no shared source yet) refuses to overwrite without `ensure_shared_source_generated` being called first by an explicit operator step. (Confirms "auto-recovery of canonical source is forbidden.")
- **Anti-regression:** import scan + grep that asserts `deploy.py` no longer contains the strings `evolve-backup-<bot>` (sans the docstring), `ssh-keygen`, or `mkstemp(... evolve-backup ...)`. Prevents a future commit from quietly reintroducing the per-bot writer.
- **Anti-regression:** import scan that asserts only `backup_keys.py` writes to `/Users/evolve/.ssh/evolve-backup-shared{,.pub}` or `/Users/<bot>/.ssh/evolve-backup-<bot>{,.pub}` shaped paths. Implemented as a grep over the `packages/` tree for `evolve-backup-` strings outside `backup_keys.py` and `analyzer/backup.py` (consumer-side, read-only).

The brief's "tests should pin the new invariant and refuse to compile a state where both models could write the same file" is enforced by the grep test — Python has no compile-time exclusion, but the grep catches the only known shape of regression.

---

## Failure modes after the change

Each row describes a foreseeable failure and the defined recovery. None of these is "retry and pray."

| Failure | Detection | Recovery |
|---|---|---|
| **Partial migration** — step 3 (distribute) succeeds for some bots, fails for others. | The reconciler runs discovery as part of step 4. Any bot not `aligned` blocks the `key_mode = "shared"` write. UI shows the per-bot status table with failed bots highlighted. | Operator clicks "Migrate to shared mode" again. The reconciler is idempotent — succeeded bots re-verify in milliseconds; failed bots are retried. No on-disk state is undone. |
| **Partial Distribute Key** — operator re-runs Distribute Key (e.g., to rotate the key), GitHub registration succeeds for some repos, fails for others. | `reconcile_pod` returns a per-bot result with `clause_4: "registration_failed"` for affected bots. UI surfaces. | Backup pushes continue using the OLD key (which is still registered) on affected bots — no immediate failure. Operator retries Distribute Key after addressing the registration error. The new shared private is NOT swapped into `/Users/evolve/.ssh/evolve-backup-shared` until every repo accepts the new pubkey (see "Rotation" below). |
| **GitHub API down at migration time** | Step 2 fails with HTTP timeouts / 5xx. Reconciler stops before any on-disk write. | Operator retries when GitHub is back. No state changes. The `migration in progress` banner persists. |
| **PAT revoked / lacks `admin:repo_hook` scope** | `ensure_deploy_key_registered` returns 401/403. | Reconciler emits a `pod_backup_pat_invalid` Signal, deep-links to the credentials page. No on-disk write. Distinct Signal type from the GitHub-down case so the operator gets the right fix steps. |
| **Operator's PAT only has access to some backup repos** | Per-repo `ensure_deploy_key_registered` returns 404 (repo not found from this PAT's perspective). | Reconciler marks those repos as `clause_4: "pat_missing_access"`. Operator either adds the PAT scope/owner-membership or removes those bots from backup. UI surfaces the affected bots distinctly from the "GitHub down" case. |
| **Per-bot deploy keys remain on GitHub after migration** (intentional, per operator preference) | Discovery reports `aligned` for those bots — the per-bot keys on the repo are extra, not missing. They don't violate the invariant. | The optional "Remove redundant per-bot deploy keys" affordance addresses this when the operator chooses. Not gated; never auto-removed. |
| **Operator runs `sudo evolve-admin deploy <bot>` AFTER migration** | The new `deploy_bot` SSH-key step calls `ensure_bot_in_sync`, which re-asserts clauses 1–3. If `/Users/evolve/.ssh/evolve-backup-shared` exists, the bot's disk converges to the canonical source. | None needed — the failure mode is now closed. The old per-bot writer is gone; deploy cannot recreate a per-bot key. |
| **Shared source key deleted (operator error, disk corruption)** | Discovery emits `pod_backup_key_missing` Signal. No bot writes happen. | Operator clicks "Generate new shared key" in the UI — explicit, never automatic. Migration flow restarts. |
| **Mixed-pod state observed for the first time on a new pod** (no `key_mode` sentinel, some per-bot keys, no shared source) | Discovery runs read-only on every admin-UI page load (cheap) and shows the "Migrate to shared mode" affordance. | Operator clicks the button. The flow described in §"Migration" runs. |
| **Manifest desync — bot in `network.json` but `/etc/passwd` has no matching user** | `ensure_bot_in_sync` returns `missing_bot_user`. | Reconciler skips, UI flags. Operator investigates separately. |

### Rotation flow

Not strictly a failure mode, but enabled by the unification. To rotate the shared key (e.g., quarterly hygiene or after a suspected compromise):

1. Generate the new key at `/Users/evolve/.ssh/evolve-backup-shared-NEW`.
2. For every backup repo, register the new pubkey alongside the existing one. Both keys authorize for a transition window.
3. Atomically rename `evolve-backup-shared-NEW` over `evolve-backup-shared` (the source-of-truth swap is one syscall; an in-flight backup push that already loaded the old key continues; the next push loads the new one).
4. Run `reconcile_pod(force_distribute=True)` to copy the new bytes into every bot.
5. After a configurable grace period (default 7 days), reconciler offers "Remove previous deploy keys" — same shape as the post-migration cleanup affordance.

This flow is mentioned here because the unification makes it possible at all — pre-unification, rotating "the backup key" meant N coordinated ssh-keygen + distribute + register cycles, with no atomic source-of-truth. Implementing the rotation UI itself is out of scope for Phase 2 (call out separately when needed).

---

## Phase split

Phase 2 (the implementation) is sized for a single PR but lands behind a feature gate to bound rollout risk.

| PR | Contents | Approximate scope |
|---|---|---|
| #1 (this spec's Phase 2) | New `backup_keys.py` module + reconciler + migration UI button + `ensure_bot_in_sync` wired into `deploy_bot` + `_ensure_deploy_key` moved to module scope. Old per-bot writers DELETED. Grep regression test added. Documentation updated. | ~500–800 LOC net (more removed than added, given the per-bot writer is ~150 LOC). |
| #2 (follow-on) | Daily `backup_key_drift` monitor + Signal type + chip on Backup → Cloud subtab. | ~150 LOC. |
| #3 (follow-on) | Mirror collapse: `{shared_dir}/pubkeys/<bot>.pub` → `{shared_dir}/pubkeys/shared.pub`. Compat shim in `/api/security/backup-config`. Legacy staging file sweep ("Remove `/Users/evolve/.ssh/evolve-backup-<bot>` files") affordance. | ~200 LOC. |
| #4 (deferred) | Key rotation UI + grace-period machinery + per-bot deploy-key cleanup affordance. | Not designed; opportunistic. |

PR #1 lands the structural fix in one merge. Subsequent PRs are polish.

---

## Deferred

- **Multi-backup-account pods.** A pod whose bots back up to multiple GitHub accounts (e.g., two bots to a personal account, six to an org). The shared model assumes ONE canonical key. Multi-account would need ONE canonical key per account, plus a per-bot mapping. No operator has asked for this; the architecture in this spec can be extended (rename `evolve-backup-shared` to `evolve-backup-shared-<account>` and key the per-bot copy by account) without a redesign. Picked up when a concrete operator request surfaces.
- **Cross-pod key sharing.** A multi-mini household with one operator. Each mini gets its own canonical key. Out of scope.
- **Encryption of the canonical private at rest.** The current model relies on macOS file permissions (mode 600, evolve-owned). FileVault is the operator's responsibility. Encrypting the key at rest with operator-provided passphrase is deferred — adds an unlock step on every backup push.
- **Removing the per-bot keys from GitHub automatically during migration.** Operator preference recorded yesterday: leave them in place. The affordance to remove them is offered but never automatic.
- **Rotating the backup key on a schedule.** Designed above but not built. PR #4.

---

## Resolved design decisions

1. **Canonical model = shared.** Argued in §"Principle." Aligns with the existing comment at server.py:9087, with yesterday's incident evidence, and with the operator-UX win for rotation and debugging. The per-bot isolation argument is real but small under realistic threat models (single mini, shared sudoers grants).

2. **Per-repo deploy-key registration, NOT user-account-level SSH key.** The shared pubkey is registered as a deploy key on EACH backup repo, even though all registrations carry the same pubkey value. Reason: per-repo authorization can be surgically revoked; account-level keys can't. Update server.py:9087's misleading "operator adds the returned pubkey ONCE to their GitHub user account" comment to reflect PR #2357's per-repo automation.

3. **Existing per-bot deploy keys on GitHub left in place during migration.** Operator preference recorded 2026-06-07 ("Leave them in place"). A failed migration must never strand a working credential. Cleanup is a separate, optional affordance with per-key confirmation. No bulk-delete on the first release.

4. **`network.json::backup.key_mode = "shared"` as the post-migration sentinel.** Absence ≡ "pre-migration legacy state." Presence ≡ "shared is asserted; drift is a bug to repair." Discovery is read-only; the sentinel is only written after the reconciler reports every backup-configured bot as aligned.

5. **Auto-recovery of the canonical source is forbidden.** If `/Users/evolve/.ssh/evolve-backup-shared` is missing, the reconciler emits a Signal and refuses to write anything. Generating a new shared credential is an explicit operator action — there is no scenario where silently creating a new pod-wide key is the right move (it would silently break every existing backup repo until each one is re-registered).

6. **Single writer enforced by grep test.** Python doesn't have a compile-time mechanism for "only this module may write these paths," but the regression test enumerates the forbidden strings and fails CI if a future commit reintroduces the per-bot writer shape. The test is the operational expression of "refuse to compile a state where both models could write the same file."

7. **Org/personal account split is orthogonal.** The shared-vs-per-bot model decision doesn't change how org-owned vs personal-owned repos are detected or created. The existing `_onboard_one_github_bot` logic is reused unchanged.

---

## Implementation order

Phase 2 lands as PR #1 above. Spec approval → branch → implementation → review → merge → reconcile the reference pod via the new "Migrate to shared mode" button → confirm `key_mode = "shared"` written → close out the 2026-06-07 incident.

After PR #1 lands, the bad states enumerated in §"Problem" are unreachable on any pod that has run the migration. Pods still on the legacy path are detected on every Backup → Cloud page load and prompted to migrate, but are not broken — backups continue working in whatever model they were in.
