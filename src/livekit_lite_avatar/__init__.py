"""Self-hosted, CPU-only LiteAvatar video avatar for LiveKit Agents.

    from livekit_lite_avatar import LiteAvatarSession

    avatar = LiteAvatarSession(renderer_url="http://127.0.0.1:8791")
    await avatar.start(session, room=ctx.room)

The renderer runs as its own process: `liteavatar-renderer serve` (install with the `renderer` extra).
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from .session import DEFAULT_RENDERER_URL, LiteAvatarError, LiteAvatarSession

AvatarSession = LiteAvatarSession  # same name as LiveKit's avatar plugins (tavus.AvatarSession, ...)

try:
    __version__ = _version("livekit-lite-avatar")
except PackageNotFoundError:  # running from a source checkout without installing
    __version__ = "0.0.0"

__all__ = ["DEFAULT_RENDERER_URL", "AvatarSession", "LiteAvatarError", "LiteAvatarSession", "__version__"]
