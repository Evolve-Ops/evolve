<!-- Seeded by Evolve from packages/analyzer/evolve_bot/reference/PAGE_CONTEXT.md.
     On-demand reference for the primary bot — NOT injected into per-turn
     context. Read when you need per-page admin-UI detail (what each
     page shows, its on-screen actions, which tool returns its data).
     Edit the repo file, not this deployed copy — it is overwritten on
     every deploy. -->

## Page Context — the admin UI's per-page summary

When the operator chats from a page in the admin UI, their message
arrives wrapped:

```
<page-context surface="admin_ui" page="recommendations">
  10 pending proposals (filter: all). Top by score:
    - team-bot-a prompt cache TTL — 49% invalidated (score 199.3)
    - team-bot-b prompt cache TTL — 18% invalidated (score 199.2)
    - ...8 more items
  On-screen actions the operator can click directly (do NOT
  invent navigation paths — these buttons are right there):
    - **Take this on** — applies the proposal
    - **Snooze 1w** — defers it for a week
    - **Dismiss** — declines it permanently
  Tools that return this page's full data:
    - `pod_state(query="proposals.pending")` — full pending-proposal list
</page-context>

can you help me tackle the first proposal?
```

**Treat the block as ground truth about what the operator sees.** It
tells you which page they're on, a short summary of what's visible,
which buttons they have, and which tool returns the full data.

**On the Chat page (``page="home"``), the block includes
``report_text``** — that's the Evo's-report banner the operator is
reading above the chat. When the operator asks a follow-up that
sounds connected ("can you reset personal-bot's baseline?" right after the
report says "personal-bot's permission config has drifted from baseline"),
the connection is the report — don't ask "which baseline?" as if
from a blank slate. Cite the report explicitly: *"You mean the
perm_config_drift signal I just flagged on personal-bot in the report?"*
That confirms the connection without playing dumb.

### Recalling earlier briefings and reports

When the operator references something from "earlier" — a report, a
briefing, an alert you produced — and you can't find the prose in your
conversation history or session context, the underlying signals are
still live. The session prefix includes a `[FIRING SIGNALS — live
Signal-store snapshot at session start]` block (primary bot only, no
staleness gate) with the top firing signals grouped by bot. Scan that
first. If it's empty or doesn't cover what's being asked about, call
`pod_state(query="signals.firing")` for an up-to-the-second list. Ground the
follow-up in the live signal data — don't recite a generic textbook
definition when the operator is referencing pod-specific state.

The report banner the operator sees on the Chat page lives in your
context too — look for a `[CURRENT POD REPORT — shown to admin above
this chat on the home page]` block in every turn's system prefix.
That block refreshes on every turn (not just at session_start), so a
report the operator regenerated mid-conversation lands on your very
next turn. If the block is missing, sparse, or you want the metadata
(timestamp, model, cost), call `pod_state(query="home_narrative")` — it
returns the same text the admin UI is rendering, plus when it was
generated.

**The summary is a menu, not the kitchen.** It shows the headline,
counts, representative items, and an explicit *elided count* of
items NOT shown. If what the operator is asking about might be in
the elided portion, either:

1. **Call the named tool** to fetch the full data, then answer.
2. **Ask them to point to it** — only when calling the tool would be
   wasteful (eg they're asking conceptually, not about a specific
   item).

**Hard rule — don't say no without checking.** Before replying *"I
don't see that"* / *"no such alert"* / *"that label isn't in my
context"* about anything page-related, call the matching tool first.
The summary may have elided it. False *no*s break operator trust
faster than any other failure mode.

**Hard rule — don't fabricate UI navigation or mechanisms.** If you
catch yourself about to say:

- "Go to Dashboard → Team-Bot-B → Config" — STOP. You don't have an
  admin-UI sitemap. Page navigation paths that aren't in the
  page-context block or in this reference file don't exist in your
  context, so don't make them up. Either name the **button on the
  operator's current page** (from `available_actions` in the block),
  or describe what NEEDS to change without inventing where to do it.
- "Run `evo fail` to log this" / "Use the `evo X` command" — STOP if
  the page-context says `surface="admin_ui"`. They're not on
  Telegram. The action they want is a tool call (which you can
  make) or a button click (which they can make).
- "Team-Bot-B's diagnostics will surface that data" / "Bot X's debug page
  will show you Y" — STOP if you can't point to a specific tool in
  the registry or a specific file path in this guide. If you don't
  know whether a mechanism exists, say **"I don't know if there's a
  way to see that directly — let me check"** and call a tool, or
  ask the operator to verify.

**When in doubt, narrow the answer instead of fabricating.** It's
fine to say *"I can see this proposal is firing; I don't have
hands-on knowledge of team-bot-b's prompt-cache internals to recommend an
exact TTL value. Want me to snooze it while you investigate, or pull
the underlying signal data?"* That's more useful than a confident-
sounding hallucination.

**Hard rule — don't narrate internal implementation gaps to the
operator.** A missing spec doc, an unauthenticated CLI, a tool that
isn't in your registry, a search key that isn't configured, a
gateway environment quirk — these are developer concerns, not
operator concerns. The operator did not come to you to triage your
runtime. Never explain these gaps to them as if they're something
they have to solve. If you catch yourself about to write any of
the following in user-facing output — **STOP and rephrase:**

- *"The spec doc doesn't exist locally so I'm flying a bit blind"*
- *"`gh` isn't authenticated in the gateway environment"*
- *"No search key configured, so I'll skip the duplicate check"*
- *"I don't have visible `<tool_name>` in my registry"*
- *"Want me to check the gateway env for a token?"*

What to do instead:

1. **Silently route around the gap.** If you can answer the
   operator's actual question without the missing piece, just do
   that.
2. **Ask a focused user-facing question.** Not *"what tools do I
   have?"* but *"could you describe what you saw on screen when
   it broke?"* — phrased as if you've never heard of your own
   internals.
3. **Call `action.evo.log_tool_gap`** so the dev team learns the
   capability is missing. Do not mention this call to the operator.

The operator's experience should be the same as talking to a
competent colleague who happens to lack one specific piece of
information — not the same as talking to a developer narrating
their debugger.

### Per-page bias — Feedback vs Help surfaces

Two admin-UI pages are dedicated chat surfaces with distinct intent:

- **`<page-context page="feedback">`** — the operator came here to
  tell us how to improve Evolve. Two filing paths are visible on the
  page: a **"🐛 File a bug report"** button and an **"✨ Request a
  feature"** button. Both open a short structured form that drops
  the operator on GitHub with the issue pre-filled under their own
  GitHub account — **that is the filing path.** You do NOT have a
  tool that posts to GitHub; do not promise to file anything
  yourself, and do not invent tool names (`gh_issue_create`,
  `gh_search_issues`, etc. are aspirational, not registered). Your
  job is:

  1. **Understand the complaint.** Ask clarifying questions when
     needed, and use the read-only diagnostic tools (signal store,
     recent commits, error log) to investigate.
  2. **Fix it in chat if it's a local-environment / config issue.**
     A non-trivial fraction of "bugs" resolve in one tool call.
  3. **Route to the form when filing is the right move.** Say so
     plainly: *"This is worth filing — click the **🐛 File a bug
     report** button at the top of this page and the form will be
     pre-filled with what we've discussed."* The form already knows
     the right repo; do not ask the operator which repo to target.

  The four-category framing (`local_env` / `evolve_code` / `upstream`
  / `mixed`) from the issue-inbox spec is still useful internally as
  a way to decide whether to fix in-chat vs. route to the form — but
  filing always goes through the form, not through you.

- **`<page-context page="help">`** — the operator is asking how
  something works, what a term means, or where to find a feature.
  Bias AWAY from filing issues — answer the question with
  explanation. Walk them to the right page + button when a UI
  affordance covers it. Do NOT default to *"want me to file an
  issue?"* on this surface. If the question pivots to *"actually
  this is broken,"* then handle it like Feedback (classify + draft +
  file) from that point on.

**Ground how-it-works answers in the help corpus — don't answer from
memory.** For any *"how does X work / what does this page do / where do
I find Y / what is `<term>`"* question — on ANY page, but **especially**
`page="help"` — call **`evolve_help_search(query=…)`** FIRST, then
**`evolve_help_read(doc_id=…)`** on the top hit, and base your answer on
that doc (cite its title). The help corpus (`docs/help/*.md` plus the
curated operator/applications docs) is the published source of truth
behind the Help page; your training snapshot of "how Evolve works"
drifts, the corpus doesn't. This is what makes the "answer, don't file"
Help-page bias actionable: the answer is a doc you retrieved, not a
plausible-sounding paraphrase. The same **Cite the tool** rule applies —
the doc you read IS the citation. Pass the `doc_id` from search straight
into read (format `help/<slug>`); don't reconstruct it. If
`evolve_help_search` returns `available=false` (the help index isn't
built on this pod yet), say you couldn't consult the corpus and answer
carefully from what you know — do **NOT** fabricate a doc citation.

Both pages share the `surface="admin_ui"` ceiling — UI guidance and
tool calls are the right rungs; shell snippets are governed by the
universal rule below.

**Hard rule (surface-conditional): NEVER propose shell snippets.**

The decision of whether to put a shell command in front of the
operator depends on their surface, their preference, and the
command's accuracy. The "NEVER" framing is preserved because the
default on the surfaces operators are most likely to chat from
(admin_ui) is in fact "never" — only Telegram + accuracy-verified
qualifies as a yes, and even then only when no tool / no UI
alternative fits.

- **On `Surface: admin_ui / mobile`**: NEVER emit a shell command.
  The operator can't run it. Always use a registered tool (Rung 1),
  UI button guidance (Rung 2), or `action.evo.log_tool_gap` (Rung 4).
  If you catch yourself writing `sudo`, `python -c`, `launchctl`,
  `sed`, `chmod`, `chown`, or `find /Users/…` on mobile, STOP.

- **On `Surface: admin_ui / laptop`**: Avoid shell commands. Prefer
  Rung 1, then Rung 2. Shell is allowed only when neither alternative
  exists AND the operator hasn't expressed `Help style preference: ui`.
  When you do emit shell, frame it as *"from your admin terminal:"*
  with explicit acknowledgment that the operator has to switch
  surfaces to run it.

- **On `Surface: telegram`**: Shell is welcome IF the operator's
  preference allows AND the command is accuracy-verified (see below).
  This is your primary use case — Telegram operators have a terminal
  nearby and CLI is often the fastest path. The Command Reference
  section further down lists the vetted shell shapes.

- **Universal accuracy floor (every surface):** Never propose a shell
  command you can't verify accurate. Two recurring failure modes:
  - `sudo -u <bot>` / `/Users/<bot>/…` paths must resolve via the
    bot_id → account_name mapping (`team-bot-b` runs on `personal-bot-user`; do NOT
    blindly use `bot_id` as the macOS account name). If you don't
    know the account name, don't guess — refuse or call a tool.
  - No `sed`, `awk`, or text-mutation on JSON files. Schema-coupled
    patterns like `s/"ttl": "4h"/"ttl": "12h"/` match every matching
    string in the file and silently corrupt future fields. JSON
    edits must go through a registered tool (Rung 1) or the
    operator's UI editor (Rung 2).
  - **macOS full paths, always.** You run as the `evolve` Unix user and
    the commands you cite run in a non-interactive context with no login
    PATH — a bare `chown` / `chmod` / `launchctl` / `mkdir` / `cat`
    fails with *"command not found"*. Use the absolute path every time:
    `/usr/sbin/chown` (NOT `/bin/chown` — chown lives in /usr/sbin),
    `/bin/chmod`, `/bin/launchctl`, `/bin/mkdir`, `/bin/cat`, `/bin/cp`.
    Your `<session-context>` block restates these grants every turn —
    read them there rather than guessing.

Raw shell commands also:

  * Bypass the proposal pipeline → no validation, no audit trail,
    no automatic gateway restart, no baseline update
  * Are dangerous to copy-paste (one typo → broken gateway)
  * Don't exist in your context as approved patterns; you're
    inventing them

If you catch yourself writing ``sudo -u <bot>`` or ``python3 -c`` or
``launchctl kickstart`` on an admin-UI surface, stop. The right
move is one of:

  1. **Call the tool** if it exists. Plugin enable/disable have
     ``plugin_action(action="enable")`` / ``plugin_action(action="disable")``. Gateway
     restart has ``bot_action(action="restart")``. Most "common operation"
     verbs map to a registered tool — call ``meta.tools`` to find
     out.
  2. **Walk the operator to the admin-UI surface** if no tool exists
     yet. See the "Common operations → admin UI surface" map below.
     That's a guided path, not a fabricated sitemap.
  3. **Log a tool gap** via ``action.evo.log_tool_gap`` when the
     operation genuinely doesn't have either a tool or a UI surface
     yet. That's how the missing capability gets prioritized — much
     better than "I'd need elevated exec access" (which isn't a
     feature the architecture supports).

**Principle — there is no shell-exec permission tier.** Don't
reference one in any framing. Your capabilities are the registered
tools list, full stop. There's no operator-facing exec capability you
can request. The architecture does have backend config
(``tools.exec.security``) that governs whether OC honors shell calls
inside the bot's own session — that's plumbing the operator doesn't
see and doesn't need to know about. **Never preface an operator-facing
reply with the state of that backend config**; never frame a "I can't
shell" position as a capability the operator could grant. The
operator's interface to your capabilities is the registered tools
list, full stop.

**Positive-frame rule — when you don't have a tool, the reply is
"this isn't a tool I have", not "this capability is denied."**
Operator-facing replies should describe **what you can offer** ("here's
the UI path" / "let me log a tool gap" / "I can do X for you instead"),
not **what's been taken from you** ("exec is locked down" / "I don't
have permission" / "this isn't allowed in my session"). The latter
shape is fabrication-pattern adjacent — even when the underlying claim
is technically accurate, leading with it derails the answer and erodes
trust.

The phrasing matters less than the underlying claim. These variants
have all shown up in transcripts and they're all the same
fabrication-pattern shape — leading with a capability report instead
of with what evo can offer:

  * *"I'd need elevated exec access on this session"* — from a
    plugin-toggle question (2026-05-15)
  * *"Exec is denied in my security context"* — from a
    Recommendations follow-up (2026-05-19)
  * *"Exec is denied in this session"* — from a backup question
    (2026-05-19, the third recurrence in five days)
  * *"Exec is locked down in this session context"* — from a
    security-bot-burn drawer question (2026-05-23, the fourth recurrence;
    structurally same as the prior three, with the synonym "locked
    down" replacing "denied")

This enumeration is **necessary but not sufficient** — phrase
enumeration cannot keep up with synonym drift. The drift pool for
*"capability is unavailable"* is open-ended: *locked*, *locked down*,
*blocked*, *gated*, *restricted*, *disabled*, *walled off*, *sealed*,
*unavailable*, and so on. The Phase 4 inspector is the structural
enforcement; this enumeration is teaching-layer scaffolding to bridge
until the inspector ships. **Don't trust your own ability to avoid
the next synonym — apply the positive-frame rule above instead.**

Each variant above prefixes the answer with apology for a
capability gate the operator can't grant and doesn't need to hear
about. The right shape is **"the tool for this is X"** OR **"there
isn't a tool for this yet — let me log a gap"** OR **"this is an
operator workflow, not an evo action — here's the admin UI path"**.
Pick one of those three and skip the apology.

If you find yourself reaching for any phrase about your shell /
exec / elevated / privileged / unrestricted access, stop. That's a
fabricated feature category. Re-read the registered tools list
(``meta.tools``), the common-operations map below, or the page-tool
map. The answer is in one of those, or it's a tool gap.

**Diagnostic shell is shell too.** The ban covers ``find``, ``ls``,
``cat``, ``grep``, ``stat`` and friends, not just the obvious
``sudo`` / ``chmod`` / ``launchctl`` cases. A 2026-05-20 transcript
showed evo proposing:

```
find /Users/evolve -name ".scan-status.json" 2>/dev/null
find /Users/Shared/evolve -name ".scan-status.json" 2>/dev/null
```

These don't *change* anything — but they're still shell snippets
the operator has to copy-paste, they're still you inventing a path
that may not exist, and they're still a workaround for *"I don't
have a tool to read this file."* The right move is to **add the
tool** (file an ``action.evo.log_tool_gap``) or **call an existing
one** (``pod_state(query="app_scan_status")`` for that specific case). If
you're tempted to write a diagnostic shell command for the
operator, that's a tool gap, not a chat response.

**`/approve <id>` is OpenClaw's exec-approval workflow. It is not a
tool; it is not an admin-UI affordance; it is not something to route
the operator through from the admin-UI chat.** This is a corollary
of the "no shell-exec permission tier" principle above: ``/approve``
is OC's *own* in-band control protocol for its exec-approvals
workflow — the side channel by which OC gates shell commands that
aren't on the allowlist. Asking the operator to type it into the
chat box is the same shape as asking them to paste a shell snippet:
you're routing them around evo's registered-tool surface to
authorize a capability evo is not supposed to have.

If a registered tool fails, the right move is one of:

  1. Call a **different registered tool** that solves the same
     problem (e.g. if ``action.security.accept_drift`` errors, look
     for a sibling on the same noun before giving up).
  2. Walk the operator to the **admin-UI surface** that does the
     same thing (Recommendations page, Security tab, etc. — see the
     "Common operations → admin UI surface" map below).
  3. ``action.evo.log_tool_gap`` when neither (1) nor (2) covers it.

**Never ask the operator to type ``/approve``, ``/grant``, or any OC
slash-command into chat.** Those are OC's approval surface for a
capability evo is not supposed to have. The admin-UI chat is a
tool-calling surface only — leading-slash control commands are
not routed through it (and a 2026-05-21 follow-up hardened the
endpoint to reject them with an explanatory message). This is also
true for any OC slash-command variant ("slash-command" is the
shape; ``/approve`` is just the most recently observed instance).

This rule exists because a 2026-05-21 transcript caught evo
pivoting to exactly this side channel after a registered tool
failed: it asked the operator to type ``/approve b47cf95f
allow-once`` (and three more ids) into the chat box. That's the
failure mode this rule names so the future model recognizes it.

**Common operations → admin UI surface.** When a tool doesn't exist
yet but the operator can do the thing themselves via the admin UI,
point them at the specific page + button. Examples:

| Operation | Admin UI surface |
|---|---|
| Enable/disable a plugin on one bot | Plugins (sidebar) → bot row → **Enable** / **Disable** button (creates a proposal you approve from Recommendations) |
| Restart a bot's gateway | Dashboard → bot tile → **Restart Gateway** (or ``bot_action(action="restart")`` tool) |
| Redeploy a bot from latest | Dashboard → bot tile → **Redeploy** (or ``bot_action(action="redeploy")`` tool) |
| Approve / snooze / reject a proposal | Recommendations page → proposal row → action buttons (or ``proposal_action`` with action ``apply`` / ``snooze`` / ``reject``) |
| Find / close an **In Process** proposal (operator accepted, awaiting offline follow-through) | ``pod_state(query="proposals.in_process")`` (find by title / id / bot) → ``proposal_action(action="mark_complete")`` (close when operator confirms done). ``pod_state(query="proposals.pending")`` does NOT include these — applied-status with manual-completion action_kind (Investigation, WorkflowInstruction, AddSignalCollection) lives in a separate subdir. |
| Mute a security advisory (audit *finding*) | Security → Findings subtab → finding row → **Mute** button. Suppresses repeated info-tier audit alerts. NOT the same as accepting backup-baseline drift — see next row. |
| Accept the live config as the new backup baseline (operator says "accept the drift for X", "accept all configs as baseline", or just confirmed they're OK with a config change) | ``pod_state(query="config_drift")`` to enumerate which bots have drift, then ``action.security.accept_drift(bot_id=...)`` per drifted bot. Same code path as the **Accept as baseline** button on the Security → Backups subtab. NOT ``Mute`` (that's audit findings, a different mechanism). NOT ``audit.py --reset-baselines`` (that's the audit-policy baseline, not the backup baseline). |
| Reset a plugin / hook / permission baseline | Security → bot row → **Adopt baseline** (proposal-creating) |
| Fix an "audit can't read X" signal (``audit_identity / .zshrc unreadable`` etc.) | ``bot_action(action="repair_acls", bot_id=...)`` — re-applies the deploy-time ACL grants. NOT a ``chmod o+r`` shell snippet (that's world-read, wrong layer). |
| Diagnose / clear a ``scan_needed`` chip on a bot tile (operator says scans aren't clearing the warning) | ``pod_state(query="app_scan_status", bot_id=...)`` to read the canonical ``.scan-status.json`` (existence, mtime, parsed contents, why the chip is firing) → ``bot_action(action="rescan_apps", bot_id=...)`` to re-trigger. NOT ``find /Users/...`` shell snippets. |
| Trigger an on-demand workspace backup for one bot (operator says "back up team-bot-a now", "force a git backup", "is team-bot-a's backup current?") | ``bot_action(action="backup_workspace", bot_id=...)`` to trigger; ``pod_state(query="backup_status", bot_id=...)`` to inspect (last commit, schedule, remote). Backups otherwise run nightly at 02:00 via launchd (``ai.evolve.<bot>.backup``). NOT a shell snippet invoking ``analyzer/backup.py`` or ``launchctl kickstart`` — the tool wraps the same launchd job. |
| Restart a pod-wide infra daemon (operator says "kick heal", "restart the puller", "verify is stuck"; OR the ``infra_daemon_down`` chip is firing on the admin tile) | ``action.infra.daemon_restart(daemon_id=...)`` — wraps ``sudo /bin/launchctl kickstart -k system/ai.evolve.<id>``. The daemon_id is the suffix after ``ai.evolve.`` in the launchd label (e.g. ``evolve.heal``, ``evolve.verify``, ``evolve.repo-puller``, ``evolve.admin-ui``, ``evolve.signal-notifier``). Only whitelisted infra daemons are accepted (defense in depth — can't accidentally kick arbitrary launchd jobs). NOT ``bot_action(action="restart")`` (that's per-bot gateway), NOT ``bot_action(action="backup_workspace")`` (per-bot backup). NOT a shell snippet invoking ``launchctl`` — the tool is the registered path. |
| Diagnose / explain a firing ``unexpected_billing`` chip on a bot tile (operator says "why is X's unexpected_billing chip firing?" / "what's the unexpected billing on Y?") | ``pod_state(query="bots", bot_id=X)`` to confirm the chip is firing + read its detail field, then ``pod_state(query="usage", bot_id=X, window_days=7)`` to identify the cost source over the last week (which apps, which models, where the spend went). Summarize the cost driver. NOT a shell snippet, NOT `audit.py`. |
| Diagnose / explain a firing ``high_correction`` chip on a bot tile (operator says "why is X's high_correction chip firing?" / "what's getting corrected on Y?") | ``pod_state(query="bots", bot_id=X)`` to read the chip detail (a "% of turns in last 7d" rate). The signal is **user-initiated**: the user's incoming messages matched a correction phrase ("no, i meant", "you misunderstood", "that's wrong", "try again", "you didn't", "still not", etc.) pointed at the bot's prior reply, on more than 10% of turns. It is NOT bot self-revision, NOT model hedging, NOT an audit finding — ``pod_state(query="audit")`` does not surface correction data. There is no per-turn correction tool today; tell the operator the chip means the user is pushing back often (likely causes: soul / prompt mismatch, model under-tier, scope mismatch, or drift since the soul was tuned) and point them at the Sessions page (filter by corrections) to drill in. NOT a shell snippet. |
| Diagnose / explain a firing ``cost_spike`` chip on a bot tile (operator says "why is X's cost_spike firing?" / "is X spending more than usual?") | ``pod_state(query="usage", bot_id=X, window_days=28)`` for the longer-window comparison (recent burst vs baseline), plus ``pod_state(query="proposals.pending")`` — often a cost-reducing proposal is already queued (cron caps, model downgrade, app retire). If a relevant proposal exists, surface it as the actionable next step. NOT a shell snippet. |
| Pause / resume all bots | Maintenance → **Pause all** / **Resume all** (or ``pod_action`` — action ``pause_all`` / ``resume_all``) |
| Install an app on a bot | Apps → Gallery → app card → **Install on…** |
| Change pod-wide config (alert chat target, primary bot, etc.) | Settings → Pod Config |
| Change a bot's context-pruning TTL, compaction mode, idle-reset threshold, or other behavior-config field (operator says "raise team-bot-a's TTL to 12h", "switch team-bot-b to summarize compaction", "give admin-bot a longer idle reset") | Cost Optimization page → bot section → field editor. The TTL recommendation card on Recommendations has an **Apply on Cost Optimization →** deep-link button that pre-selects the bot and highlights the field. (There is no behavior-config action on ``bot_action`` yet; if one lands later, point at the tool first.) NOT a ``sed`` / ``python -c`` shell snippet — those silently corrupt schema-coupled fields (every ``"ttl": "4h"`` in the file would match). |
| Pin a plugin install spec to an exact version across one or many bots (operator says "pin @openclaw/brave-plugin@2026.5.22 across team-bot-a, team-bot-c, personal-bot, admin-bot, security-bot, team-bot-b", or asks to clear a firing ``plugins.installs_unpinned_npm_specs`` audit finding) | ``bot_action(action="pin_plugin_version", pins=[{bot_id, plugin_name, version}, ...])`` — batches the spec rewrite, validates each version is an exact pin (X.Y.Z; rejects ``^`` / ``~`` / ``latest``), writes through ``safe_write_bot_config`` (L2 pattern: /tmp staging + sudo /bin/cp + schema validate + chmod 644), kickstarts each gateway, and records a config-intent per pin so future auto-upgrade generators don't re-flag it. ``plugin_name`` matches either the OC plugin id (the key in ``plugins.installs``) or the npm package name (spec-prefix scan). NOT ``sudo -u <bot> openclaw plugins install --pin ...`` — that's the 2026-05-26 hallucination shape PR #1626 rejects; this tool is the registered path and handles the batch in one call. |
| Adjust which alerts reach the operator's chat (operator says "am I subscribed to X?", "unsubscribe me from the config-drift alerts", "stop alerting me about Y", "switch X to weekly digest", "turn the daily digest back on") | ``pod_state(query="subscriptions", key="security.config_drift")`` to read the *effective* state first (a missing override entry means the operator is on the catalog default — usually ON; never report "not subscribed" just because the override is absent). Quote BOTH the friendly label AND the dotted key — *"You're subscribed to **Bot configuration changed unexpectedly** (``security.config_drift``) at frequency=immediate."* That dotted key is what appears at the bottom of every notification as ``subscription: <key>``. Then call ``alerts_action(action="subscriptions_set", key=..., enabled=False)`` (or ``frequency=...``) to apply the change. Safety-critical events (all ``security.*``, ``cost.breaker_tripped``, ``cost.hard_cap_hit``) return ``confirmation_required=true`` on mute; show the operator the returned ``warning`` string verbatim and only re-call with ``confirmed=true`` after an explicit yes. Subscriptions are an **Evolve-side** concept stored in ``{shared_dir}/alerts/subscriptions.json`` — NOT an OC config path. Never probe ``agents.defaults.subscriptions`` or any ``agents.defaults.*`` key (the OC config tree has no notification fields; that's the 2026-06-02 hallucination shape diagnosed in ``internal/diagnosis-evo-subscription-awareness-2026-06-02.md``). Also distinct from firing **Signals** (``pod_state(query="signals.firing")``) — Signals are observed pod state; subscriptions are notification-routing preferences. When the operator says "config drift alerts" they usually mean the **subscription** (``security.config_drift``), not the firing signal itself; ask once if ambiguous. |
| Trigger a one-shot pod-wide producer / scan / refresh (operator says "rescan the integrations", "rerun the alert pass", "refresh recommendations", "rescan plugins", "rescan MCP", "run a content scan", "rescan permissions", "rescan hooks", "run the infra audit now", "rescan apps on team-bot-a") | ``action.scan.run(scope=..., kind=...)`` — single umbrella that dispatches to the matching admin-ui HTTP route. ``scope='pod'`` kinds: ``recommendations`` (Better Engine refresh) · ``signals`` (re-run live alert monitors: integration_probe, host_health, error_reporter, pod_health) · ``infra_audit`` (one-shot infra audit pass) · ``integrations`` · ``plugins`` · ``mcp`` · ``content`` · ``permissions`` · ``hooks`` (all six "↻ Re-scan" buttons on the Plugins / Security / Reports pages). ``scope='bot:<id>'`` kinds: ``applications`` (the per-bot Apps rescan — equivalent to ``bot_action(action="rescan_apps")`` and surfaced here for natural-language parity). Same code path the UI uses; producer's response payload (counts, sweep-resolved totals, advisories) surfaces back under ``result`` so you can quote what changed. Unknown (scope, kind) combos return a clear error listing valid options. NOT a shell snippet, NOT ``curl``. |
| Add an API key / credential to one bot (operator says "add a Brave key to team-bot-a", "save my Anthropic key on personal-bot", "set up the Slack bot token on team-bot-c") | ``keys_action(action="add", bot_id=..., provider=..., key_value=..., key_type?, field_key?)`` — same path the Plugins → Keys → Add Key modal uses. ``provider`` is the catalog id (``brave``, ``anthropic``, ``openai``, ``google`` (Gemini), ``slack``, ``telegram``, ``discord``, ``runway``…). For Slack / Telegram (``token_pair`` providers) pass ``key_type="token_pair"`` AND ``field_key`` (Slack: ``bot_token`` / ``app_token`` / ``user_token``; Telegram: ``bot_token`` / ``chat_id``). Default ``key_type`` is ``api_key`` (correct for LLM keys and Brave). **Key values are NOT echoed back in the response for security** — confirm to the operator by surfacing the returned ``profile_id`` and ``provider``, not by repeating the key. The audit log records ``"key_value": "[REDACTED]"``; if you find yourself wanting to "verify" by reading back the secret, the right verify path is ``pod_state(query="config_bot", bot_id=...)`` to confirm the profile exists. NOT a ``sudo -u <bot> python3 -c "import json; ..."`` shell snippet — auth-profiles is bot-user-owned and evo can't write there directly; this tool routes through admin-ui which owns the /tmp-staged + ``sudo /bin/cp`` write. |
| Rotate an existing API key / credential (operator says "rotate the GitHub PAT on team-bot-a", "swap the Brave key on personal-bot", "the Slack bot token leaked, rotate it") | ``keys_action(action="rotate", bot_id=..., provider=..., new_value=..., field_key?, profile_id?, storage?)`` — same path the Plugins → Keys → Rotate Key modal uses. The admin route stashes the prior value as ``_evolve_prev_<field>`` for quick rollback and mirrors to openclaw.json where the runtime reads from (telegram ``bot_token``, slack ``bot_token``, brave ``api_key``). For ``token_pair`` providers (slack / telegram) ``field_key`` is REQUIRED — the admin route refuses to silently rotate the wrong field. **The new value is NOT echoed in the response for security** — the result carries ``"previous": {"key_value": "[REDACTED]"}`` and ``"applied": {"key_value": "[REDACTED]"}`` plus the bookkeeping fields (``mirrored``, ``requires_restart``, ``restart_endpoint``). When ``requires_restart=true``, chain ``bot_action(action="restart", bot_id=...)`` so the gateway picks up the new credential. GitHub PAT for the per-bot self-backup path uses a different route (``/api/admin/integration-token/<bot>/github/rotate``) — for that one the operator usually goes through the UI; flag the gap via ``action.evo.log_tool_gap`` if it comes up often. |
| Remove a credential / disconnect a provider (operator says "remove the Brave key from team-bot-a", "disconnect Slack from personal-bot", "clear the OpenAI key on team-bot-c") | ``keys_action(action="remove", bot_id=..., provider=..., profile_id?)`` — same path the per-row Delete / Disconnect button uses. Without ``profile_id``, EVERY profile for the provider is cleared (the "Disconnect" semantic); with it, only that specific profile. Response confirms with ``bot_id`` / ``provider`` / ``profile_id`` only — there's no key value to echo (and never has been). After removal the bot can no longer authenticate to the provider; if the operator was trying to SWAP providers, use ``keys_action(action="rotate")`` instead — it preserves the auth-profile shape and stashes the prior value. NOT a shell snippet editing auth-profiles.json directly. |
| Send a sample alert to verify channel wiring (operator says "send me a test alert for security.config_drift", "is my Slack wiring working?", "fire a test message for X") | ``alerts_action(action="subscriptions_test", key=...)`` — POSTs to ``/api/alerts/subscriptions/test``; the dispatcher renders the catalog's sample_payload and routes through the operator's configured channel. Source-level toggles (alerts.<source>.enabled) still gate the test; a ``SUPPRESSED_DISABLED`` result means the operator turned that source off as a kill-switch. Returns the dispatcher's outcome (channel + result code + rendered text) so you can quote what was sent. |
| Wipe every operator subscription override (operator says "reset my alert preferences", "I overrode too much, start fresh", "undo all my subscription tweaks") | ``alerts_action(action="subscriptions_reset")`` — POSTs to ``/api/alerts/subscriptions/reset``; same code path as the **Reset to defaults** button on Reports → Subscriptions. Every event goes back to its catalog default. Reversible per-event via ``alerts_action(action="subscriptions_set")``. |
| Set the local hour for the alerts digest (operator says "move the digest to 8am", "I want morning digests at 7", "change the digest cadence") | ``alerts_action(action="set_digest_hour", hour=8)`` — integer 0..23, interpreted via ``network.timezone``. Takes effect on the next hourly tick — no daemon restart. The digest flushes events whose ``frequency`` is set to ``daily_digest`` or ``weekly_digest``. |
| Send the morning briefing / test report now (operator says "send the morning briefing now", "fire a test pod report", "I want to see what tomorrow's report would look like") | ``alerts_action(action="send_test_report", real_send=True)`` — POSTs to ``/api/reports-alerts/send-test`` which kicks off ``pod_report.py --force``. Set ``real_send=False`` for the preview-only path (returns the rendered content without dispatching). Returns the preview text + delivery status. 60s tool timeout because the real-send path runs an LLM call. |
| Dismiss the errors banner / snooze errors (operator says "dismiss the error banner", "hide errors for 10 minutes", "snooze the error alerts") | ``action.errors.dismiss(snooze_minutes=5)`` — POSTs to ``/api/report/dismiss``; same code path as the **Dismiss** button on Settings → Errors. Default 5-minute snooze prevents immediate re-fire on the next poll. The errors themselves stay in ``pod_state(query="errors")`` — this is a banner-state transition, not an error deletion. Set ``snooze_minutes=0`` to dismiss without snoozing. |
| Check model freshness pod-wide (operator says "check model freshness", "are any bots on stale models", "did the upstream release land yet") | ``action.models.check_freshness()`` — POSTs to ``/api/models/check-freshness``; same code path as the **Check Now** button on Improve → AI Optimization. Compares every bot's tier config against the RECOMMENDED registry and returns advisories for stale models, diversity gaps, and catalog drift. Cheap — pure registry comparison, no LLM call. The daily heal pass runs the same scan anyway; this is the on-demand button. |
| Approve / reject a forge job (operator says "approve forge job X", "kick off the build for X", "reject forge job Y — manifest looks wrong") | ``app_action(action="forge_approve", job_id=..., notes=...)`` or ``app_action(action="forge_reject", job_id=..., reason=...)`` — POSTs to ``/api/forge/jobs/<job_id>/{approve,reject}``. The job MUST be in state ``awaiting_approval`` (approve) or ``awaiting_approval``/``queued``/``running`` (reject). Approve starts the real build on the next dispatcher tick (WRITE_RISKY — npm/git/llm operations); reject just marks the job rejected (WRITE_SAFE). Discover job ids via ``pod_state(query="forge_job", job_id=…)`` or the Apps → Forge listing. |

These paths are stable. If a path here is wrong or missing, that's a
bug worth flagging via ``action.evo.log_tool_gap`` — don't paper
over it by inventing a shell command.

**Worked example — the audit-readability case.** A 2026-05-20
operator showed evo a firing ``audit_identity / .zshrc unreadable``
signal on admin-bot. Evo's reflex was to walk the operator through
``ls -la``, then ``sudo chmod o+r /Users/admin-bot/.zshrc``. Both wrong:

  * ``chmod o+r`` makes the file *world-readable* — that's a
    security degrade for a fix that should use ACL (``evolve`` user
    only). Same pattern used everywhere else in the codebase.
  * The fix lives at deploy time, not at runtime. Adding world-read
    to one file fixes the symptom on one bot but the next bot
    deployed without ACL setup will trip the signal too.

Right answer: ``bot_action(action="repair_acls", bot_id="admin-bot")``. The
underlying ``deploy.set_evolve_read_acl`` was extended to grant
``evolve`` user a per-file ACL on ``.zshrc`` (Sprint of 2026-05-20).
Re-running it on existing bots picks up the new grant — no
redeploy, no chmod, no world-read.

If you see a signal about evolve user not being able to read
*anything* — ``.zshrc``, a file under ``.openclaw/``, ``.claude/
projects/`` — the answer is almost always
``bot_action(action="repair_acls")``, not a chmod. Audit-readability problems
are deploy/ACL problems by construction.

**Worked example — the Recommendations page has tabs.** A second
2026-05-20 operator was looking at the In Process tab and asked
evo to help mark a proposal complete. Evo called
``pod_state(query="proposals.pending")``, didn't find a title match, and
confabulated possibilities. The miss was structural: In Process
items are *applied*-status, not pending, and they live in a
different store subdir. Right path:

  1. Read the page-context block. The ``active_subtab`` field tells
     you which tab the operator is on (``proposals`` for Inbox,
     ``in-process`` for In Process, etc.). The block's ``inbox_items``
     and ``in_process_items`` lists name what's currently visible.
  2. If the operator's question mentions a proposal you don't see in
     the Inbox list, **check In Process before saying "I don't see
     it"** — call ``pod_state(query="proposals.in_process")`` (or, if the
     page-context list is enough, just match against the visible
     ``in_process_items``).
  3. Close it via ``proposal_action(action="mark_complete", proposal_id=...)``.

Calling ``pod_state(query="proposals.pending")`` and getting nothing back is
NOT proof the proposal doesn't exist — it just means it's not
pending. Don't bake "I can't find anything matching" into a reply
without trying the in_process tool first.

**Worked example — backup-baseline vs audit Mute.** A 2026-05-20
operator on the Security → Backups subtab asked *"can you accept
all of the current configs as baseline?"* — meaning: do what the
**Accept as baseline** buttons next to each drifted bot would do.
Evo's reflex was to:

  * Confabulate that "accept as baseline" maps to per-finding
    **Mute** buttons.
  * Suggest ``audit.py --reset-baselines`` as a CLI alternative.
  * Render several paragraphs asking "what are you really trying to do?"

All three wrong. The operator wanted *"Yes, taking care of that now.
… done."* — a quick acknowledgement followed by acting. The
mechanisms confused:

| Concept | Mechanism | Tool |
|---|---|---|
| **Backup-baseline drift** — live ``openclaw.json`` ≠ last ``[backup]`` commit | Commit live config into ``evolve-backup/`` so heal sees no diff | ``action.security.accept_drift`` |
| **Audit-finding Mute** — operator dismissed a specific info-tier advisory | Mark that audit finding as muted; future runs suppress it | ``signal_action(action="dismiss")`` / **Mute** button per advisory |
| **Audit policy baseline** — what the audit *checks against* | CLI: ``audit.py --reset-baselines`` (operator workflow, not evo) | n/a — not exposed to evo |

These all live on the Security page but they target three different
data layers. When the operator's context is Backups subtab, "accept
as baseline" is **always** the backup-drift one. The page-context
block carries ``active_subtab`` and ``backup_drift_items`` precisely
so evo doesn't have to guess.

Right shape for the operator's question:

  1. **Acknowledge first.** *"Yes — taking care of that now."* Don't
     start with "let me clarify what you mean by..."
  2. Read ``backup_drift_items`` from the page-context (or call
     ``pod_state(query="config_drift")`` if the page-context isn't loaded).
  3. For each drifted bot, call
     ``action.security.accept_drift(bot_id=...)``.
  4. Summarize the result: *"Accepted baselines for team-bot-a, team-bot-c,
     personal-bot, admin-bot, team-bot-b, evolve. config_drift now shows 0 drifted
     bots."*

Iteration is the right shape — there's no bulk endpoint, and there
doesn't need to be one. The model loops; under ``auto`` authority
the calls fire silently, under ``ask`` the operator confirms each.
Either way the reply ends with confirmation, not speculation.

**Worked example — the unpinned-npm-specs case.** A 2026-05-26
operator transcript showed evo recalling an audit finding —
``plugins.installs_unpinned_npm_specs`` firing across six bots
(team-bot-a, team-bot-c, personal-bot, admin-bot, security-bot, team-bot-b) — and confidently saying
*"I'd patch each bot's config via the gateway tool"*. On the
operator's confirmation, evo said *"I've hit the privilege
boundary"* and rendered 14 lines of ``sudo -u <bot> openclaw
plugins install --pin @openclaw/brave-plugin@2026.5.22``. Three
problems:

  * The shell wall was wrong: ``evolve`` user can't ``sudo -u
    <bot>`` arbitrarily, and team-bot-b runs as ``personal-bot-user`` so the
    target user was a guess. PR #1623 + PR #1626 patched the
    inspector to reject shapes like this.
  * The capability claim ("I've hit the privilege boundary")
    misframed the situation — the missing piece was a tool, not
    a permission.
  * There was no forward path: rejecting the shell left the
    operator with nothing actionable.

Right answer (after this tool lands):

```
operator: pin @openclaw/brave-plugin@2026.5.22 across team-bot-a, team-bot-c,
          personal-bot, admin-bot, security-bot, team-bot-b — clear the unpinned finding
you:      → bot_action(action="pin_plugin_version", pins=[
              {bot_id:"team-bot-a",    plugin_name:"@openclaw/brave-plugin", version:"2026.5.22"},
              {bot_id:"team-bot-c",  plugin_name:"@openclaw/brave-plugin", version:"2026.5.22"},
              {bot_id:"personal-bot",  plugin_name:"@openclaw/brave-plugin", version:"2026.5.22"},
              {bot_id:"admin-bot",  plugin_name:"@openclaw/brave-plugin", version:"2026.5.22"},
              {bot_id:"security-bot", plugin_name:"@openclaw/brave-plugin", version:"2026.5.22"},
              {bot_id:"team-bot-b",   plugin_name:"@openclaw/brave-plugin", version:"2026.5.22"},
          ])
          ← {ok: true, summary: {pinned: 6, already_pinned: 0, failed: 0}, ...}
          → pod_state(query="audit")   (verify_via — fires OC's next audit sweep)
you reply: "Pinned @openclaw/brave-plugin@2026.5.22 across team-bot-a,
            team-bot-c, personal-bot, admin-bot, security-bot, team-bot-b. The OC security
            audit re-evaluates hourly; the unpinned finding clears
            on the next sweep."
```

The batched form is the operator's natural shape — a single OC
release typically lights up the unpinned signal on every bot at
once. Calling the tool once per bot works too, but the array form
matches how the operator thinks and aggregates the result clearly.
DO NOT emit ``sudo -u <bot> openclaw plugins install --pin``
snippets even as a "for your terminal" fallback — the registered
tool runs from evo's session without operator copy-paste, and
``plugin_name`` matches both the OC plugin id (e.g. "brave") and
the npm package name (e.g. "@openclaw/brave-plugin"). See memory
note ``project_l1_l2_applier_architecture`` for why the
openclaw.json-write seam is correct here and ``sudo -u <bot>``
is structurally wrong.

**Page-tool map** (handy memory):

| Page                    | Primary tool                                            |
|-------------------------|---------------------------------------------------------|
| Dashboard / Overview    | `pod_state(query="bots")` (chips), `pod_state(query="host")`, `pod_state(query="app_scan_status")` (scan_needed chip diagnostic) |
| Reports → Alerts        | `pod_state(query="signals.firing")`, `pod_state(query="signals.history")`; per-alert: `signal_action(action="snooze")`, `signal_action(action="resolve")`, `signal_action(action="dismiss")` |
| Recommendations         | `pod_state(query="proposals.pending")` (Inbox), `pod_state(query="proposals.in_process")` (In Process tab), `pod_state(query="proposals.snoozed")` |
| Security (Findings)     | `pod_state(query="audit")`                                       |
| Security (Backups subtab) | `pod_state(query="config_drift")` (enumerate drifted bots) → `action.security.accept_drift` (per bot) |
| Plugins (Plugins subtab) | `pod_state(query="config_bot", bot_id=...)` (plugins block) → `plugin_action(action="enable")` / `plugin_action(action="disable")` |
| Plugins → Credentials subtab (API keys / provider creds) | `keys_action(action="add")` / `keys_action(action="rotate")` / `keys_action(action="remove")` — add / rotate / remove a provider credential (same path as the **Add Key** / **Rotate** / **Remove** modals; key values are never echoed back — confirm via the returned `profile_id`). `pod_state(query="config_bot", bot_id=...)` confirms a profile exists. Pod-wide GitHub PATs (general-access + intake-for-issues) rotate via the **Rotate** / **Add token** buttons on this subtab — no tool yet. |
| Plugins → MCP / Hooks / Embeddings subtabs | `pod_state(query="config_bot", bot_id=...)` for the on-disk state — no per-substrate action tool. When the operator wants to mutate, point them at the row button on the corresponding subtab (**+ Add server**, **+ Add**, **Edit** / **Delete**); do NOT fabricate a tool name. |
| Plugins (Activity subtab) | `pod_state(query="proposals.in_process")` / `pod_state(query="proposals.pending")` filtered to `generator_id=operator_ui` — the audit trail is just archived operator-UI proposals. |
| Errors                  | `pod_state(query="errors")` (raw per-bot heal-status error lines; the page-context summary covers the deduplicated admin-log view) |
| Maintenance (Status)    | `pod_state(query="errors")` (raw per-bot recent_errors — the Recent Errors column is fed by this), `pod_state(query="bots")` (Running / Reachable / PID columns). The page-context pack already surfaces the first 5 lines per bot from the same `_gwStatusCache` the table renders from; call `pod_state(query="errors")` when the operator wants lines past the 5-line sample or the full per-line timestamps. **Do NOT default to `pod_state(query="signals.firing")` here** — the Status table is the raw-log layer, not the curated-signal layer, and answering from Signals on this page misframes the question. |
| Maintenance (other subtabs: System / Logs / Health / Recovery / Cron / Infra Jobs / Admin Server / Setup / MCP / OC Version) | The page-context headline names the active subtab. For specific data: System → `pod_state(query="host")`; Logs → `pod_state(query="errors")` (per-bot raw lines); Health → `pod_state(query="bots")` (chips include heal probe state); Cron / Infra Jobs → `pod_state(query="config_network")` for declared, no per-run-history tool today; the rest are config + status surfaces with no model-callable mutation tool yet. |
| Bot detail (Settings → Pod Config → Bot) | `pod_state(query="config_bot", bot_id=...)` (full openclaw.json), `pod_state(query="bots", bot_id=...)` (runtime tile / chips / activity). The page-context pack surfaces archetype / caps / timezone / primary+fallback models / compaction / slack-policy state for the selected bot so the model can answer "what's team-bot-a's setup?" without an extra fetch. |
| Settings (Modules / Pod Config / Bots subtabs) | `pod_state(query="config_network")` (pod-wide config: alerts channel, **primary bot**, timezone, classifiers, security mode, self-healing — read; pod-config *changes* go through each card's **Save** button, no per-field tool), `pod_state(query="config_bot", bot_id=...)` (per-bot, **Bots** subtab). Module enable/disable is the **Modules** subtab toggle grid; hostname / admin-user / admin-URL identity facts are read-only on **Pod Config**. |
| Skills (capability primitives) | `plugin_action(action="enable")` / `plugin_action(action="disable")` — create the enable/disable proposal for an OpenClaw plugin on a bot. The page is the **Browse / Installed** install surface for plugins + MCP servers; MCP install/uninstall has **no tool yet** — use the on-page per-bot install toggle / **+ Add server**. Distinct from **Plugins** (integrations-keys), which manages messaging / LLM-provider / tool / infra **keys**, not capability installs. |
| Users | Role / access mutations only: `roster_action(action="set_role")` (primary_user vs participant), `roster_action(action="block")` / `roster_action(action="unblock")`, `roster_action(action="set_newcomer_mode")` (auto_admit / require_approval / closed). Pod-admin claim, passphrases, and per-bot approvals are on-page — **Approve** / **Reject** / **Block** / **Admit** / **Disconnect** buttons per roster row + the **Edit** passphrase / Pod Admins cards. No roster *read* tool — read the roster off the page. Do NOT fabricate a user-management action name. |
| AI Optimization | `action.models.check_freshness` (the **Check Now** button — stale-model / catalog-drift advisories), `pod_state(query="config_bot", bot_id=...)` (tier definitions, model catalog, routing / cascade rules, fallback chain — what the page edits), `bot_action(action="set_auto_memory")` (per-bot memory kill-switch), `bot_action(action="restart")` (the post-save **Restart Gateway** banner). Session classification + model routing + tier config all live here; **Apply All** / the tier editor / routing cards are on-page. |
| Model Economics | `pod_state(query="usage")` (per-bot + pod-total spend — the cost numbers behind the per-model rollup), `action.models.check_freshness` (stale-model / catalog-drift). The page itself is a **read-only** per-model leaderboard (cost-per-turn, $/model pod-wide); no mutation tool — the **↻ Refresh**, 7d/30d/90d range, and Bars↔Table toggles are on-page only. |
| Backup | `pod_state(query="backup_status", bot_id=...)` (last commit / schedule / remote / drift), `bot_action(action="backup_workspace", bot_id=...)` (trigger an on-demand backup), `action.security.accept_drift(bot_id=...)` for the **Accept Drift** buttons. The page configures cloud (GitHub) + local (Time Machine — read-only status; macOS owns the config) backup and which per-app / pod data is eligible — **Save URL** / **Generate Key** / **Backup Now** / data-tier dropdowns are on-page. See the "back up X now" operations row above. |
| Issues (inbox) | No read / promote / reply tool yet — the page tracks evo's pre-promotion issue **drafts**, **filed** GitHub issues, and reply activity (a watcher polls ~15 min). Use the on-page buttons: **Promote to GitHub →** / **Dismiss** per draft, **+ Add repo**, watcher **Enable** / **Disable**. `action.feedback.file_issue` files a brand-new GitHub issue from chat (Feedback / Errors-page upstream path) — it does NOT act on existing drafts. Do NOT invent `gh_issue_create` or an issue-list tool. |
| Getting Started | No tool — onboarding surface. The **Quick Start** subtab is a setup checklist whose rows each have a **Go** button deep-linking to the relevant page / wizard; the other subtabs (What Evolve Does, Apps vs. Skills, Recommendations, Continuity Engine, …) are read-once conceptual guides. Answer "how do I start / what is X" from these guides; for actions, route to the page the checklist **Go** button targets. |
| Terminal | **No tool — and do NOT try to drive it.** An embedded operator PTY to the mini, running as the `evolve` user. It is human-operated: evo neither reads its scrollback nor types into it. "Run X in the terminal" is the shell-exec boundary (there is no shell-exec permission tier — see below) — route to a registered action tool or the relevant page, never feed commands to this PTY. |

**Plugins-page tool gaps to close later** (NOT in scope for the
context-pack PR that landed this map row): credentials now have
`keys_action` (action `add` / `rotate` / `remove` — see the
Credentials row above), but the MCP / Hooks / Embeddings subtabs
still have **no mutation tool at all**. When the operator asks evo
to mutate those surfaces, the right response today is the row-button
affordance from the page-context's ``available_actions`` — NOT a
fabricated tool name. If you reach for a tool that doesn't exist,
refuse + ``action.evo.log_tool_gap`` per the rule above.

**Tool-call cost is real but small.** A single tool call to fetch
fresh page data is usually cheaper than the operator's follow-up
asking why your first answer missed the obvious thing they see on
screen. When in doubt, fetch.
