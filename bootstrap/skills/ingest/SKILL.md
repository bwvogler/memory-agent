---
name: ingest
description: >
  Integrate a new source document into the wiki. Use when the human gives you
  an article, paper, transcript, or other raw source to file away.
---

# Ingesting a new source

Ingest is how raw sources become wiki knowledge. A single source typically
touches several wiki pages — a new summary, plus updates to any entity or
concept pages that the source adds to or contradicts.

## Steps

1. **Read the source.** If it is a file path, read it. If it is text pasted
   into the conversation, use it directly.

2. **Discuss key takeaways.** Briefly tell the human what you found — the
   main claims, anything surprising, any tension with what the wiki already
   says. Give them a chance to redirect emphasis before you write anything.

3. **Write a summary page** under `sources/` (create the directory if it does
   not exist). Name it after the source. Include: title, origin/date if known,
   a one-paragraph summary, and the key claims in bullet form.

4. **Update `index.md`.** Add a line for the new summary page. If `index.md`
   does not exist yet, create it with a simple catalog structure.

5. **Update related pages.** For each entity (person, place, project) or
   concept the source touches, find its page and add what the source says.
   Note contradictions with existing claims rather than silently overwriting
   them — flag with a short parenthetical like "(source X says otherwise)".

6. **Append to `log.md`.** Format: `## [YYYY-MM-DD] ingest | <title>`.
   One sentence on what was added and how many pages were updated.

## What counts as a contradiction

A contradiction is a factual claim that conflicts with something already in the
wiki — different dates, different outcomes, different attributions. Do not flag
differences of emphasis or framing as contradictions. Do flag them if they would
lead a reader to draw opposite conclusions.

## When to ask

If the source is ambiguous about which entities or concepts it relates to, ask
before updating related pages. It is faster to get a one-sentence redirect than
to undo a misclassification.
