"""evolve_admin.skills.apple_local_install — Apple local-apps skill.

.. note::
   **WITHDRAWN from catalog 2026-05-30** (Phase 1c of the deep skills
   audit; see ``internal/skills-deep-audit-2026-05-30.md``). Classic
   probe-is-the-only-consumer dead-end:
     - NO bot has any plugin/mcp.servers/channels entry for
       Contacts/Calendar/Reminders/Notes (verified live on all 7 pod bots).
     - OC bundle has no apple-* extension; ``packages/plugin/src`` has
       zero AppleScript wrappers.
     - TCC grants would land for ``evolve`` admin user but bots run as
       team_bot_a/team_bot_c/personal_bot_user/etc. — wrong user.
   The probe functions stay reachable for future use; the catalog
   surface + pod-wide matrix entry + server.py dispatchers were removed.
   Re-add via apple-mcp-server (per-bot via InstallMcpServer, with
   per-bot-user TCC grants) OR by adding Apple tool surfaces in
   ``packages/plugin/src`` using osascript wrappers.

Apple Contacts, Calendar, Reminders, and Notes are the native-Mac
counterparts of "who is this, what's on my plate, what do I owe people,
and where did I jot that down." Adding them as a single installable
skill lets bots resolve contact names from a number, list today's
events, surface what's due, and pull notes by title or content — the
macOS-native counterpart to the existing Google Workspace skill (which
only works for GOG-signed-in users).

This is a single skill rather than four, so the user grants once and
gets the whole bundle. One card on the catalog, one TCC pass, no
proliferation of near-identical skills.

Auth shape (TCC consent, not OAuth)
------------------------------------
Like iMessage, this is gated entirely by macOS TCC — the per-app
permission system in System Settings → Privacy & Security. Users grant
the running bot user permission to automate each of the four Apple apps
under Automation:

  * Contacts   — read address book entries (names, phone, email, birthdays).
  * Calendar   — list events on local calendars.
  * Reminders  — read and surface due items from local Reminders lists.
  * Notes      — read note titles and contents from local accounts.

No tokens, no OAuth callback, no config file. The "install state" IS the
TCC grant state — we probe with benign AppleScript ``count`` calls that
have zero side effects.

State machine
-------------

    active                 — all four toggles granted.
    needs_tcc              — at least one toggle missing; ``missing``
                             carries the list of unfilled toggles so the
                             modal can name them precisely.
    unknown                — any probe raised; ``error`` has the detail.

We deliberately collapsed the previous combinatorial states
(``needs_contacts_tcc`` / ``needs_calendar_tcc`` / ``needs_both_tcc``)
into a single ``needs_tcc`` with a ``missing[]`` list — 4 apps means 15
non-active combinations, too many for distinct status strings, and the
UI cares only about "installed vs not" plus the instruction text. The
JS catalog button logic was updated to treat ``needs_tcc`` as a
finish-setup state alongside the OAuth ones.

Local-only privacy guarantee
----------------------------
Nothing leaves the machine. AppleScript runs each app under the bot
user's session; the bot reads what the user has chosen to let it see.
Revocation: flip the toggle off in System Settings — the next status
probe surfaces the change.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


# ── Skill identifier ──────────────────────────────────────────────────────────

APPLE_LOCAL_SKILL_ID = "apple_local"
APPLE_LOCAL_SKILL_KIND = "local_system"

#: How long to wait for an AppleScript probe before declaring the check failed.
APPLE_PROBE_TIMEOUT_S = 5


# ── Apps covered by this skill ────────────────────────────────────────────────

# Each entry pairs the AppleScript application name with the ``count`` target
# used by the probe and the user-facing toggle label as it appears in System
# Settings → Privacy & Security → Automation. Adding a new Apple app means
# adding a row here — every state computation drives off this list.
_APPS: list[dict[str, str]] = [
    {"key": "contacts",  "app": "Contacts",  "count": "every person",     "toggle": "Contacts"},
    {"key": "calendar",  "app": "Calendar",  "count": "every calendar",   "toggle": "Calendar"},
    {"key": "reminders", "app": "Reminders", "count": "every list",       "toggle": "Reminders"},
    {"key": "notes",     "app": "Notes",     "count": "every note",       "toggle": "Notes"},
]

_APP_KEYS = tuple(a["key"] for a in _APPS)


# ── Plain-language access panel ───────────────────────────────────────────────

APPLE_LOCAL_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": APPLE_LOCAL_SKILL_ID,
    "skill_display_name": "Apple (Contacts, Calendar, Reminders, Notes)",
    "summary": (
        "Lets this bot read your Mac's Contacts, Calendar, Reminders, and "
        "Notes — names, phone numbers, birthdays, today's events, what's "
        "due, and notes you've jotted down. One permission, four apps."
    ),
    "will": [
        "Read entries from your Mac's Contacts app",
        "List events on your local calendars (today, this week)",
        "Surface what's due from your Reminders lists",
        "Read note titles and contents from your local Notes accounts",
        "Tell you whose birthday is coming up",
    ],
    "wont": [
        "Add, change, or delete contacts, events, reminders, or notes",
        "See data on other Apple IDs or other Macs",
        "Send any of your Apple-app data outside this bot's machine",
        "Touch iCloud sync settings",
    ],
    "where_credentials_live": (
        "There are no passwords or keys for this skill — it uses macOS's "
        "built-in permission system. You can revoke at any time from "
        "System Settings → Privacy & Security → Automation by turning "
        "off any of the toggles."
    ),
}


# ── Install status ─────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of TCC grant state across the four Apple apps.

    status:
      ``active``    — all four toggles granted.
      ``needs_tcc`` — at least one toggle missing; ``missing`` lists the
                      unfilled toggle keys ("contacts" / "calendar" /
                      "reminders" / "notes").
      ``unknown``   — any probe raised; ``error`` has the detail.
    """

    bot_id: str
    status: str
    granted: dict[str, bool] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "bot_id": self.bot_id,
            "skill_id": APPLE_LOCAL_SKILL_ID,
            "kind": APPLE_LOCAL_SKILL_KIND,
            "status": self.status,
            "granted": dict(self.granted),
            "missing": list(self.missing),
            "error": self.error,
        }
        # Back-compat alias keys consumed by existing UI / tests that
        # reference contacts_granted / calendar_granted from the v1 shape.
        d["contacts_granted"] = self.granted.get("contacts", False)
        d["calendar_granted"] = self.granted.get("calendar", False)
        return d


# ── Install plan ──────────────────────────────────────────────────────────────


@dataclass
class InstallStep:
    """One step the UI drives. Apple-local steps are always informational —
    we cannot grant TCC programmatically; the user must click through
    System Settings. The modal's ``manual_setup`` step renderer shows the
    label as the user-facing instruction."""

    id: str
    label: str
    endpoint: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    access_panel: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "endpoint": self.endpoint,
            "payload": dict(self.payload),
            "access_panel": self.access_panel,
        }


def _instruction_for(missing: list[str]) -> str:
    """Build the System-Settings instruction naming only the missing toggles.

    Uses the per-app toggle labels (as they appear in System Settings) so
    the user can match against the UI literally. Joins with ", " plus a
    serial-Oxford "and" for the last item — the Plex-test crowd reads
    serial commas naturally; cute punctuation hides the action."""
    toggle_labels = [
        next((a["toggle"] for a in _APPS if a["key"] == k), k.title())
        for k in missing
    ]
    if len(toggle_labels) == 1:
        toggles_phrase = f"the toggle for {toggle_labels[0]}"
    elif len(toggle_labels) == 2:
        toggles_phrase = f"toggles for {toggle_labels[0]} and {toggle_labels[1]}"
    else:
        toggles_phrase = (
            "toggles for "
            + ", ".join(toggle_labels[:-1])
            + f", and {toggle_labels[-1]}"
        )
    return (
        "Open System Settings → Privacy & Security → Automation. Find the "
        f"evolve service (or your shell) and turn on {toggles_phrase}."
    )


def build_install_plan(status: InstallStatus) -> list[InstallStep]:
    """Return the steps the user needs to take.

    Active or unknown → empty plan (UI handles those branches). Otherwise
    a single ``manual_setup`` step whose label names exactly the missing
    toggles, plus a ``confirm`` step that polls status."""
    if status.status in ("active", "unknown"):
        return []
    if not status.missing:
        # Defensive: if we somehow got needs_tcc with an empty missing list,
        # fall back to the all-four instruction rather than emitting nothing.
        missing = list(_APP_KEYS)
    else:
        missing = status.missing

    return [
        InstallStep(
            id="manual_setup",
            label=_instruction_for(missing),
            access_panel=dict(APPLE_LOCAL_ACCESS_PANEL),
        ),
        InstallStep(
            id="confirm",
            label="Confirm the bot can read your Apple apps",
            endpoint=f"/api/skills/install/{APPLE_LOCAL_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        ),
    ]


# ── TCC probes ────────────────────────────────────────────────────────────────


def _probe_apple_app(app_name: str, count_target: str) -> bool:
    """Run a benign osascript against *app_name* and return True if it works.

    The check has zero side effects: ``count`` returns the number of
    contacts / calendars / lists / notes without changing or surfacing
    anything. A return code of 0 means TCC granted; non-zero (typically
    -1743 in the error message) means denied or not yet prompted.

    Subprocess failures (missing osascript, timeout, etc.) return False —
    "we couldn't verify the grant" UX-wise looks like "not granted yet."
    """
    try:
        r = subprocess.run(
            ["osascript", "-e",
             f'tell application "{app_name}" to return (count of {count_target})'],
            capture_output=True, text=True, timeout=APPLE_PROBE_TIMEOUT_S,
        )
        return r.returncode == 0
    except FileNotFoundError:
        log.debug("apple_local_install: osascript not found")
        return False
    except subprocess.TimeoutExpired:
        log.debug("apple_local_install: probe timed out for %s", app_name)
        return False
    except Exception as exc:
        log.debug("apple_local_install: probe error for %s: %s", app_name, exc)
        return False


def probe_contacts_tcc() -> bool:
    """True if the running process can read Contacts via AppleScript."""
    return _probe_apple_app("Contacts", "every person")


def probe_calendar_tcc() -> bool:
    """True if the running process can read Calendar via AppleScript."""
    return _probe_apple_app("Calendar", "every calendar")


def probe_reminders_tcc() -> bool:
    """True if the running process can read Reminders via AppleScript."""
    return _probe_apple_app("Reminders", "every list")


def probe_notes_tcc() -> bool:
    """True if the running process can read Notes via AppleScript."""
    return _probe_apple_app("Notes", "every note")


# ── Status resolver ───────────────────────────────────────────────────────────


def resolve_status(
    bot_id: str,
    *,
    check_contacts:  Callable[[], bool] | None = None,
    check_calendar:  Callable[[], bool] | None = None,
    check_reminders: Callable[[], bool] | None = None,
    check_notes:     Callable[[], bool] | None = None,
) -> InstallStatus:
    """Resolve the TCC grant state across all four Apple apps.

    Every probe is injectable so tests don't need a real macOS session.
    If any probe raises, ``status='unknown'`` so the UI can surface the
    error rather than silently treating that app as missing.

    The defaults reference the module-level probe functions directly (not
    through a dict captured at import) so unittest.mock.patch.object on
    those names works as expected — a captured dict would freeze the
    original references and silently bypass patches.
    """
    probes: dict[str, Callable[[], bool]] = {
        "contacts":  check_contacts  or probe_contacts_tcc,
        "calendar":  check_calendar  or probe_calendar_tcc,
        "reminders": check_reminders or probe_reminders_tcc,
        "notes":     check_notes     or probe_notes_tcc,
    }

    granted: dict[str, bool] = {}
    for key in _APP_KEYS:
        try:
            granted[key] = bool(probes[key]())
        except Exception as exc:
            return InstallStatus(
                bot_id=bot_id, status="unknown",
                granted=granted,
                error=f"{key}_probe_failed: {exc.__class__.__name__}: {exc}",
            )

    missing = [k for k in _APP_KEYS if not granted[k]]
    status_str = "active" if not missing else "needs_tcc"
    return InstallStatus(
        bot_id=bot_id,
        status=status_str,
        granted=granted,
        missing=missing,
    )


# ── Skill registry entry ──────────────────────────────────────────────────────


SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": APPLE_LOCAL_SKILL_ID,
    "kind": APPLE_LOCAL_SKILL_KIND,
    "display_name": APPLE_LOCAL_ACCESS_PANEL["skill_display_name"],
    "summary": APPLE_LOCAL_ACCESS_PANEL["summary"],
    "access_panel": dict(APPLE_LOCAL_ACCESS_PANEL),
}
