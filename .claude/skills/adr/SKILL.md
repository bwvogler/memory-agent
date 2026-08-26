---
name: adr
description: >
  Write a new architecture decision record or amend an existing one, in this
  repo's exact template: a claim-shaped title, a Status line, Context/
  Decision/Consequences, an optional Note on verification, and append-only
  Amendments. Use when asked to write an ADR, document a decision, record why
  something was built a certain way, or when a design choice needs to survive
  past the PR that made it.
argument-hint: "[topic]"
---

# Write or Amend an ADR

`docs/decisions/` holds 16 ADRs and they follow one template closely enough
that deviating from it is more noticeable than following it. Read one before
writing — `docs/decisions/0016-one-page-three-panes.md` is a good recent
example, `docs/decisions/0009-one-machine-owns-the-ledger.md` shows the
Amendment convention.

## Decide first: new ADR, or amendment to an existing one?

If the topic is a consequence of, or was discovered while implementing, an
**existing** decision, amend that file instead of writing a new one — append
a `## Amendment: <a claim> (\`bead-id\`)` section naming the bead that
prompted it. Seven of the sixteen ADRs carry amendments this way, one (0015)
carries three.

**Never rewrite an existing Context/Decision/Consequences section after the
fact.** What the team believed at decision time is itself part of the record;
an amendment says what changed and why, without erasing what came before.

## The template (for a genuinely new decision)

```markdown
# NNNN — <a claim, not a topic>

**Status:** proposed

## Context

<What forced this decision. Name the constraint, not just the goal.>

## Decision

<What was decided, stated plainly. A bold lead sentence stating the core
claim is idiomatic here.>

## Consequences

<What this costs, what it rules out, what would change the calculus later.>

## Note on verification

<State plainly what was measured versus reasoned. Six of sixteen ADRs have
this section, and it's the one that most raises the bar — don't claim
something was observed if it was actually inferred from reading the code.>
```

- **Number:** `ls docs/decisions/` and take the next integer, zero-padded to
  four digits. Filename is `NNNN-kebab-case-of-the-title.md`.
- **Title is a claim**, not a label: "The human is a tool the agent can call",
  not "Human interaction model". Em dash (`—`) between number and title, not a
  hyphen.
- **`**Status:**` is `proposed` or `accepted`** — nothing else. No date, no
  author, no "supersedes" field, no YAML frontmatter.
- `## Context` → `## Decision` → `## Consequences` is present in essentially
  every ADR, in that order. Everything else (`## Rejected`, `## References`,
  `## The escape hatch, and its price`, etc.) is freeform and named for what it
  actually argues, not a generic label.

## Style

- Hard-wrap ~78–80 columns.
- Bold lead sentences that state a claim outright:
  `**Each split fails silently, and one of them is dangerous.**`
- Cross-reference precisely: `(ADR 0006)`, `` `app/kb.py` ``,
  `` `turns.Registry.begin` ``, bead ids in backticks (`` `img-lsp` ``,
  `` `kb-b82` ``).
- Narrate failure modes as observed events, and name the symptom that made
  them look like something else: "which reads like a broken container rather
  than a port collision."
- Numbered, force-ranked arguments are fine when there's more than one reason:
  state which one is actually decisive.

## Steps

1. `ls docs/decisions/` — find the next number, or the existing file to amend.
2. Draft per the template (or the Amendment heading) above.
3. Update the matching paragraph in CLAUDE.md's "Key design decisions" — a new
   or amended ADR almost always ships with one; check whether one already
   exists before adding a redundant paragraph.
4. Commit the ADR, the code it describes, and the CLAUDE.md paragraph
   together — 16 of 20 ADR-touching commits do this as one atomic commit, not
   three.
