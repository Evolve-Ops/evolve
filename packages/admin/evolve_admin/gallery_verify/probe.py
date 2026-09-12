"""The read-only pod-inspection seam.

Everything the post-install / teardown assertions need to *observe* the pod,
behind the ``PodProbe`` protocol so tests inject a fixture-backed fake. The
live implementation reads directly (the ``evolve`` user has ACL read on the
bot ``.openclaw`` dirs + owns ``{shared_dir}``) and shells out only through
the platform-keyed scheduler factory — never a hardcoded ``launchctl`` /
``systemctl`` path.

Assertions operate on plain dicts/strings returned here, decoupled from the
manifest/recon dataclasses, so a fixture is just a literal.
"""

# identity: see applications.app_identity.resolve_app_id (AL-1.4b). Every ``pkg_id`` here is the GALLERY PACKAGE key
# this harness sweeps by — a function parameter threaded to load_gallery_package
# / preflight_check, not a manifest read. ``app_manifest`` is the one place a
# manifest is touched, and it MATCHES ``m.pkg_id == pkg_id`` on purpose: the
# caller holds a package key and wants the install of THAT package, so a
# resolved id would match a scanner-born manifest that was never installed
# from the gallery at all.

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class PodProbe(Protocol):
    platform: str  # "macos" | "linux"
    shared_dir: Path

    def gallery_packages(self) -> list[dict]: ...
    def gallery_package(self, pkg_id: str) -> dict | None: ...
    def preflight(self, pkg_id: str, bot_id: str) -> dict: ...
    def app_manifest(self, pkg_id: str, bot_id: str) -> dict | None: ...
    def recon_buckets(self, bot_id: str) -> dict[str, list[dict]]: ...
    def covered_ids(self, bot_id: str) -> set[str]: ...
    def scheduler_status(self, label: str) -> dict: ...
    def delivery_state(self) -> dict: ...
    def installed_apps_md(self, bot_id: str) -> str: ...
    def visible_manifests(self, bot_id: str) -> list[dict]: ...
    def heartbeat_sections(self, bot_id: str) -> list[dict]: ...
    def path_exists(self, path: str) -> bool: ...
    def bot_workspace(self, bot_id: str) -> str | None: ...


class LivePodProbe:
    """Real pod reader. All heavy imports are lazy so this module loads
    off-pod (the pure pipeline/assertion code imports the protocol, not this).
    """

    def __init__(self, shared_dir: Path | None = None):
        from .. import config  # noqa: PLC0415

        self.shared_dir = Path(shared_dir) if shared_dir else Path(config.DEFAULT_SHARED_DIR)
        self.platform = self._detect_platform()

    @staticmethod
    def _detect_platform() -> str:
        try:
            from platform_profile import get_profile  # noqa: PLC0415

            return get_profile().name
        except Exception:
            return "macos"

    def gallery_packages(self) -> list[dict]:
        from ..applications.gallery import list_gallery_packages  # noqa: PLC0415

        return list_gallery_packages(self.shared_dir, [])

    def gallery_package(self, pkg_id: str) -> dict | None:
        """Full per-package JSON (the index rows from ``gallery_packages`` are
        denormalized and do NOT carry ``app_dependencies``)."""
        from ..applications.gallery import load_gallery_package  # noqa: PLC0415

        try:
            return load_gallery_package(pkg_id, self.shared_dir)
        except Exception:
            return None

    def preflight(self, pkg_id: str, bot_id: str) -> dict:
        from ..applications.gallery import preflight_check  # noqa: PLC0415

        return preflight_check(pkg_id, bot_id, self.shared_dir)

    def app_manifest(self, pkg_id: str, bot_id: str) -> dict | None:
        """Find the installed manifest for *pkg_id* on *bot_id* by identity
        (pkg_id is stable across installs; the filename slug is not), return
        its full serialized dict.
        """
        from ..applications.manifest import list_manifests  # noqa: PLC0415

        try:
            manifests = list_manifests(self.shared_dir, bot_id)
        except Exception:
            return None
        for m in manifests:
            if getattr(m, "pkg_id", "") == pkg_id:
                return m.to_dict()
        return None

    def recon_buckets(self, bot_id: str) -> dict[str, list[dict]]:
        """In-memory (non-persisting) recon over just *bot_id* — cheap, scoped,
        and never mutates the pod's shared ledger.
        """
        from ..applications.recon_ledger import build_recon_ledger  # noqa: PLC0415

        result = build_recon_ledger(self.shared_dir, [bot_id], persist=False)
        return result.to_dict().get("buckets", {})

    def covered_ids(self, bot_id: str) -> set[str]:
        from ..applications.app_integrity_coverage import load_covered_ids  # noqa: PLC0415

        try:
            return load_covered_ids(self.shared_dir, bot_id)
        except Exception:
            return set()

    def scheduler_status(self, label: str) -> dict:
        from ..runtime import get_scheduler  # noqa: PLC0415

        try:
            return get_scheduler().status(label)
        except Exception as exc:
            return {"installed": False, "managed": False, "status_error": f"probe_failed: {exc}"}

    def delivery_state(self) -> dict:
        try:
            from delivery_monitor import load_state  # noqa: PLC0415

            return load_state(self.shared_dir)
        except Exception:
            return {}

    def installed_apps_md(self, bot_id: str) -> str:
        from ..applications.manifest import applications_dir  # noqa: PLC0415

        # INSTALLED_APPS.md sits alongside the manifests dir's parent (workspace).
        try:
            workspace = applications_dir(self.shared_dir, bot_id).parent
            return (workspace / "INSTALLED_APPS.md").read_text(encoding="utf-8")
        except Exception:
            return ""

    def visible_manifests(self, bot_id: str) -> list[dict]:
        """Serialized manifests that render into the capability index
        (INSTALLED_APPS.md) — the same status filter ``app_registry`` uses.
        Lets the teardown residue check attribute an index mention to a
        different, legitimately-installed app with the same display name
        (live false positive: the Journal name collision, 2026-07-11 sweep).
        """
        from ..applications.app_registry import _VISIBLE_STATUSES  # noqa: PLC0415
        from ..applications.manifest import list_manifests  # noqa: PLC0415

        try:
            return [
                m.to_dict() for m in list_manifests(self.shared_dir, bot_id)
                if (getattr(m, "status", "") or "active") in _VISIBLE_STATUSES
            ]
        except Exception:
            return []

    def heartbeat_sections(self, bot_id: str) -> list[dict]:
        try:
            from ..applications.scanner import _read_heartbeat_md_sections  # noqa: PLC0415

            return _read_heartbeat_md_sections(bot_id)
        except Exception:
            return []

    def path_exists(self, path: str) -> bool:
        try:
            return Path(path).exists()
        except Exception:
            return False

    def bot_workspace(self, bot_id: str) -> str | None:
        from ..config import get_bot_workspace  # noqa: PLC0415

        try:
            ws = get_bot_workspace(bot_id)
        except Exception:
            return None
        return str(ws) if ws else None
