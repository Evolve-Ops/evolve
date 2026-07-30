"""env_portability_lint.py — flag environment assumptions that break portability.

Spec: docs/spec-forge-side-effects-2026-06-02.md §14. PR 7 of that spec
adds this as a critic-cycle check that catches Cluster-C patterns the
2026-06-02 audit surfaced on personal-bot ea-pack and task-manager:

  - `/Users/Shared/evolve-venv/bin/python3` hardcoded path that may not
    exist on a fresh install (broken_path finding on ea-pack).
  - `systemsetup -gettimezone` requires admin on macOS, silently falls
    back to UTC, **inherited verbatim from the gallery build_spec** so
    every install reproduces the bug (behavior_mismatch on task-manager).

Four check families:

  H1 — hardcoded absolute paths outside the bot's workspace, not
       declared in ``manifest.requirements.system[]``
  H2 — sudo-required macOS commands (``systemsetup``,
       ``launchctl bootstrap``, ``pmset``, ``nvram``, ``scutil --set``)
       without an explicit ``sudo`` prefix AND no
       ``manifest.requirements.privileged: true`` declaration
  H3 — Python invocation via hardcoded venv path
       (``/opt/.../bin/python3``, ``/Users/.../venv/bin/python3``)
       instead of ``#!/usr/bin/env python3`` shebang
  H4 — the specific ``systemsetup -gettimezone`` + UTC fallback pattern
       — covered by H2 but called out separately so the operator sees
       a focused remediation suggestion

Pure Python — no LLM, no network. Operates over file paths + contents
and the manifest. Runs alongside ``orphan_check`` /
``constraint_critic`` / ``negative_path_tests`` in Phase 2.5.

Symmetric lint over gallery build_specs (spec §14.3) is deferred — the
bot-side lint catches the bug as it appears in the produced code, which
is the more immediate need.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# ── Sudo-required macOS commands (H2) ───────────────────────────────────────
# Documented in `man systemsetup`, `man launchctl`, `man pmset`, etc. as
# requiring admin / root. The bot LLM frequently copies snippets that use
# them without a sudo prefix; on macOS these silently fail or fall back to
# a degraded path (UTC for timezone, no-op for pmset, etc.).
_SUDO_REQUIRED_PATTERNS = (
    # Match systemsetup followed by any -<word> flag — covers both shell
    # form (`systemsetup -gettimezone`) and Python subprocess list form
    # (`["systemsetup", "-gettimezone"]`).
    ("systemsetup-gettimezone", re.compile(r"systemsetup['\"]?[\s,]+['\"]?-gettimezone", re.IGNORECASE)),
    ("systemsetup", re.compile(r"\bsystemsetup\b['\"]?[\s,]+['\"]?-", re.IGNORECASE)),
    ("launchctl-bootstrap", re.compile(r"\blaunchctl\b['\"]?[\s,]+['\"]?(?:bootstrap|bootout)\b", re.IGNORECASE)),
    ("pmset", re.compile(r"\bpmset\b['\"]?[\s,]+['\"]?-?[a-z]+", re.IGNORECASE)),
    # `nvram boot-args=-v` — the key can contain `-`. The negative lookahead
    # `(?!print)` keeps `nvram -p` / `nvram print` (read-only) safe.
    ("nvram", re.compile(r"\bnvram\b\s+(?!print|-p\b)[A-Za-z_][A-Za-z0-9_\-]*=", re.IGNORECASE)),
    ("scutil-set", re.compile(r"\bscutil\b\s+--set\b", re.IGNORECASE)),
)

# Patterns that DO have a sudo wrapper — the lint must not double-fire on
# these. Matches at most one sudo prefix per call site.
_SUDO_PREFIX_RE = re.compile(r"\bsudo\b", re.IGNORECASE)

# ── Hardcoded venv python paths (H3) ────────────────────────────────────────
# Two known shapes:
#   /opt/<...>/bin/python3
#   /Users/<user>/<...>/venv/bin/python3
#   /usr/local/.../bin/python3
# vs. the portable shape: #!/usr/bin/env python3
# `(?:[^/\s"';]+/)*` matches zero-or-more directory segments after the
# top-level prefix, then the venv token must be the immediate parent of
# `bin/python`. This handles both `/opt/.venv/bin/python3` (no intermediate
# segments) and `/Users/Shared/evolve-venv/bin/python3` (one segment).
_HARDCODED_PY_PATH_RE = re.compile(
    r"(?P<path>(?:/opt|/Users|/usr/local|/Library)/"
    r"(?:[^/\s\"';]+/)*"
    r"(?:venv|env|\.venv|evolve-venv|virtualenv|conda)/bin/python\d?)"
)

_VENV_PREFIX_TOKENS = ("venv", ".venv", "env", "evolve-venv", "virtualenv", "conda")

# ── Hardcoded absolute paths (H1) ───────────────────────────────────────────
# Paths matching these prefixes are platform-/install-specific. We
# tolerate the bot's own workspace (/Users/{bot}/.openclaw/workspace/...)
# because that's where the app's own files live by design. Anything else
# under /Users, /opt, /usr/local, /Library, /etc, /var that's not also
# declared in manifest.requirements.system[] is suspicious.
_ABSOLUTE_PATH_RE = re.compile(
    r"['\"]?(?P<path>(?:/Users/|/opt/|/usr/local/|/Library/|/etc/|/var/)[^\s'\"`]+)"
)

# ── H4: systemsetup-gettimezone + UTC fallback ──────────────────────────────
# The specific pattern that lives in p-9bfa1c84's build_spec and got
# faithfully copied into the personal-bot task-manager. Catches the shape
# even when systemsetup isn't a literal substring (e.g. via subprocess
# call composition) by looking for the UTC fallback in a tz function.
_UTC_FALLBACK_RE = re.compile(
    r"return\s+ZoneInfo\(['\"]UTC['\"]\)",
)


@dataclass
class PortabilityFinding:
    """One env-portability concern surfaced by the lint."""
    file: str            # workspace-relative path
    line: int            # 1-based line number where the pattern matched
    family: str          # H1 | H2 | H3 | H4
    pattern: str         # specific subfamily (e.g. systemsetup-gettimezone, evolve-venv)
    snippet: str         # the offending line, trimmed
    severity: str        # "should-fix" | "info"
    suggestion: str      # one-line remediation

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "family": self.family,
            "pattern": self.pattern,
            "snippet": self.snippet,
            "severity": self.severity,
            "suggestion": self.suggestion,
        }


def lint_files(
    workspace: Path,
    files: list[str],
    *,
    manifest: dict | None = None,
) -> list[PortabilityFinding]:
    """Scan the given workspace-relative file paths for portability issues.

    Parameters
    ----------
    workspace
        Resolved workspace root. Findings report ``file`` paths relative
        to this.
    files
        Workspace-relative paths to scan. Non-existent files are silently
        skipped (forge can call this with the freshly-built file list
        before everything is on disk).
    manifest
        Optional manifest dict; if provided, ``requirements.system[]`` and
        ``requirements.privileged`` are honored as exemptions per spec
        §14.2. When omitted, no exemptions apply.

    Returns
    -------
    list[PortabilityFinding]
        One entry per pattern hit. Empty when the code is portable.
    """
    declared_systems, privileged = _declared_exemptions(manifest)
    bot_id = _bot_id(manifest)

    findings: list[PortabilityFinding] = []
    for rel in files:
        if not rel:
            continue
        rel = rel.lstrip("/")
        full = workspace / rel
        try:
            text = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for fnd in _scan_text(
            text, rel,
            declared_systems=declared_systems,
            privileged=privileged,
            bot_id=bot_id,
        ):
            findings.append(fnd)

    return findings


def _scan_text(
    text: str,
    rel_path: str,
    *,
    declared_systems: set[str],
    privileged: bool,
    bot_id: str | None,
) -> Iterable[PortabilityFinding]:
    lines = text.splitlines()
    is_shell = rel_path.endswith((".sh", ".bash"))

    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # H4: UTC fallback first so it gets a specific suggestion rather
        # than the generic H2 systemsetup hit.
        if _UTC_FALLBACK_RE.search(line):
            # Only flag when paired with a systemsetup call somewhere in
            # the file — a generic UTC default isn't suspicious on its own.
            if "systemsetup" in text.lower():
                yield PortabilityFinding(
                    file=rel_path, line=lineno, family="H4",
                    pattern="systemsetup-utc-fallback",
                    snippet=_trim_snippet(line),
                    severity="should-fix",
                    suggestion=(
                        "Replace `systemsetup -gettimezone` with "
                        "`datetime.now().astimezone().tzinfo` "
                        "(no privilege required). The systemsetup path needs "
                        "admin on macOS and silently falls back to UTC, "
                        "giving wrong-timezone timestamps in production."
                    ),
                )

        # H2: sudo-required commands. Skip when the line is already
        # prefixed with sudo.
        for subfamily, regex in _SUDO_REQUIRED_PATTERNS:
            if not regex.search(line):
                continue
            if _SUDO_PREFIX_RE.search(line):
                continue
            if privileged:
                # Operator declared the app needs privileges — sudo
                # patterns are expected.
                continue
            yield PortabilityFinding(
                file=rel_path, line=lineno, family="H2",
                pattern=subfamily,
                snippet=_trim_snippet(line),
                severity="should-fix",
                suggestion=(
                    f"`{subfamily.split('-', 1)[0]}` requires admin on macOS. "
                    f"Either prefix with `sudo` AND declare "
                    f"`requirements.privileged: true` in the manifest, "
                    f"or rewrite using a non-privileged API."
                ),
            )

        # H3: hardcoded venv python paths
        m = _HARDCODED_PY_PATH_RE.search(line)
        if m:
            yield PortabilityFinding(
                file=rel_path, line=lineno, family="H3",
                pattern="hardcoded-venv-python",
                snippet=_trim_snippet(line),
                severity="should-fix",
                suggestion=(
                    f"Replace hardcoded `{m.group('path')}` with "
                    f"`#!/usr/bin/env python3` (shebang) or "
                    f"`shutil.which('python3')` (runtime resolve). "
                    f"Venv paths are install-specific; this won't survive "
                    f"a fresh deploy."
                ),
            )

        # H1: absolute paths outside the bot's workspace, not declared
        # in requirements.system[]
        for path_m in _ABSOLUTE_PATH_RE.finditer(line):
            path = path_m.group("path")
            if _is_path_in_workspace(path, bot_id):
                continue
            if _is_declared_dependency(path, declared_systems):
                continue
            # Skip very common system paths the LLM uses for shebangs.
            if path.rstrip(":").endswith(("/bin/bash", "/bin/sh", "/usr/bin/env",
                                          "/bin/cat", "/bin/cp", "/bin/mkdir",
                                          "/bin/chmod", "/usr/sbin/chown")):
                continue
            # H3 owns venv-python paths; don't double-fire H1.
            if any(token in path for token in _VENV_PREFIX_TOKENS):
                continue
            yield PortabilityFinding(
                file=rel_path, line=lineno, family="H1",
                pattern="hardcoded-absolute-path",
                snippet=_trim_snippet(line),
                severity="should-fix",
                suggestion=(
                    f"Path `{path}` is install-specific. Either declare "
                    f"it in `manifest.requirements.system[]` with a "
                    f"rationale, or rewrite to discover the path at "
                    f"runtime (e.g. `shutil.which`, env var, config)."
                ),
            )


def _declared_exemptions(manifest: dict | None) -> tuple[set[str], bool]:
    """Pull (requirements.system[], requirements.privileged) from the manifest.

    Both fields are optional. Empty results mean "no exemptions declared,"
    so the lint applies in its default form.
    """
    if not isinstance(manifest, dict):
        return set(), False
    reqs = manifest.get("requirements") or {}
    if not isinstance(reqs, dict):
        return set(), False
    systems_raw = reqs.get("system") or []
    systems: set[str] = set()
    if isinstance(systems_raw, list):
        for entry in systems_raw:
            if isinstance(entry, str):
                systems.add(entry.strip().rstrip("/"))
            elif isinstance(entry, dict):
                p = entry.get("path") or entry.get("name") or ""
                if isinstance(p, str) and p.strip():
                    systems.add(p.strip().rstrip("/"))
    privileged = bool(reqs.get("privileged", False))
    return systems, privileged


def _bot_id(manifest: dict | None) -> str | None:
    if not isinstance(manifest, dict):
        return None
    bid = manifest.get("bot_id")
    return bid if isinstance(bid, str) and bid else None


def _is_path_in_workspace(path: str, bot_id: str | None) -> bool:
    """Workspace paths are NOT a portability concern — they're the app's
    own files, which is by design.

    A path is workspace-resident when it matches one of:
      /Users/{bot}/.openclaw/workspace/...
      /Users/{bot}/.openclaw/...
    """
    if not bot_id:
        return False
    return path.startswith(f"/Users/{bot_id}/.openclaw")


def _is_declared_dependency(path: str, declared_systems: set[str]) -> bool:
    """A path is exempted when the manifest declares it (or a prefix) in
    ``requirements.system[]``."""
    if not declared_systems:
        return False
    path_norm = path.rstrip("/")
    for declared in declared_systems:
        if path_norm == declared:
            return True
        if path_norm.startswith(declared + "/"):
            return True
    return False


def _trim_snippet(line: str, *, width: int = 140) -> str:
    s = line.strip()
    if len(s) > width:
        return s[:width - 1] + "…"
    return s
