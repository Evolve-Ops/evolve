"""
better_engine/adapters/onboarding.py — OnboardingAdapter (§8.7)

Reads getting-started.json and the onboarding state bundle to emit
Recommendation objects for incomplete onboarding tasks.
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

from ..model import Recommendation, now_iso
from ..scoring import freshness_score, compute_base_score
from ..storage import load_getting_started
from ..onboarding import build_onboarding_state, get_active_tasks, ONBOARDING_TASKS

_log = logging.getLogger(__name__)

# Actions that are bot-executable (bot can call the scanner/review API inline)
_BOT_EXECUTABLE_ACTIONS = {"run_scanner", "open_applications", "run_health_check"}

# Accept labels by action
_ACCEPT_LABELS: dict[str, str] = {
    "run_scanner": "Run scanner",
    "open_applications": "Start review",
    "run_health_check": "Run health check",
    "open_setup": "Open settings",
    "open_cost_config": "Open settings",
    "open_reports_config": "Open settings",
    "open_security": "Open settings",
}

_DEFAULT_ACCEPT_LABEL = "Open settings"


def _make_id(dedup_key: str) -> str:
    h = hashlib.sha1(dedup_key.encode()).hexdigest()[:8]
    return f"rec_{int(time.time())}_{h}"


class OnboardingAdapter:
    """Adapter for onboarding task recommendations (§8.7)."""

    source_name = "onboarding"

    def generate(self, shared_dir: Path, network: dict) -> list[Recommendation]:
        members = network.get("members", [])
        getting_started_path = shared_dir / "better-engine" / "getting-started.json"

        getting_started = load_getting_started(getting_started_path)

        # If dismissed, emit nothing
        if getting_started.get("dismissed"):
            return []

        state = build_onboarding_state(shared_dir, network)
        active_tasks = get_active_tasks(state, getting_started, members)

        if not active_tasks:
            return []

        recs: list[Recommendation] = []
        freshness = freshness_score(now_iso(), 0)

        for task in active_tasks:
            try:
                rec = self._task_to_rec(task, freshness)
                if rec is not None:
                    recs.append(rec)
            except Exception as exc:
                _log.error(
                    "OnboardingAdapter: error processing task %s: %s",
                    task.id,
                    exc,
                    exc_info=True,
                )

        return recs

    def _task_to_rec(self, task, freshness: int) -> Recommendation | None:
        # Determine scope: per-bot tasks have bot_id embedded in id
        # Pod-level tasks: scope=admin, scope_id=admin
        # Per-bot tasks (instantiated — per_bot=False with bot_id in id): scope=bot
        is_per_bot = any(
            t.per_bot and task.id.startswith(t.id.replace("{bot_id}", ""))
            for t in ONBOARDING_TASKS
            if t.per_bot
        )

        if is_per_bot:
            # Extract bot_id from the instantiated task id
            # e.g. "scan_applications_team_bot_a" → bot_id = "team_bot_a"
            scope = "bot"
            # Find the bot_id suffix
            bot_id = _extract_bot_id_from_task(task)
            scope_id = bot_id or "admin"
        else:
            scope = "admin"
            scope_id = "admin"

        # Urgency: onboarding blocker = 15
        urgency = 15
        impact = 6  # informational/setup
        actionability_val = 14  # known single step

        base = compute_base_score(urgency, impact, actionability_val, freshness)

        dedup_key = f"onboarding::{scope_id}::{task.id}"

        bot_executable = task.action in _BOT_EXECUTABLE_ACTIONS
        accept_label = _ACCEPT_LABELS.get(task.action, _DEFAULT_ACCEPT_LABEL)

        tags = [
            "source:onboarding",
            "intent:setup",
        ] + list(task.tags)

        return Recommendation(
            id=_make_id(dedup_key),
            dedup_key=dedup_key,
            type="onboarding",
            source="onboarding",
            scope=scope,
            scope_id=scope_id,
            title=task.title,
            detail=task.detail,
            context=task.context,
            action_label=accept_label,
            action=task.action,
            action_args=dict(task.action_args),
            bot_executable=bot_executable,
            bridge_strategy=None,
            accept_label=accept_label,
            member_bot_title=None,
            member_bot_detail=None,
            priority_score=0,
            priority_components={
                "urgency": urgency,
                "impact": impact,
                "actionability": actionability_val,
                "freshness": freshness,
            },
            learning_weight=1.0,
            tags=tags,
            source_ref={"task_id": task.id, "scope_id": scope_id},
        )


def _extract_bot_id_from_task(task) -> str | None:
    """Extract bot_id from an instantiated per-bot task id.

    Examples:
      "scan_applications_team_bot_a" → "team_bot_a"
      "review_first_application_admin_bot" → "admin_bot"
    """
    for t in ONBOARDING_TASKS:
        if not t.per_bot:
            continue
        # Template id like "scan_applications_{bot_id}"
        prefix = t.id.split("{bot_id}")[0]  # e.g. "scan_applications_"
        if task.id.startswith(prefix):
            suffix = task.id[len(prefix):]
            if suffix:
                return suffix
    return None
