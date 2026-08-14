# 0011 — A Fair Play card is a capability contract, and the deck is the spine

**Status:** proposed

## Context

The wiki has one content type. `memory/wiki/recipes/` holds a soup recipe and a
`GUIDE.md` describing how to write another one, and that is the whole of the
human knowledge in the knowledge base. Everything else built so far —
savepoints, beads, signals, bounded self-evolution — is machinery waiting for
something to be about.

The household runs a Fair Play deck. Eve Rodsky's system splits domestic labour
into named cards, each held by one person who owns the whole of it, against a
written **Minimum Standard of Care**: the agreed definition of done. It lives in
a spreadsheet of 105 rows, and in an Airtable base whose collaborator list
contains exactly one person.

The spreadsheet says this, today:

| | |
|---|---|
| Holder | Laura 37, Brian 31, shared 6, **unassigned 31** |
| Daily Grind (the cards done every day) | 32, split 13 / 11 / 5 / 3 |
| Categories | Home 26, Out 23, Caregiving 22, Magic 22, Wild 10, Unicorn 2 |
| **MSC** | **blank on all 105 rows** |

The blank column is the finding. Fair Play's entire mechanism is the MSC — the
part that converts "you never do the dishes properly" into a sentence both
people agreed to in advance. A spreadsheet cell is a bad place to write a
paragraph, so nobody wrote one, so the deck records an allocation and settles
nothing.

The ask is not to file the deck in the wiki. It is for the agent to **facilitate
the cards** — build skills for them, acquire the integrations they need, watch
the balance between two people, and take load off a family rather than
describing it.

## Decision

**A card is a capability contract.** Fair Play's own decomposition is already an
agent-capability decomposition, and that is the whole idea:

| Fair Play | What it means | What this system has |
|---|---|---|
| **Conception** | noticing it needs doing | nothing — there is no scheduler in `app/` |
| **Planning** | deciding when, and in what order | beads (ADR 0006), which works |
| **Execution** | doing the thing | skills exist; integrations do not |

So a card page grows three things over its life: the **holder**, the **MSC**,
and a **"how the agent helps"** block naming the skill that serves the card and
the integrations it would need. The last of those is the point. It turns the
deck into *demand data*: `kb-068` (MCP management) is currently blocked and
entirely speculative, and 105 cards each stating what they need is the
requirements document it is missing. Calendar Keeper, Weekend Plans, School
Breaks and Birthdays all want `kb-b82`; School Forms and Teacher Communication
want mail.

**Cards carry YAML frontmatter, and that is a deliberate exception.**

```yaml
---
card: Meals — Weekday Dinner
category: home          # home | out | caregiving | magic | wild | unicorn
holder: [brian, laura]  # [] means undealt, and that is a finding, not a gap
daily_grind: true
msc_agreed: false
skills: []
needs: [google-calendar]
---
```

`wiki/recipes/GUIDE.md` sets a no-frontmatter convention and this breaks it.
The reason is that something will read these files across all 105 of them, and
a regex over `- **Holder:** Brian` is one agent edit away from silently dropping
a card from the balance counts — a wrong denominator that looks like a working
report. The exception is documented in `wiki/fair-play/GUIDE.md` so the next
turn knows it is deliberate rather than drift.

**Skills are earned, not generated.** One skill per card would be 105
descriptions competing in the router, which does not make the agent good at 105
things; it makes it bad at choosing. A skill exists when several cards, or one
genuinely heavy card, justify it.

**The deck becomes central through one line, not a new skill.** `kb-curator`
loads on every turn and already carries an editable overlay at
`memory/skills/kb-curator/LEARNED.md` (ADR 0008). A line there pointing at
`wiki/fair-play/` is the entire mechanism by which cards become the thing every
turn consults. Nothing else is needed, and anything else would compete with the
curator for the same attention.

**Staged, because most of it does not exist yet.**

- **Stage 0** — the ledger holds a household, not a user. Blocks everything the
  second person touches. See ADR 0012.
- **Stage 1** — the deck exists: `wiki/fair-play/`, a `GUIDE.md`, 105 cards, an
  index, the `AGENT_GUIDE.md` layout line, the curator overlay line.
- **Stage 2** — the MSCs, written by interviewing the holder one card at a time.
  Daily Grind first, then the 31 nobody holds.
- **Stage 3** — a load-balance report, over `kb.sql_list_files()` and
  `kb.sql_read_file()`, which already exist.
- **Stage 4** — Conception: a scheduled ritual turn, reusing the `Turn`
  machinery so it savepoints and files beads like any other turn.
- **Stage 5** — integrations, in the order the cards asked for them.

## Consequences

**The deck lands in five turns, not one, and the reason is `allowed_tools`.**
`_options` grants `Bash(bd:*)` and nothing else, so the agent cannot run a
script, a loop, or `cp`. 105 cards is 105 `Write` calls against `max_turns=30`.
Batching by category is not a preference; it is the only shape that fits, and it
has the accidental virtue of giving each category its own savepoint to revert to.

**The load-balance metric is a proxy, and saying otherwise would be a lie.**
Fair Play's own premise is that a card's weight is invisible in a count —
Garbage and Middle of the Night Comfort are one row each. Three readings are
defensible: the **Daily Grind split**, because frequency is the closest thing to
weight the data has; the count of **cards with no agreed MSC**, each of which is
a live disagreement waiting to happen; and the **31 undealt cards**. That last
one is likely the most useful thing the deck will ever surface and it is
available on day one. Nobody holds Holidays, Pets, Death, Marriage and Romance,
or Hard Questions. Unheld cards do not go away — they land on whoever notices
first, which is the invisible labour the system exists to make visible.

**A report about how two married people divide labour is not a neutral
artifact.** Anything the agent renders here can be read as a scorecard, and a
scorecard is a good way to have a worse marriage. The metric should describe
cards and standards, not people, and the deck should never be summarised as a
number with two names next to it. This is a design constraint on Stage 3, not a
caveat about it.

**The wiki now has two conventions, and the second one is load-bearing.**
Recipes are prose; cards are prose with a machine-readable head. Every future
content type has to choose, and the honest rule is: frontmatter when code will
read it, prose when only people will.

**Undealt is data, not absence.** `holder: []` is a claim the deck makes, and
Stage 3 counts it. Filling those 31 in with a guess to make the file look
finished would destroy the most interesting thing in the dataset.

**The Airtable base is superseded but not closed.** Provenance goes in
`GUIDE.md` with the export date. Retiring the base is a decision for the humans,
and nothing here forces it.

## What this does not do

It does not schedule anything, integrate anything, or write a single MSC. Stages
2 through 5 are the work; Stage 1 is a directory of 105 mostly-empty files whose
only immediate value is the three numbers in the Consequences above. That is
worth saying plainly, because a deck that stops at Stage 1 is a spreadsheet with
extra steps.
