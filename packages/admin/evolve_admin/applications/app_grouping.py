"""app_grouping — a reversible CLAIM that two app ids are the same app.

ALPHA-3a (audit `internal/audit-alpha-journey-2026-08.md` B3; operator
decision **D-I**, roadmap `internal/roadmap-app-layer-2026-08-15.md` §2).

THE PROBLEM, EXACTLY. ``app_id`` is minted per manifest. Two bots that
independently discover the same app — the ordinary case, because discovery
is what a stranger's pod is full of — get two independent ids, so
``pod_apps.build_pod_apps`` renders two rows, each saying "on 1 bot", and
the App detail of one says *"Not on team-bot-a"* while team-bot-a visibly
has it. The page promises one row per app and delivers the opposite for
everything the operator already owned. Gallery installs share a ``pkg_id``
and therefore do collapse — so the pod-first view worked for apps Evolve
installed and failed for apps the operator already had.

WHAT THIS MODULE IS. A **claim**, computed at read time, that two app ids
describe the same app. It is not an identity. Nothing here writes, nothing
here renames, and no manifest changes: the stored ids stay exactly as they
are, every install keeps its own, and turning the claim off (``grouped=0``)
withdraws it completely — every id renders as its own row again, exactly as
it did before ALPHA-3a. What is reversible is the GROUPING, not every byte
of the payload: ALPHA-3a also landed audit P4, which stops listing the
``evolve`` service account among the bots an app is "not on", and that
subtraction is deliberate and applies regardless of ``grouped``. That
reversibility — of the claim, which is the part that could be wrong — is the
whole reason D-I chose presentation grouping now and deferred a birth-time
content-derived identity (option a) — a claim you can withdraw costs
nothing if it is wrong, and a minted id you must migrate does.

Promotion-time cross-bot ADOPT (D-I's other half, ALPHA-3b) is what makes
the claim unnecessary for apps that go through promotion after it lands.
This module keeps working for everything already on disk — and ALPHA-3b
does not re-state the claim, it CALLS it: :func:`equivalent` is the pairwise
form, used both by the clustering below and by
``app_promotion.adopt_or_confer_app_id``. One definition, so a threshold
that moves moves for both, and the page can never disagree with the id
promotion converged on.

THE CLAIM, IN ONE SENTENCE. *Two apps are the same app when their names
normalize to the same string, their evidence files overlap by at least half,
and no bot has both of them.*

Each clause earns its place:

  * **Same normalized name** — an exact gate, not a fuzzy one. Case,
    punctuation and separators are noise ("Morning Brief" / "morning-brief"
    / "morning_brief"); anything past that is a different name and a
    different app. A fuzzy name match would merge "Weekly Report" with
    "Weekly Reports" *and* with "Weekly Report Archive", and a wrong merge
    hides an app the operator owns.

  * **Evidence overlap ≥ ``EVIDENCE_SIMILARITY_THRESHOLD`` (0.5)** — Jaccard
    over each app's evidence files, so a shared name alone never merges two
    genuinely different apps. Half is the threshold because the name gate has
    already done the discriminating work: at that point the question is only
    "is this the same body of files", and two copies of one app that have
    drifted by a file or two still answer yes, while two apps that merely
    share a helper answer no. The number is a knob, stated here rather than
    inlined, because it is a judgement and not a fact.

  * **Both evidence sets empty → name alone.** An app can be standing
    instructions with nothing on disk (``evidence: memory`` /
    ``conversation``). Refusing to group those would leave exactly the apps
    with the least visible identity showing as duplicates. Note the
    asymmetry: *one* side empty and the other not gives Jaccard 0 and no
    claim — "I have no evidence to compare" is not "the evidence matches".

  * **No shared bot** — two ids that both live on ``team-bot-a`` are not one
    app on two bots; they are two records on one bot, and merging them would
    produce a row whose bots column lists that bot twice and whose Files
    panel (keyed by bot) would collide. Duplicate records on one bot are a
    real condition, and they belong to the deduplication work, not here.

    The check is on the **clusters**, not on the pair. A pairwise-only guard
    is not enough and the difference is not theoretical: with A on bot-b and
    B and C both on bot-a, every pair passes a pairwise guard (A-B and A-C
    share no bot), and the union-find then puts all three in one cluster
    with bot-a in it twice. Refusing the union when the two clusters already
    share a bot is what makes "one bot appears at most once per row" an
    invariant rather than a coincidence of input order.

WHY PATHS ARE COMPARED BY THEIR LAST TWO SEGMENTS. The same app on two bots
lives under two different homes, and its recorded evidence paths may be
absolute (``/Users/personal-bot/.openclaw/workspace/apps/morning-brief/run.py``),
workspace-relative (``apps/morning-brief/run.py``) or app-relative
(``morning-brief/run.py``) depending on which carrier stamped them. Comparing
full strings would score every such pair at 0. The last two segments
(``morning-brief/run.py``) are what all three forms agree on, and they keep
the folder — so ``run.py`` alone, which is in half the apps ever written,
cannot carry a match on its own.

DETERMINISM. The clustering is a union-find over ids in sorted order, so the
same pod produces the same clusters and the same lead in the same order on
every read. Nothing here consults a clock, a random source or the filesystem.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

__all__ = [
    "EVIDENCE_SIMILARITY_THRESHOLD",
    "GROUP_BASIS_NAME",
    "GROUP_BASIS_NAME_AND_FILES",
    "AppGroup",
    "cluster_app_ids",
    "equivalent",
    "evidence_paths",
    "evidence_signature",
    "lead_of",
    "normalize_app_name",
    "path_signature",
    "similarity",
]

#: How much of two apps' evidence must overlap, as a Jaccard ratio, before
#: the claim is made. A judgement, not a measurement — see the module
#: docstring for why one half.
EVIDENCE_SIMILARITY_THRESHOLD = 0.5

#: Why a group was claimed. Reported so the payload can be audited and so a
#: future reader can tell a files-backed claim from a name-only one. NEVER
#: rendered: the surface says "these look like the same app", in words.
GROUP_BASIS_NAME_AND_FILES = "name_and_files"
GROUP_BASIS_NAME = "name"

#: Punctuation that carries no meaning in an app name. Everything outside
#: ``[a-z0-9]`` collapses to a single space, so "Morning-Brief",
#: "morning_brief" and "Morning  Brief!" all normalize to "morning brief".
_NAME_KEEP = set("abcdefghijklmnopqrstuvwxyz0123456789")


def normalize_app_name(name: Any) -> str:
    """An app name reduced to the part two bots would agree on.

    Lowercase, every run of non-alphanumeric characters collapsed to one
    space, ends trimmed. Returns ``""`` for anything that is not a non-empty
    string — and an empty normalized name never matches anything, including
    another empty one, because "these two apps both have no name" is not
    evidence that they are the same app.
    """
    if not isinstance(name, str):
        return ""
    out: list[str] = []
    for char in name.lower():
        if char in _NAME_KEEP:
            out.append(char)
        elif out and out[-1] != " ":
            out.append(" ")
    return "".join(out).strip()


def path_signature(path: Any) -> str:
    """One evidence path reduced to its last two segments, lowercased.

    ``/Users/personal-bot/.openclaw/workspace/apps/morning-brief/run.py``,
    ``apps/morning-brief/run.py`` and ``morning-brief/run.py`` all reduce to
    ``morning-brief/run.py`` — the part that survives living under two
    different bots' homes. A single-segment path keeps its one segment.
    """
    if not isinstance(path, str):
        return ""
    parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
    if not parts:
        return ""
    return "/".join(parts[-2:]).lower()


def evidence_signature(paths: Iterable[Any]) -> "frozenset[str]":
    """The comparable form of one app's evidence file list."""
    return frozenset(sig for sig in (path_signature(p) for p in paths) if sig)


def evidence_paths(raw: Any) -> "list[str]":
    """Every file path one manifest's evidence names, deduped, in order.

    Three carriers, all real on the pod: ``evidence_files`` (the v-migrated
    list), the legacy ``evidence.files`` mapping it was migrated FROM, and
    the app's own ``files`` registry, which is ``list[str]`` on v4 manifests
    and ``list[dict]`` from v5 on.

    It lives HERE, next to the claim it feeds, rather than in ``pod_apps``
    where it was written: since ALPHA-3b the same "is this the same app"
    question is asked at promotion time, and a second extractor is how the
    page and promotion would end up comparing different sets of files.
    ``pod_apps`` calls this one.
    """
    out: list[str] = []
    if not isinstance(raw, dict):
        return out

    def _add(value: Any) -> None:
        path = value.get("path") if isinstance(value, dict) else value
        path = path.strip() if isinstance(path, str) else ""
        if path and path not in out:
            out.append(path)

    for entry in (raw.get("evidence_files") or []):
        _add(entry)
    evidence = raw.get("evidence")
    legacy = evidence.get("files") if isinstance(evidence, dict) else None
    for entry in (legacy if isinstance(legacy, list) else []):
        _add(entry)
    for entry in (raw.get("files") or raw.get("realized_files") or []):
        _add(entry)
    return out


def similarity(left: "frozenset[str]", right: "frozenset[str]") -> float:
    """Jaccard overlap of two evidence signatures.

    Two empty sets score ``0.0``, not ``1.0``: an absence of evidence on both
    sides is not a match, and the empty-vs-empty case is handled explicitly
    by the caller (name alone) rather than smuggled in through arithmetic
    that would also make "empty vs non-empty" look defensible.
    """
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union)


def _facts(value: Any) -> "dict[str, Any]":
    return value if isinstance(value, dict) else {}


def equivalent(left: Any, right: Any) -> "str | None":
    """**The claim, pairwise.** The basis two apps are the same app on, or ``None``.

    ``left`` / ``right`` are facts dicts — ``{"name", "evidence", "bots"}``,
    the same shape :func:`cluster_app_ids` takes per id (it ignores
    ``spec_version``, which only breaks lead ties). Returns
    :data:`GROUP_BASIS_NAME_AND_FILES`, :data:`GROUP_BASIS_NAME`, or ``None``
    for "not the same app".

    **This is the one definition.** :func:`cluster_app_ids` calls it for every
    candidate pair, and ``app_promotion.adopt_or_confer_app_id`` calls it to
    decide D-I's promotion-time cross-bot ADOPT (ALPHA-3b) — so the page's
    "these look like the same app" and promotion's "this app already has an id
    on this pod" can never drift apart into two thresholds.

    The three clauses are the module docstring's, unchanged, including the
    no-shared-bot one: two records on ONE bot are not one app on two bots, and
    at promotion time that same clause is what stops a draft adopting from a
    defined app on its own bot (a duplicate record, which is the deduplication
    work, not this).
    """
    left, right = _facts(left), _facts(right)
    name = normalize_app_name(left.get("name"))
    if not name or name != normalize_app_name(right.get("name")):
        return None
    if set(left.get("bots") or ()) & set(right.get("bots") or ()):
        return None
    left_ev = frozenset(left.get("evidence") or ())
    right_ev = frozenset(right.get("evidence") or ())
    if not left_ev and not right_ev:
        return GROUP_BASIS_NAME
    if similarity(left_ev, right_ev) >= EVIDENCE_SIMILARITY_THRESHOLD:
        return GROUP_BASIS_NAME_AND_FILES
    return None


class AppGroup:
    """One presentation row's worth of app ids.

    ``lead`` is the id the row is keyed by, ``members`` every id it stands
    for (``lead`` first, then the rest in sorted order), and ``basis`` why
    the claim was made. A group of one is the ungrouped case and carries
    ``basis = None`` — it is a claim about nothing.
    """

    __slots__ = ("lead", "members", "basis")

    def __init__(self, lead: str, members: "Sequence[str]",
                 basis: "str | None") -> None:
        self.lead = lead
        self.members = list(members)
        self.basis = basis

    @property
    def grouped(self) -> bool:
        return len(self.members) > 1

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AppGroup(lead={self.lead!r}, members={self.members!r})"


def lead_of(members: "Sequence[str]", facts: "dict[str, dict]") -> str:
    """Which member id keys the row.

    Most bots first — the id that already covers the most of the pod is the
    one an operator has most likely seen — then the highest spec_version,
    then the lexicographically smallest id, so the answer is total and
    stable rather than dependent on dict order.

    Public since ALPHA-3b: when a promoting draft matches more than one
    defined app, promotion adopts the id this function names, so the id it
    converges on is the id the Apps page already leads that row with.
    """
    def key(app_id: str) -> tuple:
        fact = facts.get(app_id) or {}
        return (
            -len(fact.get("bots") or ()),
            -int(fact.get("spec_version") or 0),
            app_id,
        )

    return sorted(members, key=key)[0]


#: Kept as the previous private name so nothing that imported it breaks.
_lead_of = lead_of


def cluster_app_ids(facts: "dict[str, dict]") -> "list[AppGroup]":
    """Partition app ids into presentation groups.

    ``facts`` is ``{app_id: {"name": str, "evidence": frozenset[str],
    "bots": set[str], "spec_version": int}}`` — everything the claim needs
    and nothing else, so this function is pure and testable without a pod.

    Returns one :class:`AppGroup` per row, in sorted-lead order. Every input
    id appears in exactly one group; an id that matches nothing comes back as
    a group of one, which is how the caller renders today's behaviour without
    a second code path.
    """
    ids = sorted(facts)
    parent = {app_id: app_id for app_id in ids}
    cluster_bots = {app_id: set((facts[app_id] or {}).get("bots") or ())
                    for app_id in ids}

    def find(app_id: str) -> str:
        while parent[app_id] != app_id:
            parent[app_id] = parent[parent[app_id]]
            app_id = parent[app_id]
        return app_id

    def union(a: str, b: str) -> bool:
        """Merge two clusters unless they already share a bot.

        Returns whether the merge happened, so the caller records a basis
        only for links that actually exist.
        """
        ra, rb = find(a), find(b)
        if ra == rb:
            return True
        if cluster_bots[ra] & cluster_bots[rb]:
            return False
        # Smaller id wins the root so the structure is deterministic.
        root, merged = min(ra, rb), max(ra, rb)
        parent[merged] = root
        cluster_bots[root] |= cluster_bots[merged]
        return True

    # Only same-normalized-name apps are candidates, so the pairwise sweep
    # runs inside a name bucket rather than over the whole pod: a pod with
    # 200 apps and no repeated name does no comparisons at all.
    buckets: "dict[str, list[str]]" = {}
    for app_id in ids:
        name = normalize_app_name((facts[app_id] or {}).get("name"))
        if not name:
            continue
        buckets.setdefault(name, []).append(app_id)

    basis_of: "dict[tuple[str, str], str]" = {}
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        for i, left in enumerate(bucket):
            for right in bucket[i + 1:]:
                # ONE definition of the claim, shared with promotion's
                # cross-bot ADOPT (:func:`equivalent`). Its no-shared-bot
                # clause is the pairwise half of what ``union`` enforces on
                # the clusters below; a pair that shares a bot is in two
                # clusters that share a bot, so refusing here refuses exactly
                # what ``union`` would have.
                basis = equivalent(facts[left], facts[right])
                if basis is None:
                    continue
                # ``union`` refuses when the two clusters already share a
                # bot — two records on one bot are not one app on two bots.
                if union(left, right):
                    basis_of[(left, right)] = basis

    clusters: "dict[str, list[str]]" = {}
    for app_id in ids:
        clusters.setdefault(find(app_id), []).append(app_id)

    groups: "list[AppGroup]" = []
    for members in clusters.values():
        members = sorted(members)
        if len(members) == 1:
            groups.append(AppGroup(members[0], members, None))
            continue
        # A cluster is files-backed when ANY pair in it was claimed on files;
        # name-only when every link was made on the name alone. Reporting the
        # weaker basis for a cluster that has a strong link would understate
        # what the pod actually checked.
        pairs = [basis_of.get((a, b)) for i, a in enumerate(members)
                 for b in members[i + 1:]]
        basis = (GROUP_BASIS_NAME_AND_FILES
                 if GROUP_BASIS_NAME_AND_FILES in pairs
                 else GROUP_BASIS_NAME)
        lead = lead_of(members, facts)
        groups.append(AppGroup(
            lead, [lead] + [m for m in members if m != lead], basis,
        ))
    groups.sort(key=lambda g: g.lead)
    return groups
