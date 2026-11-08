"""Renderer server and settings, without loading any models (a fake pool stands in for the workers)."""

from __future__ import annotations

import argparse
import asyncio

from aiohttp.test_utils import TestClient, TestServer

from livekit_lite_avatar.renderer.server import create_app
from livekit_lite_avatar.renderer.settings import Settings, add_arguments


class FakeProc:
    pid = 4242


class FakePool:
    def __init__(self) -> None:
        self.warm = asyncio.Queue()
        self.active: set = set()
        self.loading = 0
        self.jobs: list[dict] = []

    def top_up(self, delay: float = 0.0) -> None:
        pass

    async def take(self, job: dict):
        self.jobs.append(job)
        self.active.add(object())
        return FakeProc()

    async def close(self) -> None:
        pass


def _settings(tmp_path, **kw) -> Settings:
    (tmp_path / "presenters" / "P1").mkdir(parents=True)
    (tmp_path / "presenters" / "P1" / "bg_video.mp4").write_bytes(b"")
    return Settings(home=tmp_path, presenter="P1", **kw)


def _run(settings: Settings, pool: FakePool, fn):
    async def go():
        async with TestClient(TestServer(create_app(settings, pool))) as client:
            return await fn(client)

    return asyncio.run(go())


def test_sessions(tmp_path):
    pool = FakePool()
    s = _settings(tmp_path, secret="s3", max_sessions=1)
    ok = {"url": "wss://x", "token": "t"}

    async def fn(client):
        h = {"X-Renderer-Secret": "s3"}
        r = [
            (await client.post("/sessions", json=ok)).status,                                         # no secret
            (await client.post("/sessions", json=ok, headers={"X-Renderer-Secret": "no"})).status,     # wrong
            (await client.post("/sessions", data=b"not json", headers=h)).status,
            (await client.post("/sessions", json={"url": "wss://x"}, headers=h)).status,                # no token
            (await client.post("/sessions", json={**ok, "presenter": "../etc"}, headers=h)).status,
            (await client.post("/sessions", json={**ok, "presenter": "P2"}, headers=h)).status,         # missing
            (await client.post("/sessions", json={**ok, "presenter": "P1"}, headers=h)).status,
            (await client.post("/sessions", json=ok, headers=h)).status,                                # full
        ]
        health = await (await client.get("/health")).json()
        return r, health

    statuses, health = _run(s, pool, fn)
    assert statuses == [403, 403, 400, 400, 400, 400, 201, 503]
    assert pool.jobs == [{"url": "wss://x", "token": "t", "presenter": "P1"}]
    assert health["active"] == 1 and health["max"] == 1 and health["presenters"] == ["P1"]


def test_settings_env_flags_and_worker_env(tmp_path):
    env = {"LITEAVATAR_FPS": "30", "LITEAVATAR_THREADS": "4", "LITEAVATAR_HOME": str(tmp_path),
           "LITEAVATAR_SECRET": "s3", "LITEAVATAR_OPEN_GAIN": "1.8"}
    s = Settings.from_env(env)
    assert (s.fps, s.threads, s.home, s.open_gain, s.secret) == (30, 4, tmp_path, 1.8, "s3")
    assert (s.lip_delay_frames, s.bitrate, s.width) == (1, 2_500_000, 576)  # learned defaults

    ap = argparse.ArgumentParser()
    add_arguments(ap)
    s2 = s.with_args(ap.parse_args(["--fps", "25", "--bitrate", "3000000"]))
    assert (s2.fps, s2.bitrate, s2.threads) == (25, 3_000_000, 4)

    worker_env = s2.to_env()
    assert worker_env["LITEAVATAR_FPS"] == "25" and "LITEAVATAR_SECRET" not in worker_env
    assert Settings.from_env(worker_env).fps == 25
