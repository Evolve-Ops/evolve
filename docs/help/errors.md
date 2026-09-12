---
title: "Help: Errors Page"
slug: errors
audience: public
last_reviewed: 2026-06-23
concepts:
  - errors
  - crash-report
  - upstream-submission
ui_surface: admin.errors
related_specs: []
---

# Help: Errors Page

The Errors page (in the **Developer** bucket) collects the runtime errors and
crashes the pod caught while running — the admin UI, the background jobs, the
bot gateways. It's the place to look when something felt off ("the page hiccuped",
"a job didn't run") and you want to see what actually went wrong, and to send a
genuine bug upstream so it gets fixed.

**Errors stay on your pod by default.** Nothing here leaves the machine until
*you* decide to submit a specific error. When you do, it opens a draft in your
browser for you to review and post — you're always the one who hits send.

---

## What you see

A summary band across the top gives you the shape of things at a glance:

- **Captured errors** — how many error occurrences are in the window.
- **Unique signatures** — how many *distinct* problems those occurrences boil
  down to. The same error happening 200 times is one signature, not 200 things
  to read.
- **Last error** — when the most recent one landed.

Below that, a table lists each distinct error:

| Column | What it tells you |
|--------|-------------------|
| **Error** | The error message. Click the row to expand the full detail. |
| **Sev** | How serious — *Alert* (critical) or *Warn*. |
| **Module** | Which part of the pod raised it. |
| **Count** | How many times this same error has occurred. |
| **First seen** / **Last seen** | When it started and when it last happened. |
| **Status** | Where it stands — *New*, *Acknowledged*, *Submitted*, or *Won't fix*. |
| **Actions** | Acknowledge it, submit it upstream, or mark it won't-fix. |

Errors are grouped by **signature** — a fingerprint of the error and where it
came from — so a noisy, repeating failure shows up as one row with a rising
count instead of burying everything else.

### Filtering

Narrow the list by severity, by status, by how far back to look (1 hour up to
30 days), or by typing into the search box. **Clear all** hides every row
currently shown so you can start from a clean slate; it doesn't erase the
underlying log, and anything that occurs after that point still surfaces.

---

## Reviewing an error

Click a row to expand it. You'll see the full message and the captured detail —
enough to tell an expected hiccup (a service that was mid-restart when a request
landed) from a real defect. Two outcomes from there:

- **Acknowledge** — you've seen it and it's understood or harmless. The row
  moves to *Acknowledged* and stops drawing your eye.
- **Won't fix** — a known, accepted condition you don't want to keep seeing.

---

## Submitting an error upstream

When an error looks like a genuine bug in Evolve, you can send it to the
project's issue tracker. The flow is built so you never file a duplicate and
never post anything you haven't read:

1. Click the submit action on the error row.
2. A **pre-flight** check looks for existing reports of the same problem. If
   there's already an issue open, it points you there instead of opening a
   second one.
3. If it's new, Evolve assembles a draft — the error, its context, and a
   diagnostic snapshot — and opens **GitHub in your browser**.
4. You review the draft and post it yourself. Once posted, the row's status
   becomes *Submitted* so you know it's been reported.

Because the draft opens in your own browser and you're the one who posts, you
stay in control of exactly what gets shared.

---

## Common Questions

**Is this the same as the Reports → Alerts page?**
No. Alerts are *findings* the pod's monitors raise about configuration, cost, or
security — things to act on operationally. The Errors page is *crashes and
exceptions* the software hit while running. Alerts ask "is the pod set up
safely?"; Errors ask "did the code fall over?"

**An error has a high count — is that bad?**
Not necessarily. A high count means the same thing happened many times; whether
it matters depends on what it is. Expand the row to read it. A transient
network blip during a restart is harmless; a repeating failure in a job you rely
on is worth submitting.

**Do my errors get sent to Evolve automatically?**
No. They stay on your pod until you explicitly submit one, and even then you
review and post the report yourself. Nothing is phoned home in the background.

**I cleared the list — did I delete the errors?**
No. *Clear all* just hides what's currently shown so you can focus. The
underlying log is untouched, and new occurrences after that point still appear.
