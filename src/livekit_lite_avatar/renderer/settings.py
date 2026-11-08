"""Renderer settings: environment variables (LITEAVATAR_*), overridden by command-line flags.

The server passes its resolved settings to each worker process through the environment, so a flag given to
`liteavatar-renderer serve` reaches every worker it starts.
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, fields, replace
from pathlib import Path

DEFAULT_PRESENTER = "P1yerb8kIA7eBpaIydU2lwzA"  # LiteAvatarGallery 20250408
DEFAULT_GALLERY = "20250408"
_PRESENTER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _default_home() -> Path:
    base = os.getenv("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "livekit-lite-avatar"


@dataclass(frozen=True)
class Settings:
    home: Path = _default_home()  # noqa: RUF009  (resolved once, at import)  LiteAvatar code, weights, presenters
    presenter: str = DEFAULT_PRESENTER  # default presenter (a session may ask for another installed one)
    fps: int = 25  # video frame rate (LiteAvatar's own rate is 30; 25 saves ~10% CPU)
    width: int = 576  # output width in pixels; height follows the presenter's aspect ratio
    threads: int = 6  # torch threads per worker; leave cores for encoding and the agent
    lip_delay_frames: int = 1  # video starts this many frames after its audio (LiteAvatar's lips run early)
    bitrate: int = 2_500_000  # single video layer, bits per second
    max_sessions: int = 2  # concurrent sessions (one worker process each)
    warm_workers: int = 1  # preloaded workers kept ready for the next session
    host: str = "127.0.0.1"
    port: int = 8791
    secret: str = ""  # if set, POST /sessions needs a matching X-Renderer-Secret header
    refill_delay_s: float = 60.0  # wait before preloading the next worker, so loading doesn't slow a new call
    max_session_s: float = 1800.0  # a worker leaves its room after this long
    # look (all off by default; see the README's "Tuning")
    mouth_gain: float = 1.0  # scales every mouth parameter; artefacts from ~1.3
    open_gain: float = 1.0  # scales only the jaw-opening direction; 1.8 opens the mouth wider cleanly
    smooth_hz: float = 10.0  # low-pass on the mouth parameters (LiteAvatar's own value)
    head_motion: float = 0.0  # gentle sway and nods by moving the whole frame; softens the picture

    # ---------- paths
    @property
    def lite_avatar_dir(self) -> Path:
        return self.home / "lite-avatar"

    @property
    def presenters_dir(self) -> Path:
        return self.home / "presenters"

    def presenter_dir(self, presenter: str | None = None) -> Path:
        pid = presenter or self.presenter
        if not valid_presenter_id(pid):
            raise ValueError(f"invalid presenter id: {pid!r}")
        return self.presenters_dir / pid

    def installed_presenters(self) -> list[str]:
        if not self.presenters_dir.is_dir():
            return []
        return sorted(p.name for p in self.presenters_dir.iterdir() if (p / "bg_video.mp4").is_file())

    # ---------- loading
    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        values = {}
        for f in fields(cls):
            raw = env.get(f"LITEAVATAR_{f.name.upper()}")
            if raw is not None and raw != "":
                values[f.name] = _cast(f.name, raw)
        return cls(**values)

    def with_args(self, ns: argparse.Namespace) -> Settings:
        changes = {f.name: getattr(ns, f.name) for f in fields(self) if getattr(ns, f.name, None) is not None}
        return replace(self, **changes)

    def to_env(self) -> dict[str, str]:
        """The settings a worker needs, as LITEAVATAR_* variables (the secret stays with the server)."""
        return {f"LITEAVATAR_{f.name.upper()}": str(getattr(self, f.name)) for f in fields(self) if f.name != "secret"}


def valid_presenter_id(pid: str) -> bool:
    return bool(_PRESENTER_ID.match(pid or ""))


def _cast(name: str, raw: str):
    default = getattr(Settings, name)
    if isinstance(default, Path):
        return Path(raw).expanduser()
    if isinstance(default, bool):
        return raw.lower() in ("1", "true", "yes")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


_FLAGS = {
    "home": (Path, "data folder (LITEAVATAR_HOME, default ~/.cache/livekit-lite-avatar)"),
    "presenter": (str, "default presenter id (LITEAVATAR_PRESENTER)"),
    "fps": (int, "video frame rate (LITEAVATAR_FPS, 25)"),
    "width": (int, "video width in pixels (LITEAVATAR_WIDTH, 576)"),
    "threads": (int, "torch threads per worker (LITEAVATAR_THREADS, 6)"),
    "lip_delay_frames": (int, "frames the video starts after its audio (LITEAVATAR_LIP_DELAY_FRAMES, 1)"),
    "bitrate": (int, "video bitrate in bit/s (LITEAVATAR_BITRATE, 2500000)"),
    "max_sessions": (int, "concurrent sessions (LITEAVATAR_MAX_SESSIONS, 2)"),
    "warm_workers": (int, "preloaded spare workers (LITEAVATAR_WARM_WORKERS, 1)"),
    "host": (str, "address to bind (LITEAVATAR_HOST, 127.0.0.1)"),
    "port": (int, "port to bind (LITEAVATAR_PORT, 8791)"),
    "refill_delay_s": (float, "seconds before preloading the next worker (LITEAVATAR_REFILL_DELAY_S, 60)"),
    "max_session_s": (float, "longest session in seconds (LITEAVATAR_MAX_SESSION_S, 1800)"),
    "mouth_gain": (float, "scale every mouth parameter (LITEAVATAR_MOUTH_GAIN, 1.0)"),
    "open_gain": (float, "scale the jaw opening only (LITEAVATAR_OPEN_GAIN, 1.0)"),
    "smooth_hz": (float, "mouth smoothing cut-off in Hz (LITEAVATAR_SMOOTH_HZ, 10)"),
    "head_motion": (float, "head sway amount, 0 = off (LITEAVATAR_HEAD_MOTION, 0)"),
}


def add_arguments(parser: argparse.ArgumentParser, names: list[str] | None = None) -> None:
    """Add --flag options for the given settings (all by default). Unset flags keep the environment's value."""
    group = parser.add_argument_group("renderer settings")
    for name in names or list(_FLAGS):
        typ, help_ = _FLAGS[name]
        group.add_argument(f"--{name.replace('_', '-')}", dest=name, type=typ, default=None, help=help_)
