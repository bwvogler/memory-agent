# 0020 — Search ranks the wiki; the filesystem still lists it

**Status:** accepted

## Context

`skills/kb-curator/SKILL.md` has said, since the beginning: "Search first,
always. The most common failure in an agent-maintained knowledge base is not
a wrong fact, it is five near-duplicate documents that disagree slightly."
It has never named a mechanism. Retrieval today is `tools=["Read", "Glob",
"Grep"]` on the `kb-query` subagent (`app/agent.py`) and nothing else — Grep
finds only the words you guessed right, so the duplicate-page failure the
skill warns about has had no tool behind the warning.
`bootstrap/skills/lint/SKILL.md` asks for contradictions, orphan pages,
missing concept pages and missing cross-references — corpus-wide semantic
work Grep structurally cannot do either.

A household wiki is also a particular shape of corpus: thick with proper
nouns a dense embedding tends to blur (people, dish names, brands) and thin
on the kind of jargon dense retrieval is best at. That is the case for a
hybrid of dense and lexical search, not for either alone.

## Decision

**Chunked, hybrid ranking, gated so the lexical half needs no third party at
all.** `kb_chunks` (`app/search.py`) lives in the KB database, one row per
chunk, keyed on `(file_id, chunk_index)` and carrying `body_sha` and
`source_modified_at`. Postgres full-text search (a generated `tsvector`
column, GIN-indexed) is always on. Dense retrieval — Voyage AI embeddings in
a pgvector `vector(1024)` column, HNSW-indexed (`m=16, ef_construction=64`,
`vector_cosine_ops`) — turns on only when `VOYAGE_API_KEY` is set. The two are
fused by Reciprocal Rank Fusion at `k=60` (Cormack, Clarke & Büttcher 2009).
The one agent-facing surface is an in-process SDK MCP tool,
`mcp__wiki__search`, following `app/interact.py`'s `ask_server_for` pattern.

Every chunk's content — the text that is both embedded and fed to
`to_tsvector` — carries the wiki's own hierarchy as a three-line prefix: the
live path and title, the frontmatter (already parsed by TigerFS, so this
costs no YAML parser, same fact ADR 0018 relies on), and the heading trail.
That prefix is the whole reason hybrid earns itself here: the lexical half
gets `ragu`, `italian`, a person's name for free from it, and the dense half
gets the topical frame that makes a bare `## Method` section embeddable at
all.

Unlike the reference this was prompted by (`scalefreegan/hybrid-rag`,
dense-only despite its name), nothing here generates a disclosure hierarchy.
This wiki already has one — the directory tree, `VIEW.md`, `GUIDE.md`, index
pages — and a generated second one would be exactly the drifting derived
artifact the next section exists to avoid.

## Why this is not the derived index the views skill argues against

`bootstrap/skills/views/SKILL.md` is the standing argument against a
persisted derived index: "Re-deriving from them is idempotent, cannot go
stale, and cannot disagree with what is there. Do not keep a separate list of
what is done; there is nothing to keep it honest." `kb_chunks` is a persisted
derived index, and the answer is a **read-time liveness join**: the fusion
query joins every candidate back to
`tigerfs.memory ON m.id = c.file_id AND m.modified_at = c.source_modified_at`.
A chunk whose file has been written, moved or deleted since indexing does not
match, so it is simply not returned — invisible the instant the file
changes, not something a background reindex pass catches up on later.
Under-inclusive in that window, which is the safe direction; never wrong. The
returned path comes from the live recursive CTE (`kb.PATHS_CTE`), never from
`kb_chunks.path` — a moved file returns wherever it lives now, or nothing.
`tigerfs.memory.modified_at` is the thing that keeps the index honest, on
every read, for free; there is no separate progress list to keep in sync.

Measured, not reasoned: against a real (non-container) Postgres seeded
directly with the `tigerfs.memory` shape, rewriting a row and querying before
any reindex pass returned zero results for the old content; reindexing then
found the new content; deleting a row made it disappear from search
immediately, with no reindex run at all. See Note on verification.

## Search ranks; it never lists

Extends ADR 0018's argument against a `filter`: "An index that omits files
that exist makes a liar of the one artifact whose value is 'the wiki says
what is there', and it would be a hiding primitive an agent could write after
ingesting a poisoned page." The same is true of a ranking tool with no
listing counterpart standing next to it. `mcp__wiki__search`'s own
description, the appended system-prompt text, and `render_results`'s output
all say the same sentence on every call: "This RANKS; it does not list."
Glob and Grep are untouched, remain allowlisted, and remain the only way to
answer "what is there" — `kb-query` and `kb-lint` gain the search tool
*alongside* them, not instead of them.

## What a revert does not undo

Savepoints are `git add -A` over `$KB_MOUNT/memory` (ADR 0003) and do not
cover `kb_chunks`, which lives in Postgres outside the versioned tree. But a
revert is itself a write through the mount, so the reverted file's
`modified_at` moves and every one of its chunks goes invisible immediately —
via the same liveness join, before any reindex pass runs. `POST
/api/turns/{id}/revert` (`app/main.py`) triggers a detached reindex pass
right after `kb.undo_to_savepoint` succeeds, which closes the window by
re-indexing the reverted content. The reconciliation mechanism is the same
one invariant 2 already relies on, not a second one.

## Sending the wiki to a third party

Setting `VOYAGE_API_KEY` means page text is sent to Voyage AI to be embedded.
Stated plainly, and not treated as a detail: `app/embed.py`'s `enabled()` is
checked *before* an `httpx.AsyncClient` is ever constructed, so an unset key
means no outbound request happens at all, not merely that one would fail —
pinned by `tests/test_embed.py`. Deliberately **not** routed through
`app/mcp_catalog.py`, which grants the *agent* a tool behind a reviewed
secret. This key is used by the *app* — `app/search.py`'s reindex pass and
query path — and is never reachable from a turn at all. That is a stronger
containment than the catalog's, not a weaker one, and it means the catalog's
`auto_approve`/`deny` machinery (ADR 0015) has nothing to say here: there is
no tool call to approve or deny, only an app-level background job.

## Why lexical is on by default and dense is not

This is the one split in this ADR worth flagging on its own, since a first
reading might expect the whole feature to ship dark by default the way the
MCP catalog does. It does not, on purpose: Postgres full-text search over
data this app already reads is not a data-flow change and not a capability
grant, so gating it behind a flag would be a knob nobody has a reason to
turn. What is gated is exactly the one thing that changes what leaves this
process — the Voyage key — and nothing else.

## Consequences

A search issued in the same second as the write that would satisfy it may
miss it, because reindexing is detached and runs after `run_turn`,
`run_reflection` and revert rather than inside them. Acceptable because Glob
and Grep are still there, are immediately consistent, and see everything —
search is a ranking convenience layered on top of a filesystem that was
already complete.

`kb_chunks` grows without bound today; the reindex pass is a full-table hash
diff every time, fine at a few hundred files and not fine at tens of
thousands — filed as a bead rather than solved here, the same posture
`app/search.py`'s own comments take toward `hnsw.ef_search` tuning and a
`path_prefix` scope argument.

## What was rejected

The reference repo's generated L0–L3 disclosure hierarchy — this wiki has a
human-curated one already, and a generated second one would be the drifting
derived artifact this ADR's second section exists to avoid, at the cost of a
Claude turn per document in an app with exactly one turn slot
(`turns.Registry.begin`). Apache AGE / a knowledge graph — not available on
Neon, and the wiki's own links are already a walkable graph. A local
embedding model — ruled out by the ~1.53 GB image against the hard 2 GB Fly
suspend ceiling (`fly.toml`). A `pgvector` Python binding — vectors go in as
`$N::vector` text and are never read back, so there is nothing to parse on
the way in. A reranker, and BM25 via `pg_search`/ParadeDB — not available on
Neon; `ts_rank_cd` has no IDF term and this is stated rather than glossed as
BM25. A `path_prefix` filter on the search tool — deferred rather than
rejected, but ADR 0018's argument against `filter` belongs in that bead's
body so it is not re-litigated from scratch.

## Note on verification

Separated honestly, because the two halves rest on different evidence, and
because getting the container tier running was itself not straightforward —
worth recording so the next person does not repeat the detour.

**Measured against a real (non-container) Postgres 17**, seeded directly with
the `tigerfs.memory` column shape (no Docker, no FUSE mount — a smaller claim
than the container tier below): schema creation, the reindex diff query,
lexical search finding a real match, reindex idempotence, staleness by
construction, and deletion through the liveness join. This was the first
pass, done while Docker was unreachable in the sandbox this was built in.

**Measured against the real container**, once Docker Desktop was started and
`pytest --container tests/test_container.py` run to completion: all 52
tests pass, including `SEARCH_PROBE`'s full hybrid path — `CREATE EXTENSION
vector` succeeding against `pgvector/pgvector:pg18`, `search.start()`
reaching `state == "hybrid"`, the HNSW index, and the `$1::vector` round
trip, all against Neon's actual base image rather than reasoned about. Every
invariant this ADR claims — staleness by construction, deletion via the
liveness join, RRF keeping a one-sided hit, reindex idempotence, a no-op
rewrite costing no embedding call — is now a passing container-tier test,
not just a locally-reasoned one.

Getting there caught a real bug, in the *test*, not in `app/search.py`: the
first version of `SEARCH_PROBE` wrote its probe content without a trailing
newline and then re-read-and-rewrote it to simulate a no-op edit. TigerFS's
own documented quirk — CLAUDE.md: "the store adds a missing trailing
newline" — turned out not to be idempotent across a `read_text()` round
trip, so a *genuinely* unchanged file looked like a one-byte content change
and the no-op path correctly (and wrongly, given the false premise) treated
it as a real edit. Fixed by writing the exact same literal — with an
explicit trailing newline, per CLAUDE.md's own stated convention — on both
passes instead of reading the file back. A second, milder test bug in the
same pass: the first cut of the probe used "soffritto" as a supposedly-
unique marker word, not knowing the seeded bootstrap wiki already has a real
`wiki/recipes/ragu.md` that genuinely contains it. Both are documented in
`tests/test_container.py`'s comments at the point they were fixed, since
either failure mode is a plausible way to misdiagnose a future regression as
a probe artifact when it might not be one.

**Attempted against a real key, and blocked by an account setting, not a code
defect.** `tests/test_live_search.py::test_dense_search_finds_a_genuine_paraphrase`
writes two pages sharing no vocabulary with a paraphrase query, reindexes,
and checks that the real embedding finds the right one. Getting it to run
surfaced a real bug worth fixing regardless of the outcome below: a fresh
`app_exec` subprocess calling `search.reindex()` without first calling
`search.start()` in that SAME process is a silent no-op (`_state` defaults
to `"unavailable"`, scoped per process), and a second, more serious one -
`embed.py`'s retry had NO backoff between attempts, so a 429 was retried
immediately into the same rate-limit window and failed again essentially
every time. Both are fixed: the test helper calls `start()` first, and
`embed._call` now sleeps before a retry, honouring a `Retry-After` header
when Voyage sends one.

With that fixed, the reindex still could not get this account's key
embedded after six passes and a dozen-plus underlying requests. The raw API
answered why, in its own response header, unprompted:

> "You have not yet added your payment method in the billing page and will
> have reduced rate limits of **3 RPM and 10K TPM**. To unlock our standard
> rate limits, please add a payment method..."

Three requests per minute is not a limit any reasonable client-side retry
should try to absorb - it is Voyage's own account-tier throttle, separate
from the 200M free *tokens* this ADR's "Why lexical is on by default"
section already banks on, which the same header confirms still apply
regardless. This is not a code defect: `embed_documents`/`embed_query`
degrading to `None` and the caller falling back to lexical-only is the
correctly-designed response to exactly this condition, observed for real.
`test_a_real_turn_reaches_for_the_search_tool`, the other live claim in the
same file, passed on every attempt - the system prompt genuinely makes the
agent reach for `mcp__wiki__search`.

**The paraphrase claim is now measured, not reasoned.** A payment method
was added to the Voyage account (the free-token allowance is unaffected -
Voyage's own message says so); Voyage's docs promise the standard rate
limit takes effect "after several minutes," which was confirmed for real by
polling the raw API until its `x-api-warning` header stopped appearing,
rather than assumed. With the throttle actually lifted,
`test_dense_search_finds_a_genuine_paraphrase` passed: a real Voyage
embedding, queried for "kids falling asleep at night" - a phrase sharing no
literal word or stem with the target page's text or path - ranked that page
above an unrelated distractor, via the dense candidate list specifically
(`dense_rank` set), not by lexical coincidence.

Getting a clean pass surfaced one more real bug, caught only because a
production-shaped client (a browser hitting `GET /api/kb/search`, not this
ADR's own SQL-level container probe) was in the loop: the RRF formula's bare
`1.0`/`0.0` literals are Postgres's `numeric` type, which asyncpg decodes as
`Decimal` - and `Hit.score`'s `float` annotation enforces nothing at
runtime. A `Decimal` reaching `app/main.py`'s JSON response did not raise;
it silently stringified, and `target_hit["score"] > 0` broke one HTTP round
trip downstream with `'>' not supported between instances of 'str' and
'int'`. Fixed by casting the score expressions to `::float8` at the SQL
source, plus a defensive `float()` at the one place every row already
passes through (`search.search()`'s `Hit` construction) - and pinned in the
container tier, which does not need a live key to catch a Decimal that
survived past the SQL layer.

## Amendments

_None yet._
