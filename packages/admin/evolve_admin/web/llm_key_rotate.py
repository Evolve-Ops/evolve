"""Rotate an LLM provider API key where the runtime actually reads it.

Reached from ``routes_admin.api_admin_rotate_key`` with
``storage="oc_agent_store"`` — the tag ``OcAgentCatalogKeyProbe`` stamps on
the row, so the modal echoes it back and the write lands on the right store
without the route having to guess. Same dispatch shape as
``_rotate_openclaw_channels`` and ``_rotate_dotenv``.

THE SEQUENCE, and why each step is there:

1. **Locate.** Re-scan the runtime store. Targets come from what is on disk
   right now, never from the request — a rotation writes where the runtime
   already reads and never mints a provider block in a file that had none.
2. **Stash.** Keep the outgoing key for exactly one rotation so a bad paste
   is one click back (``oc_agent_keys_io.stash_previous_key``).
3. **Write every agent.** A bot with ``main`` and ``email-reader`` has TWO
   copies of the same key; writing one leaves the other billing on the old
   credential. Partial success is reported as a failure with the exact
   surviving files named — never as "ok".
4. **Verify the write.** Read each file back and compare. No success claim
   without a side effect we can see.
5. **Restart the gateway.** The runtime reads these files at startup, so the
   key does not go live until the bounce.
6. **Verify the key.** One cheap call to the provider (``llm_key_verify``).
   A rejection here does NOT roll back — the write already landed where the
   runtime reads, and undoing it on a transient 503 would restore the stale
   key. The row shows the result and offers Undo.

No key value is logged, echoed, or returned. The audit entry records
provider, agent list, store list and relpaths.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from flask import jsonify

from ..telemetry import get_logger
from . import llm_key_verify
from .oc_agent_keys import RUNTIME_STORAGE_ID, write_targets_for_provider
from .oc_agent_keys_io import (
    previous_key,
    record_verification,
    scan_bot_runtime_llm_keys,
    stash_previous_key,
    verify_runtime_key,
    write_runtime_key,
)

_log = get_logger("web.llm_key_rotate")


def rotate_oc_agent_store(
    bot_id: str,
    provider: str,
    key_value: str,
    *,
    network_path: Path,
    audit: Callable[..., Any],
    restart_gateway: Callable[[str], dict] | None = None,
    verify: Callable[[str, str], Any] | None = None,
):
    """``POST /api/admin/keys/<bot>/<provider>/rotate`` with the S8 storage.

    Returns a Flask response (or ``(response, status)``), like its sibling
    rotate helpers.
    """
    located = scan_bot_runtime_llm_keys(bot_id, network_path=network_path)
    targets = write_targets_for_provider(located, provider)
    if not targets:
        return jsonify({
            "error": (
                f"no {provider} key found in OpenClaw's per-agent store for "
                f"{bot_id} — nothing to rotate here. Use storage="
                "\"auth_profiles\" to seed a key through the wizard path."
            ),
            "storage": RUNTIME_STORAGE_ID,
        }), 404

    outgoing = next(
        (hit.value for hit in located if hit.provider == provider), None,
    )
    stashed = False
    if outgoing and outgoing != key_value:
        stashed = stash_previous_key(
            bot_id, provider, outgoing, network_path=network_path,
        )

    written: list[str] = []
    failures: list[dict] = []
    for slot in targets:
        ok, err = write_runtime_key(
            bot_id, slot, key_value, network_path=network_path,
        )
        if ok and not verify_runtime_key(
            bot_id, slot, key_value, network_path=network_path,
        ):
            ok, err = False, (
                "post-write verification failed: the value we just wrote is "
                "not what reads back"
            )
        if ok:
            written.append(slot.relpath)
        else:
            failures.append({"relpath": slot.relpath, "agent": slot.agent,
                             "error": err or "unknown"})

    if failures:
        # Loud and specific. A half-written rotation is the state that puts a
        # two-agent bot on two different keys, and the operator needs to know
        # WHICH file still holds the old one.
        _log.error(
            "llm key rotation partial for %s/%s: wrote %s, failed %s",
            bot_id, provider, written,
            [f["relpath"] for f in failures],
        )
        audit("keys.rotate", bot_id, {
            "provider": provider, "storage": RUNTIME_STORAGE_ID,
            "outcome": "partial", "written": written,
            "failed": [f["relpath"] for f in failures],
        })
        return jsonify({
            "error": (
                f"rotation is INCOMPLETE: {len(written)} of {len(targets)} "
                "files updated. The files below still hold the previous key, "
                "so agents reading them keep billing on it."
            ),
            "storage": RUNTIME_STORAGE_ID,
            "written": written,
            "failures": failures,
        }), 500

    restart = _restart(bot_id, restart_gateway)
    verify_result = _verify(provider, key_value, verify)
    # Kept on the row so an operator who closed the modal can still see
    # whether the key they pasted actually works.
    record_verification(bot_id, provider, verify_result, network_path=network_path)

    audit("keys.rotate", bot_id, {
        "provider": provider,
        "storage": RUNTIME_STORAGE_ID,
        "outcome": "ok",
        "agents": sorted({slot.agent for slot in targets}),
        "stores": sorted({slot.store for slot in targets}),
        "written": written,
        "previous_key_retained": stashed,
        "gateway_restarted": bool(restart.get("ok")),
        "verified": bool(verify_result.get("ok")),
    })
    return jsonify({
        "ok": True,
        "storage": RUNTIME_STORAGE_ID,
        "provider": provider,
        "agents": sorted({slot.agent for slot in targets}),
        "written": written,
        "requires_restart": False,
        "restart": restart,
        "verify": verify_result,
        # Drives the row's one-click Undo. False when the outgoing value could
        # not be kept — the operator should be told before they rely on it.
        "can_undo": stashed,
    })


def undo_oc_agent_store(
    bot_id: str,
    provider: str,
    *,
    network_path: Path,
    audit: Callable[..., Any],
    restart_gateway: Callable[[str], dict] | None = None,
    verify: Callable[[str, str], Any] | None = None,
):
    """Put the stashed pre-rotation key back — the "one click back" half.

    Deliberately routed through :func:`rotate_oc_agent_store` rather than a
    parallel writer: an undo is a rotation to a known value, and having two
    write paths is how one of them ends up missing an agent.
    """
    prev = previous_key(bot_id, provider, network_path=network_path)
    if not prev:
        return jsonify({
            "error": (
                f"no previous {provider} key is retained for {bot_id} — undo "
                "covers exactly one rotation"
            ),
        }), 404
    return rotate_oc_agent_store(
        bot_id, provider, prev, network_path=network_path, audit=audit,
        restart_gateway=restart_gateway, verify=verify,
    )


def _restart(bot_id: str, restart_gateway: Callable[[str], dict] | None) -> dict:
    """Bounce the gateway so the new key is loaded. Never fatal.

    A failed restart leaves the correct key on disk and the old one in the
    running process — recoverable with the Restart button, and much better
    than refusing the write.
    """
    try:
        if restart_gateway is not None:
            result = restart_gateway(bot_id)
        else:
            from runtime.agent_runtime import get_runtime
            result = get_runtime().gateway_restart(bot_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"gateway restart failed: {exc}"}
    if not isinstance(result, dict):
        return {"ok": False, "detail": "gateway restart returned no result"}
    if not result.get("ok"):
        return {
            "ok": False,
            "detail": (
                result.get("error")
                or "gateway restart did not report success — the new key is "
                   "on disk but the running gateway still holds the old one"
            ),
        }
    return {"ok": True, "service": result.get("service", "")}


def _verify(provider: str, key_value: str, verify) -> dict:
    """Normalise whatever the verifier returned into the wire dict.

    Accepts a :class:`llm_key_verify.VerifyResult` (production) or a plain
    dict (tests, and any future verifier), so the caller never has to care.
    """
    try:
        result = (verify or llm_key_verify.verify_key)(provider, key_value)
    except Exception as exc:  # noqa: BLE001 — verification never fails a write
        return {
            "ok": False, "skipped": False, "status": None,
            "detail": f"verification could not run ({type(exc).__name__})",
        }
    if isinstance(result, llm_key_verify.VerifyResult):
        return result.to_dict()
    return dict(result) if isinstance(result, dict) else {}


__all__ = ["rotate_oc_agent_store", "undo_oc_agent_store"]
