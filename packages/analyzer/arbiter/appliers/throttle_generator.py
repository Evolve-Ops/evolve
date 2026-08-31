"""ThrottleGenerator + PauseGenerator appliers.

Spec §7.3. ThrottleGenerator edits the target generator's record to
reduce cadence, budget, or confidence. PauseGenerator sets status to
``paused``. Both support revert from the captured snapshot.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from arbiter.appliers.base import (
    ApplyResult,
    RevertResult,
    register_applier,
)
from schema.generator import GeneratorRecord
from schema.proposal import PauseGenerator, ThrottleGenerator


_SHARED_DIR = Path("/Users/Shared/evolve")


def set_shared_dir(path: Path) -> None:
    global _SHARED_DIR
    _SHARED_DIR = Path(path)


def _record_path(generator_id: str) -> Path:
    return _SHARED_DIR / "generators" / f"{generator_id}.json"


def _load_record(generator_id: str) -> GeneratorRecord | None:
    path = _record_path(generator_id)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return GeneratorRecord.from_dict(json.load(fh))
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def _save_record(record: GeneratorRecord) -> None:
    path = _record_path(record.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record.to_dict(), fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─────────────────────────────────────────────────────────────────────────────
# ThrottleGenerator
# ─────────────────────────────────────────────────────────────────────────────


class ThrottleGeneratorApplier:
    def capture_snapshot(
        self, action: ThrottleGenerator, bot_id: str
    ) -> dict:
        record = _load_record(action.generator_id)
        return {
            "action_kind": "ThrottleGenerator",
            "generator_id": action.generator_id,
            "prior_config": dict(record.config) if record else {},
            "prior_status": record.status if record else "active",
            "existed_before": record is not None,
        }

    def apply(
        self, action: ThrottleGenerator, bot_id: str
    ) -> ApplyResult:
        record = _load_record(action.generator_id)
        if record is None:
            return ApplyResult(
                ok=False,
                message=f"generator {action.generator_id!r} record not found",
            )

        tt = action.throttle_type
        if tt == "reduce_cadence":
            # Cadence lives in the charter (immutable); the runtime throttle
            # is expressed as a state marker that the runner reads.
            record.state["throttled_cadence"] = action.new_value
        elif tt == "reduce_budget":
            try:
                record.config["cost_budget_per_window_usd"] = float(
                    action.new_value
                )
            except (TypeError, ValueError):
                return ApplyResult(
                    ok=False,
                    message=f"cannot parse budget value {action.new_value!r}",
                )
        elif tt == "lower_confidence":
            try:
                record.config["confidence_threshold"] = float(action.new_value)
            except (TypeError, ValueError):
                return ApplyResult(
                    ok=False,
                    message=f"cannot parse confidence {action.new_value!r}",
                )
        else:
            return ApplyResult(
                ok=False,
                message=f"unknown throttle_type {tt!r}",
            )

        # Record the throttle expiry if set
        if action.revert_after_days is not None:
            record.state["throttle_expires_days"] = action.revert_after_days

        _save_record(record)
        return ApplyResult(
            ok=True,
            details={"generator_id": action.generator_id, "type": tt},
            message=f"throttled {action.generator_id!r} ({tt})",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        gen_id = snapshot.get("generator_id", "")
        prior_config = snapshot.get("prior_config") or {}
        prior_status = snapshot.get("prior_status", "active")
        record = _load_record(gen_id)
        if record is None:
            return RevertResult(
                ok=False,
                message=f"generator {gen_id!r} record missing for revert",
            )
        record.config = dict(prior_config)
        record.status = prior_status
        record.state.pop("throttled_cadence", None)
        record.state.pop("throttle_expires_days", None)
        _save_record(record)
        return RevertResult(
            ok=True, message=f"reverted throttle on {gen_id!r}"
        )


register_applier("ThrottleGenerator", ThrottleGeneratorApplier())


# ─────────────────────────────────────────────────────────────────────────────
# PauseGenerator
# ─────────────────────────────────────────────────────────────────────────────


class PauseGeneratorApplier:
    def capture_snapshot(
        self, action: PauseGenerator, bot_id: str
    ) -> dict:
        record = _load_record(action.generator_id)
        return {
            "action_kind": "PauseGenerator",
            "generator_id": action.generator_id,
            "prior_status": record.status if record else "active",
            "existed_before": record is not None,
        }

    def apply(self, action: PauseGenerator, bot_id: str) -> ApplyResult:
        record = _load_record(action.generator_id)
        if record is None:
            return ApplyResult(
                ok=False,
                message=f"generator {action.generator_id!r} record not found",
            )
        record.status = "paused"
        if action.resume_after_days is not None:
            record.state["pause_expires_days"] = action.resume_after_days
        _save_record(record)
        return ApplyResult(
            ok=True,
            details={"generator_id": action.generator_id},
            message=f"paused {action.generator_id!r}",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        gen_id = snapshot.get("generator_id", "")
        prior_status = snapshot.get("prior_status", "active")
        record = _load_record(gen_id)
        if record is None:
            return RevertResult(
                ok=False, message=f"generator {gen_id!r} record missing"
            )
        record.status = prior_status
        record.state.pop("pause_expires_days", None)
        _save_record(record)
        return RevertResult(ok=True, message=f"resumed {gen_id!r}")


register_applier("PauseGenerator", PauseGeneratorApplier())
