"""Test agent for the end-to-end run: a real AgentSession with a LiteAvatar face, but no STT, LLM or TTS.

It speaks WAV files with `session.say(..., audio=...)`, optionally interrupting one, and publishes a JSON report
as a participant attribute (`e2e.report`) for the viewer to read. Started by tests/e2e/run.py as:

    python tests/e2e/agent.py start      (LIVEKIT_* and LITEAVATAR_RENDERER_URL set; the plan arrives as job metadata)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import numpy as np
import soundfile as sf
from livekit import rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli, room_io

from livekit_lite_avatar import LiteAvatarSession

logger = logging.getLogger("e2e-agent")
AGENT_NAME = "liteavatar-e2e"


def own_jobs_load(server: AgentServer) -> float:
    """This worker's load as its share of calls, not the machine's CPU. The renderer keeps the CPU busy when it runs on
    the same machine, and LiveKit's default (CPU above 70%) would then stop this worker from taking calls."""
    try:
        return min(1.0, len(server.active_jobs) / 4)
    except AttributeError:  # not running yet
        return 0.0


server = AgentServer(load_fnc=own_jobs_load)


async def wav_frames(path: str, frame_ms: int = 20):
    """A WAV file as 20 ms mono int16 frames, the way a TTS stream arrives."""
    data, sr = sf.read(path, dtype="int16", always_2d=True)
    mono = np.ascontiguousarray(data[:, 0])
    n = sr * frame_ms // 1000
    for i in range(0, len(mono), n):
        chunk = mono[i : i + n]
        yield rtc.AudioFrame(chunk.tobytes(), sr, 1, len(chunk))
        await asyncio.sleep(0)


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    plan = json.loads(ctx.job.metadata)
    report: dict = {"speech": []}
    session = AgentSession()
    await ctx.connect()

    avatar = LiteAvatarSession(presenter=plan.get("presenter"))
    t0 = time.time()
    await avatar.start(session, room=ctx.room)
    report["renderer_accepted_s"] = round(time.time() - t0, 3)
    await session.start(
        agent=Agent(instructions="End-to-end test agent."),
        room=ctx.room,
        room_options=room_io.RoomOptions(audio_input=False, text_input=False),
    )
    await avatar.wait_for_join(timeout=90)
    report["avatar_joined_s"] = round(time.time() - t0, 3)
    report["agent_identity"] = ctx.room.local_participant.identity

    await asyncio.sleep(plan.get("idle_before_s", 2.0))  # let the viewer see the idle loop first
    speaking = asyncio.Event()  # the agent turns "speaking" when the renderer reports playback started
    session.on("agent_state_changed", lambda ev: speaking.set() if ev.new_state == "speaking" else None)
    for item in plan["speech"]:
        speaking.clear()
        said_at = time.time()
        handle = session.say(
            item.get("text") or "(test speech)", audio=wav_frames(item["wav"]), allow_interruptions=True
        )
        entry = {"wav": os.path.basename(item["wav"]), "said_at": said_at}
        if item.get("interrupt_after_s"):
            await asyncio.wait_for(speaking.wait(), timeout=30)
            entry["playback_started_at"] = time.time()
            await asyncio.sleep(item["interrupt_after_s"])
            entry["interrupted_at"] = time.time()
            handle.interrupt()
        await handle.wait_for_playout()
        entry["playout_done_at"] = time.time()
        entry["interrupted"] = handle.interrupted
        report["speech"].append(entry)
        await asyncio.sleep(plan.get("gap_s", 1.5))

    await ctx.room.local_participant.set_attributes({"e2e.report": json.dumps(report)})
    logger.info("e2e report published")


if __name__ == "__main__":
    cli.run_app(server)
