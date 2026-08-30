"""Route wiring that only matters once static/index.html serves the wiki too.

`/` was safe to leave unauthenticated while it was an empty shell whose every
fetch was authenticated anyway - that stopped being true once it became the
merged tree/renderer/chat page, since an open shell that 403s its own tree
fetch on load is a worse experience than the Access login redirect it gets
instead. This cannot be asserted in the container tier: that stack runs
DEV_BYPASS_AUTH=1, so a 401 there would prove nothing about production.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from app.auth import current_identity
from app.main import app


def _dependency_callables(path: str, method: str) -> list:
    for route in app.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and method in (route.methods or ())
        ):
            return [d.call for d in route.dependant.dependencies]
    raise AssertionError(f"no route registered for {method} {path}")


def test_the_merged_page_requires_auth():
    assert current_identity in _dependency_callables("/", "GET")


def test_kb_deep_links_still_require_auth():
    assert current_identity in _dependency_callables("/kb", "GET")
    assert current_identity in _dependency_callables("/kb/{path:path}", "GET")


def test_the_checkbox_write_requires_auth():
    assert current_identity in _dependency_callables("/api/kb/checkbox", "PATCH")


def test_sw_is_reachable_without_auth():
    """Registration must work before, or without, a Cloudflare Access session."""
    assert current_identity not in _dependency_callables("/sw.js", "GET")
