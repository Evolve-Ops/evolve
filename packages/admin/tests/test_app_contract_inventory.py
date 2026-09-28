"""The app-facing contract v1 seam test (D-AP1..3).

Design: ``internal/design-application-platform-2026-09-22.md`` §2 row 12, §4;
inventory ``internal/design-app-contract-v1.md``; registry
``evolve_admin/app_contract.py``.

WHAT THESE PIN:
  * **The inventory is one thing in three places.** The doc's table, the
    Python ``ROWS`` and the plugin mirror (``rows.ts``) agree row for row —
    same names, kinds, versions, services, statuses; the doc also agrees on
    owner, introducer and the works-without-Evolve verdict.
  * **Registration fails closed.** Every bot-facing daemon route carries a
    row; a route registered without one raises ``ContractRowMissing`` naming
    the row it needs.
  * **The PA reaches platform services through the contract.** Every source
    file under the six PA briefs' ``touches:`` is classified; every import
    from a scanned PA module into a DIFFERENT service goes through
    ``app_contract`` — and what does not is exactly ``KNOWN_DEBT``.
  * **The design §4 forbidden list is four named assertions**, each proven
    against a known-good and a known-bad fixture — and each known-bad
    fixture is red on its OWN assertion only (control-registry rule) —
    before being run over the real tree.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Callable

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import app_contract as ac  # noqa: E402

REPO = _ADMIN_DIR.parent.parent
DOC = REPO / "internal" / "design-app-contract-v1.md"
MIRROR = REPO / "packages" / "plugin" / "src" / "apps" / "contract" / "rows.ts"
SCANNED_ROLES = ("app", "service", "transport")


# ── (a) doc ⇔ registry ⇔ plugin mirror ─────────────────────────────────────


def _doc_rows() -> dict[str, dict[str, str]]:
    text = DOC.read_text(encoding="utf-8")
    block = text.split("<!-- contract-rows:start -->", 1)[1].split(
        "<!-- contract-rows:end -->", 1)[0]
    rows: dict[str, dict[str, str]] = {}
    for line in block.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        name, kind, version, service, owner, wwoe, by, status = cells
        assert name not in rows, f"duplicate doc row {name}"
        rows[name.strip("`")] = {
            "kind": kind, "version": version,
            "service": service.split(" ", 1)[0], "owner": owner.strip("`"),
            "verdict": wwoe.split(" ", 1)[0],
            "introduced_by": by.split(" · ", 1)[0].strip("`"), "status": status,
        }
    return rows


def _mirror_rows() -> dict[str, dict[str, str]]:
    pat = re.compile(
        r'\{ name: "(?P<name>[^"]+)", kind: "(?P<kind>[^"]+)", '
        r'version: "(?P<version>[^"]+)", service: (?P<service>\d+), '
        r'status: "(?P<status>[^"]+)" \}')
    rows: dict[str, dict[str, str]] = {}
    for m in pat.finditer(MIRROR.read_text(encoding="utf-8")):
        assert m["name"] not in rows, f"duplicate mirror row {m['name']}"
        rows[m["name"]] = {k: m[k] for k in ("kind", "version", "service", "status")}
    return rows


def test_registry_rows_are_well_formed():
    assert ac.CONTRACT_VERSION == "v1"
    assert len(ac.ROWS_BY_NAME) == len(ac.ROWS), "duplicate row names"
    for r in ac.ROWS:
        assert r.kind in ac.KINDS, r
        assert r.service in ac.SERVICES, r
        assert r.status in ac.STATUSES, r
        assert r.version == ac.CONTRACT_VERSION, r
        assert r.works_without_evolve.split(" ", 1)[0] in ("yes", "no", "degraded"), r
        assert r.owner and r.introduced_by, r
        if r.status == "shipped":
            assert (REPO / r.owner).is_file(), f"{r.name}: owner {r.owner} missing"


def test_doc_and_registry_match_row_for_row():
    doc = _doc_rows()
    reg = {r.name: r for r in ac.ROWS}
    assert set(doc) == set(reg), (
        f"doc-only: {sorted(set(doc) - set(reg))}; "
        f"registry-only: {sorted(set(reg) - set(doc))}")
    assert len(doc) == len(ac.ROWS)
    for name, d in doc.items():
        r = reg[name]
        assert d == {
            "kind": r.kind, "version": r.version, "service": str(r.service),
            "owner": r.owner, "verdict": r.works_without_evolve.split(" ", 1)[0],
            "introduced_by": r.introduced_by, "status": r.status,
        }, name


def test_plugin_mirror_matches_registry():
    mirror = _mirror_rows()
    assert mirror == {
        r.name: {"kind": r.kind, "version": r.version,
                 "service": str(r.service), "status": r.status}
        for r in ac.ROWS
    }


# ── registration fails closed ──────────────────────────────────────────────


def _flask_app():
    flask = pytest.importorskip("flask")
    return flask.Flask(__name__)


def test_every_bot_facing_route_has_a_row(tmp_path):
    from evolve_admin.web.board_bot_routes import register_board_bot_routes
    app = _flask_app()
    register_board_bot_routes(app, tmp_path / "network.json")  # must not raise
    names = ac.route_names(app, "/api/board-bot/")
    assert names, "no bot-facing routes found — the prefix drifted"
    for n in names:
        assert ac.row(n, "daemon_endpoint").status == "shipped"


def test_a_route_without_a_row_is_refused_naming_the_row():
    app = _flask_app()
    app.add_url_rule("/api/board-bot/cards/<card_id>/archive", "x",
                     lambda **_kw: "", methods=["POST"])
    with pytest.raises(ac.ContractRowMissing) as exc:
        ac.require_route_rows(app, "/api/board-bot/")
    msg = str(exc.value)
    assert "'POST /api/board-bot/cards/<card_id>/archive'" in msg
    assert "ContractRow(" in msg and "app_contract.py" in msg


def test_row_lookup_refuses_unknown_and_wrong_kind():
    assert ac.row("board.move", "tool_verb").service == 8
    with pytest.raises(ac.ContractRowMissing):
        ac.row("board.archive")
    with pytest.raises(ac.ContractRowMissing):
        ac.row("board.move", "daemon_endpoint")


# ── scan scope: the six briefs' touches ────────────────────────────────────


def _brief_touches(brief_id: str) -> list[str]:
    hits = sorted((REPO / "internal" / "dispatch").glob(f"*/{brief_id}.md"))
    assert hits, f"PA brief {brief_id} not found under internal/dispatch/"
    front = hits[0].read_text(encoding="utf-8").split("---", 2)[1]
    touches, inside = [], False
    for line in front.splitlines():
        if line.startswith("touches:"):
            inside = True
            continue
        if inside:
            if line.startswith("  - "):
                touches.append(line[4:].strip())
            elif line and not line.startswith(" "):
                inside = False
    return touches


def _pa_touch_paths() -> set[str]:
    paths: set[str] = set()
    for brief_id in ac.PA_BRIEFS:
        paths.update(_brief_touches(brief_id))
        paths.update(ac.PA_TOUCHES_FROM_PRS.get(brief_id, ()))
    return paths


def _is_source(p: Path) -> bool:
    return p.suffix in (".py", ".ts") and not p.name.endswith(".d.ts")


def _pa_source_files() -> set[str]:
    files: set[str] = set()
    for rel in _pa_touch_paths():
        p = REPO / rel
        if p.is_file() and _is_source(p):
            files.add(rel)
        elif p.is_dir():
            files.update(str(f.relative_to(REPO)) for f in p.rglob("*")
                         if f.is_file() and _is_source(f) and "tests" not in f.parts)
    return files


def _classify(rel: str) -> tuple[str, int | None] | None:
    if rel in ac.PA_MODULES:
        return ac.PA_MODULES[rel]
    for key, val in ac.PA_MODULES.items():
        if key.endswith("/") and rel.startswith(key):
            return val
    return None


def _scanned_files() -> list[str]:
    return sorted(f for f in _pa_source_files()
                  if (_classify(f) or ("", None))[0] in SCANNED_ROLES)


def test_every_pa_touched_source_file_is_classified():
    files = _pa_source_files()
    assert files, "scan scope is empty — the briefs' touches drifted"
    unclassified = sorted(f for f in files if _classify(f) is None)
    assert not unclassified, (
        "PA-touched source files with no app_contract.PA_MODULES entry "
        f"(add one: app / service / transport / host): {unclassified}")


def test_no_stale_classification():
    touched = _pa_touch_paths()
    stale = [k for k in ac.PA_MODULES
             if not any(t == k or t.rstrip("/") + "/" == k or t.startswith(k)
                        for t in touched)]
    assert not stale, f"PA_MODULES entries no PA brief touches: {stale}"


# ── the import scan ────────────────────────────────────────────────────────


def _module_name(rel: str) -> str | None:
    parts = Path(rel).with_suffix("").parts
    if "evolve_admin" in parts:
        return ".".join(parts[parts.index("evolve_admin"):])
    if parts[:2] == ("packages", "analyzer"):
        return ".".join(parts[2:])
    return None


def _imported_modules(rel: str, src: str) -> set[str]:
    me = _module_name(rel) or ""
    pkg = me.split(".")[:-1]
    out: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            out.add(mod)
            out.update(f"{mod}.{a.name}" for a in node.names)
    return out


def _service_of(module: str) -> tuple[str, int] | None:
    best = None
    for key, svc in ac.SERVICE_OWNERS.items():
        if module == key or module.startswith(key + "."):
            if best is None or len(key) > len(best[0]):
                best = (key, svc)
    return best


def cross_service_imports(rel: str, src: str, own: int | None) -> list[str]:
    """``import:<owner>`` for each import into another service not via app_contract."""
    me = _module_name(rel)
    found = set()
    for mod in _imported_modules(rel, src):
        hit = _service_of(mod)
        if hit is None or (me and _service_of(me) == hit):
            continue
        if hit[1] != own:
            found.add(f"import:{hit[0]}")
    return sorted(found)


# ── the four forbidden-list assertions (design §4) ─────────────────────────


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    getattr(body[0], "value", None), ast.Constant):
                ids.add(id(body[0].value))
    return ids


_TS_STRING = r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\''


def _code_strings(src: str, rel: str) -> list[str]:
    """String literals in code — not docstrings, not comments."""
    if rel.endswith(".ts"):
        no_comments = re.sub(r"/\*.*?\*/|//[^\n]*", "", src, flags=re.S)
        return [a or b for a, b in re.findall(_TS_STRING, no_comments)]
    tree = ast.parse(src)
    docs = _docstring_nodes(tree)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def _write_calls(src: str) -> list[str]:
    hits = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        owner = getattr(getattr(f, "value", None), "id", "")
        if name in ("write_text", "write_bytes", "mkstemp"):
            hits.append(name)
        elif (owner, name) in (("os", "replace"), ("os", "rename"), ("json", "dump")):
            hits.append(f"{owner}.{name}")
        elif name in ("open", "fdopen"):
            # open(path, mode) / os.fdopen(fd, mode) / Path(...).open(mode)
            pos = 0 if (name == "open" and owner != "os"
                        and isinstance(f, ast.Attribute)) else 1
            modes = [a.value for a in node.args[pos:pos + 1]]
            modes += [k.value for k in node.keywords if k.arg == "mode"]
            if any(isinstance(m, ast.Constant) and isinstance(m.value, str)
                   and set(m.value) & set("wax+") for m in modes):
                hits.append(name)
    return hits


def private_state_outside_store(rel: str, src: str) -> list[str]:
    """Forbidden #1: a PA module writing state outside the Board/Tracker
    store — unless it IS a registered state writer, or an app module whose
    writes are all manifest-declared ``data_files[]``."""
    if rel.endswith(".ts") or rel in ac.STATE_WRITERS:
        return []
    writes = _write_calls(src)
    if not writes:
        return []
    if rel in ac.DECLARED_APP_DATA:
        manifest, paths = ac.DECLARED_APP_DATA[rel]
        declared = {d.get("path") for d in json.loads(
            (REPO / manifest).read_text(encoding="utf-8"))
            .get("interface_contract", {}).get("data_files", [])}
        missing = [p for p in paths if p not in declared]
        return [f"declared data not in {manifest}: {missing}"] if missing else []
    return [f"writes state ({', '.join(sorted(set(writes)))}) outside the store"]


_MODEL_IDENTIFIERS = {
    "model_client", "ModelClient", "run_delegation", "resume_delegation",
    "_dispatch_one", "_hand_off_to_worker", "call_anthropic", "bot_tier_models",
    "model_tier_chain", "runPinnedSubagent", "anthropic",
}


def reminder_wakes_model(rel: str, src: str, roots: list[str]) -> list[str]:
    """Forbidden #2: nothing reachable (same-module call graph) from a
    reminder root may reference a model client or the worker hand-off."""
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = [r for r in roots if r not in funcs]
    if missing:
        return [f"{rel}: zero-model root(s) not found: {missing}"]
    seen: set[str] = set()
    todo = list(roots)
    idents: set[str] = set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for node in ast.walk(funcs[name]):
            ident = node.id if isinstance(node, ast.Name) else (
                node.attr if isinstance(node, ast.Attribute) else None)
            if ident is None:
                continue
            idents.add(ident)
            if ident in funcs and ident not in seen:
                todo.append(ident)
    bad = sorted(i for i in idents
                 if i in _MODEL_IDENTIFIERS or "anthropic" in i.lower())
    return [f"{rel}: reminder path reaches a model: {bad}"] if bad else []


_ROSTER_KEYS = ("primary_user", "external_ids", "roster.json")


def roster_read_by_file(rel: str, src: str) -> list[str]:
    """Forbidden #3: reading the roster from ``network.json`` / its
    ``primary_user``/``external_ids`` keys instead of an Identity row."""
    hits = sorted({s for s in _code_strings(src, rel)
                   if "network.json" in s or s in _ROSTER_KEYS})
    return [f"reads the roster by file: {hits}"] if hits else []


_SEND_MARKERS = ("api.telegram.org", "chat.postMessage", "hooks.slack.com", "/sendMessage")


def pa_only_notification_route(rel: str, src: str) -> list[str]:
    """Forbidden #4: a send path of its own — ``openclaw message send``, a
    chat API URL — instead of the Delivery row (``delivery.send_to_owner``)."""
    hits = [m for m in _SEND_MARKERS if any(m in s for s in _code_strings(src, rel))]
    if rel.endswith(".ts"):
        if re.search(r'["\']message["\']\s*,\s*["\']send["\']', src):
            hits.append("message send")
    else:
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.List, ast.Tuple)):
                vals = [e.value for e in node.elts if isinstance(e, ast.Constant)]
                if any(a == "message" and b == "send" for a, b in zip(vals, vals[1:])):
                    hits.append("message send")
            if isinstance(node, ast.ImportFrom):
                hits += [a.name for a in node.names if a.name.startswith("_dispatch_via")]
    return [f"own notification route: {sorted(set(hits))}"] if hits else []


def _run_roots(rel: str, src: str) -> list[str]:
    return reminder_wakes_model(
        rel, src, [fn for p, fn in ac.ZERO_MODEL_ROOTS if p == rel]) if any(
        p == rel for p, _ in ac.ZERO_MODEL_ROOTS) else []


ASSERTIONS: dict[str, Callable[[str, str], list[str]]] = {
    "private_state_outside_store": private_state_outside_store,
    "reminder_wakes_model": _run_roots,
    "roster_read_by_file": roster_read_by_file,
    "pa_only_notification_route": pa_only_notification_route,
}


# ── fixtures: one known-good and one known-bad per assertion ───────────────

_FIX = "packages/admin/evolve_admin/_fixture_pa_module.py"
_REMIND_FIX = "packages/admin/evolve_admin/_fixture_touch.py"

GOOD = {
    "private_state_outside_store": (_FIX, (
        "from . import app_contract\n"
        "def keep(bot, net, card):\n"
        "    return app_contract.send_to_owner(bot, net, card['title'])\n")),
    "reminder_wakes_model": (_REMIND_FIX, (
        "def compose(card):\n    return card['title']\n"
        "def fire_remind(bot, net, card):\n"
        "    return deliver(bot, net, compose(card))\n"
        "def deliver(bot, net, msg):\n    return (True, None)\n")),
    "roster_read_by_file": (_FIX, (
        '"""Resolves nothing from network.json — this docstring may say so."""\n'
        "from . import app_contract\n"
        "def who(bot, net):\n    return app_contract.google_configured(bot, net)\n")),
    "pa_only_notification_route": (_FIX, (
        "from . import app_contract\n"
        "def notify(bot, net, text):\n"
        "    return app_contract.send_to_owner(bot, net, text)\n")),
}

BAD = {
    "private_state_outside_store": (_FIX, (
        "import json\nfrom pathlib import Path\n"
        "def remember(ws, tasks):\n"
        "    Path(ws, 'pa-tasks.json').write_text(json.dumps(tasks))\n")),
    "reminder_wakes_model": (_REMIND_FIX, (
        "def compose(card, model_client):\n"
        "    return model_client(prompt=card['title'])\n"
        "def fire_remind(bot, net, card, model_client):\n"
        "    return compose(card, model_client)\n")),
    "roster_read_by_file": (_FIX, (
        "from pathlib import Path\nimport json\n"
        "def who(bot):\n"
        "    net = json.loads(Path('/Users/Shared/evolve/network.json').read_text())\n"
        "    return net['bots'][bot]['primary_user']\n")),
    "pa_only_notification_route": (_FIX, (
        "import subprocess\n"
        "def notify(channel, target, text):\n"
        "    subprocess.run(['openclaw', 'message', 'send', '--channel', channel,\n"
        "                    '--target', target, '--message', text])\n")),
}


def _assert_all(rel: str, src: str, roots: list[str] | None = None) -> dict[str, list[str]]:
    out = {}
    for name, fn in ASSERTIONS.items():
        if name == "reminder_wakes_model":
            out[name] = reminder_wakes_model(rel, src, roots) if roots else []
        else:
            out[name] = fn(rel, src)
    return out


@pytest.mark.parametrize("name", sorted(ASSERTIONS))
def test_known_good_fixture_passes_every_assertion(name):
    rel, src = GOOD[name]
    roots = ["fire_remind"] if name == "reminder_wakes_model" else None
    assert _assert_all(rel, src, roots) == {k: [] for k in ASSERTIONS}


@pytest.mark.parametrize("name", sorted(ASSERTIONS))
def test_known_bad_fixture_is_red_on_its_own_assertion_only(name):
    rel, src = BAD[name]
    roots = ["fire_remind"] if name == "reminder_wakes_model" else None
    result = _assert_all(rel, src, roots)
    assert result[name], f"{name}: known-bad fixture not caught"
    others = {k: v for k, v in result.items() if k != name and v}
    assert not others, f"{name}: known-bad fixture also tripped {others}"


def test_missing_zero_model_root_fails_closed():
    assert reminder_wakes_model(_REMIND_FIX, "def other():\n    pass\n", ["fire_remind"])


def test_cross_service_import_fixture():
    good = "from . import app_contract\nfrom . import board_store\n"
    bad = "from .alerts import dispatcher\nfrom primary_bot import bot_tier_models\n"
    # a State module may import the store; it may not reach Delivery or Model selection.
    rel = "packages/admin/evolve_admin/board_fixture.py"
    assert cross_service_imports(rel, good, 8) == []
    assert cross_service_imports(rel, bad, 8) == [
        "import:evolve_admin.alerts.dispatcher", "import:primary_bot"]


# ── the real tree ──────────────────────────────────────────────────────────


def _findings() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for rel in _scanned_files():
        src = (REPO / rel).read_text(encoding="utf-8")
        own = (_classify(rel) or ("", None))[1]
        if rel.endswith(".py"):
            found.update((rel, f) for f in cross_service_imports(rel, src, own))
        for name, fn in ASSERTIONS.items():
            if fn(rel, src):
                found.add((rel, name))
    return found


def test_zero_model_roots_are_scanned():
    scanned = set(_scanned_files())
    for rel in {entry[0] for entry in ac.ZERO_MODEL_ROOTS}:
        assert rel in scanned, f"{rel} holds a zero-model root but is not scanned"


def test_the_seam_is_exactly_the_known_debt():
    found = _findings()
    debt = {(entry[0], entry[1]) for entry in ac.KNOWN_DEBT}
    assert not (found - debt), (
        "new reach past the contract — route it through app_contract (add a row "
        f"first if it needs a new one), or record it in KNOWN_DEBT with a reason: "
        f"{sorted(found - debt)}")
    assert not (debt - found), (
        f"KNOWN_DEBT entries the scan no longer finds — strike them: {sorted(debt - found)}")
