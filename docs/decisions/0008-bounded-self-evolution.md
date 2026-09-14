# 0008 — Let the agent rewrite its own skills, within a remit it cannot exceed

**Status:** accepted

## Context

Skills are guidance written by someone imagining how a turn would go. Turns then
go differently. Without a path from what happened back into the guidance, every
skill stays as good as its author's first guess forever, and the same failure
recurs until a human happens to notice and edit the file.

Three things already in this system make closing that loop unusually cheap.
TigerFS versions every write, so a bad edit is one `undo_to_savepoint` away.
The Revert button (ADR 0003) is an explicit, human-labelled "that was wrong"
bound to an exact turn — a supervision signal most agent systems do not have.
And beads (ADR 0006) is already the place work and evidence live, so reflection
needs no new schema: signal beads in, bounded edit out, rejected proposals
recorded back onto the same graph.

**The gate was answered "no", and we built it anyway.** Bead `kb-3sv` said to
run Stage 2 for several weeks and look at real signals first, and explicitly
allowed "not enough signal" as a successful outcome that closes Stage 3. At the
time of writing the ledger held 6 turns, 0 reverts and 0 errors — all of them
from tests. The user read that and chose to proceed. Recorded plainly because
the gate was a good gate and the reasoning behind it has not been refuted, only
overridden: nothing below has been validated against real usage, and the first
weeks of it running are still the experiment `kb-3sv` asked for.

One design consequence follows directly, and it is not cosmetic. Signal-gated
reflection alone would have been dead code on arrival: with zero signals it
could never fire, and we would have shipped unexercised self-modification —
the worst possible thing to deploy untested. So reflection is also manually
triggerable, which is how every claim in this document was actually verified.

## Decision

A reflection turn may do exactly two things to one skill: rewrite its
`description` frontmatter field, and append entries under a `## Learned`
heading. Nothing else. `app/evolve.py` holds the whole policy.

**The remit is enforced by a hook, not by instructions.** ADR 0007 records two
rules that were written into the prompt, agreed to by the model, and broken
anyway. Self-modification is the worst place to rely on a promise, because a
reflection turn that misreads its remit damages every later turn silently. So a
`PreToolUse` hook compares the file on disk against the content about to be
written and refuses anything outside the remit. `bounded_skill_edit` is a pure
function, which means the entire policy is arguable in a unit test rather than
only observable in production.

**Why those two fields.** The description is the only routing signal that
exists before a skill loads, so an under-triggering skill is broken there and
nowhere else — high value, and the change is a few lines. An append-only
section is monotonic: it reads as a diff, a human can trim it, and it cannot
destroy what it is adding to. Both are recoverable by construction.

**What is out of reach, and why each one.** Skills shipped in the image are
code — reviewed and deployed atomically, and an edit there would also silently
vanish at the next deploy, which is worse than being refused. (Their *lessons*
are reachable — see the amendment below.) `AGENT_GUIDE.md` is the human's own
schema document. New skills are a human's decision, so
reflection improves what exists and files a bead when something is missing.
`Edit` is refused outright: whole-file writes are the safe pattern on this
mount anyway (ADR 0007), and they let the bound see the complete proposed
document rather than a fragment it would have to apply itself to judge.

**Reflection is an ordinary Turn.** It appears in the registry, streams to the
UI, gets a savepoint, and is revertable by the same button as anything else.
That is what makes the whole thing defensible: a self-edit a human dislikes is
one click from gone, through a path that already existed and is already trusted.

**Reverting a self-edit files a rejection.** Without it the loop oscillates —
reflection reads the same evidence, reaches the same conclusion, makes the same
edit, is reverted again, forever, and each cycle looks locally reasonable. The
rejection is filed as a `signal`-labelled bead so it arrives in the same
`bd list --label signal` that reflection already reads. A rejection nobody
looks at is not immune memory, it is just a record.

**The evolution log is written by the application, not the agent.** The failure
mode to design against is not a bad edit; it is a bad edit nobody noticed. A log
the agent writes is a log the agent can forget to write, so `memory/evolution.md`
is built from what the guard actually allowed — recording what happened rather
than what was intended — and renders in `/kb` for free.

## Consequences

The skill library can now improve from deployment experience with no retraining
path and no human in the loop. The cost is one extra agent turn per *signalling*
turn, not per turn, and reflection is skipped entirely whenever anything else is
running — the 2GB suspend ceiling is real and the user's turn always wins.

**Reflection can only ever make small changes, and that is the point.** A skill
needing more than a description tweak produces a bead for a human instead. If
that turns out to be most of them, the honest conclusion is that this loop is
not earning its keep, and the evolution log is exactly where that will be
visible.

**"No change is warranted" is a correct outcome and is stated as such** in the
reflection prompt, the skill, and this document. A reflection loop that always
finds something to change drifts the skills toward whatever the last accident
happened to be, one locally-reasonable step at a time. Observed already: given
evidence it had previously acted on, a reflection turn correctly did nothing.

**Rejected alternative: shadow evolution.** Fork a skill, route a fraction of
traffic, promote on measured win. It is the more rigorous design and it fails
here on arithmetic — a personal wiki produces tens of turns a week, so
distinguishing a better skill from noise would take months. Worth revisiting if
this ever becomes multi-tenant and busy; not before. Recorded because a future
reader with more traffic will need the reasoning, not the conclusion.

**Eval-gating is not built.** `kb-56n` proposes replaying the turn that produced
a signal against the candidate skill. Until then the only gate is a human
noticing and clicking Revert, which is exactly as strong as how closely the
evolution log is read. This is the largest gap in the design as shipped.

**Two rough edges found while building, both filed rather than papered over.**
A reflection turn often writes the same file twice — refining wording, or
rewriting after the store's markdown re-serialisation makes the file read back
differently — so log entries are collapsed to their net effect per skill. And a
reflection turn that reaches for shell it does not have still files a P1
"check allowed_tools" bead, because the *real* permission system denied it; our
own guards' refusals are already excluded, but that case is not.

## Amendment — `## Learned` has to be prunable, and it has to be reachable

Two things the original decision did not settle, both of them consequences of
append-only rather than departures from it. Recorded here rather than as a new
ADR because neither changes the remit, the hook-not-instructions argument, or
reversibility by savepoint; what changes is the reach of what is above, and
"what is out of reach" is where a reader will look for it.

**An append-only section grows forever, and it is charged on every turn that
loads the skill.** Reflection cannot prune it: deciding which lessons have been
absorbed into the body means rewriting the body, which is exactly the power the
remit withholds. So pruning is ordinary work, and the way work reaches an
ordinary turn here is a bead. Every append files or escalates one
`consolidate`-labelled bead per skill — escalating rather than multiplying,
because one skill accumulating five lessons is one job that got more urgent, not
five jobs.

**Preferences are routed by scope, for the same cost reason.** `memory/CLAUDE.md`
is injected into the system prompt on every turn, so a preference that only
matters while ingesting documents is overpriced there and correctly priced in
the `ingest` skill's `## Learned`. The test is one question: *would I want this
to apply while doing something else?*

**Which left the rule unfollowable for the skill it mattered most for.**
`kb-curator` ships in the image. Curation-scoped lessons therefore had exactly
one home — the system prompt, on every turn — which is the cost the routing rule
exists to avoid (`kb-5uu`). So an image skill now keeps a `LEARNED.md` overlay
in the knowledge base at `memory/skills/<skill>/LEARNED.md`, seeded as a stub by
the same bootstrap path as any other file, and the skill's own body points at it.

The overlay is deliberately not a skill: no frontmatter, no description, nothing
the router sees. A shadow *skill* was the obvious alternative and needs no code
at all, since the remit already covers knowledge-base skills — it was rejected
because it would put a second skill in front of the router competing with
`kb-curator` on the same triggers, and because a mutable `description` is
meaningless on a companion file. The skill's text stays code; only what was
learned about it becomes data.

Reflection may append to an overlay under the same append-only bound
(`bounded_overlay_edit` shares its core with `bounded_skill_edit`, so the promise
is defined once). Ordinary turns rewrite it freely, which is what makes it
prunable, and is also where human-stated curation preferences now land — the
half that actually shrinks the prompt, since reflection fires rarely and
ordinary turns are where preferences get stated.

Its consolidation bead is a different job and says so in a different title:
there is no body to fold into, so the ask is to prune what a later entry
superseded, and to file a proposed change to `skills/<skill>/SKILL.md` in the
repository for anything durable enough to belong in the image. That is the only
route by which a lesson learned in production reaches the shipped skill, and it
goes through a human by construction.

**No size cap.** A hard limit was considered and rejected: a refused lesson is a
lost lesson, and refusing one would also break the rule from ADR 0007 that every
refusal names the safe alternative. The escalating bead is the pressure instead.

## Note on verification

The unit tier weights the allowances as heavily as the refusals. A remit so
tight that nothing useful fits produces a loop that can only fail, which teaches
nothing and burns a turn every time a signal arrives.

`--live` runs the blast-radius test against a real model: plant a deliberately
useless description, file a signal, run reflection, and assert what must hold
regardless of what the model decides — the body untouched, the identity
untouched, `AGENT_GUIDE.md` untouched, the image skill untouched. Whether it
chooses to rewrite the description is judgment, and asserting on judgment would
flake.

Everything else here was verified by running it: a real reflection rewrote a
useless description into a trigger-rich one, appended a dated `## Learned`
entry, left the body byte-identical, wrote the evolution log; a Revert restored
the original and filed `REJECTED self-edit: probe: rewrote description`.

The first budget tried was 12 turns and a real reflection spent all of it
reading evidence without reaching the edit. It is 30. The reflection prompt also
told the agent to `GET /api/signals`, which it has no tool to call — an
instruction the agent cannot follow does not merely fail, it burns turns being
retried, so the application now injects those numbers into the prompt instead.
Both were found only by running it against a real model, which is the argument
for the manual trigger in one paragraph.

## Amendment — a finding outside the remit has to leave the turn

**Status:** accepted, 2026-09-11

The remit answers "may reflection make this change?" and nothing else. When the
answer is no, the denial text has always said to file a bead instead — and
nothing ever checked that one was. That is precisely the shape ADR 0007 was
written about: a rule stated in prose, agreed to, and then not followed. It
took longer to notice here because the failure is silent rather than
destructive.

It was found by reading a transcript. A production reflection turn reviewed 24
signal beads, identified a real recurring pattern — the agent shelling out with
`cat`/`cp` instead of using `Write` for knowledge-base files — observed
correctly that no skill had been loaded on any of those turns, and concluded
that skills therefore could not fix it and no action was warranted. Every step
is true. The conclusion is still wrong, because the fix was never skill-shaped:
it belongs in `AGENT_GUIDE.md`, or in the universal system prompt in
`app/agent.py`, both of which load on every turn regardless of routing and
neither of which reflection can reach. The finding was correct, unavailable to
anyone else in the system, and lost when the turn ended.

**"No skill change is warranted" and "no skill *can* be at fault, because none
was loaded" are different findings wearing the same sentence.** The first is a
correct and common ending, and the original decision was right to bless it. The
second names a gap in the always-on guidance and points somewhere specific. The
prompt could not tell them apart because it never asked reflection to look at
whether a pattern's turns had read any skill at all.

Three changes, layered so each stands on the last.

**The evidence learns to say which it is.** Skill attribution counts a skill
only when a turn actually read it, which is what makes the per-skill rates
meaningful and is not being changed. Its cost is that a turn which read nothing
joins no row in `skill_signal_summary` and is invisible in every per-skill rate.
Such signal beads now carry a `no-skill-context` label, applied in
`signals._file_signal` for every signal type rather than at four call sites, and
`store.no_skill_totals()` buckets those turns by outcome for both
`GET /api/signals` and the evidence block injected into the reflection prompt.
This is the cheap half and would have been worth doing alone: the distinction
existed in the data and nothing surfaced it.

**The prompt learns to ask.** `REFLECTION_PROMPT` gains a step between
"decide whether a skill was at fault" and "check what was rejected": if every
occurrence of a pattern read no skill, say where the fix belongs —
`AGENT_GUIDE.md`, the system prompt, or nowhere — and file it as a `guide-gap`
bead with the exact wording you would add. `bootstrap/skills/reflect/SKILL.md`
mirrors it for the human-triggered path, which reads a different file.

**And the code stops depending on either.** A prompt is advice; this ADR's own
opening argument is that self-modification is the worst place to rely on one. So
`guide-gap` beads are filed by the application, in the two shapes the failure
actually takes:

- `evolve.write_guard_for` files one when a reflection turn aims a `Write`
  outside the skill tree entirely, carrying the content it was about to write.
  The proposal is the valuable part — it is the exact wording a human would
  apply, and it exists only in that refused tool call. Dedupe-by-title and
  escalate-on-repeat are copied from `request_consolidation`, for the reason
  recorded there: one recurring gap is one job that got more urgent, not five
  jobs. An image skill's own `SKILL.md` is excluded, because that refusal names
  the overlay and the overlay is inside the remit — filing there would be noise
  about a case with a correct local answer.
- `guards.reflection_stop_guard` covers the case that actually happened, where
  reflection never attempted a write at all. A deny-path fix alone would have
  caught nothing here.

**The Stop hook is the part that needs justifying, because this ADR's
implementation deliberately installed none.** "Reflection has no 'defer the
work' failure mode" is still true, and the ordinary deferred-work guard is still
not installed on a reflection turn. This one is narrower in three ways, each
chosen to keep it from becoming the same unnoticed-dismissal failure in a new
place. It arms only when `signals.no_skill_failures()` is non-zero — counted
before the turn starts, from the same ledger the prompt is built from, so the
guard and the evidence the agent was shown can never disagree; a healthy
deployment never sees it. It blocks at most once, via `stop_hook_active`, like
the guard it is modelled on. And its message says explicitly that "this is
nobody's guidance to fix" is an acceptable answer — it has to be *said*, not
arrived at silently. A guard with no way past it teaches a model to file junk to
get through, which is ADR 0007's "always explain the safe alternative" applied
to an ending rather than to a tool call.

**`guide-gap` is a reporting escalation, not a new capability.** Nothing about
the remit moves: reflection still cannot write `AGENT_GUIDE.md`, still cannot
touch an image skill, still cannot reach `app/`. What it can now do is say, in
the ledger rather than in a transcript, that the fix belongs somewhere it cannot
go. The beads are filed `deferred` for the same reason signal beads are — this
is a finding for a human to judge, not a job for the next session to claim off
`bd ready`.

**Deliberately not done: closing the loop on perpetual review.** Those 24 signal
beads had been reviewed by several reflection turns and re-derived from scratch
each time, and an obvious next step is a review counter that escalates after N
passes with no result. It is not built, because counting reliably means parsing
`bd note` history and nothing confirms that is cheap on the pinned `bd` surface.
It is also plausibly smaller than it looks now: the reason those cycles produced
nothing was that the only available conclusion was "no skill fix", and that is
no longer a dead end.

### Note on verification

Both new mechanisms are unit-tested on the pure parts — which target counts as a
gap, which transcript shapes satisfy the Stop guard, what the escalation does on
a second occurrence — and the container tier exercises the guards against the
real stack, where the `bd` calls are real. Two properties are pinned
specifically because a mutation passing every other test would break them: an
unreachable ledger must count zero no-skill failures (a broken store has to
relax the guard, never wedge every reflection turn behind a bead it has no
evidence to write), and a bead filed by the evolution guard must satisfy the
Stop guard, since that filing happens in a subprocess and cannot appear in the
transcript the guard reads.
