"""oc_introspection.py — Read OC's bundled JS files for tier-currency signals.

Why this exists
===============

For two years Evolve's "model freshness" check compared bot tier configs
against a hand-curated Python literal (``model_registry.RECOMMENDED``).
That literal ages out the moment any provider ships a new model — and
the symptom isn't a stale-but-harmless advisory, it's a feedback loop
where Evolve's reconcile button keeps re-adding models that OC's
``legacy-config-migrations`` then silently prunes again.

OC already knows what's current and what's retired. It just doesn't
expose the data as a CLI surface — only as JavaScript that ships in
``/opt/homebrew/lib/node_modules/openclaw/dist/``. This module reaches
into those bundled files and extracts the three signals we care about:

  - ``DEFAULT_MODEL_ALIASES`` (io-*.js) — OC's tier hints. ``opus``
    points at Anthropic's flagship, ``gpt-mini`` at OpenAI's small/fast
    workhorse, etc. OC keeps this current per release.

  - ``OPENAI_DEFAULT_MODEL`` / ``GOOGLE_GEMINI_DEFAULT_MODEL`` etc.
    (default-models-*.js) — the concrete model OC would pick if the
    operator hadn't.

  - ``upgradeRetiredOpenAiModelId`` / ``upgradeOldClaudeToken`` /
    ``upgradeRetiredXaiModelId`` / ``upgradeRetiredGroqModelId``
    (legacy-config-migrations-*.js) — OC's retirement map.
    Mostly literal ``old → new`` pairs, easy to extract via regex.

The parsers are deliberately tolerant: OC's dist file names carry
content hashes that change every release, so we glob by prefix and
match by structural pattern, not literal filename. A parse failure
returns empty data — the resolver gracefully falls back to the static
RECOMMENDED dict so a missing OC install or an unexpected refactor in a
future OC version degrades to today's behavior rather than crashing
the freshness check.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


# ── Path discovery ─────────────────────────────────────────────────────────


# Common install paths OC ends up at via npm global install. Checked in
# order; first one that exists wins. Overridable via OPENCLAW_DIST_DIR.
_DEFAULT_DIST_CANDIDATES: tuple[str, ...] = (
    "/opt/homebrew/lib/node_modules/openclaw/dist",     # Apple Silicon brew
    "/usr/local/lib/node_modules/openclaw/dist",        # Intel brew + linuxbrew
    "/usr/lib/node_modules/openclaw/dist",              # native package managers
)


def find_oc_dist_dir() -> Path | None:
    """Locate OC's bundled ``dist/`` directory.

    Returns the first existing directory in:
      1. ``$OPENCLAW_DIST_DIR`` (env override)
      2. ``/opt/homebrew/lib/node_modules/openclaw/dist``
      3. ``/usr/local/lib/node_modules/openclaw/dist``
      4. ``/usr/lib/node_modules/openclaw/dist``

    Returns ``None`` if none exist. Callers should treat ``None`` as
    "OC not installed locally" and degrade gracefully.
    """
    env_override = os.environ.get("OPENCLAW_DIST_DIR")
    if env_override:
        p = Path(env_override)
        if p.is_dir():
            return p
        return None

    for candidate in _DEFAULT_DIST_CANDIDATES:
        p = Path(candidate)
        if p.is_dir():
            return p
    return None


def _glob_first_with_marker(dist: Path, prefix: str, marker: str) -> Path | None:
    """Find a ``dist/<prefix>-*.js`` file whose body contains ``marker``.

    OC's dist files all carry a Rollup content hash in their name
    (``io-AmXZn-TT.js``). The hash changes every release. We match on a
    prefix and verify by looking for a structural marker inside the file
    — that way a future OC refactor that splits or renames a file fails
    obviously (returns None → graceful degrade) rather than silently
    parsing the wrong file.
    """
    for candidate in sorted(dist.glob(f"{prefix}-*.js")):
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if marker in text:
            return candidate
    return None


# ── Parsers ────────────────────────────────────────────────────────────────


# DEFAULT_MODEL_ALIASES = {
#   opus: "anthropic/claude-opus-4-8",
#   "gpt-mini": "openai/gpt-5.4-mini",
#   ...
# }
# Captures `<alias-key>: "<model-id>"` lines inside the block.
_ALIAS_BLOCK_RE = re.compile(
    r"const\s+DEFAULT_MODEL_ALIASES\s*=\s*\{([^}]*)\}",
    re.DOTALL,
)
_ALIAS_ENTRY_RE = re.compile(
    r'["]?([A-Za-z][\w\-]*)["]?\s*:\s*"([^"]+)"',
)


def load_alias_map(dist: Path) -> dict[str, str]:
    """Extract OC's ``DEFAULT_MODEL_ALIASES`` map from io-*.js.

    Returns ``{alias_token: full_model_id}``. Empty dict on failure
    (file not found, no match, parse error) — callers should treat
    empty as "no OC alias guidance available."
    """
    src = _glob_first_with_marker(dist, "io", "DEFAULT_MODEL_ALIASES")
    if not src:
        return {}
    try:
        text = src.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    block_match = _ALIAS_BLOCK_RE.search(text)
    if not block_match:
        return {}
    body = block_match.group(1)
    return {alias: model for alias, model in _ALIAS_ENTRY_RE.findall(body)}


# const OPENAI_DEFAULT_MODEL = "openai/gpt-5.5";
# const GOOGLE_GEMINI_DEFAULT_MODEL = "google/gemini-3.1-pro-preview";
# Captures one constant per match.
_DEFAULT_CONST_RE = re.compile(
    r'const\s+([A-Z][A-Z_]+)_DEFAULT_MODEL\s*=\s*"([^"]+)"',
)


# Maps OC's constant prefix to the canonical provider id.
_CONST_PREFIX_TO_PROVIDER: dict[str, str] = {
    "OPENAI": "openai",
    "GOOGLE_GEMINI": "google",
    "ANTHROPIC": "anthropic",
    "XAI": "xai",
    "GROK": "xai",
}


def load_default_constants(dist: Path) -> dict[str, str]:
    """Extract per-provider ``*_DEFAULT_MODEL`` constants from default-models-*.js.

    Returns ``{provider_id: full_model_id}``. Constants whose prefix we
    don't recognize (e.g., ``OLLAMA_DEFAULT_MODEL``) are kept under
    their lowercased prefix so future-us can extend the map without
    losing the data here.
    """
    out: dict[str, str] = {}
    # default-models-*.js is the canonical file but constants may live in
    # any of several files; scan everything that prefixes with the family.
    candidates: list[Path] = []
    for prefix in ("default-models", "defaults"):
        candidates.extend(sorted(dist.glob(f"{prefix}-*.js")))
    for src in candidates:
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for prefix, model in _DEFAULT_CONST_RE.findall(text):
            provider = _CONST_PREFIX_TO_PROVIDER.get(prefix, prefix.lower())
            # Skip entries whose value looks like a non-LLM model
            # (image / tts / embedding constants live in the same file
            # and use the same naming pattern but produce nonsense for
            # tier resolution). Filter by presence of a "/" — LLM model
            # refs are always provider-qualified.
            if "/" not in model:
                continue
            # Don't clobber an earlier hit — first file wins. Keeps
            # results deterministic even when two files duplicate the
            # constant (rare but possible across OC's chunk splits).
            out.setdefault(provider, model)
    return out


# upgradeRetiredOpenAiModelId / upgradeRetiredXaiModelId / etc are
# `switch(case)` or `if (... === "x" || ... === "y") return "z"` shapes.
# We extract `case "x":` and `=== "x"` literals along with the `return
# "z"` that follows, mapping each literal name to its replacement.

# Match a function body by name. The body is the longest balanced-brace
# span starting after the `function NAME(...)` signature. We approximate
# the balance with a non-greedy capture stopping at the next top-level
# `}\nfunction` boundary — fragile against minification but OC ships
# with whitespace preserved in dist, so this works in practice.
def _extract_function_body(text: str, func_name: str) -> str | None:
    pat = re.compile(
        rf"function\s+{re.escape(func_name)}\s*\([^)]*\)\s*\{{(.*?)\n\}}",
        re.DOTALL,
    )
    m = pat.search(text)
    return m.group(1) if m else None


# Inside an if-chain: ``if (... === "old1" || ... === "old2") return "new"``.
# Captures every `"literal"` between the `if (` and the matching `return "value"`.
_IF_CHAIN_RE = re.compile(
    r'if\s*\([^)]*\)\s*return\s*"([^"]+)"',
)
_LITERAL_IN_COND_RE = re.compile(r'===\s*"([^"]+)"')


def _parse_if_chain_retirements(body: str) -> dict[str, str]:
    """Parse a body of ``if (a === "x" || b === "y") return "z";`` clauses.

    Returns ``{x: z, y: z}``. Each literal in the condition maps to the
    return value of that branch. Branches with no string literal in the
    condition (e.g., regex tests) are skipped — they can't be encoded
    as a flat dict and the resolver doesn't need them to identify the
    common gpt-4o-style retirements.
    """
    out: dict[str, str] = {}
    # Walk the body looking for `if (...) return "X"` patterns; for each,
    # also pull the condition fragment so we can extract its `=== "Y"` literals.
    cond_then_return = re.finditer(
        r'if\s*\(([^)]+)\)\s*return\s*"([^"]+)"',
        body,
    )
    for m in cond_then_return:
        cond, replacement = m.group(1), m.group(2)
        for old in _LITERAL_IN_COND_RE.findall(cond):
            # First occurrence wins so a later, more-specific branch
            # doesn't accidentally overwrite an earlier mapping for a
            # name that legitimately appears twice.
            out.setdefault(old, replacement)
    return out


# Inside a switch: ``case "x": case "y": return "z";``.
# We walk every case-label and pair it with the next return literal.
_CASE_OR_RETURN_RE = re.compile(
    r'case\s+"([^"]+)"\s*:|return\s+"([^"]+)"',
)


def _parse_switch_retirements(body: str) -> dict[str, str]:
    """Parse a body of ``case "x": case "y": return "z";`` clauses.

    Cases accumulate until the next ``return "z"`` — at which point
    every case in the buffer maps to ``z``. Mirrors the JS semantics of
    fall-through cases.
    """
    out: dict[str, str] = {}
    pending: list[str] = []
    for case_label, ret_val in _CASE_OR_RETURN_RE.findall(body):
        if case_label:
            pending.append(case_label)
        elif ret_val:
            for old in pending:
                out.setdefault(old, ret_val)
            pending.clear()
    return out


# Functions we know how to extract from legacy-config-migrations-*.js,
# keyed by canonical provider id. The shape hint tells the parser which
# extractor to use.
_RETIREMENT_FUNCTIONS: tuple[tuple[str, str, str], ...] = (
    # (provider, function name, shape: "if_chain" | "switch")
    ("openai", "upgradeRetiredOpenAiModelId", "if_chain"),
    ("xai", "upgradeRetiredXaiModelId", "switch"),
    ("groq", "upgradeRetiredGroqModelId", "switch"),
)


def load_retirement_map(dist: Path) -> dict[str, dict[str, str]]:
    """Extract per-provider retirement maps from legacy-config-migrations-*.js.

    Returns ``{provider_id: {retired_model_id: replacement_model_id}}``.
    The model ids in this dict are BARE (no ``provider/`` prefix) —
    matches what OC stores in its switch cases. Callers that want to
    check a catalog entry like ``openai/gpt-4o`` should split on ``/``
    first.

    Anthropic retirements live in ``upgradeOldClaudeToken`` and use
    prefix-matching against a list of ``claude-opus-4-{1,5}`` etc. The
    full mirror of that logic isn't worth doing here — the same
    bundled OC code applies the migration anyway. We extract just the
    LITERAL bare-name retirements that the simple if-chain covers,
    enough to flag a tier config explicitly referencing one of the
    dead names. Operators with prefix-shaped tier entries get the
    advisory from OC's own model-list fallback path.
    """
    src = _glob_first_with_marker(
        dist, "legacy-config-migrations", "upgradeRetiredOpenAiModelId",
    )
    if not src:
        return {}
    try:
        text = src.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    out: dict[str, dict[str, str]] = {}
    for provider, func_name, shape in _RETIREMENT_FUNCTIONS:
        body = _extract_function_body(text, func_name)
        if body is None:
            continue
        if shape == "if_chain":
            mapping = _parse_if_chain_retirements(body)
        elif shape == "switch":
            mapping = _parse_switch_retirements(body)
        else:
            continue
        if mapping:
            out[provider] = mapping

    # Anthropic retirement names — extracted from ``upgradeOldClaudeToken``
    # by pulling the explicit ``.startsWith("claude-opus-4-5")``-style
    # literals. We can't replicate OC's full matcher (prefix + regex)
    # without re-implementing it, but the literals alone catch the
    # common-tier-config case of a bot whose evolve-tiers.json names
    # ``anthropic/claude-opus-4-5`` directly.
    anthropic = _extract_anthropic_literal_retirements(text)
    if anthropic:
        out["anthropic"] = anthropic

    return out


# Inside upgradeOldClaudeToken: ``.startsWith("claude-opus-4-5")`` etc.
# We collect every claude-{family}-{X.Y} literal that appears as a
# `startsWith` argument. Replacement is inferred from the family token
# (opus → claude-opus-current, sonnet/haiku → claude-sonnet-current)
# — the actual current model ids come from the alias map at resolve
# time, not from this parser.
_CLAUDE_LITERAL_RE = re.compile(
    r'"(claude-(?:opus|sonnet|haiku)-[\d.][\d.\-]*)"',
)


# Markers that flush the pending-literal buffer to a target family.
# ``return null`` means "these names are CURRENT — keep them". The
# others mean "these names are retired and migrate to this family's
# current id." We can't know the current id from this parser; mark
# with ``__alias:opus__`` etc. and let the resolver substitute at
# lookup time.
_RETURN_TARGETS: tuple[tuple[str, str | None], ...] = (
    ("return null", None),                       # KEEP — not retired
    ("return opusTarget", "__alias:opus__"),     # Opus family → opus alias
    ("return sonnetTarget", "__alias:sonnet__"), # Sonnet/Haiku → sonnet alias
)


def _extract_anthropic_literal_retirements(text: str) -> dict[str, str]:
    """Walk upgradeOldClaudeToken body and partition claude-* literals.

    The OC function uses a mix of ``startsWith(...)`` calls AND inline
    array literals (passed to ``hasAnyRetiredVersionPrefix``). We can't
    tell them apart syntactically without a real parser — but we don't
    need to. The logical structure is "accumulate names, then hit a
    return-target, then assign each pending name to that target." We
    walk the body linearly, buffering string literals until a return
    marker fires, then flush.
    """
    body = _extract_function_body(text, "upgradeOldClaudeToken")
    if body is None:
        return {}
    # Walk the body, tokenizing on the smaller of: next string literal
    # match OR next return marker. Whichever comes first wins this step.
    out: dict[str, str] = {}
    pending: list[str] = []
    pos = 0
    while pos < len(body):
        lit_match = _CLAUDE_LITERAL_RE.search(body, pos)
        # Find nearest return marker after pos
        next_target_pos = -1
        next_target: str | None = "?"
        for marker, target in _RETURN_TARGETS:
            idx = body.find(marker, pos)
            if idx == -1:
                continue
            if next_target_pos == -1 or idx < next_target_pos:
                next_target_pos = idx
                next_target = target
        if lit_match is None and next_target_pos == -1:
            break
        if lit_match and (next_target_pos == -1 or lit_match.start() < next_target_pos):
            pending.append(lit_match.group(1))
            pos = lit_match.end()
            continue
        # Return marker hits first — flush pending names to that target.
        if next_target is not None:  # None = keep-list, drop pending
            for name in pending:
                out.setdefault(name, next_target)
        pending.clear()
        pos = next_target_pos + 1
    return out


# ── OC version detection ──────────────────────────────────────────────────


def detect_oc_version(dist: Path) -> str | None:
    """Read the OC package version from ``dist/../package.json``.

    Used purely for telemetry — the freshness UI surfaces it as
    "checked against OC vX" so operators can spot stale introspection
    when an OC upgrade lands.
    """
    pkg_json = dist.parent / "package.json"
    try:
        import json
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    v = data.get("version")
    return str(v) if v else None


# ── Composed entrypoint ───────────────────────────────────────────────────


@dataclass(frozen=True)
class OcSignals:
    """Aggregated signals extracted from OC's bundled JS.

    All fields default to empty so a missing or unparseable OC install
    produces a usable (if uninformative) signals object — callers can
    check ``ok`` to know whether anything was actually loaded.
    """

    aliases: dict[str, str] = field(default_factory=dict)
    defaults: dict[str, str] = field(default_factory=dict)
    retirement: dict[str, dict[str, str]] = field(default_factory=dict)
    oc_version: str | None = None
    source_dir: str | None = None

    @property
    def ok(self) -> bool:
        """True when at least one signal class was extracted."""
        return bool(self.aliases or self.defaults or self.retirement)


@lru_cache(maxsize=4)
def _cached_signals(dist_str: str) -> OcSignals:
    dist = Path(dist_str)
    return OcSignals(
        aliases=load_alias_map(dist),
        defaults=load_default_constants(dist),
        retirement=load_retirement_map(dist),
        oc_version=detect_oc_version(dist),
        source_dir=str(dist),
    )


def load_oc_signals(dist: Path | None = None) -> OcSignals:
    """Load and cache all OC tier-currency signals.

    Pass ``dist`` to override the auto-discovery (useful in tests).
    Repeated calls with the same dist return the cached result —
    callers that need to pick up a post-OC-upgrade refresh should call
    :func:`reset_cache` first.
    """
    target = dist or find_oc_dist_dir()
    if target is None:
        return OcSignals()
    return _cached_signals(str(target))


def reset_cache() -> None:
    """Drop the cached signals — call after an OC upgrade."""
    _cached_signals.cache_clear()
