// Speech-linked expression, live: the brows lift on stressed syllables and the head gives a small nod on the
// strongest. The rules are the approved offline ones, made causal so they work on a stream:
//   - loudness every 40 ms, smoothed over 200 ms (5 hops, centred);
//   - a stress is a local peak at least 4 dB above the median of the last 1.5 s of speech, louder than -40 dBFS;
//   - at most one per 0.9 s; a nod only at 5 dB or more;
//   - the raise is scaled by how prominent the peak is (0.4 to 1).
// The smoothing needs 80 ms of audio after a peak, and the voice is heard `delayMs` (150 ms) after it is captured, so
// a stress is found at about the moment it is heard. The raise starts then: rise 120 ms, hold 150 ms, fall 450 ms.
// Times are in "heard" audio-context ms, the same clock as the lip-sync.
const HOP_S = 0.04;
const SPEECH_DB = -45;
const STRESS_DB = 4;
const NOD_DB = 5;
const MIN_GAP_MS = 900;
const WINDOW_HOPS = 38; // 1.5 s of history for the median
const KEEP_HOPS = 64;

const smooth = (u) => {
  const x = Math.min(1, Math.max(0, u));
  return x * x * (3 - 2 * x);
};

export class Expression {
  constructor() {
    this.sr = 0;
    this.reset(0);
  }

  reset(timeS) {
    this.hopStart = timeS;
    this.expected = timeS;
    this.filled = 0;
    this.sum = 0;
    this.db = [];
    this.events = [];
    this.lastEventMs = -Infinity;
  }

  /** Captured audio: a block of `samples` at `sampleRate`, starting at audio-context time `timeS`. */
  push(timeS, samples, sampleRate) {
    if (sampleRate !== this.sr) {
      this.sr = sampleRate;
      this.hopN = Math.round(sampleRate * HOP_S);
      this.reset(timeS);
    } else if (Math.abs(timeS - this.expected) > 0.02) {
      this.reset(timeS); // a gap in the stream: start again
    }
    for (let i = 0; i < samples.length; i += 1) {
      this.sum += samples[i] * samples[i];
      this.filled += 1;
      if (this.filled === this.hopN) {
        const nowMs = (timeS + (i + 1) / sampleRate) * 1000;
        this.hop(10 * Math.log10(this.sum / this.hopN + 1e-10), nowMs);
        this.hopStart += HOP_S;
        this.filled = 0;
        this.sum = 0;
      }
    }
    this.expected = timeS + samples.length / sampleRate;
  }

  env(k) {
    let s = 0;
    let c = 0;
    for (let j = k - 2; j <= k + 2; j += 1) {
      if (j >= 0 && j < this.db.length) {
        s += this.db[j];
        c += 1;
      }
    }
    return s / c;
  }

  /** One 40 ms hop's loudness, completed at `nowMs`; looks for a stress 3 hops back (its smoothing is complete). */
  hop(db, nowMs) {
    this.db.push(db);
    if (this.db.length > KEEP_HOPS) this.db.shift();
    const k = this.db.length - 4;
    if (k < 1 || nowMs - this.lastEventMs < MIN_GAP_MS) return;
    const e = this.env(k);
    if (e < this.env(k - 1) || e < this.env(k + 1) || this.db[k] <= -40) return;
    const speech = [];
    for (let j = Math.max(0, k - WINDOW_HOPS + 1); j <= k; j += 1) if (this.db[j] > SPEECH_DB) speech.push(this.env(j));
    if (!speech.length) return;
    speech.sort((a, b) => a - b);
    const prominence = e - speech[Math.floor(speech.length / 2)];
    if (prominence < STRESS_DB) return;
    this.lastEventMs = nowMs;
    const amount = Math.min(1, Math.max(0.4, (prominence - STRESS_DB) / 4 + 0.4));
    this.events.push({ ms: nowMs, amount, nod: prominence >= NOD_DB });
    if (this.events.length > 4) this.events.shift();
  }

  /** Brow lift and nod (0..1) at heard time `ms`. */
  at(ms) {
    let brow = 0;
    let nod = 0;
    for (const ev of this.events) {
      const t = (ms - ev.ms) / 1000;
      if (t < 0 || t > 0.72) continue;
      const shape = t < 0.12 ? smooth(t / 0.12) : t < 0.27 ? 1 : 1 - smooth((t - 0.27) / 0.45);
      brow = Math.max(brow, ev.amount * shape);
      if (ev.nod && t < 0.55) nod = Math.max(nod, t < 0.17 ? smooth(t / 0.17) : 1 - smooth((t - 0.17) / 0.38));
    }
    return { brow, nod };
  }
}
