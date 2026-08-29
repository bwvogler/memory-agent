"""Configuration, read once from the environment at import time."""

import os
from dataclasses import dataclass, field


def _csv(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _bool(name: str, *, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0").lower() in ("1", "true", "yes")


def _name_map(name: str) -> dict[str, str]:
    """Parse `email=Name,email=Name` into a lookup. Malformed entries are skipped."""
    raw = os.environ.get(name, "")
    out: dict[str, str] = {}
    for raw_pair in raw.split(","):
        pair = raw_pair.strip()
        if "=" not in pair:
            continue
        email, _, display = pair.partition("=")
        email = email.strip().lower()
        display = display.strip()
        if email and display:
            out[email] = display
    return out


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    agent_model: str = os.environ.get("AGENT_MODEL", "claude-sonnet-4-6")

    kb_database_url: str = os.environ.get("KB_DATABASE_URL", "")
    kb_mount: str = os.environ.get("KB_MOUNT", "/mnt/kb")

    session_database_url: str = os.environ.get("SESSION_DATABASE_URL", "")
    work_dir: str = os.environ.get("WORK_DIR", "/work")

    # Where app/mcp_catalog.py writes the credential files its servers insist on
    # reading from disk. Container-local ON PURPOSE, and the default should stay
    # that way: not WORK_DIR, because the volume outlives the process and has no
    # savepoint covering it, and not KB_MOUNT, which the agent can read. Nothing
    # here is state - it is rewritten from the environment at every boot.
    mcp_state_dir: str = os.environ.get("MCP_STATE_DIR", "/tmp/mcp-catalog")  # noqa: S108

    cf_team_domain: str = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "")
    cf_aud: str = os.environ.get("CF_ACCESS_AUD", "")
    # Two independent allowlists, checked as OR: a caller passes if their email
    # matches EITHER list. Domain matching is for "anyone at this company";
    # explicit emails are for "these specific people", which matters whenever
    # the addresses aren't on a domain you control (e.g. personal Gmail
    # accounts) - allowlisting "gmail.com" would let in every Gmail user alive.
    # Leaving both empty allows anyone Access itself lets through.
    allowed_email_domains: list[str] = field(
        default_factory=lambda: _csv("ALLOWED_EMAIL_DOMAINS")
    )
    allowed_emails: list[str] = field(default_factory=lambda: _csv("ALLOWED_EMAILS"))

    dev_bypass_auth: bool = _bool("DEV_BYPASS_AUTH")
    dev_fake_email: str = os.environ.get("DEV_FAKE_EMAIL", "dev@localhost")

    max_turns: int = int(os.environ.get("MAX_TURNS", "30"))

    # A whole-turn backstop, independent of max_turns (which counts model turns,
    # not wall-clock time - a single slow tool call never trips it). Exists
    # because a stuck turn is never evicted (see turns.Registry._evict) and
    # blocks the whole household behind it until the process restarts. See
    # img-r7o and docs/decisions/0017.
    turn_timeout_seconds: float = float(os.environ.get("TURN_TIMEOUT_SECONDS", "900"))

    # Display names for the household chat, since Cloudflare Access does not
    # reliably carry a name claim. Reviewed config, same shape and same
    # argument as MCP_CLIENT_IDS/MCP_IDENTITY_EMAIL (ADR 0014): a name is
    # config, not KB content, because the prompt is built before any KB read.
    # An email missing from this map falls back to its local part - see
    # agent.display_name_for - never to blank.
    household_names: dict[str, str] = field(
        default_factory=lambda: _name_map("HOUSEHOLD_NAMES")
    )

    # How long a turn waits for a human. The two differ because their timeouts
    # resolve in opposite directions: an unanswered *question* proceeds on the
    # agent's judgement, so waiting longer costs only latency; an unanswered
    # *permission request* is denied, so waiting longer wastes a turn's budget
    # on something that was never going to be allowed.
    ask_timeout_seconds: float = float(os.environ.get("ASK_TIMEOUT_SECONDS", "600"))
    permission_timeout_seconds: float = float(
        os.environ.get("PERMISSION_TIMEOUT_SECONDS", "300")
    )

    # Identity for a machine caller (the /mcp surface). Cloudflare Access
    # service tokens carry `common_name`, not `email`, so verify() would reject
    # one outright; these two turn an allowlisted token into a real identity.
    # Empty MCP_CLIENT_IDS - the default - keeps that rejection exactly as it
    # was. See docs/decisions/0014-the-machine-is-a-caller.md.
    mcp_client_ids: list[str] = field(default_factory=lambda: _csv("MCP_CLIENT_IDS"))
    mcp_identity_email: str = os.environ.get("MCP_IDENTITY_EMAIL", "")

    # A second, real-per-person path onto /mcp (ADR 0014 amendment): a household
    # member's own OAuth-authenticated email, permitted to drive the surface as
    # THEMSELVES rather than collapsed onto MCP_IDENTITY_EMAIL. Deliberately its
    # own list rather than a reuse of ALLOWED_EMAIL_DOMAINS/ALLOWED_EMAILS - the
    # same reasoning MCP_CLIENT_IDS already rests on: browsing and driving an
    # unattended, non-interactive turn are different grants. Empty by default,
    # so an untouched deployment refuses every real identity here.
    mcp_oauth_emails: list[str] = field(
        default_factory=lambda: _csv("MCP_OAUTH_EMAILS")
    )

    # Attachments arrive base64-encoded inside a JSON body, which has no
    # natural size limit: without a cap, one large file is decoded into memory
    # and written to the volume before anything can object. The per-request cap
    # is what stops ten files each just under the per-file one.
    max_upload_bytes: int = int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024**2)))
    max_upload_total_bytes: int = int(
        os.environ.get("MAX_UPLOAD_TOTAL_BYTES", str(25 * 1024**2))
    )

    @property
    def certs_url(self) -> str:
        return f"https://{self.cf_team_domain}/cdn-cgi/access/certs"

    def validate(self) -> list[str]:
        """Return a list of fatal misconfigurations. Empty list means OK."""
        problems: list[str] = []
        if not self.anthropic_api_key:
            problems.append("ANTHROPIC_API_KEY is not set")
        if not self.kb_database_url:
            problems.append("KB_DATABASE_URL is not set")
        if self.dev_bypass_auth:
            # Loud, because shipping this to production would expose the agent
            # to anyone who can reach the origin.
            problems.append(
                "DEV_BYPASS_AUTH=1 — authentication is DISABLED. "
                "Never set this in production."
            )
        else:
            if not self.cf_team_domain:
                problems.append("CF_ACCESS_TEAM_DOMAIN is not set")
            if not self.cf_aud:
                problems.append("CF_ACCESS_AUD is not set")
        if self.mcp_client_ids and not self.mcp_identity_email:
            # Fails closed either way - an empty email cannot pass the allowlist
            # check - but silently, and the operator believes they enabled MCP.
            problems.append(
                "MCP_CLIENT_IDS is set but MCP_IDENTITY_EMAIL is not, so every "
                "machine caller will be rejected"
            )
        return problems


config = Config()
