# 0004 — Agent memory lives in the knowledge base, loaded explicitly

**Status:** accepted

## Context

The agent SDK's `SessionStore` mirrors *transcripts only* — not `CLAUDE.md`
memory files or other working-directory artifacts. Those need a separate
strategy. The obvious options are a mounted volume, an object-store sync, or
putting them in the knowledge base itself.

An initial objection to the third option turned out to be wrong and is worth
recording, because it is an easy mistake: it *seems* like memory content would
sit in the cached system-prompt prefix, so agent-edited memory would thrash the
prompt cache and re-charge full input tokens every turn. Not so. The SDK injects
CLAUDE.md-style content **into the conversation, not the system prompt**, and the
docs state directly that "CLAUDE.md content doesn't affect the system prompt
cache." TigerFS-backed memory is free in caching terms.

## Decision

Memory lives at `memory/CLAUDE.md` inside the TigerFS mount. The application
reads it and passes it as an appended system prompt, rather than letting the SDK
discover a `CLAUDE.md` by walking the filesystem.

## Why explicit loading rather than file discovery

**A missing memory file is silently skipped.** No error, no warning. If the mount
is not up when a session starts, the agent quietly loses its instructions and
just acts a bit dumber — the worst class of bug, because nothing looks broken.
Explicit loading lets us log a warning and surface it on `/healthz`.

**`@path` imports outside the working directory trigger a one-time approval
dialog.** Nobody is present in a headless container to answer it.

**`setting_sources=[]` is required for multi-tenant isolation anyway.** Without
it, filesystem settings and one user's memory leak across sessions. Explicit
loading is compatible with that; discovery is not. Note that auto memory (at
`~/.claude/projects/<project>/memory/`) loads regardless of `setting_sources`,
so it must be disabled separately with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`.

## Consequences

Memory gets versioning, history, and atomic undo for free — the same machinery
as the rest of the knowledge base. An agent that writes its own accumulated
notes becomes safe to allow, because every note is a versioned row you can roll
back.

Memory loads at session start and is re-injected on every request; a mid-session
edit does not take effect until the next session. This is fine for accumulated
notes and wrong for anything that needs to change within a conversation.

Instructions and skills deliberately do **not** live here. They ship in the
container image and are reviewed in version control: you want them diffed and
deployed atomically, and you do not want the agent editing its own operating
instructions by accident. Reference content and memory are data; instructions
are code.

## To verify

`autoMemoryDirectory` (pointing auto-memory into the mount) and the precise
re-read-on-compaction behaviour came back muddled in research. Confirm both
against current docs before depending on them.

## References

- <https://code.claude.com/docs/en/memory>
- <https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts>
- <https://code.claude.com/docs/en/agent-sdk/session-storage>
