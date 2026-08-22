# docs/help/ — the Evolve knowledge corpus

This folder is the **canonical, public-audience description of Evolve**. Every file
here is written for an outside reader — the Plex-test voice, no internal jargon.
Two surfaces consume it:

1. **Atlas** loads this corpus as a bundled OpenClaw skill (`evolve-knowledge`)
   and is constrained to answer Evolve questions only from retrieved content.
2. **The public help site** (planned `help.evolve.ai`) is generated from these
   files by a static-site generator.

Anything internal — specs, diagnoses, audits, incident reports, design notes —
lives elsewhere under `docs/` and **does not** belong here.

---

## File layout

Flat. One file per admin UI page, plus a handful of orientation files
(`overview.md`, `getting-started.md`, `quick-start.md`) and per-system
explainers (`profile-inferrer.md`, `continuity.md`).

If the corpus grows past "one file per surface," fold in
`concepts/`, `how-to/`, `reference/` subfolders. Today it doesn't need them.

---

## Frontmatter contract

Every `.md` file in this folder begins with YAML frontmatter:

```yaml
---
title: "Help: Apps Page"
slug: apps
audience: public          # public | internal — `internal` is hidden from Atlas + the site
last_reviewed: 2026-06-05  # YYYY-MM-DD; touched by humans and by the weekly updater
status: active             # active (default) | deprecated
concepts:                  # controlled vocabulary — see _index.yaml
  - applications
  - forge
ui_surface: admin.apps     # which admin UI page this documents, or null
related_specs:             # internal specs that ground this doc; not user-visible
  - docs/spec-app-derived-permissions-2026-05-24.md
---
```

Field rules:

| Field           | Required | Notes |
|-----------------|----------|-------|
| `title`         | yes      | Mirrors the H1 below. |
| `slug`          | yes      | Stable URL slug. Matches the filename. |
| `audience`      | yes      | Only `public` files are served externally or to Atlas. |
| `last_reviewed` | yes      | Bump whenever a human or the updater verifies the doc against current code. |
| `status`        | no       | Default `active`. Set `deprecated` for redirect docs. |
| `concepts`      | yes      | Every term must appear in `_index.yaml`. Add to the index first. |
| `ui_surface`    | yes      | `admin.<page>` if this documents an admin UI surface; else `null`. The `<page>` must be a live admin page id — see the registry below; the coverage lint blocks a stale one. |
| `related_specs` | yes      | List or `[]`. Internal specs only — never quoted to users. |

---

## Page registry (`_pages.yaml`) and the coverage lint

The admin UI describes each page in several places that historically drifted
apart — the live sidebar (`data-page="…"` in
`packages/admin/evolve_admin/web/index.html`), the `ui_surface:` frontmatter
here, the Evo tray's per-page context, and
`packages/analyzer/evolve_bot/AGENTS.md` — because nothing tied them together.

**[`_pages.yaml`](_pages.yaml)** is the canonical registry that does: one row per
live admin sidebar page mapping `page_id ↔ label ↔ bucket ↔ help_slug ↔ purpose`.
It is the single source of truth for "which help doc documents which page."

We kept this in a **separate `_pages.yaml`** rather than folding a `pages:` block
into [`_index.yaml`](_index.yaml): `_index.yaml` is the *concept → files*
vocabulary (consumed by the weekly refresh), while `_pages.yaml` is the
*page → doc* map (consumed by the coverage lint). Different consumers, different
churn — keeping them apart avoids overloading either. Neither YAML is picked up
by the help-index or help-site builders (both glob `*.md` only), so adding the
registry is inert to those pipelines.

Each row's `help_slug` is the doc that documents the page (or `null`), and
`doc_status` is one of:

- `present` — `docs/help/<help_slug>.md` must exist (a missing one is **blocked**
  by the lint: it means a doc was deleted/renamed out from under the registry).
- `planned` — `help_slug` names the intended slug for a doc that doesn't exist
  yet; the lint **warns** (actionable backlog), never blocks.
- `none` — `help_slug` is `null` on purpose (the page has no public doc).

**[`tools/help-coverage-lint`](../../tools/help-coverage-lint)** validates the
live UI, this registry, and the corpus against each other. It **blocks** on: a
`data-page` in index.html with no registry row (an unregistered new page), a
registry row whose `page_id` is no longer live, a `present` page with a missing
doc, or a `ui_surface: admin.<x>` that resolves to no live page. It **warns** on
known doc gaps, null-slug pages, and pages not yet mentioned in AGENTS.md (a
warn-only dimension for now — flip CI to `--strict` once AGENTS.md coverage
lands). Run it any time:

```
python3 tools/help-coverage-lint          # exit 1 on any block
```

It runs in CI as the `help-coverage` job. **When you add or rename an admin
page, add/edit its row in `_pages.yaml` in the same PR** — the lint will block a
new `data-page` that has no row.

---

## Controlled concept vocabulary

`_index.yaml` is the controlled list of concepts. The weekly updater (below) keys
off it.

- To introduce a new concept: add it to `_index.yaml` first, then to a file's
  `concepts:` block.
- A concept may be owned by multiple files; the **primary** owner is listed
  first in `_index.yaml`.
- Renaming a concept is a vocabulary change — update `_index.yaml` and every
  file that uses it in the same commit.

---

## Weekly knowledge-refresh routine

Implemented as **[`tools/help-refresh`](../../tools/help-refresh)**, run weekly
(Mondays) by **[`.github/workflows/help-refresh.yml`](../../.github/workflows/help-refresh.yml)**.
It is meta/dev tooling — deterministic, cheap, no LLM call, and it never ships to
pods. Each run does the following:

1. **Collect** the prior week's merged PRs to `main`:
   ```
   gh pr list --state merged --base main \
     --search "merged:>=$(date -v-7d +%F)" --json number,title,body,files
   ```
   The cutoff comes from `--since YYYY-MM-DD` (default: 7 days before today).
2. **Triage** each PR. Internal-only PRs (touching only
   `evolve_admin/`, `tests/`, `docs/spec-*`, `docs/incident-*`, `docs/diagnosis-*`,
   `docs/audit-*`) are skipped. Candidate PRs touch user-visible UI, bot/app
   capabilities, integrations, `docs/help/`, or the public site source.
3. **Match concepts.** For each candidate PR, the concept terms in
   [`_index.yaml`](_index.yaml) are matched against the PR's **title and changed
   file paths** (the high-signal fields); the free-prose **body** is matched only
   for *specific* concepts — multi-word/hyphenated ones like `model-tiers` or
   `app-audit` — because bare single tokens (`cost`, `apps`, `install`) flood
   from incidental body mentions. Each matched concept resolves to its owning
   help files via `_index.yaml`.
4. **Stale-detect.** A file whose `last_reviewed` is >30 days before the run date
   **AND** that owns a concept appearing in this week's candidate PRs is flagged
   for review — even if no substantive change turns out to be needed.
5. **Produce a human-gated review PR.** The tool (a) bumps `last_reviewed` to the
   run date on the stale-flagged docs, and (b) writes
   a generated `docs/help/REVIEW-QUEUE.md` listing, per affected doc, the
   merged PRs that touched its concepts — so a human knows exactly what to verify.
   The workflow opens **one PR per week** titled
   `docs(help): weekly knowledge refresh YYYY-MM-DD`. It does **not** rewrite
   prose: drafting accurate user-facing copy is a human judgment call (see the
   future-work note below). A week with zero candidate PRs is a clean no-op — no
   empty PR is opened.
6. **Human gate.** The operator reviews `REVIEW-QUEUE.md`, verifies each doc
   against its linked PRs, edits the prose where it drifted, and merges — same
   gate as code. **No auto-merge.** The bumped `last_reviewed` only "counts" once
   that human verification has actually happened.

The routine is **public-content discipline**: if a PR's user-visible behavior
isn't reflected here within a week, the corpus is wrong and Atlas will
confidently say wrong things. Treat the weekly PR with the same seriousness as
a code review.

**Future work (v2):** an LLM pass could draft the proposed prose edits per doc
(grounded in the linked PRs' diffs) instead of leaving the whole edit to the
human — still landing in the same human-gated PR. v1 deliberately stops at
"surface what to verify" to keep the routine deterministic and free of model
cost.

---

## Adding a new help file

1. Pick a `slug` matching the filename (`apps.md` → `slug: apps`).
2. Write the body in Plex-test voice — assume a reader who installs Plex and
   runs Home Assistant but has never used OpenClaw.
3. Add every term it authoritatively covers to `_index.yaml` under `concepts:`
   with this file listed first.
4. Frontmatter, then H1, then body.

If you're not sure whether a topic is public-audience material, ask
[overview.md](overview.md) — if it doesn't show up there as part of the
elevator pitch, it probably isn't (yet).

---

## Anti-goals

- **No internals.** No file paths under `evolve_admin/`, no LaunchDaemon names,
  no references to `pod-admin` or the `evolve` user. If you find yourself
  writing about ACLs, you're in the wrong folder.
- **No spec excerpts.** Specs change; help files describe what the user sees
  and does. Reference specs in `related_specs:` for the updater's benefit, but
  don't quote them.
- **No feature-by-feature changelogs.** The weekly PR is the changelog. Help
  files describe current state, not history.
