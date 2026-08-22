"""The structured ``verification`` block: schema validation + executor.

Gallery packages carry machine-runnable smoke verification (wave 3a) as a
top-level ``verification: [...]`` list. Each entry has a ``kind`` axis so the
schema can grow (a future run-as-bot kind slots in without breaking v1):

    {"kind": "command", "name": "cli_help",
     "argv": ["python3", "scripts/foo.py", "--help"],
     "expected_exit": 0, "stdout_regex": "(?i)usage", "timeout_s": 30,
     "modes": ["install", "dry-run"]}

    {"kind": "artifact", "name": "config_seeded",
     "path": "config.json", "checks": ["exists", "json_valid"],
     "max_age_days": 2, "modes": ["dry-run"]}

**The v1 hard constraint (CLAUDE.md):** the harness runs as the ``evolve``
user, which cannot ``sudo -u <bot>``. Every v1 entry must therefore be
evolve-runnable: read-only/idempotent commands executed with cwd = the bot
workspace (``evolve`` holds a read ACL there), plus declarative artifact
checks. **No entry may write to bot-owned files** — an entry that tries will
hit EACCES and FAIL loudly, which is the correct conservative outcome.

Command entries follow the forge exec posture (#2602, mirrored from
``install_helpers``): an argv *vector* (never a shell string), a small
interpreter allowlist, and the script argument must be a workspace-relative
path — never an option flag, so ``python3 -c "<payload>"`` cannot ride
through. Validation runs BOTH in the repo conformance gate (builtin
packages) and again at execution time, because imported packages never pass
the repo gate.

``py_compiles`` is deliberately an *artifact* check, not a command:
``python3 -m py_compile`` both needs a ``-m`` flag (refused shape) and
writes ``__pycache__`` into the bot workspace (refused behavior). The
in-process ``compile(text, path, "exec")`` is a pure read.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .model import (
    WARN,
    Check,
    fail_check,
    ok_check,
    skip_check,
)

# ── Schema constants ──────────────────────────────────────────────────────────

KIND_COMMAND = "command"
KIND_ARTIFACT = "artifact"
KNOWN_KINDS = frozenset({KIND_COMMAND, KIND_ARTIFACT})

MODE_INSTALL = "install"
MODE_DRY_RUN = "dry-run"
VALID_MODES = frozenset({MODE_INSTALL, MODE_DRY_RUN})
_DEFAULT_MODES = [MODE_INSTALL, MODE_DRY_RUN]

# Bare interpreter names only (resolved via PATH at run time) — a path-shaped
# head could point at a bot-planted executable. Suffix pinning keeps the
# script argument honest (no ``python3 tasks.json``).
ALLOWED_INTERPRETERS: dict[str, tuple[str, ...]] = {
    "python3": (".py",),
    "bash": (".sh",),
    "sh": (".sh",),
}

ARTIFACT_CHECKS = frozenset({"exists", "json_valid", "py_compiles", "owner_exec"})

MAX_ENTRIES = 32
MAX_ARGV_LEN = 16
MAX_TOKEN_LEN = 256
MAX_REGEX_LEN = 512
DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 120.0
# stdout is scanned for stdout_regex against at most this many chars (the
# regex runs outside the subprocess timeout; a bound caps the scan window).
_STDOUT_SCAN_LIMIT = 1_000_000
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")

# Substrings that request shell evaluation — inert under shell=False, but
# their presence means the author expected shell semantics (see #2602).
_SHELL_EVAL_MARKERS = ("$(", "`", "${", "$")


def _token_errors(tok: Any, where: str) -> list[str]:
    if not isinstance(tok, str) or not tok:
        return [f"{where}: token must be a non-empty string, got {tok!r}"]
    errs = []
    if len(tok) > MAX_TOKEN_LEN:
        errs.append(f"{where}: token longer than {MAX_TOKEN_LEN} chars")
    if "\n" in tok or "\r" in tok:
        errs.append(f"{where}: newline in token")
    if any(ord(c) < 0x20 for c in tok):  # NUL + other control chars
        errs.append(f"{where}: control character in token")
    if "\\" in tok:
        errs.append(f"{where}: backslash in token")
    for marker in _SHELL_EVAL_MARKERS:
        if marker in tok:
            errs.append(
                f"{where}: shell-evaluation marker {marker!r} in {tok!r} — "
                "entries run as an argv vector with no shell and no "
                "substitution (cwd is the bot workspace; use relative paths)"
            )
            break
    if tok.startswith("/"):
        errs.append(
            f"{where}: absolute path {tok!r} — all paths are workspace-relative"
        )
    if "/" in tok and ".." in tok.split("/"):
        errs.append(f"{where}: '..' path segment in {tok!r}")
    return errs


def _relpath_errors(path: Any, where: str, *, allow_glob: bool) -> list[str]:
    errs = _token_errors(path, where)
    if errs:
        return errs
    if not allow_glob and any(c in path for c in "*?["):
        errs.append(f"{where}: glob characters not allowed in {path!r}")
    return errs


def _modes_errors(entry: dict, where: str) -> list[str]:
    modes = entry.get("modes", _DEFAULT_MODES)
    if (
        not isinstance(modes, list)
        or not modes
        or not set(modes) <= VALID_MODES
        or len(set(modes)) != len(modes)
    ):
        return [
            f"{where}: modes must be a non-empty subset of "
            f"{sorted(VALID_MODES)} without duplicates, got {modes!r}"
        ]
    return []


def _validate_command(entry: dict, where: str) -> list[str]:
    errs: list[str] = []
    argv = entry.get("argv")
    if not isinstance(argv, list) or not argv:
        return [f"{where}: command entry needs a non-empty 'argv' list"]
    if len(argv) > MAX_ARGV_LEN:
        errs.append(f"{where}: argv longer than {MAX_ARGV_LEN} tokens")
    for i, tok in enumerate(argv):
        errs.extend(_token_errors(tok, f"{where}.argv[{i}]"))
    if errs:
        return errs

    head = argv[0]
    suffixes = ALLOWED_INTERPRETERS.get(head)
    if suffixes is None:
        errs.append(
            f"{where}: argv[0] {head!r} is not an allowed interpreter "
            f"({', '.join(sorted(ALLOWED_INTERPRETERS))}) — bare names only"
        )
    else:
        if len(argv) < 2:
            errs.append(f"{where}: {head} requires a script-path argument")
        elif argv[1].startswith("-"):
            errs.append(
                f"{where}: argv[1] {argv[1]!r} is an option flag — inline-code "
                "flags (-c/-m/-e/stdin) are refused (#2602); the first "
                "argument must be a workspace-relative script"
            )
        elif not argv[1].endswith(suffixes):
            errs.append(
                f"{where}: argv[1] {argv[1]!r} must end with "
                f"{' or '.join(suffixes)} for {head}"
            )

    exp = entry.get("expected_exit", 0)
    if not isinstance(exp, int) or isinstance(exp, bool) or not 0 <= exp <= 255:
        errs.append(f"{where}: expected_exit must be an int in 0..255, got {exp!r}")

    rx = entry.get("stdout_regex")
    if rx is not None:
        if not isinstance(rx, str) or not rx or len(rx) > MAX_REGEX_LEN:
            errs.append(f"{where}: stdout_regex must be a non-empty string ≤ {MAX_REGEX_LEN} chars")
        else:
            try:
                re.compile(rx)
            except re.error as exc:
                errs.append(f"{where}: stdout_regex does not compile: {exc}")

    t = entry.get("timeout_s", DEFAULT_TIMEOUT_S)
    if not isinstance(t, (int, float)) or isinstance(t, bool) or not 1 <= t <= MAX_TIMEOUT_S:
        errs.append(f"{where}: timeout_s must be a number in 1..{MAX_TIMEOUT_S:.0f}, got {t!r}")
    return errs


def _validate_artifact(entry: dict, where: str) -> list[str]:
    errs = _relpath_errors(entry.get("path"), f"{where}.path", allow_glob=True)

    checks = entry.get("checks")
    if (
        not isinstance(checks, list)
        or not checks
        or not set(checks) <= ARTIFACT_CHECKS
        or len(set(checks)) != len(checks)
    ):
        errs.append(
            f"{where}: checks must be a non-empty subset of "
            f"{sorted(ARTIFACT_CHECKS)} without duplicates, got {checks!r}"
        )

    age = entry.get("max_age_days")
    if age is not None and (
        not isinstance(age, (int, float)) or isinstance(age, bool) or not 0 < age <= 365
    ):
        errs.append(f"{where}: max_age_days must be a number in (0, 365], got {age!r}")
    return errs


def validate_entry(entry: Any, where: str = "entry") -> list[str]:
    """Validate one verification entry. Returns a list of problems (empty ⇒ valid)."""
    if not isinstance(entry, dict):
        return [f"{where}: entry must be an object, got {type(entry).__name__}"]
    errs: list[str] = []
    name = entry.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name or ""):
        errs.append(
            f"{where}: name must match {_NAME_RE.pattern}, got {name!r}"
        )
    kind = entry.get("kind")
    if kind == KIND_COMMAND:
        errs.extend(_validate_command(entry, where))
    elif kind == KIND_ARTIFACT:
        errs.extend(_validate_artifact(entry, where))
    else:
        errs.append(
            f"{where}: unknown kind {kind!r} (known: {sorted(KNOWN_KINDS)})"
        )
    errs.extend(_modes_errors(entry, where))
    return errs


def validate_verification_block(block: Any) -> list[str]:
    """Validate a whole ``verification`` block (list of entries).

    Used by the repo conformance gate over builtin packages, and re-run at
    execution time per entry (imported packages never pass the repo gate).
    """
    if not isinstance(block, list):
        return [f"verification must be a list, got {type(block).__name__}"]
    if len(block) > MAX_ENTRIES:
        return [f"verification has {len(block)} entries (max {MAX_ENTRIES})"]
    errs: list[str] = []
    seen: set[str] = set()
    for i, entry in enumerate(block):
        name = entry.get("name") if isinstance(entry, dict) else None
        where = f"verification[{i}]" + (f" ({name})" if name else "")
        errs.extend(validate_entry(entry, where))
        if isinstance(name, str):
            if name in seen:
                errs.append(f"{where}: duplicate entry name {name!r}")
            seen.add(name)
    return errs


# ── Executor ──────────────────────────────────────────────────────────────────

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _tail(text: str, n: int = 200) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else "…" + text[-n:]


def _run_command(
    entry: dict, workspace: str, runner: Runner, clock: Callable[[], float],
) -> Check:
    name = entry["name"]
    argv = list(entry["argv"])
    timeout = min(float(entry.get("timeout_s", DEFAULT_TIMEOUT_S)), MAX_TIMEOUT_S)
    expected = int(entry.get("expected_exit", 0))
    start = clock()
    try:
        proc = runner(
            argv, cwd=workspace, capture_output=True, text=True,
            timeout=timeout, shell=False,
            # New session ⇒ the child leads its own process group, so a
            # timeout kill doesn't leave a same-group grandchild attached
            # (partial mitigation; a detached grandchild holding the stdout
            # pipe can still delay EOF — bounded by the sequential sweep).
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return fail_check(name, f"timed out after {timeout:.0f}s: {' '.join(argv)}")
    except FileNotFoundError as exc:
        return fail_check(name, f"interpreter/script not found: {exc}")
    except Exception as exc:
        return fail_check(name, f"exec failed: {exc}")
    dur = clock() - start

    if proc.returncode != expected:
        return fail_check(
            name,
            f"exit {proc.returncode} (expected {expected}) — "
            f"{' '.join(argv)}; stderr: {_tail(proc.stderr) or '<empty>'}",
        )
    rx = entry.get("stdout_regex")
    # Bound the regex INPUT: stdout is unbounded and the pattern (trusted for
    # builtin, self-authored for imported) runs outside the subprocess
    # timeout — a huge stdout would widen any backtracking window (finding 5).
    stdout = proc.stdout or ""
    if rx and not re.search(rx, stdout[:_STDOUT_SCAN_LIMIT]):
        return fail_check(
            name,
            f"stdout did not match /{rx}/ — got: {_tail(stdout) or '<empty>'}",
        )
    evidence = f"exit {proc.returncode} in {dur:.1f}s"
    if rx:
        evidence += f", stdout matched /{rx}/"
    return ok_check(name, evidence)


def _artifact_matches(base: Path, rel: str) -> list[Path]:
    if any(c in rel for c in "*?["):
        try:
            return [p for p in base.glob(rel) if p.is_file()]
        except Exception:
            return []
    p = base / rel
    return [p] if p.is_file() else []


def _run_artifact(
    entry: dict, workspace: str, now: Callable[[], float],
) -> Check:
    name = entry["name"]
    rel = entry["path"]
    checks = list(entry.get("checks") or [])
    matches = _artifact_matches(Path(workspace), rel)

    if not matches:
        if "exists" in checks or entry.get("max_age_days") is not None:
            return fail_check(name, f"no file matched {rel!r} in the workspace")
        return skip_check(name, f"no file matched {rel!r} — nothing to check")

    try:
        newest = max(matches, key=lambda p: p.stat().st_mtime)
    except OSError as exc:
        return fail_check(name, f"stat failed on {rel!r}: {exc}", severity=WARN)
    evidence = [f"{len(matches)} file(s) matched {rel!r}"]

    content: str | None = None
    if {"json_valid", "py_compiles"} & set(checks):
        try:
            content = newest.read_text(encoding="utf-8")
        except PermissionError:
            # evolve should hold a read ACL here — unreadable means the probe
            # is not authoritative (ACL drift), not that the file is bad.
            return fail_check(
                name, f"{newest.name} unreadable as evolve (read-ACL drift?)",
                severity=WARN,
            )
        except UnicodeDecodeError:
            # A file the app declared as JSON/Python but wrote as binary is a
            # genuine defect — a BLOCK, and (critically) it must NOT escape as
            # an unhandled ValueError that would collapse the whole block.
            return fail_check(
                name, f"{newest.name} is not valid UTF-8 (expected text for "
                f"{sorted({'json_valid', 'py_compiles'} & set(checks))})",
            )
        except OSError as exc:
            return fail_check(name, f"read failed on {newest.name}: {exc}", severity=WARN)

    if "json_valid" in checks:
        try:
            json.loads(content or "")
            evidence.append(f"{newest.name} parses as JSON")
        except json.JSONDecodeError as exc:
            return fail_check(name, f"{newest.name} is not valid JSON: {exc}")

    if "py_compiles" in checks:
        try:
            compile(content or "", str(newest), "exec")
            evidence.append(f"{newest.name} compiles")
        # SyntaxError is a ValueError subclass; a NUL byte raises a bare
        # ValueError ("source code string cannot contain null bytes") — catch
        # both so a planted NUL can't crash out of the block (finding 1).
        except (SyntaxError, ValueError) as exc:
            return fail_check(name, f"{newest.name} does not compile: {exc}")

    if "owner_exec" in checks:
        # st_mode, not os.access — the question is "can the BOT execute it"
        # (owner x-bit), not whether evolve can.
        try:
            mode = newest.stat().st_mode
        except OSError as exc:
            return fail_check(name, f"stat failed on {newest.name}: {exc}", severity=WARN)
        if mode & 0o100:
            evidence.append(f"{newest.name} owner-executable")
        else:
            return fail_check(
                name, f"{newest.name} is not owner-executable (mode {mode & 0o777:o})",
            )

    age_max = entry.get("max_age_days")
    if age_max is not None:
        try:
            age_days = (now() - newest.stat().st_mtime) / 86400.0
        except OSError as exc:
            return fail_check(name, f"stat failed on {newest.name}: {exc}", severity=WARN)
        if age_days > float(age_max):
            return fail_check(
                name,
                f"{newest.name} is stale: {age_days:.1f}d old (max {age_max}d) — "
                "the producing schedule is likely not firing",
            )
        evidence.append(f"age {age_days:.1f}d ≤ {age_max}d")

    return ok_check(name, "; ".join(evidence))


def _package_is_command_trusted(pkg: dict) -> bool:
    """Command entries execute an app-authored script *as the evolve user*
    (which holds a cross-bot read ACL + shared-store write access). For an
    IMPORTED package the script body is importer-controlled, so a
    legit-shaped ``python3 scripts/x.py`` — which the #2602 posture accepts —
    would be a bot→evolve escalation. Gate command execution to packages that
    ship in the repo (``source == "builtin"``); imported packages get their
    command entries skipped, and only artifact checks (which *parse*, never
    *execute*) run against their untrusted content. ``source`` is set by
    ``list_gallery_packages``; absent (unit tests / direct calls) is treated
    as builtin so the executor's own mechanics stay testable.
    """
    return pkg.get("source", "builtin") == "builtin"


def run_verification_block(
    pkg: dict,
    *,
    workspace: str,
    mode: str,
    runner: Runner | None = None,
    now: Callable[[], float] | None = None,
    clock: Callable[[], float] | None = None,
    trust_commands: bool | None = None,
) -> list[Check]:
    """Execute a package's ``verification`` block against *workspace*.

    Returns one ``Check`` per entry (plus a single skip/fail row for the
    degenerate block shapes). **Never raises** — a per-entry executor that
    throws is converted to a BLOCK fail_check for that entry, so one bad
    entry can never collapse (or silently drop) its siblings.
    """
    runner = runner or subprocess.run
    now = now or time.time
    clock = clock or time.monotonic
    if trust_commands is None:
        trust_commands = _package_is_command_trusted(pkg)

    block = pkg.get("verification")
    if block is None:
        return [skip_check("verification", "package has no verification block")]
    if not isinstance(block, list):
        return [fail_check("verification", f"verification block is not a list ({type(block).__name__})")]
    if not block:
        return [skip_check(
            "verification",
            "verification block is empty — nothing app-local to verify (meta package)",
        )]
    if len(block) > MAX_ENTRIES:
        return [fail_check("verification", f"{len(block)} entries exceeds the {MAX_ENTRIES} cap")]
    if not workspace:
        return [fail_check(
            "verification", "bot workspace not resolvable — block not run",
            severity=WARN,
        )]

    out: list[Check] = []
    for i, entry in enumerate(block):
        kind = entry.get("kind") if isinstance(entry, dict) else None
        name = (entry.get("name") if isinstance(entry, dict) else None) or f"entry_{i}"
        try:
            if isinstance(entry, dict) and kind not in KNOWN_KINDS:
                # Forward compat: a NEWER schema's kind (e.g. run-as-bot) on an
                # older harness is absent coverage, not a failure.
                out.append(skip_check(name, f"unknown verification kind {kind!r} — not run by this harness"))
                continue
            # Re-validate at execution time — imported packages never pass the
            # repo conformance gate, so this is the real boundary, not a rerun.
            errs = validate_entry(entry, f"verification[{i}]")
            if errs:
                out.append(fail_check(name, f"invalid entry: {errs[0]}"))
                continue
            modes = entry.get("modes", _DEFAULT_MODES)
            if mode not in modes:
                out.append(skip_check(name, f"not applicable in {mode} mode (modes={modes})"))
                continue
            if kind == KIND_COMMAND:
                if not trust_commands:
                    out.append(skip_check(
                        name,
                        "command entries run only for builtin packages — this "
                        "package is imported (untrusted); artifact checks still run",
                    ))
                    continue
                out.append(_run_command(entry, workspace, runner, clock))
            else:
                out.append(_run_artifact(entry, workspace, now))
        except Exception as exc:  # never-raises guarantee (finding 1)
            out.append(fail_check(name, f"entry raised: {exc!r}"))
    return out


def make_verify_fn(
    probe: Any,
    mode: str,
    *,
    runner: Runner | None = None,
    now: Callable[[], float] | None = None,
) -> Callable[[dict, dict | None, str], list[Check]]:
    """Build the pipeline's ``verify_fn`` seam: resolves the bot workspace via
    the probe and executes the package's verification block."""

    def verify_fn(pkg: dict, manifest: dict | None, bot_id: str) -> list[Check]:
        try:
            ws = probe.bot_workspace(bot_id)
        except Exception:
            ws = None
        return run_verification_block(
            pkg, workspace=str(ws) if ws else "", mode=mode,
            runner=runner, now=now,
        )

    return verify_fn
