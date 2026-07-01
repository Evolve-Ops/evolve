# Backup SSH-Key Distribution — Unification (2026-06-07)

**Status:** draft, awaiting decision on §3.
**Incident motivator:** 2026-06-07 — five member bots lost
backups silently for 2–3 days because `/Users/<bot>/.ssh/evolve-backup-<bot>`
contained the **shared** private key while the `.pub` next to it was a **per-bot**
public key. OpenSSH's `identity_sign` step refused both ("private key contents do
not match public") before any GitHub round-trip. Failure was invisible to the
backup daemon until `backup_failing`'s 3-strikes threshold tripped, so the gap
went unalerted for days.

The diagnostic surfacing for this state (Backup → Status inline mismatch row +
proactive `backup_ssh_key_pub_priv_mismatch` Signal) is being shipped in the
in-flight PR. **This spec is about the structural cause that produced the
crossed state — two writers with orthogonal models — and proposes a single
canonical writer.**

---

## 1. Today: two writers, orthogonal models

### Writer A — `deploy._ensure_backup_ssh_key` (per-bot model)

[packages/admin/evolve_admin/deploy.py:8037](packages/admin/evolve_admin/deploy.py:8037).
Runs on every `sudo evolve-admin deploy <bot>`.

1. Generates a per-bot Ed25519 keypair at
   `/Users/evolve/.ssh/evolve-backup-<bot>{,.pub}` if absent.
2. Calls `_distribute_backup_ssh_key` to copy *that* pair to
   `/Users/<bot_user>/.ssh/evolve-backup-<bot>{,.pub}`.
3. Comment in §1 of the function header tells the operator to "register the
   .pub as a GitHub **deploy key**" — i.e. on the backup *repo*, not the user.

Mental model: **per-repo deploy key per bot**. Operator adds a different deploy
key to each repo's `Settings → Deploy keys` on GitHub.

### Writer B — `api_security_backup_distribute_key` (shared model)

[packages/admin/evolve_admin/web/server.py:9038](packages/admin/evolve_admin/web/server.py:9038).
Triggered when the operator clicks **Distribute Key** in the Backup UI.

1. Generates one shared Ed25519 keypair at
   `/Users/evolve/.ssh/evolve-backup-shared{,.pub}` if absent (or accepts a
   `source_path` override).
2. For every bot in `network.json::bots` with a `backupRepoUrl`, copies the
   *same* shared keypair into `/Users/<bot_user>/.ssh/evolve-backup-<bot>{,.pub}`
   — per-bot filename, identical contents.
3. The comment block at server.py:9067–9072 is explicit about intent: "the
   operator adds the returned pubkey **ONCE to their GitHub user account**; the
   same key authenticates as that user against every repo the user owns / has
   push access to."

Mental model: **one identity for the operator's GitHub user; every backup repo
they own honors that identity automatically.**

### Why this crosses

Both writers target the same destination filename
(`/Users/<bot>/.ssh/evolve-backup-<bot>`). Neither knows about the other. After
the operator runs **Distribute Key** to switch to the shared model, the next
`deploy --all` reverts the bot home back toward per-bot if
`/Users/evolve/.ssh/evolve-backup-<bot>` still exists from an earlier deploy
(its `read_bytes()` won't equal the now-shared destination → mismatch → it
overwrites).

The specific "shared priv + per-bot pub" failure mode happened when one of the
two writers landed only the private half, leaving a stale pub from the other
generation behind — the explicit comment at server.py:9156–9159 acknowledges
exactly this hazard. The unification removes the surface entirely; the
mismatch Signal in the in-flight PR will catch any future drift.

### Daemon side is already agnostic

[packages/analyzer/backup.py:78](packages/analyzer/backup.py:78) (`ssh_key_path`)
resolves to `$HOME/.ssh/evolve-backup-<bot_id>` — bot-user-relative, per-bot
filename. [packages/analyzer/backup.py:132–149](packages/analyzer/backup.py:132)
(`_ssh_env`) builds `GIT_SSH_COMMAND` from that path. It does not care what
the key's *contents* are. The existing comment there already says: "The same
KEY VALUE can be copied into each bot's per-bot file (one GitHub account, one
pubkey, multiple repos — Scenario B), or each bot can carry a different value
(per-repo deploy keys — Scenario A). backup.py is agnostic."

**The contract daemon-side is the filename, not the key model.** No daemon
changes are needed regardless of which model we pick.

---

## 2. Picking the canonical model

Both models map a bot's backup push to a GitHub identity; they differ in how
the operator manages that identity on GitHub.

| | Per-bot deploy keys (Writer A) | Shared user-level key (Writer B) |
|---|---|---|
| GitHub registration | N deploy keys, one per bot, on N repos | 1 key on the operator's GitHub account |
| Adding a new bot | Generate key, paste into new repo's Settings | Nothing on GitHub side |
| Removing a bot | Revoke that repo's deploy key | Nothing on GitHub side (or revoke if it was the only user) |
| Repo isolation | Per-repo (one bot's key compromise ≠ another's) | None (any leaked copy authenticates everywhere the user has push) |
| Audit trail | Per-bot identity in GitHub event log | All pushes attributed to one user |
| UX fit for one-operator pod | Friction every onboarding | Match the operator's own SSH-key habits |

Evolve's stated audience (the "Plex test" — see
[`feedback_design_constraint_mildly_tech_capable.md`](../memory/feedback_design_constraint_mildly_tech_capable.md))
is one operator running a home pod. They already have an SSH key registered on
their GitHub account — they expect one identity to work across their repos.
The per-bot deploy-key model imposes the per-repo GitHub UI dance on every
onboarding, with no isolation payoff in practice (the operator's account has
push access to every repo anyway; an attacker who reaches `/Users/<bot>/.ssh/`
on the mini has already compromised the host).

**Proposed canonical model: shared user-level key.** Per-repo isolation
becomes an opt-in advanced override, not a default obligation.

### Filename stays per-bot

`backup.py::ssh_key_path` resolves to a per-bot file; the daemon reads that
path. The per-bot filename keeps the daemon contract intact and preserves the
**capability** for per-repo isolation — an operator who really wants
per-bot keys can drop a different priv/pub pair into one bot's file
manually and that bot diverges, no code change. The default writer just
makes them all identical.

---

## 3. Design decision: subtract A, or invariant-check both?

Two viable shapes. Both produce a coherent end state; they differ in code
surface and operator power.

### Option 3a (recommended) — Subtract Writer A

`deploy._ensure_backup_ssh_key` is rewritten to consume the shared source
that Writer B already produces. There is no longer a per-bot ssh-keygen
branch in deploy.py. The per-bot generate endpoint in server.py
(`/api/backup/cloud/keys/generate`, [server.py:9017](packages/admin/evolve_admin/web/server.py:9017))
and the `--generate-keys` mode of backup.py
([backup.py:931](packages/analyzer/backup.py:931)) are removed —
they have no caller in the canonical model.

- Pros: one writer, one mental model, zero risk of crossed state. Smallest
  ongoing maintenance surface.
- Cons: an operator who wants per-bot keys has to drop them in by hand
  (which the comment at server.py:9071 already advertises as the override
  path; the spec just makes that the explicit-only path).

### Option 3b — Keep both, add invariant check

Both writers persist, but every distribute call (from either path) refuses to
write if the destination is currently from the other model unless the
operator passes an explicit `force=true`.

- Pros: explicit power to maintain per-bot in mixed mode without manual
  file drops.
- Cons: two code paths to keep in sync; the mental model still requires
  operators to understand both. The crossed-state surface returns the
  moment someone forgets to set `force`.

**Recommendation: 3a.** Pre-launch we should be subtracting code, not adding
invariants over duplicated paths
(see [`feedback_prelaunch_architect_properly.md`](../memory/feedback_prelaunch_architect_properly.md)).
The override capability is preserved by the per-bot filename — the only thing
3a removes is automatic per-bot *generation*, which the data says nobody
actually wants.

Section 4 onward assumes Option 3a.

---

## 4. Canonical model (post-spec)

### Source of truth

`/Users/evolve/.ssh/evolve-backup-shared{,.pub}` is the **only** Evolve-managed
backup keypair. Generated on first deploy that needs it. Mode 600 priv, 644 pub,
owned by `evolve:staff`.

### Distribution

Each bot with `bots.<id>.backupRepoUrl` set has
`/Users/<bot_user>/.ssh/evolve-backup-<id>{,.pub}` byte-identical to the
shared source. Mode 600/644, owned by `<bot_user>:staff`.

### Per-bot override (advanced, undocumented in UI)

If an operator drops a different priv/pub pair into `/Users/<bot>/.ssh/` by
hand (or via `source_path` on the distribute API), deploy's distribute call
must NOT overwrite it. The shared writer compares dst bytes to the **shared
source**; if they don't match, it could be:
- (a) drift from a stale earlier write — should be repaired, or
- (b) an intentional per-bot override — must be preserved.

Distinguish via a pubkey check, not a private-key check: if the destination's
`.pub` derives (via `ssh-keygen -y -f <priv>`) into something other than the
shared pubkey AND the priv/pub on disk are internally consistent (the priv
matches its own pub), treat it as an intentional override and leave it alone.
Drift (priv ≠ pub) is unambiguously broken and gets the shared key written on
top.

> The override behavior is intentional but not advertised in the UI for v1
> — the in-flight mismatch Signal will surface the override as a "this isn't
> the shared key" notice, not an error. We document the behavior here so a
> future "advanced backup identity" UI has somewhere to attach.

### Pubkey copy for the UI

The admin-readable pubkey copy at `{shared_dir}/pubkeys/<bot>.pub` stays. The
distribute writer keeps writing it. With the shared model every per-bot file
will contain the same key text — the UI already collapses this into a "shared
key" indicator visually
([server.py:8900–8903](packages/admin/evolve_admin/web/server.py:8900)).

---

## 5. Code changes (deltas only)

### Subtract from `deploy.py`

- [`_ensure_backup_ssh_key`](packages/admin/evolve_admin/deploy.py:8037) — replace
  the per-bot generation branch with: locate (or generate) the shared key,
  then call the distribute helper with the shared source. The function
  signature stays the same so the caller at line 6238 is unchanged.
- [`_distribute_backup_ssh_key`](packages/admin/evolve_admin/deploy.py:8102) —
  add the override-preserving branch from §4 before the unconditional
  overwrite. Keep the existing byte-equality fast path.

### Subtract from `analyzer/backup.py`

- [`generate_ssh_deploy_key`](packages/analyzer/backup.py:82) — delete. No
  remaining caller in the canonical model. The only live invocations are
  through `/api/backup/cloud/keys/generate` (which is also going away) and
  `--generate-keys` (same).
- [`--generate-keys` CLI mode in `main`](packages/analyzer/backup.py:931) —
  delete the branch.

### Subtract from `web/server.py`

- [`/api/backup/cloud/keys/generate`](packages/admin/evolve_admin/web/server.py:9017)
  and its alias `api_security_backup_generate_key_alias` — delete. The
  Distribute Key endpoint is sufficient for both first-run and rotation.
- [Onboarding callers at 16957](packages/admin/evolve_admin/web/server.py:16957) and
  the rotation message at 15945 — repoint to the shared key path
  (`/Users/evolve/.ssh/evolve-backup-shared.pub`).
- The `api_security_backup_config_get` legacy-fallback that reads
  `/Users/evolve/.ssh/evolve-backup-<bot>.pub`
  ([server.py:8916](packages/admin/evolve_admin/web/server.py:8916)) stays
  for one release as the migration-period read path for unmigrated pods.

### Keep unchanged

- `analyzer/backup.py::ssh_key_path` and `_ssh_env` — the daemon contract.
- The `backup_ssh_key_missing` Signal — still load-bearing for the
  "destination filename absent" case.
- The pubkey copy at `{shared_dir}/pubkeys/<bot>.pub`.

### Add (small)

- A `backup_ssh_key_pub_priv_mismatch` Signal type in `backup_signal.py` —
  fires when `ssh-keygen -y -f <priv>` doesn't equal `<pub>` on disk. This
  is the in-flight PR's surfacing work; mentioned here only to flag that
  the spec's "drift" branch in §4 should *resolve* this Signal as a side
  effect when it overwrites with the shared key.

---

## 6. Migration

A small number of bots on this pod are still on per-bot keys (admin-bot, evo,
and a personal-bot). They have a per-bot deploy key registered on their backup repo's GitHub
`Settings → Deploy keys`. The shared key may or may not be registered on the
operator's GitHub user account.

The migration is **deploy-driven and convergent**, not a separate command:

1. **Precondition step (operator):** confirm the shared pubkey at
   `/Users/evolve/.ssh/evolve-backup-shared.pub` is registered on the
   operator's GitHub user account. The Backup UI's "Distribute Key" flow
   already prints this pubkey for copy-paste; on a freshly-unified pod the
   admin server should also surface a one-line status card
   ("Shared backup key registered on GitHub?" with a "Mark as registered"
   toggle that just flips a boolean in network.json::backup.sharedKeyRegistered
   — there's no API to actually verify it from the user-account side).

2. **First post-unification deploy of each per-bot bot:** the new
   `_ensure_backup_ssh_key` notices the bot's distributed priv is NOT the
   shared source and the priv/pub on disk *are* internally consistent — so
   per §4 it's treated as an intentional override and left alone… **unless**
   `network.json::backup.sharedKeyRegistered == true`, in which case we
   take that as the operator opting into migration and overwrite with the
   shared key.

   Rationale: the operator's GitHub-side click is the gate. Until they
   register the shared key, "overwrite per-bot with shared" would just
   break the bot's backup push (auth fails). Once they register, the next
   deploy converges.

3. **The per-bot deploy keys on the GitHub repo side** are NOT auto-revoked
   — that's an operator-judgment call (they may still want the per-bot
   identity around for some other reason). The deploy result's log line
   surfaces the action item:

   > `<bot>` migrated from per-bot to shared backup key. The per-bot
   > deploy key on `<repo>` (fingerprint `<sha256:…>`) is still registered;
   > revoke it manually at `https://github.com/<owner>/<repo>/settings/keys`.

4. **The stale `/Users/evolve/.ssh/evolve-backup-<bot>{,.pub}`** on the
   `evolve` user is left in place too, for the same "leave the audit trail
   intact" reason. The lifecycle inventory at
   [`inventory.py:489`](packages/admin/evolve_admin/lifecycle/inventory.py:489)
   already lists it under the BACKUP category with a manual_action prompt;
   that stays correct.

5. **Phase-E `_evo_cutover_migrate_backup_ssh_key`** at
   [setup_wizard.py:1430](packages/admin/evolve_admin/setup_wizard.py:1430)
   keeps copying `evolve-backup-<primary>` AND `evolve-backup-shared` to
   `/Users/evo/.ssh/` — the per-bot copy is vestigial after this spec
   ships but keeps the cutover code resilient on pods migrated mid-spec.
   A follow-on cleanup PR drops the per-bot copy from cutover too once
   we're confident no pod has a mixed state.

---

## 7. Out of scope

- The diagnostic surfacing in the current PR (Backup → Status inline
  mismatch row + `backup_ssh_key_pub_priv_mismatch` Signal). This spec
  references it but doesn't redefine it.
- A UI for the per-bot override capability described in §4. Documented
  here for the future, not built in v1.
- Auto-revocation of stale per-bot GitHub deploy keys. Requires a PAT with
  `admin:public_key` on the repo and explicit operator opt-in; out of
  scope for the unification.

---

## 8. Validation

After implementation, verify:

1. **Fresh pod:** install + deploy + configure backup repo for a bot.
   `/Users/evolve/.ssh/evolve-backup-shared` exists; bot's home has
   byte-identical `evolve-backup-<bot>{,.pub}`; backup push works after
   operator registers the shared pubkey once.

2. **Crossed-state recovery (regression test for the 2026-06-07 incident):**
   Manually plant `shared priv + per-bot pub` in a bot's home, run
   `sudo evolve-admin deploy <bot>`, confirm the mismatch is detected and
   the distribute step overwrites both with the shared pair. Verify the
   `backup_ssh_key_pub_priv_mismatch` Signal (in-flight PR) resolves on
   the next sweep.

3. **Override preservation:** plant a different but internally-consistent
   priv/pub pair, with `backup.sharedKeyRegistered = false`, run deploy.
   Confirm the override is preserved.

4. **Migration of a per-bot bot (admin-bot):** flip
   `backup.sharedKeyRegistered = true`, run `sudo evolve-admin deploy admin-bot`,
   confirm the per-bot key is replaced with the shared pair AND the
   deploy result logs the GitHub-side manual revoke instruction.
