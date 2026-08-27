# skills/

Reusable agent skills: each is a subdirectory containing a `SKILL.md` that the
human invokes explicitly by name. Skills are not loaded automatically the way
`kb-curator` is — they exist so the human can ask for one by name.

## Adding a skill

Create a subdirectory under `skills/` and write a `SKILL.md` inside it
describing what the skill does and how to invoke it. A skill may keep
supporting reference material in a `references/` subdirectory next to its
`SKILL.md`.

Four conventions, each for a reason that is not obvious from the file:

**Exactly two frontmatter keys: `name` and `description`, and nothing else.**
The guard that lets a reflection turn improve a skill refuses any frontmatter
key added, removed or reordered, so a third key is permanent — it locks the
skill out of its own evolution, and the refusal happens months later, somewhere
else, attributed to the reflection rather than to the skill. Write them `name`
then `description` for consistency, but do not read anything into the order you
see in a stored file: the store sorts the keys, so what ships one way is stored
the other.

**The description is the whole of the routing.** It is what decides whether the
skill is reached at all, so it says what the skill does, when to use it, and
then the phrases a human would actually type. Follow the shape the existing
ones use: purpose, then "Use when …", then `Triggers include "…", "…"`.

**Keep `SKILL.md` short and put the detail in `references/`.** The body is read
in full every time the skill fires; a reference file costs nothing until the
step that needs it. Split by *phase* rather than by topic, name the reference
at the step that reads it, and keep every reference one hop from `SKILL.md` —
a file reached through a second hop tends to be read only in part. Give a
reference of any length a `## Contents` list.

**A skill here cannot ship a script.** Only `bd` is pre-approved, so anything
else a skill runs stops to ask a human for permission — every time, and a turn
with nobody watching is refused rather than asked. Write checks the skill can
perform by reading and grepping instead.

Skills that ship with the image (`ingest`, `lint`, `reflect`, `views`, and
`kb-curator`, which is the one loaded on every turn) can carry a `LEARNED.md`
overlay recording lessons picked up over time — that file is data, not a second
skill, and has no frontmatter of its own. A skill that lives here rather than
in the image does not need one: reflection can append to its `SKILL.md`
directly.
