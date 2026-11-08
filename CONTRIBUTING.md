# Contributing

Issues and pull requests are welcome. Benchmark results from CPUs not listed in the README are especially useful:
there is an issue form for them.

## Set up

```bash
scripts/setup-renderer.sh      # .venv with the renderer and dev dependencies, plus LiteAvatar's assets (~1.3 GB)
brew install livekit           # or: curl -sSL https://get.livekit.io | bash   (for the live tests and the demo)
```

## Check your change

```bash
uvx ruff@0.17.4 check src tests examples scripts
uvx ruff@0.17.4 format src tests examples scripts
.venv/bin/pytest                # everything, including live end-to-end runs on a local livekit-server (~3 min)
```

Tests skip themselves when something they need is missing (the renderer extra, the assets, `livekit-server`), so
`pytest` also works in an agent-only environment. On a CPU slower than real time, `LITEAVATAR_PERF_CHECKS=0`
keeps the functional checks and skips the timing ones.

| Test | What it covers |
|---|---|
| `test_session.py`, `test_server.py` | agent-side token and request; server validation, auth and settings |
| `test_resampler.py` | the incremental 24 to 16 kHz resampler matches resampling the whole reply |
| `test_offline.py` | WAV in, MP4 out; the worker's generator played in real time, and on a simulated slow CPU |
| `test_e2e.py` | livekit-server, renderer, a real `AgentSession` and a viewer: publishing, playback, interruption |
| `test_demo.py` | the browser demo's agent: greeting, then repeating a microphone track |

For performance work, `scripts/profile_renderer.py` splits the time per second of speech into speech features,
audio-to-mouth and the face decoder, and `scripts/benchmark.py` measures a live call from a viewer.

## Release

1. Update the version in `pyproject.toml` and the date in `CHANGELOG.md`.
2. Tag and push: `git tag v0.1.0 && git push origin v0.1.0`. The release workflow checks the tag against the
   version, publishes to PyPI (trusted publishing, environment `pypi`), pushes the renderer image to GHCR and
   creates the GitHub release from the changelog.
