"""
Interactive capability manifest reviewer.

Takes a DetectedApplication (from scanner.py), generates a draft manifest
using an LLM (optional — works in offline mode too), then walks the user
through a structured review conversation:

  1. Confirm this is a real capability worth tracking
  2. Rate how well it's working (1-5)
  3. Describe what's broken or unreliable
  4. Describe what's missing that you'd want
  5. Confirm or edit goals
  6. Confirm or edit tests
  7. Confirm privacy constraints
  8. Approve → saved manifest

The review questions are the real product: they generate the test cases
and success metrics that feed into Evolve's RSI measurement.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


def _resolve_tier3() -> str:
    """Get the current tier3 model from config, or fall back to default."""
    try:
        from evolve_config import load_config
        from models import resolve_tier
        config = load_config()
        return resolve_tier("tier3", config)
    except Exception:
        return "anthropic/claude-haiku-4-5"


from .manifest import (
    ApplicationManifest,
    ApplicationTest,
    SuccessMetric,
    save_manifest,
    now_iso,
)
from .scanner import DetectedApplication


# ── Terminal helpers (mirrors wizard.py) ──────────────────────────────────────

def _c(text: str, code: str) -> str:
    codes = {"bold": "1", "dim": "2", "green": "32", "yellow": "33",
             "red": "31", "blue": "34", "cyan": "36", "reset": "0"}
    return f"\033[{codes.get(code, '0')}m{text}\033[0m"

def _header(title: str) -> None:
    print()
    print(_c("─" * 62, "dim"))
    print(_c(f"  {title}", "bold"))
    print(_c("─" * 62, "dim"))

def _ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        val = input(f"  {prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else default

def _ask_multiline(prompt: str) -> list[str]:
    """Collect multiple items, one per line. Empty line to finish."""
    print(f"  {prompt} (one per line, empty line to finish):")
    items = []
    while True:
        try:
            line = input("    > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            break
        items.append(line)
    return items

def _confirm(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        val = input(f"  {prompt} ({hint}): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val.startswith("y") if val else default

def _numbered_list(items: list[str], title: str = "") -> None:
    if title:
        print(f"  {_c(title, 'dim')}")
    for i, item in enumerate(items, 1):
        print(f"    {_c(str(i), 'dim')}. {item}")

def _edit_list(items: list[str], label: str) -> list[str]:
    """Let user add/remove/keep items from a list."""
    print()
    _numbered_list(items, f"Current {label}:")
    print()
    print(f"  [k] Keep as-is   [e] Edit   [a] Add more   [c] Clear and re-enter")
    choice = input("  Choice: ").strip().lower()

    if choice == "k" or not choice:
        return items
    elif choice == "a":
        new = _ask_multiline(f"Add {label}")
        return items + new
    elif choice == "c":
        return _ask_multiline(f"Enter {label}")
    elif choice == "e":
        idx_str = input(f"  Remove item numbers (space-separated, or Enter to skip): ").strip()
        if idx_str:
            remove = {int(x) - 1 for x in idx_str.split() if x.isdigit()}
            items = [item for i, item in enumerate(items) if i not in remove]
        new = _ask_multiline(f"Add more {label} (or Enter to finish)")
        return items + new
    return items


# ── LLM draft generation (optional) ──────────────────────────────────────────

def generate_draft_with_llm(
    detected: DetectedApplication,
    bot_id: str,
    openclaw_cmd: str = "openclaw",
) -> ApplicationManifest | None:
    """
    Use a cheap LLM call to enrich the draft manifest beyond what the
    scanner could determine from file patterns alone.

    Returns None if LLM is unavailable — caller falls back to scanner output.
    """
    prompt = f"""You are analyzing an AI assistant bot's workspace to generate a capability manifest.

Capability detected: {detected.name}
Evidence found: {detected.evidence_summary}

File excerpts:
{json.dumps(detected.raw_content, indent=2)[:2000]}

Generate a structured capability manifest with:
1. A clear 1-2 sentence description of what this capability does
2. 3-5 concrete goals (what the bot should accomplish)
3. 3-5 success metrics (measurable indicators it's working)
4. 3-5 test cases (behavioral: "when X happens, expect Y")
5. 1-3 privacy constraints (what data must never leave this capability)

Be specific and grounded in the evidence. Focus on what would be testable.

Respond with JSON only:
{{
  "description": "...",
  "goals": ["..."],
  "success_metrics": [{{"name": "...", "description": "...", "measurement": "...", "target": "..."}}],
  "tests": [{{"name": "...", "description": "...", "trigger": "...", "expect": "..."}}],
  "privacy_constraints": ["..."]
}}"""

    try:
        # Use openclaw subagent for a cheap single-turn LLM call
        result = subprocess.run(
            [openclaw_cmd, "run", "--model", _resolve_tier3(),
             "--max-turns", "1", "--message", prompt],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None

        # Extract JSON from response
        text = result.stdout.strip()
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        if json_start == -1:
            return None

        data = json.loads(text[json_start:json_end])

        manifest = ApplicationManifest(
            id=detected.id,
            name=detected.name,
            bot_id=bot_id,
            source=detected.source,
            description=data.get("description", ""),
            goals=data.get("goals", detected.suggested_goals),
            success_metrics=[
                SuccessMetric(
                    name=m.get("name", ""),
                    description=m.get("description", ""),
                    measurement=m.get("measurement", ""),
                    target=m.get("target", ""),
                )
                for m in data.get("success_metrics", [])
            ],
            tests=[
                ApplicationTest(
                    name=t.get("name", ""),
                    description=t.get("description", ""),
                    trigger=t.get("trigger", ""),
                    expect=t.get("expect", ""),
                )
                for t in data.get("tests", [])
            ],
            privacy_constraints=data.get("privacy_constraints", detected.suggested_privacy),
            evidence={"files": detected.evidence_files, "summary": detected.evidence_summary},
            created_at=now_iso(),
        )
        return manifest

    except Exception:
        return None


def build_draft_from_scanner(detected: DetectedApplication, bot_id: str) -> ApplicationManifest:
    """Build a draft manifest from scanner output alone (no LLM)."""
    return ApplicationManifest(
        id=detected.id,
        name=detected.name,
        bot_id=bot_id,
        source=detected.source,
        description=detected.evidence_summary,
        goals=detected.suggested_goals,
        success_metrics=[
            SuccessMetric(
                name=f"metric_{i+1}",
                description=t,
                measurement="Manual review",
                target="Pass",
            )
            for i, t in enumerate(detected.suggested_tests)
        ],
        tests=[
            ApplicationTest(
                name=f"test_{i+1}",
                description=t,
                trigger="manual",
                expect=t,
            )
            for i, t in enumerate(detected.suggested_tests)
        ],
        privacy_constraints=detected.suggested_privacy,
        evidence={"files": detected.evidence_files, "summary": detected.evidence_summary},
        created_at=now_iso(),
    )


# ── Review conversation ───────────────────────────────────────────────────────

def review_manifest(
    detected: DetectedApplication,
    bot_id: str,
    shared_dir: Path,
    use_llm: bool = True,
) -> ApplicationManifest | None:
    """
    Full interactive review flow for one detected capability.
    Returns approved manifest, or None if user skipped/rejected.
    """
    _header(f"Reviewing: {detected.name}")
    print()
    print(f"  {_c('What was detected:', 'dim')}")
    print(f"  {detected.evidence_summary}")
    print()
    print(f"  {_c('Evidence files:', 'dim')}")
    for f in detected.evidence_files[:6]:
        print(f"    • {f}")
    print()

    # ── Gate: is this a real capability? ──────────────────────────────────────
    if not _confirm("Is this a real capability worth tracking?", default=True):
        print(f"  Skipped: {detected.name}")
        return None

    # ── Generate draft ────────────────────────────────────────────────────────
    print()
    print(_c("  Generating draft manifest...", "dim"))

    manifest = None
    if use_llm:
        manifest = generate_draft_with_llm(detected, bot_id)
        if manifest:
            print(_c("  ✓ LLM draft generated", "green"))
        else:
            print(_c("  LLM unavailable — using scanner output", "yellow"))

    if manifest is None:
        manifest = build_draft_from_scanner(detected, bot_id)

    # ── Structured review questions ───────────────────────────────────────────

    print()
    _header("How Is This Working?")

    # Satisfaction score
    print()
    print("  Rate how well this capability is currently working:")
    print("    [1] Broken — doesn't work")
    print("    [2] Mostly broken — works occasionally")
    print("    [3] Partial — works but has significant gaps")
    print("    [4] Good — works well with minor issues")
    print("    [5] Excellent — works as intended")
    score_str = _ask("Rating", "3")
    manifest.satisfaction_score = int(score_str) if score_str.isdigit() and 1 <= int(score_str) <= 5 else 3

    # What's broken
    print()
    print("  What's currently broken, unreliable, or frustrating?")
    print(_c("  (These become known issues and negative test cases)", "dim"))
    broken = _ask_multiline("Known issues")
    manifest.known_issues = broken

    # What's missing
    print()
    print("  What would you want this capability to do that it doesn't yet?")
    print(_c("  (These become desired improvements and future goals)", "dim"))
    desired = _ask_multiline("Desired improvements")
    manifest.desired_improvements = desired

    # Free-form notes
    notes = _ask("Any other notes about this capability", "")
    manifest.satisfaction_notes = notes

    # ── Review goals ──────────────────────────────────────────────────────────
    print()
    _header("Goals")
    print()
    print("  Goals define what this capability is supposed to accomplish.")
    print()
    manifest.goals = _edit_list(manifest.goals, "goals")

    # ── Review tests ──────────────────────────────────────────────────────────
    print()
    _header("Test Cases")
    print()
    print("  Test cases define what 'working' looks like in a checkable way.")
    print()
    test_descriptions = [f"{t.trigger} → {t.expect}" for t in manifest.tests]
    edited_tests = _edit_list(test_descriptions, "test cases")

    # Convert back to ApplicationTest objects for any new ones
    manifest.tests = []
    for i, t in enumerate(edited_tests):
        if " → " in t:
            trigger, expect = t.split(" → ", 1)
        else:
            trigger, expect = "manual", t
        manifest.tests.append(ApplicationTest(
            name=f"test_{i+1}",
            description=t,
            trigger=trigger.strip(),
            expect=expect.strip(),
        ))

    # ── Review privacy constraints ────────────────────────────────────────────
    print()
    _header("Privacy Constraints")
    print()
    print("  Privacy constraints define data that must never leave this capability.")
    print()
    manifest.privacy_constraints = _edit_list(manifest.privacy_constraints, "privacy constraints")

    # ── Final review ──────────────────────────────────────────────────────────
    print()
    _header("Summary")
    print()
    print(f"  Name:        {manifest.name}")
    print(f"  Bot:         {manifest.bot_id}")
    print(f"  Satisfaction: {'★' * manifest.satisfaction_score}{'☆' * (5 - manifest.satisfaction_score)} ({manifest.satisfaction_score}/5)")
    print(f"  Goals:       {len(manifest.goals)}")
    print(f"  Tests:       {len(manifest.tests)}")
    print(f"  Privacy:     {len(manifest.privacy_constraints)}")
    if manifest.known_issues:
        print(f"  Issues:      {len(manifest.known_issues)}")
    if manifest.desired_improvements:
        print(f"  Desired:     {len(manifest.desired_improvements)}")
    print()

    if not _confirm("Approve and save this manifest?", default=True):
        if _confirm("Save as draft instead?", default=False):
            manifest.status = "draft"
            path = save_manifest(manifest, shared_dir)
            print(f"  Saved draft: {path}")
        return None

    manifest.status = "approved"
    manifest.approved_at = now_iso()
    path = save_manifest(manifest, shared_dir)
    print(_c(f"  ✓ Manifest saved: {path}", "green"))
    return manifest
