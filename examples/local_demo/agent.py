"""Demo agent with no STT, LLM or TTS: it greets you with three recorded lines, then repeats what you say.

Your voice goes through the same path a TTS reply would, so you see the avatar lip-sync to your own speech. Started
by run.py next to it; the greeting was made with Kokoro (Apache-2.0).

DEMO_STYLE picks the avatar:
- "video" (default): the LiteAvatar renderer draws the face and publishes it as video (session.say -> data stream ->
  renderer -> the room).
- "3d": no avatar here at all. The agent publishes its own voice, as any agent does, and the web page draws a 3D face
  that lip-syncs to it (web/, examples/local_demo/3d.html).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from pathlib import Path

import numpy as np
import soundfile as sf
from livekit import rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli, room_io

from livekit_lite_avatar import LiteAvatarSession

logger = logging.getLogger("liteavatar-demo")
AGENT_NAME = "liteavatar-demo"
AUDIO = Path(__file__).resolve().parent / "audio"
GREETING = [
    (
        "hello.flac",
        "Hi! I'm a LiveKit agent, and this face is being drawn live, on a laptop's CPU. "
        "No GPU, and no per-minute fees.",
    ),
    (
        "how.flac",
        "My voice streams to a renderer over LiveKit, which publishes my video and audio back into the room, in sync.",
    ),
    ("now.flac", "Now say something, and I will repeat it, lip-synced."),
]
GREETING_3D = [
    (
        "hello_3d.flac",
        "Hi! I'm a LiveKit agent, and this face is drawn right here, in your browser. No video, and no extra server.",
    ),
    ("how_3d.flac", "Your browser hears my voice, and moves my lips to it, so the agent stays exactly as it was."),
    ("now_3d.flac", "Now say something, and I will repeat it, lip-synced."),
]
STYLE = os.getenv("DEMO_STYLE", "video")
RATE = 24000
FRAME_MS = 20
SPEECH_DB = -45.0  # louder than this counts as speech
END_SILENCE_MS = 700  # this much quiet ends an utterance
MIN_SPEECH_MS = 300
MAX_UTTERANCE_S = 15.0
PRE_ROLL_MS = 200  # keep a little audio from before speech started, so first syllables aren't clipped


def own_jobs_load(server: AgentServer) -> float:
    """This worker's load as its share of calls, not the machine's CPU. The renderer keeps the CPU busy when it runs on
    the same machine, and LiveKit's default (CPU above 70%) would then stop this worker from taking calls."""
    try:
        return min(1.0, len(server.active_jobs) / 4)
    except AttributeError:  # not running yet
        return 0.0


server = AgentServer(load_fnc=own_jobs_load)


async def frames(audio: np.ndarray, sr: int = RATE):
    n = sr * FRAME_MS // 1000
    for i in range(0, len(audio), n):
        chunk = audio[i : i + n]
        yield rtc.AudioFrame(chunk.tobytes(), sr, 1, len(chunk))
        await asyncio.sleep(0)


def load(name: str) -> np.ndarray:
    data, _ = sf.read(AUDIO / name, dtype="int16", always_2d=True)
    return np.ascontiguousarray(data[:, 0])


async def utterances(track: rtc.RemoteAudioTrack, busy: asyncio.Event):
    """Yield each thing the user says, as 24 kHz int16 audio, using a simple loudness test."""
    pre: deque[np.ndarray] = deque(maxlen=PRE_ROLL_MS // FRAME_MS)
    buf: list[np.ndarray] = []
    voiced = quiet = 0
    async for ev in rtc.AudioStream(track, sample_rate=RATE, num_channels=1, frame_size_ms=FRAME_MS):
        a = np.frombuffer(ev.frame.data, np.int16)
        if busy.is_set():  # half-duplex: don't record the avatar talking
            buf, voiced, quiet = [], 0, 0
            pre.clear()
            continue
        loud = 20 * np.log10(np.sqrt(np.mean((a.astype(np.float32) / 32768) ** 2)) + 1e-9) > SPEECH_DB
        if loud:
            if not buf:
                buf.extend(pre)
            buf.append(a)
            voiced += FRAME_MS
            quiet = 0
        elif buf:
            buf.append(a)
            quiet += FRAME_MS
        else:
            pre.append(a)
        long_enough = len(buf) * FRAME_MS >= MAX_UTTERANCE_S * 1000
        if buf and (quiet >= END_SILENCE_MS or long_enough):
            if voiced >= MIN_SPEECH_MS:
                yield np.concatenate(buf)
            buf, voiced, quiet = [], 0, 0


async def mic_track(room: rtc.Room, person: rtc.RemoteParticipant) -> rtc.RemoteAudioTrack:
    """The person's microphone track, once they turn it on. (Not just any microphone track: the avatar publishes
    the agent's voice as one too.)"""
    got: asyncio.Future = asyncio.get_running_loop().create_future()

    def check(track: rtc.Track, pub: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant) -> None:
        mine = participant.identity == person.identity and pub.source == rtc.TrackSource.SOURCE_MICROPHONE
        if mine and not got.done():
            got.set_result(track)

    for pub in person.track_publications.values():
        if pub.track is not None:
            check(pub.track, pub, person)
    room.on("track_subscribed", check)
    try:
        return await got
    finally:
        room.off("track_subscribed", check)


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    session = AgentSession()
    await ctx.connect()
    avatar = None
    if STYLE != "3d":
        avatar = LiteAvatarSession(presenter=os.getenv("DEMO_PRESENTER") or None, avatar_participant_name="Avatar")
        await avatar.start(session, room=ctx.room)
    await session.start(
        agent=Agent(instructions="Demo agent."),
        room=ctx.room,
        room_options=room_io.RoomOptions(audio_input=False, text_input=False),
    )
    if avatar:
        await avatar.wait_for_join(timeout=120)
        await asyncio.sleep(1.0)
    else:
        # the page draws the face from the agent's own voice. RoomIO holds that audio until someone has subscribed to
        # it; a moment more lets the page attach it before the first word
        await ctx.wait_for_participant()
        await asyncio.sleep(1.5)
    busy = asyncio.Event()

    async def say(text: str, audio: np.ndarray) -> None:
        busy.set()
        try:
            handle = session.say(text, audio=frames(audio))
            await handle.wait_for_playout()
            await asyncio.sleep(0.3)
        finally:
            busy.clear()

    for name, text in GREETING_3D if STYLE == "3d" else GREETING:
        logger.info("greeting: %s", name)
        await say(text, load(name))
    logger.info("greeting done")

    person = await ctx.wait_for_participant()  # a person, not the avatar (an agent-kind participant)
    track = await mic_track(ctx.room, person)
    logger.info("echoing the user's microphone")
    async for speech in utterances(track, busy):
        logger.info("repeating %.1fs of speech", len(speech) / RATE)
        await say("(your words, repeated)", speech)


if __name__ == "__main__":
    cli.run_app(server)
