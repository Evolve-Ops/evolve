"""
evolve_admin.pod_templates — Pod template framework (capture half).

A pod template is the top of the composition ladder:

    apps → bot templates → **pod templates**

Where a ``BotTemplate`` (``evolve_admin.bot_templates``) groups skills + apps
for *one* bot, a ``PodTemplate`` groups bot-templates + pod-wide settings so a
*whole pod* is reproducible.

The **capture** half (Bite 1) — schema (``loader``), structural + secret-leak
validation (``validator``), and an extractor that reads a live pod's
``network.json`` (the only source of pod membership — never a ``/Users/`` scan)
and emits ``gallery/pod-templates/<name>/pod.yaml``.

The **provision** half (Bite 2, ``provisioner``) is the inverse: take a
``PodTemplate`` and stand its bots + pod-wide seed up on an existing pod —
skip-existing / create-missing bots (looping the bot-template provisioner) and
fill-missing the pod-wide ``network.json`` seed, with dry-run (default) + apply
modes. Materialization (account, skill install) stays the existing
``sudo evolve-admin deploy <bot>`` path.

The ``pod.yaml`` body is forward-compatible with the multi-pod typed-artifact
envelope ``{type, schema_version, payload, signature}``
(design-multi-pod-2026-06-11.md §5): the body *is* the payload, and
:meth:`PodTemplate.wrap_typed_artifact` produces the envelope (signature
left ``None`` — signing is a multi-pod deposit-path concern, out of scope here).

Spec: internal/spec-apps-meta-2026-06-13.md §8 (the apps-coordinator charter;
lands with PR #2864 — may post-date this package on ``main``). The
typed-artifact envelope this schema targets is in the already-merged
internal/design-multi-pod-2026-06-11.md §5.

Module layout
─────────────
    loader.py     — dataclasses (PodTemplate/PodSeed/PodBotSpec) + load_pod_template
    validator.py  — structural + secret-leak validation
    extractor.py  — capture a live pod → PodTemplate; write pod.yaml
    errors.py     — exception types
    __main__.py   — ``python3 -m evolve_admin.pod_templates`` CLI (capture)
"""

from __future__ import annotations

from .errors import (
    PodTemplateError,
    PodTemplateExtractError,
    PodTemplateNotFoundError,
    PodTemplateValidationError,
)
from .loader import (
    POD_TEMPLATE_SCHEMA_VERSION,
    TYPED_ARTIFACT_TYPE,
    PodBotSpec,
    PodSeed,
    PodTemplate,
    builtin_pod_templates_dir,
    list_pod_templates,
    load_pod_template,
    pod_template_from_dict,
    wrap_typed_artifact,
)
from .validator import (
    ValidationReport,
    find_secrets,
    is_secret_key,
    validate_pod_template,
    value_looks_secret,
)
from .extractor import (
    BotEvidence,
    bot_evidence_from_files,
    build_pod_evidence,
    extract_pod_template,
    load_bot_template_candidates,
    match_bot_template,
    pod_members,
    read_bot_evidence,
    write_pod_template,
)
from .provisioner import (
    BotProvisionItem,
    PodConfigChange,
    PodProvisionResult,
    provision_pod_template,
)

__all__ = [
    # errors
    "PodTemplateError",
    "PodTemplateNotFoundError",
    "PodTemplateValidationError",
    "PodTemplateExtractError",
    # loader
    "POD_TEMPLATE_SCHEMA_VERSION",
    "TYPED_ARTIFACT_TYPE",
    "PodBotSpec",
    "PodSeed",
    "PodTemplate",
    "builtin_pod_templates_dir",
    "list_pod_templates",
    "load_pod_template",
    "pod_template_from_dict",
    "wrap_typed_artifact",
    # validator
    "ValidationReport",
    "validate_pod_template",
    "find_secrets",
    "is_secret_key",
    "value_looks_secret",
    # extractor
    "BotEvidence",
    "bot_evidence_from_files",
    "build_pod_evidence",
    "extract_pod_template",
    "load_bot_template_candidates",
    "match_bot_template",
    "pod_members",
    "read_bot_evidence",
    "write_pod_template",
    # provisioner (Bite 2)
    "BotProvisionItem",
    "PodConfigChange",
    "PodProvisionResult",
    "provision_pod_template",
]
