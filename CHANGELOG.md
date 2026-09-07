# Changelog

**This file is retired (2026-08-15).** Evolve ships continuously — every PR
merged to `main` is a release, stamped with a CalVer version of the form
`YYYY.MMDD.PR` (date + PR number). There is no curated per-version changelog;
maintaining one by hand drifted from reality within weeks, and a stale
changelog is worse than none.

Where to look instead:

- **Per-change history** — `git log --first-parent main`: each merge subject
  carries the PR title and number, one PR per release.
- **What your pod runs** — `evolve-admin status` shows the pod and per-bot
  versions; on canary pods, `sudo evolve-admin release status` shows the
  stable pointer, candidate, and soak state. See
  [Keeping Evolve Up to Date](docs/help/updating.md).
- **Historical entries** — the semver-era entries (through v0.3.0, April
  2026) are preserved in this file's git history.
