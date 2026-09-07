"""
extractor.py — Capture a live pod's shape into a :class:`PodTemplate`.

The driving Bite-1 use case: derive a pod-template *from* the running pod so
"this pod" becomes reproducible (round-trip is the proof). Provisioning
*from* a pod-template is Bite 2 and lives elsewhere.

Two hard invariants, enforced here:

1. **Membership comes only from ``network.json``.** The pod roster is
   ``network.json::bots`` (or ``network.json::pod.bots``) — the single
   source of truth ([[feedback_explicit_pod_membership]]). The extractor
   **never scans ``/Users/``** to discover bots. The FS reader below
   (:func:`read_bot_evidence`) reads files belonging to bots *already named
   in network.json* to enrich each captured bot — that is targeted member
   enrichment, not discovery, and it never adds a bot to the roster.

2. **No secrets.** A pod-template captures shape — release mode, model
   tiers, integration names — never credentials. Capture is built on an
   **allowlist** (only the three pod-wide blocks are copied; the
   passphrase-bearing ``pod`` block is never copied wholesale) and then
   defense-in-depth scrubbed and *asserted clean* via
   :func:`validator.find_secrets` before a :class:`PodTemplate` is returned.
   A leak is a hard :class:`PodTemplateExtractError`, never a silent emit.

Design split (so the core is unit-testable without a filesystem):

* :func:`extract_pod_template` is **pure** — network dict + an evidence map
  in, ``PodTemplate`` out. No IO, no ``/Users``.
* :func:`bot_evidence_from_files` is **pure** — already-read dicts in,
  ``BotEvidence`` out.
* :func:`read_bot_evidence` / :func:`build_pod_evidence` are the thin,
  best-effort FS wrappers used by the CLI; failures degrade to minimal
  evidence rather than raising.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .errors import PodTemplateExtractError
from .loader import (
    POD_TEMPLATE_SCHEMA_VERSION,
    PodBotSpec,
    PodSeed,
    PodTemplate,
    builtin_pod_templates_dir,
)
from .validator import find_secrets, is_secret_key, value_looks_secret


# ── Evidence ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BotEvidence:
    """Per-bot facts used to enrich a captured bot beyond network.json.

    All optional — a bot with no evidence is still captured (membership is
    authoritative), just with ``bot_template=None`` and no app list.

    Attributes:
        channel:         Primary messaging channel, if known.
        voice_preset:    Voice/personality preset id, if known.
        role:            Pod role, if not already in the network bot entry.
        installed_apps:  App identifiers the bot runs (gallery ids / names),
                         used both for bot-template matching and to keep an
                         unmatched bot reproducible.
        installed_skills: Skill ids the bot has, used for matching.
        overrides:       Per-bot deviations to preserve (template vars, etc.).
                         Scrubbed before it lands in the template.
    """

    channel: str | None = None
    voice_preset: str | None = None
    role: str | None = None
    installed_apps: tuple[str, ...] = ()
    installed_skills: tuple[str, ...] = ()
    overrides: dict[str, Any] = field(default_factory=dict)


# ── Membership (network.json only — the hard invariant) ─────────────────────────


def pod_members(network: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return the pod roster as ``[(bot_id, bot_cfg), ...]`` from network.json.

    Reads ``network.json::bots`` (the canonical location) and, if present,
    merges ``network.json::pod.bots`` (the alternate location named in the
    spec). **Never** scans ``/Users/`` — this is the single source of truth
    for membership.

    *Planned* bots (a block whose only key is ``purpose`` — the add-bot
    wizard's not-yet-deployed placeholder) are excluded: they have no
    account or runtime to capture. Order follows insertion order of the
    ``bots`` dict so the emitted template is stable.
    """
    if not isinstance(network, dict):
        return []
    members: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    pod_block = network.get("pod")
    # Guard a non-dict pod block (malformed network.json) — computing
    # ``pod_block.get(...)`` unconditionally would raise AttributeError and
    # escape the module's "core raises only PodTemplateExtractError" contract.
    pod_bots = pod_block.get("bots") if isinstance(pod_block, dict) else None
    for source in (network.get("bots"), pod_bots):
        if not isinstance(source, dict):
            continue
        for bot_id, cfg in source.items():
            if not isinstance(bot_id, str) or bot_id in seen:
                continue
            cfg = cfg if isinstance(cfg, dict) else {}
            if _is_planned_bot(cfg):
                continue
            seen.add(bot_id)
            members.append((bot_id, cfg))
    return members


def _is_planned_bot(cfg: dict[str, Any]) -> bool:
    # Mirror config.is_planned_bot_block without importing it (keeps the
    # extractor's core dependency-free and testable in isolation).
    return set(cfg.keys()) <= {"purpose"} and "purpose" in cfg


# ── Pure extraction core ────────────────────────────────────────────────────────


def extract_pod_template(
    network: dict[str, Any],
    *,
    name: str,
    display_name: str | None = None,
    description: str | None = None,
    evidence: Mapping[str, BotEvidence] | None = None,
    candidates: "list[Any] | None" = None,
    templates_dir: Path | None = None,
) -> PodTemplate:
    """Capture ``network`` into a :class:`PodTemplate` (pure — no IO).

    Args:
        network:       A loaded ``network.json`` dict.
        name:          Slug for the new pod-template (becomes the dir name).
        display_name:  Human label; defaults to ``network.networkId`` or name.
        description:   One-line description; a sensible default is generated.
        evidence:      Optional ``{bot_id: BotEvidence}`` enrichment map.
        candidates:    Optional pre-loaded ``BotTemplate`` list to match
                       against. Defaults to loading every gallery bot-template
                       (lazy import — only if matching is actually needed).
        templates_dir: Bot-template search root for the default candidate load.

    Raises:
        PodTemplateExtractError: if ``network`` is not a dict, the roster is
            empty, or a secret survives the scrubber (defensive — should
            never happen).
    """
    if not isinstance(network, dict):
        raise PodTemplateExtractError(
            f"network must be a dict (a loaded network.json), "
            f"got {type(network).__name__}"
        )
    members = pod_members(network)
    if not members:
        raise PodTemplateExtractError(
            "network.json has no bots — nothing to capture. Membership is "
            "read only from network.json::bots (never inferred from a "
            "filesystem home-directory scan)."
        )

    evidence = evidence or {}

    # Resolve candidate bot-templates lazily — only load the gallery when at
    # least one bot has app/skill evidence worth matching, and only once.
    _candidates = candidates
    needs_match = any(
        evidence.get(bid)
        and (evidence[bid].installed_apps or evidence[bid].installed_skills)
        for bid, _ in members
    )
    if _candidates is None and needs_match:
        _candidates = load_bot_template_candidates(templates_dir=templates_dir)
    _candidates = _candidates or []

    pod = _extract_pod_seed(network, members, evidence)

    bots: list[PodBotSpec] = []
    for bot_id, cfg in members:
        ev = evidence.get(bot_id)
        bots.append(_extract_bot(bot_id, cfg, ev, _candidates))

    net_id = str(network.get("networkId") or "").strip()
    final_display = display_name or net_id or name
    final_desc = description or (
        f"Captured shape of pod {net_id or name!r}: "
        f"{len(bots)} bot(s), reproducible scaffold (no secrets)."
    )

    template = PodTemplate(
        name=name,
        path=None,
        schema_version=POD_TEMPLATE_SCHEMA_VERSION,
        display_name=final_display,
        description=final_desc,
        pod=pod,
        bots=tuple(bots),
    )

    # Defense-in-depth: the allowlist + scrubber should have removed every
    # secret already. Assert it before returning so a regression fails loud
    # at capture time rather than shipping a credential in a shared artifact.
    leaks = find_secrets(template.to_payload())
    if leaks:
        where = "; ".join(f"{p} ({why})" for p, why in leaks[:5])
        raise PodTemplateExtractError(
            f"refusing to emit pod-template {name!r}: secret survived scrubbing "
            f"at {where}. This is a scrubber bug — fix before capturing."
        )
    return template


def _extract_pod_seed(
    network: dict[str, Any],
    members: list[tuple[str, dict[str, Any]]],
    evidence: Mapping[str, BotEvidence],
) -> PodSeed:
    """Build the pod-wide seed by **allowlist** — only known-safe blocks.

    Crucially this never copies ``network.json::pod`` wholesale (that block
    holds the admin/primary passphrases). It pulls exactly three things:
    release mode, the models block, and an integration name map.
    """
    # Release mode — pod.release.mode only; nothing else from the pod block.
    release_mode: str | None = None
    pod_block = network.get("pod")
    if isinstance(pod_block, dict):
        rel = pod_block.get("release")
        if isinstance(rel, dict):
            mode = rel.get("mode")
            if isinstance(mode, str) and mode.strip():
                release_mode = mode.strip().lower()

    # Model tiers — the whole models block (rungs/roles/tiers/catalog).
    # By construction this block holds provider *names* and rung shapes, not
    # credentials (real provider keys live in the vault / auth-profiles, never
    # network.json). The scrub below is defense-in-depth — it drops
    # secret-NAMED keys and known secret-SHAPED values, but does not claim to
    # detect an arbitrary free-text secret hidden under an innocent key. The
    # primary guarantee is the allowlist: only this block (plus release mode +
    # integration names) is copied at all; the credential-bearing blocks
    # (pod passphrases, mcp_bridge.auth, …) are never read.
    model_tiers_raw = network.get("models")
    model_tiers = _scrub_secrets(model_tiers_raw) if isinstance(model_tiers_raw, dict) else {}

    integrations = _extract_integrations(network, members, evidence)

    return PodSeed(
        release_mode=release_mode,
        model_tiers=model_tiers,
        integrations=integrations,
    )


def _extract_integrations(
    network: dict[str, Any],
    members: list[tuple[str, dict[str, Any]]],
    evidence: Mapping[str, BotEvidence],
) -> dict[str, Any]:
    """Capture the integration map by *name + scope only* — never tokens.

    Pod-invariant integrations (``network.podInvariantIntegrations``) are
    scoped ``"pod"``. Per-bot integrations surfaced via evidence are scoped
    ``"bot"`` with the using bots listed. Both carry names only.
    """
    out: dict[str, Any] = {}
    pod_invariant = network.get("podInvariantIntegrations") or []
    if isinstance(pod_invariant, list):
        for raw in pod_invariant:
            n = str(raw).strip()
            if n and not is_secret_key(n):
                out[n] = {"scope": "pod"}

    # Per-bot integration names (from evidence overrides, if present). We do
    # NOT read tokens — only names. A name colliding with a pod-scope entry
    # leaves the pod scope (broader) in place.
    for bot_id, _cfg in members:
        ev = evidence.get(bot_id)
        if not ev:
            continue
        names = ev.overrides.get("integrations") if isinstance(ev.overrides, dict) else None
        if not isinstance(names, (list, tuple)):
            continue
        for raw in names:
            n = str(raw).strip()
            if not n or is_secret_key(n):
                continue
            entry = out.setdefault(n, {"scope": "bot", "bots": []})
            if entry.get("scope") == "bot":
                bots_list = entry.setdefault("bots", [])
                if bot_id not in bots_list:
                    bots_list.append(bot_id)
    return out


def _extract_bot(
    bot_id: str,
    cfg: dict[str, Any],
    ev: "BotEvidence | None",
    candidates: "list[Any]",
) -> PodBotSpec:
    # network.json is authoritative for role; evidence fills gaps.
    role = cfg.get("role")
    role = str(role) if role else (ev.role if ev else None)

    channel = (ev.channel if ev else None) or _str_or_none(cfg.get("channel"))
    voice = (ev.voice_preset if ev else None) or _str_or_none(cfg.get("voice_preset"))

    installed_apps = ev.installed_apps if ev else ()
    matched = match_bot_template(ev, candidates) if ev else None

    # When no template matched, keep the app list inline so the capture is
    # not lossy. When a template DID match, drop the redundant app list
    # (the template already declares them) to keep the artifact lean.
    apps_for_spec: tuple[str, ...] = () if matched else tuple(installed_apps)

    overrides = _scrub_overrides(ev.overrides) if ev else {}

    return PodBotSpec(
        bot_id=bot_id,
        bot_template=matched,
        channel=channel,
        voice_preset=voice,
        role=role,
        installed_apps=apps_for_spec,
        overrides=overrides,
    )


# ── Bot-template matching (best-effort) ──────────────────────────────────────────


def match_bot_template(
    evidence: "BotEvidence | None",
    candidates: "list[Any]",
    *,
    threshold: float = 0.6,
) -> str | None:
    """Best-effort match a bot's installed apps/skills to a bot-template.

    Scores each candidate by how completely the bot's evidence *covers* the
    template's declared apps + skills (coverage of the template, not of the
    bot — a bot may run extra apps and still match a template it is built
    on). Requires at least one **app** match (apps are the strong signal),
    coverage ≥ ``threshold``, and breaks ties by (more matched, then name).

    Returns the matching template name, or ``None`` (→ ``bot_template: null``
    + inline app list). Candidates are duck-typed (``.name``,
    ``.applications`` with ``.app_id``/``.name``, ``.skills`` with ``.id``)
    so tests can pass lightweight stand-ins without the real loader.
    """
    if not evidence or not candidates:
        return None
    bot_apps = {a.lower() for a in evidence.installed_apps}
    bot_skills = {s.lower() for s in evidence.installed_skills}
    if not bot_apps and not bot_skills:
        return None

    # (score, n_matched, name) for every qualifying candidate. Selection is
    # max score, then most matched, then lexicographically smallest name.
    scored: list[tuple[float, int, str]] = []
    for tpl in candidates:
        tpl_name = str(getattr(tpl, "name", "")).strip()
        if not tpl_name:
            continue
        # Each application is ONE declared unit (matched if any of its
        # identifiers — app_id or name — is present), so a single app never
        # double-counts against the denominator. Skills are one unit each.
        app_units = _template_app_units(tpl)
        skill_units = [
            {str(getattr(s, "id", "")).lower()}
            for s in getattr(tpl, "skills", ())
            if str(getattr(s, "id", "")).strip()
        ]
        total_units = len(app_units) + len(skill_units)
        if total_units == 0:
            continue
        app_matches = sum(1 for ids in app_units if ids & bot_apps)
        if app_matches == 0:
            continue  # apps are the required strong signal
        skill_matches = sum(1 for ids in skill_units if ids & bot_skills)
        matched = app_matches + skill_matches
        score = matched / total_units
        if score >= threshold:
            scored.append((score, matched, tpl_name))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
    return scored[0][2]


def _template_app_units(tpl: Any) -> list[set[str]]:
    """Each application as a set of its lowercased identifiers (app_id, name)."""
    units: list[set[str]] = []
    for app in getattr(tpl, "applications", ()):
        ids = {
            str(getattr(app, attr, "") or "").lower()
            for attr in ("app_id", "name")
        }
        ids.discard("")
        if ids:
            units.append(ids)
    return units


def load_bot_template_candidates(*, templates_dir: Path | None = None) -> "list[Any]":
    """Load every gallery bot-template as a candidate (lazy, best-effort).

    Imports ``bot_templates`` lazily so the pod-template core has no hard
    dependency on the bot-template package at import time. A template that
    fails to load is skipped rather than aborting the whole capture.
    """
    try:
        from ..bot_templates import list_templates, load_template
    except Exception:
        return []
    out: list[Any] = []
    try:
        names = list_templates(templates_dir=templates_dir)
    except Exception:
        return []
    for n in names:
        try:
            out.append(load_template(n, templates_dir=templates_dir))
        except Exception:
            continue
    return out


# ── Secret scrubbing ─────────────────────────────────────────────────────────────


def _scrub_secrets(value: Any) -> Any:
    """Return a deep copy with secret-named keys and secret-valued strings removed.

    The complement to the allowlist: even within an allowlisted block we
    drop anything that looks like a credential. Pairs with the post-extract
    :func:`find_secrets` assertion.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if is_secret_key(str(k)):
                continue
            if isinstance(v, str) and value_looks_secret(v):
                continue
            out[k] = _scrub_secrets(v)
        return out
    if isinstance(value, list):
        return [
            _scrub_secrets(item)
            for item in value
            if not (isinstance(item, str) and value_looks_secret(item))
        ]
    return copy.deepcopy(value)


def _scrub_overrides(overrides: "dict[str, Any] | None") -> dict[str, Any]:
    """Scrub a bot's overrides for the template.

    Drops the per-bot integrations name list (captured pod-wide instead, so
    it would be redundant here) and runs the secret scrubber over the rest.
    Overrides are intended to carry only non-secret deviations (template vars
    like ``user_name``/``time_zone``, channel hints); the scrubber removes
    secret-named keys and known secret-shaped values as defense-in-depth, and
    the post-extract :func:`find_secrets` assertion is the final backstop.
    """
    if not isinstance(overrides, dict):
        return {}
    cleaned = _scrub_secrets({k: v for k, v in overrides.items() if k != "integrations"})
    return cleaned if isinstance(cleaned, dict) else {}


# ── Filesystem evidence readers (best-effort; CLI only) ──────────────────────────


def bot_evidence_from_files(
    *,
    bot_cfg: "dict[str, Any] | None" = None,
    oc_config: "dict[str, Any] | None" = None,
    manifests: "list[dict[str, Any]] | None" = None,
) -> BotEvidence:
    """Build :class:`BotEvidence` from already-read dicts (pure — testable).

    Args:
        bot_cfg:    The bot's ``network.json`` entry (role/channel/voice).
        oc_config:  The bot's ``openclaw.json`` (channel + installed plugins).
        manifests:  Parsed app manifest dicts from the bot's ``manifests/``.
    """
    bot_cfg = bot_cfg or {}
    oc_config = oc_config or {}
    manifests = manifests or []

    role = _str_or_none(bot_cfg.get("role"))
    channel = _str_or_none(bot_cfg.get("channel")) or _channel_from_oc(oc_config)
    voice = _str_or_none(bot_cfg.get("voice_preset"))

    apps: list[str] = []
    for m in manifests:
        if not isinstance(m, dict):
            continue
        # identity: see resolve_app_id — NOT swept (AL-1.4b). This chain emits
        # the BOT-TEMPLATE MATCHING key, not the canonical app id: the values
        # are matched (lowercased) against ``ApplicationSpec.app_id`` /
        # ``.name`` in ``match_bot_template``, and a bot template's ``app_id``
        # is a gallery slug (``"app_task_manager"``), never a forge ``p-``
        # pkg_id. ``id`` therefore leads DELIBERATELY — the inverse of the
        # canonical order, which puts ``pkg_id`` first. Sweeping this would
        # replace every stem-shaped identifier on a stamped pod with an opaque
        # ``p-`` id and silently stop every template from matching.
        ident = m.get("id") or m.get("pkg_id") or m.get("name")
        if ident:
            apps.append(str(ident))

    skills = _skills_from_oc(oc_config)

    return BotEvidence(
        channel=channel,
        voice_preset=voice,
        role=role,
        installed_apps=tuple(dict.fromkeys(apps)),       # de-dup, keep order
        installed_skills=tuple(dict.fromkeys(skills)),
    )


def read_bot_evidence(
    bot_id: str,
    bot_cfg: dict[str, Any],
    network: dict[str, Any],
) -> BotEvidence:
    """Read a single *member's* files into evidence (best-effort, FS).

    Reads only files belonging to a bot already named in network.json —
    targeted member enrichment, never roster discovery. Any read failure
    degrades to whatever could be gathered (often just the network.json
    entry); it never raises.
    """
    oc_config: dict[str, Any] = {}
    manifests: list[dict[str, Any]] = []
    home: "Path | None" = None
    try:
        # Resolve the member's home via the blessed helper (never a /Users
        # literal). Lazy import — config pulls in heavy deps.
        from ..config import bot_home
        home = bot_home(bot_id, network)
    except Exception:
        home = None
    if home is not None:
        oc_config = _read_json(home / ".openclaw" / "openclaw.json") or {}
        manifests = _read_manifests(home / ".openclaw" / "workspace" / "manifests")
    return bot_evidence_from_files(
        bot_cfg=bot_cfg, oc_config=oc_config, manifests=manifests
    )


def build_pod_evidence(network: dict[str, Any]) -> dict[str, BotEvidence]:
    """Build an evidence map for every member (best-effort, FS). CLI helper."""
    out: dict[str, BotEvidence] = {}
    for bot_id, cfg in pod_members(network):
        out[bot_id] = read_bot_evidence(bot_id, cfg, network)
    return out


def write_pod_template(
    template: PodTemplate, *, templates_dir: Path | None = None
) -> Path:
    """Write ``template`` to ``<templates_dir>/<name>/pod.yaml``; return the path.

    Defaults to the in-repo gallery dir. On the read-only deploy checkout,
    pass an alternate ``templates_dir`` (e.g. a worktree or shared dir).
    Re-validates (secret sweep) before writing — a belt to the extractor's
    braces, so this can never be the path that ships a leak.
    """
    leaks = find_secrets(template.to_payload())
    if leaks:
        raise PodTemplateExtractError(
            f"refusing to write pod-template {template.name!r}: secret in body "
            f"at {leaks[0][0]} ({leaks[0][1]})"
        )
    root = templates_dir or builtin_pod_templates_dir()
    tdir = root / template.name
    tdir.mkdir(parents=True, exist_ok=True)
    dest = tdir / "pod.yaml"
    dest.write_text(template.to_yaml(), encoding="utf-8")
    return dest


# ── Internal FS / parsing helpers ────────────────────────────────────────────────


def _read_json(path: Path) -> "dict[str, Any] | None":
    import json
    # The evolve user has macOS ACL read on every member bot's .openclaw/
    # (deploy.set_evolve_read_acl — CLAUDE.md), so a direct read works. This
    # is best-effort enrichment: a read failure (bot not yet deployed through
    # the ACL path) degrades to minimal evidence rather than escalating.
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, PermissionError):
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _read_manifests(manifests_dir: Path) -> list[dict[str, Any]]:
    import json
    out: list[dict[str, Any]] = []
    try:
        entries = sorted(manifests_dir.iterdir())
    except (OSError, PermissionError):
        return out
    for f in entries:
        if f.suffix != ".json" or f.name.startswith(("_", ".")):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, PermissionError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _channel_from_oc(oc_config: dict[str, Any]) -> "str | None":
    """Infer the primary channel from an openclaw.json, best-effort.

    Looks for a ``channels``/``integrations`` mapping and returns the first
    messaging-shaped key. Deliberately conservative — returns ``None`` when
    nothing obvious is present rather than guessing.
    """
    for key in ("channels", "channel", "messaging"):
        val = oc_config.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            for ch in ("telegram", "slack", "discord", "signal", "email"):
                if ch in val:
                    return ch
    return None


def _skills_from_oc(oc_config: dict[str, Any]) -> list[str]:
    """Extract installed plugin/skill ids from an openclaw.json, best-effort."""
    plugins = oc_config.get("plugins")
    out: list[str] = []
    if isinstance(plugins, dict):
        installed = plugins.get("installed") or plugins.get("list")
        if isinstance(installed, dict):
            out.extend(str(k) for k in installed.keys())
        elif isinstance(installed, list):
            for item in installed:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict) and item.get("id"):
                    out.append(str(item["id"]))
    elif isinstance(plugins, list):
        for item in plugins:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and item.get("id"):
                out.append(str(item["id"]))
    return out


def _str_or_none(v: Any) -> "str | None":
    if v is None:
        return None
    s = str(v).strip()
    return s or None
