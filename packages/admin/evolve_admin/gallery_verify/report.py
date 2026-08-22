"""Rendering: a compact console table + a JSON artifact.

The console table is the operator's at-a-glance view; the JSON artifact is the
durable, machine-readable record (default ``{shared_dir}/gallery-verify/
<timestamp>.json``). Both derive from the same ``RunReport``.
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import (
    BLOCKED_OAUTH,
    FAIL,
    PASS,
    SKIPPED,
    AppResult,
    RunReport,
)

_STATUS_GLYPH = {
    PASS: "PASS ",
    FAIL: "FAIL ",
    BLOCKED_OAUTH: "OAUTH",
    SKIPPED: "SKIP ",
}


def _row(app: AppResult) -> str:
    glyph = _STATUS_GLYPH.get(app.status, app.status)
    # identity: see applications.app_identity.resolve_app_id — ``AppResult``
    # dataclass field (this harness's own report schema), used purely as a
    # console label when the package has no display name. Not a manifest read.
    name = (app.display_name or app.pkg_id)[:26].ljust(26)
    bot = app.bot_id[:14].ljust(14)
    step = (app.failed_step or "-")[:12].ljust(12)
    cov = f"{app.checks_run}✓"
    if app.checks_skipped:
        cov += f"/{app.checks_skipped}⊘"
    cov = cov.ljust(8)
    return f"  {glyph}  {name} {bot} {step} {cov} {app.summary}"


def render_console(report: RunReport) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append(
        f"Gallery verify — mode={report.mode} platform={report.pod_platform} "
        f"teardown={'on' if report.teardown else 'off'}"
    )
    lines.append(
        "  "
        + "STATUS".ljust(7)
        + "APP".ljust(27)
        + "BOT".ljust(15)
        + "FAILED-STEP".ljust(13)
        + "CHECKS".ljust(9)
        + "SUMMARY"
    )
    lines.append("  " + "-" * 92)
    for app in report.apps:
        lines.append(_row(app))
        # Under a FAIL, surface the failing checks for immediate triage.
        if app.status == FAIL:
            for st in app.steps:
                for c in st.checks:
                    if c.failed:
                        lines.append(f"        ↳ [{st.step}] {c.name}: {c.detail}")
    c = report.counts()
    lines.append("  " + "-" * 92)
    lines.append(
        f"  {c[PASS]} pass · {c[FAIL]} fail · {c[BLOCKED_OAUTH]} oauth-blocked · "
        f"{c[SKIPPED]} skipped   →  exit {report.exit_code()}"
    )
    lines.append("")
    return "\n".join(lines)


def default_artifact_path(shared_dir: Path, timestamp: str) -> Path:
    return Path(shared_dir) / "gallery-verify" / f"{timestamp}.json"


def write_artifact(report: RunReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=False), encoding="utf-8")
    tmp.replace(path)
    return path
