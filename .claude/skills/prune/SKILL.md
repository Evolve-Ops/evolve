---
name: prune
description: Close + archive finished/idle META sessions in one pass — sessions whose PR has merged, stale scheduled-task runs, and checkpointed META coordinators. Lists candidates by safety bucket, you confirm, it archives (frees worktrees, cuts clutter + cost). Reopen anytime from the Archived list or a fresh /meta.
---

The operator wants to clear the session pile in ONE command instead of hunting each in the UI.
Archiving **stops a session's process and cleans its worktree**, then it can be reopened from the
Archived list — so it is safe ONLY when the work is durably saved (committed/pushed or merged).
Be conservative: when in doubt, KEEP.

1. **Enumerate.** `list_sessions` (limit ~50). **Exclude** the current session and any
   `isRunning: true` session (never archive a live one — it may have unsaved work).
2. **Classify each remaining session into a safety bucket:**
   - **DONE — PR merged/closed:** `prNumber` set and `prState` ∈ {MERGED, CLOSED}. The work is in
     main; the worktree is disposable. Safest, highest-volume bucket (chip sessions `[META:…]` and
     named PR sessions).
   - **STALE INFRA:** scheduled-task leftovers — title like `Meta fleet watch`, `Meta loose ends`,
     `meta-reconcile`, not running. Pure clutter.
   - **CHECKPOINTED META:** a `META <id>` coordinator, not running, whose aspect ledger is current
     (reconciler/`/close` left it checkpointed) with no chip needing it open. Safe to archive — it
     reconstitutes from the trio via `/meta <id>` in seconds.
   - **KEEP:** chip whose PR is still OPEN (may need iteration — the reconciler merges it when
     green+PASS, then it becomes DONE next pass), the current session, anything running, or
     anything you cannot confirm is saved.
3. **Present the buckets** with counts + one line each, DONE first. Recommend the default
   (DONE + STALE INFRA) and ask which buckets/sessions to archive. (CHECKPOINTED META is offered
   but opt-in — the operator may want a coordinator left open.)
4. **Archive the approved.** Call `archive_session(session_id, reason)` per session — the tool
   prompts each time; that per-session confirm is the safety rail (never archive speculatively).
   Reason: "PR #X merged" / "stale scheduled run" / "META idle, checkpointed".
5. **Report** how many were archived and what was kept and why.

**Make the bulk automatic:** suggest enabling **"Auto-archive on PR close"** in Settings — then
chip sessions self-archive the moment their PR merges, and `/prune` only handles infra leftovers
and idle coordinators.

Never archive the current session, a running session, or anything with unsaved/unpushed work.
The complement is `/launch` (which aspects to re-open) and `/queue` (what needs you).
