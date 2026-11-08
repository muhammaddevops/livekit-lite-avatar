"""Minimal LiveKit voice agent with a self-hosted LiteAvatar face.

1. Start the renderer (once set up):   liteavatar-renderer serve
2. Run this agent:                     python examples/agent.py dev
3. Join the room from a LiveKit client (for example the Agents Playground) with adaptiveStream turned off.

Needs LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET. Speech-to-text, the LLM and text-to-speech go through
LiveKit Inference here; any STT/LLM/TTS works, since the avatar only needs the agent's audio.
"""

from __future__ import annotations

import logging

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli, inference

from livekit_lite_avatar import LiteAvatarError, LiteAvatarSession

logger = logging.getLogger("liteavatar-example")


def own_jobs_load(server: AgentServer) -> float:
    """This worker's load as its share of calls, not the machine's CPU. The renderer keeps the CPU busy when it runs on
    the same machine, and LiveKit's default (CPU above 70%) would then stop this worker from taking calls."""
    return min(1.0, len(server.active_jobs) / 4)


server = AgentServer(load_fnc=own_jobs_load)


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    session = AgentSession(
        stt=inference.STT(model="deepgram/nova-3", language="en"),
        llm=inference.LLM(model="openai/gpt-4.1-mini"),
        tts=inference.TTS(model="cartesia/sonic-3", voice="79a125e8-cd45-4c13-8a67-188112f4dd22"),
        vad=inference.VAD(),
    )
    await ctx.connect()

    avatar = LiteAvatarSession(
        renderer_url="http://127.0.0.1:8791",  # or LITEAVATAR_RENDERER_URL
        presenter=None,  # None = the renderer's default presenter
        avatar_participant_name="Assistant",
    )
    try:
        await avatar.start(session, room=ctx.room)
    except LiteAvatarError as e:  # renderer not running or full: carry on with voice only
        logger.warning("no avatar for this call: %s", e)

    await session.start(
        agent=Agent(instructions="You are a friendly assistant. Keep replies short and conversational."),
        room=ctx.room,
    )
    await session.generate_reply(instructions="Greet the user briefly.")


if __name__ == "__main__":
    cli.run_app(server)
