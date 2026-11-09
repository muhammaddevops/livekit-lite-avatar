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
import math
import os
import sys
import time
from collections import deque
from collections.abc import Callable

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
from .stream import FPS, LOOKAHEAD, START_MARGIN, WIN, LiteAvatarStream, estimate, from_settings

logger = logging.getLogger("liteavatar.worker")
SAMPLE_RATE = 24000  # the agent side sends 24 kHz mono
_END = object()
LEAD_FRAMES = 8  # about the time to the first drawn frame of a reply
DRAW_BATCH = 6  # frames drawn per hand-over
FIRST_BATCH = 2  # each window's first hand-over is smaller, so it is ready sooner (drawing is faster than playback)
# Longest a reply's voice waits for its first frame once there is enough audio to draw it. A machine fit for live use
# has the first frame ready in well under half of this; on one that has fallen behind (overloaded, or too slow), the
# voice starts anyway and the lips join late, rather than the reply starting seconds late.
START_WAIT_S = 1.0
END_WAIT_FRAMES = 4  # once a reply's audio has all played, frames to wait for the producer before ending it anyway
FIRST_WINDOW_SAMPLES = (WIN + LOOKAHEAD + START_MARGIN) * SAMPLE_RATE // FPS  # audio a reply's first window needs
# When playback has caught up with drawing, the next frame drawn must be one that will still be ahead of playback
# once it is ready: as many frames ahead as drawing one takes, plus one.


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
        try:
            await self._publish_one_layer()
        except AttributeError:  # AvatarRunner's private attributes changed: use LiveKit's own (simulcast) publishing
            logger.warning("this livekit-agents version changed AvatarRunner; publishing with simulcast")
            await super()._publish_track()

    async def _publish_one_layer(self) -> None:
        async with self._lock:
            await self._room_connected_fut
            audio_track = rtc.LocalAudioTrack.create_audio_track("avatar_audio", self._audio_source)
            self._audio_publication = await self._room.local_participant.publish_track(
                audio_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            )
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
        self._pending: list[tuple[str, tuple]] = []  # notifications that arrived while restarting

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
        pending, self._pending = self._pending, []
        for name, args in pending:  # tell the agent what it missed, or it waits for playback to finish forever
            result = self._notify(name, *args)
            if asyncio.iscoroutine(result):
                await result

    async def start(self) -> None:
        await self._renew()

    def _notify(self, name: str, *args):
        try:
            return getattr(self._inner, name)(*args)
        except ChanClosed:
            logger.info("receiver restarting; %s will be sent once it is back", name)
            self._pending.append((name, args))
            return None

    def notify_playback_finished(self, playback_position: float, interrupted: bool):
        return self._notify("notify_playback_finished", playback_position, interrupted)

    def notify_playback_started(self):
        return self._notify("notify_playback_started")

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


class _Buffer:
    """Append-only array with amortised O(1) appends; view() is the filled part, without copying.

    clear() starts a fresh array rather than reusing the old one, so views handed out earlier (audio chunks
    still queued for playback) are never overwritten."""

    def __init__(self, dtype) -> None:
        self._dtype = dtype
        self.clear()

    def clear(self) -> None:
        self._a = np.zeros(1 << 16, self._dtype)
        self.n = 0

    def truncate(self, n: int) -> None:
        self.n = min(self.n, n)

    def extend(self, x: np.ndarray) -> None:
        need = self.n + len(x)
        if need > len(self._a):
            grown = np.zeros(max(need, 2 * len(self._a)), self._dtype)
            grown[: self.n] = self._a[: self.n]
            self._a = grown
        self._a[self.n : need] = x
        self.n = need

    def view(self) -> np.ndarray:
        return self._a[: self.n]


class Resampler24to16:
    """24 kHz int16 in, 16 kHz float32 out, incrementally; equal to scipy's resample_poly(x, 2, 3) on the whole
    reply so far. resample_poly's filter reaches only ~16 input samples either side, so when audio is appended
    only the last few outputs change: recompute from just before the old end (with some context) rather than
    resampling the whole reply again for every chunk, which grew quadratically on long replies."""

    CTX = 300  # input samples of context before the recomputed part (a multiple of 3)
    REDO = 120  # input samples before the old end whose outputs are recomputed (a multiple of 3)

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.x = _Buffer(np.float32)
        self.y = _Buffer(np.float32)

    def append(self, pcm: np.ndarray) -> np.ndarray:
        from scipy.signal import resample_poly

        old = self.x.n
        self.x.extend(pcm.astype(np.float32) / 32768.0)
        k = max(0, (old - self.REDO) // 3 * 3)  # first input sample whose outputs are recomputed
        i0 = max(0, k - self.CTX)
        seg = resample_poly(self.x.view()[i0:], 2, 3).astype(np.float32)
        self.y.truncate(k * 2 // 3)
        self.y.extend(seg[(k - i0) * 2 // 3 :])
        return self.y.view()


def _mono(frame: rtc.AudioFrame) -> np.ndarray:
    pcm = np.frombuffer(frame.data, np.int16)
    if frame.num_channels > 1:
        pcm = pcm.reshape(-1, frame.num_channels)[:, 0]
    return pcm


class _Reply:
    """One reply, from its first audio to its end. Its audio is taken in as it arrives (24 kHz int16 as received,
    and 16 kHz float32 for the models), so the player can play it even while the producer is busy drawing."""

    def __init__(self, epoch: int) -> None:
        self.epoch = epoch  # interruptions so far when it began: an interruption drops it
        self._buf = _Buffer(np.int16)
        self._rs = Resampler24to16()
        self.audio = self._buf.view()
        self.audio16 = np.zeros(0, np.float32)
        self.t_audio = 0.0  # when its first audio arrived (perf_counter)
        self.ended = False  # all of its audio has arrived
        self.produced = False  # the producer is done with it: its frames are all handed over, or given up
        self.finished = False  # the player has ended it, all its audio played: frames still being drawn are moot
        self.faceless = False  # drawing failed on it: the rest of its voice plays without new frames
        self.skipped = 0  # frames not drawn because their audio had already played
        self.start_by = 0.0  # when its voice may start without a frame (perf_counter; 0: not set yet)
        self.voice_first = False  # its voice started before its first frame was ready

    def add(self, pcm: np.ndarray) -> None:
        if not self.t_audio:
            self.t_audio = time.perf_counter()
        self._buf.extend(pcm)
        self.audio = self._buf.view()
        self.audio16 = self._rs.append(pcm)  # 24 kHz -> 16 kHz for the models


class LiteAvatarGenerator(VideoGenerator):
    """Audio in, (video, audio) frames out.

    The agent's audio is taken in as it arrives (push_audio). A producer task draws each reply's frames ahead of
    playback, and the player (this async iterator) plays them with their audio, and the idle loop between replies,
    so the video never stalls. The voice never waits long for the face:

    * if the next frame isn't ready (the CPU is slower than real time), the next audio chunk plays over the last
      picture and the producer skips the frames it missed;
    * if a reply's first frame isn't ready START_WAIT_S after there was audio enough to draw it, its voice starts
      over the idle video, and the lips join once frames are ready in time;
    * once all of a reply's audio has played, the reply ends, even if the producer is still drawing it, so the agent
      hears that playback finished on time;
    * if drawing fails, the rest of that reply's voice still plays.

    Lips get choppier on a slow machine, but speech starts and ends on time and stays smooth."""

    def __init__(self, stream: LiteAvatarStream, lip_delay_frames: int = 1) -> None:
        self._s = stream
        self._fps = stream.out_fps
        self._spf = SAMPLE_RATE // self._fps  # audio samples per video frame
        self._lip_delay = max(0, lip_delay_frames)
        self._replies: deque[_Reply] = deque()  # taken in and not yet ended by the player, oldest first
        self._intake: _Reply | None = None  # the reply whose audio is arriving
        self._wake = asyncio.Event()  # set when audio, or the end of a reply, arrives
        self._out: asyncio.Queue = asyncio.Queue()  # (epoch, reply, frame index, payload): producer -> player
        self._epoch = 0  # interruptions so far
        self._producer: asyncio.Task | None = None
        self._playing: _Reply | None = None  # the reply being played, and how many of its frames have played
        self._played = 0
        self._k = 0  # idle-video frame index of the next frame shown
        self._warned_rate = False
        self._frame_s = 0.5 / stream.out_fps  # recent time to draw one frame, for aiming ahead when behind
        self.av_sync: rtc.AVSynchronizer | None = None

    async def push_audio(self, frame: rtc.AudioFrame | AudioSegmentEnd) -> None:
        """Take in the agent's audio as it arrives: AvatarRunner calls this for every frame and every reply's end."""
        r = self._intake
        if r is None:  # the first audio of a new reply (or the end of one with no audio)
            r = self._intake = _Reply(self._epoch)
            self._replies.append(r)
        if isinstance(frame, AudioSegmentEnd):
            r.ended = True
            self._intake = None
        else:
            r.add(self._pcm(frame))
        self._wake.set()

    async def clear_buffer(self) -> None:
        """Drop the current reply at once (an interruption); AvatarRunner awaits this."""
        self._epoch += 1
        self._replies.clear()
        self._intake = self._playing = None
        while not self._out.empty():
            self._out.get_nowait()
        self._wake.set()  # a producer waiting for more of the dropped reply's audio moves on
        if self.av_sync is not None:
            await self.av_sync.clear_queue()

    def _vf(self, bgr: np.ndarray) -> rtc.VideoFrame:
        bgra = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
        return rtc.VideoFrame(self._s.width, self._s.height, rtc.VideoBufferType.BGRA, bgra.tobytes())

    def _af(self, audio: np.ndarray, i: int) -> rtc.AudioFrame:
        return rtc.AudioFrame(self._chunk(audio, i).tobytes(), SAMPLE_RATE, 1, self._spf)

    def _pcm(self, frame: rtc.AudioFrame) -> np.ndarray:
        if frame.sample_rate != SAMPLE_RATE and not self._warned_rate:
            self._warned_rate = True
            logger.warning("audio arrives at %d Hz, expected %d Hz; lips will drift", frame.sample_rate, SAMPLE_RATE)
        return _mono(frame)

    def _hold(self, last: np.ndarray | None) -> np.ndarray:
        """The picture to show while the voice is ahead of the face: the reply's last one, or else (its voice started
        before its first frame) the idle video."""
        if last is not None:
            return last
        img = self._s.move(self._s.idle_frame(self._k), self._k, 0.0)
        self._k += 1
        return img

    def _chunk(self, a24: np.ndarray, i: int) -> np.ndarray:
        """Audio for video frame i of the segment, zero-padded at the end."""
        spf = self._spf
        chunk = a24[i * spf : (i + 1) * spf]
        if len(chunk) < spf:
            chunk = np.concatenate([chunk, np.zeros(spf - len(chunk), np.int16)])
        return chunk

    # ---------- producer: draws each reply's frames, in order

    async def _produce(self) -> None:
        while True:
            r = next((x for x in self._replies if not x.produced), None)
            if r is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                await self._produce_reply(r)
            except asyncio.CancelledError:
                raise
            except Exception:  # never let a rendering error freeze the face or cut the voice short
                logger.exception("renderer error; the rest of this reply plays without new frames")
                r.faceless = True
                await asyncio.sleep(0.2)
            finally:
                r.produced = True

    async def _produce_reply(self, r: _Reply) -> None:
        """Draw one reply's frames and hand them to the player, until it is all drawn, ended or dropped."""
        s = self._s
        s.reset()
        emitted = 0  # frames handed to the player for this reply (drawn, skipped or idle)
        t_first = 0.0  # when its first drawn frame was ready

        async def put(payload, i: int) -> None:
            await self._out.put((r.epoch, r, i, payload))

        def gone() -> bool:  # interrupted, or ended by the player
            return r.epoch != self._epoch or r.finished

        while not gone():
            ended = r.ended
            s.audio = r.audio16
            if not s.ready(ended) and not (ended and len(r.audio)):
                if ended:  # a reply with no audio
                    await put(_END, 0)
                    return
                self._wake.clear()
                await self._wake.wait()
                continue
            s.audio = r.audio16.copy()  # what has arrived so far: more may arrive while this window is drawn
            if s.done == 0:  # first window: continue the idle motion from where it will be shown
                s.k_next = self._k + LEAD_FRAMES
            rows = await asyncio.to_thread(s.plan, ended)
            if gone():
                break
            if emitted == 0 and len(rows) and self._lip_delay:
                for _ in range(self._lip_delay):  # voice starts, lips follow a moment later
                    await put(None, emitted)
                    emitted += 1
            # draw a few frames at a time and hand each batch over at once, so playback starts early
            j = 0
            while j < len(rows) and not gone():
                playing = self._playing is r and self._played > 0
                played = self._played if playing else 0
                # frames playback moves on while one is drawn, plus one: playback may be part-way through a frame
                lead = math.ceil(self._frame_s * self._fps) + 1
                if playing and played + lead > emitted:
                    # the next frames would be late: skip to the first one that will be ready in time
                    n = min(len(rows) - j, played + lead - emitted)
                    s.skip(n)
                    r.skipped += n
                    emitted += n
                    j += n
                    continue
                # only just ahead of playback: one frame at a time, so each is ready in time; otherwise a few,
                # with each window's first frames early
                just_ahead = playing and emitted - played < lead + DRAW_BATCH
                size = 1 if just_ahead else (FIRST_BATCH if j == 0 else DRAW_BATCH)
                batch = rows[j : j + size]
                k0 = s.k_next
                t_draw = time.perf_counter()
                frames = await asyncio.to_thread(s.draw, batch)
                if frames:
                    per_frame = (time.perf_counter() - t_draw) / len(frames)
                    self._frame_s += 0.3 * (per_frame - self._frame_s)
                if gone():
                    break
                if frames and not t_first:
                    t_first = time.perf_counter()
                for jj, (f, energy) in enumerate(frames):
                    await put((k0 + jj, f, energy), emitted)
                    emitted += 1
                j += len(batch)
            if gone():
                break
            if ended and not s.ready(True):
                # any audio beyond the last drawn frame plays over idle frames
                while emitted * self._spf < len(r.audio):
                    await put(None, emitted)
                    emitted += 1
                await put(_END, emitted)
                self._log_reply(r, emitted, t_first)
                return
        if r.finished:
            self._log_reply(r, emitted, t_first)

    def _log_reply(self, r: _Reply, emitted: int, t_first: float) -> None:
        audio_s = len(r.audio) / SAMPLE_RATE
        frames = max(emitted, -(-len(r.audio) // self._spf))
        skipped = r.skipped + max(0, frames - emitted)  # frames never reached count as skipped too
        notes = [f"{skipped} of {frames} frames skipped to keep up"] if skipped else []
        if r.voice_first:
            notes.append("the voice started before the first frame")
        if r.finished:
            notes.append("the reply ended before the renderer caught up")
        log = logger.warning if notes else logger.info
        log(
            "reply: %.1fs audio, %s, real-time factor %.2f%s",
            audio_s,
            f"first frame {t_first - r.t_audio:.2f}s after its audio arrived" if t_first else "no frame drawn",
            self._s.stats["compute_s"] / audio_s if audio_s else 0.0,
            f"; {', '.join(notes)} (try a lower --fps)" if notes else "",
        )

    # ---------- player: what AvatarRunner publishes, one video frame and its audio at a time

    def _next_voice(self) -> _Reply | None:
        """The next reply, if its voice should start before its first frame: it has had audio enough to draw for
        START_WAIT_S (or drawing it failed), and has audio to play."""
        r = self._replies[0] if self._replies else None
        if r is None or r.finished or not len(r.audio):
            return None
        now = time.perf_counter()
        if r.faceless:
            r.start_by = now
        elif not r.start_by:
            if not (r.ended or len(r.audio) >= FIRST_WINDOW_SAMPLES):
                return None
            r.start_by = now + START_WAIT_S
        if now < r.start_by or (len(r.audio) < self._spf and not r.ended):
            return None
        return r

    def _end(self, r: _Reply) -> None:
        r.finished = True
        if r in self._replies:
            self._replies.remove(r)
        if self._playing is r:
            self._playing = None

    def __aiter__(self):
        return self._frames()

    async def _frames(self):
        if self._producer is None:
            self._producer = asyncio.create_task(self._produce())
        epoch = self._epoch
        last: np.ndarray | None = None  # the last picture shown in the current reply
        waited = 0  # frames shown since the current reply's audio all played, waiting for the producer to end it
        while True:
            if epoch != self._epoch:  # interrupted
                epoch, last, waited = self._epoch, None, 0
            try:
                ep, reply, i, payload = self._out.get_nowait()
            except asyncio.QueueEmpty:
                cur = self._playing
                if cur is None and (cur := self._next_voice()) is not None:
                    # its first frame is late: start its voice over the idle video, the lips follow
                    self._playing, self._played, last, waited = cur, 0, None, 0
                    cur.voice_first = True
                # whole chunks only while audio is still arriving (a partial one would be padded with silence and
                # the rest of it skipped); the reply's last chunk once it has all arrived
                if cur is not None:
                    whole = (self._played + 1) * self._spf <= len(cur.audio)
                    last_bit = cur.ended and self._played * self._spf < len(cur.audio)
                else:
                    whole = last_bit = False
                if whole or last_bit:
                    # the next frame isn't drawn yet: keep the voice going on the last picture
                    yield self._vf(self._hold(last))
                    if epoch == self._epoch and self._playing is cur:
                        yield self._af(cur.audio, self._played)
                        self._played += 1
                    continue
                if cur is not None and cur.ended:
                    # all of its audio has played and the producer is behind. Give it a moment (it may be about to
                    # hand over the frames that close the mouth), then end the reply, so the agent hears that
                    # playback finished when it did, not once the renderer has caught up
                    waited += 1
                    if cur.faceless or waited > END_WAIT_FRAMES:
                        self._end(cur)
                        last, waited = None, 0
                        yield AudioSegmentEnd()
                        continue
                    yield self._vf(self._hold(last))
                    continue
                yield self._vf(self._s.move(self._s.idle_frame(self._k), self._k, 0.0))
                self._k += 1
                continue
            if ep != self._epoch or reply.finished:  # stale, or a reply already ended
                continue
            waited = 0
            if payload is _END:
                if self._playing is reply:
                    # frames skipped at the very end of the reply: their audio still plays, on the last picture
                    while self._played * self._spf < len(reply.audio):
                        yield self._vf(self._hold(last))
                        if epoch != self._epoch or self._playing is not reply:
                            break
                        yield self._af(reply.audio, self._played)
                        self._played += 1
                if epoch == self._epoch:  # (an interruption is reported by AvatarRunner itself)
                    self._end(reply)
                    last = None
                    yield AudioSegmentEnd()
                continue
            if reply is not self._playing:  # a new reply starts
                self._playing, self._played, last = reply, 0, None
            if i < self._played:  # its audio already played over an earlier picture
                continue
            while self._played < i:  # frames the producer skipped to catch up
                yield self._vf(self._hold(last))
                if epoch != self._epoch or self._playing is not reply:
                    break
                yield self._af(reply.audio, self._played)
                self._played += 1
            if epoch != self._epoch or self._playing is not reply:
                continue
            if payload is None:
                img = self._s.move(self._s.idle_frame(self._k), self._k, 0.0)
                self._k += 1
            else:
                img = self._s.move(payload[1], payload[0], payload[2])
                self._k = payload[0] + 1
            last = img
            yield self._vf(img)
            if epoch == self._epoch and self._playing is reply:  # not interrupted while the frame was shown
                yield self._af(reply.audio, i)
                self._played = i + 1

    async def aclose(self) -> None:
        if self._producer is not None:
            self._producer.cancel()


async def run_session(
    url: str, token: str, stream: LiteAvatarStream, settings: Settings, report: Callable[[str], None] = print
) -> None:
    """Join the room and be the avatar until the agent leaves, the room closes or the time limit passes.
    report("JOINED") once in the room, or report("ERROR ...") if joining fails: the server waits for one of them
    before it tells the agent the session is accepted."""
    room = rtc.Room()
    done = asyncio.Event()
    room.on("disconnected", lambda *_: done.set())
    try:
        # the avatar only publishes; it has no use for anyone's tracks
        await asyncio.wait_for(room.connect(url, token, rtc.RoomOptions(auto_subscribe=False)), timeout=20)
    except Exception as e:  # noqa: BLE001 - reported to the server, which tells the agent
        report(f"ERROR could not join the room: {e!r}")
        return
    owner = room.local_participant.attributes.get("lk.publish_on_behalf")
    logger.info("joined %s as %s (for %s)", room.name, room.local_participant.identity, owner)
    report("JOINED")

    @room.on("participant_disconnected")
    def _left(p: rtc.RemoteParticipant) -> None:
        if p.identity == owner or not room.remote_participants:
            done.set()

    gen = LiteAvatarGenerator(stream, lip_delay_frames=settings.lip_delay_frames)
    runner = CrispAvatarRunner(
        room,
        audio_recv=ResilientAudioReceiver(room, sender_identity=owner),
        video_gen=gen,
        options=AvatarOptions(
            video_width=stream.width,
            video_height=stream.height,
            video_fps=stream.out_fps,
            audio_sample_rate=SAMPLE_RATE,
            audio_channels=1,
        ),
        bitrate=settings.bitrate,
        fps=stream.out_fps,
    )
    gen.av_sync = runner.av_sync
    try:
        await asyncio.wait_for(runner.start(), timeout=60)  # waits for the agent; it may already have gone
        await asyncio.wait_for(done.wait(), timeout=settings.max_session_s)
    except asyncio.TimeoutError:
        logger.info("leaving: the agent never arrived or the session time limit was reached")
    finally:
        for close in (runner.aclose, gen.aclose):
            try:
                await close()
            except Exception as e:  # noqa: BLE001 - e.g. the runner never started
                logger.debug("closing: %s", e)
        await room.disconnect()
        logger.info("left %s", room.name)


def load(settings: Settings) -> tuple[LiteAvatarStream, dict]:
    t = time.perf_counter()
    s = from_settings(settings)
    load_s = time.perf_counter() - t
    est = estimate(s)
    logger.info(
        "models loaded in %.1fs (%dx%d @ %d fps, presenter %s); estimated real-time factor %.2f",
        load_s,
        s.width,
        s.height,
        s.out_fps,
        settings.presenter,
        est["real_time_factor"],
    )
    if est["real_time_factor"] > 0.85:
        hint = f"try --fps {est['suggested_fps']}" if est["suggested_fps"] else "this CPU is too slow for live use"
        logger.warning(
            "this machine draws at an estimated real-time factor of %.2f at %d fps, so replies will skip frames "
            "(%.0f ms per frame, %d ms of speech features per second of speech): %s",
            est["real_time_factor"],
            s.out_fps,
            est["ms_per_frame"],
            est["speech_features_ms_per_s"],
            hint,
        )
    return s, est


def main(
    settings: Settings, *, warm: bool, url: str | None = None, token: str | None = None, presenter: str | None = None
) -> None:
    stream, est = load(settings)
    report = print
    if warm:
        # stdout is the line protocol with the server (READY, then JOINED or ERROR). Keep a private copy of it and
        # point fd 1 at stderr, so nothing else (LiteAvatar's prints, native libraries) can write into the pipe.
        proto = os.fdopen(os.dup(1), "w", buffering=1)
        os.dup2(2, 1)
        sys.stdout = sys.stderr

        def report(line: str) -> None:
            proto.write(line.replace("\n", " ") + "\n")

        report("READY " + json.dumps(est))
        line = sys.stdin.readline()
        if not line.strip():  # the server went away before handing us a session
            return
        job = json.loads(line)
        url, token, presenter = job["url"], job["token"], job.get("presenter")
    if not url or not token:
        raise SystemExit("worker needs --url and --token (or --warm)")
    if presenter and presenter != settings.presenter:
        t = time.perf_counter()
        try:
            stream.load_presenter(settings.presenter_dir(presenter))
        except Exception as e:  # noqa: BLE001 - reported to the server, which tells the agent
            report(f"ERROR could not load presenter {presenter}: {e!r}")
            return
        logger.info("switched to presenter %s in %.1fs", presenter, time.perf_counter() - t)
    asyncio.run(run_session(url, token, stream, settings, report))
