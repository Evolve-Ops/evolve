"""audit_pod_acls — pod-wide ACL + permission auditor.

Diagnostic that walks the documented invariants for `/Users/Shared/evolve/`
and every bot's `.openclaw/` workspace and reports drift between actual
macOS file state (mode bits, sticky bit, owner, ACL entries) and the
invariants codified below.

This is intentionally an **independent** source of truth from
:func:`evolve_admin.deploy.ensure_pod_perms` (the existing applier).
The two agree on most invariants; where they disagree, the auditor is
the spec and the applier is what needs to catch up. The motivating
incident was the sticky bit on ``proposals/pending/`` silently blocking
``evo`` from os.replace-ing proposal files that ``evolve`` had written —
the ACL allowed write but the sticky bit kicked in on the implicit
unlink. The applier set mode 1777; the auditor's rule table says 0o0775
(no sticky), backed by the explicit non-sticky reasoning in
``deploy._PROPOSAL_LIFECYCLE_SUBDIRS`` and ``deploy.deploy_shared_dir``.

Invocation::

    evolve-admin audit-acls                  # report only (default)
    evolve-admin audit-acls --json           # JSON for Signal ingestion
    evolve-admin audit-acls --apply          # fix the drifted entries

Exit codes::

    0 — no drift
    1 — drift detected (and not auto-fixed, or --apply left some unfixed)
    2 — some paths could not be inspected (permission errors, etc.)
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


# ── Canonical invariants ─────────────────────────────────────────────────────
#
# Source: CLAUDE.md + internal/spec-evo-account-separation-2026-05-25.md, with
# the sticky-bit correction from the 2026-06-08 still_motivated incident.

EVOLVE_SERVICE_USER = "evolve"
EVO_GATEWAY_USER = "evo"

# Shared root. Lives under {shared_dir} in network.json; the default is
# /Users/Shared/evolve/. Owned by evolve:wheel. 0o0755 is enough — evolve
# is the only writer at the root.
SHARED_ROOT_MODE = 0o0755

# Proposal + signal lifecycle dirs. The 2026-06-08 incident proved sticky
# is wrong here: with mode 1775+sticky, evo cannot ``os.replace`` a file
# owned by evolve even with full ACL because sticky blocks the implicit
# unlink. Use mode 0o0775 (group/world traverse but no sticky) plus an
# inherited evo write ACL — both writers can move files regardless of
# who wrote any individual file. ``deploy.PROPOSAL_LIFECYCLE_SUBDIRS``
# already documents this rationale; the applier just hadn't been wired
# up against the matching mode.
LIFECYCLE_DIR_MODE = 0o0775

# Per-bot workspace evolve/ — bot owns, evolve has full ACL via
# set_evolve_read_acl(). 0o0775 lets the bot user umask new files
# group-readable so evolve picks them up without needing a recursive
# ACL backfill on every write.
WORKSPACE_EVOLVE_MODE = 0o0775

# `.openclaw/` root — bot owns, evolve has read+inherit ACL.
OPENCLAW_DIR_MODE = 0o0755

# The evo write ACL (post-account-separation). Mirrors the
# evolve-side write ACL on workspace/evolve/ in shape, inverted in
# user direction. Source: deploy.EVO_WRITE_ACL_PERMS.
#
# ``execute`` — macOS stores it as ``search`` on a directory — is the TRAVERSE
# bit. It is what allows an evo-granted dir to drop below world-x (0755 → 0750);
# ``keystore/`` and ``keystore/vault/`` do exactly that, so an ACE missing it
# strands the gateway with the silent "no token in keystore slot". Rationale and
# the live probe: secret_config_perms.py's KEYSTORE_DIR_MODES section.
EVO_WRITE_ACL_PERMS: tuple[str, ...] = (
    "read", "write", "execute", "delete", "append",
    "readattr", "writeattr", "readextattr", "writeextattr", "readsecurity",
    "file_inherit", "directory_inherit",
)

# Evolve's read ACL on bot .openclaw/ dirs. Source: deploy.set_evolve_read_acl.
EVOLVE_READ_ACL_PERMS: tuple[str, ...] = (
    "list", "search",
    "readattr", "readextattr", "readsecurity",
    "file_inherit", "directory_inherit",
)

# Evolve's write ACL on bot workspace/evolve/. Source: deploy.set_evolve_read_acl.
EVOLVE_WRITE_ACL_PERMS: tuple[str, ...] = (
    "list", "search",
    "add_file", "add_subdirectory",
    "readattr", "writeattr", "readextattr", "writeextattr", "readsecurity",
    "delete", "write",
    "file_inherit", "directory_inherit",
)

# Lifecycle subdirs covered by the evo write contract.
PROPOSAL_SUBDIRS: tuple[str, ...] = ("pending", "snoozed", "applied", "archived")
SIGNAL_SUBDIRS: tuple[str, ...] = ("firing", "snoozed", "archived")


# ── Result types ─────────────────────────────────────────────────────────────


@dataclass
class Finding:
    """One audit result. ``ok=True`` means actual state matches the invariant.

    ``apply`` is a zero-arg callable that, when invoked, attempts to repair
    the drift. None when the rule is informational-only or no safe automated
    fix exists.
    """

    category: str            # "mode", "owner", "acl", "sticky", "exists"
    path: str                # absolute path being checked
    rule: str                # short rule id, e.g. "proposals/pending mode"
    ok: bool
    actual: str = ""         # observed value, human-readable
    expected: str = ""       # invariant value, human-readable
    fix: str = ""            # one-line repair command (for the report)
    apply: Callable[[], bool] | None = None
    severity: str = "drift"  # "drift" | "warn" | "info"


@dataclass
class AuditReport:
    findings: list[Finding] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)  # paths we couldn't stat
    applied: list[str] = field(default_factory=list)
    failed_fixes: list[str] = field(default_factory=list)

    @property
    def drift(self) -> list[Finding]:
        return [f for f in self.findings if not f.ok]

    @property
    def ok_count(self) -> int:
        return sum(1 for f in self.findings if f.ok)

    def add(self, f: Finding) -> None:
        self.findings.append(f)

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": all(f.ok for f in self.findings) and not self.unreadable,
            "summary": {
                "checked": len(self.findings),
                "passed": self.ok_count,
                "drifted": len(self.drift),
                "unreadable": len(self.unreadable),
                "applied": len(self.applied),
                "failed_fixes": len(self.failed_fixes),
            },
            "findings": [
                {
                    "category": f.category,
                    "path": f.path,
                    "rule": f.rule,
                    "ok": f.ok,
                    "actual": f.actual,
                    "expected": f.expected,
                    "fix": f.fix,
                    "severity": f.severity,
                }
                for f in self.findings
            ],
            "unreadable": list(self.unreadable),
            "applied": list(self.applied),
            "failed_fixes": list(self.failed_fixes),
        }


# ── Low-level helpers ────────────────────────────────────────────────────────


def get_acl_entries(path: Path) -> list[str]:
    """Return the ACL entries on `path`, one per line, with the index prefix stripped.

    Uses ``/bin/ls -lde`` (the ``d`` so we read the directory's own ACL,
    not its children's). On macOS, ACL entries print below the stat line,
    one per indented line, formatted as::

        0: user:evolve allow list,search,readattr,readextattr,readsecurity

    The leading ``N:`` index is dropped. Returns an empty list on read
    error so callers can detect "no ACL" identically to "couldn't read".
    """
    try:
        proc = subprocess.run(
            ["/bin/ls", "-lde", str(path)],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for line in proc.stdout.splitlines()[1:]:
        s = line.strip()
        if not s:
            continue
        head, _, rest = s.partition(":")
        if rest and head.strip().isdigit():
            out.append(rest.strip())
        else:
            out.append(s)
    return out


# When chmod +a "user:x allow read,write,append" is applied to a
# directory, macOS rewrites the source-form perm names into their
# directory-resolved equivalents (read→list, write→add_file +
# add_subdirectory, append→add_subdirectory, execute→search).
# `ls -lde` prints the resolved names. Without translating, a literal-
# set check against the source names would always look like drift.
# Mirrors runtime.perms._MACOS_DIR_ACL_PERM_MAP (the Perms seam owns
# the deploy-side copy since W4a).
_DIR_ACL_PERM_MAP: dict[str, tuple[str, ...]] = {
    "read":    ("list",),
    "write":   ("add_file", "add_subdirectory"),
    "execute": ("search",),
    "append":  ("add_subdirectory",),
}


def _resolve_dir_perms(perms: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for p in perms:
        out.update(_DIR_ACL_PERM_MAP.get(p, (p,)))
    return out


def acl_user_has_perms(
    entries: Sequence[str],
    user: str,
    required: Iterable[str],
    *,
    is_dir: bool,
) -> bool:
    """True if any ``user:<user> allow`` entry covers every required perm.

    Inherited entries (marked ``inherited`` by ``ls -lde``) count the
    same as explicit ones — the access they grant is identical.

    Resolution: if ``is_dir`` is True, source-form perm names (``read``,
    ``write``, ``append``, ``execute``) are translated to their dir-
    resolved equivalents before matching, so an ACE applied via
    ``chmod +a "... allow read,write,..."`` reads back as semantically
    matching even though the kernel rewrote the names on storage.
    """
    needed = _resolve_dir_perms(required) if is_dir else set(required)
    user_prefix = f"user:{user} "
    for entry in entries:
        # Entry forms (ls -lde):
        #   user:evolve allow list,search,readattr...
        #   user:evolve inherited allow list,search,readattr...
        #   group:wheel allow list,search...
        # Only match the user-form lines. The "inherited" marker counts
        # the same as an explicit ACE — they grant identical access.
        if not entry.startswith(user_prefix):
            continue
        if " allow " not in entry and not entry.endswith(" allow"):
            continue
        try:
            perms_part = entry.split(" allow ", 1)[1].strip()
        except IndexError:
            continue
        present = {p.strip() for p in perms_part.split(",") if p.strip()}
        if needed.issubset(present):
            return True
    return False


def _owner_name(uid: int) -> str:
    import pwd
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid:{uid}"


def _user_exists(name: str) -> bool:
    import pwd
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


# ── Rule helpers ─────────────────────────────────────────────────────────────


def _check_mode(
    path: Path,
    expected_mode: int,
    rule: str,
    *,
    apply: Callable[[], bool] | None = None,
    apply_command: str | None = None,
) -> Finding:
    """Check ``path.st_mode`` (the perm bits including sticky) equals expected."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return Finding(
            category="exists", path=str(path), rule=rule,
            ok=False, actual="missing", expected=f"mode {oct(expected_mode)}",
        )
    except PermissionError as e:
        return Finding(
            category="mode", path=str(path), rule=rule,
            ok=False, actual=f"unreadable: {e}", expected=f"mode {oct(expected_mode)}",
        )
    cur = st.st_mode & 0o7777
    ok = cur == expected_mode
    return Finding(
        category="mode",
        path=str(path),
        rule=rule,
        ok=ok,
        actual=oct(cur),
        expected=oct(expected_mode),
        fix=("" if ok else (apply_command or f"chmod {oct(expected_mode)[2:]} {path}")),
        apply=(None if ok else apply),
    )


def _check_sticky_absent(path: Path, rule: str) -> Finding:
    """Specifically catch the sticky bit — the proposals/ incident regressing."""
    try:
        st = path.stat()
    except (FileNotFoundError, PermissionError) as e:
        return Finding(
            category="sticky", path=str(path), rule=rule,
            ok=False, actual=f"unreadable: {e}", expected="sticky absent",
        )
    is_sticky = bool(st.st_mode & stat.S_ISVTX)
    if not is_sticky:
        return Finding(
            category="sticky", path=str(path), rule=rule,
            ok=True, actual="absent", expected="absent",
        )
    return Finding(
        category="sticky", path=str(path), rule=rule,
        ok=False,
        actual=f"set ({oct(st.st_mode & 0o7777)})",
        expected="absent",
        fix=f"chmod -t {path}",
        apply=lambda: _run_sudo(["/bin/chmod", "-t", str(path)]),
        severity="drift",
    )


def _check_owner(path: Path, expected_user: str, rule: str) -> Finding:
    try:
        st = path.stat()
    except (FileNotFoundError, PermissionError) as e:
        return Finding(
            category="owner", path=str(path), rule=rule,
            ok=False, actual=f"unreadable: {e}", expected=expected_user,
        )
    owner = _owner_name(st.st_uid)
    if owner == expected_user:
        return Finding(
            category="owner", path=str(path), rule=rule,
            ok=True, actual=owner, expected=expected_user,
        )
    return Finding(
        category="owner", path=str(path), rule=rule,
        ok=False, actual=owner, expected=expected_user,
        fix=f"chown {expected_user}:wheel {path}",
        apply=lambda: _run_sudo(
            ["/usr/sbin/chown", f"{expected_user}:wheel", str(path)]
        ),
    )


def _check_acl(
    path: Path,
    user: str,
    required_perms: Sequence[str],
    rule: str,
    *,
    severity: str = "drift",
) -> Finding:
    """Check that an ``allow`` ACE for ``user`` covers ``required_perms``."""
    try:
        is_dir = path.is_dir()
    except OSError:
        is_dir = False
    if not path.exists():
        return Finding(
            category="acl", path=str(path), rule=rule,
            ok=False, actual="path missing", expected=f"user:{user} ACL present",
            severity=severity,
        )
    entries = get_acl_entries(path)
    present = acl_user_has_perms(entries, user, required_perms, is_dir=is_dir)
    expected = f"user:{user} allow {','.join(required_perms)}"
    if present:
        return Finding(
            category="acl", path=str(path), rule=rule,
            ok=True, actual=f"user:{user} ACE present", expected=expected,
            severity=severity,
        )
    return Finding(
        category="acl", path=str(path), rule=rule,
        ok=False, actual=f"user:{user} ACE missing", expected=expected,
        fix=f'chmod +a "user:{user} allow {",".join(required_perms)}" {path}',
        apply=lambda: _run_sudo([
            "/bin/chmod", "+a",
            f"user:{user} allow {','.join(required_perms)}",
            str(path),
        ]),
        severity=severity,
    )


def _run_sudo(argv: list[str]) -> bool:
    """Run ``sudo <argv>`` non-interactively. Returns True on exit code 0."""
    try:
        proc = subprocess.run(
            ["sudo", "-n", *argv],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    if proc.returncode == 0:
        return True
    # chmod +a returns 1 with "exists" in stderr when the ACE is already
    # present — treat as success since the target state matches.
    if "exists" in (proc.stderr or "").lower():
        return True
    return False


# ── Rule sets ────────────────────────────────────────────────────────────────


def audit_shared_root(shared_dir: Path) -> list[Finding]:
    """Top-level ``/Users/Shared/evolve/`` invariants."""
    return [
        _check_owner(shared_dir, EVOLVE_SERVICE_USER, "shared_dir/owner"),
        # Mode is allowed to be 0o0755 or 0o0775 — both are acceptable
        # under the "evolve-only writes, world reads" model.
        _check_shared_root_mode(shared_dir),
    ]


def _check_shared_root_mode(path: Path) -> Finding:
    """Accept either 0o0755 or 0o0775 — both keep the single-writer contract."""
    try:
        st = path.stat()
    except (FileNotFoundError, PermissionError) as e:
        return Finding(
            category="mode", path=str(path), rule="shared_dir/mode",
            ok=False, actual=f"unreadable: {e}", expected="0o0755 or 0o0775",
        )
    cur = st.st_mode & 0o7777
    ok = cur in (0o0755, 0o0775)
    return Finding(
        category="mode", path=str(path), rule="shared_dir/mode",
        ok=ok, actual=oct(cur), expected="0o0755 or 0o0775",
        fix=("" if ok else f"chmod 0755 {path}"),
        apply=(None if ok else lambda: _run_sudo(["/bin/chmod", "0755", str(path)])),
    )


def audit_lifecycle_subdir(
    path: Path, subdir_name: str, evo_user_exists: bool
) -> list[Finding]:
    """Invariants for one ``proposals/<sub>`` or ``signals/<sub>`` directory.

    These are the multi-writer dirs that triggered the 2026-06-08 incident.
    Three checks: mode (0o0775), sticky bit must be absent, and the evo
    write ACL must be present and cover the needed perms.
    """
    findings: list[Finding] = []
    # Existence + mode (the mode check folds in non-existence).
    findings.append(_check_mode(
        path, LIFECYCLE_DIR_MODE, rule=f"{subdir_name}/mode",
        apply=lambda: _run_sudo(["/bin/chmod", oct(LIFECYCLE_DIR_MODE)[2:], str(path)]),
    ))
    findings.append(_check_sticky_absent(path, rule=f"{subdir_name}/sticky"))
    findings.append(_check_owner(path, EVOLVE_SERVICE_USER, rule=f"{subdir_name}/owner"))
    if evo_user_exists:
        findings.append(_check_acl(
            path, EVO_GATEWAY_USER, EVO_WRITE_ACL_PERMS,
            rule=f"{subdir_name}/evo-write-acl",
        ))
    else:
        findings.append(Finding(
            category="acl", path=str(path), rule=f"{subdir_name}/evo-write-acl",
            ok=True, actual="evo user not provisioned — skipped",
            expected=f"user:{EVO_GATEWAY_USER} write ACL",
            severity="info",
        ))
    return findings


def audit_proposals_tree(shared_dir: Path, evo_user_exists: bool) -> list[Finding]:
    findings: list[Finding] = []
    root = shared_dir / "proposals"
    findings.append(_check_owner(root, EVOLVE_SERVICE_USER, "proposals/owner"))
    for sub in PROPOSAL_SUBDIRS:
        findings.extend(audit_lifecycle_subdir(
            root / sub, f"proposals/{sub}", evo_user_exists,
        ))
    return findings


def audit_signals_tree(shared_dir: Path, evo_user_exists: bool) -> list[Finding]:
    findings: list[Finding] = []
    root = shared_dir / "signals"
    findings.append(_check_owner(root, EVOLVE_SERVICE_USER, "signals/owner"))
    for sub in SIGNAL_SUBDIRS:
        findings.extend(audit_lifecycle_subdir(
            root / sub, f"signals/{sub}", evo_user_exists,
        ))
    return findings


def audit_bot_workspace(bot_id: str, bot_user: str) -> list[Finding]:
    """Per-bot invariants for ``/Users/<bot_user>/.openclaw/`` + workspace/evolve/.

    Skipped quietly when ``/Users/<bot_user>/`` doesn't exist (bot not
    yet deployed) — surfaced as a single info finding so the auditor
    is honest about what it did *not* check.
    """
    findings: list[Finding] = []
    home = Path(f"/Users/{bot_user}")
    if not home.exists():
        findings.append(Finding(
            category="exists", path=str(home), rule=f"bot/{bot_id}/home",
            ok=True, actual="no home dir — bot not deployed yet",
            expected="(skipped)", severity="info",
        ))
        return findings

    oc = home / ".openclaw"
    if not oc.exists():
        findings.append(Finding(
            category="exists", path=str(oc), rule=f"bot/{bot_id}/openclaw-dir",
            ok=True, actual="no .openclaw/ — bot not deployed yet",
            expected="(skipped)", severity="info",
        ))
        return findings

    findings.append(_check_acl(
        oc, EVOLVE_SERVICE_USER, EVOLVE_READ_ACL_PERMS,
        rule=f"bot/{bot_id}/openclaw-evolve-read-acl",
    ))

    workspace = oc / "workspace"
    if workspace.exists():
        ws_evolve = workspace / "evolve"
        if ws_evolve.exists():
            findings.append(_check_acl(
                ws_evolve, EVOLVE_SERVICE_USER, EVOLVE_WRITE_ACL_PERMS,
                rule=f"bot/{bot_id}/workspace-evolve-acl",
            ))
        else:
            findings.append(Finding(
                category="exists", path=str(ws_evolve),
                rule=f"bot/{bot_id}/workspace-evolve-dir",
                ok=True, actual="no workspace/evolve/ — pre-evolve bot",
                expected="(skipped)", severity="info",
            ))
    return findings


# ── Entry point ──────────────────────────────────────────────────────────────


def run_audit(
    *,
    network_path: Path | None = None,
    shared_dir_override: Path | None = None,
) -> AuditReport:
    """Run the full pod-wide audit and return the report.

    Pure read-only — never mutates anything. Apply is a separate step
    via :func:`apply_fixes`, and even then only when the operator opts
    in via ``--apply`` on the CLI.

    ``network_path`` defaults to ``/Users/Shared/evolve/network.json``.
    ``shared_dir_override`` lets tests + dev runs point at a tmp tree
    without going through network.json.
    """
    from ..config import DEFAULT_NETWORK_CONFIG, DEFAULT_SHARED_DIR

    report = AuditReport()

    network: dict[str, Any]
    if shared_dir_override is not None:
        shared_dir = shared_dir_override
        # Tests may not have a network.json; default to no bots.
        network = {"members": [], "bots": {}, "sharedDir": str(shared_dir)}
    else:
        from ..config import load_network
        np = network_path or DEFAULT_NETWORK_CONFIG
        try:
            network = load_network(np)
        except (FileNotFoundError, json.JSONDecodeError):
            report.unreadable.append(str(np))
            network = {"members": [], "bots": {}}
        shared_dir = Path(network.get("sharedDir") or DEFAULT_SHARED_DIR)

    evo_user_exists = _user_exists(EVO_GATEWAY_USER)

    # Pod-wide checks.
    for f in audit_shared_root(shared_dir):
        report.add(f)
    for f in audit_proposals_tree(shared_dir, evo_user_exists):
        report.add(f)
    for f in audit_signals_tree(shared_dir, evo_user_exists):
        report.add(f)

    # Per-bot checks. ``members`` is the live list; ``bots`` is the
    # detail map. Fall back to ``bots`` keys when members is empty —
    # some older network.json files only populated the detail map.
    members = list(network.get("members") or [])
    if not members:
        members = list((network.get("bots") or {}).keys())
    for bot_id in members:
        bot_user = (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id
        for f in audit_bot_workspace(bot_id, bot_user):
            report.add(f)

    return report


def apply_fixes(report: AuditReport) -> AuditReport:
    """Run each drifted finding's repair, mutating the report in place.

    Only findings carrying a non-None ``apply`` callable are attempted.
    Findings without an applier — informational items, severity=warn,
    or rules that intentionally have no automated fix — are left alone.
    Returns the same report for chaining.
    """
    for f in report.findings:
        if f.ok or f.apply is None:
            continue
        try:
            ok = bool(f.apply())
        except Exception as e:
            report.failed_fixes.append(f"{f.rule} @ {f.path}: {e}")
            continue
        if ok:
            f.ok = True
            f.actual = f"{f.actual} → fixed"
            report.applied.append(f"{f.rule} @ {f.path}: {f.fix}")
        else:
            report.failed_fixes.append(f"{f.rule} @ {f.path}: fix returned non-success")
    return report


# ── CLI ──────────────────────────────────────────────────────────────────────


def render_text_report(report: AuditReport) -> str:
    """Render the report as a human-readable text block (no color codes)."""
    lines: list[str] = []
    # Group by category for readability.
    from collections import OrderedDict
    groups: "OrderedDict[str, list[Finding]]" = OrderedDict()
    for f in report.findings:
        groups.setdefault(f.category, []).append(f)
    for category, items in groups.items():
        lines.append(f"== {category} ==")
        for f in items:
            mark = "OK " if f.ok else ("INFO" if f.severity == "info" else "DRIFT")
            tail = f"  expected={f.expected}" if not f.ok else ""
            lines.append(f"  [{mark}] {f.path}  {f.rule}: {f.actual}{tail}")
            if not f.ok and f.fix:
                lines.append(f"        fix: {f.fix}")
        lines.append("")
    summary = report.to_json()["summary"]
    lines.append(
        f"summary: checked={summary['checked']} "
        f"passed={summary['passed']} "
        f"drifted={summary['drifted']} "
        f"unreadable={summary['unreadable']}"
    )
    if report.applied:
        lines.append(f"applied: {len(report.applied)} fix(es)")
    if report.failed_fixes:
        lines.append(f"failed_fixes: {len(report.failed_fixes)}")
        for ff in report.failed_fixes:
            lines.append(f"  - {ff}")
    return "\n".join(lines)


def main_cli(
    *,
    as_json: bool = False,
    apply: bool = False,
    network_path: Path | None = None,
) -> int:
    """Run the auditor and return an exit code.

    Kept as a plain function so the click wrapper in ``cli.py`` just
    forwards flags. Apply mode requires sudo for most fixes (chmod +a,
    chown). When not running as root, ``apply=True`` still attempts
    the fix via ``sudo -n``; non-interactive sudo will succeed only
    if the operator has the relevant grant.
    """
    report = run_audit(network_path=network_path)
    if apply:
        apply_fixes(report)

    if as_json:
        print(json.dumps(report.to_json(), indent=2, sort_keys=True))
    else:
        print(render_text_report(report))

    if report.unreadable:
        return 2
    if report.drift:
        return 1
    return 0


__all__ = [
    "AuditReport",
    "Finding",
    "EVOLVE_SERVICE_USER",
    "EVO_GATEWAY_USER",
    "EVO_WRITE_ACL_PERMS",
    "EVOLVE_READ_ACL_PERMS",
    "EVOLVE_WRITE_ACL_PERMS",
    "LIFECYCLE_DIR_MODE",
    "PROPOSAL_SUBDIRS",
    "SIGNAL_SUBDIRS",
    "acl_user_has_perms",
    "apply_fixes",
    "audit_bot_workspace",
    "audit_lifecycle_subdir",
    "audit_proposals_tree",
    "audit_shared_root",
    "audit_signals_tree",
    "get_acl_entries",
    "main_cli",
    "render_text_report",
    "run_audit",
]
