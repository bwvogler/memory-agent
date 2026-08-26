#!/usr/bin/env python3
"""Drive headless Chromium against a running page and report what happened.

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
"""

from __future__ import annotations

import argparse
import json
import sys

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=900)
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
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
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
