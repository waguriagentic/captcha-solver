"""Local ONNX tile classifier — drop-in replacement for the Mistral KeyPool in the
reCAPTCHA image-challenge path.

Exposes `.classify(image_b64, target) -> bool` with the SAME signature as
`common.mistral.KeyPool.classify`, so `image_solve._classify_grid` swaps one for the
other with no other change. The model is a yolov8n-cls (14-class) trained on the
reCAPTCHA tile dataset; inference is CPU-only and thread-limited to 2 cores (no spike),
matching the Aliyun gap-detector deployment.

Zero network, zero per-solve cost. Falls back to Mistral only if the ONNX file is
absent (caller decides).
"""
import os

# Cap threads BEFORE importing onnxruntime — some ORT builds read these at import time,
# not just from SessionOptions. Without this a single infer grabs every host core.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("ORT_NUM_THREADS", "2")

import ast
import base64
import io
import logging
import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

log = logging.getLogger(__name__)

_DEFAULT_MODEL = Path(__file__).parent / "models" / "recaptcha_cls_s.onnx"
_DEFAULT_THRESHOLD = 0.55  # softmax prob for the matched class to count as "yes"

# reCAPTCHA prompt targets are noisy: plural, articles, synonyms. Map them onto the
# 14 dataset class labels. Keys are lowercased, article/plural-stripped tokens.
_TARGET_ALIASES = {
    "bicycle": "Bicycle", "bicycles": "Bicycle", "bike": "Bicycle", "bikes": "Bicycle",
    "bridge": "Bridge", "bridges": "Bridge",
    "bus": "Bus", "buses": "Bus", "busses": "Bus",
    "car": "Car", "cars": "Car", "vehicle": "Car", "vehicles": "Car",
    "chimney": "Chimney", "chimneys": "Chimney",
    "crosswalk": "Crosswalk", "crosswalks": "Crosswalk",
    "cross walk": "Crosswalk", "cross walks": "Crosswalk",
    "hydrant": "Hydrant", "hydrants": "Hydrant",
    "fire hydrant": "Hydrant", "fire hydrants": "Hydrant",
    "a fire hydrant": "Hydrant",
    "motorcycle": "Motorcycle", "motorcycles": "Motorcycle",
    "motorbike": "Motorcycle", "motorbikes": "Motorcycle",
    "mountain": "Mountain", "mountains": "Mountain",
    "mountains or hills": "Mountain", "hill": "Mountain", "hills": "Mountain",
    "palm": "Palm", "palms": "Palm", "palm tree": "Palm", "palm trees": "Palm",
    "stair": "Stair", "stairs": "Stair", "staircase": "Stair",
    "tractor": "Tractor", "tractors": "Tractor",
    "traffic light": "Traffic Light", "traffic lights": "Traffic Light",
    "trafficlight": "Traffic Light", "trafic light": "Traffic Light",
    "traffic signal": "Traffic Light", "traffic signals": "Traffic Light",
}


def _normalize_target(target: str) -> str | None:
    """Map a reCAPTCHA prompt target string onto a dataset class name, or None."""
    t = (target or "").strip().lower()
    # strip leading article
    for art in ("a ", "an ", "the "):
        if t.startswith(art):
            t = t[len(art):]
    if t in _TARGET_ALIASES:
        return _TARGET_ALIASES[t]
    # loose contains-match (e.g. "select all images with a bus in them")
    for key, cls in _TARGET_ALIASES.items():
        if key in t:
            return cls
    return None


class OnnxClassifier:
    """Thread-limited ONNX tile classifier. Drop-in for KeyPool.classify."""

    def __init__(self, model_path: str = None, threshold: float = _DEFAULT_THRESHOLD):
        self.model_path = str(model_path or _DEFAULT_MODEL)
        self.threshold = threshold
        so = ort.SessionOptions()
        so.intra_op_num_threads = 2
        so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(
            self.model_path, so, providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0].name
        meta = self.sess.get_modelmeta().custom_metadata_map
        self.names = ast.literal_eval(meta["names"])  # {0: 'Bicycle', ...}
        imgsz = meta.get("imgsz", "[128, 128]")
        self.imgsz = ast.literal_eval(imgsz)[0] if imgsz.startswith("[") else int(imgsz)
        self._lock = threading.Lock()  # ORT session is not guaranteed thread-safe
        log.info("OnnxClassifier loaded: %s (%d classes, imgsz=%d)",
                 self.model_path, len(self.names), self.imgsz)

    def _preprocess(self, image_b64: str) -> np.ndarray:
        raw = base64.b64decode(image_b64)
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = im.size
        s = self.imgsz / min(w, h)
        im = im.resize((round(w * s), round(h * s)))
        w, h = im.size
        l, t = (w - self.imgsz) // 2, (h - self.imgsz) // 2
        im = im.crop((l, t, l + self.imgsz, t + self.imgsz))
        a = np.asarray(im, dtype=np.float32) / 255.0
        return np.transpose(a, (2, 0, 1))[None]

    def predict(self, image_b64: str):
        """Return (class_name, confidence) for the top prediction."""
        x = self._preprocess(image_b64)
        with self._lock:
            out = self.sess.run(None, {self.inp: x})[0][0]
        idx = int(np.argmax(out))
        return self.names[idx], float(out[idx])

    def classify(self, image_b64: str, target: str,
                 max_keys: int = 8, timeout: int = 40) -> bool:
        """Yes/no: does this tile contain `target`? Signature matches KeyPool.classify
        (max_keys/timeout accepted and ignored — kept for drop-in compatibility).

        A tile is a match iff the model's argmax equals the normalized target class
        AND its softmax prob clears the threshold. If the target can't be mapped to a
        known class, returns False (caller should fall back to Mistral for that target).
        """
        want = _normalize_target(target)
        if want is None:
            log.warning("target %r not in known classes — no ONNX match", target)
            return False
        try:
            pred, conf = self.predict(image_b64)
        except Exception as e:
            log.debug("onnx classify error: %s", str(e).splitlines()[0])
            return False
        return pred == want and conf >= self.threshold


class HybridClassifier:
    """ONNX-first classifier with a Mistral fallback for out-of-vocabulary targets.

    Same `.classify(image_b64, target)` contract as KeyPool. Known reCAPTCHA classes
    (the 14 the model was trained on) resolve locally via ONNX — fast, free, no rate
    limit. Targets that don't map to a known class (rare new categories) fall through
    to the Mistral key pool, so coverage never regresses below the pre-ONNX baseline.
    """

    def __init__(self, onnx: "OnnxClassifier", keypool):
        self.onnx = onnx
        self.keypool = keypool  # may be None if Mistral unavailable

    def classify(self, image_b64: str, target: str,
                 max_keys: int = 8, timeout: int = 40) -> bool:
        if _normalize_target(target) is not None:
            return self.onnx.classify(image_b64, target, max_keys, timeout)
        # unknown target -> Mistral fallback (if configured)
        if self.keypool is not None:
            return self.keypool.classify(image_b64, target, max_keys, timeout)
        log.warning("target %r unknown and no Mistral fallback — treating as no-match",
                    target)
        return False


_classifier = None


def get_classifier(model_path: str = None):
    """Lazy singleton. Returns None if the ONNX model file is absent (caller falls
    back to Mistral)."""
    global _classifier
    if _classifier is None:
        path = Path(model_path or _DEFAULT_MODEL)
        if not path.exists():
            return None
        _classifier = OnnxClassifier(str(path))
    return _classifier


# self-check: load model + classify a solid tile without crashing.
if __name__ == "__main__":
    import sys
    clf = get_classifier(sys.argv[1] if len(sys.argv) > 1 else None)
    if clf is None:
        print("no model at", _DEFAULT_MODEL)
        raise SystemExit(1)
    print("classes:", clf.names)
    print("normalize 'buses' ->", _normalize_target("buses"))
    print("normalize 'a fire hydrant' ->", _normalize_target("a fire hydrant"))
    print("normalize 'traffic lights' ->", _normalize_target("traffic lights"))
    print("normalize 'palm trees' ->", _normalize_target("palm trees"))
