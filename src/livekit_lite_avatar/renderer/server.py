"""Renderer service: keeps workers preloaded and hands each new session to one.

    liteavatar-renderer serve            # http://127.0.0.1:8791

POST /sessions  {"url": "wss://...", "token": "<LiveKit token for the avatar participant>", "presenter": "<id>"}
    -> 201 {"ok": true} once a preloaded worker has joined the room (loading the models takes seconds to a minute,
       so it is done before anyone asks); 503 when max_sessions are already running or no worker is ready in time;
       502 when the worker could not join the room (bad URL or token, unreachable server); 400 for a bad body or
       a presenter that isn't installed; 403 for a wrong secret.
GET  /health    -> {"healthy": true, "ready": n, "loading": n, "active": n, "max": n, "presenters": [...],
                    "estimate": {"real_time_factor": 0.46, "suggested_fps": 30, ...}}
                   503 with "healthy": false when no worker can be loaded (the last attempts failed).

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
TAKE_TIMEOUT_S = 45.0  # wait for a preloaded worker
JOIN_TIMEOUT_S = 40.0  # wait for it to join (a presenter switch and a 20 s connect timeout); the agent waits 90 s
WORKER = [sys.executable, "-m", "livekit_lite_avatar.renderer", "worker", "--warm"]


class JoinError(RuntimeError):
    """The worker took the job but could not join the room."""


class Pool:
    def __init__(self, settings: Settings, worker_cmd: list[str] | None = None) -> None:
        self.settings = settings
        self.worker_cmd = worker_cmd or WORKER
        self.warm: asyncio.Queue[asyncio.subprocess.Process] = asyncio.Queue()
        self.active: set[asyncio.subprocess.Process] = set()
        self.loading = 0
        self.pending = 0  # sessions waiting for a worker; they count against max_sessions
        self.failures = 0  # workers in a row that died while loading
        self.retry_base_s = 5.0  # back-off after a failed load: 5 s, 10 s, 20 s ... up to a minute
        self.procs: set[asyncio.subprocess.Process] = set()  # every worker started and not yet finished
        self.ready: set[asyncio.subprocess.Process] = set()  # loaded, alive and not handed a session yet
        self.estimate: dict = {}  # the last worker's estimate of this machine's speed (see stream.estimate)
        self._tasks: set[asyncio.Task] = set()
        self._hurry = asyncio.Event()  # set when a session is waiting: skip the refill delay
        self._closed = False

    @property
    def healthy(self) -> bool:
        return self.failures < 3 or bool(self.ready)

    def _bg(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _spawn(self) -> None:
        ok = False
        try:
            env = {**os.environ, **self.settings.to_env()}
            env.pop("LITEAVATAR_SECRET", None)  # workers don't need it
            proc = await asyncio.create_subprocess_exec(
                *self.worker_cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env=env
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
            self.ready.add(proc)
            self._bg(self._watch_ready(proc))
            ok = True
            self.failures = 0
            logger.info("worker %d ready", proc.pid)
        finally:
            self.loading -= 1
            if not ok and not self._closed:  # try again, backing off
                self.failures += 1
                self.top_up(delay=min(60.0, self.retry_base_s * 2 ** (self.failures - 1)), retry=True)

    async def _watch_ready(self, proc: asyncio.subprocess.Process) -> None:
        """A preloaded worker that dies before getting a session is no longer ready; load another."""
        await proc.wait()
        if proc in self.ready:
            self.ready.discard(proc)
            self.procs.discard(proc)
            logger.warning("preloaded worker %d exited (code %s)", proc.pid, proc.returncode)
            self.top_up()

    def top_up(self, delay: float = 0.0, retry: bool = False) -> None:
        if self._closed:
            return
        s = self.settings
        while (
            len(self.ready) + self.loading < s.warm_workers
            and len(self.active) + len(self.ready) + self.loading < s.max_sessions
        ):
            self.loading += 1  # count it now so a second call doesn't spawn twice

            async def _later() -> None:
                if delay > 0:
                    if retry:  # a failed load: wait the full back-off
                        await asyncio.sleep(delay)
                    else:
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(self._hurry.wait(), timeout=delay)
                await self._spawn()

            self._bg(_later())

    async def _join(self, proc: asyncio.subprocess.Process) -> None:
        """Wait for the worker to report JOINED (or ERROR) after it got the job."""
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=JOIN_TIMEOUT_S)
            if not line:
                raise JoinError(f"worker exited (code {await proc.wait()})")
            if line.startswith(b"JOINED"):
                return
            if line.startswith(b"ERROR"):
                raise JoinError(line[5:].decode(errors="replace").strip())

    async def take(self, job: dict) -> asyncio.subprocess.Process:
        """Hand the job to a preloaded worker and return once it is in the room. Raises asyncio.TimeoutError if no
        worker is ready in time, JoinError if it couldn't join. If the caller gives up (cancelled), any worker that
        already has the job is stopped, so no avatar is left in the room."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TAKE_TIMEOUT_S
        self.pending += 1
        reserved = True  # this request holds a place against max_sessions until its worker is active
        proc = None
        try:
            self.top_up()
            while True:
                if self.warm.empty():
                    self._hurry.set()
                proc = await asyncio.wait_for(self.warm.get(), timeout=max(0.1, deadline - loop.time()))
                self.ready.discard(proc)
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
            self.pending -= 1  # counted as active from here on
            reserved = False
            self._bg(self._reap(proc))
            # loading a worker takes several seconds of CPU: don't do it while the new session is getting started
            self.top_up(delay=self.settings.refill_delay_s)
            try:
                await self._join(proc)
            except (JoinError, asyncio.TimeoutError, asyncio.CancelledError) as e:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                if isinstance(e, asyncio.TimeoutError):
                    raise JoinError(f"worker did not join the room within {JOIN_TIMEOUT_S:.0f} s") from e
                raise
            return proc
        finally:
            if reserved:
                self.pending -= 1
            if not self.pending:  # nobody else is waiting for a worker
                self._hurry.clear()

    async def _reap(self, proc: asyncio.subprocess.Process) -> None:
        code = await proc.wait()
        self.active.discard(proc)
        self.procs.discard(proc)
        logger.info("worker %d finished (code %s)", proc.pid, code)
        self.top_up()

    async def close(self) -> None:
        """Stop every worker this server started: loading, warm and in a session (their avatars leave)."""
        self._closed = True
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
        if len(pool_.active) + pool_.pending >= settings.max_sessions:
            return web.json_response({"error": "busy"}, status=503)
        job = {"url": body["url"], "token": body["token"], "presenter": presenter}
        try:
            proc = await pool_.take(job)
        except asyncio.TimeoutError:
            return web.json_response({"error": "no renderer ready"}, status=503)
        except JoinError as e:
            logger.warning("session failed: %s", e)
            return web.json_response({"error": f"renderer could not join the room: {e}"}, status=502)
        return web.json_response({"ok": True, "pid": proc.pid}, status=201)

    async def health(request: web.Request) -> web.Response:
        pool_: Pool = request.app[POOL]
        return web.json_response(
            {
                "healthy": pool_.healthy,
                "estimate": pool_.estimate,
                "ready": len(pool_.ready),
                "loading": pool_.loading,
                "active": len(pool_.active),
                "max": settings.max_sessions,
                "presenters": settings.installed_presenters(),
            },
            status=200 if pool_.healthy else 503,
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
    # handler_cancellation: if an agent gives up on its request, stop handling it (and the worker it got)
    web.run_app(create_app(settings), host=settings.host, port=settings.port, print=None, handler_cancellation=True)
