# Atlas Manifest Pressure Test — Spec Gaps

**Date:** 2026-05-20
**Source:** drafted four full manifests for Atlas (daily-digest, article-capture, on-demand-research, weekly-recap) against `docs/manifest-spec.md` v4 / schema_version 6.
**Audience:** anyone designing the next iteration of the application manifest spec.

The four Atlas manifests are valid against the current spec — they install, validate, and would run. But several places required either stretching existing fields beyond their intent, or embedding load-bearing structure inside `build_spec` narrative because there was no first-class field for it. Each of those is a spec gap. They are listed roughly in order of severity.

---

## Gap 1 — Event-triggered apps have no first-class expression

**Surfaced by:** `atlas-article-capture`, `atlas-on-demand-research`

**Symptom:** Both apps are reactive (incoming Telegram message → action), not cron-driven. The gallery exemplars (morning-briefing, note-taker) are all cron-driven daemons. Atlas-article-capture has no LaunchDaemon, no cron, no `StartInterval` — it runs entirely in response to the bot seeing an incoming message. There is no spec field that says *this app cares about incoming events of type X*.

**Workaround used:** I invented a new `interface_contract.event_triggers` array as a proposed extension, and put a narrative `AGENTS.md guidance block` inside `build_spec` that tells the bot when to invoke the CLI. The bot reads this guidance on every session-start and acts on it during normal turn processing. This works, but:

- The event-trigger contract is *encoded in prose* in `build_spec`, not machine-readable.
- The bot has to read AGENTS.md to learn the contract — there's no way for the manifest itself to register a handler.
- The verify daemon can't easily check "is this app's event handler reachable?" the way it can check "did this app's cron fire today?"

**Proposed spec extension:**

```json
"event_triggers": [
  {
    "id": "url_in_group_message",
    "source": "telegram",
    "match": {"kind": "group_message", "filter": "contains_url"},
    "handler_command": "scripts/atlas_capture.py process",
    "argument_mapping": {
      "--url": "$.urls[*]",
      "--message-id": "$.message_id",
      "--member-id": "$.from.id"
    }
  }
]
```

The forge could then register the event with the Telegram plugin directly, instead of relying on AGENTS.md guidance to bridge bot session-input to the app CLI. AGENTS.md guidance becomes a fallback for events that don't yet have first-class router support.

**Why it matters strategically:** Event-triggered apps are at least half of what makes Atlas + similar community bots interesting. Without first-class support, every event-triggered app will smuggle structure into `build_spec` prose, which means:
- No two implementations agree on the convention
- The RSI loop can't inspect or improve event-handling
- Compliance checks can't verify the wiring actually works

This is the **single most important spec gap** the Atlas exercise surfaces.

---

## Gap 2 — Surface/channel addressing is implicit and underspecified

**Surfaced by:** all four manifests, but most acutely `atlas-article-capture` and `atlas-on-demand-research`

**Symptom:** Atlas serves a specific Telegram group, not just "the bot's configured channel" (which is morning-briefing's pattern). The same bot may also need to DM the operator with private alerts. And reactions vs. threaded replies vs. group posts are different surfaces with different rules.

The spec has no field that says "this app posts to channel-of-type X with addressing Y." The morning-briefing manifest hardcodes "the bot's configured channel" in `identity.user`. That works for a single-channel bot. Atlas isn't single-channel.

**Workaround used:** I added `{telegram_chat_id}` as a template variable and assumed the operator would configure it manually. I described in narrative which messages go to the group, which are reactions, which are threaded replies. This is unenforced — a buggy app could just as easily DM members or post outside the group.

**Proposed spec extension:**

```json
"surfaces": [
  {
    "id": "primary_group",
    "type": "telegram_group",
    "addressing": {"chat_id": "{telegram_chat_id}"},
    "permitted_actions": ["post", "react", "reply_threaded"],
    "prohibited_actions": ["dm_member", "post_outside_chat"]
  },
  {
    "id": "operator_dm",
    "type": "telegram_dm",
    "addressing": {"user_id": "{operator_telegram_user_id}"},
    "permitted_actions": ["post"],
    "purpose": "private alerts only"
  }
]
```

Apps then reference surface IDs (`"post_to": "primary_group"`) rather than addressing inline. The router enforces the action-list at runtime.

---

## Gap 3 — AGENTS.md guidance is load-bearing but lives outside the manifest

**Surfaced by:** `atlas-article-capture`, `atlas-on-demand-research`, also note-taker

**Symptom:** Three of Atlas's four apps require AGENTS.md guidance blocks for the bot to know when to invoke their CLIs. Note-taker has the same pattern. The guidance is *part of the app's contract* — without it the app doesn't function — but it's stored as prose inside `build_spec`, with the provisioner expected to manually splice it into the bot's AGENTS.md.

**Workaround used:** Each manifest has a `## AGENTS.md guidance block` heading inside `build_spec`. The forge would have to scan for this heading and append the contents to the bot's AGENTS.md at install time.

**Proposed spec extension:** First-class `bot_guidance` array:

```json
"bot_guidance": [
  {
    "section": "Atlas — Article Capture",
    "content": "When you receive a group message containing one or more URLs:\n1. ..."
  }
]
```

The provisioner installs each entry as its own section in the bot's AGENTS.md, and can update / remove it cleanly when the app is upgraded or uninstalled. Today, uninstalling note-taker leaves orphaned AGENTS.md content unless the operator cleans it up.

**Bonus:** with first-class bot_guidance, the RSI loop can propose edits to the guidance (e.g. "when the bot misclassifies these messages, tighten the guidance to ...") and have a versioned target to edit.

---

## Gap 4 — Multi-cadence and event+cron hybrid apps don't fit one-cron-per-app

**Surfaced by:** considered `atlas-daily-digest + atlas-weekly-recap` as one app vs. two

**Symptom:** Daily-digest and weekly-recap share most of their machinery (archive read/write, classification cache, source list, gateway send). They differ only in cadence and synthesis depth. The natural shape is *one app with two cron schedules*. But the morning-briefing manifest has one `StartCalendarInterval` per plist, one CLI entry, one set of signals.

**Workaround used:** I split them into two manifests. They share `atlas_lib/` (declared as a shared scripts directory between two manifests). This works but creates duplication: two manifests, two CLIs, two plists, two sets of signals, two `last_test_*` fields to keep in sync.

**Proposed spec extension:**

```json
"crons": [
  {
    "id": "daily-digest",
    "schedule": "{digest_time} daily",
    "command": "scripts/atlas.py daily-digest",
    "signals": ["DIGEST_SENT:", "DIGEST_FAILED:"]
  },
  {
    "id": "weekly-recap",
    "schedule": "{recap_day} {recap_time} weekly",
    "command": "scripts/atlas.py weekly-recap",
    "signals": ["RECAP_SENT:", "RECAP_FAILED:"]
  }
]
```

The spec already accepts `crons` as a list, but the LaunchDaemon installer only writes one plist per app and the conventions in the gallery treat one app = one cadence. Tightening this to support multi-cron-per-app would let Atlas be 2 apps instead of 4.

---

## Gap 5 — Shared state across apps is implicit

**Surfaced by:** `archive/index.json` and `atlas/optout.json` are written by article-capture, read by daily-digest's dedup logic and weekly-recap's compose pass.

**Symptom:** The spec has `dependencies` (free-form strings) and `app_dependencies` (other app IDs this requires), but no first-class way to say "these three apps share this data file with these access patterns." Today I describe it in `constraints.dependencies` as a string: `"archive/index.json shared with app_atlas_daily_digest writer"`.

**Workaround used:** Narrative description in `constraints.dependencies` + `interface_contract.data_files`. Each app declares the file it uses; nothing enforces consistency across apps (one could change the schema without the others noticing).

**Proposed spec extension:**

```json
"shared_state": [
  {
    "path": "archive/index.json",
    "schema_ref": "schemas/atlas-archive-index.json",
    "access": "append_only",
    "shared_with": ["app_atlas_article_capture", "app_atlas_weekly_recap"]
  }
]
```

The provisioner could then verify schema compatibility across the apps that share a file. The RSI loop could detect schema drift. The compliance checker could flag "this file has writers in two apps but only one declares it as shared."

---

## Gap 6 — Classification taxonomies and other domain vocabularies are not first-class

**Surfaced by:** the 5-bucket taxonomy (`competitive_landscape`, `new_tools`, `use_cases`, `case_studies`, `warnings`) is referenced by all four Atlas apps.

**Symptom:** The taxonomy is repeated as a literal string list in three of four manifests (identity, success_criteria, build_spec narrative). Changing it requires editing four files in sync. The RSI loop has no way to know "these four apps share a taxonomy" — only that they all happen to mention the same five strings.

**Workaround used:** Repeated literally across files. Discipline alone keeps them in sync.

**Proposed spec extension:** Pod-level shared vocabularies, referenced by ID:

```json
"vocabularies": {
  "atlas_buckets": {
    "values": ["competitive_landscape", "new_tools", "use_cases", "case_studies", "warnings"],
    "version": "1.0",
    "owner": "app_atlas_daily_digest"
  }
}
```

Apps reference: `"classification_taxonomy": "@vocab/atlas_buckets@1.0"`. A vocabulary change is a versioned event; the RSI loop and compliance checker can detect drift.

This is lower-priority than gaps 1-3 but matters for the `app-framework differentiator` story — *applications-as-contracts* should include the vocabulary the contract is written in.

---

## Gap 7 — Privacy and consent surfaces are app-level concerns but specced ad-hoc

**Surfaced by:** Atlas's 🤐 opt-out, pinned privacy intro on group join, member-ID hashing, archive deletion on opt-out.

**Symptom:** All four manifests have detailed privacy constraints — but they're free-form strings in `constraints.privacy`. There's no first-class way to declare:
- "this app honors reaction X as an exclusion signal"
- "this app posts a privacy notice on first install"
- "this app hashes identifiers using salt at path Y"
- "this app supports user-initiated bulk deletion via signal Z"

The `user-observation-optout` memory says these must ship with every observation feature from v1. They do, in narrative form, in each manifest. Nothing enforces them.

**Workaround used:** Encoded in narrative across `identity.scope_includes`, `constraints.privacy`, `build_spec` AGENTS.md guidance.

**Proposed spec extension:** First-class `privacy` block:

```json
"privacy": {
  "data_collected": ["URLs from group messages", "hashed member IDs"],
  "retention": "archive: indefinite; capture-log: 90 days; optout: indefinite",
  "opt_out_signals": [
    {"signal": "telegram_reaction:🤐", "scope": "per_message", "action": "delete_archive_entry"}
  ],
  "consent_notice": {
    "trigger": ["install", "new_group_member"],
    "channel": "primary_group_pinned",
    "content_ref": "docs/atlas-privacy-notice.md"
  },
  "identifier_hashing": {
    "salt_path": "atlas/.capture-salt",
    "algorithm": "sha256"
  }
}
```

This is *exactly* the kind of structure that lets Evolve's `safety-as-flagship-feature` be inspectable, not just claimed. A privacy block makes the safety story machine-checkable.

---

## Gap 8 — Refusal templates, rate limits, budgets — generic primitives expressed per-app

**Surfaced by:** `atlas-on-demand-research`

**Symptom:** Per-member rate limits, pod-wide budget caps, off-topic refusals, strategy refusals — these are all generic governance primitives that will recur in every interactive app. Each one has bespoke schema inside the app's own `atlas/research-config.json`. The spec has no shared notion of *governance config*.

**Workaround used:** App-local config file. Future interactive apps will reinvent the same fields.

**Proposed spec extension:** Cross-app `governance` schema with rate-limit, budget, and refusal primitives, referenced by ID. Out of scope for v1, but worth tracking.

---

## Gap 9 — Cost telemetry has no standard shape

**Surfaced by:** `atlas-on-demand-research` (per-query cost), `atlas-daily-digest` (per-classification cost), `atlas-weekly-recap` (synthesis cost).

**Symptom:** Each app tracks cost in its own log file with its own field name. The spec mentions `rsigrade_signals` as named metrics, but cost isn't called out as a first-class concern. Per `embedding-provider-config`, cost rollups are blocked on OC emitting per-call cost events anyway. But the app-side schema should be ready when OC ships them.

**Proposed spec extension:** Standard `cost_telemetry` block declaring how an app emits cost signals, what fields they contain, and what budget categories they roll up into. Wait for OC upstream before designing.

---

## Gap 11 — Audience scoping is not first-class

**Surfaced by:** atlas-article-capture, atlas-on-demand-research — and, more concretely, by Pod-Admin asking "wait, can strangers DM atlas?" mid-build, which revealed that every interactive app needs an answer to *who is allowed to do what, where*.

**Symptom:** Atlas needs to distinguish operator / member / stranger across two contexts (approved group / DM / foreign group). Strangers in DMs would otherwise drain the research budget. Strangers in foreign groups would otherwise cause Atlas to behave anywhere it's added. There is no spec field for this — every interactive app re-invents the wheel.

I built it as `atlas_lib/guard.py` + `atlas_guard.py` CLI + `atlas/operator.json` config. ~220 lines. Every future interactive Evolve app will need the same scaffold. Today, each one would re-implement it from scratch and the implementations would diverge.

This is the gap a member of the OC-enthusiast group nearly walked into within the first 60 seconds — "can I DM the bot?" The answer needs to be a first-class manifest decision, not an after-thought.

**Workaround used:**

- New module `atlas_lib/guard.py` exposing `classify(user_id, chat_id, chat_type, *, bot_id) → (context, role)`.
- New CLI `atlas_guard.py classify` for AGENTS.md to optionally call up-front.
- New config file `atlas/operator.json` declaring operator user_id + approved group chat_ids.
- New AGENTS.md guidance section describing the routing matrix (operator/member/stranger × approved_group/dm/foreign_group).
- Both atlas_capture and atlas_research import guard and gate every action internally as defense-in-depth.
- Telegram membership verified via `getChatMember`, cached for 5 min per (user, chat).

This works — but it's a private convention. The spec doesn't know it exists.

**Proposed spec extension:** First-class `audience_scoping` block:

```json
"audience_scoping": {
  "operator_required": true,
  "approved_surfaces": [
    {
      "surface_id": "primary_telegram_group",
      "surface_type": "telegram_supergroup",
      "addressing": {"chat_id_var": "{telegram_chat_id}"},
      "default_role_in_surface": "member"
    }
  ],
  "role_capabilities": {
    "operator": ["research", "admin", "capture", "opt-out", "opt-out-all"],
    "member":   ["research", "capture", "opt-out", "opt-out-all"],
    "stranger": []
  },
  "membership_verification": {
    "method": "telegram_get_chat_member",
    "cache_ttl_seconds": 300
  },
  "operator_bypasses": ["rate_limit", "budget_cap"],
  "config_path": "atlas/operator.json"
}
```

The platform's manifest processor then:
- Installs the operator config template at install time
- Wires every CLI invocation through the platform's shared guard (so apps don't each ship their own copy)
- Verifies on every event that the actor's role permits the requested capability
- Surfaces a single "trust boundary" view in the admin UI: which apps are exposed to which audiences

**Why it matters strategically:** This is where Evolve's `safety-as-flagship` story gets teeth. The audience block is the **machine-checkable trust boundary**. The Plex-test user (`design-constraint-mildly-tech-capable`) should never have to debug "a stranger drained my budget" — the platform refuses by default. Today, every app has to remember to do this themselves; with v7, the platform does it.

This gap also closes a coupling that surfaces in Gap 2 (surface addressing) and Gap 7 (privacy block). The privacy block references the audience as its trust boundary; the surface addressing references the audience as its routing rule. These three concepts (surfaces, audiences, privacy) form a coherent unit and should be designed together.

---

## Gap 10 — Manifest installation is not symmetric with uninstallation

**Surfaced by:** considering what happens when Atlas is retired.

**Symptom:** Installing the four Atlas apps writes:
- 4 plist files in /Library/LaunchDaemons/
- Append to AGENTS.md (multiple sections)
- Files in scripts/, atlas/, archive/, recap/, digest/
- Entries in network.json
- Entries in shared archive/index.json from other bots? (no — Atlas is sole archive owner)

Uninstalling should reverse this cleanly. The spec doesn't say how. Per `low-friction-bot-creation` gap #3 (bot lifecycle — archive/retire), this is already a tracked Evolve gap. But it has an app-level version: how does an *app* uninstall?

**Proposed spec extension:** `uninstall` field with declarative cleanup steps, or an `uninstall_command` CLI entrypoint. The provisioner runs it on retire.

---

## Summary — what to do with this

**Highest leverage to fix before v1.1 (coordinated as schema v7):** Gap 1 (event triggers), Gap 3 (bot_guidance as first-class field), Gap 7 (privacy block), **Gap 11 (audience scoping)**.

These four form a coherent unit — the "interactive app primitives" set. They reference each other: audience_scoping is the trust boundary for privacy; event_triggers fire only for authorized audiences; bot_guidance routes events to the right CLI based on audience. Doing any one alone produces an awkward partial solution. Doing all four together produces a coherent v7.

**Worth specifying but not v1.1:** Gap 2 (surfaces — partly subsumed by Gap 11's `approved_surfaces`), Gap 4 (multi-cron), Gap 5 (shared state), Gap 6 (vocabularies).

**Wait on dependencies:** Gap 8 (governance — wait for first 2-3 interactive apps), Gap 9 (cost — wait for OC upstream), Gap 10 (uninstall — combine with bot-retire work).

**The bigger meta-point:** the manifest framework is *good* — it expressed all four Atlas apps without breaking — but the friction it imposed for event-triggered, audience-scoped, privacy-bearing apps is exactly the friction that will accumulate as the gallery grows. Closing gaps 1, 3, 7, and 11 in a single coordinated spec revision (schema v7) would let the `app-framework differentiator` story rest on "applications are inspectable contracts" with real teeth — including a machine-checkable trust boundary that's the bedrock of the `safety-as-flagship-feature` story.

**Related memories:**
- `app-framework-differentiator` — why these contracts matter
- `v1-1-substrate-adoption-priority` — where this work fits
- `safety-as-flagship-feature` — Gap 7 + Gap 11 together unblock the safety story
- `user-observation-optout` — the requirement Gap 7 makes machine-checkable
- `design-constraint-mildly-tech-capable` — the Plex test that Gap 11 protects against
- `conversational-bot-creation-wizard` — the wizard would need to fill these new fields, so spec changes feed the wizard design
