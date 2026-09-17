---
name: land
description: >
  Land finished work end to end: split it into separate logical batches if
  more than one unrelated thing is sitting uncommitted, then for each batch —
  commit it in this repo's voice (via the commit skill), push a feature
  branch, open a PR, wait for CI, and merge with a plain merge commit,
  matching every PR in this repo's history. Once every batch is merged, `fly
  deploy` if anything landed actually changed the deployed image. Invoke
  explicitly with /land, at the end of a planned chunk of work, only when the
  user has asked for the whole commit-PR-merge(-deploy) sequence.
argument-hint: "[branch-name] [--deploy|--no-deploy]"
disable-model-invocation: true
---

# Land a Bolus of Work

The manual sequence — commit, push, PR, wait for CI, merge, clean up — is
mechanical but has enough steps that skipping one is easy: merging on a
still-pending check, forgetting to prune the deleted remote branch, or
writing a PR body that silently drifts from this repo's actual convention.
This skill is that sequence, run the same way every time. It was written by
running it once by hand for PR #15 and turning what that took into rules.

It also splits: PR #22 (a tool-failure-signal fix) and PR #23 (the
viewer-context feature) landed as two separate PRs only because they
happened to be worked in two separate sessions. Nothing stopped a single
`/land` invocation from bundling two unrelated changes into one commit and
one PR if they'd been uncommitted at the same time — this skill now makes
that split deliberate instead of accidental.

And it deploys: "landed" (merged to `main`) and "live" used to be two
separate manual steps, with the gap between them a place a merged fix could
sit unnoticed for days. `/land` now closes that gap itself, once, after
every batch in the invocation has merged — never per batch, since `fly
deploy` is a real (if short — see `prod-ops` rule 5) production outage, and
deploying three times for three small unrelated fixes would be three
outages for one release's worth of change.

## Non-Negotiable Rules

1. **Only run when explicitly invoked** — same posture as the `commit` and
   `ship` skills. Invoking `/land` is itself the user's authorization to
   push, open the PR, and merge it; it is not authorization to merge past a
   failing or still-pending check, or to reach for `--admin` on a protected
   branch. If a check fails, stop and report it — do not retry blindly and
   do not bypass it.
2. **Never discard uncommitted work to get here.** `git status` first. If
   there are changes that don't belong in ANY batch you can identify, stop
   and ask rather than guessing what to stage or leaving them behind
   silently.
3. **When it's unclear whether two changes belong together or apart, ask —
   don't guess.** A wrong split is not free: it either merges unrelated
   diffs into one reviewable unit (the thing splitting exists to prevent) or
   creates PR/CI churn for changes that were actually one thought. Only
   proceed without asking when the grouping is obvious from the diff itself
   (e.g. two files touched for two visibly different reasons, or a single
   file whose hunks address two unrelated concerns).
4. **Branch only when currently on `main` (or the repo's default branch).**
   If already on a feature branch from earlier in the session, use it —
   don't create a second branch on top of one already in flight. This
   applies per batch: after landing one batch, the next one branches from
   the freshly-updated `main`, never from a branch that still contains a
   different batch's now-merged changes.
5. **The commit message itself follows the `commit` skill's rules exactly —
   don't restate them here, invoke that skill's steps.** That includes its
   bead-export rule: if `.beads/` changed this session, `bd export -o
   .beads/issues.jsonl` and stage it before committing.
6. **The PR body is not the commit body copy-pasted.** Measured from PRs
   #13 and #14 in this repo: a PR body is `## Summary` / `## Test plan`,
   and — unlike a commit message, which never carries this line — ends with
   `🤖 Generated with [Claude Code](https://claude.com/claude-code)` on its
   own line above the `Co-Authored-By:` trailer. Carrying the commit body's
   *content* into the PR's Summary is fine; dropping this footer is the
   actual mistake to avoid.
7. **Merge with a plain merge commit, never squash or rebase, unless asked.**
   Every merged PR in `git log` here is `Merge pull request #N from
   <branch>` — `gh pr merge --merge --delete-branch` is what reproduces
   that shape.
8. **After merge, verify the cleanup rather than assuming it.** `gh
   --delete-branch` removes the branch on GitHub, but a local checkout still
   needs `git fetch --prune` before `git branch -a` stops showing it, and
   `gh pr merge` does not always leave you on `main` with the local branch
   gone — check both.
9. **One batch fully lands (merged and cleaned up) before the next one
   branches.** Never have two of this skill's branches open at once: a
   second batch branching before the first is merged risks basing it on
   unmerged work, and if the first batch's PR is later revised, the second
   batch's diff would silently carry those changes too.
10. **A deploy still gets a real, live confirmation each time — invoking
    `/land` covers commit/PR/merge (rule 1) but not this.** `fly deploy` is a
    production outage on a real household's running app (`prod-ops` rule 5),
    and unlike a merge it cannot be undone by reverting a commit once it's
    out. After the last batch merges, ask (do not assume) unless `--deploy`
    was passed explicitly as an argument this invocation; `--no-deploy` (or
    being told to skip it) skips asking entirely and just stops after
    landing. Only ask at all when something image-relevant actually landed —
    see "Deploying" below.

## Splitting Into Batches

Most invocations have exactly one thing to land — the common case, and steps
1–2 below are trivial for it. When there's more, decide the split before
doing anything else:

1. `git status` and a full `git diff` (staged and unstaged) to see
   everything outstanding, not just the files named when this was invoked.
2. Group the changes into batches that would each make sense as one PR on
   their own: usually one batch per distinct concern (a bug fix, a separate
   feature, an unrelated cleanup), not one batch per file. Two files changed
   together for one reason are one batch even if they live in different
   directories; two unrelated edits to the *same* file are two batches, and
   need `git add -p` (or `git diff` + a manual patch) to stage only one
   file's relevant hunks at a time — never split by discarding a hunk, only
   by choosing which commit it goes in first.
3. If the split isn't obvious, say what you're about to do (the proposed
   batches, one line each) and ask before staging anything — seeing a wrong
   split after two commits and a pushed branch costs a lot more than asking
   up front. Skip asking only when the boundary is clearly legible from the
   diff itself.
4. Order the batches. Independent batches can land in any order; if one
   batch's code depends on another's (rare, but possible if this is running
   mid-session over accumulated work), land the dependency first.

## Steps (run once per batch, in order)

1. Confirm `git branch --show-current` is `main` (rule 4) before staging
   this batch.
2. Pick a kebab-case branch name describing this batch (match the style of
   existing branches — `wiki-links-in-chat`, `auto-title-conversations` —
   or use `$1` when there is exactly one batch and a name was passed), then
   `git checkout -b <branch-name>`.
3. Stage exactly this batch's files or hunks (never a blind `git add -A` —
   see the repo-wide git safety rules), then run the `commit` skill's
   steps 2–4 to write and create the commit.
4. `git push -u origin <branch-name>`.
5. `gh pr create --title "<subject, matching the commit subject>" --body
   "$(cat <<'EOF' ... EOF)"` — Summary bullets, a Test plan checklist, then
   the footer from rule 6 above.
6. Wait for CI: `gh pr checks <n> --watch`. This routinely runs past two
   minutes (the container test tier alone takes ~1–2 min) — if run via the
   Bash tool it will auto-background past its timeout, which is fine; just
   don't proceed to merge until it actually reports every check green.
7. `gh pr merge <n> --merge --delete-branch`.
8. `git fetch --prune`, then confirm `git branch --show-current` is `main`
   and `git branch -a | grep <branch-name>` finds nothing. If the local
   branch is still there, `git branch -d <branch-name>`. This also
   fast-forwards local `main` to include what was just merged, which is
   what makes it safe to branch the next batch from it.
9. If another batch remains, go back to step 1 for it. Otherwise, report
   every PR URL landed (in order) and the final `git log -N --oneline`
   (N = number of batches + 1, to show every merge commit just made), then
   move on to "Deploying" below.

## Deploying

Runs once, after every batch above has merged — not per batch (rule 10).

1. Check whether anything just landed actually touches the deployed image:
   `app/`, `static/`, `skills/`, `bootstrap/`, `entrypoint.sh`, `scripts/`,
   `docs/shipped-beads.jsonl`, `requirements.txt`, `Dockerfile` or
   `fly.toml` — the exact set the Dockerfile `COPY`s or that `fly deploy`
   reads. A batch confined to `.claude/`, `tests/`, `docs/decisions/` or
   `.github/` produces a byte-identical image; deploying it is a real outage
   for zero effect, so skip straight to reporting without asking.
2. If something image-relevant landed: say so and ask whether to deploy now
   (rule 10) — unless this invocation was explicitly told `--deploy` (then
   just do it) or `--no-deploy`/"skip the deploy" (then skip straight to
   reporting, no question needed).
3. On a yes: `fly deploy`. It already blocks on Fly's own health checks
   before returning, but confirm it independently rather than trusting the
   exit code alone (rule 11): `curl -fsS https://memory-agent-proud-island-3747.fly.dev/healthz`
   and check `kb_mounted` and `transcripts` in the response, the same two
   fields `dev-checks`/`browser-test` gate on.
4. Report what was deployed (or that it was skipped, and why) alongside the
   landing summary from step 9 above.

## Notes

- This assumes `gh` is already authenticated (`gh auth status`) and `origin`
  is the repo the PR should open against — both were true when this skill
  was written; if either isn't, stop and say so rather than working around
  it.
- Full reasoning for the commit-message conventions this depends on lives in
  the `commit` skill; this file only adds the PR/merge/cleanup layer on top
  of it.
