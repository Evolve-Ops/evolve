#!/usr/bin/env python3
# evolve: pkg=p-aab5e569 file=f-4d5e6f7a
"""commitment_tracker.py — EA Pack commitment tracker for {bot_id} workspace.

Writes commitments to memory/contacts/{person}.md and surfaces past-due ones.
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Optional

# ── Shared config utilities ───────────────────────────────────────────────────

SHARED_DIR = Path("/Users/Shared/evolve")
NETWORK_JSON = SHARED_DIR / "network.json"


def load_network() -> dict:
    if NETWORK_JSON.exists():
        try:
            return json.loads(NETWORK_JSON.read_text())
        except Exception:
            pass
    return {}


def get_workspace(bot_id: str) -> Path:
    oc_json = Path(f"/Users/{bot_id}/.openclaw/openclaw.json")
    if oc_json.exists():
        try:
            cfg = json.loads(oc_json.read_text())
            ws = cfg.get("agents", {}).get("defaults", {}).get("workspace")
            if ws:
                return Path(ws)
        except (json.JSONDecodeError, OSError):
            pass
    return Path(f"/Users/{bot_id}/.openclaw/workspace")


# ── Contact file helpers ──────────────────────────────────────────────────────

def person_filename(person: str) -> str:
    """Normalize person name to filename: lowercase, spaces → hyphens."""
    return person.strip().lower().replace(" ", "-") + ".md"


def append_commitment(contacts_dir: Path, person: str, commitment: str, due: Optional[str]) -> None:
    """Append a commitment entry to the person's contact file."""
    filename = person_filename(person)
    contact_file = contacts_dir / filename
    contacts_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    entry = f"- {today}: {commitment}"
    if due:
        entry += f" [due: {due}]"
    if not contact_file.exists():
        # Initialize with header
        header = f"# {person.title()}\n\n## Commitments\n\n"
        contact_file.write_text(header + entry + "\n")
    else:
        text = contact_file.read_text()
        # Insert after '## Commitments' header
        if "## Commitments" in text:
            text = text.replace(
                "## Commitments\n",
                f"## Commitments\n\n{entry}\n",
                1,
            )
            # Avoid double blank lines if header already had a blank
            text = text.replace("\n\n\n", "\n\n")
        else:
            text += f"\n## Commitments\n\n{entry}\n"
        contact_file.write_text(text)
    print(f"Commitment recorded for {person} in {contact_file}")


# ── List-due mode ─────────────────────────────────────────────────────────────

# Pattern: - YYYY-MM-DD: ... [due: YYYY-MM-DD]
_COMMITMENT_RE = re.compile(
    r"^- (\d{4}-\d{2}-\d{2}): (.+?) \[due: (\d{4}-\d{2}-\d{2})\]\s*$"
)


def list_due(contacts_dir: Path) -> None:
    """Scan all contact files and emit FOLLOWUP_DUE: lines for past-due commitments."""
    if not contacts_dir.exists():
        return
    today = date.today()
    for md_file in sorted(contacts_dir.glob("*.md")):
        # Derive person name from filename
        person = md_file.stem.replace("-", " ").title()
        try:
            text = md_file.read_text()
        except Exception:
            continue
        in_commitments = False
        for line in text.splitlines():
            if line.strip() == "## Commitments":
                in_commitments = True
                continue
            if line.startswith("## ") and line.strip() != "## Commitments":
                in_commitments = False
                continue
            if not in_commitments:
                continue
            # Skip struck-through (done) entries
            if line.startswith("~~"):
                continue
            m = _COMMITMENT_RE.match(line)
            if m:
                commitment_text = m.group(2)
                due_str = m.group(3)
                try:
                    due_date = date.fromisoformat(due_str)
                    if due_date < today:
                        print(f"FOLLOWUP_DUE: {person} | {commitment_text}")
                except ValueError:
                    pass


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="EA Pack commitment tracker")
    parser.add_argument("--bot", default="{bot_id}")
    parser.add_argument("--person", help="Person name for the commitment")
    parser.add_argument("--commitment", help="Commitment text")
    parser.add_argument("--due", help="Due date (YYYY-MM-DD)")
    parser.add_argument(
        "--list-due",
        action="store_true",
        help="List all past-due commitments across all contacts",
    )
    args = parser.parse_args()

    workspace = get_workspace(args.bot)
    contacts_dir = workspace / "memory" / "contacts"

    if args.list_due:
        list_due(contacts_dir)
        return

    if not args.person:
        print("ERROR: --person is required when not using --list-due", file=sys.stderr)
        sys.exit(1)
    if not args.commitment:
        print("ERROR: --commitment is required when not using --list-due", file=sys.stderr)
        sys.exit(1)

    append_commitment(contacts_dir, args.person, args.commitment, args.due)


if __name__ == "__main__":
    main()
