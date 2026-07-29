"""
app_bootstrap_footprint.py — measure per-app per-turn context cost.

Surfaces the "App bootstrap footprint" chip on the bot detail page and
backs the `app_*` verifier checks in app_audit_structural.py.

The measurement covers what an installed app contributes to every turn's
system prompt:

  - manifest `bot_guidance` (injected into the system prompt verbatim)
  - INSTALLED_APPS.md per-app section (autogen from manifest fields)
  - tool definitions registered by the app (skill defs, exec hooks)

It does NOT cover conversation history, OC framing, or POD_CONDUCT.md —
those are bot-wide constants, not per-app cost. The chip's purpose is to
isolate the slice operators can act on: trimming a manifest, splitting an
app, or moving work off heartbeat.

Calibration phase (per principle-apps-minimize-bootstrap-cost.md):
thresholds report at info severity. Bytes are the source of truth; tokens
and $/turn are estimates for legibility. The token-per-byte ratio is held
at 0.25 (well-known approximation for English manifests; deliberate
over-estimate so the chip is never accused of underselling cost).

Reads from the bot-side manifests dir; falls back to sudo cat if the
ACL hasn't been refreshed yet. Never writes.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from evolve_config import bot_home as _bot_home

# ── Tunables ─────────────────────────────────────────────────────────────────
#
# Per-byte token estimate. Manifests are dense English with JSON quoting;
# 4 bytes/token is the rough Anthropic guidance. Holding to 0.25 tokens/byte
# (= 4 bytes/token) keeps the chip honest at the small end and slightly
# pessimistic at the large end — both are fine for an attention chip.
_TOKENS_PER_BYTE = 0.25

# Default per-MTok price for the cost estimate. Picked Haiku 4.5 input
# pricing ($1/MTok) because (a) bootstrap-paying turns are typically tier3
# / heartbeat traffic that lands on Haiku, and (b) anything pricier should
# look even more expensive to the operator. The endpoint can override via
# query param when the operator wants to model a different tier.
_DEFAULT_USD_PER_MTOK = 1.0

# Thresholds for the verdict field. These match the principle doc and the
# four verifier-check trip points. Verifier checks copy these constants
# rather than re-deriving — single source of truth.
THRESHOLD_BOT_GUIDANCE_BYTES = 1024          # per-app
THRESHOLD_INSTALLED_APPS_ENTRY_CHARS = 500    # per-app
THRESHOLD_PER_APP_BYTES = 2048                # per-app subtotal
THRESHOLD_PER_BOT_AGGREGATE_BYTES = 10 * 1024  # bot total before warn
ALERT_PER_BOT_AGGREGATE_BYTES = 25 * 1024     # bot total before alert


# ── Public API ───────────────────────────────────────────────────────────────


def compute_app_bootstrap_footprint(bot_id: str) -> dict:
    """Return a per-app + per-bot footprint breakdown for `bot_id`.

    Always returns a dict with the same shape; populates `error` when the
    bot's manifests dir is unreachable. Callers should treat any non-None
    `error` as "no data this turn", not as a failure to display.

    The shape mirrors what the chip + endpoint surface verbatim — keep it
    stable; the UI parses these field names directly.
    """
    manifests_dir = _bot_home(bot_id) / ".openclaw" / "workspace" / "manifests"
    installed_apps_md = _bot_home(bot_id) / ".openclaw" / "workspace" / "INSTALLED_APPS.md"

    manifests = _load_manifests(manifests_dir)
    if manifests is None:
        return _empty_result(bot_id, error=f"manifests dir unreadable: {manifests_dir}")

    installed_apps_text = _read_text_with_fallback(installed_apps_md) or ""
    per_app_entries = _split_installed_apps_md(installed_apps_text)

    apps: list[dict] = []
    for manifest in manifests:
        app_id = manifest.get("id") or manifest.get("pkg_id") or "<unknown>"
        bg_bytes = _bot_guidance_bytes(manifest)
        ia_bytes = len(per_app_entries.get(app_id, "").encode("utf-8"))
        tool_bytes = _tool_defs_bytes(manifest)
        subtotal = bg_bytes + ia_bytes + tool_bytes
        apps.append({
            "id": app_id,
            "display_name": manifest.get("display_name") or manifest.get("name") or app_id,
            "bot_guidance_bytes": bg_bytes,
            "installed_apps_entry_bytes": ia_bytes,
            "tool_defs_bytes": tool_bytes,
            "subtotal_bytes": subtotal,
            "verdict": _per_app_verdict(bg_bytes, ia_bytes, subtotal),
            "transport_kind": _transport_kind(manifest),
            "transport_hint": _transport_hint(manifest),
        })

    apps.sort(key=lambda a: a["subtotal_bytes"], reverse=True)
    total_bytes = sum(a["subtotal_bytes"] for a in apps)
    total_tokens = int(total_bytes * _TOKENS_PER_BYTE)
    estimated_cost = round(total_tokens * _DEFAULT_USD_PER_MTOK / 1_000_000, 6)

    return {
        "bot_id": bot_id,
        "app_count": len(apps),
        "total_bytes": total_bytes,
        "total_tokens_estimated": total_tokens,
        "estimated_cost_per_cache_miss_usd": estimated_cost,
        "model_used_for_estimate": "anthropic/claude-haiku-4-5",
        "apps": apps,
        "thresholds": {
            "bot_guidance_bytes_per_app": THRESHOLD_BOT_GUIDANCE_BYTES,
            "installed_apps_entry_chars_per_app": THRESHOLD_INSTALLED_APPS_ENTRY_CHARS,
            "per_app_subtotal_bytes": THRESHOLD_PER_APP_BYTES,
            "per_bot_aggregate_warn_bytes": THRESHOLD_PER_BOT_AGGREGATE_BYTES,
            "per_bot_aggregate_alert_bytes": ALERT_PER_BOT_AGGREGATE_BYTES,
        },
        "verdict": _per_bot_verdict(total_bytes),
        "error": None,
    }


# ── Internals ────────────────────────────────────────────────────────────────


def _empty_result(bot_id: str, *, error: str | None = None) -> dict:
    return {
        "bot_id": bot_id,
        "app_count": 0,
        "total_bytes": 0,
        "total_tokens_estimated": 0,
        "estimated_cost_per_cache_miss_usd": 0.0,
        "model_used_for_estimate": "anthropic/claude-haiku-4-5",
        "apps": [],
        "thresholds": {
            "bot_guidance_bytes_per_app": THRESHOLD_BOT_GUIDANCE_BYTES,
            "installed_apps_entry_chars_per_app": THRESHOLD_INSTALLED_APPS_ENTRY_CHARS,
            "per_app_subtotal_bytes": THRESHOLD_PER_APP_BYTES,
            "per_bot_aggregate_warn_bytes": THRESHOLD_PER_BOT_AGGREGATE_BYTES,
            "per_bot_aggregate_alert_bytes": ALERT_PER_BOT_AGGREGATE_BYTES,
        },
        "verdict": "ok",
        "error": error,
    }


def _read_text_with_fallback(path: Path) -> str | None:
    """Direct read with sudo /bin/cat fallback (mirrors cost_profiles)."""
    try:
        return path.read_text()
    except PermissionError:
        pass
    except (OSError, UnicodeDecodeError):
        return None
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return r.stdout
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def _load_manifests(manifests_dir: Path) -> list[dict] | None:
    """Enumerate manifest JSON files. Returns None on permission failure
    (caller surfaces as error); empty list when the dir exists but has
    no manifests."""
    try:
        if not manifests_dir.exists():
            return []
    except PermissionError:
        # Dir exists but listing it errored; try sudo ls fallback below.
        pass

    json_paths: list[Path] = []
    try:
        for p in manifests_dir.iterdir():
            if p.suffix == ".json" and not p.name.startswith("."):
                json_paths.append(p)
    except (PermissionError, OSError):
        ls = subprocess.run(
            ["sudo", "/bin/ls", str(manifests_dir)],
            capture_output=True, text=True, timeout=5,
        )
        if ls.returncode != 0:
            return None
        for name in ls.stdout.splitlines():
            if name.endswith(".json") and not name.startswith("."):
                json_paths.append(manifests_dir / name)

    manifests: list[dict] = []
    for p in json_paths:
        text = _read_text_with_fallback(p)
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            manifests.append(data)
    return manifests


def _bot_guidance_bytes(manifest: dict) -> int:
    """Total UTF-8 bytes the manifest's bot_guidance contributes.

    The field is conventionally a list of {audience, text, ...} dicts; some
    older manifests use a string. We sum the `text` fields when present and
    fall back to the JSON-serialized form for safety — over-count is fine
    here, under-count would let oversized blocks slip the check.
    """
    bg = manifest.get("bot_guidance")
    if bg is None:
        return 0
    if isinstance(bg, str):
        return len(bg.encode("utf-8"))
    if isinstance(bg, list):
        total = 0
        for entry in bg:
            if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                total += len(entry["text"].encode("utf-8"))
            else:
                # Unknown shape — count the JSON form so we don't miss bytes.
                total += len(json.dumps(entry).encode("utf-8"))
        return total
    # Unknown shape — JSON-encode and count.
    return len(json.dumps(bg).encode("utf-8"))


def _tool_defs_bytes(manifest: dict) -> int:
    """Bytes the app's tool/skill registrations add to per-turn injection.

    Sums `exported_hooks`, `interface_contract`, and `recursive_llm.purposes`
    sizes — these are the manifest fields that template into per-turn tool
    catalogs. Approximate; the real upstream-OC injection is opaque to us.
    """
    total = 0
    for key in ("exported_hooks", "interface_contract"):
        v = manifest.get(key)
        if v:
            total += len(json.dumps(v).encode("utf-8"))
    rl = manifest.get("recursive_llm")
    if isinstance(rl, dict):
        purposes = rl.get("purposes") or []
        if purposes:
            total += len(json.dumps(purposes).encode("utf-8"))
    return total


# Matches "## <Display Name> — ..." or "## <Display Name>\n" — INSTALLED_APPS.md
# uses level-2 headings for each app. We key sections by the manifest id
# rather than the heading text, because heading text drifts.
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _split_installed_apps_md(text: str) -> dict[str, str]:
    """Slice INSTALLED_APPS.md into per-app sections.

    Returns {app_id_or_display_name: section_text}. Best-effort — falls back
    to the full file split by `## ` when heading→app-id matching fails.

    INSTALLED_APPS.md is autogenerated and we don't control its exact format
    here; the goal is "how many bytes for this app, roughly" not "exact
    rendered prompt." Over- or under-counting by a few percent is fine.
    """
    if not text:
        return {}
    sections: dict[str, str] = {}
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return {}
    for i, m in enumerate(matches):
        heading = m.group(1).strip()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        # Drop leading "<App Name> — " prefix; keep section keyed by display.
        sections[heading] = text[start:end]
    return sections


def _per_app_verdict(bg_bytes: int, ia_bytes: int, subtotal: int) -> str:
    if bg_bytes > THRESHOLD_BOT_GUIDANCE_BYTES * 2:
        return "warn"
    if subtotal > THRESHOLD_PER_APP_BYTES:
        return "warn"
    if bg_bytes > THRESHOLD_BOT_GUIDANCE_BYTES or ia_bytes > THRESHOLD_INSTALLED_APPS_ENTRY_CHARS:
        return "info"
    return "ok"


def _per_bot_verdict(total_bytes: int) -> str:
    if total_bytes > ALERT_PER_BOT_AGGREGATE_BYTES:
        return "alert"
    if total_bytes > THRESHOLD_PER_BOT_AGGREGATE_BYTES:
        return "warn"
    return "ok"


# ── Transport classification ────────────────────────────────────────────────
#
# Mirrors the verifier checks in app_audit_structural.py so the footprint
# chip can surface the same hints inline — operator sees "could be a cron"
# next to the bytes that hint would save, instead of finding it in an
# info-severity Signal they have to toggle visible.
#
# Kept narrow on purpose: we look at manifest declarations only (same as
# the verifier). The runtime-bot-transport scan (does any script import
# bot_tool / shell to openclaw_headless) is out of scope here.

def _manifest_declares_llm_intent(manifest: dict) -> bool:
    """Mirror of app_audit_structural._manifest_declares_llm_intent."""
    rl = manifest.get("recursive_llm")
    if not isinstance(rl, dict):
        return False
    return bool(rl.get("purposes"))


def _manifest_has_cli(manifest: dict) -> bool:
    contract = manifest.get("interface_contract") or {}
    cli = contract.get("cli") or []
    for entry in cli:
        if isinstance(entry, dict) and (entry.get("command") or "").strip():
            return True
    return False


def _transport_kind(manifest: dict) -> str:
    """Classify the manifest's declared transport: heartbeat, subagent,
    cron, or unknown.

    The choice maps to the per-turn cost the app pays:
      heartbeat — full bot session injection every tick
      subagent  — narrow context the app owns
      cron      — none; runs outside the bot session entirely
      unknown   — no producer surface declared (the v23 check catches this)
    """
    if manifest.get("heartbeat_evidence"):
        return "heartbeat"
    mode = str(manifest.get("invocation_mode") or "").strip().lower()
    if mode == "subagent":
        return "subagent"
    if manifest.get("crons") or manifest.get("scheduled_actions"):
        return "cron"
    return "unknown"


# Match the verifier: invocation-mode hint applies only when usage.model
# indicates user-routed invocation. Scheduled/event-driven apps don't get
# CLI calls from users, so the hint doesn't apply.
_USER_ROUTED_MODELS = frozenset({"user-initiated", "ambient", ""})


def _usage_model(manifest: dict) -> str:
    usage = manifest.get("usage")
    if not isinstance(usage, dict):
        identity = manifest.get("identity")
        if isinstance(identity, dict):
            usage = identity.get("usage") or {}
        else:
            usage = {}
    return str(usage.get("model") or "").strip().lower()


def _transport_hint(manifest: dict) -> str | None:
    """Return a one-word hint when the manifest's transport choice doesn't
    match its work shape. Mirrors the assertions in
    app_audit_structural.check_cron_eligible_used_heartbeat and
    check_invocation_mode_subagent so the chip surfaces the same signal
    inline.

    Returns None when no hint applies.
    """
    if manifest.get("heartbeat_evidence") and not _manifest_declares_llm_intent(manifest):
        return "could_be_cron"
    if _manifest_declares_llm_intent(manifest) and _manifest_has_cli(manifest):
        model = _usage_model(manifest)
        if model in _USER_ROUTED_MODELS:
            mode = str(manifest.get("invocation_mode") or "").strip().lower()
            if mode != "subagent":
                return "could_be_subagent"
    return None
