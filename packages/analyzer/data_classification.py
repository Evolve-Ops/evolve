"""data_classification — decide which workspace paths are cloud-eligible.

Phase 3 of the backup architecture rework
(spec internal/spec-backup-and-data-classification-2026-05-28.md).

The resolver answers one question per file: cloud | local | ephemeral.

- ``cloud``     — eligible for cloud backup (push to GitHub).
- ``local``     — stays on the mini. Local backups (Time Machine) still
                  cover it; cloud backup does not.
- ``ephemeral`` — not backed up by either mechanism. Caches, scratch
                  space, regenerable indices.

Sources of truth, in increasing specificity (longest-prefix wins):

  1. Built-in rules. Always applied (currently empty — see the
     ``_BUILTIN_RULES`` comment for the 2026-05-29 retraction).
  2. Pod-wide rules from ``network.json::backup.data_paths``. For paths
     pod-wide concerns own (proposals/, signals/, observations/).
  3. Per-app rules from each manifest's ``app_files_privacy`` (for the
     app's ``files:`` list) and ``data_paths`` (for the runtime
     directories the app produces).

Per-app rules only apply to paths inside the bot's workspace; paths
outside the workspace (absolute paths, ``..``-traversal) are ignored
when building the rule set.

Backwards compat: an app manifest with NO v15 classification fields set
contributes NO rules — its files fall through to the pod-wide default,
which itself defaults to ``cloud`` so a pod without any v15 declarations
behaves exactly like pre-v15 (everything cloud-eligible).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Built-in rules. Hard-coded because they would enforce invariants we never
# want operators to override.
#
# **2026-05-29 — there are no built-in rules.** The original design had
# ``evolve-backup/ → ephemeral`` as a "recursion guard," but that framing
# was wrong: ``evolve-backup/`` doesn't contain a recursive copy of the
# workspace, it contains the exact payload files ``backup.py`` writes
# (redacted ``openclaw.json``, metrics, run-state) that MUST be
# cloud-backed-up to preserve the security/audit trail. The rule was
# stripping the entire backup commit's payload before push, and the
# Phase 4a audit was then false-positive-alerting on those same files.
# See the review session 2026-05-29 for the full triage.
#
# If a genuine recursion concern shows up later (e.g., if ``backup.py``
# starts copying the workspace into a subdirectory for some reason), add
# the rule then.
_BUILTIN_RULES: list[tuple[str, str, str]] = []

PRIVACY_CLOUD     = "cloud"
PRIVACY_LOCAL     = "local"
PRIVACY_EPHEMERAL = "ephemeral"
VALID_PRIVACY = (PRIVACY_CLOUD, PRIVACY_LOCAL, PRIVACY_EPHEMERAL)


@dataclass(frozen=True)
class ClassificationRule:
    """One (prefix, privacy) entry. ``source`` is for debugging only."""
    prefix: str
    privacy: str
    source: str

    def matches(self, path: str) -> bool:
        """True iff ``path`` is the rule's prefix or a descendant of it."""
        if self.prefix == "":
            return True  # universal default
        p = self.prefix.rstrip("/")
        return path == p or path.startswith(p + "/")


@dataclass
class ClassificationResolver:
    rules: list[ClassificationRule] = field(default_factory=list)
    default: str = "cloud"

    def classify(self, path: str) -> str:
        """Return ``cloud`` | ``local`` | ``ephemeral`` for a workspace-relative path.

        Longest matching prefix wins; ties broken by rule ordering (later
        rules added override earlier — manifests are processed after the
        pod-wide block, which is itself after built-ins, so manifests
        override pod-wide which overrides built-ins for equal-length
        prefixes). Built-ins are encoded first so this ordering matters.
        """
        norm = _normalise_path(path)
        if norm is None:
            return self.default

        best: ClassificationRule | None = None
        for rule in self.rules:
            if not rule.matches(norm):
                continue
            if best is None or len(rule.prefix) >= len(best.prefix):
                best = rule
        return best.privacy if best is not None else self.default

    def explain(self, path: str) -> tuple[str, str]:
        """Return (privacy, source) — useful for debugging / UI surfacing."""
        norm = _normalise_path(path)
        if norm is None:
            return self.default, "fallthrough:invalid-path"
        best: ClassificationRule | None = None
        for rule in self.rules:
            if rule.matches(norm) and (best is None or len(rule.prefix) >= len(best.prefix)):
                best = rule
        if best is None:
            return self.default, "fallthrough:default"
        return best.privacy, best.source


# ─── Path normalisation ────────────────────────────────────────────────────


def _normalise_path(path: str) -> str | None:
    """Return a workspace-relative POSIX-style path, or None if invalid.

    Strips a leading ``./`` if present and rejects anything that looks
    like it leaves the workspace (absolute paths, ``..`` segments). Paths
    that look outside the workspace shouldn't be classified at all; the
    caller decides what to do (typically skip them rather than treating
    them as a "default" miss).
    """
    if not isinstance(path, str):
        return None
    s = path.replace("\\", "/").strip()
    if not s:
        return None
    if s.startswith("/"):
        return None  # absolute path; reject
    if s.startswith("./"):
        s = s[2:]
    parts = s.split("/")
    if any(p == ".." for p in parts):
        return None
    return s


# ─── Building rules from sources ───────────────────────────────────────────


def _add_path_rule(
    rules: list[ClassificationRule],
    raw_path: str,
    privacy: str,
    source: str,
) -> bool:
    """Append a rule iff the path normalises and privacy is valid. Returns success."""
    if privacy not in VALID_PRIVACY:
        return False
    norm = _normalise_path(raw_path)
    if norm is None:
        return False
    # Trailing slash semantics: declared paths nearly always reference a
    # *directory*. Preserve the slash so matches() treats it as a prefix
    # rather than an exact file name. Callers that want an exact-file rule
    # can pass a path without a trailing slash; matches() still requires
    # exact match for those.
    if raw_path.endswith("/") and not norm.endswith("/"):
        norm = norm + "/"
    rules.append(ClassificationRule(prefix=norm, privacy=privacy, source=source))
    return True


def _rules_from_app_manifest(manifest: dict) -> list[ClassificationRule]:
    """Convert one app manifest's v15 fields into rules.

    Manifests with no v15 classification declared contribute nothing —
    they fall through to the pod-wide default (which itself defaults to
    cloud).

    **Limitation — per-app ``default_for_unclassified`` is NOT applied
    at runtime.** The field is accepted by the PATCH endpoint and stored
    on the manifest, but the resolver does not consult it. Implementing
    it correctly requires an "app scope" concept (what files belong to
    which app) that doesn't exist yet — without scope, a per-app default
    is either ignored or becomes a last-app-wins universal override,
    neither of which matches operator intent. The PATCH endpoint logs a
    warning when the field is set so operators know it's stored-but-
    inert. A future PR will integrate this with the app scanner so
    newly-discovered files under the app's territory inherit the default
    by becoming explicit ``data_paths`` entries.
    """
    rules: list[ClassificationRule] = []
    app_id = (manifest.get("id") or manifest.get("name") or "?").strip() or "?"

    # data_paths: per-directory rules. Each entry shape:
    # {"path": str, "privacy": "cloud"|"local"|"ephemeral", "note"?: str}
    for entry in manifest.get("data_paths") or []:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        privacy = entry.get("privacy")
        if not isinstance(path, str) or not isinstance(privacy, str):
            continue
        _add_path_rule(rules, path, privacy, source=f"app:{app_id}:data_paths")

    # app_files_privacy: classification for the manifest's `files:` list.
    # Only applied when explicitly set ("" means "not declared").
    afp = manifest.get("app_files_privacy")
    if isinstance(afp, str) and afp in ("cloud", "local"):
        for entry in manifest.get("files") or []:
            # v4: list[str]. v5+: list[dict] with "path".
            if isinstance(entry, str):
                raw = entry
            elif isinstance(entry, dict):
                raw = entry.get("path")
                if not isinstance(raw, str):
                    continue
            else:
                continue
            _add_path_rule(rules, raw, afp, source=f"app:{app_id}:app_files_privacy")

    return rules


def _rules_from_pod_config(network: dict) -> tuple[list[ClassificationRule], str | None]:
    """Read pod-wide rules from ``network.json::backup.data_paths``.

    Returns (rules, pod_default). ``pod_default`` is None when not
    declared in the config (caller decides the fallback default).
    """
    backup = network.get("backup") if isinstance(network.get("backup"), dict) else {}
    rules: list[ClassificationRule] = []
    for entry in backup.get("data_paths") or []:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        privacy = entry.get("privacy")
        if not isinstance(path, str) or not isinstance(privacy, str):
            continue
        _add_path_rule(rules, path, privacy, source="pod:data_paths")

    pod_default = backup.get("default_for_unclassified")
    if not isinstance(pod_default, str) or pod_default not in VALID_PRIVACY:
        pod_default = None
    return rules, pod_default


def build_resolver(
    *,
    manifests: Iterable[dict] = (),
    network: dict | None = None,
    fallback_default: str = "cloud",
) -> ClassificationResolver:
    """Compose a resolver from built-ins + pod-wide rules + per-app rules.

    ``manifests`` is the list of app manifest dicts (typically loaded
    from each bot's workspace via load_bot_manifests). ``network`` is
    the loaded network.json dict. ``fallback_default`` is the policy
    when neither manifests nor pod_default declare otherwise — kept at
    ``cloud`` so pre-v15 pods behave exactly like before (everything
    cloud-eligible).
    """
    rules: list[ClassificationRule] = []
    # 1. Built-ins first (least specific in our intent, but their
    #    prefixes are themselves specific so they still win for matching
    #    paths over any pod-wide universal default).
    for prefix, privacy, source in _BUILTIN_RULES:
        rules.append(ClassificationRule(prefix=prefix, privacy=privacy, source=source))

    # 2. Pod-wide rules.
    pod_rules, pod_default = _rules_from_pod_config(network or {})
    rules.extend(pod_rules)

    # 3. Per-app rules.
    for manifest in manifests:
        if not isinstance(manifest, dict):
            continue
        rules.extend(_rules_from_app_manifest(manifest))

    default = pod_default if pod_default is not None else fallback_default
    return ClassificationResolver(rules=rules, default=default)


# ─── Convenience loader ────────────────────────────────────────────────────


# ─── Four-tier backup posture ──────────────────────────────────────────────
#
# UI shortcut from spec §"Four-tier backup posture (UI shortcut)". The
# operator picks one of four tiers; the Data tab derives the underlying
# manifest fields. Tiers are *inferred* from the manifest's current
# state (not stored as a separate field) so per-path overrides can
# auto-promote to "some_data_local" without the UI fighting the operator.

TIER_UNCLASSIFIED    = "unclassified"
TIER_WHOLE_APP_LOCAL = "whole_app_local"
TIER_ALL_DATA_LOCAL  = "all_data_local"
TIER_SOME_DATA_LOCAL = "some_data_local"
TIER_FULL_CLOUD      = "full_cloud"

VALID_TIERS = (
    TIER_WHOLE_APP_LOCAL,
    TIER_ALL_DATA_LOCAL,
    TIER_SOME_DATA_LOCAL,
    TIER_FULL_CLOUD,
)

# What each tier writes when the operator picks it. ``data_paths_privacy``
# is applied to every existing data_paths entry's privacy; ``some_data_local``
# is operator-authored so no template — the existing data_paths are kept.
TIER_TEMPLATES: dict[str, dict] = {
    TIER_WHOLE_APP_LOCAL: dict(
        app_files_privacy="local",
        default_for_unclassified="local",
        data_paths_privacy="local",
    ),
    TIER_ALL_DATA_LOCAL: dict(
        app_files_privacy="cloud",
        default_for_unclassified="local",
        data_paths_privacy="local",
    ),
    TIER_FULL_CLOUD: dict(
        app_files_privacy="cloud",
        default_for_unclassified="cloud",
        data_paths_privacy="cloud",
    ),
}


def infer_tier(manifest: dict) -> str:
    """Return which of the four tier shortcuts most closely matches the manifest.

    - ``unclassified`` when no v15 field is set (the empty-state question
      in the Data tab fires)
    - One of the four shortcuts when the manifest's state matches its
      template exactly
    - ``some_data_local`` whenever the operator has authored per-path
      classification that doesn't match any whole-app shortcut

    Auto-promotion: once the operator overrides any data_path, the tier
    drifts to ``some_data_local`` even if they intended ``whole_app_local``
    plus one carve-out. That's the spec'd behaviour — the UI surfaces
    intent honestly rather than pretending a mixed state is a shortcut.
    """
    afp = manifest.get("app_files_privacy") or ""
    default = manifest.get("default_for_unclassified") or ""
    data_paths = manifest.get("data_paths") or []

    if not afp and not default and not data_paths:
        return TIER_UNCLASSIFIED

    dp_privacies = {
        entry.get("privacy") for entry in data_paths
        if isinstance(entry, dict) and entry.get("privacy")
    }
    # all-local means: no data_paths OR every data_path is "local"
    all_dp_local = (not dp_privacies) or dp_privacies == {"local"}
    all_dp_cloud = (not dp_privacies) or dp_privacies == {"cloud"}

    if afp == "local" and default == "local" and all_dp_local:
        return TIER_WHOLE_APP_LOCAL
    if afp == "cloud" and default == "local" and all_dp_local:
        return TIER_ALL_DATA_LOCAL
    if afp == "cloud" and default == "cloud" and all_dp_cloud:
        return TIER_FULL_CLOUD
    return TIER_SOME_DATA_LOCAL


def apply_tier_to_manifest(manifest: dict, tier: str) -> dict:
    """Return an updated copy of ``manifest`` with the tier's template applied.

    For the three whole-app tiers, the template overwrites
    ``app_files_privacy``, ``default_for_unclassified``, and the privacy
    of every existing ``data_paths`` entry. ``some_data_local`` is a
    no-op shortcut — it just signals "the operator will set things by
    hand"; existing per-path declarations are preserved.

    Raises ValueError on an unknown tier.
    """
    if tier == TIER_SOME_DATA_LOCAL:
        # Spec semantics: picking this tier doesn't actually change anything
        # automatically. The operator authors data_paths individually.
        return dict(manifest)
    if tier not in TIER_TEMPLATES:
        raise ValueError(f"unknown tier: {tier!r}")
    template = TIER_TEMPLATES[tier]
    out = dict(manifest)
    out["app_files_privacy"] = template["app_files_privacy"]
    out["default_for_unclassified"] = template["default_for_unclassified"]
    new_dp_privacy = template["data_paths_privacy"]
    new_paths: list[dict] = []
    for entry in manifest.get("data_paths") or []:
        if not isinstance(entry, dict):
            continue
        new_entry = dict(entry)
        new_entry["privacy"] = new_dp_privacy
        new_paths.append(new_entry)
    out["data_paths"] = new_paths
    return out


def _manifest_has_classification(manifest: dict) -> bool:
    """True iff the manifest already carries any v15 classification field.

    Used by the scanner-side stamp to avoid clobbering operator-authored
    classifications. Empty string and empty list are "not declared."
    """
    if (manifest.get("app_files_privacy") or "").strip():
        return True
    if (manifest.get("default_for_unclassified") or "").strip():
        return True
    if manifest.get("data_paths"):
        return True
    return False


def stamp_per_bot_default(
    manifest: dict,
    *,
    bot_id: str,
    network: dict,
) -> dict:
    """Apply the bot's ``backup_default_tier`` to a freshly-created manifest.

    Reads ``network.json::bots[bot_id].backup_default_tier``. If set and
    the manifest doesn't already carry any v15 classification field,
    applies the tier template via ``apply_tier_to_manifest`` and returns
    the stamped copy. Otherwise returns the manifest unchanged.

    No-op conditions (idempotent — safe to call from scanner/forge):

      - Bot has no default tier configured → unchanged
      - Default tier is ``some_data_local`` → unchanged (template is a
        no-op anyway; the operator authors per-path on this tier)
      - Manifest already has classification declared → unchanged
        (prevents clobbering a rescan of an already-authored manifest)
      - Unknown tier value in network.json → unchanged (defensive)

    Spec: internal/spec-backup-and-data-classification-2026-05-28.md +
    the 2026-05-29 review session's per-bot-default extension. Called
    from ``applications/scanner.py`` and ``applications/forge_engine.py``
    at the point a new manifest is first persisted.
    """
    if not isinstance(network, dict):
        return manifest
    bots_cfg = network.get("bots") if isinstance(network.get("bots"), dict) else {}
    bot_cfg = bots_cfg.get(bot_id) or {}
    tier = (bot_cfg.get("backup_default_tier") or "").strip() if isinstance(bot_cfg, dict) else ""
    if not tier or tier == TIER_SOME_DATA_LOCAL or tier not in VALID_TIERS:
        return manifest
    if _manifest_has_classification(manifest):
        return manifest
    try:
        return apply_tier_to_manifest(manifest, tier)
    except ValueError:
        # Unknown tier — defensive; VALID_TIERS check above should catch it.
        return manifest


def load_bot_manifests(workspace: Path) -> list[dict]:
    """Read every manifest in a bot's workspace.

    Returns a list of dicts (parsed JSON) for every ``*.json`` file in
    the ``manifests/`` subdir. Silently skips files we can't read or
    parse — a broken manifest must not block backup.

    Filter matches ``server.py::_glob_manifests`` (the Apps tab's
    discovery): skip dotfiles + ``*_history*`` archives. Do NOT require
    an ``id`` field — v7-arc Instance manifests carry their identity via
    the bound Spec, and pre-id-required legacy manifests still need
    their classification fields honored. When a manifest lacks an
    ``id``, derive one from the filename stem so downstream code has a
    stable key.

    2026-05-29 fix for the Data tab vs Apps tab mismatch: the previous
    ``data.get("id")`` filter silently dropped every v7-arc Instance,
    which made the Data tab report "no apps installed" on bots that
    clearly had apps on the Apps tab. It also made the analyzer-side
    pruner and audit blind to those apps' classifications — operators
    setting an app to local would have its files pushed to cloud
    anyway because the manifest never made it into the resolver.
    """
    manifests_dir = workspace / "manifests"
    if not manifests_dir.is_dir():
        return []
    out: list[dict] = []
    for entry in sorted(manifests_dir.glob("*.json")):
        if entry.name.startswith(".") or "_history" in entry.name:
            continue
        try:
            data = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if not data.get("id"):
            data = {**data, "id": entry.stem}
        out.append(data)
    return out
