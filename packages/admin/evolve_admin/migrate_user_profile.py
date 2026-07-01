"""One-shot migration from v1 user-profile storage to v2.

Spec: docs/spec-user-profile-2026-05-07.md (Migration & removals).

What changes:

* **From** ``{shared_dir}/profiles/users/<user_key>.json`` (the wizard's
  legacy flat-JSON store) **to** ``~/<bot>/.openclaw/profiles/<user_key>.md``
  (v2 bot-side per-user markdown).
* ``{shared_dir}/profiles/<bot_id>.md`` is **left in place**. Its
  frontmatter still holds the bot-admin config (archetype, surfacing
  cadence, timezone) read by the admin's bot-setup endpoint; the user-
  content sections in the body are vestigial (the inferrer that would
  have populated them was scaffolded but never built) and we ignore
  them. A future cleanup can split admin-config storage into its own
  file; for now, the v1 file is repurposed as bot-admin-config-only.
* Writes ``~/<bot>/.openclaw/profiles/.status.json`` for every bot (so
  the admin endpoint's primary read path has data).

How it runs:

* Idempotent. Already-archived files are left alone; bots that already
  have a bot-side profile aren't overwritten unless the legacy data is
  meaningfully different.
* Best-effort per item — a single failure logs a warning and the
  migration continues with other items.
* All bot-side writes go through the existing
  ``user_profile_writer.write_user_profile_via_sudo`` path. Tests inject
  the same subprocess seam used by the writer's own tests.

Invoke from the host (as the evolve user, since the writer needs sudo
grants that the evolve sudoers file provides):

    sudo -u evolve python3 -m evolve_admin.migrate_user_profile

Or programmatically:

    from evolve_admin.migrate_user_profile import migrate_pod
    report = migrate_pod()
    print(report.summary())
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import bot_home, get_bot_user, load_network, DEFAULT_NETWORK_CONFIG
from .evo.wizard.user_profile_writer import (
    build_profile_from_extracted,
    read_user_profile_from_bot,
    write_status_only_via_sudo,
    write_user_profile_via_sudo,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class MigrationReport:
    json_profiles_migrated: list[str] = field(default_factory=list)  # user_keys
    json_profiles_skipped: dict[str, str] = field(default_factory=dict)  # user_key -> reason
    write_failures: dict[str, str] = field(default_factory=dict)  # key -> error

    def summary(self) -> str:
        lines = [
            f"User-JSON profiles migrated: {len(self.json_profiles_migrated)}",
            f"User-JSON profiles skipped:  {len(self.json_profiles_skipped)}",
            f"Write failures:              {len(self.write_failures)}",
        ]
        if self.json_profiles_skipped:
            lines.append("\nSkipped JSON profiles:")
            for k, why in self.json_profiles_skipped.items():
                lines.append(f"  - {k}: {why}")
        if self.write_failures:
            lines.append("\nWrite failures:")
            for k, err in self.write_failures.items():
                lines.append(f"  - {k}: {err}")
        return "\n".join(lines)

    def has_failures(self) -> bool:
        return bool(self.write_failures)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry
# ─────────────────────────────────────────────────────────────────────────────


def migrate_pod(
    *,
    network_path: Path = DEFAULT_NETWORK_CONFIG,
    archive_subdir: str | None = None,
) -> MigrationReport:
    """Run the migration. Idempotent.

    Args:
        network_path: where network.json lives. Defaults to the production
            location.
        archive_subdir: subdir name under ``{shared_dir}/profiles/_archive/``
            to drop archived files into. Defaults to ISO date — passing
            an explicit name keeps tests deterministic.
    """
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))

    if archive_subdir is None:
        archive_subdir = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive_root = shared_dir / "profiles" / "_archive" / archive_subdir
    archive_root.mkdir(parents=True, exist_ok=True)

    report = MigrationReport()

    # 1. Migrate per-user JSON store
    _migrate_user_jsons(
        shared_dir=shared_dir,
        network=network,
        archive_root=archive_root,
        report=report,
    )

    # 2. (Removed.) {shared_dir}/profiles/<bot_id>.md files are NOT
    # archived — their frontmatter is still load-bearing for bot-admin
    # config. See the module docstring.

    # 3. Ensure each bot has a .status.json so the admin endpoint's
    # primary read path has a definite answer (even if empty).
    _ensure_status_files(
        network=network,
        report=report,
    )

    return report


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: per-user JSON → v2 bot-side
# ─────────────────────────────────────────────────────────────────────────────


def _migrate_user_jsons(
    *,
    shared_dir: Path,
    network: dict[str, Any],
    archive_root: Path,
    report: MigrationReport,
) -> None:
    src = shared_dir / "profiles" / "users"
    if not src.exists():
        return

    archive_users = archive_root / "users"
    archive_users.mkdir(parents=True, exist_ok=True)

    member_ids = _network_member_ids(network)

    for json_path in sorted(src.glob("*.json")):
        user_key_safe = json_path.stem  # this is the safe-encoded form

        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            report.json_profiles_skipped[user_key_safe] = f"unreadable: {e}"
            continue

        if not isinstance(data, dict):
            report.json_profiles_skipped[user_key_safe] = "not a dict"
            continue

        bot_id = data.get("last_wizard_bot")
        if not isinstance(bot_id, str) or not bot_id:
            report.json_profiles_skipped[user_key_safe] = "no last_wizard_bot"
            continue
        if bot_id not in member_ids:
            report.json_profiles_skipped[user_key_safe] = (
                f"bot {bot_id!r} not in network.json members"
            )
            continue

        # Recover the original user_key from the JSON itself (the filename
        # form is filesystem-safe but lossy on first :__ : rewrite).
        user_key = data.get("user_key") or user_key_safe

        # Some legacy fields are wizard scratch — strip them. The mapper's
        # whitelist drops anything not in its known set, so this is mostly
        # belt-and-suspenders, but it keeps the audit log entry concise.
        extracted = {
            k: v
            for k, v in data.items()
            if k in {
                "name",
                "role",
                "environment",
                "current_tooling",
                "current_workflow_notes",
                "top_goals",
                "pain_points",
            }
        }

        try:
            home = bot_home(bot_id, network)
            bot_user = get_bot_user(bot_id, network)
        except Exception as e:  # noqa: BLE001
            report.json_profiles_skipped[user_key_safe] = (
                f"cannot resolve bot home for {bot_id!r}: {e}"
            )
            continue

        # Read existing profile (in case the wizard has run since migration
        # was prepared) so we don't blow away newer content.
        existing = read_user_profile_from_bot(bot_home=home, user_key=user_key)
        try:
            profile = build_profile_from_extracted(
                extracted=extracted,
                user_key=user_key,
                bot_id=bot_id,
                existing=existing,
            )
        except ValueError as e:
            report.write_failures[user_key_safe] = f"build failed: {e}"
            continue

        if not profile.has_content():
            # Nothing to write — the JSON had no meaningful fields. Still
            # archive the source file so we don't keep retrying.
            _archive_file(json_path, archive_users / json_path.name, report)
            report.json_profiles_skipped[user_key_safe] = "json had no content"
            continue

        try:
            write_user_profile_via_sudo(
                profile, bot_home=home, bot_user=bot_user
            )
        except Exception as e:  # noqa: BLE001
            report.write_failures[user_key_safe] = f"write failed: {e}"
            continue

        report.json_profiles_migrated.append(user_key_safe)
        _archive_file(json_path, archive_users / json_path.name, report)


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3: ensure .status.json exists for each bot
# ─────────────────────────────────────────────────────────────────────────────


def _ensure_status_files(
    *,
    network: dict[str, Any],
    report: MigrationReport,
) -> None:
    """For each bot, ensure ``.status.json`` exists. If a profile already
    exists for any user, ``write_user_profile_via_sudo`` has already
    written the status. This stage handles bots with NO user profiles
    (the common case for un-wizarded bots) by writing an empty status
    file directly so the admin endpoint can read a definite answer.
    """
    for bot_id in _network_member_ids(network):
        try:
            home = bot_home(bot_id, network)
            bot_user = get_bot_user(bot_id, network)
        except Exception as e:  # noqa: BLE001
            report.write_failures[f"<status:{bot_id}>"] = f"resolve failed: {e}"
            continue

        status_path = home / ".openclaw" / "profiles" / ".status.json"
        if status_path.exists():
            # Either a wizard wrote a profile and update_status ran, or
            # a previous migration run created the file. Don't clobber.
            continue

        try:
            write_status_only_via_sudo(
                bot_home=home,
                bot_user=bot_user,
                any_has_content=False,
            )
        except Exception as e:  # noqa: BLE001
            report.write_failures[f"<status:{bot_id}>"] = f"write status failed: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _network_member_ids(network: dict[str, Any]) -> set[str]:
    raw = network.get("members") or []
    out: set[str] = set()
    for m in raw:
        if isinstance(m, str):
            out.add(m)
        elif isinstance(m, dict):
            mid = m.get("id") or m.get("botId")
            if mid:
                out.add(str(mid))
    return out


def _archive_file(src: Path, dest: Path, report: MigrationReport) -> None:
    """Move ``src`` to ``dest``. If ``dest`` already exists, leave both alone
    (idempotent — the file was already archived in a prior run)."""
    if dest.exists():
        try:
            src.unlink(missing_ok=True)
        except OSError:
            pass
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dest))
    except OSError as e:
        report.write_failures[f"<archive:{src.name}>"] = f"move failed: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network-path",
        type=Path,
        default=DEFAULT_NETWORK_CONFIG,
        help=f"Path to network.json (default: {DEFAULT_NETWORK_CONFIG})",
    )
    parser.add_argument(
        "--archive-subdir",
        default=None,
        help="Subdir name under profiles/_archive/ (default: today's date)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    report = migrate_pod(
        network_path=args.network_path,
        archive_subdir=args.archive_subdir,
    )
    print(report.summary())
    return 1 if report.has_failures() else 0


if __name__ == "__main__":
    sys.exit(main())
