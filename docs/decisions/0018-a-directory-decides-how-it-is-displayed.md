# 0018 — A directory decides how it is displayed, and the spec is its own file

**Status:** accepted

## Context

A KB directory had almost no presence in the UI. Clicking one in the tree
toggled its disclosure triangle and, if it happened to hold a `SKILL.md` or
`GUIDE.md`, opened that one file; a directory with neither rendered nothing at
all. The files inside were a flat alphabetical list of labels in the sidebar.
There was no way to see `wiki/recipes/` as a table of cuisines and cook times,
or ADR 0011's deck as cards grouped by category with the undealt ones visible.

`GUIDE.md` was the only per-directory convention and it is prose for the
*agent* — "here is how to write a file in this directory". Nothing told the
renderer anything, and `bootstrap/skills/ingest/SKILL.md` asks the agent to
hand-maintain an `index.md` catalog in every source directory: a generated view,
written by hand, that goes stale the first turn that forgets.

Three facts, all measured against the real store during this change, decided
almost the whole design.

**Frontmatter is already parsed and structured in Postgres.** The backing table
carries `title`, `author`, `headers` (JSONB), `body` and `encoding` beside
`id`/`parent_id`/`filename`/`filetype`. `_PATHS_CTE` selected four of them and
nothing read the rest. So `holder: [brian, laura]` arrives as a JSON list, not
as a string to regex — there is **no YAML parser in this feature and no new
dependency**, which is the opposite of what the plan for it assumed.

**`headers` is full-replace on every write, and `body` is always replaced.**
Omitting a key removes it. A `view:` block living in `GUIDE.md`'s frontmatter —
or a fenced block in its body — would therefore be **silently deleted** by any
turn that rewrote that file's prose without repeating the block. That is the
same shape of data loss as `test_appending_to_a_kb_file_still_destroys_it`, and
it ruled out the two locations that otherwise looked most natural. It is also
the fact a plain reading of the design would have got wrong: putting config in
the file that already exists is the obvious move, and it is the one that loses
the config.

**`filetype` is `'directory'`, not `'dir'`.** The TigerFS reference documents
the latter. A children query filtering on the documented value matches nothing,
which renders as an empty workspace rather than as an error.

## Decision

**A directory may hold a `VIEW.md` whose frontmatter *is* a display spec.** It
controls the directory's generated index and the field header shown above each
page at that level.

Its own file, because that makes full-replace *correct* rather than dangerous:
the whole file is the spec, so rewriting it always rewrites the spec and there
is nothing to forget. Frontmatter rather than a fenced block or a `VIEW.json`,
because the store already parses it into `headers` and hands it back as data.
Nested under `view:` and `page:` keys rather than at the top level, because
`title`, `author` and `encoding` are routed to dedicated columns and a spec key
with one of those names would silently never arrive.

`GUIDE.md` is unchanged and keeps its job. Prose for the agent, config for the
renderer, one file each — so a turn revising the prose cannot break the
rendering.

**The spec is declarative, and this does not flip ADR 0016.** That ADR ends its
sanitizer section with *"This trade flips the moment raw HTML in wiki pages is
wanted as a feature."* It is not wanted. A spec names a layout from a fixed set
and a list of field names; `static/view.js` builds DOM nodes and assigns
`textContent`. No templates, no HTML, no expressions, and therefore no iframe
sandbox and no `postMessage` bridge.

`textContent` is not the whole defence, because the values are frontmatter an
agent may have written after reading a fetched page or an emailed document:

- A frontmatter **key** can be `__proto__`, `constructor` or `toString`, and an
  object-literal lookup returns a prototype or a function that `textContent`
  then prints. Every lookup built from untrusted keys goes through a `Map`.
- No `class`, `id` or `name` is ever derived from a group key or field name —
  `active`/`collapsed` collide with app.css, and an id collides with
  `getElementById('content'|'input'|'send')`.
- One `fmt()` bounds every value: 200 characters, C0/C1 controls and the bidi
  overrides stripped, nested objects dropped.
- **No field ever becomes an `href` or `src`.** The only link an index emits is
  built from the entry's own path. A field that could carry a URL is a phishing
  primitive in the pane the reader trusts most.
- `page.header` renders above the prose, so it is human-directed injection
  surface. The mitigation is shape, not escaping: always a labelled, muted chip,
  never a heading.

**There is no `filter`, and there should not be one.** A view may reorder and
group; it may never drop an entry. An index that omits files that exist makes a
liar of the one artifact whose value is "the wiki says what is there", and it
would be a hiding primitive an agent could write after ingesting a poisoned
page. `build_groups` sweeps anything it fails to place into the empty group
rather than losing it, and a test asserts the property directly.

**Sorting, grouping and normalising happen on the server.** All three pytest
tiers are Python and none executes the served JavaScript, so every decision
that can live on that side of the wire is one that gets tested by default. The
client paints. `app/kbview.py` is pure — dicts in, dataclasses out — which is
also what lets a browser check call `renderDirView` directly with hostile input
and assert on the DOM it built.

**Children come from a `parent_id` join, not a path prefix.** A
`LIKE $1 || '/%' AND path NOT LIKE $1 || '/%/%'` formulation works and is worse
three ways: `%` and `_` in a directory name are wildcards needing correct
escaping every time, it scans the materialised paths twice, and it silently
matches nothing at the workspace root where `path` is `''` and the pattern
degrades to `'/%'`. `_PATHS_CTE` also gained a depth bound — an unbounded
`WITH RECURSIVE` hangs on a cycle, and a directory endpoint makes that reachable
on an ordinary click rather than only at startup.

**Two routes, and the second one exists for a cost.** `GET /api/kb/dir` needs
no extra queries at all — `GUIDE.md` and `VIEW.md` are children of the
directory like any other file, so both arrive with the entries.
`GET /api/kb/spec` is the one-row subset, so that opening a *file* does not pay
for its directory's children. Bolting a `page` key onto `/api/kb/file` instead
would have put a second lookup on every tree click, which is the per-click cost
ADR 0016 split `_PATHS_CTE` to remove.

`/api/kb/file` did gain `fields`, and that is a wider select on a row it was
already fetching rather than a second lookup. It is load-bearing: without it a
file opened by **deep link** cannot tell "this value is empty" from "I was
never told", so it printed the spec's `empty_labels` — "Unfiled", "not timed
yet" — over data it simply did not have. Both halves looked correct on their
own; only a browser caught it.

**A bad spec never costs the reader the page.** `normalise` degrades to the
default view and returns a warning naming the problem, which renders in a box
above the entries. Nothing raises: the input is written by an agent and read by
someone who did not write it, and the person who can fix it is the one looking
at the page, not at a console.

**The vocabulary is taught through the image skill, not the bootstrap file.**
`_seed_tree` only replaces a bootstrap file whose *stored* hash still matches,
so any deployment where `wiki/GUIDE.md` was edited never receives new text, with
a log warning as the only trace. `skills/kb-curator/SKILL.md` ships in the image
and loads every turn; it points at a new `references/directory-views.md` holding
the schema. `bootstrap/wiki/GUIDE.md` and the `AGENT_GUIDE.md` starter are
updated too, but nothing depends on them arriving.

## Consequences

`bootstrap/wiki/recipes/` now ships — a `VIEW.md`, a `GUIDE.md` and two
recipes. It is the feature's first-boot demo and the fixture the browser checks
need, since nothing previously seeded carried frontmatter at all. It also means
a fresh KB is no longer empty, which is a change in what "bootstrap" means here.

The wiki's frontmatter rule from ADR 0011 — *frontmatter when code will read
it, prose when only people will* — now has a second consumer and a real risk of
over-application. A directory of free prose should not grow frontmatter because
it could; the reference document says so explicitly.

A skill directory keeps opening its `SKILL.md` rather than a generated index.
The folder is one document plus its references, not a collection. `kb-1m7`
covers that surface separately.

Three pre-existing bugs were fixed in passing because the refactor exposed them:
`loadFiles()` no longer chooses the centre pane (which is why the ↻ button used
to navigate you away from what you were reading, and why opening one file
reloaded the whole tree); `popstate` no longer pushes the entry it just popped;
and a directory header now always carries `dataset.path`, where it was set only
when there was a file to open — leaving exactly the guide-less directories that
now gain a view unable to ever show as active.

Two rapid navigations could already race in `openKbFile`; the directory fetch is
heavier, so it would have lost more often. Every pane render now takes a
sequence token before its first `await` and gives up if the token moved on.

## What was rejected

**A `view:` key in `GUIDE.md`, and a fenced ```` ```view ```` block in its
body.** Both lose the spec to full-replace on the first prose rewrite. This was
the plan of record until the store was measured.

**A `VIEW.json` parsed with stdlib `json`.** The fallback if `headers` had
mangled nested structure. It did not: a container test writes a spec with
nested maps and a list through the real mount and reads it back with `fields`
order intact.

**PyYAML.** Unnecessary once `headers` is read as a column, and
`requirements.txt` argues at length that every dependency be floored, capped
and re-verified. Worth noting that `evolve._fields` refuses to parse YAML for a
reason that does *not* transfer — it compares two documents for byte-shape
equality and a parse would hide the reformatting it exists to notice.

**A `cards` layout.** A third CSS surface and a third failure mode in a 720px
column; `table` and `list` cover both real cases.

**Omitting subdirectories from `/api/kb/dir`.** Rejected during the build,
having initially been the plan. The client already builds the full tree from
`/api/kb/files`, so listing folders again duplicates a fact it holds and opens a
window where tree and index disagree — but the alternative is worse, and a
browser check is what showed it: `wiki/` rendered "Nothing here yet." over a
`recipes/` visible in the tree beside it. That is the same lie as a filter,
reached by omission rather than by design, and the no-filter rule above has no
force if the index quietly drops folders instead of files.

## Note on verification

The measurement came first and was the gate: `test_a_view_spec_survives_the_store`
and `test_rewriting_a_neighbour_does_not_erase_the_spec` were written and run
against the real mount before any renderer existed — the same move ADR 0016 made
with `test_the_path_the_ui_would_open_is_the_path_the_api_accepts`. Both stay in
the suite as the regression that catches a TigerFS upgrade changing its mind, as
does `test_the_store_speaks_yaml_1_2_and_does_not_coerce`, which contradicted the
assumption it was written to confirm: `no` stays `"no"` and `1:30` stays
`"1:30"`, so the reference document warns about no coercion footguns.

`tests/test_kbview.py` covers normalisation and grouping in the fast tier;
`tests/test_container.py` covers the routes against a real TigerFS table,
including a directory named `dir_probe%odd`, which is what stops anyone
reintroducing the prefix query.

Because no pytest tier executes the served JavaScript, the renderer was checked
with the `browser-test` skill against the real stack. Its purity is what made
those checks possible without a fixture: `renderDirView` was called directly
with `<script>` in a title, `<img onerror>` in a group key, `__proto__` in both
a label map and a field map, a 50 000-character value, a bidi override, a nested
object, and a `../../etc/passwd` path. No element was created, no global was
polluted, values were bounded at 200 characters, and the traversal path rendered
as text with no link at all. The collapse-toggle regression from `1fe5cff` was
re-asserted on the new directory-click path, and the boot-order change was
checked by reading the pane before and after the tree finished loading.
