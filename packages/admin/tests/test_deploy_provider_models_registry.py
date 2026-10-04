"""tests/test_deploy_provider_models_registry.py — regression guards
around the provider-models registry sync.

## History (read me before editing this file)

This file's job has changed twice. Each iteration was correct given
the OpenClaw behavior available at the time, but the runtime kept
moving and the guards had to follow.

### Iteration 1 — "lock the merge-only contract" (2026-06-03 morning)

PR #2019 added ``_reconcile_provider_models_registry`` in
``ensure_plugin_config`` to derive
``models.providers[<provider>].models[]`` from
``agents.defaults.models`` keys. The original tests in this file
enforced "merge-only" semantics: existing entries preserved,
missing entries appended with ``{id, name}``.

### Iteration 2 — "regression guard against re-introduction" (PR #2025)

The reconciler shipped on OpenClaw v2026.5.28. On that version OC's
resolver had a bug (filed upstream as openclaw#88560): it
double-prefixed any model id that already contained a ``/`` separator
at lookup time. Combined with the reconciler's correctly-shaped
unprefixed ``id`` (e.g. ``"claude-haiku-4-5"``), this produced
``"anthropic/anthropic/claude-haiku-4-5"`` → ``model_not_found`` on
every routing decision → bot stuck in a broken state. PR #2025
removed the reconciler and replaced this file with regression guards
forbidding any future writer of ``models.providers``.

That decision was correct for the OC version then running.

### Iteration 3 — "re-introduce, gated on OC version + correct schema" (now)

Upstream merged the resolver fix as openclaw#88587 (2026-05-31 13:47
UTC). OC v2026.6.1 (released 2026-06-03 19:35 UTC, auto-installed on
this pod at 14:10 PT) contains the fix. With the resolver bug gone:

1. The registry-gate IS strictly required — a bot whose
   ``agents.defaults.models`` contains a catalog-keyed reference but
   whose ``models.providers[<provider>].models[]`` is empty will hit
   ``FailoverError: Unknown model: <provider>/<model-id>`` (now
   correctly single-prefixed) on every routing decision.
2. The previously-wrong-looking unprefixed-``id`` shape is what OC's
   schema actually requires — confirmed by inspection of OC's
   bundled schema (``docs/schemas/oc-config-schema.txt``) and by
   live validation: the new helper writes
   ``{"id": "<model-id>", "name": "<model-id>"}`` and OC's
   ``config validate --json`` accepts it.

Empirical validation: backfill applied to six affected bots; both
subsequent heartbeats routed to ``anthropic/claude-haiku-4-5`` and
resolved cleanly. Zero schema or ``FailoverError`` events on any
bot since.

The new writer is in ``packages/analyzer/oc_model.py`` as
``sync_provider_models_from_catalog`` — structurally distinct from
the deleted reconciler:

- Lives at the canonical write surface (``oc_model.set_catalog``)
  rather than being duplicated in ``deploy.py``.
- Called transitively from ``set_catalog``, so every catalog write
  (provisioning seed, AI Optimization reconcile, reconcile-catalog
  CLI, deploy gap-fill) keeps both layers in sync.
- Skips providers OC does NOT bundle (e.g. ``runway``) since those
  require ``baseUrl`` we can't synthesize. Validated empirically
  when the pre-write OC validator rejected a runway overlay.
- Pre-write validation in ``safe_write_bot_config`` AND pre-kickstart
  validation in ``_kickstart_gateway_and_wait`` reject any
  schema-invalid write before it reaches disk.

## What this test enforces

The OLD function name and shape stay deleted; the NEW helper is
wired correctly into the deploy path.

1. ``_reconcile_provider_models_registry`` is NOT exported from
   ``evolve_admin.deploy``. The old function name remains forbidden
   so anyone reading the history can't accidentally bring it back.
2. ``sync_provider_models_from_catalog`` IS callable from
   ``oc_model``.
3. ``deploy.py`` calls ``sync_provider_models_from_catalog`` (not
   ``_reconcile_provider_models_registry``).
4. The smoking-gun textual signature from the broken reconciler —
   ``partition("/")`` inside a ``models.providers``/``providers_block``
   /``by_provider`` window — is NOT present in ``deploy.py``. The
   correct helper does its partition in ``oc_model.py``, which is
   intentional: ``deploy.py`` stays as the thin call site, not a
   re-implementation.

If a future maintainer needs to re-debug the FailoverError, START
from these three references:

- openclaw#88560 (original bug report, this fleet)
- openclaw#88587 (the resolver fix that made the writer safe)
- ``packages/analyzer/oc_model.py::sync_provider_models_from_catalog``
  (the canonical writer)

DO NOT re-introduce a writer in ``deploy.py`` that calls
``partition("/")`` on ``agents.defaults.models`` keys — keep the
helper in ``oc_model.py`` so there's a single source of truth and a
single place to update if the schema moves again.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def test_old_reconciler_function_stays_deleted():
    """``_reconcile_provider_models_registry`` (the PR #2019 function
    name, deleted in PR #2025) must not return to
    ``evolve_admin.deploy``. The new sync lives in
    ``oc_model.sync_provider_models_from_catalog`` — see this file's
    module docstring for the full history."""
    import evolve_admin.deploy as deploy

    assert not hasattr(deploy, "_reconcile_provider_models_registry"), (
        "_reconcile_provider_models_registry was re-introduced to "
        "evolve_admin.deploy. The current writer lives at "
        "oc_model.sync_provider_models_from_catalog and is called "
        "transitively from oc_model.set_catalog. If you need to "
        "modify the registry-write logic, do it there, not by "
        "re-introducing the deleted function."
    )


def test_new_sync_helper_is_importable():
    """``sync_provider_models_from_catalog`` must be importable from
    ``oc_model``. The deploy gap-fill block depends on it; AI
    Optimization's tier-write path calls it transitively via
    ``set_catalog``."""
    from oc_model import sync_provider_models_from_catalog  # noqa: F401

    assert callable(sync_provider_models_from_catalog)


def test_deploy_calls_new_helper_not_old_function():
    """``deploy.py`` must reference the new helper (positive guard) and
    NOT reference the old function name (negative guard)."""
    deploy_src = (
        Path(__file__).parent.parent
        / "evolve_admin"
        / "deploy.py"
    ).read_text()

    assert "sync_provider_models_from_catalog" in deploy_src, (
        "deploy.py no longer imports/calls "
        "sync_provider_models_from_catalog. The provider-registry "
        "sync block in ensure_plugin_config has been removed or "
        "renamed — re-check the deploy path to ensure new bots get "
        "their models.providers populated correctly. Without it, "
        "every cron / heartbeat / fallback that touches a catalog "
        "model will hit FailoverError on OpenClaw v2026.6.1+."
    )

    # Negative guard: the deleted function name must not return as a
    # callable. Mentions in docstrings/comments that explain history
    # are allowed.
    forbidden_call = "_reconcile_provider_models_registry("
    for lineno, line in enumerate(deploy_src.splitlines(), start=1):
        if forbidden_call not in line:
            continue
        stripped = line.strip()
        if (
            stripped.startswith("#")
            or stripped.startswith('"""')
            or stripped.startswith('"')
            or stripped.startswith("``")
        ):
            continue  # documentation mention — allowed
        raise AssertionError(
            f"deploy.py:{lineno} contains a callable reference to "
            f"_reconcile_provider_models_registry. That function "
            f"was deleted in PR #2025 and must not return. Use "
            f"sync_provider_models_from_catalog instead. Line: "
            f"{stripped!r}"
        )


def test_deploy_does_not_inline_partition_on_provider_keys():
    """The smoking-gun signature of the original broken reconciler
    was a ``partition("/")`` call in ``deploy.py`` inside a
    ``models.providers``/``providers_block``/``by_provider`` window —
    that combination produced unprefixed ids from prefixed catalog
    keys. The correct writer does its partition in ``oc_model.py``,
    so ``deploy.py`` should never contain that shape. Catches a
    future re-implementation that copies the helper inline rather
    than calling it."""
    deploy_src = (
        Path(__file__).parent.parent
        / "evolve_admin"
        / "deploy.py"
    ).read_text()

    lines = deploy_src.splitlines()
    for idx, line in enumerate(lines):
        if 'partition("/")' not in line:
            continue
        stripped = line.strip()
        # Skip pure comment/docstring lines that just explain history.
        if (
            stripped.startswith("#")
            or stripped.startswith('"""')
            or stripped.startswith('"')
            or stripped.startswith("``")
        ):
            continue
        # Window around this line; only fail if the shape touches
        # the registry path.
        window = "\n".join(lines[max(0, idx - 5):idx + 10])
        if (
            "models.providers" in window
            or "providers_block" in window
            or "by_provider" in window
        ):
            raise AssertionError(
                f"deploy.py:{idx + 1} uses partition('/') near a "
                f"models.providers / providers_block / by_provider "
                f"context. This is the smoking-gun signature of the "
                f"PR #2019 broken reconciler. The correct writer "
                f"lives in oc_model.sync_provider_models_from_catalog "
                f"and is called by deploy.py, NOT inlined. Move the "
                f"partition logic back to oc_model.py."
            )
