---
view:
  v: 1
  layout: table
  fields: [cuisine, time, serves]
  labels:
    time: Cook time
    serves: Serves
  sort: [cuisine, title]
  group_by: cuisine
  counts: true
  empty_labels:
    cuisine: Unfiled
    time: not timed yet
  empty: No recipes yet — ask the agent to add one.
page:
  header: [cuisine, time, serves]
---
# How this directory is displayed

The frontmatter above is a **view spec**: it tells the wiki how to render this
directory and the pages inside it. The whole file is the spec, which is why it
lives here rather than in `GUIDE.md` — the store replaces a file's frontmatter
wholesale on every write, so a spec sharing a file with prose would be deleted
by the first turn that rewrote the prose without repeating it.

Ask the agent to change it in plain language: "show the recipes grouped by
course instead", or "add a difficulty column".
