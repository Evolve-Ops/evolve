"""Per-bot config file perm enforcement (mode for secrets, ownership for tiers).

``openclaw.json`` (the gateway token + every messaging-channel bot token)
and ``auth-profiles.json`` (LLM-provider API keys) must never be world-
readable on a multi-user box. A bare ``sudo /bin/cp`` (no ``-p``) creates the
destination at root's umask → 0644, so every config-write path must tighten
the mode back to 0600 after the copy.

``evolve-tiers.json`` (per-bot model-routing config) is the inverse problem:
it is NOT a secret (0644 is correct), but a bare ``sudo /bin/cp`` to a *fresh*
dest lands it ``root:wheel 0600`` — and then the bot user, which runs
``oc_model.py`` to read/rewrite its OWN tier config, can no longer read it.
``check_bot_tiers_ownership`` (also wired into ``ensure_pod_perms``) converges
a drifted file back to bot-owned 0644.

This lives in its own module (rather than inline in deploy.py) for two
reasons: deploy.py is a frozen hot-hazard file under a no-growth cap, and the
"what files are secret + how to enforce 0600" contract has one home that both
the write paths (``chmod_secret_config``) and the deploy-time self-heal
(``check_bot_secret_modes``, wired into ``ensure_pod_perms``) share.

Why 0600 doesn't break the admin read path: on **macOS** ``chmod`` changes
neither ownership nor ACLs, so the bot keeps owner-read and the ``evolve``
admin user keeps the inherited read ACL that ``set_evolve_read_acl`` grants
on ``.openclaw/``. Verified live 2026-06-12. On **Linux** the story differs:
a POSIX ACL has a *mask* and ``chmod 600``'s zeroed group bits BECOME that
mask, silently capping the inherited ``u:evolve`` read ACE to nothing — so
admin daemons (e.g. ``pod_perms_drift_monitor``) hit EACCES reading
``auth-profiles.json``. ``chmod_secret_config`` therefore re-grants the
evolve read ACE on Linux (setfacl recalculates the mask), and
``check_bot_secret_modes`` compares the perms-seam *effective* mode (which
corrects the ACL-mask display) so the re-grant doesn't read as 0640 drift.
(W10-G #2.)

Sudoers: the ``chmod 600`` grants for these paths are rendered by
``setup_wizard._render_evolve_sudoers`` (§4 for openclaw.json, §5 for
auth-profiles.json). Changing those requires ``sudo evolve-admin
refresh-sudoers``.
"""

from __future__ import annotations

import os
import pwd
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from evolve_config import user_home as _user_home
from platform_profile import get_profile as _get_profile

from .runtime import get_perms as _get_perms
from .runtime.perms import POD_READ_ACL_PERMS as _POD_READ_ACL_PERMS
from .runtime.perms import Perms as _Perms
from .telemetry import get_logger as _get_logger

if TYPE_CHECKING:
    from .deploy import _PermCheck

_log = _get_logger("secret_config_perms")

_EVOLVE_USER = "evolve"

# Per-bot config files that carry secrets and must be mode 0600. Paths are
# relative to the bot's ``.openclaw/`` directory. ``openclaw.json.bak`` is the
# pre-write backup safe_write_bot_config makes — a copy of the same token file,
# so it carries the same exposure and is converged here too (it simply reads as
# "not present" on bots that never went through safe_write_bot_config).
BOT_SECRET_CONFIG_RELPATHS: tuple[str, ...] = (
    "openclaw.json",
    "openclaw.json.bak",
    "agents/main/agent/auth-profiles.json",
    # Under https_pat auth the backup remote URL embeds a GitHub PAT, so the
    # workspace .git/config is token-bearing. 0600 is harmless on ssh/
    # credhelper bots (bot keeps owner rw; evolve reads via ACL + the §11f
    # sudo cat grant), and the deploy self-heal converges files that a
    # pre-2026-07 rotate left at 644.
    "workspace/.git/config",
)
BOT_SECRET_CONFIG_MODE = 0o600
_MODE_ARG = oct(BOT_SECRET_CONFIG_MODE)[2:]  # "600"

# Bot-PRIVATE secret files — the opposite contract from
# ``BOT_SECRET_CONFIG_RELPATHS``: evolve must NEVER hold a read ACE on these
# (threat-model §3.1 carve-out class, file-shaped — same as the credentials/
# dir and profiles/*.md carve-outs). The scripts that use them run as the bot;
# the admin daemon has no read path by design (spec-darwin-pm §10.b).
#
# Why the strip matters beyond least-privilege (#3452): the recursive
# ``.openclaw`` read grant sweeps these files up, and on Linux a planted ACE
# makes the stat group triad display the ACL MASK — a 0600 file reads as 640 —
# so strict app-side ``mode == 0600`` gates (pm-inbox's tokens check) refuse
# to run after every heal. Paths relative to the bot's ``.openclaw/``.
# A new entry needs matching carve-out grants in ``_render_evolve_sudoers``
# (Linux: ``setfacl -b`` + ``chmod 600``; macOS: ``chmod -N``) — the renderer
# emits them from this tuple.
BOT_PRIVATE_SECRET_RELPATHS: tuple[str, ...] = (
    "pm-inbox-github-tokens.json",
)


def strip_bot_private_acl(oc_dir: "str | Path") -> bool:
    """Remove any ACL (and restore 0600) on the bot-private secret files.

    The file-shaped twin of the credentials/ + profiles carve-out: run after
    any recursive ``.openclaw`` read grant so the sweep's collateral ACE comes
    back off. Idempotent and best-effort (missing files are fine; a failed
    strip returns False and the next deploy/heal converges it). macOS
    ``clear_acl`` maps to ``chmod -N``; the 0600 restore is the same
    ``sudo /bin/chmod`` shape as ``chmod_secret_config`` (no mask re-grant
    afterwards — no ACE should remain on these files, that is the point).
    """
    oc = Path(oc_dir)
    perms = _get_perms()
    ok = True
    for rel in BOT_PRIVATE_SECRET_RELPATHS:
        path = oc / rel
        if not exists_or_unreachable(path):
            continue
        if not perms.clear_acl(path):
            ok = False
        proc = subprocess.run(
            ["sudo", "/bin/chmod", _MODE_ARG, str(path)],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            ok = False
    return ok


def chmod_secret_config(path: "str | Path") -> bool:
    """``sudo /bin/chmod 600`` a bot-owned secret config file.

    Mode only on macOS — ownership and ACLs are untouched (``chmod`` changes
    neither). On Linux the chmod clobbers the POSIX-ACL mask (group bits →
    mask), so this re-grants the evolve read ACE afterwards to keep the admin
    read path working (see module docstring; W10-G #2). Best-effort: returns
    True on success, False on any non-zero exit. Callers treat a failure as
    non-fatal (the deploy-time self-heal converges it on the next
    ``ensure_pod_perms`` pass).
    """
    proc = subprocess.run(
        ["sudo", "/bin/chmod", _MODE_ARG, str(path)],
        capture_output=True, text=True, timeout=5,
    )
    if proc.returncode != 0:
        return False
    # Linux only: re-widen the mask by re-granting evolve's read ACE (setfacl
    # recomputes the mask to cover named entries). macOS has no ACL mask and a
    # grant here with dir-shaped verbs would perturb the byte-exact golden —
    # skip it (the inherited ACE already survives the chmod there).
    if _get_profile().name != "macos":
        _get_perms().grant(Path(path), _EVOLVE_USER, _POD_READ_ACL_PERMS)
    return True


def _reassert_evolve_read_acl(oc_dir: "str | Path") -> bool:
    """Re-grant evolve's recursive read ACL on the bot's ``.openclaw`` tree.

    ``grant_read_recursive`` is ``setfacl -R -m u:evolve:rX`` (sudo-granted) —
    the ``-R`` walk recomputes every child's ACL mask to cover the named entry,
    which re-widens a mask that an OC-created 0700 dir clamped to nothing. This
    is the same operation ``set_evolve_read_acl`` runs on every deploy; offered
    here as the drift apply so ``ensure-pod-perms`` clears the unreachable-secret
    drift without a full redeploy. macOS: the inheritable +a ACE re-add, a no-op
    when already present.

    Deliberately WITHOUT ``restrict_group_other`` (the bot-private group/other
    clamp): this is a narrow mask-repair, not the full deploy contract, and it
    does NOT re-run the workspace/ ``share_group_other_read`` re-widen — so
    clamping group/other here would starve the bot's read of evolve-written
    workspace files until the next full deploy. The bare ``-m`` preserves any
    existing ``group::---``/``other::---`` the last deploy set (setfacl only
    touches the entries named in the spec), so this re-grant neither widens nor
    breaks the clamp — it just recomputes the mask. The full bot-private clamp
    lives on the deploy path (``set_evolve_read_acl``).

    The recursive re-grant sweeps up the bot-private secret files (#3452), so
    the file-shaped carve-out runs right after — same pairing as the deploy
    path's credentials/profiles carve-out."""
    ok = _get_perms().grant_read_recursive(Path(oc_dir), _EVOLVE_USER)
    strip_bot_private_acl(oc_dir)
    return ok


# Dir whose write contract feeds the evolve daemons (defer-runner,
# manifest-reflex-runner). Relative to ``.openclaw/``.
_WORKSPACE_EVOLVE_RELPATH = "workspace/evolve"

# The workspace ROOT — where the bot's instruction + identity docs (AGENTS.md,
# the *.md identity files) live and where the content scanner reads them. evolve
# must be able to TRAVERSE this dir to read those docs; a 0700-harden recompute
# on it (the same shape the OC gateway does to ``.openclaw``) clamps the ACL mask
# to ``---`` and caps evolve's traverse ACE, hiding every required doc at once.
# Relative to ``.openclaw/``.
_WORKSPACE_ROOT_RELPATH = "workspace"


def _secret_relpath_parent_dirs(oc_dir: "Path") -> "list[Path]":
    """Every intermediate dir between ``.openclaw`` and a secret relpath.

    For ``agents/main/agent/auth-profiles.json`` that is ``agents/``,
    ``agents/main/`` and ``agents/main/agent/``; for ``workspace/.git/config``
    it is ``workspace/`` and ``workspace/.git/``. Deduped across relpaths and
    sorted shallow-first — load-bearing for the reassert callers: the per-path
    getfacl guard inside ``reassert_mask`` runs unprivileged, so a clamped
    ancestor hides a clamped child until the ancestor is re-widened first.
    """
    out: "set[Path]" = set()
    for rel in BOT_SECRET_CONFIG_RELPATHS:
        parent = (oc_dir / rel).parent
        while parent != oc_dir:
            out.add(parent)
            parent = parent.parent
    return sorted(out, key=lambda p: (len(p.parts), str(p)))


def exists_or_unreachable(path: "Path") -> bool:
    """``path.exists()`` that treats an EACCES on the parent as PRESENT.

    On Linux a ``.openclaw`` (or ``agents/...``) dir the OC gateway hardened to
    0700 clamps evolve's inherited traverse ACE's mask to ``---`` — so a plain
    ``Path.exists()`` RAISES ``PermissionError`` (modern Python — 3.12 on the
    Ubuntu/VPS pods — does not swallow EACCES the way 3.11 did, where it returned
    False; ``feedback_exists_check_lies_under_0700_parents`` inverted). A clamped-
    and-therefore-unreachable path is exactly the broken state the perms checks
    must report, AND the state deploy/ACL-repair sites must keep WORKING under
    (they re-grant the read ACL, which recomputes the clamped mask) — so classify
    it as ``present`` rather than letting the raise crash the deploy / backstop.
    Shared by ``set_evolve_read_acl``, ``ensure_pod_perms`` per-bot checks, and
    the verify path below. (W10-G round-8; durable EACCES sweep 2026-06-23.)"""
    try:
        return path.exists()
    except OSError:
        return True


# Back-compat private alias — internal callers below predate the public name.
_exists_or_unreachable = exists_or_unreachable


def verify_evolve_access(bot_user: str, perms: "_Perms | None" = None) -> list[str]:
    """Assert the evolve read/write contract actually HOLDS after the grants ran.

    The backstop for ``set_evolve_read_acl``. The W10-G round-6 EACCES failures
    shipped silently because every ACL call is best-effort (``check=False``) and
    a Linux ``chmod 600`` on a secret file zeroes the POSIX-ACL **mask**, capping
    evolve's inherited read ACE to ``#effective:---``. Nothing re-read the
    contract, so the breakage only surfaced when a daemon hit EACCES on the live
    pod (``pod_perms_drift_monitor`` couldn't read ``auth-profiles.json``;
    ``defer-runner`` couldn't append its queue). This re-checks the EFFECTIVE
    perms through the Perms seam (``getfacl`` honours the mask) and logs at ERROR
    on any gap — naming the file and the ``refresh-sudoers`` remedy — so this
    class of mask-masking can never ship mute again.

    Three facets, each a distinct mask-clamp the OC gateway introduces AFTER the
    deploy-time grant (W10-G round-8 — the recurrence in two more places):
      (0) **traverse** — the gateway re-hardens ``.openclaw`` itself to 0700 on
          startup, clamping evolve's ``rX`` ACE so it cannot even reach
          ``workspace/evolve`` (defer-runner / manifest-reflex-runner
          ``PermissionError`` on the queue). Checked FIRST so the root cause is
          named, not a misleading "cannot read <deep file>".
      (0b) **parent-dir traverse** — the gateway re-hardens the secret files'
          INTERMEDIATE parent dirs too (``agents/main/agent`` to 0700 on auth
          writes), clamping THAT dir's mask. The file's own getfacl still looks
          healthy, so facet (1) alone passes while the file is unreadable in
          practice — ensure-pod-perms reported "canonical" on exactly this
          state (the 2026-07-29 evolve-vps recurrence; same blind-spot class
          as facet (3)'s workspace/ clamp).
      (1) **read** — ``chmod 600`` on a token file clamps that file's mask.
      (2) **write** — the queue dir's mask, clamped by a 0700-created parent.

    Linux only: macOS mode bits and ACLs are orthogonal (no mask), so a chmod
    can't cap the inherited ACE there (verified live 2026-06-12) — the contract
    is structurally held and a check would only add golden churn + a needless
    ``getfacl`` per deploy. Mirrors ``chmod_secret_config``'s own Linux key.

    Returns the list of human-readable failure strings (empty == contract holds);
    the caller logs are loud but non-fatal — the contract self-heals on the next
    ``ensure_pod_perms`` pass (now via the first-class ``_check_evolve_access``
    drift check) and the hourly ``pod_perms_drift_monitor`` turns a persistent
    gap into a Signal. (W10-G round-7 / round-8.)
    """
    if _get_profile().name == "macos":
        return []
    perms = perms or _get_perms()
    oc_dir = _user_home(bot_user) / ".openclaw"
    if not _exists_or_unreachable(oc_dir):
        return []  # bot not bootstrapped yet — nothing to enforce
    failures: list[str] = []

    # (0) evolve must TRAVERSE .openclaw to reach anything beneath it. The OC
    #     gateway hardens .openclaw to 0700 on startup, clamping the mask → the
    #     inherited rX ACE caps to ---. This is the round-8 root cause for the
    #     primary (evo) bot's defer/manifest-reflex queue EACCES; name it first.
    # "search" is the macOS ACL vocabulary for traverse/execute (what the read
    # grant seeds); the Linux backend collapses it to the x bit the kernel checks.
    if not perms.acl_user_effective(oc_dir, _EVOLVE_USER, "search"):
        failures.append(f"evolve cannot TRAVERSE {oc_dir} (0700 clamps the ACL mask)")

    # (0b) evolve must TRAVERSE every intermediate parent dir of the secret
    #     relpaths (agents/, agents/main/, agents/main/agent/, workspace/.git/).
    #     The OC gateway re-hardens agents/main/agent to 0700 on auth writes —
    #     the dir's clamped mask makes auth-profiles.json unreachable while the
    #     FILE's own getfacl (facet (1)) still reads healthy, so without this
    #     facet the verify passed, ensure-pod-perms said "canonical", and the
    #     hourly drift monitor never escalated (2026-07-29 evolve-vps). The
    #     workspace/ root itself is excluded — facet (3) below reports it with
    #     the content_scan context.
    ws_root = oc_dir / _WORKSPACE_ROOT_RELPATH
    for parent in _secret_relpath_parent_dirs(oc_dir):
        if parent == ws_root:
            continue
        if _exists_or_unreachable(parent) and not perms.acl_user_effective(parent, _EVOLVE_USER, "search"):
            failures.append(f"evolve cannot TRAVERSE {parent} (ACL mask clamp)")

    # (1) evolve must READ every token-bearing secret file that exists. The
    #     chmod-600 hardening clobbers the mask; chmod_secret_config re-grants —
    #     but a MISSING sudoers grant (round-6's auth-profiles.json gap) makes
    #     that re-grant fail silently. This is the check that catches it.
    for rel in BOT_SECRET_CONFIG_RELPATHS:
        path = oc_dir / rel
        if _exists_or_unreachable(path) and not perms.acl_user_effective(path, _EVOLVE_USER, "read"):
            failures.append(f"evolve cannot READ {path}")

    # (2) evolve must WRITE the workspace/evolve queue dir. _ensure_evolve_write_dir
    #     creates it under evolve's write ACL; a clobbered mask would cap that too.
    ws_evolve = oc_dir / _WORKSPACE_EVOLVE_RELPATH
    if _exists_or_unreachable(ws_evolve) and not perms.acl_user_effective(ws_evolve, _EVOLVE_USER, "write"):
        failures.append(f"evolve cannot WRITE {ws_evolve}")

    # (3) evolve must TRAVERSE the workspace/ ROOT to read the bot's instruction +
    #     identity docs (AGENTS.md, the *.md identity files — all directly in
    #     workspace/) that the content scanner checks each run. A 0700-harden
    #     recompute on this dir clamps its mask exactly like it does .openclaw,
    #     capping evolve's traverse ACE → the daily content_scan then fires
    #     content_scan_file_disappeared for EVERY required workspace file in
    #     LOCKSTEP (the 2026-06-29 evo-vps recurrence). This clamp was a blind spot:
    #     facets (0)-(2) — .openclaw traverse, the secret reads, the workspace/evolve
    #     write — all stay healthy while workspace/ itself is clamped, so the hourly
    #     self-heal never saw a failure and never escalated; only a full redeploy's
    #     recursive re-grant repaired it (the 9 min–2 h flap windows). "search" is
    #     the macOS ACL verb for traverse/execute the read grant seeds.
    #     (ws_root is bound in facet (0b) above.)
    if _exists_or_unreachable(ws_root) and not perms.acl_user_effective(ws_root, _EVOLVE_USER, "search"):
        failures.append(f"evolve cannot TRAVERSE {ws_root} (content_scan read path; ACL mask clamp)")

    if failures:
        _log.error(
            "set_evolve_read_acl: evolve access contract NOT satisfied for "
            "bot_user=%s after the ACL grants — %s. The Linux POSIX-ACL mask "
            "likely clamps evolve's inherited ACE (a 0700 dir or chmod 600 zeroes "
            "it). Run `sudo evolve-admin refresh-sudoers` then redeploy, or "
            "`sudo evolve-admin ensure-pod-perms` to self-heal.",
            bot_user, "; ".join(failures),
        )
    return failures


def heal_evolve_access(bot_id: str, bot_user: str) -> bool:
    """Re-assert + re-verify the evolve read/write contract; return whether it HOLDS.

    The self-healing apply behind ``check_evolve_access`` AND the
    wizard's final post-gateway pass (W10-G round-9). It does three things,
    in order:

      1. Re-runs ``set_evolve_read_acl(bot_id)`` — its recursive
         ``grant_read_recursive`` recomputes every gateway-clamped child mask
         and re-plants the default ACL.
      2. **Explicitly re-widens the ``.openclaw`` mask** with the proven
         ``setfacl -m m::rwX`` (``reassert_mask``). The OC gateway hardens
         the top ``.openclaw`` dir to 0700 on startup — and again on every
         ``openclaw`` invocation against it (the wizard's Telegram
         channel-add + plugin-install steps both do) — and that chmod's
         zeroed group bits BECOME the ACL mask, clamping evolve's inherited
         ``rX`` traverse ACE to ``---``. A bare re-grant recomputes the mask
         too, but the explicit re-widen is belt-and-suspenders so the heal is
         deterministic regardless of grant ordering (round-9 root cause #1).
      3. Re-verifies via ``verify_evolve_access`` (the perms-seam ``getfacl``
         EFFECTIVE check) and returns ``True`` iff no gaps remain.

    Returning a real bool also fixes the ``ensure_pod_perms`` apply-phase
    false-failure: ``set_evolve_read_acl`` returns ``None``, so wiring it
    directly as a ``_PermCheck.apply`` made ``bool(c.apply())`` always
    ``False`` → "fix did not return success" even on a successful heal
    (the round-8 ``evolve-access//home/<bot>/.openclaw`` deploy-log noise).

    Linux-only signal: ``reassert_mask`` + ``verify_evolve_access`` are
    structural no-ops on macOS (no ACL mask), so this is a re-grant + a
    constant ``True`` there — byte-identical to the prior behaviour.
    """
    from .deploy import set_evolve_read_acl  # lazy: deploy imports us

    try:
        set_evolve_read_acl(bot_id)
    except Exception as exc:  # noqa: BLE001 — best-effort; the re-verify below is authoritative
        _log.warning(
            "heal_evolve_access: set_evolve_read_acl(%s) raised %s; "
            "continuing to the mask re-widen + re-verify (authoritative)",
            bot_id, exc,
        )
    perms = _get_perms()
    oc_dir = _user_home(bot_user) / ".openclaw"
    perms.reassert_mask(oc_dir)  # `setfacl -m m::rwX .openclaw`; no-op on macOS / un-ACL'd
    # The gateway also re-clamps the secret relpaths' PARENT dirs (it re-hardens
    # agents/main/agent to 0700 on auth writes — verify facet (0b), the
    # 2026-07-29 evolve-vps recurrence). Re-widen those explicitly too,
    # shallow-first so each reassert's unprivileged getfacl guard runs under an
    # already-re-widened ancestor. Same belt-and-suspenders rationale as the
    # .openclaw re-widen above.
    for parent in _secret_relpath_parent_dirs(oc_dir):
        if _exists_or_unreachable(parent):
            perms.reassert_mask(parent)
    # Belt-and-suspenders (#3452): whichever grant path ran above, make sure
    # the bot-private secret files come out of it ACE-free and 0600 — a
    # planted ACE turns their stat group triad into the ACL mask and strict
    # app-side 0600 gates (pm-inbox tokens) refuse until the next strip.
    strip_bot_private_acl(oc_dir)
    return not verify_evolve_access(bot_user, perms)


def reassert_evolve_access(bot_id: str, bot_user: str) -> "tuple[bool, list[str]]":
    """Light, frequent self-heal of the evolve read/traverse contract — the
    RUNTIME counterpart to the deploy-time ``heal_evolve_access``.

    The flap this exists to kill: the OC gateway re-hardens ``~/.openclaw`` to
    ``0700`` on its own ops — gateway (re)start, and every ``openclaw``
    invocation an hourly Evolve daemon makes against a bot (the Tier-3 app
    audit's ``openclaw agent``, the security audit, digests). On Linux that
    chmod's zeroed group bits BECOME the POSIX-ACL mask (``mask::---``),
    clamping evolve's inherited ``user:evolve:r-x`` traverse ACE to
    ``#effective:---`` — evolve loses read+traverse pod-wide until something
    re-widens the mask. The re-harden lives inside the OC gateway (Node), fired
    from several jobs plus the gateway's own lifecycle, so there is no single
    Evolve-side trigger to couple a reassert to (the way deploy.py couples it to
    ``safe_write_bot_config``'s ``openclaw config validate``). The robust fix is
    a PERIODIC reassert: run it on the existing hourly perms-drift cadence so a
    clamp is undone within ≤1 cycle and stops generating ACL-drift Signals +
    Sysadmin-Watchdog restore Proposals (which previously healed it reactively,
    one bounce per hour — the "same alert 5× in 24h" flurry).

    Two tiers, cheapest first:

      1. **Light** — ``reassert_mask`` on ``.openclaw`` (``setfacl -m m::rwX``)
         AND, recursively, on the ``workspace/`` subtree, AND on each secret
         relpath's intermediate parent dir (``agents/``, ``agents/main/``,
         ``agents/main/agent/``, ``workspace/.git/``). The top-dir re-widen
         repairs the common case (a 0700 chmod recomputed only the top
         ``.openclaw`` dir's mask; the named ACEs and child masks survive); the
         workspace re-widen repairs the INDEPENDENT clamp on the ``workspace/``
         root that hid the bot's identity docs from the content scanner (verify
         facet (3) — the 2026-06-29 evo-vps recurrence); the parent-dir
         re-widen repairs the equally independent clamp the gateway puts on
         ``agents/main/agent`` on auth writes (verify facet (0b) — the
         2026-07-29 evolve-vps recurrence). It uses only evolve's existing
         ``setfacl`` sudoers grants — no root chmod/chown — so it is safe
         to run unattended every hour, unlike the pod-wide owner/mode apply
         ``pod_perms_drift_monitor`` deliberately withholds.
      2. **Escalate** — only if the post-reassert VERIFY still fails (a rarer
         shape: a child secret's mask clamped too, or the named ACE itself was
         stripped) fall back to the full ``heal_evolve_access`` (recursive
         re-grant + default-ACL re-plant + reassert).

    The access-VERIFY is the LAST step in each tier (the "false-green: passed
    then re-hardened" lesson — never report success off a grant call's return,
    only off a fresh effective-perm read).

    Returns ``(ok, remaining_failures)``. macOS is a structural no-op (no ACL
    mask) → always ``(True, [])``; un-bootstrapped bots → ``(True, [])``.
    """
    if _get_profile().name == "macos":
        return True, []
    perms = _get_perms()
    oc_dir = _user_home(bot_user) / ".openclaw"
    if not exists_or_unreachable(oc_dir):
        return True, []  # bot not bootstrapped yet — nothing to reassert

    # Tier 1: light re-widen, then VERIFY (last).
    perms.reassert_mask(oc_dir)  # `setfacl -m m::rwX .openclaw`; no-op on un-ACL'd
    # The SAME 0700-harden recompute also clamps the workspace/ ROOT dir's mask
    # (the content_scan read path for the bot's identity docs). That clamp is
    # independent of .openclaw's and was the blind spot behind the recurring
    # content_scan_file_disappeared flap (evo-vps 2026-06-29): verify facets
    # (0)-(2) never touched workspace/, so the hourly self-heal never escalated
    # and only a full redeploy repaired it. Re-widen the workspace subtree's masks
    # here in the SAME cheap Tier-1 pass — RECURSIVE so a dir-level chmod AND a
    # ``chmod -R``-clamped identity doc both self-heal within ≤1 cycle. The
    # recursive form is guarded to touch only paths that already carry an ACL
    # (getfacl -R -s), so the credentials/ + profiles carve-outs are never widened.
    ws_root = oc_dir / _WORKSPACE_ROOT_RELPATH
    if exists_or_unreachable(ws_root):
        perms.reassert_mask(ws_root, recursive=True)
    # logs/ + cron/: the OC gateway mints its app log (logs/openclaw.log) and
    # cron store (cron/jobs.json) mode 0600 — on Linux the create-mode group
    # bits become the file's ACL mask at birth, so every rewrite/rotation
    # re-clamps evolve's inherited read ACE (VPS 2026-07-29: openclaw.log at
    # mask::--- with a healthy-looking user:evolve:r-x ACE). Neither subtree
    # was in this Tier-1 pass, so the clamp never self-healed and the readers
    # (cost watchdog log-tail, cron-health audit) lived on their sudo-cat
    # fallbacks. Small bounded dirs — the recursive form only touches paths
    # whose mask actually caps an entry.
    for rel in ("logs", "cron"):
        sub = oc_dir / rel
        if exists_or_unreachable(sub):
            perms.reassert_mask(sub, recursive=True)
    # The auth-write re-clamp on agents/main/agent (verify facet (0b), the
    # 2026-07-29 evolve-vps recurrence) is OUTSIDE workspace/, so neither
    # re-widen above reaches it. Re-widen every intermediate parent of the
    # secret relpaths in the same cheap Tier-1 pass, shallow-first — the
    # per-path getfacl guard inside reassert_mask is unprivileged, so an
    # ancestor must be re-widened before its clamped child becomes visible.
    for parent in _secret_relpath_parent_dirs(oc_dir):
        if exists_or_unreachable(parent):
            perms.reassert_mask(parent)
    failures = verify_evolve_access(bot_user, perms)
    if not failures:
        return True, []

    # Tier 2: the light pass didn't fully restore — escalate to the full
    # re-grant. heal_evolve_access re-verifies internally and returns the bool.
    if heal_evolve_access(bot_id, bot_user):
        return True, []
    return False, verify_evolve_access(bot_user, perms)


def reassert_pod_evolve_access(
    bot_pairs: "list[tuple[str, str]]",
) -> "dict[str, tuple[bool, list[str]]]":
    """Run :func:`reassert_evolve_access` for every ``(bot_id, bot_user)`` pair.

    The pod-wide driver the hourly ``pod_perms_drift_monitor`` calls. Each bot
    is independent and best-effort: a raise on one bot becomes that bot's
    ``(False, [...])`` result and never aborts the sweep. Returns
    ``{bot_id: (ok, remaining_failures)}`` so the caller can fold only the
    genuinely-unhealable bots into a Signal (the self-healed ones stay silent —
    that silence is the whole point).
    """
    out: "dict[str, tuple[bool, list[str]]]" = {}
    for bot_id, bot_user in bot_pairs:
        try:
            out[bot_id] = reassert_evolve_access(bot_id, bot_user)
        except Exception as exc:  # noqa: BLE001 — one bad bot must not stop the sweep
            _log.warning(
                "reassert_pod_evolve_access: %s (%s) raised %s",
                bot_id, bot_user, exc,
            )
            out[bot_id] = (False, [f"reassert raised: {exc}"])
    return out


def check_evolve_access(bot_id: str, bot_user: str) -> "_PermCheck":
    """The evolve read/write contract as a FIRST-CLASS, self-healing drift check.

    This is the architectural fix that ends the round-6/7/8 whack-a-mole. Every
    prior round patched one more file: ``set_evolve_read_acl`` grants the
    contract ONCE at deploy time, *before* the OC gateway starts — but the
    gateway then disturbs it in ways that only surface as a daemon EACCES:

      - it re-hardens ``.openclaw`` to 0700 → the POSIX-ACL mask clamps evolve's
        inherited ``rX`` to ``---`` and it loses *traverse* (round-8 facet 1:
        defer-runner / manifest-reflex-runner can't reach the queue);
      - it creates ``agents/main/agent/auth-profiles.json`` + the queue files
        mode 0700/0600 — each clamps its OWN mask, and a ``.openclaw`` that lost
        its default ACL leaves them with no inherited ACE at all (round-8
        facet 2: evolve can't read auth-profiles).

    A fire-once grant + a log-only backstop cannot fix a disturbance that
    happens *after* it runs. So the contract is promoted to the same shape as
    every other pod perm invariant — a drift check with an apply — enforced
    where it actually needs to be: the **final health scan** at the end of
    ``setup --fresh`` (post-gateway), **every deploy** (``ensure_pod_perms``),
    and **hourly** via ``pod_perms_drift_monitor`` (``check_only=True``), which
    escalates a persistent gap to a Signal.

    The apply is ``heal_evolve_access`` — re-runs ``set_evolve_read_acl``
    (recursive ``grant_read_recursive`` recomputes every gateway-clamped
    child mask + re-plants the default ACL), explicitly re-widens the
    ``.openclaw`` mask (``setfacl -m m::rwX``), and RE-VERIFIES, returning a
    real bool so the ``ensure_pod_perms`` apply phase reports true success
    (``set_evolve_read_acl`` returns ``None`` → ``bool(None)`` was a
    perpetual "fix did not return success" false-failure). Detection rides
    ``verify_evolve_access`` (Linux EFFECTIVE-perm check via the seam); macOS
    returns no failures (no mask), so this is a structural no-op there.

    Returns a ``deploy._PermCheck`` (imported lazily to avoid the import cycle —
    deploy imports this module at module load).
    """
    from .deploy import _PermCheck  # lazy: deploy imports us

    oc_dir = _user_home(bot_user) / ".openclaw"
    if not _exists_or_unreachable(oc_dir):
        return _PermCheck(
            category="evolve-access", target=str(oc_dir), ok=True,
            detail="(bot not yet bootstrapped — skipping)",
        )
    failures = verify_evolve_access(bot_user)
    ok = not failures
    return _PermCheck(
        category="evolve-access", target=str(oc_dir), ok=ok,
        detail="contract satisfied" if ok else "; ".join(failures),
        fix_description="" if ok else f"re-assert evolve read/write ACL (recompute clamped masks) on {oc_dir}",
        apply=None if ok else (lambda b=bot_id, u=bot_user: heal_evolve_access(b, u)),
    )


def check_bot_secret_modes(bot_user: str) -> list:
    """One ``_PermCheck`` per token-bearing config file for ``bot_user``.

    Asserts mode 0600; offers a ``chmod 600`` repair when drifted. ``os.stat``
    needs only traverse on the parent dirs (granted by ``set_evolve_read_acl``),
    so observing the mode needs no sudo — only the repair does. A drifted file
    converges on every deploy, and the hourly ``pod_perms_drift_monitor`` turns
    a regression into a Signal between deploys.

    Returns ``deploy._PermCheck`` instances (imported lazily to avoid an
    import cycle — deploy.py imports this module at load time).
    """
    from .deploy import _PermCheck  # lazy: deploy imports us at module load

    oc_dir = _user_home(bot_user) / ".openclaw"
    checks: list = []
    for rel in BOT_SECRET_CONFIG_RELPATHS:
        path = oc_dir / rel
        # NB: a plain ``path.exists()`` RAISES PermissionError (it doesn't
        # swallow EACCES on modern Python) when a parent dir is non-traversable
        # for evolve — which happens on Linux when the OC gateway creates
        # ``agents/main/agent/`` mode 0700 AFTER deploy's read-ACL grant: the
        # 0700 creation clamps the inherited ``u:evolve`` traverse ACE's mask to
        # nothing. That unhandled raise crashed pod_perms_drift_monitor (W10-G
        # round-6). Classify present / absent / unreachable explicitly so the
        # daemon never dies, and turn "unreachable" into an actionable,
        # self-healing-on-apply drift (re-asserting the read ACL recomputes the
        # clamped mask — the granted ``setfacl -R -m u:evolve:rX``).
        try:
            path.stat()
        except FileNotFoundError:
            checks.append(_PermCheck(
                category="config-mode", target=str(path), ok=True,
                detail="(not present — nothing to enforce)",
            ))
            continue
        except PermissionError:
            checks.append(_PermCheck(
                category="config-mode", target=str(path), ok=False,
                detail="unreachable: a parent dir's ACL mask clamps evolve's "
                       "traverse ACE (OC created it 0700 post-deploy)",
                fix_description=f"re-assert evolve read ACL on {oc_dir}",
                apply=(lambda p=oc_dir: _reassert_evolve_read_acl(p)),
            ))
            continue
        except OSError as e:
            checks.append(_PermCheck(
                category="config-mode", target=str(path), ok=False,
                detail=f"stat failed: {e}",
            ))
            continue
        try:
            # effective_mode (not raw stat): on Linux a file carrying the
            # evolve read ACL shows the ACL *mask* in its group triad, so raw
            # stat reads 0640 even though group:: is --- and the file is
            # effectively 0600. The perms seam substitutes the real group::
            # bits; on macOS it's a plain stat. Without this the W10-G #2
            # re-grant would read as perpetual 0640 drift. (W10-G #2.)
            mode = _get_perms().effective_mode(path) & 0o777
        except OSError as e:
            checks.append(_PermCheck(
                category="config-mode", target=str(path), ok=False,
                detail=f"stat failed: {e}",
            ))
            continue
        ok = mode == BOT_SECRET_CONFIG_MODE
        checks.append(_PermCheck(
            category="config-mode", target=str(path), ok=ok,
            detail=(f"mode={oct(mode)}" if ok
                    else f"mode={oct(mode)} (token-bearing; expected {oct(BOT_SECRET_CONFIG_MODE)})"),
            fix_description="" if ok else f"chmod {_MODE_ARG} {path}",
            apply=None if ok else (lambda p=path: chmod_secret_config(p)),
        ))
    return checks


# ── Bot-OWNED (non-secret) config: ownership enforcement ─────────────────────
#
# evolve-tiers.json is per-bot model-ROUTING config — NOT a secret, so mode
# 0644 (world-readable) is correct, unlike the 0600 secrets above. The
# invariant here is OWNERSHIP, not a tight mode: a bare ``sudo /bin/cp`` (no
# ``-p``) to a *fresh* dest creates it ``root:wheel 0600`` (cp runs as root).
# The bot user — which runs ``oc_model.py`` to read/rewrite its own tiers —
# then can't read its own file, so every tier read/write 500s with
# ``[Errno 13] Permission denied: .../evolve-tiers.json`` until repaired (the
# 2026-06-16 fleet-wide repo-puller heal failure). Existing dests are preserved
# by cp, so only first-creation (new bot, post-migration recreate) drifts.
#
# Kept separate from BOT_SECRET_CONFIG_RELPATHS because the repair is
# chown-back-to-bot + chmod 644, NOT chmod 600.
BOT_OWNED_CONFIG_RELPATHS: tuple[str, ...] = (
    "evolve-tiers.json",
)
BOT_OWNED_CONFIG_MODE = 0o644
_OWNED_MODE_ARG = oct(BOT_OWNED_CONFIG_MODE)[2:]  # "644"


def _bot_user_from_path(path: "str | Path") -> "str | None":
    """Derive the owning bot user from a ``/Users/<user>/.openclaw/...`` path.

    Returns ``None`` for any path not under ``/Users/<user>/`` (Linux homes,
    test tmpdirs) — callers no-op rather than chown an unrelated file.
    """
    parts = Path(path).parts
    if len(parts) >= 3 and parts[1] == "Users":
        return parts[2]
    return None


def chown_chmod_bot_config(path: "str | Path") -> bool:
    """``sudo chown <bot>:staff`` + ``sudo /bin/chmod 644`` a bot-owned
    (non-secret) config file that a root ``cp`` may have left root-owned.

    The bot user is derived from the path, so this is correct no matter which
    user the caller runs as (the AI-Optimization writer runs as the bot; the
    deploy self-heal runs as evolve). Best-effort: returns True only when BOTH
    the chown and the chmod succeed. Sudoers grants: setup_wizard §4b.
    """
    bot_user = _bot_user_from_path(path)
    if not bot_user:
        return False
    # Route chown/chmod through the platform profile so the INVOKED binary
    # matches the path the evolve sudoers grant was rendered with (W7). On
    # Linux chown is /usr/bin/chown, not the macOS /usr/sbin/chown — a
    # hardcoded macOS path is absent from the Linux NOPASSWD allowlist, so
    # sudo would fall to a password prompt and the TTY-less admin daemon
    # fails ("sudo: a terminal is required"). The ``:staff`` primary group
    # stays literal (staff exists on Ubuntu, gid 50; per-OS primary-group
    # resolution is a separate platform-wide sweep — see deploy.py W7).
    prof = _get_profile()
    chown = subprocess.run(
        ["sudo", prof.chown, f"{bot_user}:staff", str(path)],
        capture_output=True, text=True, timeout=5,
    )
    chmod = subprocess.run(
        ["sudo", prof.chmod, _OWNED_MODE_ARG, str(path)],
        capture_output=True, text=True, timeout=5,
    )
    return chown.returncode == 0 and chmod.returncode == 0


def check_bot_tiers_ownership(bot_user: str) -> list:
    """One ``_PermCheck`` per bot-owned (non-secret) config file for
    ``bot_user``.

    Asserts the file is owned by the bot user (so the bot can read its own
    tier config) and offers a chown+chmod repair when a root-owned file is
    found — the fresh-``cp`` drift described on BOT_OWNED_CONFIG_RELPATHS.
    ``os.stat`` needs only traverse on the parent dirs (granted by
    ``set_evolve_read_acl``) and works even on a ``root:0600`` file, so the
    detection needs no sudo; only the repair does. Wired into
    ``ensure_pod_perms`` ahead of the deploy's tier heal, so a drifted file is
    converged before anything tries to read it; the hourly
    ``pod_perms_drift_monitor`` catches a regression between deploys.

    Returns ``deploy._PermCheck`` instances (imported lazily to avoid an
    import cycle — deploy.py imports this module at load time).
    """
    from .deploy import _PermCheck  # lazy: deploy imports us at module load

    try:
        bot_uid = pwd.getpwnam(bot_user).pw_uid
    except KeyError:
        return []  # unknown user (dev box / tests without bot accounts)

    oc_dir = _user_home(bot_user) / ".openclaw"
    checks: list = []
    for rel in BOT_OWNED_CONFIG_RELPATHS:
        path = oc_dir / rel
        # A bare path.exists()/path.stat() RAISES PermissionError (it doesn't
        # swallow EACCES on modern Python) when a parent dir is non-traversable
        # for evolve — the OC gateway hardens .openclaw to 0700 AFTER deploy's
        # read-ACL grant, clamping the inherited ``u:evolve`` traverse ACE's mask
        # to nothing. That unhandled raise would crash this deploy check /
        # pod_perms_drift_monitor (the W10-G round-6 class). Classify present /
        # absent / unreachable in one stat (mirrors ``check_bot_secret_modes``),
        # turning "unreachable" into a self-healing-on-apply drift — re-asserting
        # the read ACL recomputes the clamped mask (granted ``setfacl -R -m
        # u:evolve:rX``). The stat result feeds the ownership check below, so this
        # also drops the redundant second stat.
        try:
            owner_uid = path.stat().st_uid
        except FileNotFoundError:
            checks.append(_PermCheck(
                category="config-owner", target=str(path), ok=True,
                detail="(not present — nothing to enforce)",
            ))
            continue
        except PermissionError:
            checks.append(_PermCheck(
                category="config-owner", target=str(path), ok=False,
                detail="unreachable: a parent dir's ACL mask clamps evolve's "
                       "traverse ACE (OC created it 0700 post-deploy)",
                fix_description=f"re-assert evolve read ACL on {oc_dir}",
                apply=(lambda p=oc_dir: _reassert_evolve_read_acl(p)),
            ))
            continue
        except OSError as e:
            checks.append(_PermCheck(
                category="config-owner", target=str(path), ok=False,
                detail=f"stat failed: {e}",
            ))
            continue
        ok = owner_uid == bot_uid
        checks.append(_PermCheck(
            category="config-owner", target=str(path), ok=ok,
            detail=(f"owner uid={owner_uid}" if ok
                    else f"owner uid={owner_uid} (expected bot uid={bot_uid}; "
                         "root-owned → bot can't read its own tier config)"),
            fix_description="" if ok else f"chown {bot_user}:staff + chmod {_OWNED_MODE_ARG} {path}",
            apply=None if ok else (lambda p=path: chown_chmod_bot_config(p)),
        ))
    return checks


# ── Shared evolve-OWNED secret key files: 0600 enforcement ───────────────────
#
# Google Path-C service-account JSON keys + Path-A OAuth-token records live
# POD-WIDE under ``{shared_dir}/secrets/`` (NOT per-bot), owned by
# ``evolve:wheel`` mode 0600. An SA key with domain-wide delegation is
# password-equivalent for every Workspace user it can impersonate, so a
# world-readable (0644) key exposes the whole domain to every local/bot user on
# the multi-user box. (Live finding 2026-06-20: ``google-sa-prime-…json`` —
# a DwD key actively used by a bot — was found at 0644, installed 2026-06-03 by
# the OLD ``sudo-cp-then-chmod`` flow whose ``check=False`` chmod silently
# swallowed failures; see the comment in
# ``web/wizard_google_routes._install_sa_file``. The current installers all use
# ``O_NOFOLLOW`` + 0600 from inception — what was missing was a deploy-time
# self-heal for a PRE-fix key or any later drift, the way per-bot secrets get
# one via ``check_bot_secret_modes``.)
#
# Why this is the SIBLING of, not a reuse of, ``check_bot_secret_modes``:
#   • Ownership — per-bot secrets are owned by the BOT and evolve reads them via
#     an inherited ACL; these are evolve-OWNED, so evolve reads via the owner
#     bits and is itself the chmod-er. The repair is therefore a PLAIN
#     ``os.chmod`` — NO sudo grant and NO ACL re-grant. (Verified: ``secrets``
#     is not in EVOLVE_OWNED/EVO_WRITE_SHARED_SUBDIRS and the evo ACLs are
#     applied per-subdir, not at the shared-dir root, so these files carry no
#     inherited named ACE.)
#   • Detection reads the RAW stat mode (not the perms-seam ``effective_mode``
#     the per-bot check uses): for an evolve-OWNED secret the contract is
#     "raw 0600 — no group/other/named-ACE read." ``effective_mode`` would
#     substitute the real ``group::`` bits and HIDE a stray inherited read ACE
#     (reporting 0600-effective and skipping the chmod). Raw stat instead treats
#     an ACL-mask-inflated group triad as drift → chmod 600 → the mask is zeroed
#     → any stray named read ACE is neutered. That is the security-correct
#     outcome for a key that must be evolve-only.
#
# Relative to ``{shared_dir}``. A ``*.json`` match also covers the
# ``*.meta.json`` sidecars (the wizard now writes those 0600 too — the
# client_id is mildly sensitive) and the per-bot ``<bot>.json`` token records.
SHARED_SECRET_SUBDIRS: tuple[str, ...] = (
    "secrets/google_service_accounts",
    "secrets/google_oauth_tokens",
)
SHARED_SECRET_MODE = 0o600
_SHARED_MODE_ARG = oct(SHARED_SECRET_MODE)[2:]  # "600"


def chmod_shared_secret(path: "str | Path") -> bool:
    """Re-assert mode 0600 on an evolve-OWNED shared-secret key file.

    A plain chmod (no sudo) because evolve owns these files — but routed
    through an ``O_NOFOLLOW`` open + ``os.fchmod`` rather than ``os.chmod`` for
    two privileged-path safety properties:

      • **O_NOFOLLOW** — a symlink at ``path`` fails the open with ``ELOOP``
        instead of letting us chmod through to an attacker-chosen target. (A
        symlink in a 0700 evolve-owned dir can only be planted by evolve/root,
        but a privileged path defends in depth anyway.)
      • **fchmod on the opened fd** — closes the check→chmod TOCTOU window: we
        chmod the exact inode we opened, so a dir-entry swap between detection
        and repair can't redirect the chmod.

    Owner-read survives the chmod and chmod 600 zeroes any POSIX-ACL mask
    (neutering a stray inherited read ACE), so no ACL re-grant is needed.
    Returns True on success; False on any ``OSError`` — ``ELOOP`` (symlink),
    ``ENOENT`` (race), or ``EPERM`` (a root-owned residual from the legacy
    manual-runbook install, which evolve can't chmod without sudo). A False
    surfaces as a LOUD fix-failure in the ``ensure_pod_perms`` result rather
    than being swallowed — the explicit opposite of the bug that created this
    exposure.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        os.fchmod(fd, SHARED_SECRET_MODE)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def check_shared_secret_modes(shared_dir: "str | Path") -> list:
    """One ``_PermCheck`` per evolve-owned shared-secret key file under
    ``{shared_dir}/secrets/``; asserts mode 0600, offers a plain-chmod repair.

    POD-WIDE (not per-bot) — wired into ``ensure_pod_perms``'s pod-wide phase
    alongside the other shared-dir checks, so every ``evolve-admin deploy``
    re-asserts 0600 and the hourly ``pod_perms_drift_monitor`` turns a
    regression into a Signal between deploys. Idempotent and cheap: a stat per
    file, a chmod only when the mode is wrong.

    No-op (produces no checks) on pods that never configured Google integration
    — the ``secrets/`` subdirs simply don't exist.

    Returns ``deploy._PermCheck`` instances (imported lazily to avoid the
    import cycle — deploy.py imports this module at load time).
    """
    from .deploy import _PermCheck  # lazy: deploy imports us at module load

    base = Path(shared_dir)
    checks: list = []
    for subdir in SHARED_SECRET_SUBDIRS:
        d = base / subdir
        try:
            entries = sorted(d.iterdir())
        except FileNotFoundError:
            continue  # Google integration never configured on this pod
        except OSError as e:
            checks.append(_PermCheck(
                category="shared-secret-mode", target=str(d), ok=False,
                detail=f"cannot list secrets dir: {e}",
            ))
            continue
        for path in entries:
            # ``.tmp`` staging files (the installers' same-dir rename) and any
            # stray non-JSON are not the secret payload — skip them.
            if not path.name.endswith(".json"):
                continue
            # lstat (no symlink follow): a symlink in a 0700 evolve-owned
            # secrets dir is anomalous. Flag it for operator review; DON'T
            # auto-chmod through it (and chmod_shared_secret would ELOOP anyway).
            try:
                st = path.lstat()
            except OSError as e:
                checks.append(_PermCheck(
                    category="shared-secret-mode", target=str(path), ok=False,
                    detail=f"lstat failed: {e}",
                ))
                continue
            if stat.S_ISLNK(st.st_mode):
                checks.append(_PermCheck(
                    category="shared-secret-mode", target=str(path), ok=False,
                    detail="unexpected symlink in evolve-owned secrets dir "
                           "(not auto-repaired — review for tampering)",
                ))
                continue
            if not stat.S_ISREG(st.st_mode):
                continue  # subdirs / sockets — not secret key files
            # RAW mode (see the section docstring on why NOT effective_mode).
            mode = st.st_mode & 0o777
            ok = mode == SHARED_SECRET_MODE
            checks.append(_PermCheck(
                category="shared-secret-mode", target=str(path), ok=ok,
                detail=(f"mode={oct(mode)}" if ok
                        else f"mode={oct(mode)} (DwD-capable secret; "
                             f"expected {oct(SHARED_SECRET_MODE)})"),
                fix_description="" if ok else f"chmod {_SHARED_MODE_ARG} {path}",
                apply=None if ok else (lambda p=path: chmod_shared_secret(p)),
            ))
    return checks


def shared_dir_targets_excluding_checkout(shared_dir, *, profile=None):
    """Yield ``(path, recursive)`` targets that together cover *shared_dir* for
    a recursive perms pass, EXCEPT they never descend into a nested deploy
    checkout (the Linux layout ``{shared_dir}/repo``).

    - Non-nested layout (macOS sibling checkout; unknown→macOS): yields exactly
      ``(shared_dir, True)`` — byte-identical to a single ``-R`` over the root,
      and it never enumerates children, so the macOS path is unchanged.
    - Nested layout (Linux): yields ``(shared_dir, False)`` then ``(child,
      True)`` for every direct child of *shared_dir* except the deploy
      checkout. Widening the root non-recursively + each non-checkout child
      recursively reaches the same inode set as ``-R`` over the root, minus the
      pruned git tree.

    Why prune: a recursive widen that descends into the checkout flips its
    tracked files 100644→100755; with ``core.fileMode=true`` git reports them
    modified and ``git pull --ff-only`` refuses — the 2026-06-23 Linux freeze.
    """
    prof = profile or _get_profile()
    base = Path(shared_dir)
    checkout = prof.nested_deploy_checkout(base)
    if checkout is None:
        yield base, True
        return
    yield base, False
    try:
        children = sorted(base.iterdir())
    except OSError:
        # Can't enumerate (should not happen — the daemon owns shared_dir). We
        # already yielded the root non-recursively; stop rather than fall back
        # to a recursive pass that would re-enter the pruned checkout.
        return
    for child in children:
        if child == checkout:
            continue
        yield child, True


def widen_shared_dir_world_read(shared_dir, *, chmod, sudo_fallback=None, profile=None):
    """``chmod -R a+rX {shared_dir}`` that never descends into a nested deploy
    checkout — the prune-aware replacement for ``deploy_shared_dir``'s blanket
    recursive widen (the 2026-06-23 Linux freeze; see
    :func:`shared_dir_targets_excluding_checkout`).

    Best-effort, mirroring the original pass: runs the unprivileged ``chmod``
    and, only if it fails to even spawn, retries via *sudo_fallback* (a callable
    taking an argv that begins with ``"chmod"``). On the non-nested (macOS)
    layout this is exactly one ``chmod -R a+rX {shared_dir}`` — byte-identical
    to the pre-fix behavior."""
    for path, recursive in shared_dir_targets_excluding_checkout(shared_dir, profile=profile):
        flags = ["-R"] if recursive else []
        try:
            subprocess.run([chmod, *flags, "a+rX", str(path)], capture_output=True)
        except Exception:
            if sudo_fallback is not None:
                sudo_fallback(["chmod", *flags, "a+rX", str(path)])


def tighten_shared_secret_tree(shared_dir: "str | Path") -> bool:
    """Strip group/other access from the whole ``{shared_dir}/secrets/`` subtree.

    THE deploy-time re-exposer this whole fix exists for: ``deploy_shared_dir``
    runs ``chmod -R a+rX {shared_dir}`` so bots can read each other's output —
    but that recursion ALSO adds ``o+r`` to the 0600 secret keys under
    ``secrets/`` (Google SA/DwD keys + OAuth token records), re-widening a
    correctly-installed 0600 key back to 0644 on EVERY deploy. That is almost
    certainly the real cause of the 2026-06-20 live 0644 finding — not just the
    long-gone install flow. ``check_shared_secret_modes`` runs BEFORE
    ``deploy_shared_dir`` in ``deploy_bot``, so the self-heal alone is undone
    within the same deploy; this re-tightens the subtree immediately AFTER the
    ``a+rX`` pass to close the window.

    ``chmod -R go-rwx`` is the exact inverse of the ``a+rX`` widen: it zeroes
    the group/other bits (the only exposure dimension) and leaves owner bits
    untouched. On the a+rX-widened tree that means 0644 files → 0600 and 0705
    dirs → 0700. (Strictly it strips group/other rather than forcing an exact
    0600 — an owner-exec file would land 0700 — but secret keys are never
    owner-exec, and ``check_shared_secret_modes`` enforces the exact 0600
    per-file afterward.) ``-R`` does not follow symlinks on either macOS
    (default ``-P``) or GNU chmod, so a stray link can't redirect the change.
    The files are uniformly evolve-owned, so a plain chmod works as the evolve
    service user (and as root from the CLI) — NO sudo grant needed, matching the
    sibling ``a+rX`` line which also runs sudo-less on the happy path.

    Best-effort: returns True on a 0 exit (or when ``secrets/`` doesn't exist —
    non-Google pods), False otherwise. ``chmod -R`` does not abort on the first
    error, so a single unreachable file (e.g. a legacy root-owned residual)
    still leaves every other key tightened. A False is backstopped by the
    ``check_shared_secret_modes`` self-heal, which ``deploy_shared_dir`` runs in
    its trailing ``ensure_pod_perms`` pass (surfacing any per-file failure in
    the deploy log) and which the hourly ``pod_perms_drift_monitor`` re-runs.
    """
    root = Path(shared_dir) / "secrets"
    if not root.exists():
        return True
    proc = subprocess.run(
        ["/bin/chmod", "-R", "go-rwx", str(root)],
        capture_output=True, text=True, timeout=10,
    )
    return proc.returncode == 0


# ── Shared evolve-OWNED directory store (contact PII): 0600 enforcement ───────
#
# ``{shared_dir}/directory/{bot_id}.json`` (spec-user-directory-2026-06-22 §4) is
# the FIRST shared store to hold real contact emails (PII). Phase 1's
# ``user_directory.storage.save_directory`` writes it 0600 from inception — but
# the deploy-time ``chmod -R a+rX {shared_dir}`` pass (``deploy_shared_dir``)
# re-widens every file to 0644, so without a compensating re-tighten the PII rows
# land world-readable on the multi-user box after each deploy. This is the EXACT
# shape already solved for ``{shared_dir}/secrets/`` (Google SA/DwD keys) above;
# ``directory/`` gets the same treatment — a post-``a+rX`` subtree re-tighten
# (``tighten_shared_directory_tree``, composed into ``tighten_shared_pii_trees``)
# plus a per-file 0600 self-heal (``check_shared_directory_modes``) wired into
# ``ensure_pod_perms``.
#
# Why mirror ``secrets/`` and NOT the sibling ``{shared_dir}/rosters/`` store
# (the Phase-2 task's "mirror rosters/'s treatment" prompt): the roster overlay
# is *deliberately* world-readable 0644 (``roster_overlay.save_overlay`` forces
# it) for two reasons that BOTH invert for the directory store —
#   • the bot's TS ``roleResolver`` reads ``rosters/{bot}.json`` DIRECTLY per
#     turn, so it must be bot-readable; the directory store is NEVER read by the
#     bot directly (Phase 3's bot path goes through the server-side resolver), so
#     nothing needs to read it but the ``evolve`` admin (owner bits suffice); and
#   • the overlay carries NO PII — only roles, block/ignore indices, notes,
#     engagement surfaces; the directory store carries contact emails (PII).
# So the correct sibling to mirror is ``secrets/`` (the other evolve-owned
# PII-at-rest tree), NOT ``rosters/`` — matching the explicit Phase-2
# prerequisite called out in ``user_directory.storage.save_directory``'s
# docstring. Enforced PII-at-rest hardening (encryption / key custody) remains
# roadmap R3 (spec §8); 0600 is the conservative local-mode default.
#
# Like ``secrets/``: files are uniformly evolve-OWNED (the admin server writes
# them as ``evolve`` under ``{shared_dir}``'s ACL), so the repair is a PLAIN
# chmod — NO sudo grant, NO ACL re-grant — reusing ``chmod_shared_secret``
# (``O_NOFOLLOW`` + ``fchmod`` 0600, the same mode + the same TOCTOU/symlink
# safety). Detection reads the RAW stat mode (the contract is exactly "raw
# 0600 — no group/other/named-ACE read"; ``effective_mode`` would hide a stray
# inherited read ACE). The ``log/`` audit JSONL is swept too — it records email
# values in its ``from``/``to``/``after`` payloads, so it is PII at rest as well.
DIRECTORY_STORE_SUBDIR = "directory"
DIRECTORY_STORE_MODE = 0o600
_DIRECTORY_MODE_ARG = oct(DIRECTORY_STORE_MODE)[2:]  # "600"


def tighten_shared_directory_tree(shared_dir: "str | Path") -> bool:
    """Strip group/other access from the whole ``{shared_dir}/directory/`` subtree.

    The directory-store analogue of ``tighten_shared_secret_tree``:
    ``deploy_shared_dir``'s ``chmod -R a+rX {shared_dir}`` adds ``o+r`` to the
    0600 directory rows (and the ``log/`` audit JSONL, which also carries email
    values), re-widening contact PII to 0644 on EVERY deploy. This re-tightens
    the subtree immediately AFTER the ``a+rX`` pass to close that window;
    ``check_shared_directory_modes`` then enforces the exact 0600 per file in the
    trailing ``ensure_pod_perms`` pass.

    ``chmod -R go-rwx`` is the exact inverse of the ``a+rX`` widen (zeroes
    group/other, leaves owner bits) and ``-R`` follows no symlinks on macOS
    (default ``-P``) or GNU chmod, so a stray link can't redirect the change. The
    files are uniformly evolve-owned, so a plain chmod works sudo-less as the
    evolve service user (and as root from the CLI). Best-effort: True on a 0 exit
    (or when ``directory/`` doesn't exist — pods with no directory writes yet);
    ``chmod -R`` does not abort on the first error, so one unreachable file still
    leaves every other row tightened, and ``check_shared_directory_modes``
    backstops any per-file failure.
    """
    root = Path(shared_dir) / DIRECTORY_STORE_SUBDIR
    if not root.exists():
        return True
    proc = subprocess.run(
        ["/bin/chmod", "-R", "go-rwx", str(root)],
        capture_output=True, text=True, timeout=10,
    )
    return proc.returncode == 0


def tighten_shared_pii_trees(shared_dir: "str | Path") -> bool:
    """Re-tighten BOTH evolve-owned PII-at-rest trees after deploy's ``a+rX`` widen.

    ``secrets/`` (Google SA/DwD keys + OAuth token records) AND ``directory/``
    (per-bot contact emails). One call site in ``deploy_shared_dir`` runs this
    immediately after the ``chmod -R a+rX {shared_dir}`` pass so neither tree is
    left world-readable. Returns True iff both subtree re-tightens succeeded
    (each is independently best-effort and backstopped by its
    ``check_shared_*_modes`` self-heal). Composed (rather than inlined at the call
    site) so ``deploy.py`` — at its no-growth line cap — adds no line.
    """
    secrets_ok = tighten_shared_secret_tree(shared_dir)
    directory_ok = tighten_shared_directory_tree(shared_dir)
    return secrets_ok and directory_ok


def _iter_directory_store_files(root: Path) -> "list[Path]":
    """Every PII-bearing regular file under ``{shared_dir}/directory/``.

    The per-bot ``{bot_id}.json`` rows plus the ``log/*.jsonl`` audit trail (which
    records email values). Sorted for deterministic check ordering. Raises
    ``OSError`` to the caller if the tree can't be walked (surfaced as a check
    failure rather than silently skipped)."""
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.suffix in (".json", ".jsonl"):
            out.append(path)
    return out


def check_shared_directory_modes(shared_dir: "str | Path") -> list:
    """One ``_PermCheck`` per directory-store file under ``{shared_dir}/directory/``;
    asserts mode 0600, offers a plain-chmod repair.

    The contact-PII analogue of ``check_shared_secret_modes`` — same self-heal
    shape, wired into ``ensure_pod_perms``'s pod-wide phase so every
    ``evolve-admin deploy`` re-asserts 0600 and the hourly
    ``pod_perms_drift_monitor`` turns a regression into a Signal between deploys.
    Idempotent and cheap: a stat per file, a chmod only when the mode is wrong.

    Walks the per-bot ``{bot_id}.json`` rows AND ``log/*.jsonl`` (both carry email
    PII). RAW stat mode (see the shared-secret section docstring on why NOT
    ``effective_mode``: for an evolve-owned PII file the contract is exactly raw
    0600 — ``chmod 600`` also zeroes any stray inherited read ACE's mask). Skips
    symlinks (flagged for operator review, never auto-chmod'd through). No-op
    (produces no checks) on pods with no directory store yet.

    Returns ``deploy._PermCheck`` instances (imported lazily to avoid the import
    cycle — deploy.py imports this module at load time).
    """
    from .deploy import _PermCheck  # lazy: deploy imports us at module load

    root = Path(shared_dir) / DIRECTORY_STORE_SUBDIR
    if not root.exists():
        return []  # no directory writes on this pod yet — nothing to enforce
    checks: list = []
    try:
        files = _iter_directory_store_files(root)
    except OSError as e:
        return [_PermCheck(
            category="directory-mode", target=str(root), ok=False,
            detail=f"cannot list directory store: {e}")]
    for path in files:
        # lstat (no symlink follow): a symlink in an evolve-owned directory store
        # is anomalous. Flag it for operator review; DON'T auto-chmod through it
        # (and chmod_shared_secret would ELOOP anyway).
        try:
            st = path.lstat()
        except OSError as e:
            checks.append(_PermCheck(
                category="directory-mode", target=str(path), ok=False,
                detail=f"lstat failed: {e}"))
            continue
        if stat.S_ISLNK(st.st_mode):
            checks.append(_PermCheck(
                category="directory-mode", target=str(path), ok=False,
                detail="unexpected symlink in evolve-owned directory store "
                       "(not auto-repaired — review for tampering)"))
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        mode = st.st_mode & 0o777  # RAW mode — see check_shared_secret_modes docstring
        ok = mode == DIRECTORY_STORE_MODE
        checks.append(_PermCheck(
            category="directory-mode", target=str(path), ok=ok,
            detail=(f"mode={oct(mode)}" if ok
                    else f"mode={oct(mode)} (contact-PII; "
                         f"expected {oct(DIRECTORY_STORE_MODE)})"),
            fix_description="" if ok else f"chmod {_DIRECTORY_MODE_ARG} {path}",
            apply=None if ok else (lambda p=path: chmod_shared_secret(p))))
    return checks
