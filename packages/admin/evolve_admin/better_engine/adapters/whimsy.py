"""
better_engine/adapters/whimsy.py — WhimsyAdapter (§8.7)

Reads {shared_dir}/better-engine/whimsy-pool.json and emits one randomly
chosen unused item as a Recommendation. Falls back to the whimsy/ seed
directory if the pool is absent.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from datetime import date
from pathlib import Path

from ..model import Recommendation, now_iso
from ..scoring import freshness_score, compute_base_score

_log = logging.getLogger(__name__)

# Seed directory lives alongside this package
_SEED_DIR = Path(__file__).parent.parent / "whimsy"

# Urgency and actionability for whimsy are always fixed (§8.7)
_URGENCY = 6
_ACTIONABILITY = 4


def _make_id(dedup_key: str) -> str:
    h = hashlib.sha1(dedup_key.encode()).hexdigest()[:8]
    return f"rec_{int(time.time())}_{h}"


def _load_seed_items() -> list[dict]:
    """Load and merge all *.json files from the whimsy/ seed directory."""
    all_items: list[dict] = []
    if not _SEED_DIR.exists():
        return all_items
    for path in sorted(_SEED_DIR.glob("*.json")):
        try:
            items = json.loads(path.read_text())
            if isinstance(items, list):
                all_items.extend(items)
        except Exception as exc:
            _log.warning("WhimsyAdapter: failed to read seed file %s: %s", path.name, exc)
    return all_items


def _load_pool(shared_dir: Path) -> list[dict]:
    """Load whimsy-pool.json; fall back to seed directory if missing."""
    pool_path = shared_dir / "better-engine" / "whimsy-pool.json"
    try:
        return json.loads(pool_path.read_text())
    except FileNotFoundError:
        pass
    except Exception as exc:
        _log.warning("WhimsyAdapter: failed to read pool, falling back to seed: %s", exc)

    items = _load_seed_items()
    if not items:
        _log.error("WhimsyAdapter: no seed items found in %s", _SEED_DIR)
    return items


class WhimsyAdapter:
    """Adapter for whimsy pool items (§8.7)."""

    source_name = "whimsy"

    def generate(self, shared_dir: Path, network: dict) -> list[Recommendation]:
        items = _load_pool(shared_dir)
        if not items:
            return []

        unused = [i for i in items if not i.get("used", False)]
        if not unused:
            return []

        # Generate up to 5 random candidates so the merge engine has options.
        # If the first-choice item was already accepted in a previous run, the
        # merge will keep it accepted and skip it — having multiple candidates
        # ensures at least one fresh item appears as pending.
        sample = random.sample(unused, min(5, len(unused)))
        freshness = freshness_score(now_iso(), 0)

        recs: list[Recommendation] = []
        for item in sample:
            try:
                rec = self._item_to_rec(item, freshness)
                if rec is not None:
                    recs.append(rec)
            except Exception as exc:
                _log.warning("WhimsyAdapter: error processing item %s: %s", item.get("id", "?"), exc)
        return recs

    def _item_to_rec(self, item: dict, freshness: int) -> Recommendation | None:
        item_id = item.get("id", "")
        if not item_id:
            return None

        item_type = item.get("type", "unknown")
        content = item.get("content", "")
        answer = item.get("answer")

        # Include ISO week so accepted items automatically rotate back as
        # eligible each week.  With 89 seed items and 5 candidates per refresh,
        # the queue is guaranteed to have fresh whimsy every week even if the
        # user has accepted every item before.
        week_key = date.today().strftime("%Y-W%W")
        dedup_key = f"whimsy::{item_id}::{week_key}"
        has_answer = answer is not None

        # Answer goes in context for expand-to-reveal in the UI
        context = answer if has_answer else ""

        # Bot delivery: include the answer below the content — member bot
        # recipients may not have dashboard access.
        if has_answer:
            member_bot_detail = f"{content}\n\nAnswer: {answer}"
        else:
            member_bot_detail = content

        title = _make_title(item_type, content)

        impact = 6
        compute_base_score(_URGENCY, impact, _ACTIONABILITY, freshness)

        tags = [
            "source:whimsy",
            f"whimsy_type:{item_type}",
            "scope:pod",
        ]

        return Recommendation(
            id=_make_id(dedup_key),
            dedup_key=dedup_key,
            type="whimsy",
            source="whimsy",
            scope="admin",
            scope_id="pod",
            title=title,
            detail=content,
            context=context,
            action_label="Got it",
            action=None,
            action_args={},
            bot_executable=True,
            bridge_strategy=None,
            accept_label="Got it",
            member_bot_title=title,
            member_bot_detail=member_bot_detail,
            priority_score=0,
            priority_components={
                "urgency": _URGENCY,
                "impact": impact,
                "actionability": _ACTIONABILITY,
                "freshness": freshness,
            },
            learning_weight=1.0,
            tags=tags,
            source_ref={
                "item_id": item_id,
                "item_type": item_type,
                "has_answer": has_answer,
            },
        )


_TYPE_TITLES: dict[str, str] = {
    "word_of_the_day": "Word of the day",
    "historical_trivia": "Historical trivia",
    "dad_joke": "Dad joke",
    "riddle": "Riddle",
    "fun_fact": "Fun fact",
    "quote": "Quote",
    "brain_teaser": "Brain teaser",
}


def _make_title(item_type: str, content: str) -> str:
    """Build a short title for a whimsy item."""
    label = _TYPE_TITLES.get(item_type, item_type.replace("_", " ").title())
    # Use truncated content as subtitle if short enough
    if len(content) <= 60:
        return f"{label}: {content}"
    # Otherwise just the type label
    return label
