# Changelog

## [0.1.0] - unreleased

First release.

- `LiteAvatarSession`: a self-hosted, CPU-only video avatar for LiveKit Agents 1.8, used like the hosted avatar
  plugins. The agent's TTS audio goes to the renderer over LiveKit data streams; the renderer publishes lip-synced
  video and the same audio on the agent's behalf, and reports playback start and end, so the agent's speaking
  state, transcription timing and interruptions follow the avatar.
- `liteavatar-renderer`: a warm pool of worker processes behind a small HTTP API (`serve`), asset download from
  upstream with SHA-256 checks (`setup`), an offline render (`render`), presenter switching per session, and a
  speed estimate for the machine in the log and `/health`.
- Streaming: one video layer at 2.5 Mbps (no simulcast), a one-frame lip delay, 25 fps, and small first batches,
  so speech starts about 0.35 s after `say()` on an Apple M4 Pro. When the CPU can't keep up, the voice never
  pauses: late frames are skipped instead. TTS that streams only at real-time pace plays without skipped frames.
- Robustness: the renderer accepts a session only once its worker has joined the room (502 with the reason
  otherwise); if the avatar leaves mid-call, the agent falls back to its own voice; workers that fail to load are
  retried and reported by `/health`; an agent that gives up doesn't leave a silent avatar behind.
- `LITEAVATAR_DECODER=onnx`: the face decoder in ONNX Runtime, rebuilt from the presenter's weights.
- Tests: unit tests, an offline render, the worker's generator played in real time (and on a simulated slow CPU),
  and live end-to-end tests on a local `livekit-server` with a real `AgentSession` and a measuring viewer.
- A zero-key browser demo (`examples/local_demo`), a minimal agent example, a Docker image, a benchmark script
  and CI on Linux x86, Linux ARM and macOS.
