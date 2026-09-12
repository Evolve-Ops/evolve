"""Inbox auto-response policy (Phase 5 of Issue Inbox).

The policy is a small JSON blob persisted at
``{shared_dir}/inbox_policy.json``. It gates whether the auto-responder
takes action against an inbound intake's triage verdict, and at what
confidence threshold.

Default is **opt-out everything** — the auto-responder is a no-op until
the operator explicitly enables one or more action kinds via the
Triage UI (or by editing the JSON file). This is deliberate: we don't
auto-close someone else's GH issue on first install. The operator has
to see the queue, see what kinds of verdicts the classifier is
producing, and decide what to trust.

Independent of the global ``enabled`` flag, each action kind has its
own enable + confidence-threshold gate. The 24-hour undo window is
NOT operator-configurable — the spec fixes it at 24h so the audit
trail and user expectation are uniform.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


POLICY_FILENAME = "inbox_policy.json"

# Default confidence floors per action. Phase 5 intentionally pins these
# high — auto-closing someone else's issue is high-stakes, so the floor
# must be above the classifier's typical "I'm pretty sure" range. The
# operator can lower these via the UI, but the defaults reflect a
# "would the operator regret approving this in advance?" risk read.
DEFAULT_CLOSE_DUPLICATE_MIN_CONFIDENCE = 0.9
DEFAULT_REPLY_CLARIFYING_MIN_CONFIDENCE = 0.85
DEFAULT_LABEL_ONLY_MIN_CONFIDENCE = 0.7


@dataclass
class AutoResponsePolicy:
    """Operator-configurable gates on the auto-responder.

    All-fields-default-False means a fresh install does nothing
    automatically. The operator must opt in.

    ``min_confidence`` per kind is a soft gate — the auto-responder
    checks ``triage.confidence >= floor`` before firing. If the
    classifier produces a low-confidence verdict it routes back to
    the operator.
    """

    enabled: bool = False
    close_duplicate_enabled: bool = False
    close_duplicate_min_confidence: float = DEFAULT_CLOSE_DUPLICATE_MIN_CONFIDENCE
    reply_clarifying_enabled: bool = False
    reply_clarifying_min_confidence: float = DEFAULT_REPLY_CLARIFYING_MIN_CONFIDENCE
    label_only_enabled: bool = False
    label_only_min_confidence: float = DEFAULT_LABEL_ONLY_MIN_CONFIDENCE
    # Free-form note from the operator: "I enabled close_duplicate after
    # watching the classifier for two weeks." Surfaces in the audit
    # trail next to AutoActionRecord.reason="policy".
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "close_duplicate_enabled": self.close_duplicate_enabled,
            "close_duplicate_min_confidence": self.close_duplicate_min_confidence,
            "reply_clarifying_enabled": self.reply_clarifying_enabled,
            "reply_clarifying_min_confidence": self.reply_clarifying_min_confidence,
            "label_only_enabled": self.label_only_enabled,
            "label_only_min_confidence": self.label_only_min_confidence,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AutoResponsePolicy":
        if not isinstance(data, dict):
            return cls()

        def _f(key: str, default: float) -> float:
            try:
                v = float(data.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(0.0, min(1.0, v))

        return cls(
            enabled=bool(data.get("enabled", False)),
            close_duplicate_enabled=bool(data.get("close_duplicate_enabled", False)),
            close_duplicate_min_confidence=_f(
                "close_duplicate_min_confidence",
                DEFAULT_CLOSE_DUPLICATE_MIN_CONFIDENCE,
            ),
            reply_clarifying_enabled=bool(data.get("reply_clarifying_enabled", False)),
            reply_clarifying_min_confidence=_f(
                "reply_clarifying_min_confidence",
                DEFAULT_REPLY_CLARIFYING_MIN_CONFIDENCE,
            ),
            label_only_enabled=bool(data.get("label_only_enabled", False)),
            label_only_min_confidence=_f(
                "label_only_min_confidence",
                DEFAULT_LABEL_ONLY_MIN_CONFIDENCE,
            ),
            note=str(data.get("note") or ""),
        )


# ─── Persistence ─────────────────────────────────────────────────────────────


def _policy_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / POLICY_FILENAME


def load_policy(shared_dir: Path) -> AutoResponsePolicy:
    """Read the policy JSON from ``{shared_dir}/inbox_policy.json``.

    Returns the default (everything off) when the file doesn't exist
    or is malformed — this is the safe behavior since the default
    policy does nothing.
    """
    path = _policy_path(shared_dir)
    if not path.exists():
        return AutoResponsePolicy()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return AutoResponsePolicy()
    return AutoResponsePolicy.from_dict(data)


def save_policy(shared_dir: Path, policy: AutoResponsePolicy) -> None:
    """Persist via temp-file + rename for atomicity.

    The shared_dir is owned by the ``evolve`` user, so this is a
    plain write — no /tmp staging + sudo needed.
    """
    path = _policy_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".inbox-policy-",
        suffix=".json.tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(policy.to_dict(), f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
