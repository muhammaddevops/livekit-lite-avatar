"""Try the avatar in your browser, with no accounts or API keys.

Starts, on this machine only: a LiveKit server (the open-source `livekit-server`, with throwaway keys), the
renderer, and a demo agent that greets you and then repeats what you say, lip-synced. Then opens a web page.

    .venv/bin/python examples/local_demo/run.py [--presenter sample]

Needs `livekit-server` on PATH (macOS: brew install livekit; Linux: curl -sSL https://get.livekit.io | bash) and
the renderer set up (scripts/setup-renderer.sh). Ctrl-C stops everything.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import http.server
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from livekit import api

HERE = Path(__file__).resolve().parent
KEY = "demo"
SECRET = secrets.token_urlsafe(32)  # throwaway: this server only lives as long as the demo


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(check, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            if check():
                return
        time.sleep(0.25)
    raise SystemExit(f"timed out waiting for {what}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--presenter", help="installed presenter to use (default: the renderer's default)")
    ap.add_argument("--no-browser", action="store_true", help="print the URL instead of opening it")
    ns = ap.parse_args()

    exe = shutil.which("livekit-server")
    if exe is None:
        raise SystemExit("livekit-server not found: brew install livekit, or curl -sSL https://get.livekit.io | bash")
    logs = Path(tempfile.mkdtemp(prefix="liteavatar-demo-"))
    procs: list[subprocess.Popen] = []

    def spawn(name: str, cmd: list[str], env: dict | None = None) -> None:
        log = open(logs / f"{name}.log", "w")  # noqa: SIM115 - lives as long as the process
        procs.append(
            subprocess.Popen(
                cmd, stdout=log, stderr=subprocess.STDOUT, env={**os.environ, **(env or {})}, start_new_session=True
            )
        )

    ws, tcp, udp, renderer_port, web_port = (free_port() for _ in range(5))
    lk_url = f"ws://localhost:{ws}"
    renderer_url = f"http://127.0.0.1:{renderer_port}"
    try:
        print("Starting a local LiveKit server, the renderer and the demo agent ...", flush=True)
        config = (
            f"port: {ws}\nrtc:\n  tcp_port: {tcp}\n  udp_port: {udp}\n  use_external_ip: false\n"
            f"keys:\n  {KEY}: {SECRET}\nlogging:\n  level: warn\n"
        )
        spawn("livekit-server", [exe, "--config-body", config, "--bind", "127.0.0.1", "--node-ip", "127.0.0.1"])
        spawn(
            "renderer",
            [sys.executable, "-m", "livekit_lite_avatar.renderer", "serve"],
            {"LITEAVATAR_PORT": str(renderer_port)},
        )
        wait_until(lambda: urllib.request.urlopen(f"http://127.0.0.1:{ws}", timeout=1).status == 200, 30, "LiveKit")
        agent_env = {
            "LIVEKIT_URL": lk_url,
            "LIVEKIT_API_KEY": KEY,
            "LIVEKIT_API_SECRET": SECRET,
            "LITEAVATAR_RENDERER_URL": renderer_url,
            "DEMO_PRESENTER": ns.presenter or "",
        }
        spawn("agent", [sys.executable, str(HERE / "agent.py"), "start", "--log-level", "info"], agent_env)

        def renderer_ready() -> bool:
            with urllib.request.urlopen(f"{renderer_url}/health", timeout=1) as r:
                return json.loads(r.read())["ready"] >= 1

        wait_until(renderer_ready, 180, "the renderer to load its models")
        wait_until(lambda: "registered worker" in (logs / "agent.log").read_text(), 60, "the agent to register")

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

        handler = functools.partial(Quiet, directory=str(HERE))
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", web_port), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        token = (
            api.AccessToken(KEY, SECRET)
            .with_identity("you")
            .with_name("You")
            .with_grants(api.VideoGrants(room_join=True, room=f"liteavatar-demo-{secrets.token_hex(3)}"))
            .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name="liteavatar-demo")]))
            .to_jwt()
        )
        url = f"http://localhost:{web_port}/#url={lk_url}&token={token}"
        print(f"\nReady. Open:\n  {url}\n\nLogs: {logs}\nCtrl-C to stop.", flush=True)
        if not ns.no_browser:
            webbrowser.open(url)
        signal.signal(signal.SIGTERM, signal.default_int_handler)
        with contextlib.suppress(KeyboardInterrupt):
            while True:
                time.sleep(3600)
    finally:
        print("\nStopping ...", flush=True)
        for p in reversed(procs):  # agent, renderer, then the server
            if p.poll() is None:
                # SIGINT: the agent worker exits at once instead of waiting for the demo call to drain
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGINT)
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(p.pid, signal.SIGKILL)


if __name__ == "__main__":
    main()
