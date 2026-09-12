"""tests/test_evo_verify_via_coherence.py — verify_via target/args must match the target tool's schema.

Background: post-merge review (2026-05-19) caught a class of bug where
action tools shipped ``verify_via.args`` containing keys the target
read tools refused (``additionalProperties: False`` on the target's
input_schema). Prior tests asserted the verify_via dict's shape but
never round-tripped its args through the target tool's schema — that
shape-only check was the silent-failure window.

This module closes that window. For every non-read tool in the registry:

  1. Invoke its handler against tmp_path (so it returns a dict; the
     handler may itself fail because the action isn't valid in the
     test fixture — that's OK, we just want the response shape).

  2. If the response has a ``verify_via`` key with a non-null
     ``tool``, resolve that name through the registry.

  3. Assert every key in ``verify_via.args`` appears in the target
     tool's ``input_schema.properties`` (or that the schema's
     ``additionalProperties`` is permissive — but our convention is
     ``additionalProperties: False``).

The test fixture is intentionally lightweight — handlers are mostly
expected to bail early with an error response when run against an
empty tmp_path. That's fine; we just need them to RETURN a dict and
populate ``verify_via`` on the happy path.

Some handlers won't ever return verify_via in the failure case (they
abort with ``{"ok": False, "error": ...}``). To cover them, we also
extract verify_via blobs by ast.parse'ing each tool source file and
checking any literal ``"verify_via": { ... }`` dict whose ``args``
key is itself a literal dict.

This is the "verify-the-verify" pass per spec §3.7 lever #4."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.evo import tools as _tools  # noqa: E402


_TOOLS_DIR = _ADMIN_PKG / "evolve_admin" / "evo" / "tools"


def _ast_dict_string_keys(node: ast.Dict) -> set[str]:
    """Return the set of string-literal keys in an ast.Dict node.
    Non-string keys (or computed keys) are silently dropped."""
    out: set[str] = set()
    for k in node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            out.add(k.value)
    return out


def _ast_dict_get(node: ast.Dict, key: str) -> ast.AST | None:
    """Return the value AST node for ``key`` in an ast.Dict, or None."""
    for k, v in zip(node.keys, node.values):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    return None


def _extract_literal_verify_vias(py_text: str) -> list[dict]:
    """Parse a tool source file and return every verify_via blob found
    in the AST, with a STATIC view of its tool name and args-key set.

    Unlike ast.literal_eval, this handles verify_via dicts whose
    values reference variables (e.g. ``"bot_id": bot_id``) — we only
    need to know the structural shape (tool name, which args keys
    appear), not the runtime values.

    Each returned dict has shape:
      ``{"tool": <str or None>, "args_keys": set[str], "has_expect": bool}``
    """
    out: list[dict] = []
    tree = ast.parse(py_text)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        verify_via_node = _ast_dict_get(node, "verify_via")
        if not isinstance(verify_via_node, ast.Dict):
            continue
        tool_node = _ast_dict_get(verify_via_node, "tool")
        if isinstance(tool_node, ast.Constant):
            tool_name = tool_node.value   # str or None
        else:
            # Computed tool name — skip; covered by runtime tests.
            continue
        args_node = _ast_dict_get(verify_via_node, "args")
        if isinstance(args_node, ast.Dict):
            args_keys = _ast_dict_string_keys(args_node)
        else:
            args_keys = set()
        out.append({
            "tool": tool_name,
            "args_keys": args_keys,
            "has_expect": _ast_dict_get(verify_via_node, "expect") is not None,
        })
    return out


def _all_literal_verify_vias() -> list[tuple[str, dict]]:
    """Walk every tool module under evo/tools/ and pull out literal
    verify_via blobs. Returns ``(source_file, blob)`` pairs so failure
    messages can point at the right file."""
    found: list[tuple[str, dict]] = []
    for path in sorted(_TOOLS_DIR.glob("*.py")):
        if path.name.startswith("__"):
            continue
        text = path.read_text()
        for blob in _extract_literal_verify_vias(text):
            found.append((str(path), blob))
    return found


# ─────────────────────────────────────────────────────────────────────────────
# The actual coherence assertions
# ─────────────────────────────────────────────────────────────────────────────


def test_at_least_one_verify_via_exists():
    """Sanity — the regex/AST scan finds non-zero verify_via blobs.
    Guards against the scan silently breaking and the rest of this
    suite trivially passing."""
    blobs = _all_literal_verify_vias()
    assert len(blobs) > 0, "no verify_via blobs found — scan broken?"


@pytest.mark.parametrize("source_file,blob", _all_literal_verify_vias())
def test_verify_via_target_tool_exists(source_file: str, blob: dict):
    """Every verify_via with a non-null ``tool`` must reference a
    registered tool name."""
    target = blob.get("tool")
    if target is None:
        # Null target — we banned this in the post-review fix.
        pytest.fail(
            f"{source_file}: verify_via.tool is None — spec §3.7 lever "
            "#4 requires every action's verify_via to point at a read "
            "tool. Fix: point at a real read tool or omit verify_via "
            "and surface the constraint in the response prose."
        )
    looked_up = _tools.lookup(target)
    assert looked_up is not None, (
        f"{source_file}: verify_via.tool={target!r} is not a registered "
        f"tool. Registered tools: {sorted(t.name for t in _tools.all_tools())}"
    )


@pytest.mark.parametrize("source_file,blob", _all_literal_verify_vias())
def test_verify_via_args_match_target_schema(source_file: str, blob: dict):
    """Every key in verify_via.args must appear in the target tool's
    input_schema.properties — otherwise the verify call fails at
    ``additionalProperties: False`` validation.

    This is the bug the post-merge review (2026-05-19) caught: 6
    action tools were sending {signal_id} to a tool that only
    accepted {producer, bot_id, max_results}.
    """
    target = blob.get("tool")
    if target is None:
        pytest.skip("null tool — covered by other test")
    looked_up = _tools.lookup(target)
    if looked_up is None:
        pytest.skip("target tool not registered — covered by other test")

    schema = looked_up.input_schema or {}
    props = set((schema.get("properties") or {}).keys())
    additional = schema.get("additionalProperties", True)

    arg_keys = blob.get("args_keys") or set()
    unknown = arg_keys - props
    if unknown and additional is False:
        pytest.fail(
            f"{source_file}: verify_via.args has keys {sorted(unknown)} that "
            f"are NOT in {target}'s input_schema properties "
            f"{sorted(props)}, and the target sets additionalProperties=False. "
            "The verify call would fail at schema validation. Either "
            "add the key to the target's schema or drop it from args."
        )


def test_verify_via_signal_history_calls_use_signal_id_filter():
    """Specific regression guard: every action.signal.* tool's
    verify_via points at pod_state.signals.history with signal_id as
    a filter (added in the post-merge fix). If a future tool emits
    verify_via for signals.history WITHOUT signal_id, it'd return the
    pod-wide transition timeline rather than the per-signal one —
    not actionable as a verify.
    """
    history_tool = _tools.lookup("pod_state.signals.history")
    assert history_tool is not None
    assert "signal_id" in (history_tool.input_schema.get("properties") or {}), (
        "pod_state.signals.history must accept signal_id as a filter "
        "(used by every action.signal.* verify_via)"
    )


def test_verify_via_proposals_pending_calls_use_proposal_id_filter():
    """Mirror guard for action.proposal.* tools."""
    pending_tool = _tools.lookup("pod_state.proposals.pending")
    assert pending_tool is not None
    assert "proposal_id" in (pending_tool.input_schema.get("properties") or {}), (
        "pod_state.proposals.pending must accept proposal_id as a filter "
        "(used by action.proposal.* verify_via to confirm the proposal "
        "left the queue — count=0 is the success case)"
    )
