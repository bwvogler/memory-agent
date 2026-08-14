# 0006 — Track wiki work in beads, not in chat

**Status:** accepted

## Context

A wiki is never finished. Every pass over it turns up more work than that pass
should do: a page contradicting another, a claim with no date, a concept
referenced everywhere and defined nowhere, a directory with no `GUIDE.md`.

We had nowhere to put any of it. A turn is atomic — the agent reads the KB,
does what was asked, and stops — so anything it merely *noticed* was either
done immediately or lost. The `lint` skill made this painfully clear: its whole
job is finding problems, and its only output was a list in chat that scrolled
away. Running it twice produced the same list twice, which is a good sign the
output was going nowhere.

Markdown TODO lists are the usual answer and they do not survive context
compaction, session boundaries, or a second agent. That is precisely the
problem beads was built for: a dependency-aware issue graph with `bd ready` as
the claimable frontier, designed to be read and written by agents.

## Decision

Adopt beads (`bd`) as the durable work ledger.

The graph lives per user at `$WORK_DIR/{user_slug}/.beads`, an embedded Dolt
database. `bd` discovers it from the process's working directory, and the
agent's cwd is already per-user scratch, so multi-tenant isolation needed no
new code.

It is deliberately **not** in the KB mount. Dolt is a binary database; running
one over FUSE→SQL invites corruption for no benefit. The volume already holds
`kb.git`, which the revert button depends on, so this is a tier we already
trust.

Skills file what they find instead of reporting it. `lint` creates a bead per
finding, deduped against what is already open; `ingest` files the follow-ups a
source implies; `kb-curator` opens substantive work with `bd ready`.

Agent-facing instructions come from `bd prime`, whose output we inject into the
appended system prompt. bd ships that text and keeps it current with the
binary, so hand-maintaining a copy would just rot at the next version bump. bd
normally installs a `SessionStart` hook to inject it, but that hook cannot fire
here — `setting_sources=[]` means project settings are never read (see 0004) —
so we run it ourselves. Same benefit, no cost to isolation.

## Consequences

Work now survives the conversation that discovered it. Lint becomes worth
running: the second pass reports what is new rather than repeating itself.

`memory/backlog.md` is regenerated from the graph after every turn. This is a
projection, not the source of truth, and it earns its place twice: it puts a
human-readable backlog in the Postgres-backed store — the volume holding Dolt
has no replication and weaker backups than the KB — and it renders in the
existing `/kb` browser with no UI work.

**Two storage tiers now exist, and they revert independently.** A per-turn
savepoint rolls back the KB but not the bead graph, so after a revert
`memory/backlog.md` is stale until the next turn regenerates it, and a bead
closed by the reverted work stays closed. We accept this rather than pairing a
Dolt commit to every savepoint: that is real complexity for a rare operation.
The curator skill says to reopen such beads explicitly.

**bd asserts ownership of two things we had already decided differently**, and
states both as rules in `bd prime`, so they are overridden explicitly in
`_BEADS_OVERRIDES`:

- It tells agents to keep persistent knowledge in `bd remember` and avoid
  memory files. We keep memory in `memory/CLAUDE.md` (0004), where it is
  Postgres-backed, versioned and visible to the human. bd's memory would sit on
  the weaker tier and be invisible in `/kb`.
- Its session-close protocol tells agents to run `git status`, commit and push.
  Git here is savepoint infrastructure owned by the application, and there is
  no remote.

bd's own text defers to explicit orchestrator instructions, so this is
sanctioned rather than a fight with the tool. It is still a standing
maintenance cost: a future `bd` release could add a third such rule, and
nothing will tell us except reading the diff of `bd prime` output.

`bd` is pinned. It refuses to open a database written by a newer schema, so an
unpinned binary would break the graph on redeploy. The embedded Dolt engine is
most of ~100MB of image growth, which matters only because fly.toml sits at the
2GB memory ceiling that suspend requires.

**Never let the agent run `bd edit`** — it opens `$EDITOR` and blocks forever,
and a headless turn has no timeout. It is prohibited in `bd prime`, in the
curator skill, and worth remembering here because the failure mode is a hung
turn rather than an error.

Seeding had to change. `seed_bootstrap` never overwrote anything, which was
correct while skills were only ever created — but it meant a shipped fix could
never reach an existing deployment, while the seeder still looked like it
worked. It now records a hash of what it last shipped: a file still matching
that hash is untouched and gets replaced, a file that differs is left alone
with a warning. Deployments predating the state file have no recorded hash, so
we cannot tell and do not guess.

## Amendment: signal beads are evidence, not work

Stage 2 added a second kind of bead, and it needs a rule of its own. When a
turn is reverted, errors, exhausts `max_turns`, or is denied a tool, `app/
signals.py` files a bead recording it. These are *observations*, and treating
them like tasks would ruin the thing this ADR is about: `bd ready` is the
frontier a fresh session works from, and filling it with evidence turns the
ledger into a queue of things nobody intends to do.

So signal beads are created `deferred` and labelled `signal`. They are out of
`bd ready` by construction and are read in bulk — by a human, or by bead
kb-3sv, which asks whether they justify Stage 3 at all.

Repeated signals are deduped by title against open signal beads, because a
standing defect (a missing `allowed_tools` entry, say) would otherwise file one
bead per turn forever. Reverts are deliberately **not** deduped: each is a
separate human judgement, and the count is the measurement.

The ledger records **every** turn, not only the bad ones, in `turn_outcomes`
and `turn_skill_uses`. This is the part most likely to look redundant and is
not: `kb-curator` is effectively loaded on every turn, so it appears in every
revert whether or not it had anything to do with the failure. Without the
denominator, a per-skill failure count is a popularity contest. `GET
/api/signals` returns the rates.

## Amendment: image work is a third kind, and it leaves

This ADR assumed every bead is work the agent can do. The ones it actually
files most are not: ideas about the application it runs in, which it cannot
touch — no repo, no git, an immutable image. Left as ordinary beads they sit in
`bd ready` and are offered as claimable work forever.

So they are labelled `image` and created `deferred`, out of the frontier by the
same mechanism signal beads use, and pulled into a ledger in the repo where the
work can happen. The deployed copy then tracks delivery rather than intent: it
closes when an image that resolves it boots. See ADR 0010.

Wiki beads do not travel. They stay on the volume, where the agent that filed
them can also work them.

## Note on verification

Run `pytest --container` before trusting any of this, and `--live` before
trusting that the agent can actually reach the ledger. Three of the bugs found
while building this were silent — `bootstrap/` missing from the image, a
Postgres too old for `uuidv7()`, and Bash permissions blocking every `bd` call
— and none of them raised anything. Since `kb.py` logs-and-continues by design,
a completely broken ledger is indistinguishable from a working one at runtime.

`bd init` does more than initialise a database: it writes `AGENTS.md`, a
`.claude/settings.json` hook, a `CLAUDE.md`, Codex and Cursor config, and runs
`git init` with a commit. In our per-user scratch dir most of that is inert,
and the skill it installs at `.agents/skills/beads/SKILL.md` is genuinely good.
We keep it. If a future bd version scaffolds something that is *not* inert,
this is where it will come from — re-run `bd init` in a clean directory and
read what appears before upgrading the pin.

Two pieces of that scaffolding are inert only because of flags set for other
reasons, which is worth knowing before anyone relaxes them:

- `.claude/settings.json` registers a `SessionStart` hook running `bd prime`.
  It is never read, because `setting_sources=[]`. This is true of *file-based*
  hooks only — hooks passed to `ClaudeAgentOptions` as Python callables fire
  regardless, and 0007 relies on that. Do not "fix" this by
  enabling project settings: the agent runs with `permission_mode` of
  `acceptEdits` and that directory is its writable cwd, so it could author its
  own hooks — and hook commands are shell, bypassing the
  `allowed_tools=["Bash(bd:*)"]` allowlist entirely. Injecting `bd prime`
  ourselves gets the same content without opening that door.
- `CLAUDE.md` lands in the agent's cwd and is suppressed only by
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`. It restates both rules overridden in
  `_BEADS_OVERRIDES`. Those overrides work by being appended *after* the
  `bd prime` text; a discovered CLAUDE.md is injected into the conversation
  instead, where that ordering does not apply. Re-enabling auto memory would
  therefore bring bd's memory and git rules back through a path the overrides
  do not cover.
