# wiki/

Human knowledge: notes, recipes, references, research — anything that is
content rather than an executable skill.

## Organizing

Group content by topic in a subdirectory (e.g. `wiki/recipes/`,
`wiki/research/`). When the human introduces a new content type, create the
subdirectory and write a `GUIDE.md` inside it describing the expected format,
then note the new subdirectory here.

Before writing into an existing subdirectory, check whether it already has a
`GUIDE.md` and follow its format. If it does not and you are creating
structured content, ask the human how they would like it formatted, then write
the `GUIDE.md` for future turns.

## How a subdirectory is displayed

A subdirectory can also hold a `VIEW.md`, whose frontmatter tells the wiki how
to render that folder as an index — a table of chosen fields, grouped and
sorted — and what to show above each page inside it. `wiki/recipes/` ships with
one as a worked example.

A view reads the frontmatter of the pages in its directory, so a folder that
has one needs its `GUIDE.md` to say which fields a new page should carry. Only
add frontmatter where a view is actually reading it: frontmatter when code will
read it, prose when only people will.

Giving a folder its first view usually means backfilling that frontmatter
across pages that do not have it yet. Ask for the `views` skill, which is the
procedure for doing that without leaving the folder half-classified.

When adding content, look for an existing page to extend before creating a new
one. Correct facts in place — the version history preserves what was there
before.
