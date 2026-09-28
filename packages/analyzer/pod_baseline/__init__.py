"""pod_baseline — pod-level declared config intent + read-only drift census.

Spec: internal/spec-pod-plane-2026-08-15.md (META:pod-plane, B1 — the keystone
bite). The pod baseline is a declared desired state over five per-bot config
surfaces (Q2, decided 2026-08-15): exec policy, tool profile, browser
enabled, context profile, model policy.

A surface the pod has not declared is **undeclared** and is never reported
as conform (Q7(b), decided 2026-08-22) — seeding refuses to elect a
no-intent sentinel (``custom``/``unset``) as if it were policy. Every bot
either conforms, matches a first-class declared exception, or deviates — and
the deviation carries a **direction** (Q7(a), same date): ``tightened``
(harder than policy — informational, and never a reason to file an
exception), ``loosened`` (the fault state) or ``divergent`` (a surface with
no safety ordering). Never silently divergent, and never silently green.

The two are not nested: a bot carrying its own declared exception on an
*undeclared* surface still classifies against that exception, because an
exception IS a declaration. Consumers whose rule is per-surface (B2's "no
per-bot drift Signal for an undeclared surface") must therefore read
``CensusRow.surface_undeclared``, not the row state.

B1 is READ-ONLY by charter invariant: this package writes exactly one file,
`{shared_dir}/pod-baseline.json` (Q1, decided 2026-08-15 — evolve-owned,
atomic temp+rename, `schema_version` field, single writer). It never writes
any bot config; reconcile writes are B2+ work and go through the existing
appliers.

Modules:
- ``schema``  — the baseline document, the exception object, and the pure
  classification into the seven states.
- ``ordering`` — which surfaces carry a safety ordering and what it is (the
  partial order behind the tightened/loosened/divergent split).
- ``store``   — load/save of ``pod-baseline.json`` (atomic, sorted keys).
- ``census``  — pure per-surface extractors over a bot's openclaw.json /
  evolve-tiers.json plus the read-only pod sweep.
- ``seed``    — pure majority computation + the undeclared rule, used by
  ``pod-baseline seed``.

CLI surface: ``evolve-admin pod-baseline [census|seed]`` — body in
``evolve_admin/pod_baseline_cli.py`` (cli.py is no-growth capped).

No facade re-exports: consumers import from the submodule that owns the
name (``pod_baseline.schema``, ``.store``, ``.census``, ``.seed``) so a
grep for callers finds every import site.
"""
