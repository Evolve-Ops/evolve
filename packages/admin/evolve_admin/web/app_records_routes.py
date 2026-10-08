"""The records surface — list, detail and history screens drawn from an app's schema (D-AD4).

Design: ``internal/design-app-records-layer-2026-09-26.md`` §2.2. Brief:
``records-surface-from-the-schema``. View model: :mod:`evolve_admin.app_records_view`.

    GET /apps/<app_id>/records                       the app's tables (pick an instance first if several)
    GET /apps/<app_id>/records/<table>               list: sort / filter / pages
    GET /apps/<app_id>/records/<table>/<key>         detail + derived rollups + history
    GET /api/apps/<app_id>/records/tile              the one summary tile for the app's card

All GETs, no writes, no CSRF surface. The operator's device-cookie gate in
``server.py`` covers them like every other admin route.

NO APP CODE. Any installed app with a ``store:`` block gets these screens; an
app may name a replacement route per screen (``screens: {list: "/route"}``) and
the platform then redirects there instead of rendering.

NO MODEL, NO PRIVATE PATH. Reads go through ``app_store`` verbs only (see the
view module); nothing on this surface calls a model.

FAIL CLOSED. A store this surface cannot read renders "this app's store is not
readable" with the loader's own reason — never a stack trace. A table, row,
instance or app that is not there — or that the verbs refuse as ``forbidden`` —
is a 404, never a 403, so the surface does not confirm what exists.

OUTPUT IS ESCAPED. Every cell is ``html.escape``d; a row value is data, never
markup. JSON columns render as truncated compact JSON.
"""
from __future__ import annotations

import html
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from flask import Flask, Response, jsonify, redirect, request
from flask.typing import ResponseReturnValue

from .. import app_records_view as view
from .. import app_store
from ..app_store import RecordsRefusal
from ..config import CANONICAL_SHARED_DIR, load_network

log = logging.getLogger(__name__)

PAGE_PREFIX = "/apps/"
JSON_CELL_MAX = 80

_CSS = """
.rec-wrap { max-width: 1100px; margin: 0 auto; padding: 18px 16px; }
.rec-crumbs { color: var(--text2); font-size: 0.85rem; margin-bottom: 14px; }
.rec-crumbs a { color: var(--accent); text-decoration: none; }
.rec-wrap h1 { font-size: 1.25rem; margin-bottom: 4px; }
.rec-bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 14px 0; }
.rec-pager { display: flex; gap: 12px; align-items: center; margin-top: 12px; font-size: 0.85rem; color: var(--text2); }
.rec-pager a { color: var(--accent); }
.rec-th a { color: inherit; text-decoration: none; }
.rec-th .rec-dir { color: var(--accent); }
.rec-kv { display: grid; grid-template-columns: minmax(120px, 200px) 1fr; gap: 6px 14px; font-size: 0.85rem; }
.rec-kv dt { color: var(--text2); }
.rec-kv dd { word-break: break-word; }
.rec-derived { color: var(--text3); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; margin-left: 6px; }
.rec-note { color: var(--text2); font-size: 0.85rem; }
.rec-err { color: var(--red); font-size: 0.85rem; margin: 8px 0; }
.rec-mono { font-family: ui-monospace, Menlo, monospace; font-size: 0.78rem; }
@media (max-width: 480px) { .rec-kv { grid-template-columns: 1fr; } }
"""

_THEME_JS = ("try{document.documentElement.setAttribute('data-theme',"
             "localStorage.getItem('evolve-theme')||'dark');}catch(e){}")


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _page(title: str, body: str, status: int = 200) -> Response:
    doc = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_e(title)} — Evolve</title>"
        '<link rel="stylesheet" href="/static/css/base.css">'
        f"<style>{_CSS}</style><script>{_THEME_JS}</script></head>"
        f'<body><main class="rec-wrap">{body}</main></body></html>'
    )
    resp = Response(doc, status=status, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _cell(ctype: str, value: Any) -> str:
    if value is None:
        return '<span style="color:var(--text3)">—</span>'
    if ctype == "bool":
        return "yes" if value else "no"
    if ctype == "json":
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if len(text) > JSON_CELL_MAX:
            text = text[: JSON_CELL_MAX - 1] + "…"
        return f'<span class="rec-mono">{_e(text)}</span>'
    return _e(value)


def _qs(instance: str | None, **extra: Any) -> str:
    items = [(k, v) for k, v in extra.items() if v not in (None, "")]
    if instance:
        items.insert(0, ("instance", instance))
    return ("?" + urlencode(items)) if items else ""


def _base(app_id: str) -> str:
    return f"{PAGE_PREFIX}{quote(app_id, safe='')}/records"


def _not_readable(app_id: str, reason: str) -> Response:
    body = (f'<div class="rec-crumbs"><a href="/">Apps</a></div><h1>{_e(app_id)}</h1>'
            '<div class="card"><div class="card-title">This app\'s store is not readable</div>'
            f'<p class="rec-note">{_e(reason)}</p></div>')
    return _page(f"{app_id} store not readable", body, 409)


def _not_found() -> Response:
    return _page("Not found", '<div class="rec-crumbs"><a href="/">Apps</a></div>'
                              '<div class="card"><p class="rec-note">There is nothing here.</p></div>', 404)


def register_app_records_routes(app: Flask, network_path: Path) -> None:
    """Register the records surface (three pages and one JSON tile) on ``app``."""

    def _shared() -> Path:
        return Path(load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR))

    def _multi(app_id: str) -> bool:
        """More than one instance on this pod, so links must carry ``?instance=``."""
        return len(view.instances_of(_shared(), app_id)) > 1

    def _app_name(shared: Path, app_id: str) -> str:
        try:
            from ..applications.app_spec_store import load_spec
            spec = load_spec(shared, app_id)
            return (spec.name if spec is not None and spec.name else app_id)
        except Exception:  # noqa: BLE001 - a name is decoration; never fail the page for it
            return app_id

    def _guard(fn: Any) -> ResponseReturnValue:
        """Map a refusal to the page that tells the truth about it, and nothing more."""
        try:
            return fn()
        except RecordsRefusal as exc:
            if exc.code in view.NOT_FOUND_CODES:
                return _not_found()
            if exc.code in view.INPUT_CODES:
                body = ('<div class="rec-crumbs"><a href="/">Apps</a></div>'
                        f'<div class="card"><p class="rec-err">{_e(exc.message)}</p></div>')
                return _page("Cannot show that", body, 400)
            if exc.code in ("store_failed", "busy"):
                log.warning("records surface: %s", exc)
            return _not_readable(request.view_args.get("app_id", "app") if request.view_args else "app",
                                 exc.message)

    def _resolve(app_id: str) -> tuple[Path, str | list[str]]:
        shared = _shared()
        return shared, view.pick_instance(shared, app_id, request.args.get("instance") or None)

    def _chooser(app_id: str, name: str, instances: list[str], table: str | None) -> Response:
        rows = "".join(
            f'<li><a href="{_e(_base(app_id) + (("/" + quote(table, safe="")) if table else "") + _qs(i))}">{_e(i)}</a></li>'
            for i in instances)
        body = (f'<div class="rec-crumbs"><a href="/">Apps</a> / {_e(name)}</div>'
                f'<h1>{_e(name)}</h1><p class="rec-note">This app keeps one set of records per '
                f'bot. Choose whose to show.</p><div class="card"><ul>{rows}</ul></div>')
        return _page(name, body)

    @app.get(PAGE_PREFIX + "<app_id>/records")
    def app_records_index(app_id: str) -> ResponseReturnValue:
        def go() -> ResponseReturnValue:
            shared, inst = _resolve(app_id)
            name = _app_name(shared, app_id)
            if isinstance(inst, list):
                return _chooser(app_id, name, inst, None)
            loaded = view.load(shared, app_id, inst)
            if isinstance(loaded, view.Unreadable):
                return _not_readable(app_id, loaded.reason)
            items = []
            for table in sorted(loaded.app.schema.tables):
                sdef = view.surface_of(loaded.app, table)
                kind = "ledger" if loaded.app.schema.is_ledger(table) else "table"
                href = (sdef.get("screens") or {}).get("list") or _base(app_id) + "/" + quote(table, safe="") + _qs(inst)
                items.append(f'<li><a href="{_e(href)}">{_e(table)}</a> '
                             f'<span class="badge badge-sm badge-neutral">{kind}</span> '
                             f'<span class="rec-note">{_e(sdef.get("description", ""))}</span></li>')
            body = (f'<div class="rec-crumbs"><a href="/">Apps</a> / {_e(name)}</div>'
                    f'<h1>{_e(name)}</h1><div class="card"><ul>{"".join(items)}</ul></div>')
            return _page(name, body)
        return _guard(go)

    def _filters(loaded: view.Loaded, table: str) -> tuple[dict[str, Any], list[str]]:
        """Filter dict from ``f.<col>=v`` and the form's ``fcol``/``fval``. A column the
        schema does not declare is passed through ON PURPOSE: the verb refuses it."""
        cols = view.table_columns(loaded.app, table)
        raw: dict[str, str] = {k[2:]: v for k, v in request.args.items() if k.startswith("f.") and v != ""}
        if request.args.get("fcol") and request.args.get("fval", "") != "":
            raw[request.args["fcol"]] = request.args["fval"]
        flt: dict[str, Any] = {}
        for col, text in raw.items():
            ctype = cols.get(col)
            if ctype is None:
                flt[col] = text  # → undeclared_column from the verb
                continue
            try:
                flt[col] = view.coerce(ctype, text)
            except ValueError as exc:
                raise RecordsRefusal("bad_filter", f"{col}: {exc}") from exc
        return flt, [f"f.{c}={t}" for c, t in raw.items()]

    @app.get(PAGE_PREFIX + "<app_id>/records/<table>")
    def app_records_list(app_id: str, table: str) -> ResponseReturnValue:
        def go() -> ResponseReturnValue:
            shared, inst = _resolve(app_id)
            name = _app_name(shared, app_id)
            if isinstance(inst, list):
                return _chooser(app_id, name, inst, table)
            loaded = view.load(shared, app_id, inst)
            if isinstance(loaded, view.Unreadable):
                return _not_readable(app_id, loaded.reason)
            if table not in loaded.app.schema.tables:
                raise RecordsRefusal("unknown_table", f"no table {table!r}")
            sdef = view.surface_of(loaded.app, table)
            replaced = (sdef.get("screens") or {}).get("list")
            if replaced:
                return redirect(replaced)
            cols = view.table_columns(loaded.app, table)
            flt, _ = _filters(loaded, table)
            sort = request.args.get("sort") or None
            try:
                page = int(request.args.get("page", "1"))
            except ValueError:
                page = 1
            data = view.list_page(shared, loaded.app, inst, table, flt=flt, sort=sort, page=page)
            return _page(f"{name} · {table}", _render_list(
                app_id, name, loaded, table, cols, flt, sort, data, sdef, inst))
        return _guard(go)

    def _render_list(app_id: str, name: str, loaded: view.Loaded, table: str,
                     cols: dict[str, str], flt: dict[str, Any], sort: str | None,
                     data: dict[str, Any], sdef: dict[str, Any], inst: str) -> str:
        schema = loaded.app.schema
        is_ledger = schema.is_ledger(table)
        keycol = None if is_ledger else schema.tables[table]["key"][0]
        base = f"{_base(app_id)}/{quote(table, safe='')}"
        carry = {f"f.{c}": (str(v).lower() if isinstance(v, bool) else v) for c, v in flt.items()}

        def link(**kw: Any) -> str:
            params = {**carry, "sort": sort, **kw}
            return base + _qs(inst if _multi(app_id) else None, **params)

        heads = []
        for col in cols:
            arrow = ""
            nxt = col
            if sort and sort.lstrip("-") == col:
                arrow = ' <span class="rec-dir">' + ("▼" if sort.startswith("-") else "▲") + "</span>"
                nxt = col if sort.startswith("-") else "-" + col
            heads.append(f'<th class="rec-th"><a href="{_e(link(sort=nxt, page=None))}">{_e(col)}{arrow}</a></th>')
        body_rows = []
        for r in data["rows"]:
            tds = []
            for col, ctype in cols.items():
                v = r.get(col)
                inner = _cell(ctype, v)
                if v is not None and keycol and col == keycol:
                    href = f"{base}/{quote(str(v), safe='')}" + _qs(
                        inst if _multi(app_id) else None)
                    inner = f'<a href="{_e(href)}" style="color:var(--accent)">{inner}</a>'
                elif v is not None and is_ledger and col == "thing_id":
                    href = f"{_base(app_id)}/{quote(schema.ledger_of() or '', safe='')}/{quote(str(v), safe='')}" + _qs(
                        inst if _multi(app_id) else None)
                    inner = f'<a href="{_e(href)}" style="color:var(--accent)">{inner}</a>'
                tds.append(f'<td data-label="{_e(col)}">{inner}</td>')
            body_rows.append("<tr>" + "".join(tds) + "</tr>")

        if data["total"] == 0 and not flt:
            what = sdef.get("description") or f"The {table} table has no rows yet."
            empty = (f'<div class="empty-state-card"><div class="card-title">Nothing here yet</div>'
                     f'<p class="rec-note">{_e(what)}</p></div>')
            table_html = empty
        elif not body_rows:
            table_html = ('<div class="empty-state-card"><p class="rec-note">No rows match that '
                          'filter.</p></div>')
        else:
            table_html = ('<div class="resp-table-wrap"><table class="resp-table"><thead><tr>'
                          + "".join(heads) + "</tr></thead><tbody>" + "".join(body_rows)
                          + "</tbody></table></div>")

        fil_cols = [c for c, t in cols.items() if view.filterable(t)]
        opts = "".join(f'<option value="{_e(c)}">{_e(c)}</option>' for c in fil_cols)
        hidden = "".join(f'<input type="hidden" name="{_e(k)}" value="{_e(v)}">' for k, v in carry.items())
        hidden += (f'<input type="hidden" name="sort" value="{_e(sort)}">' if sort else "")
        hidden += (f'<input type="hidden" name="instance" value="{_e(inst)}">'
                   if _multi(app_id) else "")
        form = (f'<form class="rec-bar" method="get" action="{_e(base)}">{hidden}'
                f'<select name="fcol" class="input-w-md" aria-label="Filter column">{opts}</select>'
                '<input name="fval" class="input-w-md" placeholder="equals…" aria-label="Filter value">'
                '<button class="btn btn-sm" type="submit">Filter</button>'
                + (f'<a class="rec-note" href="{_e(base + _qs(inst if _multi(app_id) else None))}">clear</a>'
                   if flt else "") + "</form>")

        pager = ""
        if data["pages"] > 1:
            prev = (f'<a href="{_e(link(page=data["page"] - 1))}">← Newer</a>' if data["page"] > 1 else "")
            nxt = (f'<a href="{_e(link(page=data["page"] + 1))}">More →</a>'
                   if data["page"] < data["pages"] else "")
            pager = (f'<div class="rec-pager">{prev}<span>page {data["page"]} of {data["pages"]}</span>{nxt}</div>')
        cap = ""
        if data["capped"]:
            cap = (f'<p class="rec-note">Showing the first {app_store.MAX_LIST_LIMIT} of {data["total"]} '
                   f'rows — filter to narrow.</p>')
        kind = "ledger" if is_ledger else "table"
        desc = f'<p class="rec-note">{_e(sdef["description"])}</p>' if sdef.get("description") else ""
        return (f'<div class="rec-crumbs"><a href="/">Apps</a> / <a href="{_e(_base(app_id) + _qs(inst if _multi(app_id) else None))}">{_e(name)}</a> / {_e(table)}</div>'
                f'<h1>{_e(table)} <span class="badge badge-sm badge-neutral">{kind}</span></h1>'
                f'{desc}{form}<p class="rec-note">{data["total"]} row{"" if data["total"] == 1 else "s"}</p>'
                f'{table_html}{cap}{pager}')

    @app.get(PAGE_PREFIX + "<app_id>/records/<table>/<path:key>")
    def app_records_detail(app_id: str, table: str, key: str) -> ResponseReturnValue:
        def go() -> ResponseReturnValue:
            shared, inst = _resolve(app_id)
            name = _app_name(shared, app_id)
            if isinstance(inst, list):
                return _chooser(app_id, name, inst, table)
            loaded = view.load(shared, app_id, inst)
            if isinstance(loaded, view.Unreadable):
                return _not_readable(app_id, loaded.reason)
            tdef = loaded.app.schema.table(table)
            if tdef is None or tdef.get("ledger"):
                raise RecordsRefusal("unknown_table", f"no detail page for {table!r}")
            sdef = view.surface_of(loaded.app, table)
            replaced = (sdef.get("screens") or {}).get("detail")
            if replaced:
                return redirect(replaced)
            kcol = tdef["key"][0]
            try:
                keyval: Any = view.coerce(tdef["columns"][kcol], key) if len(tdef["key"]) == 1 else None
            except ValueError as exc:
                raise RecordsRefusal("not_found", str(exc)) from exc
            if len(tdef["key"]) > 1:
                keyval = {c: view.coerce(tdef["columns"][c], request.args.get(f"k.{c}", ""))
                          for c in tdef["key"] if request.args.get(f"k.{c}")}
            d = view.detail(shared, loaded.app, inst, table, keyval)
            return _page(f"{name} · {table} · {key}", _render_detail(
                app_id, name, table, tdef, d, inst))
        return _guard(go)

    def _render_detail(app_id: str, name: str, table: str,
                       tdef: dict[str, Any], d: dict[str, Any], inst: str) -> str:
        multi = _multi(app_id)
        inq = _qs(inst if multi else None)
        fields = "".join(
            f'<dt>{_e(c)}</dt><dd>{_cell(t, d["row"].get(c))}</dd>' for c, t in tdef["columns"].items())
        derived = ""
        if d["rollups"]:
            derived = "".join(
                f'<dt>{_e(n)}<span class="rec-derived">derived</span></dt>'
                f'<dd>{_cell("json" if isinstance(v, (dict, list)) else "text", v)}</dd>'
                for n, v in d["rollups"].items())
            derived = (f'<div class="card"><div class="card-title">Derived from the history</div>'
                       f'<dl class="rec-kv">{derived}</dl></div>')
        strip = ""
        if d["has_ledger"]:
            if d["entries"]:
                rows = []
                for e in d["entries"]:
                    extra = " ".join(
                        x for x in (
                            f'<span>{_e(e["amount"])}</span>' if e.get("amount") is not None else "",
                            f'<span>with {_e(e["counterparty"])}</span>' if e.get("counterparty") else "",
                        ) if x)
                    note = f'<div class="rec-note">{_e(e["note"])}</div>' if e.get("note") else ""
                    rows.append(
                        f'<tr><td data-label="when">{_e(e["at"])}</td>'
                        f'<td data-label="kind"><span class="badge badge-sm badge-neutral">{_e(e["kind"])}</span></td>'
                        f'<td data-label="by">{_e(e["by"])}</td>'
                        f'<td data-label="detail">{extra or "—"}{note}</td></tr>')
                strip = ('<div class="card"><div class="card-title">History</div>'
                         '<div class="resp-table-wrap"><table class="resp-table"><thead><tr>'
                         '<th>when</th><th>kind</th><th>by</th><th>detail</th></tr></thead><tbody>'
                         + "".join(rows) + "</tbody></table></div></div>")
            else:
                strip = ('<div class="card"><div class="card-title">History</div>'
                         '<p class="rec-note">No entries recorded for this row yet.</p></div>')
        edits = ""
        if d["revisions"]:
            edits = "".join(f'<li>{_e(r["at"])} — {_e(r["op"])} by {_e(r["by"])}</li>' for r in d["revisions"])
            edits = f'<div class="card"><div class="card-title">Edits to this row</div><ul>{edits}</ul></div>'
        label = _e(next(iter(d["row"].values()), ""))
        return (f'<div class="rec-crumbs"><a href="/">Apps</a> / <a href="{_e(_base(app_id) + inq)}">{_e(name)}</a>'
                f' / <a href="{_e(_base(app_id) + "/" + quote(table, safe="") + inq)}">{_e(table)}</a> / {label}</div>'
                f'<h1>{label}</h1><div class="card"><div class="card-title">Fields</div>'
                f'<dl class="rec-kv">{fields}</dl></div>{derived}{strip}{edits}')

    @app.get("/api/apps/<app_id>/records/tile")
    def app_records_tile(app_id: str) -> ResponseReturnValue:
        """One summary tile per instance of the app, for its card. 404 when the app
        declares no store; ``readable: false`` + the loader's reason when it cannot be read."""
        shared = _shared()
        try:
            app_store.declared_app(shared, app_id)  # unknown app / no store → 404 below
            have = view.instances_of(shared, app_id)
            tiles: list[dict[str, Any]] = []
            for inst in have:
                loaded = view.load(shared, app_id, inst)
                if isinstance(loaded, view.Unreadable):
                    return jsonify({"app_id": app_id, "readable": False, "reason": loaded.reason,
                                    "tiles": []})
                t = view.tile(shared, loaded.app, inst)
                t["href"] = _base(app_id) + _qs(inst if len(have) > 1 else None)
                tiles.append(t)
            return jsonify({"app_id": app_id, "readable": True, "tiles": tiles})
        except RecordsRefusal as exc:
            if exc.code in view.NOT_FOUND_CODES:
                return jsonify({"error": "not found"}), 404
            return jsonify({"app_id": app_id, "readable": False, "reason": exc.message, "tiles": []})
