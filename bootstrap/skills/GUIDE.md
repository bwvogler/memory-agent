# skills/

Reusable agent skills: each is a subdirectory containing a `SKILL.md` that the
human invokes explicitly by name. Skills are not loaded automatically the way
`kb-curator` is — they exist so the human can ask for one by name.

## Adding a skill

Create a subdirectory under `skills/` and write a `SKILL.md` inside it
describing what the skill does and how to invoke it. A skill may keep
supporting reference material in a `references/` subdirectory next to its
`SKILL.md`.

Skills that ship with the image (`ingest`, `lint`, `reflect`, `kb-curator`) can
carry a `LEARNED.md` overlay recording lessons picked up over time — that file
is data, not a second skill, and has no frontmatter of its own.
