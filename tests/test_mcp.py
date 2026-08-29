"""The machine-caller surface: identity, and the four tools.

The identity half is the part worth guarding hardest. Before this change no
machine could authenticate at all - `verify` requires an `email` claim and a
Cloudflare Access service token carries `common_name` instead - and the fix has
to stay closed by default. A deployment that has not opted in must behave
byte-for-byte as it did, or this becomes a way in that nobody configured.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import auth, mcp_server
from app.main import app
from app.turns import Turn, TurnState


def _config(monkeypatch, module, **overrides):
    """Config is frozen, so swap a replaced copy into the module under test."""
    monkeypatch.setattr(
        module, "config", dataclasses.replace(module.config, **overrides)
    )


# --- identity ---------------------------------------------------------------


def _identity_from(monkeypatch, claims: dict, **overrides) -> auth.Identity:
    """Run verify()'s claim-handling half against fixed claims.

    Stubs the JWT layer rather than the function under test: the decision this
    covers is what happens to a token that verified fine and simply has no
    email, which is exactly the service-token case.
    """
    _config(
        monkeypatch,
        auth,
        allowed_email_domains=[],
        allowed_emails=[],
        **overrides,
    )
    monkeypatch.setattr(auth.jwt, "get_unverified_header", lambda _t: {"kid": "k"})
    monkeypatch.setattr(auth, "_get_jwks", _fake_jwks)
    monkeypatch.setattr(auth, "_find_key", lambda _j, _k: "key")
    monkeypatch.setattr(auth.jwt, "decode", lambda *a, **k: claims)
    return asyncio.run(auth.verify("token"))


async def _fake_jwks(*, force: bool = False) -> dict:
    return {"keys": []}


def test_a_service_token_is_still_refused_by_default(monkeypatch):
    """The default is unchanged behaviour, not a new door standing open."""
    with pytest.raises(HTTPException) as caught:
        _identity_from(monkeypatch, {"common_name": "laptop.access", "sub": ""})

    assert caught.value.status_code == 403
    assert "email" in str(caught.value.detail)


def test_an_allowlisted_service_token_becomes_the_household_identity(monkeypatch):
    identity = _identity_from(
        monkeypatch,
        {"common_name": "laptop.access", "sub": ""},
        mcp_client_ids=["laptop.access"],
        mcp_identity_email="person@example.com",
    )

    assert identity.email == "person@example.com"
    # The email is shared; the subject is the only record of which caller it was.
    assert identity.subject == "laptop.access"


def test_an_unlisted_service_token_is_refused(monkeypatch):
    with pytest.raises(HTTPException):
        _identity_from(
            monkeypatch,
            {"common_name": "someone-elses.access", "sub": ""},
            mcp_client_ids=["laptop.access"],
            mcp_identity_email="person@example.com",
        )


def test_the_mapped_email_still_faces_the_allowlist(monkeypatch):
    """ADR 0005's second layer must not be bypassed by arriving via a token."""
    monkeypatch.setattr(
        auth,
        "config",
        dataclasses.replace(
            auth.config,
            allowed_email_domains=["example.com"],
            allowed_emails=[],
            mcp_client_ids=["laptop.access"],
            mcp_identity_email="person@elsewhere.org",
        ),
    )
    monkeypatch.setattr(auth.jwt, "get_unverified_header", lambda _t: {"kid": "k"})
    monkeypatch.setattr(auth, "_get_jwks", _fake_jwks)
    monkeypatch.setattr(auth, "_find_key", lambda _j, _k: "key")
    monkeypatch.setattr(
        auth.jwt, "decode", lambda *a, **k: {"common_name": "laptop.access", "sub": ""}
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(auth.verify("token"))

    assert "allowlist" in str(caught.value.detail)


def test_a_human_token_is_unaffected(monkeypatch):
    identity = _identity_from(
        monkeypatch,
        {"email": "Person@Example.com", "sub": "abc"},
        mcp_client_ids=["laptop.access"],
        mcp_identity_email="other@example.com",
    )

    assert identity.email == "person@example.com"
    assert identity.subject == "abc"


# --- whether the surface reports itself honestly ----------------------------


def test_mcp_reports_off_when_nothing_is_configured(monkeypatch):
    _config(
        monkeypatch,
        mcp_server,
        dev_bypass_auth=False,
        mcp_client_ids=[],
        mcp_identity_email="",
    )
    assert mcp_server.enabled() is False


def test_mcp_reports_off_when_only_half_configured(monkeypatch):
    """Otherwise this reads as 'on' while refusing every call."""
    _config(
        monkeypatch,
        mcp_server,
        dev_bypass_auth=False,
        mcp_client_ids=["laptop.access"],
        mcp_identity_email="",
    )
    assert mcp_server.enabled() is False


def test_mcp_reports_on_when_a_caller_could_authenticate(monkeypatch):
    _config(
        monkeypatch,
        mcp_server,
        dev_bypass_auth=False,
        mcp_client_ids=["laptop.access"],
        mcp_identity_email="person@example.com",
    )
    assert mcp_server.enabled() is True


# --- the tools --------------------------------------------------------------


def test_all_four_capabilities_are_exposed():
    tools = asyncio.run(mcp_server.mcp.list_tools())

    assert {t.name for t in tools} == {"ingest", "query", "lint", "reflect"}


def test_every_tool_says_what_it_is_for():
    """A tool with no description is a tool no client will ever choose."""
    for tool in asyncio.run(mcp_server.mcp.list_tools()):
        assert tool.description, tool.name


def test_a_tool_call_runs_a_real_non_interactive_turn(monkeypatch):
    """Not an imitation of a turn: the savepoint, guards and signals all matter."""
    seen = {}

    async def fake_run_turn(turn, prompt, user_slug, **kwargs):
        seen["prompt"] = prompt
        seen["interactive"] = turn.interactive
        seen["slug"] = user_slug
        turn.append("text", "answered")
        turn.savepoint = f"turn-{turn.id}"
        turn.finish(TurnState.DONE)

    monkeypatch.setattr(mcp_server.agent, "run_turn", fake_run_turn)
    token = mcp_server._caller.set(
        auth.Identity(email="person@example.com", subject="laptop.access")
    )
    try:
        result = asyncio.run(mcp_server.query("what do we know about tea?"))
    finally:
        mcp_server._caller.reset(token)

    assert result["ok"] is True
    assert result["reply"] == "answered"
    assert result["savepoint"], "a caller needs this to undo the call from the UI"
    assert seen["interactive"] is False, "nobody is watching an MCP call"
    assert seen["slug"] == "person_example_com"
    assert "tea" in seen["prompt"]


def test_a_tool_points_at_the_skill_rather_than_restating_it(monkeypatch):
    """A copy of the skill's contents here would drift from the editable one."""
    seen = {}

    async def fake_run_turn(turn, prompt, user_slug, **kwargs):
        seen["prompt"] = prompt
        turn.finish(TurnState.DONE)

    monkeypatch.setattr(mcp_server.agent, "run_turn", fake_run_turn)
    token = mcp_server._caller.set(auth.Identity(email="p@e.com", subject="s"))
    try:
        asyncio.run(mcp_server.lint())
    finally:
        mcp_server._caller.reset(token)

    assert "skills/lint/SKILL.md" in seen["prompt"]


def test_a_failed_turn_returns_its_diagnosis_rather_than_raising(monkeypatch):
    async def fake_run_turn(turn, prompt, user_slug, **kwargs):
        turn.finish(TurnState.ERROR, error="the mount went away")

    monkeypatch.setattr(mcp_server.agent, "run_turn", fake_run_turn)
    token = mcp_server._caller.set(auth.Identity(email="p@e.com", subject="s"))
    try:
        result = asyncio.run(mcp_server.query("anything"))
    finally:
        mcp_server._caller.reset(token)

    assert result["ok"] is False
    assert result["error"] == "the mount went away"


def test_a_second_caller_is_refused_rather_than_queued(monkeypatch):
    """Two agents do not fit under the memory ceiling, and savepoints are global.

    Patches `running`, which is what Registry.begin consults. It used to patch
    `any_running` and stub the lock this surface kept for itself; the rule now
    lives in one place for every caller. See tests/test_concurrency.py.
    """
    in_flight = Turn(id="already-going", user_email="other@e.com")
    monkeypatch.setattr(mcp_server.registry, "running", lambda: in_flight)
    token = mcp_server._caller.set(auth.Identity(email="p@e.com", subject="s"))
    try:
        result = asyncio.run(mcp_server.query("anything"))
    finally:
        mcp_server._caller.reset(token)

    assert result["ok"] is False
    assert "Busy" in result["error"]


def test_reflect_goes_through_the_real_reflection_path(monkeypatch):
    """Its remit lives in evolve.py and _reflection_options, not in a prompt."""
    called = {}

    async def fake_maybe_reflect(user_slug, trigger):
        called["trigger"] = trigger
        return "turn-abc"

    monkeypatch.setattr(mcp_server.agent, "maybe_reflect", fake_maybe_reflect)
    token = mcp_server._caller.set(auth.Identity(email="p@e.com", subject="s"))
    try:
        result = asyncio.run(mcp_server.reflect())
    finally:
        mcp_server._caller.reset(token)

    assert result == {
        "ok": True,
        "turn_id": "turn-abc",
        "note": result["note"],
    }
    assert called["trigger"] == "mcp"


def test_reflect_reports_why_it_declined(monkeypatch):
    async def declines(user_slug, trigger):
        return None

    monkeypatch.setattr(mcp_server.agent, "maybe_reflect", declines)
    token = mcp_server._caller.set(auth.Identity(email="p@e.com", subject="s"))
    try:
        result = asyncio.run(mcp_server.reflect())
    finally:
        mcp_server._caller.reset(token)

    assert result["ok"] is False
    assert "already running" in result["error"]


# --- the reply is the agent's, not a subagent's -----------------------------


def test_subagent_output_is_not_returned_as_the_reply():
    """agent_text is on the same event buffer and must not be mistaken for text."""
    turn = Turn(id="t1", user_email="p@e.com")
    turn.append("agent_text", '{"agent": "toolu_1", "text": "subagent chatter"}')
    turn.append("text", "the actual answer")

    assert mcp_server._text(turn) == "the actual answer"


def test_streamed_tokens_are_preferred_over_the_fallback():
    turn = Turn(id="t1", user_email="p@e.com")
    turn.append("text_delta", "the ")
    turn.append("text_delta", "answer")
    turn.append("text", "the answer")

    assert mcp_server._text(turn) == "the answer"


# --- the three things mounting breaks silently ------------------------------


def test_the_endpoint_is_served_at_the_mount_root():
    """FastMCP defaults to /mcp INSIDE itself, which under a /mcp mount is /mcp/mcp.

    A 404 at the URL we publish, from a component that imported and mounted
    perfectly. Pinned as a setting rather than a request so it fails with the
    reason rather than a bare 404.
    """
    assert mcp_server.mcp.settings.streamable_http_path == "/"


def test_the_host_header_check_is_off_rather_than_localhost_only():
    """It auto-enables for localhost and would answer 421 to every real call."""
    security = mcp_server.mcp.settings.transport_security
    assert security is not None
    assert security.enable_dns_rebinding_protection is False


def test_the_tools_are_reachable_over_the_real_asgi_mount(monkeypatch):
    """End to end through the mount, the auth middleware and the session manager.

    Every layer here is one a unit test can pass while the real thing 404s or
    500s, which is why this drives the actual protocol. In particular a mounted
    sub-app's lifespan never runs, so `session_manager()` has to be entered by
    `app/main.py` - without it this request fails with "Task group is not
    initialized", a 500 on the happy path from code that imports perfectly.

    **This is the only test that may enter `session_manager()`.**
    `StreamableHTTPSessionManager.run()` raises if called twice on one instance,
    and `mcp` is a module-level singleton, so a second test doing the same thing
    fails on whichever runs second. That is fine in production - one process, one
    lifespan - and it is why this covers the startup rather than a separate test.
    """
    # The middleware still runs; this takes its dev-bypass branch so the test is
    # about routing and startup. Header enforcement is covered on its own below.
    _config(monkeypatch, mcp_server, dev_bypass_auth=True)

    async def scenario() -> set[str]:
        transport = httpx.ASGITransport(app=app)
        headers = {"Accept": "application/json, text/event-stream"}
        async with (
            mcp_server.session_manager(),
            httpx.AsyncClient(transport=transport, base_url="http://probe") as client,
        ):
            opened = await client.post(
                "/mcp/",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "probe", "version": "1"},
                    },
                },
            )
            assert opened.status_code == 200, opened.text
            session = opened.headers.get("mcp-session-id")
            assert session, "no session id; the session manager did not start"
            headers["mcp-session-id"] = session
            await client.post(
                "/mcp/",
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            listed = await client.post(
                "/mcp/",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
            assert listed.status_code == 200, listed.text
            for line in listed.text.splitlines():
                if line.startswith("data: "):
                    payload = json.loads(line[len("data: ") :])
                    return {t["name"] for t in payload["result"]["tools"]}
            pytest.fail(f"no data frame in the response: {listed.text[:300]}")

    assert asyncio.run(scenario()) == {"ingest", "query", "lint", "reflect"}


# --- the cookie is deliberately not accepted here ---------------------------


def _request(headers: dict[str, str] | None = None, cookies: str = "") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        raw.append((b"cookie", cookies.encode()))
    return Request({"type": "http", "method": "POST", "path": "/mcp/", "headers": raw})


def test_an_access_cookie_alone_is_refused(monkeypatch):
    """A cookie is ambient; this endpoint drives the agent. Header or nothing.

    Whether a cross-site POST would actually carry the cookie depends on the
    SameSite attribute Cloudflare sets, which is not verified anywhere here. That
    is why this is defence in depth rather than a patched exploit: a machine
    caller sends the header regardless, so refusing the cookie is free.
    """
    _config(monkeypatch, mcp_server, dev_bypass_auth=False)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(mcp_server._authenticate(_request(cookies="CF_Authorization=x")))

    assert caught.value.status_code == 403
    assert "cookie" in str(caught.value.detail)


def test_the_header_is_verified_rather_than_trusted(monkeypatch):
    """Present is not the same as valid; the token still goes through verify()."""
    _config(
        monkeypatch,
        mcp_server,
        dev_bypass_auth=False,
        mcp_oauth_emails=["person@example.com"],
    )
    seen = {}

    async def fake_verify(token):
        seen["presented"] = token
        return auth.Identity(email="person@example.com", subject="laptop.access")

    monkeypatch.setattr(mcp_server.auth, "verify", fake_verify)

    identity = asyncio.run(
        mcp_server._authenticate(_request({"Cf-Access-Jwt-Assertion": "tok"}))
    )

    assert seen["presented"] == "tok"
    assert identity.email == "person@example.com"


# --- the real-per-person OAuth path, and its own allowlist ------------------


def test_a_real_identity_not_on_the_oauth_allowlist_is_refused(monkeypatch):
    """Passing ADR 0005's browser allowlist is not enough for this surface.

    Real per-person OAuth (Cloudflare Access's Managed OAuth toggle on the same
    Access Application the browser uses) reaches auth.verify() with a real
    `email` claim - verify() itself never collapses that. Whether it may drive
    this NON-INTERACTIVE surface is a second, separate decision.
    """
    _config(monkeypatch, mcp_server, dev_bypass_auth=False, mcp_oauth_emails=[])

    async def fake_verify(token):
        return auth.Identity(email="person@example.com", subject="abc")

    monkeypatch.setattr(mcp_server.auth, "verify", fake_verify)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            mcp_server._authenticate(_request({"Cf-Access-Jwt-Assertion": "tok"}))
        )

    assert caught.value.status_code == 403
    assert "person@example.com" in str(caught.value.detail)


def test_a_service_token_caller_is_unaffected_by_the_oauth_allowlist(monkeypatch):
    """The pre-existing collapsed identity needs no entry on the new list."""
    _config(
        monkeypatch,
        mcp_server,
        dev_bypass_auth=False,
        mcp_identity_email="person@example.com",
        mcp_oauth_emails=[],
    )

    async def fake_verify(token):
        return auth.Identity(email="person@example.com", subject="laptop.access")

    monkeypatch.setattr(mcp_server.auth, "verify", fake_verify)

    identity = asyncio.run(
        mcp_server._authenticate(_request({"Cf-Access-Jwt-Assertion": "tok"}))
    )

    assert identity.email == "person@example.com"


def test_mcp_reports_on_when_only_oauth_emails_are_configured(monkeypatch):
    """The OAuth path needs no MCP_IDENTITY_EMAIL - it never collapses to one."""
    _config(
        monkeypatch,
        mcp_server,
        dev_bypass_auth=False,
        mcp_client_ids=[],
        mcp_identity_email="",
        mcp_oauth_emails=["person@example.com"],
    )
    assert mcp_server.enabled() is True
