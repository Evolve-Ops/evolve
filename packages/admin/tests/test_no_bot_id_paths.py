"""Fail CI if any source file constructs /Users/{bot_id}/ directly.

bot_id (logical name in network.json) may differ from the macOS account name.
Live example: the bot ``team-bot-b`` runs on the ``personal-bot-user`` account. All path
construction must go through:
  - evolve_admin.config.bot_home(bot_id) / get_bot_user(bot_id, network)
  - evolve_config.bot_home(bot_id) / get_bot_user(bot_id)   (analyzer package)

Direct f"/Users/{bot_id}/" construction silently works for every bot whose
account name happens to match and silently breaks for the ones it doesn't —
the failure is a *miss*, not an error, so callers with a permissive
except-branch (``return True`` / ``return {}``) degrade into wrong answers
with no log line. That is exactly how an operator's explicit
``userTierOverride.enabled: false`` on team-bot-b went ignored.

Detection is AST-based, not line-regex, for two reasons the previous
line-regex version got wrong: comments and docstrings are invisible to it
(prose that *names* the anti-pattern — including this docstring — must not
trip the gate), and it sees attribute forms such as ``{job.bot_id}`` that a
``{bot_id}``-only regex missed entirely.

Grandfathered sites are held in BASELINE as a per-file COUNT keyed on the
repo-relative path — not a per-basename allowlist, which let a grandfathered
``manifest.py`` silently cover any other file of that name.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Source trees to lint
SRC_ROOTS = [
    REPO_ROOT / "packages" / "admin" / "evolve_admin",
    REPO_ROOT / "packages" / "analyzer",
    REPO_ROOT / "tools",
    REPO_ROOT / "docs" / "reference" / "capabilities" / "ea-pack" / "scripts",
]

# Interpolated names that denote a LOGICAL bot id (never an OS account name).
# ``user`` / ``bot_user`` / ``account`` are deliberately absent: those already
# hold a resolved account name, which is the correct thing to join onto /Users/.
BOT_ID_NAMES = {"bot_id", "bot", "bot_name", "botid"}

# Per-file count of grandfathered ``/Users/{bot_id}`` constructions, keyed on
# the REPO-RELATIVE path. This gate BLOCKS any file that exceeds its count, so
# the map may only ratchet DOWN: convert a site onto bot_home() and drop the
# number. Goal state: empty.
#
# Two kinds of entry live here, and they are not equally acceptable:
#   • Legitimate — account-CREATION sites (bot_id genuinely IS the new account
#     name) and last-resort fallbacks whose primary path already goes through
#     bot_home(). These can stay.
#   • Debt — operator-facing message strings that embed /Users/{bot_id} as
#     advice. They do not crash, but they print a path that does not exist for
#     any bot whose account name differs. These should be migrated.
BASELINE: dict[str, int] = {
    "packages/admin/evolve_admin/applications/manifest.py": 1,
    "packages/admin/evolve_admin/evo/handlers/connect.py": 1,
    "packages/admin/evolve_admin/handover.py": 1,
    "packages/admin/evolve_admin/lifecycle/inventory.py": 1,
    "packages/admin/evolve_admin/retire.py": 3,
    "packages/admin/evolve_admin/web/server.py": 1,
    "packages/analyzer/arbiter/appliers/retire_orphan.py": 1,
    "packages/analyzer/cascade/audit_runner.py": 1,
    "packages/analyzer/digest_source_audit.py": 2,
    "packages/analyzer/embedding_monitor.py": 2,
    "packages/analyzer/exec_outcome_watchdog.py": 2,
    "packages/analyzer/generators/efficiency_hawk/signal_proposals.py": 5,
    "packages/analyzer/generators/exec_outcome_investigator/observe.py": 4,
    "packages/analyzer/generators/sysadmin_watchdog/proposals.py": 1,
    "packages/analyzer/generators/sysadmin_watchdog/signals.py": 2,
    "packages/analyzer/permissions/intent_inference.py": 1,
    "packages/analyzer/session_economics.py": 2,
    "packages/analyzer/turn_detail.py": 2,
    "tools/defer-eval/run_eval.py": 1,
}


def _expr_name(node: ast.AST) -> str | None:
    """Name of a simple interpolated expression (``bot_id``, ``job.bot_id``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """ids of Constant/JoinedStr nodes that ARE docstrings (module/class/func)."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None) or []
        if body and isinstance(body[0], ast.Expr):
            out.add(id(body[0].value))
    return out


def _snippet(parts: list, i: int, name: str) -> str:
    """Reconstruct the offending ``/Users/{name}...`` fragment for reporting.

    Implicit string concatenation fuses adjacent literals into ONE JoinedStr,
    and CPython does not preserve a usable per-chunk lineno, so the source
    line is not a reliable locator. The fragment itself is.
    """
    tail = parts[i].value[-8:] if isinstance(parts[i], ast.Constant) else "/Users/"
    nxt = parts[i + 2] if i + 2 < len(parts) else None
    rest = ""
    if isinstance(nxt, ast.Constant) and isinstance(nxt.value, str):
        rest = nxt.value[:40]
    return f"{tail}{{{name}}}{rest}"


def _violations(path: Path) -> list[str]:
    """Offending ``/Users/{bot_id}...`` fragments outside docstrings."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return []
    skip = _docstring_node_ids(tree)
    out: list[str] = []

    for node in ast.walk(tree):
        # f"/Users/{bot_id}..." — a "/Users/"-terminated literal chunk
        # immediately followed by an interpolated bot id.
        if isinstance(node, ast.JoinedStr) and id(node) not in skip:
            vals = node.values
            for i, part in enumerate(vals[:-1]):
                if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                    continue
                if not part.value.endswith("/Users/"):
                    continue
                nxt = vals[i + 1]
                if not isinstance(nxt, ast.FormattedValue):
                    continue
                name = _expr_name(nxt.value)
                if name and name in BOT_ID_NAMES:
                    out.append(_snippet(vals, i, name))
        # "/Users/" + bot_id
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = node.left, node.right
            if (
                isinstance(left, ast.Constant)
                and isinstance(left.value, str)
                and left.value.endswith("/Users/")
            ):
                name = _expr_name(right)
                if name and name in BOT_ID_NAMES:
                    out.append(f'"/Users/" + {name}')
    return out


def _scan() -> dict[str, list[str]]:
    """Repo-relative path -> offending fragments, across all source roots."""
    found: dict[str, list[str]] = {}
    for src_root in SRC_ROOTS:
        if not src_root.exists():
            continue
        for path in sorted(src_root.rglob("*.py")):
            # Skip test files (they mock things and don't ship to production)
            if "/tests/" in str(path) or path.name.startswith("test_"):
                continue
            hits = _violations(path)
            if hits:
                found[str(path.relative_to(REPO_ROOT))] = hits
    return found


def test_no_bot_id_paths_in_source():
    """No NEW /Users/{bot_id}/ construction may be added to source."""
    found = _scan()

    regressions = [
        (rel, len(hits), BASELINE.get(rel, 0), hits)
        for rel, hits in sorted(found.items())
        if len(hits) > BASELINE.get(rel, 0)
    ]

    detail = "\n".join(
        f"  {rel}: {n} construction(s), baseline allows {allowed}\n"
        + "\n".join(f"      {h}" for h in hits)
        for rel, n, allowed, hits in regressions
    )

    assert not regressions, (
        "New direct /Users/{bot_id}/ path construction found.\n\n"
        + detail
        + "\n\nbot_id is the LOGICAL name in network.json and is not the "
        "macOS account name — the bot `team-bot-b` runs on the `personal-bot-user` account. "
        "These constructions resolve to a path that does not exist for such "
        "bots, and because the usual caller wraps the read in a permissive "
        "except-branch (`return True` / `return {}`), the failure surfaces as "
        "a silently wrong answer rather than an error.\n\n"
        "Use evolve_admin.config.bot_home(bot_id) in the admin package, or "
        "evolve_config.bot_home(bot_id) in the analyzer package.\n\n"
        "If the literal is genuinely correct here — an account-CREATION site, "
        "or a last-resort fallback whose primary path already goes through "
        "bot_home() — raise this file's BASELINE entry in the same PR, with a "
        "source comment saying why, so the exception stays reviewable."
    )
