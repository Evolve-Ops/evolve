"""digest_source_audit — Signal producer for persistently-broken digest sources.

Why this exists
---------------

The 2026-06-05 fix that wired up Atlas's Brave Search key surfaced a
second-order gap: atlas's `atlas_digest.py` was logging
`[fetchers] fetch_url X: HTTP Error 404` lines for each broken RSS
feed (Anthropic's RSS retired, Google's blog URL moved, hnrss
transient 502s) but those lines were just stderr noise. Nothing
turned a persistently-broken source into an actionable Signal.

This daemon closes that gap. atlas_digest.py was updated alongside
this module to write
``workspace/digest/source_health-{YYYY-MM-DD}.json`` after every run
with per-source outcomes:

    {
      "date":   "2026-06-05",
      "bot_id": "atlas",
      "sources": [
        {"name": "openai-blog", "kind": "rss", "target": "https://...",
         "ok": true,  "items": 8},
        {"name": "anthropic-blog", "kind": "rss", "target": "https://...",
         "ok": false, "items": 0},
        ...
      ]
    }

This daemon walks every bot's `digest/source_health-*.json` files for
the past N days, computes per-(bot, source) consecutive-failure runs,
and emits one Signal of type ``digest_source_broken`` per source that
has been dark for ``CONSECUTIVE_FAILURE_THRESHOLD`` runs. Auto-resolves
via ``signals.store.sweep_resolve`` when the source comes back.

Producer:  ``digest_source_audit``
Signal:    ``digest_source_broken`` (severity=warn, flavor=maintenance)
Signature: ``digest_source_audit:digest_source_broken:{bot_id}:{source_name}``

One Signal per (bot, source) so different broken sources on the same
bot land as separate alert entries — operator can triage individually,
and the source-name in the signature stays stable even when the URL
changes mid-investigation.

Pure Python, no LLM. Runs as the ``evolve`` user (workspace ACL
already grants read access). Daily cadence.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from schema.signal import make_signature
from signals import store as signals_store


PRODUCER = "digest_source_audit"
SIGNAL_TYPE = "digest_source_broken"

# A source must fail this many consecutive runs before we fire.
# Chosen so a single transient 502 (hnrss style) doesn't page anyone,
# but a real URL-moved retirement (Anthropic-style) surfaces within ~3
# days — well before the operator notices an empty digest.
CONSECUTIVE_FAILURE_THRESHOLD = 3

# How many days of source_health files to walk back. Bounds the per-run
# I/O while staying well above the failure threshold so partial pod
# downtime windows don't reset the consecutive-failure count.
LOOKBACK_DAYS = 14

# Filename pattern atlas_digest.py writes after each run.
_HEALTH_FILE_RE = re.compile(r"^source_health-(\d{4}-\d{2}-\d{2})\.json$")


# ─────────────────────────────────────────────────────────────────────────────
# Per-bot health-file iteration
# ─────────────────────────────────────────────────────────────────────────────


def _bot_workspace(bot_id: str, bot_info: dict) -> Path:
    """Return the bot's workspace root, defaulting to /Users/<user>/.openclaw/workspace.

    Matches the convention in deploy.py / config.get_bot_workspace —
    the bot's macOS user can differ from the bot_id, so we resolve via
    network.json::bots[bot_id].user (defaulting to bot_id when absent).
    """
    user = bot_info.get("user", bot_id) if isinstance(bot_info, dict) else bot_id
    # Platform-keyed home resolution (/Users on macOS, /home on Linux). (W10-G #5.)
    from evolve_config import user_home
    return user_home(user) / ".openclaw" / "workspace"


def _read_health_file(path: Path) -> dict | None:
    """Load and minimally validate a source_health JSON file."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("sources"), list):
        return None
    return d


def _iter_bot_health_files(
    bot_id: str, bot_info: dict, *, lookback_days: int = LOOKBACK_DAYS,
    now: datetime | None = None,
) -> list[tuple[str, dict]]:
    """Yield ``(YYYY-MM-DD, parsed_dict)`` for each source_health file
    within the lookback window. Sorted oldest → newest so consecutive-
    failure scanners can walk in order.

    Returns ``[]`` (silently) when the bot has no digest/ dir — many bots
    don't run a digest app, that's not an error.
    """
    ws = _bot_workspace(bot_id, bot_info)
    digest_dir = ws / "digest"
    if not digest_dir.is_dir():
        return []

    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=lookback_days)).date()

    out: list[tuple[str, dict]] = []
    for f in digest_dir.iterdir():
        m = _HEALTH_FILE_RE.match(f.name)
        if not m:
            continue
        date_str = m.group(1)
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            continue
        parsed = _read_health_file(f)
        if parsed is None:
            continue
        out.append((date_str, parsed))
    out.sort(key=lambda p: p[0])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Consecutive-failure tracking
# ─────────────────────────────────────────────────────────────────────────────


def _per_source_consecutive_failures(
    health_files: list[tuple[str, dict]],
) -> dict[str, dict]:
    """Walk health files chronologically, return per-source state.

    Result shape::

        {
          "<source_name>": {
            "kind":              str,        # rss | github_releases | brave
            "target":            str,        # latest URL/repo/query seen
            "consecutive_failures": int,     # how many runs in a row failed
            "last_failure_date": str | None, # most recent fail date
            "last_success_date": str | None, # most recent success date
            "total_runs_seen":   int,        # how many runs touched this source
            "skipped_reasons":   set[str],   # any operator-disabled reasons
          },
          ...
        }

    Sources that the operator deliberately skipped (``skipped_reason``
    present) are NOT counted as failures — that's a config choice, not
    a regression. They still appear in the result for visibility but
    with consecutive_failures=0.

    Sources that appear in some runs but not others (e.g. operator
    edited sources.json mid-window) are tracked through their lifetime
    — the consecutive count only resets on a successful run.
    """
    state: dict[str, dict] = {}

    for date_str, run in health_files:
        for src in run.get("sources", []):
            if not isinstance(src, dict):
                continue
            name = src.get("name", "")
            if not name:
                continue
            entry = state.setdefault(name, {
                "kind":                 src.get("kind", ""),
                "target":               src.get("target", ""),
                "consecutive_failures": 0,
                "last_failure_date":    None,
                "last_success_date":    None,
                "total_runs_seen":      0,
                "skipped_reasons":      set(),
            })
            # Track latest non-empty target so a URL change is reflected.
            if src.get("target"):
                entry["target"] = src["target"]
            entry["kind"] = src.get("kind", entry["kind"])
            entry["total_runs_seen"] += 1

            if src.get("skipped_reason"):
                # Operator-disabled — not a failure, but reset the
                # consecutive count so a later un-skip starts fresh.
                entry["skipped_reasons"].add(src["skipped_reason"])
                entry["consecutive_failures"] = 0
                continue

            if src.get("ok"):
                entry["consecutive_failures"] = 0
                entry["last_success_date"] = date_str
            else:
                entry["consecutive_failures"] += 1
                entry["last_failure_date"] = date_str

    return state


# ─────────────────────────────────────────────────────────────────────────────
# Signal spec builders
# ─────────────────────────────────────────────────────────────────────────────


def _spec_for_broken_source(
    bot_id: str, source_name: str, entry: dict,
) -> dict:
    """Render a Signal spec for one persistently-broken source on one bot.

    The signature embeds bot + source_name so different sources on the
    same bot land as separate Signals (operator triages each), and
    same source on different bots also stays distinct.
    """
    kind = entry.get("kind", "?")
    target = entry.get("target", "?")
    failures = entry.get("consecutive_failures", 0)
    last_fail = entry.get("last_failure_date") or "?"
    last_ok = entry.get("last_success_date") or "(never since lookback started)"

    signature = make_signature(
        PRODUCER, SIGNAL_TYPE,
        scope_key=f"{bot_id}:{source_name}",
    )

    title = (
        f"{source_name} ({kind}) silent on {bot_id} — "
        f"{failures} consecutive failed run(s)"
    )

    body_lines = [
        f"Digest source **{source_name}** ({kind}) has failed "
        f"**{failures} consecutive runs** on bot **{bot_id}**.",
        "",
        f"- Target: `{target}`",
        f"- Last successful run: {last_ok}",
        f"- Last failed run: {last_fail}",
        "",
        "_The source either moved (URL/repo rename), retired (RSS feed "
        "deprecated), or is hitting persistent upstream errors._",
        "",
        "Fix:",
        f"  Edit `/Users/{bot_id}/.openclaw/workspace/atlas/sources.json` "
        f"to update or remove the entry, then verify with:",
        f"  `sudo -u {bot_id} /bin/bash -c 'cd /tmp && /usr/bin/python3 "
        f"/Users/{bot_id}/.openclaw/workspace/scripts/atlas_digest.py "
        f"preview --bot-id {bot_id} --chat-id 0 --time-zone UTC "
        f"--detail concise'`",
        "",
        "Auto-resolves on the next successful run for this source.",
    ]

    return dict(
        signature=signature,
        producer=PRODUCER,
        type=SIGNAL_TYPE,
        flavor="maintenance",
        severity="warn",
        scope="pod",
        title=title,
        body="\n".join(body_lines),
        details={
            "bot_id":                bot_id,
            "source_name":           source_name,
            "kind":                  kind,
            "target":                target,
            "consecutive_failures":  failures,
            "last_failure_date":     entry.get("last_failure_date"),
            "last_success_date":     entry.get("last_success_date"),
            "total_runs_seen":       entry.get("total_runs_seen", 0),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def collect(
    shared_dir: Path,
    *,
    network: dict | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Walk every bot, return Signal specs for sources at-or-over threshold."""
    if network is None:
        try:
            network = json.loads(
                (shared_dir / "network.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            network = {}

    bots = network.get("bots") or {}
    specs: list[dict] = []
    for bot_id, bot_info in bots.items():
        files = _iter_bot_health_files(bot_id, bot_info, now=now)
        if not files:
            continue
        state = _per_source_consecutive_failures(files)
        for source_name, entry in sorted(state.items()):
            if entry["consecutive_failures"] >= CONSECUTIVE_FAILURE_THRESHOLD:
                specs.append(_spec_for_broken_source(bot_id, source_name, entry))
    return specs


def run(
    shared_dir: Path,
    *,
    dry_run: bool = False,
    network: dict | None = None,
    now: datetime | None = None,
) -> tuple[set[str], int, int]:
    """Collect specs, write Signals, sweep-resolve cleared sources.

    Returns ``(kept_signatures, n_fired, n_resolved)`` matching the
    convention of every other Signal-producing monitor under
    packages/analyzer/.
    """
    specs = collect(shared_dir, network=network, now=now)
    kept: set[str] = set()
    n_fired = 0
    for spec in specs:
        kept.add(spec["signature"])
        n_fired += 1
        if dry_run:
            print(json.dumps({"would_observe": spec}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **spec)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[digest_source_audit] observe failed for "
                f"{spec['signature']}: {exc}",
                flush=True,
            )

    n_resolved = 0
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                reason="auto-resolve: digest source recovered",
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[digest_source_audit] sweep_resolve failed: {exc}",
                flush=True,
            )
    return kept, n_fired, n_resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "digest_source_audit — daily Signal producer for "
            "persistently-broken digest sources."
        ),
    )
    parser.add_argument(
        "--shared-dir", type=Path,
        default=Path("/Users/Shared/evolve"),
        help="Pod-wide shared dir (default: /Users/Shared/evolve).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print Signal specs instead of writing them.",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="No-op flag for _install_launchd invocation compatibility.",
    )
    args = parser.parse_args(argv)

    kept, n_fired, n_resolved = run(args.shared_dir, dry_run=args.dry_run)
    print(
        f"[digest_source_audit] fired={n_fired} "
        f"resolved={n_resolved} kept={len(kept)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
