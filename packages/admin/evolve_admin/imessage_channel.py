"""imessage_channel — iMessage as a first-class channel (a connection, scope ``bot``).

Spec: [internal/design-imessage-channel-2026-09-29.md](../../../internal/design-imessage-channel-2026-09-29.md)
(D-IM1..5, D-IM8's offer). OpenClaw already ships the channel
(``extensions/imessage``); this module is the Evolve half:

* the **registry row** (``connections.json``, service ``imessage``) is the source
  of truth — the bot's handle, who may text it, Apple-ID mode, health;
* the **OC channel config** is *derived* from the row (:func:`derive_channel_block`)
  and written through ``_oc_install_common.write_oc_config`` (the same
  chokepoint every other channel uses), then the gateway is restarted through
  ``kickstart_gateway``;
* the **probe** (:func:`probe`) verifies by doing — handle signed in, Messages
  running in that user's session, channel up in the gateway, config matching
  the row, no open DM policy on disk;
* the **wizard** (:func:`connect`) is the one page's last step: write row +
  config, restart, probe, send the first message.

Fail closed (D-IM4). The block this module writes ALWAYS carries
``dmPolicy: "allowlist"`` and an explicit ``allowFrom`` — an empty list means
the channel receives nothing. ``open`` is never written, and ``pairing`` (OC's
own default when the key is absent) is not what the row asks for, so a config
without an explicit allowlist policy reads as drift. ``handle`` is NOT an OC
key (``channels.imessage`` is a closed schema, ``docs/schemas/oc-config-schema.txt``);
it lives on the row only.

Every side effect (osascript, pgrep, the OC CLI, the config writer, the
clock) enters through :class:`Seams`, so the tests run the whole flow with
fake time and no Mac.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from evolve_util import now_iso

from . import connection_capabilities as caps
from . import connections as conn
from .config import user_home

log = logging.getLogger(__name__)

SERVICE = "imessage"
SCOPE = "bot"
CREDENTIAL_KIND = "messages_signin"  # the credential is a Messages sign-in, held by macOS

APPLE_ID_MODES = ("own", "shared")
DEFAULT_APPLE_ID_MODE = "own"
HOST_NATIVE = "native"

# The wizard's promise (D-IM3): under three minutes end to end.
WIZARD_BUDGET_S = 180
SIGNIN_POLL_INTERVAL_S = 3
CHANNEL_UP_TIMEOUT_S = 60
CHANNEL_UP_POLL_INTERVAL_S = 3

FIRST_MESSAGE = "{bot} is here — reply to say hi"

# Keys of ``channels.imessage`` that Evolve owns. Anything else an operator
# added (``mediaMaxMb``, ``historyLimit``, ...) is preserved and never counted
# as drift; these are the keys the row decides.
MANAGED_KEYS = ("enabled", "dmPolicy", "allowFrom", "groupPolicy", "service", "dbPath")
# Keys that must be ABSENT while groups are off (D-IM5): a group allowlist or
# per-group table on disk would re-open a door the row keeps shut.
GROUP_KEYS = ("groupAllowFrom", "groups")

_PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


class ImessageChannelError(ValueError):
    """A request the module refuses (bad handle, overlap, open policy, ...).
    The message is operator-legible; routes surface it verbatim."""


# ── Handles ──────────────────────────────────────────────────────────────────


def normalize_handle(raw: str) -> str | None:
    """A sender/handle as OC compares it: E.164 phone or lower-cased email.
    Returns ``None`` for anything else (including the wildcard ``*``, which
    would be an open policy wearing an allowlist's clothes)."""
    s = (raw or "").strip()
    if not s:
        return None
    if _EMAIL_RE.match(s):
        return s.lower()
    digits = re.sub(r"[\s().-]", "", s)
    if _PHONE_RE.match(digits):
        return digits
    return None


def normalize_allow_from(raw: list[str] | None) -> list[str]:
    """Normalise + de-duplicate an allowlist, order-preserving. A malformed
    entry is refused (never silently dropped — a dropped entry is a person who
    can't reach the bot and no one told the operator)."""
    out: list[str] = []
    for item in raw or []:
        norm = normalize_handle(str(item))
        if norm is None:
            raise ImessageChannelError(
                f"{str(item).strip()!r} is not a phone number (starting with +, "
                "country code included) or an email address."
            )
        if norm not in out:
            out.append(norm)
    return out


# ── The row ──────────────────────────────────────────────────────────────────


def new_health(state: str = "unknown", reason: str | None = None) -> dict[str, Any]:
    h = conn.new_health(state, reason)  # type: ignore[arg-type]
    h.update({
        "signed_in": None,
        "channel_up": None,
        "last_inbound": None,
        "last_outbound": None,
    })
    return h


def new_row(
    *,
    bot_id: str,
    handle: str,
    macos_user: str,
    allow_from: list[str] | None = None,
    apple_id_mode: str = DEFAULT_APPLE_ID_MODE,
    groups: bool = False,
) -> dict[str, Any]:
    """A fresh ``imessage`` registry row. ``handle`` is what Messages reported
    (never typed by the operator); ``allow_from`` is normalised here."""
    if apple_id_mode not in APPLE_ID_MODES:
        raise ImessageChannelError(f"apple_id_mode must be one of {APPLE_ID_MODES}")
    if groups:
        # D-IM5: a group joins only when every member is allowlisted — the
        # group table is a later brief; until then the row cannot say True.
        raise ImessageChannelError("Group chats are off for iMessage.")
    norm_handle = normalize_handle(handle)
    if norm_handle is None:
        raise ImessageChannelError(
            "Messages did not report a usable address for this account."
        )
    row = conn.new_connection(
        bot_id=bot_id,
        service=SERVICE,
        account_label=norm_handle,
        role="own",
        jobs=["talk_over_imessage"],
        capabilities_=caps.verbs_for_job(SERVICE, "talk_over_imessage"),
        credential_ref=f"messages:{macos_user}",
        credential_kind=CREDENTIAL_KIND,
        health=new_health(),
        # An always-on channel the operator connected is standing consent;
        # ``grant_scope`` is the grant's LIFETIME (D-TF3), not the
        # ``channel:imessage`` consent scope the verbs carry.
        grant_scope="standing",
    )
    row.update({
        "scope": SCOPE,
        "handle": norm_handle,
        "allow_from": normalize_allow_from(allow_from),
        "groups": False,
        "host": HOST_NATIVE,
        "macos_user": macos_user,
        "apple_id_mode": apple_id_mode,
    })
    return row


def get_row(bot_id: str, path: Path) -> dict[str, Any] | None:
    rows = conn.for_bot_service(bot_id, SERVICE, path=path)
    return rows[0] if rows else None


def all_rows(path: Path) -> list[dict[str, Any]]:
    return [
        c for c in conn.load_connections(path).get("connections", [])
        if c.get("service") == SERVICE
    ]


def save_row(row: dict[str, Any], path: Path) -> None:
    """One row per bot: replace it if present, add it otherwise."""
    if not conn.replace_connection(row, path):
        existing = get_row(row["bot_id"], path)
        if existing is not None:
            row = dict(row, id=existing["id"])
            conn.replace_connection(row, path)
        else:
            conn.add_connection(row, path)


def refuse_shared_overlap(
    candidate_bot: str,
    candidate_allow_from: list[str],
    rows: list[dict[str, Any]],
) -> None:
    """D-IM2: bots sharing the pod's Apple ID share one Messages inbox, so a
    sender on two of their allowlists could not be routed to one bot. Refuse
    the overlap; name the bot and the number so the operator can fix it."""
    mine = set(candidate_allow_from)
    for other in rows:
        if other.get("bot_id") == candidate_bot or other.get("apple_id_mode") != "shared":
            continue
        clash = sorted(mine & set(other.get("allow_from") or []))
        if clash:
            raise ImessageChannelError(
                f"{clash[0]} is already allowed to text {other.get('bot_id')} on the "
                "pod's shared Apple ID. Two bots on one Apple ID cannot share a sender — "
                "remove it from one of them, or give this bot its own Apple ID."
            )


# ── Derivation: row → OC channel config (D-IM1, D-IM4) ───────────────────────


def derive_channel_block(
    row: dict[str, Any], *, home_for_user: Callable[[str], Path] = user_home,
) -> dict[str, Any]:
    """The ``channels.imessage`` block for ``row``. Allowlist policy ALWAYS;
    an empty ``allow_from`` yields an empty ``allowFrom`` (receives nothing).
    Never ``open``; never a wildcard; groups disabled."""
    db_path = home_for_user(row["macos_user"]) / "Library" / "Messages" / "chat.db"
    return {
        "enabled": True,
        "dmPolicy": "allowlist",
        "allowFrom": normalize_allow_from(row.get("allow_from")),
        "groupPolicy": "disabled",
        "service": "auto",
        "dbPath": str(db_path),
    }


def merge_into_config(
    cfg: dict[str, Any], row: dict[str, Any],
    *, home_for_user: Callable[[str], Path] = user_home,
) -> dict[str, Any]:
    """A copy of ``cfg`` with the derived block merged over
    ``channels.imessage`` (operator extras preserved, managed keys forced,
    group keys stripped) and the plugin entry enabled."""
    out = copy.deepcopy(cfg)
    block = out.setdefault("channels", {}).setdefault("imessage", {})
    block.pop("handle", None)  # not an OC key; a stale write from the old install flow
    for key in GROUP_KEYS:
        block.pop(key, None)
    block.update(derive_channel_block(row, home_for_user=home_for_user))
    # Per-account overrides could re-open a policy the top level closes.
    block.pop("accounts", None)
    out.setdefault("plugins", {}).setdefault("entries", {}).setdefault(SERVICE, {})["enabled"] = True
    return out


# ── Findings: open policy and drift (D-IM4, D-CN probe) ──────────────────────


def _policy_blocks(block: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out = [("channels.imessage", block)]
    accounts = block.get("accounts")
    if isinstance(accounts, dict):
        for name, sub in accounts.items():
            if isinstance(sub, dict):
                out.append((f"channels.imessage.accounts.{name}", sub))
    return out


def open_policy_findings(cfg: dict[str, Any] | None) -> list[str]:
    """Every way ``cfg`` lets an unlisted sender in. Empty = closed. Looks at
    the DM policy, the group policy, and a wildcard sitting in an allowlist."""
    block = ((cfg or {}).get("channels") or {}).get("imessage")
    if not isinstance(block, dict):
        return []
    found: list[str] = []
    for where, b in _policy_blocks(block):
        if b.get("dmPolicy") == "open":
            found.append(f"{where}.dmPolicy is \"open\"")
        if b.get("groupPolicy") == "open":
            found.append(f"{where}.groupPolicy is \"open\"")
        for key in ("allowFrom", "groupAllowFrom"):
            vals = b.get(key)
            if isinstance(vals, list) and any(str(v).strip() == "*" for v in vals):
                found.append(f"{where}.{key} contains \"*\"")
    return found


def config_drift(
    cfg: dict[str, Any] | None, row: dict[str, Any],
    *, home_for_user: Callable[[str], Path] = user_home,
) -> list[str]:
    """Human-readable diff between what the row derives and what is on disk.
    Empty = in sync. Only the keys Evolve owns are compared."""
    expected = derive_channel_block(row, home_for_user=home_for_user)
    block = ((cfg or {}).get("channels") or {}).get("imessage")
    if not isinstance(block, dict):
        return ["channels.imessage is missing from the bot's config"]
    diffs: list[str] = []
    for key in MANAGED_KEYS:
        want, have = expected[key], block.get(key)
        if key == "allowFrom":
            have_n = sorted(str(v).strip().lower() for v in (have or [])) if isinstance(have, list) else have
            if have_n != sorted(want):
                diffs.append(f"allowFrom: expected {want}, found {have}")
        elif have != want:
            diffs.append(f"{key}: expected {want!r}, found {have!r}")
    for key in GROUP_KEYS:
        if key in block:
            diffs.append(f"{key}: expected absent (groups are off), found {block[key]!r}")
    if "accounts" in block:
        diffs.append("accounts: expected absent, found a per-account override")
    entry = (((cfg or {}).get("plugins") or {}).get("entries") or {}).get(SERVICE) or {}
    if entry.get("enabled") is not True:
        diffs.append("plugins.entries.imessage.enabled: expected true")
    return diffs


# ── Seams ────────────────────────────────────────────────────────────────────


def _default_read_config(bot_id: str) -> tuple[dict | None, str | None]:
    from .skills import _oc_install_common as oc
    return oc.read_oc_config(bot_id)


def _default_write_config(bot_id: str, cfg: dict) -> tuple[bool, str | None]:
    from .skills import _oc_install_common as oc
    return oc.write_oc_config(bot_id, cfg)


def _default_restart_gateway(bot_id: str) -> tuple[bool, str | None]:
    from .skills import _oc_install_common as oc
    return oc.kickstart_gateway(bot_id)


# The sign-in handle keys ``channels status`` may carry, in preference order.
_STATUS_HANDLE_KEYS = ("handle", "account", "selfHandle", "selfId")


def handle_from_status(st: dict[str, Any] | None) -> str | None:
    """The signed-in handle a ``channels status`` read names, normalized, or None."""
    for key in _STATUS_HANDLE_KEYS:
        norm = normalize_handle(str((st or {}).get(key) or ""))
        if norm:
            return norm
    return None


def _default_signed_in_handle(macos_user: str, bot_id: str) -> str | None:
    """Read back the handle Messages is signed in with — never typed, and
    never from a file the bot can write.

    The only source is the gateway's own ``channels status`` read. There is
    deliberately no fallback to anything under the bot's home: its agent can
    write ``workspace/evolve``, and a sign-in a bot can forge is a probe that
    fails open (review pr-4667 second pass, finding 1). An unreadable status
    is None — "not signed in" — and ``probe`` says why."""
    from .skills import imessage_install as ii
    try:
        st = ii._probe_oc_channel_status(bot_id)
    except Exception:  # noqa: BLE001 — a probe crash reads as "not known", not a wizard crash
        return None
    return handle_from_status(st)


def _default_messages_running(macos_user: str) -> bool:
    try:
        r = subprocess.run(
            ["/usr/bin/pgrep", "-x", "-u", macos_user, "Messages"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def _default_channel_status(bot_id: str) -> dict[str, Any]:
    from .skills import imessage_install as ii
    return ii._probe_oc_channel_status(bot_id)


def _default_send(bot_id: str, to: str, text: str) -> tuple[bool, str | None]:
    """Send through the bot's own gateway channel (``openclaw message send``),
    so the message leaves as the bot's Apple ID, in the bot's session."""
    from .config import bot_home
    from .deploy import _openclaw_bin
    home = bot_home(bot_id)
    env = {
        "OPENCLAW_CONFIG_PATH": str(home / ".openclaw" / "openclaw.json"),
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
    }
    try:
        r = subprocess.run(
            [_openclaw_bin(), "message", "send", "--channel", SERVICE,
             "--target", to, "--message", text],
            capture_output=True, text=True, timeout=30, env=env, cwd=str(home),
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"send_failed: {exc.__class__.__name__}"
    if r.returncode != 0:
        return False, (r.stderr or "send_failed").strip()[:200]
    return True, None


@dataclass
class Seams:
    """Everything the flow touches outside this module. Defaults are the real
    thing; tests replace them. ``sleep`` and ``clock`` are a pair — pass a
    fake clock and a sleep that advances it and the wizard runs in zero real
    time while its timing stays measurable."""

    read_config: Callable[[str], tuple[dict | None, str | None]] = _default_read_config
    write_config: Callable[[str, dict], tuple[bool, str | None]] = _default_write_config
    restart_gateway: Callable[[str], tuple[bool, str | None]] = _default_restart_gateway
    signed_in_handle: Callable[[str, str], str | None] = _default_signed_in_handle
    messages_running: Callable[[str], bool] = _default_messages_running
    channel_status: Callable[[str], dict[str, Any]] = _default_channel_status
    send: Callable[[str, str, str], tuple[bool, str | None]] = _default_send
    home_for_user: Callable[[str], Path] = user_home
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], str] = now_iso


# ── Step 1: sign in (the one by-hand step) ───────────────────────────────────


def signin_instruction(macos_user: str, *, shared: bool = False) -> dict[str, Any]:
    """What the wizard's first step shows. The by-hand step, and only that one."""
    who = "the pod's shared Apple ID" if shared else "this bot's Apple ID"
    return {
        "macos_user": macos_user,
        "text": (
            f"On the Mac, sign in to Messages as the user “{macos_user}” "
            f"(screen sharing works) with {who}, and accept the two-factor prompt. "
            "This page notices when it's done — there is nothing to type."
        ),
    }


def wait_for_signin(
    bot_id: str, macos_user: str, *, seams: Seams, timeout_s: float = WIZARD_BUDGET_S,
) -> str | None:
    """Poll the read-back until Messages reports a handle, or ``timeout_s``
    passes. Returns the handle, or ``None`` (the page keeps polling — the
    operator may simply not have signed in yet)."""
    deadline = seams.clock() + timeout_s
    while True:
        handle = seams.signed_in_handle(macos_user, bot_id)
        if handle:
            return handle
        if seams.clock() >= deadline:
            return None
        seams.sleep(SIGNIN_POLL_INTERVAL_S)


# ── Step 2: who may text it (pre-fill) ───────────────────────────────────────


def prefill_allow_from(network: dict[str, Any], bot_id: str) -> list[str]:
    """Suggested allowlist for the wizard: the iMessage handles the pod's
    roster already records for this bot's primary user and the pod admins
    (``external_ids["imessage"]``). Exact recorded ids only — nothing is
    inferred from a name or a Telegram id (roster_identity's rule: a wrong
    link is a privilege transfer). The operator confirms every entry."""
    bot = (network.get("bots") or {}).get(bot_id) or {}
    sources = [
        ((bot.get("primary_user") or {}).get("external_ids") or {}),
        (((network.get("pod") or {}).get("admins") or {}).get("external_ids") or {}),
    ]
    out: list[str] = []
    for ext in sources:
        for raw in ext.get(SERVICE) or []:
            norm = normalize_handle(str(raw))
            if norm and norm not in out:
                out.append(norm)
    return out


def telegram_binding(cfg: dict[str, Any] | None) -> bool:
    """Does the bot carry an enabled Telegram channel (the binding the wizard
    offers to retire)?"""
    tg = ((cfg or {}).get("channels") or {}).get("telegram")
    return isinstance(tg, dict) and tg.get("enabled") is not False and bool(tg)


def retire_telegram_binding(bot_id: str, *, seams: Seams) -> tuple[bool, str | None]:
    """Remove ``channels.telegram`` + its plugin entry. Only ever called when
    the operator accepted the offer — see :func:`connect` (``retire_telegram``
    defaults False; a decline keeps the binding untouched)."""
    cfg, err = seams.read_config(bot_id)
    if cfg is None:
        return False, err or "oc_read_failed"
    cfg = copy.deepcopy(cfg)
    (cfg.get("channels") or {}).pop("telegram", None)
    ((cfg.get("plugins") or {}).get("entries") or {}).pop("telegram", None)
    return seams.write_config(bot_id, cfg)


# ── Probe (verified by doing) ────────────────────────────────────────────────


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class ProbeResult:
    checks: list[Check] = field(default_factory=list)
    refuse: bool = False  # an open policy on disk: do not proceed
    status: dict[str, Any] = field(default_factory=dict)  # OC's raw channel status

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks) and not self.refuse

    @property
    def reason(self) -> str | None:
        bad = [c for c in self.checks if not c.ok]
        return "; ".join(c.detail or c.name for c in bad) if bad else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "refuse": self.refuse, "reason": self.reason,
            "checks": [c.to_dict() for c in self.checks],
        }


def probe(row: dict[str, Any], *, seams: Seams) -> ProbeResult:
    """Run every check; never stops at the first red (the operator gets the
    whole picture). ``refuse`` is set when an open DM policy is on disk — the
    wizard must not continue past it."""
    bot_id, user = row["bot_id"], row["macos_user"]
    res = ProbeResult()

    # The gateway's own channel read — the sign-in check's reason when it
    # names no account, and check 5 below.
    st = seams.channel_status(bot_id)

    # 1. The handle Messages is actually signed in with — and it is the row's.
    # Read from the pod only (never a bot-writable file); red with the reason.
    handle = seams.signed_in_handle(user, bot_id)
    if not handle:
        why = str(st.get("error") or st.get("detail") or "the channel status names no signed-in account")
        res.checks.append(Check(
            "signed_in", False,
            f"Messages is not signed in as {user} (read from the pod: {why})",
        ))
    elif handle != row.get("handle"):
        res.checks.append(Check(
            "signed_in", False,
            f"Messages is signed in as {handle}, but this connection is for {row.get('handle')}",
        ))
    else:
        res.checks.append(Check("signed_in", True, handle))

    # 2. Messages.app is open in that user's session.
    running = seams.messages_running(user)
    res.checks.append(Check(
        "messages_running", running,
        "" if running else f"Messages is not running in {user}'s session",
    ))

    # 3+4. The config on disk: not open, and equal to what the row derives.
    cfg, err = seams.read_config(bot_id)
    if cfg is None:
        res.checks.append(Check("config", False, err or "could not read the bot's config"))
    else:
        opened = open_policy_findings(cfg)
        if opened:
            res.refuse = True
            res.checks.append(Check("dm_policy", False, "open policy on disk: " + "; ".join(opened)))
        else:
            res.checks.append(Check("dm_policy", True, "allowlist"))
        drift = config_drift(cfg, row, home_for_user=seams.home_for_user)
        res.checks.append(Check("config_matches_row", not drift, "; ".join(drift)))

    # 5. The channel is up in the gateway (OC's own probe — never inferred
    # from config presence).
    up = bool(st.get("connected"))
    res.checks.append(Check(
        "channel_up", up, "" if up else str(st.get("error") or st.get("detail") or "not connected"),
    ))
    res.status = st
    return res


def record_probe(row: dict[str, Any], result: ProbeResult, *, seams: Seams) -> dict[str, Any]:
    """Fold a probe into the row's health (``verified`` / ``failed``, with the
    reason) and the channel fields the Connections page and pod report read."""
    st = result.status
    by_name = {c.name: c for c in result.checks}
    h = dict(row.get("health") or new_health())
    h["state"] = "verified" if result.ok else "failed"
    h["reason"] = result.reason
    h["last_probe"] = seams.now()
    h["verbs_verified"] = list(row.get("capabilities") or []) if result.ok else []
    h["signed_in"] = by_name["signed_in"].ok if "signed_in" in by_name else None
    h["channel_up"] = by_name["channel_up"].ok if "channel_up" in by_name else None
    for key, src in (("last_inbound", "lastInboundAt"), ("last_outbound", "lastOutboundAt")):
        if st.get(src):
            h[key] = st[src]
    row["health"] = h
    return row


def run_probe(bot_id: str, path: Path, *, seams: Seams | None = None) -> tuple[ProbeResult | None, dict | None]:
    """Probe a bot's stored row and persist the resulting health."""
    seams = seams or Seams()
    row = get_row(bot_id, path)
    if row is None:
        return None, None
    row = copy.deepcopy(row)
    res = probe(row, seams=seams)
    record_probe(row, res, seams=seams)
    conn.replace_connection(row, path)
    return res, row


def probe_all(path: Path, *, seams: Seams | None = None) -> list[tuple[str, ProbeResult]]:
    """Probe every stored iMessage row and persist each one's health — the
    periodic path (``evolve-admin health``) that keeps the Connections page
    and the pod report's channel line honest between wizard visits."""
    out: list[tuple[str, ProbeResult]] = []
    for row in all_rows(path):
        res, _ = run_probe(row["bot_id"], path, seams=seams)
        if res is not None:
            out.append((row["bot_id"], res))
    return out


# ── Step 3: connect ──────────────────────────────────────────────────────────


@dataclass
class ConnectResult:
    ok: bool
    stage: str  # where it stopped (or "done")
    detail: str = ""
    elapsed_s: float = 0.0
    probe: dict[str, Any] | None = None
    first_message_to: str | None = None
    telegram_retired: bool = False
    row: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "stage": self.stage, "detail": self.detail,
            "elapsed_s": round(self.elapsed_s, 1), "probe": self.probe,
            "first_message_to": self.first_message_to,
            "telegram_retired": self.telegram_retired,
            "row": None if self.row is None else public_row(self.row),
        }


def public_row(row: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "bot_id", "service", "scope", "handle", "allow_from", "groups", "host",
            "macos_user", "apple_id_mode", "capabilities", "health", "added_at")
    out = {k: row.get(k) for k in keep}
    out["health_display"] = conn.health_display(row.get("health"))
    return out


def connect(
    *,
    bot_id: str,
    macos_user: str,
    allow_from: list[str],
    connections_file: Path,
    apple_id_mode: str = DEFAULT_APPLE_ID_MODE,
    retire_telegram: bool = False,
    seams: Seams | None = None,
) -> ConnectResult:
    """Step 3 of the wizard. Reads the signed-in handle (never typed), refuses
    a shared-mode overlap and any open policy already on disk, writes the row
    and the derived config, restarts the gateway, probes, and sends the first
    message to the first allowed sender.

    ``retire_telegram`` is the opt-in half of the D-IM8 offer: False (the
    default, and what a declined offer sends) leaves the Telegram binding
    exactly as it was."""
    seams = seams or Seams()
    t0 = seams.clock()

    def done(ok: bool, stage: str, detail: str = "", **kw: Any) -> ConnectResult:
        return ConnectResult(ok, stage, detail, elapsed_s=seams.clock() - t0, **kw)

    try:
        allow = normalize_allow_from(allow_from)
        handle = seams.signed_in_handle(macos_user, bot_id)
        if not handle:
            return done(False, "sign_in", signin_instruction(macos_user)["text"])
        row = new_row(bot_id=bot_id, handle=handle, macos_user=macos_user,
                      allow_from=allow, apple_id_mode=apple_id_mode)
        if apple_id_mode == "shared":
            refuse_shared_overlap(bot_id, allow, all_rows(connections_file))
    except ImessageChannelError as exc:
        return done(False, "validate", str(exc))

    cfg, err = seams.read_config(bot_id)
    if cfg is None:
        return done(False, "read_config", err or "could not read the bot's config")
    opened = open_policy_findings(cfg)
    if opened:
        return done(False, "open_policy", "Refusing to continue: " + "; ".join(opened) +
                    ". Fix it in the bot's config (Evolve never writes an open policy).")

    existing = get_row(bot_id, connections_file)
    if existing is not None:
        row["id"], row["added_at"] = existing["id"], existing.get("added_at", row["added_at"])
    save_row(row, connections_file)

    ok, err = seams.write_config(bot_id, merge_into_config(cfg, row, home_for_user=seams.home_for_user))
    if not ok:
        return done(False, "write_config", err or "config write failed", row=row)

    telegram_retired = False
    if retire_telegram and telegram_binding(cfg):
        telegram_retired, err = retire_telegram_binding(bot_id, seams=seams)
        if not telegram_retired:
            log.warning("imessage connect(%s): telegram retire failed: %s", bot_id, err)

    ok, err = seams.restart_gateway(bot_id)
    if not ok:
        return done(False, "restart", err or "gateway restart failed", row=row,
                    telegram_retired=telegram_retired)

    res = _await_channel_up(row, seams)
    record_probe(row, res, seams=seams)
    conn.replace_connection(row, connections_file)
    if not res.ok:
        return done(False, "probe", res.reason or "probe failed", row=row,
                    probe=res.to_dict(), telegram_retired=telegram_retired)

    first_to: str | None = None
    if allow:
        first_to = allow[0]
        sent, err = seams.send(bot_id, first_to, FIRST_MESSAGE.format(bot=bot_id))
        if not sent:
            return done(False, "first_message", err or "send failed", row=row,
                        probe=res.to_dict(), telegram_retired=telegram_retired)
        row["health"]["last_outbound"] = seams.now()
        conn.replace_connection(row, connections_file)
    return done(True, "done",
                "" if allow else "Connected — nobody is on the allowlist yet, so it receives nothing.",
                row=row, probe=res.to_dict(), first_message_to=first_to,
                telegram_retired=telegram_retired)


def _await_channel_up(row: dict[str, Any], seams: Seams) -> ProbeResult:
    """Probe until the channel reports up, bounded by CHANNEL_UP_TIMEOUT_S —
    the gateway takes a few seconds to come back after the restart. Any other
    red (config drift, open policy) does not improve with waiting, so the
    loop only waits while ``channel_up`` is the sole failing check."""
    deadline = seams.clock() + CHANNEL_UP_TIMEOUT_S
    while True:
        res = probe(row, seams=seams)
        failing = {c.name for c in res.checks if not c.ok}
        if res.ok or failing != {"channel_up"} or seams.clock() >= deadline:
            return res
        seams.sleep(CHANNEL_UP_POLL_INTERVAL_S)


def send_message(bot_id: str, to: str, text: str, *, seams: Seams | None = None) -> tuple[bool, str | None]:
    """Send ``text`` to ``to`` through the bot's iMessage channel. The verb
    ``imessage.send`` in the capability map names this function."""
    seams = seams or Seams()
    norm = normalize_handle(to)
    if norm is None:
        return False, "not a phone number or email address"
    return seams.send(bot_id, norm, text)


# ── Keeper launch agent (Messages stays open in the bot's session) ───────────


def keeper_label(macos_user: str) -> str:
    # ``ai.openclaw.*`` so the pod's existing launchctl bootstrap/bootout
    # grants for user LaunchAgents cover it (setup_wizard §8) — one Messages
    # instance per macOS user, so the label is per user, not per bot.
    return f"ai.openclaw.imessage-keeper.{macos_user}"


def keeper_plist_content(macos_user: str) -> str:
    """The per-user LaunchAgent that keeps Messages.app open in that user's
    session. It writes nothing: the sign-in read-back comes from the gateway's
    ``channels status``, never from a file in a home the bot can write."""
    import plistlib
    spec = {
        "Label": keeper_label(macos_user),
        "ProgramArguments": ["/usr/bin/open", "-g", "-a", "Messages"],
        "RunAtLoad": True,
        "StartInterval": 60,
        "LimitLoadToSessionType": "Aqua",
    }
    return plistlib.dumps(spec).decode("utf-8")


def keeper_plist_path(macos_user: str, *, home_for_user: Callable[[str], Path] = user_home) -> Path:
    return home_for_user(macos_user) / "Library" / "LaunchAgents" / f"{keeper_label(macos_user)}.plist"


def staged_keeper_path_ok(path: str) -> bool:
    """True when ``path`` is a direct child of /tmp named like the sudoers
    source glob, with no ``..`` (or any other) path component in between."""
    p = Path(path)
    return (
        p.is_absolute()
        and ".." not in p.parts
        and p.parent == Path("/tmp")
        and p.name.startswith("evolve-imsg-keeper-")
        and p.name.endswith(".plist")
    )


def install_keeper(
    macos_user: str,
    *,
    run: Callable[..., Any] | None = None,
    home_for_user: Callable[[str], Path] = user_home,
) -> tuple[bool, str | None]:
    """Install + load the keeper agent in ``macos_user``'s GUI session. Uses
    /tmp staging + ``sudo /bin/cp`` (the file lands in a home the daemon can't
    write), then ``launchctl bootstrap gui/<uid>``. Idempotent."""
    import os
    import tempfile
    from platform_profile import get_profile
    run = run or subprocess.run
    dest = keeper_plist_path(macos_user, home_for_user=home_for_user)
    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix="evolve-imsg-keeper-", suffix=".plist")
    try:
        # The sudoers source is a `/tmp/evolve-imsg-keeper-*.plist` glob, and a
        # sudoers `*` spans `/` — so `..` would carry a root `cp` out of /tmp.
        # Refuse anything but a direct child of /tmp before calling it.
        if not staged_keeper_path_ok(tmp):
            os.close(fd)
            return False, f"refused: staged keeper path {tmp!r} is not a plain /tmp file"
        with os.fdopen(fd, "w") as f:
            f.write(keeper_plist_content(macos_user))
        for argv in (
            ["sudo", "/bin/cp", tmp, str(dest)],
            ["sudo", get_profile().chown, f"{macos_user}:staff", str(dest)],
        ):
            r = run(argv, capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return False, (r.stderr or "install failed").strip()[:200]
        uid = run(["/usr/bin/id", "-u", macos_user], capture_output=True, text=True, timeout=5)
        target = f"gui/{uid.stdout.strip()}"
        # bootout first so a changed plist reloads; rc ignored (may not be loaded).
        run(["sudo", "/bin/launchctl", "bootout", f"{target}/{keeper_label(macos_user)}"],
            capture_output=True, text=True, timeout=10)
        r = run(["sudo", "/bin/launchctl", "bootstrap", target, str(dest)],
                capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return False, (r.stderr or "bootstrap failed").strip()[:200]
        return True, None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
