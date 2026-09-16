"""Voyage AI embeddings - the only place wiki text leaves this process.

Anthropic has no embeddings endpoint, so this calls Voyage over plain
`httpx` - no SDK, no new dependency. The client is only ever constructed
inside `_call`, after `enabled()` has already returned True, so an unset
`VOYAGE_API_KEY` means literally no `httpx.AsyncClient` is ever built for
this purpose, not merely that a call would fail. That is the assertion
`app/search.py`'s data-flow claim rests on, and `tests/test_embed.py` pins it
by asserting the mock transport is never invoked.

Two entry points rather than one function with a flag, because Voyage embeds a
document and a query differently (`input_type`) and reversing that costs
recall with no error anywhere - the exact silent-failure shape this repo keeps
designing against.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

import httpx

from .config import config

log = logging.getLogger(__name__)

ENDPOINT = "https://api.voyageai.com/v1/embeddings"

# A schema fact wearing the costume of a setting. Changing it invalidates
# every stored embedding and needs an ALTER TABLE, which is not something an
# operator does by editing an environment variable - so it lives here, not in
# app/config.py.
DIMENSIONS = 1024

# The request cap is 1000 texts; 128 is the operating point. A household
# corpus is a handful of batches either way, and a smaller batch loses less
# work when one call fails.
MAX_BATCH_TEXTS = 128
MAX_BATCH_CHARS = 200_000

_TIMEOUT = 30.0

# A retry with no delay hits the same per-second/per-minute rate limit it
# just tripped - measured directly against the real API, where a fresh
# free-tier key 429'd on effectively every first call, and an immediate
# retry 429'd again every time. `Retry-After`, when Voyage sends one, wins
# over this; it is a ceiling on how long one reindex pass waits, not a
# guess at the server's actual limit.
_RETRY_DELAY_SECONDS = 5.0
_MAX_RETRY_DELAY_SECONDS = 15.0


def enabled() -> bool:
    """Whether the dense half of search can run at all.

    Checked BEFORE anything else in this module touches the network - see the
    module docstring. Lexical search never calls this and needs no key.
    """
    return bool(config.voyage_api_key)


def to_pgvector(vector: list[float]) -> str:
    """Format an embedding as a pgvector text literal, e.g. `[0.012345,...]`.

    No pgvector Python binding: the vector goes into SQL as `$N::vector` text
    and is never SELECTed back out, so there is nothing to parse on the way
    in. Six decimal places is plenty of precision for cosine similarity at
    1024 dimensions, and keeps the literal a few KB instead of tens of KB.
    """
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"


def _batches(texts: list[str]) -> list[list[str]]:
    """Split on both count and character budget.

    1000 texts is the API's own cap, but 1000 x a 2000-char chunk is far past
    any sane per-request token ceiling. Batching on characters too is what
    keeps a batch of long chunks from silently exceeding it.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for text in texts:
        if current and (
            len(current) >= MAX_BATCH_TEXTS
            or current_chars + len(text) > MAX_BATCH_CHARS
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(text)
        current_chars += len(text)
    if current:
        batches.append(current)
    return batches


def _retry_delay(resp: httpx.Response) -> float:
    """How long to wait before retrying, preferring the server's own answer.

    A `Retry-After` header (seconds, or an HTTP date - only the numeric form
    is handled, since Voyage documents seconds) beats our own guess; it is
    still capped, because a server-suggested delay is a floor on correctness
    for THAT server, not a promise this one reindex pass should honour no
    matter how long.
    """
    header = resp.headers.get("retry-after")
    if header:
        try:
            return min(float(header), _MAX_RETRY_DELAY_SECONDS)
        except ValueError:
            pass
    return _RETRY_DELAY_SECONDS


async def _call(
    texts: list[str], input_type: Literal["document", "query"]
) -> list[list[float]] | None:
    """One or more Voyage requests, batched. Never raises - see module docstring.

    A retry-then-give-up posture, matching `mcp_catalog`'s call to Google: one
    retry on a transient failure, then `None`, logged once rather than left to
    propagate into a turn. Search degrading to lexical-only is always a valid
    outcome here; a broken turn is not.
    """
    out: list[list[float]] = []
    headers = {"Authorization": f"Bearer {config.voyage_api_key}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
        for batch in _batches(texts):
            payload: dict[str, Any] = {
                "input": batch,
                "model": config.voyage_model,
                "input_type": input_type,
                "output_dimension": DIMENSIONS,
            }
            resp = None
            for attempt in range(2):
                try:
                    resp = await http.post(ENDPOINT, json=payload, headers=headers)
                except httpx.HTTPError as exc:
                    log.warning(
                        "voyage embed request failed (%s), attempt %d/2",
                        type(exc).__name__,
                        attempt + 1,
                    )
                    resp = None
                    if attempt == 0:
                        await asyncio.sleep(_RETRY_DELAY_SECONDS)
                    continue
                if resp.status_code == httpx.codes.OK:
                    break
                if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                    delay = _retry_delay(resp)
                    log.warning(
                        "voyage embed request returned %d; retrying in %.1fs",
                        resp.status_code,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                log.warning(
                    "voyage embed request returned %d; search stays lexical-only "
                    "for this pass",
                    resp.status_code,
                )
                return None
            if resp is None or resp.status_code != httpx.codes.OK:
                log.warning(
                    "voyage embed request failed after retry; search stays "
                    "lexical-only for this pass"
                )
                return None
            body = resp.json()
            # Never trust array order - the API documents that results may
            # not come back in request order, and a mismatch here would embed
            # chunk N's vector as chunk M's with no error anywhere.
            ranked = sorted(body["data"], key=lambda d: d["index"])
            out.extend(item["embedding"] for item in ranked)
    return out


async def embed_documents(texts: list[str]) -> list[list[float]] | None:
    """Embed chunk content for storage. `None` means: skip this pass, log only."""
    if not enabled() or not texts:
        return None
    return await _call(texts, "document")


async def embed_query(text: str) -> list[float] | None:
    """Embed one search query. `None` means: fall back to lexical-only."""
    if not enabled():
        return None
    result = await _call([text], "query")
    return result[0] if result else None
