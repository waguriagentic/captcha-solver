"""Mistral vision key pool — round-robin + auto-failover over many API keys.

Shared by the reCAPTCHA and hCaptcha image solvers. Exposes `classify` (yes/no),
`classify_custom`, and `ask` (free-form, e.g. numbered-grid cell picks).

`apikey.txt` holds thousands of keys; the pool starts at a caller-chosen offset,
rotates round-robin, and on a per-key failure (401/403/429) parks the key for a
wall-clock cooldown and retries the SAME request on the next key.

Sync + stdlib only (urllib) — call from async via asyncio.to_thread.
"""
import itertools
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

_ENDPOINT = "https://api.mistral.ai/v1/chat/completions"
# pixtral-12b aliases to a text model on this gateway; medium is the cheapest id
# that actually does vision here.
_DEFAULT_MODEL = "mistral-medium-latest"
# Per-key failures worth rotating past; 5xx is transient (retry same key once).
_ROTATE_STATUS = {401, 403, 429}
_COOLDOWN_S = 60  # wall-clock park duration for a failed key


class KeyPool:
    def __init__(self, keyfile: str, model: str = _DEFAULT_MODEL,
                 start_index: int = 0):
        keys = [k.strip() for k in Path(keyfile).read_text().splitlines() if k.strip()]
        # dedupe, preserve order
        seen = set()
        self.keys = [k for k in keys if not (k in seen or seen.add(k))]
        if not self.keys:
            raise ValueError(f"no keys in {keyfile}")
        self.model = model
        self._lock = threading.Lock()
        # round-robin cursor, started at a caller-chosen offset to spread load
        n = len(self.keys)
        self._cursor = itertools.cycle(
            self.keys[start_index % n:] + self.keys[:start_index % n])
        self._dead: dict[str, float] = {}   # key -> monotonic time it's live again

    def _next_live_key(self) -> str:
        with self._lock:
            now = time.monotonic()
            for _ in range(len(self.keys)):
                k = next(self._cursor)
                if self._dead.get(k, 0.0) <= now:
                    return k
            # all parked — clear cooldowns and take the next
            self._dead.clear()
            return next(self._cursor)

    def _park(self, key: str, cooldown: float = _COOLDOWN_S):
        with self._lock:
            self._dead[key] = time.monotonic() + cooldown

    def _call(self, image_b64: str, prompt: str,
              max_keys: int, timeout: int, max_tokens: int) -> str:
        """Call Mistral vision, return raw response string (lowercased, stripped).

        Rotates keys on 401/403/429 (parks dead) and 5xx (transient).
        Returns '' on total failure.
        """
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": f"data:image/png;base64,{image_b64}"}]}],
            "max_tokens": max_tokens, "temperature": 0,
        }).encode()

        last_err = None
        for _ in range(min(max_keys, len(self.keys))):
            key = self._next_live_key()
            req = urllib.request.Request(
                _ENDPOINT, data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + key})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read())
                txt = data["choices"][0]["message"]["content"]
                if isinstance(txt, list):
                    txt = ' '.join(p.get('text', '') for p in txt if isinstance(p, dict))
                return txt.strip().lower()
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
                if e.code in _ROTATE_STATUS:
                    self._park(key)
                    continue
                if 500 <= e.code < 600:
                    continue  # transient, try next key
                break  # 4xx we don't understand — stop
            except Exception as e:  # timeout, JSON, network
                last_err = str(e).splitlines()[0]
                continue
        log.warning("_call failed for %r after key rotation: %s", prompt[:60], last_err)
        return ''

    def _classify_with_prompt(self, image_b64: str, prompt: str,
                               max_keys: int, timeout: int) -> bool:
        return self._call(image_b64, prompt, max_keys, timeout, 8).startswith("y")

    def classify(self, image_b64: str, target: str,
                 max_keys: int = 8, timeout: int = 40) -> bool:
        """Yes/no: does this tile contain `target`? Rotates keys on failure.

        Returns False if every tried key fails (caller treats as 'not a match').
        """
        prompt = (f'Does this image clearly contain a {target} (or a visible part '
                  f'of one)? Answer ONLY "yes" or "no".')
        return self._classify_with_prompt(image_b64, prompt, max_keys, timeout)

    def classify_custom(self, image_b64: str, prompt: str,
                        max_keys: int = 8, timeout: int = 40) -> bool:
        """Yes/no for a custom prompt. Use when the target is a full instruction."""
        return self._classify_with_prompt(image_b64, prompt, max_keys, timeout)

    def ask(self, image_b64: str, prompt: str,
            max_keys: int = 8, timeout: int = 40, max_tokens: int = 512) -> str:
        """Ask a free-form question, return the model's full response text.

        Use for numbered-grid challenges (classify → cell number) where
        the answer is a token or short phrase, not just yes/no.
        Returns '' on total failure.
        """
        return self._call(image_b64, prompt, max_keys, timeout, max_tokens)


# self-check: pool loads, rotates, and a 1x1 red tile classifies without crashing.
if __name__ == "__main__":
    pool = KeyPool(str(Path(__file__).parent / "apikey.txt"))
    print("keys loaded:", len(pool.keys))
    red = ("iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAYAAACp8Z5+AAAAEUlEQVR42mP8"
           "z8BQz0AEYBxVSFXyW3aBAAAAAElFTkSuQmCC")
    print("classify(red, 'red square') ->", pool.classify(red, "red square"))
