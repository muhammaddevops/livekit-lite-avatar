import * as p from "three";
import { GLTFLoader as D } from "three/examples/jsm/loaders/GLTFLoader.js";
import { MeshoptDecoder as N } from "three/examples/jsm/libs/meshopt_decoder.module.js";
import { RoomEnvironment as B } from "three/examples/jsm/environments/RoomEnvironment.js";
import { RectAreaLightUniformsLib as V } from "three/examples/jsm/lights/RectAreaLightUniformsLib.js";
const W = [
  "sil",
  "aa",
  "aa",
  "O",
  "E",
  "E",
  "I",
  "U",
  "O",
  "aa",
  "O",
  "aa",
  "kk",
  "RR",
  "nn",
  "SS",
  "CH",
  "TH",
  "FF",
  "DD",
  "kk",
  "PP"
], j = /* @__PURE__ */ new Set([12, 14, 19, 20]), $ = /* @__PURE__ */ new Set([7, 8, 10, 16]), F = /* @__PURE__ */ new Set(["PP", "FF"]), G = 80, C = 95, z = 0.7, O = 250, K = 0.25, Y = 0.78, J = 0.9;
function X(a) {
  const e = a.map(({ id: n, ms: o }) => ({
    viseme: W[n] ?? "sil",
    ms: o,
    tongue: j.has(n),
    lipHeavy: $.has(n),
    level: 1
  })), t = (n) => e[n].viseme === "sil" || F.has(e[n].viseme);
  e.forEach((n, o) => {
    if (!n.tongue) return;
    let r = null;
    for (let i = o + 1; i < e.length && e[i].ms - n.ms <= O && !t(i); i += 1)
      if (!e[i].tongue) {
        r = i;
        break;
      }
    for (let i = o - 1; r === null && i >= 0 && n.ms - e[i].ms <= O && !t(i); i -= 1)
      e[i].tongue || (r = i);
    r !== null && (n.viseme = e[r].viseme, n.lipHeavy = e[r].lipHeavy, n.level = K);
  });
  const s = [];
  return e.forEach((n, o) => {
    const r = e[o + 1]?.ms ?? Number.POSITIVE_INFINITY, i = s[s.length - 1];
    i && i.viseme === n.viseme && i.level === n.level ? (i.end = r, i.lipHeavy ||= n.lipHeavy) : s.push({ viseme: n.viseme, start: n.ms, end: r, lipHeavy: n.lipHeavy, level: n.level });
  }), s;
}
const T = (a) => a <= 0 ? 0 : a >= 1 ? 1 : a * a * (3 - 2 * a);
function Z(a, e) {
  const t = a.lipHeavy ? C : G, s = a.start + z * (a.end - a.start);
  return e < a.start ? T((e - (a.start - t)) / t) : e <= s ? 1 : T(1 - (e - s) / t);
}
function q(a, e) {
  const t = {};
  let s = 0, n = 0;
  const o = [];
  for (const i of a) {
    if (i.start - C > e) break;
    const c = Z(i, e);
    c <= 0 || (F.has(i.viseme) ? (t[i.viseme] = Math.max(t[i.viseme] ?? 0, c * J), n = Math.max(n, c)) : (o.push([i, c]), s += c));
  }
  const r = (1 - n) / Math.max(1, s);
  for (const [i, c] of o)
    i.viseme !== "sil" && (t[i.viseme] = (t[i.viseme] ?? 0) + c * i.level * Y * r);
  return t;
}
const Q = 400;
function ee(a, e) {
  for (let t = a.length - 1; t >= 0; t -= 1)
    if (!(a[t].ms > e))
      return a[t].id !== 0 ? !0 : t > 0 && e - a[t].ms < Q;
  return !1;
}
const M = (...a) => a.map((e) => `h_expressions.${e}_h`), te = {
  viseme_sil: [],
  viseme_PP: M("MPB_Up", "MPB_Down"),
  viseme_FF: M("FV"),
  viseme_TH: M("TD_I"),
  viseme_DD: M("TD_I"),
  viseme_kk: M("KG"),
  viseme_CH: M("SH_CH"),
  viseme_SS: M("S"),
  viseme_nn: M("TD_I"),
  viseme_RR: M("UH_OO"),
  viseme_aa: M("AE_AA"),
  viseme_E: M("Ax_E"),
  viseme_I: M("TD_I"),
  viseme_O: M("AO_a"),
  viseme_U: M("UW_U"),
  eyeBlinkLeft: M("LeyeClose"),
  eyeBlinkRight: M("ReyeClose"),
  browInnerUp: M("LbrowUp", "RbrowUp"),
  jawOpen: M("MouthOpen"),
  mouthPucker: M("Kiss"),
  mouthFunnel: [],
  mouthRollLower: [],
  mouthClose: [],
  mouthPressLeft: [],
  mouthPressRight: []
}, se = {
  aa: 0.52,
  E: 0.95,
  I: 0.95,
  O: 0.72,
  U: 1,
  PP: 1.1,
  FF: 1.1,
  TH: 0.75,
  DD: 0.7,
  kk: 0.7,
  nn: 0.7
}, ne = {
  mouthPucker: { U: 1.2, O: 0.77, RR: 0.83, CH: 0.58 },
  mouthFunnel: { U: 0.9, O: 1.15, RR: 0.64, CH: 1.09 },
  mouthRollLower: { U: 0.26 },
  mouthClose: { U: 0.26 }
}, ie = { mouthPressLeft: 0.35, mouthPressRight: 0.35 }, oe = 0.1, re = 160, ae = 80, he = /* @__PURE__ */ new Set(["viseme_PP", "viseme_FF", "mouthPressLeft", "mouthPressRight"]), ce = ["aa", "E", "I", "O", "U", "PP", "SS", "TH", "DD", "FF", "kk", "nn", "RR", "CH", "sil"], le = { speaking: [1.8, 4.5], listening: [2.5, 6], thinking: [1.5, 4] }, P = 0.2, x = { speaking: 0, listening: 0.05, thinking: 0.15 }, me = 700, fe = 45, de = (a, e) => a + Math.random() * (e - a), U = 0.42, pe = 0.3, ue = 0.52;
function ge(a, e, t) {
  const s = new p.Box3(), n = new p.Vector3();
  for (const c of e)
    for (const m of ["eyeBlinkLeft", "eyeBlinkRight", "jawOpen"].flatMap(t)) {
      const l = c.geometry.morphAttributes.position?.[c.morphTargetDictionary?.[m] ?? -1];
      if (l)
        for (let h = 0; h < l.count; h += 1)
          Math.abs(l.getX(h)) + Math.abs(l.getY(h)) + Math.abs(l.getZ(h)) < 1e-4 || s.expandByPoint(c.localToWorld(c.getVertexPosition(h, n)));
    }
  if (!s.isEmpty()) return s.getCenter(new p.Vector3());
  const o = a.getObjectByName("Head");
  if (o) return o.getWorldPosition(new p.Vector3()).add(new p.Vector3(0, 0.05, 0));
  const r = new p.Box3().setFromObject(a), i = r.getCenter(new p.Vector3());
  return new p.Vector3(i.x, r.max.y - U * 0.4, i.z);
}
function Me(a, e) {
  if (!(e instanceof p.MeshStandardMaterial) || e.roughnessMap || !/^[fm]\d+_(head|body)$/.test(e.name)) return e;
  const t = new p.MeshPhysicalMaterial();
  if (e instanceof p.MeshPhysicalMaterial ? t.copy(e) : p.MeshStandardMaterial.prototype.copy.call(t, e), t.metalness = 0, t.roughness = 0.82, t.specularIntensity = 0.45, e.name.endsWith("_head")) {
    const s = a, n = a.geometry.getAttribute("skinIndex"), o = a.geometry.getAttribute("skinWeight"), r = new Set(s.skeleton?.bones.flatMap((h, d) => /eye$/i.test(h.name) ? [d] : []) ?? []);
    let i = 0;
    if (n && o) {
      for (let h = 0; h < n.count; h += 1)
        for (let d = 0; d < 4; d += 1)
          if (r.has(n.getComponent(h, d)) && o.getComponent(h, d) > 0.5) {
            i += 1;
            break;
          }
    }
    const c = !!n && i > n.count / 2, m = a.geometry.morphAttributes.position?.[a.morphTargetDictionary?.eyeBlinkLeft ?? -1];
    let l = !1;
    if (m)
      for (let h = 0; h < m.count && !l; h += 1)
        l = Math.abs(m.getX(h)) + Math.abs(m.getY(h)) + Math.abs(m.getZ(h)) > 1e-4;
    t.roughness = c ? 0.22 : l ? 0.62 : 0.72, t.specularIntensity = c ? 0.6 : 0.35, t.clearcoat = c ? 0.25 : 0, t.clearcoatRoughness = 0.12, c && t.normalScale.multiplyScalar(0.2);
  }
  return t;
}
function S(a) {
  const e = /* @__PURE__ */ new Set();
  a.traverse((t) => {
    const s = t;
    if (s.isMesh) {
      e.add(s.geometry);
      for (const n of Array.isArray(s.material) ? s.material : [s.material]) {
        e.add(n);
        for (const o of Object.values(n)) o instanceof p.Texture && e.add(o);
      }
    }
  });
  for (const t of e) t.dispose();
}
class ye {
  constructor(e, t) {
    this.container = e, this.getFrame = t, this.renderer = new p.WebGLRenderer({ antialias: !0, alpha: !0 }), this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2)), this.renderer.toneMapping = p.ACESFilmicToneMapping, this.renderer.toneMappingExposure = 0.95, this.renderer.outputColorSpace = p.SRGBColorSpace, Object.assign(this.renderer.domElement.style, { display: "block", width: "100%", height: "100%" }), e.appendChild(this.renderer.domElement), this.pmrem = new p.PMREMGenerator(this.renderer), this.scene.environment = this.pmrem.fromScene(new B(), 0.04).texture, this.scene.environmentIntensity = 0.35, V.init(), this.scene.add(this.keyLight, this.fillLight), this.resizeObserver = new ResizeObserver(() => this.resize()), this.resizeObserver.observe(e), this.resize(), this.frame = requestAnimationFrame(this.tick);
  }
  renderer;
  scene = new p.Scene();
  camera = new p.PerspectiveCamera(22, 1, 0.1, 10);
  pmrem;
  keyLight = new p.RectAreaLight(16774120, 4, 0.8, 1.1);
  fillLight = new p.RectAreaLight(15069183, 1, 1.2, 1.5);
  resizeObserver;
  clock = new p.Clock();
  model = null;
  faceMeshes = [];
  face = null;
  morphMap = {};
  frame = 0;
  loadId = 0;
  disposed = !1;
  blinkAt = -1;
  nextBlinkAt = 2;
  brow = x.listening;
  /** Load (or replace) the avatar. Resolves when it is on screen. */
  load(e, t = {}) {
    const s = ++this.loadId;
    return new Promise((n, o) => {
      new D().setMeshoptDecoder(N).load(e, (r) => {
        if (this.disposed || s !== this.loadId) {
          S(r.scene), n();
          return;
        }
        this.model && (this.scene.remove(this.model), S(this.model)), this.model = r.scene, this.scene.add(r.scene), this.faceMeshes = [];
        const i = /* @__PURE__ */ new Set(), c = Math.min(8, this.renderer.capabilities.getMaxAnisotropy());
        r.scene.traverse((l) => {
          const h = l;
          if (!h.isMesh) return;
          h.morphTargetDictionary && this.faceMeshes.push(h);
          const d = (g) => {
            const f = Me(h, g);
            return f !== g && i.add(g), f;
          };
          h.material = Array.isArray(h.material) ? h.material.map(d) : d(h.material);
          for (const g of Array.isArray(h.material) ? h.material : [h.material])
            if (g instanceof p.MeshStandardMaterial)
              for (const f of [g.map, g.normalMap, g.roughnessMap])
                f && (f.anisotropy = c);
        });
        for (const l of i) l.dispose();
        const m = (l) => this.faceMeshes.some((h) => h.morphTargetDictionary?.[l] !== void 0);
        this.morphMap = { ...m("h_expressions.AE_AA_h") ? te : {}, ...t }, this.targets("viseme_aa").some(m) || console.warn(`talking-avatar: ${e} has no Oculus viseme morph targets (viseme_aa, viseme_PP, …); the mouth won't move`), r.scene.updateMatrixWorld(!0), this.face = ge(r.scene, this.faceMeshes, (l) => this.targets(l)), this.keyLight.position.copy(this.face).add(new p.Vector3(-0.8, 0.5, 1.2)), this.keyLight.lookAt(this.face), this.fillLight.position.copy(this.face).add(new p.Vector3(0.9, 0.15, 1)), this.fillLight.lookAt(this.face), this.frameFace(), n();
      }, void 0, (r) => {
        const i = r instanceof Error ? r.message : String(r);
        o(new Error(`talking-avatar: couldn't load avatar ${e} (${i}). If the browser console shows a CORS error, the avatar's server must send Access-Control-Allow-Origin for this page's origin.`));
      });
    });
  }
  dispose() {
    this.disposed = !0, cancelAnimationFrame(this.frame), this.resizeObserver.disconnect(), this.model && S(this.model), this.scene.environment?.dispose(), this.pmrem.dispose(), this.renderer.dispose(), this.renderer.domElement.remove();
  }
  tick = () => {
    const e = Math.min(this.clock.getDelta(), 0.05) * 1e3, { visemes: t, mode: s } = this.getFrame();
    this.applyMouth(t, e), this.applyBlinkAndBrows(s, e), this.renderer.render(this.scene, this.camera), this.frame = requestAnimationFrame(this.tick);
  };
  /** The avatar's morph target name(s) for a standard name. */
  targets(e) {
    const t = this.morphMap[e];
    return t === void 0 ? [e] : Array.isArray(t) ? t : [t];
  }
  applyMouth(e, t) {
    const s = (o, r) => {
      const i = t / (he.has(o) ? ae : re);
      for (const c of this.targets(o))
        for (const m of this.faceMeshes) {
          const l = m.morphTargetDictionary?.[c], h = m.morphTargetInfluences;
          l === void 0 || !h || (h[l] += p.MathUtils.clamp(r - h[l], -i, i));
        }
    };
    for (const o of ce) s(`viseme_${o}`, (e[o] ?? 0) * (se[o] ?? 1));
    for (const [o, r] of Object.entries(ne))
      s(o, Math.min(1, Object.entries(r).reduce((i, [c, m]) => i + m * (e[c] ?? 0), 0)));
    for (const [o, r] of Object.entries(ie)) s(o, Math.min(1, r * (e.PP ?? 0)));
    const n = Math.max(e.aa ?? 0, e.E ?? 0, e.I ?? 0, e.O ?? 0, e.U ?? 0);
    s("jawOpen", oe * n * (1 - (e.PP ?? 0)));
  }
  applyBlinkAndBrows(e, t) {
    const s = this.clock.elapsedTime;
    s >= this.nextBlinkAt && (this.blinkAt = s, this.nextBlinkAt = s + P + de(...le[e]));
    const n = (s - this.blinkAt) / P, o = n >= 1 ? 0 : n < 0.35 ? n / 0.35 : 1 - (n - 0.35) / 0.65;
    this.setMorph("eyeBlinkLeft", o, 1), this.setMorph("eyeBlinkRight", o, 1), this.brow += (x[e] - this.brow) * (1 - Math.exp(-t / me)), this.setMorph("browInnerUp", this.brow, 1 - Math.exp(-t / fe));
  }
  setMorph(e, t, s) {
    for (const n of this.targets(e))
      for (const o of this.faceMeshes) {
        const r = o.morphTargetDictionary?.[n], i = o.morphTargetInfluences;
        r === void 0 || !i || (i[r] = p.MathUtils.lerp(i[r], t, s));
      }
  }
  frameFace() {
    if (!this.face) return;
    const { camera: e, face: t } = this, s = Math.max(U, pe / e.aspect), n = s / 2 / Math.tan(p.MathUtils.degToRad(e.fov / 2));
    e.position.set(t.x, t.y + 0.03, t.z + n), e.lookAt(t.x, t.y - (0.5 - ue) * s, t.z);
  }
  resize() {
    const { clientWidth: e, clientHeight: t } = this.container;
    !e || !t || (this.renderer.setSize(e, t, !1), this.camera.aspect = e / t, this.camera.updateProjectionMatrix(), this.frameFace());
  }
}
const we = 12, A = 128;
class ve {
  step;
  // input samples per output sample
  half;
  // taps per side, in input samples
  kernels;
  input;
  pos;
  // next output position, in `input` samples
  constructor(e, t) {
    this.step = e / t;
    const s = 0.9 * Math.min(e, t) / 2 / e;
    this.half = Math.ceil(we / (2 * s)), this.kernels = Array.from({ length: A }, (n, o) => {
      const r = o / A;
      return Float32Array.from({ length: 2 * this.half }, (i, c) => {
        const m = c - this.half + 1 - r, l = 2 * s * m, h = l === 0 ? 1 : Math.sin(Math.PI * l) / (Math.PI * l), d = 0.42 + 0.5 * Math.cos(Math.PI * m / this.half) + 0.08 * Math.cos(2 * Math.PI * m / this.half);
        return 2 * s * h * d;
      });
    }), this.input = new Float32Array(this.half - 1), this.pos = this.half - 1;
  }
  push(e) {
    const t = new Float32Array(this.input.length + e.length);
    t.set(this.input), t.set(e, this.input.length);
    const s = [], { half: n } = this;
    for (; ; ) {
      let r = Math.floor(this.pos), i = Math.round((this.pos - r) * A);
      if (i === A && (r += 1, i = 0), r + n >= t.length) break;
      const c = this.kernels[i];
      let m = 0;
      for (let l = 0; l < c.length; l += 1) m += c[l] * t[r - n + 1 + l];
      s.push(m), this.pos += this.step;
    }
    const o = Math.max(0, Math.floor(this.pos) - n + 1);
    return this.input = t.slice(o), this.pos -= o, Float32Array.from(s);
  }
}
function ke(a) {
  const e = new DataView(a).getUint32(0, !0), t = JSON.parse(new TextDecoder().decode(new Uint8Array(a, 4, e))), s = {};
  for (const { name: n, shape: o, offset: r } of t.tensors)
    s[n] = new Float32Array(a, 4 + e + r * 4, o.reduce((i, c) => i * c, 1));
  return { ...t, tensors: s };
}
function Ae({ SR: a, NFFT: e, N_MELS: t, FMIN: s, FMAX: n }) {
  const o = (h) => 2595 * Math.log10(1 + h / 700), r = (h) => 700 * (10 ** (h / 2595) - 1), i = (o(n) - o(s)) / (t + 1), c = Array.from({ length: t + 2 }, (h, d) => r(o(s) + i * d)), m = e / 2 + 1, l = new Float32Array(t * m);
  for (let h = 0; h < t; h += 1) {
    const [d, g, f] = [c[h], c[h + 1], c[h + 2]];
    for (let w = 0; w < m; w += 1) {
      const u = w * a / e;
      l[h * m + w] = Math.max(0, Math.min((u - d) / (g - d), (f - u) / (f - g)));
    }
  }
  return l;
}
function Se(a, e) {
  const t = a.length;
  for (let s = 1, n = 0; s < t; s += 1) {
    let o = t >> 1;
    for (; n & o; o >>= 1) n ^= o;
    n ^= o, s < n && ([a[s], a[n]] = [a[n], a[s]], [e[s], e[n]] = [e[n], e[s]]);
  }
  for (let s = 2; s <= t; s <<= 1) {
    const n = -2 * Math.PI / s, o = s / 2;
    for (let r = 0; r < t; r += s)
      for (let i = 0; i < o; i += 1) {
        const c = Math.cos(n * i), m = Math.sin(n * i), l = a[r + i + o] * c - e[r + i + o] * m, h = a[r + i + o] * m + e[r + i + o] * c;
        a[r + i + o] = a[r + i] - l, e[r + i + o] = e[r + i] - h, a[r + i] += l, e[r + i] += h;
      }
  }
}
function _(a, e, t, s, n, o, r) {
  for (let i = 0; i < s; i += 1) {
    let c = 0;
    const m = i * n * o + r;
    for (let l = 0; l < n; l += 1) c += e[m + l * o] * t[l];
    a[i] += c;
  }
}
class _e {
  constructor(e) {
    this.m = e;
    const { WIN: t, NFFT: s, N_MELS: n, channels: o, kernel: r, dilations: i, classes: c } = e;
    this.window = Float32Array.from({ length: t }, (m, l) => 0.5 - 0.5 * Math.cos(2 * Math.PI * l / t)), this.filters = Ae(e), this.frame = new Float32Array(t), this.re = new Float64Array(s), this.im = new Float64Array(s), this.mel = new Float32Array(n), this.history = i.map((m) => Array.from({ length: (r - 1) * m + 1 }, () => new Float32Array(o))), this.x = new Float32Array(o), this.y = new Float32Array(o), this.logits = new Float32Array(c);
  }
  window;
  filters;
  frame;
  // ring of the last WIN samples
  filled = 0;
  sinceHop = 0;
  re;
  im;
  mel;
  history;
  // per block: last (kernel-1)*dilation+1 inputs, zero-initialised
  x;
  y;
  logits;
  /** Feed 16 kHz samples; calls onFrame(visemeId) once per completed 10 ms frame. */
  push(e, t) {
    const { WIN: s, HOP: n } = this.m;
    for (const o of e)
      this.frame.copyWithin(0, 1), this.frame[s - 1] = o, this.filled = Math.min(s, this.filled + 1), this.sinceHop += 1, this.filled === s && this.sinceHop >= n && (this.sinceHop = 0, t(this.step()));
  }
  step() {
    const { NFFT: e, N_MELS: t, LOG_FLOOR: s, channels: n, kernel: o, dilations: r, classes: i, tensors: c } = this.m, { re: m, im: l, mel: h } = this;
    m.fill(0), l.fill(0);
    for (let u = 0; u < this.frame.length; u += 1) m[u] = this.frame[u] * this.window[u];
    Se(m, l);
    const d = e / 2 + 1;
    for (let u = 0; u < t; u += 1) {
      let k = 0;
      for (let y = 0; y < d; y += 1) k += this.filters[u * d + y] * (m[y] * m[y] + l[y] * l[y]);
      h[u] = (Math.log(k + s) - c.mean[u]) / c.std[u];
    }
    let { x: g, y: f } = this;
    g.set(c["inp.bias"]), _(g, c["inp.weight"], h, n, t, 1, 0), r.forEach((u, k) => {
      const y = this.history[k];
      y.push(y.shift()), y[y.length - 1].set(g), f.set(c[`blocks.${k}.bias`]);
      for (let v = 0; v < o; v += 1)
        _(f, c[`blocks.${k}.weight`], y[y.length - 1 - (o - 1 - v) * u], n, n, o, v);
      for (let v = 0; v < n; v += 1) f[v] = g[v] + Math.max(0, f[v]);
      [g, f] = [f, g];
    }), [this.x, this.y] = [g, f], this.logits.set(c["out.bias"]), _(this.logits, c["out.weight"], g, i, n, 1, 0);
    let w = 0;
    for (let u = 1; u < i; u += 1) this.logits[u] > this.logits[w] && (w = u);
    return w;
  }
}
const L = 2, Ee = 0, Re = 21;
class I {
  constructor(e, t, s, n) {
    this.model = e, this.inRate = t, this.startMs = s, this.onViseme = n, this.resampler = new ve(t, e.SR), this.stream = new _e(e);
  }
  resampler;
  stream;
  frames = 0;
  last = -1;
  candidate = -1;
  candidateRun = 0;
  push(e) {
    this.stream.push(this.resampler.push(e), (t) => this.onFrame(t));
  }
  /** Push silence through so the last sounds (held back by the model's lookahead) are reported. */
  flush() {
    const { lookahead: e, HOP: t, WIN: s, SR: n } = this.model;
    this.push(new Float32Array(Math.ceil(((e + L) * t + s) * this.inRate / n) + 64));
  }
  onFrame(e) {
    if (this.frames += 1, this.frames <= this.model.lookahead || (this.candidateRun = e === this.candidate ? this.candidateRun + 1 : 1, this.candidate = e, e === this.last) || this.candidateRun < L && !(e === Re && this.last === Ee)) return;
    this.last = e;
    const { lookahead: t, HOP: s, WIN: n, SR: o } = this.model, r = this.frames - this.candidateRun;
    this.onViseme({ id: e, ms: this.startMs + ((r - t) * s + n / 2) / o * 1e3 });
  }
}
const b = "https://cdn.jsdelivr.net/npm/@interviewflowai/talking-avatar@0.3.0/dist", be = `${b}/viseme-model.bin`, Oe = {
  female: `${b}/avatars/female.glb`,
  male: `${b}/avatars/male.glb`
}, Te = 150, Pe = 50, xe = 5e3, Le = 0.02, Ie = 1, H = "talking-avatar-capture", Fe = `
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
registerProcessor("${H}", Capture);
`, E = /* @__PURE__ */ new Map();
function R(a) {
  let e = E.get(a);
  return e || (e = fetch(a).then((t) => {
    if (!t.ok) throw new Error(`talking-avatar: viseme model ${a}: HTTP ${t.status}`);
    return t.arrayBuffer();
  }).then(ke), e.catch(() => E.delete(a)), E.set(a, e)), e;
}
class Ne {
  /** Resolves when the first avatar is on screen; rejects if it fails to load. */
  ready;
  renderer;
  modelUrl;
  streamDelayS;
  onError;
  context = null;
  contextReady = null;
  events = [];
  // in "heard" audio-context time, ms
  segments = null;
  stream = null;
  speech = null;
  streamToken = 0;
  speechToken = 0;
  status = "listening";
  disposed = !1;
  constructor(e) {
    this.modelUrl = e.modelUrl ?? be, this.streamDelayS = (e.streamDelayMs ?? Te) / 1e3, this.onError = e.onError ?? ((t) => console.error(t)), this.renderer = new ye(e.container, () => this.frameNow()), this.ready = this.setAvatar(e.avatarUrl ?? Oe.female, e.morphTargets), this.ready.catch(() => {
    }), R(this.modelUrl).catch((t) => this.onError(t));
  }
  /**
   * How the avatar behaves while it isn't speaking: "listening" (default) or "thinking" (e.g. while your agent is
   * generating a reply). Speaking is detected automatically from its audio and takes priority.
   */
  setStatus(e) {
    this.status = e;
  }
  /** Swap the avatar (with its own `morphTargets`, if it needs them). Resolves when the new one is on screen. */
  setAvatar(e, t) {
    return this.renderer.load(e, t).catch((s) => {
      throw this.onError(s), s;
    });
  }
  /**
   * Lip sync a live audio stream (WebRTC, getUserMedia, captureStream…). The avatar plays it, `streamDelayMs`
   * late. Replaces any previously attached stream.
   */
  async attachStream(e) {
    if (this.detachStream(), !e.getAudioTracks().length) throw new Error("talking-avatar: the stream has no audio track");
    const t = ++this.streamToken, [s, n] = await Promise.all([this.audioContext(), R(this.modelUrl)]);
    if (t !== this.streamToken || this.disposed) return;
    const o = new Audio();
    o.muted = !0, o.srcObject = e, o.play().catch(() => {
    });
    const r = s.createMediaStreamSource(e), i = s.createDelay(Math.max(1, this.streamDelayS * 2));
    i.delayTime.value = this.streamDelayS, r.connect(i).connect(s.destination);
    const c = new AudioWorkletNode(s, H, {
      numberOfOutputs: 0,
      channelCount: 1,
      channelCountMode: "explicit",
      channelInterpretation: "speakers"
    });
    r.connect(c);
    const m = { source: r, delay: i, capture: c, element: o, detector: null, expectedTime: 0 };
    c.port.onmessage = ({ data: l }) => {
      if (this.stream === m)
        try {
          if (!m.detector || Math.abs(l.time - m.expectedTime) > Le) {
            m.detector?.flush();
            const h = (l.time + this.streamDelayS) * 1e3;
            m.detector = new I(n, s.sampleRate, h, (d) => this.addEvent(d));
          }
          m.detector.push(l.samples), m.expectedTime = l.time + l.samples.length / s.sampleRate;
        } catch (h) {
          this.onError(h);
        }
    }, this.stream = m;
  }
  /** Stop playing and lip syncing the attached stream. */
  detachStream() {
    this.streamToken += 1;
    const e = this.stream;
    e && (this.stream = null, e.capture.port.onmessage = null, e.source.disconnect(), e.delay.disconnect(), e.capture.disconnect(), e.element.srcObject = null, this.events = [], this.segments = null);
  }
  /**
   * Play a clip (URL, encoded bytes or Blob, in any format the browser decodes) with lip sync. The whole clip is
   * analysed before it starts, so the mouth anticipates every sound. Resolves when it ends or is stopped.
   */
  async speak(e) {
    this.stopSpeaking();
    const t = ++this.speechToken;
    let s;
    if (typeof e == "string") {
      const f = await fetch(e);
      if (!f.ok) throw new Error(`talking-avatar: ${e}: HTTP ${f.status}`);
      s = await f.arrayBuffer();
    } else
      s = e instanceof Blob ? await e.arrayBuffer() : e.slice(0);
    const [n, o] = await Promise.all([this.audioContext(), R(this.modelUrl)]), r = await n.decodeAudioData(s), i = new Float32Array(r.length);
    for (let f = 0; f < r.numberOfChannels; f += 1) {
      const w = r.getChannelData(f);
      for (let u = 0; u < i.length; u += 1) i[u] += w[u] / r.numberOfChannels;
    }
    const c = [], m = new I(o, r.sampleRate, 0, (f) => c.push(f)), l = Math.round(r.sampleRate * Ie);
    for (let f = 0; f < i.length; f += l)
      if (m.push(i.subarray(f, f + l)), await new Promise((w) => setTimeout(w)), t !== this.speechToken || this.disposed) return;
    m.flush();
    const h = n.currentTime + Pe / 1e3;
    for (const f of c) this.addEvent({ id: f.id, ms: h * 1e3 + f.ms });
    const d = n.createBufferSource();
    d.buffer = r, d.connect(n.destination);
    const g = new Promise((f) => {
      d.onended = () => f();
    });
    d.start(h), this.speech = d, await g, this.speech === d && (this.speech = null);
  }
  /** Stop any clip started with speak(). */
  stopSpeaking() {
    this.speechToken += 1;
    const e = this.speech;
    e && (this.speech = null, e.stop(), e.disconnect(), this.events = [], this.segments = null);
  }
  /** Release the renderer, audio and model references. The instance can't be used afterwards. */
  dispose() {
    this.disposed || (this.disposed = !0, this.stopSpeaking(), this.detachStream(), this.renderer.dispose(), this.context?.close());
  }
  async audioContext() {
    this.contextReady ??= (async () => {
      const t = new AudioContext({ latencyHint: "interactive" }), s = URL.createObjectURL(new Blob([Fe], { type: "text/javascript" }));
      try {
        await t.audioWorklet.addModule(s);
      } finally {
        URL.revokeObjectURL(s);
      }
      return this.context = t, t;
    })();
    const e = await this.contextReady;
    return e.state === "suspended" && await e.resume(), e;
  }
  addEvent(e) {
    const { events: t } = this;
    let s = t.length;
    for (; s > 0 && t[s - 1].ms > e.ms; ) s -= 1;
    t.splice(s, 0, e);
    const n = this.heardMs() - xe;
    let o = 0;
    for (; o + 1 < t.length && t[o + 1].ms < n; ) o += 1;
    o && t.splice(0, o), this.segments = null;
  }
  /** The audio-context time (ms) currently coming out of the speakers. */
  heardMs() {
    const { context: e } = this;
    if (!e) return 0;
    const t = e.state === "running" ? e.getOutputTimestamp?.() : void 0;
    return t?.contextTime && t.performanceTime ? t.contextTime * 1e3 + (performance.now() - t.performanceTime) : (e.currentTime - (e.outputLatency || 0)) * 1e3;
  }
  frameNow() {
    if (!this.events.length) return { visemes: {}, mode: this.status };
    const e = this.heardMs();
    return this.segments ??= X(this.events), { visemes: q(this.segments, e), mode: ee(this.events, e) ? "speaking" : this.status };
  }
}
export {
  Oe as A,
  be as D,
  Ne as T,
  // bake-off patch: also export the internals (names from the source map)
  I as VisemeDetector,
  ke as parseVisemeModel,
  R as loadVisemeModel,
  X as buildSegments,
  q as mouthAt,
  ee as isSpeakingAt,
  ye as AvatarRenderer
};

