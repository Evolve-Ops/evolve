"""
loader.py — Read a pod template from disk into structured objects.

A pod template is one level up the composition ladder from a bot template:
apps → **bot templates** → **pod templates**. Where a ``BotTemplate``
groups skills + apps for *one* bot, a ``PodTemplate`` groups bot-templates
+ pod-wide settings so a *whole pod* is reproducible.

A pod-template directory looks like::

    gallery/pod-templates/<name>/
        pod.yaml              ← required manifest (pod-wide seed + bot list)

The loader is intentionally permissive — the validator is the gate. The
loader surfaces the on-disk state as Python objects without interpreting
absences as errors (same contract as ``bot_templates.loader``).

Forward-compat with multi-pod (design-multi-pod-2026-06-11.md §5)
────────────────────────────────────────────────────────────────
The body of ``pod.yaml`` *is* the ``payload`` of a multi-pod typed
artifact::

    { "type": "pod_template", "schema_version": N, "payload": {...}, "signature": ... }

:meth:`PodTemplate.to_payload` emits that payload; :func:`wrap_typed_artifact`
wraps it in the envelope. This Bite builds neither the deposit transport
nor the signature — ``signature`` is declared and left ``None`` — but the
shape is pinned so the cross-pod deposit path can adopt it without a schema
churn (the plist-consolidation lesson: type the envelope from day one).

**No secrets, ever.** A pod-template captures a pod's *shape* — release
mode, model tiers, integration names/scopes, the bot roster — never
credentials. The extractor enforces this on capture (see ``extractor.py``);
the schema simply has no field that would hold a token.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import PodTemplateError, PodTemplateNotFoundError


# The schema version this loader understands. Bumped only when the on-disk
# vocabulary changes; consumers branch on the per-file ``schema_version``,
# never on this constant at runtime.
POD_TEMPLATE_SCHEMA_VERSION = 1

# The multi-pod typed-artifact ``type`` discriminator for a wrapped
# pod-template payload (design-multi-pod-2026-06-11.md §5).
TYPED_ARTIFACT_TYPE = "pod_template"


# ── Path resolution ───────────────────────────────────────────────────────────

# This file lives at:
#   packages/admin/evolve_admin/pod_templates/loader.py
# Repo root is 4 parents up:
#   pod_templates → evolve_admin → admin → packages → repo root
_REPO_ROOT: Path = Path(__file__).resolve().parents[4]


def builtin_pod_templates_dir() -> Path:
    """Return the directory holding builtin pod templates.

    Lives at ``{repo_root}/gallery/pod-templates/`` so it ships with the
    evolve repo alongside ``gallery/bot-templates/``.
    """
    return _REPO_ROOT / "gallery" / "pod-templates"


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PodSeed:
    """The pod-wide settings block — a redacted ``network.json`` seed.

    Everything here is *shape*, never *secret*. The fields map to the
    pod-wide pieces a fresh pod needs to look like the captured one:

    Attributes:
        release_mode:  ``pod.release.mode`` — ``"canary"`` or ``"direct"``.
                       ``None`` means "inherit the platform default".
        model_tiers:   The ``network.json::models`` block (rungs / roles /
                       tiers / catalog) — provider *names* only, no keys.
        integrations:  Integration map keyed by integration name, value a
                       small dict carrying ``scope`` (``"pod"`` / ``"bot"``)
                       and the bots that use it — **never** credentials.
        extra:         Forward-compat catch-all for additional pod-wide
                       fields a later schema adds. Preserved verbatim on
                       load so an old loader round-trips a newer file's
                       extra keys without dropping them.
    """

    release_mode: str | None = None
    model_tiers: dict[str, Any] = field(default_factory=dict)
    integrations: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.release_mode is not None:
            out["release_mode"] = self.release_mode
        if self.model_tiers:
            out["model_tiers"] = copy.deepcopy(self.model_tiers)
        if self.integrations:
            out["integrations"] = copy.deepcopy(self.integrations)
        for k, v in self.extra.items():
            out.setdefault(k, copy.deepcopy(v))
        return out


@dataclass(frozen=True)
class PodBotSpec:
    """One bot in the pod's roster, captured as a reproducible reference.

    Attributes:
        bot_id:        The bot's stable id (the ``network.json::bots`` key).
        bot_template:  Name of the matching ``gallery/bot-templates/``
                       template, or ``None`` when no template matched. When
                       ``None``, ``installed_apps`` records what the bot
                       actually runs so it stays reproducible-ish.
        channel:       Primary messaging channel (``"telegram"``, ``"slack"``…).
                       ``None`` if unknown.
        voice_preset:  Voice/personality preset id, or ``None``.
        role:          ``"primary"`` / ``"member"`` — the pod role.
        installed_apps: App identifiers the bot runs (gallery ids / names).
                       Populated mainly when ``bot_template`` is ``None`` so
                       the capture is not lossy; advisory otherwise.
        overrides:     Per-bot deviations from the referenced bot-template
                       (template vars, channel-specific config). **No
                       secrets** — the extractor scrubs this.
    """

    bot_id: str
    bot_template: str | None = None
    channel: str | None = None
    voice_preset: str | None = None
    role: str | None = None
    installed_apps: tuple[str, ...] = ()
    overrides: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"bot_id": self.bot_id}
        # bot_template is emitted even when null — its presence-as-null is
        # meaningful (it says "captured, no template matched"), and it keeps
        # the round-trip stable.
        out["bot_template"] = self.bot_template
        if self.channel is not None:
            out["channel"] = self.channel
        if self.voice_preset is not None:
            out["voice_preset"] = self.voice_preset
        if self.role is not None:
            out["role"] = self.role
        if self.installed_apps:
            out["installed_apps"] = list(self.installed_apps)
        if self.overrides:
            out["overrides"] = copy.deepcopy(self.overrides)
        return out


@dataclass(frozen=True)
class PodTemplate:
    """A fully-loaded pod template, ready for validation / provisioning.

    Attributes:
        name:           Stable id (matches the directory name).
        path:           Absolute path to the template dir, or ``None`` for
                        an in-memory template (e.g. fresh from the extractor
                        before it is written to disk).
        schema_version: The on-disk ``schema_version`` (per-file, not the
                        module constant).
        display_name:   Human-readable name (defaults to ``name``).
        description:    One-paragraph description.
        pod:            The pod-wide :class:`PodSeed`.
        bots:           The bot roster.
        raw:            The parsed ``pod.yaml`` dict (forward-compat / debug).
    """

    name: str
    path: Path | None
    schema_version: int
    display_name: str
    description: str
    pod: PodSeed
    bots: tuple[PodBotSpec, ...]
    raw: dict[str, Any] = field(default_factory=dict)

    # ── Serialization ─────────────────────────────────────────────────────────

    def to_payload(self) -> dict[str, Any]:
        """Serialize to the ``pod.yaml`` body == the typed-artifact payload.

        Key order is deliberate (meta → pod-wide → bots) so the emitted
        YAML reads top-down the way an operator scans it. ``yaml.safe_dump``
        is called with ``sort_keys=False`` to preserve it.
        """
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "pod": self.pod.to_dict(),
            "bots": [b.to_dict() for b in self.bots],
        }
        return payload

    def to_yaml(self) -> str:
        """Render the pod-template as a ``pod.yaml`` string."""
        return yaml.safe_dump(
            self.to_payload(), sort_keys=False, allow_unicode=True, width=88
        )

    def wrap_typed_artifact(self, signature: Any = None) -> dict[str, Any]:
        """Wrap the payload in the multi-pod typed-artifact envelope.

        design-multi-pod-2026-06-11.md §5: ``{type, schema_version, payload,
        signature}``. This Bite does **not** sign — ``signature`` defaults to
        ``None`` and no signing is performed; the parameter exists so the
        cross-pod deposit path can supply one later without a schema change.
        """
        return {
            "type": TYPED_ARTIFACT_TYPE,
            "schema_version": self.schema_version,
            "payload": self.to_payload(),
            "signature": signature,
        }


# ── Loader ────────────────────────────────────────────────────────────────────


def load_pod_template(
    name: str,
    *,
    templates_dir: Path | None = None,
) -> PodTemplate:
    """Read a pod template from disk into a :class:`PodTemplate`.

    Args:
        name:           Template directory name (e.g. ``"home-pod"``).
        templates_dir:  Override the search root. Defaults to
                        :func:`builtin_pod_templates_dir`.

    Raises:
        PodTemplateNotFoundError: if no directory at ``<templates_dir>/<name>``
            or no ``pod.yaml`` inside it.
        PodTemplateError: if ``pod.yaml`` is unreadable or malformed.

    Note: returns the on-disk representation *as-is*. Run
    :func:`pod_templates.validator.validate_pod_template` separately before
    provisioning — the loader does not enforce invariants.
    """
    root = templates_dir or builtin_pod_templates_dir()
    tdir = root / name
    if not tdir.is_dir():
        raise PodTemplateNotFoundError(
            f"pod-template {name!r} not found at {tdir} (searched root: {root})"
        )

    manifest_path = tdir / "pod.yaml"
    if not manifest_path.is_file():
        raise PodTemplateNotFoundError(
            f"pod-template {name!r} has no pod.yaml (expected at {manifest_path})"
        )

    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except OSError as e:
        raise PodTemplateError(f"cannot read {manifest_path}: {e}") from e

    try:
        raw = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as e:
        raise PodTemplateError(f"{manifest_path} is not valid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise PodTemplateError(
            f"{manifest_path} top-level must be a mapping, "
            f"got {type(raw).__name__}"
        )

    return pod_template_from_dict(raw, name=name, path=tdir)


def pod_template_from_dict(
    raw: dict[str, Any],
    *,
    name: str,
    path: Path | None = None,
) -> PodTemplate:
    """Build a :class:`PodTemplate` from an already-parsed ``pod.yaml`` dict.

    Shared by :func:`load_pod_template` (on-disk) and the extractor's
    round-trip helpers (in-memory). Parsing is permissive; structural
    problems are the validator's job, not the loader's.

    The ``name`` argument wins over any ``name`` in ``raw`` so the
    directory name stays authoritative (matching the bot-template loader's
    contract where the dir name is the id).
    """
    schema_version = _coerce_int(
        raw.get("schema_version"), default=POD_TEMPLATE_SCHEMA_VERSION
    )
    display_name = str(raw.get("display_name") or raw.get("name") or name)
    description = str(raw.get("description") or "").strip()

    pod = _parse_pod_seed(raw.get("pod"), manifest=name)
    bots = _parse_bots(raw.get("bots"), manifest=name)

    return PodTemplate(
        name=name,
        path=path,
        schema_version=schema_version,
        display_name=display_name,
        description=description,
        pod=pod,
        bots=tuple(bots),
        raw=raw,
    )


def list_pod_templates(*, templates_dir: Path | None = None) -> list[str]:
    """Return the names of all pod-template directories under ``templates_dir``.

    A directory qualifies iff it contains a ``pod.yaml``. Silently skips
    entries that don't. Returns ``[]`` if the root doesn't exist (a pod with
    no captured templates yet is the common case, not an error).
    """
    root = templates_dir or builtin_pod_templates_dir()
    if not root.is_dir():
        return []
    out: list[str] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and (entry / "pod.yaml").is_file():
            out.append(entry.name)
    return out


def wrap_typed_artifact(
    template: PodTemplate, *, signature: Any = None
) -> dict[str, Any]:
    """Module-level convenience for :meth:`PodTemplate.wrap_typed_artifact`."""
    return template.wrap_typed_artifact(signature=signature)


# ── Internal parsing helpers ────────────────────────────────────────────────────


def _parse_pod_seed(raw: Any, *, manifest: str) -> PodSeed:
    if raw is None:
        return PodSeed()
    if not isinstance(raw, dict):
        raise PodTemplateError(
            f"pod-template {manifest!r}: 'pod' must be a mapping, "
            f"got {type(raw).__name__}"
        )
    release_mode = raw.get("release_mode")
    release_mode = str(release_mode) if release_mode is not None else None

    model_tiers = raw.get("model_tiers") or {}
    if not isinstance(model_tiers, dict):
        raise PodTemplateError(
            f"pod-template {manifest!r}: pod.model_tiers must be a mapping, "
            f"got {type(model_tiers).__name__}"
        )
    integrations = raw.get("integrations") or {}
    if not isinstance(integrations, dict):
        raise PodTemplateError(
            f"pod-template {manifest!r}: pod.integrations must be a mapping, "
            f"got {type(integrations).__name__}"
        )
    # Anything beyond the three known keys is preserved for forward-compat.
    known = {"release_mode", "model_tiers", "integrations"}
    extra = {k: v for k, v in raw.items() if k not in known}
    return PodSeed(
        release_mode=release_mode,
        model_tiers=dict(model_tiers),
        integrations=dict(integrations),
        extra=extra,
    )


def _parse_bots(raw: Any, *, manifest: str) -> list[PodBotSpec]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PodTemplateError(
            f"pod-template {manifest!r}: 'bots' must be a list, "
            f"got {type(raw).__name__}"
        )
    out: list[PodBotSpec] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise PodTemplateError(
                f"pod-template {manifest!r}: bots[{i}] must be a mapping, "
                f"got {type(entry).__name__}"
            )
        bot_id = entry.get("bot_id")
        if not bot_id or not isinstance(bot_id, str):
            raise PodTemplateError(
                f"pod-template {manifest!r}: bots[{i}].bot_id is required (string)"
            )
        bt = entry.get("bot_template")
        bt = str(bt) if bt else None
        channel = entry.get("channel")
        channel = str(channel) if channel else None
        voice = entry.get("voice_preset")
        voice = str(voice) if voice else None
        role = entry.get("role")
        role = str(role) if role else None
        apps_raw = entry.get("installed_apps") or []
        if not isinstance(apps_raw, list):
            raise PodTemplateError(
                f"pod-template {manifest!r}: bots[{i}].installed_apps must be a list"
            )
        overrides = entry.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise PodTemplateError(
                f"pod-template {manifest!r}: bots[{i}].overrides must be a mapping"
            )
        out.append(
            PodBotSpec(
                bot_id=bot_id,
                bot_template=bt,
                channel=channel,
                voice_preset=voice,
                role=role,
                installed_apps=tuple(str(a) for a in apps_raw),
                overrides=dict(overrides),
            )
        )
    return out


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
