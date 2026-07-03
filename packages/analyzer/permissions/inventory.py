"""permissions.inventory — read a bot's permission posture from its three sources.

Spec: docs/spec-permission-posture-2026-05-10.md §3.2 (PermissionInventory).

Reads three files per bot:
  - ~/.openclaw/openclaw.json — permission config (tools.exec, tools.fs,
    tools.web, commands, sandbox)
  - ~/.openclaw/exec-approvals.json — runtime approval store
  - ~/.openclaw/cron/jobs.json — scheduled invocations

The reader handles each file independently: a missing or malformed
file populates the corresponding sub-section's read_error, leaving the
others intact. This matches the spec's "observe what's there, classify
what's missing" stance.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from evolve_config import bot_home
from evolve_util import now_iso as _utc_now_iso


# ── Permission-config fields the spec models (§3.2) ──────────────────────────
# Order matters for the canonical signature.
# Sandbox posture is intentionally omitted — see baseline.py for the OC-schema
# rationale (top-level `sandbox.enabled` doesn't exist; the real path is
# `agents.defaults.sandbox.mode`). Re-add via the correct path once sandbox
# posture is modelled.
PERMISSION_CONFIG_FIELDS: tuple[str, ...] = (
    "tools.exec.security",
    "tools.exec.ask",
    "tools.exec.host",
    "tools.fs.workspaceOnly",
    "tools.web.search.enabled",
    "tools.web.fetch.enabled",
    "commands.native",
    "commands.nativeSkills",
    "commands.elevated",
    "commands.useAccessGroups",
    "commands.ownerAllowFrom",
)


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class PermissionConfig:
    openclaw_config_path: str
    fields: dict[str, Any] = field(default_factory=dict)
    field_signature: str = ""
    read_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentApprovals:
    """Per-agent approval section of exec-approvals.json."""

    agent_id: str
    count: int
    patterns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecApprovals:
    path: str
    defaults_count: int = 0
    agents: list[AgentApprovals] = field(default_factory=list)
    signature: str = ""
    read_error: str | None = None
    present: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["agents"] = [a.to_dict() if isinstance(a, AgentApprovals) else a for a in self.agents]
        return d


@dataclass
class CronJob:
    id: str
    name: str
    enabled: bool
    schedule_kind: str  # "every" | "cron" | "unknown"
    payload_kind: str  # "agentTurn" | "systemEvent" | "shell" | "unknown"
    has_turn_cap: bool
    has_budget_cap: bool
    payload_summary: str  # short text for UI ("cmd: foo" or "msg: bar")
    signature: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScheduledInvocations:
    path: str
    jobs: list[CronJob] = field(default_factory=list)
    read_error: str | None = None
    present: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["jobs"] = [j.to_dict() if isinstance(j, CronJob) else j for j in self.jobs]
        return d


@dataclass
class PermissionInventory:
    bot_id: str
    observed_at: str
    permission_config: PermissionConfig
    exec_approvals: ExecApprovals
    scheduled_invocations: ScheduledInvocations
    composite_posture: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "observed_at": self.observed_at,
            "permission_config": self.permission_config.to_dict(),
            "exec_approvals": self.exec_approvals.to_dict(),
            "scheduled_invocations": self.scheduled_invocations.to_dict(),
            "composite_posture": self.composite_posture,
        }


# ── File reader (shared with mcp.inventory pattern) ──────────────────────────

def _read_json_file(path: Path, *, timeout: float = 5.0) -> tuple[dict | None, str | None]:
    """Read a JSON file via direct read, falling back to sudo /bin/cat.

    Same pattern as mcp.inventory._read_openclaw_json. Returns
    (parsed_dict, error_str). On success error_str is None; on failure
    parsed_dict is None and error_str describes the failure mode.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None, "not_found"
    except PermissionError:
        text = None
    except OSError as exc:
        return None, f"os_error: {exc}"

    if text is None:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return None, "timeout"
        except OSError as exc:
            return None, f"sudo_error: {exc}"
        if r.returncode != 0:
            return None, f"sudo_rc={r.returncode}"
        text = r.stdout

    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"json_decode: {exc.msg}"


def _dotpath_get(obj: dict, dotpath: str) -> Any:
    """Read a value at 'a.b.c' from a nested dict; None if any segment missing."""
    cur: Any = obj
    for seg in dotpath.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
        if cur is None:
            return None
    return cur


# ── Per-source readers ───────────────────────────────────────────────────────

def _read_permission_config(oc_path: Path) -> PermissionConfig:
    pc = PermissionConfig(openclaw_config_path=str(oc_path))
    oc, err = _read_json_file(oc_path)
    if oc is None:
        pc.read_error = err
        return pc

    for fld in PERMISSION_CONFIG_FIELDS:
        pc.fields[fld] = _dotpath_get(oc, fld)

    pc.field_signature = hashlib.sha256(
        json.dumps(pc.fields, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()
    return pc


_SUBCOMMAND_RE = __import__("re").compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")
_PLACEHOLDERS: frozenset[str] = frozenset({"<path>", "<url>", "<arg>"})


def _canonicalize_pattern(raw: str) -> str:
    """Reduce a raw approved command to a canonical pattern.

    Spec §8.2: strip path-like, URL-like, and value-shaped arguments
    down to placeholders so the pattern is stable across user-context
    variation. Subcommand-shaped tokens (alpha-only words like
    "status", "view", "install") are kept verbatim so denylist regexes
    can still match meaningfully.

    Idempotent: passing in already-canonical input (containing <path>,
    <url>, or <arg>) returns it unchanged.
    """
    if not isinstance(raw, str):
        return ""
    parts = raw.strip().split()
    if not parts:
        return ""
    out = [parts[0]]
    for tok in parts[1:]:
        if tok in _PLACEHOLDERS:
            out.append(tok)
        elif tok.startswith("-"):
            out.append(tok)
        elif "://" in tok:
            out.append("<url>")
        elif "/" in tok or tok.startswith("~"):
            out.append("<path>")
        elif _SUBCOMMAND_RE.match(tok):
            out.append(tok)
        else:
            out.append("<arg>")
    return " ".join(out)


def _read_exec_approvals(path: Path) -> ExecApprovals:
    ea = ExecApprovals(path=str(path))
    if not path.exists():
        return ea
    ea.present = True
    obj, err = _read_json_file(path)
    if obj is None:
        ea.read_error = err
        return ea

    defaults = obj.get("defaults") or {}
    if isinstance(defaults, dict):
        # defaults is typically a dict of pattern → metadata, or list of patterns
        ea.defaults_count = len(defaults)
    elif isinstance(defaults, list):
        ea.defaults_count = len(defaults)

    agents = obj.get("agents") or {}
    if isinstance(agents, dict):
        for agent_id, body in sorted(agents.items()):
            if not isinstance(body, dict):
                continue
            approvals = body.get("approvals") or body.get("commands") or body.get("patterns") or []
            patterns: list[str] = []
            if isinstance(approvals, dict):
                raw_list = list(approvals.keys())
            elif isinstance(approvals, list):
                raw_list = [a if isinstance(a, str) else (a.get("command") or a.get("pattern") or "")
                            for a in approvals]
            else:
                raw_list = []
            for raw in raw_list:
                canon = _canonicalize_pattern(raw)
                if canon:
                    patterns.append(canon)
            ea.agents.append(AgentApprovals(
                agent_id=agent_id,
                count=len(patterns),
                patterns=sorted(set(patterns)),
            ))

    ea.signature = hashlib.sha256(
        json.dumps(
            {"defaults": ea.defaults_count,
             "agents": [(a.agent_id, sorted(a.patterns)) for a in ea.agents]},
            sort_keys=True, ensure_ascii=True,
        ).encode()
    ).hexdigest()
    return ea


def _read_cron_jobs(path: Path) -> ScheduledInvocations:
    si = ScheduledInvocations(path=str(path))
    if not path.exists():
        return si
    si.present = True
    obj, err = _read_json_file(path)
    if obj is None:
        si.read_error = err
        return si

    jobs_raw = obj.get("jobs") or []
    if not isinstance(jobs_raw, list):
        return si

    for j in jobs_raw:
        if not isinstance(j, dict):
            continue
        payload = j.get("payload") or {}
        payload_kind = (payload.get("kind") if isinstance(payload, dict) else None) or "unknown"
        schedule = j.get("schedule") or {}
        schedule_kind = (schedule.get("kind") if isinstance(schedule, dict) else None) or "unknown"

        # Cap detection: spec open question 1 — the exact field path may vary.
        # Read both common shapes (payload.maxTurns / payload.budgetUsd and
        # job-level caps) and treat any non-None positive value as present.
        def _has_cap(*paths: tuple[str, ...]) -> bool:
            for chain in paths:
                cur: Any = j
                for seg in chain:
                    if not isinstance(cur, dict):
                        cur = None
                        break
                    cur = cur.get(seg)
                if isinstance(cur, (int, float)) and cur > 0:
                    return True
            return False

        has_turn_cap = _has_cap(
            ("payload", "maxTurns"),
            ("payload", "turnCap"),
            ("maxTurns",),
            ("turnCap",),
        )
        has_budget_cap = _has_cap(
            ("payload", "maxBudgetUsd"),
            ("payload", "budgetUsd"),
            ("maxBudgetUsd",),
            ("budgetUsd",),
        )

        summary = ""
        if isinstance(payload, dict):
            if payload_kind == "agentTurn":
                msg = payload.get("message") or ""
                summary = f"msg: {str(msg)[:80]}"
            elif payload_kind == "systemEvent":
                cmd = payload.get("command") or payload.get("event") or ""
                summary = f"event: {str(cmd)[:80]}"
            elif payload_kind == "shell":
                cmd = payload.get("command") or ""
                summary = f"cmd: {str(cmd)[:80]}"

        sig = hashlib.sha256(
            json.dumps(
                {"id": j.get("id"), "name": j.get("name"), "payload_kind": payload_kind,
                 "schedule_kind": schedule_kind, "summary": summary},
                sort_keys=True, ensure_ascii=True,
            ).encode()
        ).hexdigest()

        si.jobs.append(CronJob(
            id=str(j.get("id") or ""),
            name=str(j.get("name") or ""),
            enabled=bool(j.get("enabled", True)),
            schedule_kind=schedule_kind,
            payload_kind=payload_kind,
            has_turn_cap=has_turn_cap,
            has_budget_cap=has_budget_cap,
            payload_summary=summary,
            signature=sig,
        ))

    return si


# ── Composite reader ─────────────────────────────────────────────────────────

def read_inventory(
    bot_id: str,
    config: "dict[str, Any] | None" = None,
    *,
    home_override: Path | None = None,
) -> PermissionInventory:
    """Read all three sources for one bot.

    `home_override` lets tests point at a synthetic home tree without
    needing pwd.getpwnam.
    """
    home = home_override or bot_home(bot_id, config)
    oc_path = home / ".openclaw" / "openclaw.json"
    ea_path = home / ".openclaw" / "exec-approvals.json"
    cron_path = home / ".openclaw" / "cron" / "jobs.json"

    return PermissionInventory(
        bot_id=bot_id,
        observed_at=_utc_now_iso(),
        permission_config=_read_permission_config(oc_path),
        exec_approvals=_read_exec_approvals(ea_path),
        scheduled_invocations=_read_cron_jobs(cron_path),
    )


# ── On-disk cache ─────────────────────────────────────────────────────────────

def inventory_dir(shared_dir: Path) -> Path:
    return shared_dir / "permissions" / "inventory"


def inventory_path(shared_dir: Path, bot_id: str) -> Path:
    return inventory_dir(shared_dir) / f"{bot_id}.json"


def write_inventory(inv: PermissionInventory, shared_dir: Path) -> None:
    """Atomically write the inventory to {shared_dir}/permissions/inventory/<bot>.json."""
    target = inventory_path(shared_dir, inv.bot_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(inv.to_dict(), indent=2, sort_keys=True))
    tmp.replace(target)


def load_inventory(shared_dir: Path, bot_id: str) -> dict | None:
    p = inventory_path(shared_dir, bot_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
