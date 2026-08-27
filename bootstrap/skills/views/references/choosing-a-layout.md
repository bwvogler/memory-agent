# Choosing a layout

Read this at step 3 of the `views` pass, when the directory does not obviously
want a plain `list`. The keys used here are specified in the `kb-curator`
skill's `references/directory-views.md`; this file is about which of them to
reach for.

## Contents

- The two layouts, and how to tell which one a directory wants
- Worked example: uniform short values
- Worked example: long titles and prose
- Worked example: a collection where absence is the point
- When grouping earns its place
- When to propose no view at all

## The two layouts, and how to tell which one a directory wants

Ask one question of the pages: **does every page have a short value for every
field?** If yes, `table`. If some values are sentences, or some pages will
always be missing some fields, `list` — a table of mostly-blank cells looks
broken in a way a list does not.

The pane is not wide. Three columns read well, four is the practical limit, and
a fifth pushes the table into horizontal scrolling.

## Worked example: uniform short values

Forty recipes, each with a cuisine, a cook time and a serving count. Every page
has all three and none is longer than a few characters.

```yaml
view:
  layout: table
  fields: [cuisine, time, serves]
  labels:
    time: Cook time
  sort: [cuisine, title]
```

Why: uniform short values are exactly what columns are for, and sorting by
`cuisine` then `title` makes the table scannable without grouping it.

## Worked example: long titles and prose

A `sources/` directory of ingested articles. Titles run long, the useful
metadata is a date and an origin, and what the reader actually wants is a
reminder of what each one said.

```yaml
view:
  layout: list
  fields: [origin, date]
  excerpt: true
  sort: ["-date"]
```

Why: `excerpt` shows each page's first paragraph, which is the whole value of a
source index and is noise in a table. `-date` puts the newest first. Note the
quotes — a leading `-` starts a YAML list otherwise.

## Worked example: a collection where absence is the point

A deck of responsibility cards, each held by somebody or by nobody. The
interesting number is how many are unheld, and a card can be shared.

```yaml
view:
  layout: list
  fields: [holder, category]
  group_by: holder
  group_order: [brian, laura]
  counts: true
  empty_labels:
    holder: Undealt
```

Why each key is there:

- `group_by: holder` on a list-valued field puts a shared card under **every**
  holder, which is what "we both do this one" should look like.
- `empty_labels` makes an unheld card say `Undealt` rather than showing a blank.
  A blank reads as "nobody has filled this in"; `Undealt` is a fact about the
  household. Do not skip this on any field where empty is meaningful.
- `counts` puts `(n)` beside each heading, which is the number the whole
  collection exists to produce.
- `group_order` because the natural order here is neither alphabetical nor by
  size. Groups you do not name are appended after the ones you do.

## When grouping earns its place

Group when the reader's first question is "which of these are X?" and the
answer partitions the directory into a handful of runs. Roughly: three to eight
groups, each with more than one member.

Do not group when it produces one heading per page — that is a sorted list with
extra furniture. Sort instead. Do not group on a field most pages are missing,
either: you get one enormous empty group and a scattering of real ones, which
tells the reader less than no grouping at all. Backfill first, then group.

## When to propose no view at all

A directory of a few pages, or of pages with nothing structured in common,
renders fine as the default alphabetical list. Say so rather than inventing
fields for it. The cost of a view is not `VIEW.md`; it is the frontmatter every
future page in that directory now has to carry.
