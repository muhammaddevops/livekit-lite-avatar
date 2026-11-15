// Lip-sync from a live audio track (the agent's voice), with talking-avatar 0.3.0's model, viseme detector,
// coarticulation and mouth mapping (MIT, ../vendor/talking-avatar). This is the wiring of its
// TalkingAvatar.attachStream(), driving our face instead of the package's own renderer:
//
//   remote track ─> capture worklet (1024-sample blocks) ─> VisemeDetector ─> viseme events, in "heard" time
//               └─> delay (150 ms) ─> volume ─> speakers
//   every frame: mouthAt(buildSegments(events), heard now) ─> the package's applyMouth (slew limits) ─> morph values
//
// The voice plays `delayMs` late, so the model hears each sound before the listener does and the mouth can move into
// it in time. It plays here, so nothing else on the page may play the same track.
import {
  AvatarRenderer,
  VisemeDetector,
  buildSegments,
  isSpeakingAt,
  mouthAt,
  parseVisemeModel,
} from "../vendor/talking-avatar/talking-avatar-core.js";

/** talking-avatar's lip-sync model (1 MB), next to this module. */
export const MODEL_URL = new URL("../vendor/talking-avatar/viseme-model.bin", import.meta.url).href;

const VISEMES = ["sil", "PP", "FF", "TH", "DD", "kk", "CH", "SS", "nn", "RR", "aa", "E", "I", "O", "U"];
/** The morph targets the mouth writes: Oculus visemes and the ARKit mouth shapes talking-avatar also drives. */
export const MOUTH_MORPHS = [
  ...VISEMES.map((v) => `viseme_${v}`),
  "jawOpen",
  "mouthFunnel",
  "mouthPucker",
  "mouthRollLower",
  "mouthClose",
  "mouthPressLeft",
  "mouthPressRight",
];
const HISTORY_MS = 5000; // viseme events kept behind the playhead (coarticulation looks back up to ~250 ms)
const STREAM_GAP_S = 0.02; // a jump this big in captured audio time restarts the viseme timeline

// Hands the stream's audio from the audio thread to the main thread, in 1024-sample blocks stamped with context time
// (talking-avatar's own capture worklet).
const CAPTURE = "livekit-lite-avatar-capture";
const CAPTURE_WORKLET = `
class Capture extends AudioWorkletProcessor {
  constructor() { super(); this.block = new Float32Array(1024); this.filled = 0; this.time = 0; }
  send() {
    this.port.postMessage({ time: this.time, samples: this.block.slice(0, this.filled) });
    this.filled = 0;
  }
  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    if (this.filled + input.length > this.block.length) this.send();
    if (this.filled === 0) this.time = currentTime;
    this.block.set(input, this.filled);
    this.filled += input.length;
    if (this.filled === this.block.length) this.send();
    return true;
  }
}
registerProcessor("${CAPTURE}", Capture);
`;

const models = new Map();
function loadModel(url) {
  let model = models.get(url);
  if (!model) {
    model = fetch(url)
      .then((res) => {
        if (!res.ok) throw new Error(`livekit-lite-avatar 3D: lip-sync model ${url}: HTTP ${res.status}`);
        return res.arrayBuffer();
      })
      .then(parseVisemeModel);
    model.catch(() => models.delete(url)); // let a later call retry
    models.set(url, model);
  }
  return model;
}

/** talking-avatar's AvatarRenderer.applyMouth (viseme gains, rounding, lip press, jaw, slew limits) on a recorder. */
function makeMouth() {
  const mesh = {
    morphTargetDictionary: Object.fromEntries(MOUTH_MORPHS.map((n, i) => [n, i])),
    morphTargetInfluences: new Float32Array(MOUTH_MORPHS.length),
  };
  const self = { faceMeshes: [mesh], morphMap: {} };
  self.targets = AvatarRenderer.prototype.targets.bind(self);
  const apply = AvatarRenderer.prototype.applyMouth;
  const values = {};
  return {
    step(weights, dtMs) {
      apply.call(self, weights, dtMs);
      MOUTH_MORPHS.forEach((n, i) => {
        values[n] = mesh.morphTargetInfluences[i];
      });
      return values;
    },
  };
}

/** The MediaStreamTrack of a livekit-client track, a MediaStreamTrack or a MediaStream. */
function mediaTrack(track) {
  if (!track) return null;
  if (track.mediaStreamTrack) return track.mediaStreamTrack; // livekit-client RemoteAudioTrack
  if (typeof MediaStream !== "undefined" && track instanceof MediaStream) return track.getAudioTracks()[0] ?? null;
  return track;
}

export class LipSync {
  constructor({ modelUrl = MODEL_URL, delayMs = 150, volume = 1, onAudio = null } = {}) {
    this.modelUrl = modelUrl;
    this.delayS = delayMs / 1000;
    this.volume = volume;
    this.onAudio = onAudio; // (contextTimeS, samples, sampleRate) for each captured block
    this.context = null;
    this.contextReady = null;
    this.stream = null;
    this.events = [];
    this.segments = null;
    this.mouth = makeMouth();
    this.token = 0;
    this.closed = false;
    loadModel(modelUrl).catch(() => {}); // warm up; errors are reported by attach()
  }

  /** The audio context, created on first use (with the capture worklet, the output gain and a level meter). */
  async audioContext() {
    this.contextReady ??= (async () => {
      const ctx = new AudioContext({ latencyHint: "interactive" });
      const url = URL.createObjectURL(new Blob([CAPTURE_WORKLET], { type: "text/javascript" }));
      try {
        await ctx.audioWorklet.addModule(url);
      } finally {
        URL.revokeObjectURL(url);
      }
      this.gain = ctx.createGain();
      this.gain.gain.value = this.volume;
      this.gain.connect(ctx.destination);
      this.meter = ctx.createAnalyser();
      this.meter.fftSize = 1024;
      this.level = new Float32Array(this.meter.fftSize);
      this.context = ctx;
      return ctx;
    })();
    return this.contextReady;
  }

  /** Allow sound: call from a click or other user gesture if the browser blocked it. */
  async resume() {
    const ctx = await this.audioContext();
    if (ctx.state !== "running") await ctx.resume();
  }

  /** Play `track` `delayMs` late and lip-sync to it. Replaces any track attached before. */
  async attach(track) {
    const mst = mediaTrack(track);
    if (!mst || mst.kind !== "audio") throw new Error("livekit-lite-avatar 3D: attach() needs an audio track");
    this.detach();
    const token = ++this.token;
    const [ctx, model] = await Promise.all([this.audioContext(), loadModel(this.modelUrl)]);
    if (token !== this.token || this.closed) return;
    if (ctx.state === "suspended") ctx.resume().catch(() => {}); // resolves only once the page has had a gesture

    const stream = new MediaStream([mst]);
    // Chrome only feeds a remote (WebRTC) stream into Web Audio while a media element is playing it
    const element = new Audio();
    element.muted = true;
    element.srcObject = stream;
    element.play().catch(() => {});
    const source = ctx.createMediaStreamSource(stream);
    const delay = ctx.createDelay(Math.max(1, this.delayS * 2));
    delay.delayTime.value = this.delayS;
    source.connect(delay);
    delay.connect(this.gain);
    delay.connect(this.meter);
    const capture = new AudioWorkletNode(ctx, CAPTURE, {
      numberOfOutputs: 0,
      channelCount: 1,
      channelCountMode: "explicit",
      channelInterpretation: "speakers",
    });
    source.connect(capture);

    const attached = { track, source, delay, capture, element, detector: null, expectedTime: 0 };
    capture.port.onmessage = ({ data }) => {
      if (this.stream !== attached) return;
      // (re)start the timeline when capture begins or after a gap (the context was suspended, say)
      if (!attached.detector || Math.abs(data.time - attached.expectedTime) > STREAM_GAP_S) {
        attached.detector?.flush();
        const heardAtMs = (data.time + this.delayS) * 1000;
        attached.detector = new VisemeDetector(model, ctx.sampleRate, heardAtMs, (event) => this.addEvent(event));
      }
      attached.detector.push(data.samples);
      attached.expectedTime = data.time + data.samples.length / ctx.sampleRate;
      this.onAudio?.(data.time, data.samples, ctx.sampleRate);
    };
    this.stream = attached;
  }

  /** Stop playing and lip-syncing the attached track. */
  detach() {
    this.token += 1;
    const attached = this.stream;
    if (!attached) return;
    this.stream = null;
    attached.capture.port.onmessage = null;
    attached.source.disconnect();
    attached.delay.disconnect();
    attached.capture.disconnect();
    attached.element.srcObject = null;
    this.events = [];
    this.segments = null;
  }

  get track() {
    return this.stream?.track ?? null;
  }

  setVolume(volume) {
    this.volume = volume;
    if (this.gain) this.gain.gain.value = volume;
  }

  addEvent(event) {
    const { events } = this;
    let i = events.length;
    while (i > 0 && events[i - 1].ms > event.ms) i -= 1;
    events.splice(i, 0, event);
    const cutoff = this.heardMs() - HISTORY_MS; // drop history well behind the playhead, keeping the one in effect
    let drop = 0;
    while (drop + 1 < events.length && events[drop + 1].ms < cutoff) drop += 1;
    if (drop) events.splice(0, drop);
    this.segments = null;
  }

  /** The audio-context time (ms) coming out of the speakers now. */
  heardMs() {
    const ctx = this.context;
    if (!ctx) return 0;
    const stamp = ctx.state === "running" ? ctx.getOutputTimestamp?.() : undefined;
    if (stamp?.contextTime && stamp.performanceTime) return stamp.contextTime * 1000 + (performance.now() - stamp.performanceTime);
    return (ctx.currentTime - (ctx.outputLatency || 0)) * 1000;
  }

  /** The mouth now: morph values after `dtMs` of movement, whether the voice is sounding, and the heard time. */
  frame(dtMs) {
    const now = this.heardMs();
    let weights = {};
    let speaking = false;
    if (this.events.length) {
      this.segments ??= buildSegments(this.events);
      weights = mouthAt(this.segments, now);
      speaking = isSpeakingAt(this.events, now);
    }
    return { values: this.mouth.step(weights, dtMs), speaking, heardMs: now };
  }

  /** Loudness of what is being played now, in dBFS (-100 when silent). */
  outputDb() {
    if (!this.meter || !this.stream) return -100;
    this.meter.getFloatTimeDomainData(this.level);
    let s = 0;
    for (const v of this.level) s += v * v;
    return Math.max(-100, 10 * Math.log10(s / this.level.length + 1e-10));
  }

  close() {
    this.closed = true;
    this.detach();
    this.context?.close().catch(() => {});
  }
}
