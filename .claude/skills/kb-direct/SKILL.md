---
name: kb-direct
description: >
  Curate the wiki from Claude Code itself — mount the TigerFS knowledge base
  locally and edit it with this session's own model, instead of spending API
  tokens on a deployed turn via kb-prod. Covers the mount ritual, what the
  laptop-side hooks enforce, how to prove a write actually landed, and which
  machinery you give up by working this way. Invoke explicitly with
  /kb-direct, only when the user has asked to work on the wiki from here.
disable-model-invocation: true
---

# Curate the Wiki From Here

There are two ways to change the production wiki, and this is the one that
uses Claude Code's own model rather than the deployed Sonnet on the API key.
The trade is real and runs in both directions: you get a stronger model with
the app's source in the same session — something the deployed agent
structurally cannot have, since it has no repo and a read-only image — and you
lose most of the machinery a deployed turn wraps around a write.

Two of those losses are recoverable and have been recovered. `.claude/settings.json`
wires `scripts/kb_guard_hook.py` into this session's `PreToolUse` and `Stop`
hooks, so the rules from ADR 0007 are enforced here by the same code that
enforces them in production. The rest are not recoverable, and the important
thing is to say so rather than let the household assume the deployed agent did
this work.

| | Deployed turn (`/kb-prod`) | This skill |
|---|---|---|
| KB corruption guard | yes | **yes**, same hazard list |
| Deferred-work guard | yes | **yes**, routed to two ledgers |
| Savepoint / revertable | yes, on the volume | **yes**, in local `work/kb.git` |
| Write can't silently no-op | n/a | **yes**, new guard |
| Signal capture | yes | no |
| Conversation event log | yes | no |
| `AGENT_GUIDE.md` in the prompt | yes | no — read it yourself |
| `backlog.md` regenerated | yes | no |
| Revert button in the web UI | yes | no |

## Non-Negotiable Rules

1. **The mount is production.** `.env`'s `KB_DATABASE_URL` is the same Neon
   database the deployed machine writes to. A file save under `mnt/kb/` is a
   live wiki edit with no review and no deploy in between. Nothing about a
   local path suggests this.

2. **Read-only is the default and usually the right answer.** `--prod` mounts
   read-only; `--prod --writable` is a separate sentence, and it prompts for
   the database host because it is the one combination that can destroy
   production from a laptop. Most reasons to mount from here are reasons to
   read.

3. **A write to a read-only mount reports SUCCESS and reaches nothing.**
   macOS's NFS client caches it; Postgres never hears about it. Reading the
   file back shows your new content, from that same cache. So `ls` and `cat`
   are not proof a write landed — see *Verifying a write landed* below. The
   hook now refuses this write outright, which is new; before, it silently
   did nothing.

4. **Never append into the mount with a shell.** The filesystem has no
   read-modify-write: `>>`, `tee -a`, `sed -i` and friends zero everything
   already in the file. Write files whole — the `Write` tool, or a single `>`
   redirect. This destroyed a user's `memory/CLAUDE.md` once (ADR 0007), and
   the hook refuses it, but the rule is yours to keep whether or not a hook is
   watching.

5. **Say what was not captured.** No signal bead, no conversation event, no
   entry in the web UI's turn list. When you report back, say the work was
   done directly rather than by the agent, so nobody looks for it in `kb log`
   and concludes it never happened.

6. **File deferred work by subject.** Wiki content goes to prod's volume
   ledger (`scripts/fly.sh --write bd create`), where the household agent can
   act on it. App or image work goes to this repo's `.beads/` with
   `--labels image`. The `Stop` hook will stop you if you forget, but only
   while a mount is live.

## Steps

1. **Mount.** Read-only unless the task genuinely requires writing:
   ```sh
   bash scripts/mount-kb.sh --prod              # read-only
   bash scripts/mount-kb.sh --prod --writable   # live edits
   bash scripts/mount-kb.sh --dev               # throwaway; needs docker compose up
   ```
   This also writes `work/.mount-state.json`, which is how the hooks know
   which database is behind the mountpoint and whether it is writable. A mount
   made any other way is refused for writes, because that question has no
   answer.

2. **Load the context nothing injects for you.** A deployed turn gets
   `AGENT_GUIDE.md`, the skill listing and `bd prime` appended to its system
   prompt. This session gets none of it. Read, at minimum:
   ```
   mnt/kb/memory/AGENT_GUIDE.md          # the operator-written schema document
   mnt/kb/memory/CLAUDE.md               # accumulated notes
   mnt/kb/memory/<dir>/GUIDE.md          # per-directory format rules
   mnt/kb/memory/skills/kb-curator/LEARNED.md
   ```
   Skipping this is how a laptop session writes a page that violates
   conventions the deployed agent would have honoured.

3. **Work.** Use `Write` and `Edit`, never a shell append. The first write to
   a live writable mount opens a savepoint named `laptop-<session-id>`
   automatically; you do not need to make one, only to know it exists.

4. **Verify, then report.** Prove the write landed (below), then tell the user
   plainly what changed and that it bypassed the deployed agent.

5. **Unmount.** `bash scripts/mount-kb.sh --kill`. The mount holds a database
   connection until the process is stopped, and a plain `umount` leaves it
   running.

## Verifying a Write Landed

Two checks, because they catch different failures.

**NUL bytes** catch a corrupted write — the mount's characteristic damage, and
the one a successful-looking tool call can still leave behind:
```sh
python3 -c "import sys; d=open(sys.argv[1],'rb').read(); print('NULs:', d.count(b'\0'))" \
  mnt/kb/memory/<path>.md
```
Anything but `0` means the file was damaged; restore it from a savepoint.

**A database round-trip** catches the read-only no-op, which no local read can
see through:
```sh
psql "$KB_DATABASE_URL" -c \
  "SELECT filename, octet_length(body) AS bytes FROM tigerfs.memory WHERE filename = '<file>.md'"
```
One row per file; paths are assembled from `parent_id`, so match on filename
and check the byte count against what you wrote. The recursive CTE that builds
full paths is in `app/kb.py:588` if you need to disambiguate a common filename.

## Undoing

The savepoint is an ordinary git repo whose work tree is the mount:
```sh
git --git-dir=work/kb.git --work-tree=mnt/kb/memory log --oneline
git --git-dir=work/kb.git --work-tree=mnt/kb/memory diff <sha>       # look first
git --git-dir=work/kb.git --work-tree=mnt/kb/memory reset --hard <sha>
```
`reset --hard` is what `kb.undo_to_savepoint` does — never `git revert`. This
history is local to this machine and separate from the volume's; the web UI's
Revert button cannot see it.

TigerFS also keeps its own control surfaces — `.log/`, `.history/<path>/`,
`.savepoint/` and `.undo/` under the mount root. Read `.log/` before undoing
anything, and never undo another user's changes. Details in
`skills/kb-curator/references/tigerfs.md:58-88`.

## Error Handling

- **"Refused: … is not mounted"** — you are about to write a real local file
  into a gitignored directory that looks exactly like the wiki. Mount first.
- **"Refused: … is mounted READ-ONLY"** — remount with `--writable`, or stop.
  Do not retry with a different tool; they all fail the same way, silently.
- **"cannot tell which database is behind it"** — something is mounted that
  the script did not mount. `--kill` and remount so the state file exists.
- **`kb-guard: savepoint failed …`** — the write was allowed but is not
  revertable. Fix `work/kb.git` before continuing, or accept it knowingly.
- **`Write verification failed: … expected N-1 bytes`** — a false alarm. The
  store appends a trailing newline. Re-read the file; if the content is right,
  you are done. Never fall back to a shell append.

## Notes

- `run-reflection` is the other direction: it triggers a real deployed turn
  over MCP, keeping every piece of machinery this skill gives up, at the cost
  of using the deployed model. Prefer it for anything that wants a savepoint on
  the volume, a signal, or a Revert button.
- A good hybrid for risky work: mount `--prod` read-only, do all the reading
  and drafting here, then hand the finished material to `mcp__kb-prod__ingest`
  so the write itself happens inside a real turn.
- `prod-ops` owns the rules for reaching the deployed machine and its volume,
  including `scripts/fly.sh`'s `--write` verb allowlist. This skill does not
  restate them.
- The hooks are in `.claude/settings.json` and implemented in
  `scripts/kb_guard_hook.py`; they are laptop-only by construction, because
  `app/agent.py` sets `setting_sources=[]` and the deployed app never reads
  them. They fail open on any internal error, so never treat their silence as
  proof a command was safe.
- Full reasoning: CLAUDE.md's "Two rules are enforced by hooks, not by
  instructions" and "Scratch vs. KB" sections, plus `docs/decisions/0007` and
  `docs/decisions/0019`.
