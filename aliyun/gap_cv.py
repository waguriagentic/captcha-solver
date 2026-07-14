"""CV gap detector for Aliyun slide-puzzle — cv2 Sobel-x gradient template match.

The gap in back.png is the piece-shaped cut whose vertical edges show up strongly in a
Sobel-x gradient map. Build a template = Sobel-x of the piece silhouette (from the
shadow's alpha channel) and matchTemplate it against the Sobel-x of the back image,
restricted to the piece's y-band. Best-scoring x = gap left edge.

Pure CPU (opencv + numpy), ~5-20ms, no server load, no LLM, no network. Accuracy ground
truth is the server verdict (T001), not vision.
"""
import cv2
import numpy as np


def detect_gap_x(back_bytes: bytes, shadow_bytes: bytes) -> dict:
    """Detect the gap left-edge x. Prefer the trained YOLO ONNX model (mean ~few px,
    robust to busy backgrounds); fall back to the cv2 template matcher when best.onnx
    is absent or ORT is unavailable. Both return the same {gap_x, piece_w, ...} contract."""
    try:
        from .gap_yolo import detect_gap_yolo
        y = detect_gap_yolo(back_bytes, shadow_bytes)
        if y is not None:
            # backfill piece geometry from the shadow alpha (exact) for the drag math
            try:
                sh = cv2.imdecode(np.frombuffer(shadow_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
                alpha = sh[:, :, 3] if (sh.ndim == 3 and sh.shape[2] == 4) else \
                    (cv2.cvtColor(sh, cv2.COLOR_BGR2GRAY) > 10).astype(np.uint8) * 255
                xs = np.where(alpha > 30)[1]
                x0, x1 = int(xs.min()), int(xs.max())
                y.setdefault("piece_x0", x0)
                y["piece_w"] = x1 - x0            # trust exact alpha width over the box
                y["slide_dist"] = y["gap_x"] - x0
            except Exception:
                pass
            return y
    except Exception:
        pass
    return _detect_gap_cv(back_bytes, shadow_bytes)


def _detect_gap_cv(back_bytes: bytes, shadow_bytes: bytes) -> dict:
    back = cv2.imdecode(np.frombuffer(back_bytes, np.uint8), cv2.IMREAD_COLOR)
    sh = cv2.imdecode(np.frombuffer(shadow_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if sh.ndim == 3 and sh.shape[2] == 4:
        alpha = sh[:, :, 3]
    else:
        alpha = (cv2.cvtColor(sh, cv2.COLOR_BGR2GRAY) > 10).astype(np.uint8) * 255
    ys, xs = np.where(alpha > 30)
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())
    pw = x1 - x0

    gray = cv2.cvtColor(back, cv2.COLOR_BGR2GRAY)
    gx = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3))
    shp = (alpha[y0:y1 + 1, x0:x1 + 1] > 30).astype(np.float32) * 255
    tgx = np.abs(cv2.Sobel(shp, cv2.CV_32F, 1, 0, ksize=3))
    band = gx[y0:y1 + 1, :].astype(np.float32)

    res = cv2.matchTemplate(band, tgx, cv2.TM_CCOEFF_NORMED)
    res[:, :x0 + pw] = -1              # gap is right of the piece
    _, maxv, _, maxloc = cv2.minMaxLoc(res)
    gap_x = int(maxloc[0])

    return {"gap_x": gap_x, "piece_x0": x0, "piece_w": pw,
            "back_w": int(back.shape[1]), "slide_dist": gap_x - x0,
            "confidence": round(float(maxv), 3)}
