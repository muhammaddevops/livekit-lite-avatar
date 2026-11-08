"""Streaming wrapper around LiteAvatar (Alibaba HumanAIGC, MIT code) for live calls, CPU only.

LiteAvatar's own script is offline: it extracts speech features for a whole file, predicts 32 mouth parameters
per frame, then draws every frame. Here the same models run on a growing audio buffer, one window at a time:

  * speech features (Paraformer encoder) for a window of 1 s, with 1 s of left context and 0.5 s of lookahead;
  * audio2mouth on the 30 feature frames of that window -> 30 x 32 mouth parameters;
  * windows hop by 0.8 s and the 6 overlapping frames are cross-faded, as in LiteAvatar's own inference;
  * a zero-phase low-pass over recent history (LiteAvatar smooths the whole clip at once);
  * long pauses (>= 0.5 s under -50 dBFS) ease the mouth back to the neutral pose;
  * the 30 fps parameters are resampled to the output frame rate on one continuous timeline;
  * each parameter frame is drawn by the presenter's decoder and blended into the presenter's idle video.

Measured on an Apple M4 Pro CPU: features 0.13 s per window, audio2mouth 26 ms, drawing 14 ms per frame, so 0.8 s
of speech takes about 0.5 s. Everything here is synchronous; call it from a worker thread.
"""

from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path
from time import perf_counter

import cv2
import numpy as np

FPS = 30        # LiteAvatar's native parameter rate
SR = 16000      # model sample rate
WIN = 30        # frames per audio2mouth window (1 s)
HOP = 24        # frames committed per window (0.8 s)
FADE = WIN - HOP
LOOKAHEAD = 15  # frames of extra audio wanted before a window is processed (0.5 s)
LEFT_CTX = 30   # frames of audio before the window given to the feature encoder (1 s)
HIST = 30       # committed frames kept for smoothing
# Top principal components of audio2mouth's output on English and Arabic test speech. The first (70% of the
# variance) is the jaw opening; `open_gain` scales only that direction.
_MOUTH_PCS = Path(__file__).with_name("mouth_pcs.npy")


def _quiet(*_args, **_kwargs) -> None:
    pass


def _load_liteavatar(repo_dir: Path):
    """Import LiteAvatar from its checkout. Its modules load weights from paths relative to that folder at import
    time, so the working directory is switched there while importing and restored afterwards."""
    repo = str(repo_dir)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    cwd = os.getcwd()
    os.chdir(repo)
    warnings.filterwarnings("ignore", module=r"funasr_local(\.|$)")  # deprecation noise from its bundled FunASR
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import extract_paraformer_feature
            import lite_avatar
            from lite_avatar import liteAvatar

            extract_paraformer_feature.print = _quiet  # it prints a timing line for every window
            lite_avatar.tqdm = lambda it, *a, **k: it  # and a progress bar for every presenter load
            av = liteAvatar(data_dir=None, num_threads=1, generate_offline=False)
    finally:
        os.chdir(cwd)
    return av, extract_paraformer_feature.extract_para_feature


class LiteAvatarStream:
    def __init__(
        self,
        lite_avatar_dir: str | Path,
        presenter_dir: str | Path,
        *,
        out_width: int = 576,
        out_fps: int = 25,
        threads: int = 6,
        mouth_gain: float = 1.0,
        open_gain: float = 1.0,
        smooth_hz: float = 10.0,
        head_motion: float = 0.0,
    ) -> None:
        import torch
        from loguru import logger as loguru_logger
        from scipy import signal

        loguru_logger.remove()  # LiteAvatar logs every load step at INFO
        loguru_logger.add(sys.stderr, level="WARNING")
        torch.set_grad_enabled(False)
        torch.set_num_threads(threads)
        self.torch = torch
        self.out_fps = out_fps
        self.out_width = out_width
        self.mouth_gain = mouth_gain
        self.open_gain = open_gain
        self.head_motion = head_motion
        self._av, self._extract = _load_liteavatar(Path(lite_avatar_dir).resolve())
        self._a2m = self._av.audio2mouth
        self._b, self._a = signal.butter(4, 2 * min(smooth_hz, 14.5) / FPS, "lowpass")
        self._filtfilt = signal.filtfilt
        self._pc1 = np.load(_MOUTH_PCS)[0].astype(np.float32) if open_gain != 1.0 else None
        self.presenter_dir = ""
        self.load_presenter(presenter_dir)
        self.k_next = 0
        self._ef = self._es = 0.0  # fast / slow speech-energy envelopes for head motion (display order)
        self.reset()

    def load_presenter(self, presenter_dir: str | Path) -> None:
        """Load (or switch to) a presenter: its face encoder/decoder, reference frames and idle video."""
        presenter_dir = str(Path(presenter_dir).resolve())
        if presenter_dir == self.presenter_dir:
            return
        av = self._av
        av.load_dynamic_model(presenter_dir)
        self.presenter_dir = presenter_dir
        self.neutral = np.asarray(av.neutral_pose[:32], dtype=np.float32)
        # presenter video scaled once to the output size
        bg = [f for f in av.bg_data_list[: av.bg_video_frame_count] if f is not None]
        h0, w0 = bg[0].shape[:2]
        self.scale = self.out_width / w0
        self.width = self.out_width - self.out_width % 2
        self.height = round(h0 * self.scale) // 2 * 2
        self._bg = [cv2.resize(f, (self.width, self.height), interpolation=cv2.INTER_AREA) for f in bg]
        self._box = tuple(round(v * self.scale) for v in (av.y1, av.y2, av.x1, av.x2))
        y1, y2, x1, x2 = self._box
        self._mask = cv2.resize(av.merge_mask.astype(np.float32), (x2 - x1, y2 - y1))

    # ---------- presenter background: frame k of an endless ping-pong through the idle video
    def _pp(self, k: int) -> int:
        n = len(self._bg)
        r = k % (2 * n - 2)
        return r if r < n else 2 * n - 2 - r

    def idle_frame(self, k: int) -> np.ndarray:
        """Frame k of the presenter's idle loop (BGR, output size)."""
        return self._bg[self._pp(k)]

    def move(self, frame: np.ndarray, k: int, energy: float) -> np.ndarray:
        """Head and body life on top of the idle clip, applied in display order (frame k at the output rate):
        a slow sway, a slight lean-in while talking, and small nods on stressed syllables. The whole frame
        rotates about a point at chest height, so the head moves more than the shoulders. Off by default."""
        if self.head_motion <= 0:
            return frame
        dt = 1.0 / self.out_fps
        self._ef += (energy - self._ef) * (1 - np.exp(-dt / 0.07))
        self._es += (energy - self._es) * (1 - np.exp(-dt / 0.6))
        talk, nod = self._es, max(0.0, self._ef - self._es)
        t = k / self.out_fps
        two_pi = 2 * np.pi
        a = self.head_motion
        rot = a * (0.55 * np.sin(two_pi * t / 7.3) + 0.35 * np.sin(two_pi * t / 3.7 + 1.3)
                   + 0.6 * talk * np.sin(two_pi * t / 2.3 + 0.4))
        h, w = frame.shape[:2]
        tx = a * w * (0.004 * np.sin(two_pi * t / 9.1 + 0.5))
        ty = a * h * (0.003 * np.sin(two_pi * t / 5.3) + 0.012 * nod)
        scale = 1.035 + a * (0.01 * talk + 0.004 * np.sin(two_pi * t / 11.0))
        m = cv2.getRotationMatrix2D((w / 2, h * 0.78), float(rot), float(scale))
        m[:, 2] += (tx, ty)
        return cv2.warpAffine(frame, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)

    def _energy(self, t0: float, t1: float) -> float:
        """Speech loudness of [t0, t1) seconds of this segment, 0 (quiet) .. 1 (loud)."""
        x = self.audio[int(t0 * SR): int(t1 * SR)]
        if not len(x):
            return 0.0
        db = 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)
        return float(np.clip((db + 45) / 30, 0, 1))

    def _draw(self, params: np.ndarray) -> np.ndarray:
        i = self._pp(self.k_next)  # the caller sets k_next so speech continues the idle motion
        self.k_next += 1
        out = self._av.generator(self._av.ref_img_list[i],
                                 self.torch.from_numpy(params[None].astype(np.float32)))
        mouth = ((out[0].permute(1, 2, 0).numpy() / 2 + 0.5).clip(0, 1) * 255).astype(np.uint8)[:, :, ::-1]
        y1, y2, x1, x2 = self._box
        mouth = cv2.resize(mouth, (x2 - x1, y2 - y1), interpolation=cv2.INTER_AREA)
        frame = self._bg[i].copy()
        region = frame[y1:y2, x1:x2].astype(np.float32)
        frame[y1:y2, x1:x2] = (mouth * (1 - self._mask) + region * self._mask).astype(np.uint8)
        return frame

    # ---------- one spoken segment
    def reset(self) -> None:
        self.audio = np.zeros(0, dtype=np.float32)  # 16 kHz mono, whole segment so far
        self.done = 0                                 # frames committed
        self._tail: np.ndarray | None = None          # previous window's uncommitted frames (FADE x 32)
        self._hist = np.zeros((0, 32), np.float32)    # recent raw committed params, for smoothing
        self.stats = {"windows": 0, "compute_s": 0.0}
        self._out_k = 0                               # output frames produced for this segment
        self._drawn = 0                               # output frames drawn for this segment
        self._prev: tuple[float, np.ndarray] | None = None  # last 30 fps row, for resampling across windows

    def feed(self, pcm16k: np.ndarray) -> None:
        self.audio = np.concatenate([self.audio, pcm16k.astype(np.float32)])

    def _frames_avail(self) -> int:
        return int(len(self.audio) / SR * FPS)

    def ready(self, final: bool) -> bool:
        n = self._frames_avail()
        if final:
            return self.done < n
        need = WIN + (LOOKAHEAD if self.done else 5)  # be quicker on the very first window
        return self.done + need <= n

    def _features(self, c0: int, c1: int) -> np.ndarray:
        """Paraformer features for frames [c0, c1), one row per 30 fps frame."""
        seg = self.audio[int(c0 / FPS * SR): int(c1 / FPS * SR)]
        feats = self._extract(seg, c1 - c0)
        if feats.shape[0] < c1 - c0:  # keep indices aligned if the encoder returns fewer frames
            feats = np.concatenate([feats, np.repeat(feats[-1:], c1 - c0 - feats.shape[0], 0)])
        return feats

    def _window_params(self, win_start: int, feats: np.ndarray, c0: int) -> np.ndarray:
        """audio2mouth for frames [win_start, win_start + WIN), shape (WIN, 32)."""
        win = feats[win_start - c0: win_start - c0 + WIN]
        if win.shape[0] < WIN:
            win = np.concatenate([win, np.repeat(win[-1:], WIN - win.shape[0], 0)])
        ph = np.zeros((1, WIN, 2), np.float32)
        out, _ = self._a2m.audio2mouth_model.run(
            ["output", "feat"],
            {"input_au": win[None].astype(np.float32), "input_ph": ph, "input_sp": self._a2m.sp, "w": self._a2m.w},
        )
        return out[0].astype(np.float32)

    def _silence(self, start: int, n: int) -> np.ndarray:
        """1 where a frame sits inside a pause of 0.5 s or more, softened at the edges."""
        hop = SR // FPS
        total = self._frames_avail()
        lo, hi = max(0, start - 20), min(total, start + n + 20)
        x = self.audio[lo * hop: hi * hop]
        if len(x) < hop:
            return np.zeros(n, np.float32)
        frames = x[: (len(x) // hop) * hop].reshape(-1, hop)
        quiet = 20 * np.log10(np.sqrt((frames ** 2).mean(1)) + 1e-9) < -50
        run = np.zeros(len(quiet), bool)
        i = 0
        while i < len(quiet):
            if quiet[i]:
                j = i
                while j < len(quiet) and quiet[j]:
                    j += 1
                if j - i >= 15:
                    run[i:j] = True
                i = j
            else:
                i += 1
        s = np.convolve(np.pad(run.astype(np.float32), 2, mode="edge"), [0.1, 0.2, 0.4, 0.2, 0.1], "same")[2:-2]
        out = s[start - lo: start - lo + n]
        return np.pad(out, (0, n - len(out))) if len(out) < n else out

    def step(self, final: bool) -> list[tuple[np.ndarray, float]]:
        """plan() + draw(): newly drawn (BGR frame, speech energy) pairs, or [] if more audio is needed."""
        return self.draw(self.plan(final))

    def plan(self, final: bool) -> np.ndarray:
        """Mouth parameters for the next one or two windows (n x 32), if enough audio is buffered."""
        if not self.ready(final):
            return np.zeros((0, 32), np.float32)
        t = perf_counter()
        total, start = self._frames_avail(), self.done
        # when audio is buffered ahead (TTS usually is), share one feature pass between two windows
        nwin = 2 if not final and start + HOP + WIN + LOOKAHEAD <= total else 1
        c0 = max(0, start - LEFT_CTX)
        c1 = min(total, start + (nwin - 1) * HOP + WIN + LOOKAHEAD)
        feats = self._features(c0, c1)
        rows = np.concatenate([self._commit_window(feats, c0, total, final) for _ in range(nwin)])
        self.stats["windows"] += nwin
        self.stats["compute_s"] += perf_counter() - t
        return self._resample(rows, start, last=final and self.done >= total)

    def _resample(self, rows: np.ndarray, start: int, last: bool) -> np.ndarray:
        """30 fps parameter rows -> output-rate rows on one continuous timeline across windows."""
        if self.out_fps == FPS or not len(rows):
            return rows
        t30 = (start + np.arange(len(rows))) / FPS
        if self._prev is not None:
            t30 = np.concatenate([[self._prev[0]], t30])
            rows_i = np.concatenate([self._prev[1][None], rows])
        else:
            rows_i = rows
        end = t30[-1] + (1 / FPS if last else 0)  # the last window plays to its end
        k1 = int(np.floor(end * self.out_fps + 1e-6)) + (0 if not last else 1)
        tk = np.arange(self._out_k, max(self._out_k, k1)) / self.out_fps
        tk = tk[tk <= end + 1e-6]
        out = np.stack([np.interp(tk, t30, rows_i[:, c]) for c in range(rows_i.shape[1])], 1) if len(tk) else rows[:0]
        self._out_k += len(tk)
        self._prev = (t30[-1], rows_i[-1])
        return out.astype(np.float32)

    def draw(self, rows: np.ndarray) -> list[tuple[np.ndarray, float]]:
        """(frame, speech energy) for each row; pass both to move() when the frame is shown."""
        if not len(rows):
            return []
        t = perf_counter()
        out = []
        with self.torch.inference_mode():  # grad mode is per thread, and this runs in a worker thread
            for r in rows:
                t0 = self._drawn / self.out_fps
                self._drawn += 1
                out.append((self._draw(r), self._energy(t0, t0 + 1 / self.out_fps)))
        self.stats["compute_s"] += perf_counter() - t
        return out

    def _commit_window(self, feats: np.ndarray, c0: int, total: int, final: bool) -> np.ndarray:
        start = self.done
        last = final and start + HOP >= total
        win_start = start if not last or total - start >= WIN or start < WIN else total - WIN
        p = self._window_params(win_start, feats, c0)[start - win_start:]
        if self._tail is not None:  # cross-fade into the previous window's overlap
            k = min(FADE, len(p), len(self._tail))
            w = (np.arange(1, k + 1, dtype=np.float32) / (k + 1))[:, None]
            p[:k] = self._tail[:k] * (1 - w) + p[:k] * w
        n = (total - start) if last else min(HOP, total - start)
        commit, self._tail = p[:n], (None if last else p[n:n + FADE])
        # zero-phase smoothing over [history, new, lookahead]
        future = self._tail if self._tail is not None else np.zeros((0, 32), np.float32)
        seq = np.concatenate([self._hist, commit, future])
        if len(seq) > 15:
            seq = self._filtfilt(self._b, self._a, seq, axis=0, padtype=None).astype(np.float32)
        smooth = seq[len(self._hist): len(self._hist) + n]
        self._hist = np.concatenate([self._hist, commit])[-HIST:]
        sil = self._silence(start, n)[:, None]
        smooth = smooth * (1 - sil) + self.neutral * sil
        if self.mouth_gain != 1.0 or self._pc1 is not None:
            d = smooth - self.neutral
            if self._pc1 is not None:
                c = d @ self._pc1
                d = d + (self.open_gain - 1.0) * c[:, None] * self._pc1
            smooth = self.neutral + d * self.mouth_gain
        if last:  # ease back to the neutral pose, as LiteAvatar's padding_last does
            ease = [(self.neutral + (smooth[-1] - self.neutral) * (4 - k) / 5) for k in range(5)]
            smooth = np.concatenate([smooth, np.asarray(ease, np.float32)])
        self.done = total if last else start + n
        return smooth.astype(np.float32)


def from_settings(settings, presenter: str | None = None) -> LiteAvatarStream:
    """A LiteAvatarStream configured from renderer Settings."""
    return LiteAvatarStream(
        settings.lite_avatar_dir,
        settings.presenter_dir(presenter),
        out_width=settings.width,
        out_fps=settings.fps,
        threads=settings.threads,
        mouth_gain=settings.mouth_gain,
        open_gain=settings.open_gain,
        smooth_hz=settings.smooth_hz,
        head_motion=settings.head_motion,
    )
