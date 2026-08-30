---
name: land
description: >
  Land a finished bolus of work end to end: commit it in this repo's voice
  (via the commit skill), push a feature branch, open a PR, wait for CI, and
  merge with a plain merge commit — matching every PR in this repo's history.
  Invoke explicitly with /land, at the end of a planned chunk of work, only
  when the user has asked for the whole commit-PR-merge sequence.
argument-hint: "[branch-name]"
disable-model-invocation: true
---

# Land a Bolus of Work

The manual sequence — commit, push, PR, wait for CI, merge, clean up — is
mechanical but has enough steps that skipping one is easy: merging on a
still-pending check, forgetting to prune the deleted remote branch, or
writing a PR body that silently drifts from this repo's actual convention.
This skill is that sequence, run the same way every time. It was written by
running it once by hand for PR #15 and turning what that took into rules.

## Non-Negotiable Rules

1. **Only run when explicitly invoked** — same posture as the `commit` and
   `ship` skills. Invoking `/land` is itself the user's authorization to
   push, open the PR, and merge it; it is not authorization to merge past a
   failing or still-pending check, or to reach for `--admin` on a protected
   branch. If a check fails, stop and report it — do not retry blindly and
   do not bypass it.
2. **Never discard uncommitted work to get here.** `git status` first. If
   there are changes that don't belong in this commit, stop and ask rather
   than guessing what to stage.
3. **Branch only when currently on `main` (or the repo's default branch).**
   If already on a feature branch from earlier in the session, use it —
   don't create a second branch on top of one already in flight.
4. **The commit message itself follows the `commit` skill's rules exactly —
   don't restate them here, invoke that skill's steps.** That includes its
   bead-export rule: if `.beads/` changed this session, `bd export -o
   .beads/issues.jsonl` and stage it before committing.
5. **The PR body is not the commit body copy-pasted.** Measured from PRs
   #13 and #14 in this repo: a PR body is `## Summary` / `## Test plan`,
   and — unlike a commit message, which never carries this line — ends with
   `🤖 Generated with [Claude Code](https://claude.com/claude-code)` on its
   own line above the `Co-Authored-By:` trailer. Carrying the commit body's
   *content* into the PR's Summary is fine; dropping this footer is the
   actual mistake to avoid.
6. **Merge with a plain merge commit, never squash or rebase, unless asked.**
   Every merged PR in `git log` here is `Merge pull request #N from
   <branch>` — `gh pr merge --merge --delete-branch` is what reproduces
   that shape.
7. **After merge, verify the cleanup rather than assuming it.** `gh
   --delete-branch` removes the branch on GitHub, but a local checkout still
   needs `git fetch --prune` before `git branch -a` stops showing it, and
   `gh pr merge` does not always leave you on `main` with the local branch
   gone — check both.

## Steps

1. `git status`, `git branch --show-current`.
2. If on `main`: pick a kebab-case branch name describing the change (match
   the style of existing branches — `wiki-links-in-chat`,
   `auto-title-conversations` — or use the one passed as `$1`), then
   `git checkout -b <branch-name>`.
3. Stage the specific files that belong to this change (never a blind
   `git add -A` — see the repo-wide git safety rules), then run the `commit`
   skill's steps 2–4 to write and create the commit.
4. `git push -u origin <branch-name>`.
5. `gh pr create --title "<subject, matching the commit subject>" --body
   "$(cat <<'EOF' ... EOF)"` — Summary bullets, a Test plan checklist, then
   the footer from rule 5 above.
6. Wait for CI: `gh pr checks <n> --watch`. This routinely runs past two
   minutes (the container test tier alone takes ~1–2 min) — if run via the
   Bash tool it will auto-background past its timeout, which is fine; just
   don't proceed to merge until it actually reports every check green.
7. `gh pr merge <n> --merge --delete-branch`.
8. `git fetch --prune`, then confirm `git branch --show-current` is `main`
   and `git branch -a | grep <branch-name>` finds nothing. If the local
   branch is still there, `git branch -d <branch-name>`.
9. Report the PR URL and the final `git log -3 --oneline` back to the user.

## Notes

- This assumes `gh` is already authenticated (`gh auth status`) and `origin`
  is the repo the PR should open against — both were true when this skill
  was written; if either isn't, stop and say so rather than working around
  it.
- Full reasoning for the commit-message conventions this depends on lives in
  the `commit` skill; this file only adds the PR/merge/cleanup layer on top
  of it.
