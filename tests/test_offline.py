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


def test_very_slow_cpu_still_plays_the_end_of_a_reply(stream, wav):
    """At 0.3 s per frame, catching up can skip the last frames of a reply: their audio must still play."""
    from livekit_lite_avatar.renderer.worker import SAMPLE_RATE

    pcm, seq, played, _drawn = _play_reply(stream, wav, extra_draw_s=0.3)
    _check_reply(pcm, seq, played, SAMPLE_RATE // stream.out_fps)  # whole and unchanged, to the last chunk


def test_an_overloaded_renderer_starts_and_ends_the_voice_on_time(stream, wav):
    """When speech features take far longer than real time (an overloaded machine: 3 s per window here), the voice
    doesn't wait for the face: it starts START_WAIT_S after the audio arrived, plays whole and smooth, and the reply
    ends once its audio has played, not once the renderer has caught up, so the agent hears that playback finished
    on time. The frames the renderer finishes later change nothing, and the next reply plays in full."""
    import librosa
    import numpy as np
    from livekit import rtc
    from livekit.agents.voice.avatar import AudioSegmentEnd

    from livekit_lite_avatar.renderer.worker import END_WAIT_FRAMES, SAMPLE_RATE, START_WAIT_S, LiteAvatarGenerator

    audio, _ = librosa.load(wav, sr=SAMPLE_RATE, mono=True)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    spf = SAMPLE_RATE // stream.out_fps
    stream.reset()
    gen = LiteAvatarGenerator(stream, lip_delay_frames=SETTINGS.lip_delay_frames)
    real_plan, plans = stream.plan, [0]

    def overloaded_plan(final):
        plans[0] += 1
        time.sleep(3.0)
        return real_plan(final)

    async def go():
        loop = asyncio.get_running_loop()
        player = gen.__aiter__()

        async def feed(a: np.ndarray) -> None:
            for i in range(0, len(a), 480):
                chunk = a[i : i + 480]
                await gen.push_audio(rtc.AudioFrame(chunk.tobytes(), SAMPLE_RATE, 1, len(chunk)))
            await gen.push_audio(AudioSegmentEnd())

        async def play(until_end: bool, seconds: float = 60.0) -> tuple[list, list, float, float, int]:
            """Play like AVSynchronizer; returns (sequence, audio, first audio time, end time, ends seen)."""
            seq, out, t_audio, t_end, ends = [], [], 0.0, 0.0, 0
            stop = loop.time() + seconds
            while loop.time() < stop:
                item = await player.__anext__()
                if isinstance(item, AudioSegmentEnd):
                    ends += 1
                    t_end = loop.time()
                    if until_end:
                        break
                elif isinstance(item, rtc.AudioFrame):
                    t_audio = t_audio or loop.time()
                    seq.append(("a", item.samples_per_channel))
                    out.append(np.frombuffer(item.data, np.int16))
                else:
                    seq.append(("v", zlib.crc32(bytes(item.data))))
                    await asyncio.sleep(1 / stream.out_fps)
            return seq, out, t_audio, t_end, ends

        stream.plan = overloaded_plan
        t0 = loop.time()
        await feed(pcm)
        seq, out, t_audio, t_end, ends = await play(until_end=True)
        # the windows the renderer still finishes for that reply (it needs ~12 s) must not come out: no second end,
        # no more of its audio
        _, late_audio, _, _, late_ends = await play(until_end=False, seconds=5.0)
        stream.plan = real_plan
        await feed(pcm)  # the next reply, on a machine that keeps up again
        _, second, _, _, _ = await play(until_end=True)
        await gen.aclose()
        return seq, np.concatenate(out), t_audio - t0, t_end - t0, ends, late_audio, late_ends, second

    try:
        seq, played, start_s, end_s, ends, late_audio, late_ends, second = asyncio.run(
            asyncio.wait_for(go(), timeout=120)
        )
    finally:
        stream.plan = real_plan
    _pairs, stalls = _check_reply(pcm, seq, played, spf)
    speech_s = len(pcm) / SAMPLE_RATE
    print(
        f"\noverloaded: voice at {start_s:.2f}s, ended at {end_s:.2f}s for {speech_s:.2f}s of speech "
        f"({plans[0]} windows planned at 3 s each); {stalls} stalls"
    )
    assert start_s < START_WAIT_S + (0.5 if PERF else 1.5), "the voice waited for the first frame"
    assert stalls == 0, "the voice paused"
    # it ends a few frames after its audio has played, while the renderer is still far behind
    assert end_s - start_s < speech_s + (END_WAIT_FRAMES + 3) / stream.out_fps + (0.5 if PERF else 1.5)
    assert ends == 1 and late_ends == 0 and not late_audio, "the reply ended more than once, or played on after it"
    assert np.array_equal(np.concatenate(second)[: len(pcm)], pcm), "the next reply lost audio"


def test_a_renderer_error_keeps_the_voice_and_ends_the_reply_once(stream, wav):
    """If drawing fails mid-reply, the rest of that reply's voice still plays (on the last picture), the reply ends
    exactly once, and the next reply plays in full with its face back."""
    import librosa
    import numpy as np
    from livekit import rtc
    from livekit.agents.voice.avatar import AudioSegmentEnd

    from livekit_lite_avatar.renderer.worker import SAMPLE_RATE, LiteAvatarGenerator

    audio, _ = librosa.load(wav, sr=SAMPLE_RATE, mono=True)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    stream.reset()
    gen = LiteAvatarGenerator(stream, lip_delay_frames=SETTINGS.lip_delay_frames)
    real_plan, real_draw, calls, drawn = stream.plan, stream.draw, [0], [0]

    def failing_plan(final):
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError("simulated renderer failure")
        return real_plan(final)

    def counting_draw(rows):
        drawn[0] += len(rows)
        return real_draw(rows)

    async def go():
        loop = asyncio.get_running_loop()
        player = gen.__aiter__()

        async def feed(a: np.ndarray, end: bool) -> None:
            for i in range(0, len(a), 480):
                chunk = a[i : i + 480]
                await gen.push_audio(rtc.AudioFrame(chunk.tobytes(), SAMPLE_RATE, 1, len(chunk)))
            if end:
                await gen.push_audio(AudioSegmentEnd())

        async def play(seconds: float = 60.0, until_end: bool = True) -> tuple[list, int]:
            """Play like AVSynchronizer: (audio chunks, reply ends seen)."""
            out, ends = [], 0
            stop = loop.time() + seconds
            while loop.time() < stop:
                item = await player.__anext__()
                if isinstance(item, AudioSegmentEnd):
                    ends += 1
                    if until_end:
                        break
                elif isinstance(item, rtc.AudioFrame):
                    out.append(np.frombuffer(item.data, np.int16))
                else:
                    await asyncio.sleep(1 / stream.out_fps)
            return out, ends

        async def the_rest() -> None:  # arrives after the failure, then the reply's end
            await asyncio.sleep(1.0)
            await feed(pcm[len(pcm) // 2 :], end=True)

        stream.plan = failing_plan
        await feed(pcm[: len(pcm) // 2], end=False)  # the first half: drawing fails on its second window
        rest = asyncio.create_task(the_rest())
        first, _ = await play()
        await rest
        stream.plan = real_plan
        late, late_ends = await play(seconds=1.5, until_end=False)  # nothing more of it, and no second end
        stream.draw = counting_draw
        await feed(pcm, end=True)  # the next reply
        second, _ = await play()
        await gen.aclose()
        return np.concatenate(first), late, late_ends, np.concatenate(second)

    try:
        first, late, late_ends, second = asyncio.run(asyncio.wait_for(go(), timeout=90))
    finally:
        stream.plan, stream.draw = real_plan, real_draw
    assert calls[0] >= 2, "the simulated failure didn't happen"
    assert np.array_equal(first[: len(pcm)], pcm), "the voice of the reply the renderer failed on was cut short"
    assert late_ends == 0 and not late, "the failed reply ended twice, or played on after its end"
    assert np.array_equal(second[: len(pcm)], pcm), "the reply after the failure lost audio"
    assert drawn[0] > 0, "the face didn't come back after the failure"
