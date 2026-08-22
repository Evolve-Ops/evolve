"""app_spec — the v-next App Spec: the portable intent of one app (AL-1.5a).

docs/build-AL-1.5-spec-vnext.md §2; the field list is
docs/design-app-spec-and-discovery-2026-08-15.md §5 and it is **FROZEN** —
fifteen fields, no more. Design §10's scope-creep mitigation is the rule: every
field added to §5 must name which of the four properties (measurable /
gateable / runnable / shareable) needs it, and that is an operator decision,
not a build one. Nothing here adds to the list on its own.

THE TWELFTH FIELD WAS AN OPERATOR DECISION, taken 2026-08-18 on this chip's
evidence and recorded in design §5. ``privacy`` serves **gateable**:
``privacy.shareable_in_lessons`` is what ``lessons_share._spec_allows_lessons_share``
reads off the Spec to decide whether an app's Lessons may be published, and the
census found the block populated on 206 of 227 artifacts on the macOS pod and
all 5 on the Linux pod. Dropping it on migration would have flipped a live
sharing gate from an explicit operator opt-in to unset across the fleet. The
gate is deny-by-default and this module preserves that exactly: an undeclared
block stays ``{}``, never a synthesized ``shareable_in_lessons: false``, so
"not declared" and "declared false" remain the distinct states v24 defined —
they read the same through the gate, and only one of them is an operator
statement.

THE INVOCATION CLUSTER FOLLOWED, 2026-08-18, the same way: ``invocation_mode``
and ``bot_guidance`` serve **runnable** (they are how an app is actually
invoked on a bot — the plugin's ``TurnObserver`` gates Layer-C interception on
``invocation_mode !== "plugin_intercept"``, and ``bot_guidance`` is the
instruction text an ``agent_invokes`` app runs on), and ``permissions`` serves
**gateable** (its five kinds drive ``app_permissions.reconciler`` and the
``app_permission_drift`` Signals against ``exec-approvals.json``). All three
have live readers today, all three are carried verbatim, and none is
default-filled — for ``invocation_mode`` in particular, absent and
``"agent_invokes"`` read identically through the plugin's gate but only one is
a declaration.

WHAT 1.5a IS. A *reader*: ``spec_from_manifest`` migrates a v28/v30 legacy
manifest, a v7-arc Spec or a v7-arc Instance **on read** into an ``AppSpec``,
and ``AppSpec`` round-trips through ``to_dict``/``from_dict`` byte-stably. It
is the design's risk-table mitigation ("migrate on read, write v-next") with
only the first half built.

WHAT 1.5a IS NOT, deliberately. Nothing writes a v-next Spec to disk. No
manifest is rewritten, no dataclass in ``manifest.py`` changes shape, no reader
in the pod is re-pointed at this module. ``evolve-admin application
migrate-specs`` (spec_migration.py) is a CENSUS: it derives an ``AppSpec`` for
every artifact on the pod and reports what does and does not derive cleanly,
which is the readiness evidence 1.5b needs before it points ``record_application``
at this model.

THE SHAPE DISCRIMINATOR IS AN ENVELOPE KEY, NOT A SIXTEENTH §5 FIELD
(AL-1.5b, 2026-08-18). A reader that finds a JSON file in the apps population
has to be able to tell a v-next Spec from a v7-arc one before it decides
whether to migrate it. 1.5a deferred that question because it wrote no Spec to
disk; 1.5b writes one, so it is answered here — and the answer is that the
marker does NOT join design §5's frozen list. §5 is the *portable intent*: what
an app needs to be measurable / gateable / runnable / shareable. "which shape
is this file written in" is a statement about the ARTIFACT, not about the app,
and it is exactly the role ``schema_version`` and ``manifest_shape`` already
play on a manifest (both dispositioned ``instance`` — never ``spec`` — in
``spec_migration.FIELD_DISPOSITION``). So ``SPEC_SHAPE_FIELD`` /
``SPEC_SHAPE_VERSION_FIELD`` are top-level keys the *writer*
(``app_spec_store.write_spec``) wraps around ``to_dict()``'s output, ``SPEC_FIELDS``
stays at fifteen, and ``to_dict()`` still emits exactly those fifteen keys.

The constants and the ``is_vnext_artifact`` predicate live HERE rather than in
``app_spec_store`` for one reason: ``spec_from_manifest`` has to recognise an
already-v-next artifact itself (it must round-trip it, never re-derive it), and
this module is pure — it cannot import the module that owns the disk. The
store imports these; nothing imports the store from here.

IDENTITY COMES FROM ONE PLACE. ``app_id`` is ``app_identity.resolve_app_id``
and nothing else — never a local ``data.get("spec_id") or data.get("id")``
chain, which is exactly the class AL-1.4 spent three PRs removing. A draft
(design §3: discovered, ``draft_id``, no identity) therefore derives an
``AppSpec`` with an empty ``app_id``, and ``validate()`` reports it. That is the
correct answer, not a failure: a draft has no portable intent to share yet.

PURE. No I/O, no clock, no ``shared_dir``. The one input that needs the disk —
the sha256 set for ``package.files`` — is INJECTED (``package_files=``), because
it lives in the files-pack metadata (``gallery/<slug>/files/manifest.json``),
not in the manifest. Callers that have it pass it; callers that do not get
``package.files`` derived from the manifest's own file list with empty shas,
and ``validate()`` names them.
"""

# identity: see applications.app_identity.resolve_app_id — this module is
# already resolver-only by construction; "IDENTITY COMES FROM ONE PLACE"
# above and ``derive_app_spec``'s docstring say so. The two mentions below are
# that prose naming the chain shape AL-1.4 removed, quoted so the rule is
# legible — not reads.

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .app_identity import draft_id_of, resolve_app_id

__all__ = [
    "AppSpec",
    "AUDIENCE_EVERYONE",
    "AUDIENCE_NAMED",
    "AUDIENCE_OWNERS",
    "AUDIENCE_VALUES",
    "DEFAULT_DELIVERS_TO",
    "KIND_BOTH",
    "KIND_ON_REQUEST",
    "KIND_SCHEDULED",
    "KIND_VALUES",
    "ORIGIN_AUTHORED",
    "ORIGIN_DISCOVERED",
    "ORIGIN_IMPORTED",
    "ORIGIN_VALUES",
    "BOT_GUIDANCE_KEYS",
    "INVOCATION_MODES",
    "PACKAGE_STANDARDS",
    "PERMISSION_KEYS",
    "PRIVACY_KEYS",
    "SPEC_FIELDS",
    "SPEC_SHAPE_FIELD",
    "SPEC_SHAPE_VERSION",
    "SPEC_SHAPE_VERSION_FIELD",
    "SPEC_SHAPE_VNEXT",
    "derive_spec_version",
    "is_vnext_artifact",
    "spec_from_manifest",
]

# ── The vocabularies design §5 fixes ─────────────────────────────────────────

KIND_SCHEDULED = "scheduled"
KIND_ON_REQUEST = "on_request"
KIND_BOTH = "both"
KIND_VALUES: tuple[str, ...] = (KIND_SCHEDULED, KIND_ON_REQUEST, KIND_BOTH)

AUDIENCE_EVERYONE = "everyone"
AUDIENCE_OWNERS = "owners"
AUDIENCE_NAMED = "named"
AUDIENCE_VALUES: tuple[str, ...] = (
    AUDIENCE_EVERYONE, AUDIENCE_OWNERS, AUDIENCE_NAMED,
)

ORIGIN_DISCOVERED = "discovered"
ORIGIN_AUTHORED = "authored"
ORIGIN_IMPORTED = "imported"
ORIGIN_VALUES: tuple[str, ...] = (
    ORIGIN_DISCOVERED, ORIGIN_AUTHORED, ORIGIN_IMPORTED,
)

# design §5 "Superset of the standards": the only two values `package.standard`
# may take. Absent means "Evolve-native package, no inner standard".
PACKAGE_STANDARDS: tuple[str, ...] = ("agent-plugins-1.0", "clawhub-bundle")

# design §5 privacy block (v24; mirrors docs/schemas/manifest-v7-spec.schema.json,
# which is additionalProperties:false over exactly these). Carried across
# unchanged rather than re-shaped: lessons_share reads this block off the Spec
# today, so a new shape would be a silent gate change, not a migration.
PRIVACY_KEYS: tuple[str, ...] = (
    "user_data_collected",   # [str]
    "opt_out_command",       # str
    "consent_notice",        # str
    "retention_days",        # int >= 1
    "shareable_in_lessons",  # bool — THE GATE. Absent/false both deny.
)

# v21 invocation_mode enum. Carried as a string, NOT coerced to this set:
# the plugin's gate is ``!== "plugin_intercept"``, so an unrecognised value and
# an absent one behave identically, and blanking it would lose the record of
# what an artifact actually said. ``validate()`` reports anything off-enum.
INVOCATION_MODE_AGENT = "agent_invokes"        # default behaviour when absent
INVOCATION_MODE_INTERCEPT = "plugin_intercept"  # Layer-C structural enforcement
INVOCATION_MODE_SUBAGENT = "subagent"           # reserved (deferred Layer B)
INVOCATION_MODES: tuple[str, ...] = (
    INVOCATION_MODE_AGENT, INVOCATION_MODE_INTERCEPT, INVOCATION_MODE_SUBAGENT,
)

# v21 bot_guidance entry shape. 604 entries across both pods, all exactly
# these two keys.
BOT_GUIDANCE_KEYS: tuple[str, ...] = ("section", "content")

# permissions block kinds, from app_permissions.reconciler._explicit_entries_for_app.
# ``exec`` is ENFORCED today; the other four are advisory in Phase A — but all
# five are carried, because a spec that silently dropped an app's declared
# network or env surface on migration would under-report it to the drift
# monitor. Only ``exec`` appears on either pod today; the rest are schema, and
# carrying them costs nothing.
PERMISSION_KEYS: tuple[str, ...] = (
    "exec", "fs_read", "fs_write", "network_egress", "env",
)
# The reconciler ignores unknown keys "other than the underscore-prefixed
# metadata fields", so ``_note`` is a deliberate part of the shape.
PERMISSION_NOTE_KEY = "_note"

# ── The on-disk shape discriminator (AL-1.5b) ────────────────────────────────
#
# ENVELOPE, NOT §5. These two keys wrap a written Spec; they are not part of
# the portable intent and they are NOT in ``SPEC_FIELDS`` — see the module
# docstring for why that boundary is where it is. ``AppSpec.to_dict()`` never
# emits them and ``AppSpec.from_dict()`` drops them like any unknown key, so
# the model stays exactly fifteen fields either way; only
# ``app_spec_store.write_spec`` adds them and only ``is_vnext_artifact`` reads
# them.
SPEC_SHAPE_FIELD = "spec_shape"
SPEC_SHAPE_VNEXT = "app-spec-v-next"

# Envelope evolution, separate from ``spec_version`` (which is the APP's
# version, monotonic per app_id — design §5) and deliberately NOT named
# ``version`` or ``schema_version``: ``derive_spec_version`` already reads a
# top-level ``version``, and ``schema_version`` means "manifest dataclass
# version" (24-30) everywhere else in this package. Two file families sharing
# one key name with different numbering is how a reader gets confidently wrong.
SPEC_SHAPE_VERSION_FIELD = "spec_shape_version"
SPEC_SHAPE_VERSION = 1


def is_vnext_artifact(data: Any) -> bool:
    """True when ``data`` is already a written v-next Spec.

    Keyed on the discriminator's CONTENT, never on where the file was found —
    a v-next Spec that has been copied, imported from another pod, or handed
    over in a share payload is still v-next, and a legacy manifest that lands
    in the v-next directory by accident is still legacy. The census reports
    the shape it computes here, not the shape its walk expected.
    """
    return isinstance(data, dict) and data.get(SPEC_SHAPE_FIELD) == SPEC_SHAPE_VNEXT


# The frozen field list, in design §5 order. Pinned by a test so a silent
# twelfth field cannot appear without the operator decision §10 requires.
SPEC_FIELDS: tuple[str, ...] = (
    "app_id",
    "spec_version",
    "name",
    "purpose",
    "kind",
    "runs",
    "invocation_mode",
    "bot_guidance",
    "requires",
    "exclusive_tools",
    "audience",
    "privacy",
    "permissions",
    "provenance",
    "package",
)

# `requires` sub-keys (design §5). Fixed set; each is a list of refs.
REQUIRES_KEYS: tuple[str, ...] = ("skills", "tools", "integrations", "secrets")

# design-app-access-2026-08-15 §"Deliveries": "Recipient lists in the spec
# default to `owners`." A user-facing run with no explicit recipient gets this;
# a run that delivers to nobody gets [].
DEFAULT_DELIVERS_TO: tuple[str, ...] = (AUDIENCE_OWNERS,)

# The canonical date-prefixed semver Spec versions are written in
# (YYYY.MM.DD-major.minor). Mirrors migrate_v7.CANONICAL_VERSION_RE — repeated
# rather than imported because migrate_v7 is an 80KB module that imports
# manifest.py, and this one stays pure. Pinned to the original by a test.
_CANONICAL_VERSION_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})-(\d+)\.(\d+)$")

# Sentence terminators for the one-sentence `purpose` trim.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def _s(value: Any) -> str:
    """A stripped string, or "" for anything that is not one."""
    return value.strip() if isinstance(value, str) else ""


def _str_list(value: Any) -> list[str]:
    """Non-empty stripped strings from a list, order-preserving, deduped."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        s = _s(item)
        if s and s not in out:
            out.append(s)
    return out


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _dicts(value: Any) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


@dataclass
class AppSpec:
    """The portable intent of one app — design §5, all eleven fields.

    Field-by-field, and which of §5's four properties each one serves (the
    §10 rule, recorded so a future field addition has a precedent to match):

      app_id          shareable + measurable — the one key attribution,
                      access and sharing agree on (design §3).
      spec_version    shareable — what updates pin. INT here; the v7-arc
                      CalVer string is order-preserved into it by
                      ``derive_spec_version``.
      name            shareable — the human handle.
      purpose         shareable — one sentence; IS the Tier-1 menu line.
      kind            runnable — scheduled / on_request / both.
      runs            runnable — schedule + action + recipients.
      invocation_mode runnable — which invocation path; the plugin's
                      TurnObserver gates Layer-C interception on it.
      bot_guidance    runnable — the AGENTS.md instruction blocks an
                      ``agent_invokes`` app actually runs on.
      requires        runnable — what must exist for an install to work.
      exclusive_tools gateable — what only this app uses.
      audience        gateable — the access default a shared app carries.
      privacy         gateable — the v24 machine-checkable block;
                      ``shareable_in_lessons`` is the live Lessons-sharing
                      gate (operator decision 2026-08-18, design §5).
      permissions     gateable — declared exec / fs / network / env surface;
                      drives exec-approvals drift (operator decision
                      2026-08-18, design §5).
      provenance      shareable — where it came from.
      package         runnable — the sha-verified files an install materializes.
    """

    app_id: str = ""
    spec_version: int = 1
    name: str = ""
    purpose: str = ""
    kind: str = KIND_ON_REQUEST
    runs: list[dict] = field(default_factory=list)
    invocation_mode: str = ""
    bot_guidance: list[dict] = field(default_factory=list)
    requires: dict = field(default_factory=lambda: {k: [] for k in REQUIRES_KEYS})
    exclusive_tools: list[str] = field(default_factory=list)
    audience: str = AUDIENCE_EVERYONE
    privacy: dict = field(default_factory=dict)
    permissions: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=lambda: {"origin": ORIGIN_AUTHORED, "at": ""})
    package: dict = field(default_factory=lambda: {"files": []})

    # ── serialization ────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Emit in design §5 order. Round-trips exactly through ``from_dict``.

        Optional sub-keys (``provenance.from_pod`` / ``from_bot``,
        ``package.standard`` / ``ref``) are omitted when empty rather than
        written as "" — that is what the ``?`` in the design's field list
        means, and it is what keeps the round-trip a fixed point.
        """
        return {
            "app_id": self.app_id,
            "spec_version": self.spec_version,
            "name": self.name,
            "purpose": self.purpose,
            "kind": self.kind,
            "runs": [dict(r) for r in self.runs],
            "invocation_mode": self.invocation_mode,
            "bot_guidance": [dict(b) for b in self.bot_guidance],
            "requires": {k: list(self.requires.get(k, [])) for k in REQUIRES_KEYS},
            "exclusive_tools": list(self.exclusive_tools),
            "audience": self.audience,
            "privacy": dict(self.privacy),
            "permissions": dict(self.permissions),
            "provenance": dict(self.provenance),
            "package": {
                "files": [dict(f) for f in self.package.get("files", [])],
                **({"standard": self.package["standard"]}
                   if _s(self.package.get("standard")) else {}),
                **({"ref": self.package["ref"]}
                   if _s(self.package.get("ref")) else {}),
            },
        }

    @classmethod
    def from_dict(cls, data: Any) -> "AppSpec":
        """Rebuild from ``to_dict`` output. Unknown keys are dropped.

        Tolerant on the way in (a hand-edited or partial spec still loads),
        strict on the way out — normalization happens here, so
        ``from_dict(to_dict(x)) == x`` for anything this module produced.
        """
        d = _dict(data)
        requires_in = _dict(d.get("requires"))
        package_in = _dict(d.get("package"))
        spec_version = d.get("spec_version")
        return cls(
            app_id=_s(d.get("app_id")),
            spec_version=(spec_version
                          if isinstance(spec_version, int)
                          and not isinstance(spec_version, bool)
                          and spec_version >= 1 else 1),
            name=_s(d.get("name")),
            purpose=_s(d.get("purpose")),
            kind=_s(d.get("kind")) if _s(d.get("kind")) in KIND_VALUES else KIND_ON_REQUEST,
            runs=[_normalize_run(r) for r in _dicts(d.get("runs"))],
            invocation_mode=_s(d.get("invocation_mode")),
            bot_guidance=_normalize_bot_guidance(d.get("bot_guidance")),
            requires={k: _str_list(requires_in.get(k)) for k in REQUIRES_KEYS},
            exclusive_tools=_str_list(d.get("exclusive_tools")),
            audience=(_s(d.get("audience"))
                      if _s(d.get("audience")) in AUDIENCE_VALUES else AUDIENCE_EVERYONE),
            privacy=_normalize_privacy(d.get("privacy")),
            permissions=_normalize_permissions(d.get("permissions")),
            provenance=_normalize_provenance(d.get("provenance")),
            package=_normalize_package(package_in.get("files"),
                                       standard=package_in.get("standard"),
                                       ref=package_in.get("ref")),
        )

    # ── validation ───────────────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """Problems with this spec, as operator-readable strings. [] = clean.

        Reports rather than raises: the census has to be able to say "this
        artifact derives, but incompletely" for every artifact on the pod, and
        an exception on the first bad one would end the census at row 1.
        """
        problems: list[str] = []
        if not self.app_id:
            problems.append("app_id is empty (a discovered draft has no identity "
                            "to confer — design §3)")
        if not isinstance(self.spec_version, int) or self.spec_version < 1:
            problems.append("spec_version must be an int >= 1")
        if not self.name:
            problems.append("name is empty")
        if not self.purpose:
            problems.append("purpose is empty (it IS the Tier-1 menu line)")
        if self.kind not in KIND_VALUES:
            problems.append(f"kind {self.kind!r} not in {KIND_VALUES}")
        if self.audience not in AUDIENCE_VALUES:
            problems.append(f"audience {self.audience!r} not in {AUDIENCE_VALUES}")
        if self.kind in (KIND_SCHEDULED, KIND_BOTH) and not self.runs:
            problems.append(f"kind is {self.kind!r} but runs[] is empty")
        for i, run in enumerate(self.runs):
            if not _s(run.get("action")):
                problems.append(f"runs[{i}] has no action")
        origin = _s(self.provenance.get("origin"))
        if origin not in ORIGIN_VALUES:
            problems.append(f"provenance.origin {origin!r} not in {ORIGIN_VALUES}")
        if self.invocation_mode and self.invocation_mode not in INVOCATION_MODES:
            problems.append(
                f"invocation_mode {self.invocation_mode!r} not in "
                f"{INVOCATION_MODES} (the plugin treats anything that is not "
                f"'plugin_intercept' as 'agent_invokes')"
            )
        for i, block in enumerate(self.bot_guidance):
            if not _s(block.get("content")):
                problems.append(f"bot_guidance[{i}] has no content")
        retention = self.privacy.get("retention_days")
        if retention is not None and (
            not isinstance(retention, int) or isinstance(retention, bool)
            or retention < 1
        ):
            problems.append("privacy.retention_days must be an int >= 1")
        share = self.privacy.get("shareable_in_lessons")
        if share is not None and not isinstance(share, bool):
            problems.append("privacy.shareable_in_lessons must be a bool")
        standard = _s(self.package.get("standard"))
        if standard and standard not in PACKAGE_STANDARDS:
            problems.append(f"package.standard {standard!r} not in {PACKAGE_STANDARDS}")
        files = self.package.get("files", [])
        if not files:
            # "clean" must not mean "nothing to check". design §6 makes
            # install = materialize package.files, so an app with none has
            # nothing to install — and design §9's determinism proof (two
            # bots, identical sha sets) passes VACUOUSLY on an empty set.
            # Reported for every spec-bearing artifact; an instruction-only
            # app carries it as a known-partial rather than a false clean.
            problems.append(
                "package.files is empty — nothing for a deterministic "
                "install to materialize (design §6)"
            )
        unsha = [f["path"] for f in files if not _s(f.get("sha256"))]
        if unsha:
            problems.append(
                f"{len(unsha)} package file(s) carry no sha256 — deterministic "
                f"install (design §6) cannot verify them: {', '.join(unsha[:3])}"
                + ("…" if len(unsha) > 3 else "")
            )
        return problems


# ── normalizers (shared by from_dict and the migrate-on-read path) ───────────

def _normalize_run(raw: Any) -> dict:
    """One ``runs[]`` entry: ``{schedule, action, delivers_to}``, fixed keys."""
    d = _dict(raw)
    return {
        "schedule": _s(d.get("schedule")),
        "action": _s(d.get("action")),
        "delivers_to": _str_list(d.get("delivers_to")),
    }


def _normalize_bot_guidance(raw: Any) -> list[dict]:
    """v21 ``bot_guidance`` entries: ``[{section, content}]``, fixed keys.

    Every one of the 604 entries across both pods carries exactly these two,
    so the shape is observed rather than assumed. An entry with neither is
    dropped (it splices nothing into AGENTS.md); an entry with only one is
    KEPT and ``validate()`` names it, because losing an operator's text is
    worse than reporting it.
    """
    out: list[dict] = []
    for entry in _dicts(raw):
        block = {k: _s(entry.get(k)) for k in BOT_GUIDANCE_KEYS}
        if any(block.values()):
            out.append(block)
    return out


def _normalize_permissions(raw: Any) -> dict:
    """The declared permission surface: five list-valued kinds + ``_note``.

    Declared keys only, same rule as ``privacy`` — an app that declared
    nothing must not acquire an empty-but-present surface from a migration,
    because ``app_manifest_monitor`` distinguishes "declares nothing" from
    "declares an empty list" when it reports ``allowed_not_declared`` drift.
    """
    d = _dict(raw)
    out: dict[str, Any] = {}
    for key in PERMISSION_KEYS:
        if key in d:
            out[key] = _str_list(d[key])
    if _s(d.get(PERMISSION_NOTE_KEY)):
        out[PERMISSION_NOTE_KEY] = _s(d[PERMISSION_NOTE_KEY])
    return out


def _normalize_privacy(raw: Any) -> dict:
    """The v24 privacy block, declared keys only.

    DELIBERATELY NOT DEFAULT-FILLED. ``{}`` means "not declared" and a missing
    ``shareable_in_lessons`` means the same thing to the gate as an explicit
    ``false`` — but only one of them is a statement an operator made, and v24
    defined those as distinct states. Synthesizing the false would erase that
    distinction on every artifact on the pod in a single migration, which is a
    bigger change than the one being asked for.

    Unknown keys are dropped: the v7 Spec schema is
    ``additionalProperties: false`` over exactly ``PRIVACY_KEYS``, so carrying
    a stray key would produce a Spec that fails its own schema.
    """
    d = _dict(raw)
    out: dict[str, Any] = {}
    for key in PRIVACY_KEYS:
        if key in d and d[key] is not None:
            out[key] = d[key]
    if isinstance(out.get("user_data_collected"), list):
        out["user_data_collected"] = _str_list(out["user_data_collected"])
    for key in ("opt_out_command", "consent_notice"):
        if key in out:
            out[key] = _s(out[key])
    return out


def _normalize_provenance(raw: Any) -> dict:
    """``{origin, from_pod?, from_bot?, at}`` — optionals omitted when empty."""
    d = _dict(raw)
    origin = _s(d.get("origin"))
    out: dict[str, Any] = {
        "origin": origin if origin in ORIGIN_VALUES else ORIGIN_AUTHORED,
    }
    if _s(d.get("from_pod")):
        out["from_pod"] = _s(d.get("from_pod"))
    if _s(d.get("from_bot")):
        out["from_bot"] = _s(d.get("from_bot"))
    out["at"] = _s(d.get("at"))
    return out


def _normalize_package(files: Any, *, standard: Any = "", ref: Any = "") -> dict:
    """``{files: [{path, sha256, role}], standard?, ref?}``, fixed sub-keys."""
    out_files: list[dict] = []
    seen: set[str] = set()
    for entry in _dicts(files):
        path = _s(entry.get("path"))
        if not path or path in seen:
            continue
        seen.add(path)
        out_files.append({
            "path": path,
            "sha256": _s(entry.get("sha256")).lower(),
            "role": _s(entry.get("role")),
        })
    pkg: dict[str, Any] = {"files": out_files}
    std = _s(standard)
    if std:
        pkg["standard"] = std
    r = _s(ref)
    if r:
        pkg["ref"] = r
    return pkg


# ── migrate-on-read: v28/v30 manifest | v7-arc Spec | v7-arc Instance → AppSpec ──

def derive_spec_version(data: dict) -> int:
    """The v-next integer ``spec_version`` for a legacy artifact.

    THE ONE PIECE OF ARITHMETIC THE FROZEN FIELD LIST FORCES, so it is spelled
    out rather than buried. Design §5 fixes ``spec_version`` as an **int,
    monotonic per app_id**; every version string on the pod today is the
    canonical date-prefixed semver ``YYYY.MM.DD-major.minor``
    (``native_write.mint_spec_version``). The map has to preserve order,
    because a shared app pins a version and an update must compare greater:

        int(YYYYMMDD) * 100 + min(major * 10 + minor, 99)

    so ``2026.05.20-1.0`` → ``2026052010`` and ``2026.06.01-1.0`` →
    ``2026060110``. The date leads because on this pod the date is the part
    that moves — ``mint_spec_version`` always mints ``-1.0`` and a republish
    carries a new date. The tail is clamped at 99, so ordering degrades above
    ``major.minor`` 9.9 on a single day; nothing on the pod is near it, and
    ``migrate-specs`` reports the legacy string alongside the int so the
    original is never lost.

    Fallback order: an already-int ``spec_version`` wins (a v-next artifact
    round-trips unchanged), then the canonical strings, then the legacy
    integer ``version``, then 1.
    """
    for key in ("spec_version", "version"):
        val = data.get(key)
        if isinstance(val, int) and not isinstance(val, bool) and val >= 1:
            return val
    candidates = [
        _s(data.get("spec_version")),
        _s(_dict(data.get("provenance")).get("spec_version")),
        _s(data.get("pkg_version")),
        _s(data.get("gallery_version")),
    ]
    for raw in candidates:
        m = _CANONICAL_VERSION_RE.match(raw)
        if m:
            y, mo, d, major, minor = (int(g) for g in m.groups())
            return (y * 10000 + mo * 100 + d) * 100 + min(major * 10 + minor, 99)
    return 1


def _derive_name(data: dict, app_id: str) -> str:
    """``name`` — the human handle. Falls back to the slug, never to "" when
    an identity exists (an unnamed app in the Tier-1 menu is unusable)."""
    for key in ("name", "display_name"):
        val = _s(data.get(key))
        if val:
            return val
    return app_id.replace("-", " ").title() if app_id else ""


def _first_sentence(text: str) -> str:
    """First sentence of ``text``. ``purpose`` IS the Tier-1 menu line
    (design §5), and the v13 ``purpose`` field is documented as 2-3
    sentences, so the trim is the migration — not a display concern."""
    text = " ".join(text.split())
    if not text:
        return ""
    return _SENTENCE_END_RE.split(text, maxsplit=1)[0].strip()


def _derive_purpose(data: dict) -> str:
    """``purpose`` — one sentence, from the richest source that has one.

    ``identity.purpose`` first: the v7-arc Spec schema calls the identity
    block "the safest place for richer prose" and it is where an operator's
    authored wording lives. ``objective`` is the v7 canonical replacement and
    comes in two shapes — a ``{primary}`` dict on a Spec, a bare string on a
    legacy v4 manifest — so both are read.
    """
    sources = [
        _s(_dict(data.get("identity")).get("purpose")),
        _s(data.get("purpose")),
        _s(_dict(data.get("objective")).get("primary")),
        _s(data.get("objective")),
        _s(data.get("description")),
    ]
    for src in sources:
        if src:
            return _first_sentence(src)
    return ""


def _delivers_to(action: dict) -> list[str]:
    """Recipients for one run.

    ``delivery_contract.user_facing`` is authoritative when declared (v23).
    When it is not, this mirrors the delivery monitor's documented Option-A
    default — "user-facing iff outputs[] declares a channel-kind output" —
    approximated as "outputs[] is non-empty", which is generous in the
    delivering direction. Nothing enforces recipients yet (that resolver is
    AL-4.1, and it reads the roster), so a generous default costs a
    to-be-corrected line in the census, not an access decision.
    """
    contract = _dict(action.get("delivery_contract"))
    user_facing = contract.get("user_facing")
    if isinstance(user_facing, bool):
        return list(DEFAULT_DELIVERS_TO) if user_facing else []
    outputs = action.get("outputs")
    if isinstance(outputs, list) and outputs:
        return list(DEFAULT_DELIVERS_TO)
    return []


def _run_action_text(action: dict) -> str:
    """What a scheduled action actually does — the command when there is one,
    else the natural-language instruction, else its own summary/id.

    design §5 types ``action`` as ``script | instruction``: both shapes are
    legitimate, and which one an app uses is exactly the ``mechanism``
    distinction (``launchd``/``crontab`` carry a command; the
    ``oc_*_instruction`` mechanisms carry prose in ``install.body``).
    """
    install = _dict(action.get("install"))
    for val in (install.get("command"), action.get("script"),
                install.get("body"), action.get("summary"),
                action.get("description"), action.get("name"),
                action.get("id")):
        s = _s(val)
        if s:
            return s
    return ""


def _derive_runs(data: dict) -> list[dict]:
    """``runs[]`` from every schedule surface a legacy artifact can carry.

    Three sources, in descending fidelity, deduped on (schedule, action):
    the v7-arc Spec's ``schedules[]``, ``scheduled_actions[]`` (v13+, the
    structured record with the install recipe and the delivery contract), and
    OC-native ``crons[]`` (v5+ dicts; v4 raw crontab lines).
    """
    runs: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(schedule: str, action: str, delivers_to: list[str]) -> None:
        if not action:
            return
        key = (schedule, action)
        if key in seen:
            return
        seen.add(key)
        runs.append({"schedule": schedule, "action": action,
                     "delivers_to": delivers_to})

    for sched in _dicts(data.get("schedules")):
        _add(_s(sched.get("cron_default")) or _s(sched.get("cron_intent")),
             _s(sched.get("invokes")) or _s(sched.get("id")), [])

    for action in _dicts(data.get("scheduled_actions")):
        trigger = _dict(action.get("trigger"))
        _add(_s(trigger.get("schedule")) or _s(trigger.get("kind")),
             _run_action_text(action), _delivers_to(action))

    crons = data.get("crons")
    if isinstance(crons, list):
        for entry in crons:
            if isinstance(entry, dict):
                _add(_s(entry.get("schedule")),
                     _s(entry.get("script")) or _s(entry.get("task"))
                     or _s(entry.get("label")) or _s(entry.get("name")), [])
            elif isinstance(entry, str) and entry.strip():
                # v4 raw crontab line: "0 2 * * * /path/script.py" — the first
                # five whitespace fields are the schedule, the rest the command.
                parts = entry.split(maxsplit=5)
                if len(parts) == 6:
                    _add(" ".join(parts[:5]), parts[5], [])
                else:
                    _add("", entry.strip(), [])
    return runs


def _derive_kind(data: dict, runs: list[dict]) -> str:
    """``kind`` — scheduled / on_request / both.

    Scheduled evidence is any derived run, any heartbeat/cron evidence block,
    or a scanner ``usage.model`` of "scheduled" (the scanner's own inferrer,
    ``scanner._infer_usage_model``, is the calibrated judge of this and is
    reused rather than re-litigated). Request evidence is any surface a person
    or another app can invoke through: example triggers, event triggers, bot
    guidance, or a non-scheduled ``usage.model``.

    An app with neither reads as ``on_request``: design §2's sentence is "a
    named thing your bot does for you", and something with no schedule is
    something you ask for.
    """
    usage_model = _s(_dict(data.get("usage")).get("model"))
    scheduled = bool(
        runs or _dict(data.get("heartbeat_evidence"))
        or _dict(data.get("cron_evidence")) or usage_model == "scheduled"
    )
    on_request = bool(
        _str_list(data.get("example_triggers"))
        or _dicts(data.get("event_triggers"))
        or (isinstance(data.get("bot_guidance"), list) and data["bot_guidance"])
        or (usage_model and usage_model != "scheduled")
    )
    if scheduled and on_request:
        return KIND_BOTH
    if scheduled:
        return KIND_SCHEDULED
    return KIND_ON_REQUEST


def _dep_names(entries: Any, keys: tuple[str, ...]) -> list[str]:
    """Names out of a v7 ``dependencies`` list — each sub-shape has its own
    id key (``skill_id``, ``integration_id``, ``name``…), and a bare string
    is accepted because legacy ``requirements`` lists are flat."""
    out: list[str] = []
    if not isinstance(entries, list):
        return out
    for entry in entries:
        name = ""
        if isinstance(entry, str):
            name = entry.strip()
        elif isinstance(entry, dict):
            for key in keys:
                name = _s(entry.get(key))
                if name:
                    break
        if name and name not in out:
            out.append(name)
    return out


def _derive_requires(data: dict) -> dict:
    """``requires`` — {skills, tools, integrations, secrets}.

    Two source shapes. The v7-arc ``dependencies`` block is the rich one
    (``oc_skills`` / ``integrations`` / ``credentials``); the legacy v6
    ``requirements`` block carries flat ``integrations`` / ``secrets`` lists.
    Both are read and unioned, so an Instance that carries one and a Spec that
    carries the other derive the same answer.

    ``tools`` comes from ``provided_capabilities[].requires_mcp_tools`` (v29)
    — the app-declared MCP tools, which is the only tool vocabulary a manifest
    has. ``python_packages`` / ``system_packages`` have no §5 home and are
    dropped here rather than smuggled into ``tools``; ``migrate-specs``
    reports the drop per artifact.
    """
    deps = _dict(data.get("dependencies"))
    reqs = _dict(data.get("requirements"))
    skills = _dep_names(deps.get("oc_skills"), ("skill_id", "name", "id"))
    integrations = _dep_names(deps.get("integrations"),
                              ("integration_id", "name", "id"))
    integrations += [i for i in _dep_names(reqs.get("integrations"), ("name", "id"))
                     if i not in integrations]
    secrets = _dep_names(deps.get("credentials"), ("name", "id"))
    secrets += [s for s in _dep_names(reqs.get("secrets"), ("name", "id"))
                if s not in secrets]
    tools: list[str] = []
    for cap in _dicts(data.get("provided_capabilities")):
        for tool in _str_list(cap.get("requires_mcp_tools")):
            if tool not in tools:
                tools.append(tool)
    return {"skills": skills, "tools": tools,
            "integrations": integrations, "secrets": secrets}


def _derive_audience(data: dict, runs: list[dict]) -> str:
    """``audience`` — the access default a shared app carries.

    A declared v24 ``audience_scoping.operator`` is authoritative and maps
    straight across (``operator_only`` → owners, ``named_users`` → named,
    ``open`` → everyone). Undeclared falls to design §7.2's promotion rule:
    ``owners`` for anything with deliveries, ``everyone`` otherwise.

    KNOWN TENSION, recorded rather than resolved here: the access sibling
    (design-app-access §"The three concepts") calls ``everyone`` the default
    "(today's behavior)", while spec §7.2 makes a delivering app ``owners`` at
    promotion. This follows §7.2 — it is the parent design for conferring a
    spec, and it is the tighter of the two. Nothing enforces the field yet
    (AL-4.1 builds the resolver), so the choice is legible and cheap to flip.
    """
    operator = _s(_dict(data.get("audience_scoping")).get("operator"))
    mapped = {"operator_only": AUDIENCE_OWNERS,
              "named_users": AUDIENCE_NAMED,
              "open": AUDIENCE_EVERYONE}.get(operator)
    if mapped:
        return mapped
    delivers = any(run.get("delivers_to") for run in runs)
    return AUDIENCE_OWNERS if delivers else AUDIENCE_EVERYONE


def _derive_provenance(data: dict) -> dict:
    """``provenance`` — {origin, from_pod?, from_bot?, at}.

    ``origin`` reads the existing axes rather than inventing one:
    ``definition_status == discovered`` (v27) or a scanner ``source`` means
    the scanner found it → ``discovered``; a ``source`` block (set on share)
    or a gallery-install source means it arrived from elsewhere →
    ``imported``; everything else is ``authored``, which is what
    ``born_definition_status``'s authored set (user_created / bot_created /
    forge_built / gallery_installed) already means.

    ``imported`` is tested before ``discovered`` for the same reason the
    census tests ``draft_id`` before the legacy chain: an imported app that a
    later scan re-found still came from another pod, and that is the fact
    worth carrying.
    """
    source_block = _dict(data.get("source"))
    prov_in = _dict(data.get("provenance"))
    prov_source = _dict(prov_in.get("source"))
    source = _s(data.get("source")) if isinstance(data.get("source"), str) else ""

    from_pod = (_s(source_block.get("pod_id")) or _s(prov_source.get("pod_id"))
                or _s(prov_in.get("source_pod_id")))
    from_bot = (_s(source_block.get("bot_id")) or _s(prov_source.get("bot_id"))
                or _s(prov_in.get("source_bot_id")))

    if from_pod or from_bot or source == "gallery_installed":
        origin = ORIGIN_IMPORTED
    elif (_s(data.get("definition_status")) == "discovered"
          or source in ("discovered", "scanner", "scan")):
        origin = ORIGIN_DISCOVERED
    else:
        origin = ORIGIN_AUTHORED

    at = (_s(source_block.get("shared_at")) or _s(data.get("created_at"))
          or _s(prov_in.get("created_at")))
    out: dict[str, Any] = {"origin": origin}
    if from_pod:
        out["from_pod"] = from_pod
    if from_bot:
        out["from_bot"] = from_bot
    out["at"] = at
    return out


def _package_role(entry: dict) -> str:
    """``package.files[].role`` — the blueprint role vocabulary
    (``vital_to_blueprint`` / ``instance_specific`` / ``reference_only``) when
    the artifact carries it, else the manifest file entry's own ``purpose``."""
    for key in ("role", "purpose", "marker_state"):
        val = _s(entry.get(key))
        if val:
            return val
    return ""


def _derive_package(data: dict, package_files: Any) -> dict:
    """``package`` — {files: [{path, sha256, role}], standard?, ref?}.

    THERE ARE TWO sha256 CARRIERS, not one. The files-pack metadata
    (``gallery/<slug>/files/manifest.json``) is the one design §6 names, and
    ``package_files`` — injected, because reading it needs a disk — wins whole
    when supplied: where a pack exists it IS the package. But a manifest's own
    ``files[]`` entries can also carry an inline ``sha256`` (some forge write
    paths stamp one; the scanner never does), so the fallback below reads it
    off each entry rather than assuming empty. Live-pod evidence 2026-08-18:
    the single fully sha-verified artifact across both pods got its shas this
    way, from a forge-written manifest, with no files-pack anywhere. Whatever
    is missing after both carriers, ``validate()`` reports as the
    deterministic-install gap it is.

    ``standard`` / ``ref`` have no legacy carrier — no manifest on the pod
    declares an inner Agent Plugins 1.0 or ClawHub package — so they are
    omitted, not guessed.
    """
    if package_files is not None:
        return _normalize_package([
            {"path": _s(f.get("path")), "sha256": _s(f.get("sha256")),
             "role": _package_role(f)}
            for f in _dicts(package_files)
        ])
    entries: list[dict] = []
    for raw in (data.get("realized_files") or data.get("files") or []):
        if isinstance(raw, dict):
            entries.append({"path": _s(raw.get("path")),
                            "sha256": _s(raw.get("sha256")),
                            "role": _package_role(raw)})
        elif isinstance(raw, str) and raw.strip():
            entries.append({"path": raw.strip(), "sha256": "", "role": ""})
    # A v7-arc Spec has no realized files — it is the recipe. Its blueprint
    # names each file logically, so the logical name (or the suggested
    # location when it has one) is the best path available. _normalize_package
    # dedupes by path, so a Spec that ALSO carries a file list keeps the
    # realized entry and its role.
    for f in _dicts(_dict(data.get("blueprint")).get("files")):
        path = _s(f.get("expected_location")) or _s(f.get("logical_name"))
        if path:
            entries.append({"path": path, "sha256": "", "role": _s(f.get("role"))})
    return _normalize_package(entries)


def spec_from_manifest(data: Any, *, package_files: Any = None) -> AppSpec:
    """Migrate any legacy artifact to a v-next ``AppSpec`` **on read**.

    Accepts all three shapes the pod carries — a v28/v30 legacy manifest, a
    v7-arc Spec, a v7-arc Instance — because the fields they share are the
    ones §5 needs, and the ones they do not share are read from whichever
    carrier is present. Nothing is written; the caller owns the result.

    ``app_id`` is ``app_identity.resolve_app_id(data)`` and nothing else — no
    local ``spec_id or id`` chain, ever. An artifact carrying a ``draft_id``
    is the one case the resolver is not asked: it derives with ``app_id == ""``
    and ``validate()`` says so, because design §3 confers identity at
    promotion and a reader that quietly minted one here would re-open exactly
    the churn AL-1.4 closed.

    ``package_files`` is the injected files-pack file list (``path`` +
    ``sha256`` per entry). Omit it and ``package.files`` comes from the
    artifact's own list with empty shas.

    AN ALREADY-V-NEXT ARTIFACT ROUND-TRIPS, IT DOES NOT RE-DERIVE (AL-1.5b).
    Every derivation below reads a LEGACY carrier — ``runs`` from
    ``schedules``/``scheduled_actions``/``crons``, ``kind`` from
    ``heartbeat_evidence``/``usage``/``example_triggers``, ``requires`` from
    ``dependencies``/``requirements``, ``audience`` from ``audience_scoping``.
    A v-next Spec carries none of those: it carries ``runs``, ``kind``,
    ``requires`` and ``audience`` directly. Re-deriving one would therefore not
    degrade gracefully, it would ZERO the fields — a spec written with
    ``kind: "scheduled"`` and two runs would read back ``on_request`` with
    none. So the discriminator is checked first and the artifact is rebuilt
    through ``from_dict``, which is the same normalizer ``to_dict`` round-trips
    against. ``derive_spec_version`` already had this property for its own
    field (an int is returned untouched); this extends it to the other
    fourteen.

    ``package_files`` is IGNORED for a v-next artifact, deliberately: its
    ``package`` block is the authoritative one that was written, and injecting
    a files-pack over it would be re-derivation by another name.
    """
    d = _dict(data)
    # The draft rule outranks the shape. It is checked FIRST for both paths,
    # not just the derive path: design §3 says a discovered draft has no
    # portable identity, and that is a statement about the APP, not about
    # which file format the artifact happens to be in. ``write_spec`` refuses
    # to produce such a file, so the only way to see one is a hand-edit or a
    # future writer's bug — which is exactly when a reader must not quietly
    # confer the identity the draft was denied.
    is_draft = bool(draft_id_of(d))
    if is_vnext_artifact(d):
        spec = AppSpec.from_dict(d)
        if is_draft:
            spec.app_id = ""
        return spec
    # A draft still carries a legacy ``id`` — it is the filename stem — so the
    # draft check has to come BEFORE consulting the resolver, exactly as
    # ``id_migration._classify`` does it, or every draft on the pod would
    # "helpfully" derive a portable identity it was deliberately denied.
    # ``draft_id`` is the positive record that a mint declined to confer one;
    # ``definition_status`` is NOT usable here (the v27 migration landed every
    # pre-existing manifest at ``discovered``).
    app_id = "" if is_draft else resolve_app_id(d)
    runs = _derive_runs(d)
    return AppSpec(
        app_id=app_id,
        spec_version=derive_spec_version(d),
        name=_derive_name(d, app_id),
        purpose=_derive_purpose(d),
        kind=_derive_kind(d, runs),
        runs=runs,
        # Carried verbatim, never default-filled. Absent ``invocation_mode``
        # and ``"agent_invokes"`` read identically through the plugin's gate
        # (``!== "plugin_intercept"``) but only one is a declaration, and the
        # value is carried as-is rather than coerced to the enum so an
        # off-enum artifact keeps saying what it said (validate() reports it).
        invocation_mode=_s(d.get("invocation_mode")),
        bot_guidance=_normalize_bot_guidance(d.get("bot_guidance")),
        requires=_derive_requires(d),
        # design §5 gives `exclusive_tools` no legacy carrier — no manifest
        # field records "only this app uses X". It stays empty on migration
        # and is authored (or inferred by forge at promotion) later; the
        # census counts how many artifacts land here so the gap is measured
        # rather than assumed away.
        exclusive_tools=[],
        audience=_derive_audience(d, runs),
        # Carried straight across from the v24 block on whichever shape the
        # artifact is — legacy manifest, v7-arc Spec and v7-arc Instance all
        # spell it ``privacy`` with the same five keys. Nothing is inferred:
        # an app that never declared a privacy posture must not acquire one
        # from a migration.
        privacy=_normalize_privacy(d.get("privacy")),
        permissions=_normalize_permissions(d.get("permissions")),
        provenance=_derive_provenance(d),
        package=_derive_package(d, package_files),
    )
