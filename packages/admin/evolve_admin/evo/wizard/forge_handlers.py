"""Forge-via-messaging orchestration helpers (slice 5b7b).

Spec: internal/spec-forge-via-messaging-2026-05-07.md.

Bridges between the wizard's FORGE_CONFIRM phase and the existing forge
pipeline. Three responsibilities:

1. **Build a draft manifest** from the wizard's extracted state — minimal
   shape sufficient for forge_engine to drive code generation.
2. **Queue a ForgeJob** with the install step sequence; mints a synthetic
   ``pkg_id`` in the canonical ``p-<8hex>`` form (it becomes the spec_id
   at the v7-arc native-write conversion — see ``synthetic_pkg_id``).
   Chat provenance travels on ``source="bot_created"`` +
   ``context_snapshot.created_via``.
3. **Run the forge pipeline asynchronously** (daemon thread) with
   ``auto_approve_actor`` set, then **emit a notification** to the user
   when the build completes or fails. The wizard wraps with a "queued"
   message immediately; the user finds out the result via their next
   session-start when the notification fires.

This module is import-light by default — the heavy forge_engine and
forge_jobs modules are imported lazily inside ``kick_off_build`` so the
wizard can be exercised in tests without pulling them in.
"""

# identity: see applications.app_identity.resolve_app_id (AL-1.4b). ``pkg_id`` here is MINTED, not read:
# ``synthetic_pkg_id()`` generates the canonical ``p-<8hex>`` form so the value
# can also serve as the spec_id. It names the build's package slot and travels
# beside ``app_id``, the app's own identity. Nothing exists on disk to resolve
# at the point these run.

from __future__ import annotations

import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import notifications as _notifications


_AUTO_APPROVE_ACTOR_DEFAULT = "api:wizard-forge"


@dataclass
class KickoffResult:
    """What ``kick_off_build`` returns to the wizard engine."""

    ok: bool
    app_id: str
    app_name: str
    pkg_id: str | None = None
    job_id: str | None = None
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Manifest construction
# ─────────────────────────────────────────────────────────────────────────────


_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def slugify_app_name(name: str) -> str:
    """Normalize a user-typed app name into a stable slug. We use this
    BOTH as the manifest's ``id`` and as the basis for ``app_id`` on the
    forge job. Lowercase, hyphens, no spaces, max 40 chars."""
    name = (name or "").strip().lower()
    name = name.replace(" ", "-").replace("_", "-")
    name = _SLUG_RE.sub("", name)
    name = name.strip("-")
    if not name:
        name = "untitled-app"
    return name[:40]


def synthetic_pkg_id() -> str:
    """Generate a canonical ``p-<8hex>`` pkg_id for messaging-driven builds.

    Until manifest-v7 Slice 3a this minted ``chat-<8hex>`` so listings
    could spot chat-built apps by prefix. Native v7-arc writes need the
    pkg_id to BE the spec_id (conformant ``p-`` form — _resolve_spec_id
    preserves it, so the ``spec=`` markers the build stamps match the
    Spec the install conversion writes). Chat provenance still travels
    on ``source="bot_created"`` + ``context_snapshot.created_via``.
    """
    return f"p-{secrets.token_hex(4)}"


def build_manifest_from_state(
    bot_id: str,
    extracted: dict[str, Any],
    user_key: str,
    *,
    pkg_id: str | None = None,
) -> dict[str, Any]:
    """Build a minimal-but-valid manifest dict from the wizard's
    ``extracted`` state. Returns a dict (not a dataclass instance) so
    the persistence helper can choose how to write it.

    Required-by-forge fields:
      * ``id`` — the manifest slug (= app_id on the job)
      * ``bot_id`` — which bot owns this app
      * ``display_name`` — human-friendly name
      * ``description`` — what the app does (one sentence is fine)
      * ``build_spec`` — markdown the forge engine reads to generate code

    Messaging-specific fields:
      * ``conversational_summary`` — verbatim from the wizard;
        re-rendered into the post-build DM (per spec §3.1)
      * ``source = "bot_created"``
      * ``context_snapshot.created_via = "chat"``
      * ``context_snapshot.requested_by_user_key`` — for traceability
      * ``status = "draft"`` — until forge runs through and lands it
    """
    raw_name = extracted.get("forge_app_name") or "untitled-app"
    app_id = slugify_app_name(str(raw_name))
    display_name = str(raw_name).strip() or "Untitled App"
    description = (extracted.get("forge_description") or "").strip() or display_name

    capabilities = extracted.get("forge_capabilities") or []
    if not isinstance(capabilities, list):
        capabilities = []
    behaviors = extracted.get("forge_example_behaviors") or []
    if not isinstance(behaviors, list):
        behaviors = []
    conv_summary = (extracted.get("forge_conversational_summary") or "").strip()

    build_spec = _render_build_spec(
        display_name=display_name,
        description=description,
        capabilities=[str(c) for c in capabilities if c],
        behaviors=[str(b) for b in behaviors if b],
        conversational_summary=conv_summary,
    )

    payload = {
        "id": app_id,
        "bot_id": bot_id,
        "display_name": display_name,
        "description": description,
        "build_spec": build_spec,
        "conversational_summary": conv_summary,
        "application_tags": [str(c) for c in capabilities if c][:6],
        "usage": _derive_usage_from_state(
            display_name=display_name,
            description=description,
            capabilities=[str(c) for c in capabilities if c],
            behaviors=[str(b) for b in behaviors if b],
            conversational_summary=conv_summary,
        ),
        "pkg_id": pkg_id or synthetic_pkg_id(),
        "pkg_version": "0.0.0",
        "source": "bot_created",
        "status": "draft",
        "context_snapshot": {
            "created_via": "chat",
            "requested_by_user_key": user_key,
        },
    }
    # Slice 3a: drafts acquire privacy{} + audience_scoping{} at mint time
    # via the same canonical defaults every other mint path seeds — the
    # draft carries a declared (conservative) boundary from birth instead
    # of "not yet declared" until the build completes. (Lazy import keeps
    # the module import-light; the validator module itself is a leaf.)
    from ...applications.privacy_scoping_validator import (
        seed_privacy_scoping_defaults,
    )
    seed_privacy_scoping_defaults(payload)
    return payload


def _derive_usage_from_state(
    *,
    display_name: str,
    description: str,
    capabilities: list[str],
    behaviors: list[str],
    conversational_summary: str,
) -> dict[str, Any]:
    """Synthesise a best-effort `usage` block from the wizard's extracted state.

    The chat wizard doesn't ask for trigger words / how-to-use directly — the
    user explains it once in plain language and the bot builds the app. To get
    INSTALLED_APPS.md / AGENTS.md cross-refs populated without adding more
    wizard turns, we derive sensible defaults: capabilities/behaviors become
    hint words, the conversational summary becomes the how-to-use paragraph.
    Operators can refine via the manifest editor later (UI editor is H-1 cont.).
    """
    name_tokens = [t for t in re.split(r"[\s_\-]+", display_name.lower()) if len(t) > 2]
    hint_set: list[str] = []
    for src in (name_tokens, capabilities, behaviors):
        for v in src:
            v = str(v).strip().lower()
            if v and v not in hint_set:
                hint_set.append(v)
    hint_words = hint_set[:8]

    how_to_use = conversational_summary or description or ""

    # Always emit at least one bot_voice_example so the rendered
    # INSTALLED_APPS.md entry has voice anchor for the LLM. Without
    # this the wizard ships every app with bot_voice_examples=[] —
    # technically passes the discoverability audit (the assertion
    # doesn't gate on bot_voice_examples) but the renderer's "Sample
    # bot replies while using this app" section stays empty, which is
    # a real loss of LLM scaffolding. The placeholder is parametric on
    # the app's display name so it isn't generic across installs.
    name = display_name.strip() or "this app"
    bot_voice_examples = [
        f"Working on it through {name} — be right back with the result.",
        f"Done — {name} handled it.",
    ]

    return {
        "model": "user-initiated",
        "trigger_recognition": {
            "pattern": f"User mentions {display_name.lower()} or related capability.",
            "hint_words": hint_words,
            "requires_keyword": False,
        },
        "auto_capture": {
            "enabled": False,
            "sources": [],
        },
        "how_to_use": how_to_use,
        "bot_voice_examples": bot_voice_examples,
    }


def _render_build_spec(
    *,
    display_name: str,
    description: str,
    capabilities: list[str],
    behaviors: list[str],
    conversational_summary: str,
) -> str:
    """Render the manifest's ``build_spec`` markdown — what the forge
    engine reads to generate code. Stays simple: header + description +
    integrations + behaviors + the conversational summary verbatim. The
    summary is the load-bearing artifact (§3.1)."""
    lines = [f"# {display_name}", "", f"## Description", "", description, ""]
    if capabilities:
        lines.append("## Integrations / capabilities")
        lines.append("")
        for c in capabilities:
            lines.append(f"- {c}")
        lines.append("")
    if behaviors:
        lines.append("## Example behaviors")
        lines.append("")
        for b in behaviors:
            lines.append(f"- {b}")
        lines.append("")
    if conversational_summary:
        lines.append("## Conversational summary (user-bot agreement)")
        lines.append("")
        lines.append(conversational_summary)
        lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Manifest persistence
# ─────────────────────────────────────────────────────────────────────────────


def persist_manifest(
    shared_dir: Path,
    manifest: dict[str, Any],
) -> None:
    """Write the manifest dict to ``{shared_dir}/applications/{bot_id}/{app_id}.json``.

    Atomic via temp+rename. Defensive — creates the parent dir tree if
    missing. Caller's contract is to have validated the manifest enough
    that forge_engine can load it; this just persists the bytes.
    """
    import json
    import os
    import tempfile

    bot_id = manifest.get("bot_id")
    app_id = manifest.get("id")
    if not bot_id or not app_id:
        raise ValueError("manifest must have bot_id and id")

    target = shared_dir / "applications" / str(bot_id) / f"{app_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(manifest, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{app_id}-", suffix=".json.tmp",
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


# ─────────────────────────────────────────────────────────────────────────────
# Job creation + async kickoff
# ─────────────────────────────────────────────────────────────────────────────


def create_chat_install_job(
    pkg_id: str,
    app_id: str,
    bot_id: str,
    shared_dir: Path,
):
    """Create + persist a ``ForgeJob`` for a messaging-driven install.

    Same shape as :func:`forge_jobs.create_install_job` but skips the
    ``gallery_version`` parameter since these manifests aren't from the
    gallery. The 10-step install sequence is reused as-is; ``run_forge_job``
    with ``auto_approve_actor`` skips Step 8 (the operator gate).
    """
    from ...applications import forge_jobs as _fj
    from ...applications.ids import now_iso

    ts = now_iso()
    job = _fj.ForgeJob(
        job_id=_fj.new_job_id(),
        run_id=_fj.run_id_for(1),
        job_type="install",
        pkg_id=pkg_id,
        app_id=app_id,
        bot_id=bot_id,
        pkg_version_before=None,
        gallery_version=None,
        steps=_fj._install_steps(),
        created_at=ts,
        last_updated=ts,
    )
    _fj.save_job(job, shared_dir)
    return job


# Test seam — production calls run_forge_job + emits a notification;
# tests substitute a stub via set_kickoff_runner so they don't pull in
# the heavy forge_engine module.
KickoffRunner = Callable[
    [Path, str, str, str, str, str, str | None],  # shared_dir, job_id, bot_id, user_key, app_name, app_id, conv_summary
    None,
]


_kickoff_runner: KickoffRunner | None = None


def set_kickoff_runner(fn: KickoffRunner | None) -> None:
    """Substitute the daemon-thread runner. Used by tests; production
    leaves this unset and uses the default Anthropic-backed forge."""
    global _kickoff_runner
    _kickoff_runner = fn


def _default_kickoff_runner(
    shared_dir: Path,
    job_id: str,
    bot_id: str,
    user_key: str,
    app_name: str,
    app_id: str,
    conv_summary: str | None,
) -> None:
    """Production path: run the existing forge pipeline with auto-approve,
    then emit a notification based on outcome."""
    try:
        from ...applications import forge_engine as _fe
    except ImportError as e:
        _emit_failed(
            shared_dir, user_key, bot_id=bot_id,
            app_name=app_name, app_id=app_id,
            conv_summary=conv_summary,
            reason=f"forge_engine import failed: {e}",
        )
        return

    try:
        _fe.run_forge_job(
            job_id=job_id,
            shared_dir=shared_dir,
            bot_id=bot_id,
            auto_approve_actor=_AUTO_APPROVE_ACTOR_DEFAULT,
        )
    except Exception as e:
        _emit_failed(
            shared_dir, user_key, bot_id=bot_id,
            app_name=app_name, app_id=app_id,
            conv_summary=conv_summary,
            reason=f"forge engine raised: {e}",
        )
        return

    # Reload job to inspect outcome — ForgeJob.status reflects whether the
    # auto-approve path succeeded all the way through complete_job.
    try:
        from ...applications import forge_jobs as _fj
        job = _fj.load_job(job_id, shared_dir)
    except Exception:
        job = None

    if job is None:
        _emit_failed(
            shared_dir, user_key, bot_id=bot_id,
            app_name=app_name, app_id=app_id,
            conv_summary=conv_summary,
            reason="job state could not be reloaded after run",
        )
        return

    status = getattr(job, "status", None)
    if status == "complete":
        _emit_complete(
            shared_dir, user_key, bot_id=bot_id,
            app_name=app_name, app_id=app_id,
            conv_summary=conv_summary,
        )
    else:
        # Find the most recent failed step's detail for a user-facing reason
        last_failure = ""
        for step in reversed(getattr(job, "steps", []) or []):
            if getattr(step, "status", None) == "failed":
                last_failure = (getattr(step, "detail", "") or "").strip()
                if last_failure:
                    break
        _emit_failed(
            shared_dir, user_key, bot_id=bot_id,
            app_name=app_name, app_id=app_id,
            conv_summary=conv_summary,
            reason=last_failure or f"build ended in status {status!r}",
        )


def kick_off_build(
    shared_dir: Path,
    bot_id: str,
    user_key: str,
    extracted: dict[str, Any],
    *,
    run_async: bool = True,
) -> KickoffResult:
    """Build manifest, persist, queue a forge job, and run it (in a
    daemon thread by default). Returns a :class:`KickoffResult` with
    ``ok=True`` and identifiers if everything queued successfully;
    ``ok=False`` with an ``error`` string otherwise.

    Tests pass ``run_async=False`` to exercise the manifest+job
    construction path without spawning a thread; or call
    :func:`set_kickoff_runner` to substitute the runner with a stub.
    """
    raw_name = extracted.get("forge_app_name") or "untitled-app"
    app_name = str(raw_name).strip() or "Untitled App"
    pkg_id = synthetic_pkg_id()

    try:
        manifest = build_manifest_from_state(
            bot_id, extracted, user_key, pkg_id=pkg_id,
        )
        app_id = manifest["id"]
        persist_manifest(shared_dir, manifest)
    except Exception as e:
        return KickoffResult(
            ok=False, app_id="", app_name=app_name,
            error=f"manifest persist failed: {e}",
        )

    try:
        job = create_chat_install_job(
            pkg_id=pkg_id, app_id=app_id, bot_id=bot_id, shared_dir=shared_dir,
        )
    except Exception as e:
        return KickoffResult(
            ok=False, app_id=app_id, app_name=app_name, pkg_id=pkg_id,
            error=f"job creation failed: {e}",
        )

    runner = _kickoff_runner or _default_kickoff_runner
    conv_summary = (extracted.get("forge_conversational_summary") or "").strip() or None

    def _runner_target() -> None:
        try:
            runner(
                shared_dir, job.job_id, bot_id, user_key,
                app_name, app_id, conv_summary,
            )
        except Exception as e:
            # Last-resort safety net — emit a failure notification so
            # the user isn't left wondering. Then swallow so the daemon
            # thread doesn't crash the whole admin process.
            try:
                _emit_failed(
                    shared_dir, user_key, bot_id=bot_id,
                    app_name=app_name, app_id=app_id,
                    conv_summary=conv_summary,
                    reason=f"runner crashed: {e}",
                )
            except Exception:
                pass

    if run_async:
        t = threading.Thread(
            target=_runner_target, daemon=True,
            name=f"forge-chat-{job.job_id}",
        )
        t.start()
    else:
        _runner_target()

    return KickoffResult(
        ok=True, app_id=app_id, app_name=app_name,
        pkg_id=pkg_id, job_id=job.job_id,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Notification emission
# ─────────────────────────────────────────────────────────────────────────────


def _emit_complete(
    shared_dir: Path,
    user_key: str,
    *,
    bot_id: str,
    app_name: str,
    app_id: str,
    conv_summary: str | None,
) -> None:
    """Write a forge_complete event to the user's notification queue."""
    detail = (
        f"`{app_name}` is installed on `{bot_id}`. You can use it right away — "
        f"just talk to the bot like normal. Run `evo apps` to see it in your "
        f"installed list."
    )
    _notifications.append_event(
        shared_dir, user_key,
        kind="forge_complete",
        bot_id=bot_id,
        app_id=app_id,
        app_name=app_name,
        summary=conv_summary or app_name,
        detail=detail,
    )


def _emit_failed(
    shared_dir: Path,
    user_key: str,
    *,
    bot_id: str,
    app_name: str,
    app_id: str,
    conv_summary: str | None,
    reason: str,
) -> None:
    """Write a forge_failed event with the user-facing reason."""
    detail = reason or "an unknown error during the build"
    _notifications.append_event(
        shared_dir, user_key,
        kind="forge_failed",
        bot_id=bot_id,
        app_id=app_id,
        app_name=app_name,
        summary=conv_summary or app_name,
        detail=detail,
    )
