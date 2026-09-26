"""redeploy_sweep_cli.py — the ``repo-redeploy-sweep`` subcommand.

Its own module, and registered onto ``main`` from cli.py's trailing
registration line, because cli.py is a no-growth-capped hot-hazard file
(tools/file-size-ratchet 4.1a). Same convention as the restore-manifest /
pod-baseline / migrate-ids registrations there.

**What it is for.** The repo-puller's lagging-bot redeploy sweep cannot run
in a tick process that just advanced HEAD: ``cli.py`` imports ``deploy`` at
module load, i.e. before the tick's ``git pull``, so ``EVOLVE_VERSION`` is the
PRE-pull commit and every stamp it wrote would mark the fleet current at a
superseded version. The old disposition was to skip and wait for a tick where
the loaded code is already the checkout — which is only ever a NO-OP tick.
On 2026-08-18 six consecutive ticks advanced with no no-op tick between them
and the sweep never ran at all (#3705 → #3721).

So the tick re-execs this command instead. A fresh process imports the
post-pull ``deploy``, so it sweeps with a truthful identity on the SAME tick
that pulled. It performs no pull and runs no hooks — the parent already did
both. Operators can also run it by hand to force a convergence pass without
a full ``deploy --all``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click


@click.command("repo-redeploy-sweep")
@click.option("--repo", default=None,
              help="Deploy checkout to sweep against (default: the "
                   "platform-keyed deploy checkout).")
@click.option("--quiet", is_flag=True, default=False,
              help="Suppress output when nothing lagged.")
def repo_redeploy_sweep(repo: str | None, quiet: bool) -> None:
    """Redeploy every bot whose install.json stamp is not the deploy checkout.

    The lagging-bot sweep on its own — no pull, no post-advance hooks. This is
    how a repo-puller tick that just advanced HEAD converges the fleet on that
    same tick (see the module docstring).
    """
    from . import repo_puller as _rp

    # Root has no deploy key and would litter the evolve-owned checkout with
    # root-owned files; same enforcement the `repo-pull` entrypoint applies.
    _rp.enforce_evolve_invocation()

    result = _rp.run_redeploy_sweep_only(Path(repo) if repo else _rp.DEFAULT_REPO)
    out = _rp.format_for_log(result, quiet=quiet)
    if out:
        print(out)
    sys.exit(0 if result.success else 1)


def register_cli(group) -> None:
    group.add_command(repo_redeploy_sweep)
