"""Solve the hCaptcha IMAGE challenge with a vision model using numbered-grid classification.

hCaptcha renders images on a <canvas>. Instead of slicing the canvas into tiles and
classifying each tile individually (which fails on reasoning-style challenges like
"click the flower the bee never lands on"), we overlay a numbered grid on the full
canvas and ask the vision model: "which cell number?" This turns an expensive
grounding problem into a simple classification problem — the VLM chooses from N²
labels instead of guessing pixel coordinates, and it costs ONE API call per page
instead of 16.

Grid-overlay also enables drag challenges: ask for two cells (source, target) and
execute a programmatic drag via page.mouse.
"""
import asyncio
import base64
import io
import logging
import re

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

_GRID = 4
_SKIP_SEL = ".button-submit"


# Extracted so the challenge-detection JS is assertable offline. Keep it a NON-RAW
# string: the drag boundary must stay `\\b` (a bare \b is Python backspace 0x08) and the
# '\\n' delimiter must stay literal, else the regex and newline-split silently break.
_CHALLENGE_META_JS = """() => {
        const txt = document.body.innerText;
        const lines = txt.split('\\n').filter(l => l.trim());
        const task = lines.find(l =>
            !l.includes('try again') && !l.match(/^(Skip|Verify|Next|EN)$/) && l.length > 5
        ) || '';
        const c = document.querySelector('canvas');
        const r = c ? c.getBoundingClientRect() : null;
        const hasDrag = /^drag\\b/i.test(task) || /help the (creature|monkey|robot|character)/i.test(task);
        const btn = document.querySelector('.button-submit');
        return {
            target: task,
            canvasRect: r ? {x: r.x, y: r.y, w: c.width, h: c.height, cssW: r.width, cssH: r.height} : null,
            isDrag: hasDrag,
            buttonText: btn ? btn.innerText.trim() : '',
        };
    }"""


async def _challenge_meta(fr) -> dict:
    """Extract challenge task text + detect layout."""
    return await fr.evaluate(_CHALLENGE_META_JS)


async def _get_canvas_b64(fr) -> str:
    """Get the full canvas as a base64-encoded PNG data URL."""
    return await fr.evaluate("""() => {
        const c = document.querySelector('canvas');
        if (!c) return '';
        return c.toDataURL('image/png').split(',')[1];
    }""")


def _grid_overlay(b64: str, grid: int = _GRID) -> str:
    """Overlay a numbered grid on a base64 PNG, return base64 PNG.

    Each cell is labelled with its index (0..grid²-1) in a yellow box.
    Grid lines in red. Turns pixel-grounding into cell-classification.
    """
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    w, h = img.size
    cw, ch = w // grid, h // grid
    draw = ImageDraw.Draw(img)
    n = 0
    for r in range(grid):
        for c in range(grid):
            x0, y0 = c * cw, r * ch
            draw.rectangle([x0, y0, x0 + cw, y0 + ch], outline="red", width=3)
            tx, ty = x0 + 6, y0 + 6
            draw.rectangle([tx - 2, ty - 2, tx + 38, ty + 26], fill="yellow")
            draw.text((tx, ty), str(n), fill="black")
            n += 1
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


async def _pick_cells(fr, keypool, target: str, grid: int = _GRID) -> list[int]:
    """Ask the vision model which numbered-grid cells satisfy *target*.

    ONE model call per page. The numbered grid turns grounding into
    classification — VLMs handle this far more accurately than pixel
    coordinates. Returns deduplicated cell indices or [].
    """
    b64 = await _get_canvas_b64(fr)
    if not b64:
        return []
    gridded = _grid_overlay(b64, grid)
    N = grid * grid
    prompt = (
        f"This image has a {grid}x{grid} numbered grid (0..{N - 1}, yellow label "
        f"top-left of each cell). Task: \"{target}\". "
        f"Reply ONLY the cell number(s) that satisfy the task, comma-separated "
        f"(e.g. `3` or `1,4,9`), or `none`."
    )
    text = await asyncio.to_thread(keypool.ask, gridded, prompt)
    log.info("_pick_cells(%r) -> %s", target, text[:120])
    nums = []
    for m in re.finditer(r"\d+", text):
        v = int(m.group())
        if 0 <= v < N:
            nums.append(v)
    seen = set()
    return [x for x in nums if not (x in seen or seen.add(x))]




async def _classify_tiles(fr, keypool, target: str) -> list[int]:
    """DEPRECATED — use _pick_cells instead."""
    return await _pick_cells(fr, keypool, target)


async def _click_tiles(page, fr, indices: list[int]) -> None:
    """Click matching tile positions on the canvas via page.mouse.

    page.mouse.click dispatches real OS-level events that hCaptcha respects.
    """
    cbox = await fr.locator("canvas").bounding_box()
    if not cbox:
        return
    tw = cbox["width"] / _GRID
    th = cbox["height"] / _GRID
    for idx in indices:
        col = idx % _GRID
        row = idx // _GRID
        x = cbox["x"] + (col + 0.5) * tw
        y = cbox["y"] + (row + 0.5) * th
        try:
            await page.mouse.click(x, y)
            await asyncio.sleep(0.3)
        except Exception as e:
            log.debug("tile %d click: %s", idx, str(e).splitlines()[0])


def _cell_center(cbox: dict, idx: int, grid: int = _GRID) -> tuple[float, float]:
    """Return (x, y) center pixel of cell `idx` within the canvas bounding box."""
    tw = cbox["width"] / grid
    th = cbox["height"] / grid
    col = idx % grid
    row = idx // grid
    return (cbox["x"] + (col + 0.5) * tw, cbox["y"] + (row + 0.5) * th)


async def _solve_drag(fr, page, keypool) -> bool:
    """Solve a drag challenge: identify source and target cells, then drag.

    Uses the same numbered-grid approach as _pick_cells but asks for TWO
    cells (source=where to grab, target=where to drop). Executes a
    programmatic drag (move, down, animate steps, up) via page.mouse.

    ponytail: best-effort — drag adversarial challenges may still fail.
    Upgrade path: mouse.drag_and_drop or target-specific coordinate tuning.
    """
    b64 = await _get_canvas_b64(fr)
    if not b64:
        return False
    gridded = _grid_overlay(b64, _GRID)
    prompt = (
        f"This image has a {_GRID}x{_GRID} numbered grid (0..{_GRID * _GRID - 1}, "
        f"yellow label). The task requires dragging one cell to another. "
        f"Reply ONLY two cell numbers: `source,target` — the block to drag, "
        f"then where to drop it (e.g. `4,10`)."
    )
    text = await asyncio.to_thread(keypool.ask, gridded, prompt)
    log.info("_solve_drag -> %s", text[:100])
    nums = re.findall(r"\d+", text)
    if len(nums) < 2:
        log.warning("_solve_drag: couldn't parse source/target from %r", text[:80])
        return False
    src, tgt = int(nums[0]), int(nums[-1])
    cbox = await fr.locator("canvas").bounding_box()
    if not cbox:
        return False
    sx, sy = _cell_center(cbox, src)
    tx, ty = _cell_center(cbox, tgt)
    try:
        await page.mouse.move(sx, sy)
        await asyncio.sleep(0.2)
        await page.mouse.down()
        # animate drag in ~10 smooth steps
        steps = 10
        for i in range(1, steps + 1):
            fx = sx + (tx - sx) * i / steps
            fy = sy + (ty - sy) * i / steps
            await page.mouse.move(fx, fy)
            await asyncio.sleep(0.05)
        await page.mouse.up()
        await asyncio.sleep(1)
        log.info("_solve_drag: %d -> %d done", src, tgt)
        return True
    except Exception as e:
        log.warning("_solve_drag failed: %s", str(e).splitlines()[0])
        return False


async def _click_submit(fr) -> bool:
    """Click the submit button (Skip/Next/Verify). Returns True if clicked."""
    try:
        btn = await fr.query_selector(_SKIP_SEL)
        if btn:
            text = await btn.inner_text()
            log.info("submit button: %r", text)
            await btn.click(timeout=5000)
            await asyncio.sleep(2)
            return True
    except Exception as e:
        log.warning("submit click: %s", str(e).splitlines()[0])
    return False


async def solve_hcaptcha_challenge(fr, page, keypool, max_pages: int = 5) -> bool:
    """Run the hCaptcha image challenge through all pages to completion.

    Returns True if Verify was pressed (caller checks for token).
    """
    meta = await _challenge_meta(fr)
    if not meta.get("canvasRect"):
        log.info("no challenge canvas found")
        return False
    if meta.get("isDrag"):
        log.warning("drag challenge — best-effort via grid")
        solved = await _solve_drag(fr, page, keypool)
        if not solved:
            await _click_submit(fr)  # Skip if drag fails
        return solved

    target = meta["target"]
    if not target:
        log.info("no challenge target found")
        return False

    log.info("challenge: target=%r btn=%r", target, meta.get("buttonText"))

    for page_num in range(1, max_pages + 1):
        meta = await _challenge_meta(fr)
        target = meta.get("target", "")
        btn_text = meta.get("buttonText", "")

        log.info("page %d/%d: target=%r btn=%r", page_num, max_pages, target, btn_text)

        if btn_text.lower() in ("verify", "verifizieren", "verificar", "vahvista"):
            break  # already on verify page, just click

        # Skip check for this fr before classify (can get stale between pages)
        fr = _find_challenge_frame(page)
        if not fr:
            log.warning("challenge frame lost")
            return False

        # Classify tiles and click matches
        if target:
            positives = await _pick_cells(fr, keypool, target)
            log.info("grid pick -> %s", positives)
            await _click_tiles(page, fr, positives)
        else:
            log.info("no target text on this page — clicking first tiles as guess")
            await _click_tiles(page, fr, list(range(_GRID)))

        await asyncio.sleep(2)

        # Click Next/Verify to advance
        clicked = await _click_submit(fr)
        if not clicked:
            return False
        if btn_text.lower() in ("skip", "跳过", "huppel", "ohita", "überspringen"):
            return False  # user skipped, not solved
        await asyncio.sleep(2)

    # After last page, check for "Verify" button
    fr = _find_challenge_frame(page)
    if fr:
        meta = await _challenge_meta(fr)
        log.info("final state: btn=%r", meta.get("buttonText"))
        if meta.get("buttonText", "").lower() != "skip":
            await _click_submit(fr)

    return True


def _find_challenge_frame(page):
    """Return the hCaptcha challenge frame, or None."""
    for fr in page.frames:
        u = fr.url or ""
        if "#frame=challenge" in u and "hcaptcha" in u:
            return fr
    return None
