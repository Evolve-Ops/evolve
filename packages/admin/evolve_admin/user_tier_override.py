"""user_tier_override — one door for the per-bot ``userTierOverride`` write.

``evolve-admin models cap <bot> <n>`` and ``evolve-admin models
user-tier-control <bot> on|off`` are the CLI half of the per-bot Power
opt-out (internal/spec-user-tier-control-2026-05-26.md §"Per-bot opt-out +
adjustable daily cap"). Both funnel through :func:`apply_user_tier_override`.

WHY THIS MODULE EXISTS — the file-path mismatch it closes
---------------------------------------------------------
Until this module, both commands wrote ``{sharedDir}/{bot}/tiers.json``
directly with ``Path.write_text``. That is NOT the file the gateway plugin
routes on. ``loadTiersFile`` (packages/plugin/src/observer/ModelRouter.ts)
resolves in this order:

  1. ``~/.openclaw/evolve-tiers.json`` — the canonical file, and the only one
     read on any bot that has it (every bot on the reference pod does);
  2. ``{sharedDir}/{botId}/tiers.json`` — a back-compat fallback, reached only
     when #1 is absent or unparseable.

So on a real pod the CLI's cap/opt-out never reached ``ModelRouter`` at all:
it wrote a file routing does not consult and printed a green check. This is
the same mismatch ModelRouter.ts records as fixed pre-2026-05-28 for the READ
side ("the plugin ONLY read path #2, but the UI ONLY wrote path #1") — the
CLI write side was never migrated with it.

The write now goes through ``runtime.full_config_set_with_error`` — the SAME
seam the admin UI's ``PUT /api/admin/config/<bot>/user-tier-override`` uses,
which lands in the bot's ``~/.openclaw/evolve-tiers.json`` via
``oc_model._save_tiers_file``. Going through that seam rather than
re-implementing the write is what buys the ownership handling for free: the
file is owned by the BOT user, so writing it needs /tmp staging + ``sudo
/bin/cp`` + ``chown`` and a **0644** (not 0600) mode, or the bot cannot read
its own routing config. ``_save_tiers_file`` already does all of that, plus
the symlink-dest refusal (``assert_safe_sudo_dest``); a hand-rolled writer
here would be a second copy of that contract to keep in sync.

WHY ``{sharedDir}/{bot}/tiers.json`` IS STILL WRITTEN (as a mirror)
------------------------------------------------------------------
It is not dead: ``home_chat_routes._read_user_tier_override`` reads THAT path
and nothing else, and the Power gate in ``/api/home/chat`` uses its
``dailyCap`` to downgrade Power → standard (``dailyCap: 0`` = the operator's
per-bot "Power disabled"). Dropping the write would move the CLI's bug from
"routing never sees it" to "the admin chat gate never sees it". So both are
written: the bot-home file is authoritative, the shared-dir file is a mirror
of whatever landed there.

THE MIRROR QUESTION, FOR THE ADMIN UI'S PUT — SETTLED: it mirrors too
--------------------------------------------------------------------
``PUT /api/admin/config/<bot>/user-tier-override`` used to write only the
canonical file, so ``home_chat_routes`` was blind to UI-set overrides: an
operator who set the documented ``{enabled: false, dailyCap: 0}`` opt-out from
the AI Optimization page still got a working Power chip in admin chat. That is
the same shape of divergence as the one the CLI fix closed, on the other
surface, so the endpoint now goes through this module and mirrors as well.

Three things made "mirror" the answer rather than "don't":

* **The trust gate does not bite.** ``home_chat_routes._trusted_uids`` is
  ``{0, os.getuid()}``, and the mirror written from the endpoint is owned by
  the admin daemon's own uid — the very uid doing the reading, since that
  reader lives in this same process. So mirroring can only ever deliver the
  operator's value, never a ``_UNTRUSTED_DAILY_CAP_CEILING`` clamp of it.
* **One mirror writer, not two.** The copy lives in
  :func:`_mirror_to_shared_dir` and stays there; the endpoint gains a call
  site, not a second implementation of the same contract.
* **Not mirroring has a cost that only ever grows.** Every future reader of
  the mirror inherits "…unless the operator used the web UI".

What is deliberately NOT done here is the other direction — repointing
``home_chat_routes`` at the canonical file. Its uid-trust gate
(``_read_tiers_file``'s ``O_NOFOLLOW`` fstat plus the
``_UNTRUSTED_DAILY_CAP_CEILING`` clamp) is written against the shared-dir
threat model (#3566 A-1) and needs re-deriving for a bot-OWNED file, where
"owner is not an operator uid" is the normal case rather than the alarm.

WHY THE CAP ALSO WRITES ``roleCaps.power.maxPerDayPerBot``
----------------------------------------------------------
Landing in the right FILE was only half the bug. ``userTierOverride.dailyCap``
is a legacy key the gateway shadows completely: the router's models source
folds ``DEFAULT_MODEL_CATALOG`` in as its base layer, that catalog ships
``roleCaps.power.maxPerDayPerBot: 10`` in code, and both legacy folds
(``_mergeRoleCaps``, ``_roleCap``) are reached only when no ``power`` cap
exists in the merged block — which is never. So ``models cap`` reached routing
on NO pod, not merely on migrated ones. ``power_cap`` carries the full
derivation and the measurement behind it.

The coupling lives in :func:`_write`, not in one command's handler, and that
placement is the point: "a write that carries ``dailyCap`` also carries
``roleCaps.power.maxPerDayPerBot``" is an invariant of the KEY, not of the
caller. Putting it at the one write seam is what let the admin UI's PUT be
fixed by routing it through this module rather than by growing a second copy
of the rule — which is how ``models cap`` and the endpoint came to disagree in
the first place.

That write is a READ-MERGE, and must stay one: ``json_full_config_set``'s
``roleCaps`` is a WHOLESALE replace (it is the "Customize this bot" payload
shape), so sending ``{"power": ...}`` alone would delete a customized bot's
``max`` cap — widening Fable spend as a side effect of lowering Power spend.
``power_cap.merge_power_cap`` is the one place that merge lives.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Keys ``oc_model.json_full_config_set`` accepts inside the block. Anything
#: else is silently dropped there, which would make a typo'd field read as a
#: successful write — reject here instead so a caller learns at the CLI.
ALLOWED_OVERRIDE_KEYS = frozenset(
    {"enabled", "dailyCap", "allowBotInitiated", "defaultTier"}
)

#: Basename of the legacy shared-dir mirror. Kept as a constant because the
#: plugin's fallback branch and ``home_chat_routes`` both hard-code it.
MIRROR_FILENAME = "tiers.json"

#: Audit action recorded for a write that came from the CLI. The admin UI's
#: PUT records its own (``config.user_tier_override.set``) so the operator log
#: keeps the two surfaces apart — it passes ``audit_action=None`` and audits
#: after, with the ``tiers:<key>`` names taken off the returned write result.
CLI_AUDIT_ACTION = "models.user_tier_override.set"


class UserTierOverrideWriteError(RuntimeError):
    """The canonical write failed, or reported success without landing."""


def apply_user_tier_override(
    network: dict[str, Any],
    network_path: Path,
    bot: str,
    updates: dict[str, Any],
    *,
    audit_action: "str | None" = CLI_AUDIT_ACTION,
) -> dict[str, Any]:
    """Merge ``updates`` into the bot's ``userTierOverride`` block.

    THE one door for this write — the CLI's two commands and the admin UI's
    ``PUT /api/admin/config/<bot>/user-tier-override`` all arrive here, so the
    file pair, the ``roleCaps`` coupling and the heal declaration cannot drift
    between surfaces the way they did until 2026-09-10.

    Writes ``<bot home>/.openclaw/evolve-tiers.json::userTierOverride`` (the
    file ``ModelRouter.loadTiersFile`` reads first) and mirrors the result to
    ``{sharedDir}/{bot}/tiers.json`` for ``home_chat_routes``. See the module
    docstring for why both.

    A ``dailyCap`` in ``updates`` also lands
    ``roleCaps.power.maxPerDayPerBot`` — the key the gateway actually enforces
    — read-merged over the bot's existing block, in the SAME write. See
    :func:`_write`.

    ``audit_action`` names the action the write is recorded under, or is
    ``None`` for a caller that records its own entry (the admin UI endpoint,
    which keeps ``config.user_tier_override.set`` so the operator log
    distinguishes a page click from a CLI run). ``None`` means this function
    declares NOTHING to heal — the caller must, from the returned result via
    ``routes_shared.tier_write_oc_keys``, or the write reads as foreign drift
    on the next cycle.

    ``updates`` is a PARTIAL block — send only the fields the command owns.
    The merge happens in ``oc_model.json_full_config_set`` against the
    canonical file's current contents, so an unsent sibling keeps its value.
    That is the reason this does not fill in a default for the sibling field
    the way the pre-migration CLI did: that default was derived from the
    *shared-dir* file, so a ``models cap`` run could resurrect ``enabled:
    true`` over a canonical ``enabled: false`` the stale mirror never knew
    about. Both readers already default ``enabled`` to true and ``dailyCap``
    to 10 when the key is absent, so nothing is lost by leaving it out.

    Returns the full post-write ``json_full_config`` — not just the override
    block — because a caller needs the siblings (``roleCaps``,
    ``tiersKeysWritten``) to verify the coupled key and declare the write
    without paying for a second read. Raises
    :class:`UserTierOverrideWriteError` when the write failed or did not
    persist — a truthy setter result is not proof of persistence (the lesson
    ``model_tier_apply`` / ``models rollback`` already encode).
    """
    return _write(
        network, network_path, bot, updates, audit_action=audit_action,
    )


def apply_power_daily_cap(
    network: dict[str, Any],
    network_path: Path,
    bot: str,
    value: int,
) -> int:
    """Set the bot's Power daily cap where the gateway actually reads it.

    Writes BOTH keys in one call:

    * ``roleCaps.power.maxPerDayPerBot`` — what ``ModelRouter._roleCap``
      enforces, read-merged over the bot's existing ``roleCaps`` so no other
      role's cap is dropped (see the module docstring);
    * ``userTierOverride.dailyCap`` — the legacy mirror, kept for the readers
      that have not moved (``home_chat_routes._read_user_tier_override``).

    One ``full_config_set_with_error`` call, so the two keys cannot land
    apart and leave the two readers disagreeing about the operator's cap.

    Raises :class:`UserTierOverrideWriteError` when the bot's current config
    cannot be READ — a degraded read would look like "this bot has no
    roleCaps" and the merge would then replace the block instead of extending
    it. Fail closed: refuse the write and say so, rather than silently
    widening a cap this process could not see.

    Returns **the cap the gateway will now enforce**, resolved from the
    post-write config through ``power_cap.resolve_from_config_view``. Not
    ``value`` echoed back: reporting the number the operator typed is what
    this command did for four months while routing used a different one, and
    the caller prints this so an operator is told what the machine will do.
    """
    from .power_cap import resolve_from_config_view

    return resolve_from_config_view(network, apply_user_tier_override(
        network, network_path, bot, {"dailyCap": value},
    ))


def _write(
    network: dict[str, Any],
    network_path: Path,
    bot: str,
    updates: dict[str, Any],
    *,
    audit_action: "str | None",
) -> dict[str, Any]:
    """The single ``userTierOverride`` write, with its coupled siblings.

    A ``dailyCap`` in ``updates`` pulls ``roleCaps`` into the SAME
    ``full_config_set_with_error`` payload, so the legacy key and the key the
    gateway enforces cannot land apart and leave the two readers reporting
    different caps. Both halves are then verified against the returned
    post-write config.

    Returns the full post-write ``json_full_config`` result — not just the
    override block — because that is what a caller needs to verify a sibling
    key, declare the write, and report the effective cap without paying for a
    second read.
    """
    unknown = set(updates) - ALLOWED_OVERRIDE_KEYS
    if unknown:
        raise UserTierOverrideWriteError(
            f"unknown userTierOverride field(s): {sorted(unknown)}; "
            f"allowed: {sorted(ALLOWED_OVERRIDE_KEYS)}"
        )

    from runtime.agent_runtime import get_runtime  # type: ignore[import-not-found]

    runtime = get_runtime()
    payload: dict[str, Any] = {"userTierOverride": dict(updates)}
    # ``is not None``, not ``in``: 0 is the live "stop Power turns" sentinel
    # and MUST couple, while an explicit ``None`` (no caller sends one today)
    # would otherwise be merged into roleCaps as a cap of None.
    cap = updates.get("dailyCap")
    if cap is not None:
        payload["roleCaps"] = _merged_role_caps(runtime, network_path, bot, cap)
    result, err = runtime.full_config_set_with_error(
        bot, payload, network_path=str(network_path)
    )
    if not result:
        raise UserTierOverrideWriteError(err or "check admin-ui.err.log")

    landed = result.get("userTierOverride") or {}
    if not isinstance(landed, dict):
        landed = {}
    missed = {k: v for k, v in updates.items() if landed.get(k) != v}
    if missed:
        raise UserTierOverrideWriteError(
            f"write reported success but {sorted(missed)} did not persist "
            f"(on disk now: {landed})"
        )
    if "roleCaps" in payload:
        _assert_cap_landed(result, cap)

    _mirror_to_shared_dir(network, bot, landed)
    if audit_action is not None:
        _declare_write(audit_action, bot, updates, result)
    return result


def _merged_role_caps(
    runtime: Any, network_path: Path, bot: str, value: Any
) -> dict[str, Any]:
    """The bot's ``roleCaps`` with the Power cap set to ``value``.

    Read-merged through ``power_cap.merge_power_cap`` — the ONE place that
    merge lives — because ``roleCaps`` is a WHOLESALE replace at the write
    seam: a payload carrying only ``{"power": ...}`` deletes a customized
    bot's ``max`` cap, widening Fable spend as a side effect of lowering Opus
    spend.

    Raises :class:`UserTierOverrideWriteError` when the current config cannot
    be READ. Fail closed: a degraded read is indistinguishable from "this bot
    has no roleCaps", and merging onto that empty block would replace it —
    silently widening every other role's cap. Refuse the whole write instead,
    ``userTierOverride`` half included, so the two keys stay in step.
    """
    from .power_cap import merge_power_cap

    current = runtime.full_config_get(bot, network_path=str(network_path))
    if not isinstance(current, dict):
        raise UserTierOverrideWriteError(
            f"could not read {bot}'s current model config, so the existing "
            "roleCaps block cannot be merged; refusing to replace it "
            "(check admin-ui.err.log)"
        )
    return merge_power_cap(current.get("roleCaps"), value)


def _assert_cap_landed(result: dict[str, Any], value: Any) -> None:
    """Verify ``roleCaps.power.maxPerDayPerBot`` persisted, or raise.

    ``result`` is the post-write ``json_full_config``, so this costs a dict
    lookup — on a real pod a second read would be a second ``sudo -u <bot>
    python3`` spawn. A truthy setter result is not proof of persistence.
    """
    from .power_cap import CAP_FIELD, POWER_ROLE

    on_disk = result.get("roleCaps")
    on_disk = on_disk if isinstance(on_disk, dict) else {}
    entry = on_disk.get(POWER_ROLE)
    if not isinstance(entry, dict) or entry.get(CAP_FIELD) != value:
        raise UserTierOverrideWriteError(
            f"write reported success but roleCaps.{POWER_ROLE}.{CAP_FIELD} "
            f"did not persist (on disk now: {on_disk or None})"
        )


def mirror_path(network: dict[str, Any], bot: str) -> Path:
    """``{sharedDir}/{bot}/tiers.json`` — the legacy mirror's location."""
    from .config import DEFAULT_SHARED_DIR

    shared = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
    return shared / bot / MIRROR_FILENAME


def _mirror_to_shared_dir(
    network: dict[str, Any], bot: str, landed: dict[str, Any]
) -> "str | None":
    """Copy the landed block to the shared-dir mirror. Best-effort.

    Returns an error string when the mirror could not be written, ``None`` on
    success. Never raises: the authoritative write already landed, so a failed
    mirror is a degraded-but-correct routing state (only the admin chat gate
    lags), and aborting here would misreport a completed write as a failure.
    """
    path = mirror_path(network, bot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["userTierOverride"] = dict(landed)
        path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as e:
        return str(e)
    return None


def _declare_write(
    action: str, bot: str, updates: dict[str, Any], result: dict[str, Any]
) -> None:
    """Audit the write so heal credits it instead of reporting drift.

    ``evolve-tiers.json`` drift is namespaced ``tiers:<key>`` by heal's
    detector, and every writer of that file self-declares the names it emits
    (internal/spec-delta-digest-audit-noise-2026-08-25 D3) — otherwise this
    command's own write reads as an unexplained hand edit on the next cycle.
    ``tier_write_oc_keys`` computes the names from the write result, so a
    ``dailyCap`` write declares ``tiers:roleCaps`` too without this function
    knowing about the coupling. No ``openclaw.json`` base key rides along,
    because a ``userTierOverride``/``roleCaps`` update never touches it
    (``oc_json_changed`` stays false in ``json_full_config_set``).
    """
    from .provisioning import _record_audit
    from .web.routes_shared import tier_write_oc_keys

    _record_audit(
        action, bot, dict(updates), oc_keys=tier_write_oc_keys(result),
    )
