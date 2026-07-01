# Per-bot user profile — v2 design

**Status:** Draft, 2026-05-07
**Companion to:** [`docs/spec-rsi-architecture-2026-04-17.md`](spec-rsi-architecture-2026-04-17.md)
**Replaces:** the user-facing surface of the prior RSI layer-4 adjacency-profile spec (internal design archive).

## Goal

Each bot maintains a **passive, private, per-user profile** that grows from observed conversation. The bot reads it to tailor behavior; the user reads/edits it to keep it accurate; the admin sees only that it exists. SOUL.md describes who the *bot* is; this profile describes who the *user* is.

The cleanup pass on the admin "Bot" tab (commit landing today) already separated admin config from profile content in the UI. This spec covers everything below the admin surface: where the profile lives, how it grows, who can see it.

## Today (the three things to fix)

1. **Two stores, no handshake.** `{shared_dir}/profiles/<bot_id>.md` is populated by the inferrer; `{shared_dir}/profiles/users/<user_key>.json` is populated by the evo wizard. They overlap in subject matter and never sync.
2. **Privacy is inverted.** Both files live in `{shared_dir}/profiles/`, which is `evolve`-readable. The "private user profile" is in fact admin-readable.
3. **Approval-gated growth.** The inferrer emits `ConfirmProfileField` proposals; facts only land after the user approves each one. This is friction without consent value — the user is being asked to re-confirm things they already said.

## Decisions

### D1. One store, bot-side, per user

**Path:** `~/.openclaw/profiles/<user_key>.md` on the bot user's home (e.g. `/Users/team-bot-c/.openclaw/profiles/alice.md`).

- Single-user bot → one file (e.g. `pod-admin.md` for Pod-Admin's personal bot).
- Multi-user bot (Team-Bot-C) → one file per `user_key`.
- File is owned by the bot user, mode `600`.
- The shared `{shared_dir}/profiles/` location is **deprecated** — see Migration.

**Why bot-side, not shared:** the file contains the user's intimate knowledge. The admin server (running as `evolve`) should not be able to read it. macOS user-home isolation already enforces this when ACLs are not extended.

**Rejected:** keeping it in `{shared_dir}` with a tightened ACL. Too easy for a future deploy step or generator to extend the ACL by accident.

### D2. Format: existing Markdown + frontmatter, with one new section

Keep the current shape ([`packages/analyzer/profile/storage.py`](../packages/analyzer/profile/storage.py)). Frontmatter for structured fields, body for sections.

**Sections (display order):**

| Section | Purpose |
|---|---|
| Goals | What the user wants from *this bot*. Driven by evo wizard's PHASE_GOALS. **NEW.** |
| Demographics | Age range, location, language, etc. — only what the user volunteers. |
| Vocation | Job, role, what they work on. |
| Interests | Hobbies, recurring topics. |
| Family | Family-of-self facts only. Names, relationships, ages of own kids/partner. |
| Communication Preferences | Tone, response length, formality. |
| Values | What they care about. |
| Constraints | Hard limits — "don't text me before 9am", allergies, etc. |
| Audit Log | Append-only log of inferrer writes and user edits. |

The `Goals` section is new. Everything else exists today and stays. The `Constraints` section gets a sharper definition (it was vague before).

**Frontmatter:**
- `user_key: str` — required, matches filename
- `bot_id: str` — required, the bot this profile is scoped to
- `schema_version: int = 2` (bump from 1)
- `created_at`, `updated_at` (ISO-8601 UTC)
- `tracking_enabled: bool = true` — DNT switch (see D5b)

**Removed from frontmatter:** `archetype`, `surfacing_cadence`, `timezone` — those are *bot-admin* config, not user-profile content. They live in [`{shared_dir}/profiles/<bot_id>.md`](../packages/analyzer/profile/storage.py) (or wherever we land bot config in a follow-up cleanup; the cleanup pass we just shipped left them there for now).

### D3. Growth model: passive, no per-fact approval

The `user_profile_inferrer` generator (renamed from `profile_inferrer`) writes facts directly into the appropriate section. No proposal queue, no per-fact approval.

**Confidence threshold replaces approval as the gate.**

- Default threshold: `0.75`. Tunable per section.
- Source: structured `(noun, verb, mood, engagement)` observation tuples + light LLM extraction. **No raw transcript content stored in the profile** — only the synthesized fact.
- Schedule: same as today (weekly per bot), or event-driven on session_end if the cost is acceptable.

**Inferrer is the only writer for passive growth.** SOUL.md, evo wizard, and the user's own edits also write — but they're explicit, not passive.

**Memory note:** "remember what I just told you" is implicitly authorized. We are not adding a confirmation step for things the user already said.

### D4. Sensitivity-aware silence (not a denylist)

Rather than a categorical block on health/finance/etc., we modulate the threshold by section:

- `Family`, `Constraints`, `Demographics` → require `0.85+` (multiple confident observations across sessions).
- `Goals`, `Vocation`, `Interests`, `Values`, `Communication Preferences` → `0.70` is fine.

If the user explicitly volunteers something ("remember that I have type 1 diabetes"), the inferrer treats that as a direct write at full confidence — bypasses the threshold. This is the "ask first" inversion: passive caution, explicit-instruction permissive.

**Open question:** is there a category we *never* passively record (mental health diagnoses, legal trouble)? V1 says no — the threshold handles it. We revisit if we see misfires.

### D5. User editability — `evo profile` is the primary surface

Three paths, in order of expected use:

1. **`evo profile`** — a structured review/edit command in the existing evo dispatch ([`evo/dispatch.py`](../packages/admin/evolve_admin/evo/dispatch.py)). The user runs it in chat with their bot:
   - Bot reads `~/.openclaw/profiles/<user_key>.md` and summarizes section by section.
   - User can correct ("remove the marathon training note"), expand ("add that I'm vegetarian"), or accept-as-is.
   - Bot writes back; Audit Log records `source: user`, confidence 1.0.
   - For multi-user bots, the speaker's `user_key` scopes the view — Alice can only ever see Alice's profile.

2. **In-conversation patterns** — the bot recognizes inline edit cues without `evo profile`:
   - "remember that X" → write to best-fit section, confidence 1.0
   - "forget X" / "that's not right" → remove the matching fact, log to Audit Log
   - "what do you know about me?" → reads its own profile and summarizes
   - These are the same writes `evo profile` would do; just no formal review surface.

3. **File-level** — power users open `~/.openclaw/profiles/<user_key>.md` directly. Header comment explains the format. Inferrer respects user-edited content (doesn't overwrite); the existing parser already preserves user sections.

The evo flow is the discoverable surface; the in-conversation patterns are the ergonomic one; file-level is the escape hatch.

### D5b. Do-not-track (DNT)

The user can disable passive profile growth at any time via `evo profile dnt on` (and re-enable with `evo profile dnt off`).

**Mechanism:**
- Frontmatter field `tracking_enabled: bool = true` (default true).
- When `false`:
  - Inferrer skips this `user_key` entirely — no observations consulted, no LLM call, nothing written.
  - All section content is wiped on the `true → false` transition. Audit Log too. The frontmatter (with the flag set to false) is preserved.
  - `evo profile` still works for *explicit* user-volunteered facts: the user can manually populate sections under DNT, and the inferrer still won't touch them. DNT gates passive observation, not user agency.
- When toggled `false → true`:
  - Inferrer resumes on its next cycle. No backfill from past observations — DNT is a forward-looking switch, and reaching back into observation tuples to reconstruct a profile would defeat the point.

**Visibility to admin:** none. The status surface in D6 stays strictly binary `has_content`. A user who opted out is indistinguishable from a fresh user with no facts yet — the choice is theirs, not the operator's to know.

**Cross-feature reuse:** DNT here is the prototype for any future observation/inference feature (security_warden capture, upcoming behavior-cluster analyzers). Same shape: per-user flag, wipe-on-flip, invisible to admin.

### D6. Privacy boundary to admin

> **Amended 2026-06-22 (gated behind a future build — see [spec-user-directory §7](spec-user-directory-2026-06-22.md)).**
> Under the current trust assumption — **Evolve admin == the bot's data owner** — the operator
> elected to surface profile *content* to the admin UI. When that phase ships, the
> `set_evolve_read_acl` carve-out below is removed (or content is read via the existing
> `sudo /bin/cat` path) and the Person card renders the profile. **DNT is reframed, not deleted:**
> it remains a genuine "don't profile me at all" opt-out (wipe-on-flip); only the "private *from the
> operator*" framing is dropped. The binary `has_content` mechanism below remains the live behavior
> until that phase lands, and is also the prototype for roadmap **R3** (operator-masked,
> bot-owner-scoped PII), which would *re-establish* a content boundary — crypto-enforced — for a
> bot owner distinct from the pod operator.

The admin server gets a binary signal — "any user has a profile with content" / "none yet" — via a small status file:

`~/.openclaw/profiles/.status.json`

```json
{ "any_has_content": true }
```

That's it. No user list, no per-user state, no DNT visibility. The cleanup we just shipped plumbs `has_content` through the API; Project B just changes the source from "read the .md" to "read the .status.json".

- Written by the inferrer, the wizard, the `evo profile` flow, and the DNT toggle whenever any profile is touched.
- File mode `644` (readable by `evolve`).
- Profile `.md` files themselves remain `600` and bot-user-owned.

**Why no user list:** if `users: ["alice", "bob"]` were exposed, an admin who sees Bob disappear from the list could infer "Bob opted out" or "Bob deleted his profile." The binary `any_has_content` collapses all those states into one.

**`set_evolve_read_acl`** ([`deploy.py:659`](../packages/admin/evolve_admin/deploy.py:659)) gets a carve-out: `~/.openclaw/profiles/*.md` is excluded from the ACL grant. Same pattern as the existing `credentials/` exclusion. Only `.status.json` is evolve-readable.

### D7. Evo wizard writes direct, no per-user JSON

**Correction from initial draft:** the wizard runs server-side in the admin process (as the `evolve` user), not in the bot's claw. The route is `POST /api/evo/dispatch` ([`evo/dispatch.py`](../packages/admin/evolve_admin/evo/dispatch.py)); the dispatch handler holds the conversation state in `{shared_dir}` and produces reply text the bot relays back to the user. Extraction happens server-side via a separate Haiku call. This is different from the inferrer (which runs in the bot's claw at session_end), and the wizard write path therefore needs to cross the user boundary.

After this spec lands:

- The wizard maps `state.extracted` to v2 sections at WRAP and writes a UserProfile to `~/<bot>/.openclaw/profiles/<user_key>.md`.
- Because the wizard runs as `evolve` and the destination is bot-owned, writes go via the established **`/tmp` staging + `sudo /bin/cp` + `sudo /usr/sbin/chown` + `sudo /bin/chmod 600`** pattern documented in the project [CLAUDE.md](../CLAUDE.md). This is the same pattern admin uses for `openclaw.json` and other bot-owned configs — well-trodden, no new sudoers grants needed.
- The wizard also refreshes `~/<bot>/.openclaw/profiles/.status.json` via the same sudo path. The bit is computed by **aggregating across all profiles in the directory** (multi-user-correct: Bob's wipe doesn't drop Alice's True). The admin sudo path uses ``read_user_profile_from_bot`` (direct read → ``sudo /bin/cat`` fallback) to read each profile and OR their ``has_content()``.
- The legacy store at `{shared_dir}/profiles/users/<user_key>.json` and `evo/wizard/profile.py` are **removed**.
- `_commit_profile_safely` in [`engine.py:1753`](../packages/admin/evolve_admin/evo/wizard/engine.py:1753) becomes the bridge: same extraction, new write path.

**Field mapping** (wizard `extracted` keys → v2 sections):

| Wizard field | Section | Format |
|---|---|---|
| `name` | Demographics | `Goes by: <name>` |
| `role` | Vocation | `- Role: <role>` |
| `environment` | Vocation | `- Environment: <environment>` |
| `current_tooling` | Vocation | `- Tools: A, B, C` |
| `current_workflow_notes` | Vocation | (free-form paragraph) |
| `top_goals` | Goals | one bullet per goal |
| `pain_points` | Goals | bulleted under "Pain points to address:" |

**What the wizard adds vs. inferrer:** wizard-supplied facts are confidence 1.0 (the user explicitly answered the question). Audit Log entries say `source: wizard`.

**Re-run merge semantics.** A wizard re-run does NOT do wholesale section replacement. The mapper uses **line-prefix ownership**: only lines matching wizard-owned prefixes (`Goes by:`, `- Role:`, `- Environment:`, `- Tools:`) get replaced; other lines (inferrer-added bullets, user-edited prose, the workflow-notes paragraph) survive. Goals has no owned prefix — wizard goals are appended with exact-match deduplication so multi-run accumulation works. This is what keeps inferrer-accumulated facts alive when a user re-invokes the wizard months later. Line-prefix ownership table lives in [`user_profile_writer._SECTION_OWNED_PREFIXES`](../packages/admin/evolve_admin/evo/wizard/user_profile_writer.py).

**Why not run the wizard bot-side?** Two answers. (1) It's a much bigger change — the wizard's state machine, extractor, and dispatch all live admin-side; moving them is out of scope for v2. (2) The privacy concern that motivated bot-side inference (LLM seeing the user's data) is weaker for the wizard, which only sees what the user is *typing in response to direct questions*. The wizard is more like a conversational form than a passive observer; the user is in control of what they share by definition.

### D8. Multi-user (Team-Bot-C)

**Per-user file, in the bot's home.** Cleanest separation: Alice can't see Bob's profile because they're separate files.

- `user_key` is supplied by the gateway integration (Slack user ID, Telegram user ID, etc.) — same key already passed into the wizard ([`engine.py:61`](../packages/admin/evolve_admin/evo/wizard/engine.py:61)).
- The bot reads only the speaking user's file at any given turn. SOUL.md still loads pod-wide.
- **Cross-user mentions**: if Alice tells the bot "Bob's birthday is next week", that fact lands in *Alice's* `Family` section (or `Interests` — design call), **not** Bob's profile. Each profile is what *that user* told the bot, never what others said about them.

**Why this design**: Bob hasn't consented to the bot remembering things about him via Alice. If Bob also chats with the bot and volunteers his birthday himself, it goes into Bob's profile.

For single-user bots, `user_key` is whatever the bot's primary surface uses (Slack ID for team-bot-a, Telegram ID for admin-bot, etc.). For a personal CLI bot with no channel, the literal string `"primary"`.

## Migration & removals

**To remove:**
- `ConfirmProfileField` proposal type ([`packages/analyzer/schema/proposal.py`](../packages/analyzer/schema/proposal.py))
- The applier at [`packages/analyzer/arbiter/appliers/confirm_profile_field.py`](../packages/analyzer/arbiter/appliers/confirm_profile_field.py)
- The proposal-emitting path of `wire_default_inferrer` (the LLM-extraction step stays; the proposal-emit step gets replaced with direct write)
- `{shared_dir}/profiles/users/<user_key>.json` store and the wizard's `profile.py` module
- The shared `{shared_dir}/profiles/<bot_id>.md` file (if it exists on a pod, content is migrated to per-user bot-side files)

**To migrate (per-pod):**
- For each bot in `network.json`: read `{shared_dir}/profiles/<bot_id>.md`, copy populated sections to `~/<bot>/.openclaw/profiles/<user_key>.md`. The `user_key` for a single-user bot is whatever the wizard last used (read from the soon-to-be-removed JSON store), defaulting to `"primary"`.
- For Team-Bot-C specifically: pre-create empty profiles for the known Star Springs Ranch users; the inferrer fills them as those users actually chat.
- After migration, the shared file gets archived to `{shared_dir}/profiles/_archive/<bot_id>.md.<date>` (don't delete — the only copy of months of inferred facts).

**Schema bump:** v1 files have `archetype`/`surfacing_cadence`/`timezone` in frontmatter; v2 files don't. The migrator strips those and writes v2.

**Order of operations:**
1. Land bot-side path + status file plumbing (no behavior change yet — both paths exist)
2. Switch inferrer to direct-write
3. Switch wizard to direct-write, drop per-user JSON
4. Add `evo profile` view/edit flow + DNT toggle
5. Run pod migration script
6. Remove `ConfirmProfileField` and shared-path code

## Inferrer details

| Aspect | v2 |
|---|---|
| Schedule | Weekly per bot (unchanged) |
| Source | Observation tuples (no transcript content) |
| Output | Direct write to `~/.openclaw/profiles/<user_key>.md` + status file |
| Confidence | Default 0.75; per-section overrides for sensitive sections (D4) |
| Threshold for explicit user volunteer | bypassed (treated as confidence 1.0) |
| Audit Log entry | every write, with source (`inferrer`/`wizard`/`user`/`assistant`) |

**Staleness is deferred to v3.** v2 stamps each fact with `last_observed`; v3 will introduce decay/restate logic. Keeps the v2 surface tight.

## Open questions

1. **`user_key` for personal bots**: hardcode `"primary"`, or pull from the OS account name, or from the gateway integration? Affects how migration handles Pod-Admin's personal bot (team-bot-a). Probably `"primary"` is fine since there's no meaningful collision.
2. **Where does "Goals" really belong** — in the user profile, or as a separate `~/.openclaw/objectives.md` per user? Goals shape the bot's behavior more than its conversation; they might want their own file. Leaving in profile for v1 since it cuts complexity.
3. **Does the inferrer write SOUL.md too?** No (out of scope). SOUL.md remains hand-authored / wizard-authored. But should the inferrer surface *suggestions* for SOUL.md changes? Probably belongs to a different generator entirely. Defer.
4. **Conversational forget/correct**: which subsystem owns the pattern matching? OpenClaw bot prompt logic, or a server-side component? Likely the bot's system prompt instructs it to recognize the pattern and call a tool. Tool design TBD.
5. **Cross-user fact handling at scale**: D8 says facts about user B told by user A go into A's profile, not B's. For Team-Bot-C at 5+ users this might create noisy Family/Interests sections cluttered with "Bob's birthday is X". Revisit after observation.
6. **DNT discoverability**: how does a user *learn* that DNT exists? Mentioning it in the bot's first onboarding message would surface it; never mentioning it makes it a power-user feature. The discoverability vs. cognitive-load tradeoff is real. Probably: evo wizard surfaces it once at WRAP ("you can run `evo profile` any time to see what I've recorded, or `evo profile dnt on` to opt out").
