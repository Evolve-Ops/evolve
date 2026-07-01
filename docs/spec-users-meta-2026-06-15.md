# Spec: users — the per-bot user model (coordinator charter, 2026-06-15)

**Status:** seed (carved via `/design` 2026-06-15)
**Aspect id:** `users`  *(renamed from `identity` 2026-06-15 — operator legibility; matches the Users page. The domain word "identity" still appears below in its IAM sense — admitted identity, cross-platform identity — and is unchanged.)*
**Design source of truth:** [spec-per-bot-users-management-2026-05-29.md](spec-per-bot-users-management-2026-05-29.md)
(the admission gate + Users page) and [spec-user-roster-and-roles-2026-06-07.md](spec-user-roster-and-roles-2026-06-07.md)
(roles, capabilities, the overlay). This charter indexes the user-model corpus; it
does not restate it.

---

## 1. Mission

The **per-bot user model** — *who a bot's users are, how they are admitted, what
they are allowed to do, and ensuring the bot has a coherent, complete, consistent
picture of all of them.* This is the substrate under the **Users page** (pod admins,
passphrases, primary user, per-channel approvals, roles, block/disconnect, email
alias). It spans:

- **Admission** — the OC-native allowlist + pairing flow (who gets in).
- **Roster + roles + capabilities** — the overlay (`{shared_dir}/rosters/{bot_id}.json`):
  role per identity, capability bundles, engagement surfaces, sticky blocks.
- **Resolution** — name ↔ stable_id, display-name enrichment via workspace tokens,
  cross-platform identity questions.
- **Activity** — per-user last-seen / counts surfaced on the page.
- **Store coherence (the load-bearing invariant)** — the bot's runtime view of its
  users must equal Evolve's admin roster. *They diverge today; closing that gap is the
  carve trigger (§6).*

## 2. Why an aspect (carve rationale)

The **Users page had no META owner** and was absent from the routing map, yet it has a
rich spec corpus (per-bot-users-management, user-roster-and-roles, user-profile,
user-tier-control, multi-user-alias, user-pushback-signal) and a dedicated code surface
(`routes_bot_users.py`, `roster_overlay.py`, `evo/name_resolver.py`, `user_activity.py`,
the plugin's `RosterTools.ts` / `roleResolver.ts` / `senderRegistry.ts`). Folding it into
`user-value` would conflate two orthogonal concerns: user-value owns *the bot's value
journey* (is it useful?); `users` owns *who the users are and what they can do*. The
diagnosed bug (§6) is an identity-coherence failure, not a value-delivery one — a bot can
be perfectly useful to a known user while blind to a registered-but-unobserved one.
Operator confirmed a new aspect over the `user-value` fold (2026-06-15).

## 3. Inherited design corpus

- [spec-per-bot-users-management-2026-05-29.md](spec-per-bot-users-management-2026-05-29.md) — **the admission gate + Users page** (Phases 1, 1.1, 2 shipped)
- [spec-user-roster-and-roles-2026-06-07.md](spec-user-roster-and-roles-2026-06-07.md) — **roles, capabilities, the overlay, the 4 enforcement layers** (Phases A & C shipped — overlay/roles/block + the daemon capability check, sender capture, and Layer-4 speaker-context injection; **Phase B (the Layer-2 tool-loading filter + the Layer-3 app-capability helper) did NOT ship** — that spec still carries `Status: Draft. Pre-implementation`, per the R1a enforcement audit, docs/audit-r1a-enforcement-matrix-2026-06-30.md, #3378); §"Open follow-ups" names the `list_admitted_users` gap that *is* this bug
- [spec-user-profile-2026-05-07.md](spec-user-profile-2026-05-07.md) — the per-user profile model
- [spec-user-tier-control-2026-05-26.md](spec-user-tier-control-2026-05-26.md) — per-user cost/tier control
- [spec-multi-user-alias-2026-06-01.md](spec-multi-user-alias-2026-06-01.md) — per-user send-as alias (the page's "Email alias" section)
- [spec-user-pushback-signal-2026-05-30.md](spec-user-pushback-signal-2026-05-30.md) — the user-pushback signal (signal *producer* quality is `reports`' call; the user model is here)
- [spec-user-directory-2026-06-22.md](spec-user-directory-2026-06-22.md) — **the unified per-bot Person model** (the **R2 realization**): platform identities + primary/secondary emails + contacts (an address book the bot acts toward) + the bot read/write directory tool, all behind one `resolve_person` seam. Adds roadmap **R3** (principal separation). Carve trigger: the address-book case — a personal-assistant bot replied "I updated my memory with their emails," having no canonical contact store (§0 there).

## 4. Boundary / hand-offs

- **Users page presentation** (tokens, primitives, table layout, chips) → `ui` (co-owns presentation everywhere).
- **Capability / MCP-tool-allowlist enforcement** (Layer 2 — the iron-clad security layer that loads only a role's tools) → co-owned with `edr`/security; `users` owns the *role/capability model + roster*, security owns the *gateway enforcement that a jailbreak can't cross*. The apps boundary already routes gateway `role_capabilities` enforcement to `edr`/security.
- **Cross-bot roster admin via the evo tray** (Path C — "evo, block alice on atlas") → the *tool plumbing* is `users`'s; the *tray assistant's reasoning/identity* is `evo-asst`'s.
- **Per-user cost attribution / tier economics** → `model-tiers` (usage = cost); `users` feeds the per-`(channel, user_id)` activity it owns.
- **Signal/alert producer quality** on roster signals (`roster_member_left`, auto-admit drift, user-pushback) → `reports`.
- **Add-bot wizard's consent/owner seeding** → `user-value` (wizard) sets initial roster state; `users` owns the model thereafter.

## 5. Invariants (seed — ratchet as the aspect matures)

1. **One source of truth, projected — authority flows roster → bot, never the reverse.**
   The Evolve roster (allowlist + overlay + name resolution + activity) is canonical and
   verified (IDs channel-captured); the bot consumes a *projection* of it and must **not**
   treat its hand-curated identity files (`USER.md`, contact cards, daily logs) as the user
   directory of record. A user Evolve admitted is a user the bot can see and name; the bot
   never hand-writes IDs into a parallel store (that drifts, and risks unverified data).
2. **Identity is `(platform, stable_id)`** — the admission key, matched at the channel
   boundary below the LLM (per per-bot-users-management). Roles attach to admitted
   identities; a role never weakens the admission decision.
3. **Enforcement lives outside the LLM wherever the worst-case bypass causes external
   effects** (sent message, exec, file write, money). The LLM is the gate only where the
   worst case is "the bot said something it shouldn't have" (user-roster-and-roles §Principle).
4. **Role/engagement/block mutations are admin-daemon API calls, not file writes** — even
   from natural-language chat; the daemon checks the *requesting* identity's role on the
   *target* bot. The LLM cannot promote itself.
5. **Roster overlay files are evolve-owned, atomic temp-file+rename**, mode 644 (read by
   the bot gateway), under `{shared_dir}/rosters/`. A new roster-file consumer reads, never
   forks, the canonical roster.

## 6. First decision — the store-coherence bug (carve trigger)

**Symptom (a multi-user Slack bot — `team-bot-a` — 2026-06-15):** the operator asked the
bot to explain a new system to four of its admitted users (call them **P1–P4**). The bot
reached **P1** and **P2** but replied *"I don't have Slack IDs on file for [P3] or [P4]."*
Yet the Users page shows all four admitted **with their Slack IDs** (P4's is `U0XXXXXXXX`).

**Root cause (verified in code; confirmed by the bot's own self-report 2026-06-15):** there
are **three** user stores, and the bot treats its own hand-curated one as authoritative
while the verified roster is invisible to it.
- **Store A — the Evolve roster** (what the Users page renders; *verified, complete*):
  allowlist + overlay ([roster_overlay.py](../packages/admin/evolve_admin/roster_overlay.py))
  + display names ([evo/name_resolver.py](../packages/admin/evolve_admin/evo/name_resolver.py))
  + activity ([user_activity.py](../packages/admin/evolve_admin/user_activity.py)). IDs are
  captured at the channel boundary from real platform events; names enriched via the
  workspace token. Has all four users with names + IDs — including P4's verified `U0XXXXXXXX`.
- **Store B — the bot's hand-curated identity files** (*the store the bot actually trusts*):
  `USER.md` / `TOOLS.md` / contact cards / daily-memory logs. **`USER.md` is a platform-wide
  OC convention** — every bot ships the "Personal Assistant base (SOUL.md + USER.md)"
  (scanner.py:1868) and OC injects these identity files into context every turn
  ([spec-prompt-injection-scanner-2026-05-10.md](spec-prompt-injection-scanner-2026-05-10.md)).
  This is freeform prose the bot authors and reads as the user directory of record. It is
  **stale, partial, and drifts** — the bot found P1/P2 in `USER.md`, P3's ID buried in an
  old daily-memory log (a "secondary store" it searched only after digging), and P4's
  contact card said `TBD`.
- **Store C — runtime conversational memory**: the thread history + current turn's sender,
  captured per-run by [senderRegistry.ts](../packages/plugin/src/util/senderRegistry.ts) —
  the *ephemeral* layer.
- **No bridge from A.** [RosterTools.ts](../packages/plugin/src/tools/RosterTools.ts) exposes
  only *mutations* (`roster_set_role/block/unblock`, `channel_set_newcomer_mode`), each
  requiring a stable_id the caller already holds. There is no read/list/lookup tool, and the
  per-turn context injection carries only the *current speaker's* role, not the full roster.
  So the bot never sees Store A — it reasons over Store B (its markdown) and Store C
  (conversation), both of which lack P4's verified ID that Store A has.

**The self-reinforcing-drift trap (the dangerous part):** because the bot trusts `USER.md`
and has no path to the roster, its instinct on a gap is to **write the missing ID into
`USER.md`** — deepening the divergence, and risking *wrong* data (hand-typed IDs are
unverified; the roster's are channel-captured). The bot's proposed "fix going forward" —
hand-adding the missing ID to `USER.md` — is the bug perpetuating itself. Authority is
flowing the wrong way.

**Fix (decision) — invert the authority: Store A is canonical, projected into the bot;
the bot stops maintaining a parallel directory.** Four pieces:

1. **Per-turn roster digest injection.** Extend the existing speaker-context injection
   (`before_prompt_build`, user-roster-and-roles Phase C.4 / [roleResolver.ts](../packages/plugin/src/util/roleResolver.ts))
   from "who is talking now" to "here is your full roster": one compact line per admitted
   user — display name, @handle, stable_id, role, engagement surfaces, rights summary.
   Size-bound it (cap N, summarize overflow). Then "explain X to [these users by name]"
   resolves for everyone Evolve admitted, and the injected roster *outranks* `USER.md` for IDs.
2. **A roster read tool** (`roster_lookup` / `list_admitted_users`) for on-demand lookups
   beyond the injected digest — the tool user-roster-and-roles §"Open follow-ups" already names.
3. **Repurpose `USER.md` as a roster projection, not a hand-curated source.** Evolve
   refreshes the contact/ID section of `USER.md` *from* the roster (or the bot is taught to
   treat the injected roster as authoritative for IDs and `USER.md` as relationship/prose
   only). Stops the drift at its source and ends the operator hand-carrying IDs the system
   already has.
4. **A single canonical resolver** that joins allowlist + overlay + name_resolver + activity
   into one roster object, consumed *identically* by (a) the admin Users page GET and (b) the
   per-turn injection / `USER.md` refresh. Same code path → admin and bot cannot diverge.

The activity column corroborates the original failure: the two missed users (low/no recent
activity) were the two whose IDs weren't in `USER.md`'s primary section; the two reached
(substantial history) were documented there. None were retrieved from the roster —
confirming the bot never consults Store A.

**Open scope question (investigate before F1 dispatch):** is `USER.md` safe to repurpose as
a roster projection on every bot, or do some bots rely on its prose for relationship context
the roster doesn't carry? Likely a *roster-managed contacts block* appended/refreshed by
Evolve, leaving the bot's prose intact. Resolve in the F1 design pass.

**Deploy:** admin-side resolver/route changes → admin-ui kickstart; plugin (TS) injection +
read tool → per-bot gateway kickstart + `sudo evolve-admin deploy <bot>`. Canary-gated
(`pod.release.mode=canary`).

## 7. Backlog (seed)

- **Canonical roster resolver** — extract the allowlist+overlay+name+activity join behind
  one function; make the Users page GET and the bot injection both consume it (the
  consolidation invariant #1 in code).
- **Roster-coherence regression lock** — a test/guard that asserts every Users-page-admitted
  identity is resolvable by name in the bot's projected context (catches future divergence).
- **Cross-platform identity unification** (user-roster-and-roles open Q4) — is Telegram-@alice
  the same person as Slack-alice? Distinct by default; surface "looks like the same person,
  link?" as administrative metadata, never an auth shortcut. **→ Resolved — designed in
  [spec-user-identity-merge-2026-06-23.md](spec-user-identity-merge-2026-06-23.md)** (operator-
  driven reversible merge; one `person_id` spans N identities; merge unifies identity, never
  authority; suggest-never-auto-apply).
- **Per-app capability declarations** (user-roster-and-roles open follow-up) — schema fields
  exist (Phase B.1) but no app declares them; migrating an app's tools to `provided_capabilities`
  is the next concrete capability-layer step.
- **Page-content review** of the Users page as an operator would read it (newcomer-mode copy,
  the "internal-only" email-alias state, the Person ID field) — route presentation issues to `ui`.

## 8. Roadmap — user-system robustness program (post-F1; operator-requested 2026-06-15)

The F1 store-coherence bug was a symptom, not the disease. The operator's read: the user
system "wasn't designed thoroughly and robustly enough previously, and who knows what other
shortcomings are lying in wait." This program hunts that class proactively, rather than
fixing one symptom at a time.

- **R1 — Robustness / completeness audit of the user-identity-access system.** Derive the
  IAM completeness checklist from best practices — *identification, authentication,
  authorization, audit, lifecycle (provisioning + deprovisioning/blocks), cross-platform
  identity, fail-closed defaults, least privilege* — then verify each dimension against
  as-built, governed by the §9 lens (admin-claims-vs-bot-reality). Output: a **gap register**;
  each confirmed gap becomes its own fix chip. Strong candidate for a fan-out audit when it runs.
  - **R1a — Access-control enforcement verification (highest priority; co-owned with
    `edr`/security).** The operator's sharpest question: *if a bot doesn't even know a user
    exists, how is it enforcing that user's roles and permissions?* Adversarially confirm the
    roster spec's enforcement layers actually **fire** — Layer 1 (channel ingress gate),
    Layer 2 (per-role MCP tool allowlist), Layer 3 (app-script capability check) — on **every
    platform**, **below the LLM**, and **fail-closed** (an unresolved/unknown identity must be
    denied or least-privileged, never silently treated as an admitted participant). The roster
    spec built/tested enforcement primarily on Telegram; Phase D left other platforms as
    "per-platform configuration" — so confirm Slack/Discord/etc. are not *admitted-without-
    enforcement*. Test: try to invoke a privileged tool as a `participant` on each channel and
    prove it is blocked, and that identity-resolution failure denies rather than admits.
- **R2 — Universal cross-platform user model.** One **person-level** identity spanning platform
  identities (Slack / Telegram / Discord / WhatsApp / …), with the access model attached
  coherently. Revisits user-roster-and-roles open Q4 (today identities are distinct per
  platform by default). Decide whether the *person* is the unit of identity + access (platform
  handles as attributes) or identities stay per-platform with explicit linking — either way the
  bot's projected roster and the admin roster must agree across platforms. Design spec.
  **→ Realized in [spec-user-directory-2026-06-22.md](spec-user-directory-2026-06-22.md)** (the
  unified Person model; person is the unit, platform handles + emails are attributes, `membership`
  attaches when admitted). Decision: the *person* is the unit (2026-06-22 operator design).
  **Cross-platform *merge* (full R2 — one `person_id` spanning N platform identities, operator-
  driven and reversible, resolving open Q4) designed in
  [spec-user-identity-merge-2026-06-23.md](spec-user-identity-merge-2026-06-23.md)** (2026-06-23):
  merge unifies *identity*, never *authority* (roles stay per-membership — the load-bearing
  `edr`/security boundary); suggest-never-auto-apply; the consolidated per-person Users page.
- **R3 — Principal separation (bot-owner-scoped PII, operator-masked).** A future structure where a
  **bot owner** (distinct from the **pod operator / Evolve admin**) sees bot PII that is *masked
  from the Evolve admin*. Today's assumption is the inverse — **Evolve admin == data owner** (it may
  see all pod PII; see [spec-user-directory §7](spec-user-directory-2026-06-22.md)). R3 flips that.
  Architecture issues (named, not solved): policy-masking ≠ enforced-masking (real masking on
  operator-controlled hardware needs **crypto separation**, not a UI toggle — any key the `evolve`
  server can decrypt with, the operator can scrape); the server-as-`evolve` brokers cleartext PII
  for name-resolution / Usage-enrichment / wizard extraction, so masking pushes those **bot-side or
  dark**; "bot owner" must become a distinct authenticated principal with key custody; and D6's
  `has_content` metadata/content split is the prototype, generalized to all PII + upgraded
  ACL→crypto. **`users` × `edr`; carve-candidate when pursued.** Demands only the
  [spec-user-directory §4](spec-user-directory-2026-06-22.md) `resolve_person` seam today.

## 9. Generalizable lesson — the "admin-claims-vs-bot-reality" audit lens

The F1 miss had a shape worth generalizing: **the admin UI advertised a capability the bot, at
runtime, did not actually have.** The Users page rendered admitted users with IDs; the bot
could not name two of them. So every claim the admin surface makes about a bot's users is a
*hypothesis about bot behavior* to be verified **at the bot**, not at the page. This lens
governs R1: for each advertised capability (admission, role, capability binding, block,
engagement surface, activity, cross-platform identity), the test is not "does the admin store
hold it?" but "does the bot **see / honor / enforce** it in a live turn?" Divergence between
those two is the bug class — and it is almost certainly not unique to name resolution.

## F1 investigation findings (USER.md scope)

*Recorded by chip F1a (admin-side resolver + USER.md scope), 2026-06-15. Read-only code
investigation; every claim below is cited to `file:line`. This resolves the §6 "open scope
question" — is `USER.md` safe to repurpose, or should Evolve only append a managed block?*

### Q1 — Who writes/seeds/refreshes `USER.md`? Does Evolve author its content?

**No. Evolve does not author `USER.md` content.** `USER.md` is **OC-template-seeded at bot
creation, then bot-authored freeform prose.** Evolve only ever *reads/scans* it.

- **Seed (one-time, OC writes it — not Evolve):** bot creation Stage 4 runs
  `openclaw onboard --non-interactive` (`packages/admin/evolve_admin/provisioning.py`
  `STAGE_OC_ONBOARD`, `_run_openclaw_onboard` at ~line 445). `USER.md` is one of the docs OC's
  onboard seeds (`_ONBOARD_DOCS = (… "USER.md")`, asserted in
  `packages/admin/tests/test_bot_creation_seeds_all_content_scan_docs.py:40`). Evolve passes
  flags; **OpenClaw's onboard command writes the template file**. After seeding it is the
  bot's own prose.
- **Per-turn injection (OC built-in):** OC's bootstrap mechanism injects the identity files
  (`SOUL.md`/`USER.md`/`AGENTS.md`/…) into model context every turn — this is OpenClaw's
  behavior, not Evolve code. (Cross-ref `docs/spec-prompt-injection-scanner-2026-05-10.md`.)
- **Reads (Evolve):**
  - `packages/admin/evolve_admin/applications/scanner.py:1398` —
    `inv.user_md_content = _safe_read(workspace / "USER.md", max_chars=600)` (inventory).
  - `packages/admin/evolve_admin/applications/scanner.py:1861,1868` — `USER.md` is named as
    an OC identity file ("that is the runtime, not an application") and the LLM scan prompt
    says *"The 'Personal Assistant' base (SOUL.md + USER.md) is always present — skip unless
    clearly distinct"* → **excluded from app detection.**
  - `packages/admin/evolve_admin/applications/layer_classifier.py:132` —
    `"USER.md": "behavior_doc"` (classified as a base behavior layer, not an app).
  - `packages/analyzer/expansion.py:124-135` — `load_user_profile()` reads `USER.md` +
    `MEMORY.md` purely as *context* for the expansion engine.

- **The conflation trap (called out so F1b doesn't repeat it):** there is a *separate*
  structured per-user store, **`user_profile`**, that Evolve *does* write —
  `.openclaw/profiles/<user_key>.md` (`PROFILES_SUBDIR = ".openclaw/profiles"`,
  `packages/analyzer/user_profile/storage.py:33,65`). It is written by the admin-side wizard
  adapter (`packages/admin/evolve_admin/evo/wizard/user_profile_writer.py`) and the bot-side
  `user_profile_inferrer` generator (`packages/analyzer/generators/user_profile_inferrer/hook.py`
  → `save_profile`). **This is NOT `USER.md`** and NOT the roster. A first-pass investigation
  can mistake "Evolve writes `.openclaw/profiles/*.md`" for "Evolve writes `USER.md`" — it does
  not. The precedent it sets, though, is instructive: *Evolve owns structured stores; the bot
  owns freeform prose.*

### Q2 — Where is the per-turn identity/speaker context injection?

- **Identity-file injection (SOUL/USER/etc.):** OpenClaw built-in (bootstrap), per Q1.
- **Evolve's per-turn speaker-context injection** (the hook F1b extends from "who is talking
  now" → "here is your full roster"): the `before_prompt_build` hook in
  `packages/plugin/src/observer/TurnObserver.ts:1575`, which calls
  `this._buildSpeakerContextBlock(ctx)` (TurnObserver.ts:1655) →
  `buildSpeakerContextBlock(...)` in `packages/plugin/src/util/roleResolver.ts:209`.
  Speaker role is resolved by `resolveSpeakerRole(...)` (roleResolver.ts:127), which reads the
  overlay + `network.json` directly. **F1b grows this block from the current speaker to the
  full admitted roster digest.**

### Q3 — What does the scanner do with `USER.md`?

Read fully (≤600 chars) into the workspace inventory as `user_md_content`
(scanner.py:1398); classified `behavior_doc` (layer_classifier.py:132); treated as the
always-present "Personal Assistant base" and **excluded from application detection**
(scanner.py:1859-1868); also read by `expansion.load_user_profile()` as user-profile context
for proactive app ideation (expansion.py:124-135). In short: `USER.md` is a *base identity
layer the scanner reads but never treats as an app and never writes.*

### Recommended F1b approach — **append a delimited Evolve-managed block; never rewrite**

Two sub-decisions:

1. **Prefer per-turn roster-digest injection over any file write.** Because the injected
   roster (F1b piece 1) *outranks* `USER.md` for IDs and requires no file mutation, F1b should
   ship **injection + the `roster_lookup` read tool first, with no `USER.md` write at all.**
   That alone closes the diagnosed bug (the bot can now name every admitted user and stops
   hand-typing IDs) without touching bot-owned prose. This is the lowest-risk, highest-value
   path and should be F1b's whole first cut.

2. **If a `USER.md` projection is ever added, append a clearly-delimited managed block —
   do NOT repurpose/rewrite the file.** Rationale, grounded in Q1–Q3:
   - `USER.md` is **bot-authored freeform prose** (Evolve never authored its content). A
     wholesale rewrite would destroy relationship/preference context the bot maintains and
     that the scanner/expansion read as a base layer.
   - Evolve already owns a *separate* structured store (`.openclaw/profiles/`) for verified
     per-user data — consistent with "Evolve owns structured stores, bot owns prose." The
     roster projection belongs in a clearly Evolve-owned region, not smeared over the bot's
     prose.
   - Shape: a fenced region between sentinel markers
     (`<!-- EVOLVE-ROSTER:BEGIN (managed — do not hand-edit) -->` … `:END`), rewritten
     idempotently from the canonical resolver, carrying a one-line note that IDs inside are
     roster-verified and authoritative. Everything outside the markers is untouched. This
     stops the self-reinforcing drift at its source (the bot stops writing IDs into prose)
     while preserving everything the bot owns.

   **Default decision (absent contrary evidence, none found): non-destructive append of a
   delimited managed block — and only if injection proves insufficient.** Injection-first;
   the block is an optional, separately-gated follow-up.

## F2 investigation findings — Slack "By User · ?" attribution (operator /design issue 2, 2026-06-17)

**Operator complaint:** on the Usage page the single largest *By User* bucket is
"Slack user · ?" (snapshot: 217 calls; live 30-day count when investigated: **748**). The
chip's hypothesis was a Slack **capture** gap (no `user_id` threaded into the turn write)
needing a plugin / `cost_event_converter.py` fix. The diagnosis below shows the picture is
different: **the capture fix already shipped and works; the dominant cause of the inflated
bucket is a read-side platform-misclassification bug.**

All numbers are from a live read-only pod inspection (`ssh mini`, real `compute_summary`
over the 30-day window the Usage route uses), 2026-06-17.

### The two turn-write paths — and which one is actually live

| Path | Writer | Carries Slack `user_id`? | Read by |
|------|--------|--------------------------|---------|
| (a) Authoritative OC turn-collector → `{bot_home}/.openclaw/workspace/memory/turns-*.jsonl` | `turn-collector.py` (OpenClaw-side) | n/a — **not running on this pod** | `cost_event_converter.py` priority-1; `load_turns` priority-2 |
| (b) Plugin fallback `writeTurnToShared` → `{shared_dir}/{bot}/turns/turns-*.jsonl` | `TurnObserver.ts` | **yes** — reads `senderRegistry.getSender(ctx.runId)` (Phase D.1, [#2402], merged 2026-06-08) | `load_turns` **priority-1** (the Usage *By User* table); `cost_event_converter.py` priority-2 (fallback when OC file absent) |

**Key structural finding:** the OC turn-collector (path a) is **not deployed** — there are
zero `workspace/memory/turns-*.jsonl` files for any bot, and no launchd job runs the
collector (only the per-bot `cost-converter` jobs exist). So **`writeTurnToShared` (path b)
is the de-facto authoritative source for every consumer**, and it *already* threads the
captured sender into `user_id` via `senderRegistry`. The chip's suggested fix ("ensure the
sender reaches the authoritative path too / have `cost_event_converter.py` join the sender")
is therefore moot — there is no separate authoritative path to bridge to, and the one live
path already enriches.

Note the priority **inversion**: `load_turns` (Usage By-User) reads the shared/enriched file
*first* (`resolve_bot_paths.turns_dir_candidates[0]`), while `cost_event_converter` reads the
OC file first. Both are fine *today* because the OC file is absent — but if the OC collector
is ever switched on and emits Slack turns without a `user_id`, the **cost lens would regress**
even though the Usage page would stay correct. Logged as a latent fragility (F2-followup-1).

### Q1 — Which path produces the nulls? → path (b), and only historically

`load_turns` reads the enriched shared file. The `user_id`-null Slack turns it surfaces are a
**deployment-timeline artifact**, not an ongoing defect. Genuine-Slack human turns with a
null `user_id`, split by half-month (source: shared dir, the only source — `oc=0`):

| Bot | May-late | Jun-early | Jun-late |
|-----|---------:|----------:|---------:|
| team-bot-a (null) | 173 | 59 | **0** |
| team-bot-a (has user) | 0 | 156 | 21 |
| team-bot-b (null) | 22 | 25 | **0** |
| team-bot-b (has user) | 0 | 50 | 3 |

The null→attributed transition tracks the per-bot gateway restart that picked up the Phase
C.3/D.1 plugin (`senderRegistry` capture landed [#2393] 2026-06-07; `user_id` population
[#2402] 2026-06-08; fleet rollout completed mid-June). **Recent (late-June) genuine-Slack
turns are 100% attributed, across DM + channel + thread contexts.**

### Q2 — Does the authoritative OC record carry a Slack sender? → N/A (not running); cost_events covered via fallback

Because the OC collector is absent, `cost_event_converter` falls through to the enriched
shared file and picks up `user_id` via `_user_id_from_turn`. Separately noted: cost_events
carry `channel_kind=None` for all real chat turns, because `_infer_channel_kind` keys off
`channel == "slack"` (the *platform name* the OC schema would write) while `writeTurnToShared`
writes the channel **id** into `channel` (e.g. `"D0AK…"`, `"c7t9…:thread:…"`). This breaks
slack DM-vs-channel bucketing **and** display-name resolution in the cost lens — pre-existing,
`model-tiers`/cost-owned, **not** the By-User symptom. Logged as F2-followup-2.

### Q3 — Is `senderRegistry` reliable for Slack groups? → yes, on the deployed version

`before_agent_run` fires with `event.senderId` for group/DM/thread turns (capture is the
first statement in `handleBeforeAgentRun`, before any veto). Cross-checked the TTL/runId
eviction theory: of team-bot-a's 81 null-`user_id` sessions only **1** also contained a captured
user (team-bot-b: 0). If TTL expiry or runId mismatch were losing senders mid-session we'd expect
*many* mixed sessions. Near-zero overlap ⇒ nulls are **whole pre-rollout sessions**, not
within-session capture flakiness. `MAX_ENTRIES=1024` / `TTL_MS=5min` are not implicated.

### Q4 — Genuine user-triggered vs legitimately user-less, and recoverability

The live "Slack user · ?" bucket = **748** turns. Broken down by the *true* underlying
`channel` value:

- **~457 (61%) are NOT Slack at all** — a read-side misclassification (see bug below):
  `channel="unknown"` (318) + `channel="webchat"` (139), mislabeled "slack". These have no
  external user by nature (webchat = admin home-chat = the operator, anonymous; `unknown` =
  channel-less human dispatches). **Not recoverable as Slack users — they aren't.**
- **~291 (39%) are genuine Slack** = ~279 real C/D/G channel/DM/`:thread:` ids + ~12 turns
  where a `U…`-prefixed Slack **user** id leaked into the channel field (these are digit-bearing,
  so the digit-guard fix below correctly keeps them as Slack). All are **historical**
  (May-late + early-June, pre-rollout). **None are retroactively recoverable** — the sender
  was never written to the record and `senderRegistry` is ephemeral in-memory; those runIds
  are long gone. Future genuine-Slack turns are *already* attributed (Q1).

**Recoverability verdict:** retroactive recovery of the genuine ~291 is **not possible** here
(the discovery sibling chip handles turns that *did* capture an id); forward capture is
**already solved**. The single actionable lever on the operator's symptom is the
misclassification bug.

### Root cause of the bucket inflation — `_infer_platform` greedy prefix match

`usage_analytics._infer_platform` (the function that derives the `(platform, user_id)` activity
key — `users`-owned per §4 boundary) classifies **any** string starting with C/D/G/U/W and an
alphanumeric tail as Slack. Confirmed live:

```
_infer_platform("unknown") -> "slack"     # U + "nknown"
_infer_platform("webchat") -> "slack"     # W + "ebchat"
_infer_platform("discord") -> "slack"     # D + "iscord"
_infer_platform("cron")    -> "slack"     # C + "ron"
_infer_platform("U9ZL3JYR3") -> "slack"   # genuine — correct
```

Real Slack object ids always contain a digit (`U9ZL3JYR3`, `C0AK…`); the English sentinel
words that pollute the bucket (`unknown`/`webchat`/`web`/`discord`/`cron`) never do. The fix
is a **digit-in-tail requirement** on the C/D/G/U/W branch (the `:thread:` branch is
untouched, so threaded ids still resolve). This drops the ~457 non-Slack turns out of the
Slack bucket and into the correct "unknown" bucket, shrinking "Slack user · ?" from 748 to
~291 (the genuine, mostly-historical remainder) without misclassifying any real id (verified:
all existing fake-id test fixtures — `C0FAKECHAN`, `U0FAKEUSER`, `W0ENT1` — carry digits).

### Disposition

- **Capture side (chip's intended fix):** no change — already shipped ([#2402]) and verified
  working. Building more capture code would be redundant.
- **This PR fixes** the `_infer_platform` misclassification (read-side, offline analytics, not
  the hot path) — the dominant, actionable cause of the operator-visible bucket. It lives in
  the `(channel, user_id)` activity identity that `users` owns (§4), adjacent to `model-tiers`'
  active Usage-legibility work ([#2985]/[#2986]); flagged for coherence, no file collision.
- **Follow-ups (not this PR):** F2-followup-1 (cost-lens regression risk if OC collector is
  switched on); F2-followup-2 (`cost_event_converter._infer_channel_kind` expects the platform
  name but receives the channel id → `channel_kind=None` for all chat → cost-lens slack
  bucketing + display-name resolution broken) — both `model-tiers`/cost-owned.

## Conversation-name cache contract (Usage "By Channel" — Bite 2 → Bite 3)

The operator's "make Usage legible" /design split into three bites: Bite 1 ([#2985], `model-tiers`)
shaped the `by_channel` rows (`{channel, platform, is_dm, label, threads}`) and resolves the
Slack-DM case opportunistically; **Bite 2 (this aspect)** builds the conversation-NAME resolver +
cache (the data layer); **Bite 3** (`model-tiers`, `usage-channel-names`) reads that cache to fill
the non-DM `label`. The locked decision (operator): **render reads the cache only — no live
Slack/Telegram API call at render time.** The cache is warmed in the background.

**Module.** `packages/admin/evolve_admin/evo/conversation_resolver.py` — the sibling of
`evo/name_resolver.py` (user id → name). It reuses that module's HTTP primitive
(`_http_get_json`), workspace-scoped token picker (`_channel_token`, pass `bot_id`), and user-name
resolver (`resolve`, for a DM's peer). New here: the conversation-shaped API calls (Slack
`conversations.info`, Telegram `getChat` for a group title) and the cache below.

**Cache location.** Mirrors `pod.admins.resolved_names` (the user-name cache): conversation
results live in `network.json` under `pod.conversations.resolved`. Atomic via `save_network`
(caller persists; the resolver only mutates the in-memory dict).

**Key.** `"<platform>:<conv_id>"` — `platform` lower-cased (`slack` / `telegram`); `conv_id` is the
**base conversation id with any `:thread:<ts>` suffix stripped** (rolled up to the parent, matching
`usage_analytics._split_thread`), kept **verbatim** (no case-fold of the id). Bite 3 and the
background sweep both source `conv_id` from the same turn `channel` field, so the casing stays
consistent (e.g. a lower-cased session stem `c7t9fby6s` keys as `slack:c7t9fby6s`; it is up-cased
only for the outbound API call).

**Value (positive).** A ready-to-display `label` plus `kind`/`name` for flexibility:

```json
{ "resolved": true,
  "kind":  "channel | private | group_dm | dm | telegram_group",
  "name":  "<raw channel name / group title / DM-peer name, or null>",
  "label": "#product-team  |  DM · Alice  |  Group DM  |  <Telegram group title>",
  "members": ["U…", "…"],          // optional, group DM only, when cheap (usually absent)
  "cached_at": "2026-06-17T00:00:00Z" }
```

**Value (negative / unresolvable).** Cached so an id the bot can't see isn't re-hit every sweep
(lower-cased `c…`/`g…` stems that `conversations.info` can't resolve, channels the token lacks
scope/membership for, deleted chats):

```json
{ "resolved": false, "kind": null, "name": null, "label": null,
  "cached_at": "2026-06-17T00:00:00Z" }
```

**How Bite 3 binds.** Call `conversation_resolver.cached_conversation(network, platform, conv_id)`
(cache-only; honors the 7-day TTL). Use `entry["label"]` when it is a non-empty string; otherwise
(negative entry, or `None` = not yet swept) fall back to the raw id — exactly the raw-id fallback
the route at `routes_analytics._enrich_usage_names` already uses for non-DM rows today. Bite 3 only
swaps that fallback for a cache read; it does **not** call `resolve_conversation` (the live path).

**Which kinds resolve vs. negative-cache.** Resolve → Slack public/private channel (`#name`),
Slack 1:1 DM (`DM · <peer>`, or `Direct message` when the peer name is unresolvable), Slack group
DM (`Group DM`), Telegram group/supergroup/channel (the title), Telegram private (`DM · <name>`).
Negative → Slack ids `conversations.info` can't classify or errors on; Telegram `getChat` errors;
any unresolvable id. Out of supported set (Discord, `unknown`) → `None`, **no** cache write (a
future impl populates them cleanly).

**Background populate (never from a render path).**
`enrich_conversation_names(network, conv_ids, *, bot_id=None, max_seconds=…)` — time-boxed
`ThreadPoolExecutor`, `use_cache=False`, skips already-fresh ids, mutates the cache, returns whether
to `save_network` (mirrors `routes_bot_users._enrich_unknown_names`).
`warm_conversation_cache(network, *, days=7, …)` — standalone sweep: pulls recent conversation ids
from `usage_analytics.load_turns`, groups by the dominant serving bot (workspace-correct Slack
tokens), enriches. Runnable as `python3 -m evolve_admin.evo.conversation_resolver` for a daily cron.

## User-name cache contract (Usage "By User" — non-admitted-name parity, [#2996])

Twin of the conversation-name cache above, for the Usage **By User** report. The operator's
2026-06-17 observation (F2 above): By-User showed real names for roster/admin users but
`Slack user · U0AV94…` for everyone else — even when those ids ARE valid Slack users the **Users**
page resolves to real names (the same `U0AV94R5SEL → Ranya Jan` shows under "Active · not admitted"
there). Root cause: `_enrich_usage_names` → `roster_resolver.resolve_display_name` is **cache-only**
(by design), and non-roster people had no entry in any cache it reads — the Users page named them
only because it makes a **live** lookup at render time, which the Usage render must not. Locked
decision (operator): **render stays cache-only; warm a cache `resolve_display_name` reads in the
background**, reusing the exact discovery path the Users page uses.

**Module.** `packages/admin/evolve_admin/evo/user_resolver.py` — sibling of `evo/name_resolver.py`
(it reuses that module's `_channel_token` + `_resolve_slack` / `_resolve_telegram` /
`_resolve_discord`) and same shape as `conversation_resolver.py`. New here: the user cache below
(with **negative** sentinels, which `name_resolver` lacks) and a pod-wide warm sweep.

**Cache location.** A **sibling** of `pod.admins.resolved_names`: results live in `network.json`
under `pod.users.resolved`. (A sibling, not an extension of `…admins.resolved_names`, so the
negative-cache layer is isolated and `name_resolver`'s existing admin-resolution write path is
untouched.) Atomic via `save_network` (caller persists; the resolver only mutates the in-memory
dict).

**Key.** `"<platform>:<user_id>"` — `platform` lower-cased (`slack` / `telegram` / `discord`);
`user_id` kept **verbatim** (no case-fold). Same id space as the By-User rows
(`usage_analytics` keys `by_user` on the turn's `user_id`) and as a bot's allowFrom entries.

**Value (positive).**

```json
{ "resolved": true,
  "name":     "<best-effort human display name, or null>",
  "username": "<platform @handle, or null>",
  "email":    "<email>",                  // Slack only, when users:read.email scope; key absent otherwise
  "cached_at": "2026-06-17T00:00:00Z" }
```

**Value (negative / unresolvable).** Cached so a deactivated account / foreign-workspace id / bot
pseudo-user isn't re-hit every sweep (`name_resolver` has no such layer — that re-hit is the bug
this fixes):

```json
{ "resolved": false, "name": null, "username": null, "cached_at": "2026-06-17T00:00:00Z" }
```

**How the render binds.** `roster_resolver.resolve_display_name` consults
`user_resolver.cached_user(network, platform, id, max_age_days=0)` as its **last** step — after
`pod.admins.resolved_names` → `pod.admins.names` → bot primary → `identity_cache`. Placed last so it
can only *add* a name for an id that currently resolves to None (source tag `users_resolved`), never
change an existing resolution → the Users-page JSON stays byte-identical. `max_age_days=0` at read
(show a slightly-stale name over reverting to a raw id; the warm refreshes stale entries on its TTL).

**Background populate (never from a render path).**
`enrich_user_names(network, pairs, *, bot_id=None, max_seconds=…)` — time-boxed `ThreadPoolExecutor`,
`use_cache=False`, skips already-fresh ids, counts a negative write as a change (mirrors
`_enrich_unknown_names`). `warm_user_cache(network, *, days=30, …)` — standalone sweep: for each bot
runs `identity_discovery.discover_candidates` + the Slack D→U rewrite (the **exact** Users-page
discovery), groups by serving bot for workspace-correct tokens, enriches. Runnable as
`python3 -m evolve_admin.evo.user_resolver` for a daily cron. The render fires a non-blocking,
throttled, time-boxed `_kick_user_warm` (twin of `_kick_conversation_warm`) on each Usage load.

## Pod-wide "not admitted" marker (Usage "By User", [#2996])

A By-User row carries `unadmitted: true` iff that person is admitted to **no bot anywhere**. Computed
pod-wide because the report is pod-wide while admission is per-bot: `_pod_admitted_index(network)`
unions every bot's per-channel allowFrom (via the same `roster_resolver._default_allowfrom_reader`
the Users page uses) **plus** pod admins (`pod.admins.external_ids`) **plus** each bot's
`primary_user` — all "admitted by definition". A user admitted to even one bot is never marked.

The index also returns the set of **platforms whose allowFrom was actually read** (a non-None
result — present, even if empty); a row is marked only when its platform is in that set. Crucially,
pod-admin/owner presence does NOT add to that set — it comes from the always-readable network.json
and proves nothing about allowFrom I/O. The allowFrom files live in a mode-700 `credentials/` dir
reachable only via the `sudo /bin/cat` grant, which can go dormant (refresh-sudoers is manual); when
it does, that platform stays out of the readable set and its users are **not** marked during the
window. A false positive — flagging an admitted user — is the harmful direction; a missed marker is
benign. Rendered in `cost.js` as a style-guide-compliant `badge badge-sm badge-warn` (semantic
"attention", not a decorative category).

## R1a diagnosis (2026-06-17) — "unpaired but active" Slack group users

*Recorded by chip R1a (security gate, co-owned with `edr`/security), 2026-06-17. Live
read-only pod forensics + OpenClaw-2026.6.1 source reading. **All user IDs, names, channel
IDs, and bot names below are anonymized** (fake `U0FAKE…`/`C0FAKE…` IDs, role-placeholder bot
names) per the public-launch scrub — the real artifacts were read on the pod and are not
reproduced here.*

**Operator question.** The Usage "By User" report and the Users-page "Active · not admitted"
group list Slack users who are shown **unpaired** (not in the approved allowlist) yet carry
**recent LLM-turn activity** (e.g. 22 / 13 / 4 turns, "1–5 days ago") in Slack group/channel
contexts, while the bot's newcomer policy reads **"Require approval."** Pre-gate, or a hole in
the gating system?

### VERDICT — not an enforcement bypass; a two-allowlist coherence/governance gap

**OpenClaw enforces Slack *group/channel* access per-sender, fail-closed, against a *different*
allowlist than the one the Evolve Users page reads.** The flagged users **are** admitted on the
list OpenClaw actually consults for groups; the Users page is blind to that list, so it
mislabels them "not admitted." Concretely there are **two independent Slack allowlists** per
bot:

| List | File | Governs | Surfaced by Evolve? |
|---|---|---|---|
| **DM pairing store** | `<bot>/.openclaw/credentials/slack-default-allowFrom.json` | DM access (`dmPolicy: pairing`) | **Yes** — the Users page "Approved" list + approve/reject/disconnect, and the "Active · not admitted" set is computed against it |
| **Channel/group allowlist** | `<bot>/.openclaw/openclaw.json` → `channels.slack.allowFrom` | Group/channel access (`groupPolicy: allowlist`) | **No** — Evolve neither reads, renders, nor manages it |

The flagged users sit in the **second** list (group-allowed) but **not** the first (not
DM-paired). OpenClaw correctly processes their channel messages; Evolve's "Require approval"
setting governs only DM pairing and has **no bearing on group access at all**. So nothing was
bypassed — but the admin surface advertises an admission model that does not match what the bot
enforces. This is the §9 *admin-claims-vs-bot-reality* lens exactly.

### Relationship to the parallel "By User" / discovery diagnosis (same users, different question)

A separate `users` bout investigates the *display* question for these same IDs — *why do they
appear in the Usage "By User" report but were (until recently) missing from the Users-page
"Active · not admitted" discovery list?* That has its own, unrelated root cause in
`evo/identity_discovery._extract_identity`: a live Slack group turn is shaped
`{channel:"c…:thread:…", user_id:"U…"}`, and the extractor must **prefer the real `user_id`**
over the conversation-channel id — otherwise the candidate is a `c…` channel id that the
Slack-`U`/`W`-only filter (`routes_bot_users.py` ~`:414`) drops, so the person never surfaces.
`_extract_identity` on `origin/main` now prefers a present, valid `user_id`
(`packages/admin/evolve_admin/evo/identity_discovery.py:225-233`), so these users **do** surface
as "Active · not admitted" — matching the premise above.

That is the **display/discovery** axis; this R1a subsection is the **enforcement** axis (*how are
they processed at all under "Require approval"?*). The two are complementary, not duplicates:
fixing discovery makes the right-hand "they're active but unpaired" rows appear; fixing the
two-allowlist gap (below) explains and governs *why* they're allowed. Both touch the
`routes_bot_users.py` `seen_recently` region — flagged for `users` coherence; this PR is
docs-only and collides with neither.

### Evidence

**1. The flagged users are genuinely absent from the DM pairing store, and never paired.**
For the Slack bot in question (`team-bot-a`), none of the three flagged IDs
(`U0FAKEUSR1`, `U0FAKEUSR2`, `U0FAKEUSR3`) appear in
`credentials/slack-default-allowFrom.json` (13 entries) or in `slack-pairing.json`
(`requests: []`). So hypotheses **#3 (role-only / actually-in-the-allowlist Evolve reads)** is
**refuted** for the pairing store, and they did not go through `/start`.

**2. The LLM ran for them — these are real, recent, recurring turns.** Sample turn record (one
of 22) pulled from `{shared_dir}/team-bot-a/turns/turns-*.jsonl` (anonymized):

```json
{ "ts": "2026-06-12T…Z", "instance": "team-bot-a", "model": "claude-sonnet-4-6",
  "source": "user", "channel": "c0fakechn1:thread:1781…", "user_id": "U0FAKEUSR1",
  "input_tokens": 4, "output_tokens": 300, "cost": 0.178… }
```

Channel ids are lower-case-`c` Slack **channel** conversation stems with `:thread:` suffixes —
public/private channels the bot is a member of, not DMs. Activity spans 2026-06-11…06-17 across
many threads; not a one-off. **Hypothesis #4 (stale code path / restart race) is refuted** — the
pattern is structural and current.

**3. Every channel/thread carrying a flagged user also carries admitted users — but that
co-occurrence is incidental, not the mechanism.** Per-channel participant maps (anonymized
counts) for `team-bot-a`:

- `C0FAKECHN1` — `U0FAKEUSR1` (flagged) 22 turns alongside `U0FAKEADM1` (admitted) 2 turns.
- `C0FAKECHN2` thread — `U0FAKEADM2` (admitted) 4 + `U0FAKEUSR3` (flagged) 4 interleaved.
- `C0FAKECHN3` threads — `U0FAKEADM3` (admitted) 8 + `U0FAKEUSR2` (flagged) 5; and similar across
  ~6 threads, each mixing admitted + non-admitted senders.

This *looks* like the chip's prime suspect **#1 (group-DM thread-processing "ride-along" hole:
an admitted primary opens a thread and non-admitted participants ride along on a
per-conversation gate)** — but the next finding **refutes the ride-along mechanism**. The gate is
**per-sender**, not per-conversation; the admitted users in the same channel are coincidental
(shared channels), not what authorizes the flagged users.

**4. Root cause — the flagged users are individually on the group allowlist OpenClaw reads.**
`team-bot-a`'s `openclaw.json` Slack block:

```
channels.slack.groupPolicy = "allowlist"     channels.slack.dmPolicy = "pairing"
channels.slack.requireMention = true          channels.slack.groupAllowFrom = (unset)
channels.slack.allowFrom = [ …31 ids… ]        # includes ALL THREE flagged users
```

All three flagged IDs are present in `channels.slack.allowFrom` (31 entries) — and only there,
not in the 13-entry pairing store. The bot has no separate `channels.slack.groupAllowFrom`, so
with `groupPolicy: "allowlist"` the **effective group allowlist is `channels.slack.allowFrom`**
itself — proven empirically: the only IDs that could have authorized these turns are in that
list, and the gate is per-sender (finding 5). (OpenClaw exposes a
`groupAllowFromFallbackToAllowFrom` flag for exactly this fall-through; its documented default is
`true` in `openclaw/dist/channel-capabilities-BPY4RcPZ.js`, though that is the *doctor*-surface
default and the one runtime fall-through directly citable in 2026.6.1 is Telegram-specific — so
for Slack the **behavior**, not the flag default, is the authority here.) Either way each flagged
user passes the group allowlist **on their own ID**. Per-channel `requireMention` (true for
`C0FAKECHN1`, false for `C0FAKECHN2/3`) only decides whether an @-mention is needed; it does not
bear on admission.

**5. OpenClaw's group gate is per-sender and fail-closed.** OpenClaw (2026.6.1) splits access
into two modules — `dm-access` (`ChannelDmPolicy = pairing|allowlist|open|disabled`) and
`group-access` (`GroupPolicy = open|disabled|allowlist`). The group decision keys **per-sender**
on `isSenderAllowed(senderId, groupAllowFrom)` and returns, under `groupPolicy: "allowlist"`,
`allowed:false` with reason `empty_allowlist` (empty list) or `sender_not_allowlisted` (unlisted
sender), and `allowed:true` only for a listed sender. (The live bot runtime imports this via the
`bot-*` runtime variant of the group-access module; the `@deprecated` plugin-SDK copy
`openclaw/dist/group-access-*.js` carries byte-identical fail-closed logic and reason strings —
both verified.) So the gate **does** fire below the LLM and **does** fail closed — it admitted
the flagged users because they are genuinely listed, not via any per-conversation ride-along. The
DM path reads the pairing store via `readStoreAllowFrom`/`useDefaultPairingStore`
(`channel-ingress-runtime`); the group path reads the config list. Two stores, two gates.

**6. The Evolve admin surface only knows the pairing store.** `routes_bot_users.py:375-435`
computes "Active · not admitted" (`seen_recently`) as *turn-activity senders* minus
`approved ∪ pending ∪ blocked ∪ ignored`, where `approved` is read from the credentials pairing
store. The plugin holds **no** allowlist veto — `TurnObserver.handleBeforeAgentRun`
(`packages/plugin/src/observer/TurnObserver.ts:4146`) captures `senderId` and runs only the L1
cost-breaker veto (~`:4190`); `packages/plugin/src/` contains **zero** `allowFrom` references.
And the prerequisite spec
([spec-per-bot-users-management-2026-05-29.md](spec-per-bot-users-management-2026-05-29.md)
§"Approved users (the allowlist)") documents **only** the credentials file as "the allowlist."
The config-level `channels.slack.allowFrom` is outside Evolve's model entirely.

**7. The divergence is systemic and bidirectional** (not a `team-bot-a` one-off). Anonymized,
across the pod's three Slack bots:

| Bot (placeholder) | `groupPolicy` | config `allowFrom` | pairing store | config-only (group-allowed, **Evolve-blind**) | store-only (DM-paired, **not group-allowed**) |
|---|---|---:|---:|---:|---:|
| `team-bot-a` | allowlist | 31 | 13 | **20** | 2 |
| `team-bot-c` | allowlist | 3 | 5 | 0 | **2** |
| `security-bot` | (default) | 0 | 1 | 0 | 0 |

`team-bot-a` has 20 identities group-authorized but invisible to the Users page; `team-bot-c`
shows the inverse (2 users DM-paired but not on the group allowlist, so they can DM but are not
processed in channels). The two lists are maintained independently and routinely drift.

### Hypothesis adjudication

| # | Hypothesis | Verdict |
|---|---|---|
| 1 | Group-DM thread "ride-along" hole (per-conversation gate) | **Refuted as stated.** The gate is per-sender, not per-conversation; admitted-user co-occurrence is incidental. The *group context* is where it surfaces, but not via ride-along. |
| 2 | `auto_admit` newcomer mode | **Refuted.** No roster overlay for this bot under `{shared_dir}/rosters/` (only one unrelated bot has an overlay); channel defaults to `require_approval`. And `auto_admit` would have *written* these users into the pairing store — it didn't. |
| 3 | Role-only ("not admitted" = no overlay role; they're in allowFrom) | **Refuted for the pairing store** Evolve reads — but true in spirit for a *different* list: they ARE in the config group allowlist. |
| 4 | Pre-Layer-1 stale code / restart race | **Refuted.** Recent, recurring, structural. |
| — | **Actual cause: two independent allowlists; Evolve surfaces only the DM pairing store; OC enforces groups per-sender against the config `channels.slack.allowFrom`.** | **Confirmed.** |

### Is it a security hole? — No bypass; a real governance/visibility gap

- **Enforcement is sound.** OC's Slack group gate is per-sender and fail-closed (finding 5).
  The flagged users are processed because they are *legitimately* allowlisted, not because a
  gate failed. For the `edr`/R1a fail-closed concern, Slack is **not** "admitted-without-
  enforcement."
- **The genuine gap is governance + coherence.** The channel allowlist (`team-bot-a`: 31
  identities authorized to drive LLM spend in the bot's channels) is **unmanaged and invisible**
  in the only admin surface the operator uses. The operator cannot see who is group-authorized,
  cannot curate it, and gets **false assurance** from "Require approval" (which governs DMs
  only). Out-of-band additions to `channels.slack.allowFrom` (OC CLI, hand edit, wizard, or any
  future channel-membership sync) would silently widen who the bot serves with **no** signal on
  the Users page. This is the carve-trigger bug class (admin-store vs bot-reality divergence)
  recurring on the *admission* dimension rather than name-resolution.

### Fix direction (proposed follow-up — NOT built here)

Propose a **fix chip** `[META:users]` (co-`edr`), Phase-D-shaped, to make the group allowlist a
first-class, managed, coherent part of the user model:

1. **Surface the group allowlist on the Users page.** Read `channels.slack.allowFrom` (and the
   equivalent group allowlist for Telegram/Discord) in addition to the credentials pairing
   store; render a per-channel "Channel access (group)" list distinct from "Approved (DM)", with
   each identity's source labeled. This alone dissolves the "Active · not admitted" false
   negative: a group-allowlisted user should read as *group-admitted*, not *not admitted*.
2. **Make the two lists coherent via the canonical resolver** (§7 backlog item). Extend the
   single allowlist+overlay+name+activity join to carry **both** the DM-pairing and group-access
   memberships, consumed identically by the Users-page GET and the bot projection, so admin and
   bot cannot diverge. Decide the source-of-truth relationship: are DM-paired users auto-added to
   the group allowlist, kept strictly separate, or reconciled with operator confirmation?
3. **Manage it** — approve/revoke for group access, and a drift Signal
   (`roster_group_allowlist_changed`) when `channels.slack.allowFrom` changes out-of-band, so the
   operator is never blind to who can spend the bot's tokens in a channel.
4. **Correct the model docs.** The roster spec
   ([spec-user-roster-and-roles-2026-06-07.md](spec-user-roster-and-roles-2026-06-07.md) §8
   Layer 1) states OC "consults `<provider>-default-allowFrom.json` to admit or deny incoming
   senders." That is accurate for **DMs only**; group access uses a separate config list. Update
   Layer 1 to describe both gates.

This is a **diagnosis artifact only**; the fix chip above is the build step and must be
dispatched separately (build-before-root-cause-pinned is the failure mode this chip avoids).

### Note for META:edr R1a backlog (enforcement verification)

R1a's sharpest test — *does identity-resolution failure deny or admit, below the LLM, on every
platform?* — for **Slack groups** resolves **PASS on enforcement, FAIL on governance**:
- **Enforcement (PASS):** `groupPolicy: allowlist` is per-sender and fail-closed
  (the group-access decision keys on `isSenderAllowed(senderId, groupAllowFrom)`:
  `empty_allowlist`/`sender_not_allowlisted` → `allowed:false`). An unlisted/unknown Slack sender
  is denied in a channel.
- **Governance (FAIL):** the list that gate consults (`channels.slack.allowFrom`) is **not the
  list the admin manages** (the credentials pairing store). Least-privilege/lifecycle is
  uncontrolled: the operator can neither see nor revoke channel access from the admin UI, and
  DM-revocation (the page's "Disconnect") does **not** remove a user from the group allowlist.
  R1a should add a dimension: *for each platform, is the allowlist the gateway enforces the same
  artifact the admin surface curates?* Today, for Slack groups, **no.** (Telegram/Discord group
  allowlist plumbing should be checked the same way under R1.)

**Update for META:edr (PR2 outcome, 2026-06-18).** The governance gap is now *closeable* for
Slack/Telegram/Discord: PR2 (below) makes `channels.<ch>.allowFrom` admin-curatable
(approve/revoke), so the gateway-enforced list and the admin-curated list are, for the first
time, the **same artifact** the operator can act on — answering R1a's new dimension with "yes,
once the operator uses it." Two enforcement residuals remain for edr's R1a register:
(a) **DM-revoke still does not remove a user from the group allowlist** (and vice-versa) — by
design (strict separation), so the operator must revoke on *both* surfaces to fully de-authorize
a user; edr should decide whether a "remove from all surfaces" affordance is warranted.
(b) The **out-of-band drift Signal** (`roster_group_allowlist_changed`) — ✅ **LANDED 2026-07-01
([#3379](https://github.com/evolve-ops/evolve/pull/3379))**. `roster_allowlist_drift_monitor` (hourly,
pod-wide) compares each bot's live on-disk group allowlist against the admin-recorded baseline
(`evolve_admin.roster_baseline`, written by the approve/revoke routes; monitor seeds once on first
sight) and fires per drifted `(bot, channel)` (added senders = the risk), plus a fail-safe
`roster_group_allowlist_unreadable` when `openclaw.json` can't be read. Detection-only; enforcement
stays OpenClaw's fail-closed gate. Residual (a) remains in the META:users (co-`edr`) backlog.

**Full enforcement matrix (R1a audit, 2026-06-30) → [audit-r1a-enforcement-matrix-2026-06-30.md](audit-r1a-enforcement-matrix-2026-06-30.md).**
The complete Layer×platform gap register (Telegram / Slack-DM / Slack-group / Discord /
WhatsApp), read-only, verified against the OpenClaw 2026.6.11 dist and both live pods.
Headline: **Layer 1 (admission) is fail-closed below the LLM on every configured
platform; Layer 2 (the "iron-clad" per-role MCP tool-loading filter) was specified but
never built, and Layer 3 (app-script capability check) is schema-only/unenforced** — so
post-admission role/permission is *not* enforced below the LLM in the general case (the
one real below-LLM authorization is a ~6-route Evolve-daemon capability check, itself
Telegram-hard-coded). Not a fail-open admission hole; a *promised-but-unbuilt
authorization* gap. New backlog items G-N1 (HIGH, L2 filter absent), G-N2/G-N3
(daemon-check Telegram-hard-coding + header fail-open), G3 (Discord/WhatsApp governance
parity), G6 (correct the "Phases A–E shipped" claim in §3 — Phase B did not ship).

### R1a remediation — phased build (PR1 surface, PR2 manage)

The diagnosis above proposed a four-point fix (surface → cohere via the resolver → manage →
correct docs). It ships in security-sensitive phases:

- **PR1 ([#2999], merged) — surface, read-only.** `roster_resolver.effective_group_allowlist`
  (pure fall-through `groupAllowFrom` → `allowFrom` under
  `groupAllowFromFallbackToAllowFrom`, fail-closed) + `read_group_allowlists`; the GET's
  per-channel `group_access[]` (resolved through the SAME canonical join as `approved`,
  `access_source: "group_allowlist"`); the Users-page "Channel access · group" section; and
  the `seen_recently` exclusion that dissolves the "Active · not admitted" false negative.
- **PR2 (this chip) — manage (approve/revoke).** Two POST routes,
  `/users/group-allowlist/{approve,revoke}` (body `{channel, id}`), mutate **only**
  `openclaw.json::channels.<ch>` group allowlist and re-render through the SAME resolver PR1
  added, so the page reflects the change with no second source of truth.

**The management contract (build to this):**

1. **Strict separation (operator invariant).** A group approve/revoke writes **only**
   `openclaw.json::channels.<ch>` (the group allowlist) and **never** the credentials DM
   pairing store. DM approval never auto-grants group access and vice-versa. The two are
   different files, so separation holds by construction; a test asserts the DM store is
   byte-unchanged after a group approve.
2. **Effective-key targeting.** The write lands in whichever key
   `effective_group_allowlist` reads — `groupAllowFrom` when a dedicated list exists, else
   `channels.<ch>.allowFrom` (the R1a-diagnosed live-pod shape under the fallback default),
   else a fresh `groupAllowFrom` when the fallback is explicitly disabled. Writing `allowFrom`
   blindly would be invisible (or wrong) on any bot with a dedicated `groupAllowFrom`.
3. **Allowlist-gated only; fail loud.** Approve/revoke require an existing `channels.<ch>`
   block with `groupPolicy: "allowlist"` (the only shape with a curated per-sender list). A
   non-allowlist channel (`open`/`disabled`) or a missing block is a 400 — the path never
   fabricates config or flips `groupPolicy`.
4. **Secret-config write discipline.** `openclaw.json` is a token-bearing 0600 file, and an
   invalid write crash-loops the gateway — so the write goes through the schema-validating
   `deploy.safe_write_bot_config` (the same path every other openclaw.json writer uses:
   /tmp-stage → OC-schema-validate → `.bak` backup → `sudo cp` → chown bot → `chmod 600`),
   not a hand-rolled `cp`. All required sudoers grants already exist (§1/§4/§6/§13) — **no
   sudoers change**.
5. **Propagation.** OpenClaw reads `channels.*` at gateway startup and does **not** hot-reload
   `openclaw.json` (unlike the DM credentials store, which it reads per-message). So the write
   is followed by a `restart_gateway` kick. A kick failure surfaces a
   `gateway_restart_warning` (the config is durable) rather than rolling back a confirmed
   write — mirroring the memory-slot / plugin-pin / arbiter-applier writers.
6. **Capability.** Gated on `bot.channel.config` (the same gate as the per-channel
   newcomer-mode write); a no-op for the UI-trusted path, a real check for gateway-attested
   callers.

**✅ PR3 (LANDED 2026-07-01, [#3379](https://github.com/evolve-ops/evolve/pull/3379)) — the
`roster_group_allowlist_changed` drift Signal.**
Diagnosis fix-point #3 also called for a Signal fired (via `signals.store.observe`) when
`channels.<ch>.allowFrom` diverges out-of-band from what the admin last wrote — so the operator
is never blind to an OC-CLI / hand-edit / membership-sync change to who can spend the bot's
tokens in a channel. Built as its own safety-net bite with its own expected-state store + sweep
cadence, exactly as PR2 anticipated:
- **Expected-state store** — `evolve_admin/roster_baseline.py`. The admin approve/revoke path
  (`routes_bot_users._group_allowlist_write`) records the baseline from the config it just
  persisted, via the SAME `roster_resolver.read_group_allowlists` seam the monitor reads with (no
  re-read, no race), so an admin change never reads as drift. The monitor seeds the baseline once
  on first sight of a bot and never advances it again (a widening cannot self-resolve).
- **Monitor** — `packages/analyzer/roster_allowlist_drift_monitor.py` (hourly, pod-wide, evolve
  user). Fires `roster_group_allowlist_changed` per drifted `(bot, channel)` — `details.added` is
  the risk (new senders who can spend the bot's tokens). Fail-safe: an unreadable `openclaw.json`
  fires `roster_group_allowlist_unreadable` (a blind tick must not read as clean) and leaves that
  bot's drift Signals untouched; `sweep_resolve` archives cleared conditions.
- **Boundary** — detection/observation only; no auto-remediation (enforcement stays OpenClaw's
  fail-closed gate). Producer `roster_allowlist_drift`: severity warn, category security,
  protection-registry NONE+gap (drift self-quiets via seed-on-first-sight; the unreadable class is
  the bring-up transient). Co-owned with META:edr.

### G3 governance-parity verification (2026-06-30, audit #3378) — Discord confirmed at Slack parity; no wiring change

R1a's audit (#3378) flagged gap **G3**: PR2 reportedly covered slack/telegram/discord, but
the **Discord** surface + management wiring was UNVERIFIED, and WhatsApp isn't live. This
subsection records the verify-first outcome. **Finding: Discord is already wired end-to-end
through the exact same canonical path as Slack — no behavior change was needed.** The proof is
that every piece of the R1a group-allowlist plumbing is *channel-generic*, iterating the single
`KNOWN_PROVIDERS = (telegram, slack, discord, whatsapp)` tuple (mirrored by
`roster_resolver.ROSTER_CHANNELS`, kept in sync by
`test_roster_resolver.test_channels_match_known_providers`) rather than branching per provider:

- **Read (curated list).** `_read_per_channel` loops `for ch in KNOWN_PROVIDERS`, building
  `group_access[]` and `group_allowlist_gated` for each channel — including `discord` — via the
  canonical `roster_resolver.read_group_allowlists` → `effective_group_allowlist`. Discord's
  `channels.discord.allowFrom` (under `groupPolicy: allowlist`) is surfaced identically to
  Slack's; the live mini bot's empty→denies-all list reads as a gated-but-empty channel
  (`group_allowlist_gated: true`, `group_access: []`), correctly showing the add-by-id control.
- **Write (enforced list).** `_handle_group_action` validates `channel in KNOWN_PROVIDERS`,
  gates on the same `bot.channel.config` capability, then calls `_group_allowlist_write` →
  `_apply_group_allowlist_change` → `_group_allowlist_target_key`. That target-key selection
  mirrors `effective_group_allowlist` exactly, so the write lands in the **same** artifact the
  GET reads. The one channel-specific branch anywhere on this path is the Slack
  `U`/`W`-prefix id-format guard (`if add and channel == "slack" …`) — a *stricter* input
  validation for Slack only, **not** a fork of the canonical join. Discord (numeric-snowflake
  ids) flows through the generic path unguarded, exactly as Telegram does.

So the enforced list (what the OC group gate reads) IS the curated list (what the Users page
approves/revokes) for Discord — R1a's parity dimension answers **"yes"** for Discord, same as
Slack. Because the path is provider-agnostic, this required **no wiring change**: the deliverable
is this note plus a regression test
(`test_routes_bot_users.py::test_discord_group_allowlist_round_trips_same_canonical_path_as_slack`
and siblings) asserting the Discord group allowlist round-trips (GET `group_access` → approve →
GET reflects → revoke) through the identical canonical resolver/write path Slack uses, so any
future refactor that special-cased Discord — or dropped it from `KNOWN_PROVIDERS` — reds CI.

**WhatsApp.** `enabled:false` on the only bot, so it cannot be live-probed. The code path is
already provider-agnostic (WhatsApp is in `KNOWN_PROVIDERS` / `ROSTER_CHANNELS` and flows
through the identical generic read+write path), so no WhatsApp-specific plumbing is built.
**Backlog (META:users, co-`edr`):** add a WhatsApp parity check (GET+approve+revoke round-trip)
when a WhatsApp channel first goes live on a pod.
