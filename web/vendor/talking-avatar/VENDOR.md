# Vendored: @interviewflowai/talking-avatar 0.3.0 (MIT)

The 3D style's lip-sync model, viseme detector, coarticulation and mouth mapping come from this package. It is
pinned here, not loaded from npm, for two reasons:
- The package's first releases (0.1.0 and 0.1.1) were PolyForm Noncommercial. It has been MIT since 0.1.2, so pinning
  means "latest" can never change the licence.
- It exports only its own avatar class. To drive another face, its internals must be exported (below).

| | |
|---|---|
| Package | `@interviewflowai/talking-avatar@0.3.0`, published 2026-11-02 |
| npm `dist.shasum` (sha1) | `fb8e0165fce51c9745921526e46c0c73c6daeb6f` |
| npm `dist.integrity` (sha512) | `DqAejc/z6oJzFwHHo6M+FSXHQUtOBsR4qnvnIa7nkw5pCp6OTXa43rMDm+m9dTEVXUk6iHmcOBLJIy5B54aV/Q==` |
| `LICENSE` | the package's MIT licence (code and model), Copyright (c) 2026 InterviewFlowAI |
| `viseme-model.bin` | the package's `dist/viseme-model.bin`, unchanged (sha256 `f1cdf2b76b9f2da8730b1d3e47d7e05a2b79c9b92d00dd00c595d64e471f419c`) |
| `talking-avatar-core.js` | the package's `dist/TalkingAvatar-C1bLAM_K.js` (sha256 `12ba5c2ac6eaece70c7becb1dab5873247d459d7ff16074339b14c0dced35312`) with only its export list extended |
| Model training data | LibriTTS-R (CC BY 4.0), not redistributed |

## The one change: a longer export list

`talking-avatar-core.patch` is the whole diff. No code changed. The extra exports, with names taken from the bundle's
source map:

| Exported as | Bundle name | Source |
|---|---|---|
| `VisemeDetector` | `I` | `src/visemeDetector.ts` |
| `parseVisemeModel` | `ke` | `src/visemeModel.ts` |
| `loadVisemeModel` | `R` | `src/TalkingAvatar.ts` (`loadModel`) |
| `buildSegments`, `mouthAt`, `isSpeakingAt` | `X`, `q`, `ee` | `src/coarticulation.ts` |
| `AvatarRenderer` | `ye` | `src/renderer.ts` (only `targets()` and `applyMouth()` are used, on a recorder) |

The bundle imports `three` and four files from `three/examples/jsm/` for the package's own renderer. They load but
aren't used.
