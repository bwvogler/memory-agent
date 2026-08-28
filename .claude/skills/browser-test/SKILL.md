---
name: browser-test
description: >
  Drive a headless browser (Playwright) against an isolated Docker instance
  of this app to verify frontend/JS behavior in static/index.html and
  static/app.js, instead of only reasoning about the code — including real
  mobile device emulation (Pixel 6 Pro, iPhone 15). Use when: "test this in
  the browser", "verify the UI", "does the tree collapse correctly", "check
  this on my phone", "test on Android/iPhone", or after editing
  static/index.html, static/app.js, or static/app.css.
allowed-tools: Bash(.venv/bin/python ${CLAUDE_SKILL_DIR}/scripts/browser_check.py *)
---

# Browser Test

Verify frontend behavior of this app by driving a real, running instance —
not by reasoning about `static/app.js` from source alone. `pytest
--container` asserts on the backend only: it never executes the served
JavaScript, so a click handler that silently does nothing looks identical to
a working one in that tier. This closes that gap, for desktop Chromium and
for two real mobile device profiles (see "Device/mobile emulation" below).

## When to Use

- After editing `static/index.html`, `static/app.js`, or `static/app.css`.
- When asked to test, verify, or check a frontend/UI change — including on a
  phone: "does this work on mobile", "check my Pixel/iPhone".
- Before reporting a frontend change complete — this project's own CLAUDE.md
  already asks for that; this skill is how to actually do it headlessly.

## Prerequisites

The project venv needs `playwright` (pinned in `requirements-dev.txt`) and
the Chromium browser binary. One-time setup if either is missing:

```bash
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
```

`--device "iPhone 15"` needs WebKit too — a separate download, since real
iPhone Safari runs WebKit rather than Chromium's Blink engine, and it is NOT
covered by the `chromium` install above:

```bash
.venv/bin/python -m playwright install webkit
```

`--device "Pixel 6 Pro"` needs no extra install — Android Chrome is
Blink-based, same engine the `chromium` install above already covers.

## Non-Negotiable Rules

1. Always use the isolated stack below (project name `memory-agent-test`,
   port 18080) — the exact same one `pytest --container` uses (see
   `tests/conftest.py`). Never point this at a plain `docker compose up` dev
   stack: its `down -v` destroys the user's local bead ledger and KB volume
   (CLAUDE.md: "`docker compose down -v` destroys the local ledger").
2. Never run this while `pytest --container` (or `--live`) is active — they
   share the same project name and port and will collide.
3. Always tear the stack down (`down -v`) when finished, success or failure.
   It is a disposable volume created for this check alone.
4. Screenshots and other artifacts go under the scratchpad directory, never
   committed to the repo.

## Steps

1. From the repo root, bring up the isolated stack:
   ```bash
   docker compose -p memory-agent-test -f docker-compose.yml -f tests/compose.test.yml up -d --build
   ```
2. Wait for it to report healthy on both flags the app itself gates tests on:
   ```bash
   for i in $(seq 1 60); do
     body=$(curl -s http://localhost:18080/healthz)
     echo "$body" | grep -q '"kb_mounted":true' && echo "$body" | grep -q '"transcripts":"ready"' && break
     sleep 2
   done
   echo "$body"
   ```
3. Drive the browser — use `scripts/browser_check.py` (see Task Recipes) for
   anything it can express; write a short one-off Playwright script for
   anything it can't.
4. Tear down, always:
   ```bash
   docker compose -p memory-agent-test -f docker-compose.yml -f tests/compose.test.yml down -v
   ```

## Task Recipes

### Run a check with the bundled CLI

`scripts/browser_check.py` runs its actions in the order they appear on the
command line — mix `--wait-for`, `--click`, `--click-text`, `--eval` freely.
Each `--eval` prints its JSON result; `--console` prints any browser console
message or page error as it happens; a trailing `--screenshot` always fires
last. Invoke it exactly this way (matches this skill's `allowed-tools` grant,
so it runs without a permission prompt):

```bash
.venv/bin/python ${CLAUDE_SKILL_DIR}/scripts/browser_check.py http://localhost:18080/kb \
  --console \
  --wait-for .section-header \
  --eval "document.querySelectorAll('.dir-children.collapsed').length" \
  --click-text wiki \
  --eval "document.querySelector('.section-header.active')?.textContent" \
  --screenshot /tmp/check.png
```

Then look at the screenshot with the Read tool — it renders as an image.

### Device/mobile emulation

`--device NAME` swaps `--width`/`--height` for one of Playwright's own device
profiles — exact viewport, device scale factor, `is_mobile`/`has_touch`, and
the real user agent — and launches whichever engine that profile actually
runs on: Chromium for `"Pixel 6 Pro"` (Android Chrome is Blink-based, so this
is a faithful engine match), WebKit for `"iPhone 15"` (real iPhone Safari is
WebKit, not Blink — spoofing the user agent under Chromium would silently
skip the engine/compositor behavior these checks exist to catch). `--device`
and `--width`/`--height` are mutually exclusive; the script refuses both.

```bash
.venv/bin/python ${CLAUDE_SKILL_DIR}/scripts/browser_check.py http://localhost:18080/ \
  --device "Pixel 6 Pro" \
  --console \
  --wait-for ".pane-tabs" \
  --eval "getComputedStyle(document.querySelector('.pane-toggles')).display" \
  --eval "getComputedStyle(document.querySelector('.pane-tabs')).display" \
  --click "[data-pane=nav]" \
  --screenshot /tmp/pixel6pro.png
```

```bash
.venv/bin/python ${CLAUDE_SKILL_DIR}/scripts/browser_check.py http://localhost:18080/ \
  --device "iPhone 15" \
  --console \
  --wait-for ".pane-tabs" \
  --eval "getComputedStyle(document.querySelector('.pane-toggles')).display" \
  --eval "getComputedStyle(document.querySelector('.pane-tabs')).display" \
  --click "[data-pane=nav]" \
  --screenshot /tmp/iphone15.png
```

Exactly one of `.pane-toggles`/`.pane-tabs` should compute `none` and the
other `flex` — never both, and never neither. If a check on the isolated
stack ever shows both visible at once the way a real deployed phone did,
that's a real CSS/JS defect; if the isolated stack looks correct but a real
phone still doesn't, suspect a stale browser cache on the phone before
suspecting the code (see "Notes" below) — `/` and `/static/*` both now send
`Cache-Control: no-cache` for exactly this reason.

Other named profiles exist in Playwright's catalog beyond these two (e.g.
`"Pixel 6 Pro landscape"`, `"iPhone 15 Pro"`) — pass any name `--device`
accepts; these two are just the ones this project's tests actually target.

Clicking a `.pane-tabs` button triggers a `scrollIntoView({behavior:
'smooth'})`, which animates over a few hundred ms — an `--eval` or
`--screenshot` fired immediately after `--click` on a tab can catch the page
mid-scroll and read the wrong pane as active. This looks exactly like a real
click-handling bug (confirmed once, chasing what turned out to be nothing);
before concluding a tab click is broken, add a short settle delay first:
`--eval "new Promise(r => setTimeout(r, 500))"`.

### Something the CLI can't express

Write a short one-off script following the same shape (`sync_playwright()`,
launch chromium, `goto`, act, assert, close) rather than growing the CLI for
a single use:

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page()
    page.goto("http://localhost:18080/kb")
    # ... whatever this one check needs ...
    browser.close()
```

## Error Handling

- Stack never becomes healthy: read the app logs before assuming the browser
  side is the problem —
  `docker compose -p memory-agent-test -f docker-compose.yml -f tests/compose.test.yml logs app | tail -50`.
- A selector never appears: run with `--console` first — a JS error mid-render
  (a thrown exception in `app.js`) looks identical to "the selector just isn't
  there yet" until you see the stack trace. Then confirm what's actually in the
  DOM (e.g. `--eval "document.querySelector('nav').outerHTML"`) before
  concluding the app is broken — a bug in the check script looks identical to a
  real regression.
- Whatever happens, still run the teardown command — a failed check must not
  leave the stack running for the next one to collide with.

## Notes

- This only proves what the DOM/JS actually does, not visual polish — treat a
  screenshot as a sanity check on layout, and `--eval` assertions as the real
  verification.
- `static/` is bind-mounted into the container by `docker-compose.override.yml`
  for a bare `docker compose up`, but the isolated test stack here is built
  from the image via `-f docker-compose.yml -f tests/compose.test.yml`
  (no override file), so a JS/CSS edit needs the `up -d --build` in Step 1 to
  actually reach the running container — there is no live-reload here.
- **A correct `--device` check does not replace tapping the real, deployed
  app on the real phone.** It proves the DOM/CSS/JS logic is right for that
  viewport and engine; it cannot reproduce behavior that depends on genuine
  browser chrome or a compositor — a real toolbar resizing the visual
  viewport mid-scroll, or real touch-to-click timing on a snap-scrolling
  carousel. It also cannot prove a real phone will actually *see* a fix: a
  session where "the isolated stack is correct" and "a user's phone still
  shows the bug" were both true at once turned out to be a stale browser
  cache on the phone, not a code defect — the isolated stack always builds
  fresh, so it structurally cannot reproduce "a browser that already cached
  the old files." When a real-device report doesn't match what this skill
  shows, ask whether the phone has ever visited this app before and suggest
  a hard-refresh or a private tab before concluding the code is still wrong.
