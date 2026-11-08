# Changelog

## [0.1.0] - 2026-11-08

First release: a self-hosted, CPU-only, lip-synced video avatar for LiveKit Agents 1.8, built on Alibaba's
LiteAvatar. It needs no GPU, no avatar vendor and no per-minute fees.

**Agent side** (a small package; no torch)
- `LiteAvatarSession`, used like the hosted avatar plugins. The agent's TTS audio goes to the renderer over LiveKit
  data streams, and the renderer publishes lip-synced video and the same audio on the agent's behalf.
- The renderer reports when each reply starts and stops playing, so the agent's speaking state, transcription
  timing and interruptions follow the avatar.
- `start()` returns once the avatar is in the room. It raises `LiteAvatarError` (with nothing changed) if the
  renderer is unreachable, busy, or can't join. If the avatar leaves mid-call, the agent carries on with its own
  voice.

**Renderer** (`liteavatar-renderer`, or the Docker image)
- Commands:
  - `setup` downloads LiteAvatar's code, weights and presenters from upstream, with SHA-256 checks; nothing from
    LiteAvatar is included.
  - `serve` runs a warm pool of worker processes behind a small HTTP API. It answers only once a worker has joined
    the room, retries workers that fail to load, and reports its health and speed.
  - `render` renders a WAV offline.
- The face decoder is rebuilt from each presenter's TorchScript weights and runs in ONNX Runtime: identical output,
  about 3x faster on x86 and ARM. `LITEAVATAR_DECODER=torch` keeps LiteAvatar's own.
- Streaming:
  - one video layer at 2.5 Mbps (no simulcast);
  - a one-frame lip delay;
  - 25 fps;
  - small first batches.
- When the CPU can't keep up, the voice never pauses: each frame is aimed far enough ahead to be ready in time, and
  the rest are skipped. TTS that streams only at real-time pace plays without skipped frames.
- Each worker estimates the machine's speed as it loads and suggests a frame rate if needed.

**Measured** (`scripts/benchmark.py`; real-time factor = drawing time divided by speech length)
- Apple M4 Pro: real-time factor 0.37, first sound about 0.45 s after `say()`, 23 new lip pictures a second (of 25).
- 4-vCPU x86 CI runner: 0.63, 0.42 s, 23 a second.

**Tests and tooling**
- Tests:
  - unit tests, and the server against fake workers;
  - the worker's output played in real time with fast TTS, real-time TTS, a simulated slow CPU, a reply after an
    interruption, and a renderer error;
  - live end-to-end calls on a local `livekit-server`, with a real `AgentSession` and a measuring viewer;
  - the browser demo, driven with a synthetic microphone.
- CI on Linux x86, Linux ARM and macOS, with a benchmark in every run's summary.
- A zero-key browser demo (`examples/local_demo`), a minimal agent example, a Docker image, a benchmark script and
  a profiler.

**Licences**: this project is Apache-2.0. LiteAvatar's code is MIT and the Paraformer model is Apache-2.0. The face
weights and gallery presenters have no stated licence; see the README before any commercial use.
