---
name: weekly-knowledge-refresh
schedule: "0 9 * * 1"  # Mondays 09:00 local
description: >
  Refresh docs/help/ from the prior week's merged PRs. Reads PR titles +
  files, matches against the concept index, edits affected help files,
  and opens one PR per week.
audience: scheduled-routine
---

# Weekly knowledge-refresh — Evolve help corpus

## What you are doing

You are running on a fresh remote checkout of the Evolve repo, on a Monday
morning. Your job is to keep `docs/help/` honest by reflecting the prior
week's merged PRs in the help corpus, then opening **one PR per week**
named `docs(help): weekly knowledge refresh YYYY-MM-DD` for the operator
to review and merge.

You have no memory of prior runs. Everything you need is in this prompt
and in the repo itself.

## The corpus

`docs/help/` is the canonical public-audience description of Evolve. It
backs both the public help site and the community-facing research bot
(Atlas). Read [docs/help/README.md](docs/help/README.md) first — it
defines the frontmatter contract, the controlled concept vocabulary in
[`_index.yaml`](docs/help/_index.yaml), and the anti-goals. Honor them.

Critically:

- Only `audience: public` files are user-visible. Internal specs,
  diagnoses, audits, and incident reports under `docs/` are NOT in scope.
- Every concept you reference in a `concepts:` block MUST exist in
  `_index.yaml`. Add it to the index first.
- The Plex-test voice. No internal jargon, no file-path namedropping,
  no LaunchDaemon names, no `evolve` vs `pod-admin` distinctions.

## Step 1 — Collect last week's merged PRs

```bash
SINCE=$(date -v-7d +%F 2>/dev/null || date -d "7 days ago" +%F)
gh pr list \
  --state merged \
  --base main \
  --search "merged:>=${SINCE}" \
  --json number,title,body,mergedAt,files \
  --limit 200 > /tmp/weekly-prs.json
```

If `gh` returns zero PRs, post an exit comment ("nothing merged this
week — skipping refresh") and stop. Do NOT open an empty PR.

## Step 2 — Triage each PR

For each PR, decide whether it has user-visible impact.

**Skip** (internal-only) when ALL changed files match these patterns:
- `evolve_admin/**` (except `evolve_admin/templates/**` which is UI copy)
- `tests/**`
- `docs/spec-*.md`, `docs/incident-*.md`, `docs/diagnosis-*.md`,
  `docs/audit-*.md`, `docs/forensic-*.md`, `docs/investigation-*.md`,
  `docs/note-*.md`
- `.github/**`, `.claude/**`
- Any `*.lock`, `*.snap`, `package-lock.json`

**Candidate** (user-visible impact) when the PR touches ANY of:
- `docs/help/**` (corpus itself)
- `docs/gitpages/**`, `docs/getting-started.md`, `docs/overview.md` (public docs)
- Admin UI templates / routes / pages
- A new generator (`packages/analyzer/generators/<id>/charter.yaml`)
- A new app manifest (`docs/atlas-app-manifests/**` or comparable)
- Anything renaming a UI surface, page, tab, button label, or chat command
- Anything changing a default cap, threshold, or behavior the operator
  sees on a page

If unsure: candidate. Better to over-include than miss a real change.

## Step 3 — Match concepts

For each candidate PR:

1. Read its title, body, and the list of changed file paths.
2. Extract tokens (lowercase, strip stopwords).
3. Match against keys in [`docs/help/_index.yaml`](docs/help/_index.yaml).
   For each matching concept, the index lists the files that own it. Add
   those files to the PR's "affected files" set.
4. If a PR matches zero concepts but is a candidate, scan its title +
   body for new product nouns (page names, feature names). Either:
   - Add them as new concepts to `_index.yaml` AND author / edit a help
     file that owns them, or
   - Note them in the routine's final report under "concepts that may
     need vocabulary" — do NOT silently drop them.

## Step 4 — Stale-detector pass

Independently of step 3: for every file in `docs/help/`, parse its
`last_reviewed` date. If it is **>30 days old** AND owns any concept
appearing in this week's PRs, flag it for review even if the per-PR
matching didn't pick it up.

## Step 5 — Edit affected files

For each affected file:

1. Read it in full.
2. Identify the sections that need to reflect each PR's change. Use the
   PR's body as the source of truth for what changed; if the body is
   thin, read the diff (`gh pr diff <number>`).
3. Edit the body to reflect the new state. Keep the voice. Do NOT add
   "as of PR #1234" or any changelog cruft — help files describe current
   state, not history.
4. Bump `last_reviewed` in the frontmatter to today's date.
5. If a PR introduces a new concept owned by this file, add it to the
   file's `concepts:` block AND to `_index.yaml`.

If you find that a PR's user-visible impact contradicts what's already
in the corpus and you can't resolve it from the PR body alone (e.g. the
PR title says "rename X to Y" but the body doesn't say what X is), leave
a `<!-- TODO(weekly-refresh): unresolved — need human input -->` comment
next to the affected paragraph. Do NOT guess.

## Step 6 — Open the PR

Create a branch named `docs/help/weekly-refresh-YYYY-MM-DD`. Commit the
changes with the message:

```
docs(help): weekly knowledge refresh YYYY-MM-DD
```

PR title: same. PR body:

```markdown
## Summary

Weekly refresh of `docs/help/` based on PRs merged between {SINCE} and {TODAY}.

## Changes by source PR

- [#1234](url) — <one-line summary of the source change>
  - Edited: `docs/help/<file>.md` (concept: <concept-name>)
  - Edited: `docs/help/<file>.md` (concept: <concept-name>)

- [#1239](url) — <one-line summary>
  - Edited: `docs/help/<file>.md`
  - Updated `_index.yaml` (added concept: <new-concept>)

## Stale-detector flags

- `docs/help/<file>.md` (last_reviewed: 2026-04-15) — owns "{concept}"
  which appears in {n} PRs this week. Reviewed and {edited / no change
  needed}.

## Unresolved

- {Any TODO markers left in files, with the source PR number}
- {Any candidate concepts that may need to be added to the vocabulary}

## Test plan

- [ ] Render the affected files locally and skim for voice / accuracy
- [ ] Confirm `_index.yaml` still parses as valid YAML
- [ ] Confirm Atlas's research script can still find concepts for a
      sample question ("how do recommendations work?")
```

Open the PR with `gh pr create`. Do NOT auto-merge. Do NOT request
review (the operator decides who reviews).

## Hard constraints

- **One PR per week.** If you can't finish, leave a TODO and open the PR
  anyway — partial is better than absent.
- **Never edit files outside `docs/help/`.** Not the spec files, not the
  PRs you're reading from, not the source code. Corpus-only.
- **Never invent product behavior.** If the PR body is ambiguous and the
  diff doesn't clarify, leave a TODO marker.
- **Never merge.** Operator gate.
- **Do not break the corpus contract.** Every file you edit must still
  have valid frontmatter with all required fields. Every concept used
  must exist in `_index.yaml`.

## When zero PRs are candidates

Acceptable outcomes:
- Post a one-line status: "0 user-visible PRs this week — corpus unchanged."
- Do NOT open a PR.
- Still run the stale-detector pass. If it surfaces files >30 days old
  with no recent activity in their concepts, post a separate one-liner:
  "stale-detector noticed N files >30d unreviewed; no edits made."

## Token budget

Aim for under 200K output tokens. The corpus is ~100KB and a typical week
has 5-15 candidate PRs; if you find yourself reading every file in `docs/`
or the entire diff of a large PR, you have gone off-script.
