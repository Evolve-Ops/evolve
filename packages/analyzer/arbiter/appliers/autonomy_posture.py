"""arbiter.appliers.autonomy_posture — apply ``UpdateAutonomyPosture``.

Spec: docs/spec-autonomy-ladder-2026-06-10.md §3.2 step 3.

Applying an approved proposal IS the deliberate operator act: the
applier writes through the same ``autonomy.store.set_posture`` API the
Permissions-tab UI uses, with ``set_by: proposal:<id>``, then
re-renders enforcement (deny-slice merge + gateway kickstart) exactly
like the UI route.

The CAS discipline is load-bearing here: ``expected_current_rung``
travels on the action from authoring time, so a posture that moved
while the proposal sat in the queue fails the apply loudly
(``stale_posture``) instead of silently clobbering a newer decision —
and it guarantees the promotion-vs-demotion direction every
auto-approve lane computed from the action still holds at apply time.

This applier does NOT decide who may apply — the upward-promotion
auto-approve exclusions live in ``arbiter.routing``, ``eligibility``,
and evo's apply tool. By the time apply() runs, a human (or, for
demotions only, an auto lane) has already approved.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from arbiter.appliers.base import ApplyResult, RevertResult, register_applier
from schema.proposal import UpdateAutonomyPosture


def _shared_dir() -> Path:
    import os
    from evolve_config import CANONICAL_SHARED_DIR
    env = os.environ.get("EVOLVE_SHARED_DIR")
    if env and Path(env).exists():
        return Path(env)
    return CANONICAL_SHARED_DIR


class UpdateAutonomyPostureApplier:
    """Write a posture change through autonomy.store + renderer.

    Test hooks: ``shared_override`` points the posture store at a test
    tree; ``home_override`` makes the renderer read/write a synthetic
    ``.openclaw/openclaw.json`` instead of the production sudo path.
    """

    home_override: Path | None = None
    shared_override: Path | None = None

    def _resolve_shared(self) -> Path:
        return self.shared_override or _shared_dir()

    def capture_snapshot(self, action: UpdateAutonomyPosture, bot_id: str) -> dict:
        from autonomy import store as _astore

        target_bot = action.bot_id or bot_id
        shared = self._resolve_shared()
        prior: dict = {}
        try:
            doc = _astore.load(shared, target_bot)
        except ValueError:
            doc = None
        posture = doc.integrations.get(action.integration_id) if doc else None
        if posture is not None:
            prior = {
                "rung": posture.rung,
                "rules": deepcopy(posture.rules),
                "kind": posture.kind,
                "set_by": dict(posture.set_by or {}),
            }
        return {
            "action_kind": "UpdateAutonomyPosture",
            "bot_id": target_bot,
            "integration_id": action.integration_id,
            "prior_posture": prior,
            # CAS witness for revert: the rung this apply is about to
            # write. A posture that moved again after apply (operator
            # action, auto-demotion) must fail the revert loudly, not
            # be clobbered back.
            "applied_rung": action.rung,
        }

    def apply(
        self,
        action: UpdateAutonomyPosture,
        bot_id: str,
        *,
        proposal_id: str | None = None,
    ) -> ApplyResult:
        from autonomy import renderer as _arenderer
        from autonomy import store as _astore

        target_bot = action.bot_id or bot_id
        shared = self._resolve_shared()
        actor = f"{_astore.ACTOR_PREFIX_PROPOSAL}{proposal_id or 'unknown'}"

        if not action.integration_id:
            return ApplyResult(
                ok=False, details={"error": "missing_integration_id"},
                message="UpdateAutonomyPosture.integration_id is required.",
            )
        if action.expected_current_rung is None:
            # Without the CAS witness the direction every lane computed
            # is unverifiable — refuse rather than apply blind.
            return ApplyResult(
                ok=False, details={"error": "missing_expected_current_rung"},
                message=(
                    "UpdateAutonomyPosture.expected_current_rung is required "
                    "(CAS guard + direction witness)."
                ),
            )

        try:
            posture = _astore.set_posture(
                shared, target_bot, action.integration_id,
                rung=action.rung,
                rules=dict(action.rules or {}),
                actor=actor,
                note=action.note or "applied via approved proposal",
                expected_current_rung=action.expected_current_rung,
            )
        except _astore.StalePostureError as exc:
            return ApplyResult(
                ok=False, details={"error": "stale_posture"},
                message=(
                    f"Posture changed since the proposal was created: {exc} "
                    "The newer decision wins; this proposal should be "
                    "dismissed or re-generated."
                ),
            )
        except ValueError as exc:
            return ApplyResult(
                ok=False, details={"error": "invalid_posture", "exc": str(exc)},
                message=f"Refused: {exc}",
            )

        render = _arenderer.render_bot(
            target_bot, shared, home_override=self.home_override,
        )
        details = {
            "bot_id": target_bot,
            "integration_id": action.integration_id,
            "rung": posture.rung,
            "rules": dict(posture.rules),
            "set_by": dict(posture.set_by),
            "render_changed": render.changed,
            "render_written": render.written,
        }
        if render.write_error:
            # Posture recorded; enforcement render failed. Same contract
            # as the UI route: surface the gap, drift check flags it,
            # next deploy repairs it. The apply itself still succeeded.
            details["render_error"] = render.write_error
            return ApplyResult(
                ok=True, details=details,
                message=(
                    f"Posture set on {target_bot}/{action.integration_id} "
                    f"(rung={posture.rung}), but applying it to the bot "
                    f"failed: {render.write_error}. The next deploy "
                    "re-applies it; the drift check flags it until then."
                ),
            )
        return ApplyResult(
            ok=True, details=details,
            message=(
                f"Set {target_bot}/{action.integration_id} to "
                f"{posture.rung} and re-rendered enforcement."
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        from autonomy import renderer as _arenderer
        from autonomy import store as _astore

        target_bot = snapshot.get("bot_id") or bot_id
        integration_id = snapshot.get("integration_id") or ""
        prior = snapshot.get("prior_posture") or {}
        if not integration_id or not prior.get("rung"):
            return RevertResult(
                ok=False, details={"reason": "no_prior_posture"},
                message=(
                    "No prior posture captured (the entry did not exist "
                    "before apply); cannot revert mechanically — restrict "
                    "it on Security → Permissions → Autonomy instead."
                ),
            )
        shared = self._resolve_shared()
        kwargs: dict = {}
        if snapshot.get("applied_rung"):
            kwargs["expected_current_rung"] = snapshot["applied_rung"]
        try:
            _astore.set_posture(
                shared, target_bot, integration_id,
                rung=str(prior["rung"]),
                rules=dict(prior.get("rules") or {}),
                actor=_astore.ACTOR_OPERATOR_UI,
                note="revert of an applied autonomy proposal",
                kind=prior.get("kind"),
                **kwargs,
            )
        except _astore.StalePostureError as exc:
            return RevertResult(
                ok=False, details={"error": "stale_posture"},
                message=(
                    f"Posture changed again after the apply: {exc} The "
                    "newer decision wins; revert refused."
                ),
            )
        except ValueError as exc:
            return RevertResult(
                ok=False, details={"error": str(exc)},
                message=f"Revert refused by posture validation: {exc}",
            )
        render = _arenderer.render_bot(
            target_bot, shared, home_override=self.home_override,
        )
        return RevertResult(
            ok=True,
            details={
                "bot_id": target_bot,
                "integration_id": integration_id,
                "rung": prior["rung"],
                "render_error": render.write_error,
            },
            message=(
                f"Restored {target_bot}/{integration_id} to {prior['rung']}."
            ),
        )


# The Applier protocol's apply() is (action, bot_id); arbiter.apply
# inspects the signature and passes proposal_id only to appliers that
# accept it (the UpdatePermissionConfig precedent) — the keyword-only
# extension is deliberate, not a protocol violation.
register_applier(
    "UpdateAutonomyPosture",
    UpdateAutonomyPostureApplier(),  # pyright: ignore[reportArgumentType]
)
