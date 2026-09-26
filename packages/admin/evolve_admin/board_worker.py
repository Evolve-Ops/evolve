"""board_worker.py — D-BI4/D-BI5 execution half: "assigned to the bot" means
an EVENT wakes a bounded, isolated worker.

Design: ``internal/dispatch/queued/board-bot-lane-worker.md`` (D-BI4/D-BI5,
the interface addendum's §2/§5/§6, D-CC, D-BI6). This module is the whole
backend: the daemon-side subscriber that wakes on board events
(:func:`poll_once`/:func:`run_loop`), the accept/decline gate
(:func:`classify_decline`), the bounded per-card run
(:func:`run_delegation`/:func:`resume_delegation`), the delivery-window miss
sweep (:func:`sweep_delivery_windows`), and the weekly-receipt line reader
(:func:`receipt_lines`).

WHAT "ISOLATED SESSION" MEANS HERE. CE-3 (``design-pa-context-economy``) asks
for a session whose whole context is the card, its enrichment, the one named
action, and the card's own event history — never the user's chat, memory
files, or the whole board. That is what :func:`build_worker_context` builds
and what :func:`run_delegation` hands to the model client; there is no
``board.list`` anywhere in this module's call graph. It is NOT a spawned OS
process or an OpenClaw agent session — the same choice Morning Board's
stocking run already made (D-BI6a: "never an agentic loop"; see the
deviations note below). A bounded, context-scoped function call gives every
guardrail this chip needs (one named action, one budget, no chat) without
inventing sandboxing machinery a fixture-tested chip cannot prove out anyway.
When CE-3's stronger process isolation lands, only the call at the bottom of
:func:`_dispatch_one` changes — the busy/queue exclusivity below already
holds for a spawned subprocess, not just an in-process call.

DEVIATIONS FROM THE BRIEF (each one is because the thing the brief pointed at
turned out not to exist, or to mean something else, on inspection — recorded
here rather than silently worked around):

1. **The brief's "``action.*`` gate in the plugin — ``canEscalateToRole``,
   ``requires_rung``" is a false lead.** ``canEscalateToRole``
   (``ModelRouter.ts``) is the MODEL-TIER cascade (fast/standard/power/max),
   unrelated to autonomy. The real, and only, live rung mechanism for board
   actions is ``board_actions.py``'s own ``requires_rung`` — a documented
   BINARY stand-in (0/1/2) for the autonomy ladder design track, which "no
   code anywhere defines a live per-integration rung" for yet. This module
   gates on that stand-in (via :func:`classify_decline`) plus its own small
   operator-configurable per-integration rung name
   (``pod.board_worker.rungs``) for the act/approval gate in §5 — see (5).

2. **Morning Board's launcher is not reusable — its PR (#4265,
   ``claude/meta-morning-board-gallery-app``) is not merged to ``main``.**
   This chip does not depend on it structurally. It reuses the same
   PRIMITIVES Morning Board's script and ``board_actions.py`` both already
   use on ``main`` — ``primary_bot.bot_tier_models(network, bot_id, "fast")``
   + ``model_pricing.lookup_price`` for rung selection and pricing — rather
   than importing a gallery script (which isn't a library import target even
   once merged). The run-file shape here (tokens/cost/steps/outcome) is
   modelled on Morning Board's ``memory/board-runs/<date>.json`` in spirit,
   not by field-for-field copy.

3. **No literal "register with the delivery monitor" call.**
   ``delivery_monitor.py``'s monitored set is built from a manifest's static
   ``scheduled_actions[]`` — the wrong shape for a dynamic, per-delegation,
   per-card window that starts and ends with one hand-over. This module
   keeps its OWN ledger (:data:`LEDGER_*`) using the exact same outcome
   vocabulary (``on_time``/``missed``, ``heal``) so the two ledgers read the
   same way to anyone who already knows delivery_monitor's, without forcing
   a per-delegation action into delivery_monitor's manifest-shaped model.

4. **``approval_granted``/``approval_declined`` are logged with
   ``actor="user"``, not the bot**, despite §8's "all server-appended with
   the bot's own identity as actor." Every other learning event here IS the
   worker's own act and gets the bot's identity; a grant/decline is the
   USER's decision about the bot's proposal. ``board_store.move_card``'s own
   docstring is explicit that ``actor`` is load-bearing for exactly this
   reason (a detector reading "what the user decided" must not count the
   bot's own actions as the user's) — the same rule applies here, so this
   module does not repeat the mislabel the brief's prose would have caused.

5. **The rung-too-low decline and the act/approval gate are dormant for
   today's shipped vocabulary — by design, not oversight.** No action in
   ``board_actions``'s starter vocabulary is act-shaped (send/book/pay/call):
   every ``kind: llm`` entry drafts, summarises, or looks something up, and
   the one real act (``call_ahead``, a phone call) is gated off entirely by
   ``RUNG_UNBUILT`` (a pod capability gap, not a rung question). So
   :func:`classify_decline`'s ``rung_too_low`` branch (keyed off an action's
   optional ``min_rung``) and the act/approval gate (keyed off an action's
   optional ``act``) are proven here only via fixture-only synthetic actions
   in the test suite — exactly like ``board_actions.py``'s own rung stand-in,
   which the SAME design track has not finished. When D-BI5's vocabulary
   grows an act-shaped action, it opts in by setting ``act``/``min_rung`` on
   its table entry; nothing here needs to change.

6. **No launchd wiring in this PR.** ``deploy.py`` and ``cli.py`` are both
   already sitting exactly on ``tools/file-size-baseline.txt``'s frozen line
   count (the no-growth ratchet, 4.1a) — this PR does not touch either file.
   :mod:`board_worker_runner` is the ready-to-install daemon entry point
   (same shape as ``signal_subscriber_runner.py``); wiring one
   ``_install_launchd_board_worker`` call into ``deploy.py`` next to
   ``_install_launchd_signal_subscriber`` (deploy.py:10803) is a one-function,
   near-zero-net-growth follow-up (it can reuse ``_job_spec_for``'s existing
   plist-building helpers), left separate so this PR's line count answers to
   the worker's own correctness, not to a capped file's ratchet math.

7. **The weekly receipt (D-PA2′) it feeds is design-only** —
   ``internal/design-pa-weekly-receipt-2026-08-31.md`` names the composer
   itself as not yet built ("Design only — the receipt composer is the
   concrete first slice of it"). There is no function to call from here.
   :func:`receipt_lines` is therefore a READER shaped for whatever calls it
   once the composer exists — ``{card, label, title, cost_usd, line}`` rows,
   ``line`` already in the exact "label (title) — $cost" shape §7 specifies
   — not a live integration. Whoever builds the composer sums these rows
   under the board app's line; nothing here needs to change when they do.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from . import board_actions
from . import board_store

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────
# Config (operator-editable via network.json ``pod.board_worker``)
# ─────────────────────────────────────────────────────────────────────────

#: The default per-card budget (D-BI4: "a budget shown before delegation").
#: Roughly 10x board_actions._EST_INPUT/OUTPUT_TOKENS at the fast rung's
#: typical per-token pricing, rounded to a number a tile can show plainly.
DEFAULT_PER_CARD_BUDGET_USD = 0.50

#: The default delivery window (D-BI4 §6): how long the bot has before
#: silence becomes a ledgered miss. An hour matches the scale of "find a
#: time that works" — long enough for a bounded LLM call plus one approval
#: round-trip, short enough that a stuck worker is caught the same morning.
DEFAULT_WINDOW_MINUTES = 60

#: Per-integration autonomy rung names (the §5 gate — see module docstring
#: deviation 5). Ordered low to high; every integration defaults to the
#: safest rung until an operator (or the not-yet-built ladder) says
#: otherwise.
RUNG_DRAFT_ONLY = "draft_only"
RUNG_ACT_WITH_APPROVAL = "act_with_approval"
RUNG_AUTONOMOUS = "autonomous_within_rules"
RUNGS = (RUNG_DRAFT_ONLY, RUNG_ACT_WITH_APPROVAL, RUNG_AUTONOMOUS)
_RUNG_ORDER = {name: i for i, name in enumerate(RUNGS)}


def worker_config(network: dict[str, Any]) -> dict[str, Any]:
    """``{per_card_usd, window_min}`` — operator overrides from
    ``network.json::pod.board_worker``, falling back to the defaults above.
    Resolved once per delegation, the same "read pod state fresh, never a
    frozen snapshot" rule ``board_actions.bot_action_context`` follows.
    """
    cfg = (network.get("pod") or {}).get("board_worker") or {}
    try:
        per_card_usd = float(cfg.get("per_card_usd", DEFAULT_PER_CARD_BUDGET_USD))
    except (TypeError, ValueError):
        per_card_usd = DEFAULT_PER_CARD_BUDGET_USD
    try:
        window_min = int(cfg.get("window_min", DEFAULT_WINDOW_MINUTES))
    except (TypeError, ValueError):
        window_min = DEFAULT_WINDOW_MINUTES
    return {"per_card_usd": per_card_usd, "window_min": window_min}


def _granted_rung(network: dict[str, Any], integration: str | None) -> str:
    """The autonomy rung an operator has granted this integration for the
    board worker's act gate. Missing/unknown integration -> the safe floor.
    """
    rungs = ((network.get("pod") or {}).get("board_worker") or {}).get("rungs") or {}
    name = rungs.get(integration or "", RUNG_DRAFT_ONLY)
    return name if name in _RUNG_ORDER else RUNG_DRAFT_ONLY


# ─────────────────────────────────────────────────────────────────────────
# Decline vocabulary (D-BI4 §3 — the fixed set, verbatim)
# ─────────────────────────────────────────────────────────────────────────

REASON_MISSING_INTEGRATION = "missing_integration"
REASON_RUNG_TOO_LOW = "rung_too_low"
REASON_NOT_YET_SUPPORTED = "not_yet_supported"
REASON_BUDGET_INSUFFICIENT = "budget_insufficient"
REASON_NEEDS_CONFIRMATION = "needs_confirmation"

DECLINE_REASONS = (
    REASON_MISSING_INTEGRATION, REASON_RUNG_TOO_LOW, REASON_NOT_YET_SUPPORTED,
    REASON_BUDGET_INSUFFICIENT, REASON_NEEDS_CONFIRMATION,
)

#: The exact prose D-BI4 §3 specifies, one per reason above — this is what
#: lands in ``board.progress``'s ``note`` and what the tile shows.
DECLINE_NOTE = {
    REASON_MISSING_INTEGRATION: "missing integration",
    REASON_RUNG_TOO_LOW: "rung too low",
    REASON_NOT_YET_SUPPORTED: "can't yet — tracking (D-PA6)",
    REASON_BUDGET_INSUFFICIENT: "budget insufficient",
    REASON_NEEDS_CONFIRMATION: "needs confirmation (email-sourced target)",
}

#: Action ids this worker has bespoke, deterministic code for (D-BI4 §4:
#: "the worker executes exactly the action_id from the event" — never a
#: model call to improvise one it doesn't recognise). ``find_directions`` is
#: board_actions' one ``kind: tool`` entry: deterministic, zero cost, no
#: model call. A ``kind: tool`` action not listed here has no code to run it
#: and is ``not_yet_supported``, whatever its id.
TOOL_ACTIONS = frozenset({"find_directions"})

#: Every ``kind: llm`` action — whatever its id, shipped vocabulary or not —
#: runs the SAME bounded, generic draft call (:func:`_make_llm_draft_handler`):
#: one model call that drafts, summarises, or looks something up from the
#: card + enrichment + action label alone. None of these performs a
#: send/book/pay/call (see deviation 5); they run to completion in one step.
#: There is deliberately no per-id allowlist here — the id only ever
#: distinguishes actions to the OPERATOR (board_actions' vocabulary); to this
#: worker every llm-kind action is the same shape of work.


def classify_decline(
    card: dict[str, Any], action: dict[str, Any], *,
    ctx: dict[str, Any], network: dict[str, Any],
    per_card_usd: float, spent_usd: float,
) -> str | None:
    """The D-BI4 §3 accept/decline gate. Returns a :data:`DECLINE_REASONS`
    member, or ``None`` to accept. Every branch is deterministic — no model
    call, per D-BI4: "a decline is free when the cause is deterministic."
    """
    # D-BI6 backstop: an email-sourced card must never carry a real
    # side-effecting (kind: tool) action whose target came from the email
    # itself. board_actions already keeps email cards to kind: llm drafts —
    # this exists so a stale or hand-edited instruction can't slip past that.
    if (card.get("source") == "email" and action.get("requires_confirm")
            and action.get("kind") == "tool"):
        return REASON_NEEDS_CONFIRMATION

    # An action may declare the rung it needs just to be ACCEPTED at all
    # (independent of the act/approval gate in run_delegation, which only
    # ever gates the FINAL send-equivalent step of an act-shaped action —
    # see module docstring deviation 5). No shipped action sets this today.
    min_rung = action.get("min_rung")
    if min_rung in _RUNG_ORDER:
        integration = action.get("_integration")
        if _RUNG_ORDER[_granted_rung(network, integration)] < _RUNG_ORDER[min_rung]:
            return REASON_RUNG_TOO_LOW

    # Integration gates, checked directly against ``ctx`` rather than
    # re-derived from board_actions.annotate_action's numeric requires_rung —
    # that number alone cannot distinguish "google not connected" from
    # "google connected but pricing unavailable" (both land on the same
    # disabled_reason), and this gate needs to tell those apart.
    integration = action.get("_integration")
    if integration == board_actions._INTEGRATION_CALLING:  # noqa: SLF001
        return REASON_NOT_YET_SUPPORTED  # RUNG_UNBUILT: a pod capability gap, not a grant gap
    if integration == board_actions._INTEGRATION_GOOGLE and not ctx.get("google"):  # noqa: SLF001
        return REASON_MISSING_INTEGRATION

    kind = action.get("kind")
    if kind == "tool":
        if action.get("id") not in TOOL_ACTIONS:
            return REASON_NOT_YET_SUPPORTED
        est_cost = 0.0
    elif kind == "llm":
        est_cost = ctx.get("llm_est_cost")
    else:
        return REASON_NOT_YET_SUPPORTED
    if est_cost is None or (spent_usd + est_cost) > per_card_usd:
        return REASON_BUDGET_INSUFFICIENT

    return None


# ─────────────────────────────────────────────────────────────────────────
# Isolated session context (CE-3)
# ─────────────────────────────────────────────────────────────────────────

#: How many days of event history to scan for one card. The events dir is
#: sharded by day (board_store.append_event); a card's own history rarely
#: spans more than a few days, and this bounds a pathological long-lived
#: card from making every wake an unbounded disk scan.
CARD_HISTORY_MAX_DAYS = 30


def read_card_events(
    shared_dir: Path, bot_id: str, card_id: str, *, max_days: int = CARD_HISTORY_MAX_DAYS,
) -> list[dict[str, Any]]:
    """This card's own rows from the per-bot event log, oldest first.

    Deliberately card-scoped: the worker's isolated session gets THIS card's
    history, never the board's — the CE-3 boundary this whole module exists
    to hold.
    """
    events_dir = board_store.board_dir(Path(shared_dir), bot_id) / "events"
    if not events_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(events_dir.glob("*.jsonl"))[-max_days:]:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("card") == card_id:
                out.append(row)
    out.sort(key=lambda r: r.get("ts", ""))
    return out


#: The context keys an isolated worker session may see. Anything not named
#: here — chat, memory files, other cards, board.list — is absent by
#: construction because :func:`build_worker_context` never reads it, not
#: because it is filtered out afterward.
CONTEXT_KEYS = frozenset({"card", "enrichment", "action", "history"})


def build_worker_context(
    shared_dir: Path, bot_id: str, card: dict[str, Any], action: dict[str, Any],
) -> dict[str, Any]:
    """The whole context one isolated session gets (CE-3). See
    :data:`CONTEXT_KEYS`."""
    return {
        "card": {
            "id": card.get("id"), "title": card.get("title"),
            "cluster": card.get("cluster"), "lane": card.get("lane"),
            "owner": card.get("owner"), "source": card.get("source"),
            "note": card.get("note", ""),
        },
        "enrichment": card.get("enrichment") or {},
        "action": {
            "id": action.get("id"), "label": action.get("label"),
            "kind": action.get("kind"),
        },
        "history": read_card_events(shared_dir, bot_id, card.get("id", "")),
    }


# ─────────────────────────────────────────────────────────────────────────
# Model client + budget (D-CC)
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class ModelReply:
    text: str
    input_tokens: int
    output_tokens: int


class ModelClient(Protocol):
    def __call__(self, *, prompt: str, context: dict[str, Any]) -> ModelReply: ...


class BudgetExceeded(Exception):
    def __init__(self, spent_usd: float, cap_usd: float, step: int):
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd
        self.step = step
        super().__init__(f"budget ${cap_usd:.2f} reached at step {step}")


@dataclass
class RunBudget:
    """Tracks tokens+cost+calls against the per-card cap (D-CC applied at
    card scope). ``record`` raises the moment the cap is exceeded — the
    caller stops immediately, per D-CC2: no turn after the checkpoint."""

    per_card_usd: float
    spent_usd: float = 0.0
    calls: int = 0
    step: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def record(self, *, cost_usd: float = 0.0, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.step += 1
        self.calls += 1
        self.spent_usd += cost_usd
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        if self.spent_usd > self.per_card_usd:
            raise BudgetExceeded(self.spent_usd, self.per_card_usd, self.step)


def _fast_rates(bot_id: str, network: dict[str, Any], shared_dir: Path) -> tuple[float, float] | None:
    """Reuse board_actions' own fast-rung pricing lookup — see deviation 2:
    the SAME primitives Morning Board's script uses, not a re-derivation."""
    return board_actions._fast_rung_cost_per_token(bot_id, network, shared_dir)  # noqa: SLF001


# ─────────────────────────────────────────────────────────────────────────
# Step handlers — one per implemented action id
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class StepResult:
    done: bool
    result_text: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    note: str = ""


StepHandler = Callable[[dict[str, Any], "ModelClient"], StepResult]


def _handle_find_directions(ctx: dict[str, Any], model_client: ModelClient) -> StepResult:
    """Deterministic, zero-cost — board_actions' one ``kind: tool`` entry."""
    location = ctx.get("enrichment", {}).get("location")
    value = location.get("value") if isinstance(location, dict) else None
    text = value.get("text") if isinstance(value, dict) else None
    if not text:
        return StepResult(done=True, result_text="No location on this card to find directions for.")
    maps_url = (value.get("maps_url") if isinstance(value, dict) else None) or (
        "https://maps.google.com/?q=" + urllib.parse.quote(text))
    return StepResult(done=True, result_text=f"Directions to {text}: {maps_url}")


def _draft_prompt(ctx: dict[str, Any]) -> str:
    card = ctx["card"]
    action = ctx["action"]
    lines = [
        f"Action: {action['label']} ({action['id']})",
        f"Card: {card['title']} [{card['cluster']}]",
    ]
    if card.get("note"):
        lines.append(f"Note: {card['note']}")
    for name, f in ctx.get("enrichment", {}).items():
        lines.append(f"{name}: {f.get('value')!r} (source: {f.get('source')})")
    lines.append(
        "Produce a short, ready-to-use draft for this one action. "
        "Do not send, book, or pay for anything — this is a draft only.")
    return "\n".join(lines)


def _make_llm_draft_handler(*, rates: tuple[float, float] | None) -> StepHandler:
    def _handler(ctx: dict[str, Any], model_client: ModelClient) -> StepResult:
        reply = model_client(prompt=_draft_prompt(ctx), context=ctx)
        cost = 0.0
        if rates is not None:
            in_cost, out_cost = rates
            cost = reply.input_tokens * in_cost + reply.output_tokens * out_cost
        return StepResult(
            done=True, result_text=reply.text, cost_usd=round(cost, 6),
            input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
        )
    return _handler


#: Bespoke handlers for ``kind: tool`` action ids (deterministic code per
#: id — see :data:`TOOL_ACTIONS`). Every ``kind: llm`` action instead gets
#: the one generic draft handler, resolved by kind rather than id.
_TOOL_HANDLERS: dict[str, StepHandler] = {"find_directions": _handle_find_directions}


def resolve_step_handler(
    action: dict[str, Any], *, rates: tuple[float, float] | None,
    step_handlers: dict[str, StepHandler] | None = None,
) -> StepHandler | None:
    """The handler that will run this action, or ``None`` if none exists.

    A caller-supplied ``step_handlers`` override (tests only) wins by id
    first; otherwise a ``kind: tool`` action resolves through
    :data:`_TOOL_HANDLERS` by id, and a ``kind: llm`` action always gets the
    generic draft handler — :func:`classify_decline` has already decided
    "implemented" the same way, by kind, so the two never disagree.
    """
    action_id = action.get("id")
    if not isinstance(action_id, str):
        return None
    if step_handlers is not None and action_id in step_handlers:
        return step_handlers[action_id]
    if action.get("kind") == "tool":
        return _TOOL_HANDLERS.get(action_id)
    if action.get("kind") == "llm":
        return _make_llm_draft_handler(rates=rates)
    return None


# ─────────────────────────────────────────────────────────────────────────
# Run files (evidence.ran) + ledger (D-BI4 §6, delivery-window miss)
# ─────────────────────────────────────────────────────────────────────────

OUTCOME_ON_TIME = "on_time"
OUTCOME_MISSED = "missed"

LEDGER_PRODUCER = "board_worker"


def _now_stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def run_file_dir(shared_dir: Path, bot_id: str) -> Path:
    return board_store.board_dir(Path(shared_dir), bot_id) / "worker-runs"


def run_file_path(shared_dir: Path, bot_id: str, card_id: str) -> Path:
    return run_file_dir(shared_dir, bot_id) / f"{card_id}.json"


def _write_run_file(shared_dir: Path, bot_id: str, card_id: str, run: dict[str, Any]) -> Path:
    d = run_file_dir(shared_dir, bot_id)
    d.mkdir(parents=True, exist_ok=True)
    p = run_file_path(shared_dir, bot_id, card_id)
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".run-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(run, f, indent=1, ensure_ascii=False)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return p


def ledger_dir(shared_dir: Path) -> Path:
    return Path(shared_dir) / "board_worker" / "ledger"


def _ledger_path(shared_dir: Path, day: str) -> Path:
    return ledger_dir(shared_dir) / f"{day}.jsonl"


def _append_ledger_row(shared_dir: Path, row: dict[str, Any]) -> None:
    d = ledger_dir(shared_dir)
    d.mkdir(parents=True, exist_ok=True)
    day = row["ts"][:10]
    with _ledger_path(shared_dir, day).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _ledger_row(
    *, bot_id: str, card_id: str, action_id: str, window_start: str,
    window_end: str, outcome: str, delivered_at: str | None, now: datetime,
    producer: str = LEDGER_PRODUCER,
) -> dict[str, Any]:
    return {
        "ts": _now_stamp(now), "bot_id": bot_id, "card_id": card_id,
        "action_id": action_id, "window_start": window_start,
        "window_end": window_end, "outcome": outcome,
        "delivered_at": delivered_at,
        # D-BI4 §6: never an automatic retry that spends again — a human
        # decides. Always "none"; distinct from delivery_monitor.py's
        # scheduled-action heal, which this module deliberately does not
        # borrow (see deviation 3).
        "heal": "none",
        # Which subsystem wrote this row — the authoritative discriminator
        # for a shared ledger with more than one writer (board_touch.py's
        # miss sweep is the other). A real action_id could theoretically
        # collide with a naming convention like a string prefix; this
        # field never can.
        "producer": producer,
    }


def _record_learning_event(
    shared_dir: Path, bot_id: str, card: dict[str, Any], event_name: str,
    *, actor: str, **fields: Any,
) -> None:
    board_store.append_event(shared_dir, bot_id, {
        "event": event_name, "card": card.get("id"), "title": card.get("title"),
        "actor": actor, **fields,
    })


def record_delivery_outcome(
    shared_dir: Path, *, bot_id: str, card_id: str, action_id: str,
    window_start: str, window_end: str, outcome: str,
    delivered_at: str | None = None, now: datetime | None = None,
    producer: str = LEDGER_PRODUCER,
) -> dict[str, Any]:
    """Public wrapper around the D-BI4 §6 ledger row writer.

    :mod:`board_touch`'s own miss sweep (D-TM3/D-TM10) reuses this so a
    ``remind``/``check_source``/``ask`` touch that misses its window lands
    in the SAME ledger a delegation miss does — one ledger the pod report
    reads for "touches due / on time / missed", not two divergent ones.
    ``producer`` (default this module's own :data:`LEDGER_PRODUCER`) is how
    a reader tells the two classes of row apart — never the ``action_id``
    string shape, which a real action id is free to collide with.
    """
    now = now or datetime.now(timezone.utc)
    row = _ledger_row(
        bot_id=bot_id, card_id=card_id, action_id=action_id,
        window_start=window_start, window_end=window_end, outcome=outcome,
        delivered_at=delivered_at, now=now, producer=producer)
    _append_ledger_row(shared_dir, row)
    return row


# ─────────────────────────────────────────────────────────────────────────
# The bounded run (D-BI4 §2/§4/§5)
# ─────────────────────────────────────────────────────────────────────────

def _act_needs_approval(
    card: dict[str, Any], action: dict[str, Any], network: dict[str, Any],
) -> bool:
    """§5: gate an externally-visible act on the granted autonomy rung, with
    the egress/exfil edge folded in — an email-sourced target always needs
    approval, even if the integration were granted ``autonomous_within_rules``
    (deviation 5's D-BI6 fold-in)."""
    if not action.get("act"):
        return False
    if card.get("source") == "email" and action.get("requires_confirm"):
        return True
    granted = _granted_rung(network, action.get("_integration"))
    return _RUNG_ORDER[granted] < _RUNG_ORDER[RUNG_AUTONOMOUS]


def _self_check_rung_gate() -> None:
    """The three-rung self-check :mod:`board_worker_runner` runs before it
    will start (hold-fix-4279-approval-rung-and-busy-expiry): approval is
    required at every rung except ``autonomous_within_rules``. Deliberately
    NOT run at module import — this module is imported broadly across the
    admin server, and a future regression here must block the DAEMON from
    starting, not crash every other consumer of this module."""
    _card, _action = {"source": "web"}, {"act": "x", "_integration": "self_check"}
    for rung, expect_approval in (
        (RUNG_DRAFT_ONLY, True), (RUNG_ACT_WITH_APPROVAL, True), (RUNG_AUTONOMOUS, False),
    ):
        _net = {"pod": {"board_worker": {"rungs": {"self_check": rung}}}}
        assert _act_needs_approval(_card, _action, _net) is expect_approval, (
            f"board_worker rung gate inverted at {rung!r} "
            "(hold-fix-4279-approval-rung-and-busy-expiry)")


def run_delegation(
    shared_dir: Path, bot_id: str, network: dict[str, Any], card_id: str,
    action_id: str, *, model_client: ModelClient, now: datetime | None = None,
    max_steps: int = 6, step_handlers: dict[str, StepHandler] | None = None,
) -> dict[str, Any]:
    """One wake: accept-or-decline, then (if accepted) run the named action
    to completion, a budget trip, or an approval gate. Synchronous — see the
    module docstring on what "isolated session" means here.
    """
    now = now or datetime.now(timezone.utc)
    board = board_store.load_board(shared_dir, bot_id)
    try:
        card = board_store.resolve_card(board, card_id)
    except (KeyError, ValueError):
        return {"outcome": "skipped", "why": "card not found"}
    card_id = card["id"]
    if (card.get("owner") or "me") != "bot":
        return {"outcome": "skipped", "why": "card not bot-owned"}

    cfg = worker_config(network)
    per_card_usd = cfg["per_card_usd"]
    prior_spent = float((card.get("delegation") or {}).get("cost_to_date") or 0.0)

    action = board_actions.find_action(card, action_id)
    if action is None:
        board_store.set_delegation_progress(
            shared_dir, bot_id, card_id, state="blocked",
            note=DECLINE_NOTE[REASON_NOT_YET_SUPPORTED],
            cost_to_date=prior_spent, actor=bot_id)
        _record_learning_event(shared_dir, bot_id, card, "declined",
                                reason=REASON_NOT_YET_SUPPORTED, actor=bot_id)
        return {"outcome": "declined", "reason": REASON_NOT_YET_SUPPORTED}

    ctx = board_actions.bot_action_context(bot_id, network, Path(shared_dir))
    reason = classify_decline(
        card, action, ctx=ctx, network=network,
        per_card_usd=per_card_usd, spent_usd=prior_spent)
    if reason is not None:
        board_store.set_delegation_progress(
            shared_dir, bot_id, card_id, state="blocked",
            note=DECLINE_NOTE[reason], cost_to_date=prior_spent, actor=bot_id)
        _record_learning_event(shared_dir, bot_id, card, "declined",
                                reason=reason, actor=bot_id)
        return {"outcome": "declined", "reason": reason}

    board_store.set_delegation_progress(
        shared_dir, bot_id, card_id, state="accepted",
        cost_to_date=prior_spent, actor=bot_id)
    _record_learning_event(shared_dir, bot_id, card, "accepted", actor=bot_id)

    window_min = cfg["window_min"]
    deadline = now + timedelta(minutes=window_min)
    run: dict[str, Any] = {
        "card_id": card_id, "bot_id": bot_id, "action_id": action_id,
        "started_at": _now_stamp(now), "window_start": _now_stamp(now),
        "window_deadline": _now_stamp(deadline),
        "steps": [], "cost_usd": prior_spent, "input_tokens": 0, "output_tokens": 0,
        "outcome": None,
    }
    _write_run_file(shared_dir, bot_id, card_id, run)

    handler = resolve_step_handler(
        action, rates=_fast_rates(bot_id, network, Path(shared_dir)),
        step_handlers=step_handlers)
    if handler is None:  # pragma: no cover — classify_decline already checked
        board_store.set_delegation_progress(
            shared_dir, bot_id, card_id, state="blocked",
            note=DECLINE_NOTE[REASON_NOT_YET_SUPPORTED],
            cost_to_date=prior_spent, actor=bot_id)
        _record_learning_event(shared_dir, bot_id, card, "declined",
                                reason=REASON_NOT_YET_SUPPORTED, actor=bot_id)
        run["outcome"] = "declined"
        _write_run_file(shared_dir, bot_id, card_id, run)
        return {"outcome": "declined", "reason": REASON_NOT_YET_SUPPORTED}

    if _act_needs_approval(card, action, network):
        wctx = build_worker_context(shared_dir, bot_id, card, action)
        try:
            step = handler(wctx, model_client)
        except BudgetExceeded:  # pragma: no cover — defensive; draft step is cheap
            step = StepResult(done=False, result_text="")
        payload_summary = step.result_text or f"proposed: {action.get('label')} on '{card.get('title')}'"
        total_cost = round(prior_spent + step.cost_usd, 6)
        board_store.record_approval_request(
            shared_dir, bot_id, card_id, act=str(action.get("act")),
            payload_summary=payload_summary, est_cost=total_cost, actor=bot_id)
        board_store.set_delegation_progress(
            shared_dir, bot_id, card_id, state="in_progress",
            note="awaiting approval", cost_to_date=total_cost, actor=bot_id)
        # record_approval_request above already appended the
        # "approval_requested" learning event (actor=bot_id) — no separate
        # _record_learning_event call here, or it would double-log.
        run["outcome"] = "approval_requested"
        run["cost_usd"] = total_cost
        _write_run_file(shared_dir, bot_id, card_id, run)
        return {"outcome": "approval_requested", "cost_usd": total_cost}

    return _run_steps(
        shared_dir, bot_id, network, card, action, handler, model_client,
        budget=RunBudget(per_card_usd=per_card_usd, spent_usd=prior_spent),
        run=run, max_steps=max_steps)


def _run_steps(
    shared_dir: Path, bot_id: str, network: dict[str, Any], card: dict[str, Any],
    action: dict[str, Any], handler: StepHandler, model_client: ModelClient,
    *, budget: RunBudget, run: dict[str, Any], max_steps: int,
) -> dict[str, Any]:
    card_id = card["id"]
    wctx = build_worker_context(shared_dir, bot_id, card, action)
    result_text = ""
    for _ in range(max_steps):
        step = handler(wctx, model_client)
        # Captured before the budget check: the call already happened (and
        # its cost is about to be charged either way), so whatever it
        # produced is real partial work — D-CC's "keep partial work on the
        # card" applies to the step that trips the budget too, not just the
        # ones before it.
        if step.result_text:
            result_text = step.result_text
        try:
            budget.record(
                cost_usd=step.cost_usd, input_tokens=step.input_tokens,
                output_tokens=step.output_tokens)
        except BudgetExceeded as exc:
            note = f"budget ${budget.per_card_usd:.2f} reached at step {exc.step}"
            board_store.set_delegation_progress(
                shared_dir, bot_id, card_id, state="blocked", note=note,
                cost_to_date=round(budget.spent_usd, 6),
                result={"text": result_text, "cost": round(budget.spent_usd, 6)} if result_text else None,
                actor=bot_id)
            _record_learning_event(
                shared_dir, bot_id, card, "budget_tripped",
                step=exc.step, cap_usd=budget.per_card_usd,
                spent_usd=round(budget.spent_usd, 6), actor=bot_id)
            run["outcome"] = "budget_tripped"
            run["cost_usd"] = round(budget.spent_usd, 6)
            run["steps"].append({"note": note})
            _write_run_file(shared_dir, bot_id, card_id, run)
            return {"outcome": "budget_tripped", "note": note, "cost_usd": run["cost_usd"]}

        run["steps"].append({"note": step.note, "done": step.done})
        if step.done:
            board_store.set_delegation_progress(
                shared_dir, bot_id, card_id, state="done",
                cost_to_date=round(budget.spent_usd, 6),
                result={"text": result_text, "cost": round(budget.spent_usd, 6)},
                actor=bot_id)
            run["outcome"] = OUTCOME_ON_TIME
            run["cost_usd"] = round(budget.spent_usd, 6)
            run["delivered_at"] = _now_stamp(datetime.now(timezone.utc))
            _write_run_file(shared_dir, bot_id, card_id, run)
            return {"outcome": "done", "result_text": result_text, "cost_usd": run["cost_usd"]}

    # Ran out of steps without finishing — returned for review rather than
    # silently discarded (D-BI4 §4: progress is done/returned_for_review/done).
    board_store.set_delegation_progress(
        shared_dir, bot_id, card_id, state="returned_for_review",
        note=f"stopped after {max_steps} steps", cost_to_date=round(budget.spent_usd, 6),
        result={"text": result_text, "cost": round(budget.spent_usd, 6)} if result_text else None,
        actor=bot_id)
    run["outcome"] = OUTCOME_ON_TIME
    run["cost_usd"] = round(budget.spent_usd, 6)
    run["delivered_at"] = _now_stamp(datetime.now(timezone.utc))
    _write_run_file(shared_dir, bot_id, card_id, run)
    return {"outcome": "returned_for_review", "cost_usd": run["cost_usd"]}


def resume_delegation(
    shared_dir: Path, bot_id: str, card_id: str, *, granted: bool, actor: str = "user",
) -> dict[str, Any]:
    """§5: "the worker resumes on approval_granted (one more wake, same
    budget line)." Declined ends the delegation (terminal ``blocked``);
    granted completes with the already-produced draft as the result — the
    draft was already produced (and its cost already charged) when the
    approval was requested, so resuming never re-runs the model.
    """
    board = board_store.load_board(shared_dir, bot_id)
    card = board_store.resolve_card(board, card_id)
    card_id = card["id"]
    pending = card.get("pending_approval")
    if not pending:
        return {"outcome": "skipped", "why": "no pending approval"}

    board_store.record_approval_decision(
        shared_dir, bot_id, card_id, granted=granted, actor=actor)
    cost_to_date = (card.get("delegation") or {}).get("cost_to_date")
    if not granted:
        board_store.set_delegation_progress(
            shared_dir, bot_id, card_id, state="blocked",
            note="approval declined", cost_to_date=cost_to_date, actor=bot_id)
        _record_learning_event(shared_dir, bot_id, card, "declined",
                                reason="approval_declined", actor=bot_id)
        return {"outcome": "declined"}

    board_store.set_delegation_progress(
        shared_dir, bot_id, card_id, state="done", note="approved",
        cost_to_date=cost_to_date,
        result={"text": pending.get("payload_summary", ""), "cost": cost_to_date},
        actor=bot_id)
    run_path = run_file_path(shared_dir, bot_id, card_id)
    if run_path.exists():
        try:
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["outcome"] = OUTCOME_ON_TIME
            run["delivered_at"] = _now_stamp(datetime.now(timezone.utc))
            _write_run_file(shared_dir, bot_id, card_id, run)
        except (OSError, json.JSONDecodeError) as exc:
            # Best-effort: the run file is evidence for the ledger sweep, not
            # the source of truth (the card's own delegation state already
            # transitioned above) — a corrupt/unreadable run file here just
            # means this delegation's run-file record stays stale.
            log.warning("board_worker: could not update run file for %s/%s: %s",
                        bot_id, card_id, exc)
    return {"outcome": "done", "cost_usd": cost_to_date}


# ─────────────────────────────────────────────────────────────────────────
# Delivery-window sweep (D-BI4 §6 — "silence is a miss")
# ─────────────────────────────────────────────────────────────────────────

#: Delegation states a run file's outcome is still considered "in flight"
#: for — i.e. the window clock is still running. Matches the D-BI4 §4 list
#: of non-terminal progress states.
_TERMINAL_OUTCOMES = frozenset({OUTCOME_ON_TIME, OUTCOME_MISSED, "budget_tripped", "declined"})


def sweep_delivery_windows(shared_dir: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Ledger every run file whose window has elapsed with no terminal
    outcome as a miss. Never retries — D-BI4 §6: "heal: none ... a human
    decides." Returns the ledger rows written this sweep.
    """
    now = now or datetime.now(timezone.utc)
    written: list[dict[str, Any]] = []
    boards_root = Path(shared_dir) / "boards"
    if not boards_root.is_dir():
        return written
    for bot_dir in sorted(boards_root.iterdir()):
        runs_dir = bot_dir / "worker-runs"
        if not runs_dir.is_dir():
            continue
        for run_path in sorted(runs_dir.glob("*.json")):
            try:
                run = json.loads(run_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if run.get("outcome") in _TERMINAL_OUTCOMES:
                continue
            deadline = board_store._parse_ts(run.get("window_deadline"))  # noqa: SLF001
            if deadline is None or now <= deadline:
                continue
            row = _ledger_row(
                bot_id=run.get("bot_id", bot_dir.name), card_id=run.get("card_id", ""),
                action_id=run.get("action_id", ""), window_start=run.get("window_start"),
                window_end=run.get("window_deadline"), outcome=OUTCOME_MISSED,
                delivered_at=None, now=now)
            _append_ledger_row(shared_dir, row)
            run["outcome"] = OUTCOME_MISSED
            _write_run_file(shared_dir, run.get("bot_id", bot_dir.name), run.get("card_id", ""), run)
            try:
                board = board_store.load_board(shared_dir, run.get("bot_id", bot_dir.name))
                card = board_store.find_card(board, run.get("card_id", ""))
                if card is not None:
                    card["delivery_missed"] = True
                    board_store.save_board(shared_dir, run.get("bot_id", bot_dir.name), board)
                    _record_learning_event(
                        shared_dir, run.get("bot_id", bot_dir.name), card, "missed",
                        action_id=run.get("action_id", ""), actor=run.get("bot_id", bot_dir.name))
            except (KeyError, ValueError) as exc:
                # Best-effort: the ledger row above is already the
                # authoritative miss record — a card that vanished or a
                # board that failed to resolve just means the tile-facing
                # "delivery_missed" stamp and the "missed" learning event
                # don't land too, not that the miss goes unrecorded.
                log.warning("board_worker: could not stamp card %s/%s as missed: %s",
                            run.get("bot_id", bot_dir.name), run.get("card_id", ""), exc)
            written.append(row)
    return written


# ─────────────────────────────────────────────────────────────────────────
# Weekly receipt line (D-PA2′, §7 — "under the board app's line")
# ─────────────────────────────────────────────────────────────────────────

def format_receipt_line(action_label: str, card_title: str, cost_usd: float) -> str:
    """"Find a time that works (dentist) — $0.04" — the exact shape §7
    specifies."""
    return f"{action_label} ({card_title}) — ${cost_usd:.2f}"


def receipt_lines(
    shared_dir: Path, bot_id: str, *, since: datetime, until: datetime,
) -> list[dict[str, Any]]:
    """Every completed delegation's cost, formatted for the weekly receipt.
    Scans the same per-bot event log every other board reader scans —
    no new plumbing, per §7."""
    events_dir = board_store.board_dir(Path(shared_dir), bot_id) / "events"
    if not events_dir.is_dir():
        return []
    titles: dict[str, str] = {}
    labels: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    day = since.date()
    while day <= until.date():
        p = events_dir / f"{day.isoformat()}.jsonl"
        day += timedelta(days=1)
        if not p.is_file():
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            ts = board_store._parse_ts(row.get("ts"))  # noqa: SLF001
            if ts is None or not (since <= ts <= until):
                continue
            if row.get("event") == "instruction":
                titles[row["card"]] = row.get("title", "")
                labels[row["card"]] = row.get("action_label", "")
            elif row.get("event") == "delegation" and row.get("to") in ("done", "returned_for_review"):
                card_id = row.get("card")
                cost = row.get("cost_to_date")
                if cost is None:
                    continue
                label = labels.get(card_id) or "Delegated action"
                title = titles.get(card_id) or row.get("title") or ""
                out.append({
                    "card": card_id, "label": label, "title": title,
                    "cost_usd": round(float(cost), 4),
                    "line": format_receipt_line(label, title, float(cost)),
                })
    return out


# ─────────────────────────────────────────────────────────────────────────
# Daemon-side subscriber (D-BI4 §1 — wake on event, not on prompt)
# ─────────────────────────────────────────────────────────────────────────

#: Board events that wake a worker. ``assigned`` only wakes one when the
#: card has NO instruction yet (an ``offered`` hand-over with nothing to run)
#: — §1's "owner -> bot, delegation.state = offered with no instruction yet."
_WAKE_EVENTS = frozenset({"instruction", "assigned", "approval_granted", "approval_declined"})


def cursor_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "board_worker" / "cursor.json"


def _load_cursor(shared_dir: Path) -> dict[str, int]:
    try:
        return json.loads(cursor_path(shared_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cursor(shared_dir: Path, cursor: dict[str, int]) -> None:
    p = cursor_path(shared_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".cursor-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cursor, f)
    os.replace(tmp, p)


def busy_path(shared_dir: Path, bot_id: str, card_id: str) -> Path:
    return Path(shared_dir) / "board_worker" / "busy" / bot_id / f"{card_id}.json"


def queue_path(shared_dir: Path, bot_id: str, card_id: str) -> Path:
    return Path(shared_dir) / "board_worker" / "queued" / bot_id / f"{card_id}.jsonl"


def _mark_busy(shared_dir: Path, bot_id: str, card_id: str, event: dict[str, Any]) -> None:
    p = busy_path(shared_dir, bot_id, card_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"started_at": event.get("ts"), "event": event.get("event")}), encoding="utf-8")


def _clear_busy(shared_dir: Path, bot_id: str, card_id: str) -> None:
    busy_path(shared_dir, bot_id, card_id).unlink(missing_ok=True)


def _is_busy(shared_dir: Path, bot_id: str, card_id: str) -> bool:
    return busy_path(shared_dir, bot_id, card_id).exists()


def _clear_stale_busy(
    shared_dir: Path, network: dict[str, Any], bot_id: str, card_id: str, *, now: datetime,
) -> bool:
    """A ``busy`` marker older than the configured delivery window means the
    runner that set it is dead (killed mid-run, power loss) — nothing will
    ever clear it. Treat it as stale: clear the marker, log a ``stale_busy``
    learning event, and return True so the caller dispatches/drains instead
    of queuing behind a marker nothing drains (pr-4279 review, finding 2). A
    marker with no readable/parseable ``started_at`` is left alone — busy,
    not stale, since there is nothing to measure its age against.
    """
    p = busy_path(shared_dir, bot_id, card_id)
    try:
        marker = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    started = board_store._parse_ts(marker.get("started_at"))  # noqa: SLF001
    if started is None:
        return False
    window_min = worker_config(network)["window_min"]
    if now < started + timedelta(minutes=window_min):
        return False
    _clear_busy(shared_dir, bot_id, card_id)
    try:
        board = board_store.load_board(shared_dir, bot_id)
        card = board_store.find_card(board, card_id)
    except (KeyError, ValueError):
        card = None
    if card is not None:
        _record_learning_event(
            shared_dir, bot_id, card, "stale_busy",
            started_at=marker.get("started_at"), window_min=window_min, actor=bot_id)
    return True


def _enqueue(shared_dir: Path, bot_id: str, card_id: str, event: dict[str, Any]) -> None:
    p = queue_path(shared_dir, bot_id, card_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _dequeue_all(shared_dir: Path, bot_id: str, card_id: str) -> list[dict[str, Any]]:
    p = queue_path(shared_dir, bot_id, card_id)
    if not p.exists():
        return []
    try:
        rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError):
        rows = []
    p.unlink(missing_ok=True)
    return rows


def _dispatch_one(
    shared_dir: Path, network: dict[str, Any], bot_id: str, event: dict[str, Any],
    *, model_client: ModelClient,
) -> dict[str, Any]:
    card_id = event.get("card")
    kind = event.get("event")
    if not isinstance(card_id, str) or not card_id:
        return {"outcome": "skipped"}  # pragma: no cover — poll_once already filters this
    if kind == "instruction":
        action_id = event.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            return {"outcome": "skipped"}
        return run_delegation(
            shared_dir, bot_id, network, card_id, action_id,
            model_client=model_client)
    if kind == "assigned":
        board = board_store.load_board(shared_dir, bot_id)
        try:
            card = board_store.resolve_card(board, card_id)
        except (KeyError, ValueError):
            return {"outcome": "skipped"}
        if (card.get("owner") or "me") != "bot" or card.get("instructed"):
            return {"outcome": "skipped"}
        if (card.get("delegation") or {}).get("state") != "offered":
            return {"outcome": "skipped"}
        # Offered with no instruction yet: nothing to decide (no action
        # named), so the worker acknowledges the hand-over — accepted — and
        # then WAITS; it never invents an action from "handle this" (§3).
        # The note makes the wait legible — without it "accepted" alone reads
        # as "bot working" on the phone (pr-4279 review, finding 3).
        board_store.set_delegation_progress(
            shared_dir, bot_id, card_id, state="accepted",
            note="waiting for you to pick an action", actor=bot_id)
        _record_learning_event(shared_dir, bot_id, card, "accepted", actor=bot_id)
        return {"outcome": "waiting"}
    if kind in ("approval_granted", "approval_declined"):
        return resume_delegation(
            shared_dir, bot_id, card_id, granted=(kind == "approval_granted"))
    return {"outcome": "skipped"}  # pragma: no cover — _WAKE_EVENTS is exhaustive


def _queued_dir(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / "board_worker" / "queued" / bot_id


def _drain_queued(
    shared_dir: Path, network: dict[str, Any], bot_id: str,
    *, model_client: ModelClient, results: list[dict[str, Any]], now: datetime,
) -> None:
    """Cards left with queued events from a prior busy window get dispatched
    even when no FRESH event arrives to re-trigger the cursor scan below —
    the cursor already advanced past whatever was enqueued the tick it
    arrived (it only records "seen", not "dispatched"), so without this a
    card that went busy-then-free between two ticks would sit on a queued
    event forever. §1's "second event queues on the card, never a second
    session" — this is what un-queues it. A stale busy marker (dead runner)
    is cleared the same way here as in :func:`poll_once`'s fresh-event path,
    or a card behind it would queue forever instead.
    """
    d = _queued_dir(shared_dir, bot_id)
    if not d.is_dir():
        return
    for qp in sorted(d.glob("*.jsonl")):
        card_id = qp.stem
        if _is_busy(shared_dir, bot_id, card_id) and not _clear_stale_busy(
                shared_dir, network, bot_id, card_id, now=now):
            continue
        pending = _dequeue_all(shared_dir, bot_id, card_id)
        if not pending:
            continue
        _mark_busy(shared_dir, bot_id, card_id, pending[0])
        try:
            for one in pending:
                results.append(_dispatch_one(
                    shared_dir, network, bot_id, one, model_client=model_client))
        finally:
            _clear_busy(shared_dir, bot_id, card_id)


def poll_once(
    shared_dir: Path, network: dict[str, Any], *, model_client: ModelClient,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """One tick: find new wake-events across every bot's board, dispatch one
    worker per card (queuing a second event on a busy card), sweep the
    delivery-window ledger. Returns the dispatch results for this tick.
    """
    results: list[dict[str, Any]] = []
    now = now or datetime.now(timezone.utc)
    cursor = _load_cursor(shared_dir)
    for bot_id in sorted((network.get("bots") or {}).keys()):
        _drain_queued(shared_dir, network, bot_id, model_client=model_client, results=results, now=now)
        events_dir = board_store.board_dir(Path(shared_dir), bot_id) / "events"
        if not events_dir.is_dir():
            continue
        for p in sorted(events_dir.glob("*.jsonl")):
            key = f"{bot_id}:{p.name}"
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            start = cursor.get(key, 0)
            for line in lines[start:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event") not in _WAKE_EVENTS:
                    continue
                card_id = event.get("card")
                if not card_id:
                    continue
                if _is_busy(shared_dir, bot_id, card_id) and not _clear_stale_busy(
                        shared_dir, network, bot_id, card_id, now=now):
                    _enqueue(shared_dir, bot_id, card_id, event)
                    continue
                _mark_busy(shared_dir, bot_id, card_id, event)
                try:
                    pending = [event, *_dequeue_all(shared_dir, bot_id, card_id)]
                    for one in pending:
                        results.append(_dispatch_one(
                            shared_dir, network, bot_id, one, model_client=model_client))
                finally:
                    _clear_busy(shared_dir, bot_id, card_id)
            cursor[key] = len(lines)
    _save_cursor(shared_dir, cursor)
    sweep_delivery_windows(shared_dir, now=now)
    return results


@dataclass
class _LoopState:
    last_prune: float = 0.0


LEDGER_PRUNE_INTERVAL_SECONDS = 3600.0


def run_loop(
    shared_dir: Path, network: dict[str, Any], *,
    model_client: ModelClient,
    poll_interval_seconds: float = 1.0,
    stop_after_seconds: float | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    now_fn: Callable[[], float] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> int:
    """The daemon loop (D-BI4 §1: 1 Hz, debounced by the busy/queue files
    above). Same shape as ``signals.subscriber.run_loop``."""
    sleep_fn = sleep_fn or time.sleep
    now_fn = now_fn or time.monotonic
    log_fn = log_fn or log.info

    total = 0
    started = now_fn()
    while True:
        results = poll_once(shared_dir, network, model_client=model_client)
        total += len(results)
        if results:
            log_fn(f"[board_worker] dispatched {len(results)} event(s)")
        if stop_after_seconds is not None and (now_fn() - started) >= stop_after_seconds:
            return total
        sleep_fn(poll_interval_seconds)


__all__ = [
    "DEFAULT_PER_CARD_BUDGET_USD", "DEFAULT_WINDOW_MINUTES",
    "RUNG_DRAFT_ONLY", "RUNG_ACT_WITH_APPROVAL", "RUNG_AUTONOMOUS",
    "worker_config", "DECLINE_REASONS", "DECLINE_NOTE", "classify_decline",
    "TOOL_ACTIONS", "resolve_step_handler", "build_worker_context", "read_card_events",
    "CONTEXT_KEYS", "ModelReply", "ModelClient", "RunBudget", "BudgetExceeded",
    "run_delegation", "resume_delegation", "sweep_delivery_windows",
    "format_receipt_line", "receipt_lines", "poll_once", "run_loop",
    "OUTCOME_ON_TIME", "OUTCOME_MISSED", "run_file_path",
    "record_delivery_outcome", "ledger_dir", "LEDGER_PRODUCER",
]
