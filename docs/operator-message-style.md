# Operator Message Style

This is the shared style guide for every message that reaches the
operator's primary chat channel — alerts, reports, check-ins, security
findings, cost notices, anything. It applies whether the message is
emitted by:

- The Python alerts dispatcher (via catalog `body_template` + `ActionOffer`),
- An LLM-driven Evolve app (e.g. `security-cve-scan`),
- An ad-hoc daemon that calls `dispatcher.send` directly.

A message that violates this style is a bug, not a stylistic
preference. The operator's attention is finite; messages that waste
it train the operator to ignore the channel, which is how real
problems get missed.

---

## The five qualities

Every operator-facing message must be:

1. **Short.** Operators read on a phone, between other tasks. Five
   lines is plenty; ten is the upper bound. If you can't fit it in
   ten lines, the message is doing two jobs — split it or drop one.

2. **Plain-English first.** The first line after the header is one
   sentence in plain English explaining what's going on. No CVE
   numbers, no field names, no jargon. The operator should
   understand the situation without scrolling, clicking, or grepping.

3. **Honest about what it is.** State the type explicitly:
   - **Action needed** — operator must do something. Say what.
   - **Informational** — operator should know, no action. Say so.
   - **Resolved / cleared** — something previously needing attention
     is now fine.
   - Do not blur these. "FYI" buried inside an alert that looks
     like it needs action is the worst case.

4. **Concrete next step (or explicit "none").** If action is needed,
   give the operator one command, one UI breadcrumb, or one reply.
   Pick the lowest-effort path. If no action is needed, write "No
   action needed." — do not leave them guessing.

5. **No noise.** No spurious "all clear" pings between events. No
   duplicate sends. No "Reply 'foo' in the Evolve bot conversation"
   when the message is already in that conversation. No repeating
   fields the operator already knows. Silence is the default; speak
   only when something is worth saying.

   **Summaries are the exception.** Scheduled daily/weekly reports
   (pod_report, weekly_rsi_review, weekly cost summary) are the
   product, not noise — operators chose to subscribe. They may
   include "All clear" sections when relevant. The "no all-clear"
   rule applies to event-driven alerts (audit scan, security CVE
   scan, etc.) where firing on a quiet day trains the operator to
   ignore the channel.

---

## Format

```
{emoji + short title}
{one-sentence plain-English summary}
{1–4 lines of context as needed}
{action line: a command, a reply, a UI path — or "No action needed."}
```

A worked example (CVE alert, after refactor):

```
⚠️ Security finding — CVE-2026-43584
A privilege-escalation flaw in OpenClaw could let an attacker
override safe-mode environment denials.
Affects: OpenClaw < 2026.4.10
Installed: OpenClaw 2026.4.29 — not affected.
No action needed.
```

vs. what we used to send:

```
⚠️ Security Scan — 2026-05-11 — ACTION NEEDED
⚠️ WARN CVE-2026-43584
Source: https://github.com/advisories/GHSA-xrgf-r9gr-jjjf
Affects: OpenClaw < 2026.4.10 (env var denylist — VIMINIT, EXINIT,
LUA_INIT, HOSTALIASES; CVSS 8.7 High)
Installed: 0.3.0

Reply 'ack-cve CVE-2026-43584' in the Evolve bot conversation to
acknowledge.
```

The second message fails on every count: scary header for a non-issue
(the install isn't affected), CVE jargon up front, no plain-English
summary, version-confusion (`0.3.0` is the wrong field), redundant
prepositional phrase in the reply.

---

## Headers

The header line conveys what the message is at a glance. Pick
**exactly one** emoji from this set:

| Emoji | When to use |
|---|---|
| `🔴` | Critical — operator should act now |
| `⚠️` | Warning — operator should look soon |
| `🟢` | Recovery — something previously alerted is now fine |
| `🛡️` | Security event (non-critical; e.g. config drift, key rotation due) |
| `💰` | Cost event (non-critical; e.g. weekly summary, daily threshold) |
| `📊` | Summary / report (pod report, weekly review) |
| `📋` | Decision needed (review queue, proposal, audit batch) |
| `🔄` | Update available (software, repo, plugin) |
| `🔧` | Maintenance / ad-hoc report |
| `⚡` | Breaker tripped — system-state change (auto-turns paused) |

**Pick severity first when there's a real action to take now.** A
gateway-down event is `🔴`, not `🛡️` or `💰`, even though it's a
system/cost issue too — the operator's first read is the urgency.
For non-actionable events in a clear category (cost summary,
weekly review, update notice), lead with the category emoji.

**`⚡` is narrow on purpose.** Only breaker-trip events use it. A
breaker trip changes how the system behaves (auto-turns disabled,
exec passthrough degraded), which is distinct from a generic
critical alert that doesn't change system state. The circuit-breaker
visual semantic matches the mental model. Don't use `⚡` for other
events — `🔴` is the catch-all for emergencies that need action.

**Do not stack emojis.** No `🛡️ 🔴` or `💰 ⚠️`. One per header.

**Do not invent new ones.** No `🚨 ⏰ 🚧 🟡 🔽` in catalog
templates — they fragment the operator's glance-recognition. If
nothing in the approved set fits, the style guide is wrong and
should be updated, not the message. (2026-05-28: `⚡` was promoted
from disallowed → approved specifically for breaker-trip events
after the Security-Bot trip incident showed `🔴` alone wasn't visually
distinct from spend-cap alerts.)

Catalog enforcement: ``tests/test_alerts_catalog.py`` asserts that
every catalog event's ``body_template`` starts with an approved
emoji. New catalog events that violate this fail CI.

---

## Common mistakes

- **Daily "all clear" messages.** Train the operator to ignore. If
  there's nothing to say, say nothing. The log file is the audit
  trail.
- **CVE / error code as the first line.** The operator doesn't know
  if `CVE-2026-43584` matters. Lead with what it *does*, then cite
  the code.
- **"Action needed" when no action is possible.** If the issue
  affects a version that isn't installed, that's *not actionable* —
  skip the alert entirely (or render as informational).
- **Pasting raw status data.** `CVSS 8.7 High` is data; "A
  privilege-escalation flaw" is information. Translate.
- **Long reply instructions.** `Reply 'mute CVE-2026-43584'` is
  enough. The operator knows where to reply.
- **Bundling cost alerts with security findings.** Different urgency,
  different visual treatment. Use the right catalog event for each.

---

## Where to enforce this

- **Python dispatcher path.** The catalog (`packages/admin/evolve_admin/alerts/catalog.py`)
  is the single source of truth for `body_template` and
  `ActionOffer`. New catalog events must follow this guide.
  `tests/test_alerts_catalog.py` enforces the emoji constraint via
  `test_body_templates_start_with_approved_emoji`.
- **LLM-driven Evolve apps.** The LLM never composes the
  operator-facing message directly. The Python harness around the
  LLM call renders it from structured output, using this guide as
  the spec. See `packages/analyzer/evolve_apps/security-cve-scan/`
  for the canonical pattern: LLM produces JSON candidates, Python
  applies discipline + renders the message.
- **Ad-hoc emitters.** Route through `dispatcher.send` with a
  `catalog_event` so the rendering is consistent. If no catalog
  event fits, add one — don't reinvent the format inline.

### Known source-side drift (deferred follow-up sweep)

A number of non-catalog emitters still hand-render messages with
disallowed emoji (`🟡`, `⏰`, `🚨`, `⚡`, `🔽`). They predate this
guide and are scheduled for a future sweep PR. Until then, the
catalog is the only path with enforcement:

- `pod_report.py` — uses `🟡` in chip rendering, `📋` as info marker
- `weekly_review.py` — uses `🟡` as health-score icon
- `analyze.py` — uses `🔴`/`🟡` per-proposal confidence indicator
- `heal.py` (some paths) — uses `🟡` for watchdog body lines
- `signal_notifier.py` — `_EMOJI_BY_SEVERITY` map includes `🟡`
- `spend_alert.py` — `🔽` as "downgraded tier" marker
- `cron_alert.py` — `⏰` in stalled-cron messages

When sweeping a source, prefer routing the render through the
catalog rather than fixing the emoji inline.

## Reformat opportunities (catalog audit notes)

The current catalog has ~15 events whose body templates read as
data dumps rather than plain-English sentences (e.g.
`security.audit_finding` renders `"team-bot-a: gateway loopback auth
missing"`). Promoting these to one-sentence summaries requires
changing the emitter's payload shape, so they're left for
per-source PRs rather than a single bulk rewrite. The catalog
emoji+structure constraint is the bar this PR enforces; the
plain-English bar is the next sweep.

---

## Why this matters

Operators ignore noisy channels. A single false-positive trains the
reader to discount the next message slightly; enough of them and the
channel becomes background noise. By the time a real CRITICAL fires
the operator has already learned not to look. The discipline above
is the cheapest insurance against that drift.
