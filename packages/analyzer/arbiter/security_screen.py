"""arbiter.security_screen — the folded security-review mandate.

This module is the retirement home of ``review.py`` (operator decision
2026-07-28, executed 2026-08-14): the standalone security reviewer was a
phantom gate — never scheduled, its ``proposals/reviewed/`` output never
promoted, and the live approve path bypassed it entirely. Its auto-reject
mandate (formerly ``security_rules.json``, which shipped alongside it) now
lives HERE, in code, where the two live gates actually consult it:

  1. ``arbiter.routing.is_autonomous_eligible`` — a proposal that trips any
     deny rule (or that cannot get AST coverage) is never eligible for
     autonomous apply; it routes to human review.
  2. The plugin approve route (``packages/plugin/src/api/networkRoutes.ts``,
     ``POST /evolve/api/proposals/:id/approve``) — invokes the CLI below and
     refuses to move a denied proposal into ``proposals/approved/``.

The mandate is deliberately code, not config: the old comment on
``security_rules.json`` ("edit only manually, never via proposal") is now
enforced by the repo itself — a proposal cannot rewrite a module that ships
read-only from the deploy checkout.

The legacy ``auto_flag`` rules were NOT folded: their semantics ("force human
attention") are the v2 arbiter's default for anything not autonomous-eligible,
so they are subsumed by routing rather than re-implemented.

Fail-closed contract:
  * A rule that ERRORS during evaluation produces a denial (the legacy
    reviewer swallowed rule errors as non-triggers — a fail-open hole).
  * The AST layer (``review_ast``, the type-spoofing/obfuscation catcher)
    failing to import marks the screen ``ast_available=False``. The autonomy
    gate treats that as ineligible (parity with review.py's force-flag);
    the human approve route does NOT hard-deny on it — a missing analyzer
    routes to human judgment, which is exactly what an approve click is.

CLI (consumed by the plugin approve route):

    python3 -m arbiter.security_screen --proposal <path>

prints ``{"result": "allow"|"deny", "denials": [...], "ast_available": bool}``
and exits 0 for any successful screen (deny included); non-zero exit means
the screen itself failed and callers must fail closed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "SecurityDenial",
    "ScreenResult",
    "screen_proposal",
    "AUTO_REJECT_RULES",
]


# ─────────────────────────────────────────────────────────────────────────────
# The deny mandate — verbatim from security_rules.json v2 ``auto_reject``
# (the file retired with review.py; git history has the original).
# ─────────────────────────────────────────────────────────────────────────────

AUTO_REJECT_RULES: list[dict] = [
    {
        "id": "no_network_exposure",
        "description": "Reject any config change that sets gateway.bind or similar to 0.0.0.0 or a non-localhost address",
        "applies_to": ["config_change"],
        "check": "path_contains",
        "pattern": "bind",
        "value_pattern": "0\\.0\\.0\\.0|^(?!127\\.0\\.0\\.1|localhost)",
    },
    {
        "id": "no_auth_disable",
        "description": "Reject any change that disables authentication",
        "applies_to": ["config_change"],
        "check": "path_matches",
        "pattern": "auth\\.enabled|security\\.enabled|requireAuth",
        "value": False,
    },
    {
        "id": "no_self_modification",
        "description": "Reject proposals that target evolve's own scripts or security rules",
        "applies_to": ["script_change"],
        "check": "target_file_contains",
        "pattern": "evolve/",
    },
    {
        "id": "no_credential_paths",
        "description": "Reject script changes targeting auth or credential files",
        "applies_to": ["script_change"],
        "check": "target_file_contains",
        "pattern": "auth-profiles|openclaw\\.json|\\.secrets|\\.env|credentials|api.key|token",
    },
    {
        "id": "no_sudo_in_scripts",
        "description": "Reject script changes that introduce sudo calls",
        "applies_to": ["script_change"],
        "check": "content_contains",
        "pattern": "\\bsudo\\b|subprocess.*sudo|os\\.setuid|os\\.setgid",
    },
    {
        "id": "no_network_calls_in_scripts",
        "description": "Reject script changes that add outbound network calls to non-local addresses",
        "applies_to": ["script_change"],
        "check": "content_contains",
        "pattern": "requests\\.get|urllib\\.request|curl|wget|http[s]?://(?!127\\.0\\.0\\.1|localhost)",
    },
    {
        "id": "no_cross_user_writes",
        "description": "Reject script changes targeting paths outside the bot's own workspace or the pod shared dir",
        "applies_to": ["script_change"],
        "check": "target_outside_allowed_roots",
    },
    {
        "id": "no_launchd_self_modification",
        "description": "Reject changes targeting launchd plists",
        "applies_to": ["script_change", "config_change"],
        "check": "target_file_contains",
        "pattern": "\\.plist|LaunchAgents|LaunchDaemons",
    },
    {
        "id": "no_executable_content_in_any_type",
        "description": "Reject any proposal type that carries exec/eval/os.system/shell injection regardless of declared type (closes type-spoofing gap)",
        "applies_to": ["all"],
        "check": "content_contains",
        "pattern": "\\beval\\s*\\(|\\bexec\\s*\\(|os\\.system\\s*\\(|os\\.popen\\s*\\(|subprocess\\.\\w+\\s*\\(.*shell\\s*=\\s*True|__import__\\s*\\(|importlib\\.import_module",
    },
    {
        "id": "no_network_clients_in_any_type",
        "description": "Reject network client calls in any proposal type that carries executable content (closes type-spoofing gap for httpx, socket, etc.)",
        "applies_to": ["all"],
        "check": "content_contains",
        "pattern": "\\bhttpx\\b|\\bsocket\\.socket\\b|\\bsocket\\.connect\\b|\\bsocket\\.create_connection\\b|\\baiohttp\\b",
    },
]

# Keys inside a v2 Action dict whose string values are "content" — code or
# code-like payloads the applier will materialize somewhere. Mirrors
# review_ast._CODE_KEYS (plus ``replacement``, MemoryCurate's payload).
_V2_CONTENT_KEYS = frozenset(
    {"content", "script", "code", "command", "commands", "value", "to", "replacement"}
)


@dataclass
class SecurityDenial:
    rule_id: str
    description: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"rule_id": self.rule_id, "description": self.description, "detail": self.detail}


@dataclass
class ScreenResult:
    """Outcome of one proposal screen.

    ``denials`` non-empty ⇒ the proposal trips the deny mandate and must not
    apply through any lane. ``ast_available=False`` ⇒ the AST layer could not
    be imported; the AUTONOMY lane must fail closed on this (human lanes may
    proceed — the human is the review).
    """

    denials: list[SecurityDenial] = field(default_factory=list)
    ast_available: bool = True

    @property
    def allowed(self) -> bool:
        return not self.denials

    def to_dict(self) -> dict:
        return {
            "result": "allow" if self.allowed else "deny",
            "denials": [d.to_dict() for d in self.denials],
            "ast_available": self.ast_available,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Static rule evaluation (ported from review.py::_evaluate_rule, with one
# deliberate change: a rule that raises now DENIES instead of passing)
# ─────────────────────────────────────────────────────────────────────────────


def _allowed_roots(bot_id: str) -> list[Path]:
    """Platform-correct allowed write roots: the bot's home + the shared dir.

    Replaces the retired JSON's macOS literals (``/Users/{bot_id}/``,
    ``/Users/Shared/evolve/``) so Linux pods get real roots instead of
    paths that never match.
    """
    from evolve_config import CANONICAL_SHARED_DIR, bot_home

    return [bot_home(bot_id), CANONICAL_SHARED_DIR]


def _check_target_outside_allowed_roots(target: str, bot_id: str) -> tuple[bool, str]:
    from evolve_config import bot_home

    if not target:
        return False, ""
    if not bot_id:
        # No bot to resolve roots for: bot_home("") degrades to the bare
        # user-home root, which would bless EVERY home directory. A write
        # target with no owning bot is malformed — fail closed.
        return True, "no bot_id on proposal — cannot resolve allowed roots (failing closed)"
    if not target.startswith("/"):
        workspace = bot_home(bot_id) / ".openclaw" / "workspace"
        target = str(workspace / target)
    # Resolve symlinks before checking allowed roots (prevents symlink escape)
    try:
        resolved = Path(target).resolve()
    except OSError:
        resolved = Path(target)
    triggered = not any(resolved.is_relative_to(root) for root in _allowed_roots(bot_id))
    return triggered, f"target={str(resolved)!r}"


def _evaluate_rule(rule: dict, change: dict, bot_id: str) -> SecurityDenial | None:
    """Return a denial if *rule* triggers on *change*; None otherwise.

    Raises nothing: an evaluation error returns a denial (fail closed) —
    the legacy reviewer's swallow-to-pass here was a fail-open hole.
    """
    rule_id = rule.get("id", "?")
    description = rule.get("description", "")
    check = rule.get("check", "")
    try:
        if check == "path_contains":
            path = change.get("path", "")
            if re.search(rule.get("pattern", ""), path, re.IGNORECASE):
                value_pattern = rule.get("value_pattern", "")
                if value_pattern:
                    to_val = str(change.get("to", ""))
                    if re.search(value_pattern, to_val, re.IGNORECASE):
                        return SecurityDenial(rule_id, description, f"path={path!r} value={to_val!r}")
                else:
                    return SecurityDenial(rule_id, description, f"path={path!r}")

        elif check == "path_matches":
            path = change.get("path", "")
            if re.search(rule.get("pattern", ""), path, re.IGNORECASE):
                if change.get("to") == rule.get("value"):
                    return SecurityDenial(rule_id, description, f"path={path!r} to={change.get('to')!r}")

        elif check == "target_file_contains":
            target = change.get("target_file", "")
            if re.search(rule.get("pattern", ""), target, re.IGNORECASE):
                return SecurityDenial(rule_id, description, f"target={target!r}")

        elif check == "content_contains":
            content = change.get("content", "")
            m = re.search(rule.get("pattern", ""), content)
            if m:
                return SecurityDenial(rule_id, description, f"found: {m.group(0)!r}")

        elif check == "target_outside_allowed_roots":
            triggered, detail = _check_target_outside_allowed_roots(
                change.get("target_file", ""), bot_id
            )
            if triggered:
                return SecurityDenial(rule_id, description, detail)

    except Exception as exc:  # noqa: BLE001 — any evaluation error denies
        return SecurityDenial(
            rule_id, description, f"rule evaluation error (failing closed): {exc}"
        )
    return None


def _applies_to(rule: dict, proposal_type: str) -> bool:
    applies = rule.get("applies_to", ["all"])
    return "all" in applies or proposal_type in applies


def _static_denials(proposal_type: str, change: dict, bot_id: str) -> list[SecurityDenial]:
    denials: list[SecurityDenial] = []
    for rule in AUTO_REJECT_RULES:
        if not _applies_to(rule, proposal_type):
            continue
        denial = _evaluate_rule(rule, change, bot_id)
        if denial:
            denials.append(denial)
    return denials


# ─────────────────────────────────────────────────────────────────────────────
# AST layer (review_ast) — lazy import, reject findings become denials
# ─────────────────────────────────────────────────────────────────────────────


def _ast_analyze(proposal_like: dict) -> tuple[list[SecurityDenial], bool]:
    """Run review_ast over *proposal_like*; (reject-denials, ast_available)."""
    try:
        from review_ast import analyze_proposal
    except ImportError:
        return [], False
    try:
        findings = analyze_proposal(proposal_like)
    except Exception as exc:  # noqa: BLE001 — analyzer crash denies (fail closed)
        return [
            SecurityDenial(
                "ast_analyzer_error",
                "AST analysis crashed — failing closed",
                str(exc),
            )
        ], True
    return [
        SecurityDenial(f"ast:{f.rule_id}", f.description, f.detail)
        for f in findings
        if f.action == "reject"
    ], True


# ─────────────────────────────────────────────────────────────────────────────
# Shape adapters — legacy proposals and v2 arbiter proposals
# ─────────────────────────────────────────────────────────────────────────────


def _walk_v2_content(node: Any, out: list[tuple[str, str]], key: str = "") -> None:
    """Collect (key, text) content surfaces from a v2 action dict, recursively."""
    if isinstance(node, dict):
        for k, v in node.items():
            _walk_v2_content(v, out, k)
    elif isinstance(node, list):
        if key in _V2_CONTENT_KEYS:
            joined = "\n".join(str(v) for v in node if isinstance(v, str))
            if joined.strip():
                out.append((key, joined))
        else:
            for v in node:
                _walk_v2_content(v, out, key)
    elif isinstance(node, str) and key in _V2_CONTENT_KEYS and node.strip():
        out.append((key, node))


def _screen_v2(action: dict, bot_id: str) -> ScreenResult:
    denials: list[SecurityDenial] = []

    # ConfigPatch maps onto the legacy config_change rule surface: the
    # patched key path is ``path`` and the written value is ``to``.
    if action.get("kind") == "ConfigPatch":
        change = {
            "path": str(action.get("target_path", "")),
            "to": action.get("value"),
            "content": json.dumps(action.get("value"))
            if isinstance(action.get("value"), (dict, list))
            else str(action.get("value", "")),
        }
        denials.extend(_static_denials("config_change", change, bot_id))

    # Type-agnostic rules run over every content surface of the action —
    # the v2 analog of the legacy "all"-scoped type-spoofing rules.
    surfaces: list[tuple[str, str]] = []
    _walk_v2_content(action, surfaces)
    for _key, text in surfaces:
        denials.extend(_static_denials("", {"content": text}, bot_id))

    ast_available = True
    if surfaces:
        for _key, text in surfaces:
            ast_denials, ast_available = _ast_analyze({"proposed_change": {"content": text}})
            denials.extend(ast_denials)
            if not ast_available:
                break
    else:
        # No content to analyze — AST availability still matters for the
        # autonomy gate's fail-closed posture, so probe the import.
        _, ast_available = _ast_analyze({})

    return ScreenResult(denials=_dedup(denials), ast_available=ast_available)


def _dedup(denials: list[SecurityDenial]) -> list[SecurityDenial]:
    """Drop exact repeats — a ConfigPatch value is both a mapped legacy
    ``to`` and a walked content surface, so a rule can fire twice."""
    seen: set[tuple[str, str]] = set()
    out: list[SecurityDenial] = []
    for d in denials:
        k = (d.rule_id, d.detail)
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


def _screen_legacy(proposal: dict) -> ScreenResult:
    proposal_type = proposal.get("type", "")
    change = proposal.get("proposed_change", {}) or {}
    bot_id = proposal.get("target_bot", "")
    denials = _static_denials(proposal_type, change, bot_id)
    ast_denials, ast_available = _ast_analyze(proposal)
    denials.extend(ast_denials)
    return ScreenResult(denials=denials, ast_available=ast_available)


def screen_proposal(proposal: dict) -> ScreenResult:
    """Screen a proposal dict against the deny mandate — BOTH shapes, always.

    v2 arbiter proposals carry a dict ``action`` with a ``kind`` tag; legacy
    proposals carry ``type`` + ``proposed_change``. The legacy pass runs
    unconditionally (it is inert on a pure-v2 dict) so a proposal smuggling a
    dangerous ``proposed_change`` UNDER a clean v2 ``action`` — legacy
    apply.py acts on ``proposed_change``, not ``action`` — cannot pick which
    screen it gets.
    """
    denials: list[SecurityDenial] = []
    ast_available = True

    action = proposal.get("action")
    if isinstance(action, dict) and "kind" in action:
        bot_id = proposal.get("bot_id") or proposal.get("target_bot") or ""
        v2 = _screen_v2(action, bot_id)
        denials.extend(v2.denials)
        ast_available = ast_available and v2.ast_available

    legacy = _screen_legacy(proposal)
    denials.extend(legacy.denials)
    ast_available = ast_available and legacy.ast_available

    return ScreenResult(denials=_dedup(denials), ast_available=ast_available)


# ─────────────────────────────────────────────────────────────────────────────
# CLI — consumed by the plugin approve route
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Evolve proposal security screen")
    parser.add_argument("--proposal", required=True, help="Path to the proposal JSON")
    args = parser.parse_args(argv)

    try:
        proposal = json.loads(Path(args.proposal).read_text())
    except Exception as exc:  # noqa: BLE001 — unreadable proposal = screen failure
        print(f"security_screen: cannot read proposal: {exc}", file=sys.stderr)
        return 2

    result = screen_proposal(proposal)
    print(json.dumps(result.to_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
