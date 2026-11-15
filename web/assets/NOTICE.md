# Nora: licences

## The face (`nora.glb`): CC0 1.0

Nora was built with MPFB 2.0.18 (MakeHuman for Blender) from these MakeHuman community assets. Each is listed as
**CC0** on its asset pack's page (checked 2026-11-10). `nora.mpfb.json` is the recipe.

| Part | Asset | Pack | Author |
|---|---|---|---|
| Body, eyes, teeth, tongue | MakeHuman base mesh, `high-poly`, `teeth_base`, `tongue01` | MakeHuman system assets | MakeHuman |
| Skin | `callharvey3d_midtoned_female` | Skins 01 | callharvey3d |
| Hair | `toigo_curled_under_bob` (texture recoloured dark brown) | Hair 01 | Margaret Toigo |
| Brows | `mindfront_eyebrows_12` | Eyebrows 01 | Mindfront |
| Lashes | `mindfront_eyelashes_02` | Eyelashes 01 | Mindfront |
| Top | `toigo_basic_tucked_t-shirt` (tinted by the renderer) | Shirts 01 | Margaret Toigo |
| Mouth and expression shapes | Oculus visemes, ARKit face units | Visemes 02, Faceunits 01 ("shared under CC0") | Mika Suominen |
| Rig and weights | `talkinghead.mpfbskel`, `talkinghead.mhw` (`"license": "CC0"`) | TalkingHead v1.8.2, `blender/MPFB/` | Mika Suominen |

MakeHuman's licence (section D) says the project "makes no claim whatsoever" over output such as exports from MPFB.
MPFB itself is GPL-3.0 software; its output isn't covered by that. CC0 needs no attribution: the table is a courtesy
and an audit trail.

## The code it runs with

- **talking-avatar 0.3.0** (`../vendor/talking-avatar/`): code and lip-sync model, MIT, Copyright (c) 2026
  InterviewFlowAI. Keep its `LICENSE` with copies of the code.
- **three.js 0.186.1**: MIT, Copyright © 2010-2026 three.js authors. Loaded by the page, not included here.
- **TalkingHead's Blender add-on**: MIT, Copyright (c) 2026 Mika Suominen. Used only to build the face.
- The renderer, live lip-sync wiring and expression code in `../src/` are this project's (Apache-2.0).

## Not covered

The voice: whatever text-to-speech your agent uses has its own terms.
