#!/usr/bin/env python3
"""Break down where a solve spends its time.

Phases: browser launch -> context/page -> route setup -> navigation ->
widget wait (token). If launch dominates, browser pooling is the next win;
if widget wait dominates, nothing more can be shaved client-side.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cloakbrowser

from common.browser import browser_kwargs, route_glob

TEMPLATE = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "turnstile", "template.html")).read()
SITEKEY = "1x00000000000000000000AA"
URL = "https://example.com"

import logging
logging.basicConfig(level=logging.WARNING)


async def timed(label, coro):
    t0 = time.monotonic()
    r = await coro
    print(f"  {label}: {time.monotonic() - t0:.2f}s")
    return r


async def main():
    print("--- solve phase breakdown ---")
    browser = await timed("launch", cloakbrowser.launch_async(**browser_kwargs("TURNSTILE")))
    page = await timed("new_page", browser.new_page())
    page_data = (TEMPLATE
                 .replace("__SITEKEY__", SITEKEY)
                 .replace("__EXTRA__", ""))
    await page.route(route_glob(URL), lambda r: r.fulfill(body=page_data, status=200))
    await timed("goto", page.goto(URL, wait_until="domcontentloaded"))

    # wait for the token
    async def wait_token():
        for _ in range(600):
            tok = await page.evaluate(
                "() => { const el = document.querySelector("
                "'[name=cf-response],input[name=cf-turnstile-response],"
                "textarea[name=cf-turnstile-response]');"
                " return el ? el.value : ''; }")
            if tok:
                return tok
            await asyncio.sleep(0.1)
        return ""

    token = await timed("widget->token", wait_token())
    print(f"  token: {token[:40]!r}")
    await timed("close", browser.close())


asyncio.run(main())
