# Onboarding — getting productive in the Evolve codebase

This is the engineering orientation for someone who will *work on* Evolve (a
second engineer, or you six months from now). It complements two neighbours:

- **[CONTRIBUTING.md](CONTRIBUTING.md)** — licensing, sign-off, external-PR mechanics.
- **[CLAUDE.md](CLAUDE.md)** — the authoritative runtime/dev-environment guide. **Read it.**
  This doc points at it repeatedly rather than restating it.

The goal of this page is concrete: by the end you will have **landed a small,
real PR** using only checked-in docs.

---

## 1. What Evolve is (the 90-second model)

Evolve is a management + safety + self-improvement layer that runs a pod of
[OpenClaw](https://github.com/) bots on a Mac. Think *Plex for AI agents*:
OpenClaw is the runtime; Evolve is the packaging, operations, and
recursive-improvement layer on top.

Start here, in order:
- **[README.md](README.md)** — what it does, end to end.
- **[docs/product-vision.md](docs/product-vision.md)** — why, and the design principles.
- **[docs/architecture.md](docs/architecture.md)** — how the pieces fit.

## 2. The three packages you'll touch

| Package | What it is | You're here when… |
|---------|-----------|-------------------|
| **`packages/admin`** | The admin server + single-page web UI (`evolve_admin/web/`). Python backend, vanilla-JS SPA. | You're changing an operator-facing screen or an `/api/*` route. |
| **`packages/analyzer`** | The RSI "Better Engine": the **arbiter** proposal pipeline, **generators**, **signals**, **metrics**, the **verify** daemon. | You're changing how the system observes, proposes, applies, or verifies improvements. |
| **`packages/plugin`** | The OpenClaw TypeScript plugin that runs inside each bot. | You're changing what Evolve does *from inside* a bot's runtime. |

The core data model lives in **CLAUDE.md** — the arbiter on-disk layout
(`proposals/`, `generators/`, `profiles/`), the **signal store** (monitors write
*Signals*, generators write *Proposals*, `Proposal.motivating_signals[]` links
them), and the L1–L6 applier separation. Read those two CLAUDE.md sections before
touching `analyzer/`.

## 3. Two checkouts, one rule that will bite you

This is the #1 newcomer footgun, and it's covered in full in CLAUDE.md
("Where to do dev work"). The short version:

- **`/Users/Shared/evolve-repo` on the deploy host is the DEPLOY checkout.** A daemon
  `git pull`s it every 15 min and every running service loads from it. **Treat it
  as read-only.** Never run an editor/agent against it — untracked files wedge the
  next pull.
- **Do all dev work from your laptop clone** (e.g. `~/GitHub/evolve`). Use
  `git worktree add` for parallel branches.

Also load-bearing: the admin server runs as the **`evolve`** macOS user, not your
login user. File-access and subprocess code must respect that — CLAUDE.md's "File
Access Pattern" section is the law here (direct reads via ACL; `/tmp` staging +
`sudo /bin/cp` for writes; never `sudo -u <bot>`).

## 4. Setup

```bash
git clone <your-fork-or-the-repo> evolve && cd evolve
# One command installs both packages (editable) plus the locked test/lint
# toolchain. Plain `pip install -e packages/admin` will NOT work — admin
# depends on evolve-analyzer, which is monorepo-internal, not on PyPI.
uv sync
uv run python -m pytest packages/admin -q   # smoke: the suite collects
```

> **Python 3.10, not "whatever's newest."** The repo pins `.python-version` to
> `3.10` so `uv sync` builds the same interpreter CI runs. Without the pin, uv
> honours the packages' `requires-python = ">=3.10"` floor and resolves the
> newest CPython on your machine — which meant a test could be green on all
> ~18 CI jobs and still break locally on 3.13 (stdlib internals differ; see
> PR #3470's `Path.stat` case). If you don't have a 3.10 to hand, `uv python
> install 3.10` fetches one. Delete `.venv` and re-run `uv sync` if your
> existing venv predates this pin.

> ⚠️ If you use a git **worktree**, note the editable install binds to whichever
> checkout you `pip install -e .`'d from. Tests in a *second* worktree may load
> the *other* checkout's code unless a conftest rebinds it. (This has bitten us;
> it's why some test dirs have a path-rebinding conftest.)

## 5. The dev loop

```bash
git checkout -b fix/short-description main      # always branch from main

# … make your change …

# Run the relevant package's tests:
cd packages/admin    && python3 -m pytest tests/     # admin changes
cd packages/analyzer && python3 -m pytest tests/     # analyzer changes

# Run the local lint gates that CI will enforce (see §6):
tools/ui-style-lint <changed-web-files>              # if you touched web/
tools/except-pass-lint --staged                      # always, after git add

git commit -m "fix: …"   # focused commits; each should pass tests
gh pr create --base main
```

Per the team's working style, **non-destructive** changes (docs, tests, tooling,
redaction) land and auto-merge; **behavior-changing** security/RSI work gets a
build-then-review pass before merge.

## 6. The gates your PR must pass

CI (`.github/workflows/ci.yml`) runs these on every PR. Run their local
equivalents *before* pushing so you're not surprised:

| Gate | What it blocks | Run locally |
|------|----------------|-------------|
| **Public-launch scrub guard** | **Reserved real names** (bot/person names) in tracked files. Use placeholders — see **[docs/PLACEHOLDER_NAMING.md](docs/PLACEHOLDER_NAMING.md)**. This *will* catch you if you paste a real bot name. | Runs as part of the admin suite in CI (the test file lives only in the private repo). |
| **Silent-exception ratchet** | New `except: pass` / `...` swallows beyond the frozen baseline. | `tools/except-pass-lint --all` |
| **Full admin / analyzer suites** | New test failures (existing known-failures are quarantined in `ci-quarantine.txt`). | the pytest commands in §5 |
| **Plugin TypeScript build** | `tsc --noEmit` errors in `packages/plugin`. | `cd packages/plugin && npx tsc --noEmit` |
| **UI style** (no CI gate yet — self-enforced) | Off-scale fonts, raw hex/shadow colors, wrong input widths, Unicode expand-glyphs. See **[docs/style-guide.md](docs/style-guide.md)**. | `tools/ui-style-lint <files>` |

**Theme parity has no hard CI gate.** If you touch `web/`, toggle dark/light in
the sidebar footer and confirm both render before you open the PR.

## 7. Your first PR (a guided, safe, real change)

A good first change exercises the whole loop without risking operations. Pay down
one entry of our silent-exception debt (roadmap item 4.2):

1. **Pick a target.** Open `tools/except-pass-baseline.txt` and pick a file with a
   small count (avoid `server.py` — start small). Find a `except …: pass` in it.
2. **Understand before you change it.** *Why* is it swallowing? Some swallows are
   legitimate best-effort cleanup; most hide a real error. The usual right fix is
   to **narrow** the except to the expected error and **log** the rest:
   ```python
   # before
   try:
       risky()
   except Exception:
       pass
   # after
   try:
       risky()
   except (OSError, ValueError) as e:
       logger.debug("risky() failed, continuing: %s", e)
   ```
   Match the file's existing logging style (find its logger; don't invent one).
3. **Verify.** Run that package's tests (§5). If the behavior is now observable,
   add or adjust a test.
4. **Ratchet the baseline down.** You just removed a swallow, so the file is now
   *below* its baseline:
   ```bash
   tools/except-pass-lint --all --update-baseline
   ```
   Commit the lowered `except-pass-baseline.txt` alongside your fix.
5. **PR it.** Title `fix: de-swallow exception in <file>`. In the body: what was
   being hidden, and how you verified the new behavior is safe.

That's a non-trivial PR — it touches real runtime code, passes every gate, and
makes the codebase measurably better. Welcome aboard.

## 8. Where to read next

- **[CLAUDE.md](CLAUDE.md)** — runtime context, file-access law, arbiter + signal-store layout, the style-guide top rules. The single most important doc.
- **[docs/architecture.md](docs/architecture.md)** — system architecture.
- **[docs/style-guide.md](docs/style-guide.md)** — mandatory before any `web/` change.
- **[AGENTS.md](AGENTS.md)** — conventions for agent-authored changes.
- **The help corpus (`docs/help/`)** — operator-facing docs for every admin
  page and subsystem. (The internal design-spec corpus lives in `internal/` and
  is not part of the public repo; architecture.md and the help corpus
  paraphrase what you need.)

Two doc trees, one rule: **`docs/` is the public product-doc tree; every
internal document — spec, design, decision, audit, build brief, incident —
goes in `internal/`.** See CLAUDE.md § "Where a new doc goes".
