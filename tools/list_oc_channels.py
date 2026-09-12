#!/usr/bin/env python3
"""list_oc_channels.py — enumerate every messaging-channel surface OpenClaw ships.

Why this exists
---------------
The 2026-05-30 skills deep audit (internal/skills-deep-audit-2026-05-30.md) read only
`dist/channel-catalog.json` and concluded iMessage was unsupported by OC — so
the iMessage skill was withdrawn from the Evolve catalog. That was wrong:
`@openclaw/imessage` ships **bundled** in `dist/extensions/imessage/`, and so
do 6 other channels (clickclack, irc, mattermost, signal, sms, telegram).
The 2026-06-04 follow-up audit (internal/openclaw-coverage-audit-2026-06-04.md, F1
cross-cutting) caught the gap and mandated this tool.

Going forward, this script is the canonical OC-channel enumeration source. The
audit framework (internal/skills-deep-audit-2026-05-30.md §Method update) references
it, and the committed snapshot (docs/skills/oc-channel-coverage.json) is the
diff target — every OC version bump must re-run `--diff-against` and either
update the snapshot or document the new channel's status in the June 4 audit's
Bucket A/B/C tables.

Smoke-test commands
-------------------
    # Quick local count:
    $ tools/list_oc_channels.py --source=both | wc -l

    # Generate audit-doc table:
    $ tools/list_oc_channels.py --markdown-table > /tmp/channels.md

    # CI: error if OC ships a new channel we don't recognize:
    $ tools/list_oc_channels.py --json --diff-against=docs/skills/oc-channel-coverage.json

    # Remote (read from the live OC on the mini):
    $ tools/list_oc_channels.py --ssh mini

Exit codes
----------
0 — informational success (default), or `--diff-against` matched the snapshot
1 — `--diff-against` mismatch (snapshot drift; investigate)
2 — usage / IO error (bad path, OC bundle missing, malformed JSON)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

DEFAULT_OC_DIST = Path("/opt/homebrew/lib/node_modules/openclaw/dist")
CATALOG_FILENAME = "channel-catalog.json"
EXTENSIONS_DIRNAME = "extensions"


@dataclass
class Channel:
    """One OC channel surface (catalog entry or bundled extension)."""

    id: str
    label: str
    selection_label: Optional[str]
    source: str  # "catalog" | "extensions"
    npm_name: Optional[str]
    clawhub_spec: Optional[str]
    min_host_version: Optional[str]
    default_install: Optional[str]  # "npm" | "clawhub" | "bundled"
    bundled_in: Optional[str]  # "extensions/<dir>" when bundled, else None

    # Sortable key for stable output.
    @property
    def sort_key(self) -> tuple:
        return (self.id, self.source)


# ---------------------------------------------------------------------------
# I/O — local or remote (SSH)
# ---------------------------------------------------------------------------


class OcReader:
    """Reads the OC dist tree, either from a local path or via `ssh <host>`.

    Remote mode shells `ssh <host> cat <path>` and `ssh <host> ls <dir>` so we
    never need to mount the mini's filesystem or hold open a long session. The
    harness's SSH alias resolves the username; copy-paste commands shown to
    operators in messages should use the explicit `ssh <admin-user>@mini ...`
    form per CLAUDE.md conventions.
    """

    def __init__(self, oc_dist: Path, ssh_host: Optional[str]):
        self.oc_dist = oc_dist
        self.ssh_host = ssh_host

    def _run_ssh(self, remote_script: str) -> str:
        # `ssh host cmd...` joins argv with spaces and re-parses through the
        # remote login shell; pass a single quoted string so the remote shell
        # sees exactly one command. BatchMode prevents an interactive password
        # prompt from hanging CI.
        cmd = ["ssh", "-o", "BatchMode=yes", self.ssh_host or "", remote_script]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=30
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"ssh failed: ssh {self.ssh_host} '{remote_script}'\nstderr: {e.stderr}"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"ssh timed out: ssh {self.ssh_host} '{remote_script}'"
            ) from e
        return result.stdout

    def read_file(self, relative_path: str) -> Optional[str]:
        """Return file contents, or None if the file does not exist."""
        if self.ssh_host:
            remote_path = str(self.oc_dist / relative_path)
            # `test -f` short-circuits with `|| true` so an absent file returns
            # empty stdout rather than a non-zero exit.
            script = f"if [ -f {remote_path} ]; then cat {remote_path}; fi"
            out = self._run_ssh(script)
            return out if out else None
        local_path = self.oc_dist / relative_path
        if not local_path.is_file():
            return None
        return local_path.read_text()

    def list_dir(self, relative_path: str) -> list[str]:
        """Return directory entries (names only), or [] if the dir is absent."""
        if self.ssh_host:
            remote_path = str(self.oc_dist / relative_path)
            script = f"if [ -d {remote_path} ]; then ls -1 {remote_path}; fi"
            out = self._run_ssh(script)
            return [line for line in out.splitlines() if line]
        local_path = self.oc_dist / relative_path
        if not local_path.is_dir():
            return []
        return sorted(p.name for p in local_path.iterdir() if p.is_dir())


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def parse_catalog(json_text: str) -> list[Channel]:
    """Parse `channel-catalog.json` → list of Channel records."""
    data = json.loads(json_text)
    entries = data.get("entries", [])
    out: list[Channel] = []
    for entry in entries:
        oc = entry.get("openclaw") or {}
        ch = oc.get("channel") or {}
        install = oc.get("install") or {}
        # Catalog entries always have a channel block. Skip defensively.
        if "id" not in ch:
            continue
        # `name` is the npm/clawhub package name; for catalog entries the
        # default install determines whether it's an npm or clawhub fetch.
        npm_name = entry.get("name") if install.get("npmSpec") or install.get("defaultChoice") == "npm" else None
        clawhub_spec = install.get("clawhubSpec")
        out.append(Channel(
            id=ch["id"],
            label=ch.get("label", ch["id"]),
            selection_label=ch.get("selectionLabel"),
            source="catalog",
            npm_name=install.get("npmSpec") or npm_name,
            clawhub_spec=clawhub_spec,
            min_host_version=install.get("minHostVersion"),
            default_install=install.get("defaultChoice"),
            bundled_in=None,
        ))
    return out


def parse_extension_package(dir_name: str, json_text: str, include_non_channel: bool) -> Optional[Channel]:
    """Parse a bundled extension's `package.json` → Channel or None.

    Filter rule: by default, include only entries with an `openclaw.channel.id`
    block (the audit's reliable signature). Bridge-only extensions like
    `webhooks` and runtime-plugin-only extensions like `phone-control` /
    `talk-voice` (which use `openclaw.plugin.json` and have no channel shape)
    are skipped unless `--include-non-channel` is set.
    """
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return None
    oc = data.get("openclaw") or {}
    ch = oc.get("channel") or {}
    if "id" not in ch:
        if not include_non_channel:
            return None
        # Surface non-channel extensions with a stub Channel so completeness
        # audits can see them; the caller can filter on `source` if needed.
        return Channel(
            id=data.get("name", dir_name),
            label=data.get("description") or dir_name,
            selection_label=None,
            source="extensions",
            npm_name=data.get("name"),
            clawhub_spec=None,
            min_host_version=(oc.get("install") or {}).get("minHostVersion"),
            default_install="bundled",
            bundled_in=f"extensions/{dir_name}",
        )
    install = oc.get("install") or {}
    return Channel(
        id=ch["id"],
        label=ch.get("label", ch["id"]),
        selection_label=ch.get("selectionLabel"),
        source="extensions",
        npm_name=data.get("name"),
        clawhub_spec=None,
        min_host_version=install.get("minHostVersion"),
        default_install="bundled",
        bundled_in=f"extensions/{dir_name}",
    )


def read_extensions(reader: OcReader, include_non_channel: bool) -> tuple[list[Channel], list[str]]:
    """Return (channel records, warning messages)."""
    warnings: list[str] = []
    dirs = reader.list_dir(EXTENSIONS_DIRNAME)
    if not dirs:
        warnings.append(
            f"warning: no {EXTENSIONS_DIRNAME}/ directory under {reader.oc_dist} "
            "(some OC versions may not ship it); skipping bundled-channel scan"
        )
        return [], warnings
    channels: list[Channel] = []
    for d in sorted(dirs):
        pkg_text = reader.read_file(f"{EXTENSIONS_DIRNAME}/{d}/package.json")
        if pkg_text is None:
            continue
        ch = parse_extension_package(d, pkg_text, include_non_channel)
        if ch:
            channels.append(ch)
    return channels, warnings


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------


def render_human(channels: list[Channel]) -> str:
    lines = []
    by_source: dict[str, list[Channel]] = {"catalog": [], "extensions": []}
    for ch in channels:
        by_source.setdefault(ch.source, []).append(ch)
    for source in ("catalog", "extensions"):
        group = sorted(by_source.get(source, []), key=lambda c: c.id)
        if not group:
            continue
        title = "channel-catalog.json" if source == "catalog" else "dist/extensions/"
        lines.append(f"== {title} ({len(group)} channels) ==")
        for ch in group:
            install = (
                ch.default_install
                or ("npm" if ch.npm_name else None)
                or "?"
            )
            extras = []
            if ch.min_host_version:
                extras.append(f"requires OC {ch.min_host_version}")
            if ch.bundled_in:
                extras.append(ch.bundled_in)
            extra_str = f"  [{', '.join(extras)}]" if extras else ""
            lines.append(f"  {ch.id:<18} {ch.label:<20} install={install}{extra_str}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_json(channels: list[Channel]) -> str:
    # Sort for deterministic CI diffs.
    sorted_channels = sorted(channels, key=lambda c: (c.id, c.source))
    return json.dumps(
        {
            "schema_version": 1,
            "channels": [asdict(c) for c in sorted_channels],
        },
        indent=2,
        sort_keys=True,
    ) + "\n"


def render_markdown_table(channels: list[Channel]) -> str:
    sorted_channels = sorted(channels, key=lambda c: (c.source, c.id))
    lines = [
        "| Channel id | Label | Source | Install | Bundled in / NPM | Min OC version |",
        "|---|---|---|---|---|---|",
    ]
    for ch in sorted_channels:
        install = ch.default_install or ("npm" if ch.npm_name else "?")
        bundled_or_npm = ch.bundled_in or ch.clawhub_spec or ch.npm_name or ""
        lines.append(
            f"| `{ch.id}` | {ch.label} | {ch.source} | {install} | "
            f"{bundled_or_npm} | {ch.min_host_version or ''} |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Diff against snapshot
# ---------------------------------------------------------------------------


def diff_against_snapshot(current: list[Channel], snapshot_path: Path) -> tuple[bool, str]:
    """Return (matches, message). `matches=False` means CI should fail."""
    if not snapshot_path.is_file():
        return False, f"snapshot file not found: {snapshot_path}"
    try:
        snapshot = json.loads(snapshot_path.read_text())
    except json.JSONDecodeError as e:
        return False, f"snapshot is not valid JSON ({snapshot_path}): {e}"
    snapshot_channels = snapshot.get("channels", [])
    current_dicts = sorted(
        (asdict(c) for c in current),
        key=lambda d: (d["id"], d["source"]),
    )
    snapshot_sorted = sorted(
        snapshot_channels,
        key=lambda d: (d.get("id", ""), d.get("source", "")),
    )
    if current_dicts == snapshot_sorted:
        return True, f"OK: {len(current_dicts)} channels match snapshot"

    # Build human-readable diff summary.
    current_keys = {(d["id"], d["source"]) for d in current_dicts}
    snapshot_keys = {(d.get("id"), d.get("source")) for d in snapshot_sorted}
    added = sorted(current_keys - snapshot_keys)
    removed = sorted(snapshot_keys - current_keys)
    changed: list[str] = []
    snap_by_key = {(d.get("id"), d.get("source")): d for d in snapshot_sorted}
    cur_by_key = {(d["id"], d["source"]): d for d in current_dicts}
    for key in sorted(current_keys & snapshot_keys):
        if cur_by_key[key] != snap_by_key[key]:
            changed.append(f"{key[0]} ({key[1]})")

    parts = ["MISMATCH against snapshot:"]
    if added:
        parts.append(f"  added:   {', '.join(f'{i} ({s})' for i, s in added)}")
    if removed:
        parts.append(f"  removed: {', '.join(f'{i} ({s})' for i, s in removed)}")
    if changed:
        parts.append(f"  changed: {', '.join(changed)}")
    parts.append("")
    parts.append(
        "To accept the change: regenerate the snapshot with\n"
        "  tools/list_oc_channels.py --json --source=both > docs/skills/oc-channel-coverage.json\n"
        "AND either add an install module (packages/admin/evolve_admin/skills/<id>_install.py)\n"
        "OR document the skip in internal/openclaw-coverage-audit-2026-06-04.md Bucket C."
    )
    return False, "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enumerate every OpenClaw messaging channel (catalog + bundled extensions).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--source",
        choices=("catalog", "extensions", "both"),
        default="both",
        help="Which OC source(s) to enumerate (default: both).",
    )
    parser.add_argument(
        "--oc-dist",
        type=Path,
        default=DEFAULT_OC_DIST,
        help=f"Path to OC's dist/ directory (default: {DEFAULT_OC_DIST}).",
    )
    parser.add_argument(
        "--ssh",
        metavar="HOST",
        default=None,
        help="Read OC files over `ssh <host>` instead of from local disk "
             "(e.g. --ssh mini). The harness resolves the user automatically; "
             "if you copy this command to a shell, write `ssh <admin-user>@mini ...`.",
    )
    parser.add_argument(
        "--include-non-channel",
        action="store_true",
        help="Also include bundled extensions that have no openclaw.channel.id "
             "block (bridges, voice runtimes). Off by default — only channel-shaped "
             "extensions are emitted.",
    )
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="Emit JSON (machine-readable).")
    fmt.add_argument("--markdown-table", action="store_true", help="Emit a Markdown table.")
    parser.add_argument(
        "--diff-against",
        type=Path,
        metavar="PATH",
        help="Compare the enumeration to a committed snapshot JSON. "
             "Exits 1 on mismatch (intended for CI).",
    )

    args = parser.parse_args(argv)

    reader = OcReader(args.oc_dist, args.ssh)
    channels: list[Channel] = []
    warnings: list[str] = []

    if args.source in ("catalog", "both"):
        catalog_text = reader.read_file(CATALOG_FILENAME)
        if catalog_text is None:
            warnings.append(
                f"warning: {CATALOG_FILENAME} not found under {args.oc_dist}"
            )
        else:
            try:
                channels.extend(parse_catalog(catalog_text))
            except json.JSONDecodeError as e:
                print(f"error: {CATALOG_FILENAME} is not valid JSON: {e}", file=sys.stderr)
                return 2

    if args.source in ("extensions", "both"):
        ext_channels, ext_warnings = read_extensions(reader, args.include_non_channel)
        channels.extend(ext_channels)
        warnings.extend(ext_warnings)

    # `--diff-against` short-circuits all human/json output and uses the
    # diff result as the exit status.
    if args.diff_against is not None:
        matches, message = diff_against_snapshot(channels, args.diff_against)
        print(message)
        for w in warnings:
            print(w, file=sys.stderr)
        return 0 if matches else 1

    if args.json:
        sys.stdout.write(render_json(channels))
    elif args.markdown_table:
        sys.stdout.write(render_markdown_table(channels))
    else:
        sys.stdout.write(render_human(channels))

    for w in warnings:
        print(w, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
