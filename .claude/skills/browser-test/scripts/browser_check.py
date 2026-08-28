#!/usr/bin/env python3
"""Drive a headless browser against a running page and report what happened.

A small, generic CLI rather than a bespoke script per check: navigate, wait
for something to appear, click things (by CSS selector or visible text),
evaluate JS expressions and print their results, then optionally screenshot.
Actions run in the order given on the command line.

Examples:

    python3 browser_check.py http://localhost:18080/kb \\
        --wait-for .section-header \\
        --eval "document.querySelectorAll('.dir-children.collapsed').length" \\
        --screenshot /tmp/tree-collapsed.png

    python3 browser_check.py http://localhost:18080/kb \\
        --click-text wiki \\
        --eval "document.querySelector('.section-header.active')?.textContent"

    python3 browser_check.py http://localhost:18080/ \\
        --device "Pixel 6 Pro" \\
        --eval "getComputedStyle(document.querySelector('.pane-tabs')).display"

Real-device emulation: --device NAME looks NAME up in Playwright's own device
catalog (`playwright.sync_api.sync_playwright().devices`) and uses its exact
viewport/scale/touch flags instead of --width/--height, and launches whichever
engine that device's own `default_browser_type` names - `chromium` for
Android entries (e.g. "Pixel 6 Pro"), `webkit` for iOS ones (e.g. "iPhone 15"),
since real iPhone Safari runs WebKit, not Blink, and spoofing the user agent
under Chromium would not exercise the same rendering/compositor path. WebKit
is a separate download - if it is missing this fails with a clear message
naming the install command, rather than silently falling back to Chromium.
"""

from __future__ import annotations

import argparse
import json
import sys

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--width", type=int, help="default 1280, ignored with --device")
    parser.add_argument("--height", type=int, help="default 900, ignored with --device")
    parser.add_argument(
        "--device", metavar="NAME",
        help='a Playwright device name, e.g. "Pixel 6 Pro" or "iPhone 15" - '
             "sets viewport/scale/touch and picks the engine for you; "
             "mutually exclusive with --width/--height",
    )
    parser.add_argument("--timeout", type=int, default=10_000, help="ms")
    parser.add_argument(
        "--wait-for", action="append", default=[], metavar="SELECTOR",
        help="wait for a CSS selector to appear (repeatable, in order)",
    )
    parser.add_argument(
        "--click", action="append", default=[], metavar="SELECTOR",
        help="click the first match of a CSS selector (repeatable, in order)",
    )
    parser.add_argument(
        "--click-text", action="append", default=[], metavar="TEXT",
        help="click the first element with this visible text (repeatable, in order)",
    )
    parser.add_argument(
        "--eval", action="append", default=[], dest="evals", metavar="JS",
        help="evaluate a JS expression and print its result (repeatable, in order)",
    )
    parser.add_argument(
        "--screenshot", metavar="PATH", help="final full-page screenshot",
    )
    parser.add_argument(
        "--console", action="store_true",
        help="print browser console messages and page errors as they happen",
    )
    args = parser.parse_args()

    if args.device and (args.width or args.height):
        parser.error("--device already sets viewport/scale/touch: no --width/--height")

    # Actions interleave in command-line order: --wait-for, --click and
    # --click-text are collected into one ordered queue by re-parsing sys.argv,
    # because argparse's separate lists lose the relative order between flags.
    action_flags = ("--wait-for", "--click", "--click-text", "--eval")
    order = [token for token in sys.argv[1:] if token in action_flags]
    queues = {
        "--wait-for": list(args.wait_for),
        "--click": list(args.click),
        "--click-text": list(args.click_text),
        "--eval": list(args.evals),
    }

    with sync_playwright() as pw:
        if args.device:
            try:
                descriptor = pw.devices[args.device]
            except KeyError:
                parser.error(
                    f"no such device {args.device!r} - see playwright.sync_api"
                    f".sync_playwright().devices for the full catalog"
                )
            # The descriptor names its own engine - real iPhone Safari is
            # WebKit, not Blink, and spoofing the user agent under Chromium
            # would silently skip the engine this profile exists to exercise.
            # default_browser_type itself isn't a new_page() kwarg, so it's
            # popped out rather than spread along with the rest.
            new_page_kwargs = dict(descriptor)
            engine_name = new_page_kwargs.pop("default_browser_type")
            engine = getattr(pw, engine_name)
            try:
                browser = engine.launch()
            except Exception as exc:  # re-raised as a clearer, actionable message
                raise SystemExit(
                    f"{engine_name} is not installed - run "
                    f"'.venv/bin/python -m playwright install {engine_name}' "
                    f"once, then retry"
                ) from exc
            page = browser.new_page(**new_page_kwargs)
        else:
            browser = pw.chromium.launch()
            page = browser.new_page(
                viewport={"width": args.width or 1280, "height": args.height or 900}
            )
        if args.console:
            # Attached before goto, or messages logged during the initial
            # load would never reach these handlers.
            page.on(
                "console",
                lambda msg: print(f"console.{msg.type}: {msg.text}"),  # noqa: T201
            )
            page.on("pageerror", lambda exc: print(f"pageerror: {exc}"))  # noqa: T201
        page.goto(args.url, timeout=args.timeout)

        for flag in order:
            value = queues[flag].pop(0)
            if flag == "--wait-for":
                page.wait_for_selector(value, timeout=args.timeout)
            elif flag == "--click":
                page.locator(value).first.click(timeout=args.timeout)
            elif flag == "--click-text":
                page.get_by_text(value, exact=False).first.click(timeout=args.timeout)
            elif flag == "--eval":
                result = page.evaluate(value)
                print(json.dumps(result))  # noqa: T201 - this output IS the tool's result

        if args.screenshot:
            page.screenshot(path=args.screenshot, full_page=True)
            print(f"screenshot: {args.screenshot}")  # noqa: T201 - ditto

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
