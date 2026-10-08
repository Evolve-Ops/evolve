"""orphaned_bot_account_monitor — Signal producer for bot accounts that outlive
the roster.

The condition
=============

An account on this host carries an **Evolve-provisioned OpenClaw install** but
backs no member of the ``network.json`` roster. Its home, its config, and its
credentials are all still on disk; nothing runs, nothing manages it, and
nothing until now reported it.

This is the designed steady state after a graceful retirement, not an
exceptional one. ``evolve-admin retire-bot`` archives the bot's data to
``{shared_dir}/archived-bots/<bot>-<date>/``, stops its services, and removes it
from the roster — and **deliberately leaves the account and home in place**
(only the irreversible ``delete-bot`` removes those). That is the right default:
retirement should be recoverable and must never be one keystroke from deleting
someone's home directory. But it means every retirement leaves live channel
tokens, gateway tokens, and SSH keys sitting in an unmanaged account, and the
retire path has no way to revoke a Telegram token — that is a BotFather action.

The 2026-08-02 finding that motivated this: ``ledger`` was retired cleanly on
2026-06-15 (``archived-bots/ledger-2026-06-15/STATUS.json`` →
``retire_complete``), and seven weeks later its account still held a live
46-char Telegram bot token, a 48-char OpenClaw gateway token, and an SSH
keypair. The only thing that noticed anything was ``audit_machine``, which
called it *"🔴 CRITICAL: New user account(s) detected: ledger"* — framing a
decommissioned bot as a brand-new account. That reads like a false positive,
which is exactly why it sat unactioned.

So this producer exists to say the true thing: not "a new account appeared" but
"this account is a decommissioned bot, here is precisely what is still live in
it, and here is where to go revoke each item."

Why the roster join must go through ``get_bot_user``
====================================================

"Account name not in ``members``" is the naive test and it is wrong on the
reference pod three separate ways:

  * ``team_bot_b`` (a roster member) runs on the account ``shared_account_b``.
  * ``evolve`` (the primary bot) runs on the account ``evo``.
  * ``pod_admin_user`` (the operator's own admin account) has a personal OpenClaw
    install that Evolve did not provision and does not manage.

The first two would be reported as orphans and the third as a bot. The roster
side therefore resolves every member through ``evolve_config.get_bot_user``
(the dup-primitive-lint-enforced single source of bot→account truth), and the
account side requires an **Evolve-specific** marker, not merely ``.openclaw/``:
``workspace/evolve/``, ``workspace/manifests/``, or ``workspace/evolve-backup/``
— directories ``deploy`` creates for every bot it provisions and no personal
OpenClaw install has. On the reference pod that combination cleanly separates
``ledger`` (orphan) from ``shared_account_b``/``evo`` (roster members under other
account names) and ``pod_admin_user``/``evolve`` (never bots).

Signal shape
============

  * ``orphaned_bot_account`` — one per orphaned account, pod-scoped, carrying
    the value-free credential inventory from ``bot_credential_inventory``.
    Signature keyed on the account name. ``details.retirement`` says whether an
    ``archived-bots/`` record exists, which is the difference between "retired
    on <date>, cleanup pending" and "left the roster with no retirement record
    at all" — a materially different conversation, and the second one is the
    case where an operator should go looking for how it happened.
  * ``orphaned_bot_account_unreadable`` — the blind fail-safe, in two flavors:
    signature ``host`` when the account enumeration itself failed (nothing can
    be concluded this run, so the sweep is skipped entirely), and signature
    ``<account>`` when one account's install could not be probed.

Report-only. This monitor never deletes an account, never touches a home
directory, never edits ``network.json``, and never revokes anything. Deleting a
retired bot's account is ``delete-bot``, which is irreversible and stays an
explicit operator decision; revoking a channel token is an action at the
platform, which no code here can perform.

Run as
======

    sudo -u evolve python3 packages/analyzer/orphaned_bot_account_monitor.py \\
        --network /path/to/network.json

Installed daily (evolve user, pod-wide) by
``analyzer_monitor_jobs.install_orphaned_bot_account_monitor``; watched by
``monitor_coverage``'s producer-liveness layer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import bot_credential_inventory as credentials
from schema.signal import make_signature
from signals import store as signals_store

PRODUCER = "orphaned_bot_account"
ORPHAN_TYPE = "orphaned_bot_account"
UNREADABLE_TYPE = "orphaned_bot_account_unreadable"

# Signature key for the host-wide blind case (enumeration failed), distinct
# from any account name so the two unreadable flavors never collide.
_HOST_SCOPE_KEY = "host"

# Directories that mean "Evolve provisioned this OpenClaw install", relative to
# ``~/.openclaw``. Any ONE is sufficient. These are created by ``deploy`` for
# every bot (``set_evolve_read_acl`` grants evolve write on the first two; the
# third is the backup snapshot dir), and a hand-rolled personal OpenClaw
# install has none of them — which is what keeps the operator's own account off
# this report. Verified against the reference pod: present on every bot account
# including the retired one, absent on the admin account and on the ``evolve``
# service account's stray config.
_EVOLVE_INSTALL_MARKERS: tuple[str, ...] = (
    "workspace/evolve",
    "workspace/manifests",
    "workspace/evolve-backup",
)

# Accounts that are Evolve infrastructure rather than bots. The bot accounts
# themselves are excluded by the roster join (which resolves through
# ``get_bot_user``, so a bot running under any account name is covered); this
# list is only for the service identities that are never roster members but
# may carry OpenClaw state — the ``evolve`` service user has a stray
# ``.openclaw/evolve-tiers.json`` on the reference pod.
#
# Note ``evo`` is deliberately NOT here: post-account-separation it IS the
# primary bot's account, and ``get_bot_user`` resolves it as such. Listing it
# would mask a genuine orphan if the primary were ever retired.
_INFRA_ACCOUNTS: frozenset[str] = frozenset({"evolve"})


def excluded_accounts(network: dict) -> "set[str]":
    """Accounts this monitor never classifies: infra + the pod admin.

    The pod admin (``network.json::admin_user``) is excluded for a reason the
    live dry-run made obvious. It is a human login account with a 0700 home and
    no evolve read ACL — by design, and permanently. So every probe of it
    returns EACCES, the fail-safe classifies it "unreadable", and it fires a
    blind-spot Signal that **can never clear**. A standing alert with no
    reachable resolution is exactly the cries-wolf pattern this producer exists
    to replace; it would train the operator to ignore this monitor the same way
    ``audit_machine``'s wrong-animal CRITICAL trained them to ignore that one.

    Excluding it costs nothing real: Evolve never provisions the admin account,
    so it cannot be a decommissioned bot. The narrow residual — someone
    piggybacking a bot onto the admin account and then retiring it — is not
    reachable by this check anyway, because the check would be permanently
    blind on that home.

    Read from config rather than hardcoded, so a pod that names its admin
    something else is covered without an edit.
    """
    admin_user = str(network.get("admin_user") or "").strip()
    return set(_INFRA_ACCOUNTS) | ({admin_user} if admin_user else set())


# ── Account classification ────────────────────────────────────────────────


def _probe_marker(path: Path) -> str:
    """``"present"`` / ``"absent"`` / ``"unreadable"`` — via ``os.lstat``.

    Not ``Path.exists()``: since Python 3.13 it swallows EACCES and returns
    False, so an unreadable marker would classify as "not an Evolve install"
    and the account would silently drop off this report. The whole value of
    this monitor is that it does not silently drop accounts.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"
    return "present"


def evolve_install_status(home: Path) -> str:
    """``"evolve"`` / ``"not-evolve"`` / ``"unreadable"`` for an account home.

    ``"evolve"`` as soon as any marker is present. ``"unreadable"`` only when
    no marker was found AND at least one probe hit EACCES — i.e. we cannot
    honestly say the account is not a bot. ``"not-evolve"`` requires every
    probe to have cleanly reported absent.
    """
    blind = False
    for marker in _EVOLVE_INSTALL_MARKERS:
        status = _probe_marker(home / ".openclaw" / marker)
        if status == "present":
            return "evolve"
        if status == "unreadable":
            blind = True
    return "unreadable" if blind else "not-evolve"


def roster_accounts(network: dict) -> "set[str]":
    """Every host account that backs a current roster member.

    Resolved through ``evolve_config.get_bot_user`` rather than by assuming
    account == bot_id — on the reference pod ``team_bot_b`` runs on ``shared_account_b`` and
    the primary bot ``evolve`` runs on ``evo``, and treating either as an
    orphan would be a loud false positive on a live bot.
    """
    from evolve_config import get_bot_user  # blessed home (dup-primitive-lint)

    members = network.get("members") or list((network.get("bots") or {}).keys())
    accounts: "set[str]" = set()
    for member in members:
        try:
            accounts.add(get_bot_user(member, network))
        except Exception:  # noqa: BLE001 — a malformed bot block must not
            # shrink the known-account set (that would manufacture orphans);
            # fall back to the bot id, which is the account name by default.
            accounts.add(member)
    return accounts


def find_retirement_record(shared_dir: Path, account: str) -> "str | None":
    """The newest ``archived-bots/<account>-<date>*`` dir name, or None.

    Presence means the account went through ``retire-bot``; absence means it
    left the roster some other way. Both are worth reporting and they call for
    different operator responses, so the Signal carries which one it is.
    """
    archive_root = shared_dir / "archived-bots"
    try:
        entries = sorted(
            p.name for p in archive_root.iterdir()
            if p.is_dir() and (p.name == account or p.name.startswith(f"{account}-"))
        )
    except OSError:
        return None
    return entries[-1] if entries else None


# ── Signal payloads (pure) ────────────────────────────────────────────────


# The Signal store's soft limit on titles. Over it, ``observe`` logs a
# "move structured payload to details/body" warning on every emit.
_TITLE_SOFT_LIMIT = 80


def _orphan_title(account: str, live_count: int, unread_count: int) -> str:
    """Signal title that degrades to fit :data:`_TITLE_SOFT_LIMIT`.

    Both inputs are variable-width — the account name is arbitrary and the
    unreadable count is optional — so a single f-string cannot be kept under
    the limit by construction. The first live run on the reference pod proved
    it: a 6-char account with 4 credentials and 1 unreadable location produced
    83 chars and logged a store warning on every tick.

    Degradation order, most-droppable first: the parenthetical unreadable
    count (recoverable from ``details.unreadable_locations`` and restated in
    the body), then a hard truncation as the last resort so an
    extraordinarily long account name still cannot breach the limit.
    """
    noun = "credential" if live_count == 1 else "credentials"
    base = f"{account}: decommissioned bot account, {live_count} live {noun}"
    for candidate in (
        f"{base} still on disk" + (f" (+{unread_count} unchecked)" if unread_count else ""),
        f"{base} still on disk",
        base,
    ):
        if len(candidate) <= _TITLE_SOFT_LIMIT:
            return candidate
    return base[: _TITLE_SOFT_LIMIT - 1] + "…"


def _orphan_signal(
    account: str,
    home: Path,
    uid: int,
    inv: "credentials.Inventory",
    retirement: "str | None",
    ssh_prefix: str = "",
) -> dict:
    """Build the per-account orphan Signal payload (pure).

    ``ssh_prefix`` comes from ``resolve_pod_context`` and is prepended to the
    ``delete-bot`` hint. This Signal body renders in the admin SPA, whose
    natural shell is the in-app Terminal — which runs as the passwordless
    ``evolve`` service user and would prompt for a password that can never be
    entered. The prefix points the command at the shell where it actually
    works, and is empty when the operator is already at the deploy box.
    """
    delete_cmd = f"{ssh_prefix}sudo evolve-admin delete-bot {account}"
    live = inv.live_artifacts
    n = len(live)
    retired_desc = (
        f"retired through `retire-bot` (archive `{retirement}`)"
        if retirement
        else "NOT retired through `retire-bot` — no archive record exists"
    )
    title = _orphan_title(account, n, len(inv.unread))

    cred_lines = "\n".join(
        f"- `{a.location}` — {a.shape}\n  - Revoke: {a.revoke_with}"
        for a in live
    ) or "- (none found)"
    unread_block = ""
    if inv.unread:
        unread_block = (
            "\n**This list may be incomplete** — could not read:\n"
            + "\n".join(f"- `{u}`" for u in inv.unread)
            + "\n"
        )

    body = (
        f"The account `{account}` (uid {uid}, home `{home}`) carries an "
        "Evolve-provisioned OpenClaw install but backs no member of the "
        "current roster. It was "
        f"{retired_desc}.\n\n"
        "Nothing runs under it and nothing manages it — but its credentials "
        "are still valid wherever they were issued. Until they are revoked at "
        "the source, anyone who obtains this account's files can act as the "
        "bot on those channels.\n\n"
        f"**Still live ({n}):**\n{cred_lines}\n"
        f"{unread_block}"
        "\nThis is detection only — nothing was changed, and nothing here "
        "deletes the account. Revoking a channel token is an action at the "
        "platform (BotFather and friends), which Evolve cannot perform on "
        "your behalf.\n\n"
        "When the credentials are revoked and you want the account gone, "
        f"`{delete_cmd}` removes the account and home irreversibly. Leaving "
        "the account in place is a valid choice too — but then the "
        "credentials above should still be revoked."
    )

    return dict(
        signature=make_signature(PRODUCER, ORPHAN_TYPE, account),
        producer=PRODUCER,
        type=ORPHAN_TYPE,
        scope="pod",
        # No bot_id: this account is by definition not a roster member, and
        # pointing a Signal at a bot_id the admin UI cannot resolve would
        # produce a dead link on every surface that renders it.
        incident_key=f"{PRODUCER}:{account}",
        title=title,
        body=body,
        details=dict(
            account=account,
            uid=uid,
            home=str(home),
            retired_via_retire_bot=retirement is not None,
            retirement_archive=retirement,
            live_credential_count=n,
            credentials=[
                dict(
                    location=a.location,
                    kind=a.kind,
                    shape=a.shape,
                    revoke_with=a.revoke_with,
                )
                for a in live
            ],
            unreadable_locations=list(inv.unread),
            what_it_means=(
                f"`{account}` is a decommissioned bot, not a new account. Its "
                "home and Evolve-provisioned OpenClaw install survive because "
                "`retire-bot` deliberately preserves them (only `delete-bot` "
                "removes an account), so every retirement leaves live channel "
                "and gateway tokens in an unmanaged account. The exposure is "
                "outward-facing: a channel token stays valid at the platform "
                "no matter what happens on this host. Note that "
                "`audit_machine` reports the same account as a NEW user "
                "account — that framing is a known artifact of its "
                "baseline-diff check and should not be read as a fresh "
                "intrusion."
            ),
            fix_steps=(
                "1. Revoke each credential listed above at its source — a "
                "Telegram token can only be revoked in BotFather, an SSH key "
                "by removing the public half from wherever it was "
                "authorized.\n"
                "2. Decide the account's fate: leave it (credentials now "
                f"revoked) or `{delete_cmd}` to remove the account and home "
                "irreversibly.\n"
                "3. This Signal clears on the next run once the account no "
                "longer has an Evolve install — i.e. after `delete-bot`. If "
                "you keep the account, snooze or dismiss it once the "
                "credentials are revoked."
            ),
        ),
    )


def _unreadable_signal(scope_key: str, detail: str) -> dict:
    """Build the blind-tick Signal payload (pure).

    ``scope_key`` is :data:`_HOST_SCOPE_KEY` when the account enumeration
    failed (nothing could be concluded at all) or an account name when one
    account's install could not be probed.
    """
    host_wide = scope_key == _HOST_SCOPE_KEY
    subject = (
        "the host account list" if host_wide
        else f"the OpenClaw install under account `{scope_key}`"
    )
    title = (
        "Orphaned-account check blind — host account list unreadable"
        if host_wide
        else f"{scope_key}: orphaned-account check blind — install unreadable"
    )
    body = (
        f"Could not read {subject}, so the orphaned-bot-account check "
        + ("could not run at all this tick. " if host_wide
           else f"cannot classify `{scope_key}`. ")
        + "A monitor that can't read must not look clean — this Signal marks "
        "the blind spot rather than silently passing, and any existing "
        "orphaned-account Signals are left in place (we can't confirm they "
        "cleared).\n\n"
        f"Read error: {detail}\n\n"
        "Fix: ensure the evolve read ACL is intact "
        "(`sudo evolve-admin ensure-pod-perms`). The Signal auto-resolves "
        "once the read succeeds again."
    )
    return dict(
        signature=make_signature(PRODUCER, UNREADABLE_TYPE, scope_key),
        producer=PRODUCER,
        type=UNREADABLE_TYPE,
        scope="pod",
        incident_key=f"{PRODUCER}:{scope_key}",
        title=title,
        body=body,
        details=dict(
            scope_key=scope_key,
            host_wide=host_wide,
            error=detail,
            what_it_means=(
                f"The orphaned-bot-account monitor could not read {subject}. "
                "Until the read is restored, a decommissioned bot account "
                "still holding live channel tokens would go unreported"
                + (" for the whole pod." if host_wide
                   else f" for `{scope_key}`.")
            ),
            fix_steps=(
                "1. Run `sudo evolve-admin ensure-pod-perms` to reassert the "
                "evolve read ACL.\n"
                "2. The Signal auto-resolves on the next run once the read "
                "succeeds."
            ),
        ),
    )


# ── Orchestration ─────────────────────────────────────────────────────────


def run(
    network_path: Path,
    *,
    dry_run: bool = False,
    now: "datetime | None" = None,
) -> dict:
    """One pass: classify every host account, emit orphan Signals, sweep."""
    now = now or datetime.now(timezone.utc)

    # Lazy import so this script boots on a host where the admin package isn't
    # installed yet (early bootstrap; degrades to no-op) — same pattern as the
    # roster monitors.
    try:
        from evolve_admin.config import load_network  # type: ignore
    except ImportError as exc:
        print(
            json.dumps({
                "status": "skipped",
                "reason": f"evolve_admin not importable: {exc}",
            }),
            flush=True,
        )
        return {"accounts_scanned": 0, "orphans": 0, "signals_fired": 0}

    from platform_profile import get_profile
    from runtime.isolation import IsolationError, get_isolation

    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir") or get_profile().shared_dir_default)

    # Operator-facing command prefix — never hardcode the SSH target
    # (CLAUDE.md §SSH). Degrades to "" if the admin helper moves.
    try:
        from evolve_admin.config import resolve_pod_context  # type: ignore

        ssh_prefix = resolve_pod_context(network).get("ssh_prefix", "")
    except Exception:  # noqa: BLE001 — a missing prefix is cosmetic
        ssh_prefix = ""

    kept_orphan: "set[str]" = set()
    kept_unreadable: "set[str]" = set()
    signals_fired = 0

    def _emit(payload: dict, label: str) -> None:
        nonlocal signals_fired
        if dry_run:
            print(json.dumps({"would_observe": payload}, default=str), flush=True)
            return
        try:
            signals_store.observe(shared_dir, **payload)
            signals_fired += 1
        except Exception as exc:  # noqa: BLE001
            print(
                f"[orphaned_bot_account] observe({label}) failed: {exc}",
                flush=True,
            )

    # ── Enumerate host accounts ──
    try:
        accounts = get_isolation().list_accounts()
    except IsolationError as exc:
        # Host-wide blind: fire the fail-safe and skip BOTH sweeps. With no
        # account list, `kept_orphan` would be empty and the sweep would
        # auto-resolve every live orphan Signal — the precise failure the
        # unreadable type exists to prevent.
        payload = _unreadable_signal(_HOST_SCOPE_KEY, str(exc))
        _emit(payload, "host-unreadable")
        summary = {
            "accounts_scanned": 0,
            "orphans": 0,
            "unreadable": 1,
            "signals_fired": signals_fired,
            "signals_resolved": 0,
            "swept": False,
            "ran_at": now.isoformat(),
        }
        print(json.dumps(summary, default=str), flush=True)
        return summary

    known_accounts = roster_accounts(network)
    skip_accounts = excluded_accounts(network)
    orphan_count = 0
    blind_count = 0

    for account in accounts:
        if account.user in known_accounts or account.user in skip_accounts:
            continue

        status = evolve_install_status(account.home)
        if status == "not-evolve":
            continue
        if status == "unreadable":
            blind_count += 1
            payload = _unreadable_signal(
                account.user,
                f"EACCES probing {account.home}/.openclaw for the Evolve "
                "install markers",
            )
            kept_unreadable.add(payload["signature"])
            # Keep any EXISTING orphan Signal for this account alive: we could
            # not confirm the condition cleared, and a blind tick must never
            # sweep-resolve it. The signature is deterministic, so this needs
            # no store lookup.
            kept_orphan.add(
                make_signature(PRODUCER, ORPHAN_TYPE, account.user)
            )
            _emit(payload, f"unreadable:{account.user}")
            continue

        try:
            inv = credentials.collect(account.home)
        except Exception as exc:  # noqa: BLE001 — one bad account must not
            # abort the pass. Without this, an unexpected error on the first
            # account would skip every later account AND skip the sweep, so
            # the monitor would go silent rather than degrade. Route it into
            # the blind fail-safe, which keeps this account's existing orphan
            # Signal alive and marks the gap.
            blind_count += 1
            payload = _unreadable_signal(
                account.user,
                f"credential inventory failed: {type(exc).__name__}: {exc}",
            )
            kept_unreadable.add(payload["signature"])
            kept_orphan.add(make_signature(PRODUCER, ORPHAN_TYPE, account.user))
            _emit(payload, f"inventory-failed:{account.user}")
            continue

        orphan_count += 1
        payload = _orphan_signal(
            account.user,
            account.home,
            account.uid,
            inv,
            find_retirement_record(shared_dir, account.user),
            ssh_prefix,
        )
        kept_orphan.add(payload["signature"])
        _emit(payload, f"orphan:{account.user}")

    signals_resolved = 0
    if not dry_run:
        for types, kept, reason in (
            (
                {ORPHAN_TYPE},
                kept_orphan,
                "auto-resolve: account no longer carries an Evolve install",
            ),
            (
                {UNREADABLE_TYPE},
                kept_unreadable,
                "auto-resolve: account list and installs readable again",
            ),
        ):
            try:
                signals_resolved += len(
                    signals_store.sweep_resolve(
                        shared_dir,
                        producer=PRODUCER,
                        kept_signatures=kept,
                        types=types,
                        reason=reason,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[orphaned_bot_account] sweep_resolve({types}) failed: {exc}",
                    flush=True,
                )

    summary = {
        "accounts_scanned": len(accounts),
        "roster_accounts": len(known_accounts),
        "orphans": orphan_count,
        "unreadable": blind_count,
        "signals_fired": signals_fired,
        "signals_resolved": signals_resolved,
        "swept": True,
        "ran_at": now.isoformat(),
    }
    print(json.dumps(summary, default=str), flush=True)
    return summary


def main(argv: "list[str] | None" = None) -> int:
    from platform_profile import get_profile

    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument(
        "--network",
        default=str(Path(get_profile().shared_dir_default) / "network.json"),
        help="Path to network.json (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Signals that would be observed but don't write them.",
    )
    args = parser.parse_args(argv)

    network_path = Path(args.network)
    if not network_path.exists():
        print(
            json.dumps({
                "status": "skipped",
                "reason": f"network.json not found at {network_path}",
            }),
            flush=True,
        )
        return 0

    run(network_path, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
