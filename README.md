# livekit-liteavatar

A self-hosted, lip-synced video avatar for [LiveKit Agents](https://github.com/livekit/agents) (Python,
`livekit-agents` 1.8.x). It runs on CPU: no GPU, no avatar vendor and no per-minute fees.

It is used like LiveKit's hosted avatar plugins (Tavus, Hedra, Simli and others): create an avatar session and
start it with your `AgentSession`. Instead of calling a vendor, it calls a renderer you run yourself. The renderer
draws the face with Alibaba's [LiteAvatar](https://github.com/HumanAIGC/lite-avatar) models and publishes the video
into the room.

```python
from livekit_liteavatar import LiteAvatarSession

avatar = LiteAvatarSession(renderer_url="http://127.0.0.1:8791")
await avatar.start(session, room=ctx.room)
```

Status: alpha. Measured on an Apple M4 Pro only. Read [Licences](#licences) before any commercial use: LiteAvatar's
face weights and presenters have no stated licence.

## How it works

```
 agent process                                   renderer process (same machine or another)
 ─────────────                                   ──────────────────────────────────────────
 LiteAvatarSession.start() ── POST /sessions ──► server: keeps a worker preloaded ("warm")
   mints a token for the avatar participant        hands the session to it
                                                    │
 AgentSession TTS audio ── LiveKit data stream ──►  worker (one process per session)
                                                     Paraformer speech features
                                                     → audio2mouth (32 mouth parameters per frame)
                                                     → presenter's face decoder, blended into its idle video
                                                    │
 LiveKit room ◄── video + the same audio, published on the agent's behalf, in sync
```

1. `LiteAvatarSession.start()` mints a LiveKit token for an avatar participant that may publish on the agent's
   behalf (`lk.publish_on_behalf`) and posts it to the renderer.
2. The renderer hands the session to a preloaded worker, which joins the room as that participant.
3. The agent's TTS audio is re-routed to the worker over LiveKit data streams (LiveKit's own
   `DataStreamAudioOutput`, the mechanism the hosted avatar plugins use).
4. The worker runs LiteAvatar on the audio as it arrives, in 1 s windows with 0.5 s of lookahead, and publishes
   video and the same audio through LiveKit's `AVSynchronizer`, so lips and voice stay together. Between replies it
   plays the presenter's idle loop. Interruptions clear its buffers at once. Each reply ends with about 0.2 s of
   silence while the mouth eases shut.

Users see one participant with video and audio, as with any avatar plugin.

## Quick start

You need Python 3.10, git, ffmpeg (for the offline test's MP4) and about 1.3 GB of disk for LiteAvatar's weights.

### 1. The renderer

```bash
git clone <this repo> livekit-liteavatar && cd livekit-liteavatar
scripts/setup-renderer.sh
```

This creates `.venv` with the renderer dependencies (it uses [uv](https://docs.astral.sh/uv/) if installed, otherwise
pip), then downloads LiteAvatar's code from GitHub and its weights and the default presenter from ModelScope into
`~/.cache/livekit-liteavatar` (`LITEAVATAR_HOME`). Nothing from LiteAvatar is included in this repository.

Check it works, offline (WAV in, MP4 out, prints the real-time factor):

```bash
.venv/bin/liteavatar-renderer render
```

With no arguments it renders a short Mandarin speech sample that comes with LiteAvatar. Give it your own WAV for a
more useful check: `liteavatar-renderer render speech.wav out.mp4` (on macOS,
`say -o speech.wav --data-format=LEI16@16000 "Hello there"` makes one).

Run the service the agent calls:

```bash
.venv/bin/liteavatar-renderer serve
```

It listens on http://127.0.0.1:8791 and loads a worker straight away (about 7 s on an M4 Pro, longer the first
time), so the first session doesn't wait for models. `GET /health` shows how many workers are ready and running.

### 2. The agent

In your agent's environment (any Python 3.10+; it doesn't need torch):

```bash
pip install /path/to/livekit-liteavatar
```

```python
from livekit.agents import AgentSession, JobContext
from livekit_liteavatar import LiteAvatarError, LiteAvatarSession

async def entrypoint(ctx: JobContext) -> None:
    session = AgentSession(stt=..., llm=..., tts=..., vad=...)
    await ctx.connect()

    avatar = LiteAvatarSession(renderer_url="http://127.0.0.1:8791")
    try:
        await avatar.start(session, room=ctx.room)
    except LiteAvatarError:
        pass  # renderer not running or full: the agent carries on voice-only

    await session.start(agent=MyAgent(), room=ctx.room)
```

`examples/agent.py` is a complete agent (STT, LLM and TTS through LiveKit Inference):

```bash
python examples/agent.py dev
```

`LiteAvatarSession` options: `renderer_url` (or `LITEAVATAR_RENDERER_URL`), `presenter` (an id installed on the
renderer; default: the renderer's default), `renderer_secret` (or `LITEAVATAR_SECRET`),
`avatar_participant_identity`, `avatar_participant_name` and `request_timeout`. `start()` also accepts
`livekit_url`, `livekit_api_key` and `livekit_api_secret`, which otherwise come from the usual `LIVEKIT_*` variables.
The agent's audio is only re-routed once the renderer has accepted, so on `LiteAvatarError` nothing has changed.

### 3. The browser

Turn off adaptive stream in the client that shows the avatar:

```js
import { Room } from "livekit-client";
const room = new Room({ adaptiveStream: false });
```

With React components: `<LiveKitRoom options={{ adaptiveStream: false }} ...>`. Adaptive stream sizes and pauses a
subscription to fit the video element; for a talking face, a smaller or choppier picture reads as lips out of sync
(see [Streaming quality](#streaming-quality-and-lip-timing)).

## Configuration

The renderer reads `LITEAVATAR_*` environment variables; command-line flags to `liteavatar-renderer serve` win, and
the server passes its settings on to every worker it starts.

| Setting | Environment variable | Flag | Default | Notes |
|---|---|---|---|---|
| Frame rate | `LITEAVATAR_FPS` | `--fps` | 25 | LiteAvatar's own rate is 30; 25 saves about 10% CPU |
| Width | `LITEAVATAR_WIDTH` | `--width` | 576 | height follows the presenter (576x792 for the default) |
| Torch threads | `LITEAVATAR_THREADS` | `--threads` | 6 | per worker; leave cores for video encoding and the agent |
| Lip delay | `LITEAVATAR_LIP_DELAY_FRAMES` | `--lip-delay-frames` | 1 | video starts this many frames after its audio |
| Bitrate | `LITEAVATAR_BITRATE` | `--bitrate` | 2500000 | one video layer, no simulcast |
| Presenter | `LITEAVATAR_PRESENTER` | `--presenter` | `P1yerb8kIA7eBpaIydU2lwzA` | default for sessions that don't ask for one |
| Pool: sessions | `LITEAVATAR_MAX_SESSIONS` | `--max-sessions` | 2 | concurrent sessions, one worker process each |
| Pool: warm workers | `LITEAVATAR_WARM_WORKERS` | `--warm-workers` | 1 | preloaded spares, within `max-sessions` |
| Refill delay | `LITEAVATAR_REFILL_DELAY_S` | `--refill-delay-s` | 60 | wait before preloading the next worker (skipped if a session is waiting) |
| Session limit | `LITEAVATAR_MAX_SESSION_S` | `--max-session-s` | 1800 | a worker leaves after this long |
| Address | `LITEAVATAR_HOST`, `LITEAVATAR_PORT` | `--host`, `--port` | 127.0.0.1, 8791 | |
| Secret | `LITEAVATAR_SECRET` | | none | if set, requests need a matching `X-Renderer-Secret` header |
| Data folder | `LITEAVATAR_HOME` | `--home` | `~/.cache/livekit-liteavatar` | LiteAvatar code, weights, presenters |

The renderer only needs to reach your LiveKit server; the agent needs to reach the renderer over HTTP. If you bind
it to anything other than localhost, set `LITEAVATAR_SECRET` (and the same secret on the agent side) and keep it
behind a firewall: the server has no other authentication.

## Presenters

The presenters are Alibaba's, from ModelScope's
[LiteAvatarGallery](https://modelscope.cn/models/HumanAIGC-Engineering/LiteAvatarGallery). LiteAvatar has no public
tool to make a presenter from your own photo or video.

```bash
.venv/bin/liteavatar-renderer setup --presenter <id> [--presenter <id> ...] [--gallery 20250408]
.venv/bin/liteavatar-renderer setup --presenter sample     # the sample presenter in LiteAvatar's own repo
.venv/bin/liteavatar-renderer presenters                   # list installed ones
```

A session can ask for any installed presenter: `LiteAvatarSession(presenter="<id>")`. The warm worker has the
default presenter loaded and switches in a few seconds.

## Performance

Measured on an Apple M4 Pro (CPU only, 576x792 at 25 fps), during live calls with the LLM, agent and browser on the
same machine:

- each reply is drawn in **0.44 to 0.47** of its spoken length (for example 14.3 s of speech in 6.5 s);
- lips start moving about 0.8 s after the first audio of a reply arrives;
- the browser played 1,321 of 1,323 frames, receiving 576x792 at 24 to 26 fps.

Offline on the same machine (`liteavatar-renderer render`): 9.1 s of English speech in 4.8 s (real-time factor
0.52), LiteAvatar's 5.6 s Mandarin sample at 0.55, first frames once 1.2 s of audio had arrived, and models loaded
in about 7 s.

There are no measurements yet for other CPUs, including the x86 machines most cloud providers rent. Run
`liteavatar-renderer render` on yours: a real-time factor below 1 means it can keep up, and live calls need
headroom on top (video encoding, the agent, other sessions), so aim for about 0.5.

Per window of 0.8 s of speech on the M4 Pro: speech features 0.13 s, audio2mouth 26 ms, then 14 ms to draw each
frame.

## Streaming quality and lip timing

These defaults come from testing in live calls; what reaches the browser matters more than the renderer's own
settings.

- **One video layer, about 2.5 Mbps.** With LiveKit's defaults (simulcast plus the client's adaptive stream), a
  smaller video element made the browser pick a 360x495 copy at about 20 fps: softer, choppier, and choppy lips read
  as out of sync. The renderer now publishes a single layer and the client turns adaptive stream off.
- **Lip delay of one frame.** LiteAvatar's lips run about 40 ms ahead of other lip-sync models on the same clips, so
  each reply's video starts one frame (40 ms at 25 fps) after its audio. With one full-quality layer, audio and
  video sit in the browser's buffers for about the same time (35 and 30 ms).
- **6 torch threads, 25 fps,** and the next warm worker loads 60 s after a session starts rather than during it.
  These left enough headroom on the M4 Pro with the agent and browser on the same machine.
- If LiveKit's audio stream to the renderer breaks mid-reply (seen once: "expected chunk index to be exactly one more
  than the previous"), the worker ends that reply and opens a fresh receiver, so the next reply plays normally.

## Tuning

Off by default, because in a live call they looked worse overall, but available:

| Setting | Effect |
|---|---|
| `LITEAVATAR_OPEN_GAIN=1.8` | Opens the mouth wider. audio2mouth's 32 parameters move mostly along one direction (70% of the variance: the jaw opening); this scales only that direction, without artefacts. On an English test clip it raised lip movement from 5.0 to 7.3 (% of face height) with closures on m/b/p still at 100%. |
| `LITEAVATAR_MOUTH_GAIN` | Scales every mouth parameter; leaves white specks and smears from about 1.3. |
| `LITEAVATAR_HEAD_MOTION=1.8` | A gentle sway, a slight lean-in while talking and small nods, by moving the whole frame about a point at chest height. It resamples every frame, which softens the picture; above about 2.5 it reads as a moving camera. |
| `LITEAVATAR_FPS=30` | Smoother lips for about 10% more CPU. |
| `LITEAVATAR_SMOOTH_HZ` | Low-pass on the mouth parameters (10 Hz, LiteAvatar's own); 14 Hz made no measurable difference. |

Try them offline first: `liteavatar-renderer render speech.wav out.mp4 --open-gain 1.8`.

## Limits

- **CPU per session.** One session keeps about 6 threads busy while the avatar talks. Two sessions at once is the
  default limit and what the M4 Pro was sized for; more needs more cores (or machines, each with its own renderer).
- **Mouth only.** LiteAvatar redraws the mouth area; the rest is the presenter's idle video, looped. No head turns
  or gestures.
- **Startup.** A worker takes from about 7 s (M4 Pro) to a minute to load, which is why one is kept warm. If none
  is ready, `start()` waits up to `request_timeout` and then raises `LiteAvatarError`.
- **Versions.** The agent side needs `livekit-agents` 1.8.x. The worker overrides a private method of LiveKit's
  `AvatarRunner` to publish a single layer, and the stream wrapper calls LiteAvatar internals, so both are pinned:
  LiteAvatar to a tested commit, LiveKit to 1.8.x. The renderer is tested on Python 3.10 on macOS (arm64).
- **Linux and x86.** The setup script installs CPU-only torch on Linux, but neither Linux nor x86 has been tested.
- **The renderer process changes into LiteAvatar's folder while importing it,** because LiteAvatar loads its
  weights from relative paths. Run it as its own process (as `serve` does), not inside your agent.

## Tests

```bash
.venv/bin/pytest                          # agent side and server, plus the offline render if set up
.venv/bin/pytest -s tests/test_offline.py # prints the real-time factor
```

`tests/test_offline.py` renders a WAV (LiteAvatar's sample, or `LITEAVATAR_TEST_WAV`) to an MP4 and fails if the
frame count doesn't match the audio or the real-time factor is 1 or more. It is skipped until
`liteavatar-renderer setup` has run.

## Licences

This package's code is licensed under the Apache License 2.0 (see `LICENSE`).

It downloads, but does not include, LiteAvatar's parts. Their licences differ:

| Part | Where it comes from | Licence |
|---|---|---|
| LiteAvatar code | GitHub [HumanAIGC/lite-avatar](https://github.com/HumanAIGC/lite-avatar) | MIT |
| Paraformer speech model (`model.pb`, `lm.pb`) | ModelScope [iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch](https://modelscope.cn/models/iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch) (fetched from LiteAvatarGallery) | Apache-2.0 |
| Face weights `model_1.onnx` and every gallery presenter | ModelScope [HumanAIGC-Engineering/LiteAvatarGallery](https://modelscope.cn/models/HumanAIGC-Engineering/LiteAvatarGallery) | **none stated** |

No licence means no permission by default. A public question asking whether the weights are covered by the MIT
licence ([lite-avatar issue #27](https://github.com/HumanAIGC/lite-avatar/issues/27), June 2026) had no answer as of
November 2026. Check upstream, or get written permission from Alibaba, before using `model_1.onnx` or the presenters
commercially. The `sample` presenter ships inside the MIT-licensed GitHub repository, but whether the MIT licence is
meant to cover it isn't stated either; treat it the same way until upstream says so. This is not legal advice.

`src/livekit_liteavatar/renderer/mouth_pcs.npy` (four 32-value vectors used by `LITEAVATAR_OPEN_GAIN`) was computed
by this project from audio2mouth's output on test speech; it contains no LiteAvatar weights.

## Acknowledgements

[LiteAvatar](https://github.com/HumanAIGC/lite-avatar) by Alibaba's HumanAIGC team, which builds on
[Paraformer](https://modelscope.cn/models/iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch)
and [FunASR](https://github.com/modelscope/FunASR); and [LiveKit Agents](https://github.com/livekit/agents), whose
avatar data-stream and A/V sync classes this builds on.
