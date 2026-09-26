"""LLM-provider API keys in OpenClaw's PER-AGENT runtime store.

The store the running agent actually reads. Three shapes live under each
agent's ``agent/`` directory (all relative to the bot's ``.openclaw/``):

===========================================  =========================  ============
relpath                                      JSON path                  store id
===========================================  =========================  ============
``agents/<a>/agent/plugins/<p>/catalog.json`` ``providers.<pid>.apiKey``  plugin_catalog
``agents/<a>/agent/models.json``              ``providers.<pid>.apiKey``  agent_models
``agents/<a>/agent/codex-home/auth.json``     ``OPENAI_API_KEY``          codex_auth
===========================================  =========================  ============

WHY THIS MODULE EXISTS (operator finding, 2026-09-04). On the PoC personal
bot the Plugins tab showed anthropic / openai / google / xai "Enabled", the
Anthropic console showed $36 month-to-date on the key, and the Credentials
tab had **no LLM Providers section at all** — every probe behind
``routes_admin.api_admin_get_keys`` looks in ``auth-profiles.json``, the
workspace ``.env`` or ``openclaw.json``, and the keys are in NONE of those.
A read-only scan of the live pod found all three shapes above, duplicated
across every agent the bot runs (``main`` and ``email-reader`` on that bot).
So the operator could not see — let alone rotate — the one key that costs
money, and the "plugin enabled + no key" defect signal was silently wrong
for LLM providers.

DESIGN NOTES
------------
* **Enumeration comes from ``openclaw.json``, never from a directory walk.**
  ``agents.list[]`` carries each agent's ``id``; the paths are then built as
  ``<oc_dir>/agents/<id>/agent/...`` from the *sanitised* id. The block's own
  ``agentDir`` field is deliberately IGNORED — it is bot-writable, and every
  read here runs through a root ``sudo /bin/cat``, so honouring it would let
  the bot aim a privileged read anywhere on the box. ``main`` is always
  probed, because a bot with no ``agents.list`` still has it.
* **Provider candidates** come from ``openclaw.json``'s ``plugins.entries``
  keys unioned with :data:`KNOWN_LLM_PROVIDERS`, so a plugin directory that
  exists without a config entry is still found, and a provider added to OC
  later is found the moment the operator enables it.
* **Provider-id aliasing is real.** ``models.json`` spells xAI ``x-ai`` while
  the plugin catalog spells it ``xai``; Evolve's registry id is ``xai``. The
  mapping lives in :data:`_STORE_PROVIDER_ALIASES` and is applied in BOTH
  directions (scan normalises to the Evolve id, writes de-normalise back).
* **No I/O of its own.** ``read_text`` is injected so the probe path can use
  the admin server's direct-read/``sudo /bin/cat`` cascade and tests can use
  a plain fixture tree. Nothing here shells out, and no value is ever logged.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

# Store ids. Stable strings — they appear in the keys-API row JSON, the
# rotate response and the audit log, so the frontend and the operator's
# eyes both depend on them.
STORE_PLUGIN_CATALOG = "plugin_catalog"
STORE_AGENT_MODELS = "agent_models"
STORE_CODEX_AUTH = "codex_auth"

#: The storage tag the keys API stamps on a row whose credential was located
#: here, and which the rotate endpoint accepts in its ``storage`` body field.
RUNTIME_STORAGE_ID = "oc_agent_store"

#: LLM providers Evolve knows by name. Unioned with the bot's own
#: ``plugins.entries`` keys when deciding which catalogs to look for, so this
#: list being incomplete costs nothing on a bot that has the plugin enabled.
#: Mirrors the ``llm`` category of ``routes_admin_shared._KEY_REGISTRY``.
# provider-literal-allow-begin
KNOWN_LLM_PROVIDERS: tuple[str, ...] = (
    "anthropic", "openai", "google", "xai", "mistral", "groq",
    "perplexity", "together", "deepseek", "cohere", "moonshot",
)

# Evolve provider id → the id that store spells it with. Only entries that
# actually DIFFER belong here; everything else is identity.
_STORE_PROVIDER_ALIASES: dict[str, dict[str, str]] = {
    STORE_AGENT_MODELS: {"xai": "x-ai"},
}

#: ``codex-home/auth.json`` is openai-only and flat: the key sits at the
#: top level under this name next to an ``auth_mode`` discriminator.
CODEX_AUTH_KEY_FIELD = "OPENAI_API_KEY"
CODEX_AUTH_PROVIDER = "openai"
# provider-literal-allow-end

#: Agent ids are used as a path component under a root ``sudo`` read/write.
#: Anything not matching is dropped rather than sanitised — a bot that can
#: name an agent ``../../etc`` is a bot trying something, not a typo.
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

DEFAULT_AGENT_ID = "main"


@dataclass(frozen=True)
class RuntimeKeySlot:
    """One place a provider's key can live, before we know whether it does.

    ``relpath`` is relative to the bot's ``.openclaw/``; ``json_path`` is the
    key's location inside that document.
    """
    provider: str          # Evolve provider id (``xai``, never ``x-ai``)
    agent: str             # ``main``, ``email-reader``, …
    store: str             # one of the ``STORE_*`` constants
    relpath: str
    json_path: tuple[str, ...]


@dataclass(frozen=True)
class LocatedRuntimeKey:
    """A slot that turned out to hold a key. ``value`` never leaves the
    process: the keys API puts only ``masked`` on the wire, and the rotate
    path uses ``value`` solely to stash the previous key for one undo."""
    slot: RuntimeKeySlot
    value: str
    masked: str
    mtime: float | None

    @property
    def provider(self) -> str:
        return self.slot.provider

    @property
    def agent(self) -> str:
        return self.slot.agent

    @property
    def store(self) -> str:
        return self.slot.store

    @property
    def relpath(self) -> str:
        return self.slot.relpath


def store_provider_id(store: str, provider: str) -> str:
    """The id *store* spells *provider* with (``xai`` → ``x-ai`` in models.json)."""
    return _STORE_PROVIDER_ALIASES.get(store, {}).get(provider, provider)


def evolve_provider_id(store: str, store_pid: str) -> str:
    """Inverse of :func:`store_provider_id` — normalise a store's id to Evolve's."""
    for evolve_id, spelled in _STORE_PROVIDER_ALIASES.get(store, {}).items():
        if spelled == store_pid:
            return evolve_id
    return store_pid


def agent_ids_from_oc_config(oc_cfg: dict) -> list[str]:
    """Agent ids for a bot, from ``openclaw.json``'s ``agents.list[]``.

    ``main`` is always included and always first — a bot with no
    ``agents.list`` block still runs it. Ids that fail
    :data:`_AGENT_ID_RE` are dropped (see the module docstring: these become
    path components under a root read). Order is stable so the row's agent
    chips render the same way on every request.
    """
    ids: list[str] = [DEFAULT_AGENT_ID]
    entries = ((oc_cfg or {}).get("agents") or {}).get("list")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            aid = str(entry.get("id") or "").strip()
            if aid and aid not in ids and _AGENT_ID_RE.match(aid):
                ids.append(aid)
    return ids


def provider_candidates_from_oc_config(oc_cfg: dict) -> list[str]:
    """Provider ids whose per-agent catalog is worth looking for.

    ``plugins.entries`` keys ∪ :data:`KNOWN_LLM_PROVIDERS`. The union (rather
    than either alone) is deliberate: the entries block catches a provider OC
    gained after this file was written, and the known list catches a plugin
    directory that exists with no entry — the shape the live PoC bot was in.
    """
    out: list[str] = [p for p in KNOWN_LLM_PROVIDERS if _PROVIDER_ID_RE.match(p)]
    entries = ((oc_cfg or {}).get("plugins") or {}).get("entries")
    if isinstance(entries, dict):
        for name in entries:
            pid = str(name or "").strip()
            if pid and pid not in out and _PROVIDER_ID_RE.match(pid):
                out.append(pid)
    return out


def slots_for_agent(agent: str, providers: Iterable[str]) -> list[RuntimeKeySlot]:
    """Every slot to probe for one agent, in winner-cascade order.

    Plugin catalog first (the per-provider file OC's model layer generates
    and reads), then ``models.json`` (where an operator-declared provider
    block carries its own key), then the codex home (openai only).
    """
    base = f"agents/{agent}/agent"
    slots: list[RuntimeKeySlot] = []
    for provider in providers:
        slots.append(RuntimeKeySlot(
            provider=provider, agent=agent, store=STORE_PLUGIN_CATALOG,
            relpath=f"{base}/plugins/{provider}/catalog.json",
            json_path=("providers", provider, "apiKey"),
        ))
    for provider in providers:
        slots.append(RuntimeKeySlot(
            provider=provider, agent=agent, store=STORE_AGENT_MODELS,
            relpath=f"{base}/models.json",
            json_path=(
                "providers", store_provider_id(STORE_AGENT_MODELS, provider),
                "apiKey",
            ),
        ))
    slots.append(RuntimeKeySlot(
        provider=CODEX_AUTH_PROVIDER, agent=agent, store=STORE_CODEX_AUTH,
        relpath=f"{base}/codex-home/auth.json",
        json_path=(CODEX_AUTH_KEY_FIELD,),
    ))
    return slots


def read_json_at(doc: dict, json_path: tuple[str, ...]):
    """Value at *json_path* in *doc*, or None if any component is missing."""
    cursor = doc
    for part in json_path:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
    return cursor


def set_json_at(doc: dict, json_path: tuple[str, ...], value: str) -> bool:
    """Set *value* at *json_path*, creating intermediate dicts.

    Returns False (writing nothing) when an intermediate component exists but
    is not an object — we never restructure someone else's document to make
    room for a key.
    """
    cursor = doc
    for part in json_path[:-1]:
        nxt = cursor.get(part)
        if nxt is None:
            nxt = {}
            cursor[part] = nxt
        elif not isinstance(nxt, dict):
            return False
        cursor = nxt
    cursor[json_path[-1]] = value
    return True


def scan_runtime_llm_keys(
    oc_dir: Path,
    agents: Iterable[str],
    providers: Iterable[str],
    read_text: Callable[[Path], str | None],
    *,
    mask: Callable[[str], str],
    list_dir: Callable[[Path], list[str] | None] | None = None,
    errors_out: list[str] | None = None,
) -> list[LocatedRuntimeKey]:
    """Every LLM key present in the per-agent runtime store.

    Two of the three stores are read DOCUMENT-DRIVEN rather than by guessing
    provider ids:

    * ``plugins/`` is listed when *list_dir* can list it, so a provider
      directory Evolve has never heard of is still found — and, just as
      importantly, we do not attempt a privileged read of eleven catalog
      paths that do not exist. *providers* is the fallback candidate list for
      when the directory cannot be listed.
    * ``models.json``'s own ``providers`` block is iterated, so ``x-ai``
      normalises to ``xai`` without the alias table having to be complete.

    ``read_text`` returns the file's text or None when it is absent OR
    unreadable — the caller's cascade already collapses those, and "absent"
    is the overwhelmingly common case. A file that fails to parse, or parses
    as something other than an object, appends to *errors_out* and is
    skipped: THAT is a setup-attempt signal worth surfacing on the row,
    unlike plain absence.
    """
    located: list[LocatedRuntimeKey] = []
    candidates = [p for p in providers if _PROVIDER_ID_RE.match(p)]
    for agent in agents:
        base = f"agents/{agent}/agent"

        # ── plugins/<p>/catalog.json ──────────────────────────────────────
        listed = None
        if list_dir is not None:
            try:
                listed = list_dir(oc_dir / base / "plugins")
            except Exception:  # noqa: BLE001 — a listing failure is not fatal
                listed = None
        agent_providers = (
            [d for d in listed if _PROVIDER_ID_RE.match(d)]
            if listed is not None else candidates
        )
        for provider in agent_providers:
            rel = f"{base}/plugins/{provider}/catalog.json"
            doc = _load_doc(oc_dir / rel, read_text, errors_out)
            if doc is None:
                continue
            block = doc.get("providers")
            if not isinstance(block, dict):
                continue
            for store_pid, entry in block.items():
                if not isinstance(entry, dict):
                    continue
                value = str(entry.get("apiKey") or "").strip()
                if not value:
                    continue
                evolve_pid = evolve_provider_id(
                    STORE_PLUGIN_CATALOG, str(store_pid),
                )
                located.append(_hit(
                    oc_dir, RuntimeKeySlot(
                        provider=evolve_pid, agent=agent,
                        store=STORE_PLUGIN_CATALOG, relpath=rel,
                        json_path=("providers", str(store_pid), "apiKey"),
                    ), value, mask,
                ))

        # ── models.json ───────────────────────────────────────────────────
        rel = f"{base}/models.json"
        doc = _load_doc(oc_dir / rel, read_text, errors_out)
        block = (doc or {}).get("providers")
        if isinstance(block, dict):
            for store_pid, entry in block.items():
                if not isinstance(entry, dict):
                    continue
                value = str(entry.get("apiKey") or "").strip()
                if not value:
                    continue
                located.append(_hit(
                    oc_dir, RuntimeKeySlot(
                        provider=evolve_provider_id(
                            STORE_AGENT_MODELS, str(store_pid),
                        ),
                        agent=agent, store=STORE_AGENT_MODELS, relpath=rel,
                        json_path=("providers", str(store_pid), "apiKey"),
                    ), value, mask,
                ))

        # ── codex-home/auth.json ──────────────────────────────────────────
        rel = f"{base}/codex-home/auth.json"
        doc = _load_doc(oc_dir / rel, read_text, errors_out)
        value = str((doc or {}).get(CODEX_AUTH_KEY_FIELD) or "").strip()
        if value:
            located.append(_hit(
                oc_dir, RuntimeKeySlot(
                    provider=CODEX_AUTH_PROVIDER, agent=agent,
                    store=STORE_CODEX_AUTH, relpath=rel,
                    json_path=(CODEX_AUTH_KEY_FIELD,),
                ), value, mask,
            ))
    return located


def _hit(
    oc_dir: Path,
    slot: RuntimeKeySlot,
    value: str,
    mask: Callable[[str], str],
) -> LocatedRuntimeKey:
    return LocatedRuntimeKey(
        slot=slot, value=value, masked=mask(value),
        mtime=_safe_mtime(oc_dir / slot.relpath),
    )


def _load_doc(
    path: Path,
    read_text: Callable[[Path], str | None],
    errors_out: list[str] | None,
) -> dict | None:
    try:
        text = read_text(path)
    except Exception as exc:  # noqa: BLE001 — a read helper must never abort a scan
        if errors_out is not None:
            errors_out.append(f"could not read {path.name}: {exc}")
        return None
    if not text:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        if errors_out is not None:
            errors_out.append(f"{path.name} is not valid JSON")
        return None
    if not isinstance(doc, dict):
        if errors_out is not None:
            errors_out.append(f"{path.name} is not a JSON object")
        return None
    return doc


def _safe_mtime(path: Path) -> float | None:
    """The file's mtime, or None when we cannot stat it.

    None is the NORMAL production answer on macOS, not an error: the OC
    gateway re-hardens ``agents/<a>/agent/`` to 0700 with no ACL after every
    auth write, so ``evolve`` can read the file through the ``sudo /bin/cat``
    grant but cannot traverse to stat it. The row renders "last written"
    only when we genuinely saw it; it never guesses.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def write_targets_for_provider(
    located: Iterable[LocatedRuntimeKey],
    provider: str,
) -> list[RuntimeKeySlot]:
    """Every slot a rotation of *provider* must write, deduped and ordered.

    Derived from what the scan FOUND, never from the full slot table: a
    rotation writes where the runtime already reads (decision B) and does not
    mint new provider blocks in files that never had one. Dedup is by
    (relpath, json_path) because one document can carry the same provider
    only once, and the scan can legitimately surface it once per agent.
    """
    seen: set[tuple[str, tuple[str, ...]]] = set()
    out: list[RuntimeKeySlot] = []
    for hit in located:
        if hit.provider != provider:
            continue
        ident = (hit.slot.relpath, hit.slot.json_path)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(hit.slot)
    return out


def agents_for_provider(
    located: Iterable[LocatedRuntimeKey], provider: str,
) -> list[str]:
    """Ordered, deduped agent ids that carry a key for *provider*.

    Feeds the row's agent chips ("main · email-reader") so the operator can
    see that one key is mirrored to N agents and that rotate covered them all.
    """
    out: list[str] = []
    for hit in located:
        if hit.provider == provider and hit.agent not in out:
            out.append(hit.agent)
    return out


def storage_locations_for_provider(
    located: Iterable[LocatedRuntimeKey], provider: str,
) -> list[str]:
    """Human-readable ``~/.openclaw/<relpath>`` list for the row's evidence chips."""
    out: list[str] = []
    for hit in located:
        if hit.provider != provider:
            continue
        label = f"~/.openclaw/{hit.relpath}"
        if label not in out:
            out.append(label)
    return out


__all__ = [
    "CODEX_AUTH_KEY_FIELD",
    "CODEX_AUTH_PROVIDER",
    "DEFAULT_AGENT_ID",
    "KNOWN_LLM_PROVIDERS",
    "LocatedRuntimeKey",
    "RUNTIME_STORAGE_ID",
    "RuntimeKeySlot",
    "STORE_AGENT_MODELS",
    "STORE_CODEX_AUTH",
    "STORE_PLUGIN_CATALOG",
    "agent_ids_from_oc_config",
    "agents_for_provider",
    "evolve_provider_id",
    "provider_candidates_from_oc_config",
    "read_json_at",
    "scan_runtime_llm_keys",
    "set_json_at",
    "slots_for_agent",
    "storage_locations_for_provider",
    "store_provider_id",
    "write_targets_for_provider",
]
