"""pod_baseline — pod-level declared config intent + read-only drift census.

Spec: docs/spec-pod-plane-2026-08-15.md (META:pod-plane, B1 — the keystone
bite). The pod baseline is a declared desired state over five per-bot config
surfaces (Q2, decided 2026-08-15): exec policy, tool profile, browser
enabled, context profile, model policy. Every bot either conforms to the
baseline, matches a first-class declared exception, or is DRIFT — never
silently divergent.

B1 is READ-ONLY by charter invariant: this package writes exactly one file,
`{shared_dir}/pod-baseline.json` (Q1, decided 2026-08-15 — evolve-owned,
atomic temp+rename, `schema_version` field, single writer). It never writes
any bot config; reconcile writes are B2+ work and go through the existing
appliers.

Modules:
- ``schema``  — the baseline document, the exception object, and the pure
  conform/exception/drift classification.
- ``store``   — load/save of ``pod-baseline.json`` (atomic, sorted keys).
- ``census``  — pure per-surface extractors over a bot's openclaw.json /
  evolve-tiers.json plus the read-only pod sweep.
- ``seed``    — pure majority computation used by ``pod-baseline seed``.

CLI surface: ``evolve-admin pod-baseline [census|seed]`` — body in
``evolve_admin/pod_baseline_cli.py`` (cli.py is no-growth capped).

No facade re-exports: consumers import from the submodule that owns the
name (``pod_baseline.schema``, ``.store``, ``.census``, ``.seed``) so a
grep for callers finds every import site.
"""
