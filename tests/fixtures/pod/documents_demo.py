#!/usr/bin/env python3
"""documents_demo — install the Documents app on the fixture pod and show the saving.

Two things the artifact-by-reference brief asks to see, produced from the
real fixture pod by the real install spine and the real app script — not
typed by hand:

  1. **The install report.** ``install_gallery_pack("p-38da680b", "personal-bot")``
     through AL-3.2: what was seeded, what landed, the 1.5c proof, the
     AGENTS.md section.
  2. **A before/after transcript excerpt.** The same three-turn job — write an
     itinerary, change day 2, change the hotel price — done the conversational
     way (the assistant re-emits the whole document every turn) and the
     Documents-app way (the assistant relays the script's summary + path).
     Token counts are ``bytes // 4``, the same estimate
     ``analyzer/install_cost_estimator`` uses; they are an ESTIMATE, labelled
     as one, and only the ratio between the two columns is the claim.

The "before" column is not a recording of a bot; it is the document text
the conversational habit sends, counted. The "after" column IS what the
script prints, because the guidance tells the bot to relay exactly that.

Usage::

    python3 tests/fixtures/pod/documents_demo.py --root /tmp/fixture-pod [--markdown]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent.parent
sys.path.insert(0, str(_REPO / "packages" / "admin"))
sys.path.insert(0, str(_REPO / "packages" / "analyzer"))

PKG_ID = "p-38da680b"
BOT = "personal-bot"
CHARS_PER_TOKEN = 4

ITINERARY = """# Lisbon long weekend

## Day 1 — Friday

- 09:40 depart SFO on TP 218, arrive LIS 06:15 the next morning
- Check in at the hotel in Alfama; walk the Alfama stairs before the heat
- Dinner at a tasca in the Baixa (no reservations, arrive before seven)
- Nightcap at a fado house if the legs still work after the flight

## Day 2 — Saturday

1. Belém: the monastery at opening (10:00), before the tour groups
2. Pastéis de Belém — buy a dozen, eat three on the wall by the river
3. LX Factory for lunch, then the MAAT museum on the waterfront
4. Tram back along the river; dinner in Cais do Sodré

## Day 3 — Sunday

Free morning. Afternoon tram 28 loop from Martim Moniz, sunset at the
Miradouro da Senhora do Monte, and a slow dinner in Graça with the
neighbourhood regulars, then an early night before the flight home.

| Item | Cost |
|---|---|
| Flights | $1,240 |
| Hotel (3 nights) | $690 |
| Food and transit | $400 |

## Practicalities

Lisbon runs on cash in the small places and cards everywhere else; keep
twenty euros of coins for trams and the miradouro kiosks. The metro from the
airport takes forty minutes and costs less than two euros; a taxi is about
twenty and takes half the time at six in the morning. Sunday most museums
are free before two, which is why Belém is on Saturday and the tram loop is
on Sunday. Pack a light layer for the evenings — the river wind is real.

## Notes

> Bring the good walking shoes — Alfama is all hills and cobbles.

```
TP 218 confirmation: ABC123
```
"""

DAY2_REVISED = ("Belém first thing: the monastery at opening, then the pastry shop.\n\n"
                "Lunch at LX Factory; MAAT after. Fado in the evening (book ahead).\n")


def _load_build():
    spec = importlib.util.spec_from_file_location("_fixture_pod_build", _HERE / "build.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _tokens(text: str) -> int:
    return max(1, len(text.encode("utf-8")) // CHARS_PER_TOKEN)


def _run(ws: Path, *args: str, stdin: str = "") -> str:
    r = subprocess.run([sys.executable, "scripts/documents.py", *args], cwd=ws,
                       input=stdin, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise SystemExit(f"documents.py {' '.join(args)} failed: {r.stderr}")
    return r.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", required=True, help="fixture pod root (built here if absent)")
    parser.add_argument("--markdown", action="store_true", help="print PR-ready markdown")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    import platform_profile

    build = _load_build()
    if not (root / "shared" / "network.json").exists():
        build.build(root)
    platform_profile.set_profile(replace(
        platform_profile.get_profile(),
        user_home_root=str(root / "homes"),
        shared_dir_default=str(root / "shared"),
        scratch_dir=str(root / "scratch"),
    ))
    network = json.loads((root / "shared" / "network.json").read_text())
    shared = root / "shared"
    ws = root / "homes" / BOT / ".openclaw" / "workspace"

    from evolve_admin.applications.gallery_pack_install import install_gallery_pack

    result = install_gallery_pack(PKG_ID, BOT, shared_dir=shared, network=network, dry_run=False)
    if not result.get("ok"):
        print(json.dumps(result, indent=2, default=str))
        return 1

    # ── The job, the Documents-app way: what the script prints IS the reply ──
    after_turns = [
        ("write me a 3-day Lisbon itinerary",
         _run(ws, "new", "lisbon", "--from", "-", stdin=ITINERARY)),
        ("change day 2 to start at the monastery, then the pastry shop; fado in the evening",
         _run(ws, "edit", "lisbon", "--section", "Day 2 — Saturday", "--body-file", "-",
              stdin=DAY2_REVISED)),
        ("the hotel is $720 now, not $690",
         _run(ws, "edit", "lisbon", "--find", "$690", "--replace", "$720")),
        ("send it to me as a PDF",
         _run(ws, "render", "lisbon")),
    ]
    source_now = (ws / "docs" / "lisbon.md").read_text()

    # ── The same job the conversational way: the whole document, every turn ──
    before_turns = [
        (after_turns[0][0], ITINERARY),
        (after_turns[1][0], _apply_day2(ITINERARY)),
        (after_turns[2][0], _apply_day2(ITINERARY).replace("$690", "$720")),
        (after_turns[3][0], source_now + "\n(…and the same text again, formatted for the PDF)\n"),
    ]

    inst = result["install"]
    sec = result["sections"][0]
    if args.markdown:
        print("### Install report (fixture pod, `install_gallery_pack` → AL-3.2 `install_app_to_bot`)\n")
        print(f"- app_id `{result['app_id']}` ← gallery `{PKG_ID}` → bot `{BOT}`")
        print(f"- pack seeded at `{_rel(result['seed']['pack_dir'], root)}` "
              f"(sha `{result['seed']['pack_sha256'][:12]}…`), Spec at "
              f"`{_rel(result['seed']['spec_path'], root)}` (spec_version {result['seed'].get('spec_version')})")
        print(f"- files landed: {', '.join('`' + p + '`' for p in inst['installed'])}; failed: {inst['failed']}")
        print(f"- proof: realized digests explained by declared substitution = **{inst['proof']['explained']}** "
              f"(unexplained: {inst['proof']['unexplained']})")
        print(f"- instance: `{_rel(inst['manifest_path'], root)}`")
        print(f"- section: `{sec['artifact']}` — "
              f"{'written' if sec['ok'] else 'FAILED'} ({sec['bytes']} bytes, marker `pkg={PKG_ID}`)")
        print(f"- menu: `{_rel(result['installed_apps_md'], root) if result['installed_apps_md'] else '(not regenerated)'}`\n")
        print("### Before / after (same 4-turn job; tokens = bytes ÷ 4, an estimate)\n")
        print("| turn | user says | before: assistant output | tokens | after: assistant output | tokens |")
        print("|---|---|---|---|---|---|")
        tb = ta = 0
        for n, ((ask, before), (_, after)) in enumerate(zip(before_turns, after_turns), start=1):
            b, a = _tokens(before), _tokens(after)
            tb += b
            ta += a
            print(f"| {n} | {ask} | whole document ({len(before.splitlines())} lines) | {b} | "
                  f"{_cell(after)} | {a} |")
        print(f"| **total** | | | **{tb}** | | **{ta}** |")
        print(f"\nOutput tokens across the job: **{tb} → {ta}** ({tb / max(ta, 1):.1f}× fewer), "
              f"before the input-side saving (each revision turn no longer carries the previous "
              f"full document as context either).\n")
        print("Verbatim `after` replies (what the bot relays):\n")
        for ask, after in after_turns:
            print(f"> **{ask}**\n>\n" + "\n".join(f"> {line}" for line in after.rstrip().splitlines()) + "\n")
        return 0

    print(json.dumps({"install": result, "before": before_turns, "after": after_turns},
                     indent=2, default=str))
    return 0


def _apply_day2(text: str) -> str:
    head, rest = text.split("## Day 2 — Saturday\n\n", 1)
    _old, tail = rest.split("\n## Day 3", 1)
    return head + "## Day 2 — Saturday\n\n" + DAY2_REVISED + "\n## Day 3" + tail


def _rel(path: str, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except ValueError:
        return path


def _cell(text: str) -> str:
    lines = [line for line in text.rstrip().splitlines() if line.strip()]
    shown = lines[0] if len(lines) == 1 else f"{lines[0][:60]}… (+{len(lines) - 1} lines)"
    return shown.replace("|", "\\|")


if __name__ == "__main__":
    sys.exit(main())
