---
name: kb-curator
description: >
  Navigate, extend and correct the TigerFS knowledge base mounted at /mnt/kb,
  and track wiki work in beads. Use this before writing anything into the
  knowledge base; when answering a question that might already be documented;
  when the user says something worth remembering; when asked what needs doing,
  what is outstanding, or what the backlog looks like; when you notice a
  problem you are not fixing right now; and when asked to review, diff, or undo
  a recent change. Triggers include "add this to the wiki", "what do we know
  about X", "remember that", "what should I work on", "what's left", "clean
  this up", and "what changed recently".
---

# Curating the knowledge base

The knowledge base is a TigerFS filesystem: ordinary file tools work on it,
every write is versioned, and nothing is ever really lost. Treat it as a garden
you are tending, not an append-only log.

A garden is never finished, which is why this skill has two halves: doing the
work in front of you, and recording the work you noticed but did not do.

## Start with the ledger

Before substantive work, see what is already outstanding:

```bash
bd ready --json
```

If anything there overlaps what the user just asked for, say so before you
start — the point of the ledger is that a past session's findings reach this
one. Claim what you are about to work on so a later session does not repeat it:

```bash
bd update <id> --claim
bd close <id> --reason="..."   # when actually done, not before
```

For a quick question ("what does the wiki say about X?") skip this. The ledger
is for work, not for lookups.

## File what you notice

This is the habit that matters most. While doing anything in the wiki you will
notice problems you are not going to fix in this turn: a page that contradicts
another, a claim with no date, a concept referenced everywhere but never
defined, a directory with no `GUIDE.md`.

Do not mention these only in chat, where they scroll away. File them:

```bash
bd create --title="Short, specific title" \
  --description="What is wrong, where, and how you would fix it." \
  --type=task --priority=2
```

Write the description for someone with no memory of this conversation, because
that is exactly who will read it. A title like "fix the notes page" is useless
in a month. Use `--type=bug` for something actually wrong, `task` for tidying,
`feature` for something missing.

When one piece of work genuinely cannot start until another finishes, record it
rather than describing it in prose:

```bash
bd dep add <blocked-id> <blocker-id>
```

Do not file a bead for every step of the task you are actively doing. Beads are
for work that outlives the turn.

## Read before you write

Search first, always. The most common failure in an agent-maintained knowledge
base is not a wrong fact, it is five near-duplicate documents that disagree
slightly. Look for an existing document that should absorb the new information,
and prefer extending a good document over creating a thin new one. If you find
two documents that overlap, say so rather than silently adding a third.

## Follow the guide hierarchy

Before writing anything in a directory, check whether a `GUIDE.md` exists there
and follow its format. For overall workspace structure, read `AGENT_GUIDE.md`
at the root of your workspace.

If no guide exists for a directory and you are creating structured content, ask
the human how they would like it formatted, then write a `GUIDE.md` recording
that format so future turns follow the same convention.

## Writing well

Write documents a colleague would want to read: a clear title, the claim up
front, supporting detail after. Date anything time-sensitive, because a
knowledge base whose facts have no timestamps decays invisibly.

When you correct something, correct it in place rather than appending a
contradiction. The history is preserved automatically, so the current version
should always read as the truth, not as an argument with itself.

## Accumulated memory

Things you have learned that should survive this conversation go in
`memory/CLAUDE.md` — not in `bd remember`, whatever the beads instructions say.
Keep it short and high-signal: it loads into every future conversation, so a
page of stale detail costs more than it is worth. Prune it when you touch it.

Memory is for facts that shape how you work. Beads are for work to be done.
"The user prefers recipes in metric" is memory; "convert the recipes to metric"
is a bead.

### Where a preference belongs

When the human states a preference, put it where its scope matches:

| the preference | where it goes |
|---|---|
| about them, or applies to everything you do | `memory/CLAUDE.md` |
| scoped to an activity a skill owns | that skill's `## Learned` section |
| a thing to go and do | a bead |

Scope and cost line up. `memory/CLAUDE.md` loads on every turn, so a preference
that only matters while ingesting documents is overpriced there and correctly
priced in the `ingest` skill. The test is one question: *would I want this to
apply while doing something else?* If yes, it is memory.

A skill's `## Learned` section is append-only, so revise a preference by adding
an entry that says it supersedes the earlier one — a later entry wins. Do not
let that section grow unbounded; folding it back into the skill body is
ordinary work, and there is usually a `consolidate` bead already asking for it.

## Version history and undo

The KB has a hidden control surface for reading history and rolling back
changes. Read `references/tigerfs.md` when you need to answer "what changed?"
or "what did this say before?", or before undoing anything.

## The backlog page

`memory/backlog.md` is regenerated from the bead graph after every turn. Never
edit it by hand — your edit will be overwritten. Change the beads instead.
