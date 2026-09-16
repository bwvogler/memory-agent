"""app/embed.py - the only place wiki text leaves this process.

No key -> no request is the load-bearing claim in docs/decisions/0020: with
`VOYAGE_API_KEY` unset, no `httpx.AsyncClient` is ever constructed for this
purpose. Asserted here by a transport that raises if it is ever invoked, not
merely by checking the return value - a call that happened to fail would pass
a weaker version of this test for the wrong reason.
"""

from __future__ import annotations

import asyncio
import json
import types

import httpx
import pytest

from app import embed


def _client(monkeypatch, handler):
    """Route embed.py's AsyncClient through httpx's own MockTransport.

    Same technique as tests/test_mcp_catalog.py's `_google` helper - no new
    test dependency, and `asyncio.run` because this repo has no async plugin.
    """
    real = httpx.AsyncClient
    monkeypatch.setattr(
        embed.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )


def _never_called(_request):
    raise AssertionError("no VOYAGE_API_KEY should mean no HTTP request at all")


def _embeddings(vectors):
    return lambda request: httpx.Response(
        200,
        json={"data": [{"embedding": v, "index": i} for i, v in enumerate(vectors)]},
    )


def _fake_config(*, voyage_api_key: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        voyage_api_key=voyage_api_key, voyage_model="voyage-4-lite"
    )


@pytest.fixture
def no_key(monkeypatch):
    # Config is a frozen dataclass, so the whole object is swapped rather
    # than assigning one field - same technique as test_agent_options.py's
    # `isolated` fixture.
    monkeypatch.setattr(embed, "config", _fake_config(voyage_api_key=""))


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(embed, "config", _fake_config(voyage_api_key="test-key"))


def test_disabled_without_a_key(no_key):
    assert embed.enabled() is False


def test_enabled_with_a_key(with_key):
    assert embed.enabled() is True


def test_no_key_means_no_http_request_at_all(no_key, monkeypatch):
    _client(monkeypatch, _never_called)

    assert asyncio.run(embed.embed_documents(["hello"])) is None
    assert asyncio.run(embed.embed_query("hello")) is None


def test_document_side_uses_document_input_type(with_key, monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return _embeddings([[0.1, 0.2]])(request)

    _client(monkeypatch, handler)
    result = asyncio.run(embed.embed_documents(["some chunk text"]))

    assert result == [[0.1, 0.2]]
    assert b'"input_type":"document"' in seen["body"]


def test_query_side_uses_query_input_type(with_key, monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return _embeddings([[0.5, 0.6]])(request)

    _client(monkeypatch, handler)
    result = asyncio.run(embed.embed_query("what does the wiki say"))

    assert result == [0.5, 0.6]
    assert b'"input_type":"query"' in seen["body"]


def test_response_is_resorted_by_index_not_array_order(with_key, monkeypatch):
    """The API may return results out of request order - never trust position."""

    def handler(_request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [9.0], "index": 1},
                    {"embedding": [1.0], "index": 0},
                ]
            },
        )

    _client(monkeypatch, handler)
    result = asyncio.run(embed.embed_documents(["first", "second"]))

    assert result == [[1.0], [9.0]]


def test_batches_split_on_text_count(with_key, monkeypatch):
    calls = []

    def handler(request):
        body = request.read()
        calls.append(body)
        n = len(json.loads(body)["input"])
        return _embeddings([[float(i)] for i in range(n)])(request)

    _client(monkeypatch, handler)
    texts = [f"chunk {i}" for i in range(embed.MAX_BATCH_TEXTS + 10)]
    result = asyncio.run(embed.embed_documents(texts))

    assert result is not None
    assert len(result) == len(texts)
    assert len(calls) == 2  # 128 + 10, split at MAX_BATCH_TEXTS


def test_batches_split_on_char_budget(with_key, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.read())
        return _embeddings([[0.0]])(request)

    _client(monkeypatch, handler)
    # Two texts that together exceed MAX_BATCH_CHARS must split, even though
    # both are well under MAX_BATCH_TEXTS.
    big = "x" * (embed.MAX_BATCH_CHARS - 100)
    result = asyncio.run(embed.embed_documents([big, big]))

    assert result is not None
    assert len(calls) == 2


def _no_real_delay(monkeypatch):
    """Stub the backoff sleep so a retry test stays fast.

    A real retry against Voyage needs a real delay - see the module
    docstring on _RETRY_DELAY_SECONDS, measured against a fresh key that
    429'd on effectively every first call - but a *test* asserting that a
    retry happens should not itself take seconds to run.
    """
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(embed.asyncio, "sleep", fake_sleep)
    return sleeps


def test_a_429_returns_none_rather_than_raising(with_key, monkeypatch):
    _no_real_delay(monkeypatch)

    def handler(_request):
        return httpx.Response(429, json={"error": "rate limited"})

    _client(monkeypatch, handler)

    assert asyncio.run(embed.embed_documents(["x"])) is None


def test_a_500_returns_none_rather_than_raising(with_key, monkeypatch):
    _no_real_delay(monkeypatch)

    def handler(_request):
        return httpx.Response(500, json={"error": "boom"})

    _client(monkeypatch, handler)

    assert asyncio.run(embed.embed_documents(["x"])) is None


def test_a_429_is_retried_once_after_a_delay(with_key, monkeypatch):
    """The retry that a bare loop with no backoff would waste on the same
    rate limit it just tripped - measured for real against Voyage, where an
    immediate retry 429'd again every time."""
    sleeps = _no_real_delay(monkeypatch)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return _embeddings([[1.0]])(request)

    _client(monkeypatch, handler)
    result = asyncio.run(embed.embed_documents(["x"]))

    assert result == [[1.0]]
    assert calls["n"] == 2
    assert sleeps == [embed._RETRY_DELAY_SECONDS]


def test_retry_after_header_is_honoured_and_capped(with_key, monkeypatch):
    sleeps = _no_real_delay(monkeypatch)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429, headers={"retry-after": "999"}, json={"error": "rate limited"}
            )
        return _embeddings([[1.0]])(request)

    _client(monkeypatch, handler)
    asyncio.run(embed.embed_documents(["x"]))

    assert sleeps == [embed._MAX_RETRY_DELAY_SECONDS]


def test_to_pgvector_formats_as_a_bracketed_literal():
    assert embed.to_pgvector([0.1, -0.25, 3.0]) == "[0.100000,-0.250000,3.000000]"


def test_embed_documents_with_no_texts_makes_no_call(with_key, monkeypatch):
    _client(monkeypatch, _never_called)

    assert asyncio.run(embed.embed_documents([])) is None
