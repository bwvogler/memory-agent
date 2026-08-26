# 0016 — One page, three panes

**Status:** accepted

## Context

`/` was the chat and `/kb` was the wiki, joined by two plain anchors. Nothing
was shared between them — the theme tokens, the header bar, the centred-column
idiom were all duplicated, and had already diverged: `kb.html` carried a
`* { margin:0; padding:0 }` reset and a `--sidebar` token `index.html` never
had. That divergence was cosmetic. The real cost was that **navigating lost
the conversation** — `sessionId` lived in a JS variable, so clicking
"Knowledge Base →" ended the session, and the wiki is the thing you most want
open *while* talking to the agent, because the agent is writing it as you
talk. A turn would say "I've updated your recipes index" and the only way to
look was to leave.

Three facts turned "merge two pages" into more than a layout change.

**The SSE stream didn't carry the path a write touched.** `tool_use`'s
`detail` (`interact.describe_tool_input`) is a 200-char display string with
only `config.kb_mount` stripped, and `tool_result` carries `{id, name, ok,
detail}` — no path at all. Neither is enough to know *what to open* when the
agent writes a KB file.

**No route could read a file back out of `$WORK_DIR`.** An uploaded document
lands at `$WORK_DIR/{slug}/uploads/{turn_id}/{name}` (`kb.resolve_upload_path`)
and stays there — grep for `FileResponse`/`StaticFiles` across `app/` found
only the two static pages and the `/static` mount. A centre pane that shows
uploads needed a new route, over a directory that also holds the bead ledger.

**The renderer had no sanitizer.** `kb.html`'s vendored `marked` overrides
`html({text:e}){return e}` — raw HTML in a wiki page already executes today.
That was tolerable while the only content source was the KB. It stops being
tolerable once the same `innerHTML` call also renders an uploaded document,
because uploads are exactly "a document someone sent you" — attacker-supplied
by construction.

## Decision

One document, `static/index.html`, served at `/`, `/kb` and `/kb/{path:path}`
(`app/main.py`'s `index()`). Left pane: the KB tree, carried over from
`kb.html` mostly unchanged. Centre pane: a renderer for KB articles, agent
writes, and uploads. Right pane: chat, carried over from the old `index.html`.
Drag-resizable gutters, widths in `localStorage`. Assets split into
`static/app.css`, `static/app.js`, `static/vendor/marked.min.js` — the
`/static` mount already existed and was unreferenced.

**A pure sibling to `describe_tool_input`, not a new event kind.**
`interact.describe_tool_target(name, tool_input)` strips
`f"{kb_mount}/{WORKSPACE_NAME}/"` (not just the mount — API paths are rooted
at `$KB_MOUNT/memory`, confirmed against a real TigerFS table by
`test_the_path_the_ui_would_open_is_the_path_the_api_accepts`, a container
test written specifically to gate this assumption before any UI was built) and
returns `{"kind": "kb", "path": ...}` for `Write`/`Edit`/`MultiEdit`/
`NotebookEdit`, `{}` otherwise. `Read` is excluded on purpose — following every
read would flicker the pane through the corpus while the agent researches.
Added as a `target` key on the existing `tool_use` payload rather than a
second kind, because it still needs the same id-correlation with `tool_result`
to know the write succeeded, and a `kb_write` event would just be that
correlation with extra steps. The client stashes `target` by tool id and
navigates only on a matching `tool_result` with `ok: true` — `tool_use` fires
*before* the tool runs, and a failed `Write` must not move the pane.

**`GET /api/uploads/{turn_id}/{name}`, ownership proven by path, not by
Turn.** The slug comes from the verified `Identity` and never appears in the
URL, so no caller can name another user's directory — stronger than a
`turn.user_email` check, which would also 404 a valid file once the
in-process, 200-turn-bounded `Registry` evicts it. `turn_id` is checked
against `\A[0-9a-f]{32}\Z` *before* touching the filesystem, and
`uploads_dir_for`/`scratch_dir_for` grew a `create=False` path
(`kb.upload_path_for_read`): without it a GET on a URL-supplied `turn_id`
would `mkdir(parents=True)` on the volume, and `resolve_upload_path`'s
containment check would pass regardless, because the escape already happened
one call up. Only `uploads/` is servable — never arbitrary scratch, which also
holds the bead ledger.

**Content-type from an allowlist, never `mimetypes.guess_type`.** `.md/.txt/
.csv/.json` as `text/plain`, images as their real type, everything else
`application/octet-stream` plus `Content-Disposition: attachment` and
`X-Content-Type-Options: nosniff`. `text/html` and `image/svg+xml` are
deliberately absent — both are scripting contexts on an origin that can also
`POST /api/turns`.

**One event carries the attachment, appended before `spawn`, cleared only on
an authoritative `done`/`failed`.** `GET /api/turns/{id}/events` replays from
`Last-Event-ID`, and a fresh `EventSource` (no id of its own) replays from
zero — so a page **reload** during a turn just needs the browser to ask for
the same turn id again for the whole transcript, including the attachment
chip, to reappear. That only works if the persisted turn id
(`localStorage`) survives the reload, and the first cut of this got it
backwards: `finish()` cleared the marker from a shared code path that
`es.onerror` also called, and a reload closes the `EventSource` exactly the
way a dead connection does — closing the tab fires `onerror` synchronously in
the *dying* document, before the reloaded page's `boot()` ever runs. Confirmed
live: a Playwright reload mid-turn showed `send.textContent === 'Send'` and no
resumed chip, and the server log showed exactly one `GET .../events` instead
of two. The fix: the marker is removed only inside the `done`/`failed`
listeners. `onerror` on a *resuming* stream instead confirms via
`GET /api/turns/{id}` before clearing it, so a genuinely-gone turn (the
Registry's eviction, or a stale id) doesn't loop forever, while an ordinary
reload — or any other transient drop — leaves the marker alone and
self-heals the next time a real terminal event arrives. Verified against the
real dev stack: a reload mid-turn re-opened the SSE stream, replayed the
attachment event, and a click on the resumed chip fetched real bytes from
`/api/uploads/...` (no local blob survives a reload).

**Sanitize at the parser, not a sandboxed iframe.** `marked.use({renderer:
{html: ({text}) => escapeHtml(text)}})` removes the raw-HTML vector, and a
post-parse pass drops any `href`/`src` whose scheme isn't `http:`, `https:`,
or `mailto:` (`marked`'s own `encodeURI` does not stop `javascript:` or
`data:text/html`). The threat model is not "who reads this app" but "what did
the agent ingest": `WebFetch`, the Gmail catalog entry, and an upload
explicitly described as "a document someone sent" are all laundered
attacker content, and script executing on this origin is not a stolen cookie —
it's a confused deputy holding an agent with `acceptEdits` over the wiki and
`Bash(bd:*)`, that can answer its own permission prompt at
`POST /api/turns/{id}/permission`. An iframe sandbox is the stronger control
and was rejected on cost: the centre pane needs same-page link handling,
chat-driven navigation, and the theme's CSS custom properties, none of which
survive `sandbox` without `allow-same-origin` and a `postMessage` bridge for
every interaction. **This trade flips the moment raw HTML in wiki pages is
wanted as a feature.**

**`/` becomes authenticated.** It was harmless open while it was an empty
shell whose every fetch was itself authenticated. Once `/` *is* the wiki, an
open shell that 403s its own tree fetch is worse than the Access login
redirect it gets instead — and Access only issues that redirect for a request
it protects. `fly.toml` probes `/healthz`, not `/`.

**`sql_read_file` stopped fetching the whole corpus per click.** It re-ran
the full recursive CTE — `path` *and* `body*, for every `.md` file — to find
one row. Split into `_PATHS_CTE`; the listing query drops `body` entirely
(never used it) and the read query gained a `WHERE path = $1`. In scope
because the merge multiplies the call rate: every tree click, every agent
write, every deep link.

## Consequences

Uploads only render inline for a fixed set of safe types; everything else is
an opaque download, by the same allowlist that sets the response header — a
PDF or spreadsheet attachment is never previewed, only downloadable.

A reload during a turn cannot reconstruct the user's own message text or
images — nothing on the event stream carries them, only `turn.prompt`
(internal, unexposed) does. Only the attachment chip survives, as a
synthetic "you" bubble built purely from the replayed event. Good enough to
keep the upload clickable; not a general transcript-durability fix.

`Identity.slug`'s collisions (`a.b@x`, `a_b@x`, `a+b@x` all fold to the same
directory) are now load-bearing for a *read* as well as scratch/beads/KB, all
of which already shared this property. Bounded by ADR 0005's allowlist and an
unguessable 32-hex `turn_id`; not fixed here — a collision-free slug would be
a data migration on an unreplicated volume, filed as a bead instead.

`static/kb.html` is deleted; nothing references it.

## What was rejected

**Re-fetching the tree and diffing it to find what changed.** The listing has
no sizes or mtimes, so an *edit* to an existing file — the common case — is
invisible to a diff.

**A `Turn.files` field for attachments.** Would be a second copy of a fact
`turn.prompt` already carries via `_attachment_note`, and the event stream
already has to name the file for the reload case regardless.

**Auto-opening the centre pane for a file the user just uploaded.** You
already know what you attached; only a file the *agent* wrote is news. The
chip is a link, not a push, for exactly one caller: a successful
`tool_use`/`tool_result` pair.
