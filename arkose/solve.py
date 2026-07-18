"""Arkose FunCaptcha solver — browser-driven challenge + ONNX prediction.

Flow: navigate to page_url → trigger Arkose → intercept gfct (context-level) →
download challenge image → predict via ONNX → encrypt answer (CryptoJS AES-CBC)
→ submit to /fc/ca/ → return token.

Answer submission format:
  POST /fc/ca/ with form-urlencoded:
    session_token, game_token, sid, guess, render_type, analytics_tier,
    bio, is_compatibility_mode, ecdata

  guess = CryptoJS.AES.encrypt(JSON.stringify([{index: N}]), session_token)
  bio = base64 encoded motion data (mouse movements)
  ecdata = base64('{"height": 450, "width": 400}')
"""
import asyncio
import base64
import json
import logging
import os
import random
import tempfile
import time
import urllib.parse

from cloakbrowser import launch_async

from arkose.predict import predict as onnx_predict, cryptojs_encrypt

log = logging.getLogger("arkose")

_ECDATA = base64.b64encode(b'{"height": 450, "width": 400}').decode()


def _generate_bio() -> str:
    """Generate minimal mouse motion bio data (base64 encoded)."""
    motion_parts = []
    ts = random.randint(5, 100)
    # Initial pause
    ts += random.randint(120, 420)
    # Generate a few mouse move points
    x, y = random.randint(100, 300), random.randint(100, 350)
    motion_parts.append(f"{ts},0,{x},{y};")
    for _ in range(random.randint(8, 15)):
        ts += random.randint(6, 55)
        x += random.randint(-20, 20)
        y += random.randint(-20, 20)
        x = max(20, min(380, x))
        y = max(36, min(414, y))
        motion_parts.append(f"{ts},0,{x},{y};")
    # Final pause + click
    ts += random.randint(30, 110)
    motion_parts.append(f"{ts},0,{x},{y};")
    ts += random.randint(60, 180)
    motion_parts.append(f"{ts},1,{x},{y};")
    ts += random.randint(60, 180)
    motion_parts.append(f"{ts},2,{x},{y};")

    motion_str = "".join(motion_parts)
    bio_json = json.dumps({"mbio": motion_str, "tbio": "", "kbio": ""})
    return base64.b64encode(bio_json.encode()).decode()


def _extract_challenge(gfct: dict) -> dict:
    game_data = gfct.get("game_data", {})
    custom_gui = game_data.get("customGUI", {})
    return {
        "instruction": game_data.get("instruction_string", ""),
        "challenge_imgs": custom_gui.get("_challenge_imgs", []),
        "session_token": gfct.get("session_token", ""),
        "challenge_id": gfct.get("challengeID", ""),
        "sec": gfct.get("sec", ""),
        "sid": gfct.get("sid", ""),
        "waves": game_data.get("waves", 1),
        "game_type": game_data.get("gameType", 4),
    }


async def solve_arkose(public_key: str, page_url: str | None = None,
                       game_type: str = "4", proxy: str | None = None,
                       timeout_s: int = 120, max_attempts: int = 10,
                       pre_actions: list | None = None) -> dict:
    if not public_key:
        return {"solved": False, "error": "public_key is required"}
    if not page_url:
        return {"solved": False, "error": "page_url is required"}

    t_start = time.monotonic()

    kw = {"headless": True, "humanize": True}
    if proxy:
        kw["proxy"] = proxy

    browser = await launch_async(**kw)
    try:
        ctx = await browser.new_context()
        page = await ctx.new_page()

        gfct_data: dict = {}
        ca_result: dict = {}
        arkose_domain: str = ""

        async def on_response(resp):
            nonlocal arkose_domain
            url = resp.url
            try:
                if "/fc/gfct/" in url:
                    from urllib.parse import urlparse
                    arkose_domain = urlparse(url).netloc
                    txt = await resp.text()
                    data = json.loads(txt)
                    gfct_data.clear()
                    gfct_data.update(data)
                    log.info("arkose: intercepted gfct domain=%s", arkose_domain)
                elif "/fc/ca/" in url:
                    txt = await resp.text()
                    try:
                        ca_result.clear()
                        ca_result.update(json.loads(txt))
                    except (json.JSONDecodeError, TypeError):
                        pass
                    log.info("arkose: intercepted ca")
            except Exception:
                pass

        ctx.on("response", on_response)

        log.info("arkose: navigating to %s", page_url)
        try:
            await page.goto(page_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            return {"solved": False, "error": f"navigation failed: {e}",
                    "elapsed": round(time.monotonic() - t_start, 1)}

        # Run pre_actions if provided (fill creds, click submit, etc.)
        if pre_actions:
            from common.browser import run_pre_actions
            log.info("arkose: running %d pre_actions", len(pre_actions))
            try:
                await run_pre_actions(page, pre_actions)
            except Exception as e:
                log.warning("arkose: pre_actions error: %s", e)

        log.info("arkose: waiting for gfct...")
        for _ in range(60):
            if gfct_data:
                break
            await asyncio.sleep(1)

        if not gfct_data:
            return {"solved": False, "error": "no gfct response",
                    "elapsed": round(time.monotonic() - t_start, 1)}

        guesses = []  # Accumulated guesses per wave

        for wave in range(max_attempts):
            if time.monotonic() - t_start > timeout_s:
                break

            info = _extract_challenge(gfct_data)
            instruction = info["instruction"]
            challenge_imgs = info["challenge_imgs"]
            session_token = info["session_token"]
            challenge_id = info["challenge_id"]
            sid = info["sid"]

            log.info("arkose wave %d: %s imgs=%d sid=%s",
                     wave, instruction, len(challenge_imgs), sid)

            if not challenge_imgs:
                break

            img_path = None
            try:
                # Download challenge image
                img_bytes = await page.evaluate(
                    """async (url) => {
                        const r = await fetch(url);
                        return Array.from(new Uint8Array(await r.arrayBuffer()));
                    }""", challenge_imgs[0])

                if len(img_bytes) < 100:
                    continue

                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp.write(bytes(img_bytes))
                    img_path = tmp.name

                from PIL import Image
                img = Image.open(img_path)
                answer = onnx_predict(img, instruction)
                if answer is None:
                    log.warning("arkose wave %d: prediction failed", wave)
                    continue

                log.info("arkose wave %d: answer=%d", wave, answer)

                # Find an Arkose-origin frame for same-origin fetch
                game_frame = None
                for frame in page.frames:
                    if "game-core" in (frame.url or ""):
                        game_frame = frame
                        break
                if not game_frame:
                    for frame in page.frames:
                        if "arkoselabs" in (frame.url or ""):
                            game_frame = frame
                            break
                if not game_frame:
                    log.warning("arkose wave %d: no Arkose frame", wave)
                    continue

                # Send required action updates to /fc/a/ before first answer
                # (Arkose SDK sends these to register session state)
                a_url = f"https://{arkose_domain}/fc/a/"
                game_type_str = str(info.get("game_type", 4))
                if game_type_str == "0":
                    game_type_str = "4"

                if wave == 0:
                    enforcement_url = gfct_data.get("challengeURL", "")
                    base_payload = urllib.parse.urlencode({
                        "sid": sid, "session_token": session_token,
                        "analytics_tier": "15", "disableCookies": "false",
                        "render_type": "canvas", "is_compatibility_mode": "false",
                    })
                    a1 = base_payload + "&category=Site+URL&action=" + urllib.parse.quote(enforcement_url)
                    a2 = base_payload + f"&game_token={urllib.parse.quote(challenge_id)}&game_type={game_type_str}&category=loaded&action=game+loaded"
                    a3 = base_payload + f"&game_token={urllib.parse.quote(challenge_id)}&game_type={game_type_str}&category=begin+app&action=user+clicked+verify"
                    for label, body_str in [("Site URL", a1), ("game loaded", a2), ("user clicked verify", a3)]:
                        await game_frame.evaluate(
                            """async ({url, body}) => {
                                await fetch(url, {method:'POST',
                                    headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8','X-Requested-With':'XMLHttpRequest'},
                                    body:body, credentials:'include'});
                            }""", {"url": a_url, "body": body_str})
                    log.info("arkose: sent3 action updates to /fc/a/")

                # Build guess array (accumulating)
                guesses.append({"index": answer})
                guess_json = json.dumps(guesses, separators=(",", ":"))

                bio = _generate_bio()
                analytics_tier = "15"

                ca_url = f"https://{arkose_domain}/fc/ca/"

                # Encrypt guess in Python (CryptoJS-compatible AES-CBC)
                encrypted_guess = cryptojs_encrypt(guess_json, session_token)

                # Submit pre-encrypted guess from Arkose-origin frame
                result = await game_frame.evaluate(
                    """async ({guess, sessionToken, gameToken, sid,
                              gameType, analyticsTier, bio, ecdata, caUrl}) => {
                        const params = new URLSearchParams();
                        params.set('session_token', sessionToken);
                        params.set('game_token', gameToken);
                        params.set('sid', sid);
                        params.set('guess', guess);
                        params.set('render_type', 'canvas');
                        params.set('analytics_tier', analyticsTier);
                        params.set('bio', bio);
                        params.set('is_compatibility_mode', 'false');
                        params.set('ecdata', ecdata);

                        const r = await fetch(caUrl, {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                                'X-Requested-With': 'XMLHttpRequest'
                            },
                            body: params.toString(),
                            credentials: 'include'
                        });
                        return {status: r.status, response: await r.text()};
                    }""", {
                        "guess": encrypted_guess,
                        "sessionToken": session_token,
                        "gameToken": challenge_id,
                        "sid": sid,
                        "gameType": game_type_str,
                        "analyticsTier": analytics_tier,
                        "bio": bio,
                        "ecdata": _ECDATA,
                        "caUrl": ca_url,
                    })

                log.info("arkose wave %d: ca result: %s", wave, str(result)[:300])

                if result.get("error"):
                    log.warning("arkose wave %d: error: %s", wave, result["error"])
                    guesses.pop()
                    continue

                try:
                    resp_data = json.loads(result.get("response", "{}"))
                except (json.JSONDecodeError, TypeError):
                    resp_data = {}

                # Check for solved token
                solved = resp_data.get("solved", False)
                solved_token = resp_data.get("token", "")
                response_str = resp_data.get("response", "")

                if solved and solved_token:
                    elapsed = round(time.monotonic() - t_start, 1)
                    log.info("arkose: SOLVED in %.1fs (wave %d)", elapsed, wave + 1)
                    return {
                        "solved": True,
                        "token": solved_token,
                        "method": "onnx-predict",
                        "waves": wave + 1,
                        "elapsed": elapsed,
                    }

                if response_str == "answered":
                    # Answered but may need more waves or got token
                    if solved_token:
                        elapsed = round(time.monotonic() - t_start, 1)
                        return {
                            "solved": True,
                            "token": solved_token,
                            "method": "onnx-predict",
                            "waves": wave + 1,
                            "elapsed": elapsed,
                        }

                if response_str == "not answered":
                    # Correct answer, next wave
                    # Next challenge image is in the ca response, NOT a new gfct
                    log.info("arkose wave %d: correct, next wave", wave)
                    next_imgs = resp_data.get("_challenge_imgs", [])
                    next_instruction = resp_data.get("instruction_string", instruction)
                    if next_imgs:
                        # Update gfct_data with next challenge info
                        gfct_data.clear()
                        gfct_data.update({
                            "session_token": session_token,
                            "challengeID": challenge_id,
                            "sid": sid,
                            "game_data": {
                                "customGUI": {"_challenge_imgs": next_imgs},
                                "instruction_string": next_instruction,
                                "gameType": info.get("game_type", 4),
                            }
                        })
                        log.info("arkose wave %d: next challenge imgs=%d instruction=%s",
                                 wave, len(next_imgs), next_instruction)
                        continue
                    # Fallback: wait for new gfct
                    gfct_data.clear()
                    ca_result.clear()
                    for _ in range(30):
                        if gfct_data:
                            break
                        await asyncio.sleep(1)
                    if gfct_data:
                        continue
                    log.warning("arkose: no next challenge after correct answer")
                    break

                log.info("arkose wave %d: unexpected response: %s",
                         wave, str(resp_data)[:200])
                guesses.pop()  # Remove wrong guess

            finally:
                if img_path:
                    try:
                        os.unlink(img_path)
                    except OSError:
                        pass

            # Wrong answer — try to get new challenge
            gfct_data.clear()
            ca_result.clear()
            try:
                await page.click("[data-theme='try-again']", timeout=3000)
            except Exception:
                pass
            await asyncio.sleep(2)
            for _ in range(15):
                if gfct_data:
                    break
                await asyncio.sleep(1)
            if not gfct_data:
                break

        elapsed = round(time.monotonic() - t_start, 1)
        return {"solved": False, "error": f"failed after {max_attempts} waves",
                "elapsed": elapsed}
    finally:
        try:
            await browser.close()
        except Exception:
            pass
