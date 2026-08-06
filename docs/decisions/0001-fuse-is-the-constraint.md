# 0001 — FUSE is the constraint that decides the architecture

**Status:** accepted

## Context

We want a coding agent with a versioned knowledge base, hosted as cheaply as
possible. The obvious cheap answers are serverless: Cloud Run, Railway, Render,
or Anthropic's Managed Agents (where Anthropic runs the sandbox and you just
send events).

TigerFS exposes the database through a FUSE filesystem on Linux (NFS on macOS).
There is no MCP server, no HTTP API, and no library mode — the CLI surface is
`mount`, `unmount`, `create`, `fork`, `list`, `info`, `status`, `migrate`, with
no server or daemon mode. The mount *is* the interface. That needs `/dev/fuse`
and, in practice, `CAP_SYS_ADMIN`.

## Decision

Treat in-container FUSE capability as a hard requirement and select the host
around it, rather than choosing a host first and discovering the constraint
later.

## Consequences

Ruled out, on the record:

- **Google Cloud Run.** The container runtime contract forbids in-container
  mounts ("Running a mount process inside the container to mount… any other
  network file system"), forbids adding or using kernel capabilities, and states
  that "Cloud Run doesn't support binaries that use setuid flags… such as gcsfuse
  or sudo." Applies to gen1 and gen2 equally. Confirmed in practice:
  `--privileged` and `--cap-add SYS_ADMIN --device /dev/fuse` both fail.
- **Railway and Render.** Both refuse privileged containers, on the record from
  staff.
- **Anthropic Managed Agents.** Anthropic operates the sandbox, so you cannot
  mount your own filesystem into it. This is the painful one: absent the FUSE
  requirement, Managed Agents would be the simplest correct answer.

Also worth recording: there is **no TigerFS MCP server**, from Tiger Data or the
community. The string `mcp` does not appear in the TigerFS source, docs, ADRs,
changelog, issues, discussions, or its launch thread except as
`github.com/tklauser/numcpus` in `go.sum`. What TigerFS ships instead is an
auto-installed *Agent Skill*, which is easy to misremember as an MCP. The
adjacent real things — `tiger-cli`'s MCP server, `pg-aiguide` — talk to Postgres
or serve docs; neither exposes filesystem semantics.

## The escape hatch, and its price

You *can* reach the data without a mount: TigerFS keeps a `tigerfs` schema with
backing tables plus views in `public`, so any Postgres MCP can read file contents
as rows. But `.history/`, `.log/`, `.savepoint/` and `.undo/` are **synthesised
in Go by the FUSE layer** — they are not materialised in Postgres. SQL gets you
the bytes; it does not get you undo. Since savepoint-per-turn (ADR 0003) is the
main reason to choose TigerFS at all, going around the mount discards the point.

Related trap: pointing a *generic* filesystem MCP server at the mount works, but
only because FUSE is already doing the real work — and TigerFS deliberately
hides parts of its control surface from `ls`, so `list_files` will not
*discover* `.undo/` or `.savepoint/`. Without the skill that names them, you get
a crippled TigerFS.

## References

- <https://github.com/timescale/tigerfs>
- <https://github.com/timescale/tigerfs/blob/main/docs/adr/015-backing-table-schema-strategy.md>
- <https://docs.cloud.google.com/run/docs/container-contract>
- <https://github.com/GoogleCloudPlatform/gcsfuse/issues/787>
- <https://station.railway.com/feedback/allow-services-to-be-run-in-privileged-m-8c66b22b>
- <https://community.render.com/t/how-to-mount-s3-object-storage-on-docker-container/6241>
- <https://platform.claude.com/docs/en/managed-agents/overview>
