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
- Streaming: one video layer at 2.5 Mbps (no simulcast), a one-frame lip delay, 25 fps, a 2-frame first batch so
  speech starts about 0.35 s after `say()` on an Apple M4 Pro. When the CPU can't keep up, the voice never pauses:
  late frames are skipped instead.
- Tests: unit tests, an offline render, the worker's generator played in real time (and on a simulated slow CPU),
  and live end-to-end tests on a local `livekit-server` with a real `AgentSession` and a measuring viewer.
- A zero-key browser demo (`examples/local_demo`), a minimal agent example, a Docker image, a benchmark script
  and CI on Linux x86, Linux ARM and macOS.
