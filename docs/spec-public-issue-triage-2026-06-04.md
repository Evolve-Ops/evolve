# Public-issue triage — design spec (2026-06-04)

**Status:** Draft. **Not for execution pre-cutover.** Build only after the public-cutover has happened and ~4 weeks of real inbound volume has accumulated.

**Date:** 2026-06-04

**Origin:** Forward planning for the moment the repo flips public at `evolve-ops/evolve`. Inbound bug reports and feature requests will arrive via GitHub Issues; this spec captures how to triage them without doing it manually forever.

**Adjacent:**

- `docs/spec-alerts-signal-store-2026-05-07.md` — the signal store this design borrows from (observe-propose-arbitrate-apply-verify).
- The existing Inbox/Issues page in the admin UI — the Drafts card + Promote/Dismiss flow is the reference UX (see commit landing `test_inbox_tab_ui.py`, 2026-06-04).
- `docs/runbook-public-cutover.md` — the cutover this spec is post-of.
- `CONTRIBUTING.md` "Scope of contributions" — the policy text the LLM evaluator should ground in.
- `docs/spec-evo-account-separation-2026-05-25.md` — establishes evo as the conversational layer this triage will surface through.

---

## Problem

Once the repo is at `evolve-ops/evolve` and visible publicly:

- Bug reports and feature requests will arrive via GitHub Issues, opened by users the maintainer doesn't know.
- Some are legitimate and need attention; some are duplicates of existing issues; some are out-of-scope; some are spam; some need clarifying questions before they can be acted on.
- Manual triage doesn't scale past ~5 issues/week without significant cost. The maintainer falls behind, issues age, users feel ignored.
- A dedicated third-party triage service (Linear Triage, etc.) lacks codebase context, past-issue embeddings, and the project's actual scope — its triage quality is structurally worse than something Evolve-native.

The right answer is to **dogfood**: Evolve's own architecture (signal store + proposals + Inbox UI + evo conversational layer) is already shaped exactly like a triage pipeline. Public issues are just another signal source.

---

## Principle

Public inbound GitHub issues flow through the **same Inbox/Issues drafts queue** that already handles internal `evo intake` items. An LLM (evo or a dedicated triage skill) drafts a verdict; the maintainer approves with one click via the existing Promote/Dismiss UI. Closures and posted comments **always require human approval** until a verdict type has a documented accuracy track record.

This means: **no new top-level UI surface, no new daemon, no new triage tool.** Extend what's there.

---

## Architecture

```
GitHub Issues (evolve-ops/evolve, public)
         │
         ▼  (poll every 15 min — extend the existing inbound watcher)
inbound-issues-watcher
  reads new + recently-updated issues via gh CLI or REST
  writes one signal per issue to {shared_dir}/intake/open/github/<issue-number>.json
         │
         ▼  (signal-subscriber daemon fires; existing infra at
                docs/spec-signal-subscriber-2026-05-31.md)
issue-triage generator (new — runs as a Better Engine generator):
  reads the intake item
  uses tools (see §4) to gather context
  emits a Proposal with the drafted verdict
  links the originating intake via Proposal.motivating_signals
         │
         ▼
{shared_dir}/proposals/pending/<id>.json
         │
         ▼
Admin UI → Issues page → Drafts card
  shows: title · reporter · age · LLM verdict · LLM reasoning ·
         confidence · [Promote] [Dismiss] · [Show on GitHub]
         │
         ▼  (operator clicks Promote)
applier fires the verdict's action:
  - post comment via gh issue comment
  - apply labels via gh issue edit --add-label
  - close via gh issue close (only if verdict is a close-type)
  - leave open if verdict is keep-open-label-X
         │
         ▼
verify daemon (next sweep)
  if closed and reporter re-opens → demote that generator's authority
  if closed and reporter accepts → promote that generator's authority
  if labels stick → no-op
```

Reuses the **observe → propose → arbitrate → apply → verify** loop already in place for Better Engine generators. The triage generator is one more entry in `packages/analyzer/generators/`.

---

## Components

### 4.1 Inbound watcher

A small daemon (or LaunchAgent cron) that:

- Polls `gh issue list --repo evolve-ops/evolve --state open --json number,title,body,labels,author,createdAt,updatedAt` every ~15 min.
- For each issue not yet seen, writes a JSON intake record to `{shared_dir}/intake/open/github/<n>.json` with:
  - `source: "github_issue"`
  - `external_id: <issue_number>`
  - `title`, `body`, `labels`, `author`, `created_at`, `updated_at`
  - `motivating_signal_type: "github_issue_inbound"` (so the signal-subscriber fires)
- For issues already seen but updated (new comments, new labels), updates the intake record. The generator gets re-run.

**Existing scaffolding:** the inbound-watcher install pattern from PR #2076ish (the recent "Inbound watcher install + UI toggle (Phase 4c of Issue Inbox)" work) already covers the cron-job mechanics — only the data source changes.

### 4.2 issue-triage generator

New generator at `packages/analyzer/generators/issue_triage/`:

- `charter.yaml` — declares `subscribes_to: [github_issue_inbound]` so the signal-subscriber dispatches to it on every inbound.
- `evaluate.py` — the LLM call. Input: the intake record + the context tools below. Output: a Proposal with one of the verdict types from §5.

**The LLM should call tools, not synthesize.** Specifically:

- `search_past_issues(query, top_k=5)` — embedding search over open + closed issues. Returns title + summary + status + similarity score. Lets the LLM detect duplicates.
- `read_project_scope()` — returns the "Scope of contributions" section from `CONTRIBUTING.md` and the "What We Won't Act On" section from `docs/gitpages/CONTRIBUTING.md`. Anchors out-of-scope decisions.
- `read_reporter_history(login)` — returns the reporter's prior issues + PRs in this repo. Surfaces first-time contributors vs. repeat reporters; helps weight tone.
- `read_recent_verdicts(top_k=20)` — returns the last N triage verdicts (approved + dismissed) so the LLM can calibrate to the maintainer's actual preferences.

Reasoning shown in the Drafts card should include which past issues the LLM compared against and what scope clauses it relied on.

### 4.3 Drafts card UI (extension of existing)

The existing Drafts card on the Issues page (per the 2026-06-04 keystore-and-issues-ui commit) already renders intake items with Promote/Dismiss. Extend it:

- New filter chip: "GitHub" vs "Internal" (default: both).
- Per-row display: `[GitHub] #1234 — "title here"  ·  bug?  · confidence 0.74  ·  by @user (first-time reporter)  ·  3h ago`.
- Click row → expand panel showing: full issue body, LLM reasoning, similar-past-issues list, proposed verdict, [Promote] [Dismiss].
- Promote button label changes by verdict: "Close as duplicate" / "Label and keep open" / "Ask for clarification" / etc.

### 4.4 Applier

New module `packages/admin/evolve_admin/applications/triage_applier.py`:

- Reads the Proposal's verdict type.
- Calls `gh issue comment`, `gh issue edit --add-label`, `gh issue close --reason X --comment "..."` as appropriate.
- Records the outcome back in `{shared_dir}/proposals/applied/<id>.json` so the verify daemon can see it.

### 4.5 Verify pass

Existing verify daemon, with one extension:

- For closed-via-triage issues, watch for re-open events from the reporter. A re-open within 14 days is a strong signal that the triage was wrong. Demote that generator's authority (track in `GeneratorRecord` like the existing pattern).

---

## Verdict types

| Verdict | Auto-apply? | Notes |
|---------|-------------|-------|
| `close-spam` | **No** (human-confirms) | LLM flags; you confirm. Closure with comment: "Closing as spam — if you believe this is an error, please re-open with context." |
| `close-duplicate-of-#N` | **No** | Show the candidate duplicate inline so you can verify. Closure references the linked issue. |
| `close-out-of-scope` | **No** | Reference `CONTRIBUTING.md` "Scope of contributions we'll take" inline. |
| `close-cannot-reproduce` | **No** | LLM detected missing repro info AND no response after 14-day "needs-info" comment. |
| `keep-open-label-bug` | **Yes** (auto-label) | Labeling is cheap, reversible, expected. |
| `keep-open-label-feature` | **Yes** | Same. |
| `keep-open-label-question` | **Yes** | Same. |
| `keep-open-label-good-first-issue` | **No** (human-curates) | Selecting "good first issue" is editorial; the LLM proposes, you decide. |
| `needs-info-from-reporter` | **No** | Drafts a comment asking for the missing details (env, repro steps, version). You approve the comment text before it posts. |
| `escalate-priority` | **No** | Surfaces in a "priority" view at the top of the Drafts card. Operationally a label + UI sort. |
| `keep-open-no-action` | **Yes** | Default for unclear-but-real issues. Acknowledgement comment auto-posts thanking the reporter; no closure. |

**Key principle**: **labels and acknowledgement comments can auto-apply; closures and posted text always require human approval.** Labels are reversible; closures with comments are public and reputational. The bright line stays at the close action.

---

## Phases — build incrementally

### Phase 1 — Surface only (no LLM verdict yet)

- Inbound watcher in place; GitHub issues mirror into the Drafts queue.
- Drafts card shows GitHub items alongside internal ones.
- No LLM verdict; you triage manually but in one place.
- Promote/Dismiss in the UI just removes the item from the queue (no action against GitHub).
- **Purpose**: collect a corpus of (issue, your-actual-verdict) pairs to calibrate Phase 2.

### Phase 2 — LLM verdict drafts

- Add the `issue_triage` generator + the context tools listed in §4.2.
- Verdict + reasoning rendered inline with Promote/Dismiss.
- Promote on a verdict actually fires the action against GitHub (post comment, label, close).
- Closures always require human approval (no auto-fire).
- Labels can auto-apply per §5.
- **Purpose**: cut your triage time per issue from minutes to seconds, while preserving review.

### Phase 3 — Trusted auto-actions

- After Phase 2 has been running for ~3 months and a verdict type's approval rate is consistently >95%, allow that type to auto-fire.
- Always with an undo window (e.g., 24h between auto-close and the close becoming permanent — though "permanent" here just means "won't auto-re-open"; the reporter can re-open anytime).
- Auto-actions emit a notification to the maintainer summarizing what was auto-handled.
- **Purpose**: amortize routine cases without needing a click for each.

---

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| LLM mis-classifies a legitimate bug as spam → reporter feels ignored | Closures always require human approval in Phase 1-2; Phase 3 only for verdicts with >95% historic approval rate; undo window. |
| Habituation — you stop reading and just click Promote | LLM surfaces uncertainty (`confidence: low`); randomly-sampled audit (e.g., once a week, re-read 3 auto-approved closures and verify they were correct). |
| Volume swamps one reviewer | Tier the queue: urgent (security, regression) vs. routine (feature request, question). PWA push notifications for urgent items only. |
| First-time contributor gets a curt auto-comment, leaves bad taste | Templates that are kind by default; auto-comments address the reporter by name; thank them; explain the reasoning briefly. Maintainer can edit the comment before approving. |
| Bias toward closing things (it's the easier verdict) | Track keep-open-vs-close ratio over time per verdict type; if drift starts, intervene. |
| LLM drafts a verdict using stale "Scope of contributions" wording | The context tool reads the file at evaluation time, not at build time. Updating `CONTRIBUTING.md` is sufficient. |
| Reporter and maintainer are in different time zones; "needs-info" requests sit for days | Phase 2 adds a 14-day stale-needs-info auto-prompt: re-comment once gently before the cannot-reproduce close-verdict becomes eligible. |

---

## Acceptance criteria

A verdict type is "production-ready" for Phase 3 when:

1. The generator has emitted at least 50 verdicts of that type.
2. Operator approval rate is ≥95% over the last 30 days.
3. Zero reporter re-opens within 14 days of an approved-and-applied close of that verdict type in the last 60 days.
4. The verdict's prompt + reasoning template has not been edited in the last 14 days (stable).

A verdict type is "demoted back to require-approval" when:

1. Approval rate drops below 80% over the last 14 days, OR
2. Two or more reporter re-opens within 14 days of close, OR
3. The maintainer manually flips a kill-switch.

---

## Out of scope (explicitly)

- **PR review.** This spec is about Issues, not PRs. PR review has different shape (code review, CI status, contributor licensing) and deserves its own design.
- **Closing stale issues.** Periodic "this issue has been open N days with no activity, are you still seeing it?" sweeps are a separate concern. Could be a second generator.
- **Issue-to-PR conversion** for feature requests with clear specs. Tempting but adds a lot of trust surface. Defer.
- **Spam pre-filter at the GitHub layer.** Use GitHub's native spam tools first; only triage what makes it past.
- **Public roadmap inference** from triaged labels. Useful but secondary; deal with after Phase 2 is stable.

---

## Timing — when to build

| Phase | Trigger | Estimated effort |
|-------|---------|------------------|
| Pre-cutover | **Don't build.** No real volume; designs are guesses. | 0 |
| Week 1–4 post-cutover | **Manual triage only.** Use GitHub's native UI. Take notes on what you wish was automated. | 0 (just attention) |
| Month 2 post-cutover | **Build Phase 1** — inbound watcher + Drafts surface for GitHub items. One PR, mostly extending existing intake code. | 1 PR, ~1 day |
| Month 3–4 post-cutover | **Build Phase 2** — issue-triage generator + verdict UI. One PR. Test against the Month-1 corpus before going live. | 1 PR, ~3 days |
| Month 6+ post-cutover | **Consider Phase 3** if patterns are stable. Per-verdict; gradual rollout. | Per-verdict ~half a day each |

**Lead indicator that it's time to build Phase 1**: triaging the Issues tab manually is taking more than ~30 minutes per week, OR you've fallen more than 7 days behind on responses.

---

## Why this beats the alternatives

- **vs. a separate triage bot** (e.g., probot triage plugins): dogfooding. Evolve already does proposal-evaluation work for internal signals; the triage system is a natural application of the same engine. One less tool to maintain. Reasoning + verdict are visible in the existing admin UI, not a different inbox.
- **vs. third-party services** (Linear, GitHub's own auto-triage, etc.): structurally worse triage quality because they don't have access to your codebase, past-issue embeddings, or project context. Also: more vendor surface to maintain.
- **vs. pure manual**: doesn't scale past ~5 issues/week without significant maintainer cost. Doesn't capture the "draft a verdict from context" value that LLMs are genuinely good at.
- **vs. pure automation** (LLM auto-closes everything): one mis-close has reputational cost. Trust isn't earned until Phase 3 conditions are met. The Promote/Dismiss flow lets you build trust gradually.

---

## Open questions

1. **Generator inference cost.** Each verdict draft is an LLM call with substantial context (issue body + ~5 past issues + scope docs + reporter history + recent verdicts). For ~50 issues/week at Sonnet, this is meaningful but bounded. Budget Hawk should treat the triage generator like any other generator and surface its monthly cost. If it becomes a real expense, route to Haiku for routine and Sonnet only for low-confidence cases.

2. **Embedding infrastructure for past-issue search.** The codebase has some embedding work (`packages/analyzer/embedding_*`) but it's not clear if it covers the GitHub-issue corpus. Phase 1's inbound watcher should ALSO populate the embedding store so by the time Phase 2 is built, past-issue search is fast.

3. **Maintainer notification channel.** PWA push for urgent items is the default plan, but operator preference may vary (some prefer email batched daily, some prefer Slack/Telegram via evo). Configurable.

4. **Multi-maintainer scenario.** Currently single-maintainer. If/when multiple maintainers exist, the Drafts queue needs claim/unclaim semantics and per-maintainer authority weighting in the generator.

5. **GitHub Discussions** is a parallel surface — questions there could feed the same triage pipeline with a different default verdict-type distribution (mostly `keep-open-label-question` and `escalate-priority`). Worth considering when Phase 2 ships.
