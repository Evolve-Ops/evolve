# Issue Inbox — 2026-05-22

> Status: design. Supersedes the "Maintainer Inbox" framing from the same
> session. The upstream-issues-watcher work that already landed
> (`docs/spec-upstream-issue-watcher-2026-05-22.md`) is Phase 1's polling
> backbone — this spec absorbs and extends it.

## Goal

Make every Evolve install a first-class participant in the GitHub issue
threads its operator cares about — both **outbound** (issues they filed on
upstream repos like OpenClaw and Evolve itself) and **inbound** (issues
others filed on repos they maintain).

The motivating story is the one we lived earlier in this very session: we
filed an issue against OpenClaw, a maintainer asked a clarifying question,
the operator nearly missed the email, we drafted a response together, the
operator approved, and we posted. Today that workflow takes a chat window,
a terminal, a browser tab, and an alert system. After this lands, it lives
on a single tab inside Evolve.

## Non-goals

- Replacing GitHub Issues as a system of record — GitHub is still the
  source of truth. The Inbox is a decision queue layered on top.
- Merging code without GitHub's review path. This spec covers
  comment-level participation. PR creation is supported as a draft-PR write
  tool, but merging follows GitHub's normal review/approval gates.
- Pulling in private repos the operator doesn't have read access to. The
  Inbox shows what `gh` already shows.
- A full project-management surface. Labels, milestones, projects, etc. are
  GitHub-native; we only touch the ones needed for triage.

## Audience reframing

Originally scoped to "just the Evolve developer." Expanded scope:

- **Any Evolve admin** who has filed issues against any repo they care about
  (Evolve itself, OpenClaw, dependencies, their own projects) — track replies,
  draft responses with evo's help, post from the Inbox.
- **Any Evolve admin who is also a maintainer of one of those repos** —
  additionally gets the inbound triage queue: incoming issues with evo's
  prepared verdict, ready to approve / classify / auto-close.
- **Future contributors** to Evolve or OpenClaw who want a low-friction way
  to file good bug reports and respond to follow-ups.

The "maintainer" view is unlocked per-repo by checking the operator's
permission level via `gh api repos/{owner}/{repo}/collaborators/{login}/permission`.
A user who tracks `evolve-ops/evolve` as a non-maintainer sees only their
own filed issues; a user with admin/maintain/triage permission sees the
inbound triage queue too.

## Architecture

New subpackage at `packages/maintainer/` (name retained for code-location
clarity even though most users will use it as participants, not
maintainers — adjust if a better single-word name emerges during build).

Feature gating splits into three layers:

```
features.evo_issue_handling.enabled   # evo can classify problems + draft + file issues
                                      # (on by default — talking to evo is the front door)
  ↓
features.issue_inbox.enabled          # the Inbox tab + tracked-repo subscriptions + watcher
                                      # (auto-enabled after evo files the first issue;
                                      #  manual opt-in via profile or explicit flag too)
    └── features.issue_triage.enabled # inbound LLM-triage view (auto-shown when the operator
                                      # has maintainer perms on at least one tracked repo)
```

`feature_profile` defaults:

| Profile | evo_issue_handling | issue_inbox | issue_triage |
|---|---|---|---|
| `standard` | **on** | off (auto-flips on after first filing) | off |
| `developer` | on | on | on |
| `minimal` | **on** | off | off |

Why `evo_issue_handling` is on for minimal: reporting a problem must
never require the operator to flip a feature flag first. Evo is on
in every Evolve install; this just gives it the tools to classify,
draft, and file.

Why `issue_inbox` auto-flips on first filing: once an issue exists,
the user wants to know when it gets replies. Silently enabling the
Inbox after the first filed issue preserves the "I don't have to
know the Inbox exists" UX while still keeping the polling daemon
gated for users who never file anything.

This replaces the original `upstream_issues_watcher` profile entry —
the polling daemon's enable flag becomes a sub-flag of `issue_inbox`.

Why same codebase: shared auth, Signal store, alerts catalog, admin UI
shell, evo. Why new subpackage: organizational signal that the feature
is opt-in and self-contained.

## Inbox: the unit of work

Every actionable thread is an Inbox item, stored at
`{shared_dir}/maintainer/inbox/{state}/{item_id}.json`. Schema:

```json
{
  "item_id": "openclaw-openclaw-84820",
  "kind": "outbound_comment | inbound_issue | inbound_comment",
  "repo": "openclaw/openclaw",
  "issue_number": 84820,
  "title": "Unclosed FileHandle on session JSONL lock …",
  "url": "https://github.com/openclaw/openclaw/issues/84820",
  "state": "new | analyzed | in_conversation | awaiting_post | done | dismissed",
  "trigger": {
    "kind": "comment_arrived | issue_filed | state_changed",
    "actor": "oc-maintainer",
    "at": "2026-05-22T03:14:00Z",
    "comment_id": "12345",
    "snippet": "Thanks for the detailed report. We tried…"
  },
  "verdict": {                       // present only for inbound triage
    "category": "bug | feature_request | question | duplicate | spam | docs",
    "merit": "real | unclear | low",
    "urgency": "p0 | p1 | p2 | p3",
    "duplicate_of": ["evolve-ops/evolve#17"],
    "evidence": ["…"],
    "recommendation": "auto_close_duplicate | auto_reply_clarifying | route_to_admin",
    "estimated_effort": "trivial | small | medium | large",
    "confidence": 0.0
  },
  "drafts": [
    {
      "draft_id": "d1",
      "produced_by": "evo",
      "at": "2026-05-22T03:15:00Z",
      "body": "Thanks for digging in…",
      "tool_calls_summary": ["gh issue view …", "log lookup …"]
    },
    {
      "draft_id": "d2",
      "produced_by": "admin_edit",
      "at": "2026-05-22T03:18:00Z",
      "body": "Thanks for digging in.\n\n…(edited)…",
      "parent_draft_id": "d1"
    }
  ],
  "actions_taken": [                  // append-only audit trail
    {"at": "...", "kind": "comment_posted", "comment_id": "67890", "draft_id": "d2"}
  ],
  "created_at": "...",
  "last_observed_at": "...",
  "resolved_at": null
}
```

The `state` field is authoritative; the subdir is a physical index for
efficient iteration. Same pattern as the Signal store (`signals.store`)
— reuse `arbiter.store`-style helpers (`iter_items`, `move_item`,
`write_item`).

## Surfaces — evo chat as the front door

There is no bug-report form. There is no feature-request form. The
front door for "make it better" is **a conversation with evo** — the
same evo the operator already talks to on the admin UI home page, in
side drawers, and over Telegram.

This is a deliberate UX choice. Forms force a binary at the worst
possible moment (am I reporting a bug, or requesting a feature, or
something in between that I can't name?), and they discard the context
evo has already built up over the operator's session. A free-form
conversation:

- Lets the user describe what's bothering them in their own words,
  including the cases that don't fit a form ("evo's reply five minutes
  ago wasn't helpful", "something feels slow but I'm not sure what").
- Gives evo the chance to **try to fix the problem first** before
  filing anything. A non-trivial fraction of "bug reports" are
  config drift evo can resolve in one tool call.
- Captures the full diagnostic path naturally — evo's tool calls
  during the investigation become the issue body, automatically.
- Routes the result to the right repo (Evolve, OpenClaw, plugin)
  based on classification, instead of asking the user to know.

### The four surfaces (revised)

Three are evo-chat surfaces; the fourth is the tracking UI.

1. **Home-page evo chat** — the existing primary surface. User opens
   the admin UI, evo is right there. They type "X is broken" and
   evo handles the rest.
2. **Side-drawer evo chats** — context-scoped chats opened from
   page drawers (Alerts page, Apps page, etc.) — same evo, but with
   the surrounding page state pre-loaded into the conversation.
   Especially good for "this thing on this page isn't working"
   reports. Evo records the originating page in the conversation
   context (`reported_from: /alerts`, `reported_from: /apps/cve-scan`,
   etc.) and uses it both as a classification signal ("user was on
   the Alerts page when this came up" → look at the alerts code path
   first) and as a metadata line in any issue evo subsequently files.
   Reuses the existing page-context-pack infrastructure rather than
   building a parallel capture mechanism.
3. **Telegram evo chat** — the same conversation from a phone. Same
   evo, same tools, same outcomes.
4. **Inbox tab** — the durable tracking surface (filed issues, their
   status, maintainer replies that need attention). Auto-enabled
   when the first issue gets filed; the user discovers it via the
   "filed as #42 — I'll let you know when there's an update"
   close-out that evo posts at the end of an issue-filing
   conversation.

### A "Report an issue" affordance

For discoverability — some users will look for an explicit button
labeled "Report an issue" even though the answer is "just chat with
evo." So:

- A small "Make Evolve better" or "Report an issue" link in the
  admin UI footer / drawer / settings page (final placement TBD).
- Clicking it opens an evo chat pre-seeded with the message
  `I'd like to report an issue` so evo starts the conversation
  framed correctly.
- Optionally: same affordance as a Telegram slash command (`/improve`
  or similar) — opens or focuses an evo conversation with the same
  pre-seed.

No form, no fields. Just chat.

### Progressive disclosure principles

- **The default path must be useful even when nothing is broken.**
  Operators chat with evo about everything else; the "make it better"
  capability lives alongside, not in a separate place.
- **Evo should solve before it files.** Filing an issue is the
  fallback when evo couldn't resolve the concern in the conversation.
- **A user never has to learn a new UI** to report a problem. If they
  know how to talk to evo, they know how to report a problem.
- **The Inbox tab reveals itself** the first time it has something
  to show. Until then, it stays hidden — no empty-state friction.

## Issue classification — four categories

When a conversation reaches the point where filing an issue might be
the right move, evo needs to classify what kind of issue it is. The
operator is rarely the right person to make this call — they're
experiencing a symptom, not a diagnosis. Evo has the codebase context
to make a better-informed call.

Four categories:

| Category | Description | Action |
|---|---|---|
| `local_env` | The Evolve / OpenClaw / plugin code is fine; the user's setup is the issue (expired token, misconfig, network issue, etc.) | Evo guides the fix in-chat. No issue filed. |
| `evolve_code` | Reveals a shortcoming in the Evolve codebase — a bug, missing feature, or design gap | Evo offers to file against evolve-ops/evolve |
| `upstream` | Reveals a shortcoming in OpenClaw or a third-party dependency | Evo offers to file against the upstream repo (or comment on an existing issue if one matches) |
| `mixed` | More than one of the above. Often the most interesting class — a local symptom uncovers both a missing Evolve detection AND an upstream root cause | Evo offers to file two linked issues, one per affected codebase |

Evo's classification is one tool call's worth of work: read the
relevant logs / Signals / config, look for matches against known
incidents, look for recent commits in the affected area, decide.
Outputs a structured verdict the user sees in chat:

```
I think this is two things:

  1. The $20 spike yesterday wasn't caught by your alerts page,
     because we don't have a watchdog for repeated heartbeat-session
     crashes. That's an Evolve code gap.
  2. The underlying crash is openclaw/openclaw#84820 (already filed
     upstream — they're investigating).

Want me to:
  • file the detection gap against evolve-ops/evolve, and
  • add your stack trace to the upstream issue as additional evidence?
```

The operator either says yes, refines ("just the upstream one"),
or pushes back ("actually I think it's something else, here's why")
and the conversation continues.

### Classification heuristics evo applies

Not exhaustive — these are the cheap, high-signal checks evo runs
before guessing:

- **Originating page context** (when the chat came from a drawer):
  cheap, high-signal — the page tells evo which code paths are
  most likely involved. A complaint surfaced from
  `/alerts` → look at signal_notifier / catalog / dispatcher first;
  from `/apps/<bot>` → look at that bot's config + recent deploys;
  from `/integrations` → look at the relevant integration probe.
  Doesn't decide the classification by itself, but narrows the
  search space substantially.
- **Logs and Signals near the time of complaint**: does anything
  match the user's description? Does a stability bundle exist?
- **Open issues on tracked repos**: any existing issue with similar
  symptoms? Use `gh search` with keywords from the user's message.
- **Recent commits**: was the affected area touched in the last
  N days? (Could be a regression we shipped.)
- **Plugin / OC version vs supported floor**: are we running
  something OC declared unsupported?
- **Bot config audit**: any obvious misconfig in the affected bot's
  openclaw.json / AGENTS.md?

If two or more of these point to "Evolve code" while one points to
"upstream," the verdict is `mixed`. Evo says so explicitly rather
than picking one — operators benefit from knowing they may need
two separate fixes.

## From conversation to filed issue: the workflow

A representative chat (the openclaw#84820 sequence, retold from a
prospective user's perspective rather than the developer's):

```
User: team-bot-a crashes every couple of hours, costing me money

Evo:  Let me check…
      [reads stability bundles, sees uncaught_exception pattern]
      [checks open upstream issues, finds #84820]
      
      I think you're hitting the same bug we filed against OpenClaw
      a couple of days ago — openclaw/openclaw#84820. They're
      investigating but haven't shipped a fix.
      
      Short-term: I can downgrade your node to 22 LTS, which we've
      confirmed works around the crash. ~5 min, no other side
      effects on this pod.
      
      Long-term: I can add your bundle to #84820 as additional
      evidence — they specifically asked for more reproducers.
      
      Which would you like? (Both, if you want.)

User: both please

Evo:  [executes node downgrade — gated by exec approval]
      [drafts comment with sanitized bundle, shows user the draft]
      
      Here's the comment I'd add to #84820:
      
        > Adding another reproducer — same crash signature on a
        > different host. Sanitized stability bundle attached.
        > Bot is running an unrelated workload (chat assistant)
        > so this isn't security-bot-specific.
        > […]
      
      [Approve & post]  [edit]

User: looks good, post it

Evo:  Done. Tracked in your Inbox as openclaw#84820 — I'll ping
      you when they respond.
```

The user never opened a form. They never had to know whether this
was a "bug report" or a "feature request." They never had to know
which repo to file against. Evo did all of that.

The Inbox item for #84820 now lives in the Inbox tab with state
`done` (for the comment-add action) — but evo subscribed the user
to future activity, so when the OC team replies, it becomes
`new_activity` and the user gets a Telegram nudge.

### Diagnostic context — evo decides, not a checkbox

Because evo is driving the issue body, the "include diagnostic info?"
checkbox from the previous draft of this spec disappears. Evo
attaches what's relevant based on what it found during the
investigation:

- For an `evolve_code` issue: include the relevant log snippets,
  Signal entries, code references, and reproduction steps evo derived
  while diagnosing.
- For an `upstream` issue: include sanitized stability bundle + the
  specific failure signature, matching what we already do for
  openclaw#84820.
- For a `local_env` issue: nothing is filed; evo just helps the user
  fix it.

The redaction rules are the same as before — bot names, user names,
channel IDs, API keys, tokens, email addresses, IP addresses are all
stripped. Evo shows the user the drafted issue body before posting,
so any over-share is caught at human review.

Hard cap: 5 KB of attached context in the issue body. Anything
larger goes into a gist created under the user's identity (with
explicit approval) and linked from the issue.

What evo always includes in the header of any filed issue (so
maintainers can grep for it):

```
- Evolve version: v0.3.0 (commit abc1234)
- OpenClaw version: 2026.5.18
- Filed via Evolve issue inbox
- Reported from: /alerts        ← only when chat originated from a drawer
```

The `Reported from:` line is one of the most valuable pieces of
metadata for a maintainer triaging an Evolve-side bug — it pins the
report to a specific page / code path without the user having to
articulate it.

That last line makes Evolve-filed issues easy to identify in the
GitHub UI — useful for triage on the maintainer side.

## Repo subscription model

Each Evolve admin maintains a per-user list of tracked repos at
`{shared_dir}/maintainer/tracked_repos.json`:

```json
{
  "repos": [
    {
      "repo": "openclaw/openclaw",
      "added_at": "2026-05-22T03:00:00Z",
      "track_filed_by_me": true,           // outbound; default true
      "track_all_issues": false,            // inbound; requires maintainer perms
      "auto_response_rules_enabled": false  // requires maintainer perms
    },
    {
      "repo": "evolve-ops/evolve",
      "added_at": "2026-05-22T03:00:00Z",
      "track_filed_by_me": true,
      "track_all_issues": true,             // operator has admin perms here
      "auto_response_rules_enabled": false
    }
  ]
}
```

Adding a repo is a two-click flow:

1. UI prompts for `owner/repo`. Validates via `gh repo view`.
2. Detects permission level via `gh api repos/{owner}/{repo}/collaborators/{login}/permission`.
3. If admin/maintain/triage perms are present, the "Track all issues
   (inbound triage)" toggle becomes available; otherwise it's grayed
   with an explanatory tooltip.

Default seed for new installs that opt into `issue_inbox`: empty list.
Operator adds explicitly — no surprise polling of "every repo I touched."

## User identity / gh auth model

**v1**: single gh identity per Evolve install, stored in the evolve
system user's home (same path as the upstream-issues-watcher already
uses). One-time setup:

```
sudo -u evolve gh auth login --hostname github.com --git-protocol https --web
```

Every comment / triage action posted from this install is attributed
to that identity. The UI surfaces "Posting as @{login}" prominently
above every Approve button so the operator never accidentally posts
under the wrong account.

**Future** (out of scope for v1): per-admin-login gh tokens, so a
multi-admin Evolve install can attribute posts to whichever admin
clicked Approve. Migration path: store the token in a per-user
keychain rather than the evolve user's gh config. Not built now — most
installs are single-admin.

## State model

```
new                    (watcher detected activity, evo hasn't looked yet)
  ↓ (evo runs analysis pass)
analyzed               (verdict + initial draft ready)
  ↓ (admin selects from inbox list)
in_conversation        (admin + evo iterating on draft)
  ↓ (admin clicks Approve)
awaiting_post          (gh subprocess running)
  ↓
done                   (action completed)
  ↑ (issue receives new activity later)
new                    (reopens — same item, fresh trigger)

OR

dismissed              (admin chose no-op; archived but reopenable)
```

Items move between subdirs as state changes (same pattern as
Signals/Proposals). Re-open on new activity within a 30-day window
preserves history; older items get a fresh item_id.

## UI

New tab in the admin UI: **Inbox**. Visible when `features.issue_inbox.enabled`
resolves true.

Two-pane layout:

```
┌─ Inbox ──────────────────────┐ ┌─ Conversation ────────────────────┐
│ Posting as @example_user          │ │  openclaw#84820 — FileHandle leak │
│                              │ │  Tracked since 2026-05-21         │
│ Tracked repos:               │ │                                    │
│ • openclaw/openclaw          │ │  [evo's prepared analysis]         │
│ • evolve-ops/evolve  [M]   │ │  Maintainer is asking about        │
│ [+ Add repo]                 │ │  filesystem sync layers. I checked │
│                              │ │  — APFS local, no Dropbox, no      │
│ ─── Needs your reply ───     │ │  iCloud, no xattrs.                │
│ ● openclaw#84820  2h         │ │                                    │
│   "share filesystem details" │ │  Draft reply:                      │
│                              │ │  ┌──────────────────────────────┐  │
│ ─── Triage queue [M] ───     │ │  │ Thanks for digging in.       │  │
│ ● evolve#42  4h   p2 bug     │ │  │ Local APFS, no sync layer…   │  │
│   "team-bot-a gateway crashes on…"  │ │  │ [editable]                   │  │
│ ● evolve#43  5h   duplicate  │ │  └──────────────────────────────┘  │
│   "feature: dark mode" → #17 │ │                                    │
│                              │ │  [Ask evo to revise…]              │
│ ─── Done (last 7d) ───       │ │  ┌──────────────────────────────┐  │
│ openclaw#82301 ✓             │ │  │ "more concise"               │  │
│ evolve#41 ✓                  │ │  └──────────────────────────────┘  │
│                              │ │                                    │
│                              │ │  Posting as @example_user               │
│                              │ │  [Approve & Post] [Dismiss]        │
└──────────────────────────────┘ └────────────────────────────────────┘
```

`[M]` markers indicate maintainer-only features (the triage queue, the
"track all issues" toggle on a repo). Hidden entirely for repos where
the operator doesn't have maintainer perms — no point teasing locked
features.

Telegram nudges keep the existing alerts path: short message with a
deep link, no full review inside Telegram. Per
`feedback_message_style_team-bot-a_like`.

## Evo integration

Each Inbox item gets a chat thread with evo, scoped to that item. Evo's
context window includes the item's full history, the underlying issue's
GitHub content, and any tool-call outputs from the analysis pass.

**Read-only tools** (available during the analysis pass before the
admin sees the item):

- `gh_issue_view(repo, number, with_comments=True)`
- `gh_search_duplicates(repo, query, before_date)` — find similar issues
  via GitHub search
- `gh_pr_search(repo, query)` — has someone already fixed this?
- `evolve_code_search(query)` — does the issue reference Evolve
  internals worth checking?
- `signal_store_query(producer, since)` — does this match a recent
  incident? (For Evolve-against-Evolve cases, very high-value.)
- `git_log_recent(file_path)` — has the code in question been touched?
- `read_session_transcript(session_id)` — for incidents like
  openclaw#84820 where the original investigation transcript is the
  best evidence.

**Write tools** (gated on admin approval at the Approve & Post moment):

- `gh_issue_comment(repo, number, body)`
- `gh_issue_close(repo, number, reason)`
- `gh_issue_label(repo, number, labels)` — maintainer-only
- `gh_issue_assign(repo, number, assignee)` — maintainer-only
- `evolve_open_spec(slug, body)` — creates a `docs/spec-{slug}-{date}.md`
  skeleton
- `evolve_open_pr_draft(branch, title, body)` — opens a draft PR with
  the proposed fix scaffold; admin still authors the actual code

Tool budget per analysis pass: capped at 10 tool calls and 30s
wall-clock. The analysis pass produces a verdict + initial draft; the
admin's "Ask evo to revise" iterations get a separate, smaller budget
(3 tool calls each) to keep cost predictable.

Per `feedback_rsi_low_cost_preference` — analysis pass uses Haiku by
default, Sonnet only when the verdict pass classifies the item as
high-stakes (p0/p1, or `critical`-labeled).

## Maintainer-only: LLM triage queue

When a tracked repo is set to `track_all_issues: true` AND the operator
has maintainer perms, the watcher also surfaces inbound issues filed
by others. Evo's analysis pass produces the structured verdict shown
in the schema above.

Auto-response policy (per `feedback_pre_launch_architect_properly` —
build it right, no quick wins):

- **Default**: nothing auto-responds. Every inbound issue waits for
  admin approval before any external action.
- **Per-category opt-in rules** at `install.json::features.issue_triage.auto_response_rules`:

  ```json
  {
    "auto_response_rules": [
      {
        "name": "auto_request_logs",
        "when": {"category": "bug", "evidence_empty": true},
        "min_confidence": 0.85,
        "action": {"kind": "comment", "template": "request_logs"},
        "rate_limit": "5 per day"
      },
      {
        "name": "auto_close_obvious_duplicate",
        "when": {"category": "duplicate", "duplicate_confidence": 0.95},
        "min_confidence": 0.95,
        "action": {"kind": "close_with_link"},
        "rate_limit": "10 per day"
      }
    ]
  }
  ```

- **Audit trail**: every auto-action writes a Signal (severity=info)
  AND an Inbox item in `done` state, so the admin sees what happened.
- **24-hour undo window**: every auto-action gets a follow-up button
  "this was wrong — revert" that posts a correction comment + reopens
  if closed.

Templates for `comment.template` live at
`packages/maintainer/templates/{template_name}.md` and use mustache-style
substitution from the issue payload.

## Privacy & safety

- **gh token at rest**: stored under `/Users/evolve/.config/gh/`, mode
  0600, owner `evolve`. Same protection as the existing
  upstream-issues-watcher uses.
- **Posting attribution**: every UI surface that leads to an outbound
  post shows "Posting as @{login}" in fixed position. No way to post
  without seeing the identity. Per `feedback_design_constraint_mildly_tech_capable`
  Plex-test — a household user must never accidentally post under the
  wrong account.
- **Rate limits**: cap outbound comments at 20/day per repo, per
  install. Spam-button prevention. Exceeded → soft block with a
  "you've commented a lot on this repo today — sure?" confirmation.
- **Cross-repo information leakage**: each item's evo context is
  scoped to one issue. Tool calls are sandboxed to the relevant repo
  (e.g. `gh_search_duplicates` defaults to the item's repo unless the
  admin explicitly cross-references). No accidental "I was just looking
  at openclaw#84820, let me leak that detail into my
  evolve-ops/evolve#42 reply" failures.
- **Public vs private repo handling**: if a tracked repo is private,
  evo's context becomes private-tagged and is not allowed to be
  included in any cross-repo reply.

## Cost shape

Steady-state per active install with one tracked repo, one new
maintainer comment per day:

- 1 analysis pass (~2k tokens in, ~500 out) on Haiku ≈ \$0.003
- ~3 revision iterations on Haiku ≈ \$0.001
- Total per item: ~\$0.005

A maintainer running the triage queue on Evolve itself with ~10 new
issues/day during a busy week:

- 10 verdict passes/day on Haiku ≈ \$0.03/day
- ~5 require admin revision iterations: \$0.005
- Worst case: \$0.05/day, \$1.50/month per maintainer-mode install

Well within the `feedback_rsi_low_cost_preference` budget. Sonnet
escalation only happens for high-stakes items, naturally rare.

## Verification

Per `feedback_two_pass_review_workflow`:

- **End-to-end**: file a test issue on a sandbox repo, comment from a
  second account, confirm the Inbox surfaces it, draft+approve a reply,
  confirm it posts under the expected login.
- **Permission detection**: tracked repo with maintainer perms shows
  triage queue; same repo with non-maintainer login does not.
- **State transitions**: replay a sequence of events (file → comment →
  reply → close → reopen) and assert the Inbox item moves through the
  documented states correctly.
- **Self-comment exclusion**: when the operator comments via the Inbox,
  the next watcher tick does NOT create a new Inbox item for their own
  comment.
- **Posting attribution test**: assert UI surfaces and the audit-trail
  entry both name the correct gh login.
- **Auto-response audit**: enable a rule, trigger it, verify Signal +
  Inbox item written, verify 24h-undo path works.
- **Token revocation**: pull the gh token mid-operation, confirm the
  watcher emits the auth-failure Signal we already built, confirm the
  Inbox surfaces "auth broken, fix it" prominently.

## Phasing

**Phase 0 — Evo issue-handling capability (lands before everything else)**:

Teach evo the workflow: notice that the user is describing a problem,
try to solve it first, classify what's left into the four categories,
draft + propose an issue body, post on approval. Independently useful
without any new UI — every Evolve install gets it because every install
has evo.

What lands:

- A small new evo skill / capability under
  `packages/admin/evolve_admin/evo/handlers/issue_handling.py` (or
  similar — pattern-match existing evo handler locations).
- Classifier prompt that takes (user message + recent conversation
  context + tool-call outputs) → returns `{category, draft_title,
  draft_body, target_repo, reasoning}`.
- Read-only tool registrations evo can call during classification:
  `gh_search_issues`, `gh_issue_view`, `signal_store_query`,
  `read_recent_logs`, `bot_config_diff_baseline`, `git_log_recent`.
- Write tool: `gh_issue_create(repo, title, body)` — gated on explicit
  in-chat operator approval, never auto-runs.
- Sanitized context-attachment helper that reuses the existing
  stability-bundle redaction rules. Evo invokes this when drafting,
  not as a checkbox the user has to remember.
- Writes an Inbox item in `done` state for every filed issue so the
  submission is trackable later. Works even when
  `features.issue_inbox.enabled` is off — the data persists silently
  and the Inbox tab simply auto-enables itself the moment there's
  something to show.
- A "Report an issue" footer link (admin UI) and `/improve` slash
  command (Telegram + admin UI) that open or focus an evo chat
  pre-seeded with `I'd like to report an issue`. Discoverability
  for users who look for an explicit button.

What does NOT land in Phase 0:

- The Inbox tab UI (Phase 1).
- The polling watcher refactor (Phase 1).
- Tracked-repos config UI (Phase 3).
- Maintainer triage queue (Phase 4).

Scope estimate: ~300 lines for the evo handler + tool registrations,
~200 for the redaction helper, ~150 for the footer link / slash
command surfaces, plus tests. Self-contained — doesn't depend on the
already-shipped upstream-issues-watcher.

**Phase 1 — Inbox foundation + outbound from current upstream watcher**
(builds on what already shipped):

- New subpackage `packages/maintainer/`
- `inbox/store.py` — read/write/iterate items, state subdirs
- Refactor: `upstream_issues_watcher` writes Inbox items in addition
  to dispatching alerts. Bridge keeps catalog-driven notifications working.
- Admin UI: Inbox tab, list view, item detail with read-only display of
  the original comment + a fixed draft of "thanks for the details — investigating".
  No evo integration yet; the draft is a stub. Approve & Post round-trip
  works.
- Feature key renamed from `upstream_issues_watcher` → `issue_inbox`,
  with `issue_inbox.poller.enabled` as the polling sub-flag.
  Migration: read the old key and treat it as `issue_inbox.enabled`
  for one release, then drop.

**Phase 2 — Evo integration**:

- `packages/maintainer/evo_integration.py` — tools, prompt scaffold, analysis pass
- "Ask evo to revise" chat surface in the detail pane
- Telegram deep-link nudges to specific Inbox items
- LLM cost guardrails (per-pass budget, Haiku-by-default, model
  escalation logic)

**Phase 3 — Multi-repo subscription + per-user gh auth wiring**:

- `[+ Add repo]` flow with permission detection
- `tracked_repos.json` schema + UI for editing
- Single gh-identity model surfaced clearly in UI
- Rate limiting at the post-time guard

**Phase 4 — Inbound triage (maintainer-only)**:

- New `inbound_issues_watcher` polling new-issue creation on tracked
  repos where `track_all_issues=true`
- Verdict generation via evo's analysis pass
- Triage labels write tool (`gh issue label`)
- Spec/PR-draft tools

**Phase 5 — Auto-response policy**:

- Rule schema + storage
- Per-rule confidence/rate-limit enforcement
- 24h-undo path
- Audit trail Signals + Inbox done items

Each phase is roughly the size of one PR (or two for the bigger ones).
Total scope: ~3-4 weeks of part-time work, depending on how the UI
buildout sequences against other admin-UI sprints.

## Open questions

1. **Naming the subpackage**: `maintainer/` overstates the dev-tier
   association. `participation/`? `issues/`? `github/`? Pick during
   Phase 1.
2. **Single vs per-user gh identity**: v1 ships single-identity. When
   does multi-identity become necessary? Probably tied to whenever
   Evolve gains real multi-admin support, not before.
3. **Cross-install collaboration**: if two Evolve admins both track
   `evolve-ops/evolve`, they see overlapping Inboxes — fine, no
   coordination needed since neither owns the other's draft. But if
   they both draft a reply to the same comment, the second to post
   "wins". Acceptable v1 behavior; flag for Phase 2 if it becomes a
   problem.
4. **Issue-creation flow**: Phase 1 only covers responding to existing
   issues. "Create a new issue" via evo (the operator describes a
   problem, evo formats a proper bug report, operator approves)
   is implicit in Phase 2's evo integration but should be called out
   as an explicit UI affordance — "+ File new issue" button on each
   tracked repo's card.
5. **Spec-doc / PR-draft from Inbox**: Phase 4's `evolve_open_spec`
   and `evolve_open_pr_draft` tools blur into the broader
   "RSI proposal" pipeline. Worth a separate spike on whether Inbox
   items can become Proposal generator inputs, or whether they stay
   separate domains. Default: separate, since the verdict schema and
   approval path differ.

## References

- `docs/spec-upstream-issue-watcher-2026-05-22.md` — Phase 1's polling
  backbone (already shipped)
- `docs/spec-alerts-signal-store-2026-05-07.md` — Signal store shape
  the Inbox borrows from
- `docs/spec-alert-subscriptions-2026-05-10.md` — catalog/dispatcher
  the Inbox augments (alerts remain ephemeral; Inbox items are durable)
- `project_evolve_sandbox_design` — the deferred per-install config
  layer this spec is forward-compatible with
- `feedback_design_constraint_mildly_tech_capable` — Plex-test for
  every UI surface
- `feedback_rsi_low_cost_preference` — cost shape budget
- `feedback_message_style_team-bot-a_like` — Telegram nudge format
- openclaw/openclaw#84820 — motivating real-world scenario
