#!/usr/bin/env python3
"""serve — boot the real admin server against a fixture pod.

Builds the pod if it isn't there, then execs ``evolve_admin.web.run`` with the
environment the fixture needs:

* ``EVOLVE_FIXTURE_POD_ROOT`` — read by the sibling ``sitecustomize`` to pin
  the platform profile at the fixture root;
* ``PYTHONPATH`` — this directory (so ``sitecustomize`` is importable) ahead
  of the two package dirs;
* ``EVOLVE_ADMIN_AUTH_DISABLED`` — the same test-only escape the browser smoke
  harness uses; the fixture never pairs a device.

Usage::

    python3 -m tests.fixtures.pod.serve --root /tmp/fixture-pod --port 5099

Everything the server then does — resolving bot homes, listing manifests,
reading the usage rollup — runs the product's own code against the fixture's
real files.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent.parent
ADMIN_DIR = REPO_ROOT / "packages" / "admin"
ANALYZER_DIR = REPO_ROOT / "packages" / "analyzer"


def fixture_env(root: Path, base: dict | None = None) -> dict:
    """The environment a process needs to see the fixture pod as its pod."""
    env = dict(base if base is not None else os.environ)
    env["EVOLVE_FIXTURE_POD_ROOT"] = str(root)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(HERE), str(ADMIN_DIR), str(ANALYZER_DIR)]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env["EVOLVE_ADMIN_AUTH_DISABLED"] = "1"
    env.setdefault("EVOLVE_LOG_LEVEL", "WARNING")
    return env


def main() -> int:
    ap = argparse.ArgumentParser(description="Serve the admin UI over a fixture pod")
    ap.add_argument("--root", required=True)
    ap.add_argument("--port", type=int, default=5099)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--rebuild", action="store_true", help="Rebuild the pod first")
    args = ap.parse_args()

    root = Path(args.root)
    if args.rebuild or not (root / "shared" / "network.json").exists():
        try:
            from . import build as build_mod
        except ImportError:
            sys.path.insert(0, str(HERE))
            import build as build_mod  # type: ignore

        print(json.dumps(build_mod.build(root), indent=2), file=sys.stderr)

    env = fixture_env(root)
    cmd = [
        sys.executable, "-m", "evolve_admin.web.run",
        "--host", args.host, "--port", str(args.port),
        "--network", str(root / "shared" / "network.json"),
    ]
    print(f"[fixture-pod] {' '.join(cmd)}", file=sys.stderr)
    return subprocess.call(cmd, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
