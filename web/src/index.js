// livekit-lite-avatar, 3D style: a talking face drawn in the browser from the agent's voice. No renderer, no video
// track and nothing to change in the agent: the page receives the agent's audio as usual and the face lip-syncs to it.
//
//   const avatar = await createAvatar3D(canvas);
//   avatar.follow(room);              // a livekit-client Room: lip-syncs the agent's voice when it arrives
//   startButton.onclick = () => avatar.resumeAudio();   // browsers play sound only after a user gesture
//
// The avatar plays the agent's voice itself (150 ms late, so the mouth can move into each sound): don't also attach
// that track elsewhere (track.attach(), <RoomAudioRenderer />), or it plays twice.
import { Expression } from "./expression.js";
import { NORA_URL, createFace } from "./face.js";
import { LipSync, MODEL_URL, MOUTH_MORPHS } from "./lipsync.js";

export { MODEL_URL, MOUTH_MORPHS, NORA_URL };

/** Whether a livekit-client participant is an agent. */
function isAgent(participant) {
  return participant?.isAgent === true || participant?.kind === 4 || /^agent$/i.test(String(participant?.kind ?? ""));
}

export class Avatar3D {
  constructor(face, options) {
    const { delayMs = 150, volume = 1, maxFps = 30, onFrame = null, modelUrl = MODEL_URL } = options;
    this.face = face;
    this.canvas = face.canvas;
    this.expression = new Expression();
    this.lipsync = new LipSync({
      modelUrl,
      delayMs,
      volume,
      onAudio: (t, samples, sr) => this.expression.push(t, samples, sr),
    });
    this.onFrame = onFrame;
    this.minFrameMs = 1000 / maxFps - 2; // a little under, so a 60 Hz display gives every other frame at 30 fps
    this.speaking = false;
    this.unfollow = null;
    this.stopped = false;
    this.timing = { frames: 0, drawMs: 0, mouthMs: 0, since: performance.now() };
    this.started = performance.now();
    this.lastFrame = 0;
    this.lastDraw = this.started;
    this.loop = this.loop.bind(this);
    requestAnimationFrame(this.loop);
  }

  loop(now) {
    if (this.stopped) return;
    requestAnimationFrame(this.loop);
    if (now - this.lastFrame < this.minFrameMs) return;
    // rAF's timestamp is the frame's start, which can be a little before this object was made: never step backwards
    const dt = Math.min(100, Math.max(0, now - this.lastDraw));
    this.lastFrame = now;
    this.lastDraw = now;
    const t0 = performance.now();
    const { values, speaking, heardMs } = this.lipsync.frame(dt);
    const expression = this.expression.at(heardMs);
    const t1 = performance.now();
    this.face.frame(values, (now - this.started) / 1000, expression);
    const t2 = performance.now();
    this.speaking = speaking;
    const s = this.timing;
    s.frames += 1;
    s.mouthMs += t1 - t0;
    s.drawMs += t2 - t1;
    this.onFrame?.({ values: { ...values }, speaking, expression, heardMs, now });
  }

  /** Lip-sync and play a track: a livekit-client RemoteAudioTrack, a MediaStreamTrack or a MediaStream. */
  attach(track) {
    return this.lipsync.attach(track);
  }

  /** Stop playing and lip-syncing the attached track. */
  detach() {
    this.lipsync.detach();
  }

  /** The attached track, if any. */
  get track() {
    return this.lipsync.track;
  }

  /**
   * Follow a livekit-client Room: attach the agent's voice when it is subscribed (and detach it when it goes).
   * `identity` picks a participant; by default, the agent. Returns a function that stops following.
   */
  follow(room, { identity = null } = {}) {
    this.unfollow?.();
    const wanted = (track, participant) =>
      track?.kind === "audio" && (identity ? participant?.identity === identity : isAgent(participant));
    const subscribed = (track, _publication, participant) => {
      if (wanted(track, participant)) this.attach(track).catch((e) => console.error(e));
    };
    const unsubscribed = (track) => {
      if (track === this.track) this.detach();
    };
    room.on("trackSubscribed", subscribed);
    room.on("trackUnsubscribed", unsubscribed);
    for (const participant of room.remoteParticipants.values()) {
      for (const publication of participant.trackPublications.values()) {
        if (publication.track) subscribed(publication.track, publication, participant);
      }
    }
    this.unfollow = () => {
      room.off("trackSubscribed", subscribed);
      room.off("trackUnsubscribed", unsubscribed);
      this.unfollow = null;
    };
    return this.unfollow;
  }

  /** Allow sound. Call it from a click (the one that joins the room, say): browsers block audio until a gesture. */
  resumeAudio() {
    return this.lipsync.resume();
  }

  setVolume(volume) {
    this.lipsync.setVolume(volume);
  }

  /** Change the drawing size (CSS pixels). */
  resize(width, height) {
    this.face.resize(width, height);
  }

  /** Frames drawn per second and the time each took (ms), since the last call; and the voice's loudness now. */
  stats() {
    const s = this.timing;
    const now = performance.now();
    const n = Math.max(1, s.frames);
    const out = {
      fps: +((s.frames * 1000) / Math.max(1, now - s.since)).toFixed(1),
      drawMs: +(s.drawMs / n).toFixed(2),
      mouthMs: +(s.mouthMs / n).toFixed(3),
      speaking: this.speaking,
      outputDb: +this.lipsync.outputDb().toFixed(1),
      audio: this.lipsync.context?.state ?? "not started",
    };
    this.timing = { frames: 0, drawMs: 0, mouthMs: 0, since: now };
    return out;
  }

  destroy() {
    this.stopped = true;
    this.unfollow?.();
    this.lipsync.close();
    this.face.dispose();
  }
}

/**
 * Load the 3D avatar into a <canvas>. Resolves once the face is on screen (it rests, blinks and looks around until a
 * voice is attached).
 *
 * Options: width, height (drawing size in CSS pixels, default 512 x 640); quality ("high", or "low" for slow
 * devices: no ambient occlusion or supersampling); maxFps (30); delayMs (150: how late the voice plays, so the mouth
 * can anticipate it); volume (1); top (the T-shirt colour); background ("gradient" or a CSS colour); smile, browLift,
 * nod (expression strength); faceUrl and modelUrl (to self-host the face and the lip-sync model elsewhere); onFrame.
 */
export async function createAvatar3D(canvas, options = {}) {
  const { faceUrl = NORA_URL, ...rest } = options;
  const face = await createFace(canvas, { url: faceUrl, ...rest });
  return new Avatar3D(face, rest);
}
