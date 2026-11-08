"""LiteAvatar's face decoder, re-created as a plain PyTorch module so it can run in ONNX Runtime.

Each presenter ships its decoder as TorchScript (net_decode.pt), which can't be exported to ONNX as is (its first
input is a list of tensors). The architecture is small: an MLP turns the 32 mouth parameters into a style vector;
three blocks each apply style-modulated instance norm and a residual 3x3 convolution, then upsample 2x with a
depthwise and a pointwise convolution and join the encoder's skip features; a last 3x3 convolution and tanh give the
mouth image. `Decoder` rebuilds exactly that from the TorchScript module's weights (bit-identical outputs), and
`OnnxDecoder` exports it in memory and runs it with ONNX Runtime.
"""

from __future__ import annotations

import io

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn


class Decoder(nn.Module):
    """A presenter's net_decode.pt as a plain module, from its state_dict."""

    def __init__(self, state_dict: dict[str, torch.Tensor]) -> None:
        super().__init__()
        self._names: dict[str, str] = {}
        for k, v in state_dict.items():
            name = k.replace(".", "_")
            self._names[k] = name
            self.register_buffer(name, v.detach().clone())

    def _w(self, key: str) -> torch.Tensor:
        return getattr(self, self._names[key])

    def _adain(self, i: int, x: torch.Tensor, style: torch.Tensor) -> torch.Tensor:
        b = f"decoder.adain_blocks.{i}"
        h = F.instance_norm(x, eps=1e-5)
        gamma = F.linear(style, self._w(f"{b}.AAD2.fc1.weight"), self._w(f"{b}.AAD2.fc1.bias"))[:, :, None, None]
        beta = F.linear(style, self._w(f"{b}.AAD2.fc2.weight"), self._w(f"{b}.AAD2.fc2.bias"))[:, :, None, None]
        h = torch.relu(gamma * h + beta)
        return F.conv2d(h, self._w(f"{b}.conv2.weight"), None, 1, 1) + x

    def _up(self, i: int, x: torch.Tensor) -> torch.Tensor:
        b = f"decoder.up_blocks.{i}"
        x = F.interpolate(x, scale_factor=2.0, mode="bilinear", align_corners=False)
        dw = self._w(f"{b}.dw_dbb.dbb_reparam.weight")
        x = F.leaky_relu(F.conv2d(x, dw, self._w(f"{b}.dw_dbb.dbb_reparam.bias"), 1, 1, groups=dw.shape[0]), 0.1)
        x = F.conv2d(x, self._w(f"{b}.pw.weight"))
        x = F.batch_norm(
            x,
            self._w(f"{b}.norm.running_mean"),
            self._w(f"{b}.norm.running_var"),
            self._w(f"{b}.norm.weight"),
            self._w(f"{b}.norm.bias"),
            False,
            0.1,
            1e-5,
        )
        return F.leaky_relu(x, 0.1)

    def forward(
        self, r0: torch.Tensor, r1: torch.Tensor, r2: torch.Tensor, r3: torch.Tensor, params: torch.Tensor
    ) -> torch.Tensor:
        """r0..r3: the encoder's features of a reference frame (3x384, 16x192, 32x96, 64x48); params: (1, 32)."""
        hidden = F.silu(F.linear(params, self._w("bs_proj.0.weight"), self._w("bs_proj.0.bias")))
        style = F.linear(hidden, self._w("bs_proj.2.weight"), self._w("bs_proj.2.bias"))
        x = torch.cat([self._up(0, self._adain(0, r3, style)), r2], 1)
        x = torch.cat([self._up(1, self._adain(1, x, style)), r1], 1)
        x = torch.cat([self._up(2, self._adain(2, x, style)), r0], 1)
        return torch.tanh(F.conv2d(x, self._w("decoder.conv.weight"), self._w("decoder.conv.bias"), 1, 1))


class OnnxDecoder:
    """The decoder in ONNX Runtime. Call it like the TorchScript one: (reference features, params) -> image."""

    def __init__(self, torchscript_decoder: torch.jit.ScriptModule, example_refs: list, threads: int) -> None:
        import onnxruntime as ort

        decoder = Decoder(torchscript_decoder.state_dict()).eval()
        params = torch.zeros(1, 32)
        buf = io.BytesIO()
        torch.onnx.export(
            decoder,
            (*example_refs, params),
            buf,
            input_names=["r0", "r1", "r2", "r3", "params"],
            output_names=["mouth"],
            opset_version=17,
            dynamo=False,
        )
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(buf.getvalue(), opts, providers=["CPUExecutionProvider"])
        self._numpy_refs: dict[int, dict[str, np.ndarray]] = {}

    def __call__(self, refs: list, params: torch.Tensor) -> torch.Tensor:
        key = id(refs)
        feeds = self._numpy_refs.get(key)
        if feeds is None:  # the reference features are fixed per idle frame: convert each once
            feeds = {f"r{i}": t.numpy() for i, t in enumerate(refs)}
            self._numpy_refs[key] = feeds
        (out,) = self._session.run(None, {**feeds, "params": params.numpy()})
        return torch.from_numpy(out)
