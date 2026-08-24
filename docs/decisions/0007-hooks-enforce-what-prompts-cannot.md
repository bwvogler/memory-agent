# 0007 — Enforce the two load-bearing rules with hooks, not prompts

**Status:** accepted

## Context

Two rules in this system are load-bearing in the sense that breaking either
destroys the thing the system exists to provide. Both were written into the
system prompt. Both were then broken by the agent, in production, on ordinary
turns.

**The first cost a user their memory file.** The KB mount has no
read-modify-write: opening a file yields a zero-filled buffer, and on close
that buffer becomes the whole file. Anything not written in that session is
gone — zeroed before the write, truncated after it. The prompt said to write
files whole. An agent hit a `Write verification failed` message (itself a false
alarm — see below), concluded the file tools were broken, reasoned its way to a
shell append as the workaround, and turned 233 bytes of personal notes into 233
zeroes. The write reported success.

**The second quietly emptied the ledger.** ADR 0006 exists so that work the
agent notices survives the turn that noticed it. Asked to write a page and told
that a missing `GUIDE.md` was follow-up work, the agent wrote the page, replied
"noted as a follow-up for a later session", and filed nothing. Named,
acknowledged, lost. The instruction to file it was present — bullet three of
`_BEADS_OVERRIDES`, after a long `bd prime` block, phrased as a clarification.

These have a common shape, and it is the reason this ADR is separate from 0006.
Both are rules the model *agrees with*. Neither failure is disobedience or a
missing instruction, so neither is fixed by stating the instruction again. The
first came from a model under pressure inventing a workaround; the second from
an instruction that was true, agreed to, and simply not acted on. A prompt
cannot distinguish "I have told you" from "you did it".

One correction worth recording, because it delayed this by a week. 0006 notes
that bd's `.claude/settings.json` `SessionStart` hook never fires under
`setting_sources=[]`, and that was over-generalised into "hooks are unavailable
to us". It is true only of *file-based* hooks. `ClaudeAgentOptions` takes
Python callables directly, and `setting_sources` has no bearing on them. The
structural defence was available the whole time.

## Decision

Pass two hooks to the SDK from `app/guards.py`.

**`PreToolUse`, matched to Bash**, denies commands that would corrupt a KB
file: `>>`, `tee -a`, `sed -i`, `dd seek=`, `truncate`, `patch`, and append-mode
`open()`. `>` is deliberately absent — a truncating redirect writes the whole
file from offset 0, which is the *safe* pattern, and blocking it would remove
the escape hatch the denial message recommends.

**`Stop`** reads the transcript, scans assistant text since the last real user
message for deferral language, and blocks if no `bd` command ran alongside it.
The model gets its own words handed back while the context is still warm and
filing costs one tool call.

Three rules govern both, each learned from the incidents above.

*Scope narrowly.* The write guard fires only on commands naming `$KB_MOUNT`.
Scratch is an ordinary filesystem where every one of those commands is correct
and useful, and a guard that blocked legitimate work would manufacture exactly
the pressure that produced the original workaround.

*Always explain, and always leave a way out.* A bare refusal is what sent the
last agent hunting for another shell command. Each denial names the safe
pattern instead: the write guard explains whole-file writes and the
scratch-then-`cp` staging route; the stop guard gives the `bd create` invocation
and an explicit out for when there is genuinely nothing durable to file. Without
that second half a guard just gets fed junk beads to satisfy it.

*Fail open.* Both are wrapped so that any exception allows the turn to
continue. The stop guard also blocks at most once per turn, keyed on
`stop_hook_active`: a guard that could fire on its own re-prompt would loop to
`max_turns`.

The prompt keeps saying both things — the hooks are a backstop, not a
replacement. The ledger rule was also moved out of the overrides into its own
end-of-turn obligation, because an instruction worth enforcing is worth stating
properly first.

We also fixed the false alarm that started the first incident, since a guard
against the workaround does not help if the agent still believes its tools are
broken. TigerFS appends a missing trailing newline; the Write tool compares
bytes-on-disk against bytes-written and reports a failure that did not happen.
The prompt now says to end KB files with a newline, and to re-read rather than
believe a file-tool failure. The original "it fails on an internal `fchmod`
step" diagnosis was a guess and was wrong — `chmod`, `utime`, same-dir rename,
`O_TRUNC` and `fsync` all work; only cross-device rename, hard links and
symlinks fail.

## Consequences

Behaviour is now enforced in three places with different properties, and it is
worth being explicit about which is which. The system prompt is advice. Skills
are advice the agent may not even load. Hooks are the only layer that holds when
the model is wrong, and they live in the image rather than in the KB — so unlike
a skill, the agent cannot edit them, and unlike `.claude/settings.json`, it
cannot author new ones in its writable cwd. That last point is what makes them
safe to rely on under `permission_mode="acceptEdits"`.

The stop guard is a heuristic over prose and will sometimes be wrong. A false
positive costs one turn and possibly one unnecessary bead; a false negative is
the status quo. The asymmetry is deliberate, and the regex is tuned to the
quiet end of it — bare "later" does not trigger, because "a later version" and
"later in the file" are ordinary English and a guard that fires on turns with
nothing to file trains the model to route around it.

Reading the transcript couples us to a CLI-internal JSONL format. This is the
weakest part of the design and is why the guard fails open on a parse error: if
the format drifts we lose the backstop and keep the prompt, which is where we
were before. The alternative — inspecting our own `Turn` buffer — races, since
the hook fires from the transport while text may still be sitting in the
message queue unconsumed.

The `>` allowance means the write guard is a speed bump, not a wall. A
determined agent can still write a corrupt file through a permitted command,
and pattern-matching shell text is not parsing it. The durable defence is a
post-write NUL-byte check, which catches mechanisms nobody has characterised
yet; it is bead `kb-wk2` and is not built.

Finally, this changes the premise Stage 3 rests on. The plan behind 0006
assumed skills shape behaviour and self-evolution improves skills. Two of the
most important behaviours in this system turned out not to be skill-shaped at
all, and they are now in code that self-evolution must never touch. That is not
a defeat for the idea — it is a boundary for it, and one worth drawing before
the reflection loop exists rather than after: some rules are config, some are
guarantees, and only the first kind should be allowed to evolve.

## Note on verification

`pytest tests/test_guards.py` covers both guards as pure functions, deliberately
weighting the "must allow" cases as heavily as the "must deny" ones. Only the
`--live` tier proves the wiring: `test_the_write_guard_blocks_a_shell_append_
and_the_agent_recovers` asserts both that the file survives *and* that the agent
still completes the task, because a guard that blocks without teaching produces
a stuck turn or a model hunting for another way around.
