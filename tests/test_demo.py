"""The zero-key browser demo (examples/local_demo): it greets a visitor, then repeats what they say, lip-synced.

Joins like the web page does and stands in for a person: listens to the greeting, then publishes a microphone
track carrying speech and checks that the avatar says it back. Skipped unless `livekit-server` is on PATH and the
renderer is set up.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from livekit_lite_avatar.renderer import assets
from livekit_lite_avatar.renderer.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "local_demo"
pytestmark = [
    pytest.mark.skipif(shutil.which("livekit-server") is None, reason="livekit-server not installed"),
    pytest.mark.skipif(bool(assets.check(Settings.from_env())), reason="renderer assets not installed"),
]
sys.path.insert(0, str(ROOT / "tests" / "e2e"))
RATE = 24000
FRAME = RATE // 50  # 20 ms


def _audible_s(chunks: list[np.ndarray]) -> float:
    """Seconds of 20 ms frames louder than -45 dBFS."""
    loud = 0
    for a in chunks:
        if len(a) and 20 * np.log10(np.sqrt(np.mean((a.astype(np.float32) / 32768) ** 2)) + 1e-9) > -45:
            loud += 1
    return loud * 0.02


def _frames(a: np.ndarray) -> list[np.ndarray]:
    return [a[i : i + FRAME] for i in range(0, len(a), FRAME)]


async def _visit(stack, greeting_s: float) -> dict:
    import soundfile as sf
    from livekit import api, rtc

    token = (
        api.AccessToken(__import__("run").KEY, __import__("run").SECRET)
        .with_identity("you")
        .with_grants(api.VideoGrants(room_join=True, room="demo-test"))
        .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name="liteavatar-demo")]))
        .to_jwt()
    )
    room = rtc.Room()
    heard: list[tuple[float, np.ndarray]] = []
    tasks = set()

    async def listen(track: rtc.RemoteAudioTrack) -> None:
        async for ev in rtc.AudioStream(track, sample_rate=RATE, num_channels=1, frame_size_ms=20):
            heard.append((time.time(), np.frombuffer(ev.frame.data, np.int16).copy()))

    @room.on("track_subscribed")
    def _sub(track, pub, participant) -> None:
        if participant.identity == "liteavatar" and track.kind == rtc.TrackKind.KIND_AUDIO:
            tasks.add(asyncio.create_task(listen(track)))

    def quiet_for(seconds: float, since: float) -> bool:
        loud = [t for t, a in heard if t > since and _audible_s([a]) > 0]
        return bool(loud) and time.time() - loud[-1] > seconds

    await room.connect(stack.lk_url, token)
    try:
        start = time.time()
        # the greeting: three recorded lines. Wait for (nearly) all of it and a pause after it: the pauses between the
        # lines can be long on a slow machine, so a pause alone doesn't mean the greeting is over.
        while _audible_s([a for _, a in heard]) < greeting_s - 0.5 or not quiet_for(2.0, start):
            assert time.time() - start < 180, f"the greeting didn't arrive ({_audible_s([a for _, a in heard])} s)"
            await asyncio.sleep(0.2)
        greeting = _audible_s([a for _, a in heard])

        # now talk: a microphone track carrying a recorded sentence, in real time, then silence
        source = rtc.AudioSource(RATE, 1)
        mic = rtc.LocalAudioTrack.create_audio_track("mic", source)
        await room.local_participant.publish_track(
            mic, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        await asyncio.sleep(1.0)
        said, _ = sf.read(DEMO / "audio" / "now.flac", dtype="int16")
        talk_at = time.time()
        for f in _frames(np.concatenate([said, np.zeros(RATE * 2, np.int16)])):
            await source.capture_frame(rtc.AudioFrame(f.tobytes(), RATE, 1, len(f)))
        while not quiet_for(2.0, talk_at + len(said) / RATE):  # the echo
            assert time.time() - talk_at < 60, "no echo"
            await asyncio.sleep(0.2)
        echo = _audible_s([a for t, a in heard if t > talk_at + len(said) / RATE])
        return {"greeting_audible_s": greeting, "said_audible_s": _audible_s(_frames(said)), "echo_audible_s": echo}
    finally:
        await room.disconnect()
        for t in tasks:
            t.cancel()


def test_demo_greets_then_repeats_you(tmp_path):
    pytest.importorskip("torch", reason="needs the renderer extra")
    import run as e2e
    import soundfile as sf

    lines = [sf.read(DEMO / "audio" / n, dtype="int16")[0] for n in ("hello.flac", "how.flac", "now.flac")]
    expected = sum(_audible_s(_frames(a)) for a in lines)
    stack = e2e.Stack(tmp_path)
    try:
        stack.start_livekit()
        stack.start_renderer()
        stack.start_agent(DEMO / "agent.py")
        result = asyncio.run(_visit(stack, expected))
    finally:
        stack.stop()
    print("\n", result)
    assert abs(result["greeting_audible_s"] - expected) < 0.5, "the greeting didn't play in full"
    assert abs(result["echo_audible_s"] - result["said_audible_s"]) < 0.5, "the avatar didn't repeat what was said"
