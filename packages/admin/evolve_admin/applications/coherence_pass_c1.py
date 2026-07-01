"""Coherence Pass C1 — code-shape static analysis — PR 15.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §6.3.

Pure Python, AST + import-graph. No LLM. Targets ~500ms per app.
Runs weekly inside the Tier 2 runner — every Sunday's 6h tick instead
of every tick (§8.2).

## What this pass checks (spec §6.3)

For each ``scheduled_actions[*]`` claim, walk the implementing code and
verify the shape is plausible:

1. **Integration shape.** If output declares a messaging integration,
   the implementing script must import or reference that integration's
   module/CLI. (Telegram-output action → `import` something
   telegram-shaped or `subprocess` a telegram CLI.)
2. **Input shape.** If inputs declare file reads, the implementing
   script must contain `read_text()` / `open()` / equivalent on a path
   matching the declared input.
3. **LLM shape.** If the action summary contains "summarizes" or
   "analyzes", the implementing script must invoke an LLM (recognized
   by import patterns: ``anthropic``, ``openai``, ``openclaw``, or
   subprocess to ``openclaw agent``).
4. **Cron-script parses.** ``crons[*]`` scripts must parse cleanly
   (``ast.parse`` for Python; ``bash -n`` for shell).

These are deliberately CRUDE heuristics — they catch egregious shape
violations cheaply without LLM. They miss subtle cases, which is what
Pass C2 (LLM monthly via Tier 3) is for.

## Output

Findings get written to ``manifest.coherence.findings[]`` with
``check_id`` ``C1-{1,2,3,4}`` for the four heuristics. Severity is
always ``warning`` — these are heuristic, not authoritative.

Severity overlay:
- File the action implements doesn't exist → ``major`` (it's not a
  C1 problem, it's a missing-file problem; surfaced for visibility
  but doesn't drown out the genuine drift).
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# ── Heuristic patterns ───────────────────────────────────────────────

# Integration → import patterns. Keys are normalized integration names
# (from output.kind or output.integration); values are substring
# patterns that appear in source code when something talks to that
# integration. Subprocess invocations to CLIs count too.
_INTEGRATION_PATTERNS = {
    "telegram":      ("telegram", "tgram", "tg-cli", "telethon"),
    "slack":         ("slack", "slack_sdk", "slackclient"),
    "discord":       ("discord",),
    "email":         ("smtp", "sendmail", "email.mime", "msmtp",
                       "imaplib"),
    "gmail":         ("gmail", "google.oauth", "googleapiclient"),
    "github":        ("github", "ghapi", "octokit",
                       "'gh'", '"gh"', "gh pr", "gh issue"),
    "google_drive":  ("google", "drive_service", "googleapiclient"),
    "calendar":      ("ical", "calendar", "calendly", "google.cal"),
    "notion":        ("notion",),
    "evolve":        ("evolve", "openclaw"),
    "openclaw":      ("openclaw",),
    "webhook":       ("requests.post", "httpx.post", "urllib.request",
                       "curl ", "wget "),
    "filesystem":    ("Path(", "open(", "write_text", "with open"),
}

# LLM imports/subprocess. Recognized by these substrings.
_LLM_PATTERNS = (
    "anthropic",
    "openai",
    "openclaw",
    "claude.ai",
    "llm.complete",
    "ChatCompletion",
    "Messages.create",
)

# Trigger phrases in summary that imply LLM invocation.
_LLM_TRIGGER_PHRASES = (
    "summariz", "analyz", "drafts", "writes",  # "summarizes" / "analyzes" / drafting / writing
    "generate",
)

# Input-read patterns (substring match in source).
_FILE_READ_PATTERNS = (
    "read_text",
    "open(",
    ".read(",
    "read_bytes",
    "json.load(",
    "yaml.safe_load(",
    "csv.reader(",
)


# ── Finding shape ────────────────────────────────────────────────────

# Severities (matching coherence_pass_a / spec §5).
SEVERITY_WARNING  = "warning"
SEVERITY_MAJOR    = "major"

# Check IDs (matching spec §6.3 ordering).
CHECK_C1_INTEGRATION = "C1-1"
CHECK_C1_INPUT       = "C1-2"
CHECK_C1_LLM         = "C1-3"
CHECK_C1_PARSE       = "C1-4"
CHECK_C1_MISSING     = "C1-M"


@dataclass
class CoherenceFinding:
    check_id: str
    severity: str
    message: str
    action_id: str = ""
    file_path: str = ""

    def to_dict(self) -> dict:
        return {
            "check_id":   self.check_id,
            "severity":   self.severity,
            "message":    self.message,
            "action_id":  self.action_id,
            "file_path":  self.file_path,
        }


# ── Source-reading helper ───────────────────────────────────────────

def _read_source(workspace_root: Path, rel_path: str) -> str | None:
    """Read a file from the bot workspace. Returns None when missing
    or unreadable — callers handle that gracefully."""
    if not rel_path:
        return None
    abs_path = (workspace_root / rel_path.lstrip("/")).resolve()
    try:
        # Guard against path traversal.
        abs_path.relative_to(workspace_root.resolve())
    except (ValueError, RuntimeError):
        return None
    try:
        return abs_path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return None


# ── Heuristic 1: integration shape ──────────────────────────────────

def _normalize_integration(name: str) -> str:
    return (name or "").strip().lower().replace("-", "_")


def _check_integration_shape(
    action: dict,
    source_text: str,
) -> CoherenceFinding | None:
    """Spec §6.3 #1: output integration must appear in source."""
    output = action.get("output") or {}
    if not isinstance(output, dict):
        return None
    integration = (output.get("kind") or output.get("integration") or
                   output.get("channel") or "")
    integration = _normalize_integration(integration)
    if not integration or integration not in _INTEGRATION_PATTERNS:
        return None
    patterns = _INTEGRATION_PATTERNS[integration]
    if any(p in source_text for p in patterns):
        return None
    return CoherenceFinding(
        check_id=CHECK_C1_INTEGRATION,
        severity=SEVERITY_WARNING,
        message=(
            f"action declares output via '{integration}' but the "
            f"implementing script doesn't import or invoke anything "
            f"{integration}-shaped"
        ),
        action_id=action.get("id") or action.get("name") or "",
    )


# ── Heuristic 2: input shape ────────────────────────────────────────

def _check_input_shape(
    action: dict,
    source_text: str,
) -> CoherenceFinding | None:
    """Spec §6.3 #2: file-read inputs must use read patterns."""
    inputs = action.get("inputs") or []
    if not isinstance(inputs, list):
        return None
    declares_file_input = False
    for entry in inputs:
        if not isinstance(entry, dict):
            continue
        kind = (entry.get("kind") or entry.get("type") or "").lower()
        if kind in {"file", "path", "filesystem"}:
            declares_file_input = True
            break
        if entry.get("path"):
            declares_file_input = True
            break
    if not declares_file_input:
        return None
    if any(p in source_text for p in _FILE_READ_PATTERNS):
        return None
    return CoherenceFinding(
        check_id=CHECK_C1_INPUT,
        severity=SEVERITY_WARNING,
        message=(
            "action declares file-read inputs but implementing script "
            "contains no read_text()/open()/read_bytes()/json.load() "
            "patterns"
        ),
        action_id=action.get("id") or action.get("name") or "",
    )


# ── Heuristic 3: LLM shape ──────────────────────────────────────────

def _check_llm_shape(
    action: dict,
    source_text: str,
) -> CoherenceFinding | None:
    """Spec §6.3 #3: 'summarizes'/'analyzes' action must invoke LLM."""
    summary = (action.get("summary") or action.get("description") or "")
    summary_lc = summary.lower()
    if not any(phrase in summary_lc for phrase in _LLM_TRIGGER_PHRASES):
        return None
    if any(p in source_text for p in _LLM_PATTERNS):
        return None
    # Subprocess-to-openclaw — recognize CLI invocation patterns
    # beyond the import substring above.
    if re.search(r"subprocess.*openclaw", source_text):
        return None
    return CoherenceFinding(
        check_id=CHECK_C1_LLM,
        severity=SEVERITY_WARNING,
        message=(
            "action summary implies LLM use ('summarize'/'analyze'/"
            "'generate'/etc.) but the script doesn't import or invoke "
            "anthropic/openai/openclaw or equivalent"
        ),
        action_id=action.get("id") or action.get("name") or "",
    )


# ── Heuristic 4: parse check ────────────────────────────────────────

def _check_parse(
    cron: dict,
    source_text: str,
    rel_path: str,
) -> CoherenceFinding | None:
    """Spec §6.3 #4: cron scripts must parse cleanly."""
    suffix = Path(rel_path).suffix.lower()
    cron_id = cron.get("id") or cron.get("name") or rel_path
    if suffix == ".py":
        try:
            ast.parse(source_text)
        except SyntaxError as e:
            return CoherenceFinding(
                check_id=CHECK_C1_PARSE,
                severity=SEVERITY_MAJOR,
                message=f"cron script {rel_path!r} has Python syntax "
                        f"error: {e.msg} at line {e.lineno}",
                action_id=cron_id,
                file_path=rel_path,
            )
        return None
    if suffix in {".sh", ".bash"}:
        # bash -n is fast and stdlib-free; skip when bash absent.
        bash = shutil.which("bash")
        if not bash:
            return None
        try:
            r = subprocess.run(
                [bash, "-n"],
                input=source_text,
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return CoherenceFinding(
                    check_id=CHECK_C1_PARSE,
                    severity=SEVERITY_MAJOR,
                    message=f"cron script {rel_path!r} has bash syntax "
                            f"error: {r.stderr.strip()[:200]}",
                    action_id=cron_id,
                    file_path=rel_path,
                )
        except (subprocess.TimeoutExpired, OSError):
            return None
    return None


# ── Public entry point ─────────────────────────────────────────────

def run_pass_c1(
    manifest: dict,
    workspace_root: Path,
) -> list[CoherenceFinding]:
    """Run all C1 heuristics on a manifest. Returns findings list."""
    findings: list[CoherenceFinding] = []

    # ── Action-level checks (#1, #2, #3) ────────────────────────────
    actions = manifest.get("scheduled_actions") or []
    if not isinstance(actions, list):
        actions = []

    for action in actions:
        if not isinstance(action, dict):
            continue
        impl = (action.get("implemented_by") or
                action.get("implementation") or
                action.get("script") or "")
        if not impl:
            continue   # action has no claimed script; nothing to check
        source = _read_source(workspace_root, impl)
        if source is None:
            findings.append(CoherenceFinding(
                check_id=CHECK_C1_MISSING,
                severity=SEVERITY_MAJOR,
                message=(
                    f"action's implementing file {impl!r} not found "
                    f"at expected path"
                ),
                action_id=action.get("id") or action.get("name") or "",
                file_path=impl,
            ))
            continue
        for check in (_check_integration_shape,
                      _check_input_shape,
                      _check_llm_shape):
            finding = check(action, source)
            if finding is not None:
                findings.append(finding)

    # ── Cron-script parse check (#4) ───────────────────────────────
    crons = manifest.get("crons") or manifest.get("openclaw_crons") or []
    if not isinstance(crons, list):
        crons = []
    for cron in crons:
        if not isinstance(cron, dict):
            continue
        script = cron.get("script") or cron.get("command") or ""
        if not script:
            continue
        # script may be a path or an inline shell snippet; only check
        # path-style entries (those ending in .py/.sh/.bash).
        if not any(script.lower().endswith(s)
                   for s in (".py", ".sh", ".bash")):
            continue
        source = _read_source(workspace_root, script)
        if source is None:
            # The missing-file finding above (for actions) doesn't cover
            # crons that aren't tied to scheduled_actions; emit our own.
            findings.append(CoherenceFinding(
                check_id=CHECK_C1_MISSING,
                severity=SEVERITY_MAJOR,
                message=(
                    f"cron's script {script!r} not found at "
                    f"expected path"
                ),
                action_id=cron.get("id") or cron.get("name") or "",
                file_path=script,
            ))
            continue
        finding = _check_parse(cron, source, script)
        if finding is not None:
            findings.append(finding)

    return findings


def write_findings_to_manifest(
    manifest: dict,
    findings: list[CoherenceFinding],
    *,
    checked_at: str = "",
) -> None:
    """Persist Pass C1 findings into ``manifest.coherence``.

    Spec §6.3 output. Replaces any prior C1 findings (this is a full
    re-run); preserves findings with non-C1 check IDs (Pass A's
    "A-*", Pass C2's "C2-*", etc.).
    """
    coherence = manifest.setdefault("coherence", {})
    existing = coherence.get("findings") or []
    # Drop prior C1 findings; keep everything else (Pass A, C2, C3).
    survivors = [
        f for f in existing
        if isinstance(f, dict)
        and not (f.get("check_id", "") or "").startswith("C1-")
    ]
    new_entries = [f.to_dict() for f in findings]
    coherence["findings"] = survivors + new_entries
    if checked_at:
        coherence["c1_last_run"] = checked_at
