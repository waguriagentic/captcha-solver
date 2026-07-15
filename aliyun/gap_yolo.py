"""
YOLOv8n ONNX gap detector for Aliyun slide-puzzle. CPU inference, THREAD-LIMITED so it
never spikes the box (the default ONNX Runtime grabs every core — for a 320px image on a
nano model that's pure waste; 2 threads run it in ~20-40ms).

Drop-in: gap_cv.detect_gap_x() calls detect_gap_yolo() first when best.onnx is present,
and falls back to the cv2 detector otherwise. Model file lives next to this module.

The YOLO model outputs a box around the gap; we take the highest-confidence box's
left-edge x as gap_x (same contract as the cv2 detector).
"""
import os
from pathlib import Path

import numpy as np

# Hard cap threads BEFORE importing onnxruntime / cv2 so their internal pools respect it.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("ORT_NUM_THREADS", "2")

import cv2  # noqa: E402

_MODEL_PATH = Path(__file__).parent / "best.onnx"
_IMGSZ = 320
_session = None
_load_failed = False


def _get_session():
    """Lazily build a thread-limited ONNX Runtime session. None if no model / no ORT."""
    global _session, _load_failed
    if _session is not None:
        return _session
    if _load_failed or not _MODEL_PATH.exists():
        return None
    try:
        import onnxruntime as ort
        cv2.setNumThreads(2)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2       # <- the anti-spike knob
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        _session = ort.InferenceSession(
            str(_MODEL_PATH), opts, providers=["CPUExecutionProvider"])
        return _session
    except Exception:
        _load_failed = True
        return None


def _letterbox(img, new=_IMGSZ):
    """Resize+pad to a square new×new (YOLO letterbox). Returns padded img + (scale, dx, dy)."""
    h, w = img.shape[:2]
    r = min(new / h, new / w)
    nh, nw = int(round(h * r)), int(round(w * r))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((new, new, 3), 114, dtype=np.uint8)
    dx, dy = (new - nw) // 2, (new - nh) // 2
    canvas[dy:dy + nh, dx:dx + nw] = resized
    return canvas, r, dx, dy


def detect_gap_yolo(back_bytes: bytes, shadow_bytes: bytes = None):
    """Return {gap_x, piece_w, back_w, confidence, method} or None if model unavailable."""
    sess = _get_session()
    if sess is None:
        return None
    if not back_bytes:
        return None
    arr = np.frombuffer(back_bytes, np.uint8)
    back = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    H, W = back.shape[:2]
    lb, r, dx, dy = _letterbox(back)
    blob = lb[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0

    out = sess.run(None, {sess.get_inputs()[0].name: blob})[0]  # (1,5,N) or (1,N,5/6)
    pred = np.squeeze(out)
    if pred.ndim != 2:
        return None
    # normalize orientation to (N, >=5): [cx, cy, w, h, conf...]
    if pred.shape[0] in (5, 6) and pred.shape[1] != 5 and pred.shape[1] != 6:
        pred = pred.T
    if pred.shape[1] < 5:
        return None
    conf = pred[:, 4]
    i = int(np.argmax(conf))
    if conf[i] < 0.25:
        return None
    cx, cy, bw, bh = pred[i, :4]
    # undo letterbox -> original pixels
    x_left = (cx - bw / 2 - dx) / r
    piece_w = bw / r
    gap_x = int(round(max(0, min(x_left, W - 1))))
    return {"gap_x": gap_x, "piece_w": int(round(piece_w)), "back_w": W,
            "confidence": round(float(conf[i]), 3), "method": "yolo-onnx"}
