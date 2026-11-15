# livekit-lite-avatar: the 3D style

A 3D talking face for LiveKit agents, drawn in the browser and lip-synced to the agent's voice. There's no renderer
and no video track, and nothing changes in the agent. Its parts are free for commercial use: the face is CC0, the
lip-sync library MIT, and this code Apache-2.0.

```js
import { createAvatar3D } from "livekit-lite-avatar-3d";

const avatar = await createAvatar3D(canvas);
avatar.follow(room); // a livekit-client Room: plays and lip-syncs the agent's voice when it arrives
// in the click that joins the call: await avatar.resumeAudio();
```

Needs `three` 0.186.1. The face plays the agent's voice itself, so don't also attach that track.

Usage, options and how it works: [the main README](../README.md#the-3d-style).

| Folder | |
|---|---|
| `src/` | the face renderer (`face.js`), live lip-sync (`lipsync.js`), brows and nods (`expression.js`), the API (`index.js`) |
| `assets/` | Nora (`nora.glb`, CC0), her build recipe, and `NOTICE.md` with the sources and licences |
| `vendor/talking-avatar/` | talking-avatar 0.3.0 (MIT), pinned; see `VENDOR.md` |
