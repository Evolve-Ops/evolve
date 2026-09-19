---
title: "Help: Sending Feedback"
slug: feedback
audience: public
last_reviewed: 2026-07-28
concepts:
  - feedback
  - bug-reports
ui_surface: "admin.feedback"
related_specs: []
---

# Help: Sending Feedback

The **Send feedback** button in the admin UI turns "something's wrong" into a
GitHub issue with the environment details already filled in. There are two
kinds — **bug report** and **feature request** — and you always review the
issue before it's posted.

## What the button does

For a bug report, the pod first builds a diagnostic snapshot and saves it to
`~/.evolve/reports/report-<timestamp>.json` (the `evolve` user's home on a
pod; the Reports tab under Maintenance lists the same files). Then:

- **If a GitHub token is configured** (keystore slot `github_intake` — set it
  with `evolve-admin keys set github_intake`), the issue is filed directly via
  the GitHub API, labelled `bug` or `enhancement`, and the freshly filed issue
  opens in a new tab for you to confirm.
- **Otherwise**, a pre-filled new-issue page opens in your browser and you
  post it under your own GitHub account — no tokens ever live on the pod for
  this path. One quirk: with GitHub's issue-form templates only the title
  survives the URL pre-fill, so paste your description into the form.

Either way the issue body carries your note, the Evolve version and git
commit, the hostname, and the last few error lines from the admin log.

## The snapshot is not attached automatically

An issue can't carry a file by URL. If you want the full snapshot on the
issue — and for bugs you usually do — drag the saved `report-*.json` file
from `~/.evolve/reports/` into the issue's comment box before (or after)
posting. The issue body includes the file path to remind you.

## Where issues get filed

The target repo is auto-detected from the deploy checkout's `git remote
get-url origin` — so a pod installed from a fork files issues against that
fork, not upstream. To override, create `~/.evolve/feedback-config.json`:

```json
{ "github_repo": "owner/repo" }
```

(or use the in-app config link, which does the same thing). If neither the
config file nor the git remote resolves, the feedback flow reports itself
unconfigured rather than guessing a repo.

## What makes a good beta bug report

- What you did, what you expected, what actually happened.
- Roughly when it happened — the snapshot's log tail is time-stamped, so a
  time lets us line the two up.
- The page or command involved, and whether it reproduces.
- Attach the snapshot. It answers the first ten questions we'd otherwise ask.

For errors the pod itself caught, the [Errors page](errors.md) has a per-error
report flow that pre-selects the relevant crash.

## Privacy: what the snapshot does and doesn't contain

The snapshot **contains**: system info (hostname, user, OS and Python
versions, Evolve version and git commit), a filtered set of environment
variables (PATH, HOME and the like), admin-service status, the last 60
error lines and last 300 lines of the admin log, the last 100 lines of the
server log, the install record, and your pod configuration (`network.json`)
**with secrets redacted** — tokens, API keys, passwords, webhooks, and the
alert chat ID are replaced with `[REDACTED]`.

It does **not** contain chat transcripts, bot memory, or workspace files.

Two caveats: log lines are included verbatim, so anything an error message
happened to echo (a file path, a username) is in there; and the config still
shows structural details like bot names and ports. Nothing uploads on its
own — the snapshot only leaves the pod when you attach it, so skim the file
first if in doubt.
