# Evo Wizard & Bot Guide — Architecture (2026-05-05)

Status: **draft**. No implementation yet. This spec proposes a major addition to the `evo` user-facing surface and a new conversation-derived artifact (the "bot guide") authored by the primary user for secondary users of the same bot.

**What this is.** Today, a bot user typing `evo` gets a single Better Engine recommendation. This spec extends `evo` into a small subcommand surface (`evo wizard`, `evo better`, `evo help`, …) and introduces the **Evo Wizard** — a multi-turn conversational onboarding the user runs once per (bot, user) pair, designed to produce a structured user profile usable by the RSI flywheel and to surface platform capabilities (gallery apps, Better Engine, Continuity Engine) at the moment the user is most receptive to learning about them. The wizard branches by user role: the **primary user** of a bot gets the full experience including app recommendations and optional custom-app forging; **secondary users** get a shorter, bot-focused intro driven in part by content the primary authored. That primary-authored content is the **bot guide**, a new pod-wide artifact at `{shared_dir}/bot_guides/{bot_id}.md` that doubles as runtime context injected into the bot's system prompt.

**Relationship to other specs.**
- [spec-rsi-architecture-2026-04-17.md](spec-rsi-architecture-2026-04-17.md) — Better Engine architecture. §4 (the user profile) is the consumer of what the wizard produces. The wizard is one of several producers of profile content; arbiter proposals are another.
- [archive/specs/spec-better-engine-2026-04-15.md](archive/specs/spec-better-engine-2026-04-15.md) — the original `evo`/`evolve` keyword surface. This spec refines that one rather than replacing it; the recommendation flow continues unchanged as `evo better`.
- [manifest-spec.md](manifest-spec.md) — app manifests. The wizard's optional forge phase produces a `bot_created` manifest fed to the existing forge pipeline (no new approval gate).
- [applications.md](applications.md) — gallery and forge UX. The wizard's recommendation phase is a thin wrapper over the existing gallery recommender.

---

## 1. Goals and non-goals

**Goals.**

1. First-time `evo` users get a guided introduction to Evolve rather than a context-free recommendation card.
2. The primary user of each bot leaves the wizard with a populated user profile and at minimum has *seen* gallery options, the Better Engine, and the Continuity Engine — even if they accept nothing.
3. Secondary users of team bots get a short, bot-specific intro that reflects how the primary set the bot up to be used.
4. The user-visible command surface is extensible: every future capability gets discoverability via `evo help` for free.
5. The wizard can be exited and resumed; nothing is lost.
6. Custom app forging is reachable from the wizard but not required.

**Non-goals.**

1. Replacing or restructuring SOUL.md / AGENTS.md ([packages/analyzer/evolve_bot/SOUL.md](packages/analyzer/evolve_bot/SOUL.md), [packages/analyzer/evolve_bot/AGENTS.md](packages/analyzer/evolve_bot/AGENTS.md)). Those remain deployer-authored, source-controlled templates.
2. Introducing a second forge approval gate. The existing Build → Critique → Test → Gate → Apply pipeline owns approval for any apps the wizard produces.
3. Inferring user role from heuristics. Primary/secondary is recorded explicitly at bot setup time; unknown users are secondary by default.
4. A web/admin-UI surface for the wizard itself (out of scope for v1; the bot guide *will* eventually have one).

---

## 2. The user-visible surface

### 2.1 Subcommand registry

`evo` becomes a small dispatcher. Subcommands are declared in a registry at `packages/admin/evolve_admin/evo/subcommands.py`:

```python
@dataclass(frozen=True)
class EvoSubcommand:
    name: str                              # "wizard", "better", "help", ...
    short_help: str                        # one line, shown in `evo help`
    long_help: str                         # multi-line, shown in `evo help <name>`
    available_to: frozenset[Role]          # {primary} | {secondary} | {primary, secondary}
    handler: str                           # importable function path
```

Initial set:

| Subcommand | Primary | Secondary | Notes |
|---|---|---|---|
| `evo wizard` | ✓ | ✓ (short variant) | Resumable. Rerun is permitted. |
| `evo better` | ✓ | — | Existing recommendation flow. |
| `evo apps` | ✓ | — | Browse gallery, install. |
| `evo continuity` | ✓ | ✓ | One-paragraph explainer + status. |
| `evo profile` | ✓ | ✓ | View own profile; editing v2. |
| `evo guide` | ✓ | — | Author/edit the bot guide for secondaries. |
| `evo help [<name>]` | ✓ | ✓ | Always available. |
| `evo default <name>` | ✓ | ✓ | Per-(bot, user) bare-`evo` resolution. |

### 2.2 Bare `evo` resolution

> **Updated 2026-05-06 (slice 5b8):** bare `evo` and `evo better` now
> start a `PHASE_REC_PENDING` wizard session via the dispatcher (§4.9).
> The legacy `mode="legacy_better"` return is retired for the happy
> path; it survives only as a safety-net fallback when the BetterEngine
> import fails mid-deploy. The original "default subcommand →
> evo better → wizard re-entry" routing below is the v1 design that
> the slice 5b8 implementation supersedes.

Bare `evo` resolves dynamically per (bot, user):

1. If the user has set a `default` (via `evo default <name>`), use that.
2. Else, if the wizard for this (bot, user) is not in `completed` status, route to `evo wizard`.
3. Else, route to `evo better` for primaries and `evo wizard` (re-entry, with phase resume) for secondaries.

Storing the user-default as part of the per-(bot, user) state file (§7) avoids a second persistence shape.

### 2.3 Help rendering

`evo help` lists the subcommands the calling user is allowed to use, filtered by `available_to`. `evo help <name>` renders the long help. Primary/secondary filtering keeps the help text honest — secondary users don't see commands they can't run.

---

## 3. Identity model

### 3.1 Primary vs secondary

Every bot has exactly one **primary user** and zero or more **secondary users**. The primary is recorded at bot setup time; everyone else is secondary by default.

**Primary's role.** Owns the bot. Full wizard. Authors the bot guide. Receives Better Engine recommendations. Can install gallery apps and forge custom ones.

**Secondary's role.** Uses the bot. Short wizard focused on this specific bot. Reads the guide. No app installation, no Better Engine recommendations, no forge.

A user can be primary on one bot and secondary on another. The role is per-bot, not pod-wide. Pod sysadmin status (admin UI access etc.) is a separate concept and does not affect evo routing.

### 3.2 Recording primary identity

Extend network.json with a `primary_user` block per bot:

```jsonc
{
  "bot_id": "team-bot-a",
  "primary_user": {
    "pod_user": "pod-admin-user",
    "external_ids": {
      "slack":    "U123ABC",
      "telegram": "4567890",
      "discord":  "987654321"
    }
  }
}
```

Per the explicit-pod-membership rule, network.json remains the only source of truth — we do not introduce a parallel `bot_owners.json`.

**Capture mechanism (revised after implementation, 2026-05-05).** The original spec assumed `setup_wizard.py` would capture external IDs at the time each integration is connected. That turned out to be impractical: per-bot integration setup happens *post-deployment* via the keys-rotation API rather than in the wizard, and Telegram/Discord chat IDs aren't even knowable at setup time — they're only learned when the user first messages the bot. Slack/Discord today use manually-pasted bot tokens, not OAuth, so there's no installer identity at setup time either.

Replaced with a **manual claim model** for v1: the operator runs

```sh
sudo evolve-admin evo claim-primary <bot> --channel <ch> --external-id <id>
```

(or hits `POST /api/evo/identity/<bot>/claim`) once per (bot, channel) pair to record the primary. This is explicit, channel-agnostic, and avoids the team-bot footgun where the first sender to type `evo` would otherwise be auto-claimed even if they aren't the owner. Idempotent for the same value; refuses to overwrite a different existing value without `--force`.

Auto-capture (e.g. claim-on-first-`evo-wizard`) is deferred until we have stronger signals for distinguishing a primary's intentional ownership claim from a team member's casual interaction.

### 3.2.2 Self-claim via passphrase (in-band)

The CLI / API claim is fine for the operator running setup, but it forces the admin to know each user's external ID up front — friction for the common case of "the bot owner is the person typing `evo` for the first time." The pod can also be configured with **two passphrases** (admin + primary) that users self-quote to claim a role:

```sh
evo claim charles          # claims admin (pod-wide)
evo claim darwin           # claims primary on this bot
evo claim charles darwin   # claims both at once (any order)
```

Pod-level config in `network.json` (defaults shipped: `admin=charles`, `primary=darwin`):

```jsonc
{
  "pod": {
    "admin_passphrase": "charles",
    "primary_passphrase": "darwin",
    "admins": {
      "external_ids": { "telegram": ["999"], "slack": ["U-POD-ADMIN"] },
      "pod_users": ["pod-admin-user"]
    }
  }
}
```

**Three roles:** `admin` (pod-wide), `primary` (per-bot), `secondary` (everyone else on multi-user bots). Admin is a strict superset of primary — admins can run anything primary can. The dispatcher special-cases this (admin bypasses `available_to` checks) so we don't have to enumerate "admin OR primary" everywhere in the registry.

**Claim semantics:**
- Tokens parsed as 1 or 2 whitespace-separated words; each tested against admin AND primary passphrases (case-insensitive). Multiple matches stack.
- Admin claim writes pod-level (`pod.admins.external_ids[channel]`); idempotent.
- Primary claim writes per-bot (existing `claim_primary`); refuses to overwrite a different existing primary on the channel — caller is told to use `evolve-admin evo reassign-primary` for that.
- Every successful claim writes one audit-log record (`action: "claim"` for primary, `action: "claim_admin"` for admin); idempotent re-claims write nothing.
- Anonymous / channel-less callers get a soft refusal explaining we can't tell who they are.

**Why this isn't a serious security risk:** the passphrases are soft secrets shared with intended users. Worst case: a curious secondary types `darwin` and claims primary on a bot before the actual owner does. The pod admin sees the audit log entry, runs `evolve-admin evo reassign-primary` to fix it, and rotates the passphrase via `evolve-admin evo set-passphrase --primary <new-word>`. No data exposure, easy recovery.

**CLI tooling**: `evolve-admin evo set-passphrase [--admin <word>] [--primary <word>]` to rotate; `evolve-admin evo show-admins` to list recorded admins.

### 3.2.1 Reassignment

Multi-user bots will eventually need to transfer ownership: a household admin hands off a personal bot, a team's bot owner moves on, etc. Two CLI verbs cover the two cases cleanly:

| Action | Command | Behavior |
|---|---|---|
| First-time claim | `evo claim-primary <bot> --channel <ch> --external-id <id>` | Idempotent on same value; refuses to overwrite a different existing value without `--force` |
| Transfer | `evo reassign-primary <bot> --channel <ch> --from <old> --to <new>` | Verifies current value matches `--from` before mutating; rejects with a clear "current is X, not Y" message otherwise |

Mirror endpoints: `POST /api/evo/identity/<bot>/claim` and `POST /api/evo/identity/<bot>/reassign`.

The split exists for human-factors reasons, not technical ones: `claim-primary --force` *can* overwrite, but it's the wrong verb for transfer because there's no `--from` check protecting against fat-fingered or stale-state mutations. `reassign-primary` is the obvious command for the obvious operation, and the `--from` check makes it self-documenting in shell history.

Both verbs write to an audit log at `{shared_dir}/audit/evo_identity.jsonl` (one record per line):

```jsonc
{"ts":"2026-05-06T15:30:00Z","actor":"cli:pod-admin-user","action":"reassign",
 "bot_id":"team-bot-a","channel":"slack","from":"U123ABC","to":"U456DEF",
 "force":false,"reason":"transferring ownership to Bob"}
```

Idempotent re-claims of the same value are silent (no audit record). The actor for CLI calls prefers `SUDO_USER` so `sudo evolve-admin …` records the human who ran the command, not `root`. Read via `evo show-audit [--bot <id>] [--channel <ch>] [--limit N]` or `GET /api/evo/identity/audit`.

What the displaced ex-primary becomes: the moment the reassign lands, their external_id no longer matches the recorded primary, so on their next `evo` invocation they get the secondary command surface — same mechanism that gates any non-primary user. No separate "demote" step is required.

### 3.3 Resolving the calling user

Plugin-side, `TurnObserver.handleBeforeAgentRun` extracts the per-channel sender ID from `ctx.sessionKey` (`_extractSenderExternalId`) and passes `(bot_id, channel, sender_external_id)` to the admin server. The server's `evo.identity.resolve_role` returns `"primary"` or `"secondary"` based on whether the sender matches the recorded `external_ids[channel]` for that bot.

**Per-channel extraction support (v1):**

| Channel | sender_external_id source | Status |
|---|---|---|
| Telegram | trailing numeric segment of `ctx.sessionKey` (`agent:main:telegram:direct:<chatId>`) | working |
| Slack | sessionKey shape not yet documented | TODO — falls back to null → primary |
| Discord | sessionKey shape not yet documented | TODO — falls back to null → primary |

For Slack/Discord, until extraction lands, role-aware filtering is effectively disabled on those channels (everyone reads as primary). `evo claim-primary` still records the ID, so the data is in place for the resolver to use as soon as the plugin can supply real IDs.

If a secondary's identity later needs lifting to a pod user (for e.g. cross-bot profile carry-over), that's a v2 concern.

---

## 4. Wizard architecture

### 4.1 The conversation engine: hybrid state-machine + LLM

The wizard is **not** a literal scripted dialogue and **not** a fully free-form LLM agent. It is a **state machine that owns the agenda** and uses the LLM for two distinct jobs.

Each turn has two LLM roles:

1. **Speak (in-conversation).** The bot's normal LLM, given a `systemAppend` block describing the current phase, what's been extracted so far, and what the next question should accomplish. The LLM's user-facing reply is whatever it normally would be — natural, in the bot's voice. We do not ask it to emit JSON inline.
2. **Extract (post-turn).** A separate, server-side classification call (cheaper model, on the admin server, not via the bot) takes the user's most recent message plus the current phase schema and returns structured fields. This call doesn't touch the user-facing message and isn't constrained by the bot's voice.

This separation has three benefits: (a) the user-facing experience stays conversational and on-brand; (b) extraction failures don't pollute output; (c) we can swap the extractor model without affecting the bot.

### 4.2 Phase definitions — primary

Phases are agenda buckets. Each phase declares: target fields to extract, exit condition, and whether it's skippable.

| # | Phase | Target fields | Exit | Skippable |
|---|---|---|---|---|
| 1 | Greet | — | One turn. | No |
| 2 | About you | name, role, environment, current tooling | Has name + ≥1 of {role, environment} | Partially |
| 3 | Goals | top_goals[], pain_points[], current_workflow_notes | ≥1 goal articulated | No |
| 4 | Platform tour | — | User says "ok" / asks to move on / declines tour | Yes (default skip if user is in a hurry) |
| 5 | Gallery recommendations | apps_accepted[], apps_dismissed[] | Either an install accepted, or all top-K dismissed, or user says "enough" | Partially |
| 6 | Bot guide drafting | bot_guide.purpose, bot_guide.audience, bot_guide.tone, bot_guide.do_say[], bot_guide.dont_say[] | A coherent guide draft saved (can be empty if bot has no secondaries) | Yes |
| 7 | Optional forge | build_spec.markdown | Either a forge job submitted, or user declines | Yes (default skip) |
| 8 | Wrap | — | Status set to `completed`, summary shown | No |

Phase 4 (platform tour) is conditional. The agenda entering phase 4 is influenced by signals from phases 2–3: if the user described concrete goals matching gallery apps, we go straight to phase 5; the tour fires only if the user seems unfamiliar or asks "what is this thing?"

Phase 6 is skipped entirely if the bot has no plausible secondary audience (e.g. a personal bot with one user). Detection: `network.json` says the integration channel is a 1:1 DM with the primary's external ID and there are no other pod users tied to the bot.

Phase 7 fires only if phases 3 and 5 left a goal unmatched by any gallery app *and* the user expresses interest in something custom. Otherwise, the wizard wraps.

### 4.3 Phase definitions — secondary

| # | Phase | Target fields | Exit | Skippable |
|---|---|---|---|---|
| 1 | Greet (bot-flavored, reads guide.purpose) | — | One turn. | No |
| 2 | About you | name, team_role | Has name | Partially |
| 3 | How to use this bot (reads guide.do_say, guide.dont_say, guide.tone) | — | One turn. | No |
| 4 | Try something | — | User asks the bot a real question | Yes |
| 5 | Wrap | — | Status set to `completed` | No |

No tour, no apps, no forge. Total expected turns: 4–6.

### 4.4 Resume and re-entry

Wizard state has five lifecycle values: `not_started`, `in_progress`, `paused`, `completed`, `skipped`. `paused` is set when the user goes silent for a configurable interval (default 24h) mid-wizard, or explicitly says "pause" / "later". `completed` is terminal but `evo wizard` can be re-invoked to re-enter at phase 1 (with prior fields pre-loaded as defaults to confirm/correct).

`skipped` is reachable via `evo wizard skip` (primary only) and is treated as `completed` for routing purposes but flagged so we don't generate fictional profile content downstream.

### 4.6 Wizard front door — CHALLENGE phase (5b2)

A user typing `evo wizard` enters the wizard at one of two phases based on resolved role:

| Caller's role | Initial phase | Why |
|---|---|---|
| `admin` | GREET | Already verified — go straight to onboarding |
| `primary` (recorded or fallback) | GREET | Same |
| `secondary` (different primary recorded) | CHALLENGE | Identity unverified — verify before gathering personal info |

**CHALLENGE behavior**: the bot asks for the admin and/or primary passphrase. The user's reply is matched deterministically (no LLM extractor) against the pod-level passphrases:

- **Recognized passphrase(s)** → matching claims applied via the same writers as `evo claim`, audit logged, advance to GREET. The greet prompt acknowledges the claim before asking for their first name.
- **Decline** ("skip", "no", "cancel", "later", "pass", "stop", "nope") → wizard ends gracefully. User can run `evo wizard` again any time.
- **Unrecognized text** → wizard ends with a slightly different framing ("didn't recognize that as a passphrase").
- **Primary passphrase + existing different primary** → primary claim refused with the standard "another user is already recorded" message; admin claim (if also matched) still proceeds.

This unblocks slice 5b3 — once a real primary is recorded, the secondary wizard variant has somewhere to land.

### 4.8 Gallery recommendations (5b5)

After the platform tour, the wizard offers 1–3 gallery apps that match the user's extracted profile. Picks are scored by keyword overlap between each candidate's `display_name + description + application_tags` and the user's `role / environment / top_goals / current_tooling / pain_points`. No LLM call for ranking — it's a deterministic count.

The bot presents the top picks conversationally ("based on what you said, you might want X for tracking deploys"). The user replies in natural language; the engine classifies the reply via the same phrase-vs-word keyword tables used elsewhere:

| Intent | Triggers | Effect |
|---|---|---|
| `accept` (named) | "calendar", "the CI one", "calendar and ci" | Accepted apps stored in `apps_accepted` (display names, for the wrap prompt to speak about); other candidates moved to `apps_dismissed` (pkg_ids) |
| `accept` (all) | "install all of them", "all three", "yes all" | Every candidate accepted |
| `accept` (only-one shortcut) | "yes" when only one candidate is on offer | The single candidate accepted |
| `dismiss_all` | "skip", "none", "later", "not now", "no thanks" | All candidates dismissed |
| `ambiguous` | anything else | Re-render the prompt with the same candidates |

The phase **does not actually install** the accepted apps — it captures intent into the user profile. Wrap mentions the apps and points the user at the admin dashboard's Apps tab (or the existing `evolve-admin evo apps`-adjacent flows) for the actual install. Wiring the wizard to create forge install jobs directly is a v2 nice-to-have; the spec defers forge integration per §10 anyway.

**Skip when empty.** If the gallery has nothing to recommend (every package already installed on the bot, or the gallery is sparse), the engine skips past `GALLERY_RECS` straight to `WRAP` rather than burning a turn on an empty "no recs available" prompt. Detected during the PLATFORM_TOUR → GALLERY_RECS advance step.

### 4.9 Conversational approval — REC_PENDING (5b8)

Slice 5b8 folded the original "conversational approval" spec
(`docs/spec-better-engine-conversational-approval-2026-04-18.md`) into
the wizard engine as a single-phase mini-wizard. Bare `evo` and
`evo better` no longer go through the legacy direct-formatting path —
the dispatcher fetches the top recommendation server-side, hands it to
`engine.start_rec_pending`, and returns a `wizard_session_id` exactly
like every other wizard subcommand.

**Phase shape.** `PHASE_REC_PENDING` is terminal (`next_phase=None`) and
deterministic (`targets=()`, the engine special-cases routing). The
audience is `"approver"` — finalize does **not** commit a user profile
(mirrors `guide_drafter`).

**Reply classification.** The handler classifies the user's reply via
`evo.wizard.intent.parse_intent`, a two-stage pipeline:

1. Stage 1 — deterministic PHRASES + WORDS classifier with the same
   split pattern used by GUIDE_CONFIRM and GALLERY_RECS. An
   `_AMBIGUITY_PHRASES` preempt catches "not sure" / "no idea" / etc.
   so single-word matches don't false-fire on hedging language.
2. Stage 2 — Anthropic Haiku call (reusing `extractor._call_anthropic`)
   that returns `{action, confidence, snooze_hint_days, rationale}`.
   Gated by length / no code blocks / no URLs. Confidence threshold
   0.80 (constant `intent.CONFIDENCE_THRESHOLD`).

**Routing.** Five actions:

| Action | Effect |
|---|---|
| `accept` | `BetterEngine.record_feedback(rec_id, "accepted")`; chain to next rec or finalize |
| `reject` | `BetterEngine.record_feedback(rec_id, "rejected")`; chain or finalize |
| `snooze` | `BetterEngine.snooze(rec_id)` (engine owns the escalation schedule); chain or finalize |
| `next` | `BetterEngine.record_feedback(rec_id, "rejected", reason="ignored")` (soft dismiss); chain or finalize |
| `context` | Re-render with the `context` variant; **no** state transition; rec stays pending |

`unknown` / low-confidence: re-render with the `clarify` variant the
first time (asking the user to disambiguate around the parser's best
guess). On a second consecutive miss, finalize without action — the
rec stays pending in the better-engine queue and the user's next
message lands on normal bot logic. Tracked via `_unknown_streak` on
`state.extracted`.

**Prompt variants.** `prompts._rec_pending_block` selects via
`ctx['variant']`: `pitch` (first turn), `clarify`, `context`,
`all_caught_up` (terminal). The pending rec lives at
`state.extracted["_pending_rec"]` (underscore-prefixed scratch — not
profile-bound). `_finalize_rec_pending` strips these before the
terminal write.

**Audit.** Every action writes an `evo.audit.append_event` record with
`action="rec_<verb>"`, `to_external_id=<sender>`, `reason=<rec_id>`.
This puts proposal approvals on the same audit surface as identity
claims and guide saves.

**Day 2 (shipped 2026-05-07):**

- **Config knobs** under `conversational_approval` in
  `better-engine-config.json`, resolved by the new
  `evo/wizard/config.py` helper (`enabled`,
  `llm_intent_parse_enabled`, `confidence_threshold`,
  `default_snooze_days`, `pending_expiry_minutes`,
  `push_preamble_enabled`). Engine threads the config into
  `intent.parse_intent` and `_record_rec_action`; dispatcher uses
  `enabled=False` as an operator escape hatch returning
  `mode="legacy_better"`.
- **Session TTL** on approver-audience sessions: `process_turn` checks
  `state.updated_at` against `pending_expiry_minutes` (default 60 min)
  and finalizes stale sessions. The pending rec stays in the
  better-engine queue. The `/api/evo/wizard/active` recovery probe
  applies the same expiry so post-restart plugins don't re-attach to
  stale rec_pending sessions.
- **Snooze duration end-to-end.**
  `snooze_recommendation(rec, *, days_override=...)` and
  `BetterEngine.snooze(rec_id, *, days_override=...)` honor a stage-2
  hint ("remind me next week" → 7); naked snoozes still escalate via
  the schedule.
- **Voice cues** via the new `evo/wizard/voice.py` module: bot guide
  frontmatter (preferred), then SOUL.md `## Tone` section, then bot
  RSI profile `## Communication Preferences` section. Engine pulls
  these into prompt context for `PHASE_REC_PENDING` and the pitch
  prompt renders them in a "Voice cues" bulleted block.
- **Push preamble scaffolding.** `engine.start_push_preamble(...)`
  starts a rec_pending session with the new `push` prompt variant
  when an urgent rec is queued and `push_preamble_enabled=True`.
  Plugin call site is deferred (real push delivery wants per-channel
  rate-limiting and a frequency cap; that's a separate spec).
- **Plugin** Case 2 (legacy `parseReply` follow-up branch) gutted —
  replaced with a defensive cleanup that clears stale plugin-side
  rec state. Wizard sessions own all follow-ups now.

### 4.7 Secondary chain (5b4)

When a CHALLENGE caller types a **tour-intent keyword** instead of a passphrase or decline, the engine flips `state.audience` to `secondary` and routes them into a four-phase tour-style chain:

```
SECONDARY_GREET → SECONDARY_ABOUT_YOU → HOW_TO_USE → SECONDARY_WRAP
```

**Tour keywords**: `tour`, `use`, `show`, `show me`, `how`, `how do i`, `help`, `info`, `guide`, `introduce`, `intro`, `explain`. Decline still wins over tour (so "skip" exits cleanly), and passphrase match still wins over both.

**SECONDARY_GREET** reads the bot guide's frontmatter and body to introduce the bot in the primary's voice. **SECONDARY_ABOUT_YOU** lightly extracts `name` and `team_role` (exits on name alone — team_role is bonus). **HOW_TO_USE** renders the guide's `do_say` / `dont_say` / `tone` fields as a one-turn explainer. **SECONDARY_WRAP** is the closing turn; profile committed via the same finalize path as primary's WRAP.

The secondary user profile contains only the lighter fields (`name`, `team_role`, plus the audit metadata). RSI consumers can distinguish primary from secondary profiles by the presence/absence of primary-only fields like `top_goals`.

**Graceful degradation**: if no bot guide exists, `SECONDARY_GREET` keeps the intro generic and `HOW_TO_USE` falls back to "just chat with the bot naturally" rather than rendering empty bullets. The team-bot scenario without a guide is the most common case before the primary writes one — the wizard shouldn't block on it.

### 4.5 Phased delivery

The wizard ships in stages so each landing is reviewable. Status:

| Slice | Adds | Shipped |
|---|---|---|
| 5a  | State machine, extractor, plugin routing, GREET / ABOUT_YOU / WRAP, profile commit | ✓ |
| 5b1 | `evo claim` passphrase + admin role | ✓ |
| 5b2 | CHALLENGE phase (passphrase front door for unrecognized callers) | ✓ |
| 5b3 | GOALS + PLATFORM_TOUR phases | ✓ |
| 5b4 | Secondary wizard variant (per §4.3) — tour-request routing from CHALLENGE | ✓ |
| 5b5 | Gallery recommendations phase — surfaces 1-3 gallery apps in-chain | ✓ |
| 5b6 | Conversational bot-guide drafting (slice 4b's deferred half) — `evo guide` | ✓ |
| —  | Forge phase (§10) | deferred |

The full primary chain post-5b3 is **GREET → ABOUT_YOU → GOALS → PLATFORM_TOUR → WRAP**, five turns end-to-end. PLATFORM_TOUR is a single-turn explainer with no extractor. The state file format, dispatcher integration, plugin mid-wizard turn routing, extractor seam, and profile commit have been stable since 5a — adding more phases is a matter of declaring more `Phase` instances in `evo/wizard/phases.py` and wiring their prompts in `evo/wizard/prompts.py`.

`paused` and `skipped` lifecycle transitions remain reserved fields in the state file (they round-trip cleanly) but aren't yet entered by the engine; that lands when there's a clear UX trigger for them.

---

## 5. The bot guide

### 5.1 What it is

`{shared_dir}/bot_guides/{bot_id}.md` — a markdown file with YAML frontmatter, primary-authored, conversation-derived during phase 6 and edit-revisable via `evo guide`. Schema:

```markdown
---
bot_id: team-bot-a
authored_by: pod-admin-user
authored_at: 2026-05-05T14:30:00Z
last_edited_at: 2026-05-05T14:30:00Z
audience: "engineering team, ~6 people"
tone: "direct, no emoji"
do_say:
  - "use team-bot-a for deployment status questions"
  - "ping team-bot-a when CI fails on main"
dont_say:
  - "don't ask team-bot-a about HR or payroll"
---

# Team-Bot-A — Team Guide

Team-Bot-A is the team's deployment and CI assistant…

## What team-bot-a knows about
…

## What team-bot-a can't help with
…
```

The frontmatter is structured (consumed by phase 3 of the secondary wizard); the body is free-form prose (rendered when a secondary asks "what is this bot for").

### 5.2 Dual use as runtime context

Beyond the wizard, the guide is **injected into the bot's session system prompt** alongside SOUL.md, via [packages/analyzer/session_surface.py](packages/analyzer/session_surface.py). Two reasons:

1. The bot stays consistent with how the primary said it should be used.
2. When a secondary asks the bot something off-mission, the bot can redirect using the primary's own framing rather than improvising.

Loading rule: SOUL.md always; bot_guide.md only if it exists and `last_edited_at` is non-null. Both are read at session start and concatenated into the system prompt as separate sections (so the bot can distinguish "what I am" from "how I'm being used here").

### 5.3 Ownership boundary vs SOUL.md

| File | Authored by | Where | Lifecycle |
|---|---|---|---|
| SOUL.md | Deployer (us) | source-controlled in [packages/analyzer/evolve_bot/SOUL.md](packages/analyzer/evolve_bot/SOUL.md), deployed to bot's `.openclaw/workspace/` | Versioned with the codebase |
| AGENTS.md | Deployer (us) | source-controlled, deployed | Versioned with the codebase |
| bot_guide.md | Primary user | `{shared_dir}/bot_guides/{bot_id}.md` | Authored in conversation, revisable any time |

No file is co-authored. SOUL describes the bot's *kind*; the guide describes *this specific bot's* mission and conventions per the primary.

### 5.4 Guide updates and secondary notification

When a primary edits the guide, runtime context picks up the change automatically on next session. We do **not** silently broadcast to secondaries. If the primary wants to actively announce, `evo guide announce` (v2) drops a hint into each secondary's next interaction: "the team intro for me has changed; run `evo wizard` if you want a refresher."

### 5.6 Conversational drafting via `evo guide` (slice 5b6)

The file-based authoring path from #772 (`evo set-guide --file <path>`) still works, but the natural way to author or edit a guide now is `evo guide` from inside the bot itself. Reuses the wizard engine's session/state/turn infrastructure so the plugin gets no changes — the guide drafter is structurally a wizard with different phases.

**Primary-only.** Secondaries hit the standard `available_to=PRIMARY` rejection. Admins can run it (registry's admin-bypass).

**Two phases**:
- `GUIDE_GATHER` — extracts `audience`, `tone`, `do_say`, `dont_say`, `body_outline` over multiple turns. Exits when audience + (tone OR body) are filled. Preview keywords (`show me`, `preview`, `see it`, etc.) force-advance to `GUIDE_CONFIRM` so the user can save partial drafts as a starting point.
- `GUIDE_CONFIRM` — renders the proposed guide in markdown form (frontmatter + body) and gates save on a deterministic keyword match: `save`/`yes`/`looks good`/etc. → write via `evo.guide.write_guide()` and audit-log a `guide_save` action; `cancel`/`no`/`don't save` → end without writing; `edit`/`change`/`redo` → kick back to `GUIDE_GATHER` preserving extracted fields; anything else → re-render the confirm prompt.

**Editing mode.** When dispatch sees an existing guide for the bot, it pre-populates the gather state with the existing fields (audience, tone, do_say, dont_say, body) so `evo guide` doubles as an editor — the user refines what's there rather than starting from scratch. The gather prompt detects this case and frames the conversation accordingly ("you're editing the existing guide" vs "you're authoring a new one").

**Audit**. Every successful save appends a `guide_save` event to `{shared_dir}/audit/evo_identity.jsonl` so guide changes are traceable alongside identity changes.

### 5.5 Phased delivery

The guide ships in two slices to keep scope tight:

* **Slice 4a (storage + injection — landed):** the markdown+YAML file at `{shared_dir}/bot_guides/{bot_id}.md`, atomic write with auto-stamped `authored_at` / `last_edited_at`, `session_surface.py` reads the guide and injects it as a labeled section between POD_CONDUCT and pending tasks. Authoring is file-based via `evo set-guide <bot> --file <path>` (or stdin), with `evo show-guide [--raw]` for read-back. Admin endpoints: `GET /api/evo/guide/<bot>` (404 if absent, 422 if malformed) and `PUT /api/evo/guide/<bot>` (body `{frontmatter, body, authored_by?}`).
* **Slice 4b (conversational authoring — pending):** `evo guide` becomes a multi-turn flow that drafts the guide with the primary, using the same systemAppend/extractor pattern the wizard engine uses. Slice 4b lands alongside or right after the wizard engine since the patterns are identical.

A primary can run the file-based path today (write a draft in their editor, pipe through `evo set-guide`); the conversational shortcut is the v2 affordance.

---

## 6. Trigger and routing

### 6.1 Plugin-side detection

`KeywordHandler.isEvoKeyword()` ([packages/plugin/src/better/KeywordHandler.ts:29](packages/plugin/src/better/KeywordHandler.ts:29)) is extended to recognize `evo <subcommand>` and `evo` (bare). Match is case-insensitive; the subcommand is the next whitespace-separated token if present.

When matched, `TurnObserver.handleBeforeAgentRun` calls a new admin-server endpoint `POST /api/evo/dispatch`:

```
request:  { bot_id, channel, sender_external_id, raw_text }
response: { mode: "speak" | "send_direct",
            system_append: <string|null>,
            direct_message: <string|null>,
            wizard_session_id: <string|null> }
```

The dispatcher resolves user role, looks up wizard state, picks the subcommand handler, and either returns a `system_append` block (for the LLM to speak) or a `direct_message` (for Telegram-style direct send, mirroring the existing recommendation path). For wizard turns we use `speak` mode — conversation needs the LLM. For `evo help` and `evo continuity` we use `send_direct` — those are static text blocks that don't need LLM mediation.

### 6.2 Mid-wizard turns

While `wizard_session_id` is set on a session, **every** subsequent user turn is routed through wizard logic, not just turns starting with `evo`. The plugin learns this from the `BetterSessionState` extension:

```ts
interface BetterSessionState {
  evoCalled: boolean;
  hintFired: boolean;
  wizardSessionId: string | null;   // new
  wizardPhase: number | null;       // new
}
```

When `wizardSessionId` is set, the plugin calls `POST /api/evo/wizard/turn` after each user message, gets back the phase context to inject and (if extraction completed) the schema-fields that were captured. The wizard ends — clearing `wizardSessionId` — when the server returns `phase: "completed"`.

### 6.3 Server-side extraction

`POST /api/evo/wizard/turn` does:

1. Load wizard state from `{shared_dir}/wizard/{bot_id}/{user_key}.json`.
2. Run the extractor LLM call on the user's message, scoped to the current phase's target schema.
3. Merge extracted fields, decide whether to advance phase based on each phase's exit condition.
4. Build the next `system_append` describing the new phase context.
5. Persist updated state.

Concurrency: writes to the state file go through the same temp-file + rename pattern used by `arbiter.store.write_proposal`.

---

## 7. Storage layout

```
{shared_dir}/
├── wizard/
│   └── {bot_id}/
│       └── {user_key}.json        ← lifecycle, phase, extracted fields, transcript ref
├── bot_guides/
│   └── {bot_id}.md                ← primary-authored (one per bot)
└── profiles/
    └── users/
        └── {user_key}.json        ← final user profile, RSI-consumable
```

Key shape:

- `user_key` is `pod:<pod_user>` for known pod users (primaries, sometimes secondaries) and `ext:<integration>:<external_id>` for secondaries we don't have a pod-level identity for. This namespacing prevents collision and makes upgrades to pod-user identity straightforward later.
- `profiles/users/{user_key}.json` is **separate** from `profiles/{bot_id}.json`. The latter is bot capability (existing, [packages/analyzer/profile_builder.py:40](packages/analyzer/profile_builder.py:40)); the former is user identity / preferences / goals (new).

The wizard state file is the in-progress scratchpad; the user profile is the committed output. The wizard never writes user-facing fields directly to the profile — final commit happens in phase 8 (Wrap), which writes the profile and clears the transient `transcript` from the wizard state.

---

## 8. Module / file plan

New code lands in `packages/admin/evolve_admin/evo/`:

```
packages/admin/evolve_admin/evo/
├── __init__.py
├── subcommands.py        # registry, EvoSubcommand dataclass
├── dispatch.py           # /api/evo/dispatch handler
├── identity.py           # primary/secondary resolution from network.json
├── wizard/
│   ├── __init__.py
│   ├── state.py          # state file IO (read/write/atomic)
│   ├── engine.py         # phase machine, exit conditions, advance logic
│   ├── extractor.py      # server-side LLM extraction call
│   ├── phases_primary.py # phase definitions
│   ├── phases_secondary.py
│   └── prompts.py        # system_append builders per phase
├── guide/
│   ├── __init__.py
│   ├── storage.py        # bot_guide.md read/write, frontmatter parsing
│   └── runtime.py        # session_surface integration
└── handlers/
    ├── help.py           # static help-text rendering from registry
    ├── better.py         # delegates to existing BetterEngineClient flow
    ├── continuity.py     # static explainer
    ├── profile.py        # view-only in v1
    ├── apps.py           # delegates to existing gallery flow
    └── default_setting.py
```

Plugin-side changes:

- [packages/plugin/src/better/KeywordHandler.ts](packages/plugin/src/better/KeywordHandler.ts): extend `isEvoKeyword` to parse subcommand token; new `parseEvoCommand(text)` returning `{ subcommand, args }`.
- [packages/plugin/src/observer/TurnObserver.ts](packages/plugin/src/observer/TurnObserver.ts): replace direct `BetterEngineClient.getTopRecommendation` call with call to new `/api/evo/dispatch`. Extend `BetterSessionState` (TurnObserver.ts:135) with `wizardSessionId`/`wizardPhase`.
- New `packages/plugin/src/better/EvoDispatchClient.ts` — thin HTTP client mirroring `BetterEngineClient`.

Existing files we touch but don't restructure:

- [packages/admin/evolve_admin/setup_wizard.py](packages/admin/evolve_admin/setup_wizard.py): record primary user external IDs during integration connection.
- [packages/analyzer/session_surface.py](packages/analyzer/session_surface.py): also load `bot_guide.md` if present and emit it as a system-append section.

---

## 9. API contract

New endpoints on the admin server:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/evo/dispatch` | Resolve role + route subcommand. Returns `system_append` or `direct_message`. |
| `POST` | `/api/evo/wizard/turn` | Process a user turn within an active wizard session. |
| `POST` | `/api/evo/wizard/skip` | Mark wizard `skipped` for current (bot, user). |
| `GET`  | `/api/evo/help` | Return registry filtered by role (used by both `evo help` and admin UI later). |
| `GET`  | `/api/evo/guide/{bot_id}` | Read current guide. |
| `PUT`  | `/api/evo/guide/{bot_id}` | Write guide (called by `evo guide` handler after a primary's authoring conversation; not exposed to secondaries). |

All endpoints are loopback-only and unauthenticated, mirroring the existing `/api/better/*` surface ([packages/plugin/src/better/BetterEngineClient.ts:11](packages/plugin/src/better/BetterEngineClient.ts:11)).

---

## 10. Phased delivery

**v1 (this spec, target: ~1 month).**

- Subcommand registry + `evo help` + `evo better` (delegated) + `evo wizard` for primaries (phases 1–8 except 7 stubbed) + secondaries (phases 1–5).
- Identity recording in setup_wizard.py.
- Wizard state files, user profiles, session-surface integration of bot_guide.
- `evo guide` authoring flow.

**v2 (deferred).**

- Phase 7 (forge) — once we've seen what users actually request.
- `evo guide announce`.
- `evo profile` editing.
- `evo apps` interactive (for v1, this just forwards to `evo better` filtered to gallery recs).
- `evo default <name>`.
- Admin-UI surface for the bot guide.

**Out of scope (no current plan).**

- Cross-bot profile sharing for the same pod user.
- Promoting the wizard's user profile into network.json.
- Multi-primary bots.

---

## 11. Open questions

1. **External ID stability.** Slack/Discord IDs are stable; Telegram chat IDs are stable per chat but a user contacting the bot from a different chat (group vs DM) will look like a different external ID. For the rare case where the same person reaches a bot from multiple Telegram contexts, we'd see two "secondary" wizard runs. Acceptable v1; revisit if it bites.
2. **Extraction model selection.** Using a smaller/cheaper model for the extractor is the obvious move. Decision: which model, and is it the same one already configured for the bot or a separate "system" model? Touches model-role policy ([docs/model-roles.md](docs/model-roles.md)).
3. **What happens on primary change?** If a bot's primary is reassigned (rare but possible — household, role change), what happens to the existing user profile and bot guide? The guide is bot-scoped so it survives; the profile is user-scoped and isn't affected. The new primary's first `evo` re-runs the wizard. Probably fine, but worth naming.
4. **Wizard transcript privacy.** The wizard state file holds a transcript reference until phase 8 wraps. Where does the transcript itself live, and is it different from the bot's normal session storage? If we use the existing session storage, the transcript is already covered by `security_warden` capture policy (200 turns / 48h). Probably reuse that. Confirm.
5. **Concurrent sessions for the same user.** A primary could have `evo wizard` in progress on Slack while also DMing the bot on Telegram. The state key includes integration namespace for secondaries but not for primaries (we use `pod:pod-admin-user` for both). Decide: lock to one channel, or allow parallel and last-write-wins? Lean: lock; new channel sees "you have a wizard in progress in Slack; finish or pause first."
