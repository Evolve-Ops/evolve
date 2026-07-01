# Spec: Gallery manifest tags — 2026-05-31

**Status:** Partially shipped.
* PR [#1873](https://github.com/evolve-ops/evolve/pull/1873) (merged) landed
  the flat-string vocabulary, backfill, gallery index/tags-index plumbing,
  and two latent-bug fixes (`manifest.from_dict` alias + `spec_routes` typo).
* This PR (follow-up) adds the `disabled_tags` manifest field and the
  forward-looking design notes for LLM-assisted tag proposal at manifest
  creation and at forge time.

**Relation to schema v7:** Additive to
[docs/spec-manifest-v7-2026-05-20.md](spec-manifest-v7-2026-05-20.md).
v7's App Spec already lists `tags`; the gallery-layer field name is
`application_tags` for the same reason `application_tags` lives on the
SpecDraft — it's the on-disk surface and predates v7. The
`from_dict` alias landed in [#1873](https://github.com/evolve-ops/evolve/pull/1873)
keeps both names interchangeable until v7 migration unifies them.

## Why

The gallery sits at 12 apps and is about to grow as personal-assistant
packs (travel concierge, household ops, etc.) land. Past ~15 apps the
operator can no longer scan the gallery visually, and there's no
machinery for marking apps that ship as a coordinated suite (e.g. the
4 apps in a "travel pack"). Tags are the cheap fix.

## Manifest fields

```json
{
  "application_tags": [
    "personal-productivity",
    "travel",
    "travel_assistant_pack",
    "provenance:acme_corp"
  ],
  "disabled_tags": ["journaling"]
}
```

* **`application_tags: list[str]`** — flat list of strings. **No
  namespace convention is enforced.** The vocabulary at
  [packages/admin/evolve_admin/applications/tag_vocabulary.py](../packages/admin/evolve_admin/applications/tag_vocabulary.py)
  is a *recommendation* layer the auto-detector matches against; operators
  can put anything in the list, including suite labels
  (`travel_assistant_pack`), provenance markers, vendor names, or made-up
  shorthands. The recommendation layer just keeps the auto-detector from
  fragmenting into `task-mgmt` / `task-management` / `tasks` variants.
* **`disabled_tags: list[str]`** — *new in this PR*. Operator's persistent
  dismissal list. Any tag here is excluded from the merged
  `application_tags` on every backfill, both from auto-detector output
  *and* from any pre-existing entry in `application_tags` itself. Empty /
  missing → no filter; behaves exactly like the pre-PR contract.

Both fields are top-level on each gallery manifest
(`gallery/<app>/p-*.json`). The backfill script reads `disabled_tags`
on every run and feeds it through `auto_detect(..., disabled_tags=…)`
and `merge_tags(..., disabled_tags=…)`.

## Why flat strings (no `category:` / `suite:` namespaces)

We considered a namespaced model (`category:travel` /
`suite:travel_assistant_pack`) during design. Chose flat strings for
v1:

* **Operator authoring stays trivial.** A flat list is what a human
  types into a JSON file. Prefixes add ceremony for no immediate gain;
  the UI can still group canonical vs free-form tags by checking
  `is_recommended()`.
* **No premature taxonomy.** Suites are a single-purpose grouping
  today (3 suites in the gallery). If category/suite/role/etc. proves
  load-bearing later, we can introduce prefixes additively without
  breaking the existing data.
* **Vocabulary is a recommendation, not an enum.** Prefixed
  namespaces invite enforcement; flat strings keep the operator-as-author
  model intact.

The namespace decision is deferred, not closed. Revisit if (a) suite
grouping becomes a primary filter dimension, or (b) more than ~3 tag
classes emerge that genuinely need distinct UI treatment.

## Scanner / merger contract

Implementation: [packages/admin/evolve_admin/applications/tag_vocabulary.py](../packages/admin/evolve_admin/applications/tag_vocabulary.py).

`auto_detect(*text_blobs, disabled_tags=()) -> list[str]`
1. Concatenate the text blobs (display_name + description + existing tags,
   typically — backfill skips build_spec to avoid false positives).
2. Word-boundary regex match against `RECOMMENDED_TAGS` keywords.
3. Drop any hit listed in `disabled_tags`.
4. Return sorted, deduplicated.

`merge_tags(existing, proposed, *, disabled_tags=()) -> list[str]`
1. Preserve `existing` in order (operator-curated entries stay in their
   chosen order).
2. Append `proposed` in alphabetical order, skipping anything already
   present.
3. Drop anything in `disabled_tags` from the result, including from
   `existing` — operator's dismissal is authoritative even against
   pre-existing manual entries.

Pure functions. The backfill script
[scripts/backfill_application_tags.py](../scripts/backfill_application_tags.py)
is the only on-disk writer of `application_tags`; it's idempotent and
safe to re-run.

## LLM-assisted tag generation (planned)

Two integration points where an LLM proposes tags and the operator
confirms before the manifest is written. Both are follow-up work
(separate PRs); the substrate is ready — operator-confirmed proposals
land in `application_tags`, dismissed ones in `disabled_tags`, and
the backfill respects both on every subsequent run.

### 1. App-creation wizard (manifest authoring)

When the wizard finalizes a new manifest, the LLM is given the
manifest text and the current vocabulary plus the operator's already-
declared `disabled_tags` and proposes:

* **Categories the keyword scanner would miss** — e.g. a "vacation
  planner" manifest does not currently match `travel` via the
  `vacation` keyword in isolation; the LLM suggests `travel`, operator
  confirms.
* **Suite membership** — if the new manifest is being added alongside
  related apps, the LLM proposes a suite tag based on the descriptions
  already in `tags-index.json`. Suite tags are operator-curated free-
  form strings (e.g. `travel_assistant_pack`); the LLM proposal is
  just discovery.

Confirmed proposals land in `application_tags`. Operator-dismissed
proposals land in `disabled_tags` — the next backfill won't bring
them back.

### 2. Forge build (manifest scaffolding)

When the forge generates an app from a SpecDraft, the LLM has already
shaped the description / objective / blueprint text. Same two
classes proposed (canonical category + suite); same operator-confirm
flow. Output lands directly in the scaffolded manifest's
`application_tags`.

### Why both happen at write time, not on every scan

The keyword scanner runs frequently (every backfill, every CI check).
The LLM proposer runs **once per manifest**, at the moment the
operator is paying attention. This keeps cost bounded and avoids
the non-determinism of "the same manifest gets different tags on
different rescans."

## Out of scope (separate PRs)

* Admin-UI filter bar on the gallery page (filter by tag / suite).
* "Show all apps in this suite" deep link.
* Wizard tag-confirm UI surface (operator accept/dismiss for both
  keyword and LLM proposals before the manifest writes).
* Forge tag proposal at build time.
* Visual distinction in the UI for canonical / operator free-form /
  suite tags (uses `is_recommended()` from the vocabulary module).
* `/api/gallery/tags` filter parameters (the reverse-map endpoint
  shipped in [#1873](https://github.com/evolve-ops/evolve/pull/1873);
  filter-by-tag query support is follow-up).
