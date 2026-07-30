"""
spec_session.py — SpecSession data model for the Create App wizard.

Storage: {shared_dir}/specs/{session_id}.json
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .ids import now_iso


# ── ID generation ─────────────────────────────────────────────────────────────

def new_session_id() -> str:
    """Generate a new spec session ID.  e.g. 's-1a2b3c4d'"""
    return "s-" + secrets.token_hex(4)


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class SpecDraft:
    """One iteration of the spec — produced by the LLM spec builder."""
    version: int
    display_name: str
    description: str          # 1-2 sentence summary
    build_spec: str           # full markdown spec — what forge consumes
    application_tags: list     # e.g. ["task-management", "productivity"]
    requirements: dict        # {integrations:[], secrets:[], python_packages:[], system:[]}
    app_dependencies: list    # [{pkg_id, display_name, required, reason}]
    test_command: str
    # Set when the design phase concludes the app is too trivial to test
    # (single-shot manual cron, no logic, etc.) — accepted by the forge
    # gate as an alternative to test_command/test_cases. Empty string
    # means "tests required". See docs/spec-app-testing-2026-05-07.md §5.
    test_exemption_reason: str
    conflicts: list           # [{bot_id, app_id, app_name, conflict_type, description}]
    suggestions: list         # [str] — design suggestions shown to user
    # Bot-facing usage manual — see manifest.py module docstring for sub-schema.
    # Optional; default empty dict, populated by the spec-builder LLM when the
    # app description is rich enough to infer how the bot should invoke it.
    usage: dict
    created_at: str

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "build_spec": self.build_spec,
            "application_tags": self.application_tags,
            "requirements": self.requirements,
            "app_dependencies": self.app_dependencies,
            "test_command": self.test_command,
            "test_exemption_reason": self.test_exemption_reason,
            "conflicts": self.conflicts,
            "suggestions": self.suggestions,
            "usage": self.usage,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SpecDraft":
        return cls(
            version=d.get("version", 1),
            display_name=d.get("display_name", ""),
            description=d.get("description", ""),
            build_spec=d.get("build_spec", ""),
            application_tags=d.get("application_tags") or [],
            requirements=d.get("requirements") or {},
            app_dependencies=d.get("app_dependencies") or [],
            test_command=d.get("test_command", ""),
            test_exemption_reason=d.get("test_exemption_reason", ""),
            conflicts=d.get("conflicts") or [],
            suggestions=d.get("suggestions") or [],
            usage=d.get("usage") or {},
            created_at=d.get("created_at", ""),
        )


@dataclass
class SpecSession:
    """Full lifecycle record for a Create App wizard session.

    The ``status`` field tracks the wizard's user-facing lifecycle:
    gathering → draft → iterating → approved → queued. The ``generation``
    sub-dict tracks the background spec-generation worker job
    independently — it can be "running" while ``status`` is "gathering"
    (initial draft) or "iterating" (feedback round). When the worker
    completes, ``status`` flips to "draft" (or "iterating" on subsequent
    rounds) and ``generation.status`` reads "completed". This split lets
    the wizard close the browser tab and come back later — the worker
    runs server-side, persists progress to ``generation.*``, and the
    UI polls ``GET /api/specs/<id>`` to render progress."""
    session_id: str                 # s-xxxxxxxx
    status: str                     # gathering|draft|iterating|approved|queued|cancelled
    target_bots: list               # ["team_bot_a", "admin_bot"]
    input: str                      # original plain-language description
    drafts: list                    # list of SpecDraft dicts (serialised)
    feedback_history: list          # [{version, feedback, created_at}]
    approved_version: Optional[int]
    forge_jobs: list                # [{job_id, bot_id, app_id}]
    created_at: str
    updated_at: str
    created_by: str                 # "ui" | "bot:<bot_id>"
    # Background-worker progress block. Shape (all optional):
    #   {
    #     "status": "queued|running|completed|failed|cancelled",
    #     "phase":  "context|model|generating|parse",
    #     "message": "operator-friendly description of current step",
    #     "model_full": "anthropic/claude-opus-4-7",
    #     "tier":   "tier1" | "tier2",
    #     "partial_chars":  int,    # bytes of draft JSON received so far
    #     "partial_tokens": int,    # output tokens reported by Anthropic
    #     "input_tokens":   int,
    #     "started_at":     ISO 8601,
    #     "completed_at":   ISO 8601,  # set on terminal status
    #     "error":  str,            # set when status == "failed"
    #     "version": int,           # draft version this generation is producing
    #   }
    # The wizard frontend polls GET /api/specs/<id> every ~2s and
    # renders progress from these fields. ``status`` here is the JOB
    # status; the outer ``status`` field above is the SESSION status.
    generation: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "target_bots": self.target_bots,
            "input": self.input,
            "drafts": self.drafts,
            "feedback_history": self.feedback_history,
            "approved_version": self.approved_version,
            "forge_jobs": self.forge_jobs,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_by": self.created_by,
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SpecSession":
        return cls(
            session_id=d.get("session_id", ""),
            status=d.get("status", "draft"),
            target_bots=d.get("target_bots") or [],
            input=d.get("input", ""),
            drafts=d.get("drafts") or [],
            feedback_history=d.get("feedback_history") or [],
            approved_version=d.get("approved_version"),
            forge_jobs=d.get("forge_jobs") or [],
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            created_by=d.get("created_by", "ui"),
            generation=d.get("generation") or {},
        )

    def latest_draft(self) -> Optional[SpecDraft]:
        """Return the most recent SpecDraft, or None if no drafts yet."""
        if not self.drafts:
            return None
        return SpecDraft.from_dict(self.drafts[-1])

    def draft_by_version(self, version: int) -> Optional[SpecDraft]:
        """Return the SpecDraft with the given version number, or None."""
        for d in self.drafts:
            if d.get("version") == version:
                return SpecDraft.from_dict(d)
        return None


# ── Storage helpers ───────────────────────────────────────────────────────────

def _specs_dir(shared_dir: Path) -> Path:
    return shared_dir / "specs"


def _session_path(session_id: str, shared_dir: Path) -> Path:
    return _specs_dir(shared_dir) / f"{session_id}.json"


def save_session(session: SpecSession, shared_dir: Path) -> None:
    """Atomic write of session to {shared_dir}/specs/{session_id}.json."""
    session.updated_at = now_iso()
    d = _specs_dir(shared_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = _session_path(session.session_id, shared_dir)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(session.to_dict(), f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_session(session_id: str, shared_dir: Path) -> Optional[SpecSession]:
    """Load a session by ID; returns None if not found or malformed."""
    path = _session_path(session_id, shared_dir)
    if not path.exists():
        return None
    try:
        return SpecSession.from_dict(json.loads(path.read_text()))
    except Exception:
        return None


def list_sessions(
    shared_dir: Path,
    status: Optional[str] = None,
) -> list[SpecSession]:
    """Return all sessions, optionally filtered by status, newest first."""
    d = _specs_dir(shared_dir)
    if not d.exists():
        return []
    sessions = []
    for p in d.iterdir():
        if p.suffix == ".json" and p.is_file():
            try:
                sess = SpecSession.from_dict(json.loads(p.read_text()))
                if status is None or sess.status == status:
                    sessions.append(sess)
            except Exception:
                pass
    # Sort newest first
    sessions.sort(key=lambda s: s.created_at, reverse=True)
    return sessions
