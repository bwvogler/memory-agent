---
name: reflect
description: >
  Review the signal beads recording turns that were reverted, errored or ran
  out of budget, decide whether a skill was actually at fault, and if so make
  one bounded improvement to it. Use when asked to reflect, to groom the
  skills, to learn from mistakes, or to work out why something keeps going
  wrong. Triggers include "reflect on that", "why does this keep happening",
  "improve your skills", "what have you learned", and "review the signals".
---

# Reflecting on your own skills

Skills are guidance, and guidance can be wrong. This skill is how a skill gets
better from what actually happened rather than from what someone imagined would
happen when they wrote it.

The remit is deliberately small, and it is enforced in code rather than by this
document: you may rewrite a skill's `description` and append under a
`## Learned` heading. Everything else is refused by a hook. Read
`docs/decisions/0008` if you want the reasoning; the short version is that a
reflection turn which misreads its remit damages every later turn, so the
remit is not left to judgement.

## Read the evidence first

```bash
bd list --label signal --json
```

Each signal bead carries the prompt, the skills that turn read, and what was
rolled back. Read them together, not one at a time — a single revert is a noisy
label, and the pattern across several is worth far more than any one of them.

Read any count against how many turns used the skill at all. A skill that loads
on every turn appears in every failure by construction, so a raw count is a
popularity contest and a rate is evidence. `GET /api/signals` returns those
rates for a human reading this; an automatic reflection turn is handed them in
its prompt, because it has no tool that can make an HTTP request.

## Decide whether a skill was at fault

This is the hard part and the part worth being slow about. A turn can go wrong
for reasons that have nothing to do with guidance:

- the human changed their mind, or asked for the wrong thing
- the model made an ordinary mistake it had all the information to avoid
- the infrastructure failed
- the task was genuinely ambiguous

**"No skill change is warranted" is a correct and common conclusion.** Say it
and stop. A reflection loop that always finds something to change will drift
the skills toward whatever the last accident happened to be, and each step will
look locally reasonable. Changing nothing is how that is avoided.

Ask specifically: *if the skill had said something different, would this turn
have gone differently?* If you cannot answer yes concretely, the answer is no.

## Check what has already been rejected

A bead titled `REJECTED self-edit:` records a change that was made and then
reverted by a human. That is a direct instruction not to make it again.

Do not reword it slightly and try once more. If the evidence still looks
actionable, the change must be materially different — or the honest answer is
that this evidence is not something you can act on.

## Make one bounded change

Pick **one** skill. Write the whole file with the Write tool; `Edit` is
unavailable here, and whole-file writes are the safe pattern on this mount
anyway.

**Rewrite the `description` when the skill did not trigger.** The description
is the only routing signal that exists before a skill loads, so an
under-triggering skill is broken there and nowhere else. Write triggers as
concrete surface forms — the words a human would actually type — not as
abstract capability.

**Append under `## Learned` when the skill triggered but misled.** Add the
heading at the end of the file if it does not exist yet. One entry, dated,
naming what happened and what to do instead:

```markdown
## Learned

- 2026-08-13: A turn that appended to a KB file destroyed it. Signal kb-abc.
  Always write the whole file, even for a one-line addition.
```

Keep entries short. This section loads with the skill every time it is used, so
a page of accumulated caution costs more than it is worth.

**You cannot remove entries, and you do not need to.** Every append files or
escalates a `consolidate` bead asking an ordinary turn to fold the section back
into the skill body and drop what has been absorbed. That job needs to rewrite
the body, which is exactly the power you do not have.

**To revise something already recorded, append an entry that supersedes it.**
A later entry beats an earlier one, so a changed preference is expressed by
adding, never by editing:

```markdown
- 2026-09-02: Use imperial for oven temperatures. Supersedes the 2026-08-13
  entry, for temperatures only.
```

Preferences change; guidance corrections usually do not. Saying which entry is
being superseded keeps the contradiction visible instead of silently resolved.

## Finish the loop

Close or note the beads you acted on, so the next reflection does not reach the
same conclusion from the same evidence:

```bash
bd close <id> --reason="rewrote the description of <skill>; see evolution.md"
```

File a bead for anything you noticed but could not fix inside the remit. A
problem too deep for a description tweak is exactly what a human should see,
and surfacing one is a successful reflection, not a failed one.

You do not need to write `memory/evolution.md`. The application writes it from
what the guard actually allowed, so the log records what happened rather than
what you intended.
