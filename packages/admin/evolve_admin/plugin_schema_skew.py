"""Plugin-config schema-skew tripwire (legibility layer).

The primary fix for the ``repoRoot`` reject-on-every-reload trap lives in
``openclaw_materializer`` (gate the write/strip on the DEPLOYED manifest the
gateway loads, not the repo source). This module is the *legibility* half:
when the deployed/staged plugin manifest LAGS the repo source schema, the
materializer silently omits/strips the lagging key(s) so reloads stay valid —
but a silent omission is exactly the producer-silence class of failure
(cron_exit fired but invisible). So we raise a pod-scope Signal naming the
dropped key(s) and both manifest paths, and auto-resolve it the moment the
staged plugin catches up.

Cheap + pure-Python: two tiny JSON reads + one ``observe()`` / ``sweep_resolve()``
per call. Wired into ``deploy.ensure_plugin_config`` and guarded there so a
signals hiccup never blocks a deploy.
"""

from __future__ import annotations

from pathlib import Path

from signals import store as signals_store
from schema.signal import make_signature

PRODUCER = "plugin_schema_skew"
SIG_TYPE = "plugin_schema_skew"


def compute_skew_keys(install_dir: Path, source_dir: Path) -> set[str]:
    """Keys the repo SOURCE manifest declares but the DEPLOYED manifest does
    NOT — i.e. keys the running gateway would reject if the admin wrote them.

    Empty when the deployed manifest is current, absent, or unreadable: if we
    can't read the deployed copy we can't claim skew (and the materializer's
    own fallback to source means nothing was actually omitted on its account).
    """
    from .openclaw_materializer import _read_manifest_config_keys

    deployed = _read_manifest_config_keys(install_dir / "openclaw.plugin.json")
    source = _read_manifest_config_keys(source_dir / "openclaw.plugin.json")
    if deployed is None or source is None:
        return set()
    return source - deployed


def reconcile_plugin_schema_skew(
    shared_dir: Path,
    *,
    install_dir: Path,
    source_dir: Path,
) -> set[str]:
    """Detect deployed-vs-source plugin-config schema skew and reconcile the
    pod Signal: fire when keys are missing from the deployed manifest, resolve
    when the skew clears. Returns the set of skew keys (empty when in sync).

    Idempotent and pod-scoped: ``observe()`` dedups by a stable signature so
    calling this per-bot during a deploy keeps a single Signal, and
    ``sweep_resolve()`` against the dedicated ``plugin_schema_skew`` producer
    only ever touches this Signal (no collateral on other producers).
    """
    skew = compute_skew_keys(install_dir, source_dir)
    signature = make_signature(PRODUCER, SIG_TYPE, "pod")
    deployed_path = install_dir / "openclaw.plugin.json"
    source_path = source_dir / "openclaw.plugin.json"

    if skew:
        keys = ", ".join(sorted(skew))
        signals_store.observe(
            shared_dir,
            signature=signature,
            producer=PRODUCER,
            type=SIG_TYPE,
            scope="pod",
            severity="warn",
            flavor="maintenance",
            category="platform",
            title="Evolve plugin config schema skew (deployed manifest lags)",
            body=(
                f"The deployed plugin manifest the gateways load is OLDER than "
                f"the admin's source schema: it does not declare {keys}. The "
                f"materializer is omitting/stripping these key(s) so config "
                f"reloads stay valid (writing them would make OpenClaw reject "
                f"the entire config on every reload). This self-corrects once "
                f"the staged plugin catches up on the next deploy; a gateway "
                f"already running the stale manifest can be reloaded with a "
                f"gateway restart.\n\n"
                f"Deployed: {deployed_path}\nSource:   {source_path}"
            ),
            details={
                "skew_keys": sorted(skew),
                "deployed_manifest": str(deployed_path),
                "source_manifest": str(source_path),
            },
        )
        return skew

    # No skew → clear any stale Signal. kept_signatures empty resolves every
    # active Signal for this dedicated producer (at most the one above).
    signals_store.sweep_resolve(
        shared_dir,
        producer=PRODUCER,
        kept_signatures=set(),
        reason="auto-resolve: deployed plugin schema caught up to source",
    )
    return set()
