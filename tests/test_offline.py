"""Offline checks of the renderer, no LiveKit server needed. Need the renderer extra and `liteavatar-renderer setup`.

    pytest -s tests/test_offline.py                         # LiteAvatar's own speech sample
    LITEAVATAR_TEST_WAV=speech.wav pytest -s tests/test_offline.py

1. WAV in, MP4 out: prints the real-time factor and fails at 1 or more.
2. The LiveKit worker's generator, played back at real-time pace: every reply's audio comes out, each audio chunk
   with its own video frame, without stalls.
"""

from __future__ import annotations

import asyncio
import itertools
import os

import pytest

from livekit_lite_avatar.renderer import assets
from livekit_lite_avatar.renderer.settings import Settings

SETTINGS = Settings.from_env()
pytestmark = pytest.mark.skipif(
    bool(assets.check(SETTINGS)), reason="renderer assets not installed (run: liteavatar-renderer setup)"
)


@pytest.fixture(scope="module")
def stream():
    pytest.importorskip("torch", reason="needs the renderer extra")
    from livekit_lite_avatar.renderer.stream import from_settings

    return from_settings(SETTINGS)


@pytest.fixture(scope="module")
def wav():
    from livekit_lite_avatar.renderer.offline import default_wav

    return os.getenv("LITEAVATAR_TEST_WAV") or str(default_wav(SETTINGS))


def test_offline_render(stream, wav, tmp_path):
    from livekit_lite_avatar.renderer.offline import render_file

    out = tmp_path / "liteavatar-test.mp4"
    result = render_file(wav, out, SETTINGS, stream=stream)
    print("\n" + result.summary())
    assert out.is_file() and out.stat().st_size > 10_000
    expected = result.audio_s * result.fps
    assert abs(result.frames - expected) <= 0.05 * expected + 6, "frame count does not match the audio length"
    assert result.real_time_factor < 1.0, "this CPU renders slower than real time"


def test_worker_generator_in_real_time(stream, wav):
    import librosa
    import numpy as np
    from livekit import rtc
    from livekit.agents.voice.avatar import AudioSegmentEnd

    from livekit_lite_avatar.renderer.worker import SAMPLE_RATE, LiteAvatarGenerator

    audio, _ = librosa.load(wav, sr=SAMPLE_RATE, mono=True)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    stream.reset()
    gen = LiteAvatarGenerator(stream, lip_delay_frames=SETTINGS.lip_delay_frames)
    spf = SAMPLE_RATE // stream.out_fps

    async def go():
        async def feed():  # TTS-like: 20 ms frames, faster than real time
            for i in range(0, len(pcm), 480):
                chunk = pcm[i : i + 480]
                await gen.push_audio(rtc.AudioFrame(chunk.tobytes(), SAMPLE_RATE, 1, len(chunk)))
            await gen.push_audio(AudioSegmentEnd())

        feeder = asyncio.create_task(feed())
        seq, played = [], []
        async for item in gen:
            if isinstance(item, AudioSegmentEnd):
                break
            if isinstance(item, rtc.AudioFrame):
                seq.append(("a", item.samples_per_channel))
                played.append(np.frombuffer(item.data, np.int16))
            else:
                seq.append(("v", 0))
                await asyncio.sleep(1 / stream.out_fps)  # play back at real-time pace, as AVSynchronizer does
        await feeder
        await gen.aclose()
        return seq, np.concatenate(played)

    seq, played = asyncio.run(asyncio.wait_for(go(), timeout=len(pcm) / SAMPLE_RATE * 3 + 30))
    first = next(i for i, (kind, _) in enumerate(seq) if kind == "a")
    reply = seq[first - 1 :]
    audio_out = len(played)
    pairs = sum(1 for (k1, _), (k2, _) in itertools.pairwise(reply) if k1 == "v" and k2 == "a")
    stalls = sum(1 for (k1, _), (k2, _) in itertools.pairwise(reply) if k1 == "v" and k2 == "v")
    print(
        f"\n{pairs} audio chunks with their frames, {stalls} stalled frames, "
        f"{first - 1} idle frames before the reply's audio started"
    )
    # the reply's audio comes out whole and unchanged, then a little silence while the mouth eases shut
    assert np.array_equal(played[: len(pcm)], pcm), "the reply's audio was not played back unchanged"
    assert not played[len(pcm) :].any(), "unexpected sound after the reply"
    assert audio_out - len(pcm) <= (SETTINGS.lip_delay_frames + 6) * spf, "too much silence after the reply"
    assert pairs == audio_out // spf, "an audio chunk came out without its video frame"
    assert stalls <= 2, "the renderer fell behind real time during the reply"
