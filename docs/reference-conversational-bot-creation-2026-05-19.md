# Reference: Conversational bot-creation — the Atlas session

**Date:** 2026-05-19
**Surface:** Claude Code (Opus) in worktree `elegant-bohr-f5356c`
**Participants:** Pod-Admin (operator) + Opus
**Outcome:** Designed "Atlas" — a research/community bot for an OC-enthusiast Telegram group that doubles as Pod-Admin's market-intel feed for Evolve.
**Why this doc exists:** This session is a worked example of what the [conversational-bot-creation wizard](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_conversational_bot_creation_wizard.md) should *feel like* when Evolve hosts it natively for non-developer users. Captured at design time so future implementers have a concrete reference, not just a spec.

---

## The conversation shape (annotated)

The session started as "tell me the implications of Google I/O 2026 on Evolve" and pivoted naturally into "I want to build a bot to monitor this space." What's interesting is *how* the bot got designed — the questions asked, the order they were asked in, and the moments where the human realized something. A wizard hosted by evo should drive a conversation with the same shape.

### Phase 1 — Surfacing the *why* before the *what*

Pod-Admin didn't open with "create a bot named X with integrations Y." He opened with the problem: "I want to scan for news of the day, have it synthesized, and provide direction without whipsawing on every announcement." The first useful response wasn't a config — it was a question about **architecture intent**: should this live inside Evolve as meta-RSI, or be a personal OC bot?

> **Wizard analog:** When a user says "I want a bot for X," the LLM should resist jumping to integrations. First question: "What's the underlying need this serves, and who's it serving?" The answer reshapes everything downstream.

The exchange clarified that strategic synthesis is the human's job; the bot is a research assistant, not a co-pilot writing roadmap proposals. That distinction set the bounds for everything that followed.

### Phase 2 — Audience reveals the shape

Pod-Admin then introduced a second audience: a Telegram group of OC enthusiasts (also Evolve beta testers). The bot serves both — the group (community archive + daily digest) and Pod-Admin (market intel).

> **Wizard analog:** "Who else will interact with this bot?" The answer is often *more than one audience*, and that fact is load-bearing. A wizard that only asks "what's it for" misses this.

Two design questions immediately surfaced:
- Does the same content serve both audiences, or do they need different feeds?
- What's the consent posture for community members whose messages get archived?

The answer landed as: one feed for both, Pod-Admin's synthesis happens downstream in his own sessions. Members get a visible privacy notice + opt-out.

### Phase 3 — Naming as a function of role

Naming came *after* the role was clear, not before. Once we knew "this bot maps the ecosystem and reports back," candidates wrote themselves: Atlas, Almanac, Loom, Field. Pod-Admin picked Atlas.

> **Wizard analog:** Naming should be deferred until role is locked. Asking "what should we call it?" upfront produces names that don't fit. Asking after the bot has shape gives the LLM enough context to propose names *with rationale*, and the human picks among options that all fit.

> Side fact: "Evo is taken" — bot-name ≠ account-name should be first-class in the wizard, per [evolve-bot-role](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_evolve_bot_role.md).

### Phase 4 — Integrations follow role, applications follow audience

Once the role was clear, integrations fell out almost mechanically:
- Telegram (primary surface — listen + post + DM)
- Brave search (pod-wide invariant)
- GitHub (OC releases + community repos)
- Web fetch (article archival)
- Workspace storage (the archive)

Applications followed the audience analysis:
1. `daily-digest` — runs once/day, classifies into 5 buckets (competitive landscape / new tools / use cases / case studies / warnings), posts to group
2. `article-capture` — when a member posts a link, fetch + summarize + file + react in-thread
3. `on-demand-research` — when @-mentioned, do focused research and reply
4. `weekly-recap` — Sundays, highest-signal items + patterns

> **Wizard analog:** The wizard should know that "what apps does this bot need?" is downstream of "who's it for, what cadence, what classification scheme?" — and ask those first.

### Phase 5 — Consent and tone as required steps, not optional

Two surfaces the wizard must enforce:

- **POD_CONDUCT for tone:** Team-Bot-A-style per [message-style-team-bot-a-like](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_message_style_team-bot-a_like.md) — short header, one fact per line, conversational close. No "Security/CRITICAL" labels.
- **Privacy intro + opt-out:** Per [user-observation-optout](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_user_observation_optout.md). Atlas's first post in the group is a privacy notice with an opt-out mechanism (react 🤐 to a message to exclude it from the archive).

> **Wizard analog:** These are non-skippable questions. The LLM should never produce a finished bot without an explicit answer to "who can see what this bot collects, and how do they opt out?"

### Phase 6 — The meta-realization

Mid-session, Pod-Admin noticed: *we are doing this conversation in Opus in Claude Code, and in a perfect world this whole process should happen inside Evolve.* The wizard is a feature, not just a setup script.

That realization is what produced the [conversational-bot-creation-wizard](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_conversational_bot_creation_wizard.md) memory.

> **Wizard analog:** None — but worth noting: a good wizard surfaces *unmet needs* the user didn't know they had. The fact that this realization happened in a developer tool with a developer-user means the gap is real and felt.

---

## What the wizard's transcript should look like (sketch)

Stitched-together flow, written as if the wizard already existed:

```
evo: What kind of bot do you want to add?

pod-admin: I want to monitor the OpenClaw / AI ecosystem and have it
       feed me market research for Evolve. I'm also going to create
       a Telegram group of OC enthusiasts — some are Evolve beta
       testers — and I want the bot to serve them too.

evo:   Two audiences then: you (private synthesis) and the group
       (shared archive + digest). Quick check before we go further —
       should the same content serve both, or do you want a private
       channel for things only you see?

pod-admin: Same content. My synthesis happens downstream when I read
       it.

evo:   Good. That keeps the bot honest with the community. One more
       framing question: is this bot a research assistant (surfaces
       signal, you decide) or a co-pilot (writes recommendations)?

pod-admin: Research assistant. Don't want it writing strategy for me.

evo:   Locked. Now: what should it watch?
       — OC release notes
       — Anthropic / Google / OpenAI official blogs
       — HN front page (filtered)
       — Anything else? Subreddits, X lists, specific newsletters?

pod-admin: HN, the official blogs, OC releases. Maybe r/LocalLLaMA.

evo:   I'll add those. How often should it post a digest? Daily,
       weekly, on-demand?

pod-admin: Daily digest. Weekly recap on Sundays. And it should react to
       links members share — fetch, summarize, file under one of
       five buckets: competitive landscape, new tools, use cases,
       case studies, warnings.

evo:   Got it. Five-bucket classification — that's becoming an app
       contract. I'll write `article-capture` and `daily-digest`
       around that taxonomy.

       Now consent. Members are sharing messages in a group, and
       this bot is archiving them. What's the privacy notice?

pod-admin: It should post a pinned intro explaining what it does. And
       there should be an opt-out — maybe a reaction.

evo:   I'll set the 🤐 reaction as an exclusion signal. The intro
       will run on first install + on every new-member join. I'll
       also make sure the daily digest never quotes members by
       name.

       Tone: Team-Bot-A-style per your pod conduct?

pod-admin: Yes.

evo:   Naming. Given the role — "maps the ecosystem, reports back"
       — candidates: Atlas, Almanac, Loom, Field. My pick is Atlas.

pod-admin: Atlas.

evo:   Atlas it is. Account name will be `atlas`. Here's the plan:

       Bot:      atlas (account: atlas)
       Surface:  Telegram
       Plugins:  telegram, brave-search, github, web-fetch
       Apps:     daily-digest, article-capture,
                 on-demand-research, weekly-recap
       Storage:  workspace archive (one file per article + index)
       Tone:     Team-Bot-A-style, no quoting members by name
       Consent:  pinned intro + 🤐 opt-out

       I need three credentials from you before I can deploy:
       1. Telegram bot token (BotFather)
       2. GitHub PAT (read public repos)
       3. Brave API key (you have one already? I'll check)

       Want me to walk through getting the Telegram token now?

pod-admin: Yes.

evo:   [walks through BotFather flow]
       ...
       Got it. Validating... ✓ token works, bot is named "Atlas".

       Next: GitHub PAT. ...

       [continues until all creds are in]

evo:   All creds validated. Deploying Atlas... ✓ deployed.

       Smoke test: I'll DM Atlas "hello, who are you?" and check
       the response.

       Atlas: "Hi! I'm Atlas. I watch the OpenClaw and AI
       ecosystem and post a daily digest to the group. ..."

       Smoke test passed. Atlas is live in the pod.

       One last thing — when you add Atlas to your Telegram group,
       it'll post the pinned intro automatically. Ready?

pod-admin: Yes.

evo:   Done. Atlas is in. First daily-digest runs tomorrow at 7am.
```

That transcript is ~60 lines. Today's `setup_wizard.py` flow plus manual file edits for Atlas will be hundreds of lines of human work. That's the gap.

---

## Pointers for the implementer

- The python primitives Atlas needs already exist in `setup_wizard.py`, `deploy.py`, the app-installer, the plugin-installer. The wizard isn't reinventing those — it's *driving* them.
- The LLM driver needs tool access to: validate credentials (per-integration), create bot user, write `openclaw.json`, write `POD_CONDUCT.md`, install plugins, install applications, deploy, run smoke-test conversation, report.
- Failure handling matters: if a credential validation fails, the wizard should ask why and offer to retry. If a smoke test fails, it should *not* leave a broken bot in the pod — roll back or quarantine.
- Templates per [low-friction-bot-creation](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_low_friction_bot_creation.md) gap #2 fold in here: if the user describes a recognized pattern ("research assistant for a community"), the wizard starts from a template and only asks the divergent questions. Atlas would be a great seed for a "community-research-bot" template.

---

## Related

- [conversational-bot-creation-wizard](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_conversational_bot_creation_wizard.md) — the design memory this doc supports
- [low-friction-bot-creation](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_low_friction_bot_creation.md) — the differentiator this enables
- [evo-oc-native-architecture](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_evo_oc_native_architecture.md) — evo is the host for this wizard
- [google-io-2026-implications](../../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_google_io_2026_implications.md) — the strategic context that motivated Atlas
