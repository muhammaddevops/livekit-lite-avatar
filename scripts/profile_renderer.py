"""Where the renderer's time goes, per second of speech: speech features, audio-to-mouth, and drawing frames.

.venv/bin/python scripts/profile_renderer.py [speech.wav]
"""

from __future__ import annotations

import sys
import time
from collections import defaultdict

import librosa
import numpy as np

from livekit_lite_avatar.renderer.offline import default_wav
from livekit_lite_avatar.renderer.settings import Settings
from livekit_lite_avatar.renderer.stream import SR, from_settings


def main() -> None:
    settings = Settings.from_env()
    wav = sys.argv[1] if len(sys.argv) > 1 else str(default_wav(settings))
    audio, _ = librosa.load(wav, sr=SR, mono=True)
    audio = np.tile(audio, max(1, int(np.ceil(20 * SR / len(audio)))))
    s = from_settings(settings)
    spent: dict[str, float] = defaultdict(float)
    calls: dict[str, int] = defaultdict(int)

    def timed(name, fn):
        def wrapper(*a, **k):
            t = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                spent[name] += time.perf_counter() - t
                calls[name] += 1

        return wrapper

    s._features = timed("speech features (Paraformer)", s._features)
    s._window_params = timed("audio2mouth", s._window_params)
    torchscript_decoder = s._av.generator
    s._av.generator = timed("face decoder (net_decode)", s._av.generator)
    s._draw = timed("draw frame (decoder + blend)", s._draw)
    t0 = time.perf_counter()
    s.feed(audio)
    while s.ready(True):
        s.step(True)
    total = time.perf_counter() - t0
    secs = len(audio) / SR
    print(
        f"{secs:.1f}s of speech in {total:.2f}s (real-time factor {total / secs:.2f}), "
        f"{settings.threads} torch threads, {settings.fps} fps, width {settings.width}"
    )
    for name, t in spent.items():
        print(
            f"  {name:32s} {t / secs * 1000:7.1f} ms per second of speech  ({calls[name]} calls, "
            f"{t / calls[name] * 1000:.1f} ms each)"
        )
    compare_decoders(s, settings, torchscript_decoder)


def compare_decoders(s, settings, torchscript_decoder) -> None:
    """ms per frame for LiteAvatar's TorchScript decoder and the same decoder in ONNX Runtime."""
    import torch

    from livekit_lite_avatar.renderer.decoder import OnnxDecoder

    refs = s._av.ref_img_list
    params = torch.from_numpy(s.neutral[None].astype(np.float32))
    onnx = OnnxDecoder(torchscript_decoder, refs[0], settings.threads)
    for name, fn in (("torchscript", torchscript_decoder), ("onnxruntime", onnx)):
        with torch.inference_mode():
            for i in range(3):
                fn(refs[i], params)
            t = time.perf_counter()
            for i in range(30):
                out = fn(refs[i % len(refs)], params)
        print(f"  decoder in {name:12s} {(time.perf_counter() - t) / 30 * 1000:6.1f} ms per frame")
    with torch.inference_mode():
        diff = float((onnx(refs[5], params) - torchscript_decoder(refs[5], params)).abs().max())
    print(f"  max difference between them: {diff:.1e} (output range -1..1)")
    del out


if __name__ == "__main__":
    main()
