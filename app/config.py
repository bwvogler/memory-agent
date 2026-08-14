"""Configuration, read once from the environment at import time."""

import os
from dataclasses import dataclass, field


def _csv(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _bool(name: str, *, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0").lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    agent_model: str = os.environ.get("AGENT_MODEL", "claude-sonnet-4-6")

    kb_database_url: str = os.environ.get("KB_DATABASE_URL", "")
    kb_mount: str = os.environ.get("KB_MOUNT", "/mnt/kb")

    session_database_url: str = os.environ.get("SESSION_DATABASE_URL", "")
    work_dir: str = os.environ.get("WORK_DIR", "/work")

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
        return problems


config = Config()
