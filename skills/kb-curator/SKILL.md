---
name: kb-curator
description: >
  Extend and correct the TigerFS knowledge base mounted at /mnt/kb, and track
  wiki work in beads. Use this before writing anything into the knowledge base;
  when the user says something worth remembering; when asked what needs doing,
  what is outstanding, or what the backlog looks like; when you notice a problem
  you are not fixing right now; and when asked to review, diff, or undo a recent
  change. Triggers include "add this to the wiki", "remember that", "what should
  I work on", "what's left", and "what changed recently". For answering a
  question the wiki may already document, delegate to the kb-query subagent
  instead of loading this.
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

## Delegate the reading

Answering a question from the wiki is a job for the `kb-query` subagent: it
reads and reports, and cannot write, so it costs you none of your own context
and can do nothing you would need to review. Auditing the wiki is `kb-lint`'s.

Dispatch a few at a time. A wide parallel fan-out from one session hits API rate
limits, and a subagent cannot ask the user anything — so anything needing a
judgement call, or leaving an edit the user should see happen, stays here.

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

### When the fix belongs in the app, not the wiki

Some of what you notice is not about the knowledge base at all — it is about
the thing you are running in. "This page contradicts that one" is wiki work.
"The browser should render `SKILL.md` when I open a skill directory" is not:
no amount of editing the wiki will do it, because it lives in the image.

File those too, but mark them, and never claim one:

```bash
bd create --title="Short, specific title" \
  --description="What should change, and what made you want it." \
  --type=feature --priority=2 --labels image
bd update <id> --status deferred
```

Two commands, and it has to be two: `bd create` has no `--status` flag on the
version running here, and passing one does not warn — it prints `unknown flag`
and **creates nothing at all**. Set the status in a second call and check the
id came back.

`deferred` keeps it out of `bd ready`, which is the frontier of work *you* can
actually do — the same reason signal beads are deferred. You cannot ship an
image: you have no repo, no git, and the one you are running is read-only.

What happens next is that a human collects these, does the work in the repo,
and the next deployment closes the bead with a note naming the commit. So the
description is the whole of your case: write what you were doing, what was
awkward, and what you wanted instead. You are the only user of this product who
sees it from the inside, and nobody can ask you a follow-up question later.

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

A directory may also hold a `VIEW.md`, whose frontmatter decides how the wiki
renders that directory as an index and what appears above each page in it. It
is yours to write and revise: when the human describes how they want a folder
to look — as a table, grouped by something, with certain fields showing — that
is a view spec, not a page you hand-maintain. Read `references/directory-views.md`
before writing or changing one.

That reference specifies the spec's keys, which is enough to *change* a view
whose fields already exist. Giving a directory a view it has never had is a
larger job — the pages have to carry the fields first, which is a survey, a
decision made with the human, and a backfill that rarely fits in one turn. Read
`memory/skills/views/SKILL.md` in the knowledge base and follow it for that.

## Read what curating has already taught you

This skill ships inside the application image and cannot be edited from a
conversation, so what it has learned lives beside it in the knowledge base, at
`memory/skills/kb-curator/LEARNED.md`. Read it before substantive curation
work.

Its entries are scoped to this activity and they win over the general guidance
above when the two conflict; a later entry wins over an earlier one. When the
human states a preference about how the wiki should be curated, that file is
where it goes.

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
| scoped to an activity a skill owns | that skill's `## Learned` section — or, for a skill that ships in the image, its overlay at `memory/skills/<skill>/LEARNED.md` |
| a thing to go and do | a bead |

Scope and cost line up. `memory/CLAUDE.md` loads on every turn, so a preference
that only matters while ingesting documents is overpriced there and correctly
priced in the `ingest` skill. The test is one question: *would I want this to
apply while doing something else?* If yes, it is memory.

`## Learned` is append-only *to a reflection turn*, so a reflection revises a
preference by adding an entry that says it supersedes the earlier one — a later
entry wins. You are an ordinary turn and may rewrite these sections and overlays
outright, which is the point: do not let them grow unbounded. Pruning them, and
folding what is durable back into a skill's body, is ordinary work, and there is
usually a `consolidate` bead already asking for it.

## Version history and undo

The KB has a hidden control surface for reading history and rolling back
changes. Read `references/tigerfs.md` when you need to answer "what changed?"
or "what did this say before?", or before undoing anything.

## The backlog page

`memory/backlog.md` is regenerated from the bead graph after every turn. Never
edit it by hand — your edit will be overwritten. Change the beads instead.
