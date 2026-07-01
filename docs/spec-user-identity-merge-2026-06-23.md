# Spec: cross-platform Person identity + operator-driven merge (R2, 2026-06-23)

**Status:** draft (2026-06-23) · **Aspect:** [`users`](spec-users-meta-2026-06-15.md) ·
this realizes **roadmap R2** ([charter §8](spec-users-meta-2026-06-15.md)) and the
[directory program spec](spec-user-directory-2026-06-22.md) **§9 Phase 5** ("Cross-platform
Person linking — full R2"). It **resolves charter open Q4** (cross-platform identity, distinct
per platform today — `spec-user-roster-and-roles-2026-06-07.md` §"Open questions" #4, line 409).

**Companion docs:**
- [spec-user-directory-2026-06-22.md](spec-user-directory-2026-06-22.md) — the program spec
  this extends. The `Person` model (§2), the four stores (§3), the `resolve_person` seam (§4),
  the Person card (§6), the invariants (§10). **R2 is built *behind the seam that spec paid
  for* — no new subsystem.**
- [spec-users-meta-2026-06-15.md](spec-users-meta-2026-06-15.md) — the aspect charter. R2
  backlog (§8), the two-allowlist group/DM diagnosis (§"R1a diagnosis" — the access data this
  spec folds onto a person), the §9 admin-claims-vs-bot-reality lens.
- [spec-user-roster-and-roles-2026-06-07.md](spec-user-roster-and-roles-2026-06-07.md) —
  roles, capabilities, the four enforcement layers (the **authority** half — §3 here is the
  load-bearing boundary against it).

Touch points (existing seams this extends, **not** greenfield):
- [`packages/admin/evolve_admin/user_directory/model.py`](../packages/admin/evolve_admin/user_directory/model.py)
  — `Person`, `Identity`, `Email`, `Membership`, `PROVENANCE`, `provenance_outranks`.
- [`packages/admin/evolve_admin/user_directory/storage.py`](../packages/admin/evolve_admin/user_directory/storage.py)
  — the directory store (keyed by `identity_key` = `"<platform>:<stable_id>"`), `person_id_for`
  (deterministic mint), `mint_person_id`, `merge_emails_preserving_stronger`,
  `merge_identities_preserving_stronger`, `_enforce_single_primary`.
- [`packages/admin/evolve_admin/user_directory/resolver.py`](../packages/admin/evolve_admin/user_directory/resolver.py)
  — `resolve_person` / `resolve_persons` / `resolve_identity` / `build_person`: THE canonical join.
- [`packages/admin/evolve_admin/user_directory/bot_view.py`](../packages/admin/evolve_admin/user_directory/bot_view.py)
  — `person_to_bot_dict`, `build_digest`, `is_blocked` (the bot-facing projection).
- [`packages/admin/evolve_admin/web/routes_directory.py`](../packages/admin/evolve_admin/web/routes_directory.py)
  — operator write routes (`operator-verified`).
- [`packages/admin/evolve_admin/roster_overlay.py`](../packages/admin/evolve_admin/roster_overlay.py)
  — the `ignored` negative-set pattern (`ignore_identity`, keyed `"<platform>:<stable_id>"`) the
  merge-dismissal store mirrors.
- [`packages/admin/evolve_admin/roster_resolver.py`](../packages/admin/evolve_admin/roster_resolver.py)
  — `effective_group_allowlist` / `read_group_allowlists` (group access) + `resolve_roster` (DM
  approval + role + activity) — the access data §6 folds per-identity.

> **Placeholder note (PII / public-scrub).** Every person in this spec is fake, per
> [docs/PLACEHOLDER_NAMING.md](PLACEHOLDER_NAMING.md). The running example is **Dana Lopez**,
> reachable on `personal-bot` as telegram `@dana_l` (`tg:12345`) and slack `@dlopez`
> (`slack:U0DANA`), with contact emails `dana@example.net` (personal) / `dana@acme.example`
> (work) and person id `pers_dana…`. No real name, handle, or email appears here; `test_public
> _launch_scrub` guards the file.

---

## 0. Why this exists — one human, two rows

On the admin Users page the operator sees **the same person as two separate rows** because
identity is **distinct-per-platform today**. Concretely, on `personal-bot`:

| Row | identity | person_id | role | emails |
|---|---|---|---|---|
| **Dana Lopez** | telegram `@dana_l` (`tg:12345`) | `pers_dana_tg…` | participant (DM) | — |
| **Dana Lopez** | slack `@dlopez` (`slack:U0DANA`) | `pers_dana_sl…` | participant (group) | `dana@acme.example` |

Two `Person` rows, two different `person_id`s, two roles — for one human. There is:

- **no way to say "these are the same person"** — no merge affordance;
- **no visible stable id** to track a unique human across platforms (the `pers_…` id exists in
  the model but is not surfaced — [program spec §11 backlog](spec-user-directory-2026-06-22.md));
- **no single place** that aggregates Dana's two identities, two channels of access, and her
  emails into one card.

This is exactly **charter Q4 left unanswered**: cross-platform identities are distinct by
default and nothing unifies them. The motivating observation is the operator's own pod, where a
`personal-bot` user appears under both Telegram and Slack. R2 closes it.

The directory program already minted the right primitive: a **stable `person_id`**
(`pers_<16hex>`, [`storage.person_id_for`](../packages/admin/evolve_admin/user_directory/storage.py)),
and [`storage.mint_person_id`'s docstring](../packages/admin/evolve_admin/user_directory/storage.py)
already anticipates this work — *"a future phase that merges two identities into one Person can
override the stored `person_id`."* R2 makes one `person_id` span N platform identities, surfaces
it, and gives the operator a reversible merge.

## 1. Decision summary (the locks this spec asserts)

| # | Fork | Decision |
|---|---|---|
| **D1** | What is the unit of identity? | **The person.** One `person_id` aggregates N platform identities. (= charter R2 "the *person* is the unit"; [directory §2](spec-user-directory-2026-06-22.md).) |
| **D2** | How is the link stored? | **Merge changes the JOIN, not the DATA.** A merge sets the absorbed identities' canonical `person_id` to the survivor's and records a reversible **merge ledger** entry; the per-identity field data (emails/contact/names) is **never physically combined** — the union is computed *at read time in the resolver*. Un-merge restores the links from the ledger; each identity's own data was never touched. (§2) |
| **D3** | **THE AUTHORITY QUESTION** | **Merge unifies *identity*, never *authority*.** Roles/capabilities/admission stay **per-membership**; a merged person carries one `Membership` per admitted identity, never a unified or union'd role. **A merge can never grant, escalate, or change any capability.** (§3 — flagged for `edr`/security to ratify.) |
| **D4** | Auto-merge? | **Never.** The system may *suggest* candidate merges (same display name, or a shared email); the operator must apply each one. A persisted **"not the same — don't suggest again"** negative stops a rejected pair resurfacing. (§4) |
| **D5** | Who can merge? | **Operator only.** The bot has no merge tool; bot identity-assertion (`directory_upsert`) can add an identity *row* to a person it already resolves, but can never **link two pre-existing distinct Persons** (that is authority-adjacent — invariant). (§2.4) |
| **D6** | Reversibility | **Fully reversible.** Un-merge splits back into the original Persons. The merge is non-destructive by construction (D2); the ledger records each absorbed identity's prior `person_id` so a *chain* of merges reverses correctly. (§2.3) |

## 2. The cross-platform Person model

### 2.1 Today vs. merged shape

**Today** ([resolver `resolve_persons`](../packages/admin/evolve_admin/user_directory/resolver.py)):
one `Person` per `(platform, stable_id)`. Dana = two Persons.

**Merged:** one `Person` whose `identities[]` holds both rows, keyed by **one canonical
`person_id`**:

```jsonc
Person {
  person_id: "pers_dana…",                 // canonical, stable, VISIBLE (§5)
  names: { display: "Dana Lopez" },
  identities: [                            // N after merge (was always 1)
    { platform:"telegram", id:"12345",   handle:"@dana_l", source:"channel-captured" },
    { platform:"slack",    id:"U0DANA",  handle:"@dlopez", source:"operator-verified" },
  ],
  memberships: [                           // ⬅ NEW: a LIST — one per ADMITTED identity (§3)
    { identity:{platform:"telegram", id:"12345"}, admitted:true, role:"participant",
      capabilities:[…], channels:["telegram"] },
    { identity:{platform:"slack",    id:"U0DANA"}, admitted:true, role:"participant",
      capabilities:[…], channels:["slack"] },
  ],
  emails: [                               // unioned across identities AT READ TIME (§2.2)
    { addr:"dana@example.net",  rank:"primary",   provenance:"bot-asserted",      verified:false },
    { addr:"dana@acme.example", rank:"secondary", provenance:"operator-verified", verified:true  },
  ],
  contact: { … },                         // unioned at read time, provenance precedence
  profile_refs: [ ".openclaw/profiles/<tg_key>.md", ".openclaw/profiles/<slack_key>.md" ],
  merged_from: [                          // ⬅ NEW: present iff this is a merge result (§2.3)
    { person_id:"pers_dana_tg…", identity_key:"telegram:12345" },
    { person_id:"pers_dana_sl…", identity_key:"slack:U0DANA" },
  ],
  audit: [ … ],
}
```

**Model deltas** (`model.py`):
- `Membership` gains an `identity: {platform, id}` field and `Person.membership` (singular)
  becomes **`Person.memberships: list[Membership]`** — one per admitted identity. There is **no
  merged/union role** (§3). For back-compat, a derived read-only `membership` property returns
  the *anchor identity's* membership (or `None`); it is explicitly **not** a union and consumers
  computing authority MUST iterate `memberships` per-identity. `is_blocked` becomes per-identity
  (§2.5), not per-person.
- `Person` gains `merged_from: list[{person_id, identity_key}]` (empty for an unmerged person)
  and `profile_refs: list[str]` (the per-identity profile links; a merged person may have N —
  Phase-4 profile surfacing renders each).

### 2.2 Where the link lives — the canonical `person_id`, grouped at resolve time

The directory store stays **keyed by `identity_key`** (`"<platform>:<stable_id>"`) — one entry
per identity, exactly as today. The change is in **how the resolver aggregates entries into
Persons**:

1. **The grouping key is the entry's `person_id` field.** Today the resolver emits one Person
   per identity; under R2 it **groups all identities sharing a canonical `person_id` into one
   Person**. For an unmerged identity the `person_id` is the deterministic
   [`person_id_for(bot_id, platform, stable_id)`](../packages/admin/evolve_admin/user_directory/storage.py)
   default (stable without a write) — so an unmerged pod resolves **identically to today** (every
   identity is its own group of one). This is precisely the override the `mint_person_id`
   docstring anticipated.

2. **A merge overrides the absorbed identities' stored `person_id`** to the survivor's canonical
   id. Grouping then folds them together. A merge of a **roster-only** identity that has no
   directory entry yet **mints one** (carrying only the canonical `person_id` link) — a harmless
   link-only entry that resolves to the same Person it would have without the row.

3. **Field union happens in the resolver, at read time — never on disk.** When a group has >1
   identity, `build_person` unions their `emails` / `identities` / `contact` / `names` using the
   **existing provenance-precedence helpers**
   ([`merge_emails_preserving_stronger`, `merge_identities_preserving_stronger`,
   `_enforce_single_primary`](../packages/admin/evolve_admin/user_directory/storage.py)): a
   stronger-provenance row wins a conflict (`operator-verified` > `bot-asserted` >
   `channel-captured`), and the whole merged person carries **at most one `primary` email** (if
   two identities each had a primary, precedence picks one and demotes the other for display).
   **Nothing is rewritten on disk** — this is the property that makes un-merge trivial.

> **Why grouping-not-combining (D2).** The alternative — physically merging both entries' fields
> into one row on disk — would require snapshotting and restoring field state to un-merge, and
> would *discard legitimate post-merge edits* on a split. By keeping each identity's data in its
> own entry and unioning at read time, the **only** persisted mutation a merge makes is the
> `person_id` link (+ the ledger). Un-merge restores the links; the data was never disturbed.
> The pure-link-table alternative (leave `person_id` as the deterministic default, store grouping
> in a side table) was considered and **rejected** in favor of overriding the entry's `person_id`
> field: the resolver already reads that field, so grouping needs no second lookup layer, and it
> matches the seam the code's own docstring anticipated. The ledger (§2.3) supplies the
> reversibility the side-table would have given for free.

### 2.3 The merge ledger (reversibility)

A top-level `merges` map in the directory store (`{shared_dir}/directory/{bot_id}.json`),
evolve-owned, atomic temp-file+rename, audit-logged under `directory/log/` — the **same
discipline as every other directory write** (no `/tmp`-sudo dance; CLAUDE.md):

```jsonc
"merges": {
  "mrg_7f…": {
    "merge_id": "mrg_7f…",
    "canonical_person_id": "pers_dana…",       // the survivor's id
    "absorbed": [                              // every identity folded INTO the survivor
      { "identity_key": "slack:U0DANA", "prior_person_id": "pers_dana_sl…" }
    ],
    "by": "<operator-login>", "at": "<iso>",
    "provenance": "operator-verified",         // the merge link is operator-stamped (§3)
    "active": true
  }
}
```

- **Merge** writes one ledger entry + overrides each absorbed identity entry's `person_id` to
  `canonical_person_id`. `prior_person_id` captures what that entry's `person_id` was *before this
  merge* — necessary because a chain of merges means the prior value may itself be a non-default
  (a previously-absorbed) id, not the deterministic mint.
- **Un-merge** (`active:false`, kept for audit) restores each absorbed identity's `person_id`
  to its recorded `prior_person_id`. Because the field data was never combined (§2.2), the two
  original Persons reappear exactly as they were at merge time.
- **Post-merge edits** (an operator edits an email on the merged Dana) land on the **canonical
  anchor identity's** entry (the survivor) — `routes_directory`'s write resolves a person id to
  its anchor `identity_key`. On un-merge those edits stay with the canonical person (they were a
  new fact about the survivor, never part of the absorbed identity). **Un-merge reverses the
  merge, not subsequent independent edits** — this is the intended, least-lossy semantics
  (snapshot-and-restore would silently drop them).
- **Idempotency / cycle safety:** merging an identity already in the target group is a no-op;
  the survivor's own identity can never appear in `absorbed`; canonical ids are themselves
  resolved through the active ledger so a chain `A←B`, then `A←C` keeps one group.

### 2.4 Who can merge — operator only (D5)

Merge / un-merge are **operator actions only**, stamped `operator-verified`. The **bot has no
merge tool.** The bot's existing `directory_upsert`
([directory_bot_routes](../packages/admin/evolve_admin/web/directory_bot_routes.py)) can add an
`identities[]` *row* to a person it already resolves — but that adds an identity to **one** entry;
it can never **link two pre-existing distinct Person entries** into one. New invariant:

> **Bot identity-assertion never merges two existing Persons.** If a bot upserts an identity that
> already keys a *different* Person, the resolver's existing dedup
> ([`resolver._match_stable_id`](../packages/admin/evolve_admin/user_directory/resolver.py))
> targets that existing entry (no duplicate), but the two Persons stay distinct — only an operator
> merge links them. This keeps person-linkage on the operator-gated surface, consistent with
> invariant #2 ("the bot may assert identity, never authority").

### 2.5 Blocked is per-identity, not per-person

`bot_view.is_blocked` today reads `Person.membership.role == "blocked"`. For a merged person this
must be **per-identity**: if Dana's Slack identity is blocked but her Telegram is fine, the bot
must still reach Dana via Telegram while the Slack identity is invisible. So:

- The bot-facing projection (`person_to_bot_dict`, `build_digest`) **strips blocked identities**
  from `identities` / `memberships`, and **omits the person entirely only if *every* identity is
  blocked**. A merge can therefore never *un-block* (or *block*) anyone — blocking stays a
  per-identity roster action, surfaced and reversed on the specific identity (§3).

## 3. THE AUTHORITY QUESTION (load-bearing — `edr`/security co-own)

> *If you merge an identity that is `admin` on one platform with one that is `participant` on
> another, what is the merged person's role and capabilities?*

This is the security boundary of the whole feature. Three options were weighed:

| Option | Behavior | Verdict |
|---|---|---|
| **A. Union / highest-wins** | The merged person gets the highest role / union of capabilities across identities. | **REJECTED — escalation trap.** Merging a low-privilege identity into a high-privilege one would **silently grant** the low one admin reach on its own platform. A merge is a *display/contact* convenience; it must never be an authority operation. An operator linking "these look like the same person" could unknowingly hand a participant admin powers. |
| **B. Per-identity roles preserved (merge unifies identity, not authority)** | Each identity keeps exactly the role/capabilities the roster gives it. The merged person shows N memberships, one per admitted identity. Enforcement is unchanged: it stays keyed on `(platform, stable_id)` below the LLM. | **RECOMMENDED.** A merge changes *who we think this is*, never *what they may do*. Capability is impossible to gain via merge by construction. |
| **C. Operator-confirms-role-on-merge** | The merge dialog asks the operator to pick the merged role. | Rejected as the *default* — it reintroduces the escalation path (an operator could pick the higher role) and conflates two concerns. Role changes already have their own fail-closed, audited path; merge should not become a second one. |

**Recommendation (this spec asserts, pending `edr`/security ratification): Option B —
merge unifies identity, NOT authority.** Concretely:

1. **Roles/capabilities/admission stay per-membership.** `Person.memberships` is a list; there
   is **no code path that computes a union or max** of roles or capabilities. The card shows each
   identity with its **own** role badge (§6).
2. **Enforcement is untouched.** All four enforcement layers
   ([roster-and-roles §8](spec-user-roster-and-roles-2026-06-07.md)) key on `(platform,
   stable_id)` below the LLM. Merge writes nothing the roster reads — it touches only the
   directory store's `person_id` link + ledger. **By construction a merge cannot change an
   enforcement decision.** This is the same property as program-spec invariant #2 ("identity,
   never authority"), generalized to the merge action.
3. **Block/admit/role changes remain per-identity**, on the specific identity, through the
   existing fail-closed operator-gated path — never on "the person."
4. **The merge link itself is `operator-verified`** (the strongest provenance) and audited, but
   that provenance governs the *identity claim* ("these are the same human"), not any authority.

**Flagged for `edr`/security:** ratify Option B as the final call, and decide the two residuals
this leaves: (a) whether a *display-only* "effective access summary" (read-only, never an
enforcement input) is worth showing on the card, and how to label it so it can never be mistaken
for a grant; (b) whether the merge dialog should *warn* when the two sides have differing roles
("these identities have different roles; merging does not change either") — a copy/clarity
question, not an authority one. The coordinator routes this to `edr` before the merge-route chip
(§7 chip 3) lands.

## 4. Suggestion heuristic — suggest, NEVER auto-apply (D4)

The system surfaces **candidate merges** but never applies one. The operator's hard constraint:
*"we can't know two users with the same name are the same person."*

### 4.1 The candidate signal (cheap, pure-Python — no LLM)

Computed over `resolve_persons(bot_id)` (per [feedback: RSI infra must be cheap — pure-Python by
default](spec-users-meta-2026-06-15.md)). A pair of **distinct** Persons is a candidate iff:

- **`shared_email`** — they share a contact email address (casefold-compared; reuses the
  resolver's `_norm_addr`). **Strong** signal — an email is near-unique.
- **`same_display_name`** — they have an exact casefold-equal display name. **Weak** signal —
  presented as a *possibility*, never a recommendation; this is the case the operator's
  constraint is about.

Each candidate carries its evidence (`signal`, the matching value, the two `person_id`s + anchor
identity keys) so the UI can show *why* it was suggested. Candidates whose pair is in the
**dismissal set** (§4.2) are excluded. Already-merged identities never appear (they are one
Person). The GET (§6) returns `suggested_merges: [...]`; nothing is ever auto-linked.

### 4.2 Dismissal — the persisted "not the same / don't suggest again"

Mirrors the [roster overlay's `ignored` negative-set pattern](../packages/admin/evolve_admin/roster_overlay.py)
(`ignore_identity`, keyed `"<platform>:<stable_id>"`, atomic, audit-logged). The merge-dismissal
set lives in the directory store:

```jsonc
"merge_dismissals": {
  "telegram:12345|slack:U0DANA": {          // UNORDERED pair of ANCHOR identity_keys, sorted
    "dismissed_at": "<iso>", "dismissed_by": "<operator>", "reason": "different people"
  }
}
```

- **Keyed by the unordered pair of the two persons' anchor `identity_key`s** (sorted, `|`-joined)
  — *not* by `person_id`, because a `person_id` can be reassigned by a later merge while an
  `identity_key` (the store key) never churns. This makes the negative churn-proof.
- A dismissed pair is skipped by §4.1 forever (until an operator un-dismisses — an optional
  `unignore`-style inverse, not required for the first cut; mirrors that
  `roster_overlay.unignore_identity` exists but is presently unwired).
- Written by an operator route (`POST …/directory/merge-dismiss`, body `{a, b}`), stamped/
  audited like every other directory mutation. A dismissal that later becomes moot (the pair gets
  merged anyway, or one side is deleted) is benign and prunable.

### 4.3 Where suggestions appear

On the consolidated Users page (§6), as an affordance on/near the two involved person cards
("Possible same person as **Dana Lopez** (slack `@dlopez`) — *Merge* · *Not the same*"). The
**visual is `ui`'s**; this spec defines only the data (`suggested_merges[]`) and the two actions
(merge → §2; dismiss → §4.2).

## 5. Visible, stable person ID (D1)

The `person_id` (`pers_<16hex>`,
[`storage.person_id_for`](../packages/admin/evolve_admin/user_directory/storage.py)) is already
stable across handle/email churn and is now **the** cross-platform handle for a human. R2
**surfaces it** so the operator can track a unique person:

- **Carried in the GET** (§6) per Person (it already is in `Person.to_dict()`).
- **Display form:** the full id is short enough to show verbatim (`pers_` + 16 hex = 21 chars). A
  monospace, copy-on-click chip on each card; truncating the middle (`pers_7f3a…b21c`) for width
  while copying the full value is `ui`'s call — the **data** requirement is: show it, make it
  copyable, keep it stable. After a merge the surviving canonical id is the one shown; the
  absorbed id is retained in `merged_from` / the ledger (recoverable on un-merge), so an operator
  who was tracking the absorbed id can still find where it went via the audit trail.

## 6. The consolidated Users-page information architecture (data-shape only — `ui` co-owns)

The operator chose to **unify** the Users page into one directory-centric, **per-person** view.
Today the page is organized **by channel**, and the same human is listed in *multiple* sections —
`APPROVED · DM` (DM-paired) and `CHANNEL ACCESS · GROUP` (group-allowlisted) list the same people
twice ([charter R1a diagnosis](spec-users-meta-2026-06-15.md); the GET returns `by_channel.<plat>.
{approved[], group_access[], seen_recently[], pending[]}` + a pod-level `blocked[]`). The
consolidated view collapses that onto **one card per person**, with **all access as badges**.

### 6.1 The per-person card's information model

Each card carries (visual → `ui`; this is the **data shape**):

| Group | Fields | Source |
|---|---|---|
| **Identity** | `person_id` (visible, copyable — §5); `names` (display / goes_by / legal); `identities[]` each `{platform, handle, id, source-provenance badge}` | directory store + resolver |
| **Access (per identity, ALL surfaces as badges)** | for **each** identity: **DM-approval** state (approved / pending / not-paired) and **group-allowlist** membership **per channel** (`#product-team ✓`, with `access_source` label); the **role** badge (per `Membership` — §3, never a merged role); newcomer-mode context | `resolve_roster` (DM + role + activity) + `read_group_allowlists` (group) |
| **Emails** | `emails[]` (primary/secondary, provenance + verified badges) — unioned across identities (§2.2) | directory store (read-time union) |
| **Contact** | `contact` open map | directory store |
| **Activity** | last_seen / turns_7d / turns_30d / cost_30d / sessions — **per identity** (a merged person shows each; an aggregate roll-up is a display choice for `ui`) | `user_activity` (via `resolve_roster`) |
| **Merge** | `merged_from[]` (if merged) → enables *Un-merge*; `suggested_merges[]` (§4) → *Merge* / *Not the same* | merge ledger + §4 heuristic |

### 6.2 Access folds onto the person — both DM and group on one row

The key consolidation: **DM-approval and group-allowlist membership both attach to one person,
per identity, as badges** — the two duplicate by-channel sections are retired. The access data
already exists and is read through the canonical join the program spec built:

- **DM approval** — the pairing store, surfaced today as `approved[]` and resolved through
  `resolve_roster` (it is the identity's `Membership`).
- **Group allowlist** — `channels.<ch>.allowFrom` / `groupAllowFrom`, read by
  [`roster_resolver.effective_group_allowlist` / `read_group_allowlists`](../packages/admin/evolve_admin/roster_resolver.py)
  (shipped [#2999]), surfaced today as the separate `group_access[]` section.

R2 attaches **both** to the identity they belong to, so Dana's card shows `telegram @dana_l — DM
✓` and `slack @dlopez — group ✓ #product-team` on **one** card instead of Dana appearing in two
sections (and, pre-merge, as two people).

### 6.3 Actions stay identity-scoped (the authority boundary in the UI)

The access actions — **Block / Disconnect (DM-revoke) / Revoke (group) / config / newcomer-mode**
— act on a **specific `(identity, channel)`**, not on "the person." On a merged card each access
badge carries its own action (you Block the *Slack identity*, not "Dana"). This mirrors §3 in the
UI: the merge unified *identity* for legibility, but every authority action still targets one
platform identity through its existing fail-closed route. **Merge / Un-merge** and **Not the same**
are the only *person*-level actions, and none of them touch authority.

### 6.4 The GET that feeds it

Extend the directory GET (`GET /api/admin/bots/<bot_id>/directory`,
[routes_directory](../packages/admin/evolve_admin/web/routes_directory.py), backed by
`resolve_persons`) to return, per Person, the §6.1 shape **with access + activity folded per
identity** — i.e. the resolver join is widened so each identity carries its DM-approval, its
group-channel memberships, its role, and its activity. Plus top-level `suggested_merges[]` (§4).
This **subsumes** the legacy `by_channel.{approved,group_access,seen_recently,pending,blocked}`
GET: the per-person GET is the one read path the consolidated page consumes. (`pending` requests
and `seen_recently`/not-yet-a-person actives remain — a pending/active identity that is not yet a
Person is rendered as a minimal "candidate" card or a dedicated lane; `ui` decides the placement,
the data is the same per-identity records.)

> **Co-owned hand-off to `ui`.** The card layout, badge styling, expand/collapse, input widths,
> theme parity, and where suggestions/pending render are `ui`'s, against
> [docs/style-guide.md](style-guide.md). **Page-content truth (which fields, what the GET
> returns, how access folds) stays here.** No pixels are designed in this spec.

## 7. Phasing → chips (build-phase breakdown)

| # | Chip | Owner | Privileged? | Reversible? | Notes |
|---|---|---|---|---|---|
| **0** | **This spec** + charter Q4 → resolved + directory-spec §9 Phase 5 / §10 cross-links + roster-and-roles Q4 pointer | `users` (docs) | no | yes | *this bout* |
| **1** | **Model + storage: multi-identity Person + merge ledger.** `Membership.identity` + `Person.memberships[]`/`merged_from[]`/`profile_refs[]`; `merges` ledger + `merge_dismissals` set in the directory store; `apply_merge` / `reverse_merge` storage helpers (override `person_id`, record `prior_person_id`); per-identity `is_blocked`. Pure model/storage, unit-tested. | `users` | no (admin-side store) | yes | Foundation; no behavior change until #2. |
| **2** | **Resolver: group-by-canonical-person_id + read-time union.** `resolve_persons`/`resolve_person`/`resolve_identity` group identities by canonical `person_id` (default = deterministic); union emails/contact/identities/names via the existing provenance-preserving helpers; resolve canonical id through the active ledger (chain-safe). Behind the seam → every consumer gets merged Persons. | `users` | no | yes | Unmerged pod resolves identically to today (regression-lock test). |
| **3** | **Merge / un-merge operator write routes + actions.** `POST …/directory/merge` (body `{into, from}`) and `…/directory/unmerge` (`{merge_id}`): `operator-verified`, gated `bot.roster.mutate`, audited; **structurally cannot touch membership/roles/admission** (reuse `upsert_entry`'s no-authority signature; routes never import roster mutators). Post-merge edits target the canonical anchor. | `users` · **`edr`/security co-own** | identity-linkage (admin-side; NOT authority) | yes (un-merge) | **Blocks on §3 authority ratification by `edr` before merge.** |
| **4** | **Suggestion heuristic + dismissal.** Pure-Python `same_display_name` / `shared_email` candidate signals over `resolve_persons`; `suggested_merges[]` in the GET; `POST …/directory/merge-dismiss` persists the unordered anchor-key-pair negative (mirrors `ignored`). **Never auto-applies.** | `users` · **`ui` co-own** (surfacing) | no | yes (dismissal is additive; un-dismiss optional) | |
| **5** | **Consolidated by-person GET + card information model.** Fold DM-approval + group-allowlist + role + activity **per identity** onto each Person in the directory GET; emit access-badge data; retire the duplicate `APPROVED·DM` / `CHANNEL ACCESS·GROUP` by-channel sections (the per-person card replaces them). Page-content truth here; **card/layout/badges → `ui`.** | `users` (data-shape) · **`ui` co-own** (presentation) | no | yes | Largest UX change; the directory page becomes the Users page. |
| **6** | **person_id surfacing.** Visible, copyable `pers_…` chip per card (§5). Small; can fold into #5. | `users` · **`ui` co-own** | no | yes | |
| **7** | **bot_view + digest: per-identity memberships + per-identity blocked filter.** `person_to_bot_dict` emits `memberships[]`; `build_digest` renders one line per merged person (collapsing N identities) and strips **blocked identities** (not blocked persons — §2.5). | `users` · **`edr`/security co-own** (blocked-projection correctness) | **yes — bot hot-path** | yes | Canary-gated, auditor-grade (a projection bug could leak/hide an identity). |

**Dependency order:** 0 → 1 → 2 → {3 (after `edr` §3 sign-off), 4, 7} → 5 → 6. Chips 1–2 are
inert-until-merged-data-exists (an unmerged pod is unchanged), so they can land ahead of the
operator-facing 3–6. Chip 7 (the privileged bot projection) is the only auditor-grade /
canary-gated piece.

## 8. Invariants (extends [program spec §10](spec-user-directory-2026-06-22.md))

In addition to the program spec's five (one read path; identity-not-authority; provenance never
lost; contacts and users one model; directory ≠ private profile):

6. **Merge unifies identity, never authority.** No code path computes a union/max of roles or
   capabilities; `memberships` is per-identity; enforcement stays keyed on `(platform,
   stable_id)` below the LLM. A merge cannot change any enforcement decision. (§3)
7. **Merge is non-destructive and fully reversible.** The only persisted mutation is the
   `person_id` link + the ledger; per-identity field data is never combined on disk (unioned at
   read time). Un-merge restores the original Persons from the ledger's `prior_person_id`. (§2)
8. **Never auto-merge.** Linking two Persons is an operator action only; the bot has no merge
   tool and bot identity-assertion never links two pre-existing Persons. Suggestions are surfaced,
   never applied; a dismissed pair never resurfaces. (§2.4, §4)
9. **Blocked is per-identity.** A merged person is reachable while any identity is unblocked;
   blocked identities are stripped from the bot projection; the person is hidden only when every
   identity is blocked. Merge never blocks/un-blocks. (§2.5)

## 9. Boundary / hand-offs

- **Presentation** (card layout, access badges, the suggestion affordance, `person_id` chip,
  retiring the by-channel sections) → `ui`, against [style-guide.md](style-guide.md). Page-content
  truth stays here. **Co-owned: chips 4, 5, 6, 7.**
- **The authority question (§3)** → `edr`/security ratify Option B and the two residuals before
  chip 3 lands. **Load-bearing — the coordinator routes this first.**
- **Enforcement / admission** (per-identity block/admit/role through the fail-closed path) →
  unchanged; `edr`/security own the gateway enforcement, `users` owns the role/capability model.
- **Bot hot-path projection** (chip 7) → canary-gated, auditor-grade; `edr`/security co-own the
  blocked-identity projection correctness.
- **Behavioral profiles** (`profile_refs[]` for a merged person) → the directory program's Phase 4
  (surface-to-admin); R2 only carries the list, it does not render profile content.
- **Per-user cost/tier** across a merged person's identities → `model-tiers` (cost stays
  per-`(channel, user_id)`; a merged person is a *display* roll-up, not a new cost unit — per
  [feedback: per-bot is the cost unit](spec-users-meta-2026-06-15.md)).
- **R3 principal separation** → still `users` × `edr`; R2 changes nothing about the R3 seam (it
  stays behind `resolve_person`).
