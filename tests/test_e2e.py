"""End-to-end on one machine: livekit-server, the renderer, a real AgentSession and a viewer (tests/e2e/run.py).

Skipped unless `livekit-server` is on PATH and the renderer is set up. Two replies of LiteAvatar's speech sample:
the first plays in full, the second is interrupted 1.5 s after the avatar starts speaking.

    pytest -s tests/test_e2e.py
    LITEAVATAR_PERF_CHECKS=0 pytest tests/test_e2e.py      # keep the functional checks, skip the timing ones
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from livekit_lite_avatar.renderer import assets
from livekit_lite_avatar.renderer.settings import Settings

SETTINGS = Settings.from_env()
pytestmark = [
    pytest.mark.skipif(importlib.util.find_spec("torch") is None, reason="needs the renderer extra"),
    pytest.mark.skipif(shutil.which("livekit-server") is None, reason="livekit-server not installed"),
    pytest.mark.skipif(bool(assets.check(SETTINGS)), reason="renderer assets not installed"),
]
sys.path.insert(0, str(Path(__file__).parent / "e2e"))
PERF = os.getenv("LITEAVATAR_PERF_CHECKS", "1") != "0"


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    import run as e2e

    from livekit_lite_avatar.renderer.offline import default_wav

    wav = os.getenv("LITEAVATAR_TEST_WAV") or str(default_wav(SETTINGS))
    out = e2e.run([wav, wav], interrupt_after=1.5, keep_logs=tmp_path_factory.mktemp("e2e-logs"))
    print("\n" + json.dumps(out, indent=2))
    return out


def test_avatar_publishes_one_layer_on_the_agents_behalf(result):
    assert result["avatar_attributes"]["lk.publish_on_behalf"] == result["agent_identity"]
    (video,) = [p for p in result["video_publications"] if p["from"] == "liteavatar"]
    (audio,) = [p for p in result["audio_publications"] if p["from"] == "liteavatar"]
    assert video["simulcasted"] is False
    assert (video["width"], video["source"]) == (SETTINGS.width, "SOURCE_CAMERA")
    assert audio["source"] == "SOURCE_MICROPHONE"
    # the agent itself publishes no audio: its voice comes from the avatar, in sync with the face
    assert not [p for p in result["audio_publications"] if p["from"] == result["agent_identity"]]


def test_full_reply_plays_and_the_agent_hears_it_finish(result):
    first = result["speech"][0]
    assert first["interrupted"] is False
    # heard in full, at its own length. On a machine far behind real time (LITEAVATAR_PERF_CHECKS=0, such as CI's
    # 3-vCPU macOS runner, 4x too slow), the renderer's drawing starves everything else on it, and the voice reaches
    # the viewer with gaps of up to a second or so in all; none of it may be lost
    assert first["audible_s"] > first["wav_audible_s"] - 0.25, "speech was cut short"
    assert first["audible_s"] < first["wav_audible_s"] + (0.25 if PERF else 2.5), "speech was stretched"
    # the agent hears playback finish when it did, not once the renderer has caught up: within 3 s of the speech
    # normally, 6 s on a slow machine (the voice starts at most 1 s late there, and may be stretched)
    slack = 3 if PERF else 6
    assert first["agent_playout_s"] <= first["wav_s"] + slack, "the agent waited too long for playback to finish"


def test_interruption_stops_the_avatar(result):
    second = result["speech"][1]
    assert second["interrupted"] is True
    # within 0.6 s normally; a machine already far behind real time (LITEAVATAR_PERF_CHECKS=0) gets 3 s
    assert second["audio_stopped_after_interrupt_s"] < (0.6 if PERF else 3.0)


@pytest.mark.skipif(not PERF, reason="LITEAVATAR_PERF_CHECKS=0")
def test_real_time(result):
    first = result["speech"][0]
    assert first["start_latency_s"] < 1.0
    assert first["video_fps_while_speaking"] >= SETTINGS.fps * 0.9
    assert first["longest_video_gap_ms"] < 250


def test_agent_falls_back_to_voice_when_the_avatar_leaves(tmp_path):
    """If the avatar leaves mid-call (renderer stopped, time limit), the agent carries on with its own voice."""
    import run as e2e

    from livekit_lite_avatar.renderer.offline import default_wav

    wav = os.getenv("LITEAVATAR_TEST_WAV") or str(default_wav(SETTINGS))
    out = e2e.run([wav, wav], remove_avatar_after_first=True, keep_logs=tmp_path)
    print("\n" + json.dumps(out["speech"], indent=2))
    first, second = out["speech"]
    assert first["heard_from"] == "avatar"
    assert second["heard_from"] == "agent", "the second reply wasn't heard at all"
    # heard in full; on a machine far behind real time (LITEAVATAR_PERF_CHECKS=0) it may arrive stretched, as above
    assert second["audible_s"] > second["wav_audible_s"] - 0.25, "the second reply was cut short"
    assert second["audible_s"] < second["wav_audible_s"] + (0.25 if PERF else 2.5), "the second reply was stretched"
