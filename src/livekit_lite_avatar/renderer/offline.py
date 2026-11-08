"""Offline check of the streaming renderer: feeds a WAV in 100 ms pieces (as TTS audio arrives), renders every
frame, writes an MP4 with the audio, and reports the real-time factor (render time / audio length).

    liteavatar-renderer render speech.wav out.mp4

A real-time factor below 1 means this machine draws faster than speech plays; live calls need headroom for
encoding, the agent and anything else on the machine, so aim for about 0.5.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .settings import Settings

if TYPE_CHECKING:
    from .stream import LiteAvatarStream

CHUNK_S = 0.1


@dataclass
class RenderResult:
    output: Path
    frames: int
    audio_s: float
    render_s: float
    load_s: float
    first_frames_after_s: float | None
    width: int
    height: int
    fps: int

    @property
    def real_time_factor(self) -> float:
        return self.render_s / self.audio_s if self.audio_s else float("inf")

    def summary(self) -> str:
        first = f"{self.first_frames_after_s:.2f}s" if self.first_frames_after_s is not None else "n/a"
        return (
            f"{self.frames} frames ({self.width}x{self.height} @ {self.fps} fps) for {self.audio_s:.2f}s of audio "
            f"in {self.render_s:.2f}s: real-time factor {self.real_time_factor:.2f}; first frames once {first} "
            f"of audio had arrived; models loaded in {self.load_s:.1f}s"
        )


def render_file(
    wav: str | Path,
    out: str | Path,
    settings: Settings,
    presenter: str | None = None,
    stream: LiteAvatarStream | None = None,
) -> RenderResult:
    """Render `wav` to `out`. Pass an already loaded `stream` to skip loading the models."""
    import cv2
    import librosa

    from .stream import SR, from_settings

    wav, out = Path(wav).resolve(), Path(out).resolve()
    t0 = time.perf_counter()
    s = stream or from_settings(settings, presenter)
    s.reset()
    load_s = time.perf_counter() - t0
    audio, _ = librosa.load(str(wav), sr=SR, mono=True)
    step = int(CHUNK_S * SR)
    frames = []
    first_at = None
    t0 = time.perf_counter()
    for i in range(0, len(audio), step):
        s.feed(audio[i : i + step])
        while s.ready(False):
            frames += s.step(False)
            if first_at is None and frames:
                first_at = min(len(audio), i + step) / SR
    while s.ready(True):
        frames += s.step(True)
    render_s = time.perf_counter() - t0

    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    silent = out if ffmpeg is None else out.with_suffix(".noaudio.mp4")
    vw = cv2.VideoWriter(str(silent), cv2.VideoWriter_fourcc(*"mp4v"), s.out_fps, (s.width, s.height))
    for k, (f, energy) in enumerate(frames):
        vw.write(s.move(f, k, energy))
    vw.release()
    if ffmpeg is not None:
        subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-y",
                "-i",
                str(silent),
                "-i",
                str(wav),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(out),
            ],
            check=True,
        )
        silent.unlink()
    return RenderResult(
        output=out,
        frames=len(frames),
        audio_s=len(audio) / SR,
        render_s=render_s,
        load_s=load_s,
        first_frames_after_s=first_at,
        width=s.width,
        height=s.height,
        fps=s.out_fps,
    )


def default_wav(settings: Settings) -> Path:
    """A speech sample that comes with the LiteAvatar checkout (Mandarin, 5.6 s), for when no WAV is given."""
    from .assets import PARAFORMER

    return settings.lite_avatar_dir / PARAFORMER / "example" / "asr_example.wav"
