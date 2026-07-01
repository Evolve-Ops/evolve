---
name: close
description: Cleanly wrap up the current META coordinator working bout — save work in progress (commit/push, update the memory in-flight ledger + spec), then report whether the session is safe to close. Use at the end of a bout. It does NOT terminate the session; it makes it safe for the operator to.
---

The operator wants to end the current META working bout cleanly. Run the **checkpoint ritual** from `docs/META-bootstrap.md` (the small, every-bout bootstrap doc; the full reference is `docs/META-session-guide.md`), then report safe-to-close.

1. **Identify the aspect** this session has been coordinating (how it was launched / what it worked on). If genuinely unclear, ask which `META:<id>`.
2. **Save work in progress** — leave nothing only in the chat:
   - Commit + push any uncommitted/unpushed repo changes on the session's branch (an immediate empty commit first if needed so nothing is lost). NEVER leave unpushed work.
   - Write the aspect's **structured ledger** `meta-state/<id>.json` (schema: `docs/meta-ledger-schema.md`): every chip with its current `bucket` + `two_pass` verdict (PASS / CONCERNS / FAIL), the refreshed `next_action`, open `gates`, and `decisions_pending`; stamp `updated`. This is what lets a fresh `/status` know what's auto-mergeable and a fresh `/meta <id>` resume exactly here.
   - **Prune as you write (keep the ledger lean — schema "Size budget & pruning"):** collapse every **prior-bout** terminal chip (`bucket` ∈ done/live/merged whose `dispatched`/`last_commit` predates this ledger's `updated` date) to the one-line archived form — keep only `{id, title, pr, bucket, two_pass}`, drop the long `note`/`output` and the spent process fields (their detail is in the PR + the memory topic file). Keep current-bout terminal chips and all non-terminal chips verbatim. Keep `bout` and `next_action` to **one line each** (~280 chars; long narrative → memory topic file), and aim for a ledger under ~8KB. (`tools/meta-ledger-prune --dry-run` shows what's over budget across all aspects; its `--apply` is the operator-supervised batch migrator, not something `/close` runs.)
   - Update the aspect's **memory** topic file with any durable decision, lesson, or "shipped X". The memory **index** line stays a terse pointer — live work-state belongs in the ledger, not the index.
   - Fold any design change into the aspect's **spec** doc.
   - Confirm the **registry row** (in `docs/META-aspect-registry.md`) is still accurate.
   - **Best-effort cleanup (non-blocking):** clear this session's active-aspect marker with `bash tools/hooks/meta-active-aspect.sh clear` so a later non-META session in the same working directory isn't mistaken for this aspect by the `prepend-meta-prefix.sh` hook. This is tidy-up of operator-local runtime state, *not* work-in-progress — a failure here never makes the session unsafe to close, and a stale marker is harmless (it only ever adds a correct-but-maybe-unwanted `[META:<id>] ` prefix, never blocks a spawn).
3. **Verify safe-to-close** (deterministic checklist) and report:
   - git: working tree clean and branch pushed?
   - ledger: `meta-state/<id>.json` current — chips, `bucket`/`two_pass`, `next_action`, `gates`, `decisions_pending`, `updated` stamped?
   - memory: durable decisions + lessons captured; index line still a terse pointer?
   - spec: reflects this bout?
   - children: every chip/PR in the ledger with its current `bucket` + two-pass verdict (PASS / CONCERNS / FAIL)?
   Then state either:
   - **✅ Safe to close** — `<one-line state>`; resume with `/meta <id>`. — or —
   - **⚠️ Not safe** — `<exactly what remains>`, and offer to finish those items now.
   When surfacing what remains or a follow-up, apply **decision triage** (`META-bootstrap.md`, How a META session behaves §9): decide path-only forks yourself and note them; escalate only product-direction forks, always with a recommendation — never pose an arbitrary question.
4. You cannot terminate your own session — once you report ✅, the operator closes/archives it.

Keep it tight: do the saves, then the one-line verdict.
