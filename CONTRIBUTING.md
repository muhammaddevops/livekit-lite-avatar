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
| `test_session.py` | agent-side token and request to the renderer |
| `test_server.py` | server validation, auth and settings; the real pool against fake workers that load, join, fail to join or die while loading |
| `test_resampler.py` | the incremental 24 to 16 kHz resampler matches resampling the whole reply |
| `test_offline.py` | WAV in, MP4 out; the worker's generator at real-time pace with fast TTS, real-time TTS, a simulated slow CPU, and a reply after an interruption |
| `test_e2e.py` | livekit-server, renderer, a real `AgentSession` and a viewer: publishing, playback, interruption, and the fall-back to voice when the avatar leaves |
| `test_demo.py` | the browser demo's agent: greeting, then repeating a microphone track |

For performance work, `scripts/profile_renderer.py` splits the time per second of speech into speech features,
audio-to-mouth and the face decoder, and `scripts/benchmark.py` measures a live call from a viewer.

## Release

1. Update the version in `pyproject.toml` and the date in `CHANGELOG.md`.
2. Tag and push: `git tag v0.1.0 && git push origin v0.1.0`. The release workflow runs the whole CI suite on that
   commit, checks the tag against the version, pushes the renderer image to GHCR (amd64 and arm64) and creates the
   GitHub release from the changelog, with the wheel and sdist attached.
3. PyPI is opt-in: add a trusted publisher on PyPI (this repository, workflow `release.yml`, environment `pypi`)
   and set the repository variable `PYPI_PUBLISH=true`. From then on, releases are published there too.
