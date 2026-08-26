---
name: commit
description: >
  Write a git commit message in this repo's own voice: a lowercase imperative
  subject stating the change and its consequence, an explanatory prose body
  citing evidence, and a Co-Authored-By trailer naming the actual model.
  Invoke explicitly with /commit — only when the user has asked for a commit.
disable-model-invocation: true
---

# Commit in This Repo's Voice

The style here is distinctive enough (measured across 115 commits) that
copying a generic commit-message habit produces something visibly off-house.

## Non-Negotiable Rules

1. Only commit when explicitly asked — this skill exists to be invoked
   directly, never to fire on its own judgment about whether code "looks
   ready."
2. **If `.beads/` state changed this session, run
   `bd export -o .beads/issues.jsonl` and stage it before committing.** bd's
   own pre-commit hook refuses to auto-export when run as a git hook
   (`auto-export: skipping — running as git hook`), so skipping this step
   ships a stale ledger that *looks* committed.
3. **Before claiming a commit is or isn't pushed, check `git ls-remote origin
   <branch>` — never infer from `git status` alone.** bd's own backup pushes
   the branch on its own whenever the bead database changed and its 15-minute
   interval has elapsed, carrying along whatever commits exist at that moment.
   This has already produced two confidently-wrong "these are local and
   unpushed" claims in this repo's own history.

## Subject line

- Lowercase, imperative mood. Median length ~55 characters — not a fragment,
  not a conventional-commits tag.
- **State the change and its consequence**, usually joined by a comma or
  "instead of" / "rather than" / "which":
  - `stop telling people to publish the app, which disables it`
  - `refuse a second turn instead of letting it savepoint over the first`
  - `give the agent a calendar and a mailbox, and no way to send`
- **No `feat:`/`fix:`/`chore:` prefixes.** Zero appear in 115 commits; `fix`
  shows up only as plain English ("fix double-encoded Google OAuth secrets").
- A subject naming what is deliberately *not* done is idiomatic here, not an
  oddity to avoid.
- A bead id in the subject (`file img-r7o: ...`, `close img-4r2: ...`) is
  reserved for commits that are purely ledger bookkeeping.

## Body

- ~90% of commits have one. The consistent shape: what was broken or absent
  and *why it was invisible* → what the change does mechanically → what was
  verified and how.
- Cite concrete evidence: log lines, timings, function/symbol names, file
  paths, ADR numbers, bead ids. "Confirmed by running X" and "Verified end to
  end against Y" are idiomatic closers.
- Wrap around 76 characters. Multi-paragraph is normal for anything beyond a
  trivial change.

## Trailer

```
Co-Authored-By: Claude <model name> <noreply@anthropic.com>
```

Name the **specific model** that did the work (e.g. `Claude Sonnet 5`,
`Claude Opus 5 (1M context)` when in extended-context mode) — every commit in
this repo's history does this. There is no `🤖 Generated with` line anywhere
in this repo; don't add one.

## Steps

1. `git status`, `git diff --staged` and `git diff`, and `git log -5` to see
   what's actually changing and refresh the voice.
2. If beads changed: `bd export -o .beads/issues.jsonl` and stage it.
3. Draft the subject and body per the rules above.
4. Commit via a heredoc, to avoid quoting problems:
   ```sh
   git commit -m "$(cat <<'EOF'
   <subject>

   <body>

   Co-Authored-By: Claude <model> <noreply@anthropic.com>
   EOF
   )"
   ```
5. If asked to push: `git fetch`, then `git ls-remote origin <branch>` before
   saying anything about what is or isn't already on the remote.
