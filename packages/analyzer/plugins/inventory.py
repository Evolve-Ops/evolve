"""plugins.inventory — read a bot's plugin configuration from openclaw.json.

Spec: docs/spec-plugin-inventory-2026-05-10.md §3.2 (PluginInventory).

The inventory is the observed state. The monitor compares it against
the baseline and emits signals on drift. The admin UI also reads
inventory files directly to render the matrix.

Four sub-surfaces (per spec §2):
  - entries: per-plugin enabled flag + config + hook-policy + subagent-policy
  - allow / deny lists: top-level plugin-name allowlist / denylist
  - install provenance: ``plugins.installs.<id>.source`` ("path" | "npm" |
    "archive" | "clawhub" | "marketplace")
  - load paths: ``plugins.load.paths`` — directories OpenClaw scans for
    plugin code

Schema reference: docs/schemas/oc-config-schema.txt:40640 (installs),
                  docs/schemas/oc-config-schema.txt:19919 (commands.plugins).
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


# Field-name patterns that look like secrets; replaced with <redacted> in the
# config-signature hash so credential rotation doesn't trip drift signals.
_SECRET_KEY_HINTS = (
    "token", "key", "secret", "password", "passwd", "apikey", "api_key",
    "bearer", "authorization", "auth_token",
)


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class PluginEntry:
    """One plugin entry as configured on a bot.

    Provenance fields (install_source, install_spec, install_path,
    resolved_name, resolved_version, clawhub_channel, clawhub_family)
    are merged from the OC install records — either the legacy
    ``openclaw.json::plugins.installs`` block (rare on current OC
    versions) or the per-bot ``~/.openclaw/plugins/installs.json``
    (current) / ``installs.json.migrated`` (OC v2026.5.28 migration
    snapshot). They're advisory-only — surfaced on the Plugins page
    so the operator can see where each plugin came from. The
    plugin_monitor does NOT alert on them; the trust model is
    multi-dimensional (source + scope + channel) and we'd rather
    show the data than guess at an allowlist. See
    docs/spec-plugin-posture-rework-2026-06-06.md §1.4.
    """

    name: str
    enabled: bool
    has_hooks_policy: bool
    has_subagent_policy: bool
    config_signature: str  # sha256 over (config + hooks-policy + subagent-policy); secrets redacted
    install_source: str | None = None  # "npm" / "path" / "archive" / "clawhub" / "marketplace"
    install_spec: str | None = None    # npm spec like "@openclaw/brave-plugin@2026.5.18" or sourcePath
    install_path: str | None = None    # resolved on-disk install directory
    resolved_name: str | None = None   # npm package name once resolved (e.g. "@openclaw/brave-plugin")
    resolved_version: str | None = None
    clawhub_channel: str | None = None  # "official" / "community" / "private"
    clawhub_family: str | None = None   # "code-plugin" / "bundle-plugin"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PluginInventory:
    """Snapshot of a bot's full plugins block at a point in time."""

    bot_id: str
    observed_at: str
    openclaw_config_path: str
    openclaw_config_present: bool
    entries: list[PluginEntry] = field(default_factory=list)
    allow_list: list[str] | None = None  # None means absent (not the same as empty list)
    deny_list: list[str] | None = None
    load_paths: list[str] = field(default_factory=list)
    self_mutation_commands_plugins: bool = False
    set_signature: str = ""  # sha256 over sorted enabled-plugin names (fast equality vs baseline)
    read_error: str | None = None
    # V1.5-3: upstream version tracking. ``upstream_version`` is the
    # parsed CalVer string from ``meta.lastTouchedVersion`` (e.g.
    # "2026.4.29"). ``upstream_version_raw`` is the original string for
    # debugging (rare edge cases — beta tags, hand-edited configs).
    # Both ``None`` if the meta block was unreadable / absent.
    upstream_version: str | None = None
    upstream_version_raw: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["entries"] = [
            e.to_dict() if isinstance(e, PluginEntry) else e for e in self.entries
        ]
        return d


# ── Reader ────────────────────────────────────────────────────────────────────

def _read_openclaw_json(path: Path, *, timeout: float = 5.0) -> tuple[dict | None, str | None]:
    """Direct read first, sudo /bin/cat fallback. Matches mcp.inventory pattern."""
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


def _redact_secrets(obj: Any) -> Any:
    """Walk a dict/list tree and replace likely-secret leaf values with <redacted>.

    Used inside the config_signature so that rotating a brave API key (or
    similar) doesn't trip plugin_config_drift. The redaction is structural,
    not value-content-based — names like ``apiKey`` / ``token`` / ``secret``
    are the signal.
    """
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if _is_secret_key(k) else _redact_secrets(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_secrets(item) for item in obj]
    return obj


def _is_secret_key(key: str) -> bool:
    lower = key.lower().replace("-", "").replace("_", "")
    return any(hint.replace("_", "") in lower for hint in _SECRET_KEY_HINTS)


def _signature_for_entry(entry_cfg: dict[str, Any]) -> str:
    """Stable sha256 over (config + hooks + subagent) with secrets redacted."""
    canonical = {
        "config": _redact_secrets(entry_cfg.get("config") or {}),
        "hooks": entry_cfg.get("hooks") or {},
        "subagent": entry_cfg.get("subagent") or {},
        "enabled": bool(entry_cfg.get("enabled", True)),
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def _set_signature(enabled_names: list[str]) -> str:
    """Fast hash over sorted enabled-plugin names — set-level equality check."""
    canonical = json.dumps(sorted(enabled_names), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _read_installs_records(home: Path) -> dict[str, dict[str, Any]]:
    """Read the bot's OC install records and return ``{plugin_id: record}``.

    Three candidate locations, merged with later sources winning per-id:

      1. ``~/.openclaw/plugins/installs.json`` — current OC location
         (writes here on contemporary versions; may not exist yet on
         bots still on the migration snapshot).
      2. ``~/.openclaw/plugins/installs.json.migrated`` — snapshot OC
         wrote during the v2026.5.28 schema migration. On bots that
         haven't been touched by the new installer path since the
         migration, this is the only record file present.
      3. ``openclaw.json::plugins.installs`` — legacy location;
         empty on recent OC versions but read by older bots.

    Returns an empty dict on any read failure — provenance is advisory,
    so we don't surface read errors as part of the inventory contract.
    """
    out: dict[str, dict[str, Any]] = {}
    for fname in ("installs.json.migrated", "installs.json"):
        path = home / ".openclaw" / "plugins" / fname
        if not path.exists():
            continue
        data, _err = _read_openclaw_json(path)
        if not isinstance(data, dict):
            continue
        records = data.get("installRecords")
        if isinstance(records, dict):
            for pid, rec in records.items():
                if isinstance(pid, str) and isinstance(rec, dict):
                    out[pid] = rec
    return out


def _provenance_from_record(rec: dict[str, Any]) -> dict[str, str | None]:
    """Extract the provenance subset we carry on PluginEntry."""
    return {
        "install_source": rec.get("source") if isinstance(rec.get("source"), str) else None,
        "install_spec": (
            rec.get("spec")
            or rec.get("sourcePath")
            or rec.get("resolvedSpec")
            if isinstance(rec, dict) else None
        ),
        "install_path": rec.get("installPath") if isinstance(rec.get("installPath"), str) else None,
        "resolved_name": rec.get("resolvedName") if isinstance(rec.get("resolvedName"), str) else None,
        "resolved_version": (
            rec.get("resolvedVersion")
            if isinstance(rec.get("resolvedVersion"), str)
            else rec.get("version") if isinstance(rec.get("version"), str) else None
        ),
        "clawhub_channel": rec.get("clawhubChannel") if isinstance(rec.get("clawhubChannel"), str) else None,
        "clawhub_family": rec.get("clawhubFamily") if isinstance(rec.get("clawhubFamily"), str) else None,
    }


def read_inventory(
    bot_id: str,
    config: "dict[str, Any] | None" = None,
) -> PluginInventory:
    """Read a bot's plugin inventory from its openclaw.json + install records."""
    home = bot_home(bot_id, config)
    oc_path = home / ".openclaw" / "openclaw.json"

    inv = PluginInventory(
        bot_id=bot_id,
        observed_at=_utc_now_iso(),
        openclaw_config_path=str(oc_path),
        openclaw_config_present=oc_path.exists(),
    )

    oc, err = _read_openclaw_json(oc_path)
    if oc is None:
        inv.read_error = err
        return inv

    plugins_block = oc.get("plugins") or {}
    entries_block = plugins_block.get("entries") or {}
    # Provenance lookup: prefer the external install-records files
    # (current OC writes there); fall back to the legacy in-config block
    # for older bots.
    installs_records = _read_installs_records(home)
    legacy_installs = plugins_block.get("installs") or {}

    enabled_names: list[str] = []
    for name, cfg in entries_block.items():
        if not isinstance(cfg, dict):
            continue
        # OC default-enabled: schema says enabled may be omitted; tools that
        # set it explicitly tend to write false. Treat missing as enabled.
        enabled = cfg.get("enabled")
        is_enabled = enabled is not False  # None or True → enabled
        rec = installs_records.get(name) or (
            legacy_installs.get(name) if isinstance(legacy_installs.get(name), dict) else None
        )
        prov = _provenance_from_record(rec) if isinstance(rec, dict) else {}
        inv.entries.append(
            PluginEntry(
                name=name,
                enabled=is_enabled,
                has_hooks_policy=bool(cfg.get("hooks")),
                has_subagent_policy=bool(cfg.get("subagent")),
                config_signature=_signature_for_entry(cfg),
                install_source=prov.get("install_source"),
                install_spec=prov.get("install_spec"),
                install_path=prov.get("install_path"),
                resolved_name=prov.get("resolved_name"),
                resolved_version=prov.get("resolved_version"),
                clawhub_channel=prov.get("clawhub_channel"),
                clawhub_family=prov.get("clawhub_family"),
            )
        )
        if is_enabled:
            enabled_names.append(name)

    # Sort entries for stable serialization
    inv.entries.sort(key=lambda e: e.name)

    # allow / deny may be absent (None) — distinguish from empty list
    allow = plugins_block.get("allow")
    deny = plugins_block.get("deny")
    inv.allow_list = list(allow) if isinstance(allow, list) else None
    inv.deny_list = list(deny) if isinstance(deny, list) else None

    load_paths = (plugins_block.get("load") or {}).get("paths") or []
    if isinstance(load_paths, list):
        inv.load_paths = [str(p) for p in load_paths]

    commands = oc.get("commands") or {}
    inv.self_mutation_commands_plugins = bool(commands.get("plugins"))

    # V1.5-3: capture meta.lastTouchedVersion so the security page can
    # show per-bot upstream OC version alongside the other plugin
    # inventory data. Cheap (no extra read). See
    # evolve_admin.upstream_version for the canonical reader.
    meta = oc.get("meta") or {}
    if isinstance(meta, dict):
        raw_version = meta.get("lastTouchedVersion")
        if isinstance(raw_version, str) and raw_version.strip():
            inv.upstream_version_raw = raw_version.strip()
            inv.upstream_version = _canonical_calver(raw_version.strip())

    inv.set_signature = _set_signature(enabled_names)
    return inv


def _canonical_calver(raw: str) -> str | None:
    """Normalize a CalVer string to ``YYYY.M.PATCH`` (strip 'v', prerelease).

    Returns ``None`` for unparseable input. Mirrors the parser in
    ``evolve_admin.upstream_version.parse_version`` but kept inline so
    the analyzer package doesn't import from admin.
    """
    s = raw.strip().lstrip("v").lstrip("V")
    s = s.split("-", 1)[0].split("+", 1)[0]
    parts = s.split(".")
    if len(parts) < 3:
        return None
    try:
        return f"{int(parts[0])}.{int(parts[1])}.{int(parts[2])}"
    except ValueError:
        return None


# ── On-disk cache ─────────────────────────────────────────────────────────────

def inventory_dir(shared_dir: Path) -> Path:
    return shared_dir / "plugins" / "inventory"


def inventory_path(shared_dir: Path, bot_id: str) -> Path:
    return inventory_dir(shared_dir) / f"{bot_id}.json"


def write_inventory(inv: PluginInventory, shared_dir: Path) -> None:
    """Atomically write the inventory cache."""
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
