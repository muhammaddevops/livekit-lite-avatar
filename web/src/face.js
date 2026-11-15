// Nora's face in three.js: loading, framing, lighting, materials and idle life. It draws whatever mouth shapes it is
// given; lipsync.js decides them from the voice.
//
// The look ("high" quality):
//   lighting   a soft warm key, a cool fill and two rim lights for hair and shoulder separation, on a gradient
//   shading    ambient occlusion (GTAO) around the eyes, nose and mouth, and 2x supersampling
//   skin       wrapped, red-shifted diffuse light (the soft, warm terminator of skin) and a generated pore normal map
//   eyes       glossy, with MakeHuman's reddish iris pulled to brown and a clean white
//   life       blinks, small eye saccades, a slight head sway and resting smile; brows that lift on stressed words
//              and small nods on emphasis (from expression.js)
// "low" quality keeps the materials but drops the ambient occlusion and supersampling, for slower devices.
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { MeshoptDecoder } from "three/addons/libs/meshopt_decoder.module.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { GTAOPass } from "three/addons/postprocessing/GTAOPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";

/** Nora (CC0), next to this module. */
export const NORA_URL = new URL("../assets/nora.glb", import.meta.url).href;

const FRAME_HEIGHT = 0.44; // metres: the top of the hair to the collarbones
const FACE_OFFSET = 0.035; // the camera aims this far above the centre of the eye-to-chin region
const ARMS = [["LeftArm", 1.15], ["RightArm", -1.15]]; // from the rig's T-pose down to the sides (z, radians)
const IDLE_S = 3600; // blink and saccade schedules repeat after an hour

/** The centre of the region moved by the blink and jaw shapes (the face), in world space. */
function faceCentre(mesh) {
  const box = new THREE.Box3();
  const v = new THREE.Vector3();
  for (const name of ["eyeBlinkLeft", "eyeBlinkRight", "jawOpen"]) {
    const i = mesh.morphTargetDictionary?.[name];
    const delta = i === undefined ? null : mesh.geometry.morphAttributes.position?.[i];
    if (!delta) continue;
    for (let k = 0; k < delta.count; k += 1) {
      if (Math.abs(delta.getX(k)) + Math.abs(delta.getY(k)) + Math.abs(delta.getZ(k)) < 1e-4) continue;
      box.expandByPoint(mesh.localToWorld(mesh.getVertexPosition(k, v)));
    }
  }
  return box.getCenter(new THREE.Vector3());
}

function gradientBackdrop(top = "#eef3f7", bottom = "#c9d6e1") {
  const c = document.createElement("canvas");
  c.width = 8;
  c.height = 256;
  const g = c.getContext("2d");
  const grd = g.createLinearGradient(0, 0, 0, 256);
  grd.addColorStop(0, top);
  grd.addColorStop(1, bottom);
  g.fillStyle = grd;
  g.fillRect(0, 0, 8, 256);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

/** Physical materials per part of the MakeHuman build: eyes, skin, hair, teeth and the rest. */
function refine(mesh) {
  const src = mesh.material;
  if (!(src instanceof THREE.MeshStandardMaterial)) return;
  const m = new THREE.MeshPhysicalMaterial();
  THREE.MeshStandardMaterial.prototype.copy.call(m, src);
  m.metalness = 0;
  const name = src.name;
  if (/high-poly|low-poly/i.test(name)) {
    Object.assign(m, { roughness: 0.08, clearcoat: 1, clearcoatRoughness: 0.04, specularIntensity: 0.7 });
    m.color.setRGB(0.72, 0.8, 0.92);
  } else if (/\.body$/i.test(name)) {
    Object.assign(m, { roughness: 0.6, specularIntensity: 0.32, sheen: 0.18, sheenRoughness: 0.6 });
    m.sheenColor = new THREE.Color("#f0c8bc");
  } else if (/ponytail|bob|braid|bun|long0|short0|hair|eyelash|eyebrow/i.test(name)) {
    Object.assign(m, { roughness: 0.85, specularIntensity: 0.12, envMapIntensity: 0.35 }); // shiny hair reads grey
    if (m.alphaTest > 0) {
      m.alphaToCoverage = true; // softer, multisampled cut-out edges
      m.alphaTest = 0.35;
    }
  } else if (/teeth/i.test(name)) {
    // polished teeth mirror the cool fill and read blue-grey: warmer, less glossy, less reflective
    Object.assign(m, { roughness: 0.45, specularIntensity: 0.4, envMapIntensity: 0.4 });
    m.color.setRGB(1.08, 1.02, 0.94);
  } else {
    m.roughness = Math.max(m.roughness, 0.6);
  }
  mesh.material = m;
  src.dispose();
}

// Skin: three.js's direct diffuse term, wrapped and red-shifted. Written against three.js r186; on another version the
// line isn't found and the skin keeps the standard shading.
const DIFFUSE_LINE =
  "reflectedLight.directDiffuse += irradiance * BRDF_Lambert( material.diffuseContribution ) * ( 1.0 - F );";
const SKIN_DIFFUSE = `
	float skinNL = dot( geometryNormal, directLight.direction );
	vec3 skinWrap = vec3( 0.45, 0.22, 0.15 );
	vec3 skinIrradiance = saturate( ( vec3( skinNL ) + skinWrap ) / ( 1.0 + skinWrap ) ) * directLight.color;
	reflectedLight.directDiffuse += skinIrradiance * BRDF_Lambert( material.diffuseContribution ) * ( 1.0 - F );`;

/** A tiling pore and micro-relief normal map, generated from a fixed seed: small dimples plus fine grain. */
function poreNormalMap(size = 256, seed = 11) {
  let a = seed;
  const rnd = () => {
    a = (a * 1664525 + 1013904223) >>> 0;
    return a / 4294967296;
  };
  const h = new Float32Array(size * size);
  for (let p = 0; p < 1100; p += 1) {
    const cx = rnd() * size, cy = rnd() * size, r = 0.9 + 1.6 * rnd(), depth = 0.6 + 0.6 * rnd();
    const R = Math.ceil(3 * r);
    for (let dy = -R; dy <= R; dy += 1) {
      for (let dx = -R; dx <= R; dx += 1) {
        const x = (Math.floor(cx) + dx + size) % size, y = (Math.floor(cy) + dy + size) % size;
        h[y * size + x] -= depth * Math.exp(-(dx * dx + dy * dy) / (2 * r * r));
      }
    }
  }
  for (let i = 0; i < h.length; i += 1) h[i] += 0.25 * (rnd() - 0.5);
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  const img = g.createImageData(size, size);
  const at = (x, y) => h[((y + size) % size) * size + ((x + size) % size)];
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const nx = -(at(x + 1, y) - at(x - 1, y)), ny = -(at(x, y + 1) - at(x, y - 1)), nz = 1;
      const l = Math.hypot(nx, ny, nz);
      const i = 4 * (y * size + x);
      img.data[i] = ((nx / l) * 0.5 + 0.5) * 255;
      img.data[i + 1] = ((ny / l) * 0.5 + 0.5) * 255;
      img.data[i + 2] = ((nz / l) * 0.5 + 0.5) * 255;
      img.data[i + 3] = 255;
    }
  }
  g.putImageData(img, 0, 0);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.NoColorSpace;
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  return t;
}

function upgradeSkin(material) {
  const patched = THREE.ShaderChunk.lights_physical_pars_fragment.includes(DIFFUSE_LINE);
  if (!patched) console.warn("livekit-lite-avatar 3D: this three.js version isn't r186; skin keeps standard shading");
  material.normalMap = poreNormalMap();
  material.normalMap.repeat.set(56, 56);
  material.normalScale.set(0.22, 0.22);
  if (patched) {
    material.onBeforeCompile = (shader) => {
      shader.fragmentShader = shader.fragmentShader.replace(
        "#include <lights_physical_pars_fragment>",
        THREE.ShaderChunk.lights_physical_pars_fragment.replace(DIFFUSE_LINE, SKIN_DIFFUSE),
      );
    };
    material.customProgramCacheKey = () => "lla-skin-v1";
  }
  material.needsUpdate = true;
}

/** Pull MakeHuman's reddish iris to a natural brown, and the white of the eye to a clean white, in the shader. */
function upgradeEyes(material) {
  material.color.setRGB(0.9, 0.94, 1.0); // under the warm key light a white sclera otherwise reads beige
  material.onBeforeCompile = (shader) => {
    shader.fragmentShader = shader.fragmentShader.replace(
      "#include <map_fragment>",
      `#include <map_fragment>
	float irisLum = dot( diffuseColor.rgb, vec3( 0.3, 0.59, 0.11 ) );
	float irisMask = smoothstep( 0.01, 0.05, diffuseColor.r - diffuseColor.b ) * ( 1.0 - smoothstep( 0.35, 0.6, irisLum ) );
	diffuseColor.rgb = mix( diffuseColor.rgb, irisLum * vec3( 2.5, 1.75, 1.0 ), irisMask );
	diffuseColor.rgb = mix( diffuseColor.rgb, irisLum * vec3( 1.02, 1.04, 1.08 ), smoothstep( 0.35, 0.55, irisLum ) * 0.8 );`,
    );
  };
  material.customProgramCacheKey = () => "lla-eyes-v1";
  material.needsUpdate = true;
}

function seeded(seed) {
  let a = seed;
  return () => {
    a = (a * 1664525 + 1013904223) >>> 0;
    return a / 4294967296;
  };
}

/** Blink times (s): every 2.5 to 5.5 s. */
function blinkTimes(duration, seed = 7) {
  const rnd = seeded(seed);
  const out = [];
  for (let t = 1.2 + rnd(); t < duration; t += 2.5 + 3 * rnd()) out.push(t);
  return out;
}

/** Eye saccades: [time s, yaw, pitch] every 0.7 to 2.5 s. */
function saccadeTimes(duration, seed = 19) {
  const rnd = seeded(seed);
  const out = [[0, 0, 0]];
  for (let t = 0.6 + rnd(); t < duration; t += 0.7 + 1.8 * rnd()) out.push([t, (rnd() - 0.5) * 0.09, (rnd() - 0.5) * 0.04]);
  return out;
}

/**
 * Load the face into `canvas`. Options: url, width, height (the drawing size in CSS pixels; default 512 x 640, 4:5),
 * quality ("high" or "low"), top (the T-shirt colour), background ("gradient" or a CSS colour), smile, browLift, nod,
 * exposure, preserveDrawingBuffer.
 */
export async function createFace(canvas, options = {}) {
  const {
    url = NORA_URL,
    width = 512,
    height = 640,
    quality = "high",
    top = "#4f6f92",
    background = "gradient",
    smile = 0.12,
    browLift = 0.35,
    nod = 0.035,
    exposure = 1.0,
    preserveDrawingBuffer = false,
  } = options;
  const high = quality !== "low";
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer });
  const pixelRatio = high ? 2 : Math.min(2, globalThis.devicePixelRatio || 1);
  renderer.setPixelRatio(pixelRatio);
  renderer.setSize(width, height, false);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = exposure;

  const scene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.5;
  scene.background = background === "gradient" ? gradientBackdrop() : new THREE.Color(background);
  const lights = [
    ["key", new THREE.DirectionalLight(0xfffaf5, 1.8), [-0.9, 0.5, 0.95]],
    ["fill", new THREE.DirectionalLight(0xeef3ff, 0.38), [0.95, 0.05, 0.9]],
    ["rim", new THREE.DirectionalLight(0xfff4ea, 0.7), [0.75, 0.0, -1.0]], // at head height: outlines the sides
    ["rim2", new THREE.DirectionalLight(0xeef3ff, 0.35), [-0.75, -0.05, -0.9]],
  ];

  const gltf = await new GLTFLoader().setMeshoptDecoder(MeshoptDecoder).loadAsync(url);
  const root = gltf.scene;
  scene.add(root);
  for (const [name, angle] of ARMS) root.getObjectByName(name)?.rotateOnAxis(new THREE.Vector3(0, 0, 1), angle);
  root.traverse((o) => {
    if (!o.isMesh) return;
    refine(o);
    const name = o.material?.name ?? "";
    if (top && /shirt|sweater|suit|top/i.test(name) && !/\.body$/.test(name)) {
      o.material.color = new THREE.Color(top);
      o.material.needsUpdate = true;
    }
  });
  root.updateMatrixWorld(true);

  const meshes = [];
  root.traverse((o) => {
    if (o.isMesh && o.morphTargetDictionary) meshes.push(o);
  });
  if (!meshes.length) throw new Error(`livekit-lite-avatar 3D: ${url} has no morph targets`);
  renderer.render(scene, new THREE.PerspectiveCamera()); // update the skeletons before reading vertex positions
  // frame on the body mesh (the one with the most shapes): hair and lashes also carry blink shapes
  const body = meshes.reduce((a, b) =>
    Object.keys(b.morphTargetDictionary).length > Object.keys(a.morphTargetDictionary).length ? b : a,
  );
  const face = faceCentre(body);
  upgradeSkin(body.material);
  root.traverse((o) => {
    if (!o.isMesh) return;
    const name = o.material.name ?? "";
    if (/high-poly/i.test(name)) upgradeEyes(o.material);
    if (/eyebrow|eyelash/i.test(name)) {
      o.material.color.setRGB(0.16, 0.11, 0.08); // more definition
      o.material.needsUpdate = true;
    }
  });

  const camera = new THREE.PerspectiveCamera(18, width / height, 0.05, 20);
  const distance = FRAME_HEIGHT / 2 / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
  const centre = new THREE.Vector3(face.x, face.y + FACE_OFFSET, face.z);
  camera.position.set(centre.x, centre.y + 0.03, centre.z + distance);
  camera.lookAt(centre);
  for (const [, light, offset] of lights) {
    light.position.copy(centre).add(new THREE.Vector3(...offset));
    light.target.position.copy(centre);
    scene.add(light, light.target);
  }

  let composer = null;
  let ao = null;
  const target = () =>
    new THREE.WebGLRenderTarget(width * 2, height * 2, { samples: 4, type: THREE.HalfFloatType });
  if (high) {
    // a multisampled target, so antialiasing (and the hair's soft edges) survives post-processing
    composer = new EffectComposer(renderer, target());
    composer.setPixelRatio(2);
    composer.setSize(width, height);
    composer.addPass(new RenderPass(scene, camera));
    ao = new GTAOPass(scene, camera, width, height);
    ao.updateGtaoMaterial({ radius: 0.08, distanceExponent: 1.5, thickness: 1.2, scale: 1.2, samples: 16 });
    ao.blendIntensity = 1.0;
    composer.addPass(ao);
    composer.addPass(new OutputPass());
  }

  const head = root.getObjectByName("Head");
  const headRest = head ? head.quaternion.clone() : null;
  const eyes = ["LeftEye", "RightEye"].map((n) => root.getObjectByName(n)).filter(Boolean);
  const eyeRest = eyes.map((e) => e.quaternion.clone());
  const blinks = blinkTimes(IDLE_S);
  const saccades = saccadeTimes(IDLE_S);
  const euler = new THREE.Euler();
  const quat = new THREE.Quaternion();

  /** Idle life at `t` seconds since start, plus brow lift and nod (0..1) from the voice. */
  function idle(t, brow, nodAmount) {
    const out = {};
    if (head) {
      euler.set(
        0.012 * Math.sin(t * 0.9) + 0.006 * Math.sin(t * 2.3 + 1) + nod * nodAmount,
        0.02 * Math.sin(t * 0.55 + 0.4) + 0.006 * Math.sin(t * 1.7),
        0.008 * Math.sin(t * 0.7 + 2),
      );
      head.quaternion.copy(headRest).multiply(quat.setFromEuler(euler));
    }
    const ti = t % IDLE_S;
    let b = 0;
    for (const s of blinks) {
      if (s > ti) break;
      const p = (ti - s) / 0.18;
      if (p < 1) b = p < 0.35 ? p / 0.35 : 1 - (p - 0.35) / 0.65;
    }
    out.eyeBlinkLeft = b;
    out.eyeBlinkRight = b;
    if (brow > 0 && browLift > 0) {
      out.browInnerUp = browLift * brow;
      out.browOuterUpLeft = 0.5 * browLift * brow;
      out.browOuterUpRight = 0.5 * browLift * brow;
    }
    // eye saccades: a quick 40 ms move to each new small target
    let i = 0;
    while (i + 1 < saccades.length && saccades[i + 1][0] <= ti) i += 1;
    const [s0, y1, p1] = saccades[i];
    const [, y0, p0] = saccades[Math.max(0, i - 1)];
    const k = Math.min(1, (ti - s0) / 0.04);
    euler.set(p0 + (p1 - p0) * k, y0 + (y1 - y0) * k, 0);
    quat.setFromEuler(euler);
    eyes.forEach((e, j) => e.quaternion.copy(eyeRest[j]).multiply(quat));
    out.mouthSmileLeft = smile;
    out.mouthSmileRight = smile;
    return out;
  }

  function setMorphs(values) {
    for (const mesh of meshes) {
      const dict = mesh.morphTargetDictionary;
      for (const name in values) {
        const i = dict[name];
        if (i !== undefined) mesh.morphTargetInfluences[i] = values[name];
      }
    }
  }

  /** Draw one frame: mouth shapes `values` (morph name -> 0..1), at `t` seconds since start. */
  function frame(values, t, { brow = 0, nod: nodAmount = 0 } = {}) {
    for (const m of meshes) m.morphTargetInfluences.fill(0);
    setMorphs(values);
    setMorphs(idle(t, brow, nodAmount));
    if (composer) composer.render();
    else renderer.render(scene, camera);
  }

  /** Change the drawing size (CSS pixels). */
  function resize(w, h) {
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    if (composer) {
      composer.setSize(w, h);
      ao.setSize(w, h);
    }
  }

  function dispose() {
    composer?.dispose();
    pmrem.dispose();
    scene.traverse((o) => {
      if (!o.isMesh) return;
      o.geometry.dispose();
      for (const m of [o.material].flat()) {
        for (const v of Object.values(m)) if (v?.isTexture) v.dispose();
        m.dispose();
      }
    });
    scene.environment?.dispose();
    scene.background?.isTexture && scene.background.dispose();
    renderer.dispose();
  }

  return { canvas, renderer, scene, camera, meshes, frame, resize, dispose };
}
