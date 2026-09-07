"""
app_blueprint.py — Parse embedded app blueprints into a renderable plan.

C1.a's provisioner renders ``AGENTS.md`` / ``SOUL.md`` /
``exec-approvals.json`` from the template directory. V1.1-1 extends that
to embedded **applications**: each app blueprint JSON's ``build_spec``
contains the actual file content (scripts, plists, etc.) for the app,
formatted using the forge convention of ``## FILE: <path>`` blocks
inside the markdown.

Provenance markers are stamped per ``provenance.embed_marker()`` after
write (the same convention forge_engine._materialize_files uses), so the
later admin-UI tooling that scans for orphan files works unchanged for
template-installed apps.

The parser is **pure** — it returns a plan; no filesystem or launchctl
side-effects. Side-effects live in :mod:`cli_integration` so dry-run is
naturally side-effect-free.

Module split rationale
──────────────────────
Kept separate from ``provisioner.py`` because:
  - The ``## FILE:`` block extraction is identical to what
    ``forge_engine._parse_file_blocks`` already does for full forge
    builds (gallery installs). Putting it in its own module makes the
    parallel obvious to a reader and keeps the existing provisioner
    focused on declarative-only artifacts (AGENTS.md/SOUL.md/exec
    approvals).
  - LaunchAgent destination classification needs its own logic
    (workspace vs ``~/Library/LaunchAgents`` vs
    ``/Library/LaunchDaemons``) that would otherwise bloat
    ``provisioner.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from .errors import TemplateValidationError
from .provisioner import render_template_vars


# ── ## FILE: block parser ─────────────────────────────────────────────────────
#
# Same format as forge_engine.BUILTIN_BUILDER_PROMPT / _parse_file_blocks.
# Match: ``## FILE: <path>`` on its own line, followed by an opening
# fenced code block (with optional language tag), the file content
# (non-greedy), and a closing fence on its own line.

_FILE_BLOCK_RE = re.compile(
    r'^##\s+FILE:\s+(\S+)\s*\n'   # header line: ## FILE: path
    r'```[^\n]*\n'                  # opening fence (optional lang tag)
    r'(.*?)'                        # file content (non-greedy)
    r'^```\s*$',                    # closing fence on its own line
    re.MULTILINE | re.DOTALL,
)


# ── Path classification ───────────────────────────────────────────────────────


def classify_path(raw_path: str) -> tuple[str, str, str | None]:
    """Classify a blueprint file path into a (destination, normalised_path, label) tuple.

    Returns:
      destination:
        - ``"workspace"`` — relative path under the bot's workspace root.
        - ``"launchagent"`` — user-level launchd plist at
          ``~/Library/LaunchAgents/<...>.plist``.
        - ``"launchdaemon"`` — system-wide launchd plist at
          ``/Library/LaunchDaemons/<...>.plist``.
      normalised_path:
        Workspace destinations have any leading ``./`` stripped.
        LaunchAgent destinations keep the path **stem** (so callers can
        join with the bot user's home). LaunchDaemon destinations keep
        the absolute path.
      label:
        The launchd Label (plist stem) for launchagent / launchdaemon
        destinations; ``None`` for workspace destinations.

    Examples:
      ``"scripts/briefing.py"`` →
        ``("workspace", "scripts/briefing.py", None)``
      ``"~/Library/LaunchAgents/com.team_bot_a.morning.plist"`` →
        ``("launchagent", "Library/LaunchAgents/com.team_bot_a.morning.plist",
        "com.team_bot_a.morning")``
      ``"/Library/LaunchDaemons/ai.openclaw.team_bot_a.briefing.plist"`` →
        ``("launchdaemon",
        "/Library/LaunchDaemons/ai.openclaw.team_bot_a.briefing.plist",
        "ai.openclaw.team_bot_a.briefing")``

    Refuses paths with absolute references (``..`` segments) for the
    workspace destination — those are not template-renderable and
    suggest a malformed blueprint.
    """
    p = raw_path.strip()
    if not p:
        raise TemplateValidationError(
            "(app_blueprint)",
            ["empty FILE: path in blueprint"],
        )

    # Security: reject ANY path containing ``..`` segments BEFORE
    # branching on prefix. After template-var substitution a malicious
    # ``bot_id`` (or a hand-authored blueprint) can embed ``..`` into an
    # otherwise-launchd-rooted path; ``/Library/LaunchDaemons/x/../../etc/
    # passwd.plist`` matches the launchdaemon prefix and would otherwise
    # be handed to ``sudo /bin/cp`` which dereferences ``..`` at syscall
    # level. The fix mirrors the workspace branch's ``..`` rejection but
    # applies it universally. Resolving via :class:`PurePosixPath` and
    # checking ``parts`` is enough (no filesystem access). Same shape as
    # the workspace check at the bottom of this function — kept identical
    # so the rejection signature is consistent across all three
    # destinations.
    if any(part == ".." for part in PurePosixPath(p).parts):
        raise TemplateValidationError(
            "(app_blueprint)",
            [f"path {raw_path!r} contains '..' (refused for security)"],
        )

    # User-relative launchd plist (~/Library/LaunchAgents/...)
    if p.startswith("~/"):
        rest = p[2:]  # strip "~/"
        # Treat anything under "Library/LaunchAgents/" as a launchagent
        if rest.startswith("Library/LaunchAgents/") and rest.endswith(".plist"):
            # Belt-and-suspenders: re-check ``rest`` for ``..``. The
            # blanket check above already rejects it, but keep the
            # destination-local assertion so a future refactor can't
            # silently regress.
            if any(part == ".." for part in PurePosixPath(rest).parts):
                raise TemplateValidationError(
                    "(app_blueprint)",
                    [f"launchagent path {raw_path!r} contains '..'"],
                )
            label = PurePosixPath(rest).stem
            return ("launchagent", rest, label)
        # Other ~/ paths are unexpected — refuse rather than guess
        raise TemplateValidationError(
            "(app_blueprint)",
            [
                f"unexpected ~-rooted path {raw_path!r}; only "
                f"~/Library/LaunchAgents/<label>.plist is supported"
            ],
        )

    # Absolute system-level launchd plist. We require the path to start
    # with the literal ``/Library/LaunchDaemons/`` prefix AND have no
    # ``..`` segments (rejected above). After those checks, resolve as a
    # PurePosixPath and verify the resolved path is still rooted at
    # ``/Library/LaunchDaemons/`` — that's the substrate's only permitted
    # absolute root. ``Path.resolve()`` would consult the live filesystem
    # (symlinks etc.); we deliberately use the lexical PurePosixPath
    # equivalent because we want to reject ``..`` BEFORE any filesystem
    # access — defense in depth against TOCTOU.
    if p.startswith("/Library/LaunchDaemons/") and p.endswith(".plist"):
        # The ``..`` check above already guarantees no escape, but we
        # double-check that the literal prefix is preserved after
        # PurePosixPath normalisation (which collapses doubled slashes,
        # etc.).
        normalised_abs = str(PurePosixPath(p))
        if not normalised_abs.startswith("/Library/LaunchDaemons/"):
            raise TemplateValidationError(
                "(app_blueprint)",
                [
                    f"launchdaemon path {raw_path!r} does not normalise "
                    f"to a /Library/LaunchDaemons/ root (got "
                    f"{normalised_abs!r})"
                ],
            )
        # Reject a path that is exactly the directory or escapes via
        # extra components after PurePosixPath normalisation. The plist
        # filename must sit directly under the dir or in a sub-dir; we
        # don't allow it to be EMPTY or to end with a slash.
        if normalised_abs == "/Library/LaunchDaemons" or normalised_abs.endswith("/"):
            raise TemplateValidationError(
                "(app_blueprint)",
                [f"launchdaemon path {raw_path!r} is not a valid plist file"],
            )
        label = PurePosixPath(normalised_abs).stem
        return ("launchdaemon", normalised_abs, label)

    # Any other absolute path is refused — too dangerous for an app to
    # rewrite arbitrary system files.
    if p.startswith("/"):
        raise TemplateValidationError(
            "(app_blueprint)",
            [
                f"absolute path {raw_path!r} outside "
                f"/Library/LaunchDaemons/ is not allowed in blueprints"
            ],
        )

    # Workspace path: already covered by the universal ``..`` check at
    # the top of this function. Tidy: strip a leading ``./`` once for
    # storage.
    normalised = p
    while normalised.startswith("./"):
        normalised = normalised[2:]

    return ("workspace", normalised, None)


# ── Plan dataclasses ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BlueprintFile:
    """One file the embedded-app provisioner will write.

    Attributes:
      raw_path:        Path exactly as written in the ``## FILE:`` header.
      destination:     ``"workspace"`` | ``"launchagent"`` | ``"launchdaemon"``.
      normalised_path: Path with workspace destinations stripped of
                       leading ``./``; launchagent destinations keep the
                       ``Library/LaunchAgents/<label>.plist`` stem so the
                       caller can join with the bot user's home.
      content:         File content with template-vars substituted.
      launchd_label:   ``Label`` value of the plist (filename stem) when
                       destination is ``launchagent`` / ``launchdaemon``;
                       ``None`` otherwise.
      executable:      ``True`` for ``.sh`` / ``.bash`` / ``.zsh`` files
                       — callers should ``chmod +x`` after write.
    """

    raw_path: str
    destination: str
    normalised_path: str
    content: str
    launchd_label: str | None
    executable: bool


@dataclass(frozen=True)
class ParsedBlueprint:
    """Result of parsing one embedded app blueprint.

    Attributes:
      app_id:        ``id`` field from the blueprint JSON
                     (e.g. ``"app_morning_briefing"``).
      app_name:      ``name`` from blueprint JSON
                     (e.g. ``"Morning Briefing"``).
      embedded_path: ``apps/<file>.json`` path under the template dir
                     (carried for traceability).
      files:         Tuple of :class:`BlueprintFile` to render.
      launchd_labels: Tuple of unique Labels across launchagent /
                     launchdaemon files (handy for rollback bookkeeping).
      warnings:      Non-blocking issues discovered while parsing
                     (e.g. ``## FILE:`` block with no content body).
    """

    app_id: str
    app_name: str
    embedded_path: str
    files: tuple[BlueprintFile, ...]
    launchd_labels: tuple[str, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        """True iff the blueprint had no ``## FILE:`` blocks at all."""
        return not self.files


# ── Public parser ─────────────────────────────────────────────────────────────


def parse_app_blueprint(
    *,
    blueprint: dict[str, Any],
    embedded_path: str,
    vars: dict[str, Any] | None = None,
) -> ParsedBlueprint:
    """Parse one embedded-app blueprint into a :class:`ParsedBlueprint`.

    Args:
      blueprint:     The parsed JSON dict from
                     ``apps/<file>.json`` (as loaded by
                     :func:`loader.load_template`).
      embedded_path: The relative path inside the template's ``apps/``
                     directory (e.g. ``"morning_briefing.json"``) —
                     carried so reviewers can trace where each file
                     came from in case of a render failure.
      vars:          Substitution map for ``{name}`` / ``{{name}}``
                     placeholders inside file content. Same convention
                     as :func:`provisioner.render_template_vars`.

    Returns:
      A :class:`ParsedBlueprint` with one :class:`BlueprintFile` per
      ``## FILE:`` block found in the blueprint's ``build_spec`` (which
      is a markdown-formatted string).

    Raises:
      TemplateValidationError — if a ``FILE:`` header references an
        unsafe path (absolute path outside /Library/LaunchDaemons/, or
        a workspace path containing ``..``).
    """
    app_id = str(blueprint.get("id") or "").strip()
    app_name = str(
        blueprint.get("name") or blueprint.get("display_name") or app_id
    ).strip()

    build_spec = blueprint.get("build_spec")
    if not isinstance(build_spec, str) or not build_spec.strip():
        # No build_spec — nothing to render. Not an error: e.g. the
        # gallery-reference application path doesn't use build_spec.
        return ParsedBlueprint(
            app_id=app_id,
            app_name=app_name,
            embedded_path=embedded_path,
            files=(),
            launchd_labels=(),
        )

    vars = vars or {}
    files: list[BlueprintFile] = []
    labels: list[str] = []
    seen_paths: dict[str, int] = {}
    warnings: list[str] = []

    # The forge convention is "last occurrence wins" if a path is
    # duplicated; mirror that here. Iterate then dedup-by-path at the
    # end while preserving document order based on FIRST occurrence
    # (matches how a reader scans the blueprint top-to-bottom).
    matches = list(_FILE_BLOCK_RE.finditer(build_spec))
    if not matches:
        # Empty build_spec is normal (blueprint may be narrative-only);
        # callers decide whether to warn.
        return ParsedBlueprint(
            app_id=app_id,
            app_name=app_name,
            embedded_path=embedded_path,
            files=(),
            launchd_labels=(),
        )

    # First pass: keep last-write-wins for duplicate paths but preserve
    # the position of the first occurrence so files render in the order
    # the blueprint introduces them.
    block_by_path: dict[str, tuple[int, str]] = {}
    for i, m in enumerate(matches):
        raw_path = m.group(1).strip()
        body = m.group(2)  # last char of body is "\n" before closing fence
        # The closing fence pattern `^```\s*$` requires that the closing
        # fence is on its own line; the captured body is everything up
        # to (but not including) that closing fence. The body therefore
        # ends with the newline before the fence — strip ONE trailing
        # newline so the rendered file isn't bloated by an extra blank
        # line. (Match forge_engine — its parser preserves the body
        # as-is which keeps trailing newline; we keep parity.)
        if raw_path in block_by_path:
            # Last-write-wins: keep the earliest index but update body.
            first_idx, _ = block_by_path[raw_path]
            block_by_path[raw_path] = (first_idx, body)
            warnings.append(
                f"duplicate FILE: header for {raw_path!r}; last block wins"
            )
        else:
            block_by_path[raw_path] = (i, body)

    # Sort by first-occurrence index to preserve document order.
    ordered = sorted(block_by_path.items(), key=lambda kv: kv[1][0])

    for raw_path, (_idx, body) in ordered:
        # Substitute template vars in BOTH the path AND the content.
        # The path can reference {bot_id} for plist filenames, see
        # morning_briefing.json's "com.{bot_id}.morning-briefing.plist".
        rendered_path = render_template_vars(raw_path, vars)
        rendered_content = render_template_vars(body, vars)

        # Empty bodies are likely placeholder/forgot-to-fill scenarios;
        # surface as a warning but still render (matches forge behaviour:
        # empty files are written without complaint).
        if not rendered_content.strip():
            warnings.append(
                f"FILE: {raw_path!r} body is empty after substitution"
            )

        destination, normalised, label = classify_path(rendered_path)

        executable = False
        suffix = PurePosixPath(normalised).suffix.lower()
        if suffix in (".sh", ".bash", ".zsh"):
            executable = True

        if label and label not in labels:
            labels.append(label)

        files.append(BlueprintFile(
            raw_path=rendered_path,
            destination=destination,
            normalised_path=normalised,
            content=rendered_content,
            launchd_label=label,
            executable=executable,
        ))

    return ParsedBlueprint(
        app_id=app_id,
        app_name=app_name,
        embedded_path=embedded_path,
        files=tuple(files),
        launchd_labels=tuple(labels),
        warnings=tuple(warnings),
    )
