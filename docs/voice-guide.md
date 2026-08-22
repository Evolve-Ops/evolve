# Evolve — Voice Guide

*Last updated: 2026-06-26*

Authoritative reference for the **words** Evolve shows the operator. This is the
sibling of [style-guide.md](style-guide.md): that doc is the law for pixels, this is
the law for words. When you write or change any operator-facing string — an alert,
a report, a recommendation, a finding, a subscription label, an app blurb, an
instruction line — check it against this guide first. A string that violates this
guide is a **bug**, not a stylistic preference: the operator's attention is finite,
and copy that makes them learn Evolve's internal vocabulary to use the product
trains them to ignore the surface.

This guide is the single source of truth that the forthcoming **voice card** (a
prompt fragment injected into the LLM calls that already happen) and **jargon lint**
(an author-time check) both read. The glossary in §3 is machine-parseable on
purpose — keep it that way.

Spec: `spec-plainlang-2026-06-26.md`. It inherits and
generalizes three existing principles — read them once:
[principle-plex-test.md](principle-plex-test.md) (who the operator is),
[operator-message-style.md](operator-message-style.md) (the chat-message expression),
and [principle-alerts-explain-and-remediate.md](principle-alerts-explain-and-remediate.md)
(explain + remediate, in plain English).

## Contents

1. [Audience & register](#1-audience--register)
2. [Principles](#2-principles)
3. [The jargon → plain glossary](#3-the-jargon--plain-glossary)
4. [Before / after examples](#4-before--after-examples)
5. [How this gets enforced](#5-how-this-gets-enforced)
6. [References](#6-references)

---

## 1. Audience & register

The operator is a **capable non-engineer who runs a pod of bots**. They know their
bots, their business, and what they want from Evolve. They do **not** know Evolve's
internal labels — "Signal", "generator", "arbiter", "coalesce key", "fingerprint",
"charter", "manifest", "gateway", "sweep-resolve" — and they should never have to.
This is Marcus, the persona from [principle-plex-test.md](principle-plex-test.md):
if a surface he routinely sees isn't usable without Stack Overflow, an LLM lookup,
or grep, it's a bug.

There are two registers, divided by **what the reader is trying to do**:

### Tier A — the everyday operator (the default; plain language mandatory)

The text the operator meets without opting into a deeper view:

- **Alerts** (the Signals/Alerts page and chat notifications)
- **Reports** (pod report, weekly review, cost summaries)
- **Recommendations & findings** (the RSI inbox)
- **Subscriptions** (what each alert category does, in operator terms)
- **App gallery** (app names, descriptions, capability blurbs)
- **Instruction & explanation copy** throughout the app (tooltips, banners, wizard
  steps, settings labels)

Plain language is **mandatory** here. No internal label appears unless it is *also*
a visible UI noun the operator must recognize — and then it is glossed once. No code
identifiers, no file paths, no bare acronyms.

### Tier B — the hands-on admin doing surgery (technical terms OK)

The text a reader meets only after opting into a technical task:

- Operator CLI output (`evolve-admin …`)
- Diagnostic dumps, the Signals page **detail** view, raw event views
- Remediation runbooks (gateway restarts, `launchctl`, ACL repair)
- The Maintenance page status detail, API responses, the MCP-tool layer
- Logs, spec docs, source code, internal comments

Technical terms are acceptable here **because the task is technical** — the reader
has chosen to do plumbing and needs the exact name of the pipe. **Do not dumb Tier B
down.** But two Tier-A habits still apply: **expand an acronym on first use**
(`ACL` → "ACL (file permissions)") and **lead with what the command does** before
the invocation.

The dividing question for any string: *does the reader need to understand Evolve's
internals to do their job on this surface?* For Tier A the answer must be **no**. If
you can't tell which tier a string is, it's Tier A — the default protects the
operator.

---

## 2. Principles

Six rules. They are the spec's Pillar 1, stated as edits you can make.

1. **Functional, not mechanical.** Describe what a thing means *for the operator*,
   not the mechanism that produced it. "A protected file changed outside the normal
   update" — not "config drift detected by the heal baseline diff."

2. **Name what they see, not the internal label.** Use the operator-facing noun. The
   thing the operator talks to is "the bot's connection to Telegram," not "the
   gateway." The thing that raised an alert is "Evolve's automated check," not "the
   generator" or "the producer."

3. **Lead with impact and the next step.** Open with what changed for them and what
   to do, *then* the why. (This is [principle-alerts-explain-and-remediate.md](principle-alerts-explain-and-remediate.md):
   what is it / what triggered it / what's the impact / what to do.) Never open with
   an error code or a field name.

4. **Expand or avoid acronyms.** Never a bare `ACL`, `DwD`, `RSI`, `CVE`, `OC`, `RPC`
   to a Tier-A reader. Expand it ("file permissions") or drop it. In Tier B, expand
   on first use.

5. **No code identifiers in operator text.** No `coalesce_key`, `human_title`,
   `proc_user`, snake_case, file paths (`/Users/<bot>/.openclaw/openclaw.json`),
   class names, or service labels (`ai.openclaw.updater`) in Tier-A copy. The storage
   schema is implementation; the label is product. They diverge — that's the work.

6. **Short sentences; concrete numbers.** State the trigger in plain English with the
   real figure. "$8.06 today vs $0.95 yesterday" is plain *and* exact. "Threshold
   exceeded" is neither. Five lines is plenty for a chat alert; ten is the ceiling.

---

## 3. The jargon → plain glossary

The heart of the cheap mechanism. The left column is the internal term as it leaks
into operator copy today; the right column is the plain Tier-A phrasing. Every entry
traces to a real string found in the codebase (see §4 and the PR body). The glossary
**grows from real fixes, not speculation** — each surface pass adds the terms it
actually hit.

Keep these tables a clean two-column shape (`Internal term | Tier-A plain phrasing`)
— the voice card and the lint both parse them. "(drop)" means the term is internal
plumbing with no operator-facing equivalent: remove it, don't translate it.

### Alerts, monitoring & the RSI inbox

| Internal term | Tier-A plain phrasing |
|---|---|
| Signal | alert / notice |
| signal producer / producer | the check that raised it (or drop) |
| monitor | automated check |
| watchdog | (drop) — "automated check" / "the monitor" |
| daemon | background service |
| generator | (drop the label) — "Evolve's review of …" / "an automated check" |
| investigator | automated check / "Evolve will look into …" |
| charter | what this check looks for |
| arbiter | (drop) — internal |
| proposal | recommendation (act) / finding (just so you know) |
| approved / pending / applied (proposal stage) | (drop the stage label) — "waiting to be applied" / "done" |
| RSI | (drop) — "Evolve's self-improvement" if the concept is needed |
| signal-producer dark / silent | an automated check stopped reporting |
| emit (a Signal) | raise (an alert) |
| observe / observation stream | what Evolve has noticed / its records |
| coalesce / coalesce key | grouped / combined (so you see it once, not 20×) |
| fingerprint / signature | (drop) — internal duplicate-detection detail |
| subscribe / subscriber | runs automatically when … |
| sweep-resolve / sweep | clears itself once the condition is gone |
| severity (ERROR/WARN/CRITICAL string) | how serious it is, in words: "needs attention now" / "worth a look" |

### Security, permissions & files

| Internal term | Tier-A plain phrasing |
|---|---|
| ACL | file permissions |
| auth hygiene | credential safety |
| credential / token | password / key |
| gateway.auth / auth token | the connection's password |
| loopback / bound to loopback | local-only (only this machine can reach it) |
| exec policy / exec | the bot's allowed actions |
| allowlist | the list of actions the bot is allowed to take |
| capability | what the app (or bot) is allowed to do |
| preflight / syntactic preflight | the safety check before an action runs |
| exec approval / approval timed out | a request waiting for your OK / a request that expired before you answered |
| EACCES / permission denied (raw) | Evolve couldn't read/write the file (permission) |
| CVE | security vulnerability (expand the ID with a plain summary) |
| bot user / proc_user | the bot's account |

### Apps, deploy & versions

| Internal term | Tier-A plain phrasing |
|---|---|
| manifest | the app's definition (what the app is and does) |
| deploy / deployed version | update / the version your bots are running |
| drift / out of sync | has changed from the expected setup |
| baseline | the saved "known-good" setup |
| admin server / admin daemon | Evolve's control service |
| module cache / git pull / reload code | (drop) — "Evolve hasn't picked up the update yet" |
| idempotent | safe to run again |
| quarantine / apply-results | (drop) — internal recovery detail |
| upstream / upstream-version drift | a newer version of the bot software |
| OC / OpenClaw | the bot software |
| auto-updater / updater state file | the bot software's auto-update |

### Connections, the "evo" path & scope

| Internal term | Tier-A plain phrasing |
|---|---|
| gateway / gateway plugin | the bot's connection (to Telegram, Slack, …) |
| transport | the channel (Telegram, Slack, Discord, …) |
| keyword path / evo keyword path | the "evo" command path |
| cookieless RPC / black-box probe | (drop) — internal connection-test detail |
| device-auth gate / 401 | the sign-in check rejected the connection |
| launchd / launchctl / LaunchAgent / service label | background service (drop the label in Tier A) |
| pod | your Evolve install (the machine running your bots) |
| pod-wide / fleet-wide | across all your bots |

---

## 4. Before / after examples

Each pair is a real string found in the Alerts surface (path cited), rewritten to the
standard. These are the evidence the glossary grew from.

**1. Audit finding (data dump → sentence)** — `alerts/catalog.py:476`

> Before: `🛡️ New security finding` / `team_bot_a: gateway loopback auth missing`
>
> After: `🛡️ Security finding on Team Bot A` / `Team Bot A's connection has no password set, so anything on this machine could control it. Set one on the Security page.`

Why: `gateway`, `loopback`, `auth` are all internal; the bot id is raw; there's no
impact and no next step.

**2. Config drift** — `alerts/catalog.py:550`

> Before: `🛡️ Config drift on admin_bot` / `A protected configuration file changed outside the deploy flow.` / `Drift: openclaw.json modified outside deploy`
>
> After: `🛡️ Admin Bot's settings changed unexpectedly` / `One of Admin Bot's protected setting files was changed outside Evolve's normal update. If that wasn't you, review it on the Security page.`

Why: "drift", "deploy flow", and the `openclaw.json` filename are all jargon; lead
with what changed and what to check.

**3. Tool denied repeatedly** — `exec_outcome_watchdog.py:666`

> Before: `team_bot_a: tool \`web_search\` denied 14× over 7d`
>
> After: `Team Bot A tried to use Web Search 14 times this week and was blocked every time.`

Why: backtick code identifier, `×`/`d` shorthand, no statement of why it matters.

**4. Tool-denial explanation** — `exec_outcome_watchdog.py:687`

> Before: `manifest declares this capability but the allowlist hasn't been updated`
>
> After: `The app is set up to do this, but the bot isn't allowed to yet — approve it on the Permissions page.`

Why: "manifest", "capability", "allowlist" are three internal terms in one line.

**5. Deploy drift** — `deploy_drift_monitor.py:213`

> Before: `Out of sync (deployed an older version)`
>
> After: `Some bots are running an older version of Evolve than the rest.`

Why: "out of sync" + "deployed" describe the mechanism; name the state the operator
sees.

**6. Stuck proposal** — `stuck_proposal_monitor.py:119`

> Before: `3 approved proposals stuck >7d without progressing`
>
> After: `3 approved recommendations have been waiting more than a week to take effect.`

Why: "proposal" → "recommendation" (already the `rsi` reframe), "progressing" →
plain.

**7. Stuck-proposal remediation (Tier B is OK here, but expand)** —
`stuck_proposal_monitor.py:140`

> Before: `Check \`apply-results/\` for an old quarantine that's blocking idempotent re-apply`
>
> After (Tier B, since this is a remediation step): `An earlier failed attempt may be blocking the retry. Clear the leftover entry in \`apply-results/\` and Evolve will safely try again.`

Why: this is a hands-on fix, so the path can stay — but "idempotent re-apply" becomes
"safely try again."

**8. Monitor silent** — `monitor_coverage.py:616`

> Before: `RSI signal-producer dark: model_discovery — recommendations stalled`
>
> After: `One of Evolve's automated checks (model updates) stopped running, so you may be missing some recommendations.`

Why: "RSI", "signal-producer", "dark", and the snake_case check name all leak.

**9. Evo path down** — `evo_path_probe_monitor.py:451`

> Before: `A black-box probe replicating the gateway plugin's cookieless call to /api/evo/dispatch failed`
>
> After: `The "evo" command stopped responding, so asking Evolve from your bot chat won't work right now.`

Why: "black-box probe", "gateway plugin", "cookieless", and the API path are pure
internals.

**10. Gateway auth missing (audit)** — `audit.py:2676`

> Before: `The bot's OpenClaw control gateway is bound to loopback but has no \`gateway.auth\` token configured`
>
> After: `This bot's connection has no password set. It only accepts connections from this machine, but adding a password closes the gap.`

Why: "OpenClaw", "control gateway", "loopback", and the `gateway.auth` field name are
all internal; keep the precise-but-plain impact.

**11. OC auto-updater silent** — `oc_substrate_monitor.py:223`

> Before: `OpenClaw auto-updater silent for 240 min`
>
> After: `The bot software hasn't checked for updates in about 4 hours.`

Why: "OpenClaw", "auto-updater", "silent", raw minutes → "bot software", "checked for
updates", humanized duration.

---

## 5. How this gets enforced

This doc is the authored law. Two mechanisms apply it at **near-zero ongoing cost** —
they are separate follow-on work (spec §5, bites B2/B3); this doc ships first as
their source of truth.

- **Voice card (for LLM-generated text).** A compact prompt fragment, single-sourced
  from one helper, restating §2 and the highest-value §3 swaps inline. It is injected
  into the system prompt of LLM calls that *already happen* (the prose-emitting checks,
  the evo assistant). Marginal cost = the snippet's tokens — **no new LLM calls, and
  no post-hoc rewrite pass.**

- **Jargon lint (for Python/static text).** An author-time check that reads the §3
  glossary and flags banned terms appearing in Tier-A operator-facing string literals.
  Warn-tier first (like the UI lint's hybrid severity), tightening to block on the
  worst offenders once the false-positive shape is understood. No runtime cost at all.

`plainlang` **owns** this guide, the glossary, the voice card, and the lint rule. It
**routes** the per-surface copy edits to each surface's owner (`reports`, `rsi`,
`apps`, `ui`, `evo-asst`) — it supplies the standard and tooling; the owners apply it
to their words.

**Seam with `ui`:** `ui` owns how and where text is *presented* (the explainer-banner,
width caps, placement). `plainlang` owns whether the *words* are plain. The lint may
live inside `tools/ui-style-lint` (shared tooling), but the rule and glossary belong
to `plainlang`.

---

## 6. References

- `spec-plainlang-2026-06-26.md` — the aspect charter
  (problem, the no-ongoing-LLM-cost constraint, tiers, the three pillars).
- [principle-plex-test.md](principle-plex-test.md) — who the operator is (Marcus); no
  internal jargon in primary surfaces.
- [operator-message-style.md](operator-message-style.md) — the chat-message expression
  (short, plain-English-first, honest, one next step, no noise); CI-enforced for the
  catalog.
- [principle-alerts-explain-and-remediate.md](principle-alerts-explain-and-remediate.md)
  — explain + remediate, in plain English.
- [style-guide.md](style-guide.md) — the law for pixels; this guide is its sibling for
  words.
- `packages/admin/evolve_admin/alerts/catalog.py` — operator alert body templates.
- `packages/analyzer/` signal producers (`exec_outcome_watchdog.py`,
  `deploy_drift_monitor.py`, `stuck_proposal_monitor.py`, `monitor_coverage.py`,
  `evo_path_probe_monitor.py`, `audit.py`, `oc_substrate_monitor.py`) and
  `packages/analyzer/generators/*/charter.yaml` — where the §4 before-strings live.
</content>
</invoke>
