"""Drift-significance classifier — narrate, don't gate (Bite 3).

Spec: docs/spec-apps-meta-2026-06-13.md §9.3 ("Drift is fluid — narrate,
don't gate"), §9.4 (the scanner's job flips with definition_status).

Apps drift over time: scripts get added, crons removed, evidence files
change. The model is that the system ROLLS WITH IT — no per-change operator
approval — keeping the manifest fresh on every scan AND giving the operator
a post-hoc NARRATIVE of the *significant* drift to review. This module is
that classifier: it consumes the reconciliation pass's existing deltas and
decides which drift is worth narrating.

## The major/minor split (§9.3)

  **MAJOR** — an executable / behavioral-surface change. A new, removed, or
              meaningfully-changed *script* (code file), *cron*,
              *scheduled_action*, or *skill/capability dependency*. The app's
              behavior changed; the operator may want to know. → absorbed AND
              appended to ``drift_log``.

  **MINOR** — a data or doc file (.md, data .json, …), a cosmetic change.
              The app still does the same thing. → absorbed SILENTLY, never
              logged. Logging these would bury the major signal in noise.

## Deterministic-first (this aspect's cheap-floor-first discipline)

The common cases are a pure-Python rule: the surface TYPE (cron / action /
dependency are always behavioral; a file is classified by its path) decides
major vs minor with no model call. The LLM is reached ONLY for the one
genuinely ambiguous case the rule cannot settle — *a script's CONTENT
changed (same path), did its behaviour meaningfully change?* — and even
there it is an injectable seam (``llm_fn``), default-to-MINOR on any error /
low-confidence / absent injection, so a flaky model never spams the log.

NB: the current reconciliation deltas are add/remove (+ a scheduled-action
evidence-anchor *modify*, which is a clear-cut behavioral surface →
deterministic major). A *file* (script) content-modify event — the one that
escalates to the LLM — is not yet produced by the reconcile pass (it needs
sha-drift detection, a later feed). The escalation branch is therefore the
spec-mandated seam, exercised by the unit tests via an injected ``llm_fn``;
it costs nothing until a script-modify feed exists.

## Where the narrative lives — drift_log, NOT change_log

Per the Schema v28 block in manifest.py: the v7-arc Instance already carries
a load-bearing, schema-locked ``change_log[]`` (the forge/capability audit
trail compressed into Lessons). The drift narrative ships as a distinct
``drift_log[]`` to avoid corrupting that pipeline — the same collision
resolution Bite 1 used for ``status`` → ``definition_status``.

## Only ``defined`` apps accrue a narrative (§9.4)

For a ``discovered`` app the scanner is a *guesser* — churn is fine, it's a
draft, so it just gets fresh content with no narrative. For a ``defined``
app the scanner is a *watchdog* — "does reality still match the vouched
contract? narrate the drift." So ``narrate_drift`` appends only when the
manifest is ``defined``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from evolve_util import now_iso


# An injectable tier-2 LLM call. Provider-agnostic by construction: the caller
# resolves the model and passes a closure that takes a prompt and returns the
# model's text. Tests pass a deterministic stub; the scanner may pass None
# (deterministic-only) until a script-content-modify feed exists. This module
# never names a provider or model.
LlmFn = Callable[[str], str]


# ── Vocabulary ───────────────────────────────────────────────────────────────

DRIFT_SIGNIFICANCE_MAJOR = "major"
DRIFT_SIGNIFICANCE_MINOR = "minor"

# Discriminator stamped on every entry so the unreviewed count (and any future
# producer) can tell scanner-drift entries apart from other drift_log writers.
DRIFT_LOG_SOURCE = "scanner_drift"

# Classifier-path provenance, for audit + idempotency.
CLASSIFIER_DETERMINISTIC = "deterministic"
CLASSIFIER_LLM = "llm"

# Drift shapes (what happened to the surface).
DRIFT_KIND_ADD = "add"
DRIFT_KIND_REMOVE = "remove"
DRIFT_KIND_MODIFY = "modify"

# Surface kinds (what drifted). cron / action / dependency are inherently
# behavioral; ``file`` is classified by its path.
TARGET_FILE = "file"
TARGET_CRON = "cron"
TARGET_ACTION = "action"
TARGET_DEPENDENCY = "dependency"

# A file is an executable/behavioral surface if its extension is code. These
# lists are intentionally explicit — an unknown extension defaults to MINOR
# (don't spam the log) unless the path lives under a scripts/bin dir.
_CODE_EXTENSIONS = frozenset({
    ".py", ".sh", ".bash", ".zsh", ".fish", ".js", ".mjs", ".cjs", ".ts",
    ".rb", ".pl", ".php", ".go", ".rs", ".lua", ".ps1", ".applescript", ".scpt",
})
_DOC_DATA_EXTENSIONS = frozenset({
    ".md", ".markdown", ".txt", ".rst", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".csv", ".tsv", ".html", ".htm", ".xml", ".css",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf", ".log", ".lock",
})
# Path segments that mark a directory of executables even for extension-less
# files (e.g. ``scripts/run`` with a shebang).
_EXECUTABLE_DIR_SEGMENTS = frozenset({"scripts", "bin"})


@dataclass(frozen=True)
class DriftVerdict:
    """Outcome of classifying one drift event."""
    significance: str          # DRIFT_SIGNIFICANCE_MAJOR | _MINOR
    classifier: str            # CLASSIFIER_DETERMINISTIC | CLASSIFIER_LLM

    @property
    def is_major(self) -> bool:
        return self.significance == DRIFT_SIGNIFICANCE_MAJOR


# ── File-path classification ─────────────────────────────────────────────────


def _extension(path: str) -> str:
    """Lower-cased final extension of ``path`` (``''`` if none)."""
    # Defensive: a non-string target (e.g. an int cron label / action id that
    # leaked from a hand-edited manifest) must not raise — treat as no-extension.
    if not isinstance(path, str):
        return ""
    base = (path or "").rsplit("/", 1)[-1]
    if "." not in base or base.startswith("."):
        # No extension, or a dotfile with no further suffix (e.g. ``.gitignore``
        # has no usable type extension for our purposes).
        if base.startswith(".") and base.count(".") >= 2:
            return "." + base.rsplit(".", 1)[-1].lower()
        return ""
    return "." + base.rsplit(".", 1)[-1].lower()


def is_executable_path(path: str) -> bool:
    """True if ``path`` names an executable/behavioral surface (a script).

    A code extension is executable; a doc/data extension is not. An unknown
    extension defaults to NOT executable (→ minor; don't spam the log) unless
    the path lives under a ``scripts/`` or ``bin/`` directory, which marks
    intent even for extension-less runnables.
    """
    ext = _extension(path)
    if ext in _CODE_EXTENSIONS:
        return True
    if ext in _DOC_DATA_EXTENSIONS:
        return False
    if not isinstance(path, str):
        return False
    segments = {seg for seg in path.split("/") if seg}
    return bool(segments & _EXECUTABLE_DIR_SEGMENTS)


def _is_behavioral_surface(target_type: str, target: str) -> bool:
    """Is this drift target an executable/behavioral surface at all?

    cron / action / dependency always are; a file depends on its path.
    """
    if target_type in (TARGET_CRON, TARGET_ACTION, TARGET_DEPENDENCY):
        return True
    if target_type == TARGET_FILE:
        return is_executable_path(target)
    # Unknown target_type → conservative: not behavioral (minor, don't spam).
    return False


# ── LLM escalation (the one ambiguous case) ──────────────────────────────────


def _build_modify_prompt(event: dict) -> str:
    """Prompt for the ambiguous 'did this script's behaviour change' case."""
    target = event.get("target", "")
    detail = event.get("detail") or ""
    return (
        "A script in an installed application changed. Decide whether its "
        "BEHAVIOUR meaningfully changed (a new/removed/altered capability, "
        "schedule, side effect, or output) versus a cosmetic edit (comments, "
        "formatting, logging, rename of a local variable).\n\n"
        f"Script: {target}\n"
        f"Change detail: {detail}\n\n"
        "Answer with exactly one word — MAJOR if the behaviour meaningfully "
        "changed, MINOR if it did not. If you are unsure, answer MINOR."
    )


def _parse_llm_significance(text: Optional[str]) -> str:
    """Parse a model reply into a significance. Conservative: anything that is
    not an unambiguous MAJOR reads as MINOR (default-to-minor on low
    confidence / unparseable / empty)."""
    if not text:
        return DRIFT_SIGNIFICANCE_MINOR
    low = text.strip().lower()
    # Require an explicit major signal and no contradicting minor token.
    has_major = "major" in low
    has_minor = "minor" in low
    if has_major and not has_minor:
        return DRIFT_SIGNIFICANCE_MAJOR
    # "major ... minor" or "minor" or neither → minor (conservative).
    return DRIFT_SIGNIFICANCE_MINOR


# ── Classification ───────────────────────────────────────────────────────────


def classify_drift_event(event: dict, *, llm_fn: Optional[LlmFn] = None) -> DriftVerdict:
    """Classify one reconciliation drift event as major or minor.

    ``event`` is a normalized dict:
        {"kind": add|remove|modify, "target_type": file|cron|action|dependency,
         "target": "<path|label|id>", "detail": "<optional>"}

    Deterministic-first:
      * add / remove of a behavioral surface (script, cron, action, dep) → MAJOR
      * add / remove of a data/doc file                                  → MINOR
      * modify of a cron / action / dependency (a behavioral surface)    → MAJOR
      * modify of a data/doc file                                        → MINOR

    LLM escalation (the only model call):
      * modify of a *script* file — "did its behaviour meaningfully change?"
        is genuinely ambiguous. Escalate to ``llm_fn``; default to MINOR on any
        exception, empty/low-confidence reply, or absent injection.
    """
    kind = event.get("kind")
    target_type = event.get("target_type", "")
    target = event.get("target", "")
    behavioral = _is_behavioral_surface(target_type, target)

    if kind in (DRIFT_KIND_ADD, DRIFT_KIND_REMOVE):
        sig = DRIFT_SIGNIFICANCE_MAJOR if behavioral else DRIFT_SIGNIFICANCE_MINOR
        return DriftVerdict(sig, CLASSIFIER_DETERMINISTIC)

    if kind == DRIFT_KIND_MODIFY:
        if not behavioral:
            # A data/doc file's content changed — cosmetic by definition here.
            return DriftVerdict(DRIFT_SIGNIFICANCE_MINOR, CLASSIFIER_DETERMINISTIC)
        if target_type != TARGET_FILE:
            # A cron / action / dependency changed — behavioral surface, no
            # ambiguity, no model call.
            return DriftVerdict(DRIFT_SIGNIFICANCE_MAJOR, CLASSIFIER_DETERMINISTIC)
        # A *script* file's content changed — the one ambiguous case.
        if llm_fn is None:
            # No injected judge → conservative deterministic floor: a script
            # whose content changed is more likely behavioral than not, but the
            # spec says default-to-minor on the LLM path being unavailable so we
            # don't spam. Treat absent injection as low-confidence → MINOR.
            return DriftVerdict(DRIFT_SIGNIFICANCE_MINOR, CLASSIFIER_DETERMINISTIC)
        try:
            reply = llm_fn(_build_modify_prompt(event))
        except Exception:  # noqa: BLE001 — a flaky model never blocks/spams
            return DriftVerdict(DRIFT_SIGNIFICANCE_MINOR, CLASSIFIER_LLM)
        return DriftVerdict(_parse_llm_significance(reply), CLASSIFIER_LLM)

    # Unknown kind → conservative minor (never spam on a shape we don't model).
    return DriftVerdict(DRIFT_SIGNIFICANCE_MINOR, CLASSIFIER_DETERMINISTIC)


# ── Narration (summary + entry construction) ─────────────────────────────────


def _summarize(event: dict) -> str:
    """One-line human narration of a drift event."""
    kind = event.get("kind")
    target_type = event.get("target_type", "")
    target = event.get("target", "")
    noun = {
        TARGET_FILE: "Script" if is_executable_path(target) else "File",
        TARGET_CRON: "Cron",
        TARGET_ACTION: "Scheduled action",
        TARGET_DEPENDENCY: "Dependency",
    }.get(target_type, "Item")
    if kind == DRIFT_KIND_ADD:
        return f"{noun} {target!r} appeared"
    if kind == DRIFT_KIND_REMOVE:
        return f"{noun} {target!r} removed"
    if kind == DRIFT_KIND_MODIFY:
        return f"{noun} {target!r} changed"
    return f"{noun} {target!r} drifted"


def make_drift_entry(event: dict, verdict: DriftVerdict, *, now: Optional[str] = None) -> dict:
    """Build a ``drift_log`` entry for a (major) drift event. Matches the
    Schema v28 entry shape (manifest.py + manifest-v7-instance.schema.json)."""
    return {
        "ts": now or now_iso(),
        "kind": event.get("kind"),
        "target_type": event.get("target_type", ""),
        "target": event.get("target", ""),
        "significance": DRIFT_SIGNIFICANCE_MAJOR,
        "summary": _summarize(event),
        "reviewed": False,
        "source": DRIFT_LOG_SOURCE,
        "classifier": verdict.classifier,
    }


def _has_unreviewed_duplicate(drift_log: list, event: dict) -> bool:
    """True if an UNREVIEWED scanner-drift entry for the same (kind,
    target_type, target) is already present.

    Idempotency guard: when an authored field STAGES drift (rather than the
    common observational-absorb-and-clear), the same delta recurs on every
    scan. Without this guard the log would accrue a duplicate each pass. Once
    the operator reviews the entry (reviewed=True), a genuine recurrence is
    allowed to re-narrate.
    """
    kind = event.get("kind")
    target_type = event.get("target_type", "")
    target = event.get("target", "")
    for e in drift_log:
        if not isinstance(e, dict):
            continue
        if (
            e.get("source") == DRIFT_LOG_SOURCE
            and e.get("reviewed") is False
            and e.get("kind") == kind
            and e.get("target_type") == target_type
            and e.get("target") == target
        ):
            return True
    return False


def narrate_drift(
    manifest: dict,
    drift_events: list,
    *,
    definition_status: str,
    llm_fn: Optional[LlmFn] = None,
    now: Optional[str] = None,
) -> int:
    """Append MAJOR-drift entries to ``manifest['drift_log']`` and return the
    number appended.

    Only a ``defined`` manifest accrues a narrative (§9.4): a ``discovered``
    app is a churnable draft, so this is a no-op for it (the reconcile pass
    already refreshed its content). MINOR drift is never logged. Duplicate
    unreviewed entries are suppressed (idempotency).

    Mutates ``manifest`` in place. Returns 0 when nothing was appended.
    """
    from .manifest import MANIFEST_DEFINITION_DEFINED

    if (definition_status or "").strip().lower() != MANIFEST_DEFINITION_DEFINED:
        return 0
    if not drift_events:
        return 0

    stamp = now or now_iso()
    drift_log = manifest.setdefault("drift_log", [])
    if not isinstance(drift_log, list):
        # Defensive: a corrupt non-list value is replaced rather than crashing.
        drift_log = []
        manifest["drift_log"] = drift_log

    appended = 0
    for event in drift_events:
        if not isinstance(event, dict):
            continue
        verdict = classify_drift_event(event, llm_fn=llm_fn)
        if not verdict.is_major:
            continue
        if _has_unreviewed_duplicate(drift_log, event):
            continue
        drift_log.append(make_drift_entry(event, verdict, now=stamp))
        appended += 1
    return appended


# ── Unreviewed marker (data source for the Bite-4 tile badge) ────────────────


def count_unreviewed_drift(manifest: dict) -> int:
    """Count of unreviewed scanner-drift entries in ``manifest['drift_log']``.

    This is the data source for the Bite-4 tile badge ("N changes since you
    looked"). Filters on the ``scanner_drift`` source AND ``reviewed is
    False`` so it is robust even if another producer ever shares the log.
    Pure / side-effect-free — safe to call per-app on a grid render.
    """
    total = 0
    for e in manifest.get("drift_log") or []:
        if (
            isinstance(e, dict)
            and e.get("source") == DRIFT_LOG_SOURCE
            and e.get("reviewed") is False
        ):
            total += 1
    return total


def iter_scanner_drift(manifest: dict, *, unreviewed_only: bool = False) -> list:
    """Return slim copies of the scanner-drift entries, newest-first.

    The Bite-4 drift panel lists these (summary / kind / target / ts /
    reviewed) and the tile badge counts the unreviewed subset. Slim so the
    Apps grid payload stays light — the raw entries also carry ``source`` /
    ``significance`` / ``classifier`` audit fields the operator surface does
    not need. Pure / side-effect-free.
    """
    out: list = []
    for e in manifest.get("drift_log") or []:
        if not isinstance(e, dict) or e.get("source") != DRIFT_LOG_SOURCE:
            continue
        if unreviewed_only and e.get("reviewed") is not False:
            continue
        out.append({
            "ts": e.get("ts", ""),
            "kind": e.get("kind", ""),
            "target_type": e.get("target_type", ""),
            "target": e.get("target", ""),
            "summary": e.get("summary", ""),
            "reviewed": bool(e.get("reviewed")),
        })
    out.sort(key=lambda d: d.get("ts", ""), reverse=True)
    return out


def mark_drift_reviewed(manifest: dict, *, reviewed: bool = True, targets=None) -> int:
    """Flip the ``reviewed`` flag on scanner-drift entries IN PLACE; return the
    number of entries actually changed.

    ``targets`` — when ``None``, every scanner-drift entry is flipped (the
    "mark all reviewed" affordance). Otherwise an iterable of ``ts`` strings
    selecting which entries to flip (the panel sends the ``ts`` of each listed
    entry; entries minted in the same scan share a ``ts`` and flip together).

    Additive + reversible: pass ``reviewed=False`` to un-review. This NEVER
    deletes an entry, so a genuine recurrence after review can still
    re-narrate (cf. ``_has_unreviewed_duplicate``). Only touches
    ``scanner_drift``-sourced entries — any other drift_log producer is left
    untouched.
    """
    target_set = None if targets is None else {str(t) for t in targets}
    want = bool(reviewed)
    changed = 0
    for e in manifest.get("drift_log") or []:
        if not isinstance(e, dict) or e.get("source") != DRIFT_LOG_SOURCE:
            continue
        if target_set is not None and str(e.get("ts", "")) not in target_set:
            continue
        if bool(e.get("reviewed")) == want:
            continue
        e["reviewed"] = want
        changed += 1
    return changed
