"""LiteAvatar renderer for one LiveKit room (CPU only).

Joins the room as the avatar participant, receives the agent's TTS audio over LiveKit data streams
(livekit-agents' DataStreamAudioReceiver), draws lip-synced frames with LiteAvatarStream, and publishes video and
the same audio in sync (AvatarRunner / AVSynchronizer) on behalf of the agent. Between replies it plays the
presenter's idle loop. One process serves one session and then exits.

Normally started by the server, which keeps workers preloaded ("warm") and hands each one a session:
    liteavatar-renderer worker --warm                 # loads models, prints READY, reads one JSON line
    liteavatar-renderer worker --url wss://... --token ...   # join one room directly, for debugging
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time

import cv2
import numpy as np
from livekit import rtc
from livekit.agents.utils.aio import ChanClosed
from livekit.agents.voice.avatar import (
    AudioReceiver,
    AudioSegmentEnd,
    AvatarOptions,
    AvatarRunner,
    DataStreamAudioReceiver,
    VideoGenerator,
)
from livekit.agents.voice.avatar._datastream_io import AUDIO_STREAM_TOPIC

from .settings import Settings
from .stream import LiteAvatarStream, from_settings

logger = logging.getLogger("liteavatar.worker")
SAMPLE_RATE = 24000  # the agent side sends 24 kHz mono
_END = object()
LEAD_FRAMES = 8      # about the time to the first drawn frame of a reply
DRAW_BATCH = 6       # frames drawn per hand-over


class CrispAvatarRunner(AvatarRunner):
    """AvatarRunner that publishes one full-quality video layer instead of simulcast.

    With simulcast the browser picks a layer by the video element's on-screen size: a smaller player got a
    360x495 copy at ~20 fps, visibly softer and choppier (and choppy lips read as out of sync). One layer at a
    proper bitrate keeps every viewer on the full picture. Overrides AvatarRunner._publish_track (livekit-agents
    1.8.x); if that private method ever disappears, LiveKit's default publishing is used instead."""

    def __init__(self, *args, bitrate: int, fps: float, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._bitrate = bitrate
        self._fps = fps

    async def _publish_track(self) -> None:
        async with self._lock:
            await self._room_connected_fut
            audio_track = rtc.LocalAudioTrack.create_audio_track("avatar_audio", self._audio_source)
            self._audio_publication = await self._room.local_participant.publish_track(
                audio_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
            await self._audio_publication.wait_for_subscription()
            video_track = rtc.LocalVideoTrack.create_video_track("avatar_video", self._video_source)
            opts = rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_CAMERA,
                simulcast=False,
                video_encoding=rtc.VideoEncoding(max_bitrate=self._bitrate, max_framerate=float(self._fps)),
            )
            self._video_publication = await self._room.local_participant.publish_track(video_track, opts)


if not hasattr(AvatarRunner, "_publish_track"):  # pragma: no cover - guards a future livekit-agents change
    logger.warning("this livekit-agents version has no AvatarRunner._publish_track; publishing with simulcast")
    del CrispAvatarRunner._publish_track


class ResilientAudioReceiver(AudioReceiver):
    """DataStreamAudioReceiver that survives a broken stream.

    LiveKit's receiver stops for good when one audio stream errors (seen live: "expected chunk index to be exactly
    one more than the previous") or a playback RPC times out, which would leave the face silent for the rest of the
    call. This ends the broken reply and opens a fresh receiver, so the next reply plays normally."""

    def __init__(self, room: rtc.Room, sender_identity: str | None) -> None:
        super().__init__()
        self._room = room
        self._sender = sender_identity
        self._inner: DataStreamAudioReceiver | None = None

    async def _renew(self) -> None:
        if self._inner is not None:
            self._room.unregister_byte_stream_handler(AUDIO_STREAM_TOPIC)
            try:
                await self._inner.aclose()
            except Exception as e:  # noqa: BLE001
                logger.debug("closing the old audio receiver failed: %s", e)
        self._inner = DataStreamAudioReceiver(self._room, sender_identity=self._sender)
        self._inner.on("clear_buffer", lambda *_: self.emit("clear_buffer"))
        await self._inner.start()

    async def start(self) -> None:
        await self._renew()

    def _notify(self, fn, *args):
        try:
            return fn(*args)
        except ChanClosed:
            logger.warning("playback notification dropped (receiver restarting)")
            return None

    def notify_playback_finished(self, playback_position: float, interrupted: bool):
        return self._notify(self._inner.notify_playback_finished, playback_position, interrupted)

    def notify_playback_started(self):
        return self._notify(self._inner.notify_playback_started)

    def __aiter__(self):
        return self._frames()

    async def _frames(self):
        while True:
            try:
                async for frame in self._inner:
                    yield frame
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("audio stream from the agent broke (%s); restarting the receiver", e)
                yield AudioSegmentEnd()  # finish the broken reply so the agent isn't left waiting
                await asyncio.sleep(0.05)
                await self._renew()

    async def aclose(self) -> None:
        if self._inner is not None:
            await self._inner.aclose()


def _mono(frame: rtc.AudioFrame) -> np.ndarray:
    pcm = np.frombuffer(frame.data, np.int16)
    if frame.num_channels > 1:
        pcm = pcm.reshape(-1, frame.num_channels)[:, 0]
    return pcm


class LiteAvatarGenerator(VideoGenerator):
    """Audio in, (video, audio) frames out. A producer task renders ahead; the iterator plays idle frames
    whenever nothing rendered is waiting, so the video never stalls."""

    def __init__(self, stream: LiteAvatarStream, lip_delay_frames: int = 1) -> None:
        self._s = stream
        self._fps = stream.out_fps
        self._spf = SAMPLE_RATE // self._fps  # audio samples per video frame
        self._lip_delay = max(0, lip_delay_frames)
        self._in: asyncio.Queue = asyncio.Queue()
        self._out: asyncio.Queue = asyncio.Queue()
        self._epoch = 0
        self._producer: asyncio.Task | None = None
        self._k = 0  # idle-video frame index of the next frame shown
        self._warned_rate = False
        self.av_sync: rtc.AVSynchronizer | None = None

    async def push_audio(self, frame: rtc.AudioFrame | AudioSegmentEnd) -> None:
        await self._in.put((self._epoch, frame))

    def clear_buffer(self) -> None:
        self._epoch += 1
        for q in (self._in, self._out):
            while not q.empty():
                q.get_nowait()
        if self.av_sync is not None:
            asyncio.ensure_future(self.av_sync.clear_queue())

    def _vf(self, bgr: np.ndarray) -> rtc.VideoFrame:
        bgra = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
        return rtc.VideoFrame(self._s.width, self._s.height, rtc.VideoBufferType.BGRA, bgra.tobytes())

    def _pcm(self, frame: rtc.AudioFrame) -> np.ndarray:
        if frame.sample_rate != SAMPLE_RATE and not self._warned_rate:
            self._warned_rate = True
            logger.warning("audio arrives at %d Hz, expected %d Hz; lips will drift", frame.sample_rate, SAMPLE_RATE)
        return _mono(frame)

    def _chunk(self, a24: np.ndarray, i: int) -> np.ndarray:
        """Audio for video frame i of the segment, zero-padded at the end."""
        spf = self._spf
        chunk = a24[i * spf:(i + 1) * spf]
        if len(chunk) < spf:
            chunk = np.concatenate([chunk, np.zeros(spf - len(chunk), np.int16)])
        return chunk

    async def _produce(self) -> None:
        while True:  # never let a rendering error silently freeze the face
            try:
                await self._produce_loop()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("renderer error; resetting")
                self.clear_buffer()
                await asyncio.sleep(0.2)

    async def _produce_loop(self) -> None:
        from scipy.signal import resample_poly

        epoch = self._epoch
        a24 = np.zeros(0, np.int16)  # this segment's audio, as received (24 kHz)
        emitted = 0                   # video frames already handed out for this segment
        ended = False

        def reset() -> None:
            nonlocal a24, emitted, ended
            a24, emitted, ended = np.zeros(0, np.int16), 0, False
            self._s.reset()

        while True:
            if epoch != self._epoch:
                epoch = self._epoch
                reset()
            # wait for audio unless there is buffered work to do
            if not self._s.ready(ended) and not (ended and len(a24)):
                ep, item = await self._in.get()
                if ep != self._epoch:
                    continue
                if isinstance(item, AudioSegmentEnd):
                    ended = True
                    if not len(a24):
                        await self._out.put((epoch, _END, None))
                        reset()
                    continue
                parts = [self._pcm(item)]
                # drain anything else already queued before doing heavy work
                while not self._in.empty():
                    ep, item = self._in.get_nowait()
                    if ep != self._epoch:
                        continue
                    if isinstance(item, AudioSegmentEnd):
                        ended = True
                        break
                    parts.append(self._pcm(item))
                a24 = np.concatenate([a24, *parts])
                x = a24.astype(np.float32) / 32768.0
                self._s.audio = resample_poly(x, 2, 3).astype(np.float32)  # 24 kHz -> 16 kHz for the models
                continue
            if self._s.done == 0:  # first window: continue the idle motion from where it will be shown
                self._s.k_next = self._k + LEAD_FRAMES
            rows = await asyncio.to_thread(self._s.plan, ended)
            if emitted == 0 and len(rows) and self._lip_delay:
                for _ in range(self._lip_delay):  # voice starts, lips follow a moment later
                    await self._out.put((epoch, None, self._chunk(a24, emitted)))
                    emitted += 1
            # draw a few frames at a time and hand each batch over at once, so playback starts early
            for b0 in range(0, len(rows), DRAW_BATCH):
                if epoch != self._epoch:
                    break
                k0 = self._s.k_next
                frames = await asyncio.to_thread(self._s.draw, rows[b0:b0 + DRAW_BATCH])
                if epoch != self._epoch:
                    break
                for j, (f, energy) in enumerate(frames):
                    await self._out.put((epoch, (k0 + j, f, energy), self._chunk(a24, emitted)))
                    emitted += 1
            if epoch != self._epoch:
                continue
            if ended and not self._s.ready(True):
                # any audio beyond the last drawn frame plays over idle frames
                while emitted * self._spf < len(a24):
                    await self._out.put((epoch, None, self._chunk(a24, emitted)))
                    emitted += 1
                await self._out.put((epoch, _END, None))
                logger.info("reply: %.1fs audio, %d windows, %.2fs compute", len(a24) / SAMPLE_RATE,
                            self._s.stats["windows"], self._s.stats["compute_s"])
                reset()

    def __aiter__(self):
        return self._frames()

    async def _frames(self):
        if self._producer is None:
            self._producer = asyncio.create_task(self._produce())
        while True:
            try:
                ep, frame, chunk = self._out.get_nowait()
            except asyncio.QueueEmpty:
                yield self._vf(self._s.move(self._s.idle_frame(self._k), self._k, 0.0))
                self._k += 1
                continue
            if ep != self._epoch:
                continue
            if frame is _END:
                yield AudioSegmentEnd()
                continue
            if frame is None:
                img = self._s.move(self._s.idle_frame(self._k), self._k, 0.0)
                self._k += 1
            else:
                img = self._s.move(frame[1], frame[0], frame[2])
                self._k = frame[0] + 1
            yield self._vf(img)
            yield rtc.AudioFrame(chunk.tobytes(), SAMPLE_RATE, 1, self._spf)

    async def aclose(self) -> None:
        if self._producer is not None:
            self._producer.cancel()


async def run_session(url: str, token: str, stream: LiteAvatarStream, settings: Settings) -> None:
    room = rtc.Room()
    done = asyncio.Event()
    room.on("disconnected", lambda *_: done.set())
    await room.connect(url, token)
    owner = room.local_participant.attributes.get("lk.publish_on_behalf")
    logger.info("joined %s as %s (for %s)", room.name, room.local_participant.identity, owner)

    @room.on("participant_disconnected")
    def _left(p: rtc.RemoteParticipant) -> None:
        if p.identity == owner or not room.remote_participants:
            done.set()

    gen = LiteAvatarGenerator(stream, lip_delay_frames=settings.lip_delay_frames)
    runner = CrispAvatarRunner(
        room,
        audio_recv=ResilientAudioReceiver(room, sender_identity=owner),
        video_gen=gen,
        options=AvatarOptions(video_width=stream.width, video_height=stream.height, video_fps=stream.out_fps,
                              audio_sample_rate=SAMPLE_RATE, audio_channels=1),
        bitrate=settings.bitrate,
        fps=stream.out_fps,
    )
    gen.av_sync = runner.av_sync
    await runner.start()
    try:
        await asyncio.wait_for(done.wait(), timeout=settings.max_session_s)
    except asyncio.TimeoutError:
        logger.info("session time limit reached")
    finally:
        await runner.aclose()
        await gen.aclose()
        await room.disconnect()
        logger.info("left %s", room.name)


def load(settings: Settings) -> LiteAvatarStream:
    t = time.perf_counter()
    s = from_settings(settings)
    logger.info("models loaded in %.1fs (%dx%d @ %d fps, presenter %s)", time.perf_counter() - t, s.width, s.height,
                s.out_fps, settings.presenter)
    return s


def main(settings: Settings, *, warm: bool, url: str | None = None, token: str | None = None,
         presenter: str | None = None) -> None:
    stream = load(settings)
    if warm:
        print("READY", flush=True)
        sys.stdout = sys.stderr  # nobody reads our stdout after READY
        line = sys.stdin.readline()
        if not line.strip():  # the server went away before handing us a session
            return
        job = json.loads(line)
        url, token, presenter = job["url"], job["token"], job.get("presenter")
    if not url or not token:
        raise SystemExit("worker needs --url and --token (or --warm)")
    if presenter and presenter != settings.presenter:
        t = time.perf_counter()
        stream.load_presenter(settings.presenter_dir(presenter))
        logger.info("switched to presenter %s in %.1fs", presenter, time.perf_counter() - t)
    asyncio.run(run_session(url, token, stream, settings))
