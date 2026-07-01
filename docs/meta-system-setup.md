# Replicating the META coordinator system

The META coordinator workflow (doctrine: [META-session-guide.md](META-session-guide.md)) is designed to be **reproducible from a clone** — so it survives a new laptop, a new operator, or a different agentic-coding tool. This doc inventories the system's footprint and how to stand it up elsewhere.

## Footprint — where each part lives

| Part | What it is | Location | In this repo? |
|---|---|---|---|
| **Doctrine** | the authoritative behavior — bootstrap + lifecycle in `docs/META-bootstrap.md` (the lean every-bout doc); ownership map · reconciler · `/design` intake in `docs/META-session-guide.md` (the on-demand full reference); the aspect registry itself in `docs/META-aspect-registry.md` | `docs/META-bootstrap.md` + `docs/META-session-guide.md` + `docs/META-aspect-registry.md` | ✅ |
| **Per-aspect specs** | the design source of truth for each aspect | `docs/spec-*`, `docs/design-*`, `docs/roadmap-*` | ✅ |
| **Launcher skills** | `/meta`, `/status`, `/close` — thin launchers that defer to the guide | repo `.claude/skills/` **+ operator-global `~/.claude/skills/`** (keep both — see step 1) | ✅ repo + mirror |
| **Fleet watchers** | edge-triggered status poke + daily loose-ends sweep | scheduled tasks (operator-local) | ⬇ templated in the Appendix |
| **Shipped digest** | daily plain-language "what shipped to your pod" summary | scheduled task (operator-local) | ⬇ templated in the Appendix |
| **Per-aspect memory** | in-flight ledgers, lessons, feedback | Claude Code auto-memory (`~/.claude/projects/<slug>/memory/`) | ❌ rebuilds from artifacts |

The split is deliberate: the **doctrine + specs** travel with the repo; the **launchers** live in the repo *and* are mirrored to operator-global `~/.claude/skills/` (step 1 — repo copies only load on a branch that includes them, so the global mirror is what guarantees availability); the **watchers** and **memory** are operator-local. The system is resilient to losing the local parts because **Bootstrap reads artifacts (`gh pr list`, the specs), not memory** — a fresh machine reconstructs in-flight state from PRs, and lessons re-accumulate as you work.

## Stand it up on a new machine (Claude Code)

1. **Clone the repo, then mirror the launcher skills to your global config.** The doctrine, specs, and the three launcher skills come with the clone and load from `.claude/skills/`. **But project skills only load when the *current checkout's branch* has them in its tree** — so the moment you work on a feature branch or worktree that predates a skill, the repo copy isn't there and `/meta` `/status` `/close` silently vanish from that session. Copy them to your operator-global `~/.claude/skills/`, which loads regardless of branch:
   ```bash
   for s in meta status close; do
     mkdir -p ~/.claude/skills/$s
     git show origin/main:.claude/skills/$s/SKILL.md > ~/.claude/skills/$s/SKILL.md
   done
   ```
   **Keep both:** the **repo** copy is the version-controlled source of truth + portability mirror; the **global** copy guarantees availability across every branch and worktree. When the repo copy changes, re-run that loop to refresh the mirror. (Skills load at session *start* — restart a session to pick up a change.)

   **This loop is now also automated** — the `tools/meta-skills-sync` tool + its SessionStart hook (see [the auto-sync subsection](#enforcement-layer-the-global-claudeskills-auto-sync-hook) below) re-run the equivalent of this loop on every session start, so the mirror self-heals instead of drifting when the manual loop is forgotten (it was once stale for 8 days, silently no-op-ing a merged mechanism). The hook is **operator-gated** (global blast radius); the manual loop above stays the source-of-truth fallback and the **immediate** post-merge refresh.

   **The unattended sweep brains are mirrored the same way** — `meta-reconcile` (the one *load-bearing* scheduled task; the watchers in step 2 are report-only) and `meta-coherence` (report-only, but version-controlled the same way so its tool-discipline can't silently rot). Both behaviors are version-controlled at `docs/meta-reconcile-procedure.md` / `docs/meta-coherence-procedure.md` (operator-agnostic — `~/` paths + a `*evolve*` project glob, so the copies need no per-machine edits). Install/refresh them into the paths the scheduler runs:
   ```bash
   for p in meta-reconcile meta-coherence; do
     mkdir -p ~/.claude/scheduled-tasks/$p
     git show origin/main:docs/$p-procedure.md > ~/.claude/scheduled-tasks/$p/SKILL.md
   done
   ```
   The cron schedule + `enabled` flag live in the scheduled-tasks registry, **not** in these files, so refreshing a body never disturbs the schedule. Treat the `docs/*-procedure.md` files as the source of truth: each local copy is **ephemeral** (the runner has been observed to truncate it to 0 bytes), so if a scheduled run ever does nothing, re-run the loop above to restore it.
2. **(Optional) recreate the watchers** from the Appendix templates as scheduled tasks, and add the **unattended-permissions allowlist** (Appendix) so they don't stall on an approval prompt. They're report-only — pure convenience, not load-bearing. (The load-bearing `meta-reconcile` brain is *not* here — it is a repo-sourced file, installed in step 1.)
3. **Memory takes care of itself.** Don't copy it. The first `/meta <id>` reconstructs the aspect's state from its spec + `gh pr list`; lessons re-accumulate.

## Multiple accounts on one machine — already shared, nothing to install

The section above is for a genuinely *separate* machine / `$HOME`. If instead you run several
Anthropic accounts (different login emails) **as the same OS user on one machine**, there is a
single `$HOME` and therefore a single `~/.claude/` with **no per-account or per-organization
subdirectory** (verified 2026-06-17). Everything in the Footprint — `settings.json`, `skills/`,
`hooks/`, `scheduled-tasks/`, and the `projects/.../memory/` + `meta-state/*.json` ledgers — is
`$HOME`-scoped, so **the whole substrate is shared across all your accounts by construction.** The
only per-account state is the OAuth credential (macOS Keychain). To run META from another account,
just switch login — there is nothing to install or sync, and all accounts coordinate on one ledger.

- **Do NOT set `CLAUDE_CONFIG_DIR` to isolate accounts.** It points each account at a separate
  `~/.claude-<x>` tree and would *fork* the shared ledger. Use separate config dirs only if you
  deliberately want isolated, non-coordinating worlds — the opposite of the shared-ledger model.
- **Scheduled-sweep quota — pin it yourself.** A scheduled task runs under *whichever account is
  active/last-logged-in when it fires*, and **cannot be pinned** to a specific account. So the
  load-bearing `meta-reconcile` (and the report-only `meta-coherence` / `meta-loose-ends` /
  `meta-fleet-watch`) bill whatever account is logged in at fire time. **Policy: designate one
  "automation account" and keep it logged in for the sweeps**, so sweep cost lands in one quota
  pool predictably rather than scattering across accounts.

## Running the substrate for a second project on the same machine

The two sections above cover a *separate machine* and *multiple accounts as one OS user*. A
third case: you run Evolve **and a second, unrelated project** (e.g. `cjalden/calgraph`) out of
different checkouts on the **same machine**, and you want the META coordinator workflow in both.
Most of the substrate is already there — one home-scoped part does the heavy lifting, and a small
per-project part needs seeding.

**The linchpin: `~/.claude/` is home-scoped, so it is already shared by every checkout.** The
skills (`/meta` `/status` `/close` `/design` …), the enforcement hooks, the scheduled sweeps, and
the per-aspect memory / `meta-state/*.json` ledgers all live under `~/.claude/` (the same reason
they are shared across accounts, above). They load and run in **any** repo on the machine with
nothing to install — the moment you open a Claude Code session in the second project's checkout,
`/meta` is available and the sweeps already see its work. What a fresh project lacks is the
**repo-tracked half** of the substrate: the doctrine docs the skills read (`docs/META-*.md` and
friends), the generic `tools/meta-*` those procedures shell out to, and a per-project
`.claude/meta.json` (+ its own aspect registry). Without them, `/meta` misfires there — e.g.
`tools/meta-config` is "command not found", and the skills fall back to Evolve's repo slug.

**Seed it with `tools/meta-substrate-sync`.** From the Evolve checkout (the substrate's dev-home /
source of truth), run:

```bash
tools/meta-substrate-sync /path/to/other-project        # install / refresh
tools/meta-substrate-sync /path/to/other-project --dry-run   # preview first
```

It installs into the target, from Evolve's working tree, the repo-tracked half:

- **Doctrine docs → `<target>/docs/`** — the closed set the skills' bootstrap reads
  (`META-bootstrap.md`, `META-session-guide.md`, `meta-ledger-schema.md`,
  `meta-reconcile-procedure.md`, `meta-coherence-procedure.md`, `meta-system-setup.md`,
  `using-the-meta-system.md`). Every doc→doc link among them stays inside the set.
- **Generic tools → `<target>/tools/`** — `meta-config` + `meta_config.py`, `meta-inflight`,
  `meta-issue`, `meta-ledger-prune`, `meta-queue`, `meta-skills-sync`, and the resolver's test
  `test_meta_config.py`. It **excludes** `tools/preflight` and `tools/ui-style-lint` — those are
  Evolve-specific; the target authors its **own** preflight (see below).
- **A scaffolded `<target>/.claude/meta.json`** (only if absent) — the per-project config from
  [P1](META-session-guide.md). `repo_slug` is prefilled from the target's `git remote get-url
  origin` when it can be inferred (else a `REPLACE-ME` placeholder you must edit); `registry_path`
  points at the target's own fresh registry (next bullet); `preflight_cmd` defaults to
  `tools/preflight`.
- **A fresh starter aspect registry** at that `registry_path` (only if absent) —
  `docs/META-aspect-registry.md`, **empty of Evolve's aspects**. This is the load-bearing
  correctness point: the registry the skills read via `registry_path` is the target's **own**, so
  the second project defines its own aspects. (The synced `docs/META-session-guide.md` still
  carries Evolve's registry *table* as inert reference doctrine — but nothing reads that embedded
  table; the live registry is resolved through `registry_path`.)

The sync is **byte-exact and idempotent** (a second run over an already-synced target is a
no-op), **never deletes** a target-local file, and **never clobbers** an existing target
`.claude/meta.json` or registry (those are the target's own config). Re-run it after you pull new
substrate changes into Evolve to refresh the target's copies.

**Two things the target still owns.** `meta-substrate-sync` seeds the shared machinery; it does
**not** decide the target's project-specific parts:

- **Its own `preflight`.** The scaffolded `meta.json` sets `preflight_cmd: tools/preflight`, but
  that file is *not* synced — the target authors a preflight that runs *its* CI gates (the "run
  what CI runs" contract from [CLAUDE.md](../CLAUDE.md)), then points `preflight_cmd` at it.
- **Its own registry content.** The starter registry ships empty; the project fills in its
  aspects per the new-aspect protocol in [`META-session-guide.md`](META-session-guide.md).

> **One synced-test caveat.** `test_meta_config.py` includes `test_evolve_own_config_matches_defaults`,
> which asserts the *repo's* `.claude/meta.json` restates Evolve's defaults. In another project
> that case is expected to fail (the target's `meta.json` intentionally differs) — adapt or drop
> that one case when the target wires the resolver into its own test run. The rest of the file is
> the shared resolver's regression suite and passes anywhere.

## Adapt it to another tool (Cursor, Codex, …)

The portability property: the **doctrine is plain markdown** and the **bootstrap reads git/`gh` artifacts** — both tool-agnostic. To run the workflow where `.claude/skills/` isn't read:

- **The skills are just procedures.** Where the tool has a command/rule mechanism (Cursor commands or `.cursor/rules`, a Codex `AGENTS.md`), wire `meta` / `status` / `close` to invoke the matching section of `META-bootstrap.md` (Bootstrap / the return-pulse / the checkpoint ritual). Where it has none, read that doc and follow those steps by hand.
- **Dispatch needs only two primitives:** spawn a fresh agent and make a git branch/worktree. Any tool with those can play the "chip" role; status is always read from artifacts (`git ls-remote` / `gh pr list`), never from a tool-specific inbox.
- **The trio is the continuity.** Registry row + specs travel in the repo; "memory" can be any durable notes store the new tool offers (or just the spec docs, which Bootstrap already treats as the source of truth).

---

## Appendix — scheduled-task templates (recreate as scheduled tasks)

Operator-local; paths shown as `~/…` and the project-memory slug as `<project-slug>`. Set `REPO` to your repo slug. All are **report-only** — they never merge, spawn, or modify. Current as of 2026-06-13; the live copies are operator-local and may drift.

### Permissions — let them run unattended

A scheduled task fires with no human watching, so any tool call that isn't pre-approved **stalls on an "approve once" prompt** that is awkward to reach. Pre-approve the read-only surface these tasks use by adding an `allow` list to operator settings (`~/.claude/settings.json`):

```json
"permissions": {
  "allow": [
    "Bash(gh pr list:*)",
    "Bash(gh pr view:*)",
    "Bash(gh pr checks:*)",
    "Bash(git fetch:*)",
    "Bash(git ls-remote:*)",
    "Bash(git log:*)",
    "Bash(mkdir:*)",
    "Read",
    "Write(~/.claude/meta-watch/**)",
    "PushNotification"
  ]
}
```

Everything here is read-only except the watcher's own state file — nothing can mutate the repo or the pod. Permission posture is **independent of the model**: this allowlist (not Sonnet-vs-Opus) is what lets an unattended task complete. If `~` isn't expanded in a `Write(...)` rule on your setup, use the absolute home path.

**Two guardrails are NOT permission-grantable — `bypassPermissions` does not suppress either, so the only fix is command-shape discipline (baked into the `docs/*-procedure.md` bodies above), never an allow rule:**

- **Untrusted git hooks.** `cd <dir> && git …` trips a hardcoded safety check (not a permission gate) and pauses the run mid-flight. Use `git -C <dir> <subcommand>` / `git log <ref>` with no `cd`. The reconciler goes further and never inspects a worktree's *working tree* at all (`git status` / `git diff`) — it reads a chip's progress from `gh pr` state + the ledger, not from files on disk.
- **simple_expansion / compound shell.** A shell loop, glob, pipe, `$(...)`, or `cat`/`echo` over the ledgers (e.g. `cd meta-state && for f in …; do cat $f.json; done`) trips the "simple_expansion" / "contains expansion" guardrail. Read each ledger with the Read tool, one file per call; issue one single-command `gh` / `git` call at a time and parse its `--json` in your own reasoning.

This is *why* both sweep procedures forbid those exact command shapes: a stalled unattended run that "did nothing" is almost always one of these two guardrails, not a missing `allow` rule — so adding a permission won't fix it, but rephrasing the command will.

#### Enforcement layer: the `cd`&&`git` rewriting hook

Command-shape discipline is necessary but not *sufficient* for the **untrusted-git-hooks** class above — prose in a procedure can't bind a model that drifts back to `cd <dir> && git …`, and it can't bind code or sub-agents at all. The deterministic complement is a PreToolUse(Bash) hook, version-controlled at [`tools/hooks/rewrite-cd-git.sh`](../tools/hooks/rewrite-cd-git.sh), that rewrites the command **before** the guardrail can see it:

> `cd <dir> && git <readonly…>`  →  `git -C <dir> <readonly…>`  (each git in the chain gets its own `-C <dir>`)

Because the guardrail fires *before* permissions and hooks and is not suppressible by `bypassPermissions` or a `permissionDecision: allow` hook, the only deterministic fix is to remove the `cd … && git` text — which a PreToolUse hook can do via `hookSpecificOutput.updatedInput.command`. This scope is **only** the untrusted-git-hooks class; the `simple_expansion` / compound-shell class stays discipline-only (the hook does not touch it — a pipe/glob/`$(…)`/loop is *not* rewritten and still requires the command-shape rules above).

**Fail-safe contract — the worst case is today's prompt, never a rewrite.** The hook rewrites **only** when *every* git invocation in the chain is read-only (`status`/`log`/`show`/`diff`/`branch --list`/`rev-parse`/… — and `stash`/`tag`/`branch`/`remote`/`config` only in their non-mutating listing forms) **and** *every* non-git segment (split on the operators `&&`/`||`/`;`/`|`) is a **whole-token** cwd-insensitive benign command — `echo`/`printf`/`true`/`:` with literal args, or `head`/`tail`/`wc` reading **stdin only** (a positional file operand bails — it would resolve against the original cwd) — with no glob char (`*`/`?`/`[`) anywhere in the post-`cd` text (the shell expands a glob against the original cwd). On *any* uncertainty — a non-readonly git (`push`/`commit`/`reset`/`checkout`/`branch -D`/`stash push`/…), an unrecognized or destructive segment (`rm -rf foo`, `ls`, `tee`), a benign command carrying a file operand or a benign-*prefixed* name (`echo-foo`/`head.py`), a nested `cd`, an embedded newline or carriage return, a redirection operator (`>`/`>>`/`<`/`2>`/`&>`), a glob char (`*`/`?`/`[`), command substitution (`$(…)`/backticks), malformed JSON on stdin, or a missing `jq`/`python3` — it emits **nothing**, the original command proceeds unchanged, and you get the normal prompt. Within that scope, destructive verbs and write-git invocations are in neither allow-list, so they always bail; and a backgrounded read-only git (`git log & rm …`) leaves the trailing command byte-for-byte untouched with its cwd unchanged (the `cd` only ever lived inside the backgrounded subshell). The regression guard for these cases is [`tools/hooks/test_rewrite_cd_git.sh`](../tools/hooks/test_rewrite_cd_git.sh) (`bash tools/hooks/test_rewrite_cd_git.sh`) — because the hook intercepts *every* Bash command, run that test after any edit to the hook.

**Closed gaps — the splitter is text-level, not a shell parser, so it hardens defensively rather than parsing.** Two boundary kinds it does *not* recognize as segment separators would once let a trailing command ride on the read-only-git segment and be rewritten without being classified — silently dropping the `cd` and relocating that command's cwd to the *original* directory. The pre-loop bail now rejects both, so each falls through to passthrough (at worst today's prompt):
> - **Embedded newline (the one that could be destructive).** A multi-line command — `cd <dir> && git log`⏎`rm -rf foo` — would have rewritten to `git -C <dir> log`⏎`rm -rf foo`, running the `rm` in the original cwd instead of `<dir>`. The hook now bails whenever the post-`cd` text contains a newline or carriage return.
> - **Relative-path redirect (benign content only).** `cd <dir> && git log > out.txt` would have left `out.txt` resolving against the original cwd, not `<dir>` (absolute targets and `| tee` were already unaffected — `tee` bails). The hook now bails whenever the post-`cd` text contains a redirection operator (`>`/`>>`/`<`/`2>`/`&>`).
> - **Relocated reads (faithfulness — no data loss).** `cd <dir> && head rel.txt && git status` would have read `rel.txt` from the original cwd; a benign-*prefixed* name (`echo-foo`) ran a different command; and a glob (`wc *`, or even `git diff *.py`) expanded against the original cwd, feeding a different fileset. The classifier now whole-token-matches the benign command, bails on a `head`/`tail`/`wc` file operand, and bails on any glob char (`*`/`?`/`[`) in the post-`cd` text. These relocate *reads*, never writes, so they never breached the destructive contract — but they did break semantic faithfulness, so they bail too.

These bails are conservative — *any* newline, redirection operator, or glob char in the post-`cd` text forces passthrough, even where it would have been harmless — consistent with the fail-safe contract above. Regression-guarded by the `P-newline-rm`, `P-redirect`, `P-head-relpath`, `P-benign-prefix`, `P-glob-benign`, and `P-glob-git` cases in [`tools/hooks/test_rewrite_cd_git.sh`](../tools/hooks/test_rewrite_cd_git.sh). Hardened by **META:substrate** follow-ups to the PR that introduced this hook (the second one closing the read-faithfulness gaps a two-pass review surfaced).

**Install (operator, per machine).** Copy the hook into place and register it as a **user-level** PreToolUse matcher:

```bash
mkdir -p ~/.claude/hooks
cp tools/hooks/rewrite-cd-git.sh ~/.claude/hooks/rewrite-cd-git.sh
chmod +x ~/.claude/hooks/rewrite-cd-git.sh
```

Then add to **user-level** `~/.claude/settings.json` (merge into any existing `hooks.PreToolUse` array — do not clobber other matchers):

```json
"hooks": {
  "PreToolUse": [
    {
      "matcher": "Bash",
      "hooks": [
        { "type": "command", "command": "~/.claude/hooks/rewrite-cd-git.sh", "timeout": 10 }
      ]
    }
  ]
}
```

It must live in **user-level** `~/.claude/settings.json` (not a project's `settings.local.json`) so it also runs in **scheduled / headless** tasks, which merge only the user-level config — that is precisely where the guardrail prompt is unreachable and silently stalls a run. The hook takes effect on the **next session start**. Deps: `jq` and `python3` (already required by the pod). If `~` isn't expanded in the `command` on your setup, use the absolute home path (e.g. `/Users/<you>/.claude/hooks/rewrite-cd-git.sh`).

**Disable.** Remove the `"matcher": "Bash"` block from `hooks.PreToolUse` in `~/.claude/settings.json` (and optionally delete `~/.claude/hooks/rewrite-cd-git.sh`); takes effect next session start.

#### Enforcement layer: the cross-checkout mutation guard

The `cd`&&`git` hook above *clears* read-only cross-directory git for unattended runs. The dangerous **inverse** — a chip / sibling / reconcile session reaching **out** of its own worktree to run a **mutating** op against the ONE shared dev checkout (`git checkout` / `reset --hard` / `commit` / `stash pop` / `clean` → a silent branch switch or WIP wipe for a concurrent session) — is exactly the "concurrency by convention, not construction" root cause the 2026-06-30 substrate audit named (≥6 documented incidents: sibling-chip wipe, dispatch-from-clean-base, worktree-cd-failure-runs-in-main, git-stash-shared-across-worktrees). `spawn_task` already starts each chip in its own worktree; the failure is a chip *escaping* it. Prose ("cd to your worktree") drifts and can't bind code or sub-agents. The deterministic complement is a PreToolUse(Bash) hook, version-controlled at [`tools/hooks/guard-cross-checkout-mutation.sh`](../tools/hooks/guard-cross-checkout-mutation.sh), that **blocks** (emits `permissionDecision: "deny"`) a high-confidence mutating op targeting a checkout that is **not this session's own worktree**:

> `cd <foreign> && git reset --hard` · `git -C <foreign> checkout main` · `cd <foreign> && rm -rf build`  →  **denied**

**"Own worktree" is generic, never a hardcoded path** — it is derived from the PreToolUse payload's `cwd` (the `git rev-parse --show-toplevel` containing it), so the guard works identically in any repo (CalGraph included) with nothing Evolve-specific. A command that `cd`s to — or `git -C`s — a **different** checkout toplevel and then runs a mutating verb is the block target; both toplevels are resolved through the *same* `git rev-parse --show-toplevel`, so the compare is canonical and symlink-safe.

**Fail-safe contract — the worst case is today's behavior (NO block), NEVER a false block of a chip working in its own tree.** It blocks **only** when it positively identifies **both** (a) a mutating verb — git `checkout`/`switch`/`reset`/`commit`/`merge`/`rebase`/`cherry-pick`/`revert`/`am`/`clean`/`restore`/`mv`/`rm`/`apply`, `stash` (except `list`/`show`), `branch` (only delete/move/force flags or a create form); or a mutating **shell** op (`rm`/`mv`/`cp`/`tee`/`truncate`/`ln`/`touch`/`sed -i`, `>`/`>>` redirect) **whose path operands are all relative** so they provably resolve under the foreign root — **and** (b) a target checkout toplevel that **differs** from the session's own. Read-only git (`log`/`status`/`diff`/`branch --list`/…) is **not** guarded (the `cd`&&`git` hook already owns it). On **any** uncertainty — cwd/own-toplevel unresolvable, target toplevel unresolvable (junk path, non-repo, `cd /nonexistent`), no explicit foreign path, an ambiguous/read-only verb, a path token carrying `$()`/backtick/glob/quote, an **absolute** shell operand (could point anywhere), malformed JSON, or a missing `jq`/`git`/`python3` — it emits **nothing**, the command proceeds to the normal permission flow, and there is no regression. It **never** blocks an op confined to the session's own worktree (including a `cd` into a **subdirectory** of that worktree, whose toplevel is identical). **Known limitation (by design):** a *bare* mutation (`git reset`, no `cd`/`-C`) that runs foreign only because a **prior, separate** Bash call left the persisted cwd in another checkout is not caught — the payload cwd then already reads as that checkout, so nothing is foreign; this hook binds the **explicit in-command** reach-out, the documented incident shape. Regression-guarded by [`tools/hooks/test_guard_cross_checkout_mutation.sh`](../tools/hooks/test_guard_cross_checkout_mutation.sh) (`bash tools/hooks/test_guard_cross_checkout_mutation.sh`), which pins both the true blocks and the wide own-worktree / read-only-foreign / junk-input passthrough surface — because the hook intercepts *every* Bash command, run that test after any edit to the hook.

**Install (operator, per machine).** Copy the hook into place and register it as a **user-level** PreToolUse matcher (merge into any existing `hooks.PreToolUse` array — the `cd`&&`git` `Bash` matcher already lives there; **add this as a second `Bash` entry, both run**):

```bash
mkdir -p ~/.claude/hooks
cp tools/hooks/guard-cross-checkout-mutation.sh ~/.claude/hooks/guard-cross-checkout-mutation.sh
chmod +x ~/.claude/hooks/guard-cross-checkout-mutation.sh
```

```json
{ "matcher": "Bash",
  "hooks": [ { "type": "command", "command": "~/.claude/hooks/guard-cross-checkout-mutation.sh", "timeout": 10 } ] }
```

User-level (not a project's `settings.local.json`) so it also runs in **scheduled / headless** sweeps — precisely where an out-of-worktree mutation is unattended and destructive. Takes effect on the **next session start**. Deps: `jq`, `git`, `python3` (already required by the pod). If `~` isn't expanded in the `command` on your setup, use the absolute home path.

**Disable.** Remove the `"matcher": "Bash"` block pointing at `guard-cross-checkout-mutation.sh` from `hooks.PreToolUse` in `~/.claude/settings.json` (leave the `rewrite-cd-git.sh` `Bash` block in place; optionally delete `~/.claude/hooks/guard-cross-checkout-mutation.sh`); takes effect next session start. With the hook gone, cross-checkout safety falls back to prose discipline — today's behavior.

#### Enforcement layer: the empty-diff PR-create guard

The 2026-06-30 substrate audit's second root cause — *auto-drive trusts self-reported STRINGS as facts* — has an even blunter shape than a fabricated verdict: a chip opens a `--draft` PR from a branch with **no changes at all**, and with no barrier the auto-lane merges an **empty scaffold** (#2846, #3122). The B2 fact-gate (`tools/meta-verdict-check`, wired into the merge rule) already refuses to merge without a PR-body review artifact; this hook stops the empty PR from being *created* in the first place. The deterministic complement is a PreToolUse(Bash) hook, version-controlled at [`tools/hooks/guard-empty-pr.sh`](../tools/hooks/guard-empty-pr.sh), that **blocks** (emits `permissionDecision: "deny"`) a `gh pr create` whose head branch has an **empty diff vs its base**:

> `gh pr create --fill` on a branch with no changes vs `origin/main`  →  **denied** · `gh pr create --fill` with real commits  →  **allowed**

The head is `HEAD` (the current branch, how `gh pr create` with no `--head` picks it); the base is `--base` if given, else the remote default branch (`origin/HEAD`); the diff is three-dot (`base...HEAD` = the fileset the PR would show), computed with `git -C <cwd>` in the session's own working directory (the payload `cwd`). Nothing is Evolve-specific — it works in any repo (CalGraph included).

**Fail-safe contract — the worst case is today's behavior (the normal prompt/allow), NEVER a false block of a real PR.** It blocks **only** when it can positively compute (a) a base ref that exists and is non-empty **and** (b) an **empty** `git diff <base>...HEAD` (git exited 0 = no changes). On **any** uncertainty it emits **nothing** and the command proceeds unchanged: not a `gh pr create`; a leading/embedded `cd` (would change the cwd our git runs in); `--repo`/`-R` (a different repo — local git state may not match); `--head`/`-H` (an explicit head ref — `HEAD` is not the compared branch); a cross-fork `--base owner:branch`; a detached `HEAD`; a base branch that resolves nowhere (neither `origin/<base>` nor local `<base>`); a **non-empty** diff (real changes — the whole point is to never block these); or any git / parse / `jq` / `python3` error or timeout. It **never** blocks a create that carries real changes. Regression-guarded by [`tools/hooks/test_guard_empty_pr.sh`](../tools/hooks/test_guard_empty_pr.sh) (`bash tools/hooks/test_guard_empty_pr.sh`), which stands up a real git clone and pins **both** directions — a genuinely-empty branch is blocked, a branch with real changes passes — plus the full uncertainty-passthrough surface. Because the hook intercepts *every* Bash command, run that test after any edit to the hook.

**Install (operator, per machine).** Copy the hook into place and register it as a **user-level** PreToolUse matcher (merge into any existing `hooks.PreToolUse` array — the `cd`&&`git` and cross-checkout `Bash` matchers already live there; **add this as a further `Bash` entry, all run**):

```bash
mkdir -p ~/.claude/hooks
cp tools/hooks/guard-empty-pr.sh ~/.claude/hooks/guard-empty-pr.sh
chmod +x ~/.claude/hooks/guard-empty-pr.sh
```

```json
{ "matcher": "Bash",
  "hooks": [ { "type": "command", "command": "~/.claude/hooks/guard-empty-pr.sh", "timeout": 10 } ] }
```

User-level (not a project's `settings.local.json`) so it also runs in **scheduled / headless** sweeps that create PRs — precisely where an empty scaffold is unattended. Takes effect on the **next session start**. Deps: `jq`, `git`, `python3` (already required by the pod). If `~` isn't expanded in the `command` on your setup, use the absolute home path.

**Disable.** Remove the `"matcher": "Bash"` block pointing at `guard-empty-pr.sh` from `hooks.PreToolUse` in `~/.claude/settings.json` (leave the other `Bash` blocks in place; optionally delete `~/.claude/hooks/guard-empty-pr.sh`); takes effect next session start. With the hook gone, empty-PR safety falls back to the B2 merge-gate (`tools/meta-verdict-check`) alone — an empty PR could still be *created*, just not auto-*merged*.

#### Enforcement layer: the `[META:<id>]` chip-title prefix hook

Every chip / subagent a META coordinator spawns is supposed to carry a `[META:<id>] ` title prefix (so the fleet watcher, `/queue`, and `gh pr list` can attribute work to an aspect). Prose in the skills can ask the model to do this, but a model drifts and forgets. The deterministic complement is a second PreToolUse hook, version-controlled at [`tools/hooks/prepend-meta-prefix.sh`](../tools/hooks/prepend-meta-prefix.sh), that prepends the prefix to the spawn's display title **when, and only when, the spawning session is a known active META aspect**:

> `Agent.description` / `spawn_task.title`  →  `[META:<id>] <original>`  (only if it lacks a `[META:` prefix)

The two spawn surfaces are the built-in **`Agent`** tool (display field `tool_input.description`) and the **`mcp__ccd_session__spawn_task`** chip tool (display field `tool_input.title`); the hook registers as a PreToolUse matcher on both.

**Active-aspect marker (how the hook knows the aspect — cwd-keyed, no session-id dependency).** The hook does not guess the aspect; it reads it from an operator-local marker file that the `/meta` and `/design` skills write at bootstrap (once the id is resolved) and `/close` clears:

- **Path:** `~/.claude/meta-state/active-aspect/<key>`, where `<key>` is the sha256 of the *canonical* (symlink-resolved) absolute working directory. The PreToolUse payload's `cwd` field and the skill's `$PWD` hash to the same key for the same directory.
- **Content:** the aspect id on one line (strict kebab `^[a-z0-9][a-z0-9-]{0,30}$`).
- **Writer/reader/cleanup** all go through the shared helper [`tools/hooks/meta-active-aspect.sh`](../tools/hooks/meta-active-aspect.sh) (`write <id>` / `read` / `clear` / `key`), so the key algorithm is defined once; the hook inlines the identical computation and is otherwise self-contained (it never sources the helper at runtime, so a missing helper can't break the interceptor).

**Fail-safe contract — the worst case is today's behavior (an un-prefixed title), NEVER an altered spawn.** The hook emits `hookSpecificOutput.updatedInput` **alone — with no `permissionDecision`** — so the (re-titled) spawn proceeds through the *normal* permission/approval flow; the hook only edits the title and **never auto-approves a spawn** as a side effect. `updatedInput` replaces the whole input, so the hook echoes every other field (`prompt`, `subagent_type`, `model`, `tldr`, `cwd`, …) back unchanged and changes only the one title field. It emits **nothing** (passthrough, exit 0) whenever: no marker exists for this cwd (non-META sessions are never touched), the title already starts with `[META:` (idempotent — never double-prefixes), the marker content is not a valid kebab id (junk / whitespace / shell-metacharacters are rejected, never spliced into a title), the display field is missing/empty, the cwd is missing, the tool is not one of the two surfaces, or any `jq` / `shasum` / parse error occurs. Regression-guarded by [`tools/hooks/test_prepend_meta_prefix.sh`](../tools/hooks/test_prepend_meta_prefix.sh) (`bash tools/hooks/test_prepend_meta_prefix.sh`) — because the hook intercepts *every* `Agent` + `spawn_task` call, run that test after any edit to the hook or the marker helper.

**Install (operator, per machine).** Copy both scripts into place and register the hook as a **user-level** PreToolUse matcher on each surface:

```bash
mkdir -p ~/.claude/hooks
cp tools/hooks/prepend-meta-prefix.sh ~/.claude/hooks/prepend-meta-prefix.sh
cp tools/hooks/meta-active-aspect.sh ~/.claude/hooks/meta-active-aspect.sh
chmod +x ~/.claude/hooks/prepend-meta-prefix.sh ~/.claude/hooks/meta-active-aspect.sh
```

Then merge into the **user-level** `~/.claude/settings.json` `hooks.PreToolUse` array (do not clobber other matchers — there is already an `Agent` matcher for the background-agent guardrail; both run):

```json
{ "matcher": "Agent",
  "hooks": [ { "type": "command", "command": "~/.claude/hooks/prepend-meta-prefix.sh", "timeout": 10 } ] },
{ "matcher": "mcp__ccd_session__spawn_task",
  "hooks": [ { "type": "command", "command": "~/.claude/hooks/prepend-meta-prefix.sh", "timeout": 10 } ] }
```

User-level (not a project's `settings.local.json`) so it also runs in **scheduled / headless** sweeps that spawn chips. Takes effect on the **next session start**. Deps: `jq` and `shasum` (or `sha256sum`). If `~` isn't expanded in the `command` on your setup, use the absolute home path (e.g. `/Users/<you>/.claude/hooks/prepend-meta-prefix.sh`).

**Disable.** Remove the `"matcher": "Agent"` block that points at `prepend-meta-prefix.sh` **and** the `"matcher": "mcp__ccd_session__spawn_task"` block from `hooks.PreToolUse` in `~/.claude/settings.json` (leave the background-agent-guardrail `Agent` block in place; optionally delete the two `~/.claude/hooks/*.sh` scripts); takes effect next session start. With the hook gone, chip titles fall back to prose-discipline prefixing.

#### Enforcement layer: the global `~/.claude/skills` auto-sync hook

The operator-global `~/.claude/skills/<n>/SKILL.md` mirror is the copy that loads across **every** branch / worktree and **every** Claude Code account (step 1). Its only refresh path used to be the manual `git show origin/main:… > ~/.claude/skills/…` loop in step 1 — with **no auto-sync**, so it silently drifts when that loop is forgotten. **Proven failure (2026-06-22):** the mirror was stale since Jun 14, so the merged `[META:<id>]` prefix hook (above) was a **silent no-op for 8 days** — the marker-write step it depends on lived only in the stale `/meta` `/design` `/close` skills. A stale mirror is insidious: everything *looks* installed. The deterministic complement (same shape as the cd&&git and prefix hooks) is a tool + a SessionStart hook that re-runs the equivalent of the step-1 loop on every session start.

> **What it does.** [`tools/meta-skills-sync`](../tools/meta-skills-sync) overwrites each `~/.claude/skills/<n>/SKILL.md` from `origin/main:.claude/skills/<n>/SKILL.md` **iff the bytes differ** (byte-exact, idempotent), reading the **locally-known** `origin/main` ref — it **never fetches** (no network on session start). It is **additive/overwrite-only**: it **never deletes** a skill present in the mirror but absent on `origin/main` (could be a local-only / operator skill). A **HEAD-unchanged fast path** (last-synced ref cached at `~/.claude/meta-state/skills-sync-last-ref`) makes the common case ~one `git rev-parse`. The hook [`tools/hooks/meta-skills-sync-on-start.sh`](../tools/hooks/meta-skills-sync-on-start.sh) just locates and runs the tool.

**Semantics.** Skills load at session *start*, so a refresh here takes effect on the **next** session — identical to the manual loop ("restart to pick up a change"). The win is that the mirror self-heals every session instead of drifting for days.

**Fail-safe contract — the worst case is today's behavior (a possibly-stale mirror), never a blocked / delayed / altered session start.** The hook runs `set -euo pipefail` under a `trap 'exit 0' EXIT`, so **any** error exits 0. It **emits nothing on stdout** — a SessionStart hook's stdout is *injected into the model context*, so the tool's summary is swallowed and the hook is silent by construction. SessionStart **cannot block** the session (its exit code is ignored), but the session **waits synchronously** for the hook, so the fast path + the `timeout` keep it cheap (no network, bounded time). It **no-ops silently outside an Evolve checkout** (the tool self-validates: no usable repo / no resolvable `origin/main` ⇒ exit 0). The tool **only overwrites-from-origin or creates** mirror files and **never deletes**, so a logic error's worst case is a mirror file matching `origin/main` — there is no data-loss path. Regression-guarded by [`tools/hooks/test_meta_skills_sync.sh`](../tools/hooks/test_meta_skills_sync.sh) (`bash tools/hooks/test_meta_skills_sync.sh`) — run it after any edit to the tool or the hook.

**Install (operator, per machine) — gated (global blast radius).** This is a SessionStart hook that runs at the start of **every** session on the machine (all accounts), so enable it deliberately. Copy both scripts into place and register the hook as a **user-level** SessionStart matcher:

```bash
mkdir -p ~/.claude/hooks
cp tools/hooks/meta-skills-sync-on-start.sh ~/.claude/hooks/meta-skills-sync-on-start.sh
cp tools/meta-skills-sync                  ~/.claude/hooks/meta-skills-sync
chmod +x ~/.claude/hooks/meta-skills-sync-on-start.sh ~/.claude/hooks/meta-skills-sync
```

(The operator-local `~/.claude/hooks/meta-skills-sync` is the always-present fallback; the hook prefers a checkout's fresher `tools/meta-skills-sync` when the session is in/near one.) Then merge into the **user-level** `~/.claude/settings.json` — add a `hooks.SessionStart` array if absent (do not disturb existing `hooks.PreToolUse` entries):

```json
"SessionStart": [
  { "hooks": [ { "type": "command", "command": "~/.claude/hooks/meta-skills-sync-on-start.sh", "timeout": 10 } ] }
]
```

No `matcher` ⇒ runs on every source (`startup` / `resume` / `clear` / `compact`). User-level (not a project's `settings.local.json`) so it also runs in scheduled / headless sessions. Takes effect on the **next** session start. Deps: `git` (the tool) and, in the hook, `jq` for cwd extraction (degrades to `$PWD` without it). If `~` isn't expanded in the `command` on your setup, use the absolute home path. Verify with a dry-run: `~/.claude/hooks/meta-skills-sync --repo ~/GitHub/evolve --dry-run`.

**Disable.** Remove the `hooks.SessionStart` block that points at `meta-skills-sync-on-start.sh` from `~/.claude/settings.json` (optionally delete the two `~/.claude/hooks/meta-skills-sync*` copies); takes effect next session start. With the hook gone, the mirror falls back to the manual step-1 loop.

**(c) Discipline — belt-and-suspenders.** The auto-sync covers the *forgotten* case at next-session granularity. The discipline still holds: **any chip that ships a `.claude/skills/` or `tools/hooks/` change must refresh the mirror post-merge** — run `tools/meta-skills-sync --force` (or the step-1 manual loop) so the change is live **immediately**, not one session later. The hook is the safety net; the post-merge refresh is the fast path.

#### Enforcement layer: the `[META:<id>]` marker self-registration hook (propagate the prefix down the spawn tree)

The prefix hook above only prefixes spawns from a session that has **written an active-aspect marker** for its cwd — and **only `/meta` and `/design` write one**. That covers *coordinators*, not the chips they spawn: a chip session never runs `/meta`, so it never registers, so the sub-agents / sub-chips the *chip* spawns are **un-prefixed** and the `[META:<id>]` lineage goes dark one level below the coordinator. **Proven failure (2026-06-22):** the prefix landed on only ~2 of ~40 live sessions — exactly the few that had run `/meta`/`/design`.

The deterministic complement is a SessionStart hook, version-controlled at [`tools/hooks/meta-marker-register-on-start.sh`](../tools/hooks/meta-marker-register-on-start.sh), that makes **any session whose title carries the tag self-register its own marker** — the *same* marker file, key, and reader as the `/meta`/`/design` writer. Because the prefix hook titles a spawned chip `[META:<id>] …`, that chip self-registers on startup, so *its* spawns get prefixed too, which titles *its* children with the tag, and so on — the tag flows down the whole tree (coordinator → chip → grandchild → …).

> **What it does.** On session start it reads `session_title` + `cwd` from the payload and, if the title matches **`[META:<id>] …`** (chip/child) or **`META <id>`** (coordinator — the title `/meta` forces), writes the active-aspect marker for that cwd. `<id>` is validated with the **exact** strict-kebab regex from the prefix hook (`^[a-z0-9][a-z0-9-]{0,30}$`); a non-matching/invalid title (`Fix the widget`, `[META:Bad Id]`, `[META:../etc]`, `META not a kebab`) writes **nothing**. The cwd→key computation is **byte-for-byte** the prefix hook's, so the marker it writes is the one the prefix hook reads.

**Semantics & a known limitation.** Fires at session *start*, so already-open sessions need a **restart** to begin self-registering. The marker is keyed by canonical cwd, so two sessions in the *same* cwd share one marker (**last-writer-wins**) — acceptable because chips run in their own worktrees (unique cwd); documented, not solved here. Cleanup stays `/close`'s job — this hook **never deletes** a marker.

**Fail-safe contract — the worst case is today's behavior (a chip's spawns stay un-prefixed), never a blocked / delayed / altered session start.** The hook runs `set -euo pipefail` under a `trap 'exit 0' EXIT`, so **any** error exits 0. It **emits nothing on stdout** — a SessionStart hook's stdout is *injected into the model context*, so this hook is silent by construction. No network; bounded time (a couple of `jq`/`shasum` calls + one small atomic `mv`); most sessions hit the no-match branch and exit at once. Regression-guarded by [`tools/hooks/test_meta_marker_register.sh`](../tools/hooks/test_meta_marker_register.sh) (`bash tools/hooks/test_meta_marker_register.sh`) — its end-to-end case writes via this hook and proves `prepend-meta-prefix.sh` then prefixes a spawn from the same cwd.

**Install (operator, per machine) — gated (global blast radius).** This is a SessionStart hook that runs at the start of **every** session on the machine (all accounts), so enable it deliberately. Copy the script into place:

```bash
mkdir -p ~/.claude/hooks
cp tools/hooks/meta-marker-register-on-start.sh ~/.claude/hooks/meta-marker-register-on-start.sh
chmod +x ~/.claude/hooks/meta-marker-register-on-start.sh
```

Then merge into the **user-level** `~/.claude/settings.json` — add a **second, additive** entry to the `hooks.SessionStart` array (alongside the `meta-skills-sync-on-start.sh` entry; do not disturb existing `hooks.PreToolUse` entries):

```json
"SessionStart": [
  { "hooks": [ { "type": "command", "command": "~/.claude/hooks/meta-skills-sync-on-start.sh",     "timeout": 10 } ] },
  { "hooks": [ { "type": "command", "command": "~/.claude/hooks/meta-marker-register-on-start.sh", "timeout": 10 } ] }
]
```

No `matcher` ⇒ runs on every source (`startup` / `resume` / `clear` / `compact`). User-level (not a project's `settings.local.json`) so it also runs in scheduled / headless sessions that spawn chips. Takes effect on the **next** session start. Deps: `jq` and `shasum` (or `sha256sum`). If `~` isn't expanded in the `command` on your setup, use the absolute home path.

**Disable.** Remove the `hooks.SessionStart` block that points at `meta-marker-register-on-start.sh` from `~/.claude/settings.json` (leave the `meta-skills-sync-on-start.sh` block in place; optionally delete the `~/.claude/hooks/meta-marker-register-on-start.sh` copy); takes effect next session start. With the hook gone, marker-writing falls back to the `/meta`/`/design` skills (coordinators only) — i.e. today's behavior, where the prefix stops one level below the coordinator.

#### Enforcement layer: the dispatch-claim layer (make just-dispatched work visible)

`tools/meta-inflight` (the dispatch-time "who's already on this?" check) scans ledgers + open PRs, but a **just-dispatched** chip has *neither* — no PR yet, and nothing in a ledger until a coordinator writes it — so a second dispatch of the same work can't see the first. **Proven failure (2026-07-01, operator-corroborated):** sessions still landed redundant work despite `meta-inflight`, because that mid-flight window is invisible, the "run meta-inflight before you spawn" instruction is advisory prose the model forgets, and it dies one level below the coordinator. The deterministic complement (same shape as the other hooks) is a third PreToolUse hook, version-controlled at [`tools/hooks/record-dispatch-claim.sh`](../tools/hooks/record-dispatch-claim.sh), that **records a claim on every spawn** so the window closes:

> On every `Agent` / `mcp__ccd_session__spawn_task` spawn from a session with an active-aspect marker, write `~/.claude/meta-state/claims/<claim-id>.json` = `{aspect, title, time, ttl_seconds, …}`.

`meta-inflight` reads that registry as a **fourth signal** (alongside ledger chips / PRs / sessions), so a no-PR-yet sibling now surfaces as an overlap. Because the prefix hook titles a spawned chip `[META:<id>] …` and `meta-marker-register-on-start.sh` self-registers that chip's marker, **claims propagate down the whole spawn tree** (coordinator → chip → grandchild) — closing the "dies one level down" gap. The claim carries `{aspect, title, time, ttl_seconds}` (schema + registry doc: [`docs/meta-ledger-schema.md`](meta-ledger-schema.md) → "The dispatch-claim registry").

**Operator policy: record-always + WARN, never block** (fuzzy title overlap must not block legit parallel work). The "warn" is surfaced by `meta-inflight`, not the hook.

**Fail-safe contract — the worst case is today's behavior (no claim recorded), NEVER an altered / blocked / delayed spawn.** The hook runs `set -euo pipefail` under `trap 'exit 0' EXIT`, so **any** error exits 0. It **performs a side-effect only** (write the claim) and **emits nothing on stdout** — no `hookSpecificOutput`, no `updatedInput`, no `permissionDecision` — so the spawn proceeds through the *normal* permission/approval flow, completely unchanged (unlike `prepend-meta-prefix.sh`, which rewrites the title; this hook never touches the input). It records **nothing** whenever: no marker exists for this cwd (non-META sessions are never claimed), the marker content is not a valid kebab id (junk / shell-metacharacters rejected, never recorded), the display field is missing/empty/non-string, the cwd is missing, the tool is not one of the two surfaces, or any `jq` / `shasum` / write error occurs. Claims carry a TTL (default 4h, `$META_CLAIM_TTL_SECONDS`); `meta-inflight` ignores AND prunes expired ones on read (or `meta-inflight --prune`), so a dead chip's claim self-expires. Regression-guarded by [`tools/hooks/test_record_dispatch_claim.sh`](../tools/hooks/test_record_dispatch_claim.sh) (`bash tools/hooks/test_record_dispatch_claim.sh`) — because the hook intercepts *every* `Agent` + `spawn_task` call, run it after any edit to the hook or the marker helper.

**Install (operator, per machine).** Copy the hook into place and register it as a **user-level** PreToolUse matcher on each surface (alongside the `prepend-meta-prefix.sh` entries — both run on the same tools; do not clobber other matchers):

```bash
mkdir -p ~/.claude/hooks
cp tools/hooks/record-dispatch-claim.sh ~/.claude/hooks/record-dispatch-claim.sh
chmod +x ~/.claude/hooks/record-dispatch-claim.sh
```

Then merge into the **user-level** `~/.claude/settings.json` `hooks.PreToolUse` array:

```json
{ "matcher": "Agent",
  "hooks": [ { "type": "command", "command": "~/.claude/hooks/record-dispatch-claim.sh", "timeout": 10 } ] },
{ "matcher": "mcp__ccd_session__spawn_task",
  "hooks": [ { "type": "command", "command": "~/.claude/hooks/record-dispatch-claim.sh", "timeout": 10 } ] }
```

Multiple hooks on the same matcher all run — this composes with the `prepend-meta-prefix.sh` and background-agent-guardrail `Agent` matchers. User-level (not a project's `settings.local.json`) so it also runs in **scheduled / headless** sweeps that spawn chips. Takes effect on the **next session start**. Deps: `jq` and `shasum` (or `sha256sum`) and `date`. If `~` isn't expanded in the `command` on your setup, use the absolute home path.

**Disable.** Remove the two matcher blocks that point at `record-dispatch-claim.sh` from `hooks.PreToolUse` in `~/.claude/settings.json` (leave the `prepend-meta-prefix.sh` blocks in place; optionally delete `~/.claude/hooks/record-dispatch-claim.sh`); takes effect next session start. With the hook gone, `meta-inflight` still runs — it just loses the no-PR-yet claims signal and falls back to ledgers + PRs + sessions (today's behavior).

### `meta-fleet-watch` — hourly, edge-triggered status poke

```markdown
---
name: meta-fleet-watch
description: Hourly edge-triggered status watcher over in-flight META/chip PRs; pokes the operator only on state changes (ready/merged/blocked/stalled)
---

You are META-FLEET-WATCH: a cheap, fast, edge-triggered status watcher for the dev "fleet" of META-coordinated work (chips/PRs spawned by META coordinator sessions). Your ONLY job is to detect when in-flight work changes state and poke the operator on transitions. Do the MINIMUM — a handful of deterministic tool calls. Do NOT design, review, merge, comment, spawn, or modify anything. Observe and notify only. A silent run (no notification) is the normal, correct outcome.

REPO: the `repo_slug` in the dev checkout's `.claude/meta.json` — Read that file first and use its value as `<REPO>` wherever `--repo` appears below; fall back to `cjalden/evolve` when the file is absent or malformed. (Resolve by reading the JSON with the Read tool, not by shelling out.)
SCOPE: pull requests whose head branch starts with "claude/" (the dispatched-work naming convention). Ignore all other PRs.

STATE FILE (for edge-triggering): ~/.claude/meta-watch/last-seen.json
- A JSON object: PR-number keys -> last-seen bucket (e.g. {"2802":"merged","2799":"cooking"}), plus an optional "__collisions__" key holding the list of already-reported collision signatures ("<loPR>+<hiPR>:<file>"). For the baseline check, "no PR entries" means no PR-number keys (ignore "__collisions__").
- Run `mkdir -p ~/.claude/meta-watch` first if the dir is missing.
- BASELINE CASE: if the file is missing, empty, or has no PR entries (e.g. "{}"), establish the baseline by writing the current state and EXIT SILENTLY — do NOT poke. (So deleting the file is a quiet reset, never a noise burst.)

STEPS (keep to ~4-6 tool calls total):
1. Current open PRs:
   gh pr list --repo <REPO> --state open --limit 50 --json number,title,headRefName,isDraft,statusCheckRollup,updatedAt
   Keep only those whose headRefName starts with "claude/".
2. Bucket each kept PR:
   - BLOCKED: any check in statusCheckRollup has conclusion FAILURE/ERROR/CANCELLED/TIMED_OUT.
   - READY: not draft AND every check is SUCCESS/NEUTRAL/SKIPPED, or there are no checks at all (actionable / mergeable).
   - COOKING: has any check still PENDING/IN_PROGRESS/QUEUED, or the PR is a draft.
   - STALLED: bucket is COOKING but updatedAt is more than 30 minutes ago (proxy for a dead chip; matches the guide's "no commits in ~30 min" heuristic).
3. Detect closures: for each PR number in last-seen.json that is NOT in the current open list, it closed — run `gh pr view <n> --repo <REPO> --json state,mergedAt,title` and bucket it MERGED (has mergedAt) or CLOSED.
4. Compute STATE EDGES = PRs whose bucket CHANGED vs last-seen, keeping only meaningful transitions: -> READY (actionable/mergeable), -> BLOCKED (a check failed), -> MERGED (landed), -> STALLED (newly stalled). Ignore unchanged buckets and COOKING -> COOKING.
5. COLLISION CHECK (only if 2+ open `claude/*` PRs; cap at 8 PRs to stay cheap): for each open `claude/*` PR get its changed files — `gh pr view <n> --repo <REPO> --json files`. Any two PRs sharing a changed file are a COLLISION (a coming merge conflict / contradictory cross-META change). It's a NEW edge only if its signature ("<loPR>+<hiPR>:<file>") is not already in "__collisions__".
6. If there are ANY new edges (state OR collision), send ONE PushNotification (status "proactive"), one line, under ~200 chars, actionable items first. Examples:
   "Fleet: READY #2802 coalesce; MERGED #2799 naming; STALLED #2810 (40m)"
   "Fleet COLLISION: #2811 & #2814 both touch model_pricing.py — coordinate"
   If many, summarize counts + the top few. NO new edges → poke nothing.
7. Write state back to ~/.claude/meta-watch/last-seen.json: the full PR->bucket map (open + just-merged/closed this run) AND "__collisions__" updated with every collision signature reported (so each is poked once). Prune PR entries both closed AND already reported, and collision signatures whose PRs are no longer both open.

RULES:
- Edge-triggered: NEVER poke for an unchanged state. Silence = nothing changed = success.
- Report-only: never merge/comment/spawn/modify. If something looks broken, report it in the poke; do not act on it.
- Cheap: no exploration, no reasoning beyond the bucket logic; ~4-6 tool calls. This task is meant to be near-free.
```

### `meta-loose-ends` — daily dangling-work sweep

```markdown
---
name: meta-loose-ends
description: Daily sweep for dangling dev work across the META fleet — orphaned branches, languishing PRs, and dropped intent (lost questions, specced-not-built); report-only digest
---

You are META-LOOSE-ENDS: a daily sweep that finds DANGLING dev work across the META fleet so nothing silently rots. Report-only — never merge, spawn, or modify anything. Produce ONE digest; if everything is clean, say so in one line.

Work in the dev clone (run `git fetch origin main -q` first). REPO: read the `repo_slug` from the dev checkout's `.claude/meta.json` (Read tool, not a shell call) and use it as `<REPO>` below; fall back to `cjalden/evolve` when absent. The aspects + their memory tags are the registry in `docs/META-aspect-registry.md`. Memory dir: ~/.claude/projects/<project-slug>/memory/.

Find these dangling-work patterns:

A. ORPHANED BRANCHES (work done, never PR'd):
   - list `claude/*` remote branches: `git ls-remote --heads origin 'claude/*'`
   - cross-ref: `gh pr list --repo <REPO> --state all --limit 200 --json number,headRefName,state`
   - FLAG any branch that has commits ahead of origin/main AND has NO PR in any state. Note its last-commit age (`git log -1 --format=%cr origin/<branch>`).

B. LANGUISHING PRs:
   - `gh pr list --repo <REPO> --state open --limit 50 --json number,title,headRefName,isDraft,updatedAt`
   - FLAG open `claude/*` PRs with no update in > 3 days, and drafts open > 3 days.

C. DROPPED INTENT (from the in-flight ledgers in memory):
   - read `MEMORY.md` and each aspect's ledger memory file.
   - surface ledger items that look stuck: OPEN QUESTIONS / undecided decisions (especially with an old date); PLANNED-NOT-STARTED bites (decided, never chipped); SPECCED-NOT-BUILT (a written/merged spec with no implementation).
   - cross-check against git where you can (did a PR for it actually land?). Flag the ones with no movement.

OUTPUT: ONE digest grouped by aspect — each line `<aspect> — <pattern> — <item> — <age/since>`. Lead with a one-line count, e.g. "Loose ends: 2 orphaned branches, 1 languishing PR, 3 dropped-intent items". Send ONE PushNotification (status "proactive") with the count + the top few items; put the full digest in your run output. If nothing dangles, send no notification (or one terse "fleet clean") — do not nag.

RULES: report-only; thorough but bounded (this is daily, not hourly); read-only toward the live pod (never touch it); never modify branches/PRs/memory. You SURFACE dangling work — the operator or the relevant META acts on it.
```

### `shipped-digest` — daily plain-language "what shipped to your pod"

Translates the day's merged changes into a brief, high-level, non-technical summary for the operator. The value is the *translation* — strip jargon, surface outcomes, skip internal/CI/process churn.

```markdown
---
name: shipped-digest
description: Daily plain-language "what shipped to your pod" digest — translates the day's merged changes into a brief, high-level, non-technical summary of what's new for your bots
---

You are SHIPPED-DIGEST: a daily, plain-language summary of what actually shipped to the operator's pod, written for a NON-TECHNICAL reader. Translate the day's merged engineering changes into "what's new for you and your bots." Report-only — never merge or modify anything.

REPO: read the `repo_slug` from the dev checkout's `.claude/meta.json` (Read tool, not a shell call) and use it as `<REPO>` below; fall back to `cjalden/evolve` when absent. Work in the dev clone; run `git fetch origin main -q` first.

GATHER:
- Merged PRs to main since the last run (default: the last 24h):
  `gh pr list --repo <REPO> --state merged --limit 120 --json number,title,mergedAt`
  Keep those merged since ~yesterday.

TRANSLATE (this is the whole job — do it well):
- Group the USER-FACING changes into AT MOST ~5–6 themes. A theme = an outcome the operator / bot-owner would actually notice: cost, speed, reliability, alerts, model choice, plugins, new-bot setup, message delivery.
- Write each as ONE short plain-language sentence about what it MEANS for them — not what the code did.
  - GOOD: "Updates go live faster — the wait before a new version reaches your bots dropped from about an hour to about 20 minutes."
  - BAD: "Reduced soak_minutes 60→20 and risk-tiered the canary." (jargon + internal)
- NO PR numbers, NO file names, NO internal jargon (soak, canary, ratchet, baseline, signal, generator, refactor, schema).
- SKIP entirely — these are NOT "what shipped to the pod": CI, refactors, lint/baseline housekeeping, docs, the dev-process / META coordination work, test-only changes.
- If a day is all-internal, say so in one line ("Mostly behind-the-scenes cleanup today — nothing user-facing.") — never manufacture themes to fill space.

STYLE: very brief, very high-level, plain English. A non-engineer should understand every line. Lead with a one-line headline (e.g. "What shipped today — 5 things"), then the ≤6 bullets, then a one-line "behind the scenes" tally.

OUTPUT: put the full digest in your run output AND send ONE PushNotification (status "proactive") with the headline + the top 2–3 items. If nothing user-facing shipped, send no notification (or one terse "nothing user-facing today").

RULES: report-only; read-only toward the live pod (never touch it); cheap on tool calls but spend your reasoning on the TRANSLATION — that plain-language quality is the entire value of this task.
```
