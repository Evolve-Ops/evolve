# Release Tiers — Tier-aware Upgrade Banner (2026-05-16)

Status: **draft** (design locked in conversation 2026-05-16; build follows immediately).

**What this is.** A small system to classify Evolve releases by significance and route the upgrade banner accordingly. Today the overview banner styles every out-of-sync version the same way — yellow, "update needed", every bot listed — which is a lie when the new release is a docs tweak and a real problem when the new release is a security fix nobody escalates. This spec adds a curated `RELEASES.yaml`, a `latest_release` block on the status payload, and a tier-aware banner that ranges from a small "v… available" chip to a persistent red "Apply now" banner.

**Relationship to other surfaces.**
- The banner is part of the Overview tab, not the Alerts page. Update prompts are a property of the pod's *version state*, not the Signal store. (See [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) for what *does* belong on Alerts — monitor output, not version drift.)
- Version generation is unchanged: `YYYY.MMDD.PR` derived from the latest commit at [deploy.py:89](../packages/admin/evolve_admin/deploy.py:89). This spec adds *metadata about* releases, not a different version scheme.

---

## 1. The problem with the current frame

Today, the overview banner at [index.html:10901](../packages/admin/evolve_admin/web/index.html:10901) does three things wrong:

1. **No severity signal.** Every out-of-sync state renders identically — yellow background, "update needed" copy, "↑ Upgrade Evolve" button. A docs typo correction reads the same as a credential-leak fix.
2. **Bot enumeration with no aggregation.** "team-bot-a, team-bot-c, personal-bot, admin-bot, security-bot, team-bot-b" is six of seven bots. The list is mostly noise; the shape ("almost everything is behind") is the actionable information.
3. **"Needed" wording is dishonest most of the time.** Updates are almost always *available*, not *needed*. We earn the word "needed" with a security or correctness reason; otherwise the banner cries wolf and the admin learns to ignore it.

The fix is two-part: (a) classify each release at merge time, (b) make the banner switch on the highest-classified release between deployed and current.

## 2. Tier model

Three tiers, with a clear default:

| Tier | When it applies | UI rendering | Copy verb | Dismissibility |
|------|-----------------|--------------|-----------|----------------|
| `security` | Vulnerability fix, auth/sandbox bypass, credential exposure, hook-bypass repair, correctness bug that loses data | Red banner, top of Overview, persistent | "Apply now" | Not dismissible — clears only on apply |
| `feature` | New user-facing capability, new integration, new alert class, meaningful UX upgrade | Amber banner, Overview only | "New in v…" | Snooze 7 days |
| `maintenance` | Bug fixes that don't lose data, refactors, docs, telemetry, deps, perf tweaks | Small gray chip near the version label, no banner | "v… available" | Snooze 30 days |

`maintenance` is the default for any release that doesn't claim a higher tier. This is the right failure mode: silence on unannotated releases, not screaming.

**Tier resolution across multiple releases.** When deployed lags current by N releases, take the **max tier** across that range. One security release in the window upgrades the whole banner to red.

## 3. Where tier metadata lives

A single curated file at repo root: **`RELEASES.yaml`**.

```yaml
# RELEASES.yaml — one entry per non-trivial release, newest first.
# Default tier is "maintenance" for any version not listed here.

- version: "2026.0516.0"
  tier: feature
  headline: "Tier-aware update banner"
  details: |
    Updates are now classified as security, feature, or maintenance,
    and the Overview banner styles accordingly.
  link: docs/spec-release-tiers-2026-05-16.md

- version: "2026.0515.1173"
  tier: maintenance
  headline: "Forge UI cleanup"
```

**Schema.**

| Field | Required | Type | Notes |
|-------|----------|------|-------|
| `version` | yes | string | Must match the `YYYY.MMDD.PR` produced by `_compute_version()`. |
| `tier` | yes | `security \| feature \| maintenance` | |
| `headline` | yes if tier != `maintenance` | string | One short sentence in the user's voice. Passes the Plex test. |
| `details` | no | string (multiline) | 1–3 sentences. Shown on the banner's expand-on-click. |
| `link` | no | string (repo-relative path or URL) | Release notes, spec doc, or upstream advisory. |

The file is hand-curated. We considered (and rejected) deriving tier from PR-title prefixes for two reasons: (a) PR titles are sloppy and don't carry headline copy a non-engineer can read, (b) tier classification is sometimes retroactive — a security finding can land a week after the bug shipped — and a curated file gives us a place to backfill.

**Default tier.** If a version is not present in `RELEASES.yaml`, it is `maintenance` with `headline=None`. The banner renders as the gray chip.

## 4. CI guard

A pre-merge check warns when a PR titled `feat:` or `security:` lands without a corresponding `RELEASES.yaml` entry. Mechanism:

- `scripts/check-release-notes.py` — given a PR title and the current `HEAD`, returns nonzero if the PR title starts with `feat:` / `security:` and the most recent `RELEASES.yaml` entry doesn't reference a version newer than the previous main.
- Surfaces as a GitHub Actions warning (non-blocking initially; we'll make it blocking after a stabilization window).

The guard is a nudge, not a wall. Direct pushes to main (no PR) still ship; they default to `maintenance`. Auto-merge culture (per memory) means this needs to be friction-light.

## 5. Status payload

`/api/status` gains a `latest_release` block:

```jsonc
{
  // …existing fields…
  "evolve_current_version": "2026.0516.0",
  "any_bots_out_of_sync": true,
  "latest_release": {
    "tier": "feature",           // max tier across deployed→current
    "version": "2026.0516.0",    // version that contributed the max tier
    "headline": "Tier-aware update banner",
    "details": "…",
    "link": "docs/spec-release-tiers-2026-05-16.md",
    "range": {
      "from_version": "2026.0514.1170",
      "to_version": "2026.0516.0",
      "count": 4               // # of releases in the gap
    }
  }
}
```

`latest_release` is **null** when no bots are out of sync. The computation, performed server-side in [web/server.py:695](../packages/admin/evolve_admin/web/server.py:695):

1. Determine `min_deployed_version` across all member bots (skip primary).
2. Load `RELEASES.yaml`. Select entries with `version > min_deployed_version`.
3. Take the entry with the highest `tier` (security > feature > maintenance). Tie-break by newest version.
4. Return the resolved entry plus the range summary.

If `RELEASES.yaml` is missing or unparseable, fall back to `tier=maintenance, headline=None` — never crash the status endpoint over a malformed releases file.

## 6. UI

The element at [index.html:1220](../packages/admin/evolve_admin/web/index.html:1220) becomes a three-way switch driven by `latest_release.tier`:

**security** (red):
```
🛡  Security update — apply now
    Fixes a credential-exfiltration path in the Slack hook.        [Details]  [Apply]
    6 of 7 bots affected.
```

**feature** (amber):
```
↑  New in v2026.0516.0 — Tier-aware update banner
   Updates are now classified by significance.            [What's new]  [Update]
   6 of 7 bots                                                    [Snooze 7d ▾]
```

**maintenance** (chip, not banner):
```
                                                v2026.0516.0 available  ↑
```
(rendered as a small inline chip next to the existing pod-section header; clicking opens the upgrade modal.)

**Aggregation.** Bot lists collapse: "6 of 7 bots one version behind" with a hover/click to expand the full list. Avoids the horizontal-list noise of today.

**Snooze.** Per-tier snooze stored in `localStorage` under `evolve.releaseSnooze.{tier}` as an ISO expiry timestamp. Snooze is UX-state, not pod state — it lives per admin per browser. `security` cannot snooze.

## 7. What needs to change

| File | Change |
|------|--------|
| `RELEASES.yaml` (new) | Seed with current version + a few historical entries. |
| `packages/admin/evolve_admin/release_notes.py` (new) | Loader: parse `RELEASES.yaml`, resolve `latest_release` for a (min_deployed, current) range. |
| `packages/admin/evolve_admin/web/server.py:695` | Compute `latest_release` and add to status payload. |
| `packages/admin/evolve_admin/web/index.html:1220, 10901` | Refactor banner element + render to a three-tier switch. Add aggregation + snooze. |
| `scripts/check-release-notes.py` (new) | CI guard for `feat:`/`security:` PRs. |
| `packages/admin/tests/test_release_notes.py` (new) | Loader, tier resolution, snooze gating. |

## 8. What's deferred

- **Per-bot tier impact.** "The security fix only affects Slack-using bots" is real signal but needs a separate field (`applies_to: [slack-using]` or similar) and a way to map that to live config. v1 treats the banner as pod-wide.
- **Auto-apply on `security`.** Tempting (the whole point is urgency); deferred because upgrade has side-effects worth keeping a human in the loop on. Surface as a follow-up.
- **In-product release-notes page.** `details` + `link` get us most of the way there. A `/releases` route that lists all entries can come later.
- **CI guard as a hard block.** Starts as a warning. We'll flip to blocking once classification is habitual.

## 9. Implementation phase order

1. `release_notes.py` loader + tests.
2. `RELEASES.yaml` seeded.
3. Status payload extension.
4. Banner UI refactor.
5. CI guard script (non-blocking).
