"""
export_engine.py — Stage 0 of the scanned-export pipeline.

Spec: docs/spec-scanned-export-2026-06-02.md.

Scanner-discovered application manifests have rich `identity` /
`success_criteria` / `constraints` fields, but they lack the things a
gallery consumer needs to re-forge the app on a different bot:

  * ``pkg_id`` / ``pkg_version`` / ``gallery_version`` — minted at forge
    time; absent on scanned manifests.
  * ``build_spec`` — the natural-language re-build recipe; absent on
    scanned manifests (the *real* spec is the source code itself).
  * ``files[].file_id`` / ``owned_by`` / ``created_in_run`` — provenance
    fields that forge stamps as it writes; scanner only records paths.

Stage 0 of the export pipeline synthesises these from the scanned
manifest + the actual source code. The result is a "draft" gallery
manifest that can flow through the rest of the export pipeline
(Stages 1-7, deferred — see spec §3) or be published as-is for
in-pod use.

This PR (S1) implements:

* **Stage 0a — Mint identifiers**: deterministic, no LLM. ``pkg_id``
  is a SHA-256 of (bot_id, manifest_id, scan_timestamp) — stable across
  re-exports from the same scan. ``pkg_version`` follows CalVer.
* **Stage 0b — Derive build_spec**: LLM-driven. Reads the scanned
  manifest's anchor fields + the source code of every classified
  build-artifact file and produces a build_spec following the
  gallery's authoring-guide format.

Stages 0c (interface_contract), 0d (round-trip), 0e (operator review),
and 1-7 (refresh/classify/generalise/scrub/validate/review/stamp) land
in follow-on PRs.

Public entrypoint: ``build_export_draft(bot_id, scanned_manifest, …)``
which orchestrates 0a + 0b and returns a dict ready to be written as
``gallery/<app-slug>/<pkg_id>.draft.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .ids import calver_today, now_iso


# ── Legacy LLM call (moved from forge_engine when it migrated to infra_llm,
#    #3466 PR-4). The Stage-0 derivers below take explicit (model, api_key)
#    args with an ANTHROPIC_API_KEY hard requirement; migrating them onto
#    infra_llm is deferred — they are not in the #3466 audit's site lists.

def _call_anthropic(
    system_prompt: str,
    user_message: str,
    model: str,
    api_key: str,
    max_tokens: int = 8192,
) -> str:
    """Call the Anthropic Messages API directly via urllib (no SDK dep)."""
    import urllib.request

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["content"][0]["text"]


log = logging.getLogger(__name__)


# ── Stage 0a — Mint identifiers (deterministic, no LLM) ──────────────────────


# ── Provenance heuristic (F-P.9.x) ───────────────────────────────────────────
# Smart-forge model
# (docs/note-smart-forge-and-file-provenance-2026-06-04.md): the deriver
# suggests per-file provenance during scanned-export so the operator
# review surface (Stage 0e) starts with informed defaults. Heuristic-
# only — no LLM call. Operator overrides during review.

# Source-code extensions default to bundled (stable shape, deserves
# cheap install path). Markup/template files default to forge (per-bot
# personalization is the whole point of those files).
_BUNDLED_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".sh", ".rb", ".go", ".rs",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".conf",
    ".sql",
})
_FORGE_EXTENSIONS: frozenset[str] = frozenset({
    ".md", ".markdown", ".txt", ".rst",
})

# Path tokens that signal forge regardless of extension. "template" /
# "prompt" / "instructions" point at per-bot LLM-generated content.
_FORGE_PATH_HINTS: tuple[str, ...] = (
    "template", "prompt", "instructions",
)

# Path tokens that signal forge even on extensions that would otherwise
# default to bundled. "local" / "personal" / "private" point at
# operator-specific config that should regenerate per install.
_FORGE_NAME_HINTS: tuple[str, ...] = (
    "local", "personal", "private",
)

# Roles (from the scanner manifest schema v13+) that override the
# extension defaults. The role is the strongest signal we have.
_BUNDLED_ROLES: frozenset[str] = frozenset({"script", "cron"})
_FORGE_ROLES: frozenset[str] = frozenset({"data_file", "data"})


def suggest_provenance(
    path: str,
    role: str = "",
) -> str:
    """Heuristic per-file provenance suggestion (F-P.9.x).

    Returns one of ``"bundled"`` or ``"forge"``. Pure heuristic — no
    LLM call. The operator-review surface (Stage 0e) shows the
    suggestion alongside the file path; operator can flip it.

    Decision order:
      1. Role override (script/cron → bundled; data_file/data → forge).
      2. Forge hint in any path segment (template / prompt /
         instructions → forge).
      3. Forge hint in the filename basename (local / personal /
         private → forge).
      4. Extension default (source code → bundled, markup → forge).
      5. Fallback: bundled (cheap install is the safer default; the
         operator can flip to forge if the file genuinely needs
         per-install LLM generation).
    """
    path_norm = (path or "").strip().lower()
    if not path_norm:
        return "bundled"
    role_norm = (role or "").strip().lower()

    # 1. Role override.
    if role_norm in _BUNDLED_ROLES:
        return "bundled"
    if role_norm in _FORGE_ROLES:
        return "forge"

    # 2. Forge path hint anywhere in the path.
    segments = path_norm.split("/")
    for seg in segments:
        for hint in _FORGE_PATH_HINTS:
            if hint in seg:
                return "forge"

    # 3. Forge name hint in the basename.
    basename = segments[-1] if segments else ""
    for hint in _FORGE_NAME_HINTS:
        if hint in basename:
            return "forge"

    # 4. Extension default.
    # Use the longest matching trailing extension so e.g. `foo.tar.gz`
    # is `.gz` and `foo.template.md` would have been caught by hint #2.
    ext = ""
    if "." in basename:
        ext = "." + basename.rsplit(".", 1)[1]
    if ext in _FORGE_EXTENSIONS:
        return "forge"
    if ext in _BUNDLED_EXTENSIONS:
        return "bundled"

    # 5. Fallback.
    return "bundled"


def _short_hex(data: str, n: int = 8) -> str:
    """Return the first ``n`` hex chars of SHA-256 of ``data``."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:n]


def mint_export_pkg_id(bot_id: str, manifest_id: str, scan_timestamp: str) -> str:
    """Compute a deterministic ``pkg_id`` for an export.

    The same (bot, manifest, scan_run) always produces the same pkg_id.
    This lets re-exports of an unchanged scanned manifest land at the
    same gallery slot without operator-visible churn — only the
    ``pkg_version`` increments.

    Args:
        bot_id: The source bot, e.g. ``"team-bot-a"``.
        manifest_id: The scanner-assigned id, e.g. ``"i-9c16b1c7"``.
        scan_timestamp: An ISO-8601 string for the scan run. Treat
            as opaque — the only requirement is that *the same scan
            run yields the same pkg_id every time it's exported*.

    Returns: ``"p-" + 8 hex chars``.
    """
    if not bot_id or not manifest_id or not scan_timestamp:
        raise ValueError(
            "mint_export_pkg_id requires non-empty bot_id, manifest_id, scan_timestamp"
        )
    return "p-" + _short_hex(f"{bot_id}|{manifest_id}|{scan_timestamp}")


def mint_export_file_id(pkg_id: str, file_path: str) -> str:
    """Compute a deterministic ``file_id`` for an exported artifact.

    Stable across re-exports of the same pkg — every file in a given
    gallery package always gets the same file_id, so cross-references
    don't churn.
    """
    if not pkg_id or not file_path:
        raise ValueError("mint_export_file_id requires non-empty pkg_id and file_path")
    return "f-" + _short_hex(f"{pkg_id}|{file_path}")


def mint_export_run_id(pkg_id: str, scan_timestamp: str) -> str:
    """Compute a synthesised ``r-…`` id for the (virtual) run that
    produced this export. Stable per (pkg, scan)."""
    if not pkg_id or not scan_timestamp:
        raise ValueError("mint_export_run_id requires non-empty pkg_id and scan_timestamp")
    return "r-" + _short_hex(f"{pkg_id}|{scan_timestamp}")


def mint_export_identifiers(
    bot_id: str,
    scanned_manifest: dict,
    *,
    scan_timestamp: Optional[str] = None,
    previous_pkg_version: Optional[str] = None,
) -> dict:
    """Stage 0a: synthesise the gallery-identifier fields for a scanned manifest.

    Returns a dict with the keys an export-draft manifest needs:

        pkg_id, pkg_version, gallery_version, source, source_detail,
        files[]  — one entry per scanned files[] entry, carrying the
                  synthesised file_id / owned_by / created_in_run

    The shape mirrors ``forge_engine``'s output so the downstream
    pipeline can treat scanned and forged manifests identically.

    Args:
        bot_id:        Source bot, e.g. ``"team-bot-a"``.
        scanned_manifest: The raw scanner-produced manifest dict.
        scan_timestamp:   ISO-8601 timestamp of the scan run. Defaults
                       to the manifest's ``updated_at`` or
                       ``last_audit`` if present, then to ``now_iso()``.
        previous_pkg_version: If this scanned manifest has been exported
                       before, pass the prior pkg_version so we bump
                       the minor counter rather than starting at 1.0.

    Raises ``ValueError`` if the manifest is missing the bare minimum
    (``id`` field).
    """
    # identity: see resolve_app_id — the export pkg-id MINT SEED, and it must
    # stay the manifest's own ``id`` (its filename stem). ``resolve_app_id``
    # leads with ``pkg_id``, so on a manifest that already carries one — i.e.
    # any app previously exported or gallery-installed — it would return a
    # different string and mint a different ``p-<hash>`` for the same app,
    # breaking the export/re-export identity the ``previous_pkg_version``
    # bump depends on. The docstring's "missing the bare minimum (``id``)"
    # contract is likewise ``id``-specific.
    manifest_id = (scanned_manifest.get("id") or "").strip()
    if not manifest_id:
        raise ValueError("scanned_manifest is missing required 'id' field")

    ts = (
        scan_timestamp
        or (scanned_manifest.get("updated_at") or "").strip()
        or (scanned_manifest.get("last_audit") or "").strip()
        or now_iso()
    )

    pkg_id = mint_export_pkg_id(bot_id, manifest_id, ts)
    pkg_version = _next_export_version(previous_pkg_version)
    run_id = mint_export_run_id(pkg_id, ts)

    files: list[dict] = []
    for entry in scanned_manifest.get("files", []) or []:
        # Scanner emits either dicts ({"path": ..., "role": ...}) or bare
        # strings — handle both.
        if isinstance(entry, str):
            path = entry.strip()
            file_dict: dict[str, Any] = {"path": path}
        elif isinstance(entry, dict):
            path = (entry.get("path") or "").strip()
            file_dict = dict(entry)
        else:
            continue
        if not path:
            continue
        file_dict["file_id"] = mint_export_file_id(pkg_id, path)
        file_dict["owned_by"] = pkg_id
        file_dict["created_in_run"] = run_id
        # F-P.9.x — provenance suggestion. Respect any pre-existing
        # provenance the operator (or upstream pipeline) already set;
        # otherwise compute a heuristic default. The operator-review
        # surface (Stage 0e) renders this with a toggle so the
        # operator can flip before publishing.
        if not file_dict.get("provenance"):
            file_dict["provenance"] = suggest_provenance(
                path, role=file_dict.get("role", ""),
            )
        files.append(file_dict)

    return {
        "pkg_id": pkg_id,
        "pkg_version": pkg_version,
        "gallery_version": pkg_version,
        "source": "scanned_export",
        "source_detail": f"bot:{bot_id} manifest:{manifest_id} scan:{ts}",
        "files": files,
        "run_id": run_id,
    }


def _next_export_version(previous: Optional[str]) -> str:
    """Pkg-version helper for exports.

    For a fresh export: ``{YYYY.MM.DD}-1.0``.
    For a re-export of an already-published manifest: bump the patch
    by appending a third ``.N`` segment so the gallery sees a strict
    bump regardless of whether the prior version was minted today or
    a month ago. We keep ``major.minor`` stable across exports because
    each export is "the same app, slightly newer source code", not a
    cross-cutting redesign.

    Patterns:
      previous None              → "<today>-1.0"
      previous "2026.06.02-1.0"  → "<today>-1.1"  (same day or later)
      previous "2026.06.02-1.3"  → "<today>-1.4"

    The ``major`` bump is the operator's decision (made via the export
    pipeline's operator-review stage, not here).
    """
    today = calver_today()
    if not previous:
        return f"{today}-1.0"
    # Reuse the parser shape from ids.py to bump minor.
    m = re.match(r"^(\d{4}\.\d{2}\.\d{2})-(\d+)\.(\d+)$", previous)
    if not m:
        return f"{today}-1.0"
    _date, major, minor = m.group(1), int(m.group(2)), int(m.group(3))
    return f"{today}-{major}.{minor + 1}"


# ── Stage 0b — Derive build_spec (LLM) ───────────────────────────────────────


# The deriver's system prompt. Output rules + structural targets + the
# strip-source-specific directive. Kept as a module constant for
# reusability and to make the prompt easy to diff in code review.
DERIVER_SYSTEM_PROMPT = """\
You are the build_spec deriver for Evolve's app-export pipeline.

Your job: given (1) a scanned application manifest with identity,
success_criteria, and constraints fields, plus (2) the actual source
code of the app's scripts, produce a build_spec markdown document
that follows the gallery's authoring-guide format. A fresh bot LLM
should be able to follow your output to re-create a functionally
equivalent app on a different bot without ever seeing the original
source code.

OUTPUT RULES
- Output ONLY the build_spec markdown. No preamble, no JSON wrapping,
  no closing commentary.
- The build_spec must be self-contained: a fresh consumer reads only
  your output, not the source.
- Use `{bot_id}` and `{workspace}` as placeholders where the source
  hardcoded a specific bot's account or path. The downstream forge run
  will interpolate the real values.

CANONICAL STRUCTURE
1. `# {App Name} — Build Specification`
2. `## Overview` — what the app does, who it's for, what it doesn't do.
3. `## File Layout` — code tree relative to workspace root.
4. `## Data Format` — JSON schemas, atomic-write conventions, first-run
   initialisation.
5. `## CLI Commands` — one subsection per command with usage + flag
   reference + side-effects.
6. `## Heartbeat / Scheduled Behavior` — describe the v17 mechanism
   (`oc_heartbeat_instruction`) for any periodic checks. The bot's LLM
   reads HEARTBEAT.md sections written by forge's Phase 4.5 and executes
   the documented command at heartbeat time.
7. `## Living Documentation` — TASKS.md / TAGS.md / similar manual files
   the bot maintains.
8. `## Provenance Marker Pattern` — embed `# evolve: pkg=… file=…`
   markers in every generated script.
9. `## Test Suite` — explicit shell-runnable validation steps for a
   fresh build.
10. `## Customization Guidance` — fields the consumer should adapt
    (categories, tags, defaults) and the contract surfaces they must
    NOT change.

GENERALISATION RULES
- Replace hardcoded user IDs (e.g. Slack `U0PLKKXV0`, channel IDs
  `C0AL2GDUA7J`) with `{bot_id}` placeholders or configuration hooks
  the consumer can wire up later.
- Replace hardcoded source-app names (the source bot's own name, names
  of sibling bots it integrates with, etc.) with neutral references;
  describe inter-app integrations as optional hooks rather than
  required dependencies.
- Storage paths default to workspace root unless the source's choice
  is structurally necessary. For task data, use `{workspace}/tasks.json`.

STRIP-SOURCE-SPECIFIC MODE
When the operator sets strip_source_specific=true (a separate
directive in the user message), explicitly DROP from the build_spec:
- Maintenance-tracker integration (severity 1-5 tiers, maintenance
  channel monitoring, repair-task workflows).
- Slack DM monitoring + watchdogs (team-member-DM cadence,
  unanswered-DM escalation patterns).
- Per-user escalation references with specific user/channel IDs.
- Any operations-bot-specific glue that wires this app to others on
  the source bot.

GENERALISE source-domain TAXONOMIES that ship as the DEFAULT and
read as bot-specific. Categories, ID prefixes, fixed enums, tag
registries, and similar lookup tables hardcoded into the source
code often reflect the source bot's specific industry or workflow
(e.g. `game design / fabrication / venue / electronics / marketing /
sound / lights / graphic design` for a creative-production bot).
Such defaults are awkward on a fresh personal-productivity consumer.

In strip mode, REPLACE the documented default taxonomy in
build_spec with a neutral set the consumer can adapt without
editing. For a personal-productivity task app, a reasonable default
is something like {work, personal, errands, learning, health,
finance, home, operations, code, uncategorized} — but pick what
naturally fits the app's general purpose, not the source bot's.

Update any matching helper structures (e.g. ID-prefix mapping)
in tandem so they stay consistent with the new taxonomy.

Crucially: PRESERVE the Customization Guidance section verbatim.
The point of strip mode is just that the DEFAULT shouldn't ship
as a specific bot's current taxonomy — the operator's freedom to
swap to their own domain stays documented.

But KEEP these generally-useful upgrades that exist in the source but
are absent from the gallery's v1 task-manager:
- The `expires` field on tasks (time-aware lifecycle, drives
  pending-todo expiry detection at heartbeat).
- The `next` command (recommend what to work on next).
- The `summary` command (one-shot status table).
- A `tag_registry` (canonical list of allowed tags, with alias
  normalisation).
- The `prune-expired` command (periodic cleanup).

HEARTBEAT OUTPUT CHANNEL
The gallery convention is for periodic checks to emit lines starting
with `TASK_DUE:` or `FOLLOWUP_NEEDED:` from a `tasks.py check`
command (or equivalent). The v17 install mechanism
(`oc_heartbeat_instruction`) writes a HEARTBEAT.md section that
directs the bot's LLM to run the check command and surface those
signals to the operator.

If the source code uses a different output channel (e.g. direct
Slack DMs, or stdout JSON), document the gallery convention in
your output instead, and explain how the consumer wires their own
output channel.
"""


def _format_deriver_user_message(
    scanned_manifest: dict,
    file_contents: dict[str, str],
    *,
    strip_source_specific: bool,
    max_chars_per_file: int = 30_000,
) -> str:
    """Render the user message for the build_spec deriver.

    Layout:
      1. The scanned manifest's anchor fields (identity / success /
         constraints / description / display_name) as JSON.
      2. Each source file's contents, capped per-file to
         ``max_chars_per_file`` so a single 50KB file doesn't drown out
         everything else.
      3. The strip directive + closing task statement.
    """
    anchors = {
        k: scanned_manifest.get(k)
        for k in (
            "display_name", "name", "description", "identity",
            "success_criteria", "constraints", "interface_contract",
            "example_triggers", "test_cases",
        )
        if scanned_manifest.get(k)
    }

    parts: list[str] = []
    parts.append("## Scanned Manifest Anchors\n")
    parts.append("```json\n")
    parts.append(json.dumps(anchors, indent=2, ensure_ascii=False))
    parts.append("\n```\n\n")

    parts.append("## Source Files\n")
    if not file_contents:
        parts.append("_(no source files were provided)_\n\n")
    else:
        for path, body in sorted(file_contents.items()):
            parts.append(f"### `{path}`\n")
            parts.append("```\n")
            if len(body) > max_chars_per_file:
                parts.append(body[:max_chars_per_file])
                parts.append(
                    f"\n... [truncated; original {len(body)} chars]\n"
                )
            else:
                parts.append(body)
            parts.append("\n```\n\n")

    parts.append("## Derivation Directive\n")
    parts.append(
        f"strip_source_specific = {str(strip_source_specific).lower()}\n\n"
    )
    parts.append(
        "Produce the build_spec following the rules in your system "
        "prompt. Output ONLY the markdown build_spec, no preamble."
    )
    return "".join(parts)


@dataclass
class DerivedBuildSpec:
    """Return shape from ``derive_build_spec``.

    ``build_spec`` is the markdown body. ``model`` / ``input_chars`` /
    ``output_chars`` are kept so the operator-review surface (S4) can
    show what was actually fed in and out.
    """

    build_spec: str
    model: str
    input_chars: int
    output_chars: int


def derive_build_spec(
    scanned_manifest: dict,
    file_contents: dict[str, str],
    *,
    strip_source_specific: bool = True,
    model: str = "claude-sonnet-4-5",
    api_key: Optional[str] = None,
    call_llm: Optional[Callable[[str, str, str, str, int], str]] = None,
    max_tokens: int = 8192,
) -> DerivedBuildSpec:
    """Stage 0b: derive a build_spec from a scanned manifest + source code.

    Args:
        scanned_manifest:        The raw scanner-produced manifest dict.
        file_contents:           ``{path: source_text}`` for every file
                                 the deriver should read. Empty dict is
                                 allowed but yields a low-quality result.
        strip_source_specific:   When ``True``, the deriver is instructed
                                 to drop operations-bot-specific glue
                                 (maintenance, Slack DMs, severity) from
                                 the build_spec while keeping
                                 generally-useful upgrades.
        model:                   Anthropic model id.
        api_key:                 Anthropic key. If ``None``, read from
                                 ``ANTHROPIC_API_KEY``. Ignored when
                                 ``call_llm`` is supplied (testing).
        call_llm:                Override for the actual LLM call.
                                 Signature:
                                 ``(system, user, model, api_key,
                                 max_tokens) -> str``. Defaults to
                                 this module's ``_call_anthropic``.

    Returns: a :class:`DerivedBuildSpec`.

    Raises:
        ValueError: if neither ``api_key`` nor ``call_llm`` is supplied
            and ``ANTHROPIC_API_KEY`` is not set.
        RuntimeError: if the LLM call returns an empty string.
    """
    if call_llm is None:
        call_llm = _call_anthropic
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError(
                "derive_build_spec needs an api_key (pass one in or "
                "set ANTHROPIC_API_KEY)"
            )

    system_prompt = DERIVER_SYSTEM_PROMPT
    user_message = _format_deriver_user_message(
        scanned_manifest, file_contents,
        strip_source_specific=strip_source_specific,
    )

    raw = call_llm(system_prompt, user_message, model, api_key or "", max_tokens)
    if not raw or not raw.strip():
        raise RuntimeError("build_spec deriver returned an empty response")

    spec = _strip_code_fence(raw.strip())
    return DerivedBuildSpec(
        build_spec=spec,
        model=model,
        input_chars=len(system_prompt) + len(user_message),
        output_chars=len(spec),
    )


def _strip_code_fence(text: str) -> str:
    """If the LLM wrapped the response in a markdown code fence, peel it.

    Tolerates both ```markdown\n...\n``` and bare ```\n...\n```.
    Leaves the text untouched if no fence is present.
    """
    s = text.strip()
    if not s.startswith("```"):
        return s
    first_newline = s.find("\n")
    if first_newline == -1:
        return s
    body = s[first_newline + 1:]
    if body.endswith("```"):
        body = body[: -3]
    return body.rstrip()


# ── Stage 0c — Derive interface_contract (LLM) ───────────────────────────────


def _format_extractor_user_message(
    file_contents: dict[str, str], *,
    max_total_chars: int = 80_000,
) -> str:
    """Render the user message for the interface-contract extractor.

    Concatenates every source file with a ``# File: <path>`` header line
    so the extractor LLM can attribute each CLI command / schema /
    key_path to its source. Caps the total payload so very large
    workspaces don't blow the context window (per-file caps already
    happened upstream in ``_read_source_files``).
    """
    if not file_contents:
        return (
            "## Implementation to Analyse\n\n"
            "_(no source files were provided)_\n\n"
            "## Task\n\nExtract the external interface surfaces from the "
            "implementation as described in your instructions. Return only "
            "the JSON object."
        )

    parts: list[str] = ["## Implementation to Analyse\n\n"]
    running = 0
    for path, body in sorted(file_contents.items()):
        header = f"### `{path}`\n```\n"
        footer = "\n```\n\n"
        remaining = max_total_chars - running - len(header) - len(footer)
        if remaining <= 0:
            parts.append(
                "_(further source files omitted to fit context window)_\n\n"
            )
            break
        if len(body) > remaining:
            parts.append(header)
            parts.append(body[:remaining])
            parts.append("\n... [truncated]")
            parts.append(footer)
            running = max_total_chars
            break
        parts.append(header)
        parts.append(body)
        parts.append(footer)
        running += len(header) + len(body) + len(footer)

    parts.append(
        "## Task\n\nExtract the external interface surfaces from this "
        "implementation as described in your instructions. Return only "
        "the JSON object."
    )
    return "".join(parts)


_FENCE_LEADING_RE = re.compile(r"^```[a-zA-Z0-9_-]*\n?")
_FENCE_TRAILING_RE = re.compile(r"\n?```\s*$")


def _strip_extractor_wrappers(raw: str) -> str:
    """Peel common LLM wrappers off the extractor response.

    Empirically Sonnet ignores the prompt's "no markdown fences"
    instruction and emits ``` ```json\\n<JSON>\\n``` ``` (verified
    against team-bot-a/i-9c16b1c7 on 2026-06-03). The unwrap step
    lets the downstream outermost-brace match land on the real JSON.

    Also tolerates a truncated trailing fence — when the response
    runs into ``max_tokens`` mid-JSON the closing ``` ``` ``` is gone,
    but the leading fence is still present; stripping it lets the
    outermost-brace match still find something useful when the JSON
    itself is complete.
    """
    text = raw.strip()
    text = _FENCE_LEADING_RE.sub("", text)
    text = _FENCE_TRAILING_RE.sub("", text)
    return text.strip()


def _parse_extractor_response(raw: str) -> dict | None:
    """Parse a JSON object out of the extractor LLM's response.

    The prompt instructs "return ONLY a JSON object (no explanation, no
    markdown fences)", but Sonnet ignores the fence rule in practice
    and prose-prefacing is also common. We:

      1. Strip a leading/trailing markdown code fence if present.
      2. Try a direct ``json.loads`` of the cleaned text.
      3. Fall back to an outermost ``{…}`` search for prose-wrapped
         responses (same pattern as the legacy
         ``_extract_interface_contract``).

    Returns ``None`` on any parse failure — the caller decides whether
    to fall back to the scanner-populated contract or surface the
    failure.
    """
    if not raw or not raw.strip():
        return None
    cleaned = _strip_extractor_wrappers(raw)
    try:
        direct = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        direct = None
    if isinstance(direct, dict):
        return direct
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(cleaned[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _capture_failed_extractor_response(
    shared_dir: Optional[Path], run_id: Optional[str], raw: str,
) -> None:
    """Persist an unparseable extractor response for operator inspection.

    Writes to ``{shared_dir}/export/logs/{run_id}-extractor.txt``. No-op
    when either argument is missing (tests + ad-hoc callers don't need
    to provide one). Swallows OSError — losing the capture is not worth
    failing the pipeline over.
    """
    if not shared_dir or not run_id or not raw:
        return
    try:
        log_dir = Path(shared_dir) / "export" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{run_id}-extractor.txt").write_text(raw, encoding="utf-8")
    except OSError as exc:
        log.debug(
            "export_engine: could not capture failed extractor response: %s",
            exc,
        )


@dataclass
class DerivedInterfaceContract:
    """Return shape from ``derive_interface_contract``.

    ``contract`` is the parsed JSON dict (or the scanner-populated
    fallback). ``extraction_failed`` is True iff the LLM ran but the
    response couldn't be parsed — useful for the operator-review UI
    (S4) to surface "extractor ran but produced unparseable output,
    showing scanner contract as fallback."
    """

    contract: dict
    model: str
    input_chars: int
    output_chars: int
    extraction_failed: bool
    used_fallback: bool


def derive_interface_contract(
    scanned_manifest: dict,
    file_contents: dict[str, str],
    *,
    model: str = "claude-sonnet-4-5",
    api_key: Optional[str] = None,
    call_llm: Optional[Callable[[str, str, str, str, int], str]] = None,
    max_tokens: int = 8192,
    shared_dir: Optional[Path] = None,
    run_id: Optional[str] = None,
) -> DerivedInterfaceContract:
    """Stage 0c: derive ``interface_contract`` from source code via LLM.

    Reuses the canonical extractor prompt from ``forge_engine`` so the
    output shape stays identical to what forge writes for forged apps.
    This is what makes the export pipeline produce manifests that look
    structurally indistinguishable from forged ones — gallery consumers
    don't need to special-case scanned-origin manifests.

    Args:
        scanned_manifest:  The raw scanner-produced manifest. Used for
                           graceful-degradation fallback: if the LLM
                           call fails or its output is unparseable,
                           we keep the scanner's
                           ``interface_contract`` rather than blowing
                           up.
        file_contents:     ``{path: body}``; concatenated for the LLM.
                           When empty the deriver returns the scanner
                           contract directly without making any LLM
                           call.
        model / api_key / call_llm / max_tokens: as for
                           ``derive_build_spec``. The extractor's
                           default ``max_tokens`` (8192) is larger than
                           the deriver's because a real-world
                           interface_contract for an app like the
                           team-bot Unified Task System runs ~8 KB of
                           JSON; the previous 2048 ceiling truncated
                           those responses mid-array (see 2026-06-03
                           fix).
        shared_dir / run_id: when both are supplied AND the LLM call
                           returns unparseable output, the raw
                           response is captured to
                           ``{shared_dir}/export/logs/{run_id}-extractor.txt``
                           so an operator can see exactly what Sonnet
                           said. Failure is silent (debug-logged)
                           because losing the capture isn't worth
                           failing the pipeline.

    Returns: a :class:`DerivedInterfaceContract`.

    Raises:
        ValueError: if neither ``api_key`` nor ``call_llm`` is supplied
            and ``ANTHROPIC_API_KEY`` is not set.
    """
    scanner_contract = dict(scanned_manifest.get("interface_contract") or {})

    if not file_contents:
        # Nothing to extract from — preserve scanner output, mark not
        # forge-populated, return.
        return DerivedInterfaceContract(
            contract=_finalise_contract(scanner_contract, source="scanner"),
            model=model,
            input_chars=0,
            output_chars=0,
            extraction_failed=False,
            used_fallback=True,
        )

    if call_llm is None:
        call_llm = _call_anthropic
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError(
                "derive_interface_contract needs an api_key (pass one in "
                "or set ANTHROPIC_API_KEY)"
            )

    # Import the locked extractor prompt only at call time so test
    # patches of forge_engine apply.
    from .forge_engine import BUILTIN_EXTRACTOR_PROMPT  # noqa: WPS433

    system_prompt = BUILTIN_EXTRACTOR_PROMPT
    user_message = _format_extractor_user_message(file_contents)

    raw = call_llm(system_prompt, user_message, model, api_key or "", max_tokens)
    output_chars = len(raw) if raw else 0
    parsed = _parse_extractor_response(raw)
    if parsed is None:
        _capture_failed_extractor_response(shared_dir, run_id, raw)
        # Extractor ran but produced unusable output — degrade to the
        # scanner contract so the export pipeline keeps moving.
        return DerivedInterfaceContract(
            contract=_finalise_contract(scanner_contract, source="scanner"),
            model=model,
            input_chars=len(system_prompt) + len(user_message),
            output_chars=output_chars,
            extraction_failed=True,
            used_fallback=True,
        )

    return DerivedInterfaceContract(
        contract=_finalise_contract(parsed, source="export"),
        model=model,
        input_chars=len(system_prompt) + len(user_message),
        output_chars=output_chars,
        extraction_failed=False,
        used_fallback=False,
    )


def _finalise_contract(contract: dict, *, source: str) -> dict:
    """Stamp provenance fields on a contract dict.

    ``source="export"`` marks LLM-extracted contracts; ``"scanner"``
    marks fall-throughs that kept the scanner's original output. The
    consumer of the gallery manifest can use these to triage
    "extractor ran but failed, this contract may be incomplete."
    """
    out = dict(contract)
    # The forge-side flag is preserved when set by the scanner — the
    # gallery's downstream consumers (forge for dependency lookup, the
    # admin UI for display) only care about a boolean.
    out["populated_by_forge"] = bool(out.get("populated_by_forge"))
    out["populated_by_export"] = source == "export"
    out["extracted_at"] = now_iso()
    return out


# ── Stage 0d — Round-trip dry-run + structural diff ──────────────────────────


# The dry-run forge prompt asks the model: "given this build_spec,
# enumerate the files you'd create + a one-line purpose for each."
# We do NOT generate code — code generation is expensive and the
# round-trip check only needs file shape, not file content.
DRY_RUN_FORGE_SYSTEM_PROMPT = """\
You are the round-trip validator for Evolve's app-export pipeline.

You receive a build_spec markdown document that describes an
application to build. Your job is to enumerate the files the
build_spec instructs a downstream forge run to create — NOT to write
the code.

OUTPUT RULES
- Return ONLY a JSON object, no preamble, no markdown fence.
- The schema is:
  {
    "files": [
      {"path": "relative/to/workspace.py", "purpose": "one short sentence"}
    ]
  }
- Include every file the build_spec instructs to be written —
  scripts, build-time data files (the empty templates forge creates
  at install, NOT user-populated data such as the bot's own tasks),
  markdown documentation, cron-trigger shell scripts, plist
  templates.
- Paths must be relative to the bot's workspace root.
- Skip files the build_spec only mentions as inputs the consumer
  must already have.
"""


def _format_dry_run_user_message(build_spec: str) -> str:
    """Render the dry-run forge's user message from a build_spec."""
    body = build_spec.strip()
    return (
        "## Build Specification to Analyse\n\n"
        + body
        + "\n\n## Task\n\n"
        + "List the files the build_spec instructs forge to create. "
        + "Return only the JSON object described in your instructions."
    )


def _parse_dry_run_response(raw: str) -> Optional[list[dict]]:
    """Pull the ``files`` array out of the dry-run response.

    Returns ``None`` on parse failure so the caller can mark the
    round-trip as failed. Same outermost-{} match as the
    interface-contract parser for resilience against prose preamble.
    """
    parsed = _parse_extractor_response(raw)  # reuses Stage 0c helper
    if parsed is None:
        return None
    files = parsed.get("files")
    if not isinstance(files, list):
        return None
    out: list[dict] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = (entry.get("path") or "").strip()
        if not path:
            continue
        out.append({
            "path": path,
            "purpose": (entry.get("purpose") or "").strip(),
        })
    return out


@dataclass
class StructuralFinding:
    """One coverage gap from the structural diff.

    ``kind`` is a short tag the operator-review UI groups by;
    ``detail`` is a human-readable description; ``hint`` carries a
    remediation suggestion when there's an obvious one.
    """

    kind: str
    detail: str
    hint: str = ""


def _scanned_files_for_diff(scanned_manifest: dict) -> list[tuple[str, str]]:
    """Extract ``(path, role)`` for every scanned file, normalising
    bare-string entries and skipping blanks. Used by both
    ``structural_diff`` and the dry-run comparison."""
    out: list[tuple[str, str]] = []
    for entry in scanned_manifest.get("files", []) or []:
        if isinstance(entry, dict):
            path = (entry.get("path") or "").strip()
            role = (entry.get("role") or "")
        elif isinstance(entry, str):
            path = entry.strip()
            role = ""
        else:
            continue
        if not path:
            continue
        out.append((path, role))
    return out


def structural_diff(scanned_manifest: dict, draft: dict) -> list[StructuralFinding]:
    """Pure-Python structural coverage check (no LLM).

    Verifies the derived ``build_spec`` actually mentions the files,
    CLI commands, and key paths the scanner found in the source. A
    miss here is a strong signal the build_spec deriver dropped or
    renamed something — the operator-review UI should surface it
    before publish.

    Specifically:
      - Every ``files[].path`` (excluding data_file role) from the
        scanned manifest should appear in build_spec — either as the
        full path or as the basename. Data files are excluded because
        the build_spec only documents them in the data-format section
        and may not name the actual file.
      - Every ``interface_contract.cli[].command`` should appear.
      - Every ``interface_contract.key_paths`` value should appear.

    The check is intentionally string-level. Semantic equivalence is
    the LLM round-trip's job; this is the cheap pre-flight that
    catches obvious deriver mistakes.
    """
    findings: list[StructuralFinding] = []
    build_spec = (draft.get("build_spec") or "").lower()
    if not build_spec.strip():
        findings.append(StructuralFinding(
            kind="missing_build_spec",
            detail="draft has no build_spec — Stage 0b must have failed",
            hint="re-run Stage 0b or inspect deriver telemetry in export_meta",
        ))
        return findings

    # Files coverage. Accept either full path or basename — the
    # deriver can legitimately restructure the directory layout
    # (e.g. move from ops/ to scripts/) while keeping each file's
    # identity.
    for path, role in _scanned_files_for_diff(scanned_manifest):
        if role == "data_file":
            continue  # data files documented as schemas, not by name
        basename = path.rsplit("/", 1)[-1].lower()
        if path.lower() in build_spec or basename in build_spec:
            continue
        findings.append(StructuralFinding(
            kind="missing_file_in_build_spec",
            detail=f"scanned file {path!r} is not referenced in the derived build_spec",
            hint="confirm the deriver was given this file's source; re-run Stage 0b if not",
        ))

    # CLI coverage. Many CLI commands look like
    # ``python3 scripts/foo.py check`` — the subcommand (last token)
    # is the most discriminating signal that survives
    # path-restructuring.
    contract = draft.get("interface_contract") or scanned_manifest.get("interface_contract") or {}
    for cmd in (contract.get("cli") or []):
        if not isinstance(cmd, dict):
            continue
        cmdstr = (cmd.get("command") or "").strip()
        if not cmdstr:
            continue
        tail = cmdstr.rsplit(" ", 1)[-1].lower()
        if tail in build_spec or cmdstr.lower() in build_spec:
            continue
        findings.append(StructuralFinding(
            kind="missing_cli_in_build_spec",
            detail=f"CLI command {cmdstr!r} is not referenced in the derived build_spec",
            hint="deriver may have collapsed or renamed this subcommand",
        ))

    # key_paths coverage — these are the named anchors other apps
    # depend on (e.g. EA Pack reads task-manager's ``active_db``).
    for name, path in (contract.get("key_paths") or {}).items():
        if not isinstance(path, str) or not path.strip():
            continue
        if path.lower() in build_spec:
            continue
        findings.append(StructuralFinding(
            kind="missing_key_path_in_build_spec",
            detail=(
                f"interface_contract.key_paths[{name!r}] = {path!r} is "
                f"not referenced in the derived build_spec"
            ),
            hint=(
                "downstream apps depend on key_paths — confirm the "
                "deriver preserved this anchor"
            ),
        ))

    return findings


@dataclass
class RoundTripResult:
    """Stage 0d output.

    ``verdict`` summarises the operator-facing call:
      - ``"good"``  — dry-run files match scanned files, no structural
                      findings.
      - ``"drift"`` — one or more findings but nothing catastrophic
                      (renamed file, missing CLI subcommand, etc.).
                      Operator should review before publishing.
      - ``"broken"`` — dry-run failed entirely (LLM didn't parse) OR
                      scanned files and dry-run files share no
                      overlap. Operator should re-run or hand-edit
                      before publish.
    """

    structural_findings: list[StructuralFinding]
    dry_run_files: list[dict]
    dry_run_missing: list[str]   # scanned but not in dry-run
    dry_run_extra: list[str]     # dry-run but not in scanned
    dry_run_failed: bool
    verdict: str
    model: str
    input_chars: int
    output_chars: int


def dry_run_forge(
    build_spec: str,
    *,
    model: str = "claude-sonnet-4-5",
    api_key: Optional[str] = None,
    call_llm: Optional[Callable[[str, str, str, str, int], str]] = None,
    max_tokens: int = 2048,
) -> tuple[Optional[list[dict]], int, int]:
    """Ask Claude what files a forge run on ``build_spec`` would create.

    Returns ``(files, input_chars, output_chars)``. ``files`` is
    ``None`` on parse failure — the caller treats that as a failed
    dry-run.
    """
    if not build_spec.strip():
        return None, 0, 0

    if call_llm is None:
        call_llm = _call_anthropic
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError(
                "dry_run_forge needs an api_key (pass one in or set "
                "ANTHROPIC_API_KEY)"
            )

    system_prompt = DRY_RUN_FORGE_SYSTEM_PROMPT
    user_message = _format_dry_run_user_message(build_spec)
    raw = call_llm(system_prompt, user_message, model, api_key or "", max_tokens)
    output_chars = len(raw) if raw else 0
    files = _parse_dry_run_response(raw)
    return files, len(system_prompt) + len(user_message), output_chars


def _compute_verdict(
    findings: list[StructuralFinding],
    dry_run_files: Optional[list[dict]],
    scanned_paths: list[str],
) -> tuple[str, list[str], list[str]]:
    """Decide good / drift / broken and compute missing/extra lists.

    Catastrophic broken: dry-run failed entirely, OR we had real
    scanned files and the dry-run produced zero overlap with them.
    """
    if dry_run_files is None:
        return "broken", sorted({p for p in scanned_paths if p}), []

    dry_run_paths = [f["path"] for f in dry_run_files]
    scanned_set = {p.strip() for p in scanned_paths if p.strip()}
    dry_run_set = {p.strip() for p in dry_run_paths if p.strip()}

    missing = sorted(scanned_set - dry_run_set)
    extra = sorted(dry_run_set - scanned_set)
    overlap = scanned_set & dry_run_set

    if scanned_set and not overlap:
        return "broken", missing, extra

    if not findings and not missing and not extra:
        return "good", missing, extra

    return "drift", missing, extra


def round_trip_validate(
    scanned_manifest: dict,
    draft: dict,
    *,
    skip_dry_run: bool = False,
    dry_run_kwargs: Optional[dict] = None,
) -> RoundTripResult:
    """Stage 0d: validate the derived draft against the scanned source.

    Combines two passes:
      1. Pure-Python ``structural_diff`` — does the derived
         build_spec mention every scanned file, CLI command, key path?
      2. LLM dry-run forge — given the derived build_spec, what files
         would forge create? Diff against the scanned files[] for
         missing / extra paths.

    Args:
        scanned_manifest: The source manifest from the scanner.
        draft: The Stage 0a-0c draft (must have ``build_spec``
            populated).
        skip_dry_run: When True, skips the LLM dry-run. The
            structural diff still runs. Useful for cost-saving
            re-runs after a manual build_spec edit.
        dry_run_kwargs: passed through to ``dry_run_forge``.

    Returns a :class:`RoundTripResult` with everything the
    operator-review UI (Stage 0e, PR S4) needs to summarise the
    round-trip.
    """
    findings = structural_diff(scanned_manifest, draft)

    # Extract scanned-file paths for the dry-run comparison. Skip
    # data files — forge typically doesn't generate user-data files
    # at install time, so dry-run is allowed to omit them without
    # being flagged as drift.
    scanned_paths = [
        path for path, role in _scanned_files_for_diff(scanned_manifest)
        if role != "data_file"
    ]

    if skip_dry_run:
        # Verdict is purely structural.
        verdict = "good" if not findings else "drift"
        return RoundTripResult(
            structural_findings=findings,
            dry_run_files=[],
            dry_run_missing=[],
            dry_run_extra=[],
            dry_run_failed=False,
            verdict=verdict,
            model="(skipped)",
            input_chars=0,
            output_chars=0,
        )

    files, in_chars, out_chars = dry_run_forge(
        draft.get("build_spec") or "",
        **(dry_run_kwargs or {}),
    )
    dry_run_failed = files is None
    verdict, missing, extra = _compute_verdict(findings, files, scanned_paths)
    return RoundTripResult(
        structural_findings=findings,
        dry_run_files=files or [],
        dry_run_missing=missing,
        dry_run_extra=extra,
        dry_run_failed=dry_run_failed,
        verdict=verdict,
        model=(dry_run_kwargs or {}).get("model", "claude-sonnet-4-5"),
        input_chars=in_chars,
        output_chars=out_chars,
    )


# ── Stage 0 orchestrator ─────────────────────────────────────────────────────


def _read_source_files(
    workspace: Path, files_meta: list[dict | str], *,
    max_chars_per_file: int = 60_000,
) -> dict[str, str]:
    """Read every file referenced by a scanned manifest's ``files`` list
    from ``workspace``.

    Skips files that don't exist or aren't readable as UTF-8 (binary
    artifacts, deleted-since-scan files). Caps each read at
    ``max_chars_per_file`` so a huge data file doesn't blow up the
    LLM input prep.
    """
    out: dict[str, str] = {}
    for entry in files_meta or []:
        if isinstance(entry, str):
            path = entry
        elif isinstance(entry, dict):
            path = (entry.get("path") or "").strip()
        else:
            continue
        if not path:
            continue
        target = workspace / path
        try:
            body = target.read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError, OSError, UnicodeDecodeError):
            log.debug("export_engine: skipping unreadable file %s", target)
            continue
        if len(body) > max_chars_per_file:
            body = body[:max_chars_per_file] + "\n... [truncated]\n"
        out[path] = body
    return out


def build_export_draft(
    bot_id: str,
    scanned_manifest: dict,
    workspace: Path,
    *,
    scan_timestamp: Optional[str] = None,
    previous_pkg_version: Optional[str] = None,
    strip_source_specific: bool = True,
    skip_interface_contract: bool = False,
    skip_round_trip: bool = False,
    include_files_pack: bool = False,
    files_pack_out_dir: Optional[Path] = None,
    snapshot_kwargs: Optional[dict] = None,
    shared_dir: Optional[Path] = None,
    derive_kwargs: Optional[dict] = None,
    contract_kwargs: Optional[dict] = None,
    round_trip_kwargs: Optional[dict] = None,
) -> dict:
    """Orchestrate Stage 0a + 0b + 0c + 0d (+ optional Stage 0f) and return a
    draft gallery manifest.

    The returned dict is suitable for atomic write to
    ``gallery/<app-slug>/<pkg_id>.draft.json``. The remaining
    pipeline stage (0e operator review, PR S4) consumes it and
    either promotes it to ``<pkg_id>.json`` or sends it back for
    revision.

    Args:
        bot_id: Source bot.
        scanned_manifest: Raw scanner-discovered manifest.
        workspace: Path to the source bot's workspace (``/Users/{bot}/
                  .openclaw/workspace``); used to read file contents.
        scan_timestamp / previous_pkg_version: passed through to
            ``mint_export_identifiers``.
        strip_source_specific: passed through to ``derive_build_spec``.
        skip_interface_contract: when ``True``, Stage 0c is bypassed
            and the scanner-populated ``interface_contract`` (if any)
            is kept verbatim.
        skip_round_trip: when ``True``, Stage 0d is bypassed entirely.
            ``round_trip`` stays absent from the draft. Cost-saving
            re-runs after manual build_spec edits can run JUST the
            structural diff via
            ``round_trip_kwargs={"skip_dry_run": True}``.
        include_files_pack: when ``True``, run Stage 0f (files-pack
            snapshot) after Stage 0d. Snapshots the bot's installed
            files into a candidate files-pack so first-time gallery
            additions can ship as manifest+files instead of
            manifest-only. F-P.9.
            Requires the source bot to have an installed manifest
            for the original pkg_id (taken from
            ``scanned_manifest['pkg_id']``); when absent, Stage 0f
            is silently skipped and ``export_stage`` stays at "0d".
        files_pack_out_dir: where Stage 0f writes the candidate
            files-pack. Defaults to a daemon-allocated temp dir.
            Only consulted when ``include_files_pack`` is True.
        snapshot_kwargs: extra kwargs for
            ``snapshot_engine.snapshot_installed_app`` (e.g.
            ``auto_detect=False``, ``scrub_personalization=False``).
        shared_dir: pod-wide shared directory (typically
            ``/Users/Shared/evolve``). When set, unparseable extractor
            responses get captured under
            ``{shared_dir}/export/logs/{run_id}-extractor.txt`` so an
            operator can diagnose Stage 0c failures without re-running.
        derive_kwargs: extra kwargs for ``derive_build_spec``.
        contract_kwargs: extra kwargs for ``derive_interface_contract``.
        round_trip_kwargs: extra kwargs for ``round_trip_validate``
            (e.g. ``skip_dry_run``, ``dry_run_kwargs``).
    """
    identifiers = mint_export_identifiers(
        bot_id, scanned_manifest,
        scan_timestamp=scan_timestamp,
        previous_pkg_version=previous_pkg_version,
    )

    file_contents = _read_source_files(workspace, scanned_manifest.get("files", []))

    derived = derive_build_spec(
        scanned_manifest, file_contents,
        strip_source_specific=strip_source_specific,
        **(derive_kwargs or {}),
    )

    contract_result: Optional[DerivedInterfaceContract] = None
    if not skip_interface_contract:
        # Plumb shared_dir + run_id through so failed extractor responses
        # land at {shared_dir}/export/logs/{run_id}-extractor.txt for
        # operator inspection. Caller-supplied contract_kwargs win.
        ic_kwargs: dict[str, Any] = {
            "shared_dir": shared_dir,
            "run_id": identifiers["run_id"],
        }
        ic_kwargs.update(contract_kwargs or {})
        contract_result = derive_interface_contract(
            scanned_manifest, file_contents, **ic_kwargs,
        )

    # Start from the scanner-populated anchors so identity /
    # success_criteria / constraints come along for free, then layer
    # the Stage-0 synthesis on top.
    draft: dict[str, Any] = dict(scanned_manifest)
    draft["pkg_id"] = identifiers["pkg_id"]
    draft["pkg_version"] = identifiers["pkg_version"]
    draft["gallery_version"] = identifiers["gallery_version"]
    draft["source"] = identifiers["source"]
    draft["source_detail"] = identifiers["source_detail"]
    draft["files"] = identifiers["files"]
    draft["build_spec"] = derived.build_spec
    draft["status"] = "draft"
    draft["author"] = "evolve"
    draft["manifest_type"] = "evolve_application"
    if contract_result is not None:
        draft["interface_contract"] = contract_result.contract
        draft["export_stage"] = "0c"
    else:
        draft["export_stage"] = "0b"  # advanced by Stage 0d below if it runs

    # Stage 0d — round-trip validation. Needs the build_spec in `draft`
    # and the (possibly Stage-0c-overlaid) interface_contract to do
    # CLI/key_paths coverage.
    round_trip_result: Optional[RoundTripResult] = None
    if not skip_round_trip:
        round_trip_result = round_trip_validate(
            scanned_manifest, draft, **(round_trip_kwargs or {}),
        )
        draft["round_trip"] = {
            "verdict": round_trip_result.verdict,
            "structural_findings": [
                {"kind": f.kind, "detail": f.detail, "hint": f.hint}
                for f in round_trip_result.structural_findings
            ],
            "dry_run_files": round_trip_result.dry_run_files,
            "dry_run_missing": round_trip_result.dry_run_missing,
            "dry_run_extra": round_trip_result.dry_run_extra,
            "dry_run_failed": round_trip_result.dry_run_failed,
        }
        draft["export_stage"] = "0d"

    draft["export_meta"] = {
        "exported_at": now_iso(),
        "deriver_model": derived.model,
        "deriver_input_chars": derived.input_chars,
        "deriver_output_chars": derived.output_chars,
        "deriver_strip_source_specific": strip_source_specific,
        "run_id": identifiers["run_id"],
    }
    if contract_result is not None:
        draft["export_meta"].update({
            "extractor_model": contract_result.model,
            "extractor_input_chars": contract_result.input_chars,
            "extractor_output_chars": contract_result.output_chars,
            "extractor_failed": contract_result.extraction_failed,
            "extractor_used_fallback": contract_result.used_fallback,
        })
    if round_trip_result is not None:
        draft["export_meta"].update({
            "round_trip_model": round_trip_result.model,
            "round_trip_input_chars": round_trip_result.input_chars,
            "round_trip_output_chars": round_trip_result.output_chars,
            "round_trip_verdict": round_trip_result.verdict,
        })

    # ── Stage 0f — files-pack snapshot (F-P.9) ─────────────────────────────
    # When include_files_pack is True AND the scanned manifest carries the
    # installed pkg_id we can snapshot, capture the bot's installed files
    # into a candidate files-pack so the draft ships with both the manifest
    # AND the on-disk artifact. First-time gallery additions can promote
    # straight to manifest+files class (per docs/note-hybrid-gallery-
    # 2026-06-03.md) without a separate F-P.7.b promote step.
    if include_files_pack:
        # identity: see resolve_app_id — PRESENCE PROBE ("does this bot have an
        # evolve-managed install we can snapshot?"), not an id read. The value
        # is also the snapshot key, and ``snapshot_engine`` finds the installed
        # manifest by ``data.get("pkg_id") == pkg_id``. Routing it through the
        # resolver would make the probe true for every scanned manifest (the
        # legacy chain always resolves something), so Stage 0f would attempt a
        # files-pack snapshot for apps that were never installed from a package
        # — and the ``files_pack_skipped`` explanation below would never fire.
        # Same shape as the ``already_packaged`` probe in ``cli.py``.
        installed_pkg_id = scanned_manifest.get("pkg_id") or ""
        if not installed_pkg_id:
            # No installed marker — the bot doesn't have an evolve-managed
            # install of this app under a recognizable pkg_id. Log into
            # export_meta so the operator-review surface can show "Stage 0f
            # skipped because…" without re-running.
            draft["export_meta"]["files_pack_skipped"] = (
                "no_installed_pkg_id: scanned manifest has no pkg_id; "
                "Stage 0f requires the bot to have an evolve-managed "
                "installation to snapshot from."
            )
        else:
            # Lazy import — pulls in network/config resolution; keep it
            # off the import path of the rest of the export pipeline so
            # standalone Stage 0a-0d use doesn't pay the cost.
            from . import snapshot_engine as _snapshot_engine
            import tempfile as _tempfile

            out_dir = files_pack_out_dir
            if out_dir is None:
                out_dir = Path(_tempfile.mkdtemp(
                    prefix=f"export-stage0f-{identifiers['pkg_id']}-",
                    suffix="-files",
                ))
            snap_kwargs: dict[str, Any] = dict(snapshot_kwargs or {})
            snap_result = _snapshot_engine.snapshot_installed_app(
                bot_id=bot_id,
                pkg_id=installed_pkg_id,
                out_dir=out_dir,
                **snap_kwargs,
            )
            if snap_result.get("ok"):
                # Carry the files-pack metadata into the draft so the
                # operator-review UI (Stage 0e) renders it alongside the
                # manifest, AND the publish step knows whether to bundle
                # the files-pack into the gallery PR.
                draft["files_pack"] = {
                    "format_version": snap_result.get("format_version"),
                    "files_count": snap_result.get("files_count"),
                    "snapshot_source_pkg_version":
                        snap_result.get("snapshot_source_pkg_version"),
                    "snapshot_at": snap_result.get("snapshot_at"),
                    "sha256": snap_result.get("sha256"),
                    "staging_dir": snap_result.get("out_dir"),
                }
                draft["export_meta"]["files_pack_per_file"] = (
                    snap_result.get("per_file") or []
                )
                draft["export_meta"]["files_pack_personalization_totals"] = (
                    snap_result.get("personalization_totals") or {}
                )
                draft["export_meta"]["files_pack_skipped_files"] = (
                    snap_result.get("skipped") or []
                )
                draft["export_stage"] = "0f"
            else:
                # Snapshot failed — log into export_meta so the operator
                # can diagnose without re-running the whole pipeline.
                # Stage stays at 0d.
                draft["export_meta"]["files_pack_failed"] = (
                    snap_result.get("error") or "unknown_snapshot_error"
                )
    return draft
