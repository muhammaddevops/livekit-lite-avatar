"""Benchmark the renderer on this machine and print a Markdown table.

1. Offline: render ~20 s of speech and report the real-time factor (render time / audio length).
2. Live (needs `livekit-server` on PATH): one call on a private local LiveKit server, measured from a viewer:
   time from say() to the first sound, frame rate while speaking, and how many of those frames are new pictures
   (fewer when the renderer can't keep up and repeats frames to keep the voice smooth).

    .venv/bin/python scripts/benchmark.py [--wav speech.wav] [--no-live] [--json results.json]

Paste the table into an issue to add your CPU to the README.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

from livekit_lite_avatar.renderer.offline import default_wav, render_file  # noqa: E402
from livekit_lite_avatar.renderer.settings import Settings  # noqa: E402


def cpu_name() -> str:
    try:
        if sys.platform == "darwin":
            return subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    return platform.processor() or platform.machine()


def long_clip(wav: str, seconds: float, folder: Path) -> Path:
    """The WAV repeated to at least `seconds`, for a steadier number than one short clip gives."""
    a, sr = sf.read(wav, dtype="float32", always_2d=True)
    a = a[:, 0]
    reps = max(1, int(np.ceil(seconds * sr / len(a))))
    out = folder / "benchmark.wav"
    sf.write(out, np.tile(a, reps), sr)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--wav", help="speech to use (default: LiteAvatar's sample, repeated to ~20 s)")
    ap.add_argument("--no-live", action="store_true", help="skip the live call on a local LiveKit server")
    ap.add_argument("--json", type=Path, help="also write the results here")
    ns = ap.parse_args()

    settings = Settings.from_env()
    tmp = Path(tempfile.mkdtemp(prefix="liteavatar-bench-"))
    clip = long_clip(ns.wav or str(default_wav(settings)), 20.0, tmp)
    machine = {
        "cpu": cpu_name(),
        "cores": os.cpu_count(),
        "os": f"{platform.system()} {platform.machine()}",
        "runner": os.getenv("ImageOS") or "",  # noqa: SIM112 - GitHub Actions' runner image name
    }
    print(f"Benchmarking on {machine['cpu']} ({machine['cores']} cores, {machine['os']})", file=sys.stderr)

    off = render_file(clip, tmp / "benchmark.mp4", settings)
    results = {
        "machine": machine,
        "settings": {"fps": settings.fps, "width": settings.width, "threads": settings.threads},
        "offline": {
            "audio_s": round(off.audio_s, 1),
            "render_s": round(off.render_s, 2),
            "real_time_factor": round(off.real_time_factor, 2),
            "model_load_s": round(off.load_s, 1),
        },
    }
    print(off.summary(), file=sys.stderr)

    if not ns.no_live and shutil.which("livekit-server"):
        import run as e2e

        live = e2e.run([str(clip)], keep_logs=tmp / "logs")
        sp = live["speech"][0]
        keys = ("start_latency_s", "video_fps_while_speaking", "lip_fps_while_speaking", "longest_video_gap_ms")
        results["live"] = {k: sp.get(k) for k in (*keys, "audible_s", "wav_audible_s")}
        results["live"]["avatar_joined_s"] = live.get("avatar_joined_s")
    elif not ns.no_live:
        print("livekit-server not found: skipping the live call", file=sys.stderr)

    o, lv = results["offline"], results.get("live", {})
    name = machine["cpu"] + (f" ({machine['runner']})" if machine["runner"] else "")
    print("| CPU | Cores | Real-time factor | First sound after say() | Frame rate | New lip pictures per second |")
    print("|---|---|---|---|---|---|")
    print(
        f"| {name} | {machine['cores']} | {o['real_time_factor']} | {lv.get('start_latency_s', 'n/a')} s "
        f"| {lv.get('video_fps_while_speaking', 'n/a')} | {lv.get('lip_fps_while_speaking', 'n/a')} |"
    )
    if ns.json:
        ns.json.write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
