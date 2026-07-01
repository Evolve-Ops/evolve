# Public Cutover Runbook

**Status:** Living document. **Not for execution yet** — the cutover is
operator-gated (`G-cutover`), date TBD. This runbook describes the
**fresh-repo publish model** locked in by spec PD-2 (see below); the older
transfer + `git filter-repo` history-rewrite plan is **superseded** and has
been removed from the cutover sequence.

**Goal:** Publish a fresh, curated **public** mirror at `evolve-ops/evolve`
(under BSL 1.1) from the private dev repo `cjalden/evolve`, with no
PII / secret / reserved-token leak and clean (squashed) git history. The
private dev repo is **never mutated**, and the deploy-host deploy pipeline is
**never touched**.

Spec: [docs/spec-public-2026-06-18.md](spec-public-2026-06-18.md) (§3 repo
model, §4 the bifurcation contract). Mechanism: `tools/publish-public` +
`docs/public-manifest.yaml`.

---

## The model — read this first (what changed from the May-2026 plan)

The original plan (preserved in git history) was to **transfer
`cjalden/evolve` → `evolve-ops/evolve`** and **`git filter-repo` the full
history in place** to scrub PII from every commit. **PD-2 reversed this** in
favor of a fresh public repo:

| | Transfer + history rewrite (old, superseded) | **Fresh public repo (current)** |
|---|---|---|
| Secret-leak surface | `filter-repo` must scrub **every historical SHA** perfectly; one miss = a permanent public leak | **zero** — public history starts clean from a curated snapshot |
| Carries over | issues, PRs, stars, GitHub redirects | nothing (low cost — solo repo) |
| Dev origin | becomes `evolve-ops/evolve` | **stays `cjalden/evolve`, private** (PD-5) |
| deploy-host deploy checkout | must be hard-reset to rewritten history (the riskiest phase) | **untouched** — the deploy-host keeps pulling `cjalden/evolve` |
| Ongoing cost | one-time | a **forward-sync discipline** (per-release re-publish) |

**Consequences for this runbook:**
- **No history rewrite.** There is no `git filter-repo` phase. The private
  repo may contain anything; only the *published output* is gated.
- **No repo transfer.** `evolve-ops/evolve` is created **fresh** as the
  publish output.
- **No deploy-host resync.** Under PD-5 the deploy-host's repo-puller keeps targeting
  `cjalden/evolve`. The cutover does not touch the deploy pipeline at all;
  Phase 8 below only *verifies* this. (An optional org-hygiene rename of the
  dev origin is a separate, deferred `deploy` task — not part of cutover.)
- **Rollback is trivial** (see below): the dev origin is never mutated, so
  there is no force-push to undo and no deploy-host to recover.

### How the publish mechanism works

```
public tree = (git ls-files at HEAD)  −  (paths matching docs/public-manifest.yaml)
```

Two gates run **on the public subset only** (never the whole repo):

1. **The denylist** — `docs/public-manifest.yaml` removes private paths
   (`docs/private/`, `issues/`, `docs/market-intelligence/`,
   `docs/incidents/`, `docs/forensic-*`, `docs/diagnosis-*`, `docs/diag-*`).
2. **The reserved-token scrub** — reused verbatim from
   `packages/admin/tests/test_public_launch_scrub.py` (same token list, scan,
   and exemptions), it must find **zero** deployment-specific identifiers in
   the public subset.

`tools/publish-public --dry-run` (the default) **computes and scrubs** the
public tree and makes no network calls and no commits. `--execute
--build-only` now **automates Phase 4** — it materializes the sanitized public
tree, re-scrubs the built artifact on disk (zero exemptions), and lands one
squashed commit, all without touching the network. The genuinely irreversible
steps — creating the public GitHub repo and flipping it public (Phase 5–7) —
remain explicit operator actions; `--execute --target <url> --push` will push
to an already-created remote, but repo creation and the public flip stay
manual (`gh`) on purpose.

---

## Living-document caveat — re-run gates if code changed since this was written

The pre-cutover PRs cleared a specific set of issues against a specific
snapshot. New commits between now and cutover day can reintroduce issues the
original scrub fixed. **Treat every gate below as "must pass against the final
pre-cutover commit," not "passed once."**

Re-run these on the head of `main` immediately before starting the cutover:

| Re-run check | Catches | How |
|--------------|---------|-----|
| **`tools/publish-public --dry-run`** | Reserved tokens in the **public subset** (the authoritative gate — runs the scrub on exactly what will ship) | `tools/publish-public --dry-run` — must end `SCRUB: PASS` and exit 0 |
| `test_public_launch_scrub.py` | New reserved tokens anywhere tracked (CI runs this per-PR) | `cd packages/admin && python3 -m pytest tests/test_public_launch_scrub.py` |
| `tools/check-public-manifest` | A dead/typo'd denylist glob, or a denylisted path that would still publish | `tools/check-public-manifest` |
| Full gitleaks scan | New committed credentials | `gitleaks detect --source . --no-banner --log-opts="--all"` |
| Full trufflehog scan | New committed credentials (different rule set) | `trufflehog git file://. --only-verified --no-update` |
| Inbound URL refs to `cjalden/evolve` | Public docs/code linking back to the **private** dev repo (broken links + origin-name leak; **not** caught by the token scrub — `cjalden` is an anonymized handle, not a reserved token) | `git grep -n 'cjalden/evolve\|github\.com/cjalden'` — see Phase 3 |
| `shell=True` site count | New unsafe shell escapes | `git grep -n 'shell=True' packages/ \| grep -v test_` |
| Author metadata leaks | New committers, leaked hostnames | `git log --all --pretty='%ae \| %an' \| sort -u` |

> **Known gap in the two-layer model (track under PUBLIC-3 → edr/diligence):**
> a file that is **not** on the manifest denylist **and** is on the scrub's
> `ALLOWLISTED_PATHS` ships publicly *with its reserved tokens intact* (the
> scrub skips it; the denylist doesn't catch it). The scrub allowlist exists
> to keep token-bearing **test files** from reddening private-repo CI — but
> those same files reach the public subset unless also denylisted. Before
> cutover, reconcile `ALLOWLISTED_PATHS` against the manifest: every
> allowlisted file that legitimately contains reserved tokens must either be
> denylisted (so it never ships) or confirmed token-clean for public.

---

## Decisions (locked in)

| # | Decision | Outcome |
|---|----------|---------|
| **PD-1** | Aspect | Tracked as META aspect `public` (coordinating). |
| **PD-2** | **Repo model** | **Fresh public repo** at `evolve-ops/evolve`, clean/curated history — **NOT** transfer + `filter-repo`. |
| **PD-3** | **Cut line** | Public-by-default minus the `docs/public-manifest.yaml` denylist. Dev apparatus (META system, EDR, `CLAUDE.md`/`AGENTS.md`/`ONBOARDING.md`, `.claude/skills`, `tools/meta-*`) ships **sanitized**, not hidden. |
| **PD-4** | **Forward-sync** | `tools/publish-public`: public tree → scrub the output → **squashed** push, per public release. One-directional private→public. |
| **PD-5** | **Dev origin** | **Keep `cjalden/evolve` private.** Zero migration; the deploy-host repo-puller stays pointed there; `evolve-ops/evolve` is born fresh as the publish output. |
| 1 (License) | License | **BSL 1.1**, simple non-commercial Additional Use Grant, 4-year Apache 2.0 change date. Done in PR #1840. |
| 2 (Author identity) | Author email | No history rewrite, so there is no `.mailmap`-driven bulk rewrite. The single squashed publish commit is authored by the committing identity. Optionally set `git config user.email hello@evolveops.dev` in the publish tree (Phase 4) so the public author-of-record is the project address rather than a personal one. |
| 3 (`docs/archive/`) | Disposition | Moved to `docs/private/archive/` + gitignored (PR #1843); never reaches the public subset. |
| 4 (`public-launch-cleanup.md`) | Disposition | Private (`docs/private/`, PR #1843). `docs/PLACEHOLDER_NAMING.md` carries the public placeholder mapping. |

---

## What's done (pre-cutover sprint + build)

**May 2026 structural cleanup** — six stacked PRs:

| PR | Title | Effect |
|----|-------|--------|
| #1835 | Phase 10 audit follow-ups | 4 real-shaped credentials swapped; 12 sensitive docs moved private; scrub-decay docs sanitized; CI guard introduced |
| #1840 | License → BSL 1.1 + SECURITY.md + CONTRIBUTING.md | License migrated; vuln-reporting path + contributor sign-off documented |
| #1843 | `docs/archive/` → private + `PLACEHOLDER_NAMING.md` | 77 historical docs moved out of public tracking; placeholder mapping kept public |
| #1845 | Code hygiene quick wins | JS feedback fallback hardened; sprint artifacts + orphan modules deleted |
| #1846 | KNOWN_RESIDUE fixture migration | 9 test files (~170 token occurrences) migrated to placeholders |
| #1847 | CI workflow | scrub-guard + launchd-scope + gitleaks run on every PR |

**June 2026 publish spine** (PD-2 model) — PRs #3021 (carve + contract) and
#3024 (the spine):

- `docs/public-manifest.yaml` — the denylist source of truth.
- `tools/check-public-manifest` — CI guard (job `public-manifest`) that fails
  if a denylist glob is dead or a denylisted path would still publish.
- `tools/publish-public` — computes the public tree, runs the scrub on the
  output, prints the split; `--dry-run` default, `--execute` a guarded stub.

---

## Pre-cutover checklist — user-action

These land independently before the cutover-day sequence.

### Critical (block cutover)

- [ ] **`tools/publish-public --dry-run` ends `SCRUB: PASS` on final `main`.**
  This is the authoritative readiness gate.
- [ ] **URL sweep clean (PUBLIC-3).** `git grep 'cjalden/evolve'` returns only
  intentional refs (`.mailmap`, the publish tooling's own docstrings). See
  Phase 3.
- [ ] **META-system sanitization done (PUBLIC-2 → substrate).** The dev
  apparatus ships public (PD-3); reference-deployment detail the token scrub
  *exempts* (operator paths, host/account refs in skill bodies + procedure
  docs) must be sanitized first.
- [ ] **`gitleaks` + `trufflehog` installed**: `brew install gitleaks trufflehog`.
- [ ] **`evolve-ops` org admin confirmed.** You need create-repo rights in
  `evolve-ops` (org created 2026-05-31).

### Important (worth doing, not blockers)

- [ ] **GitHub Pages destination decided**: project pages
  (`evolve-ops.github.io/evolve`) or the custom `evolveops.dev` domain (DNS in
  place if custom). Pages source is `/docs/gitpages`.
- [ ] **`evo-screen-coaches.png` visual inspection** — grep flagged reserved-token
  text in the PNG; regenerate if visible (binary files are not token-scrubbed).
- [ ] **Decide the private companion repo home** for `docs/private/` content if
  you ever want it off the dev repo (e.g. `evolve-ops/evolve-internal`).
  Not required — `docs/private/` simply never reaches the public subset.

### Optional (defer to post-cutover)

- [ ] GitHub Discussions for a community feedback channel.
- [ ] DCO/CLA enforcement (see "When opening to external contributions").
- [ ] Dependabot for `package.json` + `pyproject.toml`.
- [ ] Promote `docs/gitpages/CONTRIBUTING.md` to root + add `CODE_OF_CONDUCT.md`.

---

## Cutover-day sequence

Estimated total with re-run gates: **45–70 minutes** (the fresh-repo model
removes the history-rewrite and deploy-host-resync phases). Each phase has a verify
step before moving on.

### Phase 0 — Lock down (3 min)

```bash
cd ~/GitHub/evolve
git fetch --all
git checkout main
git pull --ff-only
git status        # clean
git log --oneline -5
```

No deploy-host action is required — the deploy pipeline is untouched by the publish
(PD-5). If anyone has an unpushed worktree they intend to publish from, make
sure `main` is the intended snapshot first.

### Phase 1 — Re-run gates against final state (10 min)

Run the gate battery from the "Living-document caveat" table. The primary gate
is the publish dry-run; any FAIL blocks the cutover until fixed.

```bash
cd ~/GitHub/evolve

tools/publish-public --dry-run          # MUST end "SCRUB: PASS", exit 0
tools/check-public-manifest             # MUST print "OK"

gitleaks detect --source . --no-banner --log-opts="--all"
trufflehog git file://. --only-verified --no-update

git grep -n 'shell=True' packages/ | grep -v test_     # expect 2 known sites
git log --all --pretty='%ae | %an' | sort -u           # spot-check author metadata
```

If gitleaks / trufflehog fire, or the dry-run scrub fails: stop, fix, land a
PR on the private repo, re-pull `main`, restart the cutover.

### Phase 2 — Final pre-cutover review (10 min)

Run the `/security-review` skill against `main`. Specifically eyeball the two
known `shell=True` sites (line numbers drift; grep for them):

- `packages/admin/evolve_admin/applications/forge_engine.py`
- `packages/admin/evolve_admin/applications/gallery.py`

Both pass manifest-author-supplied strings to subprocess. The trust model is
"manifest = author-trusted," but a security review at this moment is the gate.

### Phase 3 — URL sweep verify (5 min)

Public docs/code must not link back to the **private** `cjalden/evolve` repo —
those are broken links for public users and leak the dev origin's name. The
token scrub does **not** catch this (`cjalden` is an anonymized handle, not a
reserved token), so it is a separate gate (PUBLIC-3).

```bash
git grep -n 'cjalden/evolve\|github\.com/cjalden'
```

Every hit in the **public subset** must be rewritten to `evolve-ops/evolve`
(or removed). Intentional remaining refs: `.mailmap` and the publish tooling's
own docstrings that describe the private→public model. If a sweep is needed:

```bash
git grep -l 'cjalden/evolve' | xargs sed -i '' 's|cjalden/evolve|evolve-ops/evolve|g'
git grep -l 'github\.com/cjalden' | xargs sed -i '' 's|github\.com/cjalden|github.com/evolve-ops|g'
git add -A && git commit -s -m "chore(public-launch): flip cjalden/evolve → evolve-ops/evolve URL refs"
```

### Phase 4 — Build the publish tree + squashed commit (10 min)

This is the manual squashed-publish the `--execute` stub points at. Build the
public tree **outside** the working repo so nothing private can leak in.

```bash
cd ~/GitHub/evolve
PUB="$HOME/evolve-public-build"
rm -rf "$PUB" && mkdir -p "$PUB"

# 1. Snapshot exactly the tracked files at HEAD (no .git, no untracked).
git archive --format=tar HEAD | tar -x -C "$PUB"

# 2. Prune the denylisted paths the tool reports as excluded — and ABORT if
#    the scrub does not pass on the public subset.
tools/publish-public --json | PUB="$PUB" python3 -c '
import sys, json, os, pathlib
data = json.load(sys.stdin)
assert data["scrub_passed"], "ABORT: scrub FAILED on the public subset"
pub = pathlib.Path(os.environ["PUB"])
removed = 0
for e in data["excluded"]:
    f = pub / e["path"]
    if f.exists():
        f.unlink(); removed += 1
print(f"pruned {removed} private files; expected {data[\"excluded_count\"]}")
assert removed == data["excluded_count"], "ABORT: prune count mismatch"
'
find "$PUB" -type d -empty -delete

# 3. Sanity: the publish tree file count must equal the dry-run "Public files".
echo "publish tree files: $(find "$PUB" -type f | wc -l | tr -d ' ')"
tools/publish-public --dry-run | grep 'Public files'

# 4. Fresh repo, one squashed commit. Optionally set the public author identity.
cd "$PUB"
# git config user.email hello@evolveops.dev   # optional: public author-of-record
git init -q -b main
git add -A
git commit -s -m "Evolve — first public release

Curated public snapshot published from the private dev repo via
tools/publish-public. https://evolveops.dev"
```

> **Re-scrub the built tree, not just the source**, as a belt-and-suspenders:
> ```bash
> gitleaks detect --source "$PUB" --no-banner
> ```

### Phase 5 — Create the repo **private** + push (5 min)

Create `evolve-ops/evolve` **private first** so you can inspect the real
pushed artifact before anyone can see it.

```bash
cd "$PUB"
gh repo create evolve-ops/evolve --private \
  --description "Packaging layer for OpenClaw — Linux/Ubuntu for AI agents on your hardware" \
  --source=. --remote=origin --push
git push origin main          # if --push didn't run
```

### Phase 6 — Inspect the pushed artifact (5 min)

```bash
gh repo view evolve-ops/evolve --web     # eyeball the file tree, README
gitleaks detect --source <fresh clone of evolve-ops/evolve> --no-banner --log-opts="--all"
```

Confirm: no `docs/private/`, `issues/`, `docs/incidents/` etc.; no
`cjalden/evolve` links; README and gitpages render; scrub-clean.

### Phase 7 — Flip public + activate security (5 min)

```bash
# GitHub UI: Settings → Danger Zone → Change visibility → Public
# GitHub UI: Settings → Pages → source = main, /docs/gitpages
# GitHub UI: Settings → Security → enable:
#   - Private vulnerability reporting (the SECURITY.md address)
#   - Secret scanning
#   - Dependabot alerts
# GitHub UI: Settings → Branches → main → require status checks
#   (scrub-guard, launchd-scope, gitleaks, public-manifest)
```

Verify Pages resolves (`evolve-ops.github.io/evolve` or `evolveops.dev`).

### Phase 8 — Confirm the dev pipeline is UNAFFECTED (3 min)

The publish never touched the deploy-host. Confirm it explicitly — the deploy origin
must still be the **private** `cjalden/evolve` and the puller healthy:

```bash
ssh pod-admin-user@deploy-host "cd /Users/Shared/evolve-repo && sudo -u evolve git remote -v | head -1"
# Expect: origin  https://github.com/cjalden/evolve.git  (NOT evolve-ops)
ssh pod-admin-user@deploy-host "sudo launchctl list | grep repo-puller"
```

If either is wrong, the publish leaked into the deploy path — stop and
investigate (it should be structurally impossible under PD-5).

### Phase 9 — Public smoke + release tag (10 min)

- [ ] Pages site renders; links work; no `cjalden` URLs visible.
- [ ] "Send Feedback" in the admin UI routes to `evolve-ops/evolve/issues/new`
  (the running admin UI loads from `cjalden/evolve` on the deploy-host, so its
  feedback target is config-driven — confirm the config points at the public
  repo, or that the fail-loud "Feedback is not configured…" message appears).
- [ ] Tag a release on the public repo:
  `cd "$PUB" && git tag -s v0.1.0 -m "First public release" && git push origin v0.1.0`
- [ ] Update any external mentions (site, social, etc.).

---

## Rollback procedures

The fresh-repo model makes rollback trivial — **the private dev repo and the
deploy-host are never mutated**, so there is nothing to restore there.

### Rollback A — before Phase 5 push
Nothing has left your laptop. `rm -rf "$PUB"`. Done.

### Rollback B — after push, before public flip (Phase 7)
The repo is still private. Delete it: `gh repo delete evolve-ops/evolve`
(or leave it private and fix forward). The dev repo is untouched.

### Rollback C — after public flip
Flip visibility back to private (Settings → Danger Zone), or
`gh repo delete evolve-ops/evolve` and re-publish later. The dev repo is
untouched; no force-push and no deploy-host recovery are involved.

> There is intentionally **no deploy-host-resync rollback** (old Rollback C/D): the
> deploy-host never changed remote, so it cannot need recovery.

---

## Post-cutover follow-ups (next 1–2 weeks)

- [ ] Establish the **per-release re-publish cadence** (PD-4): each public
  release re-runs Phase 1–7 against the new `main` and pushes a fresh squashed
  commit. Consider automating Phase 4–5 into `tools/publish-public --execute`.
- [ ] Open GitHub issues for deferred should-fix items (one per item).
- [ ] Schedule a v0.2 milestone for anything noticed in the first public week.
- [ ] Decide whether to keep `CLAUDE.md` referencing the admin account
  verbatim (currently scrub-allowlisted) or convert to a placeholder resolved
  from `network.json`.
- [ ] (Optional, deferred) Org-hygiene rename of the dev origin — a one-time
  `deploy` task on the puller's remote URL, independent of the public repo.

## When opening to external contributions

DCO sign-off enforcement is currently NOT running (the `dco` job was removed
pre-public: the repo was solo, and agent-authored commits across worktrees
produced noisy false positives). To re-enable when external contributions open
(PUBLIC-7):

- [ ] **Decide DCO vs CLA.** DCO is lightweight (per-commit `Signed-off-by:`)
  and right for most BSL projects; a CLA is heavier and only needed for broad
  relicensing optionality. Prior attempts PR #2092 (suspended) and #2099
  (maintainer exemption) are in git history to crib from.
- [ ] **If DCO**: restore a `dco` job to `.github/workflows/ci.yml` with the
  bot exemption; add `dco` to required status checks; drop the "not currently
  enforced" qualifier in `CONTRIBUTING.md`.
- [ ] **If CLA**: wire up CLA Assistant; update `CONTRIBUTING.md`.
- [ ] (Optional) Install the [GitHub DCO App](https://github.com/apps/dco)
  instead of a custom job — native bot exemptions, friendlier UX.

External PRs land on the **public** repo and are integrated **manually,
maintainer-side**, into the private dev repo, then re-published forward
(PD-4). There is no automated back-sync.

---

## Risks I'd worry about most

1. **A reserved token reaches the public subset via a scrub-allowlisted,
   non-denylisted file** (the "Known gap" above). Mitigation: reconcile
   `ALLOWLISTED_PATHS` against the manifest pre-cutover (PUBLIC-3); the
   built-tree `gitleaks` re-scan in Phase 4 is the backstop.
2. **Code drift between gate-run and publish** reintroduces a credential or
   reserved token. Mitigation: Phase 1 runs `publish-public --dry-run` against
   the exact `main` you publish from; CI's scrub-guard catches the decay class
   continuously.
3. **A denylisted path slips into the publish tree** (manifest glob typo).
   Mitigation: `tools/check-public-manifest` in Phase 1, plus the Phase 4
   prune-count assertion (`removed == excluded_count`).
4. **Feedback URL still points at `cjalden/`** in a live install. Mitigation:
   Phase 9 explicitly checks the admin-UI feedback target.
5. **License switch confuses an earlier cloner.** Not retroactive — they retain
   prior-snapshot rights. Document in release notes.
