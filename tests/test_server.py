"""Renderer server and settings, without loading any models (a fake pool stands in for the workers)."""

from __future__ import annotations

import argparse
import asyncio
import sys

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
        self.pending = 0
        self.ready: set = set()
        self.healthy = True
        self.jobs: list[dict] = []
        self.estimate = {"real_time_factor": 0.46, "suggested_fps": 30}

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
            (await client.post("/sessions", json=ok)).status,  # no secret
            (await client.post("/sessions", json=ok, headers={"X-Renderer-Secret": "no"})).status,  # wrong
            (await client.post("/sessions", data=b"not json", headers=h)).status,
            (await client.post("/sessions", json={"url": "wss://x"}, headers=h)).status,  # no token
            (await client.post("/sessions", json={**ok, "presenter": "../etc"}, headers=h)).status,
            (await client.post("/sessions", json={**ok, "presenter": "P2"}, headers=h)).status,  # missing
            (await client.post("/sessions", json={**ok, "presenter": "P1"}, headers=h)).status,
            (await client.post("/sessions", json=ok, headers=h)).status,  # full
        ]
        health = await (await client.get("/health")).json()
        return r, health

    statuses, health = _run(s, pool, fn)
    assert statuses == [403, 403, 400, 400, 400, 400, 201, 503]
    assert pool.jobs == [{"url": "wss://x", "token": "t", "presenter": "P1"}]
    assert health["active"] == 1 and health["max"] == 1 and health["presenters"] == ["P1"]
    assert health["estimate"]["real_time_factor"] == 0.46


def test_settings_env_flags_and_worker_env(tmp_path):
    env = {
        "LITEAVATAR_FPS": "30",
        "LITEAVATAR_THREADS": "4",
        "LITEAVATAR_HOME": str(tmp_path),
        "LITEAVATAR_SECRET": "s3",
        "LITEAVATAR_OPEN_GAIN": "1.8",
    }
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


# ---------- the real pool, with tiny fake workers that speak the worker's line protocol

FAKE_WORKER = """
import json, sys, time
mode = sys.argv[1]
if mode == "dies":
    sys.exit(3)
time.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 0)
print("READY " + json.dumps({"real_time_factor": 0.5}), flush=True)
job = json.loads(sys.stdin.readline())
time.sleep(float(sys.argv[3]) if len(sys.argv) > 3 else 0)  # time to join
print("JOINED" if mode == "joins" else "ERROR could not join the room: " + job["url"], flush=True)
time.sleep(30)
"""


def _pool_app(tmp_path, mode: str, *args: str, **settings):
    from livekit_lite_avatar.renderer.server import Pool

    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER)
    s = _settings(tmp_path, refill_delay_s=0.0, **settings)
    pool = Pool(s, worker_cmd=[sys.executable, str(script), mode, *args])
    return s, pool


async def _wait(cond, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        assert loop.time() < end, "timed out"
        await asyncio.sleep(0.05)


def test_pool_answers_once_the_worker_has_joined(tmp_path):
    s, pool = _pool_app(tmp_path, "joins")

    async def fn(client):
        await _wait(lambda: pool.warm.qsize() == 1)
        r = await client.post("/sessions", json={"url": "wss://x", "token": "t"})
        health = await (await client.get("/health")).json()
        return r.status, health

    status, health = _run(s, pool, fn)
    assert status == 201
    assert health["healthy"] and health["active"] == 1 and health["estimate"] == {"real_time_factor": 0.5}


def test_pool_reports_a_worker_that_cannot_join(tmp_path):
    s, pool = _pool_app(tmp_path, "fails")

    async def fn(client):
        await _wait(lambda: pool.warm.qsize() == 1)
        r = await client.post("/sessions", json={"url": "wss://nowhere", "token": "t"})
        body = await r.json()
        await _wait(lambda: not pool.active)  # the worker that failed is stopped
        return r.status, body

    status, body = _run(s, pool, fn)
    assert status == 502 and "could not join the room: wss://nowhere" in body["error"]


def test_pool_recovers_and_reports_workers_that_die_while_loading(tmp_path):
    s, pool = _pool_app(tmp_path, "dies")

    async def fn(client):
        await _wait(lambda: pool.failures >= 3)  # retried with back-off, not given up
        r = await client.get("/health")
        return r.status, await r.json()

    pool.retry_base_s = 0.05
    status, health = _run(s, pool, fn)
    assert status == 503 and health["healthy"] is False


def test_pool_turns_away_requests_beyond_max_sessions_at_once(tmp_path):
    s, pool = _pool_app(tmp_path, "joins", "1.0", max_sessions=1)  # a slow-loading worker

    async def fn(client):
        a, b = await asyncio.gather(
            client.post("/sessions", json={"url": "wss://x", "token": "t"}),
            client.post("/sessions", json={"url": "wss://x", "token": "t"}),
        )
        return sorted([a.status, b.status])

    assert _run(s, pool, fn) == [201, 503]


def test_pool_counts_a_joining_session_once(tmp_path):
    """Two agents at once, within max_sessions: both get a session, though the first is still joining."""
    s, pool = _pool_app(tmp_path, "joins", "0", "1.0", max_sessions=2)  # loads at once, takes 1 s to join

    async def fn(client):
        await _wait(lambda: len(pool.ready) == 1)
        first = asyncio.ensure_future(client.post("/sessions", json={"url": "wss://x", "token": "t"}))
        await asyncio.sleep(0.3)  # the first is joining now
        second = await client.post("/sessions", json={"url": "wss://x", "token": "t"})
        return sorted([(await first).status, second.status])

    assert _run(s, pool, fn) == [201, 201]
