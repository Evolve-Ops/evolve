# evolve-edr — the Evolve Development Rig

EDR is the **meta-development system that builds and improves Evolve itself**.
It is a dev-environment-only sibling to the shipped product — not a pod feature,
not a bot capability. Where the product deploys to every pod and is governed by
the "mildly-tech-capable" constraint, EDR runs only in the Evolve development
environment and can be as technical as it needs to be. Its actuator is **Claude
Code** (which writes code and opens PRs), not openclaw bots. Its data sources are
GitHub issues, help tickets, feedback, opt-in pod telemetry, CI failures, and
market intelligence — the inputs to improving Evolve, not a household's context.

The architectural rule that organizes this package: EDR **recomposes Evolve's own
RSI loop primitives** — the signal store, the arbiter/proposal store, the
generators, the verify daemon — over the new domain of Evolve's own development.
It does so by **importing the packaged `evolve-analyzer` libraries**. It must
**never fork or copy them**. The dependency on `evolve-analyzer` declared in
`pyproject.toml` is the load-bearing seam: an EDR Signal is a real
`schema.signal.Signal` written through `signals.store.observe()`, and an EDR
Proposal is a real `schema.proposal.Proposal` written through
`arbiter.store.write_proposal()`. The `tests/test_library_reuse_smoke.py` test is
the falsifiable proof that this round-trip works against the real imported
library code, not a reimplementation.

**EDR is NOT shipped to pods.** It ships nothing a pod loads. It must never appear
in any deploy or install path — not `packages/admin/evolve_admin/deploy.py`, not
the plist / launchd / JobSpec renderers, not `packages/plugin/**`. The
`tests/test_not_shipped_guard.py` test (the "G1 not-shipped guard") enforces this:
it fails if any guarded pod-install path imports or path-references `edr/`. The
guard carries a positive control so it can never be vacuously green.

This is dev-environment-only code. It is **not** subject to the product's Plex
test, but it **is** subject to the repo's lint/type/test gates. The full design
record lives at `docs/edr/design-edr-2026-06-11.md`.

## Tests

```sh
# From the repo root, with the uv workspace synced (installs evolve-analyzer
# + evolve-edr editable):
uv sync --locked
uv run --no-sync pytest edr/tests
```
