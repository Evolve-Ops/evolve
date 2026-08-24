"""Purpose/fit classifier — goal-application vs capability (skill) vs system.

The deterministic app-evidence floor (#2885 / #2894 / #2898) cleared the
pure-platform / incoherent phantom classes from the scanner (and Slice 1/1b
extended it to discount platform-only surfaces). Two residual false-positive
classes remain — both carry real evidence and survive the floor, so only a
semantic LLM judgment can separate them:

1. **Capability-plumbing minted as an application** — a manifest with real
   owned code and a producer surface that is really a single reusable
   *capability* (a skill) with no goal of its own. Canonical example:
   "Google Services Integration" — realized files all ``oauth_setup.py`` /
   ``sheets_integration.py`` / ``credentials/*.json``, objective "OAuth-based
   integration with Google APIs". That is the capability "access Google
   APIs", not an application.

2. **Pod system-functions minted as an application** (Slice 2, this change) —
   goal-shaped work whose goal is operating the POD / agent-runtime ITSELF:
   self-healing, liveness/heartbeat monitoring, gateway/cron/process
   supervision, or aggregating the pod's OWN operational telemetry. Live pod
   examples the classifier mislabels ``application`` at 0.92–0.97: "Operations
   Automation", "System Heartbeat Monitor", "Nightly Reconciliation",
   "Cost & Usage Monitoring" / "Warning Consolidation". These are not
   low-confidence misses — they are a *missing category*. The operator wants
   them off the Apps grid; they are pod system functions, not user apps.

The closed enum is therefore {application, capability, system}. The
discriminator for system vs application is **whose goal is it** — the agent's
own runtime (system) vs the user's world (application). "Monitoring",
"automation" and "scheduling" appear on BOTH sides, so this is an LLM
judgment, not a rule: a user's "Greenhouse Temperature Monitor" (alerts a
farmer) is an application even though it "monitors".

Definition source of truth: ``docs/applications-vs-skills.md``. A *skill*
is a capability primitive (one thing the bot can do well); an
*application* is a goal-shaped contract built from multiple skills working
in concert toward a specific outcome in the user's world; a *system*
function pursues a goal whose beneficiary is the pod/runtime itself.

Design invariants:

* **Conservative default = keep-as-application.** Wrongly hiding a real
  app (over-route) is the failure to prevent — and now over-route has TWO
  directions (labeling a real user app ``capability`` OR ``system``). A skill
  or system-function left on the page is the tolerable miss. So a
  "capability" *or* "system" verdict requires a confident judgment;
  *anything else* — low confidence, a verdict below its threshold, a parse
  failure, an empty/absent LLM response — falls back to "application". A hard
  error (no usable response at all) returns ``None`` so the caller leaves the
  manifest unstamped and it is retried on the next scan rather than
  permanently defaulted.
* **Offline-testable seam.** The LLM call is injected as ``llm_fn`` — the
  scanner passes ``scanner._call_llm``; tests pass a deterministic
  stub. No live LLM in CI.
* **No provider/model literal in logic.** The caller resolves the model
  string through the model-tier seam (a stronger tier than the haiku-tier
  Phase-2 discovery, which already fails this exact judgment) and passes
  it in. This module never names a provider.
"""

from __future__ import annotations

import json
import math
from typing import Any, Callable, Optional

# ── App-kind vocabulary ───────────────────────────────────────────────────────
# These are the closed enum values for the ``app_kind`` manifest field. They are
# domain vocabulary (not provider/model literals), so they live here as the
# single source of truth and are imported by manifest.py for its schema default.
APP_KIND_APPLICATION = "application"
APP_KIND_CAPABILITY = "capability"
APP_KIND_SYSTEM = "system"  # pod/agent-runtime infrastructure (Slice 2)
VALID_APP_KINDS = frozenset(
    {APP_KIND_APPLICATION, APP_KIND_CAPABILITY, APP_KIND_SYSTEM}
)

# A "capability" verdict only sticks at or above this confidence. Below it we
# downgrade to "application" — the conservative direction. Tuned high because
# the over-route direction (hiding a real app) is the failure we guard against.
CAPABILITY_MIN_CONFIDENCE = 0.7

# A "system" verdict only sticks at or above this confidence — set slightly
# HIGHER than capability's, because the system over-route (hiding a real user
# app that happens to "monitor" / "automate" / "schedule") is the failure mode
# this slice most has to prevent. Below it we downgrade to "application".
SYSTEM_MIN_CONFIDENCE = 0.75

# Classifier vocabulary/prompt version, stamped into every classification block
# (``classifier_version``). Bump it whenever the verdict vocabulary or the
# discriminating prompt changes, so the reconcile pass (scanner Phase 6.5) can
# re-judge manifests carrying an older verdict. Version history:
#   v1 — #2899, binary {application, capability}; blocks carry NO version field
#        (treated as version 0 by the re-judge gate, so they are re-judged once).
#   v2 — Slice 2, adds the {system} verdict + whose-goal-is-it discriminator.
CLASSIFIER_VERSION = 2

# Stamped on the classification block so the verdict's origin is auditable.
CLASSIFIED_BY = "scanner.purpose_fit"

# Type of the injected LLM callable: (model, prompt, api_key) -> text.
LlmFn = Callable[[str, str, str], str]


def _normalize_paths(value: Any) -> list[str]:
    """Pull a flat list of path strings out of a files / realized_files /
    evidence list whose entries may be plain strings or ``{"path": ...}``
    dicts. Tolerant of any shape — returns ``[]`` on anything unexpected."""
    out: list[str] = []
    if not isinstance(value, list):
        return out
    for entry in value:
        if isinstance(entry, str):
            p = entry.strip()
            if p:
                out.append(p)
        elif isinstance(entry, dict):
            p = str(entry.get("path") or "").strip()
            if p:
                out.append(p)
    return out


def _behavior_labels(manifest: dict, bound_spec: Optional[dict]) -> list[str]:
    """Short labels for the app's recurring/scheduled behaviors, drawn from
    ``scheduled_actions`` / ``configured_schedules`` on the manifest or its
    bound Spec. These are strong application signal (a goal delivered on a
    cadence), so they go into the prompt."""
    labels: list[str] = []
    for source in (manifest, bound_spec or {}):
        if not isinstance(source, dict):
            continue
        for key in ("scheduled_actions", "configured_schedules", "schedules"):
            actions = source.get(key)
            if not isinstance(actions, list):
                continue
            for a in actions:
                if not isinstance(a, dict):
                    continue
                label = (
                    a.get("description")
                    or a.get("name")
                    or a.get("action_id")
                    or a.get("id")
                    or ""
                )
                label = str(label).strip()
                if label:
                    labels.append(label)
    # De-dup preserving order, cap length.
    seen: set[str] = set()
    uniq: list[str] = []
    for lab in labels:
        if lab not in seen:
            seen.add(lab)
            uniq.append(lab)
    return uniq[:8]


def classification_features(
    manifest: dict,
    bound_spec: Optional[dict] = None,
    extra_files: Optional[list[str]] = None,
) -> dict:
    """Extract the signals the classifier judges on from a manifest dict
    (legacy single-file, or a v7-arc Instance + its hydrated/bound Spec).

    ``extra_files`` lets the mint-time caller pass the DetectedApplication's
    evidence files, which are the real surface before Phase 5 stamps
    ``files`` / ``realized_files`` onto the freshly minted manifest.
    """
    spec = bound_spec or {}
    _identity = manifest.get("identity")
    identity = _identity if isinstance(_identity, dict) else {}

    name = (
        str(manifest.get("name") or "").strip()
        or str(spec.get("name") or "").strip()
        or str(manifest.get("display_name") or "").strip()
        # identity: see resolve_app_id — NOT swept (AL-1.4b). This is the
        # last rung of the DISPLAY-NAME ladder, not an identity read: when a
        # manifest carries no name/display_name the classifier falls back to
        # the readable stem (``app_task_manager``) because the resulting
        # string is fed to the keyword matchers and surfaced as the app's
        # label. ``resolve_app_id`` leads with ``pkg_id``, so it would supply
        # the opaque ``p-a3f91c8b`` — carrying no words to classify on, and
        # changing this classifier's output for every gallery-installed app.
        or str(manifest.get("id") or manifest.get("instance_id") or "").strip()
    )
    objective = (
        str(manifest.get("objective") or "").strip()
        or str(spec.get("objective") or "").strip()
    )
    description = (
        str(manifest.get("description") or "").strip()
        or str(spec.get("description") or "").strip()
        or str(identity.get("purpose") or "").strip()
    )

    files: list[str] = []
    files += _normalize_paths(manifest.get("files"))
    files += _normalize_paths(manifest.get("realized_files"))
    files += _normalize_paths(spec.get("files"))
    files += _normalize_paths(spec.get("realized_files"))
    files += list(extra_files or [])
    # De-dup preserving order, cap.
    seen: set[str] = set()
    uniq_files: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            uniq_files.append(f)

    return {
        "name": name,
        "objective": objective,
        "description": description,
        "files": uniq_files[:30],
        "behaviors": _behavior_labels(manifest, bound_spec),
    }


def _has_enough_signal(features: dict) -> bool:
    """A manifest with no name AND no objective/description AND no files is
    nothing to judge — keep it as an application (the default) and don't
    spend an LLM call."""
    return bool(
        features.get("objective")
        or features.get("description")
        or features.get("files")
    )


def build_prompt(features: dict) -> str:
    """Anchor the judgment with the applications-vs-skills definition and
    worked examples, then ask the single discriminating question."""
    files = features.get("files") or []
    files_str = "\n".join(f"  - {p}" for p in files) if files else "  (none recorded)"
    behaviors = features.get("behaviors") or []
    behaviors_str = (
        "\n".join(f"  - {b}" for b in behaviors) if behaviors else "  (none recorded)"
    )
    return f"""You are classifying ONE thing built into an OpenClaw AI assistant's
workspace as exactly one of: APPLICATION, CAPABILITY (skill), or SYSTEM
(pod/agent-runtime infrastructure).

DEFINITIONS (authoritative):
- A SKILL / CAPABILITY is a capability primitive — one reusable thing the bot
  can do well, with no goal of its own. Examples: "send a Slack message",
  "read Gmail", "access Google APIs (Sheets/Docs/Drive)", "control a Hue
  light", "save a file to Dropbox". A bare integration — OAuth setup, an API
  client, credential plumbing — is a CAPABILITY even when it has its own
  scripts. The scripts implement the one capability; they do not pursue a goal.
- An APPLICATION is a goal-shaped contract — it delivers a specific outcome IN
  THE USER'S WORLD by ORCHESTRATING one or more capabilities toward that goal.
  Examples: "Morning Briefing" (email + calendar + weather, delivered as one
  7 AM message), "Project Manager" (tasks + deadlines + files, to move a
  project forward), "Communication & Messaging Hub" (receive + queue + route +
  summarize the user's messages), a "Ranch Task Tracker" (track the user's
  tasks/deadlines and report status).
- SYSTEM / INFRASTRUCTURE is goal-shaped work whose goal is operating the POD /
  AGENT-RUNTIME ITSELF — keeping the bot alive, healthy, scheduled or
  reconciled. Examples: self-healing infra management, liveness/heartbeat
  monitoring, gateway / cron / process supervision, or aggregating the pod's
  OWN operational telemetry (its own spend, its own warnings/errors). Its
  beneficiary is the agent's runtime, NOT the user.

THE DECISIVE QUESTION — WHOSE GOAL IS IT?
- Delivers an outcome in the USER's world (their ranch, greenhouse, inbox,
  messages, documents, finances, calendar) → APPLICATION.
- Operates the AGENT's OWN runtime (its liveness, scheduling, gateways,
  self-healing, its own cost/error telemetry) → SYSTEM.
- No goal at all — just one reusable capability → CAPABILITY.
The APPLICATION↔SYSTEM boundary is the agent's runtime vs the user's world.
"Monitoring", "automation", "reconciliation" and "scheduling" appear on BOTH
sides — what matters is WHAT is being monitored/automated and FOR WHOM.

WORKED EXAMPLES:
- "Operations Automation" — self-healing infra management (cron-monitor,
  gateway self-heal, task-check that keep the bot running) → SYSTEM.
- "System Heartbeat Monitor" / "Heartbeat Monitoring" — watches the bot's own
  liveness/heartbeat → SYSTEM.
- "Nightly Reconciliation" — reconciles the pod's own state each night →
  SYSTEM.
- "Cost & Usage Monitoring" / "Warning Consolidation" — aggregates the pod's
  OWN spend / warnings telemetry → SYSTEM.
- "Google Services Integration" — objective "OAuth-based integration with
  Google APIs (Sheets/Docs/Drive)", files oauth_setup.py / oauth_get_url.py /
  sheets_integration.py / credentials/*.json → CAPABILITY (it IS the single
  capability "access Google APIs"; it has no goal of its own).
- "Greenhouse Temperature Monitor" — watches a USER's greenhouse and alerts the
  farmer → APPLICATION (it monitors, but the subject is the user's world, not
  the runtime).
- "Sales Dashboard" — surfaces the user's sales numbers → APPLICATION.
- "Communication & Messaging Hub" — receives, queues, routes and summarizes the
  USER's messages → APPLICATION (it processes the user's comms, not the
  runtime).
- "Ranch Task Tracker" — tracks the user's tasks/deadlines and reports status
  → APPLICATION.
- "Send SMS" — a wrapper that sends a text message via Twilio → CAPABILITY.

THE THING TO CLASSIFY:
Name: {features.get('name') or '(unnamed)'}
Objective: {features.get('objective') or '(none stated)'}
Description: {features.get('description') or '(none stated)'}
Component files:
{files_str}
Recurring/scheduled behaviors:
{behaviors_str}

BE CONSERVATIVE: the default is APPLICATION. Only answer SYSTEM when the work
clearly operates the agent's OWN runtime (liveness / scheduling / self-heal /
gateway/cron supervision / the pod's own telemetry), and only answer CAPABILITY
when it is clearly a single reusable capability with no goal of its own. If it
plausibly delivers an outcome in the USER's world — even if it "monitors",
"automates", "reconciles" or "schedules" — classify it APPLICATION.

Return ONLY a JSON object, no other text:
{{"kind": "application" | "capability" | "system", "confidence": 0.0-1.0, "rationale": "one sentence"}}"""


def parse_verdict(text: str) -> Optional[dict]:
    """Parse the model's JSON verdict object. Returns a normalized
    ``{"kind", "confidence", "rationale"}`` dict, or ``None`` when no usable
    verdict could be extracted (the caller then leaves the manifest unstamped
    and retries next scan)."""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end])
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    kind = str(obj.get("kind") or "").strip().lower()
    if kind not in VALID_APP_KINDS:
        return None
    try:
        confidence = float(obj.get("confidence", 0.0))
    except (ValueError, TypeError):
        confidence = 0.0
    # Treat non-finite confidence (NaN/Infinity — only reachable if the model
    # emits those non-standard JSON tokens) as 0.0, the conservative direction:
    # min(1.0, nan) returns nan in Python, which would otherwise sail past the
    # capability threshold. A non-judgeable confidence must never up-rank a
    # capability verdict.
    if not math.isfinite(confidence):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    rationale = str(obj.get("rationale") or "").strip()[:300]
    return {"kind": kind, "confidence": confidence, "rationale": rationale}


def build_classification_block(verdict: dict, model_tier: str) -> dict:
    """Turn a parsed verdict into the manifest ``classification`` block,
    applying the conservative decision rule:

    * a "capability" verdict only sticks at/above ``CAPABILITY_MIN_CONFIDENCE``;
    * a "system" verdict only sticks at/above ``SYSTEM_MIN_CONFIDENCE``;
    * everything else (low confidence, parse-recovered "application") resolves
      to "application".

    Both non-application verdicts have an over-route failure mode (hiding a real
    user app), so both require a confident judgment and both downgrade to
    "application" otherwise. The raw model verdict is preserved (``raw_kind``
    when it was downgraded) so the decision is auditable. ``classifier_version``
    stamps the verdict's vocabulary so a later prompt upgrade can re-judge it.
    """
    raw_kind = verdict["kind"]
    confidence = verdict["confidence"]
    final_kind = APP_KIND_APPLICATION
    if raw_kind == APP_KIND_CAPABILITY and confidence >= CAPABILITY_MIN_CONFIDENCE:
        final_kind = APP_KIND_CAPABILITY
    elif raw_kind == APP_KIND_SYSTEM and confidence >= SYSTEM_MIN_CONFIDENCE:
        final_kind = APP_KIND_SYSTEM
    block: dict[str, Any] = {
        "kind": final_kind,
        "confidence": confidence,
        "rationale": verdict["rationale"],
        "model_tier": model_tier,
        "classified_by": CLASSIFIED_BY,
        "classifier_version": CLASSIFIER_VERSION,
    }
    if raw_kind != final_kind:
        # We overrode the model's call in the conservative direction — record it.
        block["raw_kind"] = raw_kind
    return block


def needs_reclassification(manifest: dict) -> bool:
    """Whether the reconcile pass (scanner Phase 6.5) should (re)judge this
    manifest.

    True when there is no ``classification`` block yet, OR the block was stamped
    by an OLDER classifier vocabulary (``classifier_version < CLASSIFIER_VERSION``).
    A present, current block is skipped — so there is no double-spend within a
    single scan (the mint gate stamps the current version) and an up-to-date
    verdict is never re-spent. A block with no ``classifier_version`` (a v1 /
    #2899 block, written before versioning) is treated as version 0, so it is
    re-judged exactly once after a vocabulary upgrade — which is how this slice's
    {system} verdict reaches manifests already on disk.
    """
    block = manifest.get("classification")
    if not isinstance(block, dict) or not block:
        return True
    try:
        stamped = int(block.get("classifier_version", 0))
    except (TypeError, ValueError):
        stamped = 0
    return stamped < CLASSIFIER_VERSION


def classify_app_kind(
    manifest: dict,
    *,
    llm_fn: LlmFn,
    model: str,
    api_key: str,
    bound_spec: Optional[dict] = None,
    extra_files: Optional[list[str]] = None,
    model_tier: str = "",
) -> Optional[dict]:
    """Judge a single manifest goal-application vs capability vs system.

    Returns the ``classification`` block to stamp on the manifest, or
    ``None`` when no judgment was made (no usable signal, no api key, or a
    hard LLM/parse failure) — in which case the caller leaves ``app_kind`` at
    its inert "application" default and the manifest is retried next scan.

    ``model_tier`` is recorded verbatim in the block (audit only); ``model``
    is the resolved model string passed to ``llm_fn``.
    """
    features = classification_features(manifest, bound_spec, extra_files)
    if not _has_enough_signal(features):
        return None
    if not api_key:
        return None
    prompt = build_prompt(features)
    try:
        text = llm_fn(model, prompt, api_key)
    except Exception:  # noqa: BLE001 — any LLM error is a non-judgment, retry later
        return None
    verdict = parse_verdict(text)
    if verdict is None:
        return None
    return build_classification_block(verdict, model_tier or model)
