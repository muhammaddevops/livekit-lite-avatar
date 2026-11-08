"""Agent side, without LiveKit Cloud: the avatar token and the request to the renderer."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from livekit import api

from livekit_lite_avatar import LiteAvatarError, LiteAvatarSession

KEY = "devkey"
SECRET = "a-test-secret-that-is-at-least-32-characters"


def test_token_lets_the_avatar_publish_for_the_agent():
    s = LiteAvatarSession(avatar_participant_identity="face", avatar_participant_name="Face")
    token = s.mint_token(room_name="room-1", publish_on_behalf="agent-1", api_key=KEY, api_secret=SECRET)
    claims = api.TokenVerifier(KEY, SECRET).verify(token)
    assert claims.identity == "face"
    assert claims.name == "Face"
    assert claims.video.room == "room-1" and claims.video.room_join
    assert claims.attributes["lk.publish_on_behalf"] == "agent-1"
    assert claims.kind == "agent"


async def _with_renderer(handler, fn):
    app = web.Application()
    app.router.add_post("/sessions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        return await fn(f"http://127.0.0.1:{port}")
    finally:
        await runner.cleanup()


def test_request_sends_session_presenter_and_secret():
    seen = {}

    async def handler(request: web.Request) -> web.Response:
        seen["body"] = await request.json()
        seen["secret"] = request.headers.get("X-Renderer-Secret")
        return web.json_response({"ok": True}, status=201)

    async def go(url: str) -> None:
        s = LiteAvatarSession(renderer_url=url + "/", presenter="sample", renderer_secret="s3")
        await s.request_renderer("wss://example.livekit.cloud", "tok")

    asyncio.run(_with_renderer(handler, go))
    assert seen["body"] == {"url": "wss://example.livekit.cloud", "token": "tok", "presenter": "sample"}
    assert seen["secret"] == "s3"


def test_default_presenter_is_left_to_the_renderer():
    seen = {}

    async def handler(request: web.Request) -> web.Response:
        seen["body"] = await request.json()
        seen["secret"] = request.headers.get("X-Renderer-Secret")
        return web.json_response({"ok": True}, status=201)

    async def go(url: str) -> None:
        await LiteAvatarSession(renderer_url=url, renderer_secret="").request_renderer("wss://x", "tok")

    asyncio.run(_with_renderer(handler, go))
    assert "presenter" not in seen["body"]
    assert seen["secret"] is None


def test_busy_renderer_raises():
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"error": "busy"}, status=503)

    async def go(url: str) -> None:
        await LiteAvatarSession(renderer_url=url).request_renderer("wss://x", "tok")

    with pytest.raises(LiteAvatarError, match="503"):
        asyncio.run(_with_renderer(handler, go))


def test_unreachable_renderer_raises():
    async def go() -> None:
        await LiteAvatarSession(renderer_url="http://127.0.0.1:9", request_timeout=2).request_renderer("wss://x", "t")

    with pytest.raises(LiteAvatarError, match="unreachable"):
        asyncio.run(go())
