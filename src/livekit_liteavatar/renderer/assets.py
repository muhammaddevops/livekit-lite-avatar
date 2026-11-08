"""Download LiteAvatar's code, weights and presenters from upstream into the data folder.

Nothing from LiteAvatar is shipped with this package. `liteavatar-renderer setup` fetches:
  * the LiteAvatar code from GitHub (HumanAIGC/lite-avatar, MIT), at the commit this package was tested with;
  * three weight files from ModelScope (HumanAIGC-Engineering/LiteAvatarGallery), checked against known SHA-256s;
  * one or more presenters from the same ModelScope gallery, or "sample" (the sample presenter in LiteAvatar's repo).

About 1.3 GB in total. See the README's licence section before using the weights or presenters commercially.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .settings import DEFAULT_GALLERY, Settings, valid_presenter_id

LITEAVATAR_REPO = "https://github.com/HumanAIGC/lite-avatar"
LITEAVATAR_COMMIT = "5b7ec850945e03d56fb290b05fb68440c359fa86"  # tested; the stream wrapper uses its internals
MODELSCOPE = "https://modelscope.cn/models/HumanAIGC-Engineering/LiteAvatarGallery/resolve/master"
PARAFORMER = "weights/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
WEIGHTS = [  # (path inside the LiteAvatar checkout, path on ModelScope, size in bytes, sha256)
    ("weights/model_1.onnx", "lite_avatar_weights/model_1.onnx", 184_415_825,
     "312f0213a23a1c17ecb66f9edf8413b8ef193c0d4d2f5f9dd6714a178492c34c"),
    (f"{PARAFORMER}/model.pb", "lite_avatar_weights/model.pb", 880_502_012,
     "5bba782a5e9196166233b9ab12ba04cadff9ef9212b4ff6153ed9290ff679025"),
    (f"{PARAFORMER}/lm/lm.pb", "lite_avatar_weights/lm.pb", 237_373_385,
     "5b59db5f607fde57bd8d6c8df8f22ce5b37ec7adcdb7b4ee785a4478f06c1c50"),
]
SAMPLE_PRESENTER = "sample"
_UA = {"User-Agent": "livekit-liteavatar-setup"}


def _say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _download(url: str, dest: Path, size: int | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=60) as resp, part.open("wb") as out:
        total = size or int(resp.headers.get("Content-Length") or 0)
        done, shown = 0, -1
        while True:
            block = resp.read(1 << 20)
            if not block:
                break
            out.write(block)
            done += len(block)
            pct = int(done * 100 / total) if total else -1
            if total and pct // 10 != shown:
                shown = pct // 10
                _say(f"  {dest.name}: {pct}% of {total / 1e6:.0f} MB")
    part.replace(dest)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True)


def fetch_code(settings: Settings, commit: str = LITEAVATAR_COMMIT) -> Path:
    repo = settings.lite_avatar_dir
    if (repo / "lite_avatar.py").is_file():
        _say(f"LiteAvatar code: already at {repo}")
        return repo
    if shutil.which("git") is None:
        raise SystemExit("git is needed to fetch the LiteAvatar code")
    _say(f"LiteAvatar code: fetching {LITEAVATAR_REPO} at {commit[:10]}")
    repo.mkdir(parents=True, exist_ok=True)
    if (repo / ".git").is_dir():  # an earlier fetch was interrupted
        _git("remote", "set-url", "origin", LITEAVATAR_REPO, cwd=repo)
    else:
        _git("init", "-q", cwd=repo)
        _git("remote", "add", "origin", LITEAVATAR_REPO, cwd=repo)
    _git("fetch", "-q", "--depth", "1", "origin", commit, cwd=repo)
    _git("checkout", "-q", "--detach", "FETCH_HEAD", cwd=repo)
    return repo


def fetch_weights(settings: Settings, verify: bool = True) -> None:
    repo = settings.lite_avatar_dir
    for rel, remote, size, sha in WEIGHTS:
        dest = repo / rel
        if dest.is_file() and dest.stat().st_size == size:
            _say(f"weights: {rel} already present")
            continue
        _say(f"weights: downloading {remote} ({size / 1e6:.0f} MB)")
        _download(f"{MODELSCOPE}/{remote}", dest, size)
        if verify:
            got = _sha256(dest)
            if got != sha:
                dest.unlink()
                raise SystemExit(f"{rel}: SHA-256 {got} does not match the tested file ({sha}). Upstream may have "
                                 "changed it; rerun with --no-verify to accept the new file at your own risk.")


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    root = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if root != target and root not in target.parents:
            raise SystemExit(f"refusing to extract {member.filename!r}: outside the target folder")
    zf.extractall(dest)


def _install_presenter_zip(zip_path: Path, dest: Path) -> None:
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extract(zf, tmp_path)
        found = sorted(tmp_path.rglob("bg_video.mp4"))
        if not found:
            raise SystemExit(f"{zip_path.name} does not look like a LiteAvatar presenter (no bg_video.mp4)")
        shutil.move(str(found[0].parent), str(dest))


def fetch_presenter(settings: Settings, presenter: str, gallery: str = DEFAULT_GALLERY) -> Path:
    if not valid_presenter_id(presenter):
        raise SystemExit(f"invalid presenter id: {presenter!r}")
    dest = settings.presenter_dir(presenter)
    if (dest / "bg_video.mp4").is_file():
        _say(f"presenter {presenter}: already installed")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    if presenter == SAMPLE_PRESENTER:
        sample = settings.lite_avatar_dir / "data" / "sample_data.zip"
        if not sample.is_file():
            raise SystemExit("the sample presenter comes with the LiteAvatar code; run setup without --skip-code")
        _say("presenter sample: unpacking LiteAvatar's sample_data.zip")
        _install_presenter_zip(sample, dest)
        return dest
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        zip_path = Path(tmp) / f"{presenter}.zip"
        _say(f"presenter {presenter}: downloading from LiteAvatarGallery {gallery}")
        _download(f"{MODELSCOPE}/{gallery}/{presenter}.zip", zip_path)
        _install_presenter_zip(zip_path, dest)
    return dest


def setup(settings: Settings, presenters: list[str], gallery: str = DEFAULT_GALLERY, verify: bool = True,
          skip_code: bool = False) -> None:
    if not skip_code:
        fetch_code(settings)
        fetch_weights(settings, verify=verify)
    for p in presenters:
        fetch_presenter(settings, p, gallery)
    _say(f"Done. Data folder: {settings.home}")
    _say(f"Installed presenters: {', '.join(settings.installed_presenters()) or 'none'}")


def check(settings: Settings, presenter: str | None = None) -> list[str]:
    """What is missing for the renderer to run, as human-readable lines (empty when ready)."""
    missing = []
    repo = settings.lite_avatar_dir
    if not (repo / "lite_avatar.py").is_file():
        missing.append(f"LiteAvatar code ({repo})")
    for rel, _, size, _ in WEIGHTS:
        f = repo / rel
        if not f.is_file() or f.stat().st_size != size:
            missing.append(f"weight file {rel}")
    pid = presenter or settings.presenter
    if not (settings.presenter_dir(pid) / "bg_video.mp4").is_file():
        missing.append(f"presenter {pid} ({settings.presenter_dir(pid)})")
    return missing
