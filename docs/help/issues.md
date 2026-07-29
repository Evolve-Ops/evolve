---
title: "Help: Issues Page"
slug: issues
audience: public
last_reviewed: 2026-06-06
concepts:
  - issues
  - intake
  - intake-drafts
  - issue-tracking
  - triage
  - auto-response-policy
ui_surface: admin.inbox
related_specs:
  - docs/spec-issue-inbox-2026-05-22.md
---

# Help: Issues Page

The Issues page (in the **Improve** bucket, sidebar entry **❗ Issues**) is where you manage GitHub issues that involve Evolve — drafts evo captured for you, issues you've already filed and want to follow, and the repos being watched for replies.

It was called **Inbox** until 2026-06-04; the rename keeps the data and endpoints intact but matches what operators actually use it for. Internal IDs in the JS still read `inbox-…` for back-compat.

**Ask evo to file or check.** Most of this page is reachable from chat:

- *"file this as a bug"* / *"evo improve <description>"* — evo captures an intake and shows the draft. No GitHub call yet.
- *"show me my drafts"* — lists pre-promotion intakes; evo can also dismiss or promote one for you.
- *"any unread replies on the evolve issues?"* — evo reads the activity log via `pod_state.intake`.

Drafts never auto-file. Promotion is always explicit, either from chat or from this page.

---

## Summary band

Four tiles across the top:

- **Drafts** — captured-but-not-yet-filed intakes (open + triaged).
- **Filed issues** — intakes that have been promoted to GitHub.
- **With unread activity** — filed issues whose threads have new comments or state changes you haven't acknowledged.
- **Tracking is automatic** — note that the watcher polls GitHub every ~15 min once an intake is filed.

The summary tiles also feed the sidebar's **❗ Issues** badge count, so unread replies are visible from any tab.

---

## Drafts card

Sits at the top of the page when at least one draft exists. Each row is a pre-promotion intake — the `evo intake` or `evo improve` flow captured a title and body but you haven't filed it yet. Drafts live at `{shared_dir}/intake/open/` and `triaged/`.

Each row shows the title, kind (bug / feature / question), creation time, and two buttons:

- **Promote →** — opens the promote flow. Picks the default tracked repo (or prompts for one if multiple are configured) and creates the GitHub issue using your `github_intake` token. The intake moves to the **Filed issues** table below.
- **Dismiss** — drops the draft. No GitHub call.

Click a row title to open the **draft-detail modal** before promoting — it shows the full body evo wrote so you can verify the issue is well-framed. The modal has Promote and Dismiss buttons too.

**Where drafts come from.** Two ways:

1. **`evo intake`** — the conversational front door. You describe a problem to evo, the classifier picks one of `local_env` / `evolve_code` / `upstream` / `mixed`, and if it's filable evo drafts a structured body. Cheap (~$0.001/call).
2. **`evo improve <description>`** — like `intake` but framed as "make this better." Same classifier, same draft format.

You can also iterate on a draft inline with **`evo revise <intake_id> <instruction>`** — "make it more concise," "add a stack trace section," "reframe as a feature request." Each revision is appended to the draft's history. **`evo revise <intake_id> --undo`** rolls back one revision without an LLM call.

---

## Tracked repos card

Below Drafts. Lists every repo the watcher is following — adds, removes, and at-a-glance permission status. Each row shows the owner/repo, an identity chip (`as @<your-login>`), and a maintainer-tier badge (`maintainer` / `triage` / `read-only` / `not a collaborator`).

- **+ Add repo** opens a modal: owner, repo name, optional target name, and a keystore slot for the GitHub token (defaults to `github_intake`). Tick **Make this the default target** to send un-suffixed `evo intake promote` calls here.
- **Remove** (per row) — destructive; confirms before deleting. If the removed target was the default and others exist, another is promoted to default automatically.
- The first time you add a repo on a fresh install, an inline "Set up GitHub token" prompt appears. The modal walks you through which PAT scope to pick: `repo` for private repos you own, `public_repo` for public upstreams only, or a fine-grained PAT with `Issues: read+write` + `Metadata: read` on the specific repos.

**Suggestions strip.** Below the list, quick-add chips for repos that are likely candidates (Evolve's own + the OpenClaw upstream) appear when they're not already tracked.

### Inbound triage watcher pill

Sits at the bottom of the Tracked repos card, hidden until at least one repo is tracked. The watcher (`inbound_issues_watcher` LaunchDaemon, 15-minute poll) reads issues filed by **others** on tracked repos you maintain and runs the triage classifier against each one — see the Triage queue card below.

- **Status indicator** — off / enabled-but-not-installed / running, with a "last ran 4m ago" hint.
- **Toggle** — one click. Enabling it shows a confirmation dialog warning you that the watcher polls third-party repos and needs the same `github_intake` PAT as the promote flow.
- **Expandable install log** — surfaces the actions taken when you flip the toggle (which plist was installed, which command launchctl ran). Useful when something doesn't start cleanly.

The watcher only makes sense for pods that maintain repos other people file issues against. Standard installs that just use Evolve don't need it; the default is off.

---

## Filed issues table

The middle section. One row per intake that's been promoted to a GitHub issue. Sorted by most-recent activity first.

| Column | Meaning |
|--------|---------|
| (dot) | Colored dot when the thread has unread activity. |
| Issue | `repo#number — title`, links to GitHub. |
| Kind | `bug` / `feature` / `question` (from the classifier). |
| Latest activity | Most recent watcher-observed event with relative timestamp. |
| Filed | When you promoted the draft. |
| Actions | `Mark as seen` (clears the unread dot) and `↗ GitHub` (opens the issue). |

**Unread only** checkbox filters down to threads with new activity. **↺ Refresh** re-fetches from `/api/inbox`.

Click a row to open the issue's detail panel — original report body, full activity log (💬 comment / ↻ state change / ✓ closed / ↺ reopened) with snippets and actors, and the **Mark as seen** button. Unread events get an accent border.

The watcher (`upstream_issues_watcher`, 15-minute poll) does the activity tracking; you don't have to subscribe to GitHub notifications.

---

## Triage queue card

Visible only when you maintain at least one tracked repo AND there's at least one inbound issue to triage (i.e., the inbound watcher is enabled and has found something). Hidden otherwise so non-maintainer pods see a clean page.

Each row is an issue someone else filed against one of your repos, with the LLM triage verdict:

| Column | Meaning |
|--------|---------|
| Urgency | `p0` / `p1` / `p2` / `p3` / `unknown` — color-coded. |
| Category | `bug` / `feature` / `question` / `noise` / `unknown`. |
| Issue | `repo#number — title`. |
| Author | The user who filed it on GitHub. |
| Recommendation | What the classifier suggests: `auto_close_duplicate` / `reply_clarifying` / `label_only` / `maintainer_review` / `unknown`. |
| Triaged | When the verdict was produced. |

Filter the list by urgency (`p0` only, `p0+p1`, `p0+p1+p2`, or all). Click a row to see the LLM's reasoning, draft reply (rendered as plain text — these come from external users, treated as untrusted), and suggested labels.

The triage detail offers an **Apply** control showing the recommended action. Apply is operator-initiated — it bypasses the auto-response policy gates (manual = approved). For verdicts without an actionable recommendation, a secondary **labels-only** lane is available.

### Auto-response policy

A collapsible panel below the triage queue. Default is **disabled** — every action requires you to click Apply. Most pods will want to leave it that way for a couple of weeks before opting in to anything.

When you expand it:

- **Enable auto-responder (global)** — master switch. Off → nothing fires automatically.
- **Close duplicates** + minimum confidence (default 0.9) — auto-post a citation comment, then close the issue as `not_planned`.
- **Post clarifying replies** + minimum confidence (default 0.85) — post the draft reply as a comment without changing issue state.
- **Label only** + minimum confidence (default 0.7) — apply the draft labels with no other action.

Each fired auto-action gets a **24-hour undo window**. The triage row shows an "Auto-acted ↺ Undo" pill while the deadline hasn't passed; click Undo to reverse the action (re-opens an issue + deletes the comment in the close-as-duplicate case; deletes the comment in the reply case; removes the labels in the label-only case). After 24 hours the action becomes permanent and the pill reads "permanent."

The auto-responder runs as a batch job — every couple of minutes it scans inbound intakes and applies any matching the policy gates. Confidence floors prevent the LLM from acting on guesses; the close-duplicate floor is the highest because the action is the most disruptive.

---

## Common questions

**I told evo to file a bug and got an intake ID, but nothing was on GitHub — where is it?**
Drafts never auto-file. Open this page, find it in the Drafts card, and click **Promote →** when you're ready. Evo's intake flow is deliberately a two-step "capture, then approve" — the GitHub call is the moment of commitment.

**The Drafts card isn't showing — does that mean I have no drafts?**
The card hides itself when there are zero drafts in the open or triaged state. If you ran `evo intake` and the card is empty, the call may have failed or evo captured it under a different bot — check the chat reply for the intake ID and the bot it was filed against.

**Why is my Tracked repos card empty after a fresh install?**
By design. Evolve doesn't track any GitHub repo by default — the `+ Add repo` flow is opt-in, and the quick-add chips below the list show suggestions (typically Evolve's own + OpenClaw upstream) when nothing is configured yet.

**I added a tracked repo but the row shows "PAT scope too narrow" — what happened?**
Classic PATs can't read private repos without the full `repo` scope, even when the token owner owns the repo (GitHub returns 404 across the board, which surfaces as scope drift). Replace the `github_intake` slot with a PAT that has `repo` (private repos) or `public_repo` (public-only) and re-add. The setup modal links straight to the right GitHub token-creation page for each variant.

**The triage queue is empty even though I have inbound issues — is something broken?**
Three things to check: (1) the inbound triage watcher pill is **enabled** (not just installed); (2) the most recent run shown next to the pill isn't stale; (3) at least one tracked repo has inbound issues filed by someone other than you. The queue silently hides itself when these aren't all true.

**An auto-action fired that I didn't want — can I undo it?**
Yes, for 24 hours after the action. The triage detail card shows "Auto-acted • Undo" until the deadline passes. Undo reverses GitHub state in the inverse order it was applied — re-opens the issue first, then deletes the citation comment, so you never leave an orphaned "closing as duplicate" message on an issue you couldn't close. After the 24-hour window the action becomes permanent; you'd have to act on GitHub manually.

**Where do I configure the auto-response confidence floors?**
The Auto-response policy panel below the triage queue. Each action kind has its own floor; the close-duplicate floor is highest by default (0.9) because the action is the most disruptive. Lower the floor to fire on weaker verdicts; raise it to be more cautious. Setting all three checkboxes off makes the auto-responder a no-op even if the global toggle is on.

**I rejected a triage recommendation — does the classifier remember?**
Not today. The triage classifier doesn't learn from per-issue rejections. If a particular category of inbound issue keeps getting mis-classified, file a bug against Evolve via `evo intake` — the classifier prompt is something we can tune.
