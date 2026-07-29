"""negative_path_tests.py — auto-generate constraint-as-test contracts.

Spec: docs/spec-forge-side-effects-2026-06-02.md §13.3. PR 6 of that
spec adds this MVP that closes the test-gate hardening loop alongside
the orphan check (§13.4) and constraint critic (§13.2).

The 2026-06-02 audit caught two of the three Cluster-B findings on
personal-bot ea-pack as silent failures because the bot LLM ran only
the happy-path test_command:

  - "Fail silently with log entry" when the gateway is unreachable →
    code raised instead, but the test_command never simulated an
    unreachable gateway.
  - "Configurable timing for all scheduled behaviors via bot config" →
    code hardcoded times, but the test_command never overrode the config.

The pure-LLM constraint critic in §13.2 catches the static analysis
case ("does the code mention reading bot config?"). This module catches
the behavioral case: it extracts constraint clauses that match a "X
when Y" shape and synthesizes a shell assertion that the test gate
runs alongside the spec-author's tests.

MVP scope (PR 6):
  - Regex-based pattern detection over constraints.boundaries[],
    constraints.safety[], identity.scope_includes[]
  - Two pattern families with high signal:
      1. "fail silently / no error / graceful when X unreachable / unavailable"
         → generate a test that mocks X as unreachable and asserts exit 0
      2. "configurable / overridable X via bot config"
         → generate a test that sets a bot-config override and asserts
           the override took effect
  - Generates shell skeletons (with explicit TODO markers for the bot
    LLM to flesh out — never blocking forge approval based on
    skeleton-shape alone)

Out of scope for MVP, deferred to follow-up:
  - LLM-driven test synthesis for arbitrary constraint shapes
  - Pytest function generation (current MVP is shell-only)
  - Test isolation infrastructure (mocks, fixtures, fakes)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# Pattern families recognised by the MVP extractor. Each captures the
# constraint's behavioural shape; the generator below renders a shell
# assertion matching the shape. Adding a new family is safe — the
# scanner just emits more skeletons.

_FAIL_SILENTLY_RE = re.compile(
    r"\b(?:fail|fall back|degrade)\s+(?:silently|gracefully|cleanly)\b"
    r".*?\bwhen\b\s+(?P<dep>[a-zA-Z][\w\-/ ]*?)"
    r"\s+(?:is\s+)?(?:un(?:reachable|available)|down|offline|missing|absent)",
    re.IGNORECASE,
)

# Looser variant: "X when Y is unreachable" without requiring "fail/fall back"
# prefix. Catches forms like "no exception when gateway is unreachable".
_NO_EXCEPTION_RE = re.compile(
    r"\b(?:no exception|no error|don'?t (?:raise|crash))"
    r".*?\bwhen\b\s+(?P<dep>[a-zA-Z][\w\-/ ]*?)"
    r"\s+(?:is\s+)?(?:un(?:reachable|available)|down|offline|missing|absent)",
    re.IGNORECASE,
)

_CONFIGURABLE_RE = re.compile(
    r"\b(?:configurable|overridable|adjustable)\s+(?P<feature>[a-zA-Z][\w \-]*?)"
    r"\s+(?:via|through|from|using)\s+(?:the\s+)?bot[ \-]?config",
    re.IGNORECASE,
)


@dataclass
class NegativePathTest:
    """One generated shell assertion derived from a constraint clause."""
    constraint_source: str   # "constraints.boundaries" | "identity.scope_includes" | ...
    constraint_index: int    # position in source list
    constraint_text: str     # the original clause text
    family: str              # "fail_silently" | "configurable" | ...
    shell_snippet: str       # generated bash assertion (with TODO markers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraint_source": self.constraint_source,
            "constraint_index": self.constraint_index,
            "constraint_text": self.constraint_text,
            "family": self.family,
            "shell_snippet": self.shell_snippet,
        }


def extract_negative_path_tests(manifest: dict) -> list[NegativePathTest]:
    """Walk the manifest's constraint surfaces and synthesize tests.

    Returns one ``NegativePathTest`` per matched pattern. Returns an
    empty list when no constraint matches any known pattern — that's
    the common case (most constraints are general guidance, not
    "X when Y" shapes).
    """
    out: list[NegativePathTest] = []

    sources: list[tuple[str, list]] = []
    constraints = manifest.get("constraints") or {}
    if isinstance(constraints, dict):
        for key in ("boundaries", "safety"):
            vals = constraints.get(key) or []
            if isinstance(vals, list):
                sources.append((f"constraints.{key}", vals))
    identity = manifest.get("identity") or {}
    if isinstance(identity, dict):
        scope = identity.get("scope_includes") or []
        if isinstance(scope, list):
            sources.append(("identity.scope_includes", scope))

    for source_name, items in sources:
        for idx, item in enumerate(items):
            if not isinstance(item, str) or not item.strip():
                continue
            text = item.strip()

            for family, regex, render in (
                ("fail_silently", _FAIL_SILENTLY_RE, _render_fail_silently),
                ("fail_silently", _NO_EXCEPTION_RE, _render_fail_silently),
                ("configurable", _CONFIGURABLE_RE, _render_configurable),
            ):
                m = regex.search(text)
                if not m:
                    continue
                snippet = render(text, m)
                out.append(NegativePathTest(
                    constraint_source=source_name,
                    constraint_index=idx,
                    constraint_text=text,
                    family=family,
                    shell_snippet=snippet,
                ))
                # One match per item — don't double-up if the text
                # happens to hit two patterns.
                break

    return out


def append_to_test_command(
    test_command: str, tests: list[NegativePathTest],
) -> str:
    """Append generated assertions to an existing test_command.

    The result is the original test_command followed by a marked block
    of constraint-derived assertions. Idempotent: if the marker block
    is already present (from a prior forge run), the new block replaces
    it rather than stacking.

    No-op when ``tests`` is empty — keeps the original test_command
    byte-identical so we don't churn manifest snapshots.
    """
    if not tests:
        return test_command

    marker_start = "# --- BEGIN forge negative-path tests (spec §13.3) ---"
    marker_end = "# --- END forge negative-path tests ---"

    block_lines = [marker_start]
    for t in tests:
        block_lines.append("")
        block_lines.append(
            f"# constraint: {t.constraint_source}[{t.constraint_index}] — {t.constraint_text}"
        )
        block_lines.append(t.shell_snippet.rstrip())
    block_lines.append(marker_end)
    block = "\n".join(block_lines)

    # Idempotent replace: if there's already a generated block in the
    # input, swap it out so we don't accumulate stale copies over
    # successive forge runs.
    if marker_start in test_command and marker_end in test_command:
        return re.sub(
            re.escape(marker_start) + r".*?" + re.escape(marker_end),
            block, test_command, count=1, flags=re.DOTALL,
        )

    base = (test_command or "").rstrip()
    sep = "\n\n" if base else ""
    return f"{base}{sep}{block}\n"


# ── Renderers (one per family) ──────────────────────────────────────────────


def _render_fail_silently(text: str, m: re.Match) -> str:
    """Render a shell snippet for the 'fail silently when X unreachable'
    pattern. The dependency name is extracted from the match; the bot
    LLM is left to fill in the actual unreachability simulation.

    The snippet exits 0 when the constraint passes (no exception leaked
    out, exit code is 0 from the app under test), and non-zero with a
    diagnostic when it fails.
    """
    dep = m.group("dep").strip()
    # Normalise dependency name for a shell-safe variable suffix.
    var_suffix = re.sub(r"[^a-zA-Z0-9_]", "_", dep)[:30] or "dep"
    return f"""# Fail-silently assertion: when {dep} is unreachable, the app must
# exit 0 (with a log entry, no exception leak). Bot LLM: replace the
# simulation block below with the actual mock/fake/stub mechanism for
# this app's {dep} integration.
#
# TODO(forge): simulate {dep} as unreachable here
SIMULATED_UNREACHABLE_{var_suffix}=1
export SIMULATED_UNREACHABLE_{var_suffix}

# Run the entrypoint under test
OUTPUT_{var_suffix}=$($TEST_ENTRYPOINT 2>&1)
RC_{var_suffix}=$?

if [ "$RC_{var_suffix}" -ne 0 ]; then
    echo "FAIL: app exited $RC_{var_suffix} when {dep} unreachable — expected silent fail (exit 0)"
    echo "    output: $OUTPUT_{var_suffix}"
    exit 1
fi
"""


def _render_configurable(text: str, m: re.Match) -> str:
    """Render a shell snippet for the 'configurable X via bot config'
    pattern. Sets a config override and asserts the override took effect.

    The bot LLM is responsible for telling us what to check in the output
    (typically a printed schedule, an emitted log line, etc.); the
    skeleton fails closed with a TODO marker if not filled in.
    """
    feature = m.group("feature").strip()
    var = re.sub(r"[^a-zA-Z0-9_]", "_", feature)[:30] or "feature"
    return f"""# Configurable-via-bot-config assertion: setting an override in the
# bot config should be reflected in the app's behaviour. Bot LLM:
# (1) fill in the actual config key for {feature}
# (2) fill in the actual assertion against the app's output
#
# TODO(forge): set the override key/value for the {feature} feature
export EVOLVE_BOT_CONFIG_OVERRIDE_{var}="forge-test-value"

OUTPUT_{var}=$($TEST_ENTRYPOINT 2>&1)

# TODO(forge): replace this grep with a real assertion that the
# override took effect (look for the override value in the output,
# in a log file, in a state file, etc.)
if ! echo "$OUTPUT_{var}" | grep -q "forge-test-value"; then
    echo "FAIL: {feature} configurable override did not take effect"
    echo "    output: $OUTPUT_{var}"
    exit 1
fi
"""
