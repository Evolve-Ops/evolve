---
title: "Help: Reports Page"
slug: reports
audience: public
last_reviewed: 2026-06-23
concepts:
  - reports
  - subscriptions
  - digest
  - watchlist
  - alerts
  - proposals
ui_surface: admin.reports
related_specs: []
---

# Help: Reports Page

The Reports page (in the **Operate** bucket) is your review desk. It answers two
questions: *what should the pod tell me, and through which channel?* and *what
has the pod noticed that I haven't looked at yet?* Everything here is about
seeing and deciding — none of it changes a bot on its own.

It has four sections, picked from the tabs across the top:

- **Subscriptions** — what the pod sends to your chat (the daily digest plus
  the per-event notifications you opt into).
- **Alerts** — findings the pod's monitors raised, shown only here in the admin
  UI.
- **Proposals** — concrete changes a coach wants to make, each one click to
  apply.
- **Watchlist** — things the pod is keeping an eye on but hasn't acted on yet.

**Ask evo from any tab.** Open the chat widget and describe what you're looking
at — *"what alerts are firing?"*, *"apply the plugin-baseline proposal"*,
*"mute the cost-spike notification on team-bot-a"*. Evo reads the same data
this page shows and can act on it for you.

---

## Subscriptions

Subscriptions are messages the pod sends to the Evolve bot's chat thread —
Telegram, Slack, Discord, wherever you talk to it. You read them in your normal
messaging app; you don't have to open the admin UI to get them. Some run on a
schedule (the daily **Pod Report** digest), and some fire only when an event
trips a threshold (a new audit finding, a cost spike, a backup that failed).

Three inner tabs:

### Messages

A mirror of what the pod actually sent — the same messages your phone received,
newest first. Tap a row to read the full body. Use this when you want to confirm
something went out (and to which channel) without scrolling back through your
chat app. Failed deliveries are summarized separately at the top.

### Configure

Two things live here:

- **Digest Delivery** — when the daily Pod Report goes out (frequency,
  time of day, which day for a weekly cadence) and how loud the all-clear case
  is. Set "Notify when" to *Always* and you get the digest even on a quiet day;
  set it to *Trending or Broken items only* and a clean day stays silent.
- **Notification Subscriptions** — pick which event types reach your chat and
  how often. Every type can be muted, throttled, or batched. The defaults are
  tuned for a typical pod; this is where you make it match your own tolerance
  for noise.

### Pod report thresholds

The sensitivity dials for the daily digest. The Pod Report watches for cost
spikes, session drop-offs, and a silent pod; these thresholds decide how big a
move has to be before it shows up in the digest. The first row sets the pod
default and each bot row overrides it — tighter values surface more, looser
values surface less. Leave a cell blank to inherit the default.

---

## Alerts

Alerts are findings the pod's monitors raised about your bots and the pod
itself — an unexpected config change, a gateway that keeps restarting, a cost
anomaly, a backup that didn't complete. They live **only here in the admin UI**.
They are *not* sent to your chat unless you subscribe to the matching event
under Subscriptions → Configure.

Three inner tabs:

- **Firing** — alerts that need attention now. A category strip across the top
  groups them by domain so a busy pod doesn't crowd everything into one list,
  and filter chips let you narrow by which monitor raised it, which bot it's
  about, and how severe it is. Select rows and use the bulk bar to snooze or
  dismiss a batch at once. "Group similar alerts" collapses the same finding
  fanned out across several bots into one row.
- **Snoozed** — alerts you've set aside; they wake themselves at the snooze-until
  time, or you can unsnooze one to bring it back to Firing immediately.
- **History** — the state-change log: when each alert fired, snoozed, resolved,
  or was dismissed.

Dismissing an alert asks you why — *false positive*, *bad inference*, or *not
actionable*. That answer is a tuning signal: it teaches the pod which findings
were noise so the next sweep is quieter.

---

## Proposals

Proposals are concrete, actionable changes a coach wants to make — adopt a
plugin, restore a config that drifted, add a spending cap to a cron. Each one
carries a typed action you can apply with a single click, or snooze, or dismiss.
Where Alerts shows you something the pod *noticed*, Proposals shows you something
to *do*.

Select several and the bulk bar applies, snoozes, or dismisses them together.
A proposal that's tied to an active alert doesn't show up here as its own row —
it appears as an inline **Act** button on the matching alert under the Alerts
tab, so you handle the finding and the fix in one place.

App-side "consider adding X" suggestions — the ones about what your bots could
*do* — are a different surface. Those live on the [Recommendations](recommendations.md)
page; this tab is for operational fixes to the pod's plumbing.

---

## Watchlist

The Watchlist is what the pod is keeping an eye on but hasn't acted on. Each row
is a signal that didn't yet clear the bar to become a Proposal. If one keeps
recurring and accumulates enough evidence, it graduates into a Proposal you can
review.

It's also where pod-wide patterns gather: when the same condition fires on three
or more bots, that's a hint the fix belongs in a default rather than on each bot
one at a time, and those candidates wait here for the pod to draft the right
change. The "recently dropped" view shows candidates the pod filtered out — a
useful place to look if you expected something to surface and it didn't.

You don't usually act on the Watchlist directly. It's a window into the pod's
reasoning before anything reaches your desk.

---

## Common Questions

**What's the difference between an Alert and a Subscription?**
An Alert is a finding shown in the admin UI; a Subscription is a message pushed
to your chat. They're separate on purpose: you might want every audit finding
visible in the UI but only the critical ones pinging your phone. To turn an
alert type into a chat notification, subscribe to it under Subscriptions →
Configure.

**I subscribed to an event but I'm not getting messages.**
Check Subscriptions → Messages first — if the dispatcher tried and failed, the
failure is summarized at the top of that tab (a common cause is the Evolve bot's
messaging channel not being reachable). If nothing was attempted, confirm the
event type is enabled (not muted or batched) under Configure, and that the
underlying source isn't switched off in Settings → Pod Config.

**The digest is too noisy / too quiet.**
Two knobs. To change *whether* a quiet day notifies you at all, use "Notify when"
under Configure. To change *what counts as worth reporting*, tighten or loosen
the dials under Pod report thresholds — lower thresholds surface smaller moves.

**What's the difference between Proposals here and the Recommendations page?**
This tab carries operational fixes to the pod itself (plugin baselines, config
drift, cron caps). The Recommendations page carries suggestions about what your
bots could do for you (new apps, behavior tweaks). Same approve/snooze/dismiss
rhythm, different subject.

**Why is something on the Watchlist instead of in Proposals?**
It hasn't earned a proposal yet — it fired once, or its magnitude was below the
threshold, so the pod is watching to see if it's a pattern or a blip. When the
evidence adds up, it promotes itself to a Proposal automatically.
