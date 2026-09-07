# Atlas — Application Manifest Drafts

**Date:** 2026-05-20
**Status:** Drafts — pressure-test of the v6 manifest spec against four real apps for the Atlas bot (OC-enthusiast Telegram community + market intel feed).

This directory contains four full application manifests written against [`docs/manifest-spec.md`](../manifest-spec.md) v4 / schema v6, plus a [`GAPS.md`](GAPS.md) memo capturing where the spec didn't fit cleanly.

## The four apps

| File | What it does | Trigger |
|---|---|---|
| [`atlas-daily-digest.json`](atlas-daily-digest.json) | Crawls RSS / GitHub / Brave Search daily, classifies into 5 buckets, posts a Team-Bot-A-style digest to the configured Telegram group | Cron — daily at `{digest_time}` |
| [`atlas-article-capture.json`](atlas-article-capture.json) | When a member posts a URL: fetch, summarize, classify, archive, react with bucket emoji. Honors 🤐 per-message opt-out. | Event — incoming group message containing URL |
| [`atlas-on-demand-research.json`](atlas-on-demand-research.json) | When a member @-mentions @atlas: scope-check, rate-limit-check, budget-check, Brave + Haiku synthesis, threaded reply with sources | Event — @-mention in group |
| [`atlas-weekly-recap.json`](atlas-weekly-recap.json) | Sundays: read the past week's archive, rank top items per bucket, run pattern-detection pass, post longer-form recap to group | Cron — weekly Sunday `{recap_time}` |

## Shared substrate across the four

- `scripts/atlas_lib/` — fetchers, classifier, archive, composer (declared as shared dependency)
- `archive/{bucket}/{YYYY-MM-DD}-{slug}.md` — 5-bucket classified archive (writers: digest + capture; reader: recap)
- `archive/index.json` — append-only index across writers
- `atlas/.capture-salt` — hashing salt (shared across capture + research for member-ID hashing)
- `atlas/optout.json` — per-message opt-out registry (consulted by capture and recap)
- 5-bucket taxonomy: `competitive_landscape`, `new_tools`, `use_cases`, `case_studies`, `warnings`

## How to read this

1. Start with [`GAPS.md`](GAPS.md) — the actual point of the exercise is what didn't fit.
2. If you want to see the canonical cron-driven shape, read [`atlas-daily-digest.json`](atlas-daily-digest.json) — closest to `morning-briefing` from the gallery.
3. If you want to see what an event-triggered manifest looks like today (with workarounds), read [`atlas-article-capture.json`](atlas-article-capture.json) — surfaces gaps 1, 3, 5, 7.
4. If you want to see governance primitives baked into an app, read [`atlas-on-demand-research.json`](atlas-on-demand-research.json) — surfaces gap 8.
5. If you want to see app-to-app dependency, read [`atlas-weekly-recap.json`](atlas-weekly-recap.json) — depends on the archive that digest + capture populate.

## What's missing

These manifests describe *what* Atlas does and *how the contracts are shaped*. They do NOT include the full python implementations — the `build_spec` fields contain build specifications, not finished code. To actually install and run Atlas, the build_spec needs to be turned into the rendered files (scripts/atlas_digest.py, etc.) by either:

- A forge provisioner that materializes the `## FILE:` blocks (as morning-briefing and note-taker do)
- Manual implementation by Pod-Admin or an agent

The drafts as they stand are the *contract surface* — enough to install, validate, register, monitor, and improve via RSI, but not enough to actually run until the python is written.
