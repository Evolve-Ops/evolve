"""Wizard state file IO.

Per-(bot, user) wizard run state lives at
``{shared_dir}/wizard/{bot_id}/{user_key}.json`` and is the in-progress
scratchpad. Final user profile is committed separately on Wrap (see
:mod:`evo.wizard.profile`); the wizard state is just the conversation's
working memory.

Status lifecycle (5a MVP supports only ``in_progress`` and ``completed``;
``paused`` and ``skipped`` reserved for slice 5b):

  not_started  — never seen
  in_progress  — wizard is mid-conversation
  paused       — user went silent / explicitly paused
  completed    — wrap reached, profile committed
  skipped      — user opted out via ``evo wizard skip``

Atomic write via temp+rename in the same dir; reads are tolerant of
malformed files (a corrupt state file returns None and the caller can
restart, rather than wedging the user out of their own wizard).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from evolve_util import now_iso as _now_iso


Status = Literal[
    "not_started", "in_progress", "paused", "completed", "skipped", "abandoned",
]
# ``abandoned`` is set when the user breaks out of an in-flight wizard by
# typing a new ``evo`` command. The wizard is optional — it should never
# trap the user — and a non-active status means the plugin's recovery
# probe (``GET /api/evo/wizard/active``) won't re-attach to the dead
# session after a restart.
# ``approver`` is the slice 5b8 audience for conversational approval —
# bare ``evo`` callers receive a top recommendation and approve / reject
# / snooze / next / context conversationally. No profile commit on
# finalize (mirrors ``guide_drafter``).
Audience = Literal[
    "primary", "secondary", "guide_drafter", "approver",
    "app_creator", "google_setup", "add_bot",
]
# ``app_creator`` is Wave 3's audience for ``evo app create`` — the user
# describes an app, the engine drafts a spec via the same ``_build_draft``
# helper the admin Create App page uses, the user iterates / approves /
# cancels conversationally. No profile commit on finalize (mirrors
# ``guide_drafter``).
#
# ``google_setup`` is the audience for ``evo setup-google`` — a one-time
# pod-level wizard that walks the admin through Google Cloud Console
# setup (create OAuth client, enable APIs, configure consent screen),
# collects the resulting client_id + client_secret + admin base URL,
# and persists them to network.json + auth-profiles.json so per-bot
# OAuth flows can fire. No profile commit (this is operator-grade
# configuration, not user-profile data).
#
# ``add_bot`` is the audience for ``evo add-bot`` (admin-only) — the
# conversational bot-creation wizard's M1 slice: need → audience →
# purpose → name → plan. On a confirmed plan the new bot's purpose{}
# block lands in network.json; no account is created yet (that's M2).
# No profile commit (the conversation plans a bot, it doesn't profile
# the caller).


_SUBDIR = "wizard"


@dataclass
class WizardState:
    """In-memory representation of a wizard run."""

    user_key: str
    bot_id: str
    audience: Audience
    status: Status
    current_phase: str
    extracted: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None
    # Reserved for 5b — we don't write these in 5a but read tolerantly.
    paused_at: str | None = None
    skipped_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WizardState":
        # Permissive: missing fields default; unknown fields ignored.
        return cls(
            user_key=str(data.get("user_key") or ""),
            bot_id=str(data.get("bot_id") or ""),
            audience=data.get("audience") or "primary",  # type: ignore[arg-type]
            status=data.get("status") or "in_progress",  # type: ignore[arg-type]
            current_phase=str(data.get("current_phase") or ""),
            extracted=dict(data.get("extracted") or {}),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            completed_at=data.get("completed_at"),
            paused_at=data.get("paused_at"),
            skipped_at=data.get("skipped_at"),
        )

    def is_active(self) -> bool:
        return self.status in ("in_progress", "paused")


# ─────────────────────────────────────────────────────────────────────────────
# File layout
# ─────────────────────────────────────────────────────────────────────────────


def state_path(shared_dir: Path, bot_id: str, user_key: str) -> Path:
    """Return the on-disk path for ``(bot_id, user_key)`` wizard state.

    ``user_key`` may contain ``:`` (we use ``ext:telegram:12345``,
    ``pod:pod_admin_user``); we encode that as ``__`` for filename safety so the
    raw colons don't trip up tools that scan paths.
    """
    safe = user_key.replace(":", "__").replace("/", "_")
    return Path(shared_dir) / _SUBDIR / bot_id / f"{safe}.json"


# ─────────────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────────────


def read_state(shared_dir: Path, bot_id: str, user_key: str) -> WizardState | None:
    """Return the recorded state, or None if no file exists or it's
    malformed. Soft-failing on parse errors is intentional: a corrupted
    state file should let the user restart their wizard, not lock them out
    permanently."""
    path = state_path(shared_dir, bot_id, user_key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return WizardState.from_dict(data)


# ─────────────────────────────────────────────────────────────────────────────
# Write
# ─────────────────────────────────────────────────────────────────────────────


def write_state(shared_dir: Path, st: WizardState) -> WizardState:
    """Atomically persist ``st`` to disk. ``updated_at`` is stamped now;
    ``created_at`` is preserved if non-empty, otherwise stamped. Returns
    the state with stamped fields."""
    if not st.bot_id or not st.user_key:
        raise ValueError("WizardState must have non-empty bot_id and user_key")

    now = _now_iso()
    if not st.created_at:
        st.created_at = now
    st.updated_at = now

    target = state_path(shared_dir, st.bot_id, st.user_key)
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(st.to_dict(), indent=2, ensure_ascii=False)

    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f".{target.stem}-",
        suffix=".json.tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return st


def initialize(
    shared_dir: Path,
    *,
    bot_id: str,
    user_key: str,
    audience: Audience,
    initial_phase: str,
) -> WizardState:
    """Create and persist a fresh ``in_progress`` state for ``(bot_id,
    user_key)``. Overwrites any existing state — callers that want to
    resume should use :func:`read_state` first and only initialize if the
    result is None or terminal."""
    st = WizardState(
        user_key=user_key,
        bot_id=bot_id,
        audience=audience,
        status="in_progress",
        current_phase=initial_phase,
    )
    return write_state(shared_dir, st)


def mark_abandoned(
    shared_dir: Path, bot_id: str, user_key: str
) -> WizardState | None:
    """Best-effort: mark any active wizard for ``(bot_id, user_key)`` as
    abandoned and persist. No-op if no state file exists, or if the
    existing state is already terminal (completed / skipped / abandoned).

    Called by the dispatcher when a new ``evo`` command arrives for a
    user with an in-flight wizard. The wizard is optional and must
    never trap the user — a fresh command always wins."""
    st = read_state(shared_dir, bot_id, user_key)
    if st is None or not st.is_active():
        return st
    st.status = "abandoned"
    return write_state(shared_dir, st)


def mark_completed(shared_dir: Path, st: WizardState) -> WizardState:
    """Transition ``st`` to ``completed``, stamp ``completed_at``,
    persist.

    Also writes a persistent ``.onboarded`` marker for ``primary`` and
    ``secondary`` audiences. The state file itself is keyed only by
    ``(bot_id, user_key)`` and gets overwritten when subsequent flows
    (rec_pending, guide_drafter, forge_*) start, so the state's
    completion status alone can't answer "has this user gone through
    onboarding?" — the marker survives those overwrites.
    """
    st.status = "completed"
    st.completed_at = _now_iso()
    persisted = write_state(shared_dir, st)
    if st.audience in ("primary", "secondary"):
        try:
            mark_onboarded(
                shared_dir, st.bot_id, st.user_key, audience=st.audience
            )
        except OSError:
            # Marker write failure shouldn't block the wizard finalize —
            # the wrap message still goes out. Worst case: the user gets
            # routed back through onboarding once more on the next bare
            # ``evo``, which is annoying but not broken.
            pass
    return persisted


# ─────────────────────────────────────────────────────────────────────────────
# Onboarding marker — survives audience-changing state overwrites
# ─────────────────────────────────────────────────────────────────────────────


def _onboarded_marker_path(
    shared_dir: Path, bot_id: str, user_key: str
) -> Path:
    """Return the path to the ``.onboarded`` marker for ``(bot_id, user_key)``.

    Same naming convention as :func:`state_path` so a directory listing
    visually pairs the state file with its onboarding marker.
    """
    safe = user_key.replace(":", "__").replace("/", "_")
    return Path(shared_dir) / _SUBDIR / bot_id / f"{safe}.onboarded"


def mark_onboarded(
    shared_dir: Path,
    bot_id: str,
    user_key: str,
    *,
    audience: str,
) -> Path:
    """Write the ``.onboarded`` marker for ``(bot_id, user_key)``.

    Called automatically by :func:`mark_completed` for primary and
    secondary audiences. Idempotent — re-writing on later completions
    just refreshes the timestamp.
    """
    target = _onboarded_marker_path(shared_dir, bot_id, user_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"onboarded_at": _now_iso(), "audience": audience}, indent=2
    )
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.stem}-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def is_onboarded(
    shared_dir: Path, bot_id: str, user_key: str
) -> bool:
    """Return True iff ``(bot_id, user_key)`` has completed an onboarding
    wizard at some point in the past — primary or secondary audience.

    The dispatch gate uses this to decide whether bare ``evo`` should
    pre-empt with the onboarding wizard or proceed directly to
    ``rec_pending``. The marker is written by :func:`mark_completed`
    for the relevant audiences; the rec_pending / guide_drafter flows
    don't trigger it.
    """
    return _onboarded_marker_path(shared_dir, bot_id, user_key).exists()
