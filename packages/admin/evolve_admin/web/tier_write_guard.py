"""Truthfulness guards for the per-bot tiers write endpoint.

Both halves defend against the rung-collision false-success (model-tiers,
2026-06-27): on the rungs/roles shape legacy tier keys can collapse onto one
rung, and the writer folds them last-writer-wins, so a write can report
success while the operator's edit never lands. The historical trigger was
standard+judge sharing sonnet-class; the judge role is gone (judge-role
collapse, 2026-08-31), but the guard STAYS — any hand-authored roles map can
still point two roles at one rung (e.g. fast and standard both at
haiku-class), which is the same silent-clobber class. ``oc_model`` refuses a genuinely-conflicting fold (see
``detect_rung_collisions``); these helpers translate that refusal into a 409 and
catch any silent non-persist that slips through.

Kept out of routes_admin_config.py so the route file stays under its size cap.
"""

from __future__ import annotations

# Mirrors oc_model.RUNG_COLLISION_PREFIX — the writer raises with it and the
# error arrives here as a plain string over the CLI subprocess boundary, so the
# token is all there is to share.
RUNG_COLLISION_PREFIX = "RUNG_COLLISION: "


def collision_message(bot_id: str, write_err: "str | None") -> "str | None":
    """Operator-facing 409 message when ``write_err`` is a rung collision, else None.

    A collision is fixable input (two roles fighting over one rung), not a server
    fault — the caller maps a non-None return to 409 rather than a generic 500.
    """
    if write_err and write_err.startswith(RUNG_COLLISION_PREFIX):
        return f"Can't save {bot_id} tiers: {write_err[len(RUNG_COLLISION_PREFIX):]}"
    return None


def tiers_did_not_land(
    requested_tiers: "dict | None", result_tiers: "dict | None"
) -> "list[str]":
    """Models the operator asked for that are absent from the post-write tiers.

    A truthy write result is NOT proof the change persisted — re-read the
    synthesized tiers oc_model returns and confirm every requested model is
    present. Returns the sorted missing set ([] = everything landed).
    """
    missing: list[str] = []
    for tier_key, cfg in (requested_tiers or {}).items():
        if not isinstance(cfg, dict):
            continue
        want = [m for m in (cfg.get("models") or []) if isinstance(m, str) and m]
        # Same string-only filter as ``want``, for the same reason plus one
        # more: a hand-edited legacy-shape evolve-tiers.json can carry a DICT
        # in a tier's models[] (the writer's fold keeps strings only, but the
        # PRESERVE branch of json_full_config_set round-trips the legacy key
        # verbatim), and a bare ``set()`` over that raises "unhashable type:
        # 'dict'" — an opaque 500 out of the endpoint whose whole job is honest
        # reporting. A non-string entry is treated as NOT PRESENT rather than
        # crashing: it is not evidence the requested model landed, so a real
        # non-persist is still reported. The isinstance on the tier value
        # covers the same class one level up (``or {}`` only survives a FALSY
        # junk value; a non-empty list would raise AttributeError).
        got = (result_tiers or {}).get(tier_key)
        have = {
            m
            for m in ((got.get("models") if isinstance(got, dict) else None) or [])
            if isinstance(m, str)
        }
        missing += [m for m in want if m not in have]
    return sorted(set(missing))
