"""Context-health Phase 0 — the prefix-stability join.

Spec: internal/spec-context-observability-2026-07-30.md (Phase 0).
Post-mortem: internal/incident-post-mortem-2026-07-31-cost-containment.md (§2).

Answers ONE question: **does the composed system-prefix change on the turns
that miss cache?** The plugin's PrefixHashLedger (dark, per-bot flag
``prefixHashLedgerEnabled``) writes one ``prefix_hash`` record per
``before_prompt_build`` to ``{shared_dir}/{bot}/turns/prefix-hashes-*.jsonl``;
the gateway's turn writer records ``cache_read_tokens`` /
``cache_write_tokens`` per turn to ``turns-*.jsonl`` in the same directory.
This module joins the two streams per session and prints:

  * prefix change rate (share of hash records that differ from the previous
    one in the same session), with per-block attribution — WHICH of the four
    injected blocks (capabilities / digest / narrative / speaker) churned;
  * cold-miss turns (``cache_read == 0`` and ``cache_write`` above a floor,
    excluding each session's expected-cold first turn);
  * the contingency between the two: of the attributable cold misses, how many
    coincided with a prefix change.

Verdict semantics (printed, not enforced): a high coincidence rate is
CONSISTENT with the §2 root cause; a low one on adequate data REFUTES it for
Evolve's share of the prompt (the hash covers only the appendSystemContext
Evolve composes — churn upstream of the plugin is invisible here and would
show as cold misses with a stable hash). Either answer is the point.

Failure posture (spec §Failure posture): an unreadable file is reported as
UNREADABLE and excluded — it is never treated as "no data, all clean".
Insufficient data yields ``insufficient_data``, not a passing verdict.

Usage::

    python3 context_health.py --bot personal-bot [--shared-dir D] [--days 7]
    python3 context_health.py --bot personal-bot --overhead   # + Phase A audit

``--overhead`` adds the Evolve-overhead decomposition
([spec-evolve-overhead-budget-2026-07-31.md](../../internal/spec-evolve-overhead-budget-2026-07-31.md)
Phase A): bootstrap-file weight split Evolve-authored vs OC-convention (A1),
injected-block weight per turn from the prefix-hash ledger (A1), the
fixed-prefix rewrite estimate from the turn stream (A1), and the
Evolve-initiated LLM call rollup by ``trigger_kind`` from the cost-event
annotations (A2). Per-tool schema weight is an explicit v1 gap and is
reported as such.

Read-only; no Signals, no writes (that is Phase 3).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from platform_profile import get_profile

# A turn whose cache_write is below this is a heartbeat-ish/no-context call,
# not a full prefix re-prime; spec calls this the "prefix_floor".
DEFAULT_PREFIX_FLOOR_TOKENS = 1000

# Verdict thresholds: below MIN_COLD_MISSES attributable cold misses we refuse
# to conclude anything (insufficient_data, per spec).
MIN_COLD_MISSES = 5
CONSISTENT_SHARE = 0.7
REFUTED_SHARE = 0.3

BLOCK_NAMES = ("capabilities", "digest", "narrative", "speaker", "cost_downgrade")

# Sentinel: file exists but could not be read/parsed. Never collapses to [].
UNREADABLE = object()


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # py3.10 fromisoformat rejects a trailing Z.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_jsonl(path: Path) -> "list[dict] | object":
    """All records of a JSONL file, or UNREADABLE (missing file → [])."""
    if not path.exists():
        return []
    records: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if isinstance(rec, dict):
                    records.append(rec)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return UNREADABLE
    return records


@dataclass
class SessionJoin:
    """Per-session pairing of ordered turn records with ordered hash records."""

    session_id: str
    turns: list[dict] = field(default_factory=list)
    hashes: list[dict] = field(default_factory=list)


@dataclass
class JoinReport:
    total_turns: int = 0
    total_hashes: int = 0
    sessions: int = 0
    paired_turns: int = 0
    # Hash-stream stability.
    hash_transitions: int = 0
    hash_changes: int = 0
    block_change_counts: dict = field(default_factory=lambda: {b: 0 for b in BLOCK_NAMES})
    presence_flaps: int = 0  # prefix went absent→present or present→absent
    # Turn-stream cache behaviour (first turn of each session excluded).
    cold_misses: int = 0
    warm_turns: int = 0
    # The contingency (only turns that paired with two consecutive hashes).
    cold_and_changed: int = 0
    cold_and_stable: int = 0
    warm_and_changed: int = 0
    warm_and_stable: int = 0
    unreadable_files: list = field(default_factory=list)

    @property
    def attributable_cold(self) -> int:
        return self.cold_and_changed + self.cold_and_stable


def group_sessions(turns: list[dict], hashes: list[dict]) -> list[SessionJoin]:
    """Group both streams by session_id and time-order each stream."""
    by_id: dict[str, SessionJoin] = {}
    for rec in turns:
        sid = rec.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        by_id.setdefault(sid, SessionJoin(sid)).turns.append(rec)
    for rec in hashes:
        sid = rec.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        by_id.setdefault(sid, SessionJoin(sid)).hashes.append(rec)

    def _key(rec: dict) -> datetime:
        return _parse_ts(rec.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)

    for join in by_id.values():
        join.turns.sort(key=_key)
        join.hashes.sort(key=_key)
    return list(by_id.values())


def _changed_blocks(prev: dict, cur: dict) -> list[str]:
    prev_shas = prev.get("appended_block_shas") or {}
    cur_shas = cur.get("appended_block_shas") or {}
    return [b for b in BLOCK_NAMES if prev_shas.get(b) != cur_shas.get(b)]


def analyze(
    turns: list[dict],
    hashes: list[dict],
    prefix_floor: int = DEFAULT_PREFIX_FLOOR_TOKENS,
) -> JoinReport:
    """The Phase 0 join, pure. See module docstring for semantics.

    Pairing: within a session, the k-th turn record pairs with the k-th hash
    record. ``before_prompt_build`` fires once per turn at turn start and the
    turn record is written once at agent_end, so ordinal pairing is exact when
    both streams are complete; when counts differ (gateway restart mid-window,
    ledger enabled mid-session) the tail with no counterpart is left unpaired
    rather than guessed — unpaired turns still count in the cold/warm totals,
    just not in the contingency.
    """
    report = JoinReport(total_turns=len(turns), total_hashes=len(hashes))
    for join in group_sessions(turns, hashes):
        report.sessions += 1

        # Hash-stream stability (independent of pairing).
        for prev, cur in zip(join.hashes, join.hashes[1:]):
            report.hash_transitions += 1
            if prev.get("prefix_sha256") != cur.get("prefix_sha256"):
                report.hash_changes += 1
                if (prev.get("prefix_sha256") is None) != (cur.get("prefix_sha256") is None):
                    report.presence_flaps += 1
                for block in _changed_blocks(prev, cur):
                    report.block_change_counts[block] += 1

        # Turn-stream cache behaviour + the contingency.
        for idx, turn in enumerate(join.turns):
            if idx == 0:
                continue  # first turn of a session is expected-cold
            cache_read = turn.get("cache_read_tokens") or 0
            cache_write = turn.get("cache_write_tokens") or 0
            cold = cache_read == 0 and cache_write > prefix_floor
            if cold:
                report.cold_misses += 1
            else:
                report.warm_turns += 1
            if idx < len(join.hashes):
                report.paired_turns += 1
                changed = (
                    join.hashes[idx - 1].get("prefix_sha256")
                    != join.hashes[idx].get("prefix_sha256")
                )
                if cold and changed:
                    report.cold_and_changed += 1
                elif cold:
                    report.cold_and_stable += 1
                elif changed:
                    report.warm_and_changed += 1
                else:
                    report.warm_and_stable += 1
    return report


def verdict(report: JoinReport) -> str:
    """Plain-language answer to the Phase 0 question."""
    if report.total_hashes == 0:
        return (
            "no_data: no prefix-hash records found — enable "
            "prefixHashLedgerEnabled on this bot and let real turns accumulate"
        )
    if report.attributable_cold < MIN_COLD_MISSES:
        return (
            f"insufficient_data: only {report.attributable_cold} attributable "
            f"cold-miss turns (need ≥{MIN_COLD_MISSES}) — keep collecting"
        )
    share = report.cold_and_changed / report.attributable_cold
    if share >= CONSISTENT_SHARE:
        return (
            f"CONSISTENT with §2: {report.cold_and_changed}/{report.attributable_cold} "
            f"({share:.0%}) of attributable cold misses coincided with an "
            "Evolve prefix change"
        )
    if share <= REFUTED_SHARE:
        return (
            f"NOT consistent with §2 for Evolve's share: only "
            f"{report.cold_and_changed}/{report.attributable_cold} ({share:.0%}) of "
            "attributable cold misses coincided with an Evolve prefix change — "
            "churn is upstream of the plugin, or TTL/fallback-driven"
        )
    return (
        f"ambiguous: {report.cold_and_changed}/{report.attributable_cold} "
        f"({share:.0%}) of attributable cold misses coincided with a prefix "
        "change — mixed causes; decompose per session"
    )


def render(report: JoinReport, bot_id: str, days: int) -> str:
    lines = [
        f"context-health Phase 0 — {bot_id}, last {days} day(s)",
        "",
        f"  turns records:        {report.total_turns}",
        f"  prefix-hash records:  {report.total_hashes}",
        f"  sessions:             {report.sessions}",
        f"  paired turns:         {report.paired_turns}",
        "",
        "  prefix stability (hash stream):",
    ]
    if report.hash_transitions:
        rate = report.hash_changes / report.hash_transitions
        lines.append(
            f"    changed on {report.hash_changes}/{report.hash_transitions} "
            f"transitions ({rate:.0%}); presence flaps: {report.presence_flaps}"
        )
        ranked = sorted(report.block_change_counts.items(), key=lambda kv: -kv[1])
        attribution = ", ".join(f"{name}={count}" for name, count in ranked)
        lines.append(f"    block churn: {attribution}")
    else:
        lines.append("    <2 hash records per session — no transitions to measure")
    lines += [
        "",
        "  cache behaviour (turn stream, first-of-session excluded):",
        f"    cold misses: {report.cold_misses}   warm: {report.warm_turns}",
        "",
        "  contingency (paired turns only):",
        f"    cold & prefix-changed: {report.cold_and_changed}"
        f"    cold & prefix-stable: {report.cold_and_stable}",
        f"    warm & prefix-changed: {report.warm_and_changed}"
        f"    warm & prefix-stable: {report.warm_and_stable}",
        "",
        f"  verdict: {verdict(report)}",
    ]
    if report.unreadable_files:
        lines += [
            "",
            "  ⚠ UNREADABLE (excluded from every number above — NOT clean):",
        ]
        lines += [f"    {p}" for p in report.unreadable_files]
    return "\n".join(lines)


def load_streams(
    shared_dir: Path, bot_id: str, days: int
) -> "tuple[list, list, list]":
    """(turns, hashes, unreadable_paths) for the last ``days``."""
    turns_dir = shared_dir / bot_id / "turns"
    turns: list[dict] = []
    hashes: list[dict] = []
    unreadable: list[str] = []
    today = datetime.now(timezone.utc).date()
    for offset in range(days):
        day = (today - timedelta(days=offset)).isoformat()
        for prefix, sink in (("turns-", turns), ("prefix-hashes-", hashes)):
            path = turns_dir / f"{prefix}{day}.jsonl"
            loaded = load_jsonl(path)
            if loaded is UNREADABLE:
                unreadable.append(str(path))
            else:
                sink.extend(loaded)  # type: ignore[arg-type]
    return turns, hashes, unreadable


def collect(
    shared_dir: Path, bot_id: str, days: int, prefix_floor: int
) -> JoinReport:
    """Load the last ``days`` of both streams for one bot and analyze."""
    turns, hashes, unreadable = load_streams(shared_dir, bot_id, days)
    report = analyze(turns, hashes, prefix_floor=prefix_floor)
    report.unreadable_files = unreadable
    return report


# ── Phase A: Evolve-overhead decomposition ──────────────────────────────────
# Spec: internal/spec-evolve-overhead-budget-2026-07-31.md (A1 + A2).

# Rough tokens-per-byte for English/markdown prose — matches the post-mortem's
# hand probe (18,414 B ≈ 4,600 tok). An estimate, labeled as such in output.
BYTES_PER_TOKEN = 4

# Bootstrap files Evolve authors into the bot workspace. Everything else at
# the workspace root is OC-convention or bot-authored.
EVOLVE_BOOTSTRAP_FILES = frozenset({"POD_CONDUCT.md", "INSTALLED_APPS.md"})

# The workspace files OpenClaw actually ingests into per-turn context — a
# FIXED filename list (BOOTSTRAP_FILE_NAMES + the memory file), verified
# against the installed gateway dist on 2026-07-31. Workspace-root .md files
# outside this set (including Evolve's POD_CONDUCT.md / INSTALLED_APPS.md
# copies) sit on disk for on-demand reads and cost NOTHING per turn — the
# 2026-07-31 post-mortem §5.5 misattributed them; the real Evolve carrier is
# the session_surface systemAppend, measured separately below.
OC_BOOTSTRAP_INGESTED = frozenset({
    "AGENTS.md", "SOUL.md", "TOOLS.md", "IDENTITY.md",
    "USER.md", "HEARTBEAT.md", "BOOTSTRAP.md", "MEMORY.md",
})

# Evolve-initiated LLM work, as tagged by the plugin's cost_event
# trigger_kind (CostLedger.ts). user_turn/heartbeat/unknown are the bot's own
# work; these are Evolve's analysis calls.
EVOLVE_TRIGGER_KINDS = frozenset({"summarizer", "classifier", "task_extractor"})


@dataclass
class OverheadReport:
    # A1 — bootstrap files: (name, bytes, est_tokens, evolve_authored, ingested)
    bootstrap_files: list = field(default_factory=list)
    workspace_unreadable: "str | None" = None
    # A1 — session_start systemAppend (the real Evolve context carrier):
    # (total_chars, [(header, chars), ...]) or None when unmeasurable.
    session_injection: "tuple[int, list] | None" = None
    # A1 — injected blocks (from the prefix-hash ledger)
    block_records: int = 0
    block_mean_chars: float = 0.0
    block_p95_chars: int = 0
    # A1 — fixed-prefix rewrite estimate (from the turn stream)
    prefix_rewrite_estimate_tokens: "int | None" = None
    # A1/B2 — per-tool schema weights from the plugin's boot-time footprint
    # ({shared}/{bot}/context-footprint.json); None = file absent/unreadable.
    tool_footprint: "dict | None" = None
    # B7 — the evo MCP toolset manifest (mcp.servers.evo_tools). Invisible
    # to the plugin footprint above (it wraps only api.registerTool), so it
    # is measured by shelling out to
    # ``python3 -m evolve_admin.evo.tools --manifest-size``.
    # None = unmeasured (evolve_admin not importable / subprocess failed).
    mcp_manifest: "dict | None" = None
    # Whether THIS bot's openclaw.json registers mcp.servers.evo_tools —
    # "yes" / "no" / "unknown: <why>". The manifest weight only rides in
    # prompts of bots that carry the registration (the primary/evo bot).
    mcp_registered: str = "unknown: not checked"
    # A2 — cost rollup by trigger_kind: {kind: {"calls": n, "cost_usd": x}}
    call_rollup: dict = field(default_factory=dict)
    cost_events_seen: int = 0
    annotations_unreadable: list = field(default_factory=list)

    @property
    def ingested_bootstrap_tokens(self) -> int:
        return sum(t for (_, _, t, _ev, ing) in self.bootstrap_files if ing)

    @property
    def evolve_ondisk_tokens(self) -> int:
        return sum(
            t for (_, _, t, ev, ing) in self.bootstrap_files if ev and not ing)

    @property
    def evolve_call_cost_usd(self) -> float:
        return sum(
            v["cost_usd"] for k, v in self.call_rollup.items()
            if k in EVOLVE_TRIGGER_KINDS
        )


def bootstrap_weights(workspace_dir: Path) -> "tuple[list, str | None]":
    """(name, bytes, est_tokens, evolve_authored, ingested) for
    workspace-root ``*.md``. ``ingested`` = in OC's fixed bootstrap set and
    therefore carried in per-turn context; everything else is on-disk-only.

    Unreadable workspace (0700 bot home without the evolve ACL, missing dir)
    returns an error string — reported, never silently "no bootstrap weight"
    (the exists-lies-under-0700-parents lesson).
    """
    rows: list = []
    try:
        entries = sorted(workspace_dir.glob("*.md"))
    except OSError as exc:
        return [], f"{type(exc).__name__}: {exc}"
    if not entries and not workspace_dir.is_dir():
        return [], "workspace dir missing or unsearchable"
    for p in entries:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        rows.append((
            p.name, size, size // BYTES_PER_TOKEN,
            p.name in EVOLVE_BOOTSTRAP_FILES,
            p.name in OC_BOOTSTRAP_INGESTED,
        ))
    return rows, None


def split_injection_sections(text: str) -> "list[tuple[str, int]]":
    """(header, chars) per ``[SECTION HEADER]`` block of a session_surface
    output; a leading un-headed chunk reports as ``<preamble>``. Pure."""
    import re
    parts = re.split(r"(?m)^(\[[A-Z][^\]\n]*\])$", text)
    out: "list[tuple[str, int]]" = []
    if parts and parts[0].strip():
        out.append(("<preamble>", len(parts[0])))
    for i in range(1, len(parts) - 1, 2):
        out.append((parts[i], len(parts[i]) + len(parts[i + 1])))
    return out


def session_injection_weight(
    bot_id: str, shared_dir: Path, role: "str | None" = None
) -> "tuple[int, list] | None":
    """(total_chars, [(header, chars), ...]) of the session_start
    systemAppend, measured by running session_surface with a SCRATCH
    workspace dir — so the B1 apps-reference write lands in a temp dir and
    this audit stays side-effect-free while measuring the post-B1 (stub)
    shape. None on any failure (missing script, timeout, error exit)."""
    import subprocess
    import sys as _sys
    import tempfile

    script = Path(__file__).parent / "session_surface.py"
    if not script.exists():
        return None
    with tempfile.TemporaryDirectory(prefix="ctx-health-ss-") as scratch:
        cmd = [
            _sys.executable, str(script),
            "--bot", bot_id,
            "--shared-dir", str(shared_dir),
            "--workspace-dir", scratch,
        ]
        if role:
            cmd += ["--role", role]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
                cwd=str(script.parent),
            )
        except (OSError, subprocess.SubprocessError):
            return None
    if proc.returncode != 0:
        return None
    text = proc.stdout
    return len(text), split_injection_sections(text)


def mcp_manifest_weight() -> "dict | None":
    """Measure the evo MCP toolset by shelling out to
    ``python3 -m evolve_admin.evo.tools --manifest-size`` (overhead-budget
    B7). Same fail-soft convention as session_injection_weight: None on
    any failure (evolve_admin not importable in this interpreter, error
    exit, timeout, non-JSON output) — rendered as unmeasured, never zero.
    """
    import subprocess
    import sys as _sys

    try:
        proc = subprocess.run(
            [_sys.executable, "-m", "evolve_admin.evo.tools", "--manifest-size"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        stats = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return stats if isinstance(stats, dict) else None


def mcp_registration_status(workspace_dir: "Path | None") -> str:
    """Does this bot's openclaw.json register ``mcp.servers.evo_tools``?

    The workspace dir is ``<bot home>/.openclaw/workspace``, so the config
    sits one level up. Returns "yes" / "no" / "unknown: <why>" — an
    unreadable config is reported as unknown, never as no (the
    exists-lies-under-0700-parents lesson).
    """
    if workspace_dir is None:
        return "unknown: workspace dir unknown"
    oc_path = workspace_dir.parent / "openclaw.json"
    try:
        cfg = json.loads(oc_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return f"unknown: {type(exc).__name__} reading {oc_path}"
    except json.JSONDecodeError:
        return f"unknown: {oc_path} is not valid JSON"
    servers = ((cfg.get("mcp") or {}).get("servers") or {}) if isinstance(cfg, dict) else {}
    return "yes" if "evo_tools" in servers else "no"


def injected_block_weight(hashes: list) -> "tuple[int, float, int]":
    """(records, mean_chars, p95_chars) over prefix-hash records that carry
    ``combined_chars`` (all schema-v1 records do)."""
    sizes = sorted(
        rec.get("combined_chars", 0) for rec in hashes
        if isinstance(rec.get("combined_chars"), int)
    )
    if not sizes:
        return 0, 0.0, 0
    mean = sum(sizes) / len(sizes)
    p95 = sizes[min(len(sizes) - 1, int(len(sizes) * 0.95))]
    return len(sizes), mean, p95


def prefix_rewrite_estimate(turns: list, floor: int = DEFAULT_PREFIX_FLOOR_TOKENS) -> "int | None":
    """min(cache_write_tokens) over turns that wrote at least ``floor`` —
    the smallest observed full-prefix re-prime, i.e. the fixed prefix plus
    the shortest conversation state. Automates the post-mortem's hand probe;
    an upper bound on the fixed prefix."""
    writes = [
        t.get("cache_write_tokens") or 0 for t in turns
        if (t.get("cache_write_tokens") or 0) > floor
    ]
    return min(writes) if writes else None


def call_rollup(annotation_records: list) -> "tuple[dict, int]":
    """Cost/count by ``trigger_kind`` over ``type == "cost_event"`` records."""
    rollup: dict = {}
    seen = 0
    for rec in annotation_records:
        if rec.get("type") != "cost_event":
            continue
        seen += 1
        kind = rec.get("trigger_kind") or "unknown"
        slot = rollup.setdefault(kind, {"calls": 0, "cost_usd": 0.0})
        slot["calls"] += 1
        try:
            slot["cost_usd"] += float(rec.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            # Malformed cost on one record: count the call, skip the dollars —
            # a bad row must not sink the whole rollup.
            slot.setdefault("bad_cost_rows", 0)
            slot["bad_cost_rows"] += 1
    return rollup, seen


def collect_overhead(
    shared_dir: Path,
    bot_id: str,
    days: int,
    turns: list,
    hashes: list,
    workspace_dir: "Path | None" = None,
) -> OverheadReport:
    report = OverheadReport()
    if workspace_dir is not None:
        report.bootstrap_files, report.workspace_unreadable = bootstrap_weights(workspace_dir)
    else:
        report.workspace_unreadable = "workspace dir unknown (pass --workspace-dir)"
    report.session_injection = session_injection_weight(bot_id, shared_dir)
    try:
        # Canonical home is the gateway-owned turns/ subdir; the bot-root
        # path is a fallback for any file written before the relocation.
        for fp_path in (
            shared_dir / bot_id / "turns" / "context-footprint.json",
            shared_dir / bot_id / "context-footprint.json",
        ):
            if fp_path.exists():
                loaded_fp = json.loads(fp_path.read_text(encoding="utf-8"))
                if isinstance(loaded_fp, dict):
                    report.tool_footprint = loaded_fp
                break
    except (OSError, json.JSONDecodeError):
        report.tool_footprint = None  # reported as unmeasured, never as zero
    # Probe the manifest only when this bot actually carries the server —
    # the weight is a property of the evo_tools registry, not of bots that
    # never load it, and the probe is a subprocess we shouldn't spawn for
    # every member bot.
    report.mcp_registered = mcp_registration_status(workspace_dir)
    if report.mcp_registered == "yes":
        report.mcp_manifest = mcp_manifest_weight()
    report.block_records, report.block_mean_chars, report.block_p95_chars = (
        injected_block_weight(hashes))
    report.prefix_rewrite_estimate_tokens = prefix_rewrite_estimate(turns)

    ann_dir = shared_dir / "annotations" / bot_id
    records: list = []
    today = datetime.now(timezone.utc).date()
    for offset in range(days):
        day = (today - timedelta(days=offset)).isoformat()
        # The converted per-bot store is cost_events-<date>.jsonl (the
        # authoritative output of cost_event_converter). The plugin's own
        # mixed annotations file (<date>.jsonl) also carries cost_event rows
        # and is the fallback for bots the converter hasn't covered — read
        # ONE of the two per day, never both (the converter mirrors the
        # plugin rows, so reading both double-counts).
        primary = ann_dir / f"cost_events-{day}.jsonl"
        fallback = ann_dir / f"{day}.jsonl"
        chosen = primary if primary.exists() else fallback
        loaded = load_jsonl(chosen)
        if loaded is UNREADABLE:
            report.annotations_unreadable.append(str(chosen))
        else:
            records.extend(loaded)  # type: ignore[arg-type]
    report.call_rollup, report.cost_events_seen = call_rollup(records)
    return report


def render_overhead(report: OverheadReport) -> str:
    lines = ["", "  ── Evolve overhead (spec-evolve-overhead-budget Phase A) ──", ""]

    lines.append("  workspace *.md (tokens ≈ bytes/4; 'ingested' = in OC's fixed")
    lines.append("  bootstrap set and carried in per-turn context):")
    if report.workspace_unreadable:
        lines.append(f"    ⚠ UNREADABLE — {report.workspace_unreadable} (NOT zero)")
    else:
        for name, size, tokens, evolve_authored, ingested in report.bootstrap_files:
            owner = "Evolve" if evolve_authored else "OC/bot"
            carried = "ingested" if ingested else "on-disk only"
            lines.append(
                f"    {name:<28} {size:>7} B  ~{tokens:>5} tok  [{owner}, {carried}]")
        lines.append(
            f"    ingested bootstrap total: ~{report.ingested_bootstrap_tokens} tok/turn; "
            f"Evolve on-disk-only (free): ~{report.evolve_ondisk_tokens} tok"
        )

    lines += ["", "  session-start injection (Evolve systemAppend — carried for the"]
    lines.append("  session's whole life, ahead of the conversation):")
    if report.session_injection is not None:
        total, sections = report.session_injection
        lines.append(f"    total: {total} chars (~{total // BYTES_PER_TOKEN} tok)")
        for header, chars in sorted(sections, key=lambda hc: -hc[1]):
            lines.append(
                f"    {header[:52]:<54} {chars:>6} chars ~{chars // BYTES_PER_TOKEN:>5} tok")
    else:
        lines.append("    unmeasured (session_surface unavailable or errored)")

    lines += ["", "  injected blocks (prefix-hash ledger):"]
    if report.block_records:
        lines.append(
            f"    {report.block_records} records; mean {report.block_mean_chars:.0f} chars "
            f"(~{int(report.block_mean_chars) // BYTES_PER_TOKEN} tok), "
            f"p95 {report.block_p95_chars} chars"
        )
    else:
        lines.append("    no records — arm prefixHashLedgerEnabled to measure")

    lines += ["", "  fixed-prefix rewrite estimate (min full cache_write):"]
    if report.prefix_rewrite_estimate_tokens is not None:
        lines.append(f"    ~{report.prefix_rewrite_estimate_tokens} tokens")
    else:
        lines.append("    no qualifying turns in window")

    lines += ["", "  LLM calls by trigger_kind (cost events, window total):"]
    if report.cost_events_seen:
        for kind, slot in sorted(report.call_rollup.items(),
                                 key=lambda kv: -kv[1]["cost_usd"]):
            tag = "Evolve" if kind in EVOLVE_TRIGGER_KINDS else "bot"
            lines.append(
                f"    {kind:<16} {slot['calls']:>5} calls  ${slot['cost_usd']:.4f}  [{tag}]"
            )
        lines.append(
            f"    Evolve-initiated total: ${report.evolve_call_cost_usd:.4f} "
            f"of {report.cost_events_seen} events"
        )
    else:
        lines.append("    no cost events in window")
    if report.annotations_unreadable:
        lines.append("    ⚠ UNREADABLE annotation days (excluded, NOT clean):")
        lines += [f"      {p}" for p in report.annotations_unreadable]

    lines += ["", "  tool schemas (plugin boot-time footprint):"]
    if report.tool_footprint:
        fp = report.tool_footprint
        total = fp.get("total_chars", 0)
        lines.append(
            f"    {fp.get('tool_count', 0)} tools registered at tier="
            f"{fp.get('tier', '?')}: {total} chars (~{total // BYTES_PER_TOKEN} tok)"
        )
        tools = sorted(
            (t for t in fp.get("tools", []) if isinstance(t, dict)),
            key=lambda t: -(t.get("chars") or 0),
        )
        for t in tools[:8]:
            chars = t.get("chars") or 0
            lines.append(
                f"    {str(t.get('name', '?')):<32} {chars:>6} chars "
                f"~{max(0, chars) // BYTES_PER_TOKEN:>4} tok")
        if len(tools) > 8:
            lines.append(f"    … {len(tools) - 8} more (see context-footprint.json)")
    else:
        lines.append(
            "    unmeasured — no context-footprint.json (plugin predates the "
            "footprint writer, or gateway has not restarted since)")

    lines += ["", "  evo MCP toolset (mcp.servers.evo_tools — separate from the"]
    lines.append("  plugin footprint above; rides only in prompts of bots that")
    lines.append(f"  register the server. this bot: {report.mcp_registered}):")
    if report.mcp_registered == "no":
        lines.append("    not registered — no MCP toolset weight in this bot's prompts")
    elif report.mcp_registered.startswith("unknown"):
        lines.append(
            "    probe skipped — registration unknown (pass --workspace-dir "
            "or run where the bot's openclaw.json is readable)")
    elif report.mcp_manifest:
        mm = report.mcp_manifest
        total = mm.get("total_chars", 0)
        lines.append(
            f"    {mm.get('tool_count', 0)} tools, {total} chars as served "
            f"(~{total // BYTES_PER_TOKEN} tok)"
        )
        fams = [f for f in mm.get("families", []) if isinstance(f, dict)]
        for f in fams[:6]:
            chars = f.get("chars") or 0
            lines.append(
                f"    {str(f.get('family', '?')):<24} {f.get('tool_count', 0):>3} tools "
                f"{chars:>7} chars ~{chars // BYTES_PER_TOKEN:>5} tok")
        if len(fams) > 6:
            rest = sum((f.get("chars") or 0) for f in fams[6:])
            lines.append(
                f"    … {len(fams) - 6} more families, {rest} chars "
                "(python3 -m evolve_admin.evo.tools --manifest-size for all)")
    else:
        lines.append(
            "    unmeasured — evolve_admin not importable from this "
            "interpreter (run under the pod venv), or the probe errored")
    return "\n".join(lines)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--bot", required=True, help="bot id (turns dir owner)")
    parser.add_argument(
        "--shared-dir",
        default=get_profile().shared_dir_default,
        help="pod shared dir (default: platform profile)",
    )
    parser.add_argument("--days", type=int, default=7, help="lookback window (default 7)")
    parser.add_argument(
        "--prefix-floor",
        type=int,
        default=DEFAULT_PREFIX_FLOOR_TOKENS,
        help="min cache_write tokens for a cold miss to count (default 1000)",
    )
    parser.add_argument(
        "--overhead",
        action="store_true",
        help="append the Evolve-overhead decomposition (spec Phase A1+A2)",
    )
    parser.add_argument(
        "--workspace-dir",
        default=None,
        help=(
            "bot workspace dir for bootstrap-file weights (default: "
            "<bot home>/.openclaw/workspace via evolve_config.bot_home; "
            "reads may need the evolve user's ACL)"
        ),
    )
    args = parser.parse_args(argv)
    turns, hashes, unreadable = load_streams(Path(args.shared_dir), args.bot, args.days)
    report = analyze(turns, hashes, prefix_floor=args.prefix_floor)
    report.unreadable_files = unreadable
    print(render(report, args.bot, args.days))
    if args.overhead:
        workspace: "Path | None" = None
        if args.workspace_dir:
            workspace = Path(args.workspace_dir)
        else:
            try:
                from evolve_config import bot_home  # analyzer top-level module
                workspace = Path(bot_home(args.bot)) / ".openclaw" / "workspace"
            except Exception:
                workspace = None  # reported as unknown by collect_overhead
        overhead = collect_overhead(
            Path(args.shared_dir), args.bot, args.days, turns, hashes,
            workspace_dir=workspace,
        )
        print(render_overhead(overhead))
    return 0


if __name__ == "__main__":
    sys.exit(main())
