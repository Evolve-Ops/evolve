"""Coherence Pass A — manifest-internal graph walk (PR 4 of the framework).

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §6.1.

Pass A is the cheapest coherence check: pure Python, no filesystem, no
subprocess, no LLM. Targets ~10ms per app. Catches the class of failure
where a manifest's CLAIMS don't hang together internally — e.g., a
description claiming "daily briefing" with empty ``scheduled_actions[]``,
``crons[]``, and no heartbeat trigger.

Importantly, Pass A is **provenance-independent**: even a fully
observational manifest gets checked. If the scanner-discovered
description claims something the implementation can't possibly do,
the operator should see it regardless of whether the field was authored
or observed. The reasoning is in spec §4.5.

Pass A is also **shape-independent**: it reads both the legacy single-file
manifest and the v7-arc Instance shape (spec docs/spec-manifest-v7-2026-05-20.md).
v7-arc Instances put their file roster in ``realized_files[]`` (legacy
``files[]`` empty) and their schedules in ``configured_schedules[]`` /
Spec-side ``schedules[]`` / ``event_triggers[]`` (legacy
``scheduled_actions[]`` / ``crons[]`` empty). The helpers read the UNION of
both shapes so a v7-arc app is evaluated against its real surfaces — without
this, every v7-arc Instance fires false C-A1 (no trigger found) and false
C-A2 (input not in the empty ``files[]``). Callers that have a ``shared_dir``
should additionally hydrate the Instance first (overlays the bound Spec's
objective / description) — see ``pass_runner.hydrate_if_needed``; the scanner
Phase 7 path does this.

## The 8 assertions

Per §6.1. Each returns a list of findings; the runner aggregates and
writes them into ``manifest.coherence.findings[]``.

  C-A1: recurring-behavior phrase in description but no triggers
        declared → critical
  C-A2: scheduled_action.inputs[*].path missing from files[] / vp glob
        AND not provided by a declared app_dependency → major
  C-A3: scheduled_action.outputs[*] with no producing mechanism → major
  C-A4: messaging output without messaging integration → critical
  C-A5: crons[*].script not in files[] as code → major
  C-A6: code file not referenced anywhere → minor (orphan code)
  C-A7: requirements.integrations[*] not referenced → minor
  C-A8: interface_contract.cli[*] command doesn't resolve → major

## Skip rules

Two manifest-side flags suppress findings:

  scheduled_actions[*].state != "active"
      Disabled / paused actions don't fire findings — the operator
      intentionally turned them off (e.g., the protein-daily-checkin
      DISABLED pattern surfaced during validation).

  scheduled_actions[*].quality == "suspect"
      Legacy noisy entries the scanner over-extracted (validation
      against production confirmed many such entries — first-line
      excerpts of AGENTS.md sections captured as if they were scheduled
      behaviors). Without this skip, Pass A would flood operators with
      false positives on every existing manifest on day 1.

  coherence.coherence_accepted[]
      Signatures the operator has explicitly accepted; Pass A drops
      matching findings before output. Operators use this when a
      finding is technically true but intentional.

## Output

``apply_pass_a`` writes ``manifest.coherence``:

    {
        "last_checked_at": "<iso>",
        "status": "ok | warnings | incoherent",
        "findings": [
            {"id": "C-A1", "severity": "critical", "assertion": "...",
             "description": "...", "evidence": [...]},
            ...
        ],
    }

Status:
    ok          — no findings
    warnings    — findings, all minor / info
    incoherent  — at least one critical or major finding
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .. import channel_registry as _channel_registry


# ── Severity (matches the rest of the audit vocabulary) ─────────────────────

SEVERITY_CRITICAL = "critical"
SEVERITY_MAJOR    = "major"
SEVERITY_MINOR    = "minor"
SEVERITY_INFO     = "info"


# Stable assertion IDs surfaced in findings + signatures. Don't rename —
# operator-accepted signatures and the trail use these as keys.
ASSERTION_IDS = (
    "C-A1",   # recurring behavior without trigger
    "C-A2",   # scheduled_action input missing from files / volatile_paths / dependency
    "C-A3",   # scheduled_action output with no producing mechanism
    "C-A4",   # messaging output without messaging integration
    "C-A5",   # cron script missing from files (as code layer)
    "C-A6",   # orphan code file (never referenced)
    "C-A7",   # declared integration never used
    "C-A8",   # interface_contract CLI command doesn't resolve
)


# Phrases that signal "this app does something on a schedule" inside
# free-form prose (description, usage.how_to_use). Used by assertion
# C-A1. Case-insensitive substring match; we accept false positives at
# this layer because escalating to a critical finding is cheap if
# wrong (operator marks accepted or rewrites the claim).
_RECURRING_BEHAVIOR_PHRASES = (
    "daily",
    "every morning",
    "every evening",
    "every night",
    "weekly",
    "monthly",
    "quarterly",
    "every hour",
    "every day",
    "every week",
    "every month",
    "every n hours",   # spec example phrasing
    "every few hours",
    "each morning",
    "each evening",
    "each day",
    "each week",
    "twice daily",
    "twice a day",
    "every 6 hours",
    "every 12 hours",
    "every 24 hours",
    "nightly",
    "morning briefing",
    "evening summary",
    "scheduled",
)


# Output kinds delivered via the bot's OWN session turn rather than an
# app-declared messaging integration. A v7-arc heartbeat/cron action that
# surfaces results to the operator (spec docs/spec-manifest-v7-2026-05-20.md
# §3.4 — heartbeat instructions "surface to the operator") emits a
# ``session_message``: the bot relays it through whatever base channel the
# operator already uses, which is NOT an app-level ``requirements.integrations``
# dependency. Treating it as one fires a false C-A4 on essentially every
# v7-arc heartbeat app (validated against several task/tracker apps on the
# pod 2026-06-11). C-A3 still treats it as a message-shaped output (no file
# producer required); only the C-A4 integration requirement is waived.
_SELF_DELIVERED_OUTPUT_KINDS = frozenset({
    "session_message",
})


# Integration ids that imply messaging capability. Used by C-A4 to
# decide whether ``requirements.integrations[]`` actually has a
# messaging-capable entry. Conservative list — better to miss a finding
# than fire on a slack integration that's "messaging" by some
# definition.
# Vendor ids that are not channels but imply a messaging capability when an
# app declares them as an integration. They have no channel-registry row (a
# bot is never "on gmail" the way it is on Telegram) so they stay local.
_MESSAGING_VENDOR_ALIASES = frozenset({"gmail", "twilio"})

# Registry channels flagged ``messaging_integration`` + the vendor aliases.
# ``webhook`` is deliberately excluded by the registry column: a webhook is a
# delivery sink, not a channel a person is reachable on, so it must not
# satisfy a C-A4 messaging requirement.
_MESSAGING_INTEGRATION_IDS = (
    _channel_registry.messaging_integration_ids() | _MESSAGING_VENDOR_ALIASES
)

# Public alias — the channel-state reader (evolve_admin.channels) and
# the forge approval stamp consume the same vocabulary so "messaging-
# capable" means one thing everywhere. Data, not logic: adding a new
# provider here is the whole change.
MESSAGING_INTEGRATION_IDS = _MESSAGING_INTEGRATION_IDS


# usage.model values that exempt an app from the C-A1 recurring-behavior
# check. Library/on-demand apps are CALLED BY other apps — the "daily"
# phrase in their description refers to a caller's schedule, not this
# app's own behavior.
#
# Production validation 2026-06-08: Biometric Integration is a Whoop
# OAuth token-management library; its example_triggers include
# "System: Scheduled job to sync biometric data runs daily → Calls
# get_token()". The C-A1 prose check saw "daily" in description and
# fired critical despite the app being correctly designed as reactive.
_RECURRING_EXEMPT_MODELS = frozenset({
    "library",       # called by other apps; no schedule of its own
    "on-demand",     # synonym; user/caller invokes when needed
    "on_demand",     # underscore variant for tolerance
})


@dataclass
class CoherenceFinding:
    """One Pass A finding. Shape matches spec §6.1 example."""
    id: str                          # one of ASSERTION_IDS
    severity: str                    # SEVERITY_*
    assertion: str                   # short snake_case assertion label
    description: str                 # one-line human-readable
    evidence: list[dict] = field(default_factory=list)

    def signature(self) -> str:
        """Stable signature for accepted_signatures matching.

        Operators ``Approve`` findings via the chip flow (spec §11.2);
        the accepted signature lands in ``coherence.coherence_accepted[]``.
        Pass A drops findings whose signature appears there.
        """
        ev_blob = ""
        for ev in self.evidence[:3]:   # cap so order changes are bounded
            for k in sorted(ev.keys()):
                ev_blob += f"{k}={ev[k]!s};"
        raw = f"{self.id}|{self.assertion}|{ev_blob}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "severity": self.severity,
            "assertion": self.assertion,
            "description": self.description,
            "evidence": list(self.evidence),
            "signature": self.signature(),
        }


# ── Helpers ─────────────────────────────────────────────────────────────────


def _active_scheduled_actions(manifest: dict) -> list[dict]:
    """Pull scheduled_actions[] entries that should fire findings.

    Filters per the skip rules:
      - state != "active" → skipped (operator intentionally disabled)
      - quality == "suspect" → skipped (legacy over-extraction noise)
    """
    out: list[dict] = []
    for entry in manifest.get("scheduled_actions") or []:
        if not isinstance(entry, dict):
            continue
        if (entry.get("state") or "active") != "active":
            continue
        if (entry.get("quality") or "") == "suspect":
            continue
        out.append(entry)
    return out


# Extensions the layer classifier (spec §3.5) maps to the ``code`` layer.
# Used to infer a layer for v7-arc ``realized_files[]`` entries, which —
# unlike legacy ``files[]`` — don't carry an explicit ``layer`` stamp.
_CODE_EXTS = (".py", ".sh", ".bash", ".js", ".ts", ".rb")


def _infer_layer_from_path(path: str) -> str:
    """Infer a ``files[*].layer`` value from a path's extension.

    Only ``code`` matters to Pass A's code-layer assertions (C-A3 / C-A5 /
    C-A6); everything else returns ``""`` (unknown) so it's treated as a
    non-code file. This is the extension rung of the spec §3.5 classifier,
    sufficient because the code-vs-not distinction is all Pass A needs.
    """
    import os
    ext = os.path.splitext(path)[1].lower()
    return "code" if ext in _CODE_EXTS else ""


def _iter_declared_files(manifest: dict):
    """Yield every file the manifest declares — legacy ``files[]`` ∪ v7-arc
    ``realized_files[]`` — as uniform ``{path, layer, ...}`` dicts.

    v7-arc Instances put their file roster in ``realized_files[]`` and leave
    legacy ``files[]`` empty (the scanner runs Pass A on the RAW on-disk
    Instance; spec docs/spec-manifest-v7-2026-05-20.md §4). Without reading
    the union, every file-resolution check (C-A2 inputs, C-A5 cron scripts,
    C-A8 CLI) and every code-layer check (C-A3 / C-A6) silently mis-evaluates
    a v7-arc app — the bug this helper closes.

    Legacy ``files[]`` entries pass through verbatim (they carry an explicit
    ``layer``). ``realized_files[]`` entries are normalized to the same shape
    with ``layer`` inferred from the extension and ``_realized: True`` flagged
    so the orphan check (C-A6) can treat them as owned-by-declaration.
    """
    for entry in manifest.get("files") or []:
        if isinstance(entry, dict):
            yield entry
    for rf in manifest.get("realized_files") or []:
        if not isinstance(rf, dict):
            continue
        path = rf.get("path") or ""
        if not path:
            continue
        yield {
            "path": path,
            "layer": rf.get("layer") or _infer_layer_from_path(path),
            "description": rf.get("logical_name", ""),
            "file_id": rf.get("file_id", ""),
            "_realized": True,
        }


def _has_recurring_trigger_mechanism(manifest: dict) -> bool:
    """True if the manifest declares ANY mechanism that could drive a
    recurring behavior — across both the legacy and the v7-arc shapes.

    Legacy surfaces: an active ``scheduled_actions[]`` entry, ``crons[]``,
    or ``oc_heartbeat_instruction``.

    v7-arc surfaces: ``configured_schedules[]`` (the Instance's resolved
    schedules) plus ``schedules[]`` / ``event_triggers[]`` (Spec-side trigger
    declarations, present on the manifest once it's been hydrated — see
    manifest.hydrate_v7_arc_instance). Reading these is what stops C-A1 from
    firing falsely on a v7-arc app whose schedule lives in
    ``configured_schedules`` rather than legacy ``scheduled_actions[]``.
    """
    if _active_scheduled_actions(manifest):
        return True
    if manifest.get("crons"):
        return True
    if manifest.get("oc_heartbeat_instruction"):
        return True
    # v7-arc trigger surfaces.
    if manifest.get("configured_schedules"):
        return True
    if manifest.get("schedules"):
        return True
    if manifest.get("event_triggers"):
        return True
    return False


def _files_by_path(manifest: dict) -> dict[str, dict]:
    """Index declared files (legacy ``files[]`` ∪ v7-arc ``realized_files[]``)
    by path for O(1) lookup."""
    out: dict[str, dict] = {}
    for entry in _iter_declared_files(manifest):
        path = (entry.get("path") or "").lstrip("/")
        if path:
            out.setdefault(path, entry)
    return out


def _code_files(manifest: dict) -> list[dict]:
    """Declared files (legacy ∪ v7-arc realized) with layer == 'code'.

    Legacy ``files[]`` carry the post-PR-3 classifier's ``layer``; v7-arc
    ``realized_files[]`` get ``layer`` inferred from the extension by
    ``_iter_declared_files``."""
    return [
        e for e in _iter_declared_files(manifest)
        if e.get("layer") == "code"
    ]


def _volatile_path_globs(manifest: dict) -> list[str]:
    """Glob patterns from volatile_paths[]."""
    return [
        vp.get("glob", "") for vp in (manifest.get("volatile_paths") or [])
        if isinstance(vp, dict) and vp.get("glob")
    ]


def _matches_any_glob(path: str, globs: list[str]) -> bool:
    import fnmatch
    return any(fnmatch.fnmatch(path, g) for g in globs)


def _declared_dependency_keys(manifest: dict) -> set[str]:
    """pkg_ids / spec_ids and (lowercased) display_names of declared
    dependencies — the set of apps this manifest legitimately reads
    cross-app data from.

    A scheduled_action input that reads another app's output (C-A2) is
    satisfiable only against an app the manifest actually declares it
    depends on; this is the valid-provider set.

    Reads both shapes (Pass A is shape-independent): the legacy
    ``app_dependencies[]`` ({pkg_id, display_name, ...}) and the v7-arc
    ``apps[]`` ({spec_id, ...}, where spec_id == pkg_id) the migration
    produces. Defensive against a hybrid manifest that carries legacy
    ``scheduled_actions[]`` inputs alongside a migrated ``apps[]`` list.
    """
    keys: set[str] = set()
    for dep in manifest.get("app_dependencies") or []:
        if not isinstance(dep, dict):
            continue
        pid = dep.get("pkg_id")
        if pid:
            keys.add(str(pid))
        name = dep.get("display_name")
        if name:
            keys.add(str(name).strip().lower())
    for dep in manifest.get("apps") or []:
        if not isinstance(dep, dict):
            continue
        sid = dep.get("spec_id") or dep.get("pkg_id")
        if sid:
            keys.add(str(sid))
    return keys


def _input_from_declared_dependency(inp: dict, dep_keys: set[str]) -> bool:
    """True when a scheduled_action input is provided by one of the app's
    declared ``app_dependencies[]`` — the "watcher reads a dependency's
    output" pattern (e.g. Evening Sweep reads Task Manager's ``tasks.json``).

    The input self-declares as dependency-provided two ways, both honored:
      - ``kind == "dependency"`` — read from a declared dependency app;
      - ``from_dependency`` / ``provider_pkg_id`` — names the provider
        (pkg_id or display_name).

    When a provider is named it MUST match a declared dependency; naming a
    provider the manifest doesn't depend on is NOT satisfied (the
    dependency is undeclared — a real gap C-A2 should keep surfacing).
    When ``kind == "dependency"`` with no named provider, any declared
    dependency satisfies it. With no declared dependencies at all nothing
    is satisfied — claiming a dependency input while declaring no
    dependency is itself the incoherence.

    Without this carve-out the entire cross-app watcher shape can never
    pass C-A2: the input file is owned by the dependency, so it is never
    in the watcher's own ``files[]`` / ``volatile_paths[]``.
    """
    provider = (
        inp.get("from_dependency") or inp.get("provider_pkg_id") or ""
    ).strip()
    if provider:
        return provider in dep_keys or provider.lower() in dep_keys
    if (inp.get("kind") or "").lower() == "dependency":
        return bool(dep_keys)
    return False


def _contains_recurring_behavior_phrase(text: str) -> str | None:
    """Return the first matching phrase, or None if no match."""
    if not text:
        return None
    lower = text.lower()
    for phrase in _RECURRING_BEHAVIOR_PHRASES:
        if phrase in lower:
            return phrase
    return None


def _claims_messaging_output(action: dict) -> bool:
    """True if the action's outputs[] declare a messaging delivery."""
    for out in action.get("outputs") or []:
        if not isinstance(out, dict):
            continue
        kind = (out.get("kind") or "").lower()
        if kind in _SELF_DELIVERED_OUTPUT_KINDS:
            # Delivered via the bot's own session turn — uses the bot's base
            # channel, not an app-declared messaging integration.
            continue
        if "message" in kind or kind == "messaging_channel" or "channel" in kind:
            return True
        target = (out.get("target") or "").lower()
        if any(m in target for m in _MESSAGING_INTEGRATION_IDS):
            return True
    return False


def _has_messaging_integration(manifest: dict) -> bool:
    integrations = (manifest.get("requirements") or {}).get("integrations") or []
    for integ in integrations:
        if isinstance(integ, dict):
            iid = (integ.get("id") or "").lower()
        elif isinstance(integ, str):
            iid = integ.lower()
        else:
            continue
        if iid in _MESSAGING_INTEGRATION_IDS:
            return True
    return False


def manifest_missing_messaging_integration(manifest: dict) -> bool:
    """True when this manifest would trip C-A4: some active
    scheduled_action declares a messaging output, but
    ``requirements.integrations[]`` has no messaging-capable entry.

    Used by the forge approval path to decide whether to declare the
    bot's *connected* channel(s) on the manifest before the gate runs —
    so C-A4 ends up verifying real channel state: a bot with a live
    channel ships a briefing that declares it; a channel-less bot still
    gets the (correct) refusal."""
    if _has_messaging_integration(manifest):
        return False
    return any(
        _claims_messaging_output(action)
        for action in _active_scheduled_actions(manifest)
    )


def _integration_ids(manifest: dict) -> list[str]:
    out = []
    for integ in (manifest.get("requirements") or {}).get("integrations") or []:
        if isinstance(integ, dict):
            iid = integ.get("id") or ""
        elif isinstance(integ, str):
            iid = integ
        else:
            continue
        if iid:
            out.append(iid)
    return out


def _file_referenced_anywhere(file_entry: dict, manifest: dict) -> bool:
    """C-A6 + C-A7 helper. True if path/basename appears anywhere
    structurally — scheduled_actions, crons, test_command,
    interface_contract.cli, evidence_files, usage prose, or any other
    code file's name reference.

    ``evidence_files`` and ``usage.how_to_use`` were added 2026-06-07
    after production validation surfaced false positives on
    content-store apps (e.g. atlas_knowledge.py). The scanner records
    every file it attributed to the app in ``evidence_files``; if the
    file is there, the manifest is saying "this is mine" — that's a
    legitimate reference even when no other field cites it.
    """
    import os
    path = (file_entry.get("path") or "").lstrip("/")
    if not path:
        return False
    basename = os.path.basename(path)
    needles = {path, basename}

    # scheduled_actions any field
    for action in manifest.get("scheduled_actions") or []:
        if not isinstance(action, dict):
            continue
        blob = str(action)
        if any(n in blob for n in needles):
            return True

    # v7-arc surfaces, symmetric with scheduled_actions/crons above:
    # configured_schedules[] (Instance) and realized_files[] (a file
    # referenced by another realized file's logical_name / path).
    for sched in manifest.get("configured_schedules") or []:
        if isinstance(sched, dict):
            blob = str(sched)
            if any(n in blob for n in needles):
                return True
    for rf in manifest.get("realized_files") or []:
        if not isinstance(rf, dict):
            continue
        rf_path = (rf.get("path") or "").lstrip("/")
        if rf_path == path:
            continue  # don't count the file as referencing itself
        blob = " ".join(
            str(v) for v in (rf.get("path"), rf.get("logical_name")) if v
        )
        if any(n in blob for n in needles):
            return True

    # crons[*].script and .label
    for cron in manifest.get("crons") or []:
        if isinstance(cron, str):
            if any(n in cron for n in needles):
                return True
        elif isinstance(cron, dict):
            blob = " ".join(
                str(v) for v in (cron.get("script"), cron.get("label"),
                                 cron.get("command"))
                if v
            )
            if any(n in blob for n in needles):
                return True

    # test_command
    tc = manifest.get("test_command") or ""
    if any(n in tc for n in needles):
        return True

    # interface_contract.cli
    for cli in (manifest.get("interface_contract") or {}).get("cli") or []:
        if isinstance(cli, dict):
            cmd = cli.get("command") or ""
        elif isinstance(cli, str):
            cmd = cli
        else:
            continue
        if any(n in cmd for n in needles):
            return True

    # evidence_files — the scanner's own attribution. If the scanner
    # saw the file as part of this app, the manifest is OWNED by the
    # app even when no other field cites it. Content-store apps
    # (atlas_knowledge, memory continuity, etc.) typically have files
    # that aren't called from cron/action — they're invoked by the
    # bot's LLM via INSTALLED_APPS.md.
    for ev in manifest.get("evidence_files") or []:
        if isinstance(ev, str):
            if any(n in ev for n in needles):
                return True
        elif isinstance(ev, dict):
            blob = " ".join(str(v) for v in ev.values() if v)
            if any(n in blob for n in needles):
                return True

    # usage.how_to_use prose. When the manifest documents how the LLM
    # invokes the app (e.g. "run `python3 scripts/X.py --tag foo`"),
    # that's a legitimate reference even though it's free-form text.
    usage = manifest.get("usage")
    if isinstance(usage, dict):
        how = usage.get("how_to_use") or ""
        if any(n in how for n in needles):
            return True
    identity = manifest.get("identity")
    if isinstance(identity, dict):
        nested_usage = identity.get("usage")
        if isinstance(nested_usage, dict):
            how = nested_usage.get("how_to_use") or ""
            if any(n in how for n in needles):
                return True

    # capability_tags / session_keywords — when the file is named
    # after a tag and the LLM routes to the app via tag matching, this
    # is the "reference" path. Token-based match: split both sides on
    # space/dash/underscore and check overlap. A 2-token overlap is
    # required to avoid spurious hits ("backup.py" against a tag list
    # that happens to contain "back").
    import re
    stem = os.path.splitext(basename)[0]
    if stem:
        stem_tokens = {
            t.lower() for t in re.split(r"[\s_\-]+", stem) if len(t) > 2
        }
        if stem_tokens:
            for tag_field in ("capability_tags", "session_keywords"):
                for tag in manifest.get(tag_field) or []:
                    if not isinstance(tag, str):
                        continue
                    tag_tokens = {
                        t.lower() for t in re.split(r"[\s_\-]+", tag)
                        if len(t) > 2
                    }
                    # Full overlap on a single-token stem OR ≥2 token
                    # overlap on multi-token stem.
                    common = stem_tokens & tag_tokens
                    if len(stem_tokens) == 1 and common:
                        return True
                    if len(stem_tokens) >= 2 and len(common) >= 2:
                        return True

    return False


# ── Assertions ──────────────────────────────────────────────────────────────


def _suspect_scheduled_actions(manifest: dict) -> list[dict]:
    """Active scheduled_actions whose quality is 'suspect'.

    Pod-wide-validation 2026-06-06: every personal-bot heartbeat-driven app
    has 8-11 active entries all tagged ``quality: "suspect"`` (legacy
    over-extraction from prose docs). C-A1 must distinguish "no
    actions at all" from "actions exist but none are quality-confirmed"
    — same root condition, but different operator response.
    """
    out: list[dict] = []
    for entry in manifest.get("scheduled_actions") or []:
        if not isinstance(entry, dict):
            continue
        if (entry.get("state") or "active") != "active":
            continue
        if (entry.get("quality") or "") == "suspect":
            out.append(entry)
    return out


def check_c_a1_recurring_without_trigger(manifest: dict) -> list[CoherenceFinding]:
    """C-A1: description / usage contains recurring-behavior phrase but
    every trigger surface is empty. The "briefing-with-no-trigger" case
    (spec §6.1 #1).

    Critical. Catches the protein-reminder-style failure where the
    manifest claims a daily behavior but nothing schedules it.

    Suspect-quality variant — when scheduled_actions[] has entries but
    ALL are quality:"suspect", the finding is recategorized as a
    *minor* "promote-or-prune" prompt rather than a critical bug.
    Production tally (2026-06-06): every personal-bot heartbeat-driven app hit
    the original critical incorrectly because the suspect filter strips
    entries before the count.

    Library/on-demand exemption (2026-06-08): apps whose
    ``usage.model`` is ``library`` or ``on-demand`` are called by other
    apps; the "daily" phrase in their description typically refers to
    a CALLER's schedule, not the library's own behavior. The biometric-
    integration false positive (production validation 2026-06-08)
    surfaced this: a token-management library claimed "daily" in its
    description because its example_triggers mention a daily caller —
    C-A1 incorrectly flagged it as critical.
    """
    # Library/on-demand apps are exempt — their description mentions
    # "daily" only as context for the callers that invoke them.
    usage = manifest.get("usage") or {}
    if isinstance(usage, dict):
        model = (usage.get("model") or "").strip().lower()
        if model in _RECURRING_EXEMPT_MODELS:
            return []
    # Also accept identity.usage.model placement.
    identity = manifest.get("identity") or {}
    if isinstance(identity, dict):
        nested_usage = identity.get("usage") or {}
        if isinstance(nested_usage, dict):
            model = (nested_usage.get("model") or "").strip().lower()
            if model in _RECURRING_EXEMPT_MODELS:
                return []

    desc = manifest.get("description") or ""
    usage_how = ""
    if isinstance(usage, dict):
        usage_how = usage.get("how_to_use") or ""
    phrase = (_contains_recurring_behavior_phrase(desc)
              or _contains_recurring_behavior_phrase(usage_how))
    if not phrase:
        return []

    # Any active trigger surface qualifies as "the claim is supported".
    # Covers both legacy (scheduled_actions / crons / heartbeat) and v7-arc
    # (configured_schedules / Spec schedules / event_triggers) shapes.
    if _has_recurring_trigger_mechanism(manifest):
        return []

    # If we get here: nothing quality-confirmed schedules the claimed
    # behavior. Check whether suspect-quality entries exist — if so,
    # emit the softer variant.
    suspect = _suspect_scheduled_actions(manifest)
    if suspect:
        return [CoherenceFinding(
            id="C-A1",
            severity=SEVERITY_MINOR,
            assertion="recurring_behavior_only_suspect_actions",
            description=(
                f"manifest claims recurring behavior ({phrase!r}) and "
                f"{len(suspect)} scheduled_action(s) are present but all "
                f"are tagged quality:'suspect' (legacy over-extraction) "
                f"— promote one to 'confirmed' or prune them"
            ),
            evidence=[
                {"field": "description", "phrase": phrase},
                {"field": "scheduled_actions",
                 "active_count": 0,
                 "suspect_count": len(suspect)},
            ],
        )]

    return [CoherenceFinding(
        id="C-A1",
        severity=SEVERITY_CRITICAL,
        assertion="recurring_behavior_without_trigger",
        description=(
            f"manifest claims recurring behavior ({phrase!r}) but no "
            f"trigger is declared — scheduled_actions[], crons[], "
            f"configured_schedules[], schedules[], event_triggers[], and "
            f"oc_heartbeat_instruction are all empty / disabled"
        ),
        evidence=[
            {"field": "description", "phrase": phrase},
            {"field": "scheduled_actions", "active_count": 0},
            {"field": "crons", "count": 0},
        ],
    )]


def check_c_a2_action_inputs_resolve(manifest: dict) -> list[CoherenceFinding]:
    """C-A2: every active scheduled_action.inputs[*].path appears in
    files[] OR matches a volatile_paths[] glob OR is provided by a
    declared ``app_dependencies[]`` app. Skips inputs whose kind is
    'external' (the spec carve-out).

    The dependency carve-out (``_input_from_declared_dependency``) is what
    lets a watcher read another app's output — a watcher that reads a
    dependency's data file (the input is owned by the dependency, never by
    the watcher's own ``files[]``) would otherwise always trip C-A2.

    Severity: major.
    """
    files = _files_by_path(manifest)
    globs = _volatile_path_globs(manifest)
    dep_keys = _declared_dependency_keys(manifest)
    findings: list[CoherenceFinding] = []
    for action in _active_scheduled_actions(manifest):
        for inp in action.get("inputs") or []:
            if not isinstance(inp, dict):
                continue
            kind = (inp.get("kind") or "").lower()
            if kind == "external":
                continue
            if _input_from_declared_dependency(inp, dep_keys):
                continue
            path = (inp.get("path") or "").lstrip("/")
            if not path:
                continue
            if path in files:
                continue
            if _matches_any_glob(path, globs):
                continue
            findings.append(CoherenceFinding(
                id="C-A2",
                severity=SEVERITY_MAJOR,
                assertion="action_input_not_resolved",
                description=(
                    f"scheduled_action {action.get('id', '?')!r} declares "
                    f"input {path!r} not in files[] or volatile_paths[]"
                ),
                evidence=[
                    {"action_id": action.get("id"),
                     "input_path": path},
                ],
            ))
    return findings


def check_c_a3_action_outputs_have_producer(manifest: dict) -> list[CoherenceFinding]:
    """C-A3: every active scheduled_action.outputs[*] has SOMEONE who
    could plausibly produce it — code file, messaging integration, or
    volatile_paths entry.

    Severity: major. Heuristic — the name-match for code producer is
    weak, so this can produce false positives on apps with terse
    output names. Operators accept-and-mute when wrong.
    """
    findings: list[CoherenceFinding] = []
    code_paths = [
        (f.get("path") or "").lstrip("/")
        for f in _code_files(manifest)
    ]
    has_messaging = _has_messaging_integration(manifest)
    globs = _volatile_path_globs(manifest)

    for action in _active_scheduled_actions(manifest):
        for out in action.get("outputs") or []:
            if not isinstance(out, dict):
                continue
            kind = (out.get("kind") or "").lower()
            # Messaging outputs are satisfied by messaging integrations.
            if "message" in kind or "channel" in kind:
                if has_messaging:
                    continue
                # No messaging integration → C-A4 will flag at critical.
                # Skip here to avoid duplicate findings.
                continue
            # File outputs need either a code producer or a
            # volatile_paths glob.
            target = (out.get("target") or out.get("path") or "").lstrip("/")
            if target and _matches_any_glob(target, globs):
                continue
            if target and any(target in cp or cp in target for cp in code_paths):
                continue
            findings.append(CoherenceFinding(
                id="C-A3",
                severity=SEVERITY_MAJOR,
                assertion="action_output_no_producer",
                description=(
                    f"scheduled_action {action.get('id', '?')!r} declares "
                    f"output {target or kind!r} with no plausible "
                    f"producing mechanism (no code file, no messaging "
                    f"integration, no volatile_paths match)"
                ),
                evidence=[
                    {"action_id": action.get("id"),
                     "output_kind": kind,
                     "output_target": target},
                ],
            ))
    return findings


def check_c_a4_messaging_output_needs_integration(manifest: dict) -> list[CoherenceFinding]:
    """C-A4: any scheduled_action declaring messaging output requires a
    messaging-capable integration in requirements.integrations[].

    Severity: critical. The asymmetry here is critical (vs C-A3's major)
    because a missing messaging integration means the user never sees
    the output — exactly the production failure mode validated on the
    pod.
    """
    if _has_messaging_integration(manifest):
        return []
    findings: list[CoherenceFinding] = []
    for action in _active_scheduled_actions(manifest):
        if _claims_messaging_output(action):
            findings.append(CoherenceFinding(
                id="C-A4",
                severity=SEVERITY_CRITICAL,
                assertion="messaging_output_no_integration",
                description=(
                    f"scheduled_action {action.get('id', '?')!r} declares "
                    f"a messaging output but requirements.integrations[] "
                    f"contains no messaging-capable entry "
                    f"({sorted(_MESSAGING_INTEGRATION_IDS)})"
                ),
                evidence=[
                    {"action_id": action.get("id")},
                ],
            ))
            # One finding per app — they all share the same root cause.
            break
    return findings


def check_c_a5_cron_script_in_files(manifest: dict) -> list[CoherenceFinding]:
    """C-A5: every crons[*].script must resolve to a files[] entry with
    layer 'code'. Severity: major.
    """
    files = _files_by_path(manifest)
    findings: list[CoherenceFinding] = []
    for cron in manifest.get("crons") or []:
        if isinstance(cron, str):
            # Legacy string-form cron; can't extract a script without
            # parsing the line. Skip — check_cron_schedules in the
            # structural verifier handles unparseable entries.
            continue
        if not isinstance(cron, dict):
            continue
        script = (cron.get("script") or "").lstrip("/")
        if not script:
            continue
        entry = files.get(script)
        if entry is None:
            findings.append(CoherenceFinding(
                id="C-A5",
                severity=SEVERITY_MAJOR,
                assertion="cron_script_not_in_files",
                description=(
                    f"cron {cron.get('label', '?')!r} script {script!r} "
                    f"is not declared in files[]"
                ),
                evidence=[
                    {"cron_label": cron.get("label"),
                     "script": script},
                ],
            ))
            continue
        if entry.get("layer") != "code":
            findings.append(CoherenceFinding(
                id="C-A5",
                severity=SEVERITY_MAJOR,
                assertion="cron_script_not_code_layer",
                description=(
                    f"cron {cron.get('label', '?')!r} script {script!r} "
                    f"is in files[] but layer is "
                    f"{entry.get('layer', 'unknown')!r}, not 'code'"
                ),
                evidence=[
                    {"cron_label": cron.get("label"),
                     "script": script,
                     "layer": entry.get("layer")},
                ],
            ))
    return findings


def check_c_a6_orphan_code(manifest: dict) -> list[CoherenceFinding]:
    """C-A6: every code-layer file in files[] is referenced by something
    structural. Severity: minor (might be dead code, might be a library
    imported by name).

    Exemptions:
      - ``owned_by: "admin"`` — file lives in the evolve repo and is
        scheduled externally (LaunchDaemon / systemd / cron handled by
        the operator). The bot's OC manifest can't reference it because
        the bot doesn't run it.
      - ``owned_by: "external"`` — similar carve-out for libraries the
        bot imports without naming them in cron/action.
    """
    findings: list[CoherenceFinding] = []
    for entry in _code_files(manifest):
        if entry.get("_realized"):
            # v7-arc realized_files[] are the app's explicitly declared
            # blueprint files (each carries a logical_name, role, and file
            # marker). They are owned by declaration — the orphan check is
            # for scanner-ATTRIBUTED legacy files[] that nothing cites, not
            # for files the Instance manifest lists as its own. Treating
            # them as orphans would flood every v7-arc app with minor
            # findings (e.g. a 42-file app), the opposite of the goal.
            continue
        owned_by = (entry.get("owned_by") or "").strip().lower()
        if owned_by in {"admin", "external"}:
            # Production calibration 2026-06-06: security-cve-scan's
            # finalize.py is admin-owned + scheduled by a LaunchDaemon;
            # C-A6 doesn't see launchd, so admin-owned exempts.
            continue
        if _file_referenced_anywhere(entry, manifest):
            continue
        findings.append(CoherenceFinding(
            id="C-A6",
            severity=SEVERITY_MINOR,
            assertion="orphan_code_file",
            description=(
                f"code-layer file {entry.get('path', '?')!r} is not "
                f"referenced by any scheduled_action, cron, "
                f"test_command, interface_contract, or other code file"
            ),
            evidence=[
                {"path": entry.get("path")},
            ],
        ))
    return findings


def check_c_a7_integration_used(manifest: dict) -> list[CoherenceFinding]:
    """C-A7: every requirements.integrations[*] is actually referenced
    by something in the manifest. Severity: minor.

    Heuristic — looks for the integration id as a substring in the
    blobified scheduled_actions / crons / files structure. Doesn't read
    file contents (that's a deeper check that PR 5+ may add).
    """
    findings: list[CoherenceFinding] = []
    integrations = _integration_ids(manifest)
    if not integrations:
        return findings
    blob = (
        str(manifest.get("scheduled_actions") or "")
        + " " + str(manifest.get("crons") or "")
        + " " + str(manifest.get("test_command") or "")
        + " " + str(manifest.get("interface_contract") or "")
        + " " + " ".join(
            (f.get("path") or "") for f in manifest.get("files") or []
            if isinstance(f, dict)
        )
    )
    lower_blob = blob.lower()
    for iid in integrations:
        if iid.lower() in lower_blob:
            continue
        findings.append(CoherenceFinding(
            id="C-A7",
            severity=SEVERITY_MINOR,
            assertion="integration_not_referenced",
            description=(
                f"requirements.integrations declares {iid!r} but no "
                f"scheduled_action, cron, or code file references it"
            ),
            evidence=[
                {"integration_id": iid},
            ],
        ))
    return findings


def check_c_a8_interface_cli_resolves(manifest: dict) -> list[CoherenceFinding]:
    """C-A8: interface_contract.cli[*].command resolves to a files[]
    entry. Severity: major.

    Skips entries with no command (no claim to verify). The resolution
    is a substring match: if the command's first token (the script
    path) appears as a files[] path, we treat that as a resolution.
    """
    findings: list[CoherenceFinding] = []
    file_paths = {(f.get("path") or "").lstrip("/")
                  for f in manifest.get("files") or [] if isinstance(f, dict)}
    cli_entries = (manifest.get("interface_contract") or {}).get("cli") or []
    for cli in cli_entries:
        if isinstance(cli, dict):
            cmd = cli.get("command") or ""
        elif isinstance(cli, str):
            cmd = cli
        else:
            continue
        cmd = cmd.strip()
        if not cmd:
            continue
        # Extract the first token that looks like a file path. The
        # command may be "python3 scripts/foo.py --flag" — we want
        # "scripts/foo.py".
        tokens = cmd.split()
        candidate = ""
        for t in tokens:
            if "/" in t or t.endswith((".py", ".sh", ".js", ".ts", ".rb")):
                candidate = t.lstrip("/").lstrip("./")
                break
        if not candidate:
            continue
        if candidate in file_paths:
            continue
        # Soft match: the basename appears anywhere in files[].
        from os.path import basename
        if any(basename(candidate) in fp for fp in file_paths):
            continue
        findings.append(CoherenceFinding(
            id="C-A8",
            severity=SEVERITY_MAJOR,
            assertion="cli_command_not_resolved",
            description=(
                f"interface_contract.cli command {cmd!r} references "
                f"{candidate!r} which is not in files[]"
            ),
            evidence=[
                {"command": cmd, "candidate_path": candidate},
            ],
        ))
    return findings


# Public entry point: run all assertions.

DEFAULT_ASSERTIONS = (
    check_c_a1_recurring_without_trigger,
    check_c_a2_action_inputs_resolve,
    check_c_a3_action_outputs_have_producer,
    check_c_a4_messaging_output_needs_integration,
    check_c_a5_cron_script_in_files,
    check_c_a6_orphan_code,
    check_c_a7_integration_used,
    check_c_a8_interface_cli_resolves,
)


def run_pass_a(manifest: dict) -> list[CoherenceFinding]:
    """Run every Pass A assertion against the manifest dict.

    Findings whose signature is already in
    ``manifest.coherence.coherence_accepted[]`` are filtered out — the
    operator already accepted them.

    Each assertion is wrapped so an unexpected exception from one
    doesn't abort the rest.
    """
    findings: list[CoherenceFinding] = []
    for fn in DEFAULT_ASSERTIONS:
        try:
            findings.extend(fn(manifest))
        except Exception as exc:  # noqa: BLE001
            findings.append(CoherenceFinding(
                id="C-A-CRASH",
                severity=SEVERITY_INFO,
                assertion="assertion_crashed",
                description=(
                    f"coherence_pass_a {fn.__name__} raised "
                    f"{type(exc).__name__}: {exc}"
                ),
                evidence=[{"assertion": fn.__name__, "error": str(exc)}],
            ))

    accepted = set()
    coh = manifest.get("coherence") or {}
    for entry in coh.get("coherence_accepted") or []:
        if isinstance(entry, dict):
            sig = entry.get("signature")
            if sig:
                accepted.add(sig)
        elif isinstance(entry, str):
            accepted.add(entry)
    if accepted:
        findings = [f for f in findings if f.signature() not in accepted]
    return findings


def status_for_findings(findings: list[CoherenceFinding]) -> str:
    """Resolve coherence.status from a finding list.

    incoherent — any critical or major
    warnings   — only minor / info
    ok         — empty
    """
    if not findings:
        return "ok"
    if any(f.severity in (SEVERITY_CRITICAL, SEVERITY_MAJOR) for f in findings):
        return "incoherent"
    return "warnings"


def apply_pass_a(manifest: dict, *, now_iso: str | None = None) -> dict:
    """Run Pass A and write findings into ``manifest.coherence``.

    Mutates ``manifest`` in place. Returns a summary dict for the caller
    (scanner Phase 7) to log.

    The coherence_accepted[] list is preserved — accepted signatures
    survive the rewrite. Operators' accept clicks aren't undone by a
    re-scan.
    """
    from datetime import datetime, timezone
    findings = run_pass_a(manifest)
    coh = manifest.setdefault("coherence", {})
    accepted = coh.get("coherence_accepted") or []
    coh["findings"] = [f.to_dict() for f in findings]
    coh["coherence_accepted"] = accepted
    coh["status"] = status_for_findings(findings)
    coh["last_checked_at"] = now_iso or datetime.now(timezone.utc).isoformat()
    if "last_capability_check" not in coh:
        coh["last_capability_check"] = None
    return {
        "findings_count": len(findings),
        "status": coh["status"],
        "by_severity": {
            SEVERITY_CRITICAL: sum(1 for f in findings if f.severity == SEVERITY_CRITICAL),
            SEVERITY_MAJOR:    sum(1 for f in findings if f.severity == SEVERITY_MAJOR),
            SEVERITY_MINOR:    sum(1 for f in findings if f.severity == SEVERITY_MINOR),
            SEVERITY_INFO:     sum(1 for f in findings if f.severity == SEVERITY_INFO),
        },
    }
