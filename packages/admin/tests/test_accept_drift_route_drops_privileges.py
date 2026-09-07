"""Regression guard for the 2026-06-04 ``Accept as baseline`` EACCES.

The admin UI's ``Accept as baseline`` button POSTs to
``/api/security/accept-drift``. The original handler called
``backup.commit_baseline_local`` in-process. That worked while
``/Users/<bot>/.openclaw/workspace/.git/`` was root-owned (pre-2026-05-30,
when deploy_bot wrote it as root). After the 2026-05-30 fix that workspace
``.git/`` is bot-owned; the admin daemon (running as ``evolve``) then
EACCES'd on ``.git/index.lock`` and the operator saw:

    Failed: commit failed: fatal: Unable to create
    '/Users/<bot>/.openclaw/workspace/.git/index.lock': Permission denied

The fix mirrors ``deploy._baseline_refresh_as_bot_user``: shell out to
``backup.py --commit-baseline-local <bot>`` under
``sudo -H -u <bot_user>`` so the resulting commit + lock file stay
bot-owned. These tests pin the fix shape with source inspection (the
route is registered inside a 6k-line ``_register_oc_routes`` block that
isn't worth standing up a Flask test client for) and assert the
matching sudoers grant exists in the rendered evolve sudoers content.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

_ADMIN = Path(__file__).resolve().parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for _p in (_ADMIN, _ANALYZER):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _route_source() -> str:
    """Return the source of the function that registers the accept-drift route."""
    from evolve_admin.web import server as _srv

    src = inspect.getsource(_srv._register_oc_routes)
    marker = '"/api/security/accept-drift"'
    idx = src.find(marker)
    assert idx != -1, "accept-drift route registration not found in _register_oc_routes"
    # Window forward to the next route decorator so assertions don't bleed
    # across handlers.
    end = src.find("@app.", idx + len(marker))
    return src[idx: end if end != -1 else len(src)]


def test_accept_drift_route_shells_out_via_sudo_to_bot_user():
    """The handler must invoke ``sudo -H -u <bot_user> /usr/bin/python3
    .../backup.py --commit-baseline-local <bot_id>``. An in-process call
    from the evolve-user admin daemon EACCES's on the bot-owned
    ``.git/index.lock`` and reintroduces the 2026-06-04 regression.
    """
    body = _route_source()
    # The new shape uses a list literal that begins with the sudo prefix.
    # Match the prefix flexibly so trivial reformatting doesn't break us.
    assert '"sudo"' in body and '"-H"' in body and '"-u"' in body, (
        "accept-drift handler doesn't construct a `sudo -H -u …` argv. "
        "An in-process commit_baseline_local from the evolve user EACCES's "
        "on the bot-owned workspace .git/ — see 2026-06-04 regression."
    )
    assert "bot_user" in body, (
        "accept-drift handler doesn't reference bot_user — the sudo runas "
        "argument must come from get_bot_user, not be hard-coded."
    )
    assert "/usr/bin/python3" in body, (
        "accept-drift handler doesn't pin /usr/bin/python3 — sudoers "
        "command matching requires a full path."
    )
    assert "--commit-baseline-local" in body, (
        "accept-drift handler no longer references --commit-baseline-local; "
        "the backup CLI hook the route depends on is gone."
    )


def test_accept_drift_route_does_not_call_commit_baseline_local_in_process():
    """Belt-and-suspenders: forbid the in-process call inside the handler.
    The shell-out form is the only correct one; a future refactor that
    reverts to ``backup_mod.commit_baseline_local(...)`` brings back the
    EACCES on bot-owned ``.git/``.
    """
    body = _route_source()
    assert "commit_baseline_local(" not in body, (
        "accept-drift handler calls commit_baseline_local in-process. "
        "Must shell out via sudo -H -u <bot_user> (see 2026-06-04 fix)."
    )


def test_evolve_sudoers_grants_backup_commit_baseline_runas_any_user():
    """The shell-out only works if evolve has a passwordless sudo grant
    for ``/usr/bin/python3 .../backup.py --commit-baseline-local *`` as
    any user. Without this, the admin UI hits ``sudo: a password is
    required`` instead of the EACCES — equally broken, different log line.
    """
    from evolve_admin.setup_wizard import _render_evolve_sudoers

    content = _render_evolve_sudoers()
    assert content is not None, "sudoers renderer returned None (openclaw missing?)"
    # Look for the exact grant shape:
    #   evolve ALL=(ALL) NOPASSWD: /usr/bin/python3 …/backup.py --commit-baseline-local *
    needle = "/usr/bin/python3"
    backup_lines = [
        line for line in content.splitlines()
        if needle in line
        and "backup.py" in line
        and "--commit-baseline-local" in line
        and "ALL=(ALL)" in line
        and "NOPASSWD" in line
    ]
    assert backup_lines, (
        "No sudoers grant lets evolve run "
        "`python3 backup.py --commit-baseline-local <bot>` as the bot user. "
        "The admin UI 'Accept as baseline' button will hit "
        "'sudo: a password is required' until the grant lands."
    )
