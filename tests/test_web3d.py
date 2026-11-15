"""The 3D style (web/): a face drawn in the browser, lip-synced to the agent's voice. Runs in headless Chromium.

- The face loads and draws, with no errors.
- A recorded line, attached as a track (locally, and through an in-page WebRTC connection with Opus, like a
  LiveKit track): the voice plays 150 ms late, the mouth follows it, it rests in silence, and the brows lift.
- The zero-key demo's 3D page in a live call on a local LiveKit server: it follows the agent's voice, which nothing
  else on the page plays.

Skipped without Playwright and its Chromium (pip install playwright && playwright install chromium); the live test
also needs `livekit-server`. The pages load three.js and livekit-client from jsDelivr.

    pytest -s tests/test_web3d.py
"""

from __future__ import annotations

import functools
import http.server
import importlib.util
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "local_demo"
CLIP = "/examples/local_demo/audio/hello.flac"
DELAY_MS = 150
pytestmark = pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright not installed")
sys.path.insert(0, str(ROOT / "tests" / "e2e"))
# headless Chromium: WebGL on SwiftShader where there's no GPU, sound without a click, local WebRTC addresses as they
# are (not mDNS names)
ARGS = [
    "--enable-unsafe-swiftshader",
    "--ignore-gpu-blocklist",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-features=WebRtcHideLocalIpsWithMdns",
]


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


@pytest.fixture(scope="module")
def site():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(ROOT)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as p:
        try:
            b = p.chromium.launch(args=ARGS)
        except Error as e:
            pytest.skip(f"no Chromium for Playwright ({str(e).splitlines()[0]})")
        yield b
        b.close()


@pytest.fixture(scope="module")
def webrtc(browser, site):
    """Skip unless this browser can make WebRTC connections. On a machine whose default route is a VPN, headless
    Chromium offers only that interface and nothing connects. CI sets LITEAVATAR_REQUIRE_WEBRTC=1 to fail instead."""
    page = _page(browser, site, "/tests/web3d/loopback.html")
    page.wait_for_function("window.ready === true")
    ok = page.evaluate("probe()")
    page.close()
    if not ok:
        msg = "this browser can't make WebRTC connections here (a VPN as the default route, say)"
        if os.getenv("LITEAVATAR_REQUIRE_WEBRTC") == "1":
            pytest.fail(msg)
        pytest.skip(msg)


def _page(browser, site: str, path: str, **kw):
    page = browser.new_page(**kw)
    page.console = []
    page.on("console", lambda m: page.console.append((m.type, m.text)))
    page.on("pageerror", lambda e: page.console.append(("pageerror", str(e))))
    page.goto(site + path)
    return page


def _problems(page) -> list[str]:
    return [f"{kind}: {text}" for kind, text in page.console if kind in ("error", "pageerror") or "isn't r186" in text]


def test_face_draws(browser, site):
    page = _page(browser, site, "/tests/web3d/loopback.html", viewport={"width": 512, "height": 640})
    page.wait_for_function("window.ready === true")
    result = page.evaluate("still({ quality: 'high' })")
    # brightness of the drawing: the whole canvas, the middle (the face) and the top strip (the backdrop)
    pixels = page.evaluate(
        """() => {
          const src = document.getElementById('face'), c = document.createElement('canvas');
          c.width = 128; c.height = 160;
          const g = c.getContext('2d'); g.drawImage(src, 0, 0, 128, 160);
          const d = g.getImageData(0, 0, 128, 160).data, lum = [];
          for (let i = 0; i < d.length; i += 4) lum.push(0.3 * d[i] + 0.59 * d[i + 1] + 0.11 * d[i + 2]);
          return lum;
        }"""
    )
    page.close()
    assert not result["errors"] and not _problems(page), _problems(page) + result["errors"]
    img = np.asarray(pixels, np.float32).reshape(160, 128)
    # a face on the gradient, not a blank or single-colour canvas
    assert img.std() > 20, f"the canvas looks blank (std {img.std():.1f})"
    assert abs(img[40:120, 32:96].mean() - img[:10].mean()) > 10, "nothing in the middle differs from the backdrop"


def _lag_corr(rel_s: np.ndarray, y: np.ndarray, envelope: list[float], lag_ms: int, mask: np.ndarray) -> float:
    env = np.asarray(envelope)
    e = np.interp(rel_s - lag_ms / 1000, np.arange(len(env)) * 0.04 + 0.02, env, left=-100, right=-100)
    return float(np.corrcoef(y[mask], np.clip(e[mask], -60, 0))[0, 1])


@pytest.mark.parametrize("via", ["local", "webrtc"])
def test_lip_sync_follows_the_voice(browser, site, via, request):
    if via == "webrtc":
        request.getfixturevalue("webrtc")
    page = _page(browser, site, "/tests/web3d/loopback.html")
    page.wait_for_function("window.ready === true")
    r = page.evaluate(f"run({{ clip: '{CLIP}', via: '{via}' }})")
    page.close()
    assert "skipped" not in r, r.get("skipped")
    assert not r["errors"] and not _problems(page), _problems(page) + r["errors"]
    f = r["frames"]
    rel = (np.array([x["t"] for x in f]) - r["playedAt"]) / 1000
    mouth = np.array([x["open"] for x in f])
    played = np.clip(np.array([x["db"] for x in f]), -60, 0)
    speaking = np.array([x["speaking"] for x in f])
    during = (rel > 0) & (rel < r["durationS"] + 0.4)
    before = rel < -0.1
    print(f"\n{via}: {len(f)} frames")
    assert before.sum() > 20 and during.sum() > 100, "the lip-sync wasn't stepped every 33 ms"

    # the voice plays, DELAY_MS late (plus a little for the WebRTC jitter buffer)
    lags = range(0, 451, 25)
    voice = {lag: _lag_corr(rel, played, r["envelope"], lag, during) for lag in lags}
    best = max(voice, key=voice.get)
    print("played vs clip:", {k: round(v, 2) for k, v in voice.items()})
    assert voice[best] > 0.8, "the voice that plays isn't the clip"
    assert DELAY_MS - 25 <= best <= DELAY_MS + 150, f"the voice plays {best} ms late, not about {DELAY_MS}"

    # the mouth follows the voice the listener hears, not the one the model heard 150 ms earlier
    lips = {lag: _lag_corr(rel, mouth, r["envelope"], lag, during) for lag in lags}
    best_lips = max(lips, key=lips.get)
    print("mouth vs clip:", {k: round(v, 2) for k, v in lips.items()})
    assert lips[best_lips] > 0.35, "the mouth doesn't follow the voice"
    assert abs(best_lips - best) <= 150, f"the mouth is {best_lips - best} ms off the voice"

    # silent and still before the clip; speaking while it plays; at least one stress lifts the brows
    assert mouth[before].max() < 0.05 and played[before].max() <= -55, "the mouth moved or sound played before"
    assert speaking[during].mean() > 0.4 and not speaking[before].any()
    assert max(x["brow"] for x in f) > 0.3, "the brows never lifted"


_RECORD_FRAMES = """() => {
  const OPEN = ["viseme_aa", "viseme_E", "viseme_I", "viseme_O", "viseme_U", "jawOpen"];
  window.frames3d = [];
  avatar.onFrame = (f) => window.frames3d.push({
    t: f.now,
    speaking: f.speaking,
    open: OPEN.reduce((a, n) => a + (f.values[n] ?? 0), 0),
    db: avatar.lipsync.outputDb(),
  });
}"""
# seconds of sound heard so far, from the frames' timestamps; done once 10 s were heard and then 2 s of quiet
_GREETING_DONE = """() => {
  const f = window.frames3d;
  let heard = 0, last = null;
  for (let i = 1; i < f.length; i += 1) if (f[i].db > -50) { heard += f[i].t - f[i - 1].t; last = f[i].t; }
  return heard > 10000 && performance.now() - last > 2000;
}"""
# who the face follows, and whether anything else on the page could be playing audio
_ATTACHED = """() => {
  const t = avatar.track;
  const mine = (p) => [...p.trackPublications.values()].some((x) => x.track === t);
  const p = [...room.remoteParticipants.values()].find(mine);
  const audioElements = document.querySelectorAll("audio").length;
  return { identity: p?.identity ?? null, agent: p?.isAgent ?? null, audioElements };
}"""


@pytest.mark.skipif(shutil.which("livekit-server") is None, reason="livekit-server not installed")
@pytest.mark.skipif(importlib.util.find_spec("soundfile") is None, reason="the demo agent needs soundfile")
def test_demo_3d_page_follows_the_agent(browser, site, webrtc, tmp_path):
    import run as e2e
    from livekit import api

    stack = e2e.Stack(tmp_path)
    try:
        stack.start_livekit()
        stack.start_agent(DEMO / "agent.py", {"DEMO_STYLE": "3d"})
        token = (
            api.AccessToken(e2e.KEY, e2e.SECRET)
            .with_identity("you")
            .with_grants(api.VideoGrants(room_join=True, room="demo-3d-test"))
            .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name="liteavatar-demo")]))
            .to_jwt()
        )
        # low quality at a few frames a second: on a runner without a GPU each frame is drawn in software, and the
        # page must stay free to handle the click and the agent's audio
        page = _page(
            browser, site, f"/examples/local_demo/3d.html?quality=low&maxFps=8#url={stack.lk_url}&token={token}"
        )
        page.wait_for_function("window.avatar !== undefined && !document.getElementById('start').disabled")
        page.evaluate(_RECORD_FRAMES)
        page.evaluate("document.getElementById('start').click()")
        # the greeting: three recorded lines, about 15 s of speech; done once 2 s pass without sound after speech
        deadline = time.time() + 90
        while time.time() < deadline and not page.evaluate(_GREETING_DONE):
            time.sleep(0.5)
        frames = page.evaluate("window.frames3d")
        attached = page.evaluate(_ATTACHED)
        page.close()
    finally:
        stack.stop()
    assert not _problems(page), _problems(page)
    print("\nattached:", attached, "frames:", len(frames))
    assert attached["agent"] is True, f"the face followed {attached['identity']}, not the agent"
    assert attached["audioElements"] == 0, "something else on the page plays audio"
    t = np.array([x["t"] for x in frames]) / 1000
    db = np.array([x["db"] for x in frames])
    loud = db > -50
    span = np.diff(t, prepend=t[0])
    heard_s = float(span[loud].sum())
    print(f"heard {heard_s:.1f} s of the greeting")
    assert heard_s > 10, f"only {heard_s:.1f} s of the greeting was heard"
    # the mouth moves with the voice (the quiet frames include the short pauses inside the greeting, where it is still
    # closing), and rests before the greeting and once it is over
    mouth = np.array([x["open"] for x in frames])
    print(f"mouth open {mouth[loud].mean():.2f} while the voice sounds, {mouth[~loud].mean():.2f} otherwise")
    assert mouth[loud].mean() > max(0.1, 2 * mouth[~loud].mean()), "the mouth doesn't move with the voice"
    first, last = t[loud][0], t[loud][-1]
    assert mouth[t < first - 0.5].max(initial=0) < 0.05, "the mouth moved before the agent spoke"
    assert mouth[t > last + 0.8].max(initial=0) < 0.05, "the mouth kept moving after the agent stopped"
    assert np.array([x["speaking"] for x in frames])[loud].mean() > 0.6
