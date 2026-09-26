"""proposal_synthesizer.budget — Soft/hard budget caps for synthesis runs.

Spec: internal/spec-proposal-synthesizer-2026-05-10.md §5.2.

Two layers of budget control per the spec:

  - **Soft targets** — where the synthesizer aims. The charter tells
    the model to push past these only when investigation is clearly
    converging.
  - **Hard caps** — walls the agent must not cross. Reaching a hard
    cap forces an emit using whatever the agent has.

Tracked dimensions:

  - **turns** — model invocations (each tool-use round trip counts as
    one turn)
  - **tokens** — input + output across all turns
  - **cost** — estimated USD; computed from token usage at the
    model's current rate
  - **wall-time** — seconds since the run started

Per-candidate and per-run aggregates. The agent loop checks
:func:`Budget.status` between turns and reacts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal


# Default model rates ($ per million input/output tokens). Sonnet
# 4-6 pricing as of 2026-05; tunable per-test.
DEFAULT_INPUT_RATE_USD_PER_MTOK = 3.0
DEFAULT_OUTPUT_RATE_USD_PER_MTOK = 15.0


# Per spec §5.2.
@dataclass(frozen=True)
class BudgetLimits:
    soft_cost_usd_per_candidate: float = 0.50
    soft_turns_per_candidate: int = 10
    soft_cost_usd_per_run: float = 5.00

    hard_cost_usd_per_candidate: float = 2.00
    hard_turns_per_candidate: int = 25
    hard_cost_usd_per_run: float = 10.00
    hard_wall_seconds_per_candidate: float = 600.0  # 10 minutes
    hard_wall_seconds_per_run: float = 1800.0  # 30 minutes


DEFAULT_LIMITS = BudgetLimits()


Status = Literal["continue", "soft_warning", "hard_cap"]


@dataclass
class TokenUsage:
    """Running totals for a single candidate or the whole run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0

    def add_turn(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        input_rate: float = DEFAULT_INPUT_RATE_USD_PER_MTOK,
        output_rate: float = DEFAULT_OUTPUT_RATE_USD_PER_MTOK,
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.turns += 1
        self.cost_usd += (input_tokens / 1_000_000.0) * input_rate
        self.cost_usd += (output_tokens / 1_000_000.0) * output_rate


@dataclass
class Budget:
    """Per-candidate + per-run budget tracker.

    Two scopes:

      - :attr:`run` — totals across the whole synthesis run; persists
        from the first candidate to the last.
      - :attr:`current` — totals for the candidate the agent is
        currently working on. Reset by :meth:`start_candidate`.

    The agent loop calls :meth:`record_turn` after each model call
    and :meth:`status` before each subsequent call. ``soft_warning``
    is the agent's cue to wrap up unless investigation is clearly
    converging; ``hard_cap`` means stop immediately and emit best-
    effort output.
    """

    limits: BudgetLimits = field(default_factory=lambda: DEFAULT_LIMITS)
    run: TokenUsage = field(default_factory=TokenUsage)
    current: TokenUsage = field(default_factory=TokenUsage)
    run_started_at: float = field(default_factory=time.monotonic)
    candidate_started_at: float = field(default_factory=time.monotonic)

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start_candidate(self) -> None:
        """Reset the per-candidate counter at the start of investigating one."""
        self.current = TokenUsage()
        self.candidate_started_at = time.monotonic()

    def record_turn(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        input_rate: float = DEFAULT_INPUT_RATE_USD_PER_MTOK,
        output_rate: float = DEFAULT_OUTPUT_RATE_USD_PER_MTOK,
    ) -> None:
        self.current.add_turn(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_rate=input_rate,
            output_rate=output_rate,
        )
        self.run.add_turn(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_rate=input_rate,
            output_rate=output_rate,
        )

    # ── checks ─────────────────────────────────────────────────────────────

    def candidate_wall_seconds(self) -> float:
        return time.monotonic() - self.candidate_started_at

    def run_wall_seconds(self) -> float:
        return time.monotonic() - self.run_started_at

    def status(self) -> Status:
        """Return the most severe applicable status across all dimensions."""
        # Hard caps first — agent must stop on any of these.
        if (
            self.current.cost_usd >= self.limits.hard_cost_usd_per_candidate
            or self.current.turns >= self.limits.hard_turns_per_candidate
            or self.run.cost_usd >= self.limits.hard_cost_usd_per_run
            or self.candidate_wall_seconds()
            >= self.limits.hard_wall_seconds_per_candidate
            or self.run_wall_seconds() >= self.limits.hard_wall_seconds_per_run
        ):
            return "hard_cap"
        if (
            self.current.cost_usd >= self.limits.soft_cost_usd_per_candidate
            or self.current.turns >= self.limits.soft_turns_per_candidate
            or self.run.cost_usd >= self.limits.soft_cost_usd_per_run
        ):
            return "soft_warning"
        return "continue"

    def status_reason(self) -> str:
        """One-line reason for the current status — useful for logs and for
        injecting into the model's context when warning it to wrap up."""
        reasons: list[str] = []
        L = self.limits
        if self.current.cost_usd >= L.hard_cost_usd_per_candidate:
            reasons.append(
                f"candidate-cost ${self.current.cost_usd:.2f}≥${L.hard_cost_usd_per_candidate:.2f}"
            )
        elif self.current.cost_usd >= L.soft_cost_usd_per_candidate:
            reasons.append(
                f"candidate-cost ${self.current.cost_usd:.2f}≥${L.soft_cost_usd_per_candidate:.2f}"
            )
        if self.current.turns >= L.hard_turns_per_candidate:
            reasons.append(f"candidate-turns {self.current.turns}≥{L.hard_turns_per_candidate}")
        elif self.current.turns >= L.soft_turns_per_candidate:
            reasons.append(f"candidate-turns {self.current.turns}≥{L.soft_turns_per_candidate}")
        if self.run.cost_usd >= L.hard_cost_usd_per_run:
            reasons.append(f"run-cost ${self.run.cost_usd:.2f}≥${L.hard_cost_usd_per_run:.2f}")
        elif self.run.cost_usd >= L.soft_cost_usd_per_run:
            reasons.append(f"run-cost ${self.run.cost_usd:.2f}≥${L.soft_cost_usd_per_run:.2f}")
        wall_cand = self.candidate_wall_seconds()
        if wall_cand >= L.hard_wall_seconds_per_candidate:
            reasons.append(
                f"candidate-wall {wall_cand:.0f}s≥{L.hard_wall_seconds_per_candidate:.0f}s"
            )
        wall_run = self.run_wall_seconds()
        if wall_run >= L.hard_wall_seconds_per_run:
            reasons.append(
                f"run-wall {wall_run:.0f}s≥{L.hard_wall_seconds_per_run:.0f}s"
            )
        return "; ".join(reasons) if reasons else "within budget"

    def snapshot(self) -> dict:
        """Structured stats for the synthesis log."""
        return {
            "run": {
                "turns": self.run.turns,
                "input_tokens": self.run.input_tokens,
                "output_tokens": self.run.output_tokens,
                "cost_usd": round(self.run.cost_usd, 6),
                "wall_seconds": round(self.run_wall_seconds(), 2),
            },
            "current_candidate": {
                "turns": self.current.turns,
                "input_tokens": self.current.input_tokens,
                "output_tokens": self.current.output_tokens,
                "cost_usd": round(self.current.cost_usd, 6),
                "wall_seconds": round(self.candidate_wall_seconds(), 2),
            },
        }
