# Principle: Each Bot Applies Its Own Changes (No Cross-User Writes)

> **SUPERSEDED 2026-08-18 — clause 1 is no longer true, and never was in
> practice.** The mechanism this principle names — `analyzer/apply.py`, a
> per-bot daemon that applied approved proposals inside the target bot's own
> user context — has been deleted. It polled `{shared_dir}/proposals/approved/`,
> a directory no arbiter status maps to, so it was structurally unable to see a
> modern proposal and had applied nothing in its entire recorded history. The
> applier that actually runs is `arbiter.apply` on the admin host, as the
> `evolve` service user, writing bot config through narrow sudo grants. See
> [design-proposal-signing-key-2026-08-18.md](design-proposal-signing-key-2026-08-18.md).
>
> **Clause 2 survives and is worth keeping**: cross-user writes are still narrow,
> named and bounded, and the anti-patterns below are still anti-patterns. What
> changed is that "the applier" is now one of the named exceptions rather than
> living outside them. Read this document as a record of an intended boundary
> and the reasons for it, not as a description of the code.
>
> Two factual corrections in the body, marked inline below: the `1777`
> sticky-bit claim does not hold for `proposals/`, and `review.py` was retired
> on 2026-08-14.

**Status:** ~~load-bearing architecture principle~~ — **superseded**; retained
as a design record.
**Adopted:** 2026-05-31. **Superseded:** 2026-08-18.

---

## The principle, in two clauses

1. ~~**Approved proposals are applied by the target bot in its own user context.** When a proposal targets `member-bot`, it is `apply.py` running as `member-bot` (in `/Users/member-bot/...`) that writes the change — not the admin server, not the primary bot, not root. The shared directory at `/Users/Shared/evolve/` is the message bus: the primary writes a proposal there, the target bot polls and picks it up.~~ **Retired 2026-08-18** — the daemon is deleted; `arbiter.apply` runs on the admin host as `evolve`.

2. **There are no cross-user writes during normal operation.** The admin user (`pod-admin`) and root are involved only in initial setup and exceptional interventions (re-installing a missing file, recovering from a broken deploy). Anything that happens day-to-day — config updates, capability changes, fixes — happens inside the target bot's account, signed and approved, then applied locally.

## What this implies in code

Practical translation across the codebase:

### The shared directory is the only cross-user surface

`/Users/Shared/evolve/` is `1777` (sticky bit) so every bot can write its own files but cannot overwrite another bot's files ([architecture.md:140](architecture.md:140)). The primary writes proposals there; the target bot reads them there. All cross-bot communication routes through this shared dir.

> **Correction (2026-08-18):** the sticky-bit guarantee does **not** extend to
> `proposals/`. That subtree is `0777` with the sticky bit *deliberately*
> omitted (`deploy.POD_PROPOSALS_MODE`), so any local account can overwrite or
> delete another's proposal file there. Tracked as diligence backlog 7.1 C —
> see [design-proposals-store-write-boundary-2026-08-18.md](design-proposals-store-write-boundary-2026-08-18.md).

### ~~`apply.py` runs per-bot, polling for proposals targeting it~~ — **retired 2026-08-18**

The intent was: each bot has its own `apply.py` polling
`/Users/Shared/evolve/proposals/approved/`; when a proposal targets that bot,
the bot's own daemon picks it up and writes the change inside
`/Users/<bot>/.openclaw/...` — its own account, no sudo, no cross-user file ops.

That is not what happened. `proposals/approved/` is not a status any arbiter
proposal is ever routed to, so the daemon never saw one, and the deployed fleet's
entire log history for it is a single repeated "No new proposals" line. The
run-as-the-bot write boundary described here was therefore never exercised on a
real change. `arbiter.apply` on the admin host has been doing the applying all
along.

### The pod-admin sudoers grant is for setup, not for normal flow

`/etc/sudoers.d/evolve-admin` grants `pod-admin` the ability to `sudo` as bot users (per CLAUDE.md). That grant exists for CLI use during setup and exceptional recovery — not for the day-to-day pipeline. Daemons running as `evolve` deliberately do NOT have `sudo -u <bot>` (CLAUDE.md is explicit on this); they use ACL reads + `/tmp` staging + `sudo /bin/cp` instead.

### Exceptions are named and bounded

There are narrow exceptions where evolve must write a bot-owned file — initial deploy, applier installing config, recovery flows. Each uses the `/tmp` staging + `sudo /bin/cp` pattern from CLAUDE.md and is treated as exceptional, not normal. The principle bounds these: they exist for installation and recovery, not for runtime improvement flows.

### Evo's privileged ops route through the admin-daemon unix socket, not direct sudo

Per `project_evo_account_separation`, evo (running as its own `evo` macOS user) does not sudo. Privileged operations evo requests go through a unix-socket API exposed by the admin daemon, which itself respects the per-bot-applies pattern (it writes to `/Users/Shared/evolve/` for the target bot to pick up, rather than writing into `/Users/<target>/` directly).

## Anti-patterns to grep for

These are violations:

- A daemon running as `evolve` calling `subprocess.run(["sudo", "-u", "<bot>", ...])` for routine ops (will fail by design; also wrong by principle)
- An applier or generator that writes into `/Users/<other-bot>/.openclaw/` directly
- An admin endpoint that bypasses the proposal pipeline to "just fix" a bot's config
- Code that assumes pod-admin sudo is available at runtime (it's a setup-time grant)
- "Cross-bot" actions that aren't routed through the shared-dir message bus

## What this principle is NOT

- **Not a ban on shared infrastructure.** `/Users/Shared/evolve/` is by design a shared surface for messages, signals, proposals, metrics. The principle is about *who writes into a bot's own directory*, not about whether there's any shared state.
- **Not a claim that setup is also per-bot.** Initial deploy (`evolve-admin deploy <bot>`) is admin-initiated and uses elevated grants to install the bot's first config. Once running, the bot owns its own files. The principle covers normal operation, not bootstrap.
- **Not a substitute for the security model.** Proposals still pass a security screen before they apply — `arbiter/security_screen.py` as a fail-closed leg of `arbiter.routing.is_autonomous_eligible`, since `review.py` was retired 2026-08-14. The principle is about *where* the write happens, not whether it's gated.

## Why this matters

The pattern is what makes the whole pipeline tractable. Every bot's filesystem is its own — there's no "central admin writes everywhere" surface that has to be airtight against every bug in every generator. The blast radius of an applier bug is one bot's config, recoverable from git + the pre-apply test gate. The blast radius of a cross-user-write bug, by contrast, is every bot at once.

It also makes the security model defensible: when proposals can only be applied by the bot they target, a compromised generator can at most produce bad proposals (which the reviewer will catch), not bypass the reviewer to directly write files in someone else's account. The boundary holds at the user-account level, which the OS already enforces.

This is the same logic that motivated the evo-account-separation work — privileged daemons should not be able to silently mutate any bot's state. By keeping all writes inside the target bot, the system's blast-radius math stays simple.

## References

- [architecture.md](architecture.md) §"Bot Autonomy Model" (lines 150-164) — the canonical statement
- [architecture.md](architecture.md) §"Shared directory" (line 140) — the `1777` sticky-bit invariant
- CLAUDE.md §"File Access Pattern" — the practical implication for code interacting with bot-owned files
- `project_evo_account_separation` — the evo-side evolution of the same principle
- [principle-per-bot-inference.md](principle-per-bot-inference.md) — sibling principle: inference also runs per-bot
- [principle-no-self-modification.md](principle-no-self-modification.md) — sibling principle: the reviewer's mandate is unmodifiable
