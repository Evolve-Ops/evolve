"""generators.workspace_security.signal_proposals — Signal → Proposal factory.

``make_misplaced_secret_proposal`` takes one ``misplaced_secret`` rollup
Signal and emits one Investigation Proposal per item in
``details.items[]``. The signal's details carry the relative path and a
short label naming the credential type per item — the proposal repeats
both and points at the rotation playbook. Severity maps to
``urgency=security_critical`` so the proposal sorts to the top of the
queue.
"""

from __future__ import annotations

from typing import Any, Iterator

from schema.proposal import (
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)


GENERATOR_ID = "workspace_security"
DIMENSION = "safety"


def dismiss_signature_for_path(path: str) -> str:
    return f"workspace_security:misplaced_secret:{path}"


def _signal_dict_get(signal: Any, key: str, default: Any = None) -> Any:
    if isinstance(signal, dict):
        return signal.get(key, default)
    return getattr(signal, key, default)


def _iter_signal_items(signal: Any) -> Iterator[dict[str, Any]]:
    """Yield per-item dicts from a rollup Signal, or one synthetic item
    from a legacy per-item Signal (see manifest_quality for shape doc)."""
    details = _signal_dict_get(signal, "details") or {}
    items = details.get("items") if isinstance(details, dict) else None
    if isinstance(items, list) and items:
        for item in items:
            if isinstance(item, dict):
                yield item
        return
    synthetic: dict[str, Any] = {}
    if isinstance(details, dict):
        for k in ("app_id", "path", "cron", "message"):
            v = details.get(k)
            if v is not None:
                synthetic[k] = v
    yield synthetic


def make_misplaced_secret_proposal(signal: Any) -> list[Proposal]:
    """`misplaced_secret` Signal → list of Investigation Proposals — one per item."""
    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    sig_id = _signal_dict_get(signal, "id") or ""
    out: list[Proposal] = []
    for item in _iter_signal_items(signal):
        path = item.get("path") or "<unknown>"
        message = item.get("message") or ""
        out.append(_build_misplaced_secret_proposal(bot_id, sig_id, path, message))
    return out


def _build_misplaced_secret_proposal(
    bot_id: str, sig_id: str, path: str, message: str
) -> Proposal:
    problem = f"{bot_id}: credential found in workspace file — {path}"
    headline = f"Check the credential found in {bot_id}'s {path}"
    summary = (
        f"The scanner found a credential-shaped string in {bot_id}'s "
        f"workspace at `{path}` ({message}). Don't dismiss without "
        f"inspecting — if it's a real token, rotate it now; if it's "
        f"a sample or test fixture, prefix it with EXAMPLE_ or move "
        f"it to a `.env.example` file (those are excluded from "
        f"scanning)."
    )
    explanation = (
        f"Credentials in workspace prose are one of the highest-"
        f"impact mistakes a bot can make. The scanner doesn't know "
        f"whether a credential-shaped string is a real token or a "
        f"sample — both look the same. So this finding is a "
        f"prompt to look, not a verdict.\n\n"
        f"Diagnosis. A token matching credential patterns appears "
        f"in `{path}`. Source: {message}. The scanner can't read "
        f"intent, so we surface it for the operator to check.\n\n"
        f"Three resolutions, in priority order. (1) **Real leak**: "
        f"rotate the token at the provider immediately, redact the "
        f"file, audit recent activity for misuse. (2) **Sample / "
        f"placeholder**: prefix with `EXAMPLE_` or move into a "
        f"`.env.example` file. (3) **Test fixture**: same fix — "
        f"prefix or move.\n\n"
        f"What could go wrong. Dismissing this without inspecting "
        f"is the case the engine can't protect you from. Real "
        f"credentials in workspace prose stay there until you act. "
        f"Credentials belong in `auth-profiles.json` or "
        f"`openclaw.json` (both excluded from this scan); never in "
        f"workspace prose, source files, or notes."
    )
    context = (
        f"The compliance scanner found a credential-shaped string in "
        f"{bot_id}'s workspace at `{path}`. Detected: {message}\n\n"
        f"**Don't dismiss this without inspecting.** Common cases:\n"
        f"- **Real leak** — a credential was pasted into a workspace file "
        f"by the agent during a previous session. Treat as a leaked "
        f"credential: rotate the underlying token at the provider, redact "
        f"the file, and audit recent activity for misuse.\n"
        f"- **Documented sample** — a placeholder / example credential in "
        f"a README. The scanner can't tell real from sample. If sample, "
        f"either prefix it with `EXAMPLE_` or move it into a `.env.example` "
        f"file (those are excluded from scanning).\n"
        f"- **Test fixture** — a known-fake token used by a script's tests. "
        f"Same fix: prefix or move.\n\n"
        f"Credentials belong in `auth-profiles.json` or `openclaw.json` "
        f"(both excluded from this scan); never in workspace prose, source "
        f"files, or notes. The Applications tab → Compliance subtab "
        f"surfaces the full list with the line each one was found on."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"misplaced_secret:{bot_id}:{path}"],
        provenance=Provenance(
            technique="workspace_security.misplaced_secret",
            signals={"path": path, "message": message},
            confidence=0.95,
        ),
        problem=problem,
        action=Investigation(context=context),
        # Touches the bot's workspace once the operator acts on it
        # (manual redaction); the proposal itself is read-only.
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="security_critical",
        admin_surface_summary=headline[:120],
        motivating_signals=[sig_id] if sig_id else [],
        # ── Phase C-8 operator-first content (Tier 2 — UI manual) ───────
        summary=summary,
        explanation=explanation,
        action_label="Open Compliance subtab",
        manual_path=f"Applications → {bot_id} → Compliance",
        dismiss_signature=dismiss_signature_for_path(path),
        dismiss_scope="kind",
    )
