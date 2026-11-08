"""End-to-end run on one machine: a private LiveKit server, the renderer, a test agent and a viewer.

Nothing leaves the machine and no API keys or paid services are involved: the LiveKit server is the open-source
`livekit-server` binary with throwaway keys, and the agent speaks WAV files instead of using a TTS.

    python tests/e2e/run.py                         # LiteAvatar's speech sample, prints a JSON report
    python tests/e2e/run.py --wav a.wav --wav b.wav --interrupt-after 1.5 --record demo.mp4

The viewer joins like any participant and checks what it receives: who publishes, simulcast or not, resolution,
frame rate, whether all the speech arrives, how long it takes to start, and how quickly an interruption stops it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from livekit import api, rtc

HERE = Path(__file__).resolve().parent
KEY = "e2e"
SECRET = "e2e-throwaway-secret-for-a-local-livekit-server"
AVATAR_IDENTITY = "liteavatar"
AGENT_NAME = "liteavatar-e2e"  # matches tests/e2e/agent.py
VIEW_RATE = 24000  # viewer audio sample rate


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(check, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):  # not up yet
            if check():
                return
        time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {what}")


@dataclass
class Stack:
    """The processes of one run; stop() ends them all."""

    logs: Path
    procs: list[subprocess.Popen] = field(default_factory=list)
    lk_url: str = ""
    renderer_url: str = ""

    def spawn(self, name: str, cmd: list[str], env: dict | None = None) -> subprocess.Popen:
        log = open(self.logs / f"{name}.log", "w")  # noqa: SIM115 - closed with the process
        p = subprocess.Popen(
            cmd, stdout=log, stderr=subprocess.STDOUT, env={**os.environ, **(env or {})}, start_new_session=True
        )
        self.procs.append(p)
        return p

    def start_livekit(self) -> None:
        exe = shutil.which("livekit-server")
        if exe is None:
            raise RuntimeError(
                "livekit-server not found (brew install livekit, or https://docs.livekit.io/home/self-hosting/local/)"
            )
        ws, tcp, udp = free_port(), free_port(), free_port()
        config = (
            f"port: {ws}\nrtc:\n  tcp_port: {tcp}\n  udp_port: {udp}\n  use_external_ip: false\n"
            f"keys:\n  {KEY}: {SECRET}\nlogging:\n  level: warn\n"
        )
        self.spawn("livekit-server", [exe, "--config-body", config, "--bind", "127.0.0.1", "--node-ip", "127.0.0.1"])
        self.lk_url = f"ws://127.0.0.1:{ws}"
        wait_until(
            lambda: urllib.request.urlopen(f"http://127.0.0.1:{ws}", timeout=1).status == 200, 30, "livekit-server"
        )

    def start_renderer(self, env: dict | None = None) -> None:
        port = free_port()
        self.renderer_url = f"http://127.0.0.1:{port}"
        self.spawn(
            "renderer",
            [sys.executable, "-m", "livekit_lite_avatar.renderer", "serve"],
            {"LITEAVATAR_PORT": str(port), **(env or {})},
        )

        def ready() -> bool:
            with urllib.request.urlopen(f"{self.renderer_url}/health", timeout=1) as r:
                return json.loads(r.read())["ready"] >= 1

        wait_until(ready, 180, "a warm renderer worker")

    def start_agent(self, script: Path = HERE / "agent.py", env: dict | None = None) -> None:
        """An agent worker that registers with the server and waits for dispatches, as in production."""
        env = {
            "LIVEKIT_URL": self.lk_url,
            "LIVEKIT_API_KEY": KEY,
            "LIVEKIT_API_SECRET": SECRET,
            "LITEAVATAR_RENDERER_URL": self.renderer_url,
            **(env or {}),
        }
        self.spawn("agent", [sys.executable, str(script), "start", "--log-level", "info"], env)
        log = self.logs / "agent.log"
        wait_until(lambda: "registered worker" in log.read_text(), 60, "the agent worker to register")

    def stop(self) -> None:
        """Agent first, then the renderer, then the server, each given time to shut down cleanly."""
        for p in reversed(self.procs):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                    p.wait(timeout=15)
                except ProcessLookupError:
                    pass
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)


class Recorder:
    """Writes the received video (as it arrives, at the publisher's frame rate) and audio to an MP4."""

    def __init__(self, path: Path, fps: int) -> None:
        self.path = path
        self.fps = fps
        self.proc: subprocess.Popen | None = None
        self.t0: float | None = None
        self.size: tuple[int, int] | None = None

    def video(self, frame: rtc.VideoFrame, now: float) -> None:
        if self.proc is None:
            self.size = (frame.width, frame.height)
            self.t0 = now
            self.raw = self.path.with_suffix(".video.mp4")
            self.proc = subprocess.Popen(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "-s",
                    f"{frame.width}x{frame.height}",
                    "-r",
                    str(self.fps),
                    "-i",
                    "-",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "20",
                    "-pix_fmt",
                    "yuv420p",
                    str(self.raw),
                ],
                stdin=subprocess.PIPE,
            )
        if (frame.width, frame.height) == self.size:
            self.proc.stdin.write(frame.convert(rtc.VideoBufferType.RGB24).data)

    def finish(self, audio: np.ndarray, audio_start: float | None) -> None:
        if self.proc is None:
            return
        self.proc.stdin.close()
        self.proc.wait()
        import soundfile as sf

        wav = self.path.with_suffix(".audio.wav")
        lead = max(0.0, (audio_start or self.t0) - self.t0)
        sf.write(wav, np.concatenate([np.zeros(int(lead * VIEW_RATE), np.int16), audio]), VIEW_RATE)
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(self.raw),
                "-i",
                str(wav),
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-shortest",
                str(self.path),
            ],
            check=True,
        )
        self.raw.unlink()
        wav.unlink()


async def view(stack: Stack, room_name: str, plan: dict, *, record: Path | None, timeout: float) -> dict:
    """Join as a viewer, collect what arrives from the avatar, and return measurements."""
    # like a frontend's token: joining the room dispatches the agent, with the plan as job metadata
    token = (
        api.AccessToken(KEY, SECRET)
        .with_identity("viewer")
        .with_name("Viewer")
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .with_room_config(
            api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME, metadata=json.dumps(plan))])
        )
        .to_jwt()
    )
    room = rtc.Room()
    got: dict = {"video_pubs": [], "audio_pubs": [], "avatar_attributes": None}
    vtimes: list[float] = []
    asamples: list[tuple[float, np.ndarray]] = []
    report_fut: asyncio.Future = asyncio.get_running_loop().create_future()
    recorder = Recorder(record, plan.get("fps", 25)) if record else None
    tasks: set[asyncio.Task] = set()

    async def read_video(track: rtc.RemoteVideoTrack) -> None:
        async for ev in rtc.VideoStream(track):
            now = time.time()
            vtimes.append(now)
            got["video_size"] = (ev.frame.width, ev.frame.height)
            if recorder:
                recorder.video(ev.frame, now)

    async def read_audio(track: rtc.RemoteAudioTrack) -> None:
        async for ev in rtc.AudioStream(track, sample_rate=VIEW_RATE, num_channels=1):
            asamples.append((time.time(), np.frombuffer(ev.frame.data, np.int16).copy()))

    @room.on("track_subscribed")
    def _sub(track: rtc.Track, pub: rtc.RemoteTrackPublication, p: rtc.RemoteParticipant) -> None:
        info = {
            "from": p.identity,
            "simulcasted": pub.simulcasted,
            "width": pub.width,
            "height": pub.height,
            "mime": pub.mime_type,
            "source": rtc.TrackSource.Name(pub.source),
        }
        if track.kind == rtc.TrackKind.KIND_VIDEO:
            got["video_pubs"].append(info)
            tasks.add(asyncio.create_task(read_video(track)))
        else:
            got["audio_pubs"].append(info)
            tasks.add(asyncio.create_task(read_audio(track)))
        if p.identity == AVATAR_IDENTITY:
            got["avatar_attributes"] = dict(p.attributes)

    @room.on("participant_attributes_changed")
    def _attrs(changed: dict, p: rtc.Participant) -> None:
        if "e2e.report" in changed and not report_fut.done():
            report_fut.set_result(json.loads(changed["e2e.report"]))

    await room.connect(stack.lk_url, token)
    try:
        agent_report = await asyncio.wait_for(report_fut, timeout=timeout)
        await asyncio.sleep(0.5)
    finally:
        await room.disconnect()
        for t in tasks:
            t.cancel()

    audio = np.concatenate([a for _, a in asamples]) if asamples else np.zeros(0, np.int16)
    audio_start = asamples[0][0] - len(asamples[0][1]) / VIEW_RATE if asamples else None
    if recorder:
        recorder.finish(audio, audio_start)
    return measure(got, vtimes, asamples, agent_report, plan)


def _speech_spans(asamples: list[tuple[float, np.ndarray]], threshold_db: float = -45.0) -> list[tuple[float, float]]:
    """(start, end) arrival times of audible audio, merging gaps under 0.6 s."""
    spans: list[list[float]] = []
    for t, a in asamples:
        if not len(a):
            continue
        db = 20 * np.log10(np.sqrt(np.mean((a.astype(np.float32) / 32768) ** 2)) + 1e-9)
        if db < threshold_db:
            continue
        start = t - len(a) / VIEW_RATE
        if spans and start - spans[-1][1] < 0.6:
            spans[-1][1] = t
        else:
            spans.append([start, t])
    return [(a, b) for a, b in spans]


def _wav_audible(path: str, threshold_db: float = -45.0) -> tuple[float, float, float]:
    """(duration, first audible second, last audible second) of a WAV, by the same 20 ms test as the viewer."""
    import soundfile as sf

    a, sr = sf.read(path, dtype="float32", always_2d=True)
    a = a[:, 0]
    n = sr // 50
    loud = [i for i in range(0, len(a), n) if 20 * np.log10(np.sqrt(np.mean(a[i : i + n] ** 2)) + 1e-9) >= threshold_db]
    if not loud:
        return len(a) / sr, 0.0, 0.0
    return len(a) / sr, loud[0] / sr, min(len(a), loud[-1] + n) / sr


def measure(got: dict, vtimes: list[float], asamples: list, agent: dict, plan: dict) -> dict:
    out: dict = {
        "avatar_attributes": got["avatar_attributes"],
        "agent_identity": agent.get("agent_identity"),
        "video_publications": got["video_pubs"],
        "audio_publications": got["audio_pubs"],
        "video_size": got.get("video_size"),
        "renderer_accepted_s": agent.get("renderer_accepted_s"),
        "avatar_joined_s": agent.get("avatar_joined_s"),
        "speech": [],
    }
    vt = np.asarray(vtimes)
    spans = _speech_spans(asamples)
    for i, (item, rep) in enumerate(zip(plan["speech"], agent["speech"], strict=True)):
        said, done = rep["said_at"], rep["playout_done_at"]
        mine = [s for s in spans if said <= s[0] <= done + 1.0]
        dur, lead, last = _wav_audible(item["wav"])
        entry = {
            "wav": rep["wav"],
            "wav_s": round(dur, 2),
            "wav_audible_s": round(last - lead, 2),
            "interrupted": rep["interrupted"],
            "agent_playout_s": round(done - said, 2),
        }
        if mine:
            start, end = mine[0][0], max(e for _, e in mine)
            # from say() until the first sound reaches the viewer, not counting the WAV's own leading silence
            entry["start_latency_s"] = round(start - said - lead, 2)
            entry["audible_s"] = round(end - start, 2)
            window = vt[(vt >= start) & (vt <= end)]
            if len(window) > 1:
                entry["video_fps_while_speaking"] = round((len(window) - 1) / (window[-1] - window[0]), 1)
                gaps = np.diff(window)
                entry["longest_video_gap_ms"] = round(float(gaps.max()) * 1000)
            if rep.get("interrupted_at"):
                entry["audio_stopped_after_interrupt_s"] = round(end - rep["interrupted_at"], 2)
        out["speech"].append(entry)
        if i == 0 and len(vt) > 1:
            idle = vt[vt < said]
            if len(idle) > 1:
                out["video_fps_idle"] = round((len(idle) - 1) / (idle[-1] - idle[0]), 1)
    return out


def run(
    wavs: list[str],
    *,
    presenter: str | None = None,
    interrupt_after: float | None = None,
    record: Path | None = None,
    renderer_env: dict | None = None,
    timeout: float = 300,
    keep_logs: Path | None = None,
) -> dict:
    logs = keep_logs or Path(tempfile.mkdtemp(prefix="liteavatar-e2e-"))
    logs.mkdir(parents=True, exist_ok=True)
    plan = {"presenter": presenter, "speech": [{"wav": str(Path(w).resolve())} for w in wavs]}
    if interrupt_after:
        plan["speech"][-1]["interrupt_after_s"] = interrupt_after
    stack = Stack(logs)
    try:
        stack.start_livekit()
        stack.start_renderer(renderer_env)
        stack.start_agent()
        result = asyncio.run(view(stack, f"e2e-{int(time.time())}", plan, record=record, timeout=timeout))
        result["logs"] = str(logs)
        return result
    finally:
        stack.stop()


def main() -> None:
    from livekit_lite_avatar.renderer.offline import default_wav
    from livekit_lite_avatar.renderer.settings import Settings

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--wav", action="append", help="speech to say, in order (default: LiteAvatar's sample)")
    ap.add_argument("--presenter", help="presenter for the session (default: the renderer's default)")
    ap.add_argument(
        "--interrupt-after",
        type=float,
        help="interrupt the last WAV this many seconds after the avatar starts speaking",
    )
    ap.add_argument("--record", type=Path, help="write what the viewer received to this MP4")
    ap.add_argument("--logs", type=Path, help="keep process logs in this folder")
    ns = ap.parse_args()
    wavs = ns.wav or [str(default_wav(Settings.from_env()))]
    result = run(
        wavs,
        presenter=ns.presenter,
        interrupt_after=ns.interrupt_after,
        record=ns.record.resolve() if ns.record else None,
        keep_logs=ns.logs,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
