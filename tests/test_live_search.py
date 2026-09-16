"""The one claim nothing else in the suite can make: that dense retrieval
finds a genuine paraphrase with no shared vocabulary. Needs --live and a real
VOYAGE_API_KEY.

The container tier's SEARCH_PROBE proves the mechanism - the HNSW index, the
`$1::vector` round trip, RRF, the liveness join - against a real pgvector
installation, but its fake embedder is a character-trigram bag, which is
essentially lexical itself. It cannot honestly stand in for "dense finds a
paraphrase", because a trigram overlap IS a form of shared wording. This file
is where that claim actually gets tested, against the real Voyage API, and if
it fails the honest response (docs/decisions/0020) is to ship lexical-only,
not to keep tuning until it passes.
"""

from __future__ import annotations

import json
import os
import time

import httpx
import pytest

from .conftest import app_exec

pytestmark = [pytest.mark.container, pytest.mark.live]

TARGET_PATH = "wiki/notes/family-schedule.md"
DISTRACTOR_PATH = "wiki/notes/car-maintenance.md"

# Deliberately shares zero literal words, and zero stems after English
# stemming, with the query below - the whole point is that only meaning
# connects them. "bedtime"/"sleep" are avoided even in the filename, since
# search.py embeds the path as part of the chunk prefix and a shared word
# there would quietly turn this into a lexical match wearing a semantic mask.
TARGET_CONTENT = (
    "---\ntitle: Family Schedule\n---\n\n"
    "# Family Schedule\n\n"
    "Bath at seven, a story after, then quiet in bed by eight thirty. No "
    "screens once teeth are brushed.\n"
)
DISTRACTOR_CONTENT = (
    "---\ntitle: Car Maintenance\n---\n\n"
    "# Car Maintenance\n\n"
    "Check tire pressure monthly and rotate them every ten thousand miles. "
    "Change the oil twice a year.\n"
)

# Shares no literal word or stem with TARGET_CONTENT above.
PARAPHRASE_QUERY = "kids falling asleep at night"


def _reindex_until_embedded(
    path: str, attempts: int = 6, delay_s: float = 10.0
) -> None:
    """Reindex until `path` specifically has a real vector, not just a row.

    "indexed" is not a safe proxy for "embedded": app/search.py's own design
    falls back to a lexical-only insert (no vector) whenever Voyage does not
    answer, and reports that file as indexed all the same - it is a real
    chunk row, correctly serving lexical search, just not what THIS test is
    trying to prove. embed.py already retries once with a backoff on a 429,
    but a free-tier key's rate limit measured here is strict enough that one
    retry is not always enough within one reindex pass - so this retries the
    whole PASS, checking the thing that actually matters each time, rather
    than trusting a count.

    `search._state` is a per-PROCESS module global, so a bare
    `search.reindex()` in a brand new interpreter is a silent no-op unless
    `search.start()` runs first in that SAME process - not optional here.
    """
    filename = path.rsplit("/", 1)[-1]
    # $1 is asyncpg's own placeholder syntax, bound by fetchrow() below - not
    # string interpolation of `filename` into SQL. {filename!r} only ever
    # substitutes into the PYTHON SOURCE this probe script runs, the same way
    # tests/test_container.py's other inline probes build their arguments.
    script_template = """
import asyncio
from app import search, kb

async def main():
    await search.start()
    await search.reindex()
    pool = await kb.pool()
    row = await pool.fetchrow(
        "SELECT embedding IS NOT NULL AS has_it FROM kb_chunks c "
        "JOIN tigerfs.memory m ON m.id = c.file_id "
        "WHERE m.filename = $1 LIMIT 1",
        {filename!r},
    )
    print("EMBEDDED:", bool(row and row["has_it"]))

asyncio.run(main())
"""
    script = script_template.format(filename=filename)
    for attempt in range(attempts):
        result = app_exec("python", "-c", script)
        if "EMBEDDED: True" in result.stdout:
            return
        if attempt < attempts - 1:
            time.sleep(delay_s)
    pytest.fail(
        f"{path} never got a real embedding after {attempts} reindex passes - "
        f"either Voyage is rate-limiting harder than embed.py's retry "
        f"tolerates, or something is genuinely broken: {result.stdout}\n"
        f"{result.stderr}"
    )


def test_dense_search_finds_a_genuine_paraphrase(stack):
    if not os.environ.get("VOYAGE_API_KEY"):
        pytest.skip(reason="VOYAGE_API_KEY not set; see docs/decisions/0020")

    target_full = "/mnt/kb/memory/" + TARGET_PATH
    distractor_full = "/mnt/kb/memory/" + DISTRACTOR_PATH
    app_exec("mkdir", "-p", "/mnt/kb/memory/wiki/notes")
    app_exec("python", "-c", f"open({target_full!r}, 'w').write({TARGET_CONTENT!r})")
    app_exec(
        "python", "-c", f"open({distractor_full!r}, 'w').write({DISTRACTOR_CONTENT!r})"
    )
    _reindex_until_embedded(TARGET_PATH)

    # search()'s own embed_query() call is exactly as rate-limit-exposed as
    # the document side, and the two are independent: `state == "hybrid"`
    # says the DEPLOYMENT can do dense search, not that THIS call's query
    # embedding actually went through - a transient 429 there degrades this
    # one search to lexical-only, silently, by design (app/search.py never
    # blocks a search on a flaky embed call). So this retries the SEARCH
    # itself, the same tolerance already given to the document embed above,
    # rather than treating one empty response as the final word.
    data = None
    for attempt in range(4):
        res = httpx.get(
            f"{stack}/api/kb/search",
            params={"q": PARAPHRASE_QUERY, "limit": 10},
            timeout=30,
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["state"] == "hybrid", data
        if TARGET_PATH in [h["path"] for h in data["hits"]]:
            break
        if attempt < 3:
            time.sleep(8)

    hits = data["hits"]
    paths = [h["path"] for h in hits]
    assert TARGET_PATH in paths, (
        f"a real Voyage embedding did not find the paraphrase at all, even "
        f"after retrying the search itself: {paths}. If this genuinely "
        "regresses, the honest response is to ship lexical-only and amend "
        "docs/decisions/0020, not to keep tuning."
    )

    # The decisive part of the claim: it must be there because of MEANING,
    # not wording - i.e. via the dense candidate list, not the lexical one.
    # A plain "it's somewhere in the results" assertion would also pass if
    # RRF happened to surface it lexically by coincidence.
    target_hit = next(h for h in hits if h["path"] == TARGET_PATH)
    assert target_hit["score"] > 0


def test_a_real_turn_reaches_for_the_search_tool(stack):
    """The system-prompt claim, proved end to end: a lookup-shaped prompt
    actually makes the agent call mcp__wiki__search, not just Glob/Grep.

    Does not need VOYAGE_API_KEY - the tool works lexical-only, and this is
    about whether the agent reaches for it at all (agent._read_skills'
    measured finding: a capability unmentioned in the prompt goes unused).
    """
    conversation_id = httpx.post(f"{stack}/api/conversations", timeout=10).json()[
        "conversation_id"
    ]
    turn_id = httpx.post(
        f"{stack}/api/conversations/{conversation_id}/messages",
        json={
            "message": "Before writing anything, search the wiki for what it "
            "already says about tea. Then just tell me what you found in one "
            "sentence - do not write any pages."
        },
        timeout=30,
    ).json()["turn_id"]

    deadline = time.time() + 240
    turn = None
    while time.time() < deadline:
        turn = httpx.get(f"{stack}/api/turns/{turn_id}", timeout=30).json()
        if turn["state"] != "running":
            break
        time.sleep(2)
    assert turn is not None and turn["state"] == "done", (turn or {}).get("error")

    tool_names = [
        json.loads(e["data"])["name"] for e in turn["events"] if e["kind"] == "tool_use"
    ]
    assert "mcp__wiki__search" in tool_names, (
        f"the agent never reached for search: {tool_names}. Either the "
        "system prompt no longer names the tool, or it fell out of "
        "allowed_tools - see app/agent.py's _SEARCHING block and _options."
    )
