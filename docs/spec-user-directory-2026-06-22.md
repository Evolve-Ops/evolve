# Spec: user directory — the unified per-bot Person model (2026-06-22)

**Status:** draft (2026-06-22) · **Aspect:** [`users`](spec-users-meta-2026-06-15.md) (this is the
realization of the charter's **roadmap R2 — universal cross-platform user model**)
**Companion docs:**
- [spec-users-meta-2026-06-15.md](spec-users-meta-2026-06-15.md) — the aspect charter (§6 store-coherence bug, §8 roadmap R1/R1a/R2, §F1 USER.md findings)
- [spec-user-roster-and-roles-2026-06-07.md](spec-user-roster-and-roles-2026-06-07.md) — roles, capabilities, the overlay, the 4 enforcement layers (the `membership` half of a Person)
- [spec-user-profile-2026-05-07.md](spec-user-profile-2026-05-07.md) — the private behavioral profile (D6 admin-opacity — **amended by §7 here**)
- [spec-multi-user-alias-2026-06-01.md](spec-multi-user-alias-2026-06-01.md) — `correspondence` / "Email alias" (send-as, **distinct** from a Person's contact emails)

Touch points (existing seams this extends, not greenfield):
- [`packages/admin/evolve_admin/roster_resolver.py`](../packages/admin/evolve_admin/roster_resolver.py) — `resolve_roster(...)` (the canonical full-read), `resolve_email`, `resolve_display_name`, `resolve_labels`
- [`packages/admin/evolve_admin/roster_overlay.py`](../packages/admin/evolve_admin/roster_overlay.py) — overlay at `{shared_dir}/rosters/{bot_id}.json` (evolve-owned, atomic, audit log under `rosters/log/`)
- [`packages/plugin/src/observer/TurnObserver.ts:1575`](../packages/plugin/src/observer/TurnObserver.ts) `before_prompt_build` → `_buildSpeakerContextBlock` (1655) → [`roleResolver.ts:209`](../packages/plugin/src/util/roleResolver.ts) `buildSpeakerContextBlock`

---

## 0. Why this exists — the address-book case

An operator asked a personal-assistant bot (`personal-bot`) to email four people a meeting invite,
then corrected their primary addresses ("use `dana@example.net` as Dana's primary,
`dana@acme.example` secondary"). The bot's reply: *"I've also updated my memory with their personal
emails as primary and the work domain as secondary for future reference."*

That sentence is the bug. The bot had **nowhere to durably store a person's contact identity except
its own freeform memory** (`USER.md` / contact cards / daily logs — the bot-authored prose layer
OpenClaw ships on every bot). The consequence:

- **Invisible to the operator.** The admin Users page shows the roster (IDs, roles, names, one
  Slack-scoped email) — never the bot's freeform notes. The emails the bot saved cannot be seen.
- **Un-editable from the admin UI.** The operator cannot correct, add, or remove them.
- **Not a standard.** Every bot lands here because there is no canonical contact store with a bot
  write-path — so this is the common *bad default*, not a designed approach.
- **It's the §6/§F1 drift, in a new dimension.** The aspect was carved because the bot
  hand-curates *identity* into prose (authority flowing the wrong way). The same failure now
  shows up in *contact data*. The fix is the same shape: one canonical store, projected into the
  bot, that the bot reads — and, new here, **writes through a tool, not into prose.**

These four people are also not bot *users* — they never talk to the bot. They are an **address book**
the bot acts *toward*. The current architecture has no home for that population at all.

## 1. Decision summary (operator, 2026-06-22 design)

Three forks were locked, plus one trust assumption:

| Fork | Decision |
|---|---|
| **Scope** | **One unified Person model.** A roster user is a Person *with* `membership`; a contact is the same record *without* it. Address book + admitted users in one directory. (= charter R2.) |
| **Privacy** | **Surface everything to the Evolve admin**, incl. the today-private behavioral profile — **under the current trust assumption (§7).** |
| **Bot writes** | **Bot writes via a tool, provenance-flagged `bot-asserted`.** Operator edits promote a field to `operator-verified`. Ends drift-into-memory; operator sees/overrides all. |
| **Trust assumption** | Today: **Evolve admin == data owner** — the operator may see all PII on their pod. A future model where a *bot owner* sees bot PII masked from the Evolve admin is **roadmap R3 (§8)**, not built. |

## 2. The model — `Person`

Per-bot, keyed by a stable Evolve-minted person id (survives handle/email churn):

```jsonc
Person {
  person_id: "pers_…",                    // Evolve-minted, stable
  names:      { display, goes_by?, legal? },
  identities: [                           // messaging info — "slack ID, telegram ID, names"
    { platform:"slack",    id:"U0…", handle:"@dana", source:"channel-captured" },
    { platform:"telegram", id:"12345",             source:"operator-verified"  },
  ],
  emails: [                               // primary + N secondary, ordered
    { addr:"dana@example.net",   rank:"primary",   provenance:"bot-asserted",      verified:false },
    { addr:"dana@acme.example",  rank:"secondary", provenance:"operator-verified", verified:true  },
  ],
  contact: { phone?, org?, notes?, … },   // growing attributes (open map)
  membership: {                           // PRESENT ⇒ admitted user of THIS bot; ABSENT ⇒ pure contact
    admitted:true, role:"participant", capabilities:[…], channels:[…],
  },
  profile_ref: ".openclaw/profiles/<user_key>.md",   // → the behavioral profile (§7)
  audit: [ { field, from, to, by, source, at } … ],  // per-field provenance trail
}
```

- A **contact** = Person with no `membership` (the §0 invitees).
- An **admitted user** = Person with `membership`. Admitting a contact runs the *existing*
  fail-closed admission flow → it gains `membership`. **Roles/permissions are not re-forked** —
  `membership` is a *view over the existing roster* (overlay + allowlists), enforcement unchanged.
- **`provenance` per field** is load-bearing: `channel-captured` (e.g. Slack `users.info`),
  `bot-asserted` (the bot's tool write), `operator-verified` (operator entered/confirmed in the
  UI). The UI badges these; operator-verified outranks bot-asserted on display and conflict.

### Email semantics (the operator's explicit ask)
- Exactly one `rank:"primary"`; zero-or-more `secondary`. Re-ranking is an operator (or bot) edit
  that flips ranks atomically. This is a person's **contact emails** — *distinct from*
  `correspondence.email_address` (the bot's *send-as* From header, [multi-user-alias](spec-multi-user-alias-2026-06-01.md)).
  A Person's primary email *may* be offered as a default when configuring send-as, but the two
  stores stay separate (send-as is "who the mail is from"; emails[] is "how to reach this person").

## 3. The four stores today → how Person subsumes them

| Store | Owns | Admin-editable? | Bot writes? | Fate under this spec |
|---|---|---|---|---|
| **Roster** (allowlist + overlay + `name_resolver` + activity, joined by `resolve_roster`) | IDs, names, roles/admission, activity, 1 Slack email | Yes | No | Becomes the **`membership` + base-identity** source of a Person; `resolve_roster` grows into `resolve_person` |
| **`.openclaw/profiles/`** (`user_profile`) | Inferred behavioral profile | **No — D6 opaque** | Yes (private) | Linked via `profile_ref`; surfaced to admin in Phase 4 (§7) |
| **`correspondence` / "Email alias"** | Bot send-as From header | Yes | No | Stays separate; may *read* a Person's primary email as a default |
| **`USER.md` / contact cards** (where the §0 bot put it) | Freeform prose | No | Yes | **Demoted from a data store to prose**; the directory digest (§5) outranks it for IDs/emails; optional managed-block projection (charter §F1b) |

**Nowhere today is there: messaging IDs + primary/N-secondary emails + growing contact attrs +
a bot read/write path + admin editability + a tie to roles.** Person is exactly that object.

## 4. Storage & the resolver seam

**The seam already mostly exists.** `roster_resolver.resolve_roster(...)` is "the canonical
full-read … loads the overlay + activity," and the Users page GET and (planned) bot injection
both consume it. This spec **extends that projection into a `Person`** rather than introducing a
parallel subsystem.

- **`membership` + base identity** → sourced from the existing roster (`roster_overlay`
  `{shared_dir}/rosters/{bot_id}.json` + allowlists + `name_resolver`). Unchanged, security-critical.
- **Directory-owned new fields** (`person_id`, `emails[]`, `contact`, operator-/bot-added
  `identities`, `profile_ref`) → a sibling directory store, `{shared_dir}/directory/{bot_id}.json`
  (evolve-owned, atomic temp-file+rename, audit log under `directory/log/` — same shape as
  `rosters/`). **No `/tmp`-sudo dance** (it's under `{shared_dir}`, evolve owns it).
- **`resolve_person(bot_id, person_id|handle|email)`** joins the two and is the **only** read path
  for consumers — Users page GET, bot injection, and the bot lookup tool all call it. *Same code
  path → admin and bot cannot diverge* (charter invariant #1).

> **R3 forward-compat seam (§8):** consumers MUST go through `resolve_person` and never read the
> directory store directly. That single indirection is what lets a future principal-separation
> model relocate or encrypt the PII store without touching any consumer. It is the one design cost
> we pay now, and it is free (we're building the resolver regardless).

## 5. Bot read/write — the tool the bot didn't have

1. **`directory_lookup(name | email | handle)`** (plugin tool) → returns the resolved Person.
   *Subsumes* the charter's planned `roster_lookup` read tool (F1b piece 2).
2. **`directory_upsert(person_ref, { emails?, contact?, identities?, names? })`** → writes
   directory-owned fields with `provenance:"bot-asserted"`. **Hard constraint: it CANNOT touch
   `membership` / roles / admission** — those mutate only through the existing operator-gated,
   fail-closed admission path. The bot can describe *who someone is and how to reach them*; it
   cannot grant *what they may do*.
3. **Directory-digest injection** — grow `buildSpeakerContextBlock` from "who is talking now" to a
   size-bounded digest of this bot's Persons (names, handles, primary email, role). The injected
   digest **outranks `USER.md`** for IDs/emails, so the bot stops hand-typing them into prose.
   *(= charter F1b piece 1, generalized from roster to directory.)*
4. **`USER.md` demotion** — injection-first (no file write). An optional, separately-gated
   follow-up may append a delimited `<!-- EVOLVE-DIRECTORY:BEGIN (managed) -->` block per charter
   §F1b — never a rewrite of bot prose.

This is the piece that closes the §0 case: "set Dana's primary to `dana@example.net`, secondary
`dana@acme.example`" becomes a `directory_upsert` → lands in the canonical store → appears in
the admin UI tagged `bot-asserted` → survives, instead of rotting in the bot's memory.

## 6. Admin UI — the Person card

The Users page grows from "roster (admission/roles)" to the **full directory**:
- Per-Person card: identities (platform handles), **emails (primary/secondary — add · edit ·
  reorder · verify)**, contact attributes, role/capabilities, provenance badges, audit trail.
- **Contacts** (no `membership`) appear in the same directory, filterable ("Users" vs "Contacts"
  vs "All"); an **Admit** action on a contact runs the existing admission flow.
- Behavioral profile content rendered inline (Phase 4, §7).
- All presentation routes through `ui` (style-guide §9.2 input widths, badges, expand/collapse).
  Page-content truth stays here; visual review is `ui`'s.

## 7. Privacy — surface-to-admin under the current trust assumption

The operator chose **surface everything**, including the behavioral profile that
[spec-user-profile §D6](spec-user-profile-2026-05-07.md) deliberately made admin-opaque (admin
sees only a binary `any_has_content`; `set_evolve_read_acl` carves `~/.openclaw/profiles/*.md`
out of the evolve read ACL; per-user DNT wipe-on-flip is invisible to admin).

**Amendment (gated behind Phase 4):** under the current trust assumption — **Evolve admin == the
bot's data owner** — the operator may read profile content. Phase 4:
- Removes the profiles ACL carve-out (or reads via the existing `sudo /bin/cat` path) so the admin
  server can render profile content in the Person card.
- **Reframes, does not delete, DNT.** DNT stays a *genuine opt-out* — "don't profile me at all,"
  wipe-on-flip — rather than "profile me but hide it from the admin." The user's real lever (no
  inference) is preserved; the "private from the operator" framing is what's dropped, honestly,
  because the operator owns the bot.
- **Multi-user guardrail (confirm at Phase-4 build).** For genuine multi-user team bots the
  profiled humans are third parties, not the operator. Default = surface (per the decision), but
  the build re-confirms whether *behavioral-inference* visibility should be gated by bot-type
  (single-principal vs multi-user) while *directory* fields (IDs/emails/contact) are always
  operator-visible. This is a copy/consent question, not a re-litigation of the decision.

This amendment is recorded as a pointer in spec-user-profile §D6.

## 8. Roadmap R3 — principal separation (bot-owner-scoped PII, operator-masked)

The operator flagged the future: a structure where a **bot owner** (distinct from the **pod
operator / Evolve admin**) sees bot-specific PII that is **masked from the Evolve admin**. This is
a real architecture, deferred, with issues named so the seam (§4) is built honestly:

1. **Policy-masking ≠ enforced-masking.** Hiding PII in the admin *UI* is trivial but not
   confidential — the data lives on a box the operator administers and the admin server runs as
   `evolve` with ACL/sudo reach. *Enforced* masking on operator-controlled hardware needs
   **cryptographic separation** (envelope encryption under a bot-owner-held key the operator can't
   extract) — and any key the `evolve` process can use to decrypt-for-display is a key the
   operator can scrape. So real masking likely implies a **different deployment/key-custody model**,
   not a toggle.
2. **The server is the broker.** Name resolution, Usage enrichment, server-side wizard extraction,
   the directory-digest build all run as `evolve` on cleartext. If the operator can't read PII,
   neither can that server → each feature moves **bot-side** (decrypt in the bot's claw) or goes
   dark. A per-feature capability cost.
3. **"Bot owner" must become a distinct principal** — its own identity, auth, and key custody.
   None exists today (effectively one operator).
4. **D6 is the prototype.** The metadata/content split (operator sees "a user exists / activity /
   cost / admission state / enforcement," never content) is exactly D6's `has_content` binary,
   generalized from the profile to all PII and upgraded from ACL-enforced to crypto-enforced.

**Disposition:** R3 is a `users` × `edr`/security joint (the crypto/enforcement half is `edr`); it
is a **candidate to carve its own aspect** if/when pursued. Not built now. The only thing it
demands today is §4's resolver indirection.

## 9. Phasing → chips

| Phase | What | Surface / risk | Status |
|---|---|---|---|
| **0** | **This spec** + charter §3/§8 + profile §D6 amendment | docs | *this bout* |
| **1** | `resolve_roster` → `resolve_person` + `Person` model + `{shared_dir}/directory/{bot_id}.json` store (behind the resolver). Users-page GET & bot both consume it. | admin-side, reversible | next dispatch |
| **2** | Emails (primary/secondary add·edit·reorder·verify) + contact attrs + provenance badges + contacts in the directory + Admit action | admin + SPA (`ui`-collab) | backlog |
| **3** | `directory_lookup` + `directory_upsert` (bot-asserted; **never** `membership`) + directory-digest injection + `USER.md` demotion *(= generalized F1b)* | plugin/TS, privileged hot-path, canary, auditor-grade | backlog |
| **4** | Surface behavioral profile to admin: ACL grant, render in Person card, DNT reframe, multi-user guardrail | ACL change — **`edr`/security-collab** | backlog |
| **5** | Cross-platform Person linking ("Telegram-@alice == Slack-alice?") as admin metadata, **never an auth shortcut** — full R2. **→ Designed in [spec-user-identity-merge-2026-06-23.md](spec-user-identity-merge-2026-06-23.md)** (one `person_id` spans N identities; operator-driven, reversible merge; merge unifies identity, never authority; suggest-never-auto-apply; the consolidated per-person Users page). | follow-on | spec drafted (R2) |

Phase 1 finishes the long-backlogged canonical-resolver consolidation (charter §7 backlog #1) as a
side effect, and is the prerequisite for 2–4.

## 10. Invariants (seed)

> **R2 extends these** with four more (merge unifies identity not authority; merge is
> non-destructive + reversible; never auto-merge; blocked is per-identity) — see
> [spec-user-identity-merge-2026-06-23.md §8](spec-user-identity-merge-2026-06-23.md).

1. **One read path.** Every consumer (Users page GET, bot injection, bot lookup tool) resolves
   through `resolve_person`; nothing reads the directory store directly. (Enables R3; prevents
   admin/bot divergence — charter invariant #1.)
2. **The bot may assert identity/contact, never authority.** `directory_upsert` writes
   emails/contact/identities only; `membership`/roles/admission mutate solely through the existing
   fail-closed operator-gated path.
3. **Provenance is never lost.** Every field carries `{provenance, by, at}`; `operator-verified`
   outranks `bot-asserted` on conflict and display.
4. **Contacts and users are one model.** A contact is a Person without `membership`; admitting it
   adds `membership` through the existing flow. No second store, no second resolver.
5. **Directory ≠ private profile.** Directory fields (who/how-to-reach/what-they-may-do) are
   operator-managed; the behavioral profile (what the bot has learned) stays a linked, separately
   governed store (§7). Surfacing the latter is gated and reframes DNT but never deletes the
   user's opt-out.

## 11. Boundary / hand-offs

- **Presentation** (Person card layout, badges, widths) → `ui`. Page-content truth stays here.
- **Enforcement / crypto** (admission fail-closed; R3 masking) → `edr`/security joint.
- **Send-as alias** (`correspondence`) → stays its own [multi-user-alias](spec-multi-user-alias-2026-06-01.md) concern; reads a Person email as a default only.
- **Per-user cost/tier** → `model-tiers`.
- **Cross-bot evo roster path** → `evo-asst`.
- **R3 principal separation** → `users` × `edr`; carve-candidate when pursued.
