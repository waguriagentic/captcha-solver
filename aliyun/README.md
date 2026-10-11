# aliyun — Aliyun Captcha 2.0 solver (stub + real-page)

Solver for Aliyun Captcha 2.0 (the slider/INPAINTING challenge used by chat.z.ai
et al). No third party at solve time. Two modes:

| Mode | Request | How it works |
|---|---|---|
| **stub** (default) | `scene_id` + `prefix` | Renders the widget on a minimal self-hosted page, detects the gap, drags the slider along the empirically-fitted **quadratic** handle→piece curve, harvests the token from the widget's own success callback. Optional `post_fetch` runs the caller's redeem calls from the server process. |
| **real_page** | `real_page: true` + `url` | Navigates the REAL target page, drives its own widget, harvests the token, then fires the caller's submit call (`post_fetch`) from inside that same page. Use for scenes that only render in-page. |

## Which mode do I need?

**Stub mode by default.** The token is portable: what a target validates is the
request *shape* at redeem time, not the browser that solved the challenge.

Correction (2026-10-11): an earlier version of this file claimed the flow was
"session-bound" and that real-page was required. That was wrong — the rejections
came from a **missing request header**. chat.z.ai's own frontend patches
`window.fetch` to add `x-region: overseas` to every same-origin call; without it
every redeem returns 400 *even from the same browser session that solved it*.
Measured matrix (live):

| Setup | Solve | Redeem |
|---|---|---|
| Stub at a neutral origin, plain HTTP client, **with the site's header** | ✓ | **✓ 200** |
| Stub at a neutral origin, plain HTTP client, without the header | ✓ | ✗ 400 |
| Stub at target origin + real cookies, same-page submit, without the header | ✓ | ✗ 400 |
| Real page, widget driven in place, same-page submit | ✓ | ✓ 200 |

So the flow is provider-style:

1. Solve on a stub (fast, no target navigation, no cookies).
2. Hand the token to the caller (or let `post_fetch` run it server-side).
3. Redeem with the target's required request headers (`post_fetch[].headers`).

If a target rejects a stub-solved token even with correct headers, check what
its frontend's fetch/XHR wrappers add (open the site's bundle and grep for
`window.fetch=` / `XMLHttpRequest`) — that header is the usual gate. Only when
the widget itself cannot render on a stub (scene bound to in-page context) do
you need `real_page: true`.

## Gap detection — YOLOv8n (with cv2 fallback)

The gap detector is a trained **YOLOv8n** ONNX model (`best.onnx`, 1-class "gap").
It runs on CPU, **thread-limited to 2 cores** (`intra_op_num_threads=2`) so it never
spikes the box. `gap_cv.detect_gap_x()` uses YOLO when `best.onnx` is present and
falls back to the cv2 Sobel-x template matcher otherwise — same return contract.

## Drag — overshoot-and-correct (behavioral, NOT just position)

Aliyun 2.0 scores drag **kinematics**, not only the final position. A monotonic
smoothstep drag lands the piece pixel-correct yet is rejected as a bot (verify code
**F001** = "near miss / behavioral fail"). Proven two ways:

- **Wide hx-offset sweep** (±40px, 30 live attempts): **0 T001** at any offset — so the
  failure is not positional.
- **A/B drag profiles, same YOLO position**: monotonic smoothstep **0/3** T001 vs
  overshoot-correct **3/3** T001.

`human_drag` therefore uses a **ballistic** trajectory: fast-start / slow-approach
ease-out, **overshoot the target by ~6-11px**, then a multi-step correction back to the
true target. This is the human tell that passes. Do **not** over-jitter — a heavier
profile with random pauses + big jitter ("over2") regressed to 2/3. Clean overshoot wins.

## The two callback shapes (why the old flow failed)

The widget hands the page one of two things depending on how it was initialised:

- **`captchaVerifyCallback(payload)`** (legacy) — `{sceneId, certifyId, deviceToken, …}`.
  The page must call `VerifyCaptchaV3` itself to obtain a VerifyCode. That call is
  **one-time-use** — it consumes the certifyId.
- **`success(R)`** (self-verify, what current sites use) — the widget runs its own
  verify, receives a `securityToken`, and hands back
  `R = base64({certifyId, sceneId, isSign:true, securityToken})`. This is the exact
  string targets expect as `captcha_verify_param`. Calling verify again would burn
  the certifyId.

The solver handles both: it harvests `success(R)` when the widget self-verifies and
only falls back to the legacy verify path otherwise.

## Latency note

Each attempt costs ~20-25s (drag + widget verify + challenge refresh). Stub mode
typically succeeds in 2-5 attempts (~30-80s); real-page mode in 1-3 attempts.
Aliyun's page-side `timeout` option is the widget's internal timer, not a deadline
for the solve — allow `timeout_s` 240-300.

## Dependencies

`onnxruntime` (CPU) + `opencv-python-headless` + `numpy`. The YOLO path degrades to
cv2 gracefully if `onnxruntime` or `best.onnx` is missing. `curl_cffi` is optional
(chrome-TLS HTTP for `post_fetch`; falls back to urllib).

## Request — stub mode

```json
POST /solve
{
  "type": "aliyun",
  "scene_id": "36qgs6xb",     // required — target site's captcha SceneId
  "prefix": "no8xfe",         // required — captcha-open endpoint prefix
  "region": "sgp",            // optional — sgp (default) | cn | intl
  "proxy": "http://user:pass@host:port",  // optional
  "timeout_s": 90,            // optional
  "post_fetch": [
    {"url": "https://target.example/api/redeem", "method": "POST",
     "headers": {"x-region": "overseas"},
     "body": {"captcha_verify_param": "__TOKEN__"}}
  ]
}
```

`post_fetch` in stub mode runs from the **server process** (plain HTTP, chrome TLS
via curl_cffi) — no browser session needed. Use `headers` for whatever the target's
own frontend adds to its API calls.

## Request — real_page mode

```json
POST /solve
{
  "type": "aliyun",
  "real_page": true,
  "url": "https://chat.z.ai/auth",
  "scene_id": "36qgs6xb",
  "prefix": "no8xfe",
  "timeout_s": 300,
  "pre_actions": [
    {"type": "click", "selector": "text=Continue with Email"},
    {"type": "wait", "value": "2.5"},
    {"type": "click", "selector": "text=Sign up"},
    {"type": "fill", "selector": "input[type=email]", "value": "user@example.com"},
    {"type": "fill", "selector": "input[type=password]", "value": "…"}
  ],
  "post_fetch": [
    {"url": "https://chat.z.ai/api/v1/auths/signup", "method": "POST",
     "headers": {"x-region": "overseas"},
     "body": {"email": "user@example.com", "password": "…",
              "captcha_verify_param": "__TOKEN__"}}
  ]
}
```

- `pre_actions` run before the widget appears (click/fill/wait/select/press;
  add `"optional": true` to tolerate a missing element on multi-variant pages).
- `post_fetch` calls fire **from inside the solving browser**; `__TOKEN__` is
  replaced by the harvested token. Results come back under `post_fetch` with
  `{status, body}`.

## Response

```json
{
  "type": "aliyun",
  "solved": true,
  "token": "eyJjZX…",            // the token (R string)
  "verify_code": "T001",
  "method": "quadratic-slide",
  "attempts": 2,
  "elapsed": 28.8,
  "post_fetch": [{"url": "https://chat.z.ai/api/v1/auths/signup",
                   "status": 200, "body": "{\"success\":true}"}]
}
```

## Failure modes

- `captcha_type: "TRACELESS"` + `solved: false` — the scene served a silent
  challenge. This happens when the scene id does not match the page context and is
  **always rejected** by targets; the solver fails hard instead of pretending to
  succeed. Check the scene id.
- `solved: false, error: "no R in N attempts"` — the drag never got accepted;
  inspect the attempt logs (gap detection failures show as `challenge images missing`).
- Token solved but redeem 400 — check the target's frontend request wrappers for
  required headers (see "Which mode do I need?").

## How it works (RE notes)

1. **Widget render** — stub mode mounts the SDK on our own minimal page with the
   site's sceneId; real-page mode drives the target's own widget.
2. **Gap detection** — `gap_cv.py`: YOLOv8n ONNX when present, else cv2 Sobel-x
   gradient template match. Pure CPU, ~5-20ms.
3. **Quadratic drag (the anti-bot trick)** — handle→piece is NOT linear:
   `piece_rel = 0.00355·hx² + 0.0769·hx` (residual ≈0; verified identical on
   chat.z.ai by live measurement). Naive linear solvers always miss (F015).
4. **Token harvest** — the widget's own success callback (`success(R)`); never
   call verify again for self-verify widgets (one-time-use certifyId).
5. **Submit** — stub mode returns the token and can fire `post_fetch` server-side;
   real-page mode fires `post_fetch` from the same browser session. Include the
   target's required headers either way.
