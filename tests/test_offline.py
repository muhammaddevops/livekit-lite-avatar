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
import time
import zlib

import pytest

from livekit_lite_avatar.renderer import assets
from livekit_lite_avatar.renderer.settings import Settings

SETTINGS = Settings.from_env()
PERF = os.getenv("LITEAVATAR_PERF_CHECKS", "1") != "0"  # 0: skip the "faster than real time" checks (slow CPUs)
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
    if PERF:
        assert result.real_time_factor < 1.0, "this CPU renders slower than real time"


def _play_reply(stream, wav, extra_draw_s: float = 0.0, feed_pace: float = 0.0):
    """Feed a WAV to the worker's generator like TTS, play its output at real-time pace like AVSynchronizer.

    Returns the source audio, the (kind, n) sequence of what came out, the audio played and how many frames were
    drawn. extra_draw_s makes every drawn frame slower, to stand in for a slower CPU."""
    import librosa
    import numpy as np
    from livekit import rtc
    from livekit.agents.voice.avatar import AudioSegmentEnd

    from livekit_lite_avatar.renderer.worker import SAMPLE_RATE, LiteAvatarGenerator

    audio, _ = librosa.load(wav, sr=SAMPLE_RATE, mono=True)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    stream.reset()
    gen = LiteAvatarGenerator(stream, lip_delay_frames=SETTINGS.lip_delay_frames)
    drawn = [0]
    real_draw = stream.draw

    def draw(rows):
        drawn[0] += len(rows)
        time.sleep(extra_draw_s * len(rows))
        return real_draw(rows)

    stream.draw = draw

    async def go():
        async def feed():  # TTS-like: 20 ms frames, as fast as possible or at feed_pace x real time
            for i in range(0, len(pcm), 480):
                chunk = pcm[i : i + 480]
                await gen.push_audio(rtc.AudioFrame(chunk.tobytes(), SAMPLE_RATE, 1, len(chunk)))
                if feed_pace:
                    await asyncio.sleep(0.02 / feed_pace)
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
                seq.append(("v", zlib.crc32(bytes(item.data))))  # to tell new pictures from repeated ones
                await asyncio.sleep(1 / stream.out_fps)  # play back at real-time pace, as AVSynchronizer does
        await feeder
        await gen.aclose()
        return seq, np.concatenate(played)

    try:
        seq, played = asyncio.run(asyncio.wait_for(go(), timeout=len(pcm) / SAMPLE_RATE * 4 + 30))
    finally:
        stream.draw = real_draw
    return pcm, seq, played, drawn[0]


def _new_pictures(seq) -> int:
    """How many video frames in seq show a different picture from the one before."""
    shown = [h for kind, h in seq if kind == "v"]
    return sum(1 for a, b in itertools.pairwise(shown) if a != b)


def _check_reply(pcm, seq, played, spf: int) -> tuple[int, int]:
    """The reply's audio came out whole and unchanged, each chunk with a video frame; returns (pairs, stalls)."""
    import numpy as np

    first = next(i for i, (kind, _) in enumerate(seq) if kind == "a")
    reply = seq[first - 1 :]
    pairs = sum(1 for (k1, _), (k2, _) in itertools.pairwise(reply) if k1 == "v" and k2 == "a")
    # pauses while speech remains: a frame with no audio after it, before the source audio has all played
    speech_chunks, chunks, stalls = -(-len(pcm) // spf), 0, 0
    for (k1, _), (k2, _) in itertools.pairwise(reply):
        chunks += k1 == "a"
        stalls += k1 == "v" and k2 == "v" and chunks < speech_chunks
    # the reply's audio comes out whole and unchanged, then a little silence while the mouth eases shut
    assert np.array_equal(played[: len(pcm)], pcm), "the reply's audio was not played back unchanged"
    assert not played[len(pcm) :].any(), "unexpected sound after the reply"
    assert len(played) - len(pcm) <= (SETTINGS.lip_delay_frames + 6) * spf, "too much silence after the reply"
    assert pairs == len(played) // spf, "an audio chunk came out without its video frame"
    return pairs, stalls


def test_worker_generator_in_real_time(stream, wav):
    from livekit_lite_avatar.renderer.worker import SAMPLE_RATE

    spf = SAMPLE_RATE // stream.out_fps
    pcm, seq, played, drawn = _play_reply(stream, wav)
    pairs, stalls = _check_reply(pcm, seq, played, spf)
    print(
        f"\n{pairs} audio chunks with their frames, {drawn} drawn, {_new_pictures(seq)} new pictures, {stalls} stalled"
    )
    if PERF:
        assert stalls == 0, "the voice paused during the reply"
        assert drawn >= pairs - SETTINGS.lip_delay_frames - 3, "frames were skipped on a fast enough CPU"


def test_slow_cpu_keeps_the_voice_smooth(stream, wav):
    """On a CPU too slow for real time, frames are skipped but the voice never pauses."""
    from livekit_lite_avatar.renderer.worker import SAMPLE_RATE

    spf = SAMPLE_RATE // stream.out_fps
    pcm, seq, played, drawn = _play_reply(stream, wav, extra_draw_s=0.05)  # ~2.5x slower than real time
    pairs, stalls = _check_reply(pcm, seq, played, spf)
    new = _new_pictures(seq)
    print(f"\nslow CPU: {pairs} audio chunks, {drawn} frames drawn, {new} new pictures, {stalls} stalled frames")
    assert stalls == 0, "the voice paused while the renderer was behind"
    assert drawn < 0.8 * pairs, "the renderer should have skipped frames to keep up"
    # the lips keep moving at the rate frames can be drawn: nearly every drawn frame is shown, not drawn too late
    assert new >= 0.8 * drawn, "late frames were drawn and dropped instead of aiming ahead: the face froze"


@pytest.mark.parametrize("pace", [1.0, 1.5])
def test_tts_at_real_time_pace(stream, wav, pace):
    """TTS that streams only as fast as speech (or a little faster): the voice never pauses, and on a fast enough
    CPU the lips don't skip either."""
    from livekit_lite_avatar.renderer.worker import SAMPLE_RATE

    spf = SAMPLE_RATE // stream.out_fps
    pcm, seq, played, drawn = _play_reply(stream, wav, feed_pace=pace)
    pairs, stalls = _check_reply(pcm, seq, played, spf)
    print(f"\nTTS at {pace}x real time: {pairs} audio chunks, {drawn} drawn, {stalls} stalled frames")
    assert stalls == 0, "the voice paused"
    if PERF:
        assert drawn >= pairs - SETTINGS.lip_delay_frames - 3, "frames were skipped although the CPU keeps up"


def test_reply_after_an_interruption_plays_in_full(stream, wav):
    """Interrupt a reply while the producer is idle, then send the next: none of the next reply may be lost."""
    import librosa
    import numpy as np
    from livekit import rtc
    from livekit.agents.voice.avatar import AudioSegmentEnd

    from livekit_lite_avatar.renderer.worker import SAMPLE_RATE, LiteAvatarGenerator

    audio, _ = librosa.load(wav, sr=SAMPLE_RATE, mono=True)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    stream.reset()
    gen = LiteAvatarGenerator(stream, lip_delay_frames=SETTINGS.lip_delay_frames)

    async def go():
        async def feed(n_samples: int, end: bool) -> None:
            for i in range(0, n_samples, 480):
                chunk = pcm[i : min(i + 480, n_samples)]
                await gen.push_audio(rtc.AudioFrame(chunk.tobytes(), SAMPLE_RATE, 1, len(chunk)))
            if end:
                await gen.push_audio(AudioSegmentEnd())

        player = gen.__aiter__()
        await feed(len(pcm) // 2, end=False)  # first reply: half of it, then the user barges in
        played_first = 0
        while played_first < 40:  # let ~1.6 s play, so the producer has caught up and is waiting for audio
            item = await player.__anext__()
            if isinstance(item, rtc.AudioFrame):
                played_first += 1
            else:
                await asyncio.sleep(1 / stream.out_fps)
        await asyncio.sleep(0.5)
        await gen.clear_buffer()  # the interruption
        await feed(len(pcm), end=True)  # the next reply, in full
        out = []
        while True:
            item = await player.__anext__()
            if isinstance(item, AudioSegmentEnd):
                break
            if isinstance(item, rtc.AudioFrame):
                out.append(np.frombuffer(item.data, np.int16))
            else:
                await asyncio.sleep(1 / stream.out_fps)
        await gen.aclose()
        return np.concatenate(out)

    second = asyncio.run(asyncio.wait_for(go(), timeout=90))
    assert np.array_equal(second[: len(pcm)], pcm), "the reply after the interruption lost audio"
