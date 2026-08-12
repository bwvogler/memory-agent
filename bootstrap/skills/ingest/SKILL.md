---
name: ingest
description: >
  Integrate a new source document into the wiki — write a summary page, update
  the index, update related entity and concept pages, and file follow-up work
  as beads. Use when the human gives you an article, paper, transcript, link,
  PDF, meeting notes, or any other raw source to file away. Triggers include
  "add this to the wiki", "file this", "ingest this", "here's an article",
  "summarise this into the KB", and pasting a long document with no other
  instruction.
---

# Ingesting a new source

Ingest is how raw sources become wiki knowledge. A single source typically
touches several wiki pages — a new summary, plus updates to any entity or
concept pages the source adds to or contradicts.

Most sources imply more work than one turn should do. Write the summary and the
clearly-correct updates now; file the rest.

## Steps

1. **Read the source.** If it is a file path, read it. If it is text pasted
   into the conversation, use it directly.

2. **Discuss key takeaways.** Briefly tell the human what you found — the main
   claims, anything surprising, any tension with what the wiki already says.
   Give them a chance to redirect emphasis before you write anything.

3. **Write a summary page** under `sources/` (create the directory if needed).
   Name it after the source. Include title, origin and date if known, a
   one-paragraph summary, and the key claims in bullet form.

4. **Update `index.md`.** Add a line for the new summary page. Create
   `index.md` with a simple catalog structure if it does not exist.

5. **Update related pages.** For each entity or concept the source touches,
   find its page and add what the source says. Note contradictions rather than
   silently overwriting — flag with a short parenthetical like "(source X says
   otherwise)".

6. **File the follow-ups** (see below).

7. **Append to `log.md`.** Format: `## [YYYY-MM-DD] ingest | <title>`. One
   sentence on what was added, how many pages were updated, and how many beads
   were filed.

## Filing the follow-ups

Ingest is the richest source of discovered work in the wiki. Typical follow-ups:

- A concept the source leans on that has no page yet.
- A contradiction with an existing page that the human needs to resolve.
- An entity page that should be updated but that you lack context to write.
- A source referenced by this one that is worth ingesting too.

File each as its own bead, describing it so it can be picked up cold:

```bash
bd create --title="Create a concept page for <X>" \
  --description="Referenced throughout sources/<this-source>.md and in two other pages, with no page of its own. Should cover <the specific gap>." \
  --type=feature --priority=3
```

When the summary page must exist before another page can link to it, record the
order instead of describing it:

```bash
bd dep add <blocked-id> <blocker-id>
```

Do not file a bead for the steps above — those are this turn's work, and the
ledger is for work that outlives the turn.

## What counts as a contradiction

A factual claim that conflicts with something already in the wiki: different
dates, different outcomes, different attributions. Differences of emphasis or
framing are not contradictions — unless they would lead a reader to draw
opposite conclusions, in which case they are.

## When to ask

If the source is ambiguous about which entities or concepts it relates to, ask
before updating related pages. A one-sentence redirect is cheaper than undoing
a misclassification.
