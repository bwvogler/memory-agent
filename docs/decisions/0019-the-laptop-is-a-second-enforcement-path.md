# 0019 — The laptop is a second enforcement path, not a second implementation

**Status:** accepted

## Context

There have been two ways to change the production wiki, and the gap between
them is a model.

`mcp__kb-prod__*` (ADR 0014, the `run-reflection` skill) runs a real deployed
turn: savepoint on the volume, the ADR 0007 guards, signal capture, backlog
regeneration, revertable from the web UI. The model doing the work is the
deployed Sonnet, on the deployed API key.

`scripts/mount-kb.sh --prod --writable` puts the same wiki on a laptop as
ordinary files, so Claude Code's own model edits it on subscription tokens —
with the app's source tree in the same session, which the deployed agent
structurally cannot have (no repo, read-only image). That combination is worth
having for occasional hands-on curation. It also leaves behind every structural
defence in `app/guards.py`, because those are Python callables handed to
`ClaudeAgentOptions` inside the app's process. ADR 0007 exists because a prompt
stating those rules was not enough; a laptop session has neither the hook nor
the prompt.

The losses are not equal, and lumping them together is what kept this open. Some
are inherent to not running a turn — no signal bead, no conversation event, no
`AGENT_GUIDE.md` in the system prompt, no Revert button. Those are a fair price
for occasional use, provided they are *said* rather than assumed away. But the
two rules ADR 0007 calls load-bearing are not a fair price, because breaking
either destroys the thing the system exists to provide: one already turned 233
bytes of a user's personal notes into 233 zeroes, and the other silently empties
the ledger.

Four things were measured before deciding, not reasoned:

1. Claude Code's hook protocol is the SDK's hook protocol. The deny payload
   (`hookSpecificOutput.permissionDecision`) and the block payload
   (`decision`/`reason`) that `app/guards.py` already returns are accepted
   verbatim. Checked against a working implementation, not against docs.
2. `app/guards.py` imports under bare stdlib `python3` — no `.venv`, no
   `claude_agent_sdk`, which is `TYPE_CHECKING`-only. So a hook script can
   import it with nothing installed.
3. Claude Code's transcript JSONL carries `message.role` / `message.content` as
   typed blocks: the exact shape `guards.unfiled_deferral` parses. It also
   interleaves subagent rows into the same file, marked `isSidechain`.
4. **`guards._mentions_kb` does not fire on a laptop.** It is a substring test
   against `config.kb_mount`, an absolute `/mnt/kb` in the container. A laptop
   session names the mount relatively — `mnt/kb/memory/x.md` — all day, and
   that form matches nothing. The hazard list was reachable; the rule was not.

## Decision

Enforce the same two rules for Claude Code sessions in this repo, from
`.claude/settings.json`, implemented in `scripts/kb_guard_hook.py`.

**Reuse, do not restate.** The hazard list (`guards.kb_hazard`), the refusal
prose (`guards.GUIDANCE`) and the deferral detector
(`guards.unfiled_deferral`) are imported. `unsafe_kb_write` was split so its
second half is callable by a caller with a different notion of "is this the
mount"; nothing about which commands are hazardous moved. A second copy would
drift, and the drift would be silent in the worst direction — a command the
deployed agent is refused and a laptop session is allowed.

**Add the guard the container never needed.** A laptop has two ways to write
into something that looks exactly like the wiki and is not it, both reporting
success: an unmounted mountpoint (an ordinary local file in a gitignored
directory), and a read-only mount, where macOS's NFS client caches the write
and nothing reaches Postgres. The second is `prod-ops` rule 3, which until now
was documented and enforced nowhere. Both are refused, and the refusal names
the database.

**Answer questions about the mount from a state file, not from `mount`.**
`scripts/mount-kb.sh` writes `$WORK_DIR/.mount-state.json` on a successful
mount and removes it on `--kill`. Parsing `mount` output would answer
"writable?" differently on macOS and Linux and could never answer "which
database?" — and naming the database is most of what makes the refusal
readable.

**Open the savepoint rather than nagging for one.** The first write of a
session to a live writable mount creates `savepoint:laptop-<session_id>` via
`kb.create_savepoint`, so there is one definition of what a savepoint is. This
restores revertability, which was the largest of the recoverable losses.

**Scope the deferred-work guard to live mounts.** This repo is also where the
app is developed. Blocking an ordinary `app/` refactor for saying "follow-up"
would be the false positive ADR 0007 warns costs as much as a miss, and it is
the kind that teaches people to reach for `--no-verify`. It also routes to two
ledgers: wiki content to prod's volume ledger, where the household agent can
act on it; app and image work to this repo's committed `.beads/`.

The remaining losses stay lost, and the `kb-direct` skill's job is to make the
agent say so — the failure to design against is the household assuming the
deployed agent made a change it has no record of.

## Consequences

A laptop session in this repo can no longer corrupt a KB file with a shell
append, write into an unmounted or read-only mountpoint believing it landed, or
end a turn having named work it did not file. None of that depends on the agent
having read a skill first, which is the whole point.

`.claude/settings.json` has no effect on the deployed app and cannot acquire
one: `app/agent.py` sets `setting_sources=[]` precisely so file-based settings
are dead there, and bead `img-9g8` already rejected changing that. The two
enforcement paths share code and nothing else.

The Bash guard over-matches, inherited from `unsafe_kb_write` and now slightly
worse because relative markers are shorter. A command that merely *quotes* a
hazardous command while naming the mount is refused — which happened within
minutes of the hook going live, to a probe written to test it. The refusal is
cheap and explains itself, an under-refusal is not, and the asymmetry is the
same one ADR 0007 settled. Worth knowing, not worth narrowing.

`scripts/kb_guard_hook.py` fails open on every internal error, so its silence
is never proof a command was safe.

## Note on verification

The hazard-list split, the three-way write verdict, the ledger routing, the
`isSidechain` filtering and fail-open behaviour are covered in
`tests/test_kb_guard_hook.py` and `tests/test_guards.py`, fast tier, no Docker.
The suite was mutation-checked: removing the relative markers and disabling the
write verdict each fail it.

Beyond the unit tier, the hook was driven end-to-end through its real CLI
interface for ten payloads, and `ensure_savepoint` was exercised against a real
git repo — confirming the savepoint is created once per session, separately per
session, and that `reset --hard` restores a file clobbered in between. The
read-only refusal was confirmed to name the database it refused.

It was then rehearsed against a **real TigerFS mount** (`--dev`, on the
throwaway Postgres). Every guard behaved: the append refused, the truncating
write and the read allowed, the write to the live writable mount allowed, and
the savepoint opened covering all 18 files of the real workspace. `reset --hard`
rolled a write back *through FUSE*. With a mount live, the `Stop` guard fired
and offered both ledgers; with none, it stayed silent. `--kill` removed the
state file.

One thing that rehearsal settled, which the plan had assumed: **`mount` output
could not have carried this.** A live writable dev mount reports
`(nfs, nodev, nosuid, mounted by bwvogler)` — no `rw` marker to find, only the
absence of `read-only`, and no hint of which database. The state file is not a
convenience over parsing `mount`; parsing `mount` was never going to work.

What is still *not* verified by any tier: the behaviour of a real **read-only**
TigerFS mount, which is where the silent-no-op claim originates. That claim is
carried over from `scripts/mount-kb.sh`'s own measured caveat rather than
re-measured here. The refusal path around it is verified; the filesystem
behaviour it describes is not.

## Amendments

_None yet._
