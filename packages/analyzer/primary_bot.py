"""
Primary-bot resolution helpers for engine LLM calls.

The "primary bot" is the bot referenced by ``network.json → primary``.
Its OS user owns the auth-profiles.json and openclaw.json that pod-wide
"engine" background work — judges, summarizers, classifier verifiers,
the help bot, the spec extractor, etc. — should authenticate against and
inherit model choices from.

Per-bot scans (e.g. ``applications/scanner.py``) intentionally read the
*target bot's* keys directly and must NOT use this module.

Both the admin and analyzer packages import from here. Keep it dependency-
free (stdlib only) so it loads cleanly from either side.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any


# ── Identity helpers ──────────────────────────────────────────────────────────


def primary_bot_id(network: dict[str, Any]) -> str | None:
    """Return the primary bot ID from network.json, or None if unset.

    Resolution order:
      1. ``network.primary`` (top-level field set by the wizard).
      2. The first bot in ``network.bots`` whose ``role`` is ``"primary"``.
      3. Legacy fallback: ``"evolve"`` if it exists in ``network.bots``.

    The last fallback covers pods that predate the explicit ``primary``
    field — every existing pod has a bot literally named ``evolve`` filling
    this role, so the lookup still resolves correctly there.
    """
    pid = network.get("primary")
    if isinstance(pid, str) and pid:
        return pid
    bots = network.get("bots") or {}
    if isinstance(bots, dict):
        for bot_id, cfg in bots.items():
            if isinstance(cfg, dict) and cfg.get("role") == "primary":
                return bot_id
        if "evolve" in bots:
            return "evolve"
    return None


def primary_bot_user(network: dict[str, Any]) -> str | None:
    """Resolve the primary bot's system user.

    Falls back to the bot ID itself when ``bots[<id>].user`` is unset
    (matching the convention elsewhere — see ``_user_for_bot`` in
    ``packages/admin/evolve_admin/web/server.py``).
    """
    pid = primary_bot_id(network)
    if not pid:
        return None
    bot_cfg = (network.get("bots") or {}).get(pid) or {}
    return bot_cfg.get("user") or pid


# The dedicated, unprivileged macOS/Linux account the evo gateway runs on
# AFTER the Phase E.2.b cutover (spec-evo-account-separation-2026-05-25). The
# ``evolve`` account is the privileged admin-daemon service user and is NOT a
# bot account — see ``deploy.EVO_GATEWAY_USER`` (the admin-side twin of this
# constant). Kept here, stdlib-only, so analyzer-side monitors can detect the
# separated state without importing the admin package.
EVO_GATEWAY_USER = "evo"


def primary_is_separated_evo(network: dict[str, Any]) -> bool:
    """True iff the pod's primary bot runs on its OWN dedicated ``evo`` account.

    This is the post-evo-account-separation state (spec Phase E.2.b cutover):
    the primary bot is ``evo`` AND its resolved OS account is the dedicated
    ``evo`` user — distinct from the privileged ``evolve`` service account that
    runs only the admin daemon and carries NO bot-shaped ``openclaw.json``.

    Used by the openclaw.json-centric monitors (audit_config / install-integrity
    ownership + config-validate gauntlet) to skip the primary on a separated
    pod, where its config legitimately is not a stat-able bot ``openclaw.json``
    the way an ordinary member bot's is (the ``evolve`` service account hosts
    none; the ``evo`` account's config may live in the migrated OC agent SQLite
    store rather than at the JSON path the monitors stat).

    **True no-op on pre-separation / legacy pods:** resolves False whenever the
    primary's gateway account is NOT the dedicated ``evo`` user — i.e. legacy
    pods whose primary is a bot literally named ``evolve``, and fresh macOS pods
    pre-cutover where the primary ``evo`` still runs on the ``evolve`` service
    account (its ``openclaw.json`` is present at ``/Users/evolve`` and SHOULD be
    audited). Pure dict resolution — no fs read, no account lookup — so the same
    answer holds on a dev box without the accounts provisioned.
    """
    return primary_bot_user(network) == EVO_GATEWAY_USER


def primary_bot_home(network: dict[str, Any]) -> Path | None:
    """Return the primary bot's home directory, or None."""
    user = primary_bot_user(network)
    if not user:
        return None
    try:
        import pwd
        return Path(pwd.getpwnam(user).pw_dir)
    except Exception:
        return Path(f"/Users/{user}")


def _coerce_port(value: Any) -> int | None:
    """Best-effort int coercion for a port value; ``None`` when not a usable
    port (absent, empty, zero, or non-numeric). The ``except`` returns a value
    rather than swallowing — a malformed port resolves to None, not silence."""
    if value in (None, "", 0):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def primary_bot_gateway_port(network: dict[str, Any]) -> int | None:
    """Resolve the primary bot's gateway port.

    Reader-resolve direction (evo-account-separation S1): the primary's port
    lives on its own bot entry ``bots[<primary>].port`` — the same canonical
    field every other bot uses (see ``config.get_bot_port``). Falls back to the
    legacy top-level ``network["evolve"].gateway_port`` block so pods
    provisioned before this change keep resolving until the S4 migration runs.

    Returns ``None`` when neither location carries a usable port. Pure dict
    resolution (no fs read) — keeps this module stdlib-only and import-light.
    """
    pid = primary_bot_id(network)
    if pid:
        cfg = (network.get("bots") or {}).get(pid) or {}
        port = _coerce_port(cfg.get("port") or cfg.get("gateway_port"))
        if port is not None:
            return port
    # Legacy fallback: top-level evolve block (pre-S1 pods).
    return _coerce_port((network.get("evolve") or {}).get("gateway_port"))


def primary_bot_comms_mode(network: dict[str, Any], default: str = "") -> str:
    """Resolve the primary bot's comms mode (``headless`` / ``dedicated``).

    Reader-resolve direction: the comms mode lives on
    ``bots[<primary>].comms_mode``, with the legacy top-level
    ``network["evolve"].comms_mode`` as a fallback for pods provisioned before
    this change. Returns ``default`` when unset in both locations.
    """
    pid = primary_bot_id(network)
    if pid:
        cfg = (network.get("bots") or {}).get(pid) or {}
        mode = cfg.get("comms_mode")
        if isinstance(mode, str) and mode:
            return mode
    legacy = (network.get("evolve") or {}).get("comms_mode")
    if isinstance(legacy, str) and legacy:
        return legacy
    return default


# ── Auth resolution ───────────────────────────────────────────────────────────


def primary_bot_auth_profile_paths(network: dict[str, Any]) -> list[Path]:
    """Candidate paths for the primary bot's auth-profiles.json.

    Multiple shapes are tried because openclaw has stored auth-profiles in
    different locations across versions; each callsite previously walked
    its own list. Centralising here means a single point of truth.
    """
    home = primary_bot_home(network)
    if not home:
        return []
    return [
        home / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json",
        home / ".openclaw" / "agents" / "main" / "auth-profiles.json",
        home / ".openclaw" / "auth-profiles.json",
    ]


def extract_anthropic_key(raw: Any) -> str:
    """Extract an Anthropic api_key from various auth-profiles.json shapes.

    Tolerated shapes (covering every variant the prior callsites handled):
      - canonical:    {"profiles": {"anthropic:api": {"type": "api_key", "key": "sk-..."}}}
      - flat dict:    {"anthropic": {"key": "sk-..."}} or {"anthropic_api_key": "sk-..."}
      - ultra-flat:   {"anthropic": "sk-..."}
      - list:         [{"key": "anthropic", "value": "sk-..."}, ...]

    Prefers ``api_key``-typed profiles over ``token`` profiles (OAuth/session
    tokens are not valid for direct API calls).
    """
    # List shape: [{"key"|"name": "anthropic", "value"|"api_key": "sk-..."}]
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            label = (entry.get("key") or entry.get("name") or "").lower()
            if "anthropic" in label or "claude" in label:
                v = entry.get("value") or entry.get("api_key")
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return ""

    if not isinstance(raw, dict):
        return ""

    profiles = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else raw

    # Two passes: prefer api_key over token profiles
    for prefer_api_key in (True, False):
        for pid, pdata in profiles.items():
            pid_lower = str(pid).lower()
            # Ultra-flat: {"anthropic": "sk-ant-..."}
            if isinstance(pdata, str):
                if "anthropic" in pid_lower and pdata.strip():
                    return pdata.strip()
                continue
            if not isinstance(pdata, dict):
                continue
            if "anthropic" not in pid_lower:
                continue
            ptype = (pdata.get("type") or "").lower()
            if prefer_api_key and ptype and ptype != "api_key":
                continue
            for fld in ("api_key", "key", "token"):
                v = pdata.get(fld)
                if isinstance(v, str) and v.strip():
                    return v.strip()

    # Top-level fallbacks: ANTHROPIC_API_KEY / anthropic_api_key
    for fld in ("ANTHROPIC_API_KEY", "anthropic_api_key"):
        v = raw.get(fld)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


_extract_anthropic_key = extract_anthropic_key  # backcompat alias for prior underscore name


def _load_network_default() -> dict[str, Any]:
    """Load network.json from the standard location.

    Used as a fallback when callers don't have a network dict handy.
    Returns ``{}`` on any failure (caller's auth lookup will then degrade
    to env-var-only — same behaviour as the prior hardcoded callsites
    when their auth-profiles.json was unreadable).
    """
    for candidate in (
        "/Users/Shared/evolve/network.json",
        os.environ.get("EVOLVE_NETWORK_JSON", ""),
    ):
        if not candidate:
            continue
        try:
            return json.loads(Path(candidate).read_text())
        except Exception:
            continue
    return {}


# ── Migrated SQLite auth store (OC 2026.6.x) — primary-bot read fallback ───────
#
# OpenClaw 2026.6.x migrated each agent's ``auth-profiles.json`` into the
# per-agent SQLite store ``openclaw-agent.sqlite`` (table ``auth_profile_store``,
# row ``store_key='primary'``, column ``store_json`` carrying the SAME
# ``{"profiles": {...}}`` shape), renaming the JSON to
# ``auth-profiles.json.sqlite-import.<epoch_ms>.bak``. The primary-bot key
# readers below walk the legacy JSON candidates first, then fall back to the
# SQLite store — so an engine background call (judge, summarizer, classifier,
# spec extractor, help bot, …) keeps resolving the primary's key across the
# migration. Without this fallback every primary-bot reader returns ``""``
# pod-wide once the migration lands (the bug this closes; verified live
# 2026-06-22). The *target-bot* read path (admin scanner/server, via the
# ``oc_store`` adapter) needs the same shim — landed separately; this module
# owns only the primary-bot/engine reader path.


def primary_bot_agent_sqlite_path(network: dict[str, Any]) -> Path | None:
    """Path to the primary bot's per-agent OpenClaw SQLite auth store, or None.

    The migrated ``auth-profiles.json`` now lives in this DB (see the module
    note above). Returns ``None`` when the primary bot / its home cannot be
    resolved — the readers then degrade to env-var-only, exactly as they did
    when the legacy JSON was unreadable.
    """
    home = primary_bot_home(network)
    if not home:
        return None
    return home / ".openclaw" / "agents" / "main" / "agent" / "openclaw-agent.sqlite"


def read_agent_auth_store_json(db_path: Path) -> dict[str, Any] | None:
    """Read OpenClaw's migrated auth profiles from a per-agent SQLite store.

    The migrated profiles live in the ``auth_profile_store`` table under
    ``store_key='primary'``, whose ``store_json`` carries the *same*
    ``{"profiles": {...}}`` shape the JSON used — so the existing extractors
    (:func:`extract_anthropic_key`, :func:`_extract_keys_by_provider`) parse the
    returned dict unchanged.

    Opened **read-only** (no OpenClaw CLI exports the raw secret — ``openclaw
    infer model auth status`` reports auth *state*, never the key — so a
    read-only SQLite query is the supported recovery path). Two modes are tried
    in order so a *running* gateway is read correctly without ever taking a
    write lock:

      1. ``mode=ro`` (+ ``PRAGMA query_only``) — a normal read-only connection
         that DOES consult the WAL, so a row the gateway has committed but not
         yet checkpointed into the main DB file is still visible. (``immutable=1``
         skips the WAL entirely and would return "no such table" while the
         gateway holds the only copy of the row in an un-checkpointed WAL — the
         common live case, since the tiny auth DB never hits the auto-checkpoint
         threshold.)
      2. ``immutable=1`` — fallback for when the read-only open cannot
         initialise the WAL ``-shm`` sidecar (e.g. the gateway is down and the
         ``evolve`` user holds only a read ACL, so it cannot create ``-shm``).
         A clean last-connection close checkpoints the WAL into the main file,
         so by then the immutable read of the main file is current.

    Tolerant of OpenClaw schema drift: any error — missing file/table/column,
    malformed JSON, a non-dict payload — yields ``None``, never an exception.
    The DB is never written; the ``evolve`` user's ``.openclaw`` read ACL
    suffices. (The per-bot ``oc_store`` adapter adds a root ``sqlite3 -readonly``
    belt for the narrow gateway-crashed-mid-WAL + read-only-ACL case; the
    primary bot's gateway is the engine's own, so the two library modes cover
    it without a sudo grant here.)
    """
    try:
        if not db_path.exists():
            return None
    except OSError:
        return None
    import sqlite3
    base = db_path.as_uri()  # percent-encodes spaces / ? / # in the path
    row: tuple[Any, ...] | None = None
    for mode in ("mode=ro", "immutable=1"):
        try:
            conn = sqlite3.connect(f"{base}?{mode}", uri=True, timeout=2.0)
            try:
                conn.execute("PRAGMA query_only=ON")
                row = conn.execute(
                    "SELECT store_json FROM auth_profile_store WHERE store_key = ?",
                    ("primary",),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            # connect/-shm-init failure OR "no such table" on a drifted schema —
            # fall through to the next mode (and ultimately to None).
            continue
        except sqlite3.Error:
            return None
        break  # opened + queried cleanly; use this row (may legitimately be None)
    if not row or not isinstance(row[0], str):
        return None
    try:
        data = json.loads(row[0])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _extract_keys_by_provider(raw: Any) -> dict[str, str]:
    """Extract ``{provider: api_key}`` from one auth-profiles payload.

    Handles canonical (``{"profiles": {...}}``), flat, and ultra-flat shapes.
    Prefers ``api_key`` profiles over ``token`` profiles (the latter are
    OAuth/session tokens that don't authorise direct API calls). Pure dict walk
    — shared by every primary-bot auth source (the legacy JSON files and the
    migrated SQLite store) so both extract identically.
    """
    keys: dict[str, str] = {}
    if not isinstance(raw, dict):
        return keys
    profiles = raw.get("profiles") or raw
    if not isinstance(profiles, dict):
        return keys
    # Two passes: prefer api_key over token
    for prefer_api_key in (True, False):
        for pid, pdata in profiles.items():
            if isinstance(pdata, str):
                prov = str(pid).lower()
                key = pdata.strip()
            elif isinstance(pdata, dict):
                ptype = (pdata.get("type") or "api_key").lower()
                if prefer_api_key and ptype == "token":
                    continue
                prov = (pdata.get("provider") or pid).lower()
                candidate = (
                    pdata.get("api_key")
                    or pdata.get("key")
                    or pdata.get("token")
                    or pdata.get("value")
                    or ""
                )
                key = candidate.strip() if isinstance(candidate, str) else ""
            else:
                continue
            if prov and key and prov not in keys:
                keys[prov] = key
        if keys:
            break
    return keys


def _iter_primary_bot_auth_payloads(network: dict[str, Any]) -> Iterator[Any]:
    """Yield each primary-bot auth payload in precedence order.

    Legacy ``auth-profiles.json`` candidates first (pre-migration / older
    OpenClaw), then the migrated ``openclaw-agent.sqlite`` ``auth_profile_store``
    (OpenClaw 2026.6.x+). Each yielded payload is a parsed object ready for
    :func:`extract_anthropic_key` / :func:`_extract_keys_by_provider`. Sources
    that don't exist or won't parse are skipped — the iterator simply yields
    fewer payloads, never raises. Lazy: the SQLite store is only opened when no
    earlier JSON candidate satisfied the caller.
    """
    for path in primary_bot_auth_profile_paths(network):
        try:
            yield json.loads(path.read_text())
        except Exception:
            continue
    db = primary_bot_agent_sqlite_path(network)
    if db is not None:
        store = read_agent_auth_store_json(db)
        if store is not None:
            yield store


def read_primary_bot_keys_by_provider(
    network: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Return ``{provider: api_key}`` from the primary bot's auth store.

    Walks the legacy ``auth-profiles.json`` candidates, then the migrated
    SQLite store (:func:`_iter_primary_bot_auth_payloads`), returning the first
    source that yields keys. Handles canonical, flat, and ultra-flat shapes.
    Prefers ``api_key`` profiles over ``token`` profiles (the latter are
    OAuth/session tokens that don't authorise direct API calls).

    Used by the admin UI's help bot, which needs to know which providers
    have keys before choosing which tier3 candidate model to call.
    """
    if network is None:
        network = _load_network_default()
    for raw in _iter_primary_bot_auth_payloads(network):
        keys = _extract_keys_by_provider(raw)
        if keys:
            return keys
    return {}


def read_primary_bot_anthropic_key(network: dict[str, Any] | None = None) -> str:
    """Resolve an Anthropic API key for engine background calls.

    Resolution order:
      1. ``ANTHROPIC_API_KEY`` env var (operator override, also covers tests).
      2. The primary bot's legacy ``auth-profiles.json`` (candidate paths).
      3. The primary bot's migrated ``openclaw-agent.sqlite`` auth store
         (OpenClaw 2026.6.x+ — see :func:`read_agent_auth_store_json`).

    If ``network`` is None, network.json is loaded from the standard pod
    location. Pass it explicitly when the caller already has it loaded to
    avoid the disk read.

    Returns ``""`` if no key is found — callers must check before making a
    request and surface a clear "no key" error to the operator.
    """
    env = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env:
        return env
    if network is None:
        network = _load_network_default()
    for raw in _iter_primary_bot_auth_payloads(network):
        key = extract_anthropic_key(raw)
        if key:
            return key
    return ""


# ── Tier resolution helpers ───────────────────────────────────────────────────


def bot_tier_models(
    network: dict[str, Any], bot_id: str, tier: str
) -> list[str]:
    """Return the model chain for ``tier`` from ``bot_id``'s
    ``evolve-tiers.json``, or [] if none set.

    AI Optimization → Save Tiers writes via
    ``PUT /api/admin/config/<bot>/tiers`` → ``oc_full_config_set`` →
    ``oc_model.py config set`` → ``~/.openclaw/evolve-tiers.json``.
    That file is the single source of truth for which models live at
    each tier for a given bot.

    Prior to 2026-05-25 the read path looked at
    ``network.json::models.tier_assignments[<bot>][tier]`` — a parallel
    storage location that no user-facing flow wrote to. The admin Tier
    Resolution card displayed every tier as ``default`` even when AI
    Optimization had set non-default models (PR #1544 — the bug fix).
    The current implementation reads from where AI Optimization
    actually persists.

    Reads BOTH config shapes via :func:`resolve_tier_chain`: the new
    rungs/roles shape (tier3→fast→haiku-class rung, etc.) wins, with the
    legacy ``tiers.<tierN>`` shape as a fall-through. Prior to the
    2026-06-09 rungs/roles migration this function read ``data.get("tiers")``
    only, so it returned ``[]`` for every migrated file — that empty result is
    what made the admin Tier Resolution card render every bot's allocations as
    "erased" (the files were intact). The resolver fixes the read side; the
    write side (oc_model config set) normalizes mixed-shape files.

    Returns ``[]`` on any error (file missing, permission denied,
    malformed JSON, tier not present) — callers fall through to
    ``DEFAULT_TIERS``.
    """
    if not bot_id:
        return []
    path = _bot_evolve_tiers_path(network, bot_id)
    data, _ok = _read_bot_owned_json(path)
    if not isinstance(data, dict):
        data = {}
    # Keyed merge with the pod-base catalog (network.json::models) so a
    # pod-wide rung adoption is visible even though every bot carries its
    # own per-bot rungs (spec §Addendum A.4). The per-bot file overrides
    # by rung id; pod-only rungs surface through the merge.
    pod_models = (network or {}).get("models")
    merged = merge_model_catalog(pod_models, data)
    if not merged.get("rungs") and not merged.get("tiers"):
        return []
    return resolve_tier_chain(merged, tier)


def primary_bot_tier_models(
    network: dict[str, Any], tier: str
) -> list[str]:
    """Return the primary bot's model chain for ``tier`` — convenience
    wrapper around :func:`bot_tier_models`. See that function for
    storage details and behavior."""
    pid = primary_bot_id(network)
    if not pid:
        return []
    return bot_tier_models(network, pid, tier)


# Canonical role order for the all-roles resolution view (cheap → premium,
# judge last as the constrained role). The AI-Optimization page and
# ``evolve-admin models show`` both render in this order.
ROLE_ORDER: tuple[str, ...] = ("fast", "standard", "power", "max", "judge")

# The cost-ordered ladder roles, cheapest → premium. This — NOT the raw merged
# ``rungs`` array — is what the rank meter is derived from (spec Addendum 8 §D):
# the meter shows how the four user-selectable roles sit relative to each other
# in cost order, so a junk/duplicate/accumulated rung in the on-disk catalog
# must NOT inflate the step count. ``judge`` is off-ladder (provider-diversity,
# not strength) and gets no rank. This is the role order, not a model literal.
_LADDER_ROLES: tuple[str, ...] = ("fast", "standard", "power", "max")

# Canonical costClass cost order (cheapest first). Mirrors ``COST_CLASS_ORDER``
# in models.py — defined locally to keep primary_bot import-light (models.py
# imports primary_bot lazily; a top-level back-import would be circular). This
# is ordering data, not a provider/model literal.
_COST_CLASS_ORDER: tuple[str, ...] = ("low", "medium", "high", "premium")


def ladder_rank_view(catalog: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Derive the rank meter from the cost-ordered LADDER, robust to junk rungs.

    Spec Addendum 8 §D — the rank/rungCount the AI-Optimization meter renders
    are NOT the role's index in the raw merged ``rungs`` array (which can carry
    stray, duplicate, or accumulated rungs from a mis-keyed pod/bot merge and
    would then show ~9 non-monotonic steps). Instead:

      - ``rungCount`` = the number of DISTINCT rungs that the four ladder roles
        (fast < standard < power < max) actually point at, ordered by cost
        (4 by default).
      - ``rank`` = the role's 1-based position in that cost-ordered DISTINCT
        set (fast=1 … max=4). Two ladder roles pointing at the SAME rung share
        a rank; a role whose rung is missing from the catalog is dropped from
        the ladder (it has no resolvable cost position).

    Ordering key per rung: (cost-class rank, first array index) — costClass is
    the hard ordering dependency (spec design principle 3); the array index is
    a stable tiebreak for rungs sharing a costClass or carrying none. Stray
    rungs no ladder role points at never enter the set, so they cannot inflate
    the count. Returns ``{role: {"rank": int, "rungCount": int}}`` for each
    ladder role that resolves to a rung; judge is intentionally absent.
    """
    rungs = catalog.get("rungs")
    rungs = rungs if isinstance(rungs, list) else []

    # rung id → (costClass rank, first array index). First index wins on dupes.
    rung_sort_key: dict[str, tuple[int, int]] = {}
    for idx, r in enumerate(rungs):
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if not isinstance(rid, str) or not rid or rid in rung_sort_key:
            continue
        cost = r.get("costClass")
        cost_rank = (
            _COST_CLASS_ORDER.index(cost)
            if isinstance(cost, str) and cost in _COST_CLASS_ORDER
            else len(_COST_CLASS_ORDER)  # unknown/None costClass sorts last
        )
        rung_sort_key[rid] = (cost_rank, idx)

    # The rung each ladder role points at (skip roles whose rung isn't present).
    role_rung: dict[str, str] = {}
    for role in _LADDER_ROLES:
        rid = _resolve_role_to_rung(catalog, role)
        if isinstance(rid, str) and rid in rung_sort_key:
            role_rung[role] = rid

    # The DISTINCT set of rungs the ladder points at, cost-ordered.
    distinct_rungs = sorted(set(role_rung.values()), key=lambda rid: rung_sort_key[rid])
    rung_count = len(distinct_rungs)
    rank_of_rung = {rid: i + 1 for i, rid in enumerate(distinct_rungs)}

    return {
        role: {"rank": rank_of_rung[rid], "rungCount": rung_count}
        for role, rid in role_rung.items()
    }


def resolve_roles_with_provenance(
    network: dict[str, Any],
    bot_id: str | None = None,
    credentialed_providers: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve all five roles through the defaults ← pod ← bot merge, tagging
    each with the **winning layer** that supplied its rung.

    Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Addendum 2.3 — the AI
    Optimization page renders ALL FIVE roles unconditionally, each labeled with
    the layer (``default`` / ``pod`` / ``bot``) whose config decided it.

    Layering:
      - ``default`` — :data:`DEFAULT_MODEL_CATALOG` (code-shipped).
      - ``pod``     — ``network.json::models``.
      - ``bot``     — the bot's ``evolve-tiers.json`` (only when ``bot_id`` is
        given; for the pod engine view it is the primary bot).

    Returns ``{role: {rung, models, primary, layer, costClass}}`` for every
    role in :data:`ROLE_ORDER`. ``layer`` is the deepest layer whose presence
    changes the resolved rung+models for that role — bot wins over pod wins
    over default. A role always resolves (defaults map all five), so there is
    no "unconfigured" row.
    """
    pod_models = (network or {}).get("models")
    pod_models = pod_models if isinstance(pod_models, dict) else {}

    bot_doc: dict = {}
    if bot_id:
        try:
            path = _bot_evolve_tiers_path(network, bot_id)
            data, _ok = _read_bot_owned_json(path)
            if isinstance(data, dict):
                bot_doc = data
        except Exception:
            bot_doc = {}

    # Three progressively-deeper merged catalogs. Each uses the SAME keyed
    # merge as the loaders, so the resolved rung+models match what the gateway
    # would route. include_defaults folds DEFAULT_MODEL_CATALOG as the base.
    cat_default = merge_model_catalog({}, {})                  # defaults only
    cat_pod = merge_model_catalog(pod_models, {})              # defaults ← pod
    cat_bot = merge_model_catalog(pod_models, bot_doc)         # defaults ← pod ← bot

    def _resolve(cat: dict, role: str) -> tuple[str | None, list[str]]:
        rung = _resolve_role_to_rung(cat, role)
        models = _rung_models(cat, rung) if rung else []
        return rung, models

    # Availability-aware resolution (spec §Addendum3.A). The bot-merged
    # catalog is the one the gateway would route from, so availability is
    # computed against it. credentialed_providers is None when the caller
    # can't see the bot's credentials (fail-open: every role renders
    # available rather than spuriously grayed).
    avail = available_providers_for_resolution(cat_bot, credentialed_providers)

    # Derived rank meter from the cost-ordered LADDER, not the raw rungs array
    # (spec Addendum 8 §D). rungCount = the number of distinct rungs the four
    # ladder roles point at (4 by default); rank = the role's position in that
    # cost-ordered set. Stray/duplicate/accumulated rungs in the on-disk catalog
    # (e.g. a mis-keyed pod merge that left 9 rungs) do NOT inflate the meter —
    # only rungs a ladder role actually points at count. judge is off-ladder.
    ladder_view = ladder_rank_view(cat_bot)
    # One canonical ladder rung count for every row (the meter draws this many
    # steps); judge reports it too for a consistent surface even though it has
    # no meter. Any ladder entry carries the same count; fall back to 0.
    ladder_rung_count = next(
        (v["rungCount"] for v in ladder_view.values()), 0
    )

    out: dict[str, dict[str, Any]] = {}
    for role in ROLE_ORDER:
        d_rung, d_models = _resolve(cat_default, role)
        p_rung, p_models = _resolve(cat_pod, role)
        b_rung, b_models = _resolve(cat_bot, role)

        # Winning layer = deepest layer that changed rung+models. Bot first.
        if (b_rung, b_models) != (p_rung, p_models):
            layer, rung, models = "bot", b_rung, b_models
        elif (p_rung, p_models) != (d_rung, d_models):
            layer, rung, models = "pod", p_rung, p_models
        else:
            layer, rung, models = "default", b_rung, b_models

        # Recommended-for-this-tier set (spec §Addendum 6, Phase 9 §v1 item 1 —
        # the SOFT "suggested" signal). This is the pod-merged default cluster
        # (defaults ← pod) for the role — exactly what "Reset to pod defaults"
        # would put here — provider-matched to the bot's credentialed providers
        # so we never recommend a model the bot can't actually run. It is
        # independent of the bot's CURRENT (possibly-customized) ``models``; the
        # tier-editor picker stars these and greys the ones already present.
        # When credentials are unknown (presentation reader), keep the full
        # cluster (fail-open — same convention as availability resolution).
        if credentialed_providers is None:
            default_models = list(p_models)
        else:
            default_models = [m for m in p_models if provider_of(m) in avail]

        cost_class = None
        for r in (cat_bot.get("rungs") or []):
            if isinstance(r, dict) and r.get("id") == rung:
                cost_class = r.get("costClass")
                break
        # Backfill a blank costClass so the cost chip is never empty even when
        # the on-disk catalog has a stray ``*-default`` rung with no costClass
        # (spec Addendum 8 §D acceptance — populated chips even pre-migration).
        # Prefer the canonical cost class for the role's canonical rung; this is
        # presentation derivation, the underlying config is fixed by the
        # migration / write-path canonicalization.
        if not cost_class:
            cost_class = _CANONICAL_RUNG_COST_CLASS.get(
                _DEFAULT_ROLE_TO_RUNG.get(role, "")
            )

        # Availability + degradation. judge keeps its diversity machinery;
        # diversity is a PREFERENCE — judge is "available" when the rung resolves
        # to any credentialed model, cross-vendor preferred but same-vendor still
        # routes (flagged via advisoryReason below).
        if role == "judge":
            avail_info = _resolve_judge_availability(cat_bot, avail)
        else:
            avail_info = resolve_role_with_availability(cat_bot, role, avail)

        # Rank (1-based, cost-ordered) for the meter; None for judge
        # (off-ladder) and for any ladder role whose rung isn't in the catalog.
        rank = ladder_view.get(role, {}).get("rank")

        # Inert (non-credentialed) providers in the rung — used by the UI to
        # distinguish a soft-dormant role (resolves fine but carries inert
        # fallbacks) from a hard-break (no credentialed model at all).
        # Spec §Addendum 10 §C: "dormant" providers are those present in the
        # rung's model list but NOT in the credentialed ∩ llm-capable set.
        # When credentials are unknown, report [] (fail-open — no false alarms).
        if credentialed_providers is None:
            inert_providers: list[str] = []
        else:
            rung_provs: set[str] = {p for m in models for p in (provider_of(m),) if p}
            inert_providers = sorted(rung_provs - avail)

        out[role] = {
            "rung": rung,
            "models": models,
            # Recommended cluster for this tier (pod-merged default, credential-
            # matched) — the picker's soft "suggested" set (spec §Addendum 6).
            "defaultModels": default_models,
            "primary": models[0] if models else None,
            "layer": layer,
            "costClass": cost_class,
            # Availability surface (spec §Addendum3.A.3): never hidden, grayed
            # with a computed reason when unavailable.
            "available": avail_info["model"] is not None,
            "resolvedModel": avail_info["model"],
            "degraded": avail_info["degraded"],
            "resolvedRole": avail_info["resolved_role"],
            "unavailableReason": avail_info["reason"] if avail_info["model"] is None else None,
            "degradeReason": avail_info["reason"] if avail_info["degraded"] else None,
            # Soft advisory (judge same-vendor-as-standard): routes fine, so it is
            # NOT an unavailableReason — surfaced separately so the tier editor
            # renders an amber nudge, not the red "won't route" band. The doubled
            # provider rides along for the copy (derived data, no literal).
            "advisoryReason": (
                avail_info["reason"]
                if avail_info["model"] is not None
                and avail_info["reason"] == "same_vendor_as_standard"
                else None
            ),
            "advisoryProvider": avail_info.get("advisory_provider"),
            "providers": avail_info["providers"],
            # Rank presentation (spec §Addendum3.C): derived ascending rank +
            # the rung count so the UI can render a filled-steps meter.
            "rank": rank,
            "rungCount": ladder_rung_count,
            # Spec §Addendum 10 §C — soft-dormant vs hard-break distinction.
            # Non-empty when credentialed_providers is known AND the rung has
            # models from providers the bot lacks keys for.  Empty list when all
            # rung providers are credentialed, or when credentials are unknown.
            "inertProviders": inert_providers,
        }
    return out


def read_bot_tiers_doc(network: dict[str, Any], bot_id: str) -> dict:
    """Return the RAW per-bot ``evolve-tiers.json`` doc (never the merged view).

    Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Addendum 5 — the
    per-bot Use-defaults/Custom toggle decides its state from the bot's OWN
    config, not the merged resolution (the merge always carries values from the
    code/pod defaults, so it can never say "this bot defines nothing").

    Returns ``{}`` when the file is absent or unreadable — a bot that has never
    saved its own tiers is "use pod defaults", which is exactly the empty-doc
    case. The caller inspects ``rungs`` presence (see :func:`bot_has_custom_tiers`).
    """
    try:
        path = _bot_evolve_tiers_path(network, bot_id)
        data, _ok = _read_bot_owned_json(path)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def bot_has_custom_tiers(network: dict[str, Any], bot_id: str) -> bool:
    """True when the bot defines its OWN rungs (Custom), False when it inherits.

    Spec §Addendum 5 "Strict all-or-nothing": a bot is either Custom (its
    ``evolve-tiers.json`` carries a non-empty ``rungs`` array) or Use-pod-
    defaults (no ``rungs``). Decided from the RAW per-bot doc, never the merged
    view — see :func:`read_bot_tiers_doc`.
    """
    doc = read_bot_tiers_doc(network, bot_id)
    rungs = doc.get("rungs")
    return isinstance(rungs, list) and bool(rungs)


def materialize_bot_tier_override(
    network: dict[str, Any],
    bot_id: str,
) -> dict[str, Any]:
    """Build the per-bot rungs/roles override that seeds a bot's first Customize.

    Spec §Addendum 5: "Customize this bot" materializes a per-bot override
    *seeded from the bot's current resolved roles*, so nothing changes on click
    except the bot becomes editable. The seed is the fully-merged catalog
    (``DEFAULT_MODEL_CATALOG ← pod ← bot``) — the exact rungs/roles the gateway
    would route from today — written wholesale into the bot's
    ``evolve-tiers.json``. From then on the bot carries its OWN ``rungs`` (so
    :func:`bot_has_custom_tiers` reads True) and the merge resolves entirely
    from the bot layer.

    Returns ``{"rungs": [...], "roles": {...}, "roleCaps": {...}}`` ready to
    pass to ``json_full_config_set`` as the ``rungs``/``roles``/``roleCaps``
    update keys. No provider/model literals here — every value flows from the
    merge, which folds the code-shipped catalog.
    """
    pod_models = (network or {}).get("models")
    pod_models = pod_models if isinstance(pod_models, dict) else {}
    bot_doc = read_bot_tiers_doc(network, bot_id)
    merged = merge_model_catalog(pod_models, bot_doc)
    out: dict[str, Any] = {
        "rungs": merged.get("rungs") or [],
        "roles": merged.get("roles") or {},
    }
    role_caps = merged.get("roleCaps")
    if isinstance(role_caps, dict) and role_caps:
        out["roleCaps"] = role_caps
    return out


# ── POD-default editor support (spec §Addendum 5) ─────────────────────────────
#
# The POD-tab "Default tier definitions" editor edits the pod layer
# (``network.json::models.rungs/roles``). The three helpers below back it:
#   - ``pod_default_catalog_view`` — the effective default cluster per role,
#     plus a per-role ``layer`` chip ("default" when the cluster comes from the
#     code catalog, "pod" once an operator has materialized a pod override).
#   - ``validate_pod_default_catalog`` — the judge provider-diversity invariant,
#     checked BEFORE writing a pod override (a pod default whose judge rung has
#     no provider distinct from standard's would make judge unresolvable on
#     every credential-complete bot).
#   - ``bot_redundant_vs_default`` — the "redundant — reset to pod defaults?"
#     advisory: a Custom bot whose own config resolves equal-after-merge to the
#     pod default (i.e. clearing it changes nothing) is flagged.


def pod_default_catalog_view(
    network: dict[str, Any],
    credentialed_providers: set[str] | None = None,
) -> dict[str, Any]:
    """Effective POD-default rungs/roles + per-role provenance layer.

    Returns ``{"rungs": [...], "roles": {...}, "roleCaps": {...},
    "roleLayers": {role: "default"|"pod"},
    "roleDefaultModels": {role: [model, ...]}, "podConfigured": bool}``.

    ``rungs``/``roles``/``roleCaps`` are the ``defaults ← pod`` merge — the
    exact default cluster a Use-defaults bot inherits. Each rung's ``models`` is
    credential-matched to ``credentialed_providers`` (show only providers the
    pod holds a key for), so the default card is credential-honest like the
    picker; every rung id is kept even when its filtered ``models`` is empty
    (role→rung resolution + the editor's zero-credential empty state depend on
    it). ``credentialed_providers is None`` keeps the full cluster (fail-open).
    The runtime gateway credential-matches per bot independently, so this is a
    display filter only — the code catalog and resolution are untouched.
    ``roleLayers`` tags each role with the deepest layer
    that decided its rung+models: ``pod`` once ``network.json::models`` carries
    an override for that role's rung, else ``default`` (code catalog). The
    editor shows ``Evolve default`` until an edit materializes the pod layer,
    then ``pod`` — no provider/model literals here, the labels are derived from
    whether the pod layer changed the resolved cluster.

    ``roleDefaultModels`` is the per-role RECOMMENDED set for the POD editor's
    "Add a model" picker (spec §Addendum 6, Phase 9 §v1 item 1 — the SOFT
    "suggested" signal). The POD editor edits the POD's tier definitions, so the
    layer ABOVE it — what "Reset to Evolve defaults" would put there — is the
    code ``DEFAULT_MODEL_CATALOG`` cluster (``cat_default``), NOT the
    ``defaults ← pod`` cluster (that is what's already in the editor, so
    recommending it would be circular). It is credential-matched to
    ``credentialed_providers`` (the pod's credentialed set) so the picker never
    stars a model no bot in the pod can run; ``None`` keeps the full cluster
    (fail-open — same convention as availability resolution).

    ``podConfigured`` is True iff ``network.json::models`` carries a non-empty
    ``rungs`` array (an actual pod override exists) — drives the editor's
    "Reset to Evolve defaults" affordance (only meaningful when there is a pod
    layer to clear).
    """
    pod_models = (network or {}).get("models")
    pod_models = pod_models if isinstance(pod_models, dict) else {}

    cat_default = merge_model_catalog({}, {})          # defaults only
    cat_pod = merge_model_catalog(pod_models, {})      # defaults ← pod

    # Provider set the code-default recommendation may star: credentialed ∩
    # llm-capable (against the code catalog — that's the layer being
    # recommended). None → all llm-capable (fail-open for a reader that can't
    # see credentials), mirroring resolve_roles_with_provenance.
    avail = available_providers_for_resolution(cat_default, credentialed_providers)

    def _resolve(cat: dict, role: str) -> tuple[str | None, list[str]]:
        rung = _resolve_role_to_rung(cat, role)
        models = _rung_models(cat, rung) if rung else []
        return rung, models

    role_layers: dict[str, str] = {}
    role_default_models: dict[str, list[str]] = {}
    for role in ROLE_ORDER:
        d_rung, d_models = _resolve(cat_default, role)
        p_rung, p_models = _resolve(cat_pod, role)
        role_layers[role] = "pod" if (p_rung, p_models) != (d_rung, d_models) else "default"
        # Recommended-for-this-tier set = the CODE default cluster (one layer
        # above the pod), credential-matched. No model literals — providers are
        # derived from the model ids via provider_of.
        if credentialed_providers is None:
            role_default_models[role] = list(d_models)
        else:
            role_default_models[role] = [m for m in d_models if provider_of(m) in avail]

    pod_rungs = pod_models.get("rungs")
    pod_configured = isinstance(pod_rungs, list) and bool(pod_rungs)

    # Credential-match the DISPLAYED rung cluster too, so the default card is
    # credential-honest like the picker (above): show only credentialed-provider
    # models per tier. The runtime gateway already credential-matches per bot, so
    # this is a pure operator-facing display filter — DEFAULT_MODEL_CATALOG and
    # the gateway resolution are unchanged. Constraints:
    #   • Filter each rung's models[] to providers in `avail` (the credentialed ∩
    #     llm-capable set computed above), deriving provider via provider_of —
    #     never a literal — exactly as roleDefaultModels does.
    #   • KEEP every rung id present even when its filtered models[] is empty:
    #     role→rung resolution and the editor depend on the rung existing, and an
    #     empty models[] is precisely what drives the JS zero-credential empty
    #     state.
    #   • `credentialed_providers is None` → fail-open, keep the full cluster
    #     (unchanged behavior for readers that can't see credentials — same
    #     convention as roleDefaultModels / availability resolution above).
    # Provenance (roleLayers) is computed on the UNFILTERED merge above, so this
    # filter never changes which layer decided a role.
    out_rungs: list[Any] = cat_pod.get("rungs") or []
    if credentialed_providers is not None:
        filtered_rungs: list[Any] = []
        for rung in out_rungs:
            if isinstance(rung, dict) and isinstance(rung.get("models"), list):
                kept = [m for m in rung["models"] if provider_of(m) in avail]
                filtered_rungs.append({**rung, "models": kept})
            else:
                filtered_rungs.append(rung)
        out_rungs = filtered_rungs

    out: dict[str, Any] = {
        "rungs": out_rungs,
        "roles": cat_pod.get("roles") or {},
        "roleLayers": role_layers,
        # Per-role recommended set for the picker's soft "suggested" signal —
        # the code-default cluster (the "Reset to Evolve defaults" target),
        # credential-matched (spec §Addendum 6).
        "roleDefaultModels": role_default_models,
        "podConfigured": pod_configured,
    }
    role_caps = cat_pod.get("roleCaps")
    if isinstance(role_caps, dict) and role_caps:
        out["roleCaps"] = role_caps
    return out


def validate_pod_default_catalog(catalog: dict[str, Any]) -> str | None:
    """Validate a candidate POD-default catalog before it is written.

    Returns an error string when the candidate is invalid, else ``None``.

    Checks (spec §Addendum 5 — "Validate the judge provider-diversity
    invariant before write"):
      1. ``rungs`` is a non-empty list of ``{id, models[]}`` clusters.
      2. Every ladder role (fast/standard/power/max) resolves to a non-empty
         rung cluster.
      3. **Judge provider-diversity (pod-default best-practice), only when
         SATISFIABLE:** the judge rung must contain at least one model whose
         provider differs from standard's resolved provider — but ONLY when the
         candidate spans ≥2 providers across its rungs. A single-provider
         candidate cannot satisfy cross-vendor diversity, and at runtime
         :func:`_resolve_judge_availability` routes such a judge with a soft
         ``same_vendor_as_standard`` advisory rather than failing (#3040). Once
         the default view is credential-matched to a single provider (e.g. an
         anthropic-only pod), an operator edit+save legitimately sends a
         single-provider candidate; enforcing the hard check there would
         contradict the runtime's soft stance, so it is skipped. The judge rung
         must still resolve to at least one model (it routes like any role).

    No provider literals: providers are derived from model ids via
    :func:`provider_of`, mirroring the resolver's own diversity logic.
    """
    if not isinstance(catalog, dict):
        return "catalog must be an object"
    rungs = catalog.get("rungs")
    if not isinstance(rungs, list) or not rungs:
        return "rungs must be a non-empty list"
    for rung in rungs:
        if not isinstance(rung, dict) or not isinstance(rung.get("id"), str) or not rung["id"]:
            return "every rung needs a non-empty string id"
        models = rung.get("models")
        if not isinstance(models, list):
            return f"rung {rung.get('id')!r} needs a models list"

    # Ladder roles must each resolve to a non-empty cluster.
    for role in ("fast", "standard", "power", "max"):
        rung_id = _resolve_role_to_rung(catalog, role)
        if not rung_id or not _rung_models(catalog, rung_id):
            return f"role {role!r} resolves to no model — add models to its rung"

    # Judge must resolve to at least one model (it routes like any role).
    judge_rung = _resolve_role_to_rung(catalog, "judge")
    judge_models = _rung_models(catalog, judge_rung) if judge_rung else []
    if not judge_models:
        return "judge resolves to no model — add models to its rung"

    # Judge provider-diversity (cross-vendor best-practice) is only ENFORCEABLE
    # when the candidate actually spans ≥2 providers across its rungs. A
    # single-provider candidate (e.g. a credential-matched anthropic-only pod)
    # cannot satisfy it; the runtime routes such a judge with the soft
    # ``same_vendor_as_standard`` advisory (#3040), so rejecting it at write time
    # would contradict that stance. Providers derived from model ids via
    # provider_of (no literals).
    candidate_providers = {
        p
        for rung in rungs
        if isinstance(rung, dict)
        for m in (rung.get("models") or [])
        for p in (provider_of(m),)
        if p
    }
    if len(candidate_providers) >= 2:
        std_rung = _resolve_role_to_rung(catalog, "standard")
        std_models = _rung_models(catalog, std_rung) if std_rung else []
        std_provider = provider_of(std_models[0]) if std_models else None
        if not any(provider_of(m) and provider_of(m) != std_provider for m in judge_models):
            return (
                "judge provider-diversity unsatisfiable — the judge rung must "
                "include a model from a provider other than Standard's"
            )
    return None


def bot_redundant_vs_default(network: dict[str, Any], bot_id: str) -> bool:
    """True when a Custom bot's tiers resolve equal-after-merge to the pod default.

    Spec §Addendum 5 "Migration / cleanup": a bot whose explicit per-bot config
    is equal-after-merge to the pod default is "redundant — reset to defaults?".
    Cheap, derived, NO auto-action (operator-owned).

    Computed by comparing the bot's merged resolution WITH its per-bot override
    against the resolution WITHOUT it (i.e. clearing the bot's rungs/roles): if
    the resolved rung+models for every role are identical, clearing the override
    changes nothing, so the override is redundant.

    A bot that is already Use-defaults (no per-bot rungs) is NOT flagged — there
    is nothing to reset. Returns False on any read error (fail-safe: never nag
    about a bot whose config could not be read).
    """
    try:
        if not bot_has_custom_tiers(network, bot_id):
            return False
        pod_models = (network or {}).get("models")
        pod_models = pod_models if isinstance(pod_models, dict) else {}
        bot_doc = read_bot_tiers_doc(network, bot_id)
        cat_with = merge_model_catalog(pod_models, bot_doc)   # defaults ← pod ← bot
        cat_without = merge_model_catalog(pod_models, {})     # defaults ← pod
        for role in ROLE_ORDER:
            rung_w = _resolve_role_to_rung(cat_with, role)
            rung_wo = _resolve_role_to_rung(cat_without, role)
            models_w = _rung_models(cat_with, rung_w) if rung_w else []
            models_wo = _rung_models(cat_without, rung_wo) if rung_wo else []
            if (rung_w, models_w) != (rung_wo, models_wo):
                return False
        return True
    except Exception:
        return False


def _reorder_models_by_preference(
    models: list[str],
    provider_order: list[str],
    credentialed: set[str] | None,
) -> list[str]:
    """Reorder a rung's ``models[]`` so preferred providers come first.

    The fallback chain of a rung is its ``models`` order (the resolver walks it
    head-to-tail). The easy-setup wizard sorts that chain by the operator's
    ranked ``provider_order``: every model whose provider sits earlier in the
    preference list sorts ahead of one that sits later; providers absent from
    the preference list keep their original relative order and trail the ranked
    ones. When ``credentialed`` is given, models from providers the pod holds no
    key for are dropped (a tier with no credentialed model becomes empty).

    Pure string/data operation — provider is derived from the model id via
    :func:`provider_of`, never compared to a literal. The preference list is the
    only provider-name input and it is operator-supplied data.
    """
    pref_rank = {p.lower(): i for i, p in enumerate(provider_order)}
    fallback = len(pref_rank)
    kept: list[tuple[int, int, str]] = []
    for original_index, model in enumerate(models):
        if not isinstance(model, str) or not model:
            continue
        prov = provider_of(model)
        if credentialed is not None and (prov is None or prov not in credentialed):
            continue
        rank = pref_rank.get(prov, fallback) if prov else fallback
        kept.append((rank, original_index, model))
    # Stable sort by (preference rank, original position) — preserves the
    # catalog's intra-provider order and the relative order of unranked tails.
    kept.sort(key=lambda t: (t[0], t[1]))
    return [model for _, _, model in kept]


def compute_easy_setup_catalog(
    base_catalog: dict[str, Any],
    provider_order: list[str],
    credentialed_providers: set[str] | None = None,
) -> dict[str, Any]:
    """Easy-setup wizard compute: sensible defaults, ordered by preference.

    Spec §Addendum 6 #2 — the 90%-path wizard. Takes each role's *default*
    rung cluster (from ``base_catalog`` — the ``DEFAULT_MODEL_CATALOG`` /
    ``pod_default_catalog_view`` shape) and reorders that cluster's ``models[]``
    by the operator's ranked ``provider_order`` so the resolver's fallback chain
    leads with the preferred provider's model for the tier, then the rest in
    preference order. Providers absent from a tier's cluster (e.g. no frontier
    model in ``max``) are simply not present in that tier — the wizard never
    invents models, it only reorders what the blessed catalog already ships.

    Models are filtered to ``credentialed_providers`` when given, so a tier with
    no credentialed model resolves empty rather than offering an unreachable id.

    ``roles`` / ``roleCaps`` are carried through unchanged — judge keeps its
    ``{"rung": ..., "provider": "not-standard"}`` diversity form; the reorder
    only touches each rung's ``models`` list. The result is a full
    rungs/roles/roleCaps catalog ready for ``validate_pod_default_catalog`` and
    the existing safe-write paths.

    NO provider/model literals in this logic: every value flows from
    ``base_catalog`` (whose only literal home is ``DEFAULT_MODEL_CATALOG``) and
    the operator-supplied ``provider_order`` data.
    """
    base = base_catalog if isinstance(base_catalog, dict) else {}
    out_rungs: list[dict[str, Any]] = []
    for rung in (base.get("rungs") or []):
        if not isinstance(rung, dict):
            continue
        new_rung = dict(rung)
        models = rung.get("models")
        models = models if isinstance(models, list) else []
        new_rung["models"] = _reorder_models_by_preference(
            models, provider_order or [], credentialed_providers,
        )
        out_rungs.append(new_rung)

    roles = base.get("roles")
    out: dict[str, Any] = {
        "rungs": out_rungs,
        "roles": dict(roles) if isinstance(roles, dict) else {},
    }
    role_caps = base.get("roleCaps")
    if isinstance(role_caps, dict) and role_caps:
        out["roleCaps"] = dict(role_caps)
    return out


def _resolve_judge_availability(
    catalog: dict[str, Any], available_providers: set[str] | None
) -> dict[str, Any]:
    """Availability of the judge role under its provider-diversity *preference*.

    Provider diversity for judge is a RECOMMENDATION, not a hard routing
    constraint (operator-locked 2026-06-19): a cross-vendor judge is the ideal,
    but judge must still route when only a same-vendor model is available. The
    resolution is a two-pass preference ladder over the judge rung:

      - Pass 1 (ideal): the first model whose provider is available AND differs
        from standard's resolved provider → ``reason=None`` (cross-vendor).
      - Pass 2 (soft fallback): if none, the first model whose provider is
        available even when it equals standard's → ROUTES with
        ``reason="same_vendor_as_standard"`` (a soft advisory, NOT a failure;
        ``model`` is non-None). ``advisory_provider`` carries the doubled-up
        provider for the UI nudge.
      - Only when NO model in the rung has an available provider at all →
        ``{model: None, reason: "uncredentialed"}`` (the true hard break — no
        credentialed key anywhere in the rung).
      - Empty rung → ``unconfigured``.

    ``available_providers is None`` (presentation reader, credentials unknown)
    fails open: every model's provider counts as available, so Pass 1 still
    prefers a non-standard model and Pass 2 falls back to a same-standard model
    with the soft reason — it never returns None for a populated rung.

    judge is OFF the degradation ladder (never falls through to power/standard),
    mirroring its boot-warn / fall-through precedent.
    """
    rung_id = _resolve_role_to_rung(catalog, "judge")
    models = _rung_models(catalog, rung_id) if rung_id else []
    rung_providers = sorted({p for p in (provider_of(m) for m in models) if p})
    if not models:
        return {"model": None, "resolved_role": None, "degraded": False,
                "reason": "unconfigured", "providers": rung_providers,
                "advisory_provider": None}

    std = resolve_role_with_availability(catalog, "standard", available_providers)
    std_provider = provider_of(std.get("model"))

    def _avail(mp: str | None) -> bool:
        return bool(mp) and (available_providers is None or mp in available_providers)

    # Pass 1 — ideal cross-vendor: an available provider OTHER than standard's.
    for m in models:
        mp = provider_of(m)
        if _avail(mp) and mp != std_provider:
            return {"model": m, "resolved_role": "judge", "degraded": False,
                    "reason": None, "providers": rung_providers,
                    "advisory_provider": None}
    # Pass 2 — soft fallback: an available provider even if it equals standard's.
    # Diversity is a recommendation: judge still ROUTES, flagged for the nudge.
    for m in models:
        mp = provider_of(m)
        if _avail(mp):
            return {"model": m, "resolved_role": "judge", "degraded": False,
                    "reason": "same_vendor_as_standard", "providers": rung_providers,
                    "advisory_provider": std_provider}
    # No model in the rung has an available provider — the genuine hard break.
    return {"model": None, "resolved_role": None, "degraded": False,
            "reason": "uncredentialed", "providers": rung_providers,
            "advisory_provider": None}


def _bot_evolve_tiers_path(network: dict[str, Any], bot_id: str) -> Path:
    """Resolve a bot's ``evolve-tiers.json`` path from its OS user.

    Mirrors the home-resolution in :func:`bot_tier_models` — the bot's OS
    account name (``network.json::bots[<id>].user``) is authoritative, not
    the bot_id (a bot_id is NOT always its account name; see the
    bot_id≠account memory note). Falls back to ``/Users/<user>`` when pwd
    lookup fails (e.g. a member bot whose account the engine can't resolve).
    """
    bot_cfg = (network.get("bots") or {}).get(bot_id) or {}
    user = bot_cfg.get("user") or bot_id
    try:
        import pwd
        home = Path(pwd.getpwnam(user).pw_dir)
    except Exception:
        home = Path(f"/Users/{user}")
    return home / ".openclaw" / "evolve-tiers.json"


def _read_bot_owned_json(path: Path) -> tuple[dict | None, bool]:
    """Read a bot-owned JSON file the engine has macOS ACL read on.

    Returns ``(data, ok)``:
      - ``({...}, True)``  — parsed successfully.
      - ``(None, True)``   — file simply does not exist (a bot that has never
        had tiers saved is "knows nothing", NOT an error — ``ok`` stays True).
      - ``(None, False)``  — the file exists but could not be read or parsed
        (PermissionError after the sudo fallback, OSError, malformed JSON).
        This is a DEGRADED read: the caller must NOT treat it as "knows
        nothing", or an unreadable file would silently empty the known set.

    The ``evolve`` user normally has ACL read on every bot's ``.openclaw/``
    (set by ``set_evolve_read_acl`` on deploy), so the plain read succeeds.
    The ``sudo /bin/cat`` fallback (sudoers grant, per CLAUDE.md File Access
    Pattern) covers bots not yet deployed through the ACL path. We never use
    ``sudo -u <bot>`` — the evolve user has no such grant.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None, True
    except (PermissionError, OSError):
        # Fall back to sudo /bin/cat (root read) before declaring degraded.
        try:
            import subprocess
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True,
            )
        except Exception:
            return None, False
        if r.returncode != 0 or not r.stdout:
            # A non-zero rc here is ambiguous (missing OR denied). Treat as
            # degraded — the safe posture is "we could not confirm", never
            # "this bot knows nothing".
            return None, False
        text = r.stdout
    try:
        data = json.loads(text)
    except ValueError:
        return None, False
    if not isinstance(data, dict):
        return None, False
    return data, True


# ── Canonical tier ↔ role ↔ rung mapping (shape resolution) ───────────────────
#
# Single source of truth on the Python read side for the rungs/roles shape
# introduced by spec-model-rungs-and-roles-2026-06-09. Mirrors
# TIER_TO_ROLE / TIER_TO_RUNG in migrate_model_roles.py and ModelRouter.ts —
# keep all three in sync.
#
# The migration shipped 2026-06-09 moved every bot's evolve-tiers.json from
# ``{tiers: {tierN: {models}}}`` to ``{rungs: [...], roles: {...}}``. A reader
# that only knows the legacy ``tiers`` key returns [] for every migrated file
# — that empty-on-unexpected-shape silent failure is exactly what made the
# admin UI render every bot's allocations as "erased". Route every read
# through :func:`resolve_tier_chain` so the new shape resolves first and the
# legacy shape stays a fall-through, not the only path.

TIER_TO_ROLE: dict[str, str] = {
    "tier3": "fast",
    "tier2": "standard",
    "tier1": "power",
    "tier0": "judge",
}


# ── DEFAULT_MODEL_CATALOG — Evolve's blessed model ladder, shipped in code ─────
#
# KEEP IN SYNC with ``DEFAULT_MODEL_CATALOG`` in
# packages/plugin/src/observer/ModelRouter.ts — the two must resolve a given
# (pod, bot) override pair to byte-identical merged catalogs. A reviewer traces
# parity rule-by-rule; the parity fixtures on both sides enforce it.
#
# Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Addendum 2 (2026-06-10):
# product capabilities ship as code defaults; proposals/config carry instance
# state. This is the BASE layer of the keyed merge:
#
#     code defaults (this) ← network.json (pod) ← evolve-tiers.json (bot)
#
# Max ships ARMED, not dormant — cost safety holds via pull-only routing, the
# per-role daily cap (roleCaps.max.maxPerDayPerBot), and the per-bot breaker.
#
# MODEL LAUNCH: update this catalog (and the TS mirror) at each release when the
# blessed ladder changes — new frontier SKU, new fast-band entry, a retired
# model. Discovery surfaces market drift when this lags (by design).
# provider-literal-allow-begin: catalog DATA (home #1 of the three-homes rule)
DEFAULT_MODEL_CATALOG: dict[str, Any] = {
    "rungs": [
        {
            "id": "haiku-class",
            "costClass": "low",
            # anthropic FIRST (blessed default primary); the openai/google/xai
            # entries are each provider's latest tier3-appropriate model (per
            # model_registry.RECOMMENDED) so easy-setup can populate every tier
            # with every credentialed provider's model (Addendum 7 item 13).
            "models": [
                "anthropic/claude-haiku-4-5",
                "openai/gpt-4.1-mini",
                "google/gemini-2.0-flash",
                "xai/grok-4-mini",
            ],
        },
        {
            "id": "sonnet-class",
            "costClass": "medium",
            "models": [
                "anthropic/claude-sonnet-4-6",
                "openai/gpt-4.1",
                "google/gemini-2.5-pro",
                "xai/grok-4",
            ],
        },
        {
            "id": "opus-class",
            "costClass": "high",
            "models": [
                "anthropic/claude-opus-4-8",
                "openai/gpt-4.1",
                "google/gemini-2.5-pro",
                "xai/grok-4",
            ],
        },
        {
            "id": "fable-class",
            "costClass": "premium",
            # max stays anthropic-only — no peer frontier SKU yet. Add openai/
            # google/xai here only when each ships a comparable max-tier model.
            "models": [
                "anthropic/claude-fable-5",
            ],
        },
    ],
    "roles": {
        "fast": "haiku-class",
        "standard": "sonnet-class",
        "power": "opus-class",
        "max": "fable-class",
        # judge is rung-constrained: sonnet-class, but diversity-constrained to
        # a provider other than the standard role's primary (sonnet-class holds
        # several non-anthropic providers so judge diversity is satisfiable from
        # defaults alone).
        "judge": {"rung": "sonnet-class", "provider": "not-standard"},
    },
    "roleCaps": {
        "power": {"maxPerDayPerBot": 10},
        "max": {"maxPerDayPerBot": 5},
    },
}
# provider-literal-allow-end


def default_model_catalog() -> dict[str, Any]:
    """Return a deep copy of :data:`DEFAULT_MODEL_CATALOG`.

    Callers must never mutate the shared module constant — the merge folds it
    as the base layer on every read. Deep-copies so a caller that mutates the
    returned dict can't corrupt the defaults for the next read.
    """
    return json.loads(json.dumps(DEFAULT_MODEL_CATALOG))

# Default role → rung slug, used only when a role is absent from the file's
# ``roles`` map but the rung it conventionally points at exists (a partially
# configured file). An explicit ``roles`` entry always wins.
_DEFAULT_ROLE_TO_RUNG: dict[str, str] = {
    "fast": "haiku-class",
    "standard": "sonnet-class",
    "power": "opus-class",
    "max": "fable-class",
    "judge": "sonnet-class",
}


def _rung_models(data: dict, rung_id: str) -> list[str]:
    """Return the models[] cluster for ``rung_id`` from a new-shape file."""
    for rung in (data.get("rungs") or []):
        if isinstance(rung, dict) and rung.get("id") == rung_id:
            models = rung.get("models")
            if isinstance(models, list):
                return [m for m in models if isinstance(m, str) and m]
    return []


def _resolve_role_to_rung(data: dict, role: str) -> str | None:
    """Resolve a role ID to its rung slug via the file's ``roles`` map.

    ``judge`` (and any future constrained role) may be a structured object
    ``{"rung": "...", "provider": "..."}`` rather than a bare slug — both
    forms resolve to the rung id. Falls back to the canonical default rung
    for the role when the file carries rungs but no explicit ``roles`` entry
    (a partially-migrated file), so a read still finds the right cluster.
    """
    roles = data.get("roles")
    if isinstance(roles, dict):
        entry = roles.get(role)
        if isinstance(entry, str) and entry:
            return entry
        if isinstance(entry, dict):
            rung = entry.get("rung")
            if isinstance(rung, str) and rung:
                return rung
    return _DEFAULT_ROLE_TO_RUNG.get(role)


# Canonical costClass per canonical rung id — the cost weight the cost-reporting
# layer keys on. Backfilled onto any rung that arrives without one (the
# pod-editor / easy-setup write path used to emit ``costClass: null``, leaving
# cost chips blank). Ordering data, not a provider/model literal.
_CANONICAL_RUNG_COST_CLASS: dict[str, str] = {
    "haiku-class": "low",
    "sonnet-class": "medium",
    "opus-class": "high",
    "fable-class": "premium",
}


def canonicalize_catalog_rung_ids(catalog: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a one-rung-per-role catalog onto CANONICAL rung ids + costClass.

    Spec Addendum 8 §D — the pod-default editor and easy-setup wizard used to
    write synthetic rung ids (``fast-default`` / ``standard-default`` / …) with
    no ``costClass``. Because the keyed merge dedupes rungs by ``id`` and these
    ids never matched the code-default ids (``haiku-class`` / ``sonnet-class`` /
    …), the pod rungs ACCUMULATED on top of the code defaults instead of
    OVERLAYING them — a Use-defaults bot ended up with ~9 rungs (4 dead code +
    5 active pod), the rank meter ran to ~9 non-monotonic steps, and the
    ``costClass: null`` rungs left cost chips blank.

    This canonicalizer makes a write OVERLAY the code default: for each role,
    its pointed-at rung is renamed to the canonical id for that role
    (``_DEFAULT_ROLE_TO_RUNG``) and its ``costClass`` is backfilled from
    ``_CANONICAL_RUNG_COST_CLASS``. ``judge`` shares ``standard``'s rung
    (``sonnet-class``) per the catalog design, so a separate ``judge-default``
    rung is folded into the standard rung's cluster (judge-only models appended
    as fallbacks) rather than producing a stray rung. Roles are rewritten to
    point at the canonical id (judge keeps its ``{rung, provider}`` shape).

    Idempotent: a catalog already on canonical ids with costClass set is
    returned structurally equal. Operates on a deep copy; never mutates the
    input. No provider/model literals — the role→rung and rung→costClass maps
    are catalog ordering data (the three-homes rule's home #1).
    """
    if not isinstance(catalog, dict):
        return catalog
    src = json.loads(json.dumps(catalog))  # deep copy, never mutate caller's
    src_rungs = src.get("rungs")
    src_rungs = src_rungs if isinstance(src_rungs, list) else []
    src_roles = src.get("roles")
    src_roles = src_roles if isinstance(src_roles, dict) else {}

    # Index the source rungs by id for cluster lookup.
    by_id: dict[str, dict] = {
        r["id"]: r for r in src_rungs
        if isinstance(r, dict) and isinstance(r.get("id"), str) and r.get("id")
    }

    # Safety: if the source defines NO roles at all, there is nothing to map a
    # rung onto — canonicalizing would emit empty rungs and wipe the catalog.
    # Leave such a file (rungs present, roles absent — a partially-edited /
    # hand-authored doc) entirely untouched. The pod editor / easy-setup always
    # write roles alongside rungs, so this only protects malformed inputs.
    if not any(
        isinstance(src_roles.get(role), (str, dict)) and src_roles.get(role)
        for role in ROLE_ORDER
    ):
        return catalog

    # For each role the source ACTUALLY DEFINES (a key in ``roles``), find the
    # rung it points at and emit it under the canonical id. A role the source
    # omits (e.g. a 3-tier partial Custom set with no ``max``) is NOT
    # fabricated — the keyed merge fills it from the pod/code default, so
    # minting an empty rung here would brick the merge for that role. judge
    # folds into standard's rung rather than minting a separate one.
    new_rungs: list[dict[str, Any]] = []
    new_rungs_by_canon: dict[str, dict] = {}
    new_roles: dict[str, Any] = {}

    def _models_of(rung_id: str | None) -> list[str]:
        r = by_id.get(rung_id) if rung_id else None
        models = r.get("models") if isinstance(r, dict) else None
        return [m for m in models if isinstance(m, str) and m] if isinstance(models, list) else []

    def _role_defined(role: str) -> bool:
        # The source explicitly speaks for this role iff its ``roles`` map names
        # it. (``_resolve_role_to_rung`` falls back to a default rung even for an
        # absent role, so we can't lean on it alone.)
        return isinstance(src_roles.get(role), (str, dict)) and bool(src_roles.get(role))

    for role in ("fast", "standard", "power", "max"):
        if not _role_defined(role):
            continue
        canon = _DEFAULT_ROLE_TO_RUNG[role]
        src_rung_id = _resolve_role_to_rung(src, role)
        models = _models_of(src_rung_id)
        existing = new_rungs_by_canon.get(canon)
        if existing is None:
            rung = {
                "id": canon,
                "costClass": _CANONICAL_RUNG_COST_CLASS.get(canon),
                "models": models,
            }
            new_rungs.append(rung)
            new_rungs_by_canon[canon] = rung
        else:
            # Two ladder roles share a canonical rung (unusual) — union models.
            for m in models:
                if m not in existing["models"]:
                    existing["models"].append(m)
        new_roles[role] = canon

    # judge → fold into the standard rung (sonnet-class) keeping the diversity
    # constraint. Only when the source defines judge; its models append as
    # fallbacks so a provider-diverse option survives even if it differed from
    # standard's cluster. If standard wasn't defined either, judge mints the
    # sonnet-class rung on its own (judge is the role that requires it).
    if _role_defined("judge"):
        judge_canon = _DEFAULT_ROLE_TO_RUNG["judge"]
        judge_src_rung = _resolve_role_to_rung(src, "judge")
        judge_models = _models_of(judge_src_rung)
        judge_rung = new_rungs_by_canon.get(judge_canon)
        if judge_rung is None:
            judge_rung = {
                "id": judge_canon,
                "costClass": _CANONICAL_RUNG_COST_CLASS.get(judge_canon),
                "models": list(judge_models),
            }
            new_rungs.append(judge_rung)
            new_rungs_by_canon[judge_canon] = judge_rung
        else:
            for m in judge_models:
                if m not in judge_rung["models"]:
                    judge_rung["models"].append(m)
        judge_entry = src_roles.get("judge")
        if isinstance(judge_entry, dict):
            new_roles["judge"] = {
                "rung": judge_canon,
                "provider": judge_entry.get("provider", "not-standard"),
            }
        else:
            new_roles["judge"] = {"rung": judge_canon, "provider": "not-standard"}

    out: dict[str, Any] = dict(src)
    out["rungs"] = new_rungs
    out["roles"] = new_roles
    return out


def canonicalize_and_validate_pod_catalog(
    catalog: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Canonicalize rung ids + backfill costClass, then validate. Returns (catalog, err).

    The single enforcement point for the pod-default write surfaces (the manual
    pod PUT, the easy-setup wizard, and the bot tier write all run a catalog
    through this before persisting). Canonicalizing BEFORE validate/write makes
    the saved catalog OVERLAY the code default (matching rung ids → replace)
    instead of accumulating synthetic ``*-default`` rungs (spec Addendum 8 §D):
    a client may still send ``fast-default``/etc, but the server normalizes it
    regardless. ``err`` is the validator message (or ``None`` when valid); the
    returned ``catalog`` is always the canonicalized form so the caller persists
    the normalized shape even on a non-fatal path.
    """
    catalog = canonicalize_catalog_rung_ids(catalog)
    return catalog, validate_pod_default_catalog(catalog)


def easy_setup_catalog_for(
    network: dict[str, Any],
    provider_order: list[str],
    credentialed_providers: set[str] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Compute the easy-setup catalog for a pod and normalize it. Returns (catalog, err).

    The base cluster is the effective DEFAULT a Use-defaults bot inherits
    (``pod_default_catalog_view`` — defaults ← pod), reordered by the operator's
    ``provider_order`` preference and filtered to ``credentialed_providers``.
    Judge keeps its diversity form; the result runs through
    ``canonicalize_and_validate_pod_catalog`` (same enforcement + judge
    provider-diversity gate as the manual pod write).
    """
    base = pod_default_catalog_view(network)
    catalog = compute_easy_setup_catalog(
        base, provider_order, credentialed_providers=credentialed_providers,
    )
    return canonicalize_and_validate_pod_catalog(catalog)


# ── Availability-aware resolution (spec §Addendum3.A) ─────────────────────────
#
# A role resolves to the FIRST model in its rung whose provider is credentialed
# AND LLM-capable. The credentialed set is discovered from the bot's
# auth-profiles (presence of a usable key, not provider NAME). The LLM-capable
# set is DERIVED from the catalog's rung clusters — the three-homes rule
# (§Addendum3.B): no provider literal in this logic. Availability is the set
# intersection ``providers(rung) ∩ (credentialed ∩ llm_capable)``.
#
# When a role's rung has NO credentialed+LLM-capable provider, the role
# degrades DOWN the ladder through the SAME chain + reason machinery as a cap
# hit. The three degradation reasons unify into one concept:
#
#     cap_exhausted   — the role's daily cap is spent (TS routing owns this)
#     uncredentialed  — no credentialed provider for the rung's models
#     unconfigured    — the rung is empty / role maps to nothing
#
# Mirrors ModelRouter.ts ``resolveRoleAvailability`` / ``providerOf`` /
# ``llmProvidersFromCatalog`` — keep the two in sync (parity fixture).

# Downward degradation ladder (spec §max semantics #6): max→power→standard→fast,
# fast/judge terminal. This is the AVAILABILITY chain — the cap-exhaustion path
# (``degradeRoleOnCap`` in ModelRouter.ts) terminates at standard, but this one
# continues standard→fast so an uncredentialed standard can still reach a
# cheaper credentialed rung. judge is OFF the ladder (provider-diversity
# constrained, never degrades through the power ladder). Mirrors
# ``_degradeRole`` in ModelRouter.ts (the cap-vs-availability split is
# documented at ModelRouter.ts:1271-1276).
DEGRADE_CHAIN: dict[str, str | None] = {
    "max": "power",
    "power": "standard",
    "standard": "fast",
    "fast": None,
    "judge": None,
}


def provider_of(model: str | None) -> str | None:
    """Provider prefix of a qualified model id ("anthropic/claude-..." →
    "anthropic"). Lower-cased; ``None`` when the id has no provider prefix.

    This is the ONE place that splits a model id — not a provider literal,
    a pure string operation (mirrors ``_providerOf`` in ModelRouter.ts).
    """
    if not isinstance(model, str) or not model:
        return None
    slash = model.find("/")
    return model[:slash].lower() if slash > 0 else None


def llm_providers_from_catalog(catalog: dict[str, Any]) -> set[str]:
    """Derive the LLM-capable provider set from the merged catalog's rung
    clusters (spec §Addendum3.B — the three-homes rule). A provider is
    "LLM-capable" iff it names at least one model in any rung's ``models``
    list. This is how availability filters out non-LLM auth-profile entries
    (brave, runway, …) WITHOUT a provider-name literal: the set is data-
    derived, so a new frontier provider is a one-line catalog-data edit.
    """
    out: set[str] = set()
    for rung in (catalog.get("rungs") or []):
        if not isinstance(rung, dict):
            continue
        for m in (rung.get("models") or []):
            p = provider_of(m) if isinstance(m, str) else None
            if p:
                out.add(p)
    return out


def available_providers_for_resolution(
    catalog: dict[str, Any], credentialed_providers: set[str] | None
) -> set[str]:
    """The provider set role resolution may pick from: credentialed ∩
    llm-capable. ``credentialed_providers`` is the SET discovered from
    auth-profiles (no names in logic — it arrives pre-computed). ``None``
    means "credential state unknown" → return ALL llm-capable providers so a
    reader that can't see credentials does NOT gray every role (fail-open for
    presentation; the routing hot path passes a concrete set).
    """
    llm_capable = llm_providers_from_catalog(catalog)
    if credentialed_providers is None:
        return llm_capable
    return {p for p in credentialed_providers if p in llm_capable}


def resolve_role_with_availability(
    catalog: dict[str, Any],
    role: str,
    available_providers: set[str] | None,
) -> dict[str, Any]:
    """Resolve ``role`` to a concrete model, degrading down the ladder when
    no provider in its rung is available, and tagging the outcome with a
    unified reason.

    ``available_providers`` is the credentialed ∩ llm-capable set
    (:func:`available_providers_for_resolution`). ``None`` short-circuits the
    availability check (presentation reader with unknown credentials) — the
    role resolves to its rung's first model with no degradation.

    Returns::

        {
          "role": <requested role>,
          "resolved_role": <role actually used after degradation, or None>,
          "model": <model id or None>,
          "degraded": <bool>,
          "reason": "cap_exhausted" | "uncredentialed" | "unconfigured" | None,
          "providers": <sorted list of the rung's provider set>,
        }

    Mirrors ModelRouter.ts ``resolveRoleAvailability``. judge is resolved
    by its own diversity machinery and never degrades through this ladder.
    """
    seen: set[str] = set()
    cur: str | None = role
    while cur and cur not in seen:
        seen.add(cur)
        rung_id = _resolve_role_to_rung(catalog, cur)
        models = _rung_models(catalog, rung_id) if rung_id else []
        rung_providers = sorted({p for p in (provider_of(m) for m in models) if p})

        if not models:
            # Nothing configured for this rung — degrade with `unconfigured`.
            nxt = DEGRADE_CHAIN.get(cur)
            if nxt is None:
                return {
                    "role": role, "resolved_role": None, "model": None,
                    "degraded": cur != role, "reason": "unconfigured",
                    "providers": rung_providers,
                }
            cur = nxt
            continue

        if available_providers is None:
            # Credential state unknown → resolve as configured, no degradation.
            return {
                "role": role, "resolved_role": cur, "model": models[0],
                "degraded": cur != role, "reason": None,
                "providers": rung_providers,
            }

        # Prefer the first model whose provider is available (credentialed +
        # llm-capable). Within-rung fallback is preferred over degradation.
        for m in models:
            if provider_of(m) in available_providers:
                return {
                    "role": role, "resolved_role": cur, "model": m,
                    "degraded": cur != role,
                    "reason": "uncredentialed" if cur != role else None,
                    "providers": rung_providers,
                }

        # No available provider in this rung → degrade with `uncredentialed`.
        nxt = DEGRADE_CHAIN.get(cur)
        if nxt is None:
            return {
                "role": role, "resolved_role": None, "model": None,
                "degraded": cur != role, "reason": "uncredentialed",
                "providers": rung_providers,
            }
        cur = nxt

    return {
        "role": role, "resolved_role": None, "model": None,
        "degraded": True, "reason": "uncredentialed", "providers": [],
    }


# Tier-severity classification (spec §Addendum 10 §C — hard-break vs dormant).
SEVERITY_HARD_BREAK = "hard_break"
SEVERITY_DORMANT = "dormant"
SEVERITY_OK = "ok"
# Soft advisory: a judge that ROUTES but on the same vendor as standard
# (provider diversity is a recommendation, not a requirement — 2026-06-19).
# Never a hard break; surfaced as an amber nudge, not the red "won't route" panel.
SEVERITY_ADVISORY = "advisory"


def classify_role_severity(
    catalog: dict[str, Any], role: str, available_providers: set[str] | None,
) -> dict[str, Any]:
    """Classify ONE role's credential health from the runtime resolution.

    Spec §Addendum 10 §C — distinguish a genuinely broken tier from an inert
    deep-chain fallback so the UI keeps attention on real routing failures:

      - ``hard_break`` — the role's entire chain resolves to NO credentialed
        model (:func:`resolve_role_with_availability` returns ``model: None`` /
        ``reason: uncredentialed``). A REAL routing failure: the role won't
        route. Example: judge needs a provider other than standard's but the bot
        only holds standard's key.
      - ``dormant`` — the role resolves to a credentialed model but its rung
        ALSO carries inert, uncredentialed fallback entries. These are skipped
        at runtime and even useful (they auto-activate if the operator adds that
        provider's key later — Addendum 10 §2); NEVER a break, surfaced quietly.
      - ``advisory`` — judge-only: it ROUTES, but on the same vendor as standard
        (``reason: same_vendor_as_standard``). Provider diversity is a
        recommendation, not a requirement (2026-06-19), so this is a soft amber
        nudge to add a cross-vendor model — NOT a hard break. ``advisory_provider``
        carries the doubled-up provider for the copy.
      - ``ok`` — resolves with no uncredentialed fallbacks.

    judge is dispatched through its provider-diversity resolver
    (:func:`_resolve_judge_availability`), matching
    :func:`resolve_roles_with_provenance`: the diversity preference, not the bare
    rung walk, decides judge availability.

    ``available_providers`` is the credentialed ∩ llm-capable set (see
    :func:`available_providers_for_resolution`). ``None`` = credential state
    unknown → fail-open: the role resolves as configured and is reported ``ok``
    (never spuriously flagged broken/dormant for a reader that can't see keys).

    Returns ``{role, severity, model, reason, providers, dormant_models}``
    (plus ``advisory_provider`` for the judge advisory case) where ``providers``
    is the resolved rung's provider set (the remedy targets) and
    ``dormant_models`` is the inert uncredentialed fallback ids in that rung.
    """
    if role == "judge":
        info = _resolve_judge_availability(catalog, available_providers)
    else:
        info = resolve_role_with_availability(catalog, role, available_providers)
    model = info.get("model")
    rung_providers = info.get("providers") or []
    reason = info.get("reason")
    if model is None:
        return {
            "role": role, "severity": SEVERITY_HARD_BREAK, "model": None,
            "reason": reason, "providers": rung_providers,
            "dormant_models": [],
        }
    # Resolves. Surface any inert (uncredentialed) fallback in the role's rung —
    # dormant, never auto-stripped (it auto-activates when the key is added).
    dormant_models: list[str] = []
    if available_providers is not None:
        rung_id = _resolve_role_to_rung(catalog, role)
        for m in (_rung_models(catalog, rung_id) if rung_id else []):
            if provider_of(m) not in available_providers and m not in dormant_models:
                dormant_models.append(m)
    # Judge soft advisory: routes, but same vendor as standard. Diversity is a
    # recommendation — this is an amber nudge, never the red hard-break panel.
    if reason == "same_vendor_as_standard":
        return {
            "role": role, "severity": SEVERITY_ADVISORY, "model": model,
            "reason": reason, "providers": rung_providers,
            "dormant_models": dormant_models,
            "advisory_provider": info.get("advisory_provider"),
        }
    return {
        "role": role,
        "severity": SEVERITY_DORMANT if dormant_models else SEVERITY_OK,
        "model": model, "reason": reason,
        "providers": rung_providers, "dormant_models": dormant_models,
    }


def classify_bot_tier_severities(
    pod_models: dict[str, Any] | None,
    bot_tiers: dict[str, Any] | None,
    credentialed_providers: set[str] | None,
) -> dict[str, dict[str, Any]]:
    """Per-role severity for a bot, built from the SAME merged catalog the
    credential-gap drift findings use, so the two never disagree.

    ``bot_tiers`` is the bot's legacy ``{tierN: {models}}`` map (the
    ``full_config_get`` ``tiers`` field — what ``find_catalog_drift`` walks). It
    is folded over the pod layer (``network.json::models``) and the code defaults
    via :func:`merge_model_catalog` (which normalizes the legacy shape into
    rungs/roles), so role resolution matches what the gateway would route.
    Returns ``{role: classification}`` for every role in :data:`ROLE_ORDER`.
    """
    pod_models = pod_models if isinstance(pod_models, dict) else {}
    override = bot_tiers if isinstance(bot_tiers, dict) else {}
    # The route hands the bare legacy tierN map; wrap it as a layer the merge
    # understands. A caller passing a full {rungs, roles} doc is honored as-is.
    if not ({"tiers", "rungs", "roles"} & set(override)):
        override = {"tiers": override}
    cat_bot = merge_model_catalog(pod_models, override)
    avail = available_providers_for_resolution(cat_bot, credentialed_providers)
    return {role: classify_role_severity(cat_bot, role, avail) for role in ROLE_ORDER}


def _usable_models(rung: dict | None) -> list[str]:
    """Models in ``rung`` that actually name something (non-blank strings)."""
    if not isinstance(rung, dict):
        return []
    return [m for m in (rung.get("models") or []) if isinstance(m, str) and m.strip()]


def _merge_one_rung(base_rung: dict, over_rung: dict) -> dict:
    """Merge one same-id override rung onto its base rung (override wins).

    Empty-models override = no-op FOR MODELS. The spec's rule is "explicit
    config wins wherever it speaks" (§Addendum 2). A rung whose ``models``
    list is empty or all-whitespace does NOT speak for models — so it must
    not SHADOW the base (code-default / pod) rung and brick resolution for
    that rung's role (e.g. an operator-written ``sonnet-class: {models: []}``
    silently bricking ``standard``). Such an override keeps the base rung's
    models while still applying its other fields (``costClass`` etc.).

    A rung with at least one usable model wins wholesale, as before.

    Mirrors ``mergeOneRung`` in ModelRouter.ts — keep the two in sync.
    """
    if _usable_models(over_rung):
        return over_rung
    merged = dict(base_rung)
    merged.update(over_rung)
    # Restore the base rung's models — the override's empty/blank list doesn't
    # speak, so resolution still finds the base cluster.
    merged["models"] = base_rung.get("models", [])
    return merged


def _merge_two(base: dict | None, override: dict | None) -> dict:
    """Keyed merge of exactly two ``models``-block shapes (override wins).

    The pure two-layer keyed merge. :func:`merge_model_catalog` folds the
    code-default base layer beneath this; this helper is the kernel both it
    and the TS mirror share rule-for-rule.

      - ``rungs``     — merged by ``id``. A per-bot rung with the same id
        wins *wholesale* (its ``models``/``costClass`` replace the base
        rung's). Base-only rungs are appended after the merged set, in pod
        order. Override-only rungs keep their relative order at the end.
      - ``roles``     — merged by role key; the per-bot entry wins.
      - ``roleCaps``  — merged by role key; the per-bot entry wins.

    Other top-level keys (``routing``, ``userTierOverride``, ``cascade``,
    …) take the override value when present, else the base value — the
    per-bot file stays authoritative for its own routing/cascade knobs.

    Block-precedence (the pre-Addendum behavior) made a pod-wide adoption
    invisible: every bot carries per-bot rungs, so ``override`` always won
    the whole block and a base-only rung (a freshly adopted model) never
    surfaced. Keyed merge fixes exactly that — a base-only rung is now
    visible to every bot whose override doesn't shadow its id.

    The TS gateway loader mirrors this in ``mergeModelCatalog`` in
    ModelRouter.ts — keep the two in sync.
    """
    base = base if isinstance(base, dict) else {}
    override = override if isinstance(override, dict) else {}

    # If neither side carries a new-shape ``rungs`` array, there is nothing
    # to keyed-merge — preserve block-precedence (override wins) so a
    # legacy-only or empty file resolves exactly as before.
    base_rungs = base.get("rungs")
    over_rungs = override.get("rungs")
    base_has = isinstance(base_rungs, list) and bool(base_rungs)
    over_has = isinstance(over_rungs, list) and bool(over_rungs)
    if not base_has and not over_has:
        return dict(override) if override else dict(base)

    merged: dict = dict(base)
    merged.update(override)  # override wins for scalar/other keys

    # ── rungs: merge by id ────────────────────────────────────────────────
    over_by_id: dict[str, dict] = {}
    over_order: list[str] = []
    for r in (over_rungs or []):
        if isinstance(r, dict) and isinstance(r.get("id"), str):
            over_by_id[r["id"]] = r
            over_order.append(r["id"])
    merged_rungs: list[dict] = []
    seen: set[str] = set()
    # Base order first; a same-id override replaces the base rung in place.
    for r in (base_rungs or []):
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if isinstance(rid, str) and rid in over_by_id:
            merged_rungs.append(_merge_one_rung(r, over_by_id[rid]))
            seen.add(rid)
        else:
            merged_rungs.append(r)
            if isinstance(rid, str):
                seen.add(rid)
    # Override-only rungs append in override order.
    for rid in over_order:
        if rid not in seen:
            merged_rungs.append(over_by_id[rid])
            seen.add(rid)
    merged["rungs"] = merged_rungs

    # ── roles: merge by key (override wins) ───────────────────────────────
    base_roles = base.get("roles") if isinstance(base.get("roles"), dict) else {}
    over_roles = override.get("roles") if isinstance(override.get("roles"), dict) else {}
    if base_roles or over_roles:
        merged["roles"] = {**base_roles, **over_roles}

    # ── roleCaps: merge by key (override wins) ────────────────────────────
    base_caps = base.get("roleCaps") if isinstance(base.get("roleCaps"), dict) else {}
    over_caps = override.get("roleCaps") if isinstance(override.get("roleCaps"), dict) else {}
    if base_caps or over_caps:
        merged["roleCaps"] = {**base_caps, **over_caps}

    return merged


# Legacy ``tierN`` → rung slug, used to synthesize a legacy-only layer into
# the rungs/roles shape so it participates in the keyed merge. Mirrors
# ``_LEGACY_TIER_TO_RUNG`` in ModelRouter.ts — keep in sync.
_LEGACY_TIER_TO_RUNG: dict[str, str] = {
    "tier3": "haiku-class",
    "tier2": "sonnet-class",
    "tier1": "opus-class",
    "tier0": "sonnet-class",  # judge shares the sonnet-class rung
}


def _normalize_legacy_layer(layer: dict | None) -> dict | None:
    """Synthesize a legacy ``tiers.tierN`` layer into the rungs/roles shape so
    it participates in the keyed merge as a first-class override.

    Without this, folding the code-default ``rungs`` as the base layer (spec
    §Addendum 2) would silently SHADOW an un-migrated bot/pod whose config only
    speaks the legacy ``tiers`` shape: the merged catalog carries the defaults'
    rungs, role resolution (``_resolve_role_to_rung``) keys off ``rungs``/
    ``roles`` first, and the legacy ``tiers`` never resolves — violating the
    spec's "existing config wins wherever it speaks". Synthesizing the legacy
    tiers into rungs/roles up front lets them override the defaults by id/key.

    Mirrors ``_normalizeLegacyLayer`` in ModelRouter.ts. A layer that already
    carries ``rungs`` (or has no legacy ``tiers``) is returned unchanged.

    NOTE: ``tiers.tierN.fallbacks`` are folded into the synthesized cluster on
    the Python side ONLY — by design, see F2 of the #2561 review (TS routes
    legacy fallbacks via the profile-fallback chain; Python needs them in the
    known-set/UI). Don't "fix" the TS side to match.
    """
    if not isinstance(layer, dict):
        return layer
    rungs = layer.get("rungs")
    if isinstance(rungs, list) and rungs:
        return layer
    tiers = layer.get("tiers")
    if not isinstance(tiers, dict):
        return layer
    # Cost order so rung array position stays a valid cost rank (cheapest first).
    cost_order = [("tier3", "low"), ("tier2", "medium"), ("tier1", "high")]
    new_rungs: list[dict] = []
    new_roles: dict[str, Any] = {}
    synthesized = False
    for tier_key, cost_class in cost_order:
        t = tiers.get(tier_key) if isinstance(tiers.get(tier_key), dict) else {}
        # Fold per-tier ``fallbacks`` into the rung cluster, matching the Phase-1
        # migration (``migrate_models_block``): a fallback on an un-migrated bot
        # is an adopted model, not a discovery. Mirrors ``_models_from_tiers_file``.
        models: list[str] = []
        for key in ("models", "fallbacks"):
            for m in (t.get(key) or []):
                if isinstance(m, str) and m and m not in models:
                    models.append(m)
        if not models:
            continue
        rung_id = _LEGACY_TIER_TO_RUNG[tier_key]
        new_rungs.append({"id": rung_id, "models": models, "costClass": cost_class})
        new_roles[TIER_TO_ROLE[tier_key]] = rung_id
        synthesized = True
    # tier0 (judge) shares the sonnet-class rung with the diversity constraint.
    tier0 = tiers.get("tier0")
    tier0_models = (tier0 or {}).get("models") if isinstance(tier0, dict) else None
    tier0_models = [m for m in (tier0_models or []) if isinstance(m, str) and m]
    if tier0_models:
        sonnet = next((r for r in new_rungs if r["id"] == "sonnet-class"), None)
        if sonnet is not None:
            for m in tier0_models:
                if m not in sonnet["models"]:
                    sonnet["models"].append(m)
        else:
            new_rungs.insert(0, {"id": "sonnet-class", "models": tier0_models, "costClass": "medium"})
        new_roles["judge"] = {"rung": "sonnet-class", "provider": "not-standard"}
        synthesized = True
    if not synthesized:
        return layer
    rest = {k: v for k, v in layer.items() if k != "tiers"}
    rest["rungs"] = new_rungs
    rest["roles"] = {**new_roles, **(layer.get("roles") or {})}
    return rest


def merge_model_catalog(
    base: dict | None,
    override: dict | None,
    *,
    include_defaults: bool = True,
) -> dict:
    """Keyed merge folding the code-default base layer beneath ``base``/``override``.

    Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Addendum 2 — the
    canonical layering is::

        DEFAULT_MODEL_CATALOG  ←  base (network.json pod)  ←  override (bot)

    The two existing arguments keep their meaning — ``base`` is the pod layer
    (``network.json::models``), ``override`` the per-bot layer
    (``evolve-tiers.json``). The default catalog is prepended as the deepest
    base so EVERY existing call site gains the defaults layer for free, without
    editing the call itself. The fold is two applications of the pure
    two-layer kernel :func:`_merge_two`: ``_merge_two(_merge_two(defaults,
    pod), bot)`` — defaults < pod < bot, override-wins-wholesale per key at
    each step.

    ``include_defaults=False`` reproduces the pre-Addendum-2 pure two-layer
    behavior (pod ← bot only) for callers that explicitly want the un-defaulted
    merge (and for the parity kernel tests).

    Keep in lockstep with ``mergeModelCatalog`` in ModelRouter.ts, which folds
    its own ``DEFAULT_MODEL_CATALOG`` the same way.
    """
    norm_base = _normalize_legacy_layer(base)
    norm_override = _normalize_legacy_layer(override)
    if not include_defaults:
        return _merge_two(norm_base, norm_override)
    with_pod = _merge_two(default_model_catalog(), norm_base)
    return _merge_two(with_pod, norm_override)


def resolve_tier_chain(data: dict, tier: str) -> list[str]:
    """Resolve the model chain for a legacy ``tierN`` key from any file shape.

    Resolution order:
      1. **New shape (rungs/roles wins):** map ``tierN`` → role
         (tier3→fast, tier2→standard, tier1→power, tier0→judge), resolve the
         role through ``roles`` → rung slug, return that rung's ``models``.
         Applies whenever the file carries a non-empty ``rungs`` array — even
         if a stale legacy ``tiers`` key is also present (mixed shape: the new
         shape is authoritative, matching the gateway loader).
      2. **Legacy fall-through:** when no ``rungs`` are present, read
         ``tiers.<tierN>.models`` as before.

    Returns ``[]`` only when neither shape yields a chain for ``tier`` — a
    genuine "this tier is unconfigured", which callers fall through to
    DEFAULT_TIERS on. The pre-migration silent failure (``[]`` returned for a
    perfectly-good migrated file) is gone: a migrated file always resolves via
    branch 1.
    """
    if not isinstance(data, dict):
        return []
    rungs = data.get("rungs")
    if isinstance(rungs, list) and rungs:
        role = TIER_TO_ROLE.get(tier)
        if role is None:
            return []
        rung_id = _resolve_role_to_rung(data, role)
        if not rung_id:
            return []
        return _rung_models(data, rung_id)
    # Legacy-only file: read the old tier shape.
    tiers = data.get("tiers")
    if isinstance(tiers, dict):
        cfg = tiers.get(tier)
        if isinstance(cfg, dict):
            models = cfg.get("models")
            if isinstance(models, list):
                return [m for m in models if isinstance(m, str) and m]
    return []


def tier_override_is_broken(override: dict | None, tier: str) -> bool:
    """True iff ``override`` EXPLICITLY speaks for ``tier`` but yields no model.

    The companion to the empty-rung-is-a-no-op merge rule (``_merge_one_rung``).
    The merge intentionally lets the code-default / pod base fill a tier whose
    per-bot override is empty, so the bot still resolves and *works*. But an
    operator who hand-wrote ``tier2: {models: []}`` (or all-whitespace) authored
    BROKEN config, not absent config — onboarding / setup-checklist must still
    flag it so they fix it, even though the runtime falls back gracefully.

    This inspects the RAW per-bot override (never the merged catalog): the merge
    has already hidden the breakage by design. "Explicitly speaks" means the
    override carries an entry keyed to this tier's role/rung — via either shape:

      - legacy: ``tiers.<tierN>`` present (a dict) but its ``models`` resolve to
        nothing usable.
      - new shape: a ``rungs`` entry whose id is the role's rung (e.g.
        ``sonnet-class`` for tier2→standard), present but with no usable models.

    Returns False when the override simply omits the tier (absent → defaulted,
    which is *configured* per spec §Addendum 2) — only an explicit-but-empty
    entry flags. Mirrors ``tierOverrideIsBroken`` in ModelRouter.ts.
    """
    if not isinstance(override, dict):
        return False
    role = TIER_TO_ROLE.get(tier)
    # ── new shape: an explicit rung for this tier's rung id ───────────────────
    rungs = override.get("rungs")
    if isinstance(rungs, list) and rungs:
        rung_id = _resolve_role_to_rung(override, role) if role else None
        # Fall back to the canonical rung slug so a rungs-shaped file that omits
        # the ``roles`` map is still checked against the conventional rung.
        if not rung_id and role:
            rung_id = _DEFAULT_ROLE_TO_RUNG.get(role)
        if rung_id:
            for r in rungs:
                if isinstance(r, dict) and r.get("id") == rung_id:
                    return not _usable_models(r)
    # ── legacy shape: an explicit ``tiers.<tierN>`` entry ─────────────────────
    tiers = override.get("tiers")
    if isinstance(tiers, dict) and tier in tiers:
        cfg = tiers.get(tier)
        if isinstance(cfg, dict):
            models = [
                m for m in (cfg.get("models") or [])
                if isinstance(m, str) and m.strip()
            ]
            return not models
    return False


def _models_from_tiers_file(data: dict) -> set[str]:
    """Extract every model id carried by a parsed ``evolve-tiers.json``.

    Unions BOTH shapes at the file's top level:
      - new rungs/roles shape: ``rungs[].models[]``
      - legacy tier shape:     ``tiers.<tierN>.models[]`` +
        ``tiers.<tierN>.fallbacks[]`` — the Phase-1 migration folds per-tier
        fallbacks into the rung cluster (``migrate_models_block``), so a
        fallback on an un-migrated bot is an adopted model, not a discovery.
    Ids are returned verbatim (provider-qualified or bare) — the caller
    normalizes to bare-lowercase via the discovery side's ``_bare_id``.
    """
    out: set[str] = set()
    for rung in (data.get("rungs") or []):
        if isinstance(rung, dict):
            for m in (rung.get("models") or []):
                if isinstance(m, str) and m:
                    out.add(m)
    tiers = data.get("tiers")
    if isinstance(tiers, dict):
        for tier_entry in tiers.values():
            if isinstance(tier_entry, dict):
                for key in ("models", "fallbacks"):
                    for m in (tier_entry.get(key) or []):
                        if isinstance(m, str) and m:
                            out.add(m)
    return out


def bot_evolve_tiers_models(
    network: dict[str, Any], bot_id: str
) -> tuple[set[str], bool]:
    """Return ``(model_ids, ok)`` for a bot's whole ``evolve-tiers.json``.

    This is the per-bot source of truth for the pod's adopted model set —
    rungs/roles config lives in each bot's ``~/.openclaw/evolve-tiers.json``
    (NOT in ``network.json`` on a real pod). It powers the same read path
    the AI-Optimization page uses via :func:`bot_tier_models`, but returns
    the FULL set across every rung/tier rather than one tier's chain.

    ``ok`` is False only when the file exists but cannot be read or parsed
    (a degraded read). A missing file yields ``(set(), True)`` — a bot that
    has never saved tiers genuinely knows nothing, which is not an error.
    """
    if not bot_id:
        return set(), True
    path = _bot_evolve_tiers_path(network, bot_id)
    data, ok = _read_bot_owned_json(path)
    if data is None and not ok:
        # Degraded read — never flatten to "knows nothing".
        return set(), ok
    # Keyed-merge the POD layer (network.json::models) so a pod-wide adoption
    # (a rung that lives only in network.json::models) counts as KNOWN for
    # discovery. Without this the lifecycle never closes: an adopted model that
    # the per-bot file doesn't yet carry would re-surface as a new discovery
    # on the next run (spec §Addendum A.4 + A.5 closure).
    #
    # include_defaults=False on purpose: this function reports the PER-BOT (+ pod)
    # adopted set, NOT the code defaults. The defaults are unioned separately by
    # ``model_discovery.known_model_set`` (its step 0) so the empty-pod-set
    # degraded-read suppression guard can tell the pod's own set apart from the
    # always-present defaults. Folding defaults here would make a never-saved
    # bot report "knows the whole default ladder" and silently disable that
    # guard — see the pod-sourced-emptiness check in ``run_discovery``.
    pod_models = (network or {}).get("models")
    merged = merge_model_catalog(pod_models, data or {}, include_defaults=False)
    return _models_from_tiers_file(merged), ok
