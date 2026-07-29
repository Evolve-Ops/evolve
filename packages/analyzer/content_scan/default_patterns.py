"""content_scan.default_patterns — seed catalog shipped with Phase A.

Spec: docs/spec-prompt-injection-scanner-2026-05-10.md §3.1.

Ten starter patterns selected from documented attack classes
(HiddenLayer / NVIDIA / SecurityWeek / SC Media in 2025–2026).
Operator extends via UpdateContentScanCatalog proposals.
"""
from __future__ import annotations

from evolve_util import now_iso as _utc_now_iso

from .catalog import Catalog, Pattern, Scope


def default_catalog() -> Catalog:
    return Catalog(
        version=1,
        bootstrapped_at=_utc_now_iso(),
        deny_patterns=[
            Pattern(
                id="html_comment_unknown",
                kind="html_comment",
                description=(
                    "Markdown HTML comment whose marker doesn't match the "
                    "Evolve-marker allowlist. HTML comments are invisible on "
                    "rendered output but fully readable by the LLM — a common "
                    "indirect prompt-injection vector."
                ),
                severity="warn",
            ),
            Pattern(
                id="zero_width_invisible",
                kind="regex",
                pattern=r"[​-‏ - ⁠-⁯﻿]+",
                description=(
                    "Zero-width or tag-style invisible Unicode characters. "
                    "Used to hide instructions inside what looks like normal text."
                ),
                severity="alert",
            ),
            Pattern(
                id="authority_impersonation",
                kind="regex",
                pattern=r"^(?:system|user|assistant|anthropic|openclaw)\s*[:>]\s*(ignore|disregard|bypass|skip)\b",
                description=(
                    "Line framed as an out-of-band authority directive "
                    "('System: ignore all previous instructions')."
                ),
                severity="alert",
                flags=["ignorecase", "multiline"],
            ),
            Pattern(
                id="instruction_negation",
                kind="regex",
                pattern=r"(ignore|disregard|forget|override)\s+(all|previous|prior|above|earlier)\s+(instructions|rules|directives|guidelines)",
                description="Classic prompt-injection negation framing.",
                severity="alert",
                flags=["ignorecase"],
            ),
            Pattern(
                id="long_base64_block",
                kind="regex",
                pattern=r"[A-Za-z0-9+/]{160,}={0,2}",
                description=(
                    "Long base64 block (160+ chars) outside fenced code regions. "
                    "Often used to stage encoded commands or exfil payloads."
                ),
                severity="warn",
            ),
            Pattern(
                id="long_hex_block",
                kind="regex",
                pattern=r"(?:[0-9a-fA-F]{2}\s*){64,}",
                description="64+ consecutive hex bytes — encoded payload staging.",
                severity="warn",
            ),
            Pattern(
                id="subcommand_chain_long",
                kind="regex",
                pattern=r"(?:[^\n;&|]+(?:\s*[;&|]{1,2}\s*)){8,}",
                description=(
                    "8+ chained subcommands — the SC Media bypass shape. "
                    "Long shell-command chains can bypass deny rules because "
                    "individual subcommands look benign in isolation."
                ),
                severity="warn",
            ),
            Pattern(
                id="credential_exfil_url",
                kind="regex",
                pattern=r"(curl|wget)\s+[^\s]+\s+--?[a-z-]*(data|d|F)\s+[^\s]*\b(SECRET|TOKEN|PASSWORD|KEY|API_KEY)\b",
                description=(
                    "curl/wget invocation that interpolates a credential "
                    "variable — credential-exfil-via-instructions vector."
                ),
                severity="alert",
                flags=["ignorecase"],
            ),
            Pattern(
                id="single_line_oversize",
                kind="line_length",
                threshold=2000,
                description=(
                    "Single line exceeding 2000 characters. Long single "
                    "lines are often payload-bearing because they're hard to "
                    "review by eye."
                ),
                severity="warn",
            ),
            Pattern(
                id="structural_emptiness",
                kind="structural",
                min_size_bytes=1500,
                applies_to=["AGENTS.md", "SOUL.md"],
                description=(
                    "File whose content is unexpectedly short (the April 2026 "
                    "AGENTS.md truncation pattern — 14.9KB → 583B). Set per-"
                    "file via applies_to."
                ),
                severity="alert",
            ),
        ],
        evolve_markers_allowlist=[
            "<!-- evolve-handoff:begin -->",
            "<!-- evolve-handoff:end -->",
            "<!-- evolve-session-surface:begin -->",
            "<!-- evolve-session-surface:end -->",
            "<!-- evolve-pod-conduct:begin -->",
            "<!-- evolve-pod-conduct:end -->",
            "<!-- evolve-runtime-notes:begin -->",
            "<!-- evolve-runtime-notes:end -->",
            "<!-- evolve-coherence-vocab:begin -->",
            "<!-- evolve-coherence-vocab:end -->",
            "<!-- evolve-app-repair-prompt:begin -->",
            "<!-- evolve-app-repair-prompt:end -->",
            "<!-- evolve-managed:* -->",
            "<!-- BEGIN EVOLVE-* -->",
            "<!-- END EVOLVE-* -->",
            # Provenance markers emitted by
            # evolve_admin.applications.provenance.format_marker_string into
            # every forge-managed file (v6 uses `pkg=`, v7 uses `spec=`).
            "<!-- evolve: pkg=* file=* -->",
            "<!-- evolve: spec=* file=* -->",
        ],
        scope=Scope(
            scanned_files_per_bot=[
                "AGENTS.md", "SOUL.md", "HEARTBEAT.md", "IDENTITY.md",
                "TOOLS.md", "USER.md", "MEMORY.md", "README.md",
            ],
            scanned_pod_files=["POD_CONDUCT.md", "RUNTIME_NOTES.md"],
        ),
    )
