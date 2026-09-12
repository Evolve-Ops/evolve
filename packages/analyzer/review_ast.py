"""review_ast.py — AST-level analysis for code-bearing proposal fields.

Parses any Python code found in a proposal and flags dangerous constructs
regardless of how they are spelled:
  - os.system / os.popen / os.exec*
  - subprocess.* with shell=True
  - eval / exec / compile / __import__ / importlib
  - socket / ctypes
  - base64 / codecs decode feeding exec/eval
  - network clients: requests / httpx / urllib / http.client / aiohttp
  - sudo via any shell call
  - writes to sensitive paths (/etc, /usr, /System, ~/.ssh, /Users/<other>)

On parse error (non-Python or obfuscated/unparseable), emits a FLAG signal
rather than silently passing — the caller should require human review.

Return shape is the ``RuleResult`` protocol the old ``review.py`` defined.
review.py retired 2026-08-14 (#3641); the consumer is now
``arbiter.security_screen._ast_analyze``, which reads ``.action`` (only
``"reject"`` denies) plus ``.rule_id`` / ``.description`` / ``.detail`` and
adapts them into ``SecurityDenial`` records. Those four field names are the
contract — renaming one silently drops the AST layer's findings.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Optional


# ── Result type (mirrors review.RuleResult) ──────────────────────────────────

@dataclass
class ASTFinding:
    rule_id: str
    triggered: bool
    description: str
    action: str  # "reject" | "flag"
    detail: str = ""


# ── Sensitive path patterns ───────────────────────────────────────────────────

_SENSITIVE_PATH_PATTERNS: list[str] = [
    r"^/etc/",
    r"^/usr/",
    r"^/System/",
    r"/\.ssh",
    # Writes to any /Users/<other> (not the bot's own; bot-id is not checked
    # here — caller must filter if they want per-bot precision)
    r"^/Users/[^/]+/",
    # sudoers files anywhere
    r"sudoers",
]
_SENSITIVE_RE = re.compile("|".join(_SENSITIVE_PATH_PATTERNS))

# ── Network-client module names ───────────────────────────────────────────────

_NETWORK_MODULES = {
    "requests",
    "httpx",
    "urllib",
    "urllib2",
    "urllib3",
    "http",
    "http.client",
    "aiohttp",
    "pycurl",
    "httplib",
}

# ── Dangerous call targets ────────────────────────────────────────────────────

_OS_SHELL_ATTRS = {
    "system", "popen", "popen2", "popen3", "popen4",
    "execl", "execle", "execlp", "execlpe",
    "execv", "execve", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe",
    "spawnv", "spawnve", "spawnvp", "spawnvpe",
}

_DANGEROUS_BUILTINS = {"eval", "exec", "compile", "__import__"}


# ── AST visitor ───────────────────────────────────────────────────────────────

class _DangerVisitor(ast.NodeVisitor):
    """Walk an AST and collect dangerous construct signatures."""

    def __init__(self) -> None:
        self.findings: list[ASTFinding] = []
        # Track whether we're inside an exec/eval call (for base64-feed detection)
        self._exec_sites: list[ast.Call] = []

    # -- helpers --

    def _reject(self, rule_id: str, description: str, detail: str) -> None:
        self.findings.append(ASTFinding(
            rule_id=rule_id,
            triggered=True,
            description=description,
            action="reject",
            detail=detail,
        ))

    def _flag(self, rule_id: str, description: str, detail: str) -> None:
        self.findings.append(ASTFinding(
            rule_id=rule_id,
            triggered=True,
            description=description,
            action="flag",
            detail=detail,
        ))

    @staticmethod
    def _call_name(node: ast.Call) -> str:
        """Return a dotted name for a Call node's func, or ''."""
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            obj = func.value
            if isinstance(obj, ast.Name):
                return f"{obj.id}.{func.attr}"
            if isinstance(obj, ast.Attribute):
                # Two-level: e.g. http.client.HTTPConnection
                if isinstance(obj.value, ast.Name):
                    return f"{obj.value.id}.{obj.attr}.{func.attr}"
        return ""

    @staticmethod
    def _attr_chain(node: ast.expr) -> list[str]:
        """Return attribute chain from an Attribute/Name node, innermost first."""
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        parts.reverse()
        return parts

    @staticmethod
    def _is_decode_call(node: ast.Call) -> bool:
        """True if node is <expr>.decode(...) — bytes-to-string decoding."""
        func = node.func
        return isinstance(func, ast.Attribute) and func.attr == "decode"

    @staticmethod
    def _is_b64_call(node: ast.Call) -> bool:
        """True if node looks like base64.b64decode / codecs.decode."""
        name = _DangerVisitor._call_name(node)
        return bool(re.match(
            r"^(base64\.(b64decode|decodebytes|decodestring)|codecs\.decode)$",
            name,
        ))

    # -- visitors --

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in _NETWORK_MODULES or alias.name in _NETWORK_MODULES:
                self._reject(
                    "ast_network_import",
                    "Import of network client library",
                    f"import {alias.name}",
                )
            if top in {"socket", "ctypes"}:
                self._reject(
                    "ast_dangerous_import",
                    f"Import of dangerous module '{top}'",
                    f"import {alias.name}",
                )
            if top == "importlib":
                self._reject(
                    "ast_importlib",
                    "Dynamic import via importlib",
                    f"import {alias.name}",
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        top = module.split(".")[0]
        if top in _NETWORK_MODULES or module in _NETWORK_MODULES:
            self._reject(
                "ast_network_import",
                "Import of network client library",
                f"from {module} import ...",
            )
        if top in {"socket", "ctypes"}:
            self._reject(
                "ast_dangerous_import",
                f"Import of dangerous module '{top}'",
                f"from {module} import ...",
            )
        if top == "importlib":
            self._reject(
                "ast_importlib",
                "Dynamic import via importlib",
                f"from {module} import ...",
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = self._call_name(node)

        # -- eval / exec / compile --
        if name in _DANGEROUS_BUILTINS:
            self._reject(
                "ast_exec_eval",
                f"Use of '{name}' — dynamic code execution",
                f"call: {name}()",
            )

        # -- __import__ as attribute (e.g. builtins.__import__) --
        if isinstance(node.func, ast.Attribute) and node.func.attr == "__import__":
            self._reject(
                "ast_exec_eval",
                "Dynamic import via __import__",
                "__import__()",
            )

        # -- os.system / os.popen / os.exec* / os.spawn* --
        chain = self._attr_chain(node.func)
        if len(chain) >= 2 and chain[0] == "os" and chain[1] in _OS_SHELL_ATTRS:
            dotted = ".".join(chain)
            self._reject(
                "ast_os_shell",
                f"Shell execution via '{dotted}'",
                f"call: {dotted}()",
            )

        # -- subprocess.* with shell=True --
        if len(chain) >= 1 and chain[0] == "subprocess":
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    self._reject(
                        "ast_subprocess_shell",
                        "subprocess call with shell=True",
                        f"call: {'.'.join(chain)}(shell=True)",
                    )
                    break

        # -- ctypes direct call --
        if len(chain) >= 1 and chain[0] == "ctypes":
            self._reject(
                "ast_ctypes",
                "Use of ctypes — raw memory / foreign function interface",
                f"call: {'.'.join(chain[:2])}()",
            )

        # -- socket direct instantiation or call --
        if name in {"socket.socket", "socket.create_connection", "socket.connect"}:
            self._reject(
                "ast_socket",
                "Direct socket usage",
                f"call: {name}()",
            )

        # -- network client calls --
        for mod in _NETWORK_MODULES:
            mod_top = mod.split(".")[0]
            if len(chain) >= 1 and chain[0] == mod_top:
                self._reject(
                    "ast_network_call",
                    f"Network call via '{mod_top}'",
                    f"call: {'.'.join(chain)}()",
                )
                break

        # -- base64 / codecs decode feeding exec/eval: detect the decode call itself --
        # The exec/eval visit already catches the outer call; here we separately flag
        # a decode call whose result is the direct argument to exec/eval.
        # We record exec sites for cross-referencing.
        if name in _DANGEROUS_BUILTINS and node.args:
            # Check if any arg is a base64/codecs decode
            for arg in node.args:
                if isinstance(arg, ast.Call) and (
                    self._is_b64_call(arg) or self._is_decode_call(arg)
                ):
                    self._reject(
                        "ast_decode_exec",
                        "Decoded bytes fed directly to exec/eval",
                        f"{name}(<decode_call>)",
                    )

        # -- base64.b64decode / codecs.decode standalone (flag, may feed exec) --
        if self._is_b64_call(node):
            self._flag(
                "ast_base64_decode",
                "base64/codecs.decode call — possible obfuscated payload",
                f"call: {name}()",
            )

        # -- string literal containing a sensitive path passed to shell calls --
        # (catches os.system("/usr/bin/something"), subprocess.run(["sudo",...]))
        self._check_sensitive_paths_in_args(node, name)

        # -- sudo keyword in any string arg --
        self._check_sudo_in_args(node, name)

        self.generic_visit(node)

    def _check_sensitive_paths_in_args(self, node: ast.Call, name: str) -> None:
        """Flag writes to sensitive paths detected in string constants in args."""
        for arg in node.args:
            for const in ast.walk(arg):
                if isinstance(const, ast.Constant) and isinstance(const.value, str):
                    if _SENSITIVE_RE.search(const.value):
                        self._flag(
                            "ast_sensitive_path",
                            "Sensitive filesystem path in argument",
                            f"path={const.value!r} in {name}()",
                        )

    def _check_sudo_in_args(self, node: ast.Call, name: str) -> None:
        """Reject 'sudo' appearing in any string arg of a call."""
        for arg in node.args:
            for const in ast.walk(arg):
                if isinstance(const, ast.Constant) and isinstance(const.value, str):
                    if re.search(r"\bsudo\b", const.value):
                        self._reject(
                            "ast_sudo_call",
                            "sudo invocation in shell command string",
                            f"found 'sudo' in arg of {name}()",
                        )

    def visit_Constant(self, node: ast.Constant) -> None:
        # Standalone string constants aren't dangerous; only in-call use matters.
        self.generic_visit(node)


# ── Code-field extraction ─────────────────────────────────────────────────────

_CODE_KEYS = (
    "content",        # script_change body
    "script",         # direct script field
    "code",           # generic code field
    "command",        # shell command
    "commands",       # list of shell commands
    "value",          # config value (may contain embedded code)
    "to",             # config "to" value
)


def _extract_code_fields(proposal: dict) -> list[tuple[str, str]]:
    """Return [(field_path, text), ...] for all code-bearing fields in a proposal.

    Searches the top-level proposal dict and the nested ``proposed_change`` dict.
    Returns only non-empty string values.
    """
    results: list[tuple[str, str]] = []
    change = proposal.get("proposed_change", {})

    for source, prefix in [(proposal, ""), (change, "proposed_change.")]:
        for key in _CODE_KEYS:
            val = source.get(key)
            if isinstance(val, str) and val.strip():
                results.append((f"{prefix}{key}", val))
            elif isinstance(val, list):
                # commands: [...] — join for analysis
                joined = "\n".join(str(v) for v in val if isinstance(v, str))
                if joined.strip():
                    results.append((f"{prefix}{key}[]", joined))

    return results


# ── Public entry point ────────────────────────────────────────────────────────

def analyze_proposal(proposal: dict) -> list[ASTFinding]:
    """Run AST analysis on all code-bearing fields in *proposal*.

    Returns a (possibly empty) list of ASTFinding objects.  Each finding has
    ``action="reject"`` or ``action="flag"`` and ``triggered=True``.

    On parse failure (non-Python or obfuscated), a FLAG finding is emitted
    rather than silently passing — callers should treat this as requiring human
    review.
    """
    all_findings: list[ASTFinding] = []
    code_fields = _extract_code_fields(proposal)

    if not code_fields:
        return all_findings

    for field_path, text in code_fields:
        findings = _analyze_text(text, field_path)
        all_findings.extend(findings)

    return all_findings


def _analyze_text(text: str, field_path: str) -> list[ASTFinding]:
    """Parse *text* as Python and return ASTFinding list.

    If the text is not valid Python, emit a FLAG (not a reject) — the content
    may be a shell command, YAML, or legitimately non-Python; only truly
    suspicious parse failures (e.g. obfuscated eval bait) should be rejected,
    and we can't reliably distinguish, so we take the conservative approach:
    flag for human review without auto-rejecting.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        # Flag on parse failure — could be obfuscated/non-Python
        return [ASTFinding(
            rule_id="ast_parse_failure",
            triggered=True,
            description="Code field could not be parsed as Python (syntax error or obfuscated)",
            action="flag",
            detail=f"field={field_path!r} error={exc!s}",
        )]

    visitor = _DangerVisitor()
    visitor.visit(tree)
    # Tag each finding with the field it came from
    for f in visitor.findings:
        if field_path and field_path not in f.detail:
            f.detail = f"field={field_path!r}; {f.detail}"
    return visitor.findings
