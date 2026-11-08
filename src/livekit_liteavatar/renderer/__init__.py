"""The LiteAvatar renderer: a separate process that joins LiveKit rooms and publishes the avatar's video.

Needs the `renderer` extra (torch, onnxruntime, OpenCV and LiteAvatar's dependencies) and the upstream assets
(`liteavatar-renderer setup`). Run it with `liteavatar-renderer serve` or `python -m livekit_liteavatar.renderer`.
"""
