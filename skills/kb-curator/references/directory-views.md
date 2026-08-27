# Directory views: how a folder decides what it looks like

A directory can hold a `VIEW.md` whose **frontmatter is a display spec**. It
controls two things at that level:

1. the **index** the reader sees when they click the directory in the tree
2. the **field header** shown above each individual page in that directory

You write and revise these like any other file. The human asks in plain
language — "show the recipes as a table with cook times", "group the cards by
who holds them", "put the undealt ones first" — and you edit the spec.

## The file

`VIEW.md` sits beside `GUIDE.md`. Both describe the directory rather than
living in it, and neither shows up as an entry in the index.

```markdown
---
view:
  v: 1
  layout: table
  fields: [cuisine, time, serves]
  labels:
    time: Cook time
  sort: [cuisine, title]
  group_by: cuisine
  counts: true
  empty_labels:
    cuisine: Unfiled
page:
  header: [cuisine, time]
---
Prose explaining the view, for whoever edits it next.
```

**Why its own file and not a `view:` key in `GUIDE.md`.** The store replaces a
file's frontmatter *wholesale* on every write — omit a key and it is deleted.
A spec sharing a file with prose would be destroyed by the first turn that
rewrote the prose without repeating the spec, silently. Because the whole of
`VIEW.md` is the spec, rewriting it always rewrites the spec, and there is
nothing to forget. See `docs/decisions/0018`.

**Keep everything nested under `view:` and `page:`.** `title`, `author` and
`encoding` are stored in their own database columns, so a key with one of those
names at the *top* level of the frontmatter never reaches the spec at all.

## The vocabulary

Everything is optional. An absent `VIEW.md` gives a plain alphabetical list.

### `view:` — the directory index

| key | what it does |
|---|---|
| `v` | spec version. `1` today. An unknown version still renders, with a warning. |
| `layout` | `table` or `list`. `list` is the default and is better when pages have long titles or you want excerpts. |
| `fields` | ordered field names: the columns of a table, the metadata line of a list. |
| `labels` | `{field: "Human Label"}`. Shared by the index and the page header. |
| `title_field` | which field names the entry. Defaults to the page's `title`, then its first `#` heading, then its filename. |
| `excerpt` | `true` to show each page's first paragraph. Useful in `list`, noise in `table`. |
| `sort` | ordered field names; `-field` sorts descending. Ties break on title, numerically ("Card 2" before "Card 10"). |
| `group_by` | field to group on. If the field is a **list**, the entry appears under *every* one of its values. |
| `group_order` | the order groups appear in. Groups you do not name are appended alphabetically. |
| `counts` | `true` to show `(n)` beside each group heading. |
| `empty_labels` | `{field: "Undealt"}` — what a *missing* value means. |
| `value_labels` | `{field: {value: "Label"}}` — prettify particular values, e.g. `{msc_agreed: {false: "not yet agreed"}}`. |
| `empty` | the message shown when the directory has no pages yet. |

### `page:` — each page at this level

| key | what it does |
|---|---|
| `header` | ordered field names, rendered as small labelled chips above the page's prose. |

## Things that are deliberately not in the vocabulary

**There is no `filter`.** A view can reorder and group; it can never hide a
page. An index that omits files that exist is worse than no index, because the
one thing a reader trusts it for is "this is what is here". If a subset needs
its own page, that is a subdirectory, not a filter.

**No layout takes HTML, a template, or an expression.** The renderer builds
fixed layouts out of the field names you list. This is a security boundary, not
a missing feature — see `docs/decisions/0016`.

**A field never becomes a link.** Every field renders as text. The only link in
an index is the entry's own title, pointing at its page.

**Views do not inherit.** A subdirectory with no `VIEW.md` gets the default
list, not its parent's spec — the same way `GUIDE.md` does not inherit.

## Making the fields exist

A view is only as good as the frontmatter it reads. When you introduce a view
for a directory, say so in that directory's `GUIDE.md` so future pages carry
the fields — and remember the wiki's general rule: **frontmatter when code will
read it, prose when only people will** (`docs/decisions/0011`). A directory of
free prose should not grow frontmatter just because it could.

Two practical notes:

- **Read the stored file before you edit it.** The store re-serialises what you
  wrote — frontmatter keys come back in a different order — so composing an
  edit from memory produces a diff you did not intend.
- **The store does not coerce.** `no` stays the string `"no"` and `1:30` stays
  `"1:30"`; you do not need to quote them defensively. (Measured against the
  real store; `tests/test_container.py` pins it.)

## When something is wrong with a spec

The page still renders. Anything the spec got wrong appears as a warning box
above the entries naming the problem — an unknown layout, a misspelled key, a
value of the wrong shape. If the human reports one, read `VIEW.md`, fix the key
it names, and the warning goes away.
