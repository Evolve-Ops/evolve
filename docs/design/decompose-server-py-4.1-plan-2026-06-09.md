# Plan: decompose `server.py` (roadmap 4.1)

**Status:** planned (not started) · **Date:** 2026-06-09 · **Roadmap:** Phase 4 (4.1)

A risk-managed, PR-by-PR decomposition strategy for
`packages/admin/evolve_admin/web/server.py`, produced by a read-only planning
pass over the real file. Every step is a **pure code-move** (no behavior change),
independently shippable, and verified by the existing test suite.

## The surprise (and the good news)

- **server.py is 34,207 LOC**, not ~10K. The roadmap's "10,452-line function" is
  real — it's **`_register_admin_routes`** (lines 13628–24084, **10,457 LOC**) —
  but the *file* is ~3× bigger than the roadmap framing implied. Hitting the
  proof artifact (server.py < ~10K) means physically relocating ~24K LOC.
- **The good news: it's already a clean two-tier app, not a monolith.**
  `create_app()` (640–4923, 4,284 LOC) is an app factory that calls **31
  in-file `_register_*_routes(app, network_path)` functions** (27,193 LOC) and
  ~30 **already-extracted** `routes_*.py` / `*_routes.py` sibling modules. So the
  work is mechanical extraction along seams that already exist — **low-risk per
  PR, not risky logic surgery.**

## The coupling surface (small)

- `load_network` / `save_network` / `DEFAULT_NETWORK_CONFIG` — already in sibling
  `config.py`; extracted modules import them directly.
- **`_audit_log_entry`** (server.py:12097) — the *one* server-defined helper the
  giant function leans on (134 calls), and it's **cross-imported** by
  `evo/handlers/tier.py`, `oauth/__init__.py`, `wizard_routes.py`, **and
  monkeypatched** at `evolve_admin.web.server._audit_log_entry` in
  `test_oauth_provider_audit_logging.py`. → Move it to a new `routes_shared.py`
  but **re-export it from server.py** so both the import path and the patch
  target stay valid.
- A few tests call `_register_admin_routes` / `_register_analytics_routes` **by
  name** → keep those as **thin shims** that delegate to the new registrars
  (zero test churn).
- Dispatch is plain Flask decorators (485 routes), **no central table** → the one
  real failure mode is *forgetting to wire a new registrar in `create_app`* →
  silent 404. Each PR's diff must show exactly one added call per new module, and
  the targeted test run asserts the routes are live.

## Registration contract to mirror (do not invent a new one)

Each new module exposes `register_<domain>_routes(app, network_path)`;
`create_app` imports it locally and calls it in the 654–884 registration block.
Exemplar: `packages/admin/evolve_admin/web/routes_alerts.py`.

## Risk-ordered PR sequence

Each PR: cut the register fn + its routes into a new `routes_<x>.py`, add the
import+call in `create_app`, delete the old in-file def. Verify
`cd packages/admin && python -m pytest <targeted files>` then the full suite.

| PR | New file(s) | Source | server.py after |
|----|-------------|--------|-----------------|
| **0** | `routes_shared.py` (move `_audit_log_entry`, `_redact_secrets`, `_SECRET_KEY_NAMES`, `_REDACTED`; **re-export from server.py**) | 606–621, 12097 | ~34,150 |
| 1 | `routes_signals.py` | `_register_signals_routes` 33198–33828 | ~33,500 |
| 2 | `routes_report.py` | 27448–27948 | ~33,000 |
| 3 | `routes_content_scan.py` | 26891–27447 | ~32,400 |
| 4 | `routes_better.py` | 28891–29500 | ~31,800 |
| 5 | `routes_cost_measures.py` | 27949–28736 | ~31,000 |
| 6 | `routes_bot_config.py` | 7622–8627 | ~30,000 |
| 7 | `routes_analytics.py` (+ keep `_register_analytics_routes` shim — 5 tests) | 5274–7516 | ~27,800 |
| 8 | `routes_oc.py` | `_register_oc_routes` 8644–11530 | ~24,900 |
| 9 | `routes_arbiter.py` + `routes_proposals.py` (split 3,697) | 29501–33197 | ~21,200 |
| **10** | `routes_admin_models.py` | giant fn 13651–15412 | ~19,450 (`_register_admin_routes` → ~8,700) |
| **11** | `routes_admin_keys.py` (+ gateway/usage tail) | 15413–17371, 24019–24084 | ~17,450 (→ ~6,700) |
| **12** | `routes_admin_onboard.py` | 17372–19357 | ~15,500 (→ ~4,700) |
| **13** | `routes_skills_install.py` + `routes_skills_messaging.py` (split 4,640; keep `_register_admin_routes` shim — 4 tests) | 19358–23999 | **~10,800 — giant function gone (→ ~30-line shim)** |
| 14 | `routes_admin_misc.py` (batch the ~15 small register fns) | various | **< ~7,800** |

**Proof artifact satisfied at PR 13** (giant function broken up) and **fully at
PR 14** (server.py < 10K, no file > ~3K — note `routes_skills_install` is itself
split to stay under 3K — no function > ~300 lines except the trimmable
`create_app` body).

## Out of scope for 4.1

The ~178 `except: pass` swallows in server.py are **roadmap 4.2** (the ratchet
gate). Pure code-moves here neither add nor remove a swallow; don't mix the two.

## Hazards checklist (per PR)

1. Keep `_audit_log_entry` / `_redact_secrets` re-exported from server.py (import
   path + monkeypatch target).
2. Route shared helpers through `routes_shared.py` (no Flask, no server import) to
   avoid import cycles — both server.py and route modules import *from shared*,
   never from each other.
3. Keep function-scoped `from ..config import ...` / `from oc_cli import ...`
   imports verbatim with their routes (preserve laziness, dodge top-level cycles).
4. Preserve `_register_admin_routes` / `_register_analytics_routes` as shims.
5. Each PR diff: exactly one added `register_*` call in `create_app` per new
   module; targeted test run asserts route presence (guards the silent-404 mode).
