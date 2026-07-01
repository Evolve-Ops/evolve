#!/usr/bin/env python3
"""
review.py — Security review gate for Evolve proposals.

Runs between analyze.py and human approval. Filters junk automatically
so humans only review proposals that passed security screening.

Can run on:
  - The primary bot (mode: "primary" in network.json security config)
  - A dedicated security bot (mode: "dedicated", botId: "security_bot")

The security reviewer's mandate is defined in security_rules.json — a
static file that can NEVER be modified by a proposal. This is intentional:
even if a worker bot gets a bad proposal applied, it cannot rewrite the
rules that govern proposal review.

Usage:
  python3 review.py                         # review all pending proposals
  python3 review.py --proposal-id abc123    # review one specific proposal
  python3 review.py --dry-run               # show what would happen

Scheduled: launchd StartInterval=120 (every 2 minutes), on security bot
or primary bot depending on network config.

Flow:
  proposals/pending/{id}.json
    → review.py evaluates
    → PASS: proposals/reviewed/{id}.json (with security_review field added)
    → FAIL: proposals/rejected/{id}.json (with rejection reason)
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from evolve_config import load_config, get_shared_dir, get_alerts, resolve_network_path, sign_review_stamp, verify_proposal_sig, bot_home as _bot_home, get_bot_user
# mode=0o644 at the _atomic_write call sites preserves the pre-evolve_util
# behavior (Path.write_text → umask-derived perms); the canonical mkstemp
# default would be 0o600.
from evolve_util import atomic_write_json as _atomic_write, now_iso
from proposal_routing import proposal_surfaces_in_pending_inbox

# AST-level analysis — imported lazily so a missing file degrades gracefully
try:
    from review_ast import analyze_proposal as _ast_analyze_proposal
    _AST_AVAILABLE = True
except ImportError:
    _ast_analyze_proposal = None  # type: ignore[assignment]
    _AST_AVAILABLE = False

REVIEWER_VERSION = "0.1.0"
PROCESSED_REGISTRY_PREFIX = "review_processed"


# ── Better Engine integration ─────────────────────────────────────────────────

def _flag_urgent_refresh(shared_dir: Path, source: str, reason: str, **kwargs) -> None:
    """Write the Better Engine urgent-refresh flag file (§2.2)."""
    try:
        flag = shared_dir / "better-engine" / ".refresh-urgent"
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(json.dumps({"source": source, "reason": reason,
                                    "ts": datetime.now(timezone.utc).isoformat(),
                                    **kwargs}))
    except Exception:
        pass  # never let this fail the main operation


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class RuleResult:
    rule_id: str
    triggered: bool
    description: str
    action: str  # "reject" | "flag" | "pass"
    detail: str = ""


@dataclass
class SecurityReview:
    proposal_id: str
    reviewed_at: str
    reviewer_version: str
    result: str  # "approved" | "rejected" | "flagged"
    risk_score: int
    risk_level: str  # "low" | "medium" | "high" | "critical"
    triggered_rules: list[RuleResult] = field(default_factory=list)
    rejection_reason: str = ""
    flags: list[str] = field(default_factory=list)
    reviewer_mode: str = "primary"  # "primary" | "dedicated"

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "reviewed_at": self.reviewed_at,
            "reviewer_version": self.reviewer_version,
            "result": self.result,
            "risk_score": self.risk_score,
            "risk_level": self.risk_level,
            "rejection_reason": self.rejection_reason,
            "flags": self.flags,
            "triggered_rules": [
                {"rule_id": r.rule_id, "action": r.action, "detail": r.detail}
                for r in self.triggered_rules if r.triggered
            ],
            "reviewer_mode": self.reviewer_mode,
        }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve proposal security reviewer")
    parser.add_argument("--network", default=str(resolve_network_path()))
    parser.add_argument("--proposal-id", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reviewer-mode", default=None, help="Override: primary or dedicated")
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    security_cfg = config.get("security", {})
    reviewer_mode = args.reviewer_mode or security_cfg.get("mode", "primary")

    # Load security rules from the canonical location — never from a proposal
    rules_file = Path(security_cfg.get("rulesFile", str(shared_dir / "security_rules.json")))
    if not rules_file.exists():
        # Fall back to rules bundled with the script
        rules_file = Path(__file__).parent / "security_rules.json"
    if not rules_file.exists():
        print("[evolve/review] ERROR: security_rules.json not found — cannot run without rules", file=sys.stderr)
        sys.exit(1)

    rules = json.loads(rules_file.read_text())
    auto_reject_rules = rules.get("auto_reject", [])
    auto_flag_rules = rules.get("auto_flag", [])
    risk_scoring = rules.get("risk_scoring", {})
    auto_reject_risk_levels = security_cfg.get("autoRejectRisk", ["high", "critical"])

    pending_dir = shared_dir / "proposals" / "pending"
    reviewed_dir = shared_dir / "proposals" / "reviewed"
    rejected_dir = shared_dir / "proposals" / "rejected"

    quarantine_dir = shared_dir / "proposals" / "quarantine"
    for d in (reviewed_dir, rejected_dir, quarantine_dir):
        d.mkdir(parents=True, exist_ok=True)

    if not pending_dir.exists():
        print(f"[evolve/review] No pending/ directory — nothing to review.")
        return

    # Registry of already-processed proposal IDs — hold exclusive lock for the
    # entire cycle to prevent two concurrent review.py instances from processing
    # the same proposal twice (e.g. manual trigger + scheduled run).
    registry_path = shared_dir / f"{PROCESSED_REGISTRY_PREFIX}.json"
    lock_path = registry_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(lock_path, "w")
    fcntl.flock(lock_fh, fcntl.LOCK_EX)
    processed: set[str] = set(json.loads(registry_path.read_text()) if registry_path.exists() else [])

    if args.proposal_id:
        targets = [pending_dir / f"{args.proposal_id}.json"]
    else:
        targets = sorted(pending_dir.glob("*.json"))

    new_processed = []
    for proposal_path in targets:
        proposal_id = proposal_path.stem
        if proposal_id in processed:
            continue

        proposal = _load_json(proposal_path)
        if not proposal:
            print(f"[evolve/review] Cannot parse {proposal_id} — skipping")
            continue

        print(f"[evolve/review] Reviewing: {proposal_id} ({proposal.get('type','?')})")

        # Verify HMAC signature — reject unsigned/tampered proposals before evaluation
        if not verify_proposal_sig(proposal):
            print(f"[evolve/review] WARN: {proposal_id} has invalid evolve_sig — quarantining")
            dst = quarantine_dir / f"{proposal_id}.json"
            proposal["_quarantine_reason"] = "invalid_evolve_sig"
            _atomic_write(dst, proposal, mode=0o644)
            proposal_path.unlink(missing_ok=True)
            new_processed.append(proposal_id)
            _alert_quarantine(proposal_id, "invalid_evolve_sig", shared_dir, config)
            # Flag urgent Better Engine refresh — tampered proposal quarantined
            _flag_urgent_refresh(shared_dir, source="review",
                                 reason="proposal_quarantined", proposal_id=proposal_id)
            continue

        try:
            review = evaluate_proposal(
                proposal, auto_reject_rules, auto_flag_rules,
                risk_scoring, reviewer_mode,
            )
        except Exception as e:
            print(f"[evolve/review] ERROR evaluating {proposal_id}: {e}", file=sys.stderr)
            traceback.print_exc()
            continue

        # Auto-reject on high/critical risk if configured
        if (review.risk_level in auto_reject_risk_levels
                and review.result != "rejected"
                and not args.dry_run):
            review.result = "rejected"
            review.rejection_reason = (
                f"Auto-rejected: risk level '{review.risk_level}' "
                f"(score {review.risk_score}) exceeds configured threshold."
            )

        if args.dry_run:
            print(f"  [dry-run] result={review.result} risk={review.risk_level}({review.risk_score})")
            if review.flags:
                print(f"  flags: {review.flags}")
            if review.rejection_reason:
                print(f"  rejection: {review.rejection_reason}")
            continue

        # Write review into the proposal
        proposal["security_review"] = review.to_dict()

        if review.result == "rejected":
            dst = rejected_dir / f"{proposal_id}.json"
            _atomic_write(dst, proposal, mode=0o644)
            proposal_path.unlink(missing_ok=True)
            print(f"  ✗ REJECTED: {review.rejection_reason[:80]}")
            _alert_rejection(proposal, review, proposal_id, shared_dir, config)
        else:
            # approved or flagged — add review stamp then move to reviewed/ for human
            stamp = {
                "proposal_id": proposal_id,
                "reviewed_at": review.to_dict()["reviewed_at"],
                "result": review.result,
            }
            sig = sign_review_stamp(stamp)
            if sig:
                stamp["sig"] = sig
            proposal["review_stamp"] = stamp
            dst = reviewed_dir / f"{proposal_id}.json"
            _atomic_write(dst, proposal, mode=0o644)
            proposal_path.unlink(missing_ok=True)
            label = "⚑ FLAGGED" if review.result == "flagged" else "✓ APPROVED for human review"
            print(f"  {label}: risk={review.risk_level}({review.risk_score})")
            _alert_reviewed(proposal, review, proposal_id, shared_dir, config)

        new_processed.append(proposal_id)

    try:
        processed.update(new_processed)
        _atomic_write(registry_path, sorted(processed), mode=0o644)
        print(f"[evolve/review] Done — {len(new_processed)} proposal(s) processed.")
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


# ── Evaluation engine ─────────────────────────────────────────────────────────

def evaluate_proposal(
    proposal: dict,
    auto_reject_rules: list[dict],
    auto_flag_rules: list[dict],
    risk_scoring: dict,
    reviewer_mode: str,
) -> SecurityReview:
    proposal_type = proposal.get("type", "")
    proposal_id = proposal.get("id", "?")
    change = proposal.get("proposed_change", {})
    bot_id = proposal.get("target_bot", "")
    confidence = proposal.get("confidence", 1.0)

    review = SecurityReview(
        proposal_id=proposal_id,
        reviewed_at=now_iso(),
        reviewer_version=REVIEWER_VERSION,
        result="approved",
        risk_score=risk_scoring.get(proposal_type, 10),
        risk_level="low",
        reviewer_mode=reviewer_mode,
    )

    # ── Auto-reject checks ────────────────────────────────────────────────────
    for rule in auto_reject_rules:
        if not _applies_to(rule, proposal_type):
            continue
        result = _evaluate_rule(rule, proposal, change, bot_id)
        if result.triggered:
            review.triggered_rules.append(result)
            review.result = "rejected"
            review.rejection_reason = f"Rule '{rule['id']}': {rule['description']}"
            # Return immediately on first rejection — no need to evaluate further
            review.risk_score = risk_scoring.get("critical_risk_threshold", 90)
            review.risk_level = "critical"
            return review

    # ── Auto-flag checks ──────────────────────────────────────────────────────
    for rule in auto_flag_rules:
        if not _applies_to(rule, proposal_type):
            continue
        result = _evaluate_rule(rule, proposal, change, bot_id)
        if result.triggered:
            review.triggered_rules.append(result)
            review.flags.append(f"{rule['id']}: {rule['description']}")
            review.risk_score += risk_scoring.get("flag_per_auto_flag", 15)
            if review.result != "flagged":
                review.result = "flagged"

    # ── AST analysis — type-agnostic, runs on any code-bearing proposal ───────
    if _AST_AVAILABLE and _ast_analyze_proposal is not None:
        try:
            ast_findings = _ast_analyze_proposal(proposal)
        except Exception as ast_err:
            # Never let AST analysis crash the review — flag and move on
            ast_findings = []
            review.flags.append(f"ast_analyzer_error: {ast_err}")
            if review.result not in ("rejected",):
                review.result = "flagged"

        for finding in ast_findings:
            # Convert ASTFinding to the RuleResult shape
            rule_result = RuleResult(
                rule_id=finding.rule_id,
                triggered=True,
                description=finding.description,
                action=finding.action,
                detail=finding.detail,
            )
            review.triggered_rules.append(rule_result)

            if finding.action == "reject":
                review.result = "rejected"
                review.rejection_reason = (
                    f"AST rule '{finding.rule_id}': {finding.description}"
                )
                review.risk_score = risk_scoring.get("critical_risk_threshold", 90)
                review.risk_level = "critical"
                # Return immediately — first AST rejection is final
                return review
            else:
                # flag
                review.flags.append(f"{finding.rule_id}: {finding.description}")
                review.risk_score += risk_scoring.get("flag_per_auto_flag", 15)
                if review.result not in ("rejected",):
                    review.result = "flagged"

    # ── Risk level from final score ───────────────────────────────────────────
    critical = risk_scoring.get("critical_risk_threshold", 90)
    high = risk_scoring.get("high_risk_threshold", 70)
    if review.risk_score >= critical:
        review.risk_level = "critical"
    elif review.risk_score >= high:
        review.risk_level = "high"
    elif review.risk_score >= 40:
        review.risk_level = "medium"
    else:
        review.risk_level = "low"

    return review


def _applies_to(rule: dict, proposal_type: str) -> bool:
    applies = rule.get("applies_to", ["all"])
    return "all" in applies or proposal_type in applies


def _evaluate_rule(rule: dict, proposal: dict, change: dict, bot_id: str) -> RuleResult:
    check = rule.get("check", "")
    rule_id = rule.get("id", "?")
    description = rule.get("description", "")

    result = RuleResult(rule_id=rule_id, triggered=False, description=description, action=rule.get("action", "reject"))

    try:
        if check == "path_contains":
            path = change.get("path", "")
            pattern = rule.get("pattern", "")
            value_pattern = rule.get("value_pattern", "")
            if re.search(pattern, path, re.IGNORECASE):
                if value_pattern:
                    to_val = str(change.get("to", ""))
                    result.triggered = bool(re.search(value_pattern, to_val, re.IGNORECASE))
                    result.detail = f"path={path!r} value={to_val!r}"
                else:
                    result.triggered = True
                    result.detail = f"path={path!r}"

        elif check == "path_matches":
            path = change.get("path", "")
            pattern = rule.get("pattern", "")
            expected_value = rule.get("value")
            if re.search(pattern, path, re.IGNORECASE):
                to_val = change.get("to")
                result.triggered = (to_val == expected_value)
                result.detail = f"path={path!r} to={to_val!r}"

        elif check == "target_file_contains":
            target = change.get("target_file", "")
            pattern = rule.get("pattern", "")
            result.triggered = bool(re.search(pattern, target, re.IGNORECASE))
            result.detail = f"target={target!r}"

        elif check == "content_contains":
            content = change.get("content", "")
            pattern = rule.get("pattern", "")
            result.triggered = bool(re.search(pattern, content))
            if result.triggered:
                # Find the offending snippet
                m = re.search(pattern, content)
                result.detail = f"found: {m.group(0)!r}" if m else ""

        elif check == "target_outside_allowed_roots":
            target = change.get("target_file", "")
            if target:
                # Resolve relative paths against the bot's workspace so they get
                # the same allowed-root check as absolute paths. Previously relative
                # paths were skipped entirely, creating a gap in the security gate.
                bot_user = get_bot_user(bot_id)
                if not target.startswith("/"):
                    workspace = str(_bot_home(bot_id) / ".openclaw" / "workspace")
                    target = str(Path(workspace) / target)
                # Resolve symlinks before checking allowed roots (prevents symlink escape)
                try:
                    resolved_target = str(Path(target).resolve())
                except OSError:
                    resolved_target = target
                allowed = [p.replace("{bot_id}", bot_user)
                           for p in rule.get("allowed_root_patterns", [])]
                result.triggered = not any(
                    Path(resolved_target).is_relative_to(Path(r)) for r in allowed
                )
                result.detail = f"target={resolved_target!r}"

        elif check == "confidence_below":
            threshold = rule.get("threshold", 0.70)
            confidence = proposal.get("confidence", 1.0)
            result.triggered = confidence < threshold
            result.detail = f"confidence={confidence:.2f} < {threshold}"

    except Exception as e:
        result.detail = f"Rule evaluation error: {e}"

    return result


# ── Alerts ────────────────────────────────────────────────────────────────────


def _dispatch_review(
    *,
    shared_dir: Path,
    config: dict,
    catalog_event: str,
    dedup_key: str,
    severity_name: str = "info",
    payload: dict | None = None,
    message: str | None = None,
) -> None:
    """Route a review-side alert through the alerts dispatcher.

    Phase F: pass ``payload`` when the catalog body_template fits the
    event shape; fall back to ``message`` (legacy) for events that
    don't fit a single template yet. Operator enable/cooldown via
    ``alerts.review.{enabled, cooldown_seconds}`` plus per-event
    subscription preferences (decisions.proposal_*,
    security.proposal_quarantined).
    """
    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity,
        )
    except Exception as exc:
        print(f"[evolve/review] dispatcher import failed; alert dropped: {exc}",
              file=sys.stderr)
        return

    severity_map = {
        "critical": Severity.CRITICAL,
        "error": Severity.ERROR,
        "warning": Severity.WARNING,
        "info": Severity.INFO,
    }
    try:
        _dispatch_send(
            shared_dir=shared_dir,
            network=config,
            source="review",
            message=message,
            payload=payload,
            severity=severity_map.get(severity_name, Severity.INFO),
            dedup_key=dedup_key,
            catalog_event=catalog_event,
        )
    except Exception as exc:
        print(f"[evolve/review] dispatcher.send raised; alert dropped: {exc}",
              file=sys.stderr)


def _alert_rejection(
    proposal: dict, review: SecurityReview, proposal_id: str,
    shared_dir: Path, config: dict,
) -> None:
    """Phase F5: payload-driven. Catalog body_template is
    "📋 Proposal rejected / {proposal_id} / Reason: {reason}"
    plus ui_action → Proposals → Rejected."""
    _dispatch_review(
        shared_dir=shared_dir, config=config,
        catalog_event="decisions.proposal_rejected",
        dedup_key=f"review/rejected/{proposal_id}",
        severity_name="info",
        payload={
            "proposal_id": proposal_id,
            "reason": review.rejection_reason[:150],
        },
    )


def _alert_reviewed(
    proposal: dict, review: SecurityReview, proposal_id: str,
    shared_dir: Path, config: dict,
) -> None:
    """Phase F5: payload-driven. Maps to ``decisions.proposal_ready``
    (same operator-facing event as analyze.py's initial emission —
    both reach the same subscription). The catalog body_template
    "📋 {count} new proposal{s} ready / {titles}" gracefully handles
    count=1; risk/flags from the security review go into the titles
    line so the operator can see them in-line."""
    # Surface-honest gate (same predicate as analyze.send_telegram_alert):
    # this event's breadcrumb is "Recommendations → Inbox", so don't fire for
    # a proposal that won't land in the actionable Inbox (surface !=
    # improvement, or informational). It would route to Alerts/Observations
    # and the push would point the operator at an empty inbox. The other
    # review alerts (_alert_rejection / _alert_quarantine) are different
    # events and are intentionally left ungated.
    if not proposal_surfaces_in_pending_inbox(proposal):
        return
    if review.result == "flagged":
        status = f"⚑ flagged · risk {review.risk_level}/{review.risk_score}"
    else:
        status = f"✅ cleared · risk {review.risk_level}/{review.risk_score}"

    title = (
        f"[{proposal.get('target_bot','?')}] "
        f"{proposal.get('problem','')[:80]} · {status}"
    )
    if review.flags:
        title += f"\n  flags: {'; '.join(review.flags)[:200]}"

    _dispatch_review(
        shared_dir=shared_dir, config=config,
        catalog_event="decisions.proposal_ready",
        dedup_key=f"review/reviewed/{proposal_id}",
        severity_name="info",
        payload={"count": 1, "s": "", "titles": title},
    )


def _alert_quarantine(
    proposal_id: str, reason: str, shared_dir: Path, config: dict,
) -> None:
    """Send alert for a quarantined (unsigned/tampered) proposal.

    Phase F: passes payload; catalog body_template renders. The Team_bot_a-
    style cleanup intentionally drops the "Possible Tampering /
    unauthorized injection" prose — the operator who subscribed to
    security.proposal_quarantined already knows that's what this
    event means; the catalog's `is_safety_critical=True` flag and
    the UI's confirmation modal before muting carry the urgency.
    """
    _dispatch_review(
        shared_dir=shared_dir, config=config,
        catalog_event="security.proposal_quarantined",
        dedup_key=f"review/quarantine/{proposal_id}",
        severity_name="critical",
        payload={"proposal_id": proposal_id, "reason": reason},
    )


# ── Util ──────────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


if __name__ == "__main__":
    main()
