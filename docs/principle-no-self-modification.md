# Principle: `security_rules.json` Is Not Modifiable by the Proposal Pipeline

**Status:** load-bearing security principle (not a soft guideline).
**Adopted:** 2026-05-31, consolidating the rule already enforced in [architecture.md](architecture.md) §"Security Model" (the `no_self_modification` auto-reject rule).

---

## The principle, in two clauses

1. **`/Users/Shared/evolve/security_rules.json` is the enforcement ground truth, and proposals cannot modify it.** The reviewer that gates every proposal reads its mandate from this file. A proposal that could alter its own gating rules would defeat the entire pipeline. The `no_self_modification` rule in the reviewer rejects any proposal that touches `security_rules.json` outright, before any further evaluation.

2. **The same logic extends to the reviewer's own code and to evolve's own scripts.** A proposal that could modify `review.py` or other evolve infrastructure would create the same loophole as modifying the rules file. The auto-reject rule "no modification of evolve's own scripts" enforces this at the code level. Changes to security rules and to evolve infrastructure happen out-of-band, by a human editing the file or merging a PR — never through the proposal pipeline.

## What this implies in code

Practical translation across the codebase:

### The auto-reject list is the inner perimeter

The reviewer enforces eight auto-reject rules ([architecture.md:212-220](architecture.md:212)):

- No `0.0.0.0` gateway binding
- No auth disable
- No modification of evolve's own scripts
- No credential / auth-file writes
- No `sudo` in proposed scripts
- No outbound network calls in proposed scripts
- No writes outside the bot's own workspace
- No `launchd` plist modifications

These are not negotiable from inside the pipeline. A generator that wants to do any of these things must surface as a request to the human, not as a proposal that could be auto-applied.

### Four auto-flag rules pass to human with warning

For changes that *could* be principled but are inherently sensitive (model swaps, cost-threshold changes, channel config changes, low-confidence proposals), the reviewer flags rather than rejects. The principle preserved is that a human sees the change before it lands — the auto-flag rules don't bypass review, they just route differently.

### The security bot can be physically separated

The `security.mode = "dedicated"` option runs `review.py` on a separate bot account (typically `security-bot`). The same code runs either way; the separation is about blast radius — if the primary bot is compromised, the security reviewer keeps running on an isolated account. The principle is preserved by either mode; dedicated mode hardens it further ([configuration.md](configuration.md) §"security").

### Changes to security rules are PRs, not proposals

When the security rules need to evolve (new auto-reject, refined threshold, new auto-flag category), the change is a human-authored PR to `security_rules.json`. The pipeline never edits the file. This makes every change to enforcement attributable, reviewable, and reversible at the git level.

## Anti-patterns to grep for

These are violations:

- A proposal-pipeline action class that writes to `/Users/Shared/evolve/security_rules.json`
- A generator that emits proposals targeting `review.py` or any evolve-admin Python file
- An applier that touches `evolve_admin/` or `packages/analyzer/` source files
- A "self-tuning security rules" generator (the path to self-compromise)
- An auto-flag rule that is implemented as "log a warning and proceed" instead of "block until human acks"

## What this principle is NOT

- **Not a ban on tunable security.** Security configuration the operator wants to expose (per-bot exec policies, daily-cap thresholds, audit cadence) can be controlled through normal config flows. The principle is specifically about the reviewer's mandate and the reviewer's code.
- **Not a claim that the file is tamper-proof at the OS level.** A user with root on the mini can edit the file directly; the principle is about the *proposal pipeline*, which is the surface that auto-applies changes. OS-level integrity is a separate concern handled by Time Machine + GitHub backup.
- **Not a freeze on the rules.** The rules evolve regularly via PR. The principle is about *who* edits them (humans via git), not whether they change.

## Why this matters

Every self-modifying system has the same failure mode: a bug in the self-modification path can rewrite the safety rails. Auto-applied proposals are powerful; the cost of that power is a rule that proposals cannot rewrite the rules. Without `no_self_modification`, a single misbehaving generator could propose "disable all auto-reject rules" and, if it bypassed human review for any reason (auto-approve threshold, calibration window, a bug), the pipeline would unbolt itself.

The principle is the cheapest possible defense — it costs one rule, enforced at the very front of the pipeline — and it absorbs the entire class of "the system improves itself into a state where nothing checks it anymore."

## References

- [architecture.md](architecture.md) §"Security Model" (lines 206-224) — the canonical statement
- [configuration.md](configuration.md) §"security" — the `rulesFile` field that the principle protects
- `packages/admin/evolve_admin/review.py` (and the dedicated security-bot deployment) — the enforcing code
- [principle-each-bot-applies-its-own-changes.md](principle-each-bot-applies-its-own-changes.md) — sibling principle on application boundaries
