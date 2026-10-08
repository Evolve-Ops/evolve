"""PromoteApp applier — the server-side half of promotion (AL-1.7).

Design ``design-app-spec-and-discovery-2026-08-15.md`` §4 (the
discovered → defined edge) and **§7.2**, whose trust boundary this module *is*:

> "the promotion action executes server-side — the bot's LLM never holds a
> privileged tool, so the trust boundary holds."

The bot pitches the offer in the user's own channel; the user's "yes" is
classified server-side; the approval moves the Proposal; and *this* applier
performs the mutation. Nothing on the bot side can reach the manifest.

WHAT IT WRITES, and what it deliberately does not:

* ``definition_status`` → ``defined``, plus the anchored-identity field marks,
  via ``applications.coherence_actions.promote_to_defined`` — the SAME pure
  mutation the operator's UI promote route calls. Two promotion surfaces
  writing two different shapes of "defined" is how a lifecycle acquires a
  second, undocumented state; there is one mutation and both call it.
* ``app_id``, from the action. The decision (adopt vs confer) was made by
  ``evolve_admin.applications.app_promotion.adopt_or_confer_app_id`` at proposal
  time and travels on the Action — the applier never re-derives it, so a
  proposal approved a week after it was minted confers exactly the id the user
  was shown.
* The ``draft_id`` is **cleared**, because design §3/§4 make ``draft_id`` the
  positive record that identity was declined. Leaving it beside a conferred
  ``app_id`` would leave the manifest asserting both at once, and
  ``app_identity.ensure_app_id`` explicitly refuses to backfill over a
  ``draft_id`` — a promoted app that kept one would have its id treated as
  provisional by every later backfill.

Reversible: ``capture_snapshot`` stores the whole prior manifest and ``revert``
restores it byte-for-byte, the shape ``deprecate_app`` uses.
"""

from __future__ import annotations

import json
from pathlib import Path

from evolve_util import atomic_write_json as _atomic_write_json

from arbiter.appliers.base import (
    ApplyResult,
    RevertResult,
    register_applier,
)
from schema.proposal import PromoteApp

from evolve_config import bot_home as _bot_home


def _manifest_path(bot_id: str, stem: str) -> Path:
    """Mirror ``deprecate_app._manifest_path`` — the same manifests dir.

    ``stem`` is the on-disk filename stem carried on the Action, NOT the app id:
    the two diverge on gallery-installed v7-arc-pre manifests, and a mutation
    endpoint must write back to the file it read.
    """
    return _bot_home(bot_id) / ".openclaw" / "workspace" / "manifests" / f"{stem}.json"


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


class PromoteAppApplier:
    def capture_snapshot(self, action: PromoteApp, bot_id: str) -> dict:
        path = _manifest_path(bot_id, action.manifest_stem)
        return {
            "action_kind": "PromoteApp",
            "path": str(path),
            "prior_manifest": _read_json(path),
        }

    def apply(self, action: PromoteApp, bot_id: str) -> ApplyResult:
        path = _manifest_path(bot_id, action.manifest_stem)
        manifest = _read_json(path)
        if manifest is None:
            return ApplyResult(
                ok=False,
                details={"reason": "manifest_missing"},
                message=f"no manifest at {path}",
            )
        if not action.app_id:
            # app_promotion.adopt_or_confer_app_id returns mode="blocked" with an
            # empty id when it cannot produce one. Refusing here rather than
            # minting a fallback is the point: an app with a fabricated identity
            # is worse than an app that stayed discovered.
            return ApplyResult(
                ok=False,
                details={"reason": "no_app_id"},
                message="PromoteApp carries no app_id; refusing to invent one",
            )

        # Lazily imported (and type-ignored) exactly like the sibling appliers
        # reaching across into evolve_admin — ``build_app``/``forge_sweep`` do the
        # same. The mutation itself must be the admin one; see the module
        # docstring on one mutation, two surfaces.
        from evolve_admin.applications.coherence_actions import (  # type: ignore
            promote_to_defined,
        )
        from evolve_admin.applications.app_identity import (  # type: ignore
            APP_ID_FIELD,
            DRAFT_ID_FIELD,
        )

        existing = manifest.get(APP_ID_FIELD)
        if (
            isinstance(existing, str)
            and existing.strip()
            and existing.strip() != action.app_id
        ):
            # Identity is immutable once conferred (design §3). An action whose
            # app_id disagrees with the manifest's is a stale proposal racing a
            # change, not a rename — refuse instead of re-identifying, which is
            # the exact failure §7.3a's ADOPT decision exists to prevent.
            return ApplyResult(
                ok=False,
                details={
                    "reason": "app_id_conflict",
                    "on_manifest": existing.strip(),
                    "on_action": action.app_id,
                },
                message=(
                    f"manifest already carries app_id {existing.strip()!r}; "
                    f"refusing to re-identify it as {action.app_id!r}"
                ),
            )

        manifest[APP_ID_FIELD] = action.app_id
        # A promoted app is no longer a draft. See the module docstring.
        manifest.pop(DRAFT_ID_FIELD, None)
        if action.app_audience:
            manifest["audience"] = action.app_audience
        result = promote_to_defined(manifest, by="proposal:app_promotion")

        try:
            _atomic_write_json(path, manifest, sort_keys=True)
        except OSError as e:
            return ApplyResult(ok=False, message=f"write failed: {e}")
        return ApplyResult(
            ok=True,
            details={
                "app_id": action.app_id,
                "identity_mode": action.identity_mode,
                "audience": action.app_audience,
                **{k: v for k, v in result.items() if k != "manifest"},
            },
            message=f"promoted {action.app_id} ({action.identity_mode})",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        path = Path(snapshot.get("path", ""))
        prior = snapshot.get("prior_manifest")
        if prior is None:
            try:
                if path.exists():
                    path.unlink()
            except OSError as e:
                return RevertResult(ok=False, message=f"delete failed: {e}")
            return RevertResult(ok=True, message=f"deleted {path}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(path, prior, sort_keys=True)
        except OSError as e:
            return RevertResult(ok=False, message=f"restore failed: {e}")
        return RevertResult(ok=True, message=f"restored {path}")


register_applier("PromoteApp", PromoteAppApplier())  # type: ignore[arg-type]
