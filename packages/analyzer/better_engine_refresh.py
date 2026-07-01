#!/usr/bin/env python3
"""Better Engine scheduled refresh — runs every 15 minutes via launchd.

Also triggered immediately by launchd WatchPaths when the urgent flag file
appears at /Users/Shared/evolve/better-engine/.refresh-urgent.

Tier 3: all source adapters wired in, hints written, suggestions stub hooked.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

# ── Config loading ────────────────────────────────────────────────────────────

try:
    from evolve_config import load_config, get_shared_dir, get_members
except ImportError:
    # Fallback: read network.json directly
    def load_config(network_path=None):  # type: ignore[misc]
        p = Path(network_path or "/Users/Shared/evolve/network.json")
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}

    def get_shared_dir(config):  # type: ignore[misc]
        return Path(config.get("sharedDir", "/Users/Shared/evolve"))

    def get_members(config):  # type: ignore[misc]
        return config.get("members", [])


# ── Logging ───────────────────────────────────────────────────────────────────

_LOG_FILE = Path("/Users/Shared/evolve/logs/better_engine.log")


def _log(msg: str) -> None:
    print(msg, flush=True)
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(_LOG_FILE, "a") as f:
            f.write(f"{ts} {msg}\n")
    except OSError:
        pass


class _FileHandler(logging.Handler):
    """Route Python logging records from evolve_admin to the better_engine log."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(_LOG_FILE, "a") as f:
                f.write(f"{ts} [{record.levelname}] {msg}\n")
        except OSError:
            pass


def _setup_logging() -> None:
    """Attach a file handler so engine/adapter logging goes to better_engine.log."""
    handler = _FileHandler()
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    root = logging.getLogger("evolve_admin")
    if not any(isinstance(h, _FileHandler) for h in root.handlers):
        root.addHandler(handler)
    root.setLevel(logging.INFO)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Better Engine scheduled refresh")
    parser.add_argument(
        "--network",
        default="/Users/Shared/evolve/network.json",
        help="Path to network.json",
    )
    args = parser.parse_args()

    _setup_logging()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)

    _log("[better_engine_refresh] starting refresh")

    # ── Pre-step: refresh per-bot cost rollups ───────────────────────────────
    # Aggregate today + the trailing 13 days of cost_event records into the
    # metrics/<bot>/cost-<date>.json files Budget Hawk's spend_reader consumes.
    # Refreshing today's rollup every cycle (rather than only at midnight) is
    # what lets Budget Hawk fire on a same-day spike before the daily 01:00
    # boundary rolls over. Idempotent + cheap (kilobytes per bot per day).
    try:
        from cost_rollup import refresh_all as refresh_cost_rollups

        members = get_members(config)
        if members:
            # Pass ``log_fn`` so per-bot write failures land in the
            # better_engine.log alongside the rest of the pass —
            # ``refresh_all`` now isolates per-bot exceptions internally
            # so one broken dir (e.g. personal_bot's metrics/ owned by the bot
            # user instead of evolve, the 2026-05-07 regression) can't
            # silently kill every later bot's rollups.
            results = refresh_cost_rollups(
                shared_dir, members, days=14, log_fn=_log
            )
            written = sum(1 for _b, _d, r in results if r is not None)
            _log(
                f"[better_engine_refresh] cost rollups: {written} written across "
                f"{len(members)} bot(s) × 14 day(s)"
            )
    except Exception as exc:
        # Catch-all here is now defensive (import error, etc.). Per-bot
        # write failures are already caught inside ``refresh_all`` and
        # logged with bot context.
        _log(f"[better_engine_refresh] cost rollup failed (non-fatal): {exc}")

    # ── Pre-step: compliance scan ─────────────────────────────────────────────
    # Writes one Signal per issue via signals.store.observe(), then
    # sweep_resolves Signals that cleared between scans. The
    # manifest_quality / workspace_inventory / workspace_security
    # generators consume those Signals on the next step.
    try:
        from evolve_admin.applications.scanner import scan_compliance_all
        members = get_members(config)
        if members:
            scan_compliance_all(shared_dir, members)
            _log(f"[better_engine_refresh] compliance scan complete ({len(members)} bots)")
    except Exception as exc:
        _log(f"[better_engine_refresh] compliance scan failed (non-fatal): {exc}")

    # ── Pre-step: run generators whose cadence is due ────────────────────────
    # Runs AFTER the compliance scan so the new Signals-consuming
    # generators (manifest_quality, workspace_inventory,
    # workspace_security) see fresh signals from this cycle.
    try:
        from generator_runner import run_generators

        gen_count = run_generators(shared_dir, config, log_fn=_log)
        if gen_count:
            _log(f"[better_engine_refresh] generators produced {gen_count} proposals")
    except Exception as exc:
        _log(f"[better_engine_refresh] generator runner failed (non-fatal): {exc}")

    try:
        from evolve_admin.better_engine.engine import BetterEngine
        from evolve_admin.better_engine.adapters import (
            OnboardingAdapter,
            WhimsyAdapter,
            ProposalReaderAdapter,
        )
        from evolve_admin.better_engine.hints import write_all_hints

        engine = BetterEngine(shared_dir=shared_dir, network=config)

        adapters = [
            OnboardingAdapter(),
            WhimsyAdapter(),
            ProposalReaderAdapter(),
        ]
        pending = engine.refresh(adapters=adapters)

        _log(
            f"[better_engine_refresh] complete: {len(pending)} pending recommendations"
        )

        # Step 14: write rec-hints.json for each bot (§11 step 14)
        try:
            write_all_hints(pending, config, shared_dir)
            _log("[better_engine_refresh] hints written")
        except Exception as exc:
            _log(f"[better_engine_refresh] hints write failed: {exc}")

        # The Tier-4 LLM-suggestion stub that lived here is retired —
        # exploratory app suggestions now flow through the
        # generators/app_suggester generator, which the runner above
        # exercises like every other generator. No special-cased
        # post-step needed.
    except Exception as exc:
        _log(f"[better_engine_refresh] ERROR: {exc}")
        import traceback
        traceback.print_exc()

    # Delete urgent flag file if present (§11 step 15)
    urgent_flag = shared_dir / "better-engine" / ".refresh-urgent"
    try:
        if urgent_flag.exists():
            urgent_flag.unlink()
            _log("[better_engine_refresh] deleted urgent flag")
    except OSError as e:
        _log(f"[better_engine_refresh] could not delete urgent flag: {e}")


if __name__ == "__main__":
    main()
