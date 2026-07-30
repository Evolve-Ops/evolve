"""mcp.allowlist — pod-wide MCP-server allowlist.

Spec: docs/spec-mcp-administration-2026-05-10.md §3.2 (BotAllowlist) +
§3.4 (allowlist file path).

Phase A ships a single pod-wide allowlist at
``{shared_dir}/policy/mcp-allowlist.json``. Per-bot allowlists land in
Phase B alongside the proposal-driven install flow. Today the live
pod has zero MCP servers on every bot, so the allowlist is empty by
default — matching reality, signals stay silent on day one.

In addition to the operator-curated on-disk allowlist, the module
ships a built-in ``EVOLVE_SHIPPED_ENTRIES`` set covering MCP servers
that are part of Evolve itself — e.g. evo's ``evo_tools`` server,
introduced by the evo-account-separation work (see
project_evo_account_separation in memory + the post-separation spec).
These are always known-good no matter what's on disk; if the operator
chooses to remove them, that's a separate decision the operator-level
allowlist mechanism doesn't currently support (and shouldn't — they're
part of the platform). Before 2026-06-04, mcp_monitor fired an
"unknown MCP server" alert on every bot running an Evolve-shipped MCP,
which produced false alarms on the primary bot from day one of the
account-separation deploy.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ── On-disk locations ─────────────────────────────────────────────────────────

def allowlist_path(shared_dir: Path) -> Path:
    return shared_dir / "policy" / "mcp-allowlist.json"


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class AllowlistEntry:
    """One approved (bot, server) pairing.

    Phase A keeps this minimal — Phase B fills in catalog_id,
    catalog_fingerprint, env_bindings, approved_at, approved_via_proposal.
    """

    name: str
    bot_id: str  # "*" means pod-wide-applies-to-all (reserved; not used in v1)
    config_signature: str | None  # expected signature; None means "any shape OK for now"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Allowlist:
    """Pod-wide allowlist of approved MCP server bindings."""

    version: int = 1
    entries: list[AllowlistEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "entries": [e.to_dict() for e in self.entries],
        }

    def entry_for(self, bot_id: str, server_name: str) -> AllowlistEntry | None:
        """Return the allowlist entry covering (bot_id, server_name), if any.

        Lookup order:
          1. Operator-curated entries (this Allowlist's ``entries``)
          2. Evolve-shipped entries (``EVOLVE_SHIPPED_ENTRIES``)

        Pod-wide (bot_id="*") entries match every bot.
        """
        for e in self.entries:
            if e.name == server_name and e.bot_id in (bot_id, "*"):
                return e
        for e in EVOLVE_SHIPPED_ENTRIES:
            if e.name == server_name and e.bot_id in (bot_id, "*"):
                return e
        return None


# Evolve-shipped MCP server allowlist. These bindings are part of the
# Evolve platform itself; the monitor must always treat them as known-
# good, independent of whether the operator-curated on-disk allowlist
# has been populated yet. Pod-wide (bot_id="*") because the same MCP
# might land on the primary or on other bots during account-separation
# rollouts. Add new entries here when shipping a new Evolve-internal
# MCP — never via the operator on-disk file (which is for third-party /
# operator-installed servers).
EVOLVE_SHIPPED_ENTRIES: tuple[AllowlistEntry, ...] = (
    # evo's privileged-ops MCP — see project_evo_account_separation in
    # memory + spec-evo-account-separation-2026-05-25.md. evo runs as
    # its own `evo` macOS user and reaches admin-side state via this
    # MCP over a unix socket (no sudo, no broad ACLs).
    AllowlistEntry(
        name="evo_tools",
        bot_id="*",
        config_signature=None,
        notes=(
            "Evolve-shipped: evo's privileged-ops MCP over the "
            "admin-daemon unix socket. Part of the evo-account-"
            "separation architecture (2026-05-30). Operator-allowlist "
            "edits are not required for Evolve-shipped MCPs."
        ),
    ),
)


# ── Read / write ──────────────────────────────────────────────────────────────

def load(shared_dir: Path) -> Allowlist:
    """Load the pod-wide allowlist; return an empty default if absent."""
    path = allowlist_path(shared_dir)
    if not path.exists():
        return Allowlist()
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return Allowlist()
    entries = []
    for e in raw.get("entries") or []:
        if not isinstance(e, dict):
            continue
        entries.append(
            AllowlistEntry(
                name=str(e.get("name") or ""),
                bot_id=str(e.get("bot_id") or ""),
                config_signature=e.get("config_signature"),
                notes=str(e.get("notes") or ""),
            )
        )
    return Allowlist(
        version=int(raw.get("version") or 1),
        entries=[e for e in entries if e.name and e.bot_id],
    )


def write_default_if_missing(shared_dir: Path) -> bool:
    """Create an empty allowlist file if none exists.

    Returns True if a file was written; False if one already existed.
    The empty default matches today's pod state (zero MCP servers on
    any bot), so this is the right v1 baseline.
    """
    path = allowlist_path(shared_dir)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = Allowlist().to_dict()
    payload["_comment"] = (
        "Pod-wide MCP server allowlist. Phase A of "
        "docs/spec-mcp-administration-2026-05-10.md. "
        "Empty by default — matches today's live pod state. "
        "Edits flow through the UpdateMcpAllowlist proposal kind (Phase B); "
        "manual edits work too but bypass the audit trail."
    )
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)
    return True
