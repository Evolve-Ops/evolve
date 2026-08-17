"""mcp.catalog — pod-curated catalog of vetted MCP server entries.

Spec: docs/spec-mcp-administration-2026-05-10.md §3.1 (CatalogEntry) +
§5.1 (Discovery / catalog).

The catalog lives at ``{shared_dir}/mcp/catalog/pod-catalog.json``. It's
operator-curated — entries arrive via manual add (admin UI), upstream
ingest (deferred), or curator-proposed promotion from observed servers
(Phase A's curator generator, also deferred).

v1 ships seed entries baked into ``default_entries()`` so operators
have something to install from on first run.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Locations ─────────────────────────────────────────────────────────────────

def catalog_dir(shared_dir: Path) -> Path:
    return shared_dir / "mcp" / "catalog"


def catalog_path(shared_dir: Path) -> Path:
    return catalog_dir(shared_dir) / "pod-catalog.json"


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class RequiredEnv:
    """One env-var the server expects at launch.

    Phase B uses ``name`` + ``purpose`` to drive the install workflow's
    credential-binding form. The applier ensures the binding resolves
    through the keystore rather than landing as cleartext in openclaw.json.
    """

    name: str
    purpose: str
    scope_recommendation: str = ""
    keystore_hint: str = ""  # glob for matching keystore key names (e.g. "github-*")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CatalogEntry:
    """A vetted MCP server available for install on pod bots."""

    id: str
    name: str
    description: str
    transport: str  # "stdio" | "http"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    required_env: list[RequiredEnv] = field(default_factory=list)
    advertised_tools: list[str] = field(default_factory=list)
    package_name: str = ""  # e.g. "@modelcontextprotocol/server-github"
    package_kind: str = "npm"  # "npm" | "archive" | "path"
    source_url: str = ""
    vetting_status: str = "approved"  # "approved" | "candidate" | "withdrawn"
    vetting_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["required_env"] = [
            e.to_dict() if isinstance(e, RequiredEnv) else e for e in self.required_env
        ]
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CatalogEntry":
        env_list = []
        for e in data.get("required_env") or []:
            if isinstance(e, dict):
                env_list.append(RequiredEnv(
                    name=str(e.get("name") or ""),
                    purpose=str(e.get("purpose") or ""),
                    scope_recommendation=str(e.get("scope_recommendation") or ""),
                    keystore_hint=str(e.get("keystore_hint") or ""),
                ))
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            transport=str(data.get("transport") or "stdio"),
            command=data.get("command"),
            args=list(data.get("args") or []),
            url=data.get("url"),
            required_env=env_list,
            advertised_tools=list(data.get("advertised_tools") or []),
            package_name=str(data.get("package_name") or ""),
            package_kind=str(data.get("package_kind") or "npm"),
            source_url=str(data.get("source_url") or ""),
            vetting_status=str(data.get("vetting_status") or "approved"),
            vetting_notes=str(data.get("vetting_notes") or ""),
        )

    def fingerprint(self) -> str:
        """Stable hash over the entry's launch shape — used by allowlist linking."""
        canonical = {
            "id": self.id,
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "url": self.url,
            "required_env_names": sorted(e.name for e in self.required_env),
        }
        return hashlib.sha256(
            json.dumps(canonical, sort_keys=True, ensure_ascii=True).encode()
        ).hexdigest()


@dataclass
class Catalog:
    """Container for catalog entries."""

    version: int = 1
    entries: list[CatalogEntry] = field(default_factory=list)
    bootstrapped_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "bootstrapped_at": self.bootstrapped_at,
            "entries": [e.to_dict() for e in self.entries],
        }

    def find(self, entry_id: str) -> CatalogEntry | None:
        for e in self.entries:
            if e.id == entry_id:
                return e
        return None


# ── Read / write ──────────────────────────────────────────────────────────────

def load_with_status(shared_dir: Path) -> tuple[Catalog, str | None]:
    """Load the pod catalog, returning ``(catalog, error_str | None)``.

    ``error_str`` is non-None on read or parse failure so callers can tell
    "missing file" (empty catalog, no error) from "corrupt file" (empty
    catalog, error reported). ``write_default_if_missing`` uses this to
    avoid destroying operator data when the file is corrupt — the prior
    behavior treated a parse error the same as an empty catalog, which
    let the merge pass overwrite the bad file with seeds-only.
    """
    path = catalog_path(shared_dir)
    if not path.exists():
        return Catalog(), None
    try:
        text = path.read_text()
    except OSError as exc:
        return Catalog(), f"os_error: {exc}"
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return Catalog(), f"json_decode: {exc.msg} (line {exc.lineno})"
    entries = [CatalogEntry.from_dict(e) for e in raw.get("entries") or [] if isinstance(e, dict)]
    return Catalog(
        version=int(raw.get("version") or 1),
        entries=[e for e in entries if e.id],
        bootstrapped_at=str(raw.get("bootstrapped_at") or ""),
    ), None


def load(shared_dir: Path) -> Catalog:
    """Load the pod catalog; return empty Catalog if absent or corrupt."""
    cat, _ = load_with_status(shared_dir)
    return cat


def write(catalog: Catalog, shared_dir: Path) -> None:
    """Atomically write the catalog to {shared_dir}/mcp/catalog/pod-catalog.json."""
    path = catalog_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(catalog.to_dict(), indent=2, sort_keys=True))
    tmp.replace(path)


def write_default_if_missing(shared_dir: Path) -> bool:
    """Bootstrap the catalog with seed entries on first run; merge new
    seed entries on every subsequent call.

    On first run: writes the full default catalog. Returns True.

    On subsequent runs: any seed entry whose id isn't already in the
    catalog is appended. Operator-added entries and operator edits to
    existing seed entries are preserved. Caveat: operator removal of a
    seed entry will be undone on the next call — there's no "dropped"
    list yet. Operators who want permanent removal should track it in
    a per-pod override file (or use the upcoming catalog-edit proposal
    pipeline). Returns True when any entry was added; False otherwise.

    Corruption guard: if the catalog file exists but fails to parse (bad
    JSON, unreadable), this function REFUSES to overwrite it. The bad
    file is moved aside to ``pod-catalog.json.bak-<timestamp>`` and a
    fresh seed catalog is written in its place. Operators can still
    recover their original via the .bak file; without this guard the
    prior implementation would silently destroy operator-added entries
    by treating the parse error as "empty catalog" and overwriting.
    """
    path = catalog_path(shared_dir)
    if not path.exists():
        cat = Catalog(
            version=1,
            entries=default_entries(),
            bootstrapped_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        write(cat, shared_dir)
        return True

    # Detect corruption before merging so we don't silently destroy operator
    # data when the file is broken.
    existing, load_error = load_with_status(shared_dir)
    if load_error is not None:
        # Move the corrupt file aside, then write a fresh seed catalog.
        backup_path = path.with_suffix(
            f".json.bak-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        )
        try:
            path.rename(backup_path)
        except OSError:
            # If we can't rename, refuse to write — better to leave the
            # corrupt file in place than to obliterate it with a half-fix.
            return False
        cat = Catalog(
            version=1,
            entries=default_entries(),
            bootstrapped_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        write(cat, shared_dir)
        return True

    # Merge missing seed entries by id without disturbing operator state.
    existing_ids = {e.id for e in existing.entries}
    added = [e for e in default_entries() if e.id not in existing_ids]
    if not added:
        return False
    existing.entries.extend(added)
    write(existing, shared_dir)
    return True


# ── Default seed catalog ──────────────────────────────────────────────────────

def default_entries() -> list[CatalogEntry]:
    """Seed catalog entries shipped with Phase B.

    Conservative starter set — one obvious-fit server per likely use case.
    Operator extends via UpdateMcpCatalog proposals (Phase B) or manual
    edit. CVE-driven retirement / marketplace ingest come in Phase D.
    """
    return [
        CatalogEntry(
            id="github",
            name="GitHub MCP",
            description=(
                "Search repositories, read files, create issues, list commits, "
                "manage PRs through the official Anthropic GitHub MCP server."
            ),
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            required_env=[
                RequiredEnv(
                    name="GITHUB_TOKEN",
                    purpose="Personal access token with the scopes the bot needs.",
                    scope_recommendation=(
                        "Default to repo:read. Add repo:write only if the bot must push commits or open PRs."
                    ),
                    keystore_hint="github-*",
                ),
            ],
            advertised_tools=[
                "search_repositories",
                "get_file_contents",
                "create_issue",
                "create_pull_request",
                "list_commits",
            ],
            package_name="@modelcontextprotocol/server-github",
            package_kind="npm",
            source_url="https://github.com/modelcontextprotocol/servers/tree/main/src/github",
            vetting_status="approved",
            vetting_notes=(
                "First-party Anthropic MCP server, published under the modelcontextprotocol "
                "org on npm. Source-reviewed 2026-05-10. Keep scope as narrow as the bot's role allows."
            ),
        ),
        CatalogEntry(
            id="filesystem",
            name="Filesystem MCP (scoped)",
            description=(
                "Read/write a constrained directory tree. Use scope path as args; "
                "OpenClaw filesystem-tool gates still apply at the bot level."
            ),
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem"],
            required_env=[],
            advertised_tools=[
                "read_file",
                "write_file",
                "create_directory",
                "list_directory",
                "search_files",
            ],
            package_name="@modelcontextprotocol/server-filesystem",
            package_kind="npm",
            source_url="https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
            vetting_status="approved",
            vetting_notes=(
                "First-party. Default scope path is the first arg appended at install time. "
                "Caller's responsibility to ensure the scope doesn't include credentials directories."
            ),
        ),
        CatalogEntry(
            id="memory",
            name="Memory (knowledge graph)",
            description=(
                "Persistent knowledge graph: entities, relations, and observations across "
                "turns. Useful when a bot needs durable structured memory beyond the per-bot "
                "MEMORY.md file."
            ),
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-memory"],
            required_env=[],
            advertised_tools=[
                "create_entities",
                "create_relations",
                "add_observations",
                "read_graph",
                "search_nodes",
            ],
            package_name="@modelcontextprotocol/server-memory",
            package_kind="npm",
            source_url="https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
            vetting_status="approved",
            vetting_notes=(
                "First-party. Stores graph in a per-bot JSON file under the bot's workspace; "
                "no external service. Distinct from the Evolve user_profile system — that "
                "captures the bot's view of its user; this is the bot's own scratch graph."
            ),
        ),
        CatalogEntry(
            id="sequential-thinking",
            name="Sequential Thinking",
            description=(
                "Tool that lets the model externalize multi-step reasoning into explicit "
                "thought-step records. No network, no state — just structured scratchpad."
            ),
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-sequential-thinking"],
            required_env=[],
            advertised_tools=["sequential_thinking"],
            package_name="@modelcontextprotocol/server-sequential-thinking",
            package_kind="npm",
            source_url="https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking",
            vetting_status="approved",
            vetting_notes=(
                "First-party. Pure-process tool — no I/O. Safe for any bot."
            ),
        ),
        CatalogEntry(
            id="fetch",
            name="Fetch (URL fetcher)",
            description=(
                "HTTP fetch tool that returns markdown-converted page content. Replaces "
                "ad-hoc curl invocations inside the bot for read-only web pulls."
            ),
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-fetch"],
            required_env=[],
            advertised_tools=["fetch"],
            package_name="@modelcontextprotocol/server-fetch",
            package_kind="npm",
            source_url="https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
            vetting_status="approved",
            vetting_notes=(
                "First-party. Outbound HTTP only (no auth handling). Pair with the bot's "
                "execution allowlist if SSRF concerns apply."
            ),
        ),
        CatalogEntry(
            id="time",
            name="Time",
            description=(
                "Timezone-aware date/time tool. Avoids the LLM hallucinating dates and lets "
                "operators see explicit timezone math in tool calls."
            ),
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-time"],
            required_env=[],
            advertised_tools=["get_current_time", "convert_time"],
            package_name="@modelcontextprotocol/server-time",
            package_kind="npm",
            source_url="https://github.com/modelcontextprotocol/servers/tree/main/src/time",
            vetting_status="approved",
            vetting_notes=(
                "First-party. Pure-function tool, no network."
            ),
        ),
        CatalogEntry(
            id="linear",
            name="Linear",
            description=(
                "Read and act on Linear issues, projects, and teams — list "
                "team issues, search by status / assignee, fetch issue "
                "details, create issues, update status, post comments. "
                "Uses the official Linear SDK under the hood."
            ),
            transport="stdio",
            command="npx",
            args=["-y", "linear-mcp"],
            required_env=[
                RequiredEnv(
                    name="LINEAR_API_KEY",
                    purpose=(
                        "Personal API key from linear.app → Settings → API → "
                        "Personal API keys. The bot inherits ALL permissions "
                        "the key holder has in Linear; there is no way to "
                        "scope it down further (Linear's permission model is "
                        "workspace-membership-based, not API-scope-based)."
                    ),
                    scope_recommendation=(
                        "Use a dedicated bot user account in your Linear "
                        "workspace if you want scoped access. The bot will "
                        "see whatever that user can see + act as that user "
                        "for any writes."
                    ),
                    keystore_hint="linear-*",
                ),
            ],
            advertised_tools=[
                "list_teams",
                "list_issues",
                "get_issue",
                "create_issue",
                "update_issue",
                "create_comment",
                "list_projects",
                "search_issues",
            ],
            package_name="linear-mcp",
            package_kind="npm",
            source_url="https://github.com/dvcrn/linear-mcp",
            vetting_status="candidate",
            vetting_notes=(
                "Community package by dvcrn (npm/davemail.io maintainer) — "
                "MIT licensed, depends on @linear/sdk (official, actively "
                "maintained). Last published 2025-05 as of 2026-05-30. "
                "**Not first-party from Linear** — Linear hasn't published "
                "an official MCP server as of this catalog add. Trust risk: "
                "a malicious update would gain access to the bot's Linear "
                "API key with full workspace access. Mitigation: pin a "
                "specific version when installing on production-critical "
                "bots (npx -y linear-mcp@1.2.0); review release diffs "
                "before bumping. Source-reviewed 2026-05-30 (179KB "
                "unpacked, GraphQL-tag-and-SDK shape — no obvious red flags). "
                "Vetting status is **candidate**, not approved — bump to "
                "approved after running on a real bot for ~2 weeks without "
                "incident."
            ),
        ),
        CatalogEntry(
            id="postgres",
            name="PostgreSQL (read-only)",
            description=(
                "Read-only Postgres client — query schema, run SELECTs. Connection string "
                "is bound at install time; OpenClaw exec-allow does not apply."
            ),
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-postgres"],
            required_env=[
                RequiredEnv(
                    name="POSTGRES_CONNECTION_STRING",
                    purpose="Postgres URI; the server runs queries against this database only.",
                    scope_recommendation=(
                        "Use a read-only role. Never bind to a connection string with write or DDL "
                        "privileges — the server has no write path but the role would still apply if "
                        "the package shape changes."
                    ),
                    keystore_hint="postgres-*",
                ),
            ],
            advertised_tools=["query", "list_tables", "describe_table"],
            package_name="@modelcontextprotocol/server-postgres",
            package_kind="npm",
            source_url="https://github.com/modelcontextprotocol/servers/tree/main/src/postgres",
            vetting_status="approved",
            vetting_notes=(
                "First-party. Read-only by construction; relies on caller using a read-only role."
            ),
        ),
        CatalogEntry(
            id="sqlite",
            name="SQLite",
            description=(
                "Local SQLite database access — schema, queries, and writes against a "
                "single .db file. Useful for app-local datastores the bot owns."
            ),
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-sqlite"],
            required_env=[],
            advertised_tools=["read_query", "write_query", "list_tables", "describe_table"],
            package_name="@modelcontextprotocol/server-sqlite",
            package_kind="npm",
            source_url="https://github.com/modelcontextprotocol/servers/tree/main/src/sqlite",
            vetting_status="approved",
            vetting_notes=(
                "First-party. Database path is supplied as an arg at install time. "
                "Bound to the bot's filesystem — caller must ensure path is inside the bot's "
                "workspace, not under shared system locations."
            ),
        ),
        CatalogEntry(
            id="slack",
            name="Slack",
            description=(
                "Read messages, post messages, list channels, manage reactions. Distinct "
                "from the slack channel-adapter plugin — this lets the bot operate Slack "
                "as a tool, not as its primary messaging surface."
            ),
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-slack"],
            required_env=[
                RequiredEnv(
                    name="SLACK_BOT_TOKEN",
                    purpose="xoxb- Slack bot user OAuth token with the scopes the bot's tools need.",
                    scope_recommendation=(
                        "Default to channels:read + chat:write. Add other scopes only if the bot must "
                        "manage reactions, fetch threads, or post into private channels."
                    ),
                    keystore_hint="slack-*",
                ),
                RequiredEnv(
                    name="SLACK_TEAM_ID",
                    purpose="Slack workspace ID (T...). Limits the server to one workspace.",
                    scope_recommendation="",
                    keystore_hint="slack-team-*",
                ),
            ],
            advertised_tools=[
                "list_channels",
                "post_message",
                "reply_to_thread",
                "add_reaction",
                "get_channel_history",
                "get_users",
            ],
            package_name="@modelcontextprotocol/server-slack",
            package_kind="npm",
            source_url="https://github.com/modelcontextprotocol/servers/tree/main/src/slack",
            vetting_status="approved",
            vetting_notes=(
                "First-party. Token scopes determine blast radius — narrow them at install time."
            ),
        ),
        CatalogEntry(
            id="notion",
            name="Notion",
            description=(
                "Read pages and databases shared with your Notion Internal "
                "Integration; search the workspace; create or append blocks. "
                "Scope is governed by Notion's own per-page sharing — the bot "
                "only sees pages you explicitly share with the integration."
            ),
            transport="stdio",
            command="npx",
            args=["-y", "@notionhq/notion-mcp-server"],
            required_env=[
                RequiredEnv(
                    name="OPENAPI_MCP_HEADERS",
                    purpose=(
                        "JSON-encoded headers blob for the Notion API: "
                        "{\"Authorization\": \"Bearer <secret>\", "
                        "\"Notion-Version\": \"2022-06-28\"}. The wrapper "
                        "route at /api/skills/install/notion/set-token "
                        "constructs this from a plain Internal Integration "
                        "Secret paste — operators don't write the JSON."
                    ),
                    scope_recommendation=(
                        "Default is read+write on pages shared with the "
                        "integration. Notion's permission model is "
                        "per-page-share, not API-scope; narrow access by "
                        "controlling which pages get shared, not via this "
                        "token's properties."
                    ),
                    keystore_hint="notion-*",
                ),
            ],
            advertised_tools=[
                "API-retrieve-a-page",
                "API-patch-page",
                "API-post-search",
                "API-get-block-children",
                "API-patch-block-children",
                "API-create-a-database",
                "API-post-database-query",
            ],
            package_name="@notionhq/notion-mcp-server",
            package_kind="npm",
            source_url="https://github.com/makenotion/notion-mcp-server",
            vetting_status="approved",
            vetting_notes=(
                "First-party from Notion (makenotion org). Released March 2025; "
                "actively maintained as of catalog add (2026-05-30). The "
                "OPENAPI_MCP_HEADERS shape is awkward UX-wise but is the "
                "documented contract; the skill's wrapper route hides the "
                "JSON construction from the operator. Scope is determined by "
                "the Internal Integration's per-page sharing in Notion (NOT "
                "by the API token), so this skill cannot leak unshared pages "
                "regardless of token permissions. License: MIT."
            ),
        ),
        CatalogEntry(
            id="puppeteer",
            name="Puppeteer (headless browser)",
            description=(
                "Headless Chrome control: navigate, screenshot, click, fill forms, eval JS. "
                "Powerful but broad — install only on bots that genuinely need browser automation."
            ),
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-puppeteer"],
            required_env=[],
            advertised_tools=[
                "puppeteer_navigate",
                "puppeteer_screenshot",
                "puppeteer_click",
                "puppeteer_fill",
                "puppeteer_evaluate",
            ],
            package_name="@modelcontextprotocol/server-puppeteer",
            package_kind="npm",
            source_url="https://github.com/modelcontextprotocol/servers/tree/main/src/puppeteer",
            vetting_status="approved",
            vetting_notes=(
                "First-party. puppeteer_evaluate runs arbitrary JS in the page context — treat "
                "as an exec surface, not a read-only browser. Consider scoping origin via a "
                "wrapper before installing for bots without a clear browser-automation need."
            ),
        ),
        CatalogEntry(
            id="google_workspace",
            name="Google Workspace",
            description=(
                "Read and write Gmail, Calendar, Drive files, Sheets, and Docs "
                "through Google's OAuth APIs. Per-skill scope flags govern "
                "whether the bot gets read-only or read+write access (the "
                "google_workspace_read / google_workspace_write Evolve skills "
                "supply the appropriate --read-only or --permissions flags as "
                "extra_args at install time)."
            ),
            transport="stdio",
            command="uvx",
            args=["workspace-mcp"],
            required_env=[
                RequiredEnv(
                    name="GOOGLE_OAUTH_CLIENT_ID",
                    purpose=(
                        "OAuth 2.0 client_id for the pod's GCP project. "
                        "The MCP server uses this together with the secret "
                        "to refresh access tokens against Google's token "
                        "endpoint when the stored access_token expires."
                    ),
                    scope_recommendation=(
                        "Same client_id the Evolve OAuth wizard uses for "
                        "this bot (per-bot via network.bots[<id>]."
                        "googleOAuthClient, with legacy pod-wide fallback)."
                    ),
                    keystore_hint="gws-client-id-*",
                ),
                RequiredEnv(
                    name="GOOGLE_OAUTH_CLIENT_SECRET",
                    purpose=(
                        "OAuth 2.0 client_secret for the pod's GCP project. "
                        "Paired with client_id for refresh-token exchange. "
                        "Held in the Evolve credential store at "
                        "{shared_dir}/credentials/<bot>/google_oauth_client.json "
                        "and bridged to the per-bot keystore slot for the "
                        "launcher to resolve at exec time."
                    ),
                    scope_recommendation="",
                    keystore_hint="gws-client-secret-*",
                ),
                RequiredEnv(
                    name="WORKSPACE_MCP_CREDENTIALS_DIR",
                    purpose=(
                        "Absolute path to the per-bot credentials directory "
                        "(typically <bot_home>/.openclaw/google_workspace_mcp/"
                        "credentials/). The MCP server reads "
                        "<user_email>.json from this dir on first tool call "
                        "and refreshes tokens itself thereafter. Not a "
                        "secret; stored in the keystore because the launcher "
                        "only supports keystore: refs for env_bindings as of "
                        "v1 (see docs/vetting-workspace-mcp-2026-06-04.md §6)."
                    ),
                    scope_recommendation=(
                        "Always per-bot — never shared across bots, since "
                        "each bot's OAuth profile maps to a different "
                        "Google account."
                    ),
                    keystore_hint="gws-creds-dir-*",
                ),
            ],
            advertised_tools=[
                # Curated subset of workspace-mcp's surface; the per-skill
                # --permissions flag (passed as extra_args by the install
                # module) determines which of these the bot actually sees.
                "search_gmail_messages",
                "send_gmail_message",
                "list_calendars",
                "create_calendar_event",
                "list_drive_files",
                "upload_drive_file",
                "read_sheet",
                "append_sheet_row",
                "create_doc",
                "edit_doc",
            ],
            package_name="workspace-mcp",
            package_kind="pypi",
            source_url="https://github.com/taylorwilsdon/google_workspace_mcp",
            vetting_status="approved",
            vetting_notes=(
                "Vetted 2026-06-04 — see docs/vetting-workspace-mcp-2026-06-04.md. "
                "MIT license, 2,351 commits, active maintenance, Python via PyPI "
                "(workspace-mcp) launched through uvx. Covers Gmail / Calendar / "
                "Drive / Docs / Sheets + Slides / Forms / Tasks / Contacts / Chat "
                "(we enable only the first five via --permissions flags from the "
                "install module). The server refreshes OAuth tokens itself; the "
                "Evolve token shim translates the auth-profiles.json shape into "
                "the credentials.json format at install time and on every deploy. "
                "PREREQ: `uv` must be installed on PATH at the bot user's exec "
                "context (`brew install uv` on the mini, or per the deploy "
                "prereq check added in the same PR as this entry). Single-"
                "maintainer governance flag — re-evaluate at the next dependency "
                "review and fork if maintenance stalls. Vetting status is "
                "**approved** rather than candidate because the spec + token "
                "shim + install module landed in lockstep; downstream tests "
                "exercise the wiring."
            ),
        ),
        CatalogEntry(
            id="zoom",
            name="Zoom",
            description=(
                "Proxy Zoom's hosted MCP (search meetings, recordings, "
                "transcripts, Zoom Docs, chat search) and create/update/"
                "delete Zoom meetings via REST. Two Zoom Marketplace OAuth "
                "apps (General + Server-to-Server) live behind a local "
                "stdio shim (evolve-zoom-mcp) that handles refresh and "
                "merges the read + write tool surfaces."
            ),
            transport="stdio",
            command="uvx",
            args=["evolve-zoom-mcp"],
            required_env=[
                RequiredEnv(
                    name="ZOOM_OAUTH_CLIENT_ID",
                    purpose=(
                        "Client ID of the Zoom Marketplace General "
                        "(user-OAuth) app. Confidential client — the "
                        "shim uses client_secret_basic to refresh "
                        "user-OAuth tokens against zoom.us/oauth/token."
                    ),
                    scope_recommendation=(
                        "One General App per Zoom account, shared across "
                        "all bots in that account. Each bot's user-OAuth "
                        "still authorizes a distinct Zoom user."
                    ),
                    keystore_hint="zoom-oauth-client-id-*",
                ),
                RequiredEnv(
                    name="ZOOM_OAUTH_CLIENT_SECRET",
                    purpose="Client secret of the Zoom Marketplace General app.",
                    scope_recommendation="",
                    keystore_hint="zoom-oauth-client-secret-*",
                ),
                RequiredEnv(
                    name="ZOOM_OAUTH_REDIRECT_URL",
                    purpose=(
                        "OAuth redirect URL registered on the Marketplace "
                        "app. Must be HTTPS — Zoom rejects http loopback "
                        "even on 127.0.0.1. Use a cloudflared tunnel "
                        "during dev or the admin-UI callback in prod."
                    ),
                    scope_recommendation="",
                    keystore_hint="zoom-oauth-redirect-url-*",
                ),
                RequiredEnv(
                    name="ZOOM_CREDENTIALS_DIR",
                    purpose=(
                        "Absolute path to the per-bot Zoom credentials "
                        "directory (typically <bot_home>/.openclaw/zoom/). "
                        "The shim persists credentials.json (refresh "
                        "token, last access token, expires_at) here."
                    ),
                    scope_recommendation=(
                        "Always per-bot — each bot's user-OAuth refresh "
                        "token belongs to a different Zoom user."
                    ),
                    keystore_hint="zoom-creds-dir-*",
                ),
                RequiredEnv(
                    name="ZOOM_S2S_CLIENT_ID",
                    purpose=(
                        "Server-to-Server OAuth client ID — used to "
                        "create/update/delete Zoom meetings as users in "
                        "the account. Optional — when absent, the shim "
                        "filters write tools from tools/list."
                    ),
                    scope_recommendation=(
                        "Optional. One S2S app per Zoom account, shared "
                        "across all bots in that account."
                    ),
                    keystore_hint="zoom-s2s-client-id-*",
                ),
                RequiredEnv(
                    name="ZOOM_S2S_CLIENT_SECRET",
                    purpose="Server-to-Server OAuth client secret.",
                    scope_recommendation="",
                    keystore_hint="zoom-s2s-client-secret-*",
                ),
                RequiredEnv(
                    name="ZOOM_S2S_ACCOUNT_ID",
                    purpose=(
                        "Server-to-Server OAuth account_id — required to "
                        "mint S2S access tokens via the account_credentials "
                        "grant."
                    ),
                    scope_recommendation="",
                    keystore_hint="zoom-s2s-account-id-*",
                ),
            ],
            advertised_tools=[
                # Read tools — proxied verbatim from Zoom's hosted MCP at
                # mcp.zoom.us/mcp/zoom/streamable. Verified via tools/list
                # on 2026-06-06. Drift CI script (spec §11.c) checks weekly.
                "search_meetings",
                "get_meeting_assets",
                "get_recording_resource",
                "recordings_list",
                "get_file_content",
                "create_new_file_with_markdown",  # Zoom's own write tool
                "search_zoom",
                # Write tools — implemented inside the shim against the
                # Zoom REST API. Only present when S2S env is configured.
                "create_meeting",
                "update_meeting",
                "delete_meeting",
            ],
            package_name="evolve-zoom-mcp",
            package_kind="pypi",
            source_url="https://github.com/evolve-ops/evolve/tree/main/packages/evolve-zoom-mcp",
            vetting_status="candidate",
            vetting_notes=(
                "Vetted 2026-06-06 — see docs/spec-zoom-skill-2026-06-06.md. "
                "Evolve-owned Python shim (FastMCP-style) that proxies "
                "Zoom's official hosted MCP for reads and calls Zoom's "
                "REST API for meeting writes. Built because OC's built-in "
                "MCP-OAuth client hardcodes token_endpoint_auth_method=none "
                "(public/PKCE only) and Zoom Marketplace apps are "
                "confidential clients with no DCR — the only viable "
                "architecture is a local shim that holds the confidential "
                "secrets in-process. Two Zoom Marketplace apps (General + "
                "S2S) live as per-bot keystore slots; the shim handles all "
                "OAuth refresh. Status remains **candidate** until Phase 4 "
                "canary on Atlas + team-bot-a verifies clean. PREREQ: `uv` "
                "must be installed on PATH at the bot user's exec context "
                "(same prereq as workspace-mcp) AND the evolve-zoom-mcp "
                "package must be available to uvx (PyPI publish gated on "
                "Phase 4 sign-off; pre-publish testing uses uvx --from "
                "<repo-path>)."
            ),
        ),
    ]
