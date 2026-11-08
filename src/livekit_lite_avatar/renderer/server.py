"""Renderer service: keeps workers preloaded and hands each new session to one.

    liteavatar-renderer serve            # http://127.0.0.1:8791

POST /sessions  {"url": "wss://...", "token": "<LiveKit token for the avatar participant>", "presenter": "<id>"}
    -> 201 {"ok": true} once a preloaded worker has the job (loading the models takes seconds to a minute, so it is done
       before anyone asks); 503 when max_sessions are already running or no worker is ready in time;
       400 for a bad body or a presenter that isn't installed; 403 for a wrong secret.
GET  /health    -> {"ready": n, "loading": n, "active": n, "max": n, "presenters": [...],
                    "estimate": {"real_time_factor": 0.46, "suggested_fps": 30, ...}}

Binds to 127.0.0.1 by default. Set LITEAVATAR_SECRET to require a matching X-Renderer-Secret header.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import sys

from aiohttp import web

from .settings import Settings, valid_presenter_id

logger = logging.getLogger("liteavatar.server")
TAKE_TIMEOUT_S = 60.0


class Pool:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.warm: asyncio.Queue[asyncio.subprocess.Process] = asyncio.Queue()
        self.active: set[asyncio.subprocess.Process] = set()
        self.loading = 0
        self.procs: set[asyncio.subprocess.Process] = set()  # every worker started and not yet finished
        self.estimate: dict = {}  # the last worker's estimate of this machine's speed (see stream.estimate)
        self._tasks: set[asyncio.Task] = set()
        self._hurry = asyncio.Event()  # set when a session is waiting: skip the refill delay

    def _bg(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _spawn(self) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "livekit_lite_avatar.renderer",
                "worker",
                "--warm",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                env={**os.environ, **self.settings.to_env()},
            )
            self.procs.add(proc)
            while True:  # the worker prints READY on stdout once its models are loaded
                line = await proc.stdout.readline()
                if not line:
                    logger.error("worker exited while loading (code %s)", await proc.wait())
                    self.procs.discard(proc)
                    return
                if line.startswith(b"READY"):
                    with contextlib.suppress(ValueError):
                        self.estimate = json.loads(line[5:].strip() or b"{}")
                    break
            await self.warm.put(proc)
            logger.info("worker %d ready", proc.pid)
        finally:
            self.loading -= 1

    def top_up(self, delay: float = 0.0) -> None:
        s = self.settings
        while (
            self.warm.qsize() + self.loading < s.warm_workers
            and len(self.active) + self.warm.qsize() + self.loading < s.max_sessions
        ):
            self.loading += 1  # count it now so a second call doesn't spawn twice

            async def _later() -> None:
                if delay > 0:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._hurry.wait(), timeout=delay)
                await self._spawn()

            self._bg(_later())

    async def take(self, job: dict) -> asyncio.subprocess.Process:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TAKE_TIMEOUT_S
        while True:
            if self.warm.empty():
                self._hurry.set()
            proc = await asyncio.wait_for(self.warm.get(), timeout=max(0.1, deadline - loop.time()))
            self._hurry.clear()
            if proc.returncode is not None:  # died while waiting
                self.top_up()
                continue
            try:
                proc.stdin.write((json.dumps(job) + "\n").encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                self.top_up()
                continue
            break
        self.active.add(proc)

        async def _reap() -> None:
            code = await proc.wait()
            self.active.discard(proc)
            self.procs.discard(proc)
            logger.info("worker %d finished (code %s)", proc.pid, code)
            self.top_up()

        self._bg(_reap())
        # loading a worker takes several seconds of CPU: don't do it while the new session is getting started
        self.top_up(delay=self.settings.refill_delay_s)
        return proc

    async def close(self) -> None:
        """Stop every worker this server started: loading, warm and in a session (their avatars leave)."""
        for task in list(self._tasks):
            task.cancel()
        procs = [p for p in self.procs if p.returncode is None]
        for proc in procs:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        if procs:
            await asyncio.wait([asyncio.create_task(p.wait()) for p in procs], timeout=5)


POOL = web.AppKey("pool", Pool)


def create_app(settings: Settings, pool: Pool | None = None) -> web.Application:
    app = web.Application(client_max_size=64 * 1024)

    async def sessions(request: web.Request) -> web.Response:
        pool_: Pool = request.app[POOL]
        if settings.secret and not hmac.compare_digest(request.headers.get("X-Renderer-Secret", ""), settings.secret):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"error": "body must be JSON"}, status=400)
        if not isinstance(body, dict) or not isinstance(body.get("url"), str) or not isinstance(body.get("token"), str):
            return web.json_response({"error": "url and token required"}, status=400)
        presenter = body.get("presenter") or None
        if presenter is not None:
            if not isinstance(presenter, str) or not valid_presenter_id(presenter):
                return web.json_response({"error": "invalid presenter id"}, status=400)
            if presenter not in settings.installed_presenters():
                return web.json_response(
                    {"error": f"presenter {presenter} is not installed", "installed": settings.installed_presenters()},
                    status=400,
                )
        if len(pool_.active) >= settings.max_sessions:
            return web.json_response({"error": "busy"}, status=503)
        job = {"url": body["url"], "token": body["token"], "presenter": presenter}
        try:
            proc = await pool_.take(job)
        except asyncio.TimeoutError:
            return web.json_response({"error": "no renderer ready"}, status=503)
        return web.json_response({"ok": True, "pid": proc.pid}, status=201)

    async def health(request: web.Request) -> web.Response:
        pool_: Pool = request.app[POOL]
        return web.json_response(
            {
                "estimate": pool_.estimate,
                "ready": pool_.warm.qsize(),
                "loading": pool_.loading,
                "active": len(pool_.active),
                "max": settings.max_sessions,
                "presenters": settings.installed_presenters(),
            }
        )

    async def _startup(app: web.Application) -> None:
        app[POOL] = pool or Pool(settings)
        app[POOL].top_up()

    async def _cleanup(app: web.Application) -> None:
        await app[POOL].close()

    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    app.router.add_post("/sessions", sessions)
    app.router.add_get("/health", health)
    return app


def serve(settings: Settings, *, download: bool = False) -> None:
    from . import assets

    missing = assets.check(settings)
    if missing and download:
        logger.info("downloading what is missing first: %s", "; ".join(missing))
        assets.setup(settings, [settings.presenter])
        missing = assets.check(settings)
    if missing:
        raise SystemExit(
            "renderer is not set up; missing:\n  " + "\n  ".join(missing) + "\nRun: liteavatar-renderer setup"
        )
    if settings.host not in ("127.0.0.1", "localhost", "::1") and not settings.secret:
        logger.warning(
            "listening on %s without LITEAVATAR_SECRET: anyone who can reach it can start sessions", settings.host
        )
    logger.info(
        "serving on http://%s:%d (presenter %s, %d fps, max %d sessions)",
        settings.host,
        settings.port,
        settings.presenter,
        settings.fps,
        settings.max_sessions,
    )
    web.run_app(create_app(settings), host=settings.host, port=settings.port, print=None)
