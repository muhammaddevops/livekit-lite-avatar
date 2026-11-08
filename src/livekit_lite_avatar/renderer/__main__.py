"""liteavatar-renderer: set up, run and test the LiteAvatar renderer.

    liteavatar-renderer setup [--presenter ID ...]    download LiteAvatar code, weights and presenters (~1.3 GB)
    liteavatar-renderer serve [--port 8791 ...]       run the session server agents call
    liteavatar-renderer render IN.wav OUT.mp4         offline test: WAV in, MP4 out, prints the real-time factor
    liteavatar-renderer presenters                    list installed presenters
    liteavatar-renderer worker --url ... --token ...  join one room directly (debugging)

Every setting can also come from a LITEAVATAR_* environment variable; flags win.
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import settings as settings_mod
from .settings import DEFAULT_GALLERY, Settings


def _settings(ns: argparse.Namespace) -> Settings:
    return Settings.from_env().with_args(ns)


def _cmd_setup(ns: argparse.Namespace) -> None:
    from . import assets

    s = _settings(ns)
    presenters = ns.presenters or [s.presenter]
    assets.setup(s, presenters, gallery=ns.gallery, verify=not ns.no_verify, skip_code=ns.skip_code)


def _cmd_serve(ns: argparse.Namespace) -> None:
    from .server import serve

    serve(_settings(ns))


def _cmd_render(ns: argparse.Namespace) -> None:
    from . import assets
    from .offline import default_wav, render_file

    s = _settings(ns)
    missing = assets.check(s)
    if missing:
        raise SystemExit("renderer is not set up; missing:\n  " + "\n  ".join(missing))
    wav = ns.wav or default_wav(s)
    result = render_file(wav, ns.out, s)
    print(result.summary())
    print(result.output)


def _cmd_presenters(ns: argparse.Namespace) -> None:
    s = _settings(ns)
    installed = s.installed_presenters()
    for p in installed:
        print(p + ("  (default)" if p == s.presenter else ""))
    if not installed:
        print(f"none installed in {s.presenters_dir}; run: liteavatar-renderer setup", file=sys.stderr)


def _cmd_worker(ns: argparse.Namespace) -> None:
    from .worker import main as worker_main

    worker_main(_settings(ns), warm=ns.warm, url=ns.url, token=ns.token)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="liteavatar-renderer", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup", help="download LiteAvatar code, weights and presenters")
    p.add_argument(
        "--presenter",
        dest="presenters",
        action="append",
        help="presenter id from LiteAvatarGallery, or 'sample'; repeat for more (default: the default presenter)",
    )
    p.add_argument("--gallery", default=DEFAULT_GALLERY, help=f"LiteAvatarGallery folder (default {DEFAULT_GALLERY})")
    p.add_argument("--no-verify", action="store_true", help="accept weight files whose SHA-256 differs")
    p.add_argument("--skip-code", action="store_true", help="only fetch presenters")
    settings_mod.add_arguments(p, ["home"])
    p.set_defaults(fn=_cmd_setup)

    p = sub.add_parser("serve", help="run the session server")
    settings_mod.add_arguments(p)
    p.set_defaults(fn=_cmd_serve)

    p = sub.add_parser("render", help="offline test: render a WAV to an MP4 and print the real-time factor")
    p.add_argument("wav", nargs="?", help="speech WAV (default: a Mandarin sample from the LiteAvatar checkout)")
    p.add_argument("out", nargs="?", default="liteavatar-test.mp4", help="output MP4 (default liteavatar-test.mp4)")
    settings_mod.add_arguments(
        p, ["home", "presenter", "fps", "width", "threads", "mouth_gain", "open_gain", "smooth_hz", "head_motion"]
    )
    p.set_defaults(fn=_cmd_render)

    p = sub.add_parser("presenters", help="list installed presenters")
    settings_mod.add_arguments(p, ["home", "presenter"])
    p.set_defaults(fn=_cmd_presenters)

    p = sub.add_parser("worker", help="run one worker (normally started by serve)")
    p.add_argument("--warm", action="store_true", help="load, print READY, then read one JSON job from stdin")
    p.add_argument("--url", help="LiveKit server URL")
    p.add_argument("--token", help="LiveKit token for the avatar participant")
    settings_mod.add_arguments(p)
    p.set_defaults(fn=_cmd_worker)

    ns = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("liteavatar").setLevel(logging.INFO)
    ns.fn(ns)


if __name__ == "__main__":
    main()
