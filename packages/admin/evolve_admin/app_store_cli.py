"""``evolve-admin records doctor`` — the records layer's D-CS7 control (D-AD3).

Design: ``internal/design-app-records-layer-2026-09-26.md`` §2.3 — "a status that
no entry explains fails a check". Brief: ``app-store-and-ledger-verbs`` item 4.

For every app store on the pod (or one ``--app``/``--instance``), each thing
whose stored status column disagrees with the status its ledger entries derive
is a FAIL, named by app, instance, table, key and column. A store the check
cannot read — wrong uid, schema mismatch, a spec that no longer declares it —
is UNKNOWN, never OK: a control that cannot see its subject says so.

Exit: 0 all ok · 1 any fail · 2 any unknown (and no fail) · 0 with "no stores"
on a pod that has none yet. Read-only; run it as the admin daemon's user
(``sudo -u evolve …``), since only the file's owner opens it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click


def register_cli(main: click.Group) -> None:
    @main.group("records")
    def records_group() -> None:
        """App records stores (D-AD2): the per-app SQLite files under {sharedDir}/apps."""

    @records_group.command("doctor")
    @click.option("--app", "app_id", default="", help="Only this app id.")
    @click.option("--instance", default="", help="Only this instance (bot id or pod name).")
    @click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
    @click.pass_context
    def records_doctor(ctx: click.Context, app_id: str, instance: str, as_json: bool) -> None:
        """Fail any stored status that no ledger entry explains (D-CS7)."""
        from . import app_ledger, app_store
        from .config import CANONICAL_SHARED_DIR, load_network
        from .user_directory import records as _directory_records  # noqa: F401 — registers the platform app

        network = load_network(ctx.obj["network_path"])
        shared = Path(network.get("sharedDir") or CANONICAL_SHARED_DIR)
        results = [
            app_ledger.unexplained_statuses(shared, a, i)
            for a, i, _p in app_store.iter_stores(shared)
            if (not app_id or a == app_id) and (not instance or i == instance)
        ]
        if as_json:
            click.echo(json.dumps(results, indent=2, sort_keys=True, default=str))
        elif not results:
            click.echo(f"no app stores under {shared / 'apps'}")
        for r in results if not as_json else []:
            head = f"{r['status'].upper():7} {r['app_id']}/{r['instance']}"
            if r["status"] == "ok":
                click.echo(head)
            elif r["status"] == "unknown":
                click.echo(f"{head} — {r['reason']}")
            else:
                click.echo(f"{head} — {len(r['findings'])} status value(s) no entry explains")
                for f in r["findings"]:
                    click.echo(f"        {f['table']}[{f['key']}].{f['column']} = "
                               f"{f['stored']!r}, entries say {f['derived']!r} ({f['why']})")
        statuses = {r["status"] for r in results}
        sys.exit(1 if "fail" in statuses else 2 if "unknown" in statuses else 0)
