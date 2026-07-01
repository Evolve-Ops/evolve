# META:public — coordinator charter + bifurcation design (2026-06-18)

**Status:** Carved 2026-06-18 via `/design`; the bifurcation contract is
**designed** (§4 — PD-3/PD-4/PD-5 resolved same day). Remaining work is the
build (the publish manifest + script + CI guard) and the sanitization/scrub
passes. **Cutover execution is operator-gated — not for execution yet.**

**Mission.** Take Evolve public at `evolve-ops/evolve` under BSL 1.1 and keep it
public-safe forever. This is a **coordinating aspect**: it owns the *contract*
(the public/private boundary + the scrub invariant) and the *sequence* (the
cutover runbook + post-cutover triage), and routes *implementation* to the owning
subsystems.

---

## 1. Why this is an aspect (carve rationale)

This work predated the META aspect system — it lived as
`project_public_launch_cleanup` + [`docs/runbook-public-cutover.md`](runbook-public-cutover.md).
Re-activating it surfaced that it has its own spec corpus, its own backlog, and
its own invariants, and that it **spans ≥4 aspects with no single owner**:

- `deploy` — the repo-puller, the mini's deploy checkout, the `origin` remote URL
  (the riskiest cutover phase is the mini resync).
- `substrate` — the META coordinator system itself (`.claude/skills/meta/*`,
  `docs/meta-*-procedure.md`, `meta-state/` ledgers) is repo-tracked and would
  ship public unless explicitly handled.
- `edr` / `diligence` — the secret-scan + scrub-guard gates.
- every **doc-owning aspect** — each doc is a public-or-private decision.

Same carve trigger as `footprint`. Default would be to fit into an existing
aspect; nothing cleanly contains "the public/private posture of the whole repo,"
so it carves.

## 2. Locked decisions

| # | Decision | Outcome |
|---|---|---|
| **PD-1** | Carve | New aspect `public`, coordinating. (2026-06-18) |
| **PD-2** | **Repo model** | **Fresh public repo** at `evolve-ops/evolve` with clean/curated history — **NOT** the runbook's current transfer + `git filter-repo` rewrite. See §3. (2026-06-18) |
| **PD-3** | **Cut line + dev-apparatus** | Public-by-default minus a denylist (§4.1). The **dev apparatus** (META system, EDR, dev-guidance, internal-process docs) is **published *sanitized*** — it's a differentiator, not hidden. (2026-06-18) |
| **PD-4** | **Forward-sync** | A `tools/publish-public` script, **per-release squashed publish**, one-directional private→public, scrub-gated on the output (§4.2). (2026-06-18) |
| **PD-5** | **Dev origin** | **Keep `cjalden/evolve` private** as the dev origin (zero migration; the mini repo-puller already points here). `evolve-ops/evolve` is born fresh as the publish output. (2026-06-18) |
| LICENSE | License | **BSL 1.1**, non-commercial Additional Use Grant, 4-yr Apache 2.0 change date. Done pre-carve in PR #1840. |
| ORG | Org + domain | `evolve-ops` GitHub org + `evolveops.dev` domain both acquired 2026-05-31; GitHub Pages wired to `evolveops.dev`. |

## 3. The repo model — fresh, not rewritten (PD-2)

The runbook's standing plan is **transfer `cjalden/evolve` → `evolve-ops/evolve`
and `git filter-repo` the full history in place** to scrub PII from every commit.
PD-2 reverses this in favor of a **fresh public repo**:

| | Transfer + history rewrite (old) | **Fresh public repo (chosen)** |
|---|---|---|
| Secret-leak surface | filter-repo must scrub **every historical SHA** perfectly; one miss = a permanent public leak | **zero** — public history starts clean from a curated snapshot |
| Carries over | issues, PRs, stars, GitHub redirects | nothing (low cost — solo repo) |
| Bifurcation shape | one repo, history rewritten | **two repos** (see §4) |
| Ongoing cost | one-time | a **forward-sync discipline** (§4.2) |

The decisive factor is the leak surface: rewriting 1yr+ of history is error-prone,
and the failure mode is irreversible (a missed secret is public forever). A fresh
repo eliminates the entire historical-leak risk class. The price — a durable
private→public sync discipline — is exactly what makes this a standing aspect
rather than a checklist that closes.

**Consequence:** the runbook's Phase 4 (filter-repo) and Phase 6 (transfer) are
superseded and must be re-planned for the fresh-repo path (backlog `PUBLIC-4`).
Everything else in the runbook (the scrub gates, the mini resync mechanics, the
smoke tests, the rollback floors) carries over.

## 4. The bifurcation contract (PUBLIC-1 — RESOLVED 2026-06-18)

The contract is **public-by-default minus an explicit private denylist**, with a
one-directional `private→public` publish mechanism. Resolved below; the build
(manifest + publish script + CI guard) is the next implementation slice.

### 4.1 The cut line (PD-3)

Of the ~3,286 tracked files, ~2,622 are `packages/` (the product) and another
~200+ are root configs / CI / gallery / artwork / tests — overwhelmingly public.
The judgment lives almost entirely in `docs/` + the dev apparatus, so the cut line
is expressed as **public by default, minus a denylist** (not a per-file
allowlist).

**The denylist (private — stays only in the dev repo):**

| Class | Paths |
|---|---|
| Already private | `docs/private/` (gitignored), `issues/open/` (gitignored) |
| Internal tracker | `issues/` |
| Strategy | `docs/market-intelligence/` |
| Reference-deployment ops | `docs/incidents/`, `docs/forensic-*`, `docs/diagnosis-*`, `docs/diag-*` |
| Operator-local (already off-repo) | `meta-state/` ledgers, per-aspect memory, `.claude/settings.local.json` |

**Everything not on the denylist is public** — the product (`packages/`,
`gallery/`, `config/network.example.json`, `scripts/`, `tests/`), root community
files, CI (`.github/`, `.githooks/`), `artwork/`, and the doc corpus
(`docs/gitpages/`, `docs/help/`, `docs/skills/`, `docs/schemas/`,
`docs/reference/`, `docs/research/`, `docs/design/`, the bulk of `docs/spec-*` /
`docs/principle-*` / `docs/roadmap-*`, `docs/style-guide.md`,
`docs/threat-model.md`, `docs/overview.md`).

**Dev apparatus = published *sanitized* (PD-3), not hidden.** The META coordinator
system, EDR, the dev-guidance files (`CLAUDE.md`, `AGENTS.md`, `ONBOARDING.md`),
and internal-process docs (`docs/dispatch/`, `docs/routines/`, `docs/system/`,
`.claude/skills/*`, `.claude/hooks/`, `docs/meta-*`, `tools/meta-*`, `edr/`) ship
to the public repo **after a sanitization pass** that strips reference-deployment
detail (the admin-account / host / real-bot-name references the scrub guard
currently *exempts* in `CLAUDE.md` + `.claude/settings.json`). Rationale: "Evolve is built
by this agentic SDLC" is a differentiator worth showing. The live `meta-state/`
ledgers + per-aspect memory remain private (they are already operator-local, not
in the repo at all). The *sanitization* of the META-system files is delegated to
`substrate` (PUBLIC-2, §5).

**Enforcement:** the denylist lives in **one machine-readable source of truth** —
`docs/public-manifest.yaml` (or `.publicignore`) — consumed by *both* the publish
script (§4.2) and a CI guard. A human-judgment-per-file cut line decays exactly
like the 2026-06-07 URL sweep did (§6); the manifest does not.

### 4.2 The forward-sync model (PD-4)

- **Mechanism:** `tools/publish-public` computes `public tree = all tracked
  files − denylist`, runs the **scrub gate on that output tree**
  (`test_public_launch_scrub` + `gitleaks` + the URL sweep), and only on a clean
  result pushes to `evolve-ops/evolve`. The very first run **is the cutover**.
- **Cadence + unit:** **per public release** (tag-driven), published as a
  **curated/squashed commit**. Clean public history, a natural scrub checkpoint,
  low maintenance.
- **Direction is strictly private→public** for the automated path. External PRs
  land on the public repo and are integrated **manually, maintainer-side, into the
  private dev repo** (gated by DCO/CLA — PUBLIC-7), then re-published forward.
  There is no automated back-sync.
- **Re-leak defense is two-layered:** the denylist catches private *paths*; the
  output-scrub catches private *tokens* that drift into a public path. The scrub
  runs on the **publish output**, not just private-repo PRs.

**Safety win of the fresh-repo model:** the private dev repo *never needs
scrubbing* — it may hold anything. Only the publish output is gated. This is why
PD-2 (fresh, not history-rewrite) is decisive — there is no historical-SHA leak
surface to scrub-perfectly-or-leak-forever.

### 4.3 Dev origin (PD-5)

**`cjalden/evolve` stays private as the dev origin.** Zero migration; the mini's
repo-puller already targets it; `evolve-ops/evolve` is born fresh as the publish
*output*. (Optional later rename for org hygiene is a one-time `deploy` task — the
puller's remote URL — not a blocker.)

## 5. META-system disposition (PUBLIC-2 — decided; sanitization → substrate)

PD-3 resolved the yes/no: the META methodology (`/meta` `/status` `/close` skills,
the ledger schema, the reconcile/coherence procedures, `docs/META-session-guide.md`,
`docs/using-the-meta-system.md`, `tools/meta-*`) **is published, sanitized.** It's
a reusable agentic-SDLC methodology and a differentiator.

What stays private is the *live state*, which is already off-repo: the
`meta-state/` ledgers + per-aspect memory (operator-local under
`~/.claude/projects/.../memory/`) — never in the repo.

The open work routed to **`substrate`** (which owns the coordinator system) is the
**sanitization execution**: strip reference-deployment detail from the skill
bodies + procedure docs (operator-specific paths, the admin-account / host /
real-bot-name references), and confirm nothing in the shipped skills hard-codes
the reference deployment. `public` owns only that the boundary is drawn and the scrub passes on
the output; `substrate` does the sanitizing.

## 6. The scrub invariant (standing, route hardening → edr/diligence)

The public-safety floor: **no PII, no secrets, no reserved-deployment tokens in
anything that ships public.** Enforced by:

- `packages/admin/tests/test_public_launch_scrub.py` — the reserved-token
  invariant (CI, runs per-PR).
- [`docs/PLACEHOLDER_NAMING.md`](PLACEHOLDER_NAMING.md) — the role-placeholder
  mapping the guard references.
- the runbook's pre-cutover gate battery: `gitleaks`, `trufflehog`, the
  `cjalden/evolve` URL sweep, author-metadata (`.mailmap`) checks.

**Known decay (PUBLIC-3):** the URL sweep marked "completed 2026-06-07" has
regressed — `cjalden` is back in ~40 tracked files (specs, incidents,
`name_resolver.py`, test fixtures, the meta-procedure docs). This is the
runbook's "living-document caveat" coming true: **every gate re-runs against the
final pre-cutover commit; "passed once" is not "passes now."** Under the
fresh-repo model the scrub also has to run on the **publish output** (§4.2), which
is a stronger guarantee than per-PR scanning of the private repo.

## 7. Post-cutover (deferred)

- **PUBLIC-6 — public-issue triage**: [`spec-public-issue-triage-2026-06-04.md`](spec-public-issue-triage-2026-06-04.md).
  Dogfood the Inbox/proposals pipeline for inbound GitHub issues. Build after
  ~4 weeks of real inbound volume.
- **PUBLIC-7 — DCO/CLA**: re-enable contributor sign-off when external
  contributions open (runbook "When opening to external contributions").

## 8. Ownership boundary

`public` owns the **contract and the sequence**; it does not execute subsystem
work in its own context:

- repo mechanics (remote URL, mini repo-puller resync, deploy checkout) → **`deploy`**
- META-system public/private call → **`substrate`**
- secret-scan / scrub-guard hardening → **`edr` / `diligence`**
- each doc's public/private classification → that doc's **owning aspect** (deposit
  the call into its ledger; don't decide unilaterally)
- gitpages / public-site presentation → **`ui`** (co-owns presentation)

Cutover-day execution is **operator-gated** (`G-cutover`): no cutover chip
dispatches without an explicit operator go and a confirmed date.

## 9. Backlog (live state in `meta-state/public.json`)

- **PUBLIC-1** — bifurcation contract: **DESIGNED** (§4; PD-3/PD-4/PD-5 resolved
  2026-06-18). Remaining = the **build**:
  - **PUBLIC-1a** — `docs/public-manifest.yaml` (the denylist source of truth) +
    a CI guard that fails if a denylisted path would publish.
  - **PUBLIC-1b** — `tools/publish-public` (compute public tree − denylist →
    scrub-gate the output → squashed push to `evolve-ops/evolve`); `--dry-run`
    first. The first real run is the cutover.
- **PUBLIC-2** — META-system **sanitization** (→ substrate); decided to publish
  sanitized (§5).
- **PUBLIC-3** — re-scrub against final `main` (`cjalden` back in ~40 files);
  harden the gates so the scrub runs on the **publish output** (→ edr/diligence).
- **PUBLIC-4** — re-plan the cutover runbook for the fresh-repo model: Phase 4
  (filter-repo) + Phase 6 (transfer) are superseded by `tools/publish-public`;
  the mini stays on `cjalden/evolve` (no remote-URL change). Keep the scrub
  gates, smoke tests, rollback floors.
- **PUBLIC-5** — ~~private dev repo~~ **RESOLVED** (PD-5: keep `cjalden/evolve`
  private; no migration). Optional org-hygiene rename deferred.
- **PUBLIC-6** — public-issue triage (post-cutover, deferred ~4wk).
- **PUBLIC-7** — DCO/CLA on external-contribution open.

## 10. Source corpus (inherited)

- [`docs/runbook-public-cutover.md`](runbook-public-cutover.md) — the 10-phase
  cutover runbook (Phases 4 + 6 superseded by PD-2; the rest carries over).
- [`docs/spec-public-issue-triage-2026-06-04.md`](spec-public-issue-triage-2026-06-04.md)
- [`docs/spec-github-pages-destination-2026-06-04.md`](spec-github-pages-destination-2026-06-04.md)
- [`docs/PLACEHOLDER_NAMING.md`](PLACEHOLDER_NAMING.md)
- `packages/admin/tests/test_public_launch_scrub.py`
- `docs/private/public-launch-cleanup.md` (private — the original cleanup log)
