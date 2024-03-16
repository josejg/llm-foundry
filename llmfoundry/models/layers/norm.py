# Copyright 2022 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0

from typing import Dict, List, Optional, Type, Union

import torch


def _cast_if_autocast_enabled(tensor: torch.Tensor) -> torch.Tensor:
    if torch.is_autocast_enabled():
        if tensor.device.type == 'cuda':
            dtype = torch.get_autocast_gpu_dtype()
        elif tensor.device.type == 'cpu':
            dtype = torch.get_autocast_cpu_dtype()
        else:
            raise NotImplementedError()
        return tensor.to(dtype=dtype)
    return tensor


class LPLayerNorm(torch.nn.LayerNorm):

    def __init__(
        self,
        normalized_shape: Union[int, List[int], torch.Size],
        eps: float = 1e-05,
        elementwise_affine: bool = True,
        device: Optional[torch.device] = None,
        dtype: Optional[torch.dtype] = None,
    ):
        super().__init__(
            normalized_shape=normalized_shape,
            eps=eps,
            elementwise_affine=elementwise_affine,
            device=device,
            dtype=dtype,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        module_device = x.device
        downcast_x = _cast_if_autocast_enabled(x)
        downcast_weight = _cast_if_autocast_enabled(
            self.weight) if self.weight is not None else self.weight
        downcast_bias = _cast_if_autocast_enabled(
            self.bias) if self.bias is not None else self.bias
        with torch.autocast(enabled=False, device_type=module_device.type):
            return torch.nn.functional.layer_norm(
                downcast_x,
                self.normalized_shape,
                downcast_weight,
                downcast_bias,
                self.eps,
            )


def rms_norm(x: torch.Tensor,
             weight: Optional[torch.Tensor] = None,
             eps: float = 1e-5) -> torch.Tensor:
    output = x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + eps)
    if weight is not None:
        return output * weight
    return output

compiled_rms_norm = torch.compile(rms_norm)


class RMSNorm(torch.nn.Module):

    def __init__(
        self,
        normalized_shape: Union[int, List[int], torch.Size],
        eps: float = 1e-5,
        weight: bool = True,
        dtype: Optional[torch.dtype] = None,
        device: Optional[torch.device] = None,
    ):
        super().__init__()
        self.eps = eps
        if weight:
            self.weight = torch.nn.Parameter(
                torch.ones(normalized_shape, dtype=dtype, device=device))
        else:
            self.register_parameter('weight', None)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return rms_norm(x.float(), self.weight, self.eps).to(dtype=x.dtype)

class CompiledRMSNorm(RMSNorm):

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return compiled_rms_norm(x.float(), self.weight, self.eps).to(dtype=x.dtype)


class LPRMSNorm(RMSNorm):

    def __init__(
        self,
        normalized_shape: Union[int, List[int], torch.Size],
        eps: float = 1e-5,
        weight: bool = True,
        dtype: Optional[torch.dtype] = None,
        device: Optional[torch.device] = None,
    ):
        super().__init__(
            normalized_shape=normalized_shape,
            eps=eps,
            weight=weight,
            dtype=dtype,
            device=device,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        downcast_x = _cast_if_autocast_enabled(x)
        downcast_weight = _cast_if_autocast_enabled(
            self.weight) if self.weight is not None else self.weight
        with torch.autocast(enabled=False, device_type=x.device.type):
            return rms_norm(downcast_x, downcast_weight,
                            self.eps).to(dtype=x.dtype)

class CLPRMSNorm(LPRMSNorm):
 
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        downcast_x = _cast_if_autocast_enabled(x)
        downcast_weight = _cast_if_autocast_enabled(
            self.weight) if self.weight is not None else self.weight
        with torch.autocast(enabled=False, device_type=x.device.type):
            return compiled_rms_norm(downcast_x, downcast_weight,
                            self.eps).to(dtype=x.dtype)

import math

class NemoRMSNorm(torch.nn.Module):
    def __init__(self, 
        normalized_shape: Union[int, List[int], torch.Size],
        eps=1e-8,
        dtype: Optional[torch.dtype] = None,
        device: Optional[torch.device] = None,
     ):
        super().__init__()

        if isinstance(normalized_shape, int):
            dim = normalized_shape
        elif isinstance(normalized_shape, list):
            dim = math.prod(normalized_shape)
        elif isinstance(normalized_shape, torch.Size):
            dim = math.prod(normalized_shape)

        self.scale = dim ** -0.5
        self.eps = eps
        self.g = torch.nn.Parameter(
                torch.ones(dim, dtype=dtype, device=device)
        )

    def forward(self, x):
        norm = torch.norm(x, dim=-1, keepdim=True) * self.scale
        return x / norm.clamp(min=self.eps) * self.g


class CompiledNemoRMSNorm(NemoRMSNorm):

    @torch.compile
    def forward(self, x):
        norm = torch.norm(x, dim=-1, keepdim=True) * self.scale
        return x / norm.clamp(min=self.eps) * self.g


class LPNemoRMSNorm(NemoRMSNorm):

    def forward(self, x):
        downcast_x = _cast_if_autocast_enabled(x)
        downcast_g = _cast_if_autocast_enabled(self.g)
        with torch.autocast(enabled=False, device_type=x.device.type):
            norm = torch.norm(downcast_x, dim=-1, keepdim=True) * self.scale
            return downcast_x / norm.clamp(min=self.eps) * downcast_g

class CLPNemoRMSNorm(LPNemoRMSNorm):

    @torch.compile
    def forward(self, x):
        downcast_x = _cast_if_autocast_enabled(x)
        downcast_g = _cast_if_autocast_enabled(self.g)
        with torch.autocast(enabled=False, device_type=x.device.type):
            norm = torch.norm(downcast_x, dim=-1, keepdim=True) * self.scale
            return compiled_rms_norm(downcast_x / norm.clamp(min=self.eps) * downcast_g)


from flash_attn.ops.triton.layer_norm import rms_norm_fn

class TritonRMSNorm(torch.nn.Module):
    def __init__(
        self,
        normalized_shape: Union[int, List[int], torch.Size],
        eps: float = 1e-5,
        dropout_p: float = 0.0,
        dtype: Optional[torch.dtype] = None,
        device: Optional[torch.device] = None,
    ):
        if isinstance(normalized_shape, int):
            hidden_size = normalized_shape
        elif isinstance(normalized_shape, list):
            hidden_size = math.prod(normalized_shape)
        elif isinstance(normalized_shape, torch.Size):
            hidden_size = math.prod(normalized_shape)

        factory_kwargs = {"device": device, "dtype": dtype}
        super().__init__()
        self.eps = eps
        if dropout_p > 0.0:
            self.drop = torch.nn.Dropout(dropout_p)
        else:
            self.drop = None
        self.weight = torch.nn.Parameter(torch.empty(hidden_size, **factory_kwargs))
        self.register_parameter("bias", None)
        self.reset_parameters()

    def reset_parameters(self):
        torch.nn.init.ones_(self.weight)

    def forward(self, x, residual=None, prenorm=False, residual_in_fp32=False):
        return rms_norm_fn(
            x,
            self.weight,
            self.bias,
            residual=residual,
            eps=self.eps,
            dropout_p=self.drop.p if self.drop is not None and self.training else 0.0,
            prenorm=prenorm,
            residual_in_fp32=residual_in_fp32,
        )


NORM_CLASS_REGISTRY: Dict[str, Type[torch.nn.Module]] = {
    'layernorm': torch.nn.LayerNorm,
    'low_precision_layernorm': LPLayerNorm,
    'rmsnorm': RMSNorm,
    'low_precision_rmsnorm': LPRMSNorm,
    'compiled_rmsnorm': CompiledRMSNorm,
    'compiled_low_precision_rmsnorm': CLPRMSNorm,
    'nemo_rmsnorm': NemoRMSNorm,
    'compiled_nemo_rmsnorm': CompiledNemoRMSNorm,
    'low_precision_nemo_rmsnorm': LPNemoRMSNorm,
    'compiled_low_precision_nemo_rmsnorm': CLPNemoRMSNorm,
    'triton_rmsnorm': TritonRMSNorm,
}
