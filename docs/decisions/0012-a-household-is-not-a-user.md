# 0012 — A household is not a user, and the ledger has to know it

**Status:** proposed

## Context

ADR 0011 puts the Fair Play deck at the centre of the knowledge base. The deck
has two holders. The application has one.

Isolation in this codebase is per-user and deliberate: `setting_sources=[]`,
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, a `CLAUDE_CONFIG_DIR` per user, scratch at
`$WORK_DIR/{user_slug}/`, and a bead graph inside it that `bd` discovers from the
agent's cwd (ADR 0006). Every one of those is right for keeping two strangers
apart.

The knowledge base is the opposite. There is one `memory/` workspace, one
`AGENT_GUIDE.md`, one `memory/CLAUDE.md`, and — the load-bearing detail — one
`memory/backlog.md`, written by `kb.export_backlog()` after every turn from
**the acting user's private graph**.

So the moment Laura signs in:

- Her turn regenerates `memory/backlog.md` from a graph that has never seen a
  single one of Brian's beads, and overwrites his projection with it. His next
  turn overwrites hers back. The shared file flips with no indication why.
- `bd ready` answers differently for each of them, so "a past session's findings
  reach this one" — the entire argument for ADR 0006 — holds for whichever of
  them happened to file the bead.
- A card assigned in one graph is invisible in the other, which is precisely the
  failure Fair Play exists to fix, reproduced inside the tool meant to fix it.

**ADR 0009 already described this bug.** Its subject was two *machines*: "two
ledgers fight over one projection and each overwrite hides the other's work; a
human reading `/kb` sees a backlog that flips with no indication why." That is
the same sentence, arriving through a different door — two *users* on one
machine. 0009 solved it by refusing to run a second machine. There is no
equivalent move here: refusing the second person is refusing the feature.

## Decision

**One ledger per household, discovered rather than configured.**

`bd` walks upward from the working directory to find `.beads`. Confirmed
directly: a graph initialised in a parent directory is found, read and written
by `bd` run from a child, with no flag and no environment variable. Since every
user's scratch is `$WORK_DIR/{user_slug}/`, a graph at `$WORK_DIR/.beads` is
already on the discovery path of every agent session this deployment will ever
run.

So `ensure_beads` initialises the graph at `$WORK_DIR` instead of inside the
per-user scratch, and nothing else changes: the agent's own `bd` calls need no
new argument, `Bash(bd:*)` still covers them, and `export_backlog` writes one
projection of one graph into the one workspace it was always writing to.

**Attribution moves from the graph to the bead.** `bd` already stamps an owner
on every issue it creates. Who filed a bead becomes a field, which is where it
belonged: two graphs was never an attribution mechanism, it was two sets of
books.

**This deployment is one household.** That is the actual scope change, and it
should be stated rather than discovered. Per-user config dirs, disabled auto
memory and per-user scratch all stay — they still keep sessions from colliding —
but the *work ledger* is now explicitly shared, and running this image for two
unrelated families would leak one family's backlog into the other's. The
knowledge base was always shared; this makes the ledger honest about matching it.

### Rejected alternatives

**A backlog file per user** (`backlog-{slug}.md`). Fixes the overwrite and
nothing else. Two people planning one household from two private lists is the
invisible-labour problem with a projection layer on top, and Fair Play's whole
premise is a single shared system of record.

**Household-scoped beads inside per-user graphs**, synchronised. A sync protocol
between two Dolt databases on the same volume, to reconstruct the single graph
we could have had by initialising it one directory higher.

## Consequences

**Nothing in the ledger is private.** Both holders see every bead, including the
`signal` beads that record a turn going wrong and the `REJECTED self-edit` beads
from ADR 0008. For a household this is right — the deck is a shared instrument —
but it is a genuine change: a revert bead quotes the prompt that caused it, and
that prompt is now readable by the other person. Anyone who wants a private note
has `memory/CLAUDE.md`, which was already shared, so the honest answer is that
this system has no private surface at all and should not pretend to.

**Existing beads have to move, and there are two of them to move.** The deployed
graph at `$WORK_DIR/{brian}/.beads` holds real work (`kb-1m7`, `kb-nb4`,
`kb-5xv`, `kb-068`, `kb-b82`, plus signal beads). Migration is an export and an
import into the new location, and it must happen before the first turn that
would initialise an empty graph over the top. Getting this wrong loses the
backlog quietly, which is this system's characteristic failure.

**`bd init` is not a quiet command, and now it runs one directory higher.** ADR
0010 documents what it writes unprompted: `AGENTS.md`, `CLAUDE.md`, `.claude/`,
`.cursor/`, `.codex/`. At `$WORK_DIR` those land in the parent of every user's
scratch — a directory that is on the agent's cwd path. `setting_sources=[]` and
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` mean none of it is loaded, which is the only
reason this is survivable; the init still needs to be run somewhere clean and
have its debris removed, exactly as 0010 warns.

**The container tier has to grow a second user.** Every bead test today runs as
`dev@localhost` and would pass unchanged against a per-user graph, so the tier
cannot currently tell the two designs apart. A test that files a bead as one
identity and reads it back as another is what makes this ADR real; without it
the regression is invisible, which is the condition `tests/test_container.py`
exists to prevent.

**ADR 0009 still holds and is now doing more work.** One machine owns the
ledger, and the ledger is now the household's. The savepoint argument is
unchanged and the concurrency ceiling gets worse in a specific way: savepoints
are a `git add -A` over one workspace, so two people taking turns at the same
time already interfere. Two people who live together will do that on a Sunday
evening. This ADR does not fix it and should not be read as fixing it.
