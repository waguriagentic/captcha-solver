"""VLM gap detector for Aliyun slide-puzzle — Mistral vision for AIGC backgrounds.

AIGC-generated captcha backgrounds defeat Sobel-x template matching (no sharp
piece-shaped edges), and YOLO was trained on natural images. The VLM can reason
about the puzzle cutout even on synthetic backgrounds. Returns a rough gap_x
that gap_cv.refine_gap_x snaps to the exact edge.

Uses the shared KeyPool from common/mistral.py (same pool as reCAPTCHA/hCaptcha).
Call from async via asyncio.to_thread (KeyPool is sync/stdlib).
"""
import asyncio
import base64
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_KEYFILE = Path(__file__).resolve().parent.parent / "common" / "apikey.txt"
_pool = None


def _get_pool():
    global _pool
    if _pool is not None:
        return _pool
    if not _KEYFILE.exists():
        log.debug("gap_vlm: %s not found, VLM disabled", _KEYFILE)
        return None
    try:
        from common.mistral import KeyPool
        _pool = KeyPool(str(_KEYFILE), start_index=0)
        return _pool
    except Exception as e:
        log.debug("gap_vlm: KeyPool init failed: %s", e)
        return None


def _detect_vlm_sync(back_bytes: bytes, shadow_bytes: bytes) -> dict | None:
    pool = _get_pool()
    if pool is None:
        return None
    img_b64 = base64.b64encode(back_bytes).decode()
    prompt = (
        "This is a slide-puzzle captcha background image. A puzzle piece has been "
        "cut out, leaving a visible gap/notch. Estimate the X coordinate (in pixels, "
        "from the left edge) of the LEFT edge of the gap. "
        "Reply with ONLY a number, nothing else."
    )
    resp = pool.ask(img_b64, prompt, max_keys=6, timeout=30, max_tokens=16)
    if not resp:
        return None
    m = re.search(r"\d+", resp)
    if not m:
        return None
    gap_x = int(m.group())
    if gap_x < 20 or gap_x > 400:  # sanity bounds for 300px widget
        return None
    return {"gap_x": gap_x, "method": "vlm"}


def detect_gap_vlm(back_bytes: bytes, shadow_bytes: bytes) -> dict | None:
    """Sync entry point. Returns {gap_x, method} or None.

    Caller wraps in asyncio.to_thread if needed — gap_cv calls this synchronously.
    """
    try:
        return _detect_vlm_sync(back_bytes, shadow_bytes)
    except Exception as e:
        log.debug("gap_vlm: %s", e)
        return None
