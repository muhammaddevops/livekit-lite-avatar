"""The worker's incremental 24 kHz -> 16 kHz resampler matches resampling the whole reply at once."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("cv2", reason="needs the renderer extra")
from scipy.signal import resample_poly

from livekit_lite_avatar.renderer.worker import Resampler24to16


@pytest.mark.parametrize("max_chunk", [7, 480, 2400, 24000])
def test_incremental_equals_whole(max_chunk):
    rng = np.random.default_rng(max_chunk)
    x = (rng.standard_normal(24000 * 3) * 8000).clip(-32768, 32767).astype(np.int16)
    r = Resampler24to16()
    i = 0
    while i < len(x):
        n = int(rng.integers(1, max_chunk + 1))
        out = r.append(x[i : i + n])
        i += n
    ref = resample_poly(x.astype(np.float32) / 32768.0, 2, 3).astype(np.float32)
    assert len(out) == len(ref)
    assert np.array_equal(out, ref)
