"""project_discovery_rules — the evidence table behind repository discovery (D-AX1).

Spec: [internal/design-project-adoption-2026-09-26.md](../../../internal/design-project-adoption-2026-09-26.md)
§1 (the worked case) and §11 (the second repository). Rules, not a model:
each row maps one kind of evidence in a repository to a service id from
``connection_capabilities.SERVICE_CATALOG`` (the one vocabulary — add a
service THERE), the capability it implies, and a confidence:

* ``certain`` — a config file or a CI step that only exists for the service
* ``likely``  — an SDK dependency
* ``named``   — an environment-variable NAME (values are never read)

``confirmed`` / ``unverified — named in docs`` / ``planned`` /
``mentioned (alternative)`` are assigned by the scanner (``project_discovery``), not by a rule. Every rule carries an
``example`` so the tests can prove each one fires on a minimal input and
nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass

CERTAIN = "certain"
CONFIRMED = "confirmed"
LIKELY = "likely"
NAMED = "named"
UNVERIFIED = "unverified — named in docs"
PLANNED = "planned"
ALTERNATIVE = "mentioned (alternative)"
UNKNOWN = "unknown"

# Higher wins when several rules name one service. ``planned`` and
# ``mentioned (alternative)`` are docs-only tiers that sort after every
# present service (``unverified — named in docs`` included).
CONFIDENCE_RANK = {
    CONFIRMED: 7, CERTAIN: 6, LIKELY: 5, NAMED: 4, UNVERIFIED: 3, PLANNED: 2, ALTERNATIVE: 1, UNKNOWN: 0,
}

# Local tooling: a program the developer runs on their own machine. No
# management connection, no capabilities (``supabase start`` needs Docker; a
# Dockerfile is a build recipe, not an account). Rendered under its own heading.
LOCAL_TOOLING = frozenset({"docker"})


@dataclass(frozen=True)
class DepRule:
    """An SDK dependency. ``pattern`` matches the package name in
    package.json / pyproject.toml / Pipfile / requirements*.txt / Gemfile;
    ``go`` matches a module path prefix in go.mod."""
    service: str
    pattern: str
    example: str
    go: str | None = None
    go_example: str | None = None
    capabilities: tuple[str, ...] | None = None
    confidence: str = LIKELY


@dataclass(frozen=True)
class EnvRule:
    """A variable NAME in an ``.env.example``-class file (or a workflow's
    ``secrets.NAME``). Matched against the name only."""
    service: str
    pattern: str
    example: str
    capabilities: tuple[str, ...] | None = None
    confidence: str = NAMED


@dataclass(frozen=True)
class FileRule:
    """A config file. ``globs`` match the repo-relative path at any depth.
    With ``line_pattern`` the first matching line becomes the evidence line
    (``line_required``: the file only counts when a line matches)."""
    service: str
    globs: tuple[str, ...]
    example_path: str
    capabilities: tuple[str, ...] | None = None
    confidence: str = CERTAIN
    line_pattern: str | None = None
    line_required: bool = False
    example_content: str = ""


@dataclass(frozen=True)
class CiRule:
    """A step or action inside ``.github/workflows/*.yml`` — where a
    workflow deploys to."""
    service: str
    pattern: str
    example: str
    capabilities: tuple[str, ...] | None = None
    confidence: str = CERTAIN


DEP_RULES: tuple[DepRule, ...] = (
    DepRule("supabase", r"@supabase/[\w.-]+|supabase|supabase-py", "@supabase/ssr",
            go=r"github\.com/supabase-community/[\w.-]+", go_example="github.com/supabase-community/supabase-go"),
    DepRule("resend", r"resend", "resend", go=r"github\.com/resend/resend-go", go_example="github.com/resend/resend-go/v2"),
    DepRule("sendgrid", r"@sendgrid/[\w-]+|sendgrid", "@sendgrid/mail", go=r"github\.com/sendgrid/[\w.-]+", go_example="github.com/sendgrid/sendgrid-go"),
    DepRule("postmark", r"postmark|postmarker|postmark-rails", "postmark"),
    DepRule("mailgun", r"mailgun\.js|mailgun|mailgun-ruby|mailgun-js", "mailgun-js", go=r"github\.com/mailgun/mailgun-go", go_example="github.com/mailgun/mailgun-go/v4"),
    DepRule("stripe", r"stripe|@stripe/[\w-]+", "stripe", go=r"github\.com/stripe/stripe-go", go_example="github.com/stripe/stripe-go/v76"),
    DepRule("openai", r"openai|ruby-openai", "openai", go=r"github\.com/sashabaranov/go-openai|github\.com/openai/openai-go", go_example="github.com/sashabaranov/go-openai"),
    DepRule("anthropic", r"@anthropic-ai/[\w-]+|anthropic", "@anthropic-ai/sdk", go=r"github\.com/anthropics/anthropic-sdk-go", go_example="github.com/anthropics/anthropic-sdk-go"),
    DepRule("cohere", r"cohere|cohere-ai", "cohere-ai"),
    DepRule("voyage", r"voyageai", "voyageai"),
    DepRule("error-tracking", r"@sentry/[\w-]+|sentry-sdk|sentry-ruby|sentry-rails", "@sentry/nextjs", go=r"github\.com/getsentry/sentry-go", go_example="github.com/getsentry/sentry-go"),
    DepRule("prisma", r"prisma|@prisma/client", "@prisma/client"),
    DepRule("vercel", r"vercel|@vercel/[\w-]+", "@vercel/analytics"),
    DepRule("netlify", r"netlify-cli|@netlify/[\w-]+", "netlify-cli"),
    DepRule("cloudflare", r"wrangler|@cloudflare/[\w-]+|cloudflare", "wrangler", capabilities=("deploy",)),
    DepRule("clerk", r"@clerk/[\w-]+|clerk", "@clerk/nextjs"),
    DepRule("auth0", r"@auth0/[\w-]+|auth0", "@auth0/nextjs-auth0"),
    DepRule("authjs", r"next-auth|@auth/[\w-]+", "next-auth"),
    DepRule("google", r"googleapis|google-api-python-client|google-auth(?:-oauthlib)?", "googleapis", go=r"google\.golang\.org/api", go_example="google.golang.org/api"),
    DepRule("redis", r"redis|ioredis|hiredis|@upstash/redis", "ioredis", go=r"github\.com/redis/go-redis|github\.com/go-redis/redis", go_example="github.com/redis/go-redis/v9"),
    DepRule("celery", r"celery|dramatiq|kombu", "celery"),
    DepRule("rabbitmq", r"pika|amqplib|amqp", "amqplib"),
)

ENV_RULES: tuple[EnvRule, ...] = (
    EnvRule("supabase", r"(?:NEXT_PUBLIC_|PUBLIC_|VITE_|EXPO_PUBLIC_|NUXT_PUBLIC_)?SUPABASE_\w*", "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY"),
    EnvRule("resend", r"RESEND_\w*", "RESEND_API_KEY"),
    EnvRule("sendgrid", r"SENDGRID_\w*", "SENDGRID_API_KEY"),
    EnvRule("postmark", r"POSTMARK_\w*", "POSTMARK_SERVER_TOKEN"),
    EnvRule("mailgun", r"MAILGUN_\w*", "MAILGUN_API_KEY"),
    EnvRule("smtp", r"SMTP_\w*", "SMTP_HOST"),
    EnvRule("vercel", r"VERCEL_\w*", "VERCEL_TOKEN"),
    EnvRule("netlify", r"NETLIFY_\w*", "NETLIFY_AUTH_TOKEN"),
    EnvRule("fly", r"FLY_\w*", "FLY_API_TOKEN"),
    EnvRule("render", r"RENDER_\w*", "RENDER_API_KEY"),
    EnvRule("cloudflare", r"(?:CLOUDFLARE|CF)_\w*", "CLOUDFLARE_API_TOKEN"),
    EnvRule("stripe", r"(?:NEXT_PUBLIC_)?STRIPE_\w*", "STRIPE_SECRET_KEY"),
    EnvRule("openai", r"OPENAI_\w*", "OPENAI_API_KEY"),
    EnvRule("anthropic", r"ANTHROPIC_\w*", "ANTHROPIC_API_KEY"),
    EnvRule("cohere", r"COHERE_\w*", "COHERE_API_KEY"),
    EnvRule("voyage", r"VOYAGE_\w*", "VOYAGE_API_KEY"),
    EnvRule("embeddings-client", r"EMBEDDINGS?_\w*", "EMBEDDING_API_KEY"),
    EnvRule("error-tracking", r"(?:NEXT_PUBLIC_)?SENTRY_\w*", "SENTRY_DSN"),
    EnvRule("clerk", r"(?:NEXT_PUBLIC_)?CLERK_\w*", "CLERK_SECRET_KEY"),
    EnvRule("auth0", r"AUTH0_\w*", "AUTH0_CLIENT_SECRET"),
    EnvRule("authjs", r"NEXTAUTH_\w*", "NEXTAUTH_SECRET"),
    EnvRule("google", r"(?:GOOGLE|GMAIL)_\w*", "GOOGLE_CLIENT_ID"),
    EnvRule("github", r"GITHUB_(?!TOKEN$)\w*", "GITHUB_APP_ID", capabilities=("source",)),
    EnvRule("redis", r"REDIS_\w*|UPSTASH_REDIS_\w*", "REDIS_URL"),
    EnvRule("celery", r"CELERY_\w*", "CELERY_BROKER_URL"),
    EnvRule("rabbitmq", r"RABBITMQ_\w*|AMQP_\w*", "RABBITMQ_URL"),
    # A generic connection string names a database without naming its host.
    EnvRule("sql-database", r"DATABASE_URL|POSTGRES(?:QL)?_\w*|PG(?:HOST|USER|PASSWORD|DATABASE)|MYSQL_\w*", "DATABASE_URL"),
)

FILE_RULES: tuple[FileRule, ...] = (
    FileRule("vercel", ("vercel.json", ".vercelignore", ".vercel/project.json"), "vercel.json"),
    FileRule("netlify", ("netlify.toml",), "netlify.toml"),
    FileRule("fly", ("fly.toml",), "fly.toml"),
    FileRule("cloudflare", ("wrangler.toml", "wrangler.json", "wrangler.jsonc"), "wrangler.toml", capabilities=("deploy",)),
    FileRule("render", ("render.yaml", "render.yml"), "render.yaml"),
    FileRule("supabase", ("supabase/config.toml",), "supabase/config.toml", capabilities=("database", "auth")),
    # The directory alone is enough — a project with no config.toml still
    # keeps its schema in a hosted database (§11).
    FileRule("supabase", ("supabase/migrations/*.sql",), "supabase/migrations/0001_init.sql", capabilities=("database",)),
    FileRule("prisma", ("prisma/schema.prisma",), "prisma/schema.prisma", capabilities=("database",),
             line_pattern=r'^\s*provider\s*=\s*"(?:postgresql|mysql|sqlite|sqlserver|mongodb|cockroachdb)"',
             example_content='datasource db {\n  provider = "postgresql"\n}\n'),
    FileRule("docker", ("Dockerfile", "Dockerfile.*", "*.Dockerfile"), "Dockerfile"),
    FileRule("redis", ("docker-compose*.yml", "docker-compose*.yaml", "compose.yml", "compose.yaml"), "docker-compose.yml",
             capabilities=("queue", "cache"), line_pattern=r"^\s*image\s*:\s*[\"']?redis\b", line_required=True,
             example_content="services:\n  cache:\n    image: redis:7\n"),
    FileRule("rabbitmq", ("docker-compose*.yml", "docker-compose*.yaml", "compose.yml", "compose.yaml"), "docker-compose.yml",
             line_pattern=r"^\s*image\s*:\s*[\"']?rabbitmq\b", line_required=True,
             example_content="services:\n  mq:\n    image: rabbitmq:3\n"),
    FileRule("dnscontrol", ("dnsconfig.js",), "dnsconfig.js"),
    FileRule("dns-zone", ("*.zone",), "dns/example.test.zone"),
    FileRule("cloudflare", ("*.tf",), "infra/dns.tf", capabilities=("dns",),
             line_pattern=r'^\s*resource\s+"cloudflare_(?:record|zone)"', line_required=True,
             example_content='resource "cloudflare_record" "www" {\n}\n'),
    FileRule("route53", ("*.tf",), "infra/route53.tf",
             line_pattern=r'^\s*resource\s+"aws_route53_(?:record|zone)"', line_required=True,
             example_content='resource "aws_route53_record" "www" {\n}\n'),
    FileRule("github", (".github/workflows/*.yml", ".github/workflows/*.yaml"), ".github/workflows/ci.yml",
             capabilities=("ci",), line_pattern=r"^\s*jobs\s*:", example_content="name: ci\non: push\njobs:\n  t:\n    runs-on: ubuntu-latest\n"),
)

# Lines inside workflow files (comments skipped). Each says where the
# workflow deploys or what it pushes; the secret NAMES a workflow references
# are read separately (``WORKFLOW_SECRET_RE``) and classified by ENV_RULES.
CI_RULES: tuple[CiRule, ...] = (
    CiRule("vercel", r"vercel\s+(?:deploy|pull|build)\b|vercel-action", "run: vercel deploy --prod"),
    CiRule("netlify", r"netlify\s+deploy\b|actions-netlify|netlify/actions", "run: netlify deploy --prod"),
    CiRule("fly", r"flyctl\s+deploy\b|flyctl-actions|\bfly\s+deploy\b", "run: flyctl deploy"),
    CiRule("cloudflare", r"wrangler\s+(?:deploy|publish|pages)\b|wrangler-action|cloudflare/pages-action", "run: wrangler deploy", capabilities=("deploy",)),
    CiRule("supabase", r"supabase\s+db\s+push\b|supabase\s+link\b|supabase\s+migration\b", "run: supabase db push", capabilities=("database",)),
    CiRule("docker", r"docker/build-push-action|docker\s+push\b", "run: docker push img"),
)

# The one command whose absence makes ``supabase/migrations/`` a finding.
MIGRATION_PUSH_RE = r"supabase\s+db\s+push\b"

WORKFLOW_SECRET_RE = r"secrets\.([A-Za-z_][A-Za-z0-9_]*)"

# ── manifests that carry dependencies ───────────────────────────────────────
NPM_MANIFESTS = ("package.json",)
PY_MANIFESTS = ("pyproject.toml", "Pipfile")
REQUIREMENTS_GLOBS = ("requirements*.txt", "requirements/*.txt")
GO_MANIFESTS = ("go.mod",)
GEM_MANIFESTS = ("Gemfile",)

# ── env files ───────────────────────────────────────────────────────────────
ENV_EXAMPLE_GLOBS = (".env.example", ".env.sample", ".env.*.example", ".env.*.sample")
# Anything else starting ``.env`` is a real secrets file: reported, never opened.
PUBLIC_ENV_PREFIXES = ("NEXT_PUBLIC_", "PUBLIC_", "VITE_", "REACT_APP_", "EXPO_PUBLIC_", "NUXT_PUBLIC_")
# Unrecognised variables are grouped by prefix as ``unknown`` when they LOOK
# like a credential (last segment) and the prefix is not app plumbing.
CREDENTIAL_SUFFIXES = {"KEY", "TOKEN", "SECRET", "URL", "DSN", "ID", "PASSWORD", "ENDPOINT", "HOST"}
PLUMBING_PREFIXES = {
    "APP", "NODE", "NEXT", "PORT", "HOSTNAME", "LOG", "LOGGING", "DEBUG", "ENV", "ENVIRONMENT",
    "SITE", "BASE", "API", "PUBLIC", "SECRET", "JWT", "SESSION", "COOKIE", "CORS", "ALLOWED",
    "DEFAULT", "ENABLE", "FEATURE", "TZ", "PYTHON", "PATH", "URL", "CRON", "ADMIN", "WEB",
    "SERVER", "CLIENT", "HOST", "DB", "DATABASE",
}

# ── owner's docs (step 3 + §11) ─────────────────────────────────────────────
VERCEL_PROJECT_LINE_RE = r"Vercel\s+project\s+`([^`/\s]+)/([^`\s]+)`"
SETUP_DOC_GLOBS = ("*setup*.md", "*setup-decision*")

# A docs line attaches to a provider only when the provider's name (the
# catalog's ``doc_terms``) or one of these declared aliases appears in it.
DOC_ALIASES: dict[str, tuple[str, ...]] = {
    "docker": (r"supabase\s+start",),
    "error-tracking": (r"error[- ]monitoring",),
}

# Docs phrasing that makes a mention future tense: the service is not in use
# yet. Matched per line; a docs-only service whose every line matches is
# ``planned``.
PLANNED_PHRASES = (
    r"\badd\b[^.]*\bPhase\s*\d",
    r"\blater\b",
    r"\bTODO\b",
    r"\bplanned\b",
)

# A docs line that is a table row or list item naming two or more known
# providers is a comparison, not a stack declaration.
LIST_ITEM_RE = r"^\s*(?:\||[-*+]\s|\d+[.)]\s)"

# Identifier shapes masked from evidence excerpts (not credentials, but the
# excerpt must not print what the row promises it did not read).
ID_SHAPES = (
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
    r"\bproj_[A-Za-z0-9_\-]+",
    r"\b[a-z0-9]{20,}\b",
)
# ``ref: abc123def`` / ``project ref `abc123def` `` — keeps the label, masks the id.
REF_SHAPE = r"(?i)\b((?:project[-_ ]?)?ref\b(?:\s*[:=]\s*|\s+(?=[`\"'])))[`\"']?[A-Za-z0-9]{6,}[`\"']?"
