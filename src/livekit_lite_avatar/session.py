"""Agent side: give a LiveKit AgentSession a self-hosted LiteAvatar face.

Works like LiveKit's hosted avatar plugins: the agent mints a LiveKit token for an avatar participant that
publishes on the agent's behalf, asks the renderer (`liteavatar-renderer serve`) to join the room with it, and
sends the agent's TTS audio to that participant over LiveKit data streams. The renderer draws lip-synced video on
CPU and publishes video and audio together, so lips and voice stay in sync.
"""

from __future__ import annotations

import asyncio
import logging
import os

import aiohttp
from livekit import api, rtc
from livekit.agents import AgentSession, get_job_context
from livekit.agents.voice.avatar import AvatarSession, DataStreamAudioOutput
from livekit.agents.voice.room_io import ATTRIBUTE_PUBLISH_ON_BEHALF

logger = logging.getLogger("livekit_lite_avatar")

SAMPLE_RATE = 24000  # the renderer works on 24 kHz mono
DEFAULT_RENDERER_URL = "http://127.0.0.1:8791"


class LiteAvatarError(RuntimeError):
    """The renderer could not take the session (not running, full, or refused the request)."""


class LiteAvatarSession(AvatarSession):
    """A LiteAvatar video avatar for one agent session.

    Args:
        renderer_url: the renderer service (default LITEAVATAR_RENDERER_URL or http://127.0.0.1:8791).
        presenter: presenter id installed on the renderer; None uses the renderer's default.
        renderer_secret: sent as X-Renderer-Secret (default LITEAVATAR_SECRET).
        avatar_participant_identity / avatar_participant_name: how the avatar appears in the room.
        request_timeout: seconds to wait for the renderer to accept the session.
    """

    def __init__(
        self,
        *,
        renderer_url: str | None = None,
        presenter: str | None = None,
        renderer_secret: str | None = None,
        avatar_participant_identity: str = "liteavatar",
        avatar_participant_name: str = "Avatar",
        request_timeout: float = 90.0,
    ) -> None:
        super().__init__()
        self._renderer_url = (renderer_url or os.getenv("LITEAVATAR_RENDERER_URL") or DEFAULT_RENDERER_URL).rstrip("/")
        self._presenter = presenter
        self._secret = renderer_secret if renderer_secret is not None else os.getenv("LITEAVATAR_SECRET", "")
        self._identity = avatar_participant_identity
        self._name = avatar_participant_name
        self._timeout = request_timeout

    @property
    def avatar_identity(self) -> str:
        return self._identity

    @property
    def provider(self) -> str:
        return "liteavatar"

    async def start(  # type: ignore[override]
        self,
        agent_session: AgentSession,
        room: rtc.Room,
        *,
        livekit_url: str | None = None,
        livekit_api_key: str | None = None,
        livekit_api_secret: str | None = None,
    ) -> None:
        """Ask the renderer to join `room` and route the agent's audio to it.

        Raises LiteAvatarError if the renderer is unreachable or refuses; the agent's audio is only re-routed once
        the renderer has accepted, so on failure the agent can carry on voice-only.
        """
        livekit_url = livekit_url or os.getenv("LIVEKIT_URL")
        livekit_api_key = livekit_api_key or os.getenv("LIVEKIT_API_KEY")
        livekit_api_secret = livekit_api_secret or os.getenv("LIVEKIT_API_SECRET")
        if not livekit_url or not livekit_api_key or not livekit_api_secret:
            raise LiteAvatarError("livekit_url, livekit_api_key and livekit_api_secret must be given as arguments "
                                  "or LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET")
        await super().start(agent_session, room)
        job_ctx = get_job_context()
        token = self.mint_token(
            room_name=room.name or job_ctx.job.room.name,
            publish_on_behalf=job_ctx.local_participant_identity,
            api_key=livekit_api_key,
            api_secret=livekit_api_secret,
        )
        await self.request_renderer(livekit_url, token)
        agent_session.output.replace_audio_tail(
            DataStreamAudioOutput(
                room=room,
                destination_identity=self._identity,
                wait_remote_track=rtc.TrackKind.KIND_VIDEO,  # buffer audio until the face is published
                sample_rate=SAMPLE_RATE,
            )
        )
        logger.info("liteavatar renderer accepted the session", extra={"renderer": self._renderer_url})

    def mint_token(self, *, room_name: str, publish_on_behalf: str, api_key: str, api_secret: str) -> str:
        """A LiveKit token for the avatar participant, allowed to publish on behalf of the agent."""
        return (
            api.AccessToken(api_key=api_key, api_secret=api_secret)
            .with_kind("agent")
            .with_identity(self._identity)
            .with_name(self._name)
            .with_grants(api.VideoGrants(room_join=True, room=room_name))
            .with_attributes({ATTRIBUTE_PUBLISH_ON_BEHALF: publish_on_behalf})
            .to_jwt()
        )

    async def request_renderer(self, livekit_url: str, token: str) -> None:
        """POST the session to the renderer; returns once a worker has it."""
        body: dict[str, str] = {"url": livekit_url, "token": token}
        if self._presenter:
            body["presenter"] = self._presenter
        headers = {"X-Renderer-Secret": self._secret} if self._secret else {}
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as http, \
                    http.post(f"{self._renderer_url}/sessions", json=body, headers=headers) as resp:
                if resp.status != 201:
                    raise LiteAvatarError(f"renderer refused the session: {resp.status} {await resp.text()}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise LiteAvatarError(f"renderer at {self._renderer_url} unreachable: {e!r}") from e
