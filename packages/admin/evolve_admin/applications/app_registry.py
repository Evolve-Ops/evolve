"""
app_registry.py — Regenerate per-bot INSTALLED_APPS.md from manifest state.

Closes the bot-awareness gap surfaced during the manifest-coverage
investigation: forge installs scripts/journal.py on a bot but the bot's
LLM has no automated way to know that file is now a callable capability
versus a random orphan script. Hand-curated patterns (team_bot_a's CAPABILITY
DEPLOYMENT PROTOCOL, team_bot_c's TASKS.md cross-ref) work but don't update
when new apps land.

This module generates a single file — INSTALLED_APPS.md — that the bot
reads at session start. Source of truth: workspace/manifests/<app>.json.
One section per active app. Imperative voice with "USE THIS" framing
because that's the pattern bots have organically converged on for
their hand-curated capability sections.

The generator is best-effort against existing manifests. For apps with
no manifest.usage block (most existing ones), it falls back to:
  - description + identity.purpose for "how to use"
  - example_triggers + capability_tags for hint words
  - interface_contract.cli for the under-the-hood invocation
The output gets richer as the usage block gets populated.

The `usage` block was introduced as a top-level manifest field in schema
v10 (H-1). For backward compat we ALSO honor `identity["usage"]` since
some early forge-built manifests stored it there before the field was
promoted; on first encounter the renderer reads either location.

Called from:
  - forge_engine._apply_forge_output (post-approval)
  - scanner.scan_workspace_pipeline (post-Phase 5)
  - evolve-admin application regenerate-apps-md <bot> (CLI)
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_bot_workspace
from .manifest import (
    MANIFEST_DEFINITION_DEFINED,
    ApplicationManifest,
    list_manifests,
)


INSTALLED_APPS_FILENAME = "INSTALLED_APPS.md"

# Statuses that belong in the operator-facing app list. Paused/deprecated/
# hidden/dormant manifests stay in the manifest dir but should not be in
# the bot's "what can I do" view.
_VISIBLE_STATUSES = {"active", "draft", "approved"}


# ── Rendering ─────────────────────────────────────────────────────────────────


def _trim_at_word_boundary(s: str, max_len: int = 140) -> str:
    """Trim *s* to <= max_len, breaking at the last whitespace so we
    never end a header mid-word. Drops trailing punctuation/whitespace.
    """
    s = s.strip()
    if len(s) <= max_len:
        return s.rstrip(",.;:")
    cut = s[:max_len]
    # Prefer the last space before max_len so we don't dangle a partial word.
    space = cut.rfind(" ")
    if space >= max_len // 2:  # only honor if it's not absurdly early
        cut = cut[:space]
    return cut.rstrip(",.;: ") + "…"


def _one_line_desc(m: ApplicationManifest) -> str:
    """Best-effort one-line summary for the section header."""
    for source in (
        (m.description or "").strip(),
        ((m.identity or {}).get("purpose") or "").strip(),
    ):
        if not source:
            continue
        # First-sentence cut if it lands within the limit.
        for sep in (". ", "\n"):
            idx = source.find(sep)
            if 0 < idx <= 140:
                return source[:idx].rstrip(",.;: ")
        return _trim_at_word_boundary(source, 140)
    return ""


def _is_paraphrase(short: str, longer: str) -> bool:
    """Best-effort: True when *short* is essentially a prefix or duplicate
    of *longer*. Used to suppress "how to use" pasting both description
    and identity.purpose when one is a near-restatement of the other."""
    if not short or not longer:
        return False
    s, l = short.lower().strip(), longer.lower().strip()
    if s == l:
        return True
    # Prefix-y duplicate (security_bot's Heartbeat case)
    if l.startswith(s) or s.startswith(l):
        return True
    # Pick the shorter, see if most of it appears verbatim in the other
    if s in l or l in s:
        return True
    return False


def _usage_block(m: ApplicationManifest) -> dict:
    """Return the manifest's usage dict, looking in both schema slots.

    Top-level ``manifest.usage`` (schema v10, H-1) is the canonical home.
    ``manifest.identity["usage"]`` is honored as a fallback for early
    forge-built manifests that nested it under identity before the field
    was promoted. Returns an empty dict when neither is populated — the
    renderer's other helpers handle that case via description/tags
    fallbacks.
    """
    top = getattr(m, "usage", None)
    if isinstance(top, dict) and top:
        return top
    nested = m.identity.get("usage") if isinstance(m.identity, dict) else None
    if isinstance(nested, dict):
        return nested
    return {}


def _how_to_use(m: ApplicationManifest) -> str:
    """Plain-language usage paragraph.

    Prefers manifest.usage.how_to_use when set. Falls back to description /
    identity.purpose, deduping the two when one is a paraphrase of the other.
    """
    usage = _usage_block(m)
    ht = (usage.get("how_to_use") or "").strip() if usage else ""
    if ht:
        return ht

    desc = (m.description or "").strip()
    purpose = ((m.identity or {}).get("purpose") or "").strip()

    if desc and purpose:
        if _is_paraphrase(desc, purpose) or _is_paraphrase(purpose, desc):
            # Keep the longer one; it usually carries strictly more info
            return purpose if len(purpose) > len(desc) else desc
        return f"{desc} {purpose}".strip()

    if desc:
        return desc
    if purpose:
        return purpose
    return "_(no description in manifest yet)_"


def _hint_words(m: ApplicationManifest) -> list[str]:
    """Words a user might say that should route to this app."""
    usage = _usage_block(m)
    if usage:
        tr = usage.get("trigger_recognition") or {}
        explicit = tr.get("hint_words")
        if isinstance(explicit, list) and explicit:
            return [str(w).strip() for w in explicit if str(w).strip()]

    # Fall back to capability_tags + session_keywords (scanner emits these
    # for attribution; they double as hint words for the LLM).
    out: list[str] = []
    for source in (
        getattr(m, "capability_tags", None) or [],
        getattr(m, "session_keywords", None) or [],
    ):
        for w in source:
            if isinstance(w, str) and w.strip() and w not in out:
                out.append(w.strip())
    return out[:12]  # keep it readable


def _trigger_pattern(m: ApplicationManifest) -> str:
    """One-sentence "when to fire" description from the usage block."""
    usage = _usage_block(m)
    if not usage:
        return ""
    tr = usage.get("trigger_recognition") or {}
    pattern = (tr.get("pattern") or "").strip()
    if not pattern:
        return ""
    requires_keyword = bool(tr.get("requires_keyword", False))
    if requires_keyword:
        return f"{pattern} **(only fire on an explicit hint word)**"
    return pattern


def infer_usage_model(manifest: dict) -> str:
    """Infer ``usage.model`` from manifest structure when the author didn't set it.

    Pure rule-based inference (no LLM). Used by:
      - The renderer below (fall back to inferred value when usage.model is empty)
      - Forge Phase 5a (persist the inferred value before the discoverability
        check fires the no_invocation_model finding)

    The four values cover ~all real cases; the rules below pick by which
    invocation surface the manifest actually carries:

      1. ``scheduled_actions[]`` non-empty AND at least one has a real timer
         mechanism (launchd / cron, NOT ``oc_heartbeat_instruction``) →
         ``"scheduled"``. Heartbeat instructions are passive nudges that
         remind the bot to consider this app's outputs; they don't make
         the app primarily scheduled.
      2. ``event_triggers[]`` non-empty → ``"event-driven"`` (responds to
         an incoming event; cli usually present for handler dispatch).
      3. ``interface_contract.cli[]`` has at least one ``command`` →
         ``"user-initiated"``.
      4. None of the above → ``"user-initiated"`` (permissive default; the
         caller still gets a value to render and the audit's discoverability
         check stops firing the no_invocation_model finding).

    Note this is a *display* / *fallback* inference, not a *correctness*
    guarantee. An author who genuinely meant ``"ambient"`` (bot decides from
    conversation context) needs to set it explicitly — there is no structural
    cue that distinguishes ambient from user-initiated.
    """
    for action in manifest.get("scheduled_actions") or []:
        if not isinstance(action, dict):
            continue
        mechanism = str(action.get("mechanism") or "").strip().lower()
        if mechanism and mechanism != "oc_heartbeat_instruction":
            return "scheduled"
    if manifest.get("event_triggers"):
        return "event-driven"
    contract = manifest.get("interface_contract") or {}
    cli = contract.get("cli") or []
    for entry in cli:
        if isinstance(entry, dict) and (entry.get("command") or "").strip():
            return "user-initiated"
    return "user-initiated"


def _invocation_model(m: ApplicationManifest) -> str:
    """Label describing when the bot should invoke this app.

    Falls back to ``infer_usage_model`` against the manifest dict when
    ``usage.model`` is unset, so the renderer always carries a sensible
    "When to invoke" line. Authors can override by setting usage.model
    explicitly to "ambient" (no structural cue distinguishes ambient
    from user-initiated).
    """
    usage = _usage_block(m)
    model = str((usage or {}).get("model", "")).strip()
    if not model:
        # Build a minimal dict reflecting the structural cues
        # infer_usage_model reads; cheaper than asdict() and avoids
        # importing the full manifest module.
        contract = m.interface_contract or {}
        proxy = {
            "scheduled_actions": getattr(m, "scheduled_actions", None) or [],
            "event_triggers": getattr(m, "event_triggers", None) or [],
            "interface_contract": contract,
        }
        model = infer_usage_model(proxy)
    notes = {
        "user-initiated": "Invoke when the user asks.",
        "scheduled":      "Runs on cron; relay results when they arrive.",
        "event-driven":   "Runs in response to an external event; surface results.",
        "ambient":        "Decide whether to invoke based on conversation context.",
    }
    return f"`{model}` — {notes.get(model, '')}".rstrip(" —")


def _auto_capture_line(m: ApplicationManifest) -> str:
    """One line describing whether the bot auto-captures content."""
    usage = _usage_block(m)
    if not usage:
        return ""
    capt = usage.get("auto_capture") or {}
    if not capt.get("enabled"):
        return ""
    sources = [str(s).strip() for s in (capt.get("sources") or []) if str(s).strip()]
    src_line = f" from {', '.join(sources)}" if sources else ""
    return f"**Auto-capture enabled**{src_line} — capture matching content without being told to."


def _bot_voice_examples(m: ApplicationManifest) -> list[str]:
    usage = _usage_block(m)
    if not usage:
        return []
    examples = usage.get("bot_voice_examples") or []
    return [str(e).strip() for e in examples if str(e).strip()][:6]


def _scope_lines(m: ApplicationManifest) -> tuple[list[str], list[str]]:
    """Return (includes, excludes) from the identity block."""
    identity = m.identity or {}
    includes = [s for s in (identity.get("scope_includes") or []) if isinstance(s, str) and s.strip()]
    excludes = [s for s in (identity.get("scope_excludes") or []) if isinstance(s, str) and s.strip()]
    return includes, excludes


def _cli_lines(m: ApplicationManifest) -> list[str]:
    """Render the interface_contract.cli list as concise command lines."""
    contract = m.interface_contract or {}
    cli = contract.get("cli") or []
    out: list[str] = []
    for entry in cli:
        if not isinstance(entry, dict):
            continue
        cmd = (entry.get("command") or "").strip()
        if not cmd:
            continue
        flags = entry.get("key_flags") or []
        flag_str = ""
        if isinstance(flags, list) and flags:
            shown = [f for f in flags if isinstance(f, str)]
            if shown:
                flag_str = f"  *({', '.join(shown[:6])})*"
        out.append(f"`{cmd}`{flag_str}")
    return out


def _example_triggers(m: ApplicationManifest) -> list[str]:
    trig = getattr(m, "example_triggers", None) or []
    return [t for t in trig if isinstance(t, str) and t.strip()][:5]


def _section(m: ApplicationManifest) -> str:
    """Render one app's section."""
    name = m.display_name or m.name or m.id
    one_line = _one_line_desc(m)
    header = f"## {name}"
    if one_line:
        header = f"{header} — {one_line}"

    lines: list[str] = [header, ""]

    model_line = _invocation_model(m)
    if model_line:
        lines.append(f"**When to invoke:** {model_line}")
        lines.append("")

    how = _how_to_use(m)
    lines.append(f"**How to use.** {how}")
    lines.append("")

    pattern = _trigger_pattern(m)
    if pattern:
        lines.append(f"**Trigger pattern.** {pattern}")
        lines.append("")

    hints = _hint_words(m)
    if hints:
        lines.append("**Hint words to recognize in user messages:** " +
                     ", ".join(f"`{w}`" for w in hints))
        lines.append("")

    capture_line = _auto_capture_line(m)
    if capture_line:
        lines.append(capture_line)
        lines.append("")

    triggers = _example_triggers(m)
    if triggers:
        lines.append("**Example user messages that should route here:**")
        for t in triggers:
            lines.append(f"- {t}")
        lines.append("")

    voice = _bot_voice_examples(m)
    if voice:
        lines.append("**Sample bot replies while using this app:**")
        for v in voice:
            lines.append(f"- {v}")
        lines.append("")

    includes, excludes = _scope_lines(m)
    if includes:
        lines.append("**What this app does:**")
        for s in includes:
            lines.append(f"- {s}")
        lines.append("")
    if excludes:
        lines.append("**What this app does NOT do:**")
        for s in excludes:
            lines.append(f"- {s}")
        lines.append("")

    cli = _cli_lines(m)
    if cli:
        lines.append("**How to invoke (under the hood):**")
        for c in cli:
            lines.append(f"- {c}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def app_ref(m: ApplicationManifest) -> str:
    """The stable identifier a caller passes to ``expand_app(app_id)``.

    Prefers the manifest ``id`` (stable across renames); falls back to
    ``name`` then ``display_name``. The expand route resolves flexibly
    (id / name / display_name, case-insensitive) so a model that passes
    the display name still hits — this is only the *canonical* form shown
    in the Tier-1 menu.
    """
    return (m.id or m.name or (m.display_name or "")).strip()


def is_defined(m: ApplicationManifest) -> bool:
    """True when the manifest is operator-vouched (``definition_status: defined``).

    The Tier-1 always-on menu lists defined apps only, so unvouched
    ``discovered`` scanner churn never enters every bot's per-session
    context (spec OQ-3). ``expand_app`` still resolves discovered apps on
    demand — the filter is on the always-on menu, not on Tier-2 lookup.
    """
    return (getattr(m, "definition_status", "") or "").strip().lower() == \
        MANIFEST_DEFINITION_DEFINED


def render_app_detail_section(m: ApplicationManifest) -> str:
    """Tier-2 detail for a single app — the on-demand payload of ``expand_app``.

    Public wrapper over ``_section`` (the same per-app block used in
    INSTALLED_APPS.md): when-to-invoke, how-to-use, hint words, example
    triggers, scope, and CLI invocations. Kept as a clean, tool-agnostic
    string so the payload is upgradeable to a native OpenClaw deferred-tool
    primitive if/when one lands (spec §2.1 forward-compat) without reshaping
    callers.

    Read-only projection of manifest fields already surfaced to the bot —
    it carries no secret/credential content (the manifest never holds any) and
    does no caller-directed write.
    """
    return _section(m)


def render_installed_apps_md(bot_id: str, manifests: list[ApplicationManifest]) -> str:
    """Render the full INSTALLED_APPS.md document."""
    visible = [m for m in manifests if (m.status or "active") in _VISIBLE_STATUSES]
    visible.sort(key=lambda m: (m.display_name or m.name or m.id).lower())

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    head = [
        "# Installed Apps",
        "",
        f"_Generated {now_iso} from {len(visible)} active manifest(s). "
        f"Source of truth: `workspace/manifests/<app>.json`. "
        f"This file is regenerated automatically — do not edit it directly._",
        "",
        "USE THESE FOR THE THINGS THEY DO. When a user message looks like "
        "something one of these apps handles — check the **Hint words** and "
        "**Example user messages** sections below — call the app's CLI rather "
        "than improvising a one-off response.",
        "",
    ]

    if not visible:
        head.append("_No installed apps yet. Apps appear here after they're "
                    "approved through forge or discovered by the workspace scanner._")
        return "\n".join(head) + "\n"

    # Trailing blank line on the head ensures the first ## section starts
    # on its own line (markdown convention; without it, some renderers
    # treat the header as a continuation of the preceding paragraph).
    head.append("")
    body = [_section(m) for m in visible]
    return "\n".join(head) + "\n".join(body)


# ── Writing ───────────────────────────────────────────────────────────────────


def _write_md_bytes(path: Path, content: bytes) -> None:
    """Atomic write of INSTALLED_APPS.md. Direct first; sudo cp fallback.

    Mirrors manifest._write_manifest_bytes — workspace root is bot-owned
    but evolve has a workspace ACL allowing add_file. When that ACL isn't
    yet set on a fresh bot, the sudoers grant covers the /tmp + cp path.
    """
    try:
        path.write_bytes(content)
        return
    except PermissionError:
        pass

    fd, tmp_path = tempfile.mkstemp(dir="/tmp", prefix="evolve-", suffix=".md")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass
        result = subprocess.run(
            ["sudo", "/bin/cp", tmp_path, str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise PermissionError(
                f"INSTALLED_APPS.md write failed (direct EACCES + sudo cp rc="
                f"{result.returncode}): {result.stderr.strip()[:200]}"
            )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── AGENTS.md cross-reference section (H-3) ──────────────────────────────────

# Marker bookends. Anything between these gets replaced on each regenerate;
# anything outside them is left strictly alone. Operators can move the
# section around in AGENTS.md (cut + paste the whole block) — the next
# regenerate finds it by markers, not by position.
_AGENTS_MARKER_BEGIN = "<!-- BEGIN EVOLVE-INSTALLED-APPS -->"
_AGENTS_MARKER_END   = "<!-- END EVOLVE-INSTALLED-APPS -->"

# Note: there is intentionally NO durable AGENTS.md section for skills +
# configured-integration tools. CA-P1 (#3080) tried that here, but it only
# spliced on deploy_bot/forge/scanner (never on release promote / repo-pull)
# and AGENTS.md is read per-session, so it never reached already-deployed or
# long-running bots. Those capabilities now ship via the per-turn
# [INSTALLED CAPABILITIES] push (analyzer/capability_block.py rendered from
# session_surface ``--capabilities-only``, injected every turn by the plugin's
# before_prompt_build hook). Apps keep this durable AGENTS.md section because
# they carry detailed CLI invocations + hint words (see INSTALLED_APPS.md) and
# change rarely; the per-turn push covers the gap apps don't.
# Spec: internal/spec-bot-capability-awareness-2026-06-22.md §5 (P1 delivery).


def _render_agents_md_section(manifests: list[ApplicationManifest]) -> str:
    """Render the Tier-1 capability-index menu injected into AGENTS.md.

    The always-on, budget-conscious top tier of the app capability index
    (spec ``internal/spec-app-invocation-just-works-2026-06-29.md`` §2.1): one
    terse line per app — ``name — purpose`` — plus a pointer to the
    ``expand_app(app_id)`` tool that pulls the full command surface (Tier-2)
    into context ONLY when the model engages that app. A one-line entry is
    ~15–30 tokens, so a pod of 10–20 apps is a few hundred tokens always
    resident (OQ-6: no cap; lines kept tight).

    Lists **defined** (operator-vouched) apps only (OQ-3): unvouched
    ``discovered`` scanner drafts churn and don't belong in every session's
    always-on budget. They remain reachable — ``expand_app`` resolves a
    discovered app by id on demand, and INSTALLED_APPS.md still lists them.

    This is a *menu*, not a set of triggers. The model still reads intent and
    decides in natural language whether an app fits — the menu makes it
    *aware*, it does not pattern-force any app (the "just works, not rigid"
    contract of §2.1 / principle-just-works).
    """
    visible = [m for m in manifests if (m.status or "active") in _VISIBLE_STATUSES]
    defined = [m for m in visible if is_defined(m)]
    defined.sort(key=lambda m: (m.display_name or m.name or m.id).lower())
    # Count of visible-but-unvouched apps — used only to soften the empty state
    # so we never falsely claim "no apps" when discovered drafts exist. We do
    # NOT list them (that's the churn OQ-3 keeps out of always-on context).
    undefined_n = len(visible) - len(defined)

    lines: list[str] = [_AGENTS_MARKER_BEGIN]
    lines.append("## 🛠️ Installed Apps — USE THESE FOR THE THINGS THEY DO")
    lines.append("")

    if not defined:
        if undefined_n:
            lines.append(
                "No apps are ready to use yet — some are still being "
                "characterized. They'll appear here once confirmed."
            )
        else:
            lines.append(
                "You have no Evolve-managed apps installed yet. Apps appear "
                "here automatically after forge approval or scanner discovery."
            )
    else:
        n = len(defined)
        lines.append(
            f"You have **{n} Evolve-managed app{'s' if n != 1 else ''} "
            f"installed**. Each does a real job for the user. When a request "
            f"looks like something one of these handles, use it rather than "
            f"improvising a one-off — but it's your call: decide from what the "
            f"user actually means, not from a rigid keyword match."
        )
        lines.append("")
        lines.append(
            "To see exactly how to run one — its commands, arguments, and "
            "examples — call **`expand_app(\"<id>\")`** with the id shown in "
            "each line below. It loads the full usage on demand so you don't "
            "carry every app's manual in context."
        )
        lines.append("")
        for m in defined:
            name = m.display_name or m.name or m.id
            ref = app_ref(m)
            one = _one_line_desc(m)
            head = f"- **{name}** — {one}" if one else f"- **{name}**"
            lines.append(f"{head} → `expand_app(\"{ref}\")` for how to use it")
        lines.append("")
        lines.append(
            "Before telling a user you can't do something, check this list — "
            "one of these apps may already do it. `INSTALLED_APPS.md` has the "
            "same detail as a file if you'd rather read it there."
        )

    lines.append(_AGENTS_MARKER_END)
    return "\n".join(lines) + "\n"


def _splice_agents_md(
    existing: str,
    new_block: str,
    *,
    begin_marker: str = _AGENTS_MARKER_BEGIN,
    end_marker: str = _AGENTS_MARKER_END,
) -> str:
    """Replace an existing marker block in *existing* with *new_block*,
    or append *new_block* at the end if no markers are present.

    Idempotent — re-running with the same new_block produces the same
    output. Whitespace outside the markers is preserved verbatim. The
    marker pair defaults to the apps markers; the parameters are kept
    generic so a future marker-bounded section can reuse this helper.
    """
    begin = existing.find(begin_marker)
    end = existing.find(end_marker)
    if begin >= 0 and end > begin:
        # Replace the span from BEGIN through END (inclusive of END line).
        # End-of-line after the END marker is preserved if present.
        end_after = end + len(end_marker)
        # Include the trailing newline after END if there is one, so we
        # don't double up on blanks.
        if end_after < len(existing) and existing[end_after] == "\n":
            end_after += 1
        # Trim the new block's trailing newline if existing didn't have one,
        # to match the original line-ending discipline.
        block = new_block if new_block.endswith("\n") else (new_block + "\n")
        return existing[:begin] + block + existing[end_after:]

    # No markers — append at end with a separating blank line.
    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    block = new_block if new_block.endswith("\n") else (new_block + "\n")
    return existing + sep + block


def _read_agents_md(workspace: Path) -> str | None:
    """Read AGENTS.md from the bot workspace. Returns None if missing."""
    path = workspace / "AGENTS.md"
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except PermissionError:
        # Fall back to sudo cat (covered by an existing sudoers grant)
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                return r.stdout
        except Exception:
            return None
    except Exception:
        return None
    return None


def _update_agents_md_section(
    bot_id: str,
    manifests: list[ApplicationManifest],
) -> Path | None:
    """Update (or create) the EVOLVE-INSTALLED-APPS section in AGENTS.md.

    Only touches the apps marker block. Anything else in AGENTS.md — the
    bot's hand-curated identity, conduct rules, capability protocols — is
    preserved verbatim. Returns the written path, or None if the workspace
    is unresolvable or AGENTS.md is missing.

    Skills + configured-integration tools are NOT written here — they ship
    via the per-turn [INSTALLED CAPABILITIES] push (see the marker note
    above and internal/spec-bot-capability-awareness-2026-06-22.md §5).

    AGENTS.md must already exist on the bot. We don't create it from
    scratch; every deployed bot has one from set_evolve_read_acl /
    deploy_bot. If it's missing, that's an upstream config issue, not
    something this generator should paper over.
    """
    try:
        workspace = get_bot_workspace(bot_id)
    except Exception:
        workspace = None
    if workspace is None:
        return None
    existing = _read_agents_md(workspace)
    if existing is None:
        return None

    new_block = _render_agents_md_section(manifests)
    updated = _splice_agents_md(existing, new_block)

    # Only write if content actually changed — preserves mtime when nothing
    # in the manifest set has shifted, and avoids triggering unnecessary
    # downstream hooks (file watchers, pulls, etc.).
    if updated == existing:
        return workspace / "AGENTS.md"

    out_path = workspace / "AGENTS.md"
    try:
        _write_md_bytes(out_path, updated.encode("utf-8"))
    except Exception:
        return None
    return out_path


# ── Top-level entry point ─────────────────────────────────────────────────────


def regenerate_installed_apps_md(bot_id: str, shared_dir: Path) -> Path | None:
    """Regenerate the bot's INSTALLED_APPS.md from current manifest state.

    Reads every visible manifest for *bot_id* via the canonical
    workspace/manifests/ path and writes the rendered document to
    workspace/INSTALLED_APPS.md.

    Returns the written path on success, or None if the bot's workspace
    can't be resolved (most likely: bot_id not in network.json or its
    openclaw.json is unreadable). Idempotent — re-running regenerates
    from current state without ratcheting.

    Errors are caught and logged but do not raise. Callers (forge,
    scanner) treat this as a best-effort enrichment step that must not
    block their own completion.
    """
    try:
        workspace = get_bot_workspace(bot_id)
    except Exception:
        workspace = None
    if workspace is None:
        return None

    try:
        manifests = list_manifests(shared_dir, bot_id)
    except Exception:
        return None

    try:
        content = render_installed_apps_md(bot_id, manifests)
    except Exception:
        return None

    out_path = workspace / INSTALLED_APPS_FILENAME
    try:
        _write_md_bytes(out_path, content.encode("utf-8"))
    except Exception:
        return None

    # Also update the marker-bounded EVOLVE-INSTALLED-APPS section in AGENTS.md
    # (H-3) so the bot's primary anchor file cross-references the detailed
    # INSTALLED_APPS.md. Best-effort — if AGENTS.md is missing or unwritable,
    # we leave the INSTALLED_APPS.md write in place and quietly skip the
    # cross-ref. (Skills + integration tools ship via the per-turn capability
    # push, not AGENTS.md — see _update_agents_md_section.)
    try:
        _update_agents_md_section(bot_id, manifests)
    except Exception:
        pass

    return out_path
