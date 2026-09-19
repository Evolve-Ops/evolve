"""Migration A — populate the per-bot overrides file from current state.

Phase 3c of ``internal/spec-openclaw-json-derived-artifact-2026-05-24.md``.

One-shot script run by the operator (CLI or admin UI) that reconciles
every bot's current ``plugins.entries.evolve.config.*`` block against
the schema + defaults registry, and writes the divergences into each
bot's overrides file with ``set_by="migration:openclaw_derived_2026_05_24"``
and ``needs_review=True``.

Why we need a one-shot, given Phase 3b's per-deploy auto-promotion
---------------------------------------------------------------------

After Phase 3b landed, the materializer already auto-promotes drift on
every per-bot redeploy. In principle, redeploying every bot in the pod
would surface the same divergences as Migration A. In practice:

  - Redeploys are slower / more disruptive than a script.
  - Some bots (security_bot's haiku pin, team_bot_c's safeguards) carry deliberate
    deviations the operator wants to *see* before any deploy touches
    them. Migration A populates the overrides file with
    ``needs_review=True`` so the Customizations UI shows them, the
    operator audits them once, then mass-redeploys with confidence.
  - The migration's ``set_by="migration:openclaw_derived_2026_05_24"``
    field tags entries with their origin, which is more accurate than
    "auto:drift_<some-timestamp>" for the bulk migration use case.

Idempotent
----------

Re-running this command never duplicates entries: keys already
represented as overrides (whether from a prior migration run, an
operator write, or an RSI proposal) are skipped. Schema-invalid drift
is skipped silently (logged) — it can't be persisted to overrides and
will be reverted on the first deploy via the materializer's stale-key
prune.

The function does NOT modify any openclaw.json file. Migration A is a
read-only-on-bot-side operation; the actual cleanup of stale keys
(removing ``reportingEnabled: true`` etc.) happens on the next deploy,
where the materializer composes a fresh block.

Operator interaction
--------------------

After running, the operator inspects each bot's
``{shared_dir}/sandbox/overrides/<bot_id>.json`` (or the Customizations
UI from Phase 4, when shipped), reviews each ``needs_review=True``
entry, and accepts / annotates / reverts as appropriate. Once the
overrides file is "clean," the next deploy of that bot materializes
the openclaw.json from the durable inputs — no surprises.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DEFAULT_SHARED_DIR, get_bot_user, load_network
from .config_sandbox import (
    OverrideStateError,
    OverrideValidationError,
    read_bot_overrides,
    write_override,
)
from .openclaw_materializer import _IDENTITY_FIELDS, _build_field_to_schema_path


_log = logging.getLogger(__name__)


MIGRATION_TAG = "migration:openclaw_derived_2026_05_24"


# Inline copy of the defaults registry. Mirrors
# ``evolve_admin.deploy._PLUGIN_CONFIG_DEFAULTS``. Inlined here so that
# Migration A is callable from contexts where ``deploy.py``'s heavy
# top-level imports (LaunchDaemons machinery, plugin manifest loaders,
# pwd lookups for the evolve user, etc.) might fail — e.g. running on a
# host without the evolve user yet, or with a misconfigured PATH. A
# 7-line dict isn't worth coupling Migration A to deploy.py's startup
# surface.
#
# If you update one, update the other. Test
# ``test_migration_defaults_stay_in_sync_with_deploy`` pins the
# equality.
_DEFAULTS_REGISTRY: dict[str, Any] = {
    "classifierModel": "anthropic/claude-haiku-4-5",
    "tierClassification": "session",
    "tier": "full",
    "summarizerMinTurns": 2,
    "classifierKeywordConfidenceFloor": 0.80,
    "costLedgerEnabled": True,
    # Layer-2 per-role tool-call enforcement gate arming flag; False =
    # observe-only. Mirrors deploy._PLUGIN_CONFIG_DEFAULTS (drift-tested).
    "layer2Enforce": False,
    # Context-observability Phase 0 prefix-hash ledger; False = dark.
    # Mirrors deploy._PLUGIN_CONFIG_DEFAULTS (drift-tested).
    "prefixHashLedgerEnabled": False,
    # Exec-failure channel-hygiene absorber arming flag; False =
    # observe-only. Mirrors deploy._PLUGIN_CONFIG_DEFAULTS (drift-tested).
    "execFailureAbsorb": False,
}


@dataclass
class BotMigrationResult:
    bot_id: str
    promoted: list[str] = field(default_factory=list)
    already_overridden: list[str] = field(default_factory=list)
    # (field_name, current_value, reason) — preserving the value so the
    # operator can see what the typo / wrong-typed entry actually was,
    # without having to ``cat`` the openclaw.json manually.
    schema_invalid: list[tuple[str, Any, str]] = field(default_factory=list)
    persistence_failed: list[tuple[str, str]] = field(default_factory=list)  # (key, error)
    error: str = ""   # set when we couldn't even read the bot's openclaw.json


@dataclass
class MigrationResult:
    """Aggregate result across all bots."""

    per_bot: dict[str, BotMigrationResult] = field(default_factory=dict)

    def total_promoted(self) -> int:
        return sum(len(r.promoted) for r in self.per_bot.values())

    def total_schema_invalid(self) -> int:
        return sum(len(r.schema_invalid) for r in self.per_bot.values())

    def total_already_overridden(self) -> int:
        return sum(len(r.already_overridden) for r in self.per_bot.values())

    def bots_with_errors(self) -> list[str]:
        return sorted(b for b, r in self.per_bot.items() if r.error)


# ─────────────────────────────────────────────────────────────────────────────
# Reading bot openclaw.json (evolve-user-readable via ACL; sudo /bin/cat fallback)
# ─────────────────────────────────────────────────────────────────────────────


def _read_openclaw_json(
    bot_id: str,
    network: dict[str, Any],
) -> tuple[dict | None, str]:
    """Return ``(parsed_dict, error_message)``.

    Tries direct ``Path.read_text`` first (works when ACL is set up per
    CLAUDE.md's ``set_evolve_read_acl``), falls back to ``sudo /bin/cat``
    (the sudoers grant in ``/etc/sudoers.d/evolve``). The error message
    is specific to which failure mode we hit so the operator can
    distinguish (a) file genuinely doesn't exist (bot half-deployed),
    (b) ACL not yet applied + sudoers grant missing (install incomplete),
    (c) sudo /bin/cat failed for some other reason (e.g. plain text in
    stderr from a denied sudoers rule).
    """
    bot_user = get_bot_user(bot_id, network)
    path = Path(f"/Users/{bot_user}/.openclaw/openclaw.json")

    # First: does the file even exist? Reading via the privileged path
    # so a directory ACL gap doesn't shadow "file actually missing."
    try:
        exists_check = subprocess.run(
            ["sudo", "/bin/test", "-f", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        file_exists = exists_check.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        file_exists = None  # unknown — proceed and let read attempts surface it

    text: str | None = None
    direct_err: str | None = None
    try:
        text = path.read_text(encoding="utf-8")
    except (PermissionError, FileNotFoundError) as e:
        direct_err = f"{type(e).__name__}: {e}"
    except OSError as e:
        # Other OSError subclasses (e.g. macOS sometimes raises plain
        # OSError for ACL-denied dir traversal). Still try the sudo
        # fallback before giving up.
        direct_err = f"{type(e).__name__}: {e}"

    if text is None:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                text = r.stdout
            else:
                # Surface the sudoers / cat failure so the operator can
                # diagnose. The sudoers grant lives in
                # /etc/sudoers.d/evolve; missing entry → exit 1 with a
                # diagnostic on stderr.
                if file_exists is False:
                    return None, f"{path} does not exist (bot may be partially deployed)"
                stderr_tail = (r.stderr or "").strip()[:200]
                return None, (
                    f"{path} unreadable. Direct read: {direct_err}. "
                    f"sudo /bin/cat exited {r.returncode}: {stderr_tail or '(no stderr)'}. "
                    f"Check /etc/sudoers.d/evolve and the ACL on {path.parent}."
                )
        except subprocess.TimeoutExpired:
            return None, f"{path} read timed out (sudo /bin/cat hung > 10s)"
        except FileNotFoundError:
            return None, f"sudo binary not found; cannot fall back from direct read ({direct_err})"
        except OSError as e:
            return None, f"sudo /bin/cat failed to launch: {e}. Direct read: {direct_err}"

    if not text:
        return None, f"{path} returned empty content (direct: {direct_err})"

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"{path} is not valid JSON: {e}"

    if not isinstance(parsed, dict):
        return None, f"{path} top-level is {type(parsed).__name__}, expected object"

    return parsed, ""


# ─────────────────────────────────────────────────────────────────────────────
# Per-bot migration
# ─────────────────────────────────────────────────────────────────────────────


def migrate_bot(
    bot_id: str,
    network: dict[str, Any],
    *,
    defaults_registry: dict[str, Any],
    shared_dir: Path = DEFAULT_SHARED_DIR,
    now: datetime | None = None,
    dry_run: bool = False,
) -> BotMigrationResult:
    """Migrate one bot's openclaw.json deviations into overrides.

    Idempotent (skips keys already represented as overrides). Schema-
    invalid drift is logged and skipped (the first redeploy will strip
    these via the materializer's stale-key prune).

    ``dry_run=True`` computes the migration but doesn't write — useful for
    preflight inspection before a bulk run.
    """
    result = BotMigrationResult(bot_id=bot_id)

    parsed, err = _read_openclaw_json(bot_id, network)
    if parsed is None:
        result.error = err
        return result

    # Defensive dict-walk: a corrupted openclaw.json could have
    # ``plugins`` set to a list or a scalar, in which case naive
    # ``.get()`` chaining raises AttributeError. We've already verified
    # ``parsed`` is a dict in ``_read_openclaw_json``; descend by
    # checking each level.
    plugins = parsed.get("plugins")
    if not isinstance(plugins, dict):
        result.error = "plugins is not an object — openclaw.json malformed"
        return result
    entries = plugins.get("entries")
    if not isinstance(entries, dict):
        result.error = "plugins.entries is not an object — openclaw.json malformed"
        return result
    evolve = entries.get("evolve")
    if not isinstance(evolve, dict):
        # Bot has no evolve plugin entry at all → nothing to migrate.
        # This is a legitimate state (fresh bot, brave-only bot, etc.);
        # not an error.
        return result
    current_block = evolve.get("config", {})
    if not isinstance(current_block, dict):
        result.error = "plugins.entries.evolve.config is not an object"
        return result

    field_to_schema_path = _build_field_to_schema_path()
    try:
        bot_overrides = read_bot_overrides(shared_dir, bot_id)
    except OverrideValidationError as e:
        # bot_id failed the regex check inside path_for_bot — invalid
        # name shape. Record once at the bot level, not per-field, so the
        # operator sees the actual problem.
        result.error = f"invalid bot_id: {e.reason}"
        return result

    # Iterate fields that have a defaults_registry entry. Schema-known
    # fields without a default (e.g. dashboardEnabled) are
    # deploy-computed and not Migration A's business.
    for field_name, schema_path in field_to_schema_path.items():
        # Defensive: skip identity fields explicitly even if someone
        # later adds them to defaults_registry — they should never be
        # represented as per-bot overrides. The materializer applies the
        # same skip; mirroring keeps the two layers in lockstep.
        if field_name in _IDENTITY_FIELDS:
            continue
        if field_name not in defaults_registry:
            continue
        if field_name not in current_block:
            continue
        current_val = current_block[field_name]
        if bot_overrides.get(schema_path) is not None:
            result.already_overridden.append(field_name)
            continue
        default_val = defaults_registry.get(field_name)
        if current_val == default_val:
            continue   # not a divergence

        if dry_run:
            result.promoted.append(field_name)
            continue

        try:
            write_override(
                shared_dir,
                bot_id,
                schema_path,
                current_val,
                set_by=MIGRATION_TAG,
                note="Auto-recorded by Migration A; review.",
                needs_review=True,
                now=now,
            )
            result.promoted.append(field_name)
        except OverrideValidationError as e:
            # Schema-invalid (wrong type, malformed enum, etc.) — likely
            # the operator wrote a typo or the schema tightened since
            # the value was set. Migration A leaves it alone; the
            # materializer's stale-key prune will revert on next deploy.
            # Preserve the value so the operator can see what was there.
            result.schema_invalid.append((field_name, current_val, e.reason))
        except OverrideStateError as e:
            # Overrides file is malformed. Abort this bot — every
            # subsequent write would hit the same error.
            result.error = (
                f"overrides file unreadable, aborting migration for "
                f"this bot: {e}"
            )
            return result
        except Exception as e:
            result.persistence_failed.append(
                (field_name, f"{type(e).__name__}: {e}")
            )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Pod-wide migration
# ─────────────────────────────────────────────────────────────────────────────


def migrate_all_bots(
    network: dict[str, Any] | None = None,
    *,
    defaults_registry: dict[str, Any] | None = None,
    shared_dir: Path = DEFAULT_SHARED_DIR,
    dry_run: bool = False,
    now: datetime | None = None,
) -> MigrationResult:
    """Run Migration A across every bot in the network.

    ``defaults_registry`` defaults to ``deploy._PLUGIN_CONFIG_DEFAULTS``.
    Pass an explicit dict to migrate against a different baseline (e.g.
    for testing or to simulate a future schema). ``network`` defaults to
    the result of ``load_network()``.

    Never raises into the caller — per-bot failures land on
    ``BotMigrationResult.error``.
    """
    if network is None:
        network = load_network()
    if defaults_registry is None:
        defaults_registry = _DEFAULTS_REGISTRY

    bots = (network.get("bots") or {})
    aggregate = MigrationResult()

    for bot_id in sorted(bots.keys()):
        try:
            r = migrate_bot(
                bot_id,
                network,
                defaults_registry=defaults_registry,
                shared_dir=shared_dir,
                dry_run=dry_run,
                now=now,
            )
        except Exception as e:
            r = BotMigrationResult(bot_id=bot_id, error=f"{type(e).__name__}: {e}")
        aggregate.per_bot[bot_id] = r

    return aggregate


# ─────────────────────────────────────────────────────────────────────────────
# Operator-facing summary
# ─────────────────────────────────────────────────────────────────────────────


def format_migration_summary(result: MigrationResult) -> str:
    """Render a human-readable summary of the migration. Used by the CLI."""
    lines: list[str] = []
    lines.append(
        f"Migration A — populated {result.total_promoted()} override(s) "
        f"across {len(result.per_bot)} bot(s)."
    )
    lines.append("")

    for bot_id in sorted(result.per_bot.keys()):
        r = result.per_bot[bot_id]
        lines.append(f"== {bot_id} ==")
        if r.error:
            lines.append(f"  ERROR: {r.error}")
            continue
        if r.promoted:
            lines.append(
                f"  Promoted ({len(r.promoted)}, flagged needs_review):"
            )
            for k in r.promoted:
                lines.append(f"    - {k}")
        if r.already_overridden:
            lines.append(
                f"  Already overridden ({len(r.already_overridden)}; skipped):"
            )
            for k in r.already_overridden:
                lines.append(f"    - {k}")
        if r.schema_invalid:
            lines.append(
                f"  ⚠ Schema-invalid drift ({len(r.schema_invalid)}; "
                f"REVERTED to default on next deploy unless you fix "
                f"openclaw.json first):"
            )
            for k, val, reason in r.schema_invalid:
                lines.append(f"    - {k}={val!r}: {reason}")
        if r.persistence_failed:
            lines.append(
                f"  WARN: persistence failed for {len(r.persistence_failed)} key(s):"
            )
            for k, reason in r.persistence_failed:
                lines.append(f"    - {k}: {reason}")
        if not (r.promoted or r.already_overridden or r.schema_invalid or r.persistence_failed):
            lines.append("  (no drift detected — bot is at defaults)")
        lines.append("")

    # Pull schema-invalid entries to a top-level WARN section so they
    # don't get buried in per-bot detail. These are values the
    # materializer will REVERT on next deploy; the operator should fix
    # them in openclaw.json first or accept the revert.
    schema_invalid_total = sum(
        len(r.schema_invalid) for r in result.per_bot.values()
    )
    if schema_invalid_total:
        lines.append("─── ⚠ Action required: schema-invalid values ──────────")
        lines.append(
            f"{schema_invalid_total} value(s) across "
            f"{sum(1 for r in result.per_bot.values() if r.schema_invalid)} "
            f"bot(s) will be DELETED from openclaw.json on next deploy "
            f"because they don't match the current plugin configSchema. "
            f"Fix them in the source openclaw.json before redeploying if "
            f"you want to keep the operator intent; otherwise the "
            f"materializer will revert to the shipped default."
        )
        for bot_id in sorted(result.per_bot.keys()):
            r = result.per_bot[bot_id]
            for k, val, reason in r.schema_invalid:
                lines.append(f"  - {bot_id}: {k}={val!r} → {reason}")
        lines.append("")

    if result.total_promoted() == 0 and not result.bots_with_errors():
        lines.append(
            "Nothing to review. Every bot's plugins.entries.evolve.config "
            "block already matched defaults or had overrides recorded."
        )
    else:
        lines.append(
            "Next steps: open the Customizations page (or inspect "
            f"{DEFAULT_SHARED_DIR}/sandbox/overrides/<bot>.json) to "
            "review each needs_review=True entry. Accept, annotate, or "
            "revert as appropriate. The materializer in subsequent "
            "deploys will honor whichever decision you make."
        )

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point — ``python3 -m evolve_admin.openclaw_migration``
# ─────────────────────────────────────────────────────────────────────────────


def _cli_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="evolve-admin openclaw-migrate",
        description=(
            "Migration A — populate each bot's per-bot overrides file with "
            "the divergences between its current openclaw.json and the "
            "shipped defaults. Idempotent. See spec-openclaw-json-derived-"
            "artifact-2026-05-24.md §9."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute the migration but don't write any overrides.",
    )
    parser.add_argument(
        "--shared-dir", type=Path, default=DEFAULT_SHARED_DIR,
        help="Path to the evolve shared dir (default: %(default)s).",
    )
    args = parser.parse_args(argv)

    result = migrate_all_bots(
        shared_dir=args.shared_dir,
        dry_run=args.dry_run,
    )
    print(format_migration_summary(result))

    if result.bots_with_errors():
        return 1
    return 0


if __name__ == "__main__":   # pragma: no cover
    import sys
    sys.exit(_cli_main(sys.argv[1:]))
