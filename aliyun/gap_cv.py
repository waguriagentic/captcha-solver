"""CV gap detector for Aliyun slide-puzzle — cv2 Sobel-x gradient template match.

The gap in back.png is the piece-shaped cut whose vertical edges show up strongly in a
Sobel-x gradient map. Build a template = Sobel-x of the piece silhouette (from the
shadow's alpha channel) and matchTemplate it against the Sobel-x of the back image,
restricted to the piece's y-band. Best-scoring x = gap left edge.

Detection order: YOLO ONNX → VLM + cv2 refine → pure cv2. Each stage can fail
gracefully; the contract is always {gap_x, piece_w, ...}.

Pure CPU (opencv + numpy), ~5-20ms, no server load, no LLM, no network. Accuracy ground
truth is the server verdict (T001), not vision.
"""
import cv2
import numpy as np


def _piece_geometry(shadow_bytes: bytes) -> dict | None:
    """Extract piece bbox from shadow alpha. Returns {y0,y1,x0,x1,pw,alpha} or None."""
    if not shadow_bytes:
        return None
    sh = cv2.imdecode(np.frombuffer(shadow_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if sh is None:
        return None
    if sh.ndim == 3 and sh.shape[2] == 4:
        alpha = sh[:, :, 3]
    else:
        alpha = (cv2.cvtColor(sh, cv2.COLOR_BGR2GRAY) > 10).astype(np.uint8) * 255
    ys, xs = np.where(alpha > 30)
    if len(xs) == 0:
        return None
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())
    return {"y0": y0, "y1": y1, "x0": x0, "x1": x1, "pw": x1 - x0, "alpha": alpha}


def _piece_band(alpha: np.ndarray, y0: int, y1: int, x0: int, x1: int):
    """Extract the piece silhouette template restricted to its y-band."""
    band = alpha[y0:y1 + 1, x0:x1 + 1]
    return (band > 30).astype(np.float32) * 255


def refine_gap_x(back_bytes: bytes, shadow_bytes: bytes, rough_x: int,
                 search_radius: int = 15) -> dict:
    """Refine a rough gap_x estimate (from VLM or YOLO) using cv2 template matching
    in a narrow ±search_radius window. Much more reliable than full-width matching
    for AIGC backgrounds where the global best match is often a false positive."""
    geo = _piece_geometry(shadow_bytes)
    if geo is None:
        return {"gap_x": rough_x, "method": "refine-noop"}
    if not back_bytes:
        return {"gap_x": rough_x, "method": "refine-noop"}
    back = cv2.imdecode(np.frombuffer(back_bytes, np.uint8), cv2.IMREAD_COLOR)
    if back is None:
        return {"gap_x": rough_x, "method": "refine-noop"}
    y0, y1, x0, pw = geo["y0"], geo["y1"], geo["x0"], geo["pw"]
    alpha = geo["alpha"]

    gray = cv2.cvtColor(back, cv2.COLOR_BGR2GRAY)
    gx = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3))
    shp = _piece_band(alpha, y0, y1, x0, x0 + pw)
    tgx = np.abs(cv2.Sobel(shp, cv2.CV_32F, 1, 0, ksize=3))
    band = gx[y0:y1 + 1, :].astype(np.float32)

    # Restrict search to ±search_radius around the rough estimate
    lo = max(0, rough_x - search_radius)
    hi = min(band.shape[1] - tgx.shape[1], rough_x + search_radius)
    if lo >= hi:
        return {"gap_x": rough_x, "method": "refine-noop"}
    sub = band[:, lo:hi + tgx.shape[1]]
    res = cv2.matchTemplate(sub, tgx, cv2.TM_CCOEFF_NORMED)
    if res.size == 0:
        return {"gap_x": rough_x, "method": "refine-noop"}
    _, maxv, _, maxloc = cv2.minMaxLoc(res)
    gap_x = lo + int(maxloc[0])
    return {"gap_x": gap_x, "piece_x0": x0, "piece_w": pw,
            "back_w": int(back.shape[1]), "slide_dist": gap_x - x0,
            "confidence": round(float(maxv), 3), "method": "vlm-refine"}


def detect_gap_x(back_bytes: bytes, shadow_bytes: bytes) -> dict:
    """Detect the gap left-edge x. Detection order:
    1. YOLO ONNX (fast, trained, ~50% single-shot)
    2. VLM (Mistral vision) + cv2 refine (handles AIGC backgrounds)
    3. cv2 Sobel-x template match (fallback, no deps)
    """
    # --- Stage 1: YOLO ---
    try:
        from .gap_yolo import detect_gap_yolo
        y = detect_gap_yolo(back_bytes, shadow_bytes)
        if y is not None:
            geo = _piece_geometry(shadow_bytes)
            if geo:
                y.setdefault("piece_x0", geo["x0"])
                y["piece_w"] = geo["pw"]
                y["slide_dist"] = y["gap_x"] - geo["x0"]
            return y
    except Exception:
        pass

    # --- Stage 2: VLM + cv2 refine ---
    try:
        from .gap_vlm import detect_gap_vlm
        vlm = detect_gap_vlm(back_bytes, shadow_bytes)
        if vlm and vlm.get("gap_x") is not None:
            refined = refine_gap_x(back_bytes, shadow_bytes, vlm["gap_x"])
            refined["vlm_raw"] = vlm.get("gap_x")
            return refined
    except Exception:
        pass

    # --- Stage 3: cv2 ---
    return _detect_gap_cv(back_bytes, shadow_bytes)


def _detect_gap_cv(back_bytes: bytes, shadow_bytes: bytes) -> dict:
    geo = _piece_geometry(shadow_bytes)
    if geo is None:
        return {"gap_x": 0, "piece_w": 0, "method": "cv2-fail"}
    if not back_bytes:
        return {"gap_x": 0, "piece_w": 0, "method": "cv2-fail"}
    back = cv2.imdecode(np.frombuffer(back_bytes, np.uint8), cv2.IMREAD_COLOR)
    if back is None:
        return {"gap_x": 0, "piece_w": 0, "method": "cv2-fail"}
    y0, y1, x0, pw = geo["y0"], geo["y1"], geo["x0"], geo["pw"]
    alpha = geo["alpha"]

    gray = cv2.cvtColor(back, cv2.COLOR_BGR2GRAY)
    gx = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3))
    shp = _piece_band(alpha, y0, y1, x0, x0 + pw)
    tgx = np.abs(cv2.Sobel(shp, cv2.CV_32F, 1, 0, ksize=3))
    band = gx[y0:y1 + 1, :].astype(np.float32)

    res = cv2.matchTemplate(band, tgx, cv2.TM_CCOEFF_NORMED)
    res[:, :x0 + pw] = -1              # gap is right of the piece
    _, maxv, _, maxloc = cv2.minMaxLoc(res)
    gap_x = int(maxloc[0])

    return {"gap_x": gap_x, "piece_x0": x0, "piece_w": pw,
            "back_w": int(back.shape[1]), "slide_dist": gap_x - x0,
            "confidence": round(float(maxv), 3), "method": "cv2-sobel"}
