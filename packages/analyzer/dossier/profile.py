"""dossier.profile — the operator's own preferences about the dossier page.

One file, ``{shared_dir}/dossier/profile.json``, holding how ONE operator
wants the Pod Intelligence page arranged: the order modules appear in, which
ones are collapsed away, and which ones they marked useful.

WHY IT LIVES HERE AND NOT BESIDE THE PAGE. It is dossier state, not admin-UI
state: design §4a rule 1 says this preference stream feeds forward as a
ranking input to proposal generation later, which is analyzer-side work
reading an analyzer-side store. Putting it under ``{shared_dir}/dossier/``
next to the editions and module sets means that reader finds it where the
rest of the dossier lives, and means the page is not the owner of a fact
that outlives the page.

WHY IT IS A STATE FILE AND NOT A RECORD STORE. It is overwritten in place —
one file, no per-event accumulation, no dated subdirectory. That is the
footprint contract's "bounded state store" shape (breaker state, autonomy
limits, baselines), deliberately outside the auto-generated *record* output
the output-declaration contract governs. It cannot grow: every list it holds
is capped here, at write time.

WHAT IT IS NOT. It holds **no bot data and no person data** — only module
ids, an order, and a thumb. A module id is the house's own word for one of
its own cards, so a leaked profile.json says nothing about the pod. That is
why the file is written 0644 like the editions beside it rather than 0600:
nothing in it is a secret, and an operator's own tooling should be able to
read their own preferences.

THE ONE RULE THAT IS NOT ABOUT STORAGE. ``hidden`` is honoured by the
RENDERER, not by this store, and a module the house marks ``critical`` is
rendered no matter what ``hidden`` says (design §4a rule 2 — hide means
"collapse and de-emphasize", never "silence"). This module therefore stores
an operator's preference faithfully, including a preference the page will
decline to obey; refusing the write instead would put the rule in two places
and let the two disagree.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from evolve_util import atomic_write_json

from dossier.store import EDITION_MODE, dossier_root
from dossier.window import iso_z

#: Bumped only for a breaking change to the on-disk shape.
SCHEMA_VERSION = 1

#: A module id as the synthesis layer mints them (``apps_leaderboard``).
#: Validated rather than checked against today's module list on purpose: a
#: profile written by a NEWER page, naming a module this server has never
#: heard of, must survive a downgrade — and the renderer already ignores an
#: id it has no card for. What must not survive is a non-id.
MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: The two thumbs. Stored as words rather than a boolean so a third verdict
#: ("ask me later") can be added without a migration.
RATINGS = ("useful", "not_useful")

#: The bound. Four modules ship today and the class list in the design tops
#: out around nine; 64 is far above any real page and low enough that a
#: malformed client cannot turn a preference file into a store.
MAX_ENTRIES = 64


def profile_path(shared_dir: Path | str) -> Path:
    return dossier_root(shared_dir) / "profile.json"


def empty_profile() -> dict[str, Any]:
    """The profile of an operator who has never touched the page.

    Empty lists, not a guessed order: an absent preference is absent, and a
    page that invented a default order here could never tell "the operator
    put cost first" apart from "we did".
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "order": [],
        "hidden": [],
        "ratings": {},
        "updated_at": None,
    }


def load_profile(shared_dir: Path | str) -> dict[str, Any]:
    """The operator's profile, or the empty one.

    Never raises and never returns ``None``: an unreadable or corrupt
    preference file must degrade to "no preferences yet" — the page still
    renders every module, in the order synthesis wrote them, which is the
    state that shows the most rather than the least.
    """
    path = profile_path(shared_dir)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return empty_profile()
    if not isinstance(raw, dict):
        return empty_profile()
    if raw.get("schema_version") != SCHEMA_VERSION:
        # A shape we do not know is not a shape we may guess at. Reading it
        # as the empty profile loses a preference; reading it wrong loses
        # the operator's trust in the page.
        return empty_profile()
    cleaned = normalise(raw)
    cleaned["updated_at"] = _iso_or_none(raw.get("updated_at"))
    return cleaned


def normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce an untrusted body into the stored shape. Never raises.

    Everything unrecognised is dropped rather than rejected: this is a
    preference file, and a page that refuses to remember three good choices
    because a fourth arrived malformed is worse than one that remembers the
    three.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "order": _id_list(raw.get("order")),
        "hidden": _id_list(raw.get("hidden")),
        "ratings": _ratings(raw.get("ratings")),
        "updated_at": None,
    }


def save_profile(
    shared_dir: Path | str,
    raw: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Normalise, stamp, and write the profile atomically. Returns what landed.

    The whole profile is replaced rather than merged. A merge would make
    "clear my ratings" unexpressible — the page always holds the complete
    preference state it is showing, so sending all of it is both simpler and
    the only way an empty list can mean empty.
    """
    profile = normalise(raw)
    profile["updated_at"] = iso_z(now)
    out = profile_path(shared_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out, profile, indent=2, sort_keys=True, mode=EDITION_MODE)
    return profile


def _id_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not MODULE_ID_RE.match(item):
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= MAX_ENTRIES:
            break
    return out


def _ratings(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    # Filter to string keys BEFORE sorting: a hand-edited file can hold a
    # non-string key, and ``sorted`` over mixed types raises — which would
    # turn "your preference file has a typo in it" into a 500.
    for key in sorted(k for k in value if isinstance(k, str)):
        verdict = value[key]
        if not MODULE_ID_RE.match(key):
            continue
        if verdict not in RATINGS:
            continue
        out[key] = verdict
        if len(out) >= MAX_ENTRIES:
            break
    return out


def _iso_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
