"""Cloudflare Access JWT verification.

Cloudflare injects the Access token as the `Cf-Access-Jwt-Assertion` header on
every proxied request. Cloudflare's own docs are explicit that the *presence*
of this header is not authentication:

    "validation of the header alone is not sufficient - the JWT and signature
     must be confirmed to avoid identity spoofing."

So we verify the RS256 signature against the team's public keys, and check
`aud`, `iss` and `exp`. Anything that fails is a 403.

There is deliberately no second layer behind this one. Enforcing Access at a
tunnel ingress would be belt AND braces, but the tunnel would run inside a
machine that suspends at idle, taking itself down with no way to be woken (see
"Why there is no tunnel here" in the README). So the origin IS reachable
directly, on its .fly.dev hostname, and this function is the whole gate - which
is why it verifies the signature rather than trusting the header's presence.

Docs:
  https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import jwt
import jwt.algorithms
from fastapi import HTTPException, Request

from .config import config

_JWKS_TTL_SECONDS = 3600

_jwks_cache: dict | None = None
_jwks_fetched_at: float = 0.0


def display_name_for(email: str) -> str:
    """A name for the shared household chat, from a bare email.

    Module-level (not just Identity.display_name) because app/agent.py needs
    to resolve a name from `Turn.actor_email` - a plain string, not a verified
    Identity, since that turn may be replayed long after the request that
    started it. `config.household_names` is reviewed config the household
    sets up; never blank, so a member missing from it degrades to their
    email's local part rather than to anonymity - see docs/decisions/0017.
    """
    mapped = config.household_names.get(email.lower())
    if mapped:
        return mapped
    local = email.split("@", 1)[0]
    return local[:1].upper() + local[1:] if local else email


@dataclass(frozen=True)
class Identity:
    email: str
    subject: str

    @property
    def slug(self) -> str:
        """Filesystem-safe identifier, for per-user scratch directories."""
        return "".join(
            c if c.isalnum() or c in "-_" else "_" for c in self.email.lower()
        )

    @property
    def display_name(self) -> str:
        return display_name_for(self.email)


async def _get_jwks(*, force: bool = False) -> dict:
    global _jwks_cache, _jwks_fetched_at  # noqa: PLW0603 - process-wide JWKS cache
    fresh = (
        _jwks_cache is not None and (time.time() - _jwks_fetched_at) < _JWKS_TTL_SECONDS
    )
    if fresh and not force:
        return _jwks_cache  # type: ignore[return-value]
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(config.certs_url)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        _jwks_fetched_at = time.time()
    return _jwks_cache  # type: ignore[return-value]


def _find_key(jwks: dict, kid: str):
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(key)
    return None


async def verify(token: str) -> Identity:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(403, f"malformed Access token: {exc}") from exc

    kid = header.get("kid")
    if not kid:
        raise HTTPException(403, "Access token has no kid")

    jwks = await _get_jwks()
    key = _find_key(jwks, kid)
    if key is None:
        # Cloudflare rotates signing keys; a cache miss is expected occasionally.
        jwks = await _get_jwks(force=True)
        key = _find_key(jwks, kid)
    if key is None:
        raise HTTPException(403, "no matching Access signing key")

    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            audience=config.cf_aud,
            issuer=f"https://{config.cf_team_domain}",
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(403, f"invalid Access token: {exc}") from exc

    email = (claims.get("email") or "").lower()
    if not email:
        # A Cloudflare Access SERVICE token carries `common_name`, never
        # `email`, so this branch is the only thing that ever let a machine
        # caller in - and until MCP_CLIENT_IDS is set it still refuses one,
        # byte-for-byte as before. Access itself remains the gate; all this does
        # is stop rejecting a token Access already vouched for, and give it a
        # real identity so scratch, ledger and config dir resolve like anyone
        # else's. See docs/decisions/0014-the-machine-is-a-caller.md.
        common_name = (claims.get("common_name") or "").lower()
        if common_name and common_name in config.mcp_client_ids:
            email = config.mcp_identity_email.lower()
        if not email:
            raise HTTPException(403, "Access token has no email claim")

    # This is a SECOND allowlist check, defence in depth behind the Cloudflare
    # Access policy itself (see docs/decisions/0005-explicit-email-allowlist.md
    # for why both layers matter). ALLOWED_EMAIL_DOMAINS and ALLOWED_EMAILS are
    # OR'd together: an address passes if it matches either list. If both are
    # empty, this layer allows anyone Access already let through - fine only if
    # your Access policy itself is the sole gate.
    if config.allowed_email_domains or config.allowed_emails:
        domain = email.rsplit("@", 1)[-1]
        domain_ok = domain in config.allowed_email_domains
        email_ok = email in config.allowed_emails
        if not (domain_ok or email_ok):
            raise HTTPException(403, f"{email!r} is not on the allowlist")

    # `sub` is empty for a service token, so fall back to the token name. The
    # email is the household identity every caller shares; the subject is the
    # only record of WHICH caller it was.
    return Identity(
        email=email, subject=claims.get("sub") or claims.get("common_name") or ""
    )


async def current_identity(request: Request) -> Identity:
    """FastAPI dependency. Returns the verified caller, or raises 403."""
    if config.dev_bypass_auth:
        return Identity(email=config.dev_fake_email, subject="dev")

    token = request.headers.get("Cf-Access-Jwt-Assertion") or request.cookies.get(
        "CF_Authorization"
    )
    if not token:
        raise HTTPException(403, "missing Cloudflare Access token")
    return await verify(token)
