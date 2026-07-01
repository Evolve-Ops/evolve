# RSI dry-run — operator's guide

**Audience**: pod operators tuning their bots' AGENTS.md files
against the Phase 2 RSI substrate.

**Tool**: `python3 -m tools.rsi_dry_run` (lives at
`packages/analyzer/tools/rsi_dry_run.py`).

## What it does

You write a synthetic AGENTS.md and pass a synthetic conversation
pattern. The tool runs every Phase 2 producer + consumer that's
shipped on this main, and prints what would emit:

- Producer **Signals** (capability gaps, amplification opportunities)
  with their full details payload.
- Consumer **Proposals** (per-bot Recommendations cards) with
  headlines, summaries, and action labels.

Nothing is written to the pod's real `{shared_dir}/signals`. The
synthetic store lives in a `tempfile.TemporaryDirectory` and gets
cleaned up.

## When to use it

- **Tuning your `## Out of scope` section.** Does the marker
  shape you wrote get parsed correctly? Does it actually suppress
  the cap-gap proposal you don't want? Dry-run, see, iterate.
- **Designing an AGENTS.md from scratch.** Walk through what
  patterns each phrasing of your bot's purpose would surface.
- **Debugging "why didn't I see X?"** Drop the conversation pattern
  you expected to surface; the tool tells you which gate it failed
  (engagement floor, recurrence, mood, objective alignment).
- **Pre-validating a config change before redeploy.** Test on the
  laptop instead of waiting for the daemon to fire.

## Quickstart

Three shipped sample AGENTS.md fixtures cover the canonical alignment
cases. From `packages/analyzer/`:

```sh
# Confirmed alignment — fitness bot, workout pattern.
python3 -m tools.rsi_dry_run \
  --agents-md tools/samples/fitness-coach.md \
  --pattern workout:tracking:8:8

# Emergent alignment — general assistant, workout pattern.
python3 -m tools.rsi_dry_run \
  --agents-md tools/samples/general-assistant.md \
  --pattern workout:tracking:8:8

# Contradicted alignment — sailing bot with `## Out of scope: fitness`.
python3 -m tools.rsi_dry_run \
  --agents-md tools/samples/sailing-bot-with-exclusions.md \
  --pattern workout:tracking:8:8
```

The third command demonstrates what the anti-domain detection
machinery does. On any main where the anti-domain parser is loadable,
this dry-run reports the exclusion and the cap-gap monitor drops the
candidate. On a main before PR #2182 lands, the parser is absent and
the tool prints "Anti-domain parser not loadable" — exactly the
right signal that the substrate piece isn't shipped yet.

## Pattern syntax

`--pattern noun:verb:n_sessions:n_days[:engagement_each[:mood]]`

- **noun** — what the conversation is about (`workout`, `budget`,
  `journal`, …). Mapped to a `domain:*` tag via the shared keyword
  vocabulary; see `_DOMAIN_KEYWORDS` in `app_suggester/observe.py`.
- **verb** — the conversational intent (`tracking`, `planning`,
  `recording`, …). Must be in `VERB_VOCABULARY` (see
  `schema/observation.py`). The engagement_amplifier monitor clusters
  by (noun, verb); cap-gap clusters by noun alone.
- **n_sessions** — how many distinct sessions show this pattern.
  Must clear the producer's `MIN_DISTINCT_SESSIONS` gate.
- **n_days** — how many distinct days the sessions span.
  Must clear `MIN_DISTINCT_DAYS`.
- **engagement_each** — engagement per session (default 4).
  `n_sessions × engagement_each ≥ MIN_ENGAGEMENT_TOTAL` is required
  to clear the engagement gate.
- **mood** — defaults to `enthusiastic`. Override to `frustrated` to
  show how the amplifier rejects high-friction clusters (the
  persona_tuner path picks those up instead).

Repeat `--pattern` to write multiple clusters:

```sh
python3 -m tools.rsi_dry_run \
  --agents-md tools/samples/general-assistant.md \
  --pattern workout:tracking:8:8 \
  --pattern budget:planning:6:6
```

## Substrate availability report

At startup the tool prints which Phase 2 modules are loadable on the
current main:

```
Phase 2 substrate availability:
  ✓ capability_gap_monitor
  ✓ app_suggester
  — engagement_amplifier_monitor
  — engagement_amplifier
  — pod_capability_lift
  — anti_domains
```

A `—` means the module's PR hasn't merged yet. The tool gracefully
skips that producer/consumer rather than failing. As open PRs land
(#2178, #2179, #2180, #2182), the report fills in and the tool
exercises more of the substrate automatically — no version pinning
or compatibility shims required.

## AGENTS.md conventions cheat sheet

The substrate's three alignment states correspond to three patterns
in AGENTS.md:

### Confirmed alignment
The bot's purpose mentions a domain keyword for the candidate noun.

```markdown
## Purpose
A fitness coach. Helps with workout planning and exercise tracking.
```

A `workout` cluster on this bot → `confirmed`. The cap-gap monitor
fires at the default threshold; the amplifier proposal reads
"stated scope working well, consider deepening."

### Emergent alignment
No domain keyword overlap. Users have organically converged on a
pattern the bot doesn't claim.

```markdown
## Purpose
A general-purpose assistant.
```

A `workout` cluster on this bot → `emergent`. The cap-gap monitor
fires only at the stricter neutral threshold (≥ 5 sessions, ≥ 25
engagement); the amplifier proposal reads "organic cross-bot
convergence, consider whether to embrace."

### Contradicted alignment (anti-domain detection)
The bot's AGENTS.md has an explicit `## Out of scope` section
covering the candidate domain.

```markdown
## Purpose
A sailing assistant.

## Out of scope
- fitness
- finance
```

A `workout` cluster on this bot → `contradicted`. The cap-gap monitor
**drops the candidate entirely** (no proposal); the amplifier
monitor **still emits** a Signal, and the generator reframes the
pitch as "Pattern contradicts stated scope — make a scope decision."

## Accepted exclusion markers

The parser is conservative: only explicit markers count. Header
phrases recognized (case-insensitive):

- `## Out of scope`
- `## Out-of-scope`
- `## Excluded` / `## Exclusions`
- `## Not for this bot`
- `## Don't` / `## Do not`

Inline phrases recognized:

- `Out of scope: X, Y, Z.`
- `Not in scope: X.`
- `Excluded: X, Y.`

Items can be markdown bullets, numbered list items, or comma-
separated lists on a single line under the header. Items separated
by `and` are also split.

The parser explicitly does **not** interpret general negation in
prose ("this bot doesn't handle finance"). That would risk false
positives that silence legitimate proposals. Operators must use the
documented marker shapes.

## Limits

- The dry-run uses a synthetic bot id (default `team-bot-a`) and an
  empty applications directory. Real-pod manifests and per-bot
  application config are NOT consulted.
- `pod_capability_lift` requires the pattern to fire on ≥ 3 bots.
  Single-bot dry-run can't exercise it; the tool notes this and skips.
- Signal-store dedup (re-running the tool emits the same Signal
  again) is bypassed because the tempdir is fresh each run.
- The "now" anchor defaults to 2026-06-05 so the report is
  deterministic. Override with `--now 2026-06-15T12:00:00`.

## See also

- `docs/spec-rsi-proposal-eligibility-2026-06-05.md` — Phase 2 spec
  + per-factory audit + 4-criteria RSI test.
- `packages/analyzer/anti_domains.py` — anti-domain parser
  implementation (after PR #2182 lands).
- `packages/analyzer/tools/samples/*.md` — shipped fixtures.
